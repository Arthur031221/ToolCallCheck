from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from .core import sanitize
from .fixture import demo_server
from .probes import run_probes
from .report import write_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toolcall-check", description="Inspect exact and streamed tool calls on a chat-completions-compatible endpoint.")
    parser.add_argument("--base-url", help="Endpoint base URL, such as http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="local-model", help="Model name sent in requests (default: local-model)")
    parser.add_argument("--api-key-env", default="TOOLCALL_CHECK_API_KEY", metavar="VARIABLE", help="Environment variable containing the endpoint key (default: TOOLCALL_CHECK_API_KEY)")
    parser.add_argument("--output", default="toolcall-check-report", help="New private output directory (must not already exist)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="Run against a local synthetic fixture")
    group.add_argument("--demo-broken", action="store_true", help="Show intentional failures from a local synthetic fixture")
    return parser


def _validate_url(value: str) -> None:
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError as exc:
        raise ValueError("base URL is malformed") from exc
    if parts.scheme not in {"http", "https"} or not hostname:
        raise ValueError("base URL must be an http or https URL with a hostname")
    if parts.username is not None or parts.password is not None or "?" in value or "#" in value:
        raise ValueError("base URL must omit userinfo, query, and fragment")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError("base URL has an invalid port") from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        print("configuration error: --api-key-env must be an environment variable name", file=sys.stderr)
        return 2
    if not args.model.strip():
        print("configuration error: --model must not be empty", file=sys.stderr)
        return 2
    key: str | None = None
    synthetic = bool(args.demo or args.demo_broken)
    if synthetic:
        with demo_server(broken=args.demo_broken) as server:
            data = run_probes(server.base_url, args.model, None, synthetic=True)
    else:
        if not args.base_url:
            print("configuration error: --base-url is required unless --demo is used", file=sys.stderr)
            return 2
        try:
            _validate_url(args.base_url)
        except ValueError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        key = os.environ.get(args.api_key_env)
        if key and (len(key) > 8192 or any(not 33 <= ord(char) <= 126 for char in key)):
            print("configuration error: the API key must contain visible ASCII characters without whitespace", file=sys.stderr)
            return 2
        data = run_probes(args.base_url, args.model, key, synthetic=False)
    try:
        target = write_report(args.output, data, api_key=key)
    except (OSError, ValueError) as exc:
        safe_error = sanitize(str(exc), key)
        print(f"report error: {safe_error}", file=sys.stderr)
        return 2
    failed = [item for item in data["results"] if not item["passed"]]
    if synthetic:
        print("Synthetic fixture. Remote model compatibility is unverified.")
    print(f"Report written to {sanitize(str(Path(args.output).expanduser() / 'report.html'), key)}")
    print(f"Checks: {len(data['results']) - len(failed)}/{len(data['results'])} passed")
    for item in data["results"]:
        if item["passed"]:
            print(f"PASS {item['name']}")
        else:
            print(f"FAIL {item['name']}: {sanitize(item.get('error') or 'check failed', key)}")
    return 1 if failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
