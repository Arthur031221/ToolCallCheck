import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from toolcall_check.cli import main
from toolcall_check.core import CheckError, extract_stream_tool
from toolcall_check.fixture import DemoServer
from toolcall_check.probes import PROBES, _request_body, _invoke_worker
from toolcall_check.report import write_report
from toolcall_check.worker import execute
from unittest.mock import MagicMock


class ReviewBoundaryTests(unittest.TestCase):
    def _events(self):
        return [json.dumps({"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "echo", "arguments": json.dumps({"message": "x"})}}]}, "finish_reason": "tool_calls"}]}), "[DONE]"]

    def test_stream_rejects_wrong_role_refusal_legacy_call_and_missing_choices(self):
        for update in ({"role": "user"}, {"refusal": "refused"}, {"function_call": {"name": "echo"}}):
            event = json.loads(self._events()[0])
            event["choices"][0]["delta"].update(update)
            with self.subTest(update=update), self.assertRaises(CheckError):
                extract_stream_tool([json.dumps(event), "[DONE]"], "echo")
        with self.assertRaises(CheckError):
            extract_stream_tool(["{}"] + self._events(), "echo")

    def _roundtrip(self, final_transform=None, first_transform=None):
        probe = PROBES[-1]
        body = _request_body(probe, "fixture")
        fixture = DemoServer()
        requests = []
        def request(_config, request_body, _stream, _traces):
            requests.append(copy.deepcopy(request_body))
            value = fixture.respond(request_body)["value"]
            if len(requests) == 1 and first_transform:
                first_transform(value)
            if len(requests) == 2 and final_transform:
                final_transform(value)
            return value
        try:
            with patch("toolcall_check.worker._single_request", side_effect=request):
                result = execute({"mode": "roundtrip", "body": body, "expected": probe["expected"]})
            return result, requests
        finally:
            fixture.server.server_close()

    def test_roundtrip_sentinel_is_introduced_only_in_matching_tool_result(self):
        result, requests = self._roundtrip()
        self.assertTrue(result["ok"], result)
        sentinel = result["result"]["final_content"]
        self.assertNotIn(sentinel, json.dumps(requests[0]))
        self.assertEqual(len(requests), 2)
        second = requests[1]
        self.assertEqual(second["tools"], requests[0]["tools"])
        self.assertEqual(second["tool_choice"], "none")
        self.assertEqual(second["messages"][-1]["tool_call_id"], second["messages"][-2]["tool_calls"][0]["id"])
        self.assertEqual(json.loads(second["messages"][-1]["content"])["result"], sentinel)

    def test_roundtrip_rejects_extra_text_refusal_legacy_call_and_another_tool_call(self):
        transforms = [
            lambda value: value["choices"][0]["message"].update(content="unrelated answer"),
            lambda value: value["choices"][0]["message"].update(content=value["choices"][0]["message"]["content"] + " "),
            lambda value: value["choices"][0]["message"].update(refusal="refused"),
            lambda value: value["choices"][0]["message"].update(function_call={"name": "echo"}),
            lambda value: value["choices"][0]["message"].update(tool_calls=[{"id": "another"}]),
        ]
        for transform in transforms:
            with self.subTest(transform=transform):
                result, _requests = self._roundtrip(final_transform=transform)
                self.assertFalse(result["ok"])

    def test_wrong_first_arguments_stop_before_second_request(self):
        def corrupt(value):
            value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps({"message": "wrong"})
        result, requests = self._roundtrip(first_transform=corrupt)
        self.assertFalse(result["ok"])
        self.assertEqual(len(requests), 1)

    def test_credentials_split_between_stream_events_are_not_saved_as_fragments(self):
        key = "protectA123protectB456"
        arguments = json.dumps({"message": key})
        split = arguments.index("protectB456")
        events = [
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[:split]}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[split:]}}]}}]},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        data = {"results": [], "model": "fixture", "traces": [{"response": {"body": raw, "events": events}}]}
        with tempfile.TemporaryDirectory() as folder:
            target = write_report(Path(folder) / "private", data, api_key=key)
            for artifact in target.iterdir():
                contents = artifact.read_text()
                self.assertNotIn("protectA123", contents)
                self.assertNotIn("protectB456", contents)
            self.assertIn("protected_stream_fields", (target / "trace.json").read_text())

    def test_serialized_nested_credential_fields_are_redacted(self):
        raw = json.dumps({"tool_calls": [{"function": {"arguments": json.dumps({"password": "nested-credential-value"})}}]})
        data = {"results": [], "model": "fixture", "traces": [{"response": {"body": raw}}]}
        with tempfile.TemporaryDirectory() as folder:
            target = write_report(Path(folder) / "private", data)
            for artifact in target.iterdir():
                self.assertNotIn("nested-credential-value", artifact.read_text())

    def test_stream_requires_event_stream_content_type(self):
        probe = PROBES[2]
        body = _request_body(probe, "fixture")
        raw = "".join("data: " + event + "\n\n" for event in self._events()).encode()
        for content_type, expected in (("application/json", False), ("text/event-stream" + chr(59) + " charset=utf-8", True)):
            response = MagicMock(status=200)
            response.getheaders.return_value = [("Content-Type", content_type)]
            buffer = io.BytesIO(raw)
            response.read1.side_effect = buffer.read1
            connection = MagicMock()
            connection.getresponse.return_value = response
            with self.subTest(content_type=content_type), patch("toolcall_check.worker._connection", return_value=connection):
                result = execute({"mode": "stream", "body": body, "base_url": "http://localhost/v1"})
                self.assertEqual(result["ok"], expected, result)
                connection.close.assert_called_once()

    def test_interrupted_worker_is_terminated_and_reaped(self):
        process = MagicMock()
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", b"")]
        with patch("toolcall_check.probes.subprocess.Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                _invoke_worker({"base_url": "http://localhost/v1", "body": {}}, 20)
        process.terminate.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 2)

    def test_invalid_configuration_does_not_contact_endpoint(self):
        cases = [["--base-url", "http://@localhost/v1"], ["--base-url", "http://localhost/v1?"], ["--demo", "--model", ""], ["--demo", "--api-key-env", "7BAD"]]
        for args in cases:
            with self.subTest(args=args), patch("toolcall_check.cli.run_probes") as run, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
                run.assert_not_called()
        with patch.dict("os.environ", {"TOOLCALL_CHECK_API_KEY": "private-value\nsecond-line"}), patch("toolcall_check.cli.run_probes") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--base-url", "http://localhost/v1"]), 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
