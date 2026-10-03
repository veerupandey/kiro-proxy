#!/usr/bin/env python3
"""Multi-turn + tool-calling test against kiro-proxy (OpenAI-compatible).

Flow:
  Turn 1 (non-stream): user asks a question that requires the get_weather tool.
    -> expect finish_reason="tool_calls" with a get_weather call.
  Turn 2 (non-stream): send back the tool result; expect a final text answer
    that uses the returned data.
  Turn 3 (stream): new conversation with a calculator tool; expect streamed
    tool_calls deltas, then feed the result and get a streamed final answer.
"""

import json
import urllib.request

BASE = "http://127.0.0.1:8807/v1/chat/completions"
MODEL = "claude-sonnet-4.6"

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
}

CALC_TOOL = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate a simple arithmetic expression, e.g. '23*7+19'",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
}


def chat(messages, tools=None, stream=False):
    body = {"model": MODEL, "messages": messages, "stream": stream}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=300)
    if stream:
        return resp  # caller iterates SSE lines
    return json.loads(resp.read())


def read_stream(resp):
    """Parse SSE stream -> (text, tool_calls list, finish_reason)."""
    text, tool_calls, finish = "", [], None
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            for line in raw.split(b"\n"):
                if not line.startswith(b"data: "):
                    continue
                data = line[6:]
                if data == b"[DONE]":
                    return text, tool_calls, finish
                obj = json.loads(data)
                delta = obj["choices"][0]["delta"]
                if "content" in delta and delta["content"]:
                    text += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    while len(tool_calls) <= tc["index"]:
                        tool_calls.append({"id": "", "function": {"name": "", "arguments": ""}})
                    e = tool_calls[tc["index"]]
                    e["id"] = tc.get("id") or e["id"]
                    e["type"] = "function"
                    e["function"]["name"] = (tc.get("function") or {}).get("name") or e["function"]["name"]
                    e["function"]["arguments"] += (tc.get("function") or {}).get("arguments") or ""
                if obj["choices"][0].get("finish_reason"):
                    finish = obj["choices"][0]["finish_reason"]
    return text, tool_calls, finish


def main():
    results = []

    # ---- Turn 1: model should call get_weather (non-stream) ----
    msgs = [
        {"role": "system", "content": "You are a helpful assistant. Use tools when available."},
        {"role": "user", "content": "What's the weather in Paris right now? Use the get_weather tool."},
    ]
    out = chat(msgs, tools=[WEATHER_TOOL])
    msg = out["choices"][0]["message"]
    finish = out["choices"][0]["finish_reason"]
    print("== Turn 1 (non-stream, expect tool_calls) ==")
    print("finish_reason:", finish)
    print("content:", (msg.get("content") or "")[:200])
    print("tool_calls:", json.dumps(msg.get("tool_calls"), indent=2))
    ok1 = finish == "tool_calls" and msg.get("tool_calls")
    if ok1:
        tc = msg["tool_calls"][0]
        ok1 = tc["function"]["name"] == "get_weather" and "city" in tc["function"]["arguments"]
    results.append(("turn1: model returns tool_calls", ok1))
    if not ok1:
        print("TURN 1 FAILED - aborting")
        return results

    # ---- Turn 2: feed tool result, expect final answer (non-stream) ----
    tc = msg["tool_calls"][0]
    msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg["tool_calls"]})
    msgs.append({"role": "tool", "tool_call_id": tc["id"],
                 "content": json.dumps({"city": "Paris", "temperature": 18, "unit": "celsius", "condition": "light rain"})})
    try:
        out2 = chat(msgs, tools=[WEATHER_TOOL])
        m2 = out2["choices"][0]["message"]
        print("\n== Turn 2 (non-stream, expect final answer using tool data) ==")
        print("finish_reason:", out2["choices"][0]["finish_reason"])
        print("content:", (m2.get("content") or "")[:400])
        answer = (m2.get("content") or "").lower()
        ok2 = "18" in answer and ("rain" in answer or "paris" in answer)
    except Exception as e:
        print("\n== Turn 2 (non-stream, expect final answer using tool data) ==")
        print("ERROR:", type(e).__name__, str(e)[:300])
        ok2 = False
    results.append(("turn2: final answer uses tool result", ok2))

    # ---- Turn 3: streaming with calculator tool ----
    msgs3 = [
        {"role": "user", "content": "What is 23*7+19? Use the calculator tool."},
    ]
    try:
        resp = chat(msgs3, tools=[CALC_TOOL], stream=True)
        text3, tcs3, finish3 = read_stream(resp)
    except Exception as e:
        print("\n== Turn 3 (stream, expect streamed tool_calls) ==")
        print("ERROR:", type(e).__name__, str(e)[:300])
        text3, tcs3, finish3 = "", [], None
    print("\n== Turn 3 (stream, expect streamed tool_calls) ==")
    print("finish_reason:", finish3)
    print("text:", text3[:200])
    print("tool_calls:", json.dumps(tcs3, indent=2))
    ok3 = finish3 == "tool_calls" and tcs3 and tcs3[0]["function"]["name"] == "calculator"
    results.append(("turn3: streaming tool_calls", ok3))

    if ok3:
        # ---- Turn 4: feed calc result, expect streamed final answer ----
        tc3 = tcs3[0]
        msgs3.append({"role": "assistant", "content": text3 or "", "tool_calls": tcs3})
        msgs3.append({"role": "tool", "tool_call_id": tc3["id"], "content": "180"})
        try:
            resp4 = chat(msgs3, tools=[CALC_TOOL], stream=True)
            text4, tcs4, finish4 = read_stream(resp4)
        except Exception as e:
            print("\n== Turn 4 (stream, expect final answer 180) ==")
            print("ERROR:", type(e).__name__, str(e)[:300])
            text4, tcs4, finish4 = "", [], None
        print("\n== Turn 4 (stream, expect final answer 180) ==")
        print("finish_reason:", finish4)
        print("text:", text4[:300])
        ok4 = finish4 == "stop" and "180" in text4
        results.append(("turn4: streamed final answer uses tool result", ok4))

    print("\n==== RESULTS ====")
    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    return results


if __name__ == "__main__":
    main()
