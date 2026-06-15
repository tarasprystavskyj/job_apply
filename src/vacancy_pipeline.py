from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any

from job_apply_config import ROOT, settings
from job_platforms.dou import is_exact_dou_vacancy_url
from resume_index import list_resumes
from shared_job_db import DEFAULT_DB_PATH, fetch_progress_rows


DEFAULT_LINKEDIN = "https://www.linkedin.com/in/taras-prystavskyj/"
DEFAULT_SALARY_USD = 3000
BANNED_DRAFT_PHRASES = [
    "I would position",
    "I should be transparent",
    "team team",
    ".pdf",
    ".doc",
    "Stanislav_Shcherbak",
    "review artifacts",
    "diffs",
    "scoped tool use",
    "tool orchestration",
]
ROLE_TITLE_RE = re.compile(
    r"^(?P<title>.+?\b(?:AI Engineer|Python Engineer|Backend Engineer|Full-Stack Developer|Full Stack Developer|"
    r"Software Engineer|Automation Developer|Integration Engineer|Data Engineer|ML Engineer|Developer|Engineer)\b"
    r"(?:\s*\([^)]+\))?)",
    re.I,
)
DJINNI_THREAD_ID_RE = re.compile(r"^https://djinni\.co/my/inbox/(\d+)/?(?:#.*)?$")
INBOX_OFFERS_PATH = ROOT / "data" / "job_waves" / "djinni_inbox_offers.jsonl"
SUBMISSION_ATTEMPTS_PATH = ROOT / "data" / "job_waves" / "djinni_csv_submission_attempts.jsonl"
OBSERVATION_PATHS = [
    ROOT / "data" / "job_waves" / "dou_observations.jsonl",
    ROOT / "data" / "job_waves" / "wave_2026-06-11_ai_automation_observations.jsonl",
    ROOT / "data" / "job_waves" / "robotaua_observations.jsonl",
]


def latest_observations() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for path in OBSERVATION_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            url = str(row.get("source_url", ""))
            if str(row.get("source_site", "")).lower() == "dou" and not is_exact_dou_vacancy_url(url):
                continue
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            rows.append(row)
    for row in latest_shared_db_observations():
        url = str(row.get("source_url", ""))
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        rows.append(row)
    return rows


def latest_shared_db_observations(limit: int = 250) -> list[dict[str, Any]]:
    try:
        jobs = fetch_progress_rows(DEFAULT_DB_PATH, limit=limit)["jobs"]
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for job in jobs:
        platform = str(job.get("platform") or "").strip().lower()
        if platform not in {"workua", "dou", "robotaua", "djinni"}:
            continue
        source_url = str(job.get("source_url") or "")
        if platform == "dou" and not is_exact_dou_vacancy_url(source_url):
            continue
        result.append(
            {
                "source_site": platform,
                "source_url": source_url,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "summary": job.get("summary", ""),
                "requirements": job.get("requirements") or [],
                "fit_tags": job.get("fit_tags") or [],
                "risk_flags": job.get("risk_flags") or [],
                "salary_hint": job.get("salary_hint", ""),
                "published_hint": job.get("published_hint", ""),
                "status": job.get("status", ""),
                "score": str(job.get("score") or ""),
                "source_type": "shared_db_vacancy",
            }
        )
    return result


def latest_inbox_offers() -> list[dict[str, Any]]:
    if not INBOX_OFFERS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in INBOX_OFFERS_PATH.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def is_active_public_vacancy(row: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(row.get("status", "")),
            str(row.get("title", "")),
            str(row.get("summary", "")),
            " ".join(str(x) for x in row.get("risk_flags") or []),
        ]
    ).lower()
    inactive_tokens = ["inactive", "неактив", "закрит", "closed"]
    return not any(token in text for token in inactive_tokens)


def is_actionable_inbox_offer(row: dict[str, Any]) -> bool:
    text = " ".join([str(row.get("title", "")), str(row.get("snippet", "")), str(row.get("reason", ""))]).lower()
    system_tokens = [
        "відкрити листування",
        "open conversation",
        "we received your application",
        "thank you for your interest",
        "дякуємо за вашу заявку",
        "already applied",
    ]
    self_reply_tokens = ["ви:", "you:", "relevant resume:", "best regards", "taras prystavskyj"]
    if any(token in text for token in [*system_tokens, *self_reply_tokens]):
        return False
    url = str(row.get("source_url") or row.get("thread_url") or "")
    normalized_url = normalize_djinni_thread_url(url)
    if normalized_url and normalized_url in previously_replied_thread_urls():
        return False
    return row.get("recommendation") in {"digest", "review"}


def normalize_djinni_thread_url(url: str) -> str:
    match = DJINNI_THREAD_ID_RE.match(url.strip())
    if not match:
        return ""
    return f"https://djinni.co/my/inbox/{match.group(1)}/"


def previously_replied_thread_urls() -> set[str]:
    from recruiter_response_scan import load_sent_reply_events

    return {
        normalized
        for event in load_sent_reply_events()
        if event.get("sent") is True
        for normalized in [normalize_djinni_thread_url(str(event.get("thread_url") or ""))]
        if normalized
    }


def latest_submission_by_url() -> dict[str, dict[str, Any]]:
    if not SUBMISSION_ATTEMPTS_PATH.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in SUBMISSION_ATTEMPTS_PATH.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = str(row.get("source_url", ""))
        if url:
            latest[url] = row
    return latest


def terminal_submission_state(row: dict[str, Any], history: dict[str, dict[str, Any]]) -> str:
    prior = history.get(str(row.get("source_url", ""))) or {}
    result = str(prior.get("result", ""))
    blocked_reason = str(prior.get("blocked_reason", ""))
    if result == "already_applied":
        return "already_applied"
    if blocked_reason == "inactive vacancy":
        return "inactive"
    return ""


def score_vacancy(row: dict[str, Any]) -> int:
    text = " ".join(
        [
            str(row.get("title", "")),
            str(row.get("company", "")),
            " ".join(row.get("fit_tags") or []),
            " ".join(row.get("requirements") or []),
        ]
    ).lower()
    score = 0
    for token, points in {
        "python": 12,
        "ai": 10,
        "llm": 10,
        "agent": 8,
        "rag": 8,
        "automation": 8,
        "fastapi": 5,
        "backend": 4,
        "remote": 2,
    }.items():
        if token in text:
            score += points
    if "junior" in text:
        score -= 10
    score += location_score(row)
    if any("inactive" in str(x).lower() or "direct detail" in str(x).lower() for x in row.get("risk_flags") or []):
        score -= 4
    return score


def location_score(row: dict[str, Any]) -> int:
    location = str(row.get("location", "")).lower()
    prefs = settings().location_preferences.lower()
    score = 0
    if "lviv" in location or "львів" in location:
        score += 10 if "lviv onsite" in prefs else 5
    if "kyiv" in location or "київ" in location:
        score += 4
    if "remote" in location or "віддал" in location:
        score += 3
    if "usa" in location or "united states" in location:
        score += 3
    return score


def select_resume(vacancy: dict[str, Any]) -> dict[str, Any] | None:
    resumes = list_resumes()
    if not resumes:
        return None
    text = " ".join(
        [
            str(vacancy.get("title", "")),
            str(vacancy.get("summary", "")),
            " ".join(vacancy.get("fit_tags") or []),
            " ".join(vacancy.get("requirements") or []),
        ]
    ).lower()
    ranked = sorted(
        resumes,
        key=lambda r: resume_match_score(r, text),
        reverse=True,
    )
    return ranked[0]


def resume_match_score(resume: dict[str, Any], vacancy_text: str) -> int:
    score = sum(5 for tag in resume.get("tags", []) if tag in vacancy_text)
    excerpt = str(resume.get("text_excerpt", "")).lower()
    for token in ["python", "ai", "automation", "llm", "rag", "product", "manager", "business analyst"]:
        if token in vacancy_text and token in excerpt:
            score += 3
    return score


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_role_title(vacancy: dict[str, Any]) -> str:
    raw = clean_text(vacancy.get("title"))
    if not raw:
        return "Python AI Engineer"
    company = clean_text(vacancy.get("company"))
    if company and company.lower() in raw.lower():
        raw = re.split(re.escape(company), raw, maxsplit=1, flags=re.I)[0].strip(" -|,")
    match = ROLE_TITLE_RE.match(raw)
    if match:
        raw = match.group("title")
    raw = re.split(r"\s+(?:Київ|Львів|Remote|Віддалено|Україна|Ukraine)\b", raw, maxsplit=1, flags=re.I)[0].strip(" -|,")
    words = raw.split()
    if len(words) > 8:
        raw = " ".join(words[:8]).strip(" -|,")
    return raw or "Python AI Engineer"


def salutation(company: Any) -> str:
    name = clean_text(company)
    if not name or name.lower() == "team":
        return "Hi,"
    return f"Hi {name} team,"


def human_fit_tags(vacancy: dict[str, Any]) -> str:
    ignored_tags = {"apply_candidate", "review", "digest", "low_priority"}
    tags = [clean_text(tag) for tag in (vacancy.get("fit_tags") or []) if clean_text(tag)]
    clean_tags = []
    for tag in tags[:4]:
        if tag.lower() in ignored_tags:
            continue
        tag = tag.replace("-", " ")
        if tag.lower() == "ai":
            tag = "AI engineering"
        elif tag.lower() == "llm":
            tag = "LLM workflows"
        elif tag.lower() == "rag":
            tag = "RAG systems"
        elif tag.lower() == "python":
            tag = "Python"
        clean_tags.append(tag)
    return ", ".join(clean_tags) or "Python, AI automation, and reliable backend engineering"


def draft_quality_errors(message: str) -> list[str]:
    errors = []
    low = message.lower()
    for phrase in BANNED_DRAFT_PHRASES:
        if phrase.lower() in low:
            errors.append(f"banned phrase: {phrase}")
    if re.search(r"\byour\s+.{90,}\s+role\b", message, re.I | re.S):
        errors.append("role title looks contaminated by description text")
    if re.search(r"\b[A-Z][A-Za-z_]+\.pdf\b", message):
        errors.append("resume file name leaked into message")
    return errors


def draft_cover_letter(vacancy: dict[str, Any], resume: dict[str, Any] | None) -> str:
    title = clean_role_title(vacancy)
    fit = human_fit_tags(vacancy)
    location_note = f"\n\nLocation preference: {settings().location_preferences}."
    message = (
        f"{salutation(vacancy.get('company'))}\n\n"
        f"I am interested in the \"{title}\" role because it matches my current focus: practical Python-based AI automation, "
        "LLM-assisted workflows, and reliable engineering systems.\n\n"
        "My background combines 15+ years of software delivery and technical leadership with recent hands-on work in "
        f"Python automation, AI integrations, workflow reliability, and testing discipline. The strongest fit signals are: {fit}."
        f"{location_note}\n\n"
        "For the last years I have worked mostly in independent and small-team/founder-style environments. I adapt deliberately "
        "to each team's delivery process and bring ownership, fast execution, and product/architecture judgment.\n\n"
        "Best regards,\nTaras Prystavskyj"
    )
    errors = draft_quality_errors(message)
    if errors:
        raise ValueError(f"draft cover letter failed quality gate: {'; '.join(errors)}")
    return message


def build_candidate_batch(limit: int = 10, output: Path | None = None) -> Path:
    out = output or ROOT / "data" / "job_waves" / f"candidate_batch_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    history = latest_submission_by_url()
    public_vacancies = sorted(
        [
            row
            for row in latest_observations()
            if is_active_public_vacancy(row) and terminal_submission_state(row, history) == ""
        ],
        key=batch_score,
        reverse=True,
    )
    inbox_offers = sorted([row for row in latest_inbox_offers() if is_actionable_inbox_offer(row)], key=batch_score, reverse=True)
    inbox_quota = min(len(inbox_offers), limit, max(1, min(3, limit)))
    public_quota = max(0, limit - inbox_quota)
    vacancies = sorted(public_vacancies[:public_quota] + inbox_offers[:inbox_quota], key=batch_score, reverse=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        fields = [
            "site",
            "url",
            "title",
            "company",
            "message",
            "message_file",
            "salary_usd",
            "linkedin",
            "resume_policy",
            "approved_resume_name",
            "approved_to_submit",
            "final_submit_allowed",
            "source_type",
            "recommendation",
            "score",
            "reason",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for vacancy in vacancies:
            resume = select_resume(vacancy)
            site = vacancy.get("source_site", "")
            recommendation = vacancy.get("recommendation", "apply_candidate")
            is_inbox = site == "djinni_inbox"
            writer.writerow(
                {
                    "site": site,
                    "url": vacancy.get("source_url", ""),
                    "title": vacancy.get("title", ""),
                    "company": vacancy.get("company", ""),
                    "message": draft_inbox_reply(vacancy, resume) if is_inbox else draft_cover_letter(vacancy, resume),
                    "message_file": "",
                    "salary_usd": DEFAULT_SALARY_USD,
                    "linkedin": DEFAULT_LINKEDIN,
                    "resume_policy": "no_resume",
                    "approved_resume_name": "",
                    "approved_to_submit": "false",
                    "final_submit_allowed": "false",
                    "source_type": vacancy.get("source_type", "public_vacancy"),
                    "recommendation": recommendation,
                    "score": str(vacancy.get("score", score_vacancy(vacancy))),
                    "reason": vacancy.get("reason", ""),
                }
            )
    return out


def batch_score(row: dict[str, Any]) -> int:
    if row.get("source_site") == "djinni_inbox":
        return int(row.get("score") or 0)
    return score_vacancy(row)


def draft_inbox_reply(vacancy: dict[str, Any], resume: dict[str, Any] | None) -> str:
    title = clean_role_title(vacancy) if vacancy.get("title") else "your opportunity"
    message = (
        f"{salutation(vacancy.get('company'))}\n\n"
        f"Thank you for reaching out about the \"{title}\" role. The opportunity looks relevant to my Python, AI automation, "
        "LLM workflow, and engineering ownership background.\n\n"
        f"My current preferences are: {settings().location_preferences}. Expected compensation can be discussed around "
        f"{DEFAULT_SALARY_USD} USD depending on scope and format.\n\n"
        "Best regards,\nTaras Prystavskyj"
    )
    errors = draft_quality_errors(message)
    if errors:
        raise ValueError(f"draft inbox reply failed quality gate: {'; '.join(errors)}")
    return message


def candidate_summary(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


if __name__ == "__main__":
    print(build_candidate_batch(limit=10))
