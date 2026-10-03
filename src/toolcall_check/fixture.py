from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .probes import NESTED_EXPECTED


def _sse_event(value: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\r\n\r\n"


class DemoServer:
    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    request = json.loads(self.rfile.read(length))
                    body = outer.respond(request)
                except Exception:
                    self.send_error(400)
                    return
                if body["stream"]:
                    chunks = body["chunks"]
                    encoded = b"".join(chunks)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    for start in range(0, len(encoded), 13):
                        self.wfile.write(encoded[start : start + 13])
                        self.wfile.flush()
                else:
                    encoded = json.dumps(body["value"], separators=(",", ":")).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="toolcall-check-demo", daemon=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "DemoServer":
        self.thread.start()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("demo server thread did not stop")

    def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("messages", [])
        choice = request.get("tool_choice")
        if choice == "none":
            tool_message = next(message for message in messages if message.get("role") == "tool")
            result = json.loads(tool_message["content"])["result"]
            return {"stream": False, "value": {"choices": [{"index": 0, "message": {"role": "assistant", "content": result}, "finish_reason": "stop"}]}}
        name = choice["function"]["name"]
        prompt = next(message["content"] for message in messages if message.get("role") == "user")
        if name == "echo":
            if "forced-ready" in prompt:
                arguments = {"message": "forced-ready"}
            elif "stream-ready" in prompt:
                arguments = {"message": "stream-ready"}
            else:
                arguments = {"message": "roundtrip-ready"}
        else:
            arguments = dict(NESTED_EXPECTED)
            if self.broken:
                arguments["metrics"] = dict(arguments["metrics"], confidence=0.87)
        arguments_text = json.dumps(arguments, separators=(",", ":"))
        call = {"id": "call_synthetic_01", "type": "function", "function": {"name": name, "arguments": arguments_text}}
        if not request.get("stream"):
            return {"stream": False, "value": {"choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}]}}
        chunks = [
            _sse_event({"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": call["id"], "type": "function", "function": {"name": name, "arguments": ""}}]}, "finish_reason": None}]}),
        ]
        fragments = [arguments_text[: max(1, len(arguments_text) // 2)], arguments_text[max(1, len(arguments_text) // 2) :]]
        for fragment in fragments:
            chunks.append(_sse_event({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": fragment}}]}, "finish_reason": None}]}))
        chunks.append(_sse_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}))
        chunks.append(b"data: [DONE]\r\n\r\n")
        return {"stream": True, "chunks": chunks}


def demo_server(broken: bool = False) -> DemoServer:
    return DemoServer(broken=broken)
