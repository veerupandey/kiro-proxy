#!/usr/bin/env python3
"""
kiro-proxy: OpenAI-compatible proxy for Kiro (IDE/CLI) subscription models.

- Reads Builder-ID tokens directly from kiro-cli's local secret store
  (~/.local/share/kiro-cli/data.sqlite3, table auth_kv) — always fresh,
  because kiro-cli refreshes them there.
- Refreshes tokens itself via the AWS OIDC device-flow endpoint when expired,
  and writes the refreshed token back so kiro-cli stays in sync.
- Exposes:
    GET  /v1/models
    POST /v1/chat/completions   (stream + non-stream)
  and forwards them to Kiro's CodeWhisperer generateAssistantResponse API
  (AWS event-stream protocol), translating to OpenAI format.

Stdlib only. Run:  python3 server.py [--port 8787]
"""

import argparse
import base64
import json
import sqlite3
import struct
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

KIRO_DB = Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"
TOKEN_KEY = "kirocli:odic:token"
DEVICE_KEY = "kirocli:odic:device-registration"


def _read_auth():
    conn = sqlite3.connect(f"file:{KIRO_DB}?mode=ro", uri=True)
    try:
        rows = dict(conn.execute("SELECT key, value FROM auth_kv"))
    finally:
        conn.close()
    token = json.loads(rows[TOKEN_KEY])
    device = json.loads(rows[DEVICE_KEY])
    return token, device


def _write_token(token):
    conn = sqlite3.connect(KIRO_DB)
    try:
        conn.execute(
            "UPDATE auth_kv SET value=? WHERE key=?",
            (json.dumps(token), TOKEN_KEY),
        )
        conn.commit()
    finally:
        conn.close()


def refresh_access_token(token, device):
    """Refresh via AWS SSO OIDC. Returns new token dict (also persisted)."""
    region = token.get("region", "us-east-1")
    url = f"https://oidc.{region}.amazonaws.com/token"
    payload = json.dumps(
        {
            "clientId": device["client_id"],
            "clientSecret": device["client_secret"],
            "grantType": "refresh_token",
            "refreshToken": token["refresh_token"],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read())
    new = dict(token)
    new["access_token"] = out["accessToken"]
    if out.get("refreshToken"):
        new["refresh_token"] = out["refreshToken"]
    new["expires_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + out.get("expiresIn", 28800))
    )
    try:
        _write_token(new)
    except Exception:
        pass  # best-effort sync back into kiro-cli's store
    return new


def _parse_exp(exp):
    """Parse ISO8601 expiry (handles fractional seconds + Z suffix)."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def get_access_token():
    """Return a live access token, refreshing if expired."""
    token, device = _read_auth()
    exp_ts = _parse_exp(token.get("expires_at", ""))
    # Refresh when <5min left, already expired, or expiry unparseable.
    if exp_ts - time.time() < 300:
        try:
            token = refresh_access_token(token, device)
        except Exception as e:
            if exp_ts and exp_ts - time.time() < 0:
                raise RuntimeError(f"token expired and refresh failed: {e}") from e
    return token["access_token"], token.get("region", "us-east-1")


# ---------------------------------------------------------------------------
# Kiro API (CodeWhisperer generateAssistantResponse, event-stream protocol)
# ---------------------------------------------------------------------------

import os

# Kiro IDE calls the Amazon Q Developer gateway (path-based operation, no
# X-Amz-Target). Confirmed from kiro.rs reference implementation:
#   https://q.{region}.amazonaws.com/generateAssistantResponse
KIRO_API_REGION = os.environ.get("KIRO_API_REGION", "us-east-1")
KIRO_API_URL = os.environ.get(
    "KIRO_API_URL",
    f"https://q.{KIRO_API_REGION}.amazonaws.com/generateAssistantResponse",
)
KIRO_VERSION = os.environ.get("KIRO_VERSION", "2.20.1")
MACHINE_ID = os.environ.get("KIRO_MACHINE_ID", str(uuid.uuid4()))


def kiro_endpoint(region):
    # Amazon Q gateway only exists in select regions (us-east-1 works for
    # ca-central-1 Kiro accounts; q.ca-central-1.amazonaws.com does not resolve).
    return KIRO_API_URL


def _user_input_message(content, model_id, tools=None, tool_results=None):
    """Minimal UserInputMessage that the current Kiro backend accepts.

    Verified by direct experiment: editorState, userIntent, and
    userInputMessageId must be omitted; profileArn must not be sent.
    """
    ctx = {}
    if tools:
        ctx["tools"] = tools
    if tool_results:
        ctx["toolResults"] = tool_results
    return {
        "userInputMessageContext": ctx,
        "content": content,
        "modelId": model_id,
        "origin": "AI_EDITOR",
    }


def _openai_tools_to_kiro(tools):
    """OpenAI tools[] -> Kiro [{toolSpecification: {name, description, inputSchema}}]."""
    kiro_tools = []
    for t in tools or []:
        if t.get("type") == "function":
            fn = t.get("function", {})
        else:  # allow bare {name, description, parameters}
            fn = t
        spec = {
            "toolSpecification": {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "inputSchema": {"json": fn.get("parameters", {"type": "object", "properties": {}})},
            }
        }
        kiro_tools.append(spec)
    return kiro_tools


def _openai_tool_calls_to_kiro_uses(tool_calls):
    """OpenAI assistant tool_calls -> Kiro toolUses [{toolUseId, name, input}]."""
    uses = []
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments", "")}
        uses.append(
            {
                "toolUseId": tc.get("id") or str(uuid.uuid4()),
                "name": fn.get("name", ""),
                "input": args,
            }
        )
    return uses


def _openai_tool_msg_to_kiro_result(m):
    """OpenAI role:'tool' message -> Kiro ToolResult."""
    content = m.get("content") or ""
    if isinstance(content, list):
        content = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return {
        "toolUseId": m.get("tool_call_id", ""),
        "content": [{"text": str(content)}],
        "status": "success",
    }


def build_conversation_state(messages, model_id=None, tools=None):
    """Translate OpenAI messages -> Kiro conversationState (kiro.rs schema).

    Handles: system, user, assistant (with/without tool_calls), tool results.
    Tool results attach to the userInputMessageContext of the message that
    follows them; if the conversation ends on tool results, the current
    message is a synthetic user turn carrying them.
    """
    kiro_tools = _openai_tools_to_kiro(tools)

    # Normalize messages into typed entries
    entries = []  # (kind, payload)
    system_text = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content") or ""
        if isinstance(content, list):  # multimodal blocks -> plain text
            content = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        if role == "system":
            system_text.append(content)
        elif role == "tool":
            entries.append(("tool_result", _openai_tool_msg_to_kiro_result(m)))
        elif role == "assistant":
            entries.append(("assistant", content, m.get("tool_calls") or []))
        else:
            entries.append(("user", content))

    if system_text and entries:
        kind, payload = entries[0]
        if kind == "user":
            entries[0] = ("user", "\n\n".join(system_text) + "\n\n" + payload)
        else:
            entries.insert(0, ("user", "\n\n".join(system_text)))
    elif system_text and not entries:
        entries.append(("user", "\n\n".join(system_text)))

    # Build history: tool results attach to the *next* user message context
    history = []
    pending_results = []
    for entry in entries[:-1]:
        kind = entry[0]
        if kind == "tool_result":
            pending_results.append(entry[1])
            continue
        if kind == "user":
            history.append(
                {
                    "userInputMessage": _user_input_message(
                        entry[1], model_id, tool_results=pending_results or None
                    )
                }
            )
            pending_results = []
        else:  # assistant
            content, tool_calls = entry[1], entry[2]
            resp = {
                "content": content,
                "messageId": str(uuid.uuid4()),
                "requestId": str(uuid.uuid4()),
            }
            uses = _openai_tool_calls_to_kiro_uses(tool_calls)
            if uses:
                resp["toolUses"] = uses
            history.append({"assistantResponseMessage": resp})

    # Current message (last entry)
    last = entries[-1] if entries else ("user", "")
    if last[0] == "tool_result":
        # conversation ends on tool results -> synthetic current turn
        pending_results.append(last[1])
        current = _user_input_message(
            "Tool results are attached. Continue.", model_id,
            tools=kiro_tools or None, tool_results=pending_results,
        )
    elif last[0] == "user":
        current = _user_input_message(
            last[1], model_id, tools=kiro_tools or None,
            tool_results=pending_results or None,
        )
    else:  # assistant last (unusual) -> echo as user continuation
        current = _user_input_message(
            "Continue.", model_id, tools=kiro_tools or None,
        )

    return {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "history": history,
            "currentMessage": {"userInputMessage": current},
        }
    }


def call_kiro(payload, access_token, region):
    """POST to Kiro; returns a file-like streaming response.

    App identity is critical: the Power subscription is bound to the
    Kiro CLI app, so User-Agent must carry app/AmazonQ-For-CLI (verified
    by experiment — KiroIDE identity gets 403 'subscription does not
    support this application'). profileArn must NOT be sent.
    """
    req = urllib.request.Request(
        kiro_endpoint(region),
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.amazon.eventstream",
            "x-amzn-codewhisperer-optout": "true",
            "x-amzn-kiro-agent-mode": "spec",
            "x-amz-user-agent": (
                "aws-sdk-rust/1.3.9 ua/2.1 api/codewhispererstreaming/1.0.0 "
                "os/linux lang/rust/1.87.0 m/E app/AmazonQ-For-CLI"
            ),
            "User-Agent": (
                f"KiroCLI/{KIRO_VERSION} KAS/{KIRO_VERSION} os/linux "
                f"md/appVersion-{KIRO_VERSION} app/AmazonQ-For-CLI"
            ),
            "amz-sdk-invocation-id": str(uuid.uuid4()),
            "amz-sdk-request": "attempt=1; max=3",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=300)


# ---------------------------------------------------------------------------
# AWS event-stream decoding
# ---------------------------------------------------------------------------

def _parse_headers(buf, start, end):
    headers = {}
    pos = start
    while pos < end:
        nlen = buf[pos]
        pos += 1
        name = buf[pos : pos + nlen].decode("utf-8", "replace")
        pos += nlen
        t = buf[pos]
        pos += 1
        if t == 7:  # string
            vlen = int.from_bytes(buf[pos : pos + 2], "big")
            pos += 2
            headers[name] = buf[pos : pos + vlen].decode("utf-8", "replace")
            pos += vlen
        elif t == 6:  # int64
            headers[name] = int.from_bytes(buf[pos : pos + 8], "big")
            pos += 8
        elif t == 5:  # int32
            headers[name] = int.from_bytes(buf[pos : pos + 4], "big")
            pos += 4
        elif t == 2:
            headers[name] = True
        elif t == 3:
            headers[name] = False
        elif t == 8:  # timestamp
            headers[name] = int.from_bytes(buf[pos : pos + 8], "big")
            pos += 8
        elif t == 4:  # uuid
            headers[name] = base64.b64encode(buf[pos : pos + 16]).decode()
            pos += 16
        elif t == 1:  # byte
            headers[name] = buf[pos]
            pos += 1
        else:
            raise ValueError(f"unknown header type {t}")
    return headers


class EventStreamDecoder:
    """Incremental decoder for application/vnd.amazon.eventstream."""

    def __init__(self):
        self.buf = b""

    def feed(self, chunk):
        """Yield (headers, payload) for every complete message in chunk."""
        self.buf += chunk
        while True:
            if len(self.buf) < 16:
                return
            total_len = int.from_bytes(self.buf[0:4], "big")
            if total_len < 16 or total_len > 64 * 1024 * 1024:
                self.buf = b""
                return
            if len(self.buf) < total_len:
                return
            headers_len = int.from_bytes(self.buf[4:8], "big")
            headers = _parse_headers(self.buf, 12, 12 + headers_len)
            payload = self.buf[12 + headers_len : total_len - 4]
            self.buf = self.buf[total_len:]
            yield headers, payload


def extract_content(headers, payload):
    """Return text delta if this event carries assistant content."""
    etype = headers.get(":event-type", "")
    if etype not in ("assistantResponseEvent", "assistantResponse"):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data.get("content")


def extract_tool_use(headers, payload):
    """Return tool-use dict if this event is a toolUseEvent.

    Kiro toolUseEvent: {name, toolUseId, input (partial JSON str), stop}.
    """
    etype = headers.get(":event-type", "")
    if etype not in ("toolUseEvent", "toolUse"):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    return {
        "name": data.get("name", ""),
        "tool_use_id": data.get("toolUseId", ""),
        "input": data.get("input", ""),
        "stop": bool(data.get("stop", False)),
    }


# ---------------------------------------------------------------------------
# OpenAI facade
# ---------------------------------------------------------------------------

MODELS = [
    {"id": "auto", "kiro": "auto"},
    # Experimental choices may require account/organization enablement.
    {"id": "claude-sonnet-5.5", "kiro": "claude-sonnet-5.5"},
    {"id": "claude-opus-5.5", "kiro": "claude-opus-5.5"},
    {"id": "claude-opus-4.8", "kiro": "claude-opus-4.8"},
    {"id": "claude-opus-4.7", "kiro": "claude-opus-4.7"},
    {"id": "claude-opus-4.6", "kiro": "claude-opus-4.6"},
    {"id": "claude-sonnet-4.6", "kiro": "claude-sonnet-4.6"},
    {"id": "claude-opus-4.5", "kiro": "claude-opus-4.5"},
    {"id": "claude-sonnet-4.5", "kiro": "claude-sonnet-4.5"},
    {"id": "claude-haiku-4.5", "kiro": "claude-haiku-4.5"},
]
DEFAULT_KIRO_MODEL = "claude-sonnet-4.6"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[kiro-proxy] {self.address_string()} {fmt % args}", flush=True)

    # -- helpers ------------------------------------------------------------
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunk: dict):
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": m["id"],
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "kiro",
                        }
                        for m in MODELS
                    ],
                },
            )
        elif self.path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            self._json(400, {"error": {"message": f"bad request: {e}"}})
            return

        messages = req.get("messages", [])
        stream = bool(req.get("stream", False))
        model = req.get("model", "claude-sonnet-4.6")
        # Unknown names must reach upstream unchanged, never a different model.
        kiro_model = next((m["kiro"] for m in MODELS if m["id"] == model), model)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        payload = build_conversation_state(
            messages, kiro_model, tools=req.get("tools")
        )
        try:
            access_token, region = get_access_token()
            resp = call_kiro(payload, access_token, region)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            self._json(
                e.code,
                {"error": {"message": f"kiro upstream error: {detail}", "type": "upstream_error"}},
            )
            return
        except Exception as e:
            self._json(502, {"error": {"message": f"kiro call failed: {e}", "type": "proxy_error"}})
            return

        with resp:
            # NOTE: Kiro returns the event-stream body even when the
            # Content-Type header says "application/json" (verified by
            # experiment6.py), so ALWAYS decode via the event-stream path.
            if True:
                self._stream_response(resp, stream, completion_id, model)
            elif False:
                # Non-stream JSON response (some regions return plain JSON)
                try:
                    data = json.loads(resp.read())
                    text = data.get("content") or data.get("assistantResponse", {}).get("content", "")
                except Exception:
                    text = ""
                self._json(
                    200,
                    {
                        "id": completion_id,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    },
                )

    def _stream_response(self, resp, stream, completion_id, model):
        decoder = EventStreamDecoder()
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        else:
            parts = []

        tool_uses = {}   # toolUseId -> {"name": str, "input_parts": [str]}
        tool_order = []

        def emit(delta_text, finish=None, tool_calls=None):
            delta = {}
            if delta_text:
                delta["content"] = delta_text
            if tool_calls:
                delta["tool_calls"] = tool_calls
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ],
            }
            if stream:
                data = b"data: " + json.dumps(chunk).encode() + b"\n\n"
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            else:
                if delta_text:
                    parts.append(delta_text)

        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                for headers, payload in decoder.feed(chunk):
                    text = extract_content(headers, payload)
                    if text:
                        emit(text)
                    tu = extract_tool_use(headers, payload)
                    if tu:
                        tu_id = tu["tool_use_id"] or f"tu-{len(tool_order)}"
                        if tu_id not in tool_uses:
                            tool_uses[tu_id] = {"name": tu["name"], "input_parts": []}
                            tool_order.append(tu_id)
                        entry = tool_uses[tu_id]
                        if tu["name"]:
                            entry["name"] = tu["name"]
                        if tu["input"]:
                            entry["input_parts"].append(tu["input"])
                        if tu["stop"]:
                            args = "".join(entry["input_parts"]) or "{}"
                            idx = tool_order.index(tu_id)
                            if stream:
                                emit("", tool_calls=[{
                                    "index": idx,
                                    "id": tu_id,
                                    "type": "function",
                                    "function": {"name": entry["name"], "arguments": args},
                                }])
            finish = "tool_calls" if tool_order else "stop"
            emit("", finish=finish)
            if stream:
                tail = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(tail):X}\r\n".encode() + tail + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return

        if not stream:
            message = {"role": "assistant", "content": "".join(parts) or None}
            if tool_order:
                message["tool_calls"] = [
                    {
                        "id": tu_id,
                        "type": "function",
                        "function": {
                            "name": tool_uses[tu_id]["name"],
                            "arguments": "".join(tool_uses[tu_id]["input_parts"]) or "{}",
                        },
                    }
                    for tu_id in tool_order
                ]
            self._json(
                200,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if tool_order else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                },
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    # sanity-check credentials at startup
    try:
        token, _ = _read_auth()
        print(f"[kiro-proxy] found kiro token (region={token.get('region')}, expires {token.get('expires_at')})", flush=True)
    except Exception as e:
        print(f"[kiro-proxy] WARNING: could not read kiro credentials: {e}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[kiro-proxy] listening on http://{args.host}:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
