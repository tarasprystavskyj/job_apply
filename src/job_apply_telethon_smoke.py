from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from telethon import TelegramClient


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def run(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file)
    load_env_file(env_file)
    env_dir = env_file.parent if env_file.exists() else Path.cwd()
    session_raw = os.environ.get("TG_SESSION", "runs/telegram_paper/darkknighttrade_session")
    session = Path(session_raw)
    if not session.is_absolute():
        session = env_dir / session
    client = TelegramClient(str(session), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("telethon_session_authorized=false")
            return 2
        print("telethon_session_authorized=true")
        for command in args.commands:
            await client.send_message(args.bot, command)
            print(f"sent {command}")
            await asyncio.sleep(args.wait_sec)
            messages = await client.get_messages(args.bot, limit=3)
            for msg in reversed(messages):
                text = (msg.raw_text or "").replace("\n", " ")[:220]
                sender = "out" if msg.out else "bot"
                print(f"  {sender}: {text}")
    finally:
        await client.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send smoke-test commands to the job bot through an existing Telethon user session.")
    parser.add_argument("--env-file", default=r"C:\python_scripts\top_1\.env")
    parser.add_argument("--bot", default="@job_apply_happyusers_bot")
    parser.add_argument("--wait-sec", type=float, default=3.0)
    parser.add_argument("commands", nargs="*", default=["/status", "/scan", "/approve_latest"])
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
