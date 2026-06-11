from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from job_apply_config import ROOT
from job_platforms.models import OutreachDraft, ResumeArtifact, VacancyObservation


DEFAULT_DB_PATH = ROOT / "data" / "job_waves" / "job_apply_shared.sqlite3"
SCHEMA_VERSION = 1


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@contextmanager
def connect(db_path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resumes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              display_name TEXT NOT NULL UNIQUE,
              artifact_path TEXT NOT NULL DEFAULT '',
              source_kind TEXT NOT NULL DEFAULT 'local_metadata',
              content_digest TEXT NOT NULL DEFAULT '',
              tags_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'available',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              platform TEXT NOT NULL,
              platform_job_id TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              company TEXT NOT NULL DEFAULT '',
              location TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '',
              requirements_json TEXT NOT NULL DEFAULT '[]',
              fit_tags_json TEXT NOT NULL DEFAULT '[]',
              risk_flags_json TEXT NOT NULL DEFAULT '[]',
              salary_hint TEXT NOT NULL DEFAULT '',
              published_hint TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'observed',
              score INTEGER NOT NULL DEFAULT 0,
              raw_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outreach (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER NOT NULL,
              resume_id INTEGER,
              channel TEXT NOT NULL DEFAULT 'application_form',
              message_language TEXT NOT NULL DEFAULT 'en',
              draft_text TEXT NOT NULL DEFAULT '',
              approval_status TEXT NOT NULL DEFAULT 'needs_owner_review',
              approved_to_prepare INTEGER NOT NULL DEFAULT 0,
              approved_to_submit INTEGER NOT NULL DEFAULT 0,
              final_submit_allowed INTEGER NOT NULL DEFAULT 0,
              submission_allowed INTEGER NOT NULL DEFAULT 0,
              upload_allowed INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
              FOREIGN KEY(resume_id) REFERENCES resumes(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS statuses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entity_type TEXT NOT NULL,
              entity_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              platform TEXT NOT NULL DEFAULT '',
              job_id INTEGER,
              outreach_id INTEGER,
              status TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL,
              FOREIGN KEY(outreach_id) REFERENCES outreach(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_platform_status ON jobs(platform, status);
            CREATE INDEX IF NOT EXISTS idx_outreach_job ON outreach(job_id);
            CREATE INDEX IF NOT EXISTS idx_statuses_entity ON statuses(entity_type, entity_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_iso()),
        )


def upsert_resume(resume: ResumeArtifact, db_path: Path = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    ts = now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO resumes(display_name, artifact_path, source_kind, content_digest, tags_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(display_name) DO UPDATE SET
              artifact_path=excluded.artifact_path,
              source_kind=excluded.source_kind,
              content_digest=excluded.content_digest,
              tags_json=excluded.tags_json,
              status=excluded.status,
              updated_at=excluded.updated_at
            """,
            (
                resume.display_name,
                resume.artifact_path,
                resume.source_kind,
                resume.content_digest,
                dumps(list(resume.tags)),
                resume.status,
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT id FROM resumes WHERE display_name = ?", (resume.display_name,)).fetchone()
        return int(row["id"])


def upsert_job(observation: VacancyObservation, score: int = 0, db_path: Path = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    ts = now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs(
              platform, source_url, title, company, location, summary, requirements_json,
              fit_tags_json, risk_flags_json, salary_hint, published_hint, status, score,
              raw_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
              platform=excluded.platform,
              title=excluded.title,
              company=excluded.company,
              location=excluded.location,
              summary=excluded.summary,
              requirements_json=excluded.requirements_json,
              fit_tags_json=excluded.fit_tags_json,
              risk_flags_json=excluded.risk_flags_json,
              salary_hint=excluded.salary_hint,
              published_hint=excluded.published_hint,
              status=excluded.status,
              score=excluded.score,
              raw_json=excluded.raw_json,
              updated_at=excluded.updated_at
            """,
            (
                observation.source_site,
                observation.source_url,
                observation.title,
                observation.company,
                observation.location,
                observation.summary,
                dumps(list(observation.requirements)),
                dumps(list(observation.fit_tags)),
                dumps(list(observation.risk_flags)),
                observation.salary_hint,
                observation.published_hint,
                observation.status,
                score,
                dumps(observation.raw),
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT id FROM jobs WHERE source_url = ?", (observation.source_url,)).fetchone()
        job_id = int(row["id"])
    append_event("job_observed", observation.source_site, job_id, None, observation.status, observation.to_artifact(), db_path)
    return job_id


def add_outreach_draft(
    job_id: int,
    draft: OutreachDraft,
    resume_id: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    if draft.submission_allowed or draft.upload_allowed:
        raise PermissionError("outreach drafts must keep submission_allowed=false and upload_allowed=false")
    ts = now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO outreach(
              job_id, resume_id, channel, message_language, draft_text, approval_status,
              approved_to_prepare, approved_to_submit, final_submit_allowed,
              submission_allowed, upload_allowed, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, ?)
            """,
            (
                job_id,
                resume_id,
                draft.channel,
                draft.message_language,
                draft.cover_letter,
                draft.approval_status,
                ts,
                ts,
            ),
        )
        outreach_id = int(cur.lastrowid)
    append_event("outreach_drafted", draft.source_site, job_id, outreach_id, draft.approval_status, draft.to_artifact(), db_path)
    return outreach_id


def set_status(
    entity_type: str,
    entity_id: int,
    status: str,
    reason: str = "",
    payload: dict[str, Any] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    ts = now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO statuses(entity_type, entity_id, status, reason, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, status, reason, dumps(payload or {}), ts),
        )
        return int(cur.lastrowid)


def update_job_status(
    job_id: int,
    status: str,
    reason: str = "",
    payload: dict[str, Any] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    ts = now_iso()
    with connect(db_path) as conn:
        conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, ts, job_id))
    set_status("job", job_id, status, reason, payload, db_path)
    append_event("job_status_changed", "", job_id, None, status, payload or {"reason": reason}, db_path)


def update_outreach_approval(
    outreach_id: int,
    approval_status: str,
    approved_to_prepare: bool = False,
    approved_to_submit: bool = False,
    final_submit_allowed: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    if final_submit_allowed and not approved_to_submit:
        raise PermissionError("final_submit_allowed requires approved_to_submit=true")
    ts = now_iso()
    with connect(db_path) as conn:
        row = conn.execute("SELECT job_id FROM outreach WHERE id = ?", (outreach_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown outreach id: {outreach_id}")
        job_id = int(row["job_id"])
        conn.execute(
            """
            UPDATE outreach SET
              approval_status = ?,
              approved_to_prepare = ?,
              approved_to_submit = ?,
              final_submit_allowed = ?,
              submission_allowed = ?,
              updated_at = ?
            WHERE id = ?
            """,
            (
                approval_status,
                1 if approved_to_prepare else 0,
                1 if approved_to_submit else 0,
                1 if final_submit_allowed else 0,
                1 if approved_to_submit and final_submit_allowed else 0,
                ts,
                outreach_id,
            ),
        )
    payload = {
        "approved_to_prepare": approved_to_prepare,
        "approved_to_submit": approved_to_submit,
        "final_submit_allowed": final_submit_allowed,
    }
    set_status("outreach", outreach_id, approval_status, payload=payload, db_path=db_path)
    append_event("outreach_approval_changed", "", job_id, outreach_id, approval_status, payload, db_path)


def append_event(
    event_type: str,
    platform: str = "",
    job_id: int | None = None,
    outreach_id: int | None = None,
    status: str = "",
    payload: dict[str, Any] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    ts = now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO events(event_type, platform, job_id, outreach_id, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, platform, job_id, outreach_id, status, dumps(payload or {}), ts),
        )
        return int(cur.lastrowid)


def fetch_progress_rows(db_path: Path = DEFAULT_DB_PATH, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
    init_db(db_path)
    with connect(db_path) as conn:
        jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
        outreach = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM outreach ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (min(limit, 50),),
            ).fetchall()
        ]
    for row in jobs:
        row["requirements"] = loads(row.pop("requirements_json", None), [])
        row["fit_tags"] = loads(row.pop("fit_tags_json", None), [])
        row["risk_flags"] = loads(row.pop("risk_flags_json", None), [])
        row["raw"] = loads(row.pop("raw_json", None), {})
    for row in events:
        row["payload"] = loads(row.pop("payload_json", None), {})
    return {"jobs": jobs, "outreach": outreach, "events": events}
