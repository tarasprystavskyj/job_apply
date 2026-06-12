from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from djinni_inbox_scan import scan_inbox
from job_apply_config import ROOT, settings
from job_apply_web import count_approved_rows, launch_approved_runs, update_batch_approvals
from job_scan_sources import format_scan_summary, run_all_sources_scan
from recruiter_response_scan import latest_response_summary, scan_recruiter_responses
from resume_index import build_resume_index
from vacancy_pipeline import build_candidate_batch, candidate_summary


STATE_PATH = ROOT / "data" / "job_waves" / "telegram_state.json"
WEB_STATE_PATH = ROOT / "data" / "job_waves" / "web_state.json"
SUBMISSION_LOG_PATH = ROOT / "data" / "job_waves" / "djinni_csv_submission_attempts.jsonl"


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"offset": 0, "latest_batch": "", "last_daily_date": ""}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_web_state_latest_batch(batch: Path, status: str) -> None:
    WEB_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if WEB_STATE_PATH.exists():
        try:
            state = json.loads(WEB_STATE_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    state["latest_batch"] = str(batch)
    state["status"] = status
    state["last_send_jobs"] = []
    WEB_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def count_approved_djinni_rows(batch: Path) -> int:
    with batch.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return sum(
        1
        for row in rows
        if (row.get("site") or "").strip().lower() == "djinni"
        and (row.get("url") or "").startswith("https://djinni.co/jobs/")
        and row.get("approved_to_submit") == "true"
        and row.get("final_submit_allowed") == "true"
    )


def count_approved_all_rows(batch: Path) -> int:
    return count_approved_rows(batch)


def load_submission_events(path: Path = SUBMISSION_LOG_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def latest_submission_blocker_summary(limit: int = 5) -> str:
    cfg = settings()
    events = load_submission_events()
    if not events:
        return "Submission blockers: no submission log yet."
    latest_by_url: dict[str, dict[str, Any]] = {}
    for event in events:
        url = str(event.get("source_url", ""))
        if url:
            latest_by_url[url] = event
    blockers = [
        event
        for event in latest_by_url.values()
        if str(event.get("source_url", "")).startswith("https://djinni.co/jobs/")
        and (
            str(event.get("result", "")).startswith("blocked")
            or str(event.get("blocked_reason", "")).strip()
        )
    ]
    if not blockers:
        return "Submission blockers: no active blockers in latest job states."
    has_profile_update = any(
        "profile update required" in str(event.get("blocked_reason", "")).lower()
        for event in blockers
    )
    blockers = sorted(
        blockers,
        key=lambda event: (
            "profile update required" in str(event.get("blocked_reason", "")).lower(),
            str(event.get("attempted_at", "")),
        ),
        reverse=True,
    )[:limit]
    lines = ["Submission blockers:"]
    for event in blockers:
        company = event.get("company") or "unknown company"
        result = event.get("result") or "unknown result"
        reason = event.get("blocked_reason") or "; ".join(event.get("errors") or []) or "no detailed reason"
        url = event.get("source_url") or ""
        lines.append(f"- {company}: {result}; {reason}")
        if url:
            lines.append(str(url))
    if has_profile_update:
        lines.append(f"Djinni profile update: {cfg.djinni_profile_update_url}")
        lines.extend(djinni_profile_update_proposal_lines())
    return "\n".join(lines)


def djinni_profile_update_proposal_lines() -> list[str]:
    return [
        "I can prepare a Djinni profile update draft. No profile fields will be changed or saved without separate explicit approval.",
        "Suggested profile draft:",
        "position=Senior Python / AI Automation Engineer",
        "salary=3000 USD",
        "experience=More than 10 years",
        "LinkedIn=https://www.linkedin.com/in/taras-prystavskyj/",
        "locations=Lviv onsite/hybrid preferred; Kyiv remote; USA/EU remote",
        "skills=Python, AI automation, LLM, Backend, FastAPI, API integrations, PostgreSQL, Docker, Playwright/Selenium, Telegram bots, GitHub Actions",
        "Reply approval template: Approve Djinni profile update draft; final save allowed=<yes/no>.",
    ]


class TelegramBot:
    def __init__(self) -> None:
        cfg = settings()
        if not cfg.telegram_bot_token:
            raise SystemExit("JOB_APPLY_TELEGRAM_BOT_TOKEN missing")
        self.cfg = cfg
        self.base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"

    def request(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = requests.post(f"{self.base}/{method}", json=payload or {}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data}")
        return data

    def send(self, chat_id: str | int, text: str) -> None:
        self.request("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})

    def notify_auto_reply_events(self, events: list[dict[str, Any]], chat_id: str | int | None) -> None:
        if not chat_id:
            return
        for event in events:
            if not event.get("sent"):
                continue
            text = "\n".join(
                [
                    "Auto-replied to recruiter on Djinni.",
                    f"Company: {event.get('company') or 'unknown'}",
                    f"Intent: {event.get('auto_reply_intent')}",
                    f"Confidence: {event.get('confidence')}",
                    f"Thread: {event.get('thread_url')}",
                ]
            )
            self.send(chat_id, text[:3900])

    def scan(self, chat_id: str | int | None = None) -> Path:
        summary = run_all_sources_scan(
            limit=10,
            max_pages=1,
            include_recruiter_responses=True,
            execute_recruiter_auto_reply=self.cfg.recruiter_auto_reply_enabled,
        )
        self._last_scan_status = format_scan_summary(summary)
        return Path(str(summary["batch"]))

    def format_batch(self, batch: Path) -> str:
        rows = candidate_summary(batch)
        lines = ["Fresh candidates for submit / review:", ""]
        for idx, row in enumerate(rows, 1):
            site = row.get("site") or ""
            recommendation = row.get("recommendation") or ""
            score = row.get("score") or ""
            marker = "submit" if site in {"djinni", "workua", "dou", "robotaua"} else "review-only"
            lines.append(f"{idx}. [{marker}] {row.get('company')} - {row.get('title')}")
            if recommendation or score:
                lines.append(f"   recommendation={recommendation} score={score}")
            lines.append(str(row.get("url")))
        lines.append("")
        lines.append(latest_response_summary())
        lines.append("")
        lines.append(latest_submission_blocker_summary())
        lines.append("")
        if getattr(self, "_last_scan_status", ""):
            lines.append(str(self._last_scan_status))
            lines.append("")
        lines.append("To approve and send all supported rows immediately: /approve_and_send_latest")
        lines.append("To scan again: /scan")
        return "\n".join(lines)

    def run_submit_and_report(self, batch: str, chat_id: str | int) -> None:
        try:
            jobs = launch_approved_runs(Path(batch), "submit")
            message = f"Approved all-site batch started:\n{batch}"
            if jobs:
                message += "\n\nJobs:\n" + "\n".join(
                    f"- {job.get('site')} rows={job.get('rows')} pid={job.get('pid')} log={job.get('stdout_log')}"
                    for job in jobs
                )
            else:
                message += "\n\nNo approved supported rows matched site validators."
            message += "\n\n" + latest_submission_blocker_summary()
            self.send(chat_id, message[:3900])
        except Exception as exc:
            self.send(chat_id, f"Approved batch runner failed: {type(exc).__name__}: {exc}")

    def run_scan_and_report(self, state: dict[str, Any], chat_id: str | int) -> None:
        try:
            batch = self.scan(chat_id=chat_id)
            state["latest_batch"] = str(batch)
            save_state(state)
            sync_web_state_latest_batch(batch, f"telegram all-sites scan built batch: {batch}")
            self.send(chat_id, self.format_batch(batch))
        except Exception as exc:
            self.send(chat_id, f"Scan failed: {type(exc).__name__}: {exc}")

    def approve_latest(self, state: dict[str, Any], chat_id: str | int) -> None:
        batch = state.get("latest_batch")
        if not batch:
            self.send(chat_id, "No latest batch. Run /scan first.")
            return
        batch_path = Path(batch)
        if not batch_path.exists():
            self.send(chat_id, f"Latest batch file does not exist: {batch}")
            return
        changed, skipped = update_batch_approvals(batch_path, approve_all=True)
        approved_count = count_approved_all_rows(batch_path)
        if approved_count == 0:
            self.send(chat_id, f"No supported rows for auto-approval in latest batch. blocked/review-only rows: {skipped}.")
            return
        state["latest_batch"] = str(batch_path)
        save_state(state)
        threading.Thread(target=self.run_submit_and_report, args=(str(batch_path), chat_id)).start()
        self.send(
            chat_id,
            f"Approved and started all-site batch: approved={approved_count}, changed={changed}, blocked={skipped}\n{batch_path}",
        )

    def handle_text(self, chat_id: str | int, text: str, state: dict[str, Any]) -> None:
        state["chat_id"] = str(chat_id)
        command = text.strip().split()[0].lower() if text.strip() else ""
        if command in {"/start", "/status"}:
            self.send(chat_id, f"JobApply bot ready. latest_batch={state.get('latest_batch') or 'none'}\n\n{latest_submission_blocker_summary()}")
            return
        if command == "/scan":
            self.send(chat_id, "Started all-sites scan. I will send the result when it finishes.")
            threading.Thread(target=self.run_scan_and_report, args=(state, chat_id)).start()
            return
        if command in {"/approve_and_send_latest", "/approve_latest"}:
            self.approve_latest(state, chat_id)
            return
        self.send(chat_id, "Commands: /scan, /approve_and_send_latest, /status")

    def poll_once(self, state: dict[str, Any], timeout: int = 1) -> int:
        data = self.request(
            "getUpdates",
            {
                "timeout": timeout,
                "offset": state.get("offset", 0),
                "allowed_updates": ["message"],
            },
        )
        count = 0
        for update in data.get("result", []):
            count += 1
            state["offset"] = max(state.get("offset", 0), update["update_id"] + 1)
            msg = update.get("message") or {}
            text = msg.get("text") or ""
            chat_id = msg.get("chat", {}).get("id")
            if chat_id:
                state["chat_id"] = str(chat_id)
            if chat_id and text:
                self.handle_text(chat_id, text, state)
            save_state(state)
        return count

    def scheduler_loop(self) -> None:
        while True:
            try:
                state = load_state()
                now = datetime.now()
                if now.hour == self.cfg.daily_hour and state.get("last_daily_date") != now.strftime("%Y-%m-%d"):
                    chat_id = self.cfg.telegram_chat_id
                    if chat_id:
                        batch = self.scan(chat_id=chat_id)
                        state["latest_batch"] = str(batch)
                        state["last_daily_date"] = now.strftime("%Y-%m-%d")
                        save_state(state)
                        self.send(chat_id, self.format_batch(batch))
            except Exception as exc:
                print(f"scheduler error: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(60)

    def run(self) -> None:
        state = load_state()
        threading.Thread(target=self.scheduler_loop, daemon=True).start()
        while True:
            self.poll_once(state, timeout=25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process pending Telegram updates once and exit.")
    parser.add_argument("--notify-code-change", default="", help="Send a code-change/blocker-fix notice to the configured chat.")
    parser.add_argument("--i-understand-this-sends-telegram", action="store_true")
    args = parser.parse_args()
    bot = TelegramBot()
    if args.notify_code_change:
        if not args.i_understand_this_sends_telegram:
            raise SystemExit("--notify-code-change requires --i-understand-this-sends-telegram")
        if not bot.cfg.telegram_chat_id:
            raise SystemExit("JOB_APPLY_TELEGRAM_CHAT_ID missing")
        bot.send(bot.cfg.telegram_chat_id, args.notify_code_change)
    elif args.once:
        state = load_state()
        count = bot.poll_once(state, timeout=1)
        print(f"processed_updates={count}")
    else:
        bot.run()
