from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_platforms.models import ProgressEdge, ProgressNode, ProgressSnapshot, utcish_now
from shared_job_db import DEFAULT_DB_PATH, fetch_progress_rows


PIPELINE = [
    ("discover", "Discover"),
    ("normalize", "Normalize"),
    ("rank", "Rank"),
    ("draft", "Draft"),
    ("review", "Owner Review"),
    ("prepare", "Prepare"),
    ("submit", "Final Submit"),
]


TERMINAL_SUBMITTED = {"submitted", "submitted_success", "already_applied"}
BLOCKED = {"blocked", "blocked_validation", "blocked_no_apply_form", "blocked_presubmit_validation", "error"}


def status_rank(statuses: list[str]) -> str:
    clean = {status for status in statuses if status}
    if clean & BLOCKED:
        return "blocked"
    if clean & TERMINAL_SUBMITTED:
        return "complete"
    if clean & {"approved_to_submit", "prepared"}:
        return "ready"
    if clean & {"needs_owner_review", "drafted"}:
        return "active"
    if clean & {"observed", "shortlisted"}:
        return "seen"
    return "pending"


def stage_status(stage: str, jobs: list[dict[str, Any]], outreach: list[dict[str, Any]]) -> str:
    job_statuses = [str(row.get("status", "")) for row in jobs]
    outreach_statuses = [str(row.get("approval_status", "")) for row in outreach]
    if stage in {"discover", "normalize"}:
        return "complete" if jobs else "pending"
    if stage == "rank":
        return "complete" if any(int(row.get("score") or 0) for row in jobs) else ("seen" if jobs else "pending")
    if stage == "draft":
        return "complete" if outreach else "pending"
    if stage == "review":
        return status_rank(outreach_statuses)
    if stage == "prepare":
        return "ready" if any(row.get("approved_to_prepare") or row.get("approved_to_submit") for row in outreach) else "pending"
    if stage == "submit":
        clean = {status for status in job_statuses + outreach_statuses if status}
        if clean & BLOCKED:
            return "blocked"
        if clean & TERMINAL_SUBMITTED:
            return "complete"
        if any(row.get("approved_to_submit") and row.get("final_submit_allowed") for row in outreach):
            return "ready"
        return "pending"
    return "pending"


def build_progress_snapshot(db_path: Path = DEFAULT_DB_PATH, limit: int = 100) -> ProgressSnapshot:
    rows = fetch_progress_rows(db_path, limit=limit)
    jobs = rows["jobs"]
    outreach = rows["outreach"]
    events = rows["events"]

    nodes: list[ProgressNode] = []
    edges: list[ProgressEdge] = []

    for idx, (stage_id, label) in enumerate(PIPELINE):
        nodes.append(
            ProgressNode(
                id=f"stage:{stage_id}",
                label=label,
                kind="pipeline_stage",
                status=stage_status(stage_id, jobs, outreach),
                data={"order": idx + 1},
            )
        )
        if idx:
            previous = PIPELINE[idx - 1][0]
            edges.append(ProgressEdge(source=f"stage:{previous}", target=f"stage:{stage_id}", status="pipeline"))

    for job in jobs:
        job_id = int(job["id"])
        title = str(job.get("title") or "Untitled")
        company = str(job.get("company") or "")
        label = f"{company} - {title}" if company else title
        nodes.append(
            ProgressNode(
                id=f"job:{job_id}",
                label=label[:160],
                kind="job",
                status=str(job.get("status") or "observed"),
                data={
                    "platform": job.get("platform", ""),
                    "source_url": job.get("source_url", ""),
                    "score": job.get("score", 0),
                    "fit_tags": job.get("fit_tags", []),
                    "risk_flags": job.get("risk_flags", []),
                },
            )
        )
        edges.append(ProgressEdge(source="stage:normalize", target=f"job:{job_id}", label="observed", status="seen"))
        if int(job.get("score") or 0):
            edges.append(ProgressEdge(source=f"job:{job_id}", target="stage:rank", label="scored", status="complete"))

    for draft in outreach:
        outreach_id = int(draft["id"])
        job_id = int(draft["job_id"])
        approved_to_submit = bool(draft.get("approved_to_submit"))
        final_submit_allowed = bool(draft.get("final_submit_allowed"))
        status = str(draft.get("approval_status") or "needs_owner_review")
        if approved_to_submit and final_submit_allowed:
            status = "approved_to_submit"
        nodes.append(
            ProgressNode(
                id=f"outreach:{outreach_id}",
                label=f"Draft #{outreach_id}",
                kind="outreach_draft",
                status=status,
                data={
                    "job_id": job_id,
                    "channel": draft.get("channel", ""),
                    "message_language": draft.get("message_language", ""),
                    "approved_to_prepare": bool(draft.get("approved_to_prepare")),
                    "approved_to_submit": approved_to_submit,
                    "final_submit_allowed": final_submit_allowed,
                    "submission_allowed": bool(draft.get("submission_allowed")),
                    "upload_allowed": bool(draft.get("upload_allowed")),
                },
            )
        )
        edges.append(ProgressEdge(source=f"job:{job_id}", target=f"outreach:{outreach_id}", label="draft", status="active"))
        edges.append(ProgressEdge(source=f"outreach:{outreach_id}", target="stage:review", label="review gate", status=status))
        if approved_to_submit and final_submit_allowed:
            edges.append(ProgressEdge(source=f"outreach:{outreach_id}", target="stage:submit", label="final gate", status="ready"))

    return ProgressSnapshot(
        schema="job.progress_snapshot.v0",
        generated_at=utcish_now(),
        nodes=nodes,
        edges=edges,
        event_tail=events,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print graph-shaped job application progress JSON.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    print(json.dumps(build_progress_snapshot(args.db, args.limit).to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
