import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from toolcall_check.cli import main
from toolcall_check.core import sanitize
from toolcall_check.report import write_report


class CliAndReportTests(unittest.TestCase):
    def test_demo_runs_actual_http_stack_and_writes_private_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "good"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--demo", "--output", str(output)])
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertIn("5/5 passed", stdout.getvalue())
            self.assertEqual({p.name for p in output.iterdir()}, {"report.html", "results.json", "trace.json"})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for artifact in output.iterdir():
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            html = (output / "report.html").read_text()
            self.assertIn("Synthetic fixture", html)
            self.assertIn("do not establish remote model compatibility", html)
            self.assertNotIn("<script", html.lower())
            self.assertNotIn("https://", html)
            trace = json.loads((output / "trace.json").read_text())
            self.assertTrue(trace)
            self.assertNotIn("authorization", json.dumps(trace).lower())

    def test_broken_demo_has_failures_and_exit_one(self):
        with tempfile.TemporaryDirectory() as folder:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--demo-broken", "--output", str(Path(folder) / "broken")])
            self.assertEqual(code, 1)
            self.assertIn("FAIL", stdout.getvalue())
            results = json.loads((Path(folder) / "broken" / "results.json").read_text())
            self.assertTrue(any(not row["passed"] for row in results["results"]))

    def test_configuration_and_existing_output_return_two(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(main([]), 2)
        self.assertIn("--base-url is required", stderr.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "exists"
            existing.mkdir()
            self.assertEqual(main(["--demo", "--output", str(existing)]), 2)
            self.assertEqual(list(existing.iterdir()), [])

    def test_output_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "target"
            destination.mkdir()
            link = Path(folder) / "link"
            link.symlink_to(destination, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                write_report(link, {"results": [], "traces": [], "synthetic": True, "model": "x"})
            self.assertEqual(list(destination.iterdir()), [])

    def test_recursive_sanitizer_removes_known_key_and_credential_fields(self):
        safe = sanitize({"nested": {"access_token": "abc", "message": "Bearer abc sk-12345678901234567890"}, "api_key": "abc"}, "abc")
        text = json.dumps(safe)
        self.assertNotIn("abc", text)
        self.assertNotIn("sk-123", text)
        self.assertEqual(safe["nested"]["access_token"], "[redacted]")
        path_safe = sanitize({"path": "/home/levi/private/report.html", "token": "sensitive"})
        self.assertEqual(path_safe["path"], "[home]/private/report.html")
        self.assertEqual(path_safe["token"], "[redacted]")

    def test_html_escapes_injected_names_and_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            target = write_report(Path(folder) / "safe", {"results": [{"name": '<img src=x onerror="x">', "passed": False, "expected": {"a": "<script>"}, "received": None, "error": "<b>bad</b>"}], "traces": [], "synthetic": False, "model": "x"})
            html = (target / "report.html").read_text()
            self.assertNotIn('<img src=x', html)
            self.assertNotIn('<script>', html)
            self.assertIn("&lt;script&gt;", html)

    def test_no_credentials_persisted_in_any_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = "local-test-secret-9b8c"
            data = {"synthetic": False, "model": "fixture", "results": [{"name": "Check", "passed": False, "expected": {}, "received": {"token": secret}, "error": f"rejected {secret}"}], "traces": [{"request": {"headers": {"authorization": secret}, "body": {"note": secret}}, "response": {"body": f"Bearer {secret}"}}]}
            target = write_report(Path(folder) / "private", data, api_key=secret)
            for artifact in target.iterdir():
                contents = artifact.read_text()
                self.assertNotIn(secret, contents)
            self.assertNotIn("authorization", (target / "trace.json").read_text().lower())


if __name__ == "__main__":
    unittest.main()
