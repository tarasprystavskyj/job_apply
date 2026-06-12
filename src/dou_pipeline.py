from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from job_apply_config import ROOT
from job_platforms.dou import (
    DouAdapter,
    build_dou_progress_snapshot,
    build_dou_review_drafts,
    exact_dou_vacancy_observations,
    is_dou_listing_url,
    progress_gate_artifact,
    score_dou_observation,
)
from job_platforms.models import DiscoveryQuery, VacancyObservation
from shared_job_db import DEFAULT_DB_PATH, append_event


DEFAULT_OBSERVATIONS_PATH = ROOT / "data" / "job_waves" / "dou_observations.jsonl"
DEFAULT_PROGRESS_PATH = ROOT / "data" / "job_waves" / "dou_progress_snapshot.json"
DEFAULT_SUMMARY_PATH = ROOT / "data" / "job_waves" / "dou_run_once_summary.json"
DEFAULT_BLOCKERS_PATH = ROOT / "data" / "job_waves" / "dou_blockers.json"
DEFAULT_REMEDIATIONS_PATH = ROOT / "data" / "job_waves" / "dou_listing_remediations.jsonl"

FetchText = Callable[[str, int], str]


def fetch_public_html(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "job-apply-dou-public-discovery/0.1 (+review-only; no apply actions)",
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            raise ValueError(f"refusing non-HTML DOU response from {url}: {content_type}")
        return response.read().decode("utf-8", errors="replace")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _dedupe_observations(observations: list[VacancyObservation], limit: int) -> list[VacancyObservation]:
    seen: set[str] = set()
    deduped: list[VacancyObservation] = []
    for observation in sorted(observations, key=score_dou_observation, reverse=True):
        key = observation.source_url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(observation)
        if len(deduped) >= limit:
            break
    return deduped


def _approval_artifact(observations: list[VacancyObservation]) -> dict[str, Any]:
    return {
        "schema": "job.dou_approval_blockers.v0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "final_submit_implemented": False,
        "blocked_actions": [
            "submit/apply/send",
            "resume upload",
            "account/profile changes",
            "reading cookies, saved browser profiles, .env, or private resume text",
        ],
        "owner_approval_required": [
            "exact vacancy",
            "exact message",
            "exact resume metadata or manual no-resume decision",
            "separate final submit permission if a future submitter is implemented",
        ],
        "vacancies_needing_review": [
            {
                "source_url": observation.source_url,
                "title": observation.title,
                "company": observation.company,
                "score": score_dou_observation(observation),
                "risk_flags": list(observation.risk_flags),
            }
            for observation in observations
        ],
    }


def _remediation_artifact(
    source_url: str,
    observations: list[VacancyObservation],
    *,
    error: str = "",
) -> dict[str, Any]:
    exact = exact_dou_vacancy_observations(observations)
    return {
        "schema": "job.dou_listing_remediation.v0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_url": source_url,
        "remediation": "public_listing_exact_vacancy_extraction",
        "status": "exact_urls_extracted" if exact else "no_exact_url_extracted",
        "exact_urls": [observation.source_url for observation in exact],
        "observations": [
            observation.to_artifact() | {"score": score_dou_observation(observation)}
            for observation in exact
        ],
        "error": error,
        "approval_required": [
            "owner must approve the exact extracted vacancy URL",
            "owner must approve the exact message",
            "owner must approve resume/profile policy",
            "owner must allow final submit for that exact row",
        ],
        "safe_actions_only": True,
    }


def run_once(
    query_text: str,
    *,
    urls: list[str] | None = None,
    limit: int = 10,
    max_pages: int = 2,
    timeout: int = 15,
    db_path: Path = DEFAULT_DB_PATH,
    observations_path: Path = DEFAULT_OBSERVATIONS_PATH,
    progress_path: Path = DEFAULT_PROGRESS_PATH,
    summary_path: Path = DEFAULT_SUMMARY_PATH,
    blockers_path: Path = DEFAULT_BLOCKERS_PATH,
    remediations_path: Path = DEFAULT_REMEDIATIONS_PATH,
    persist: bool = True,
    fetch_text: FetchText = fetch_public_html,
) -> dict[str, Any]:
    adapter = DouAdapter()
    query = DiscoveryQuery(text=query_text, limit=limit)
    source_urls = urls or adapter.discovery_urls(query)
    bounded_urls = source_urls[:max_pages]

    observations: list[VacancyObservation] = []
    fetch_errors: list[dict[str, str]] = []
    remediations: list[dict[str, Any]] = []
    for url in bounded_urls:
        adapter.normalize_url(url)
        is_listing = is_dou_listing_url(url)
        try:
            markup = fetch_text(url, timeout)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            fetch_errors.append({"url": url, "error": str(exc)})
            if is_listing:
                remediations.append(_remediation_artifact(url, [], error=str(exc)))
            continue
        extracted = adapter.extract_public_vacancies(markup, url, query)
        if is_listing:
            remediations.append(_remediation_artifact(url, extracted))
            extracted = exact_dou_vacancy_observations(extracted)
        observations.extend(extracted)

    ranked = _dedupe_observations(observations, limit)
    observation_rows = [observation.to_artifact() | {"score": score_dou_observation(observation)} for observation in ranked]
    blockers = _approval_artifact(ranked)

    draft_result: dict[str, Any] = {"jobs": 0, "outreach": 0, "job_ids": [], "outreach_ids": []}
    if persist and ranked:
        draft_result = build_dou_review_drafts(ranked, db_path=db_path, limit=limit)
    if persist and fetch_errors:
        append_event("dou_public_fetch_errors", "dou", None, None, "blocked", {"errors": fetch_errors}, db_path)
    if persist and not ranked:
        append_event("dou_no_public_vacancies_found", "dou", None, None, "blocked", {"query": query_text, "urls": bounded_urls}, db_path)

    progress = progress_gate_artifact(build_dou_progress_snapshot(db_path)) if persist else {}
    summary = {
        "schema": "job.dou_run_once_summary.v0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "query": query_text,
        "urls": bounded_urls,
        "max_pages": max_pages,
        "limit": limit,
        "persisted": persist,
        "observations": len(ranked),
        "draft_result": draft_result,
        "fetch_errors": fetch_errors,
        "artifacts": {
            "observations": str(observations_path),
            "progress": str(progress_path) if persist else "",
            "summary": str(summary_path),
            "blockers": str(blockers_path),
            "listing_remediations": str(remediations_path),
        },
        "listing_remediations": {
            "attempted": len(remediations),
            "exact_urls_extracted": sum(len(row.get("exact_urls") or []) for row in remediations),
            "blocked": len([row for row in remediations if row.get("status") == "no_exact_url_extracted"]),
        },
        "safety": {
            "public_pages_only": True,
            "final_submit_implemented": False,
            "submission_allowed": False,
            "upload_allowed": False,
        },
    }

    _write_jsonl(observations_path, observation_rows)
    _write_jsonl(remediations_path, remediations)
    _write_json(blockers_path, blockers)
    if persist:
        _write_json(progress_path, progress)
    _write_json(summary_path, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded public DOU/Relocate DOU discovery pass.")
    parser.add_argument("--query", default="Python AI", help="Public DOU search text.")
    parser.add_argument("--url", action="append", default=[], help="Public DOU URL to fetch instead of generated search URLs.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum normalized vacancies to persist.")
    parser.add_argument("--max-pages", type=int, default=2, help="Maximum public pages to fetch.")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS_PATH)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS_PATH)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--blockers", type=Path, default=DEFAULT_BLOCKERS_PATH)
    parser.add_argument("--remediations", type=Path, default=DEFAULT_REMEDIATIONS_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and normalize, but do not write shared DB progress/drafts.")
    args = parser.parse_args(argv)

    limit = max(1, min(args.limit, 50))
    max_pages = max(1, min(args.max_pages, 4))
    summary = run_once(
        args.query,
        urls=args.url or None,
        limit=limit,
        max_pages=max_pages,
        timeout=max(3, min(args.timeout, 60)),
        db_path=args.db,
        observations_path=args.observations,
        progress_path=args.progress,
        summary_path=args.summary,
        blockers_path=args.blockers,
        remediations_path=args.remediations,
        persist=not args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
