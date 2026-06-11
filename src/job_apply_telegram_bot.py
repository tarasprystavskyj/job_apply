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
from resume_index import build_resume_index
from vacancy_pipeline import build_candidate_batch, candidate_summary


STATE_PATH = ROOT / "data" / "job_waves" / "telegram_state.json"


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"offset": 0, "latest_batch": "", "last_daily_date": ""}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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

    def scan(self) -> Path:
        build_resume_index()
        try:
            scan_inbox()
        except Exception as exc:
            print(f"inbox scan skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return build_candidate_batch(limit=10)

    def format_batch(self, batch: Path) -> str:
        rows = candidate_summary(batch)
        lines = ["Свіжі кандидати на подачу / review:", ""]
        for idx, row in enumerate(rows, 1):
            site = row.get("site") or ""
            recommendation = row.get("recommendation") or ""
            score = row.get("score") or ""
            marker = "submit" if site == "djinni" else "review-only"
            lines.append(f"{idx}. [{marker}] {row.get('company')} - {row.get('title')}")
            if recommendation or score:
                lines.append(f"   recommendation={recommendation} score={score}")
            lines.append(str(row.get("url")))
        lines.append("")
        lines.append("Для підтвердження вже approved Djinni рядків: /approve_latest")
        lines.append("Для нового пошуку: /scan")
        return "\n".join(lines)

    def approve_latest(self, state: dict[str, Any], chat_id: str | int) -> None:
        batch = state.get("latest_batch")
        if not batch:
            self.send(chat_id, "Немає latest batch. Запусти /scan.")
            return
        approved_count = count_approved_djinni_rows(Path(batch))
        if approved_count == 0:
            self.send(chat_id, "Немає approved Djinni рядків у latest batch. Підтверди рядки у web UI або CSV.")
            return
        subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "src" / "djinni_csv_apply.py"),
                "--csv",
                batch,
                "--execute",
                "--i-understand-this-submits-applications",
            ],
            cwd=str(ROOT),
        )
        self.send(chat_id, f"Запустив approved batch ({approved_count} рядків):\n{batch}")

    def handle_text(self, chat_id: str | int, text: str, state: dict[str, Any]) -> None:
        state["chat_id"] = str(chat_id)
        command = text.strip().split()[0].lower() if text.strip() else ""
        if command in {"/start", "/status"}:
            self.send(chat_id, f"JobApply bot ready. latest_batch={state.get('latest_batch') or 'none'}")
            return
        if command == "/scan":
            batch = self.scan()
            state["latest_batch"] = str(batch)
            save_state(state)
            self.send(chat_id, self.format_batch(batch))
            return
        if command == "/approve_latest":
            self.approve_latest(state, chat_id)
            return
        self.send(chat_id, "Commands: /scan, /approve_latest, /status")

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
                        batch = self.scan()
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
    args = parser.parse_args()
    bot = TelegramBot()
    if args.once:
        state = load_state()
        count = bot.poll_once(state, timeout=1)
        print(f"processed_updates={count}")
    else:
        bot.run()
