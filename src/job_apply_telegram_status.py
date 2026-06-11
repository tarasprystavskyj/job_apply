from __future__ import annotations

import json
from typing import Any

import requests

from job_apply_config import settings


def main() -> int:
    cfg = settings()
    if not cfg.telegram_bot_token:
        raise SystemExit("JOB_APPLY_TELEGRAM_BOT_TOKEN missing")
    base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    out: dict[str, Any] = {}
    for method in ["getMe", "getWebhookInfo", "getUpdates"]:
        resp = requests.get(f"{base}/{method}", timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if method == "getUpdates":
            for update in data.get("result", []):
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                if chat.get("id") and not cfg.telegram_chat_id:
                    out["suggested_chat_id"] = chat["id"]
        out[method] = data
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
