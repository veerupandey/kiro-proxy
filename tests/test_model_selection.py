"""Exercise model routing over the proxy's real HTTP boundary."""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kiro_proxy import server


class ModelSelectionTests(unittest.TestCase):
    def test_unknown_model_is_never_substituted(self):
        observed = []

        def upstream(payload, *_):
            observed.append(payload["conversationState"]["currentMessage"]["userInputMessage"]["modelId"])
            raise RuntimeError("upstream rejected the requested model")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(server, "get_access_token", return_value=("fake", "us-east-1")), patch.object(server, "call_kiro", side_effect=upstream):
                for model in ["claude-opus-4.7", "unavailable-future-model"]:
                    req = Request(f"http://127.0.0.1:{httpd.server_port}/v1/chat/completions",
                                  data=json.dumps({"model": model, "messages": [{"role": "user", "content": "Hello"}]}).encode(),
                                  headers={"Content-Type": "application/json"})
                    with self.assertRaises(HTTPError) as error:
                        urlopen(req, timeout=5)
                    self.assertEqual(error.exception.code, 502)
                self.assertEqual(observed, ["claude-opus-4.7", "unavailable-future-model"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)
