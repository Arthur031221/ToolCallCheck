from __future__ import annotations

import codecs
import json
import math
import re
from typing import Any


class CheckError(Exception):
    pass


def strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CheckError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise CheckError(f"invalid JSON constant: {value}")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise CheckError("non-finite JSON number")
        return number

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    except RecursionError as exc:
        raise CheckError("JSON nesting exceeds the parser limit") from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, CheckError):
            raise
        raise CheckError(f"invalid JSON: {exc}") from exc


def exact_equal(expected: Any, actual: Any, path: str = "$") -> None:
    if type(expected) is not type(actual):
        raise CheckError(f"{path}: expected {type(expected).__name__}, received {type(actual).__name__}")
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            raise CheckError(f"{path}: keys differ, expected {sorted(expected)}, received {sorted(actual)}")
        for key in expected:
            exact_equal(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            raise CheckError(f"{path}: expected {len(expected)} items, received {len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            exact_equal(left, right, f"{path}[{index}]")
    elif expected != actual:
        raise CheckError(f"{path}: expected {expected!r}, received {actual!r}")


class SSEParser:
    """Incremental event framing for CRLF, LF, CR, comments and UTF-8 chunks."""

    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.line = ""
        self.pending_cr = False
        self.data: list[str] = []

    def feed(self, chunk: bytes, final: bool = False) -> list[str]:
        try:
            text = self.decoder.decode(chunk, final=final)
        except UnicodeDecodeError as exc:
            raise CheckError("stream contains invalid UTF-8") from exc
        events: list[str] = []
        for char in text:
            if self.pending_cr:
                self.pending_cr = False
                events.extend(self._line())
                if char == "\n":
                    continue
            if char == "\r":
                self.pending_cr = True
            elif char == "\n":
                events.extend(self._line())
            else:
                self.line += char
        if final:
            if self.pending_cr:
                self.pending_cr = False
                events.extend(self._line())
            # An unterminated final line is intentionally not dispatched.
        return events

    def _line(self) -> list[str]:
        line, self.line = self.line, ""
        if line == "":
            if not self.data:
                return []
            event = "\n".join(self.data)
            self.data = []
            return [event]
        if line.startswith(":"):
            return []
        field, sep, value = line.partition(":")
        if not sep:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            self.data.append(value)
        return []

    def has_pending_data(self) -> bool:
        return bool(self.data or self.line or self.pending_cr or self.decoder.getstate()[0])


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckError(f"{label} must be a nonempty string")
    return value


def extract_nonstream_tool(response: Any, expected_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(response, dict):
        raise CheckError("response must be a JSON object")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise CheckError("expected exactly one assistant choice")
    choice = choices[0]
    if not isinstance(choice, dict) or type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
        raise CheckError("choice index must be integer 0")
    if choice.get("finish_reason") != "tool_calls":
        raise CheckError("expected finish_reason tool_calls")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise CheckError("assistant message is missing or has the wrong role")
    if message.get("refusal"):
        raise CheckError("assistant refused the request")
    if message.get("function_call"):
        raise CheckError("response included an unexpected legacy function call")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise CheckError("expected exactly one tool call")
    call = calls[0]
    if not isinstance(call, dict):
        raise CheckError("tool call must be an object")
    call_id = _require_string(call.get("id"), "tool call id")
    if call.get("type") != "function":
        raise CheckError("tool call type must be function")
    function = call.get("function")
    if not isinstance(function, dict):
        raise CheckError("tool call function is missing")
    name = _require_string(function.get("name"), "function name")
    if name != expected_name:
        raise CheckError(f"expected function {expected_name}, received {name}")
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or not arguments:
        raise CheckError("function arguments must be a nonempty JSON string")
    parsed = strict_json(arguments)
    if not isinstance(parsed, dict) or not parsed:
        raise CheckError("function arguments must be a nonempty JSON object")
    normalized = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
    return normalized, parsed


def extract_stream_tool(events: list[str], expected_name: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    choices_seen: set[int] = set()
    calls: dict[tuple[int, int], dict[str, str]] = {}
    finishes: dict[int, str] = {}
    event_trace: list[dict[str, Any]] = []
    done = False
    for raw in events:
        if done:
            raise CheckError("data event arrived after [DONE]")
        if raw == "[DONE]":
            done = True
            continue
        payload = strict_json(raw)
        if not isinstance(payload, dict):
            raise CheckError("stream event must be a JSON object")
        choices = payload.get("choices")
        if not isinstance(choices, list):
            raise CheckError("stream choices must be a list")
        event_trace.append(payload)
        for choice in choices:
            if not isinstance(choice, dict):
                raise CheckError("stream choice must be an object")
            choice_index = choice.get("index")
            if type(choice_index) is not int or choice_index < 0:
                raise CheckError("choice index must be a nonnegative integer")
            if choice_index in finishes:
                if choice.get("finish_reason") is not None:
                    raise CheckError("duplicate or conflicting finish_reason")
                raise CheckError("data event arrived after finish_reason")
            choices_seen.add(choice_index)
            finish = choice.get("finish_reason")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise CheckError("stream delta must be an object")
            if delta.get("role") not in (None, "assistant"):
                raise CheckError("stream role must be assistant")
            if delta.get("refusal") or delta.get("function_call"):
                raise CheckError("stream included a refusal or legacy function call")
            fragments = delta.get("tool_calls", [])
            if fragments is None:
                fragments = []
            if not isinstance(fragments, list):
                raise CheckError("tool_calls delta must be a list")
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    raise CheckError("tool-call delta must be an object")
                call_index = fragment.get("index")
                if type(call_index) is not int or call_index < 0:
                    raise CheckError("tool-call index must be a nonnegative integer")
                state = calls.setdefault((choice_index, call_index), {"id": "", "type": "", "name": "", "arguments": ""})
                for key in ("id", "type"):
                    value = fragment.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise CheckError(f"tool-call {key} fragment must be a string")
                        state[key] += value
                function = fragment.get("function", {})
                if not isinstance(function, dict):
                    raise CheckError("tool-call function delta must be an object")
                for key in ("name", "arguments"):
                    value = function.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise CheckError(f"function {key} fragment must be a string")
                        state[key] += value
            if finish is not None:
                finishes[choice_index] = finish
                if finish != "tool_calls":
                    raise CheckError(f"expected finish_reason tool_calls, received {finish}")
    if not done:
        raise CheckError("stream ended without [DONE]")
    if len(choices_seen) != 1:
        raise CheckError("expected exactly one assistant choice")
    choice_index = next(iter(choices_seen))
    if choice_index != 0:
        raise CheckError("choice index must be integer 0")
    if finishes.get(choice_index) != "tool_calls":
        raise CheckError("stream ended without finish_reason tool_calls")
    selected = [(key, value) for key, value in calls.items() if key[0] == choice_index]
    if len(selected) != 1:
        raise CheckError("expected exactly one tool call")
    (_, call_index), state = selected[0]
    if call_index != 0:
        raise CheckError("tool-call index must be integer 0")
    call_id = _require_string(state["id"], "tool call id")
    if state["type"] != "function":
        raise CheckError("tool call type must be function")
    name = _require_string(state["name"], "function name")
    if name != expected_name:
        raise CheckError(f"expected function {expected_name}, received {name}")
    if not state["arguments"]:
        raise CheckError("function arguments must be nonempty")
    parsed = strict_json(state["arguments"])
    if not isinstance(parsed, dict) or not parsed:
        raise CheckError("function arguments must be a nonempty JSON object")
    normalized = {"id": call_id, "type": "function", "function": {"name": name, "arguments": state["arguments"]}}
    return normalized, parsed, event_trace


_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|(?:^|[_-])token(?:$|[_-])|password|secret|credential|cookie)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN = re.compile(r"\b(?:sk|tok|key)-[A-Za-z0-9_-]{12,}\b")
_CRED_VALUE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|secret|token)=([^&\s]+)")
_HOME_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+")
_WINDOWS_HOME = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")


def sanitize(value: Any, api_key: str | None = None) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in {"authorization", "proxy-authorization"}:
                continue
            if _SECRET_KEY.search(key_text):
                safe[key_text] = "[redacted]"
            else:
                safe[str(key)] = sanitize(item, api_key)
        return safe
    if isinstance(value, list):
        return [sanitize(item, api_key) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item, api_key) for item in value]
    if isinstance(value, str):
        text = value
        if text.lstrip().startswith(("{", "[")):
            try:
                decoded = strict_json(text)
                if isinstance(decoded, (dict, list)):
                    cleaned = sanitize(decoded, api_key)
                    if cleaned != decoded:
                        return json.dumps(cleaned, ensure_ascii=False)
            except CheckError:
                pass
        if api_key:
            text = text.replace(api_key, "[redacted]")
        text = _BEARER.sub("Bearer [redacted]", text)
        text = _TOKEN.sub("[redacted]", text)
        text = _CRED_VALUE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
        text = _HOME_PATH.sub("[home]", text)
        text = _WINDOWS_HOME.sub("[home]", text)
        return text
    return value
