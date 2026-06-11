#!/usr/bin/env python3
r"""
Safe browser-assisted preparation for job applications.

This module reuses the Chrome/MCP pattern from
C:\python_scripts\chrome\workers_automate without importing its ChatGPT send or
upload helpers. It can open reviewed vacancy URLs, capture owner-visible page
snapshots, and stop before any apply/send/submit/upload action.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - handled by CLI preflight
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OBSERVATIONS = ROOT / "data" / "job_waves" / "wave_2026-06-11_ai_automation_observations.jsonl"
DEFAULT_PLAN = ROOT / "data" / "job_waves" / "wave_2026-06-11_browser_prep_plan.json"
DEFAULT_SNAPSHOT_DIR = ROOT / "data" / "job_waves" / "browser_snapshots"
DEFAULT_CDP_OPEN_LOG = ROOT / "data" / "job_waves" / "wave_2026-06-11_cdp_opened_tabs.jsonl"
DEFAULT_SHELL_OPEN_LOG = ROOT / "data" / "job_waves" / "wave_2026-06-11_main_chrome_opened_tabs.jsonl"
DEFAULT_RESUME = ROOT / "data" / "resumes" / "Taras_Prystavskyj_AI_Automation_Resume_2026-06-11.pdf"

PRIORITY_URLS = {
    "https://djinni.co/jobs/827863-ai-infrastructure-engineer-python/": 100,
    "https://djinni.co/jobs/797935-python-engineer-with-agentic-ai-ukraine-locat/": 95,
    "https://djinni.co/jobs/830630-senior-python-backend-engineer-fastapi-cloud-/": 90,
    "https://djinni.co/jobs/829878-senior-python-ai-full-stack-developer-ai-engi/": 85,
    "https://relocate.dou.ua/jobs/?category=Python&relocation=": 80,
    "https://djinni.co/jobs/819919-senior-python-engineer/": 75,
    "https://www.work.ua/jobs/7745352/": 70,
}

SUBMIT_WORDS = (
    "submit",
    "send",
    "apply",
    "відгукнутися",
    "надіслати",
    "подати",
    "откликнуться",
    "отправить",
)


@dataclass
class Vacancy:
    source_site: str
    source_url: str
    title: str
    company: str
    status: str
    fit_tags: list[str]
    risk_flags: list[str]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return records


def to_vacancy(record: dict[str, Any]) -> Vacancy:
    return Vacancy(
        source_site=str(record.get("source_site", "")),
        source_url=str(record.get("source_url", "")),
        title=str(record.get("title", "")),
        company=str(record.get("company", "")),
        status=str(record.get("status", "")),
        fit_tags=list(record.get("fit_tags") or []),
        risk_flags=list(record.get("risk_flags") or []),
    )


def score(v: Vacancy) -> int:
    text = " ".join([v.title, v.company, " ".join(v.fit_tags)]).lower()
    points = 0
    for token, value in {
        "agentic": 5,
        "ai-integration": 5,
        "ai-integrations": 5,
        "automation": 5,
        "llm": 4,
        "rag": 3,
        "python": 3,
        "backend": 2,
        "infrastructure": 2,
        "workflow": 2,
    }.items():
        if token in text:
            points += value
    if "junior" in text:
        points -= 8
    if "observed" == v.status:
        points -= 2
    if any("direct detail" in r.lower() for r in v.risk_flags):
        points -= 3
    return PRIORITY_URLS.get(v.source_url, 0) + points


def build_plan(limit: int, observations: Path, output: Path, resume: Path) -> dict[str, Any]:
    vacancies = [to_vacancy(r) for r in read_jsonl(observations)]
    ranked = sorted(vacancies, key=score, reverse=True)
    selected = ranked[:limit]
    plan = {
        "schema": "job.browser_prep_plan.v0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "review_first_no_submit",
        "resume_artifact": str(resume),
        "submission_allowed": False,
        "upload_allowed": False,
        "actions_allowed": ["open_vacancy_url", "capture_snapshot", "stop_at_apply_gate"],
        "actions_forbidden": ["submit", "send", "apply", "upload_resume", "bypass_login", "bypass_captcha"],
        "vacancies": [
            {
                "rank": idx,
                "score": score(v),
                "source_site": v.source_site,
                "source_url": v.source_url,
                "title": v.title,
                "company": v.company,
                "fit_tags": v.fit_tags,
                "risk_flags": v.risk_flags,
                "approved_to_prepare": False,
                "approved_to_submit": False,
            }
            for idx, v in enumerate(selected, 1)
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def slugify(value: str) -> str:
    value = re.sub(r"https?://", "", value.lower())
    value = re.sub(r"[^a-z0-9а-яіїєґ]+", "-", value, flags=re.IGNORECASE)
    value = value.strip("-")
    return value[:90] or "vacancy"


def has_submit_intent(text: str) -> bool:
    low = (text or "").lower()
    return any(word in low for word in SUBMIT_WORDS)


class SafeBrowser:
    def __init__(self, cdp_endpoint: str) -> None:
        self.cdp_endpoint = cdp_endpoint
        self.session: ClientSession | None = None
        self._exit_stack = AsyncExitStack()
        self.tools: list[str] = []

    async def connect(self) -> None:
        if ClientSession is None or StdioServerParameters is None or stdio_client is None:
            raise RuntimeError("Missing dependency: install mcp and @playwright/mcp first.")
        server_params = StdioServerParameters(
            command="npx.cmd",
            args=["@playwright/mcp", "--cdp-endpoint", self.cdp_endpoint],
        )
        read, write = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self.session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        tools_resp = await self.session.list_tools()
        self.tools = [t.name for t in tools_resp.tools]

    async def close(self) -> None:
        await self._exit_stack.aclose()
        self.session = None

    async def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        if not self.session:
            raise RuntimeError("Browser is not connected.")
        if tool_name in {"browser_click", "browser_file_upload", "browser_press_key"}:
            raise RuntimeError(f"Blocked unsafe browser tool in this workflow: {tool_name}")
        result = await self.session.call_tool(tool_name, arguments or {})
        texts: list[str] = []
        for content in getattr(result, "content", []):
            text = getattr(content, "text", None)
            if text:
                texts.append(text)
        return "\n".join(texts)

    async def navigate(self, url: str, wait_sec: float) -> str:
        await self.call("browser_navigate", {"url": url})
        await asyncio.sleep(wait_sec + random.uniform(0.3, 1.2))
        return await self.call("browser_snapshot", {})


async def open_tabs(plan_path: Path, snapshot_dir: Path, cdp_endpoint: str, max_tabs: int, min_delay_sec: float) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("submission_allowed") is not False or plan.get("upload_allowed") is not False:
        raise RuntimeError("Plan must keep submission_allowed=false and upload_allowed=false.")
    vacancies = list(plan.get("vacancies") or [])[:max_tabs]
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    browser = SafeBrowser(cdp_endpoint)
    await browser.connect()
    try:
        for idx, vacancy in enumerate(vacancies, 1):
            url = str(vacancy.get("source_url", ""))
            if not url.startswith(("https://", "http://")):
                raise RuntimeError(f"Refusing non-http URL: {url}")
            snapshot = await browser.navigate(url, wait_sec=min_delay_sec)
            if has_submit_intent(snapshot):
                gate = "submit_or_apply_language_seen"
            else:
                gate = "page_opened"
            out = snapshot_dir / f"{idx:02d}_{slugify(url)}.snapshot.txt"
            header = {
                "schema": "job.browser_snapshot.v0",
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "source_url": url,
                "title": vacancy.get("title", ""),
                "company": vacancy.get("company", ""),
                "gate": gate,
                "submission_allowed": False,
                "upload_allowed": False,
            }
            out.write_text(json.dumps(header, ensure_ascii=False) + "\n\n" + snapshot, encoding="utf-8")
            print(f"[{idx}/{len(vacancies)}] opened: {url}")
            print(f"  snapshot: {out}")
            await asyncio.sleep(min_delay_sec + random.uniform(1.0, 3.0))
    finally:
        await browser.close()


def cdp_request_json(url: str, method: str = "GET", timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw:
        return None
    return json.loads(raw)


def open_tabs_cdp(plan_path: Path, cdp_endpoint: str, output_log: Path, max_tabs: int, min_delay_sec: float) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("submission_allowed") is not False or plan.get("upload_allowed") is not False:
        raise RuntimeError("Plan must keep submission_allowed=false and upload_allowed=false.")
    base = cdp_endpoint.rstrip("/")
    version = cdp_request_json(f"{base}/json/version", timeout=3.0)
    vacancies = list(plan.get("vacancies") or [])[:max_tabs]
    output_log.parent.mkdir(parents=True, exist_ok=True)
    with output_log.open("a", encoding="utf-8") as f:
        for idx, vacancy in enumerate(vacancies, 1):
            url = str(vacancy.get("source_url", ""))
            if not url.startswith(("https://", "http://")):
                raise RuntimeError(f"Refusing non-http URL: {url}")
            encoded = urllib.parse.quote(url, safe="")
            opened = None
            errors: list[str] = []
            for method in ("PUT", "GET"):
                try:
                    opened = cdp_request_json(f"{base}/json/new?{encoded}", method=method, timeout=6.0)
                    break
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                    errors.append(f"{method}: {type(exc).__name__}: {exc}")
            event = {
                "schema": "job.cdp_opened_tab.v0",
                "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "rank": vacancy.get("rank"),
                "source_url": url,
                "title": vacancy.get("title", ""),
                "company": vacancy.get("company", ""),
                "chrome": {
                    "browser": (version or {}).get("Browser"),
                    "protocol_version": (version or {}).get("Protocol-Version"),
                },
                "cdp_result": opened,
                "errors": errors,
                "submission_allowed": False,
                "upload_allowed": False,
            }
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            if opened:
                print(f"[{idx}/{len(vacancies)}] opened tab: {url}")
            else:
                print(f"[{idx}/{len(vacancies)}] failed to open tab: {url}")
                for err in errors:
                    print(f"  {err}")
            time.sleep(min_delay_sec + random.uniform(0.8, 2.5))
    print(f"Wrote: {output_log}")


def find_chrome_exe() -> Path:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("chrome.exe not found in standard locations")


def open_tabs_main_chrome(plan_path: Path, output_log: Path, max_tabs: int, min_delay_sec: float) -> None:
    """
    Open URLs through the regular Chrome single-instance mechanism.

    This intentionally does not attach CDP and therefore cannot inspect cookies,
    click buttons, type personal data, upload files, or submit applications.
    It is useful when the owner is already logged in in the main Chrome profile.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("submission_allowed") is not False or plan.get("upload_allowed") is not False:
        raise RuntimeError("Plan must keep submission_allowed=false and upload_allowed=false.")
    chrome = find_chrome_exe()
    vacancies = list(plan.get("vacancies") or [])[:max_tabs]
    output_log.parent.mkdir(parents=True, exist_ok=True)
    with output_log.open("a", encoding="utf-8") as f:
        for idx, vacancy in enumerate(vacancies, 1):
            url = str(vacancy.get("source_url", ""))
            if not url.startswith(("https://", "http://")):
                raise RuntimeError(f"Refusing non-http URL: {url}")
            # No --user-data-dir here: Chrome routes to the already-running main
            # browser profile if one exists.
            proc = subprocess.Popen([str(chrome), url], close_fds=True)
            event = {
                "schema": "job.main_chrome_opened_tab.v0",
                "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "rank": vacancy.get("rank"),
                "source_url": url,
                "title": vacancy.get("title", ""),
                "company": vacancy.get("company", ""),
                "chrome_path": str(chrome),
                "launcher_pid": proc.pid,
                "submission_allowed": False,
                "upload_allowed": False,
                "automation_capabilities": ["open_url_only"],
            }
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            print(f"[{idx}/{len(vacancies)}] opened in main Chrome: {url}")
            time.sleep(min_delay_sec + random.uniform(0.7, 1.8))
    print(f"Wrote: {output_log}")


def print_plan(plan: dict[str, Any]) -> None:
    print(f"Plan: {len(plan['vacancies'])} vacancies")
    for item in plan["vacancies"]:
        print(f"{item['rank']}. {item['company']} - {item['title']}")
        print(f"   {item['source_url']}")
        print(f"   score={item['score']} submit_allowed={item['approved_to_submit']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe Chrome prep for job application review.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-plan")
    build.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    build.add_argument("--output", type=Path, default=DEFAULT_PLAN)
    build.add_argument("--resume", type=Path, default=DEFAULT_RESUME)
    build.add_argument("--limit", type=int, default=5)

    open_cmd = sub.add_parser("open-tabs")
    open_cmd.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    open_cmd.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    open_cmd.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    open_cmd.add_argument("--max-tabs", type=int, default=5)
    open_cmd.add_argument("--min-delay-sec", type=float, default=4.0)
    open_cmd.add_argument("--i-understand-no-submit", action="store_true")

    cdp_cmd = sub.add_parser("open-tabs-cdp")
    cdp_cmd.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    cdp_cmd.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    cdp_cmd.add_argument("--output-log", type=Path, default=DEFAULT_CDP_OPEN_LOG)
    cdp_cmd.add_argument("--max-tabs", type=int, default=5)
    cdp_cmd.add_argument("--min-delay-sec", type=float, default=3.0)
    cdp_cmd.add_argument("--i-understand-no-submit", action="store_true")

    shell_cmd = sub.add_parser("open-tabs-main-chrome")
    shell_cmd.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    shell_cmd.add_argument("--output-log", type=Path, default=DEFAULT_SHELL_OPEN_LOG)
    shell_cmd.add_argument("--max-tabs", type=int, default=5)
    shell_cmd.add_argument("--min-delay-sec", type=float, default=2.0)
    shell_cmd.add_argument("--i-understand-no-submit", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "build-plan":
        plan = build_plan(args.limit, args.observations, args.output, args.resume)
        print_plan(plan)
        print(f"Wrote: {args.output}")
        return 0
    if args.command == "open-tabs":
        if not args.i_understand_no_submit:
            print("Refusing to run without --i-understand-no-submit.", file=sys.stderr)
            return 2
        asyncio.run(open_tabs(args.plan, args.snapshot_dir, args.cdp_endpoint, args.max_tabs, args.min_delay_sec))
        return 0
    if args.command == "open-tabs-cdp":
        if not args.i_understand_no_submit:
            print("Refusing to run without --i-understand-no-submit.", file=sys.stderr)
            return 2
        open_tabs_cdp(args.plan, args.cdp_endpoint, args.output_log, args.max_tabs, args.min_delay_sec)
        return 0
    if args.command == "open-tabs-main-chrome":
        if not args.i_understand_no_submit:
            print("Refusing to run without --i-understand-no-submit.", file=sys.stderr)
            return 2
        open_tabs_main_chrome(args.plan, args.output_log, args.max_tabs, args.min_delay_sec)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
