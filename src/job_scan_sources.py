from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from djinni_inbox_scan import scan_inbox
from dou_pipeline import run_once as run_dou_once
from job_apply_config import ROOT
from job_platforms import DiscoveryQuery
from job_platforms.workua import run_workua_public_search_once
from recruiter_response_scan import scan_recruiter_responses
from resume_index import build_resume_index
from robotaua_pipeline import (
    ROBOTA_BLOCKERS_PATH,
    ROBOTA_FETCH_DIR,
    ROBOTA_OBSERVATIONS_PATH,
    ROBOTA_OUTREACH_DRAFTS_PATH,
    ROBOTA_PROGRESS_SNAPSHOT_PATH,
    ROBOTA_REVIEW_QUEUE_PATH,
    ROBOTA_STATUS_EVENTS_PATH,
    run_once as run_robotaua_once,
)
from shared_job_db import DEFAULT_DB_PATH, append_event
from vacancy_pipeline import build_candidate_batch


DEFAULT_QUERY = "Python AI automation"
DEFAULT_LIMIT = 10
DEFAULT_MAX_PAGES = 1
SUMMARY_PATH = ROOT / "data" / "job_waves" / "all_sources_scan_summary.json"


def run_all_sources_scan(
    *,
    query_text: str = DEFAULT_QUERY,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    include_recruiter_responses: bool = True,
    execute_recruiter_auto_reply: bool = False,
    summary_path: Path = SUMMARY_PATH,
) -> dict[str, Any]:
    """Refresh every configured source and rebuild the candidate CSV.

    This function does not submit applications, upload resumes, save profiles, or
    click final send buttons. Site-specific discovery runners are bounded and
    write their usual local artifacts before the batch builder reads them.
    """

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    notes: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def record_error(key: str, exc: Exception) -> None:
        errors[key] = f"{type(exc).__name__}: {exc}"
        append_event("all_sources_scan_error", key, status="blocked", payload={"error": errors[key]}, db_path=DEFAULT_DB_PATH)

    try:
        build_resume_index()
        notes["resume_index"] = "refreshed"
    except Exception as exc:
        record_error("resume_index", exc)

    try:
        inbox = scan_inbox()
        notes["djinni_inbox"] = inbox
    except Exception as exc:
        record_error("djinni_inbox", exc)

    if include_recruiter_responses:
        try:
            response_result = scan_recruiter_responses(
                auto_reply=execute_recruiter_auto_reply,
                execute_auto_reply=execute_recruiter_auto_reply,
            )
            notes["djinni_recruiter_responses"] = {
                "threads": len(response_result.get("threads") or []),
                "auto_reply_events": len(response_result.get("auto_reply_events") or []),
            }
        except Exception as exc:
            record_error("djinni_recruiter_responses", exc)

    try:
        notes["dou"] = run_dou_with_fallbacks(query_text, limit=limit, max_pages=max_pages)
    except Exception as exc:
        record_error("dou", exc)

    try:
        query = DiscoveryQuery(text=query_text, limit=limit)
        workua = run_workua_public_search_once(query, db_path=DEFAULT_DB_PATH, artifact_dir=ROOT / "data" / "job_waves" / "workua_artifacts")
        notes["workua"] = {
            "jobs": len(workua.get("jobs_persisted") or []),
            "approvals": len(workua.get("approval_artifacts") or []),
            "blockers": len(workua.get("blocker_artifacts") or []),
            "artifact_path": workua.get("artifact_path", ""),
        }
    except Exception as exc:
        record_error("workua", exc)

    try:
        notes["robotaua"] = run_robotaua_once(
            query_text,
            "",
            limit,
            max_pages,
            0.0,
            ROBOTA_OBSERVATIONS_PATH,
            ROBOTA_OUTREACH_DRAFTS_PATH,
            ROBOTA_REVIEW_QUEUE_PATH,
            ROBOTA_PROGRESS_SNAPSHOT_PATH,
            ROBOTA_FETCH_DIR,
            db_path=DEFAULT_DB_PATH,
            status_path=ROBOTA_STATUS_EVENTS_PATH,
            blocker_path=ROBOTA_BLOCKERS_PATH,
        )
    except Exception as exc:
        record_error("robotaua", exc)

    batch = build_candidate_batch(limit=limit)
    summary = {
        "schema": "job.all_sources_scan.v0",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "query": query_text,
        "limit": limit,
        "max_pages": max_pages,
        "batch": str(batch),
        "notes": notes,
        "errors": errors,
        "safety": {
            "submits_applications": False,
            "uploads_resumes": False,
            "changes_profiles": False,
        },
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    append_event(
        "all_sources_scan_complete",
        "all",
        status="complete" if not errors else "blocked",
        payload={"batch": str(batch), "errors": errors, "notes": _compact_notes(notes)},
        db_path=DEFAULT_DB_PATH,
    )
    return summary


def run_dou_with_fallbacks(query_text: str, *, limit: int, max_pages: int) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    first = run_dou_once(query_text, limit=limit, max_pages=max(2, max_pages))
    attempts.append({"kind": "primary", "query": query_text, "summary": _dou_attempt_summary(first)})
    if _dou_has_observations(first):
        first["fallback_attempts"] = attempts
        return first

    description_url = "https://jobs.dou.ua/vacancies/?" + urllib.parse.urlencode({"search": query_text, "descr": "1"})
    description = run_dou_once(query_text, urls=[description_url], limit=limit, max_pages=1)
    attempts.append({"kind": "description_search", "query": query_text, "urls": [description_url], "summary": _dou_attempt_summary(description)})
    if _dou_has_observations(description):
        description["fallback_attempts"] = attempts
        return description

    seen_queries = {query_text.strip().lower()}
    for fallback_query in ["Python AI", "Python"]:
        if fallback_query.lower() in seen_queries:
            continue
        fallback = run_dou_once(fallback_query, limit=limit, max_pages=max(2, max_pages))
        attempts.append({"kind": "broader_query", "query": fallback_query, "summary": _dou_attempt_summary(fallback)})
        if _dou_has_observations(fallback):
            fallback["fallback_attempts"] = attempts
            return fallback
    first["fallback_attempts"] = attempts
    return first


def _dou_has_observations(summary: dict[str, Any]) -> bool:
    remediations = summary.get("listing_remediations") if isinstance(summary.get("listing_remediations"), dict) else {}
    return int(summary.get("observations") or 0) > 0 or int(remediations.get("exact_urls_extracted") or 0) > 0


def _dou_attempt_summary(summary: dict[str, Any]) -> dict[str, Any]:
    remediations = summary.get("listing_remediations") if isinstance(summary.get("listing_remediations"), dict) else {}
    return {
        "observations": int(summary.get("observations") or 0),
        "exact_urls_extracted": int(remediations.get("exact_urls_extracted") or 0),
        "blocked": int(remediations.get("blocked") or 0),
        "urls": summary.get("urls") or [],
    }


def format_scan_summary(summary: dict[str, Any]) -> str:
    notes = summary.get("notes") if isinstance(summary.get("notes"), dict) else {}
    errors = summary.get("errors") if isinstance(summary.get("errors"), dict) else {}
    parts = [f"all-sites scan built batch: {summary.get('batch', '')}"]
    for key in ["djinni_inbox", "djinni_recruiter_responses", "dou", "workua", "robotaua"]:
        if key in notes:
            parts.append(f"{key}=ok")
        if key in errors:
            parts.append(f"{key}=blocked ({errors[key]})")
    return "; ".join(parts)


def _compact_notes(notes: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in notes.items():
        if isinstance(value, dict):
            compact[key] = {k: value.get(k) for k in list(value)[:6]}
        else:
            compact[key] = str(value)[:300]
    return compact
