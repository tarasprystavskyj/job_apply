from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from job_apply_config import ROOT


WEB_RUNS_DIR = ROOT / "data" / "job_waves" / "web_runs"
UNRESOLVED_BLOCKERS_PATH = ROOT / "data" / "job_waves" / "unresolved_blockers.jsonl"
CANONICAL_SUBMISSION_LOGS = {
    "djinni": ROOT / "data" / "job_waves" / "djinni_csv_submission_attempts.jsonl",
    "workua": ROOT / "data" / "job_waves" / "workua_submission_attempts.jsonl",
    "dou": ROOT / "data" / "job_waves" / "dou_submission_attempts.jsonl",
    "robotaua": ROOT / "data" / "job_waves" / "robotaua_submission_attempts.jsonl",
}
BLOCKED_RESULTS = {
    "blocked_validation",
    "blocked_no_apply_form",
    "blocked_presubmit_validation",
    "blocked_no_application_surface",
    "blocked_no_apply_navigation",
    "blocked_fill_failed",
    "blocked_resume_upload",
    "blocked_policy_requires_resume",
    "blocked_login_required",
    "blocked_auth_required",
    "blocked_inactive",
    "blocked_upload_failed",
    "blocked_presave_validation",
}
RESOLVED_RESULTS = {"submitted_success", "submitted_success_inferred", "already_applied"}
BLOCKER_CLEARING_RESULTS = RESOLVED_RESULTS | {"prepared_presubmit_ok"}
DJINNI_WRONG_SUBMITTER_ROUTE_REASON = "only site=djinni is supported by this script; url must be a djinni job url"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def event_url(event: dict[str, Any]) -> str:
    return str(event.get("source_url") or event.get("url") or event.get("thread_url") or "").strip()


def normalize_result(event: dict[str, Any]) -> str:
    result = str(event.get("result") or "")
    if result == "submit_clicked_unconfirmed":
        after = event.get("after") if isinstance(event.get("after"), dict) else {}
        after_url = str(after.get("url") or "").lower()
        if "/jobseeker/my/resumes/sent/" in after_url or "applied=ok" in after_url or "?sent" in after_url or "sent#replied-id" in after_url:
            return "submitted_success_inferred"
        surfaces = after.get("application_surfaces") if isinstance(after.get("application_surfaces"), list) else []
        if any("sent" in str(surface.get("className") or "").lower() for surface in surfaces if isinstance(surface, dict)):
            return "submitted_success_inferred"
    return result


def iter_submission_events(
    log_dir: Path = WEB_RUNS_DIR,
    log_paths: dict[str, Path] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not log_dir.exists():
        web_paths: list[Path] = []
    else:
        web_paths = sorted(log_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
    for path in web_paths:
        for event in read_jsonl(path):
            event = dict(event)
            event["_log_file"] = path.name
            event["_log_mtime"] = path.stat().st_mtime
            events.append(event)
    if log_paths is None:
        log_paths = CANONICAL_SUBMISSION_LOGS if log_dir == WEB_RUNS_DIR else {}
    for site, path in log_paths.items():
        if not path.exists():
            continue
        for event in read_jsonl(path):
            event = dict(event)
            event.setdefault("site", site)
            event["_log_file"] = path.name
            event["_log_mtime"] = path.stat().st_mtime
            events.append(event)
    return events


def blocker_key(event: dict[str, Any]) -> str:
    site = str(event.get("site") or "")
    url = event_url(event)
    reason = blocker_reason(event)
    return "|".join([site, url.lower().rstrip("/"), reason.lower()])


def blocker_reason(event: dict[str, Any]) -> str:
    if event.get("blocked_reason"):
        return str(event["blocked_reason"])
    errors = event.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors)
    presubmit = event.get("presubmit")
    if isinstance(presubmit, dict) and isinstance(presubmit.get("errors"), list) and presubmit["errors"]:
        return "; ".join(str(item) for item in presubmit["errors"])
    result = str(event.get("result") or "blocked")
    return result.replace("_", " ")


def classify_blocker(event: dict[str, Any]) -> str:
    reason = blocker_reason(event).lower()
    result = str(event.get("result") or "").lower()
    if is_stale_wrong_submitter_route(event):
        return "stale_wrong_submitter_route"
    if "eligibility mismatch" in reason or event.get("eligibility_mismatch"):
        return "djinni_eligibility_mismatch"
    if "profile update required" in reason:
        return "profile_update_required"
    if "inactive vacancy" in reason or result == "blocked_inactive":
        return "inactive_vacancy"
    if "login" in reason or "auth" in reason or result in {"blocked_login_required", "blocked_auth_required"}:
        return "auth_required"
    if "resume upload" in result or "requires resume" in reason or "resume/apply method" in reason:
        return "resume_required_or_upload_failed"
    if "validation" in result or "must be true" in reason or "requires exact" in reason:
        return "validation_or_approval_gate"
    if result == "blocked_fill_failed":
        return "fill_or_selector_failed"
    if "no visible" in reason or "application surface" in reason or "no apply" in result:
        return "site_changed_or_no_apply_surface"
    return "unknown_blocker"


def is_djinni_job_url(url: str) -> bool:
    return url.lower().startswith("https://djinni.co/jobs/")


def is_stale_wrong_submitter_route(event: dict[str, Any]) -> bool:
    reason = blocker_reason(event).lower()
    if DJINNI_WRONG_SUBMITTER_ROUTE_REASON not in reason:
        return False
    site = str(event.get("site") or "").strip().lower()
    url = event_url(event)
    return site != "djinni" or not is_djinni_job_url(url)


def remediation_options(event: dict[str, Any]) -> list[dict[str, str]]:
    category = classify_blocker(event)
    profile_url = str(event.get("profile_update_url") or "https://djinni.co/my/profile/")
    if category == "profile_update_required":
        return [
            {
                "id": "manual_profile_update",
                "label": "Manual: open Djinni profile update page",
                "action": profile_url,
                "description": "Owner reviews and completes/activates profile fields, then reruns the approved send.",
            },
            {
                "id": "auto_profile_toggle_or_prepare",
                "label": "Auto: run bounded Djinni profile helper",
                "action": "POST /profile-on",
                "description": "Automation may try the safe profile toggle/fill path, then ask owner before retrying submit.",
            },
        ]
    if category == "djinni_eligibility_mismatch":
        return [
            {
                "id": "manual_skip_mismatch",
                "label": "Manual: skip this mismatched Djinni vacancy",
                "action": "skip",
                "description": "Company filters currently exclude this profile by salary/location; treat as low-priority warning, not a broken submitter.",
            },
            {
                "id": "manual_adjust_profile_or_salary",
                "label": "Manual: lower salary or adjust locations, then retry",
                "action": profile_url,
                "description": "Only if owner wants to fit this company filter; otherwise do not mutate profile for one vacancy.",
            },
        ]
    if category == "inactive_vacancy":
        return [
            {
                "id": "manual_skip",
                "label": "Manual: mark as skipped",
                "action": "skip",
                "description": "Vacancy is inactive; do not retry this URL.",
            },
            {
                "id": "auto_find_similar",
                "label": "Auto: find similar fresh vacancies",
                "action": "scan similar",
                "description": "Discovery agent searches for similar active roles and adds them to the review queue.",
            },
        ]
    if category == "resume_required_or_upload_failed":
        return [
            {
                "id": "manual_choose_resume",
                "label": "Manual: approve exact resume file/profile",
                "action": "approve exact resume",
                "description": "Owner selects an exact resume/profile to use for this platform.",
            },
            {
                "id": "auto_prepare_upload_flow",
                "label": "Auto: inspect upload/apply form and propose patch",
                "action": "spawn blocker resolver agent",
                "description": "Agent inspects the form blocker and prepares a bounded code or selector fix for review.",
            },
        ]
    if category == "auth_required":
        return [
            {
                "id": "manual_login",
                "label": "Manual: log in in the visible browser",
                "action": "login",
                "description": "Owner logs in or refreshes the session, then reruns the approved send.",
            },
            {
                "id": "auto_recheck_session",
                "label": "Auto: recheck session after owner login",
                "action": "scan session",
                "description": "Automation verifies the page state without reading cookies or profiles.",
            },
        ]
    return [
        {
            "id": "manual_review_blocker",
            "label": "Manual: review blocker details",
            "action": event_url(event),
            "description": "Owner chooses whether to skip, retry, or allow an automated resolver.",
        },
        {
            "id": "auto_spawn_resolver",
            "label": "Auto: spawn resolver agent",
            "action": "spawn blocker resolver agent",
            "description": "Agent inspects logs and code, proposes a bounded fix, then requires tests before retry.",
        },
    ]


def build_blocker_record(event: dict[str, Any]) -> dict[str, Any]:
    category = classify_blocker(event)
    profile_update_url = str(event.get("profile_update_url") or "")
    if category == "profile_update_required" and not profile_update_url:
        profile_update_url = "https://djinni.co/my/profile/"
    return {
        "schema": "job.blocker_resolution.v0",
        "key": blocker_key(event),
        "status": "unresolved",
        "category": category,
        "site": str(event.get("site") or ""),
        "company": str(event.get("company") or ""),
        "title": str(event.get("title") or ""),
        "source_url": event_url(event),
        "result": str(event.get("result") or ""),
        "blocked_reason": blocker_reason(event),
        "profile_update_url": profile_update_url,
        "first_seen_at": str(event.get("attempted_at") or event.get("created_at") or ""),
        "last_seen_at": str(event.get("attempted_at") or event.get("created_at") or ""),
        "last_log_file": str(event.get("_log_file") or ""),
        "agent_task": {
            "status": "propose_options_to_owner",
            "instruction": "Offer owner manual action versus safe automatic resolver for this blocker before retrying submit.",
            "model_hint": "gpt-5.5 high for code-changing blocker resolvers",
        },
        "resolution_options": remediation_options(event),
    }


def blocker_site_url_key(record: dict[str, Any]) -> str:
    site = str(record.get("site") or "")
    url = str(record.get("source_url") or "").lower().rstrip("/")
    return "|".join([site, url])


def blocker_url_key(record: dict[str, Any]) -> str:
    return str(record.get("source_url") or "").lower().rstrip("/")


def is_more_specific_blocker(record: dict[str, Any]) -> bool:
    return str(record.get("category") or "") != "unknown_blocker"


def drop_stale_unknown_blockers(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    newest_specific_seen: dict[str, str] = {}
    newest_specific_seen_by_url: dict[str, str] = {}
    newest_eligibility_mismatch: dict[str, str] = {}
    for record in records:
        if not is_more_specific_blocker(record):
            continue
        key = blocker_site_url_key(record)
        url_key = blocker_url_key(record)
        last_seen = str(record.get("last_seen_at") or "")
        if record.get("category") == "djinni_eligibility_mismatch":
            if last_seen > newest_eligibility_mismatch.get(key, ""):
                newest_eligibility_mismatch[key] = last_seen
        if last_seen > newest_specific_seen.get(key, ""):
            newest_specific_seen[key] = last_seen
        if url_key and last_seen > newest_specific_seen_by_url.get(url_key, ""):
            newest_specific_seen_by_url[url_key] = last_seen

    filtered: list[dict[str, Any]] = []
    for record in records:
        if record.get("category") == "profile_update_required":
            newer_mismatch_seen = newest_eligibility_mismatch.get(blocker_site_url_key(record), "")
            if newer_mismatch_seen and newer_mismatch_seen > str(record.get("last_seen_at") or ""):
                continue
        if is_more_specific_blocker(record):
            filtered.append(record)
            continue
        newer_specific_seen = newest_specific_seen.get(blocker_site_url_key(record), "")
        if not newer_specific_seen and not str(record.get("site") or ""):
            newer_specific_seen = newest_specific_seen_by_url.get(blocker_url_key(record), "")
        if newer_specific_seen and newer_specific_seen > str(record.get("last_seen_at") or ""):
            continue
        filtered.append(record)
    return filtered


def collect_unresolved_blockers(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved_urls = {event_url(event).lower().rstrip("/") for event in events if normalize_result(event) in BLOCKER_CLEARING_RESULTS}
    records: dict[str, dict[str, Any]] = {}
    for event in events:
        result = normalize_result(event)
        url_key = event_url(event).lower().rstrip("/")
        if not url_key or url_key in resolved_urls:
            continue
        if result not in BLOCKED_RESULTS and not result.startswith("blocked_"):
            continue
        if is_stale_wrong_submitter_route(event):
            continue
        record = build_blocker_record(event)
        key = str(record["key"])
        if key in records:
            records[key]["last_seen_at"] = record["last_seen_at"]
            records[key]["last_log_file"] = record["last_log_file"]
            records[key]["result"] = record["result"]
            if record.get("profile_update_url"):
                records[key]["profile_update_url"] = record["profile_update_url"]
            if record.get("resolution_options"):
                records[key]["resolution_options"] = record["resolution_options"]
        else:
            records[key] = record
    return sorted(
        drop_stale_unknown_blockers(list(records.values())),
        key=lambda row: str(row.get("last_seen_at") or ""),
        reverse=True,
    )


def refresh_unresolved_blockers(
    log_dir: Path = WEB_RUNS_DIR,
    output: Path = UNRESOLVED_BLOCKERS_PATH,
    log_paths: dict[str, Path] | None = None,
) -> list[dict[str, Any]]:
    blockers = collect_unresolved_blockers(iter_submission_events(log_dir, log_paths=log_paths))
    write_jsonl(output, blockers)
    return blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh unresolved job application blockers from submission logs.")
    parser.add_argument("--log-dir", type=Path, default=WEB_RUNS_DIR)
    parser.add_argument("--output", type=Path, default=UNRESOLVED_BLOCKERS_PATH)
    args = parser.parse_args(argv)
    blockers = refresh_unresolved_blockers(args.log_dir, args.output)
    print(json.dumps({"output": str(args.output), "unresolved_blockers": len(blockers)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
