from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    root: Path
    resume_dir: Path
    telegram_bot_token: str
    telegram_chat_id: str
    agent_model: str
    daily_hour: int
    cdp_endpoint: str
    location_preferences: str
    djinni_profile_update_url: str
    public_resume_links: tuple[str, ...]
    recruiter_auto_reply_enabled: bool
    recruiter_auto_reply_threshold: float


def settings() -> Settings:
    load_env()
    public_resume_links = tuple(
        link.strip()
        for link in os.environ.get("JOB_APPLY_PUBLIC_RESUME_LINKS", "").split(",")
        if link.strip().startswith(("https://", "http://"))
    )
    return Settings(
        root=ROOT,
        resume_dir=Path(os.environ.get("JOB_APPLY_RESUME_DIR", r"C:\python_scripts\projects_search\my_resumes")),
        telegram_bot_token=os.environ.get("JOB_APPLY_TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("JOB_APPLY_TELEGRAM_CHAT_ID", ""),
        agent_model=os.environ.get("JOB_APPLY_AGENT_MODEL", "gpt-5.3-codex-spark"),
        daily_hour=int(os.environ.get("JOB_APPLY_DAILY_HOUR", "10")),
        cdp_endpoint=os.environ.get("JOB_APPLY_CDP_ENDPOINT", "http://127.0.0.1:9222"),
        location_preferences=os.environ.get(
            "JOB_APPLY_LOCATION_PREFERENCES",
            "Lviv onsite preferred; Kyiv remote; USA remote",
        ),
        djinni_profile_update_url=os.environ.get("JOB_APPLY_DJINNI_PROFILE_UPDATE_URL", "https://djinni.co/my/profile/"),
        public_resume_links=public_resume_links,
        recruiter_auto_reply_enabled=os.environ.get("JOB_APPLY_RECRUITER_AUTO_REPLY_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"},
        recruiter_auto_reply_threshold=float(os.environ.get("JOB_APPLY_RECRUITER_AUTO_REPLY_THRESHOLD", "0.80")),
    )
