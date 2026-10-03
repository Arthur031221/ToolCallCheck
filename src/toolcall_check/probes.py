from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core import CheckError, exact_equal, sanitize

NESTED_EXPECTED = {
    "ticket": {"id": 4821, "resolved": False, "labels": ["endpoint", "streaming"]},
    "metrics": {"confidence": 0.875, "duration_ms": 125},
    "source": "toolcall-check",
}

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Return the supplied message unchanged.",
        "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"], "additionalProperties": False},
    },
}

NESTED_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_ticket",
        "description": "Return the supplied ticket record unchanged.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "resolved": {"type": "boolean"},
                        "labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "resolved", "labels"],
                    "additionalProperties": False,
                },
                "metrics": {
                    "type": "object",
                    "properties": {"confidence": {"type": "number"}, "duration_ms": {"type": "integer"}},
                    "required": ["confidence", "duration_ms"],
                    "additionalProperties": False,
                },
                "source": {"type": "string"},
            },
            "required": ["ticket", "metrics", "source"],
            "additionalProperties": False,
        },
    },
}

PROBES = [
    {"name": "Forced echo call", "kind": "single", "stream": False, "tool": ECHO_SCHEMA, "expected": {"message": "forced-ready"}, "prompt": "Call echo exactly once with message forced-ready."},
    {"name": "Forced nested JSON call", "kind": "single", "stream": False, "tool": NESTED_SCHEMA, "expected": NESTED_EXPECTED, "prompt": "Call submit_ticket exactly once with this exact payload: " + json.dumps(NESTED_EXPECTED, separators=(",", ":"))},
    {"name": "Streaming echo", "kind": "stream", "stream": True, "tool": ECHO_SCHEMA, "expected": {"message": "stream-ready"}, "prompt": "Call echo exactly once with message stream-ready."},
    {"name": "Streaming nested JSON", "kind": "stream", "stream": True, "tool": NESTED_SCHEMA, "expected": NESTED_EXPECTED, "prompt": "Call submit_ticket exactly once with this exact payload: " + json.dumps(NESTED_EXPECTED, separators=(",", ":"))},
    {"name": "Two-turn echo round trip", "kind": "roundtrip", "stream": False, "tool": ECHO_SCHEMA, "expected": {"message": "roundtrip-ready"}, "prompt": "Call echo exactly once with message roundtrip-ready. After the local tool result arrives, return its result string exactly with no extra words and no further tool call."},
]


def _request_body(probe: dict[str, Any], model: str) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": "You are being checked for exact tool-call behavior. Follow the user's request exactly."},
        {"role": "user", "content": probe["prompt"]},
    ]
    return {
        "model": model,
        "messages": messages,
        "tools": [probe["tool"]],
        "tool_choice": {"type": "function", "function": {"name": probe["tool"]["function"]["name"]}},
        "stream": probe["stream"],
    }


def _invoke_worker(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    worker = Path(__file__).with_name("worker.py")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        [sys.executable, str(worker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        close_fds=True,
    )
    payload = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_trace = {
        "request": {
            "method": "POST",
            "url": str(config.get("base_url", "")).rstrip("/") + "/chat/completions",
            "headers": {"content-type": "application/json", "accept": "text/event-stream" if config.get("body", {}).get("stream") else "application/json"},
            "body": config.get("body", {}),
        }
    }
    try:
        stdout, _stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, _stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _stderr = process.communicate()
        message = f"request exceeded the {timeout:g}-second wall-time limit"
        request_trace["response"] = {"status": "unknown", "error": message}
        return {"ok": False, "error": message, "traces": [request_trace]}
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    if process.returncode != 0:
        message = f"worker exited with status {process.returncode}"
        request_trace["response"] = {"status": "unknown", "error": message}
        return {"ok": False, "error": message, "traces": [request_trace]}
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        request_trace["response"] = {"status": "unknown", "error": "worker returned malformed output"}
        return {"ok": False, "error": "worker returned malformed output", "traces": [request_trace]}
    if not isinstance(value, dict):
        request_trace["response"] = {"status": "unknown", "error": "worker returned a non-object result"}
        return {"ok": False, "error": "worker returned a non-object result", "traces": [request_trace]}
    return value


def run_probes(base_url: str, model: str, api_key: str | None, synthetic: bool = False) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for probe in PROBES:
        body = _request_body(probe, model)
        config = {
            "mode": probe["kind"],
            "expected": probe["expected"],
            "body": body,
            "base_url": base_url,
            "api_key": api_key,
            "socket_timeout": 15,
            "max_response_bytes": 262144,
            "max_request_bytes": 65536,
        }
        timeout = 40 if probe["kind"] == "roundtrip" else 20
        outcome = _invoke_worker(config, timeout)
        trace_rows = outcome.get("traces", [])
        if isinstance(trace_rows, list):
            for row in trace_rows:
                traces.append({"probe": probe["name"], **row})
        passed = bool(outcome.get("ok"))
        received: Any = None
        error = outcome.get("error")
        if passed:
            payload = outcome.get("result", {})
            received = payload.get("arguments")
            try:
                exact_equal(probe["expected"], received)
                if probe["kind"] == "roundtrip" and not isinstance(payload.get("final_content"), str):
                    raise CheckError("round-trip final message was not a string")
            except CheckError as exc:
                passed = False
                error = str(exc)
        results.append({
            "name": probe["name"],
            "passed": passed,
            "expected": probe["expected"],
            "received": received,
            "final_content": outcome.get("result", {}).get("final_content") if probe["kind"] == "roundtrip" and isinstance(outcome.get("result"), dict) else None,
            "error": error,
            "synthetic": synthetic,
        })
    return {"results": results, "traces": traces, "synthetic": synthetic, "model": model}
