"""Catalog routing must preserve the user's selected model."""

import io
import json
from unittest.mock import patch

import pytest

from kiro_proxy import server


def handler_for(model):
    handler = object.__new__(server.Handler)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Hi"}]}).encode()
    handler.path = "/v1/chat/completions"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.responses = []
    handler._json = lambda status, payload: handler.responses.append((status, payload))
    return handler


@pytest.mark.parametrize("model", [row["id"] for row in server.MODELS] + ["unavailable-future-model"])
def test_selection_reaches_conversation_payload_unchanged(model):
    with (
        patch.object(server, "build_conversation_state", return_value={}) as build,
        patch.object(server, "get_access_token", side_effect=RuntimeError("offline test")),
    ):
        handler_for(model).do_POST()
    assert build.call_args.args[1] == model


def test_listing_advertises_tool_support():
    handler = handler_for("auto")
    handler.path = "/v1/models"
    handler.do_GET()
    status, payload = handler.responses[0]
    assert status == 200
    assert {row["id"] for row in payload["data"]} == {row["id"] for row in server.MODELS}
    assert all("tool_calling" in row["capabilities"] for row in payload["data"])
