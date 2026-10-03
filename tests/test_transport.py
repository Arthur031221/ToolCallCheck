import contextlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from toolcall_check.probes import PROBES, _invoke_worker, _request_body


class EndpointServer:
    def __init__(self, mode):
        self.mode = mode
        self.count = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                outer.count += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if outer.mode == "slow":
                    time.sleep(2)
                    return
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/followed")
                    self.end_headers()
                    return
                if outer.mode == "http-error":
                    payload = b"server says broken"
                    self.send_response(503)
                elif outer.mode == "malformed":
                    payload = b'{"choices":[{"index":0,"message":}]} '
                    self.send_response(200)
                else:
                    payload = b"x" * 100
                    self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _config(base_url, *, timeout=5, limit=262144, api_key="transport-secret"):
    probe = PROBES[0]
    return {"mode": "single", "body": _request_body(probe, "test-model"), "base_url": base_url, "api_key": api_key, "socket_timeout": 2, "max_response_bytes": limit, "max_request_bytes": 65536}, timeout


class TransportTests(unittest.TestCase):
    def test_http_error_keeps_bounded_response_evidence(self):
        with EndpointServer("http-error") as endpoint:
            config, timeout = _config(endpoint.base_url)
            result = _invoke_worker(config, timeout)
            self.assertFalse(result["ok"])
            self.assertIn("HTTP 503", result["error"])
            self.assertIn("server says broken", result["traces"][0]["response"]["body"])

    def test_redirect_is_rejected_without_a_followup_request(self):
        with EndpointServer("redirect") as endpoint:
            config, timeout = _config(endpoint.base_url)
            result = _invoke_worker(config, timeout)
            self.assertFalse(result["ok"])
            self.assertIn("redirect response HTTP 302 rejected", result["error"])
            self.assertEqual(endpoint.count, 1)
            self.assertNotIn("Authorization", json.dumps(result["traces"]))
            self.assertNotIn("transport-secret", json.dumps(result["traces"]))

    def test_malformed_json_is_reported_with_raw_response_evidence(self):
        with EndpointServer("malformed") as endpoint:
            config, timeout = _config(endpoint.base_url)
            result = _invoke_worker(config, timeout)
            self.assertFalse(result["ok"])
            response = result["traces"][0]["response"]
            self.assertIn("invalid JSON", result["error"])
            self.assertIn('"message":}', response["body"])

    def test_response_byte_limit_is_enforced(self):
        with EndpointServer("large") as endpoint:
            config, timeout = _config(endpoint.base_url, limit=32)
            result = _invoke_worker(config, timeout)
            self.assertFalse(result["ok"])
            self.assertIn("32-byte limit", result["error"])

    def test_parent_wall_timeout_terminates_worker_and_returns(self):
        with EndpointServer("slow") as endpoint:
            config, _ = _config(endpoint.base_url, timeout=0.2)
            start = time.monotonic()
            result = _invoke_worker(config, 0.2)
            elapsed = time.monotonic() - start
            self.assertFalse(result["ok"])
            self.assertIn("wall-time limit", result["error"])
            self.assertTrue(result["traces"])
            self.assertIn("messages", json.dumps(result["traces"]))
            self.assertLess(elapsed, 1.8)


if __name__ == "__main__":
    unittest.main()
