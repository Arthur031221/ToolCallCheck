from __future__ import annotations

import html
import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

from .core import CheckError, SSEParser, sanitize, strict_json

CSS = """
:root{color-scheme:light}
:root{--ink:#17324d}
:root{--blue:#254e73}
:root{--paper:#fff}
:root{--wash:#edf3f8}
:root{--muted:#536578}
:root{--line:#c8d5e0}
:root{--pass:#177448}
:root{--fail:#ae3434}
:root{--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
:root{--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}
html{background:var(--wash)}
html{color:var(--ink)}
html{font-family:var(--sans)}
html{line-height:1.5}
body{margin:0}
body{overflow-x:hidden}
.page{max-width:1120px}
.page{margin:0 auto}
.page{padding:36px 24px 64px}
.masthead{display:flex}
.masthead{align-items:center}
.masthead{gap:18px}
.masthead{border-bottom:2px solid var(--ink)}
.masthead{padding:4px 0 20px}
.masthead img{width:48px}
.masthead img{height:48px}
.masthead h1{font-size:clamp(1.8rem,4vw,2.65rem)}
.masthead h1{line-height:1.08}
.masthead h1{letter-spacing:-.035em}
.masthead h1{margin:0}
.meta{margin-left:auto}
.meta{text-align:right}
.meta{color:var(--muted)}
.meta{font-size:.88rem}
.fixture{margin:22px 0}
.fixture{padding:12px 16px}
.fixture{border-left:4px solid var(--blue)}
.fixture{background:#dce8f1}
.fixture{font-weight:650}
.summary{display:flex}
.summary{align-items:baseline}
.summary{gap:12px}
.summary{padding:22px 0 14px}
.summary strong{font-size:2rem}
.summary strong{line-height:1}
.summary span{color:var(--muted)}
.table-wrap{width:100%}
.table-wrap{border:1px solid var(--line)}
.table-wrap{background:var(--paper)}
table{border-collapse:collapse}
table{width:100%}
table{table-layout:fixed}
th,td{text-align:left}
th,td{vertical-align:top}
th,td{padding:13px 15px}
th,td{border-bottom:1px solid var(--line)}
th{background:#dfe9f1}
th{font-size:.78rem}
th{letter-spacing:.045em}
th{text-transform:uppercase}
th:first-child{width:25%}
th:nth-child(2){width:13%}
td{overflow-wrap:anywhere}
.status{font-weight:700}
.status.pass{color:var(--pass)}
.status.fail{color:var(--fail)}
details{margin-top:8px}
summary{cursor:pointer}
summary{font-weight:650}
summary{color:var(--blue)}
summary{text-decoration:underline}
summary{text-underline-offset:3px}
summary:focus-visible,a:focus-visible{outline:3px solid #bd6c18}
summary:focus-visible,a:focus-visible{outline-offset:3px}
summary:focus-visible,a:focus-visible{border-radius:2px}
pre{font:12px/1.55 var(--mono)}
pre{white-space:pre-wrap}
pre{overflow-wrap:anywhere}
pre{word-break:break-word}
pre{background:#f2f6f9}
pre{border-left:2px solid var(--line)}
pre{padding:12px}
pre{margin:10px 0 0}
pre{max-height:360px}
pre{overflow:auto}
.error{color:var(--fail)}
.error{font-weight:600}
.section{margin-top:38px}
.section h2{font-size:1.25rem}
.section h2{border-bottom:1px solid var(--line)}
.section h2{padding-bottom:8px}
.fine{font-size:.88rem}
.fine{color:var(--muted)}
.fine{max-width:78ch}
.trace-row{padding:12px 0}
.trace-row{border-bottom:1px solid var(--line)}
.trace-row h3{font-size:1rem}
.trace-row h3{margin:0}
.footer{margin-top:44px}
.footer{padding-top:14px}
.footer{border-top:1px solid var(--line)}
.footer{color:var(--muted)}
.footer{font-size:.82rem}
@media(max-width:650px){
.page{padding:20px 14px 44px}
.masthead{align-items:flex-start}
.masthead img{width:40px}
.masthead img{height:40px}
.meta{font-size:.78rem}
.table-wrap{border:0}
.table-wrap{background:transparent}
table,tbody,tr,td{display:block}
table,tbody,tr,td{width:100%}
thead{position:absolute}
thead{width:1px}
thead{height:1px}
thead{padding:0}
thead{margin:-1px}
thead{overflow:hidden}
thead{clip:rect(0,0,0,0)}
thead{white-space:nowrap}
thead{border:0}
tr{margin:0 0 12px}
tr{background:var(--paper)}
tr{border:1px solid var(--line)}
td{border:0}
td{padding:9px 12px}
td:first-child{font-weight:700}
td:first-child{padding-top:12px}
td:last-child{padding-bottom:13px}
.summary{padding-top:18px}
.summary strong{font-size:1.7rem}
}
"""


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def render_html(data: dict[str, Any]) -> str:
    results = data["results"]
    total = len(results)
    passed = sum(bool(item["passed"]) for item in results)
    fixture = '<p class="fixture">Synthetic fixture. These local responses demonstrate report behavior and do not establish remote model compatibility.</p>' if data.get("synthetic") else ""
    rows = []
    for item in results:
        status = "PASS" if item["passed"] else "FAIL"
        expected = html.escape(_pretty(item["expected"]))
        received = html.escape(_pretty(item["received"]))
        error = f'<p class="error">{html.escape(str(item["error"]))}</p>' if item.get("error") else ""
        final = f'<h4>Final message</h4><pre>{html.escape(str(item["final_content"]))}</pre>' if item.get("final_content") is not None else ""
        detail = f'<details><summary>Expected and received evidence</summary><h4>Expected arguments</h4><pre>{expected}</pre><h4>Received arguments</h4><pre>{received}</pre>{error}{final}</details>'
        rows.append(f'<tr><td>{html.escape(item["name"])}{detail}</td><td class="status {status.lower()}">{status}</td><td>{expected}</td><td>{received}{error}</td></tr>')
    trace_parts = []
    for index, trace in enumerate(data.get("traces", []), start=1):
        label = f'{trace.get("probe", "Probe")} request' if "request" in trace else f'{trace.get("probe", "Probe")} response'
        trace_parts.append(f'<div class="trace-row"><h3>{html.escape(str(label))}</h3><details><summary>View sanitized trace</summary><pre>{html.escape(_pretty(trace))}</pre></details></div>')
    model = html.escape(str(data.get("model", "unknown")))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light"><title>Toolcall Check report</title><style>{CSS}</style></head>
<body><main class="page"><header class="masthead"><img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='5' y='5' width='54' height='54' fill='%2317324d'/%3E%3Cpath d='M17 21h30v7H17zm0 15h18v7H17z' fill='%23ffffff'/%3E%3Ccircle cx='44' cy='39' r='4' fill='%23ffffff'/%3E%3C/svg%3E" alt=""><h1>Toolcall Check</h1><div class="meta">Model: {model}<br>Generated locally</div></header>
{fixture}<div class="summary"><strong>{passed}/{total}</strong><span>checks passed</span></div>
<div class="table-wrap"><table><thead><tr><th>Check</th><th>Result</th><th>Expected arguments</th><th>Received arguments</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<section class="section"><h2>Request and response traces</h2><p class="fine">Authorization headers are omitted. Saved evidence is sanitized on a best-effort basis. Inspect it before sharing.</p>{''.join(trace_parts)}</section>
<footer class="footer">Toolcall Check records behavior from one run. The report contains sanitized request and response evidence.</footer></main></body></html>'''


def _check_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError(f"refusing symlink path component: {current}")
        except FileNotFoundError:
            continue


def _protect_stream_trace(response: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    body = response.get("body")
    if not isinstance(body, str) or "data:" not in body:
        return response
    try:
        raw_events = SSEParser().feed(body.encode("utf-8"), final=True)
        events = [(raw, strict_json(raw) if raw != "[DONE]" else None) for raw in raw_events]
    except CheckError:
        return response
    fragments: dict[tuple[Any, Any, str], list[str]] = {}
    locations: dict[tuple[Any, Any, str], list[tuple[dict[str, Any], str]]] = {}
    for _, event in events:
        if not isinstance(event, dict):
            continue
        choices = event.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict) or type(choice.get("index")) is not int:
                continue
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                continue
            fields = [(choice["index"], -1, key, delta) for key in ("content", "refusal")]
            calls = delta.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict) or type(call.get("index")) is not int:
                    continue
                fields.extend((choice["index"], call["index"], key, call) for key in ("id", "type"))
                function = call.get("function", {})
                if isinstance(function, dict):
                    fields.extend((choice["index"], call["index"], key, function) for key in ("name", "arguments"))
            for choice_index, call_index, key, owner in fields:
                value = owner.get(key)
                if isinstance(value, str):
                    group = (choice_index, call_index, key)
                    fragments.setdefault(group, []).append(value)
                    locations.setdefault(group, []).append((owner, key))
    protected = []
    for group, values in fragments.items():
        joined = "".join(values)
        original: Any = joined
        try:
            decoded = strict_json(joined)
            if isinstance(decoded, (dict, list)):
                original = decoded
        except CheckError:
            pass
        cleaned = sanitize(original, api_key)
        if cleaned != original:
            for owner, key in locations[group]:
                owner[key] = "[redacted fragment]"
            protected.append({"choice": group[0], "call": group[1], "field": group[2], "assembled": cleaned})
    if not protected:
        return response
    safe = copy.deepcopy(response)
    safe["body"] = "".join("data: " + (json.dumps(event, ensure_ascii=False) if event is not None else raw) + "\n\n" for raw, event in events)
    safe["events"] = [event for _, event in events if event is not None]
    safe["protected_stream_fields"] = protected
    return safe


def write_report(output: str | Path, data: dict[str, Any], api_key: str | None = None) -> Path:
    target = Path(output).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = Path(os.path.abspath(target))
    _check_no_symlink_components(target)
    try:
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f"output path already exists: {target}") from exc
    os.chmod(target, 0o700)
    prepared = copy.deepcopy(data)
    for trace in prepared.get("traces", []):
        if isinstance(trace, dict) and isinstance(trace.get("response"), dict):
            trace["response"] = _protect_stream_trace(trace["response"], api_key)
    safe = sanitize(prepared, api_key)
    artifacts = {
        "results.json": json.dumps({k: v for k, v in safe.items() if k != "traces"}, ensure_ascii=False, indent=2) + "\n",
        "trace.json": json.dumps(safe.get("traces", []), ensure_ascii=False, indent=2) + "\n",
        "report.html": render_html(safe),
    }
    for name, contents in artifacts.items():
        path = target / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
        os.chmod(path, 0o600)
    return target
