from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from job_apply_config import ROOT, settings
from resume_index import list_resumes


DEFAULT_LINKEDIN = "https://www.linkedin.com/in/taras-prystavskyj/"
DEFAULT_SALARY_USD = 3000


def latest_observations() -> list[dict[str, Any]]:
    path = ROOT / "data" / "job_waves" / "wave_2026-06-11_ai_automation_observations.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


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


def draft_cover_letter(vacancy: dict[str, Any], resume: dict[str, Any] | None) -> str:
    company = vacancy.get("company") or "team"
    title = vacancy.get("title") or "this role"
    fit = ", ".join((vacancy.get("fit_tags") or [])[:5]) or "Python and AI automation"
    resume_note = f" I would position the most relevant resume as {resume['name']}." if resume else ""
    location_note = f"\n\nLocation preference: {settings().location_preferences}."
    return (
        f"Hi {company} team,\n\n"
        f"I am interested in your {title} role because it matches my current focus: practical Python-based AI automation, "
        f"LLM-assisted workflows, and reliable engineering systems.\n\n"
        f"My background combines 15+ years of software delivery and technical leadership with recent hands-on work in "
        f"multi-agent workflows, review artifacts, logs, tests, diffs, and scoped tool use. The strongest fit signals I see are: {fit}."
        f"{resume_note}{location_note}\n\n"
        "I should be transparent that I have worked independently and in small-team/founder-style environments for many years, "
        "so I would align carefully with your team process and delivery expectations. I bring ownership, fast execution, and "
        "product/architecture judgment.\n\n"
        "Best regards,\nTaras Prystavskyj"
    )


def build_candidate_batch(limit: int = 10, output: Path | None = None) -> Path:
    out = output or ROOT / "data" / "job_waves" / f"candidate_batch_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    vacancies = sorted(latest_observations(), key=score_vacancy, reverse=True)[:limit]
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
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for vacancy in vacancies:
            resume = select_resume(vacancy)
            writer.writerow(
                {
                    "site": vacancy.get("source_site", ""),
                    "url": vacancy.get("source_url", ""),
                    "title": vacancy.get("title", ""),
                    "company": vacancy.get("company", ""),
                    "message": draft_cover_letter(vacancy, resume),
                    "message_file": "",
                    "salary_usd": DEFAULT_SALARY_USD,
                    "linkedin": DEFAULT_LINKEDIN,
                    "resume_policy": "no_resume",
                    "approved_resume_name": "",
                    "approved_to_submit": "false",
                    "final_submit_allowed": "false",
                }
            )
    return out


def candidate_summary(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


if __name__ == "__main__":
    print(build_candidate_batch(limit=10))
