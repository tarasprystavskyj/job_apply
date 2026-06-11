#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

from job_apply_config import ROOT
from job_platforms import DiscoveryQuery, ProgressEdge, ProgressNode, ProgressSnapshot, ResumeArtifact
from job_platforms.platforms import RobotaUaAdapter
from shared_job_db import DEFAULT_DB_PATH, add_outreach_draft, append_event, upsert_job, upsert_resume
from vacancy_pipeline import DEFAULT_LINKEDIN, DEFAULT_SALARY_USD, score_vacancy


ROBOTA_OBSERVATIONS_PATH = ROOT / "data" / "job_waves" / "robotaua_observations.jsonl"
ROBOTA_OUTREACH_DRAFTS_PATH = ROOT / "data" / "job_waves" / "robotaua_outreach_drafts.jsonl"
ROBOTA_STATUS_EVENTS_PATH = ROOT / "data" / "job_waves" / "robotaua_status_events.jsonl"
ROBOTA_REVIEW_QUEUE_PATH = ROOT / "data" / "job_waves" / f"robotaua_review_queue_{time.strftime('%Y%m%d_%H%M%S')}.csv"
ROBOTA_PROGRESS_SNAPSHOT_PATH = ROOT / "data" / "job_waves" / "robotaua_progress_snapshot.json"


PIPELINE_STAGES = [
    ("discover", "Discover", "Public search/listing URLs only"),
    ("normalize", "Normalize", "job.vacancy_observation.v0"),
    ("score", "Score", "Local fit scoring"),
    ("draft", "Draft", "Local message draft"),
    ("review_approval", "Review Approval", "Owner review required"),
    ("manual_handoff", "Manual Handoff", "No Robota.ua submit automation"),
    ("status_tracking", "Status Tracking", "Append-only local events"),
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]], append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def observation_score(row: dict[str, Any]) -> int:
    score = score_vacancy(row)
    text = " ".join(
        [
            str(row.get("title", "")),
            str(row.get("company", "")),
            str(row.get("summary", "")),
            str(row.get("location", "")),
        ]
    ).lower()
    if "remote" in text or "віддал" in text:
        score += 2
    if "manual_handoff_only" in [str(x) for x in row.get("risk_flags") or []]:
        score -= 1
    return score


def status_event(stage: str, status: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": "job.platform_status_event.v0",
        "platform": "robotaua",
        "stage": stage,
        "status": status,
        "message": message,
        "event_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "data": data or {},
    }


def default_resume_artifact() -> ResumeArtifact:
    # This intentionally anticipates the shared resume DB without reading
    # data/private/resume_index.json, because that file can contain private CV
    # excerpts. Owner review can later replace this metadata-only handle.
    return ResumeArtifact(display_name="owner_selected_resume_metadata_pending", status="needs_owner_review")


def normalize_html(
    html_path: Path,
    source_url: str,
    query_text: str,
    output: Path,
    append: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
    status_path: Path = ROBOTA_STATUS_EVENTS_PATH,
) -> dict[str, Any]:
    adapter = RobotaUaAdapter()
    query = DiscoveryQuery(text=query_text or "Python AI")
    observations = adapter.extract_public_vacancies(html_path.read_text(encoding="utf-8", errors="replace"), source_url, query)
    rows = [observation.to_artifact() for observation in observations]
    write_jsonl(output, rows, append=append)
    job_ids = [upsert_job(observation, score=observation_score(observation.to_artifact()), db_path=db_path) for observation in observations]
    event = status_event(
        "normalize",
        "complete",
        f"normalized {len(rows)} public Robota.ua vacancies",
        {"source_url": source_url, "output": str(output), "db": str(db_path), "job_ids": job_ids},
    )
    write_jsonl(status_path, [event], append=True)
    append_event("robotaua_normalized", "robotaua", status="complete", payload=event, db_path=db_path)
    return {"observations": len(rows), "output": str(output), "status_event": event}


def build_review_queue(
    observations_path: Path,
    drafts_path: Path,
    review_csv_path: Path,
    progress_path: Path,
    limit: int,
    db_path: Path = DEFAULT_DB_PATH,
    status_path: Path = ROBOTA_STATUS_EVENTS_PATH,
) -> dict[str, Any]:
    adapter = RobotaUaAdapter()
    resume = default_resume_artifact()
    resume_id = upsert_resume(resume, db_path=db_path)
    observations = read_jsonl(observations_path)
    ranked = sorted(observations, key=observation_score, reverse=True)[:limit]

    drafts: list[dict[str, Any]] = []
    outreach_ids: list[int] = []
    review_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with review_csv_path.open("w", encoding="utf-8", newline="") as f:
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
            "manual_handoff_required",
            "status",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in ranked:
            vacancy = adapter.normalize_vacancy(row)
            job_id = upsert_job(vacancy, score=observation_score(row), db_path=db_path)
            draft = adapter.draft_outreach(vacancy, resume)
            outreach_ids.append(add_outreach_draft(job_id, draft, resume_id=resume_id, db_path=db_path))
            draft_artifact = draft.to_artifact()
            draft_artifact["score"] = observation_score(row)
            draft_artifact["manual_handoff_required"] = True
            drafts.append(draft_artifact)
            writer.writerow(
                {
                    "site": "robotaua",
                    "url": vacancy.source_url,
                    "title": vacancy.title,
                    "company": vacancy.company,
                    "message": draft.cover_letter,
                    "message_file": "",
                    "salary_usd": DEFAULT_SALARY_USD,
                    "linkedin": DEFAULT_LINKEDIN,
                    "resume_policy": "manual_owner_review",
                    "approved_resume_name": "",
                    "approved_to_submit": "false",
                    "final_submit_allowed": "false",
                    "source_type": "public_vacancy",
                    "recommendation": "review" if observation_score(row) >= 8 else "low_priority",
                    "score": str(observation_score(row)),
                    "reason": "Robota.ua is manual handoff only until a separate apply adapter is approved.",
                    "manual_handoff_required": "true",
                    "status": "needs_owner_review",
                }
            )

    write_jsonl(drafts_path, drafts)
    event = status_event(
        "draft",
        "complete",
        f"built {len(drafts)} Robota.ua review drafts",
        {"review_csv": str(review_csv_path), "drafts": str(drafts_path), "db": str(db_path), "outreach_ids": outreach_ids},
    )
    write_jsonl(status_path, [event], append=True)
    append_event("robotaua_review_queue_built", "robotaua", status="needs_owner_review", payload=event, db_path=db_path)
    snapshot = build_progress_snapshot(observations_path, drafts_path, status_path)
    write_json(progress_path, snapshot.to_dict())
    return {
        "rows": len(drafts),
        "review_csv": str(review_csv_path),
        "drafts": str(drafts_path),
        "progress": str(progress_path),
        "status_event": event,
    }


def build_progress_snapshot(
    observations_path: Path = ROBOTA_OBSERVATIONS_PATH,
    drafts_path: Path = ROBOTA_OUTREACH_DRAFTS_PATH,
    status_path: Path = ROBOTA_STATUS_EVENTS_PATH,
) -> ProgressSnapshot:
    observations = read_jsonl(observations_path)
    drafts = read_jsonl(drafts_path)
    events = read_jsonl(status_path)
    has_observations = bool(observations)
    has_drafts = bool(drafts)

    statuses = {
        "discover": "ready",
        "normalize": "complete" if has_observations else "pending",
        "score": "complete" if has_observations else "pending",
        "draft": "complete" if has_drafts else "pending",
        "review_approval": "blocked_waiting_owner" if has_drafts else "pending",
        "manual_handoff": "blocked_waiting_owner" if has_drafts else "pending",
        "status_tracking": "active" if events else "pending",
    }
    nodes = [
        ProgressNode(
            id=stage,
            label=label,
            kind="gate" if stage in {"review_approval", "manual_handoff"} else "stage",
            status=statuses[stage],
            data={"description": description},
        )
        for stage, label, description in PIPELINE_STAGES
    ]
    edges = [
        ProgressEdge(
            source=PIPELINE_STAGES[idx][0],
            target=PIPELINE_STAGES[idx + 1][0],
            label="next",
            status="available" if statuses[PIPELINE_STAGES[idx][0]] in {"complete", "ready", "active"} else "pending",
        )
        for idx in range(len(PIPELINE_STAGES) - 1)
    ]
    return ProgressSnapshot(
        schema="job.progress_snapshot.langgraph.v0",
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        nodes=nodes,
        edges=edges,
        event_tail=events[-20:],
    )


def print_discovery_urls(query_text: str, location: str, limit: int) -> None:
    adapter = RobotaUaAdapter()
    urls = adapter.discovery_urls(DiscoveryQuery(text=query_text, location=location, limit=limit))
    print(json.dumps({"platform": "robotaua", "urls": urls}, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe Robota.ua discovery/draft/status pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    urls = sub.add_parser("discovery-urls")
    urls.add_argument("--query", default="Python AI")
    urls.add_argument("--location", default="")
    urls.add_argument("--limit", type=int, default=25)

    normalize = sub.add_parser("normalize-html")
    normalize.add_argument("--html", type=Path, required=True)
    normalize.add_argument("--source-url", default="https://robota.ua/zapros/Python+AI")
    normalize.add_argument("--query", default="Python AI")
    normalize.add_argument("--output", type=Path, default=ROBOTA_OBSERVATIONS_PATH)
    normalize.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    normalize.add_argument("--status", type=Path, default=ROBOTA_STATUS_EVENTS_PATH)
    normalize.add_argument("--append", action="store_true")

    draft = sub.add_parser("build-review")
    draft.add_argument("--observations", type=Path, default=ROBOTA_OBSERVATIONS_PATH)
    draft.add_argument("--drafts", type=Path, default=ROBOTA_OUTREACH_DRAFTS_PATH)
    draft.add_argument("--review-csv", type=Path, default=ROBOTA_REVIEW_QUEUE_PATH)
    draft.add_argument("--progress", type=Path, default=ROBOTA_PROGRESS_SNAPSHOT_PATH)
    draft.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    draft.add_argument("--status", type=Path, default=ROBOTA_STATUS_EVENTS_PATH)
    draft.add_argument("--limit", type=int, default=10)

    progress = sub.add_parser("progress")
    progress.add_argument("--observations", type=Path, default=ROBOTA_OBSERVATIONS_PATH)
    progress.add_argument("--drafts", type=Path, default=ROBOTA_OUTREACH_DRAFTS_PATH)
    progress.add_argument("--status", type=Path, default=ROBOTA_STATUS_EVENTS_PATH)
    progress.add_argument("--output", type=Path, default=ROBOTA_PROGRESS_SNAPSHOT_PATH)

    args = parser.parse_args(argv)
    if args.command == "discovery-urls":
        print_discovery_urls(args.query, args.location, args.limit)
        return 0
    if args.command == "normalize-html":
        result = normalize_html(
            args.html,
            args.source_url,
            args.query,
            args.output,
            append=args.append,
            db_path=args.db,
            status_path=args.status,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "build-review":
        result = build_review_queue(
            args.observations,
            args.drafts,
            args.review_csv,
            args.progress,
            args.limit,
            db_path=args.db,
            status_path=args.status,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "progress":
        snapshot = build_progress_snapshot(args.observations, args.drafts, args.status)
        write_json(args.output, snapshot.to_dict())
        print(json.dumps({"progress": str(args.output), "nodes": len(snapshot.nodes), "edges": len(snapshot.edges)}, indent=2))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
