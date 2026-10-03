from __future__ import annotations

import http.client
import json
import os
import secrets
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from toolcall_check.core import CheckError, SSEParser, extract_nonstream_tool, extract_stream_tool, strict_json, exact_equal  # noqa: E402


class TraceFailure(Exception):
    def __init__(self, message: str, traces: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.traces = traces


def _read_bounded(response: http.client.HTTPResponse, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(8192, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise CheckError(f"response exceeded the {limit}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _connection(parts: Any, timeout: float) -> http.client.HTTPConnection:
    host = parts.hostname
    if not host:
        raise CheckError("base URL must include a hostname")
    port = parts.port
    if parts.scheme == "https":
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(host, port=port, timeout=timeout)


def _url_parts(base_url: str) -> Any:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise CheckError("base URL scheme must be http or https")
    if not parts.hostname or parts.username is not None or parts.password is not None or "?" in base_url or "#" in base_url:
        raise CheckError("base URL must omit userinfo, query, and fragment")
    try:
        _ = parts.port
    except ValueError as exc:
        raise CheckError("base URL has an invalid port") from exc
    return parts


def _endpoint_path(parts: Any) -> str:
    return (parts.path.rstrip("/") or "") + "/chat/completions"


def _single_request(config: dict[str, Any], body: dict[str, Any], stream: bool, traces: list[dict[str, Any]]) -> Any:
    base_url = config["base_url"]
    parts = _url_parts(base_url)
    timeout = float(config.get("socket_timeout", 15))
    limit = int(config.get("max_response_bytes", 262144))
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
    key = config.get("api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > int(config.get("max_request_bytes", 65536)):
        raise CheckError("request exceeded the configured byte limit")
    trace: dict[str, Any] = {"request": {"method": "POST", "url": base_url.rstrip("/") + "/chat/completions", "headers": {"content-type": "application/json", "accept": headers["Accept"]}, "body": body}}
    traces.append(trace)
    conn = _connection(parts, timeout)
    try:
        conn.request("POST", _endpoint_path(parts), body=encoded, headers=headers)
        response = conn.getresponse()
        response_headers = {k.lower(): v for k, v in response.getheaders() if k.lower() in {"content-type", "content-length", "transfer-encoding"}}
        response_trace: dict[str, Any] = {"status": response.status, "headers": response_headers}
        trace["response"] = response_trace
        if 300 <= response.status < 400:
            raw = _read_bounded(response, limit)
            response_trace["body"] = raw.decode("utf-8", errors="replace")
            raise TraceFailure(f"redirect response HTTP {response.status} rejected", traces)
        if response.status < 200 or response.status >= 300:
            raw = _read_bounded(response, limit)
            response_trace["body"] = raw.decode("utf-8", errors="replace")
            raise TraceFailure(f"endpoint returned HTTP {response.status}", traces)
        if stream:
            if response_headers.get("content-type", "").split(chr(59), 1)[0].strip().lower() != "text/event-stream":
                raise CheckError("stream response Content-Type must be text/event-stream")
            parser = SSEParser()
            events: list[str] = []
            raw_chunks: list[bytes] = []
            total = 0
            done_seen = False
            try:
                while True:
                    chunk = response.read1(4096)
                    if not chunk:
                        events.extend(parser.feed(b"", final=True))
                        break
                    total += len(chunk)
                    if total > limit:
                        raise CheckError(f"response exceeded the {limit}-byte limit")
                    raw_chunks.append(chunk)
                    parsed_events = parser.feed(chunk)
                    events.extend(parsed_events)
                    if "[DONE]" in parsed_events:
                        done_seen = True
                        if parser.has_pending_data():
                            raise CheckError("stream contained data after [DONE]")
                        break
            except (CheckError, OSError, http.client.HTTPException):
                response_trace["body"] = b"".join(raw_chunks).decode("utf-8", errors="replace")
                raise
            response_trace["body"] = b"".join(raw_chunks).decode("utf-8", errors="replace")
            if not done_seen and "[DONE]" not in events:
                raise CheckError("stream ended without [DONE]")
            call, arguments, event_trace = extract_stream_tool(events, body["tools"][0]["function"]["name"])
            response_trace["events"] = event_trace
            return {"tool_call": call, "arguments": arguments}
        raw = _read_bounded(response, limit)
        response_trace["body"] = raw.decode("utf-8", errors="replace")
        decoded = raw.decode("utf-8", errors="strict")
        return strict_json(decoded)
    except TraceFailure:
        raise
    except CheckError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
        raise TraceFailure(f"request failed: {type(exc).__name__}: {exc}", traces) from exc
    finally:
        conn.close()


def execute(config: dict[str, Any]) -> dict[str, Any]:
    mode = config["mode"]
    traces: list[dict[str, Any]] = []
    try:
        body = config["body"]
        if mode == "stream":
            result = _single_request(config, body, True, traces)
            return {"ok": True, "result": result, "traces": traces}
        first = _single_request(config, body, False, traces)
        first_call, first_args = extract_nonstream_tool(first, body["tools"][0]["function"]["name"])
        if mode == "single":
            return {"ok": True, "result": {"tool_call": first_call, "arguments": first_args}, "traces": traces}
        if mode != "roundtrip":
            raise CheckError("unknown worker mode")
        if "expected" in config:
            exact_equal(config["expected"], first_args)
        sentinel = "toolcall-check-" + secrets.token_hex(20)
        function = first_call["function"]
        original_message = first["choices"][0]["message"]
        assistant_message = {"role": "assistant", "tool_calls": [first_call]}
        if "content" in original_message:
            assistant_message["content"] = original_message["content"]
        second_messages = list(body["messages"]) + [
            assistant_message,
            {"role": "tool", "tool_call_id": first_call["id"], "name": function["name"], "content": json.dumps({"result": sentinel})},
        ]
        second_body = {
            "model": body["model"],
            "messages": second_messages,
            "tools": body["tools"],
            "tool_choice": "none",
            "stream": False,
        }
        second = _single_request(config, second_body, False, traces)
        choices = second.get("choices") if isinstance(second, dict) else None
        if not isinstance(choices, list) or len(choices) != 1:
            raise CheckError("round trip expected exactly one final assistant choice")
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
            raise CheckError("round-trip choice index must be integer 0")
        if choice.get("finish_reason") != "stop":
            raise CheckError("round trip expected finish_reason stop")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise CheckError("round trip did not return a normal assistant message")
        if message.get("refusal"):
            raise CheckError("assistant refused the round-trip request")
        if (message.get("tool_calls") is not None and message.get("tool_calls") != []) or message.get("function_call") is not None:
            raise CheckError("round trip returned another tool call")
        if message.get("content") != sentinel:
            raise CheckError("round-trip final message did not exactly match the local result sentinel")
        return {"ok": True, "result": {"tool_call": first_call, "arguments": first_args, "final_content": message["content"]}, "traces": traces}
    except TraceFailure as exc:
        return {"ok": False, "error": str(exc), "traces": exc.traces}
    except (CheckError, KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "traces": traces}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        result = execute(request)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": f"worker failed: {type(exc).__name__}", "traces": []}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
