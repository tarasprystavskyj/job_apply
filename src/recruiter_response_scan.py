#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from djinni_csv_apply import CdpTab, cdp_json
from djinni_inbox_scan import wait_for_page
from job_apply_config import ROOT, settings


DEFAULT_OUTPUT = ROOT / "data" / "job_waves" / "recruiter_responses.jsonl"
DEFAULT_AUTO_REPLY_LOG = ROOT / "data" / "job_waves" / "recruiter_auto_replies.jsonl"
DEFAULT_INBOX_OFFERS = ROOT / "data" / "job_waves" / "djinni_inbox_offers.jsonl"
DEFAULT_WEB_RUNS_DIR = ROOT / "data" / "job_waves" / "web_runs"
DEFAULT_REPLY_LOCK_DIR = ROOT / "data" / "job_waves" / "recruiter_reply_locks"
DEFAULT_THREAD_URLS: list[str] = []
DJINNI_THREAD_RE = re.compile(r"^https://djinni\.co/my/inbox/\d+/(?:#.*)?$")
DJINNI_THREAD_ID_RE = re.compile(r"^https://djinni\.co/my/inbox/(\d+)/?(?:#.*)?$")
REJECTION_TOKENS = ["РІС–РґС…РёР»РµРЅРѕ", "can't offer", "cannot offer", "not move forward", "unfortunately"]
ACTION_TOKENS = ["interview", "call", "meeting", "next step", "С‚РµС…РЅС–С‡РЅ", "СЃРїС–РІР±РµСЃС–Рґ"]


@dataclass(frozen=True)
class RecruiterResponse:
    schema: str
    observed_at: str
    source_site: str
    thread_url: str
    company: str
    role: str
    status: str
    recruiter_message: str
    evidence: str
    likely_lessons: list[str]
    option_a: str
    option_b: str
    suggested_thank_you_reply: str
    confidence: float = 0.0
    auto_reply_intent: str = "manual_review"
    auto_reply_allowed: bool = False
    auto_reply_reason: str = ""
    suggested_auto_reply: str = ""
    public_resume_links: list[str] | None = None


def configured_thread_urls() -> list[str]:
    merged = list(dict.fromkeys(explicit_thread_urls() + inbox_thread_urls()))
    return [url for url in merged if is_djinni_thread_url(url)]


def explicit_thread_urls() -> list[str]:
    settings()
    raw = os.environ.get("JOB_APPLY_RECRUITER_RESPONSE_THREADS", "")
    urls = [url.strip() for url in raw.split(",") if url.strip()]
    merged = list(dict.fromkeys(urls + DEFAULT_THREAD_URLS))
    return [url for url in merged if is_djinni_thread_url(url)]


def is_djinni_thread_url(url: str) -> bool:
    return bool(DJINNI_THREAD_RE.match(url.strip()))


def normalize_thread_url(url: str) -> str:
    cleaned = url.strip()
    match = DJINNI_THREAD_ID_RE.match(cleaned)
    if not match:
        return cleaned
    return f"https://djinni.co/my/inbox/{match.group(1)}/"


def inbox_thread_urls(path: Path = DEFAULT_INBOX_OFFERS) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = str(row.get("thread_url") or row.get("source_url") or "").strip()
        if is_djinni_thread_url(url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def open_tab(endpoint: str, url: str) -> CdpTab:
    import urllib.parse

    encoded = urllib.parse.quote(url, safe="")
    tab_info = cdp_json(endpoint, f"/json/new?{encoded}", method="PUT")
    return CdpTab(tab_info["webSocketDebuggerUrl"])


def inspect_thread(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const clean = text => (text || "").replace(/\s+/g, " ").trim();
  const body = clean(document.body ? document.body.innerText : "");
  const areas = Array.from(document.querySelectorAll("article, .message, .conversation, .card, li, main, section"))
    .map(e => clean(e.innerText))
    .filter(t => t.length > 30)
    .slice(0, 20);
  return {
    url: location.href,
    title: document.title,
    body: body.slice(0, 8000),
    areas,
  };
})()
"""
    )


def classify_response(thread_url: str, state: dict[str, Any]) -> RecruiterResponse:
    cfg = settings()
    body = str(state.get("body", ""))
    low = body.lower()
    company, role = parse_company_role(state, body)
    status = "review"
    if any(token in low for token in ["відхилено", "can't offer", "cannot offer", "not move forward", "unfortunately"]):
        status = "rejected"
    elif any(token in low for token in ["interview", "call", "meeting", "next step", "технічн", "співбесід"]):
        status = "positive_or_action_needed"
    recruiter_message = extract_recruiter_message(body)
    status = classify_status(recruiter_message)
    lessons = infer_lessons(body, recruiter_message)
    recruiter_name = parse_recruiter_name(body)
    thank_you_reply = draft_thank_you_reply(company=company, role=role, recruiter_name=recruiter_name)
    confidence, intent, auto_reply, reason = plan_auto_reply(
        status=status,
        body=body,
        recruiter_message=recruiter_message,
        company=company,
        role=role,
        recruiter_name=recruiter_name,
        public_resume_links=list(cfg.public_resume_links),
    )
    auto_reply_allowed = confidence >= cfg.recruiter_auto_reply_threshold and bool(auto_reply)
    return RecruiterResponse(
        schema="job.recruiter_response.v0",
        observed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        source_site="djinni",
        thread_url=thread_url,
        company=company,
        role=role,
        status=status,
        recruiter_message=recruiter_message,
        evidence=evidence_excerpt(body),
        likely_lessons=lessons,
        option_a=(
            "Консервативний таргетинг: пріоритезувати Python AI automation, LLM workflow, internal tooling, "
            "backend automation і product-engineering ролі; понизити пріоритет вакансій, де явно потрібен "
            "великий enterprise production ownership у ML/RAG/graph."
        ),
        option_b=(
            "Посилення позиціонування: продовжувати подаватись на senior AI ролі, але переписати профіль/CV/"
            "cover letters так, щоб першими йшли конкретні LLM/RAG/multi-agent deliverables, вимірювані "
            "системи і production-like ownership; пом'якшити caveats, якщо рекрутер прямо не питає."
        ),
        suggested_thank_you_reply=thank_you_reply,
        confidence=confidence,
        auto_reply_intent=intent,
        auto_reply_allowed=auto_reply_allowed,
        auto_reply_reason=reason,
        suggested_auto_reply=auto_reply,
        public_resume_links=list(cfg.public_resume_links),
    )


def parse_company_role(state: dict[str, Any], body: str) -> tuple[str, str]:
    title = str(state.get("title", ""))
    company = title.split("—", 1)[0].strip() if "—" in title else ""
    role = ""
    markers = ["Senior Python AI Engineer", "Python Engineer", "AI Engineer", "Backend Engineer"]
    for marker in markers:
        if marker in body:
            role = marker
            break
    return company, role


def parse_recruiter_name(body: str) -> str:
    # Djinni conversation text usually contains the recruiter card near the
    # company name. Keep this conservative to avoid inventing a name.
    known_names = ["Valeriia", "Валерія", "Валерия"]
    lowered = body.lower()
    for name in known_names:
        if name.lower() in lowered:
            return name
    return ""


def draft_thank_you_reply(company: str = "", role: str = "", recruiter_name: str = "") -> str:
    greeting = f"Hi {recruiter_name}," if recruiter_name else "Hi,"
    company_part = f" at {company}" if company else ""
    role_part = f" for {role}" if role else ""
    return (
        f"{greeting}\n\n"
        f"Thank you for the quick response and for reviewing my application{role_part}. "
        "I appreciate your time. "
        f"If a future role{company_part} is a closer fit for my Python, AI automation, "
        "and engineering background, I would be glad to reconnect.\n\n"
        "Best regards,\n"
        "Taras"
    )


def draft_resume_link_reply(public_resume_links: list[str], recruiter_name: str = "") -> str:
    greeting = f"Hi {recruiter_name}," if recruiter_name else "Hi,"
    links = "\n".join(f"- {link}" for link in public_resume_links)
    return (
        f"{greeting}\n\n"
        "Thank you for your message. Here is my public resume link:\n"
        f"{links}\n\n"
        "My LinkedIn profile is also available here: https://www.linkedin.com/in/taras-prystavskyj/\n\n"
        "Best regards,\n"
        "Taras"
    )


def asks_for_resume_link(text: str) -> bool:
    low = text.lower()
    return any(
        token in low
        for token in [
            "resume",
            "cv",
            "curriculum vitae",
            "резюме",
            "send your profile",
            "send me your profile",
            "profile link",
            "linkedin",
        ]
    )


def needs_manual_reply(text: str) -> bool:
    low = text.lower()
    return any(
        token in low
        for token in [
            "salary",
            "compensation",
            "rate",
            "availability",
            "when can",
            "interview",
            "call",
            "meeting",
            "test task",
            "technical task",
            "зарплат",
            "ставк",
            "співбес",
            "дзвін",
            "тестов",
        ]
    )


def contains_any(text: str, tokens: list[str]) -> bool:
    low = text.lower()
    return any(token in low for token in tokens)


def is_clear_rejection(text: str) -> bool:
    return contains_any(text, REJECTION_TOKENS)


def classify_status(recruiter_message: str) -> str:
    if is_clear_rejection(recruiter_message):
        return "rejected"
    if contains_any(recruiter_message, ACTION_TOKENS):
        return "positive_or_action_needed"
    return "review"


def plan_auto_reply(
    *,
    status: str,
    body: str,
    recruiter_message: str,
    company: str,
    role: str,
    recruiter_name: str,
    public_resume_links: list[str],
) -> tuple[float, str, str, str]:
    text = " ".join([body, recruiter_message])
    recruiter_only = recruiter_message or body
    if status == "rejected":
        if not recruiter_message or not is_clear_rejection(recruiter_message):
            return (
                0.35,
                "manual_review",
                "",
                "rejection status is not supported by the latest recruiter message",
            )
        if needs_manual_reply(recruiter_message):
            return (
                0.55,
                "manual_rejection_with_action_needed",
                "",
                "rejection message also includes salary, scheduling, or other manual-review content",
            )
        return (
            0.92,
            "thank_you_rejection",
            draft_thank_you_reply(company=company, role=role, recruiter_name=recruiter_name),
            "clear rejection; low-risk polite thank-you reply",
        )
    if asks_for_resume_link(recruiter_only) and public_resume_links and not needs_manual_reply(recruiter_only):
        return (
            0.86,
            "send_public_resume_link",
            draft_resume_link_reply(public_resume_links, recruiter_name=recruiter_name),
            "recruiter appears to request resume/profile link; using allowlisted public resume link",
        )
    if status == "positive_or_action_needed":
        return (
            0.55,
            "manual_positive_action_needed",
            "",
            "positive/action-needed recruiter message may require scheduling or negotiation",
        )
    return 0.35, "manual_review", "", "no safe high-confidence auto-reply pattern"


def extract_recruiter_message(body: str) -> str:
    tail = body[-1200:]
    markers = [
        "Thanks for your interest.",
        "At this moment",
        "Unfortunately",
        "Дякуємо",
    ]
    latest_idx = -1
    for marker in markers:
        latest_idx = max(latest_idx, tail.rfind(marker))
    if latest_idx >= 0:
        return tail[latest_idx:]
    return tail


def evidence_excerpt(body: str) -> str:
    for token in ["Ваш відгук на цю позицію відхилено", "can't offer", "відхилено"]:
        idx = body.lower().find(token.lower())
        if idx >= 0:
            start = max(0, idx - 220)
            return body[start : idx + 360]
    return body[:700]


def infer_lessons(body: str, recruiter_message: str) -> list[str]:
    text = " ".join([body, recruiter_message]).lower()
    lessons: list[str] = []
    if any(token in text for token in ["not led large", "not managed a large-scale", "not built dedicated graph"]):
        lessons.append("Відповіді могли занадто підкреслити прогалини для senior enterprise AI ролей.")
    if any(token in text for token in ["rag", "graph", "semantic", "multi-agent", "llm"]):
        lessons.append("Рекрутери шукають конкретні докази LLM/RAG/multi-agent/semantic-systems досвіду.")
    if "can't offer" in text or "відхилено" in text:
        lessons.append("Це радше сигнал про таргетинг/позиціонування, а не детальний технічний фідбек.")
    if not lessons:
        lessons.append("Конкретної причини від рекрутера не знайдено; враховувати як нейтральний сигнал.")
    return lessons


def scan_recruiter_responses(
    thread_urls: list[str] | None = None,
    output: Path = DEFAULT_OUTPUT,
    delay_sec: float = 2.0,
    auto_reply: bool = False,
    execute_auto_reply: bool = False,
    auto_reply_log: Path = DEFAULT_AUTO_REPLY_LOG,
) -> dict[str, Any]:
    cfg = settings()
    urls = thread_urls or configured_thread_urls()
    rows: list[RecruiterResponse] = []
    for url in urls:
        tab: CdpTab | None = None
        try:
            tab = open_tab(cfg.cdp_endpoint, url)
            wait_for_page(tab, delay_sec)
            state = inspect_thread(tab)
            rows.append(classify_response(url, state))
        finally:
            if tab:
                tab.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")
    allowed_thread_urls = list(dict.fromkeys((thread_urls or []) + explicit_thread_urls()))
    auto_reply_events = run_auto_replies(
        rows,
        execute_send=execute_auto_reply and cfg.recruiter_auto_reply_enabled,
        enabled=auto_reply and cfg.recruiter_auto_reply_enabled,
        threshold=cfg.recruiter_auto_reply_threshold,
        log_path=auto_reply_log,
        allowed_thread_urls=allowed_thread_urls,
        delay_sec=delay_sec,
    )
    return {
        "ok": True,
        "output": str(output),
        "responses_found": len(rows),
        "rejected": sum(1 for row in rows if row.status == "rejected"),
        "positive_or_action_needed": sum(1 for row in rows if row.status == "positive_or_action_needed"),
        "auto_reply_enabled": auto_reply and cfg.recruiter_auto_reply_enabled,
        "auto_reply_events": auto_reply_events,
    }


def message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


def reply_dedupe_key(thread_url: str, message: str) -> str:
    payload = f"{normalize_thread_url(thread_url)}\n{message_digest(message)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_auto_reply_log(path: Path = DEFAULT_AUTO_REPLY_LOG) -> list[dict[str, Any]]:
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


def load_legacy_web_sent_reply_events(web_runs_dir: Path = DEFAULT_WEB_RUNS_DIR) -> list[dict[str, Any]]:
    if not web_runs_dir.exists():
        return []
    events: list[dict[str, Any]] = []
    for parent_log in web_runs_dir.glob("djinni_inbox_submit_*.jsonl"):
        rows: list[dict[str, Any]] = []
        for line in parent_log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        for idx, row in enumerate(rows, start=1):
            child_log = parent_log.with_name(f"{parent_log.stem}_{idx}.log")
            if not child_log.exists():
                continue
            try:
                child = json.loads(child_log.read_text(encoding="utf-8-sig", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if child.get("sent") is not True:
                continue
            url = str(row.get("source_url") or row.get("thread_url") or "")
            if not is_djinni_thread_url(url):
                continue
            events.append(
                {
                    "schema": "job.recruiter_auto_reply_legacy_web_send.v0",
                    "attempted_at": row.get("attempted_at", ""),
                    "thread_url": normalize_thread_url(url),
                    "raw_thread_url": url,
                    "message_length": row.get("message_length"),
                    "sent": True,
                    "source_log": str(child_log),
                }
            )
    return events


def load_sent_reply_events(path: Path = DEFAULT_AUTO_REPLY_LOG) -> list[dict[str, Any]]:
    return load_auto_reply_log(path) + load_legacy_web_sent_reply_events()


def append_auto_reply_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def sent_message_already(existing: list[dict[str, Any]], thread_url: str, message: str) -> bool:
    normalized = normalize_thread_url(thread_url)
    digest = message_digest(message)
    length = len(message)
    for event in existing:
        if event.get("sent") is not True:
            continue
        if normalize_thread_url(str(event.get("thread_url") or "")) != normalized:
            continue
        event_digest = event.get("message_digest")
        if event_digest and event_digest == digest:
            return True
        if not event_digest and event.get("message_length") == length:
            return True
    return False


def already_auto_replied(existing: list[dict[str, Any]], row: RecruiterResponse) -> bool:
    return sent_message_already(existing, row.thread_url, row.suggested_auto_reply)


def acquire_reply_lock(thread_url: str, message: str, lock_dir: Path = DEFAULT_REPLY_LOCK_DIR) -> Path | None:
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{reply_dedupe_key(thread_url, message)}.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "thread_url": normalize_thread_url(thread_url),
                    "message_digest": message_digest(message),
                    "message_length": len(message),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return lock_path


def release_reply_lock(lock_path: Path | None) -> None:
    if not lock_path:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def prepare_or_send_thank_you_once(
    thread_url: str,
    message: str,
    *,
    execute_send: bool = False,
    delay_sec: float = 2.0,
    log_path: Path = DEFAULT_AUTO_REPLY_LOG,
    auto_reply_intent: str = "manual_review_reply",
    record_event: bool = True,
) -> dict[str, Any]:
    normalized = normalize_thread_url(thread_url)
    digest = message_digest(message)
    base_event = {
        "schema": "job.recruiter_auto_reply.v0",
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_site": "djinni",
        "thread_url": normalized,
        "raw_thread_url": thread_url,
        "auto_reply_intent": auto_reply_intent,
        "message_digest": digest,
        "message_length": len(message),
        "message": message,
        "enabled": True,
        "execute_send": execute_send,
        "sent": False,
    }
    if execute_send and sent_message_already(load_sent_reply_events(log_path), thread_url, message):
        event = {**base_event, "result": "skipped_already_sent"}
        if record_event:
            append_auto_reply_event(log_path, event)
        return {"ok": True, "sent": False, "skipped": True, "reason": "already_sent_same_thread_message", **event}

    lock_path = acquire_reply_lock(thread_url, message) if execute_send else None
    if execute_send and lock_path is None:
        event = {**base_event, "result": "skipped_send_in_progress"}
        if record_event:
            append_auto_reply_event(log_path, event)
        return {"ok": True, "sent": False, "skipped": True, "reason": "send_in_progress_same_thread_message", **event}
    try:
        if execute_send and sent_message_already(load_sent_reply_events(log_path), thread_url, message):
            event = {**base_event, "result": "skipped_already_sent_after_lock"}
            if record_event:
                append_auto_reply_event(log_path, event)
            return {"ok": True, "sent": False, "skipped": True, "reason": "already_sent_same_thread_message", **event}
        result = prepare_or_send_thank_you(thread_url, message, execute_send=execute_send, delay_sec=delay_sec)
        event = {
            **base_event,
            "result": "sent" if result.get("sent") else "prepared_only",
            "sent": bool(result.get("sent")),
            "browser_result": result,
        }
        if record_event:
            append_auto_reply_event(log_path, event)
        return {**result, "thread_url": normalized, "message_digest": digest, "dedupe_logged": True}
    finally:
        release_reply_lock(lock_path)


def run_auto_replies(
    rows: list[RecruiterResponse],
    *,
    execute_send: bool,
    enabled: bool,
    threshold: float,
    log_path: Path = DEFAULT_AUTO_REPLY_LOG,
    allowed_thread_urls: list[str] | None = None,
    delay_sec: float = 2.0,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    existing = load_sent_reply_events(log_path)
    allowed_threads = {normalize_thread_url(url) for url in (allowed_thread_urls or [])}
    for row in rows:
        if not row.auto_reply_allowed or row.confidence < threshold or not row.suggested_auto_reply:
            continue
        base_event = {
            "schema": "job.recruiter_auto_reply.v0",
            "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_site": row.source_site,
            "thread_url": normalize_thread_url(row.thread_url),
            "raw_thread_url": row.thread_url,
            "company": row.company,
            "role": row.role,
            "confidence": row.confidence,
            "threshold": threshold,
            "auto_reply_intent": row.auto_reply_intent,
            "auto_reply_reason": row.auto_reply_reason,
            "message_digest": message_digest(row.suggested_auto_reply),
            "message": row.suggested_auto_reply,
            "enabled": enabled,
            "execute_send": execute_send,
            "sent": False,
        }
        if not enabled:
            event = {**base_event, "result": "blocked_auto_reply_disabled"}
            append_auto_reply_event(log_path, event)
            events.append(event)
            continue
        if not execute_send:
            event = {**base_event, "result": "dry_run_candidate"}
            append_auto_reply_event(log_path, event)
            events.append(event)
            continue
        if normalize_thread_url(row.thread_url) not in allowed_threads:
            event = {**base_event, "result": "blocked_thread_not_allowlisted_for_auto_send"}
            append_auto_reply_event(log_path, event)
            events.append(event)
            continue
        if already_auto_replied(existing, row):
            event = {**base_event, "result": "skipped_already_sent"}
            append_auto_reply_event(log_path, event)
            events.append(event)
            continue
        try:
            send_result = prepare_or_send_thank_you_once(
                row.thread_url,
                row.suggested_auto_reply,
                execute_send=execute_send,
                delay_sec=delay_sec,
                log_path=log_path,
                auto_reply_intent=row.auto_reply_intent,
                record_event=False,
            )
        except Exception as exc:
            event = {**base_event, "result": "error", "error": f"{type(exc).__name__}: {exc}"}
        else:
            if send_result.get("skipped"):
                event = {
                    **base_event,
                    "result": str(send_result.get("result") or send_result.get("reason") or "skipped"),
                    "sent": False,
                    "browser_result": send_result,
                }
                append_auto_reply_event(log_path, event)
                events.append(event)
                continue
            event = {
                **base_event,
                "result": "sent" if send_result.get("sent") else "prepared_only",
                "sent": bool(send_result.get("sent")),
                "browser_result": send_result,
            }
            if event["sent"]:
                existing.append(event)
        append_auto_reply_event(log_path, event)
        events.append(event)
    return events


def latest_response_summary(path: Path = DEFAULT_OUTPUT) -> str:
    if not path.exists():
        return "Відповіді рекрутерів: ще не сканувались."
    rows = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return "Відповіді рекрутерів: немає розпізнаних відповідей."
    rejected = [row for row in rows if row.get("status") == "rejected"]
    lines = [
        f"Відповіді рекрутерів: {len(rows)} діалог(ів), відмов={len(rejected)}.",
    ]
    if rejected:
        latest = rejected[-1]
        lines.append(f"Остання відмова: {latest.get('company') or 'unknown company'} - {latest.get('role') or 'unknown role'}.")
        lessons = latest.get("likely_lessons") or []
        if lessons:
            lines.append("Висновок: " + lessons[0])
        lines.append("Варіант A: " + str(latest.get("option_a", "")))
        lines.append("Варіант B: " + str(latest.get("option_b", "")))
        if latest.get("suggested_thank_you_reply") or latest.get("status") == "rejected":
            lines.append("Драфт подяки рекрутеру підготовлено; відправка тільки після окремого approval.")
    return "\n".join(lines)


def latest_response_for_thread(thread_url: str, path: Path = DEFAULT_OUTPUT) -> dict[str, Any] | None:
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("thread_url") == thread_url:
            latest = row
    return latest


def draft_thank_you_for_thread(thread_url: str, path: Path = DEFAULT_OUTPUT) -> str:
    row = latest_response_for_thread(thread_url, path)
    if row and row.get("suggested_thank_you_reply"):
        return str(row["suggested_thank_you_reply"])
    if row:
        return draft_thank_you_reply(company=str(row.get("company", "")), role=str(row.get("role", "")))
    return draft_thank_you_reply()


def prepare_or_send_thank_you(
    thread_url: str,
    message: str,
    *,
    execute_send: bool = False,
    delay_sec: float = 2.0,
) -> dict[str, Any]:
    cfg = settings()
    tab: CdpTab | None = None
    try:
        tab = open_tab(cfg.cdp_endpoint, thread_url)
        wait_for_page(tab, delay_sec)
        expression = f"""
(() => {{
  const message = {json.dumps(message)};
  const executeSend = {json.dumps(execute_send)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const clean = text => (text || "").replace(/\\s+/g, " ").trim();
  const clickByText = patterns => {{
    const controls = Array.from(document.querySelectorAll("button,a,input[type=button],input[type=submit]")).filter(visible);
    for (const control of controls) {{
      const text = clean(control.innerText || control.value || control.getAttribute("aria-label") || "").toLowerCase();
      if (patterns.some(pattern => text.includes(pattern))) {{
        control.scrollIntoView({{block: "center"}});
        control.click();
        return {{clicked: true, text}};
      }}
    }}
    return {{clicked: false}};
  }};
  const clickNearTarget = (target, patterns) => {{
    const roots = [
      target.closest("form"),
      target.closest(".modal, .card, .conversation, .thread, section, article"),
      document,
    ].filter(Boolean);
    for (const root of roots) {{
      const controls = Array.from(root.querySelectorAll("button,a,input[type=button],input[type=submit]"))
        .filter(visible)
        .reverse();
      for (const control of controls) {{
        const text = clean(control.innerText || control.value || control.getAttribute("aria-label") || "").toLowerCase();
        if (patterns.some(pattern => text.includes(pattern))) {{
          control.scrollIntoView({{block: "center"}});
          control.click();
          return {{clicked: true, text}};
        }}
      }}
    }}
    return {{clicked: false}};
  }};
  const opened = clickByText(["reply", "відповісти", "написати"]);
  const textarea = Array.from(document.querySelectorAll("textarea")).filter(visible).pop();
  const editable = Array.from(document.querySelectorAll("[contenteditable=true]")).filter(visible).pop();
  const target = textarea || editable;
  if (!target) {{
    return {{ok: false, filled: false, sent: false, reason: "no visible reply field", opened}};
  }}
  target.scrollIntoView({{block: "center"}});
  target.focus();
  if (textarea) {{
    textarea.value = message;
    textarea.dispatchEvent(new Event("input", {{bubbles: true}}));
    textarea.dispatchEvent(new Event("change", {{bubbles: true}}));
  }} else {{
    editable.innerText = message;
    editable.dispatchEvent(new InputEvent("input", {{bubbles: true, inputType: "insertText", data: message}}));
    editable.dispatchEvent(new Event("change", {{bubbles: true}}));
  }}
  if (!executeSend) {{
    return {{ok: true, filled: true, sent: false, dry_run: true, opened, messageLength: message.length}};
  }}
  const submitted = clickNearTarget(target, ["send", "відправити", "надіслати", "відповісти"]);
  return {{ok: submitted.clicked, filled: true, sent: submitted.clicked, submitted, opened, messageLength: message.length}};
}})()
"""
        return tab.eval(expression)
    finally:
        if tab:
            tab.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan visible Djinni recruiter responses and classify outcomes.")
    parser.add_argument("--thread-url", action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--auto-reply", action="store_true", help="Evaluate high-confidence auto-reply candidates.")
    parser.add_argument("--execute-auto-reply", action="store_true", help="Actually send allowed high-confidence auto replies.")
    parser.add_argument("--draft-thank-you", action="store_true", help="Print the suggested thank-you reply for a thread.")
    parser.add_argument("--prepare-thank-you", action="store_true", help="Fill the reply box but do not send.")
    parser.add_argument("--send-thank-you", action="store_true", help="Send the thank-you reply to the recruiter.")
    parser.add_argument("--message", default="", help="Exact thank-you message to prepare/send.")
    parser.add_argument("--i-understand-this-sends-recruiter-message", action="store_true")
    args = parser.parse_args()
    if args.execute_auto_reply and not args.i_understand_this_sends_recruiter_message:
        print("Refusing auto reply send without --i-understand-this-sends-recruiter-message.", file=sys.stderr)
        return 2
    if args.send_thank_you and not args.i_understand_this_sends_recruiter_message:
        print("Refusing send without --i-understand-this-sends-recruiter-message.", file=sys.stderr)
        return 2
    if args.draft_thank_you or args.prepare_thank_you or args.send_thank_you:
        if len(args.thread_url) != 1:
            print("Exactly one --thread-url is required for thank-you operations.", file=sys.stderr)
            return 2
        thread_url = args.thread_url[0]
        message = args.message or draft_thank_you_for_thread(thread_url, args.output)
        if args.draft_thank_you:
            print(message)
            return 0
        result = prepare_or_send_thank_you_once(
            thread_url,
            message,
            execute_send=bool(args.send_thank_you),
            delay_sec=args.delay_sec,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    result = scan_recruiter_responses(
        thread_urls=args.thread_url or None,
        output=args.output,
        delay_sec=args.delay_sec,
        auto_reply=args.auto_reply or args.execute_auto_reply,
        execute_auto_reply=args.execute_auto_reply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(latest_response_summary(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
