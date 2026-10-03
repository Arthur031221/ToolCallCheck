import json
import unittest

from toolcall_check.core import CheckError, SSEParser, exact_equal, extract_nonstream_tool, extract_stream_tool, strict_json


class CoreTests(unittest.TestCase):
    def test_sse_fragmented_utf8_crlf_comments_and_multiline_data(self):
        parser = SSEParser()
        raw = ': keepalive\r\ndata: {"word":"caf\u00e9"}\r\n\r\ndata: first\ndata: second\n\n'.encode()
        split = raw.index(b'\xc3') + 1
        events = parser.feed(raw[:split]) + parser.feed(raw[split:])
        self.assertEqual(events, ['{"word":"caf\u00e9"}', 'first\nsecond'])

    def test_sse_accepts_lone_cr_delimiters(self):
        parser = SSEParser()
        self.assertEqual(parser.feed(b'data: one\r\rdata: two\r\r', final=True), ['one', 'two'])

    def test_sse_rejects_invalid_utf8(self):
        parser = SSEParser()
        with self.assertRaises(CheckError):
            parser.feed(b'data: \xff\n\n')

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for payload in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e9999}'):
            with self.subTest(payload=payload), self.assertRaises(CheckError):
                strict_json(payload)

    def test_exact_compare_distinguishes_bool_integer_float_and_extra_keys(self):
        for expected, actual in ((True, 1), (1, 1.0), ({"a": 1}, {"a": 1, "b": 2})):
            with self.subTest(expected=expected, actual=actual), self.assertRaises(CheckError):
                exact_equal(expected, actual)
        exact_equal({"a": [True, 1, 1.0]}, {"a": [True, 1, 1.0]})

    def test_nonstream_requires_nonempty_call_metadata_and_object_arguments(self):
        base = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": '{"message":"x"}'}}]}}]}
        call, args = extract_nonstream_tool(base, "echo")
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(args, {"message": "x"})
        variants = []
        for field, value in (("id", ""), ("type", "not-function")):
            item = __import__("copy").deepcopy(base)
            item["choices"][0]["message"]["tool_calls"][0][field] = value
            variants.append(item)
        item = __import__("copy").deepcopy(base)
        item["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = ""
        variants.append(item)
        item = __import__("copy").deepcopy(base)
        item["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{}"
        variants.append(item)
        for item in variants:
            with self.subTest(item=item), self.assertRaises(CheckError):
                extract_nonstream_tool(item, "echo")

    def test_stream_reconstructs_calls_across_choices_and_usage_only_events(self):
        events = [
            '{"choices":[]}',
            '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_","type":"function","function":{"name":"ec","arguments":"{\\\"message\\\":\\\"h\u00e9"}}]},"finish_reason":null}]}',
            '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"1","function":{"name":"ho","arguments":"\\\"}"}}]},"finish_reason":null}]}',
            '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
            '[DONE]',
        ]
        call, args, _trace = extract_stream_tool(events, "echo")
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(args, {"message": "h\u00e9"})

    def test_stream_rejects_missing_markers_multiple_calls_and_bool_indexes(self):
        with self.assertRaisesRegex(CheckError, r"\[DONE\]"):
            extract_stream_tool(['{"choices":[]}'], "echo")
        two_calls = ['{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"a","type":"function","function":{"name":"echo","arguments":"{\\\"x\\\":1}"}},{"index":1,"id":"b","type":"function","function":{"name":"echo","arguments":"{\\\"x\\\":1}"}}]},"finish_reason":"tool_calls"}]}', '[DONE]']
        with self.assertRaisesRegex(CheckError, "exactly one tool call"):
            extract_stream_tool(two_calls, "echo")
        with self.assertRaisesRegex(CheckError, "choice index"):
            extract_stream_tool(['{"choices":[{"index":true,"delta":{},"finish_reason":"tool_calls"}]}', '[DONE]'], "echo")

    def test_stream_requires_one_choice_and_finish_reason(self):
        with self.assertRaisesRegex(CheckError, "exactly one assistant choice"):
            extract_stream_tool(['{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"},{"index":1,"delta":{},"finish_reason":"tool_calls"}]}', '[DONE]'], "echo")
        with self.assertRaisesRegex(CheckError, "finish_reason tool_calls"):
            extract_stream_tool(['{"choices":[{"index":0,"delta":{},"finish_reason":null}]}', '[DONE]'], "echo")

    def test_stream_rejects_choice_data_after_finish(self):
        call = json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "echo", "arguments": json.dumps({"x": 1})}}]}, "finish_reason": "tool_calls"}]})
        later = json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": None}]})
        with self.assertRaisesRegex(CheckError, "after finish_reason"):
            extract_stream_tool([call, later, '[DONE]'], "echo")

    def test_stream_rejects_events_after_done(self):
        with self.assertRaisesRegex(CheckError, r"after \[DONE\]"):
            extract_stream_tool(['{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"a","type":"function","function":{"name":"echo","arguments":"{\\\"x\\\":1}"}}]},"finish_reason":"tool_calls"}]}', '[DONE]', '{"choices":[]}'], "echo")


if __name__ == "__main__":
    unittest.main()
