from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .base import JobPlatformAdapter
from .models import (
    DiscoveryQuery,
    PlatformCapabilities,
    ProgressEdge,
    ProgressNode,
    ProgressSnapshot,
    ResumeArtifact,
    VacancyObservation,
    utcish_now,
)
from shared_job_db import DEFAULT_DB_PATH, add_outreach_draft, append_event, fetch_progress_rows, upsert_job


DEFAULT_USER_AGENT = "job-apply-workua-public-discovery/1.0 (+read-only; no cookies)"
DEFAULT_FETCH_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RESPONSE_BYTES = 1_500_000


FIT_KEYWORDS = {
    "python": "python",
    "fastapi": "fastapi",
    "django": "django",
    "flask": "flask",
    "ai": "ai",
    "llm": "llm",
    "rag": "rag",
    "agent": "agentic",
    "automation": "automation",
    "backend": "backend",
    "api": "api",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "docker": "docker",
    "aws": "cloud",
    "gcp": "cloud",
    "azure": "cloud",
}


PIPELINE_GATES = [
    ("discover", "Discover", "Public Work.ua search/listing URLs only"),
    ("normalize", "Normalize", "job.vacancy_observation.v0"),
    ("score", "Score", "Local fit scoring"),
    ("draft", "Draft", "Local message draft"),
    ("review_approval", "Review Approval", "Owner review required"),
    ("final_gated_apply_manual_handoff", "Final Gated Apply / Browser Adapter", "Separate approved CSV adapter required"),
    ("status_tracking", "Status Tracking", "Shared DB append-only events"),
]


TERMINAL_STATUSES = {"submitted", "submitted_success", "already_applied", "manual_handoff_complete"}


def _clean_text(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text[:limit]


def _dedupe_keep_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return tuple(result)


def _infer_fit_tags(*parts: str) -> tuple[str, ...]:
    text = " ".join(parts).lower()
    tags = [tag for token, tag in FIT_KEYWORDS.items() if token in text]
    return _dedupe_keep_order(tags)


def _risk_flags_for_text(text: str) -> tuple[str, ...]:
    low = text.lower()
    flags = ["manual_handoff_only", "public_page_only"]
    for token in ["junior", "trainee", "intern"]:
        if token in low:
            flags.append(f"seniority_risk:{token}")
    for token in ["wordpress", "php only", "sales manager", "recruiter"]:
        if token in low:
            flags.append(f"fit_risk:{token}")
    return _dedupe_keep_order(flags)


def _absolute_url(base_url: str, href: str) -> str:
    return urllib.parse.urljoin(base_url or "https://www.work.ua/", href)


def _workua_vacancy_id(url: str) -> str:
    match = re.search(r"/jobs/(\d+)/?", urllib.parse.urlparse(url).path)
    return match.group(1) if match else ""


def _is_workua_vacancy_href(href: str) -> bool:
    return bool(re.search(r"/jobs/\d+/?", href))


def _safe_artifact_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())[:80].strip("._")
    return text or "workua"


class _VacancyAnchorCollector(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self._current_href = ""
        self._text_parts: list[str] = []
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        href = attrs_dict.get("href", "")
        if not _is_workua_vacancy_href(href):
            return
        self._current_href = _absolute_url(self.base_url, href)
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        text = _clean_text(" ".join(self._text_parts), limit=500)
        if text:
            self.links.append({"source_url": self._current_href, "title": text})
        self._current_href = ""
        self._text_parts = []


def _iter_json_ld_objects(markup: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    pattern = re.compile(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(markup):
        raw = html.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = loaded if isinstance(loaded, list) else [loaded]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
            objects.append(item)
    return objects


def _json_value(value: Any) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ("name", "text", "value"):
            if key in value:
                return _json_value(value[key])
    if isinstance(value, list):
        return ", ".join(_json_value(item) for item in value if _json_value(item))
    return ""


def _json_location(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(filter(None, (_json_location(item) for item in value)))
    if isinstance(value, dict):
        address = value.get("address")
        if isinstance(address, dict):
            parts = [
                _json_value(address.get("addressLocality")),
                _json_value(address.get("addressRegion")),
                _json_value(address.get("addressCountry")),
            ]
            return ", ".join(part for part in parts if part)
        return _json_value(value.get("name"))
    return _json_value(value)


class WorkUaAdapter(JobPlatformAdapter):
    capabilities = PlatformCapabilities(
        platform_id="workua",
        display_name="Work.ua",
        allowed_hosts=("work.ua", "www.work.ua"),
        supports_prepare_only=False,
        notes="Public discovery/draft/status adapter; live browser apply is handled by the gated Work.ua CSV CLI.",
    )

    def discovery_urls(self, query: DiscoveryQuery) -> list[str]:
        text = urllib.parse.quote_plus(query.text.strip())
        location = urllib.parse.quote_plus(query.location.strip().lower()) if query.location else ""
        if not text:
            raise ValueError("Work.ua discovery query text is required")
        path = f"/jobs-{location}-{text}/" if location else f"/jobs-{text}/"
        return [urllib.parse.urljoin("https://www.work.ua", path)]

    def normalize_vacancy(self, raw: dict) -> VacancyObservation:
        source_url = self.normalize_url(str(raw.get("source_url", "")))
        title = _clean_text(raw.get("title"), limit=180)
        if not title:
            raise ValueError("Work.ua vacancy requires a public title")
        company = _clean_text(raw.get("company"), limit=180)
        location = _clean_text(raw.get("location"), limit=180)
        summary = _clean_text(raw.get("summary"), limit=1000)
        requirements = tuple(_clean_text(item, limit=300) for item in raw.get("requirements") or () if _clean_text(item))
        text = " ".join([title, company, location, summary, " ".join(requirements)])
        provided_tags = [str(tag).strip().lower() for tag in raw.get("fit_tags") or [] if str(tag).strip()]
        fit_tags = _dedupe_keep_order(provided_tags + list(_infer_fit_tags(text)))
        provided_risks = [str(flag).strip() for flag in raw.get("risk_flags") or [] if str(flag).strip()]
        risk_flags = _dedupe_keep_order(provided_risks + list(_risk_flags_for_text(text)))
        raw_payload = dict(raw.get("raw") or {})
        vacancy_id = _workua_vacancy_id(source_url)
        if vacancy_id:
            raw_payload.setdefault("workua_vacancy_id", vacancy_id)
        return VacancyObservation(
            source_site=self.platform_id,
            source_url=source_url,
            title=title,
            company=company,
            location=location,
            summary=summary,
            requirements=requirements,
            fit_tags=fit_tags,
            risk_flags=risk_flags,
            status=str(raw.get("status", "observed")).strip() or "observed",
            published_hint=_clean_text(raw.get("published_hint"), limit=80),
            salary_hint=_clean_text(raw.get("salary_hint"), limit=120),
            employment_type=_clean_text(raw.get("employment_type"), limit=80),
            language_hint=_clean_text(raw.get("language_hint"), limit=60),
            source_query=_clean_text(raw.get("source_query"), limit=120),
            raw=raw_payload,
        )

    def extract_public_vacancies(
        self,
        markup: str,
        source_url: str,
        query: DiscoveryQuery | None = None,
    ) -> list[VacancyObservation]:
        """Extract Work.ua observations from saved public listing/detail HTML.

        This function only consumes caller-provided public markup. It does not
        inspect browser state, cookies, saved sessions, profile data, or forms.
        """

        raw_rows: list[dict[str, Any]] = []
        for obj in _iter_json_ld_objects(markup):
            types = obj.get("@type")
            type_values = types if isinstance(types, list) else [types]
            if "JobPosting" not in [str(value) for value in type_values]:
                continue
            url = _json_value(obj.get("url")) or source_url
            org = obj.get("hiringOrganization") or {}
            raw_rows.append(
                {
                    "source_url": _absolute_url(source_url, url),
                    "title": _json_value(obj.get("title")),
                    "company": _json_value(org.get("name") if isinstance(org, dict) else org),
                    "location": _json_location(obj.get("jobLocation")),
                    "summary": _json_value(obj.get("description")),
                    "published_hint": _json_value(obj.get("datePosted")),
                    "salary_hint": _json_value(obj.get("baseSalary")),
                    "employment_type": _json_value(obj.get("employmentType")),
                    "source_query": query.text if query else "",
                    "raw": {"extractor": "json_ld"},
                }
            )

        anchors = _VacancyAnchorCollector(source_url)
        anchors.feed(markup)
        for link in anchors.links:
            raw_rows.append(
                {
                    "source_url": link["source_url"],
                    "title": link["title"],
                    "source_query": query.text if query else "",
                    "raw": {"extractor": "anchor"},
                }
            )

        seen: set[str] = set()
        observations: list[VacancyObservation] = []
        for raw in raw_rows:
            try:
                observation = self.normalize_vacancy(raw)
            except ValueError:
                continue
            key = observation.source_url.lower()
            if key in seen:
                continue
            seen.add(key)
            observations.append(observation)
        return observations


def workua_observation_score(observation: VacancyObservation | dict[str, Any]) -> int:
    row = observation.to_artifact() if isinstance(observation, VacancyObservation) else observation
    text = " ".join(
        [
            str(row.get("title", "")),
            str(row.get("company", "")),
            str(row.get("summary", "")),
            str(row.get("location", "")),
            " ".join(str(tag) for tag in row.get("fit_tags") or []),
            " ".join(str(req) for req in row.get("requirements") or []),
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
        "api": 3,
        "docker": 2,
    }.items():
        if token in text:
            score += points
    if "remote" in text or "\u0432\u0456\u0434\u0434\u0430\u043b" in text:
        score += 5
    if "lviv" in text or "\u043b\u044c\u0432\u0456\u0432" in text:
        score += 4
    if "kyiv" in text or "\u043a\u0438\u0457\u0432" in text:
        score += 2
    if any(token in text for token in ["junior", "trainee", "intern"]):
        score -= 10
    if "manual_handoff_only" in [str(x) for x in row.get("risk_flags") or []]:
        score -= 1
    return score


def default_resume_artifact() -> ResumeArtifact:
    return ResumeArtifact(display_name="owner_selected_resume_metadata_pending", status="needs_owner_review")


def persist_workua_observation(
    observation: VacancyObservation,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    if observation.source_site != "workua":
        raise ValueError(f"expected workua observation, got {observation.source_site!r}")
    return upsert_job(observation, score=workua_observation_score(observation), db_path=db_path)


def fetch_public_workua_url(
    url: str,
    timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> str:
    """Fetch one public Work.ua page without cookies or browser/session state."""

    adapter = WorkUaAdapter()
    normalized_url = adapter.normalize_url(url)
    request = urllib.request.Request(
        normalized_url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"refusing Work.ua response larger than {max_bytes} bytes")
    return payload.decode(content_type, errors="replace")


def create_workua_review_draft(
    observation: VacancyObservation,
    job_id: int,
    resume: ResumeArtifact | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    adapter = WorkUaAdapter()
    draft = adapter.draft_outreach(observation, resume or default_resume_artifact())
    if draft.submission_allowed or draft.upload_allowed:
        raise PermissionError("Work.ua review drafts must keep submit/upload disabled")
    outreach_id = add_outreach_draft(job_id, draft, resume_id=None, db_path=db_path)
    append_event(
        "workua_manual_handoff_required",
        "workua",
        job_id,
        outreach_id,
        "needs_owner_review",
        {
            "gate": "final_gated_apply_manual_handoff",
            "reason": "Work.ua review artifacts do not permit submit; use an approved Work.ua CSV artifact for live browser apply.",
        },
        db_path,
    )
    return outreach_id


def _write_json_artifact(artifact_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _approval_artifact_payload(
    observation: VacancyObservation,
    job_id: int,
    outreach_id: int,
    score: int,
    draft_text: str,
) -> dict[str, Any]:
    return {
        "schema": "job.workua_approval_artifact.v0",
        "platform": "workua",
        "job_id": job_id,
        "outreach_id": outreach_id,
        "source_url": observation.source_url,
        "title": observation.title,
        "company": observation.company,
        "location": observation.location,
        "score": score,
        "fit_tags": list(observation.fit_tags),
        "risk_flags": list(observation.risk_flags),
        "draft_text": draft_text,
        "approval_required": True,
        "submission_allowed": False,
        "upload_allowed": False,
        "required_owner_approval": (
            "Exact Work.ua vacancy, exact message, exact resume/profile choice, and final action policy. "
            "This artifact is review-only and does not permit final submission."
        ),
    }


def _blocker_artifact_payload(
    query: DiscoveryQuery,
    source_url: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "job.workua_blocker_artifact.v0",
        "platform": "workua",
        "source_url": source_url,
        "query": {
            "text": query.text,
            "location": query.location,
            "limit": query.limit,
        },
        "status": "blocked",
        "reason": reason,
        "details": details or {},
        "submission_allowed": False,
        "upload_allowed": False,
    }


def run_workua_public_search_once(
    query: DiscoveryQuery,
    db_path: Path = DEFAULT_DB_PATH,
    artifact_dir: Path | None = None,
    fetcher: Any | None = None,
    create_drafts: bool = True,
) -> dict[str, Any]:
    """Run one bounded public Work.ua discovery pass and stop at review artifacts."""

    adapter = WorkUaAdapter()
    urls = [query.source_url] if query.source_url else adapter.discovery_urls(query)
    fetch = fetcher or fetch_public_workua_url
    output_dir = artifact_dir or (db_path.parent / "workua_artifacts")
    approvals: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    persisted: list[dict[str, Any]] = []

    for source_url in urls:
        try:
            markup = fetch(source_url)
            append_event(
                "workua_public_search_fetched",
                "workua",
                None,
                None,
                "fetched",
                {"source_url": source_url, "query": query.text, "limit": query.limit},
                db_path,
            )
            observations = adapter.extract_public_vacancies(markup, source_url, query)[: max(0, query.limit)]
        except Exception as exc:
            blocker = _blocker_artifact_payload(query, source_url, "public_fetch_or_parse_failed", {"error": str(exc)})
            path = _write_json_artifact(output_dir, f"blocker_{_safe_artifact_name(query.text)}.json", blocker)
            blocker["artifact_path"] = str(path)
            blockers.append(blocker)
            append_event("workua_run_once_blocker", "workua", None, None, "blocked", blocker, db_path)
            continue

        if not observations:
            blocker = _blocker_artifact_payload(query, source_url, "no_public_vacancies_extracted")
            path = _write_json_artifact(output_dir, f"blocker_{_safe_artifact_name(query.text)}.json", blocker)
            blocker["artifact_path"] = str(path)
            blockers.append(blocker)
            append_event("workua_run_once_blocker", "workua", None, None, "blocked", blocker, db_path)
            continue

        for observation in observations:
            score = workua_observation_score(observation)
            job_id = persist_workua_observation(observation, db_path=db_path)
            row = {
                "job_id": job_id,
                "source_url": observation.source_url,
                "title": observation.title,
                "company": observation.company,
                "score": score,
            }
            if create_drafts:
                outreach_id = create_workua_review_draft(observation, job_id, db_path=db_path)
                draft = adapter.draft_outreach(observation, default_resume_artifact())
                approval = _approval_artifact_payload(observation, job_id, outreach_id, score, draft.cover_letter)
                path = _write_json_artifact(output_dir, f"approval_job_{job_id}.json", approval)
                approval["artifact_path"] = str(path)
                approvals.append(approval)
                row["outreach_id"] = outreach_id
                append_event("workua_owner_approval_required", "workua", job_id, outreach_id, "needs_owner_review", approval, db_path)
            persisted.append(row)

    snapshot = build_workua_progress_snapshot(db_path=db_path)
    summary = {
        "schema": "job.workua_run_once_summary.v0",
        "platform": "workua",
        "query": {"text": query.text, "location": query.location, "limit": query.limit},
        "source_urls": urls,
        "jobs_persisted": persisted,
        "approval_artifacts": approvals,
        "blocker_artifacts": blockers,
        "progress_snapshot": snapshot_to_dict(snapshot),
        "submission_allowed": False,
        "upload_allowed": False,
    }
    summary_path = _write_json_artifact(output_dir, f"summary_{_safe_artifact_name(query.text)}.json", summary)
    summary["artifact_path"] = str(summary_path)
    append_event(
        "workua_run_once_complete",
        "workua",
        None,
        None,
        "blocked_waiting_owner" if approvals else ("blocked" if blockers else "complete"),
        {
            "jobs": len(persisted),
            "approvals": len(approvals),
            "blockers": len(blockers),
            "artifact_path": str(summary_path),
        },
        db_path,
    )
    return summary


def _workua_rows(db_path: Path, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = fetch_progress_rows(db_path, limit=limit)
    jobs = [row for row in rows["jobs"] if row.get("platform") == "workua"]
    job_ids = {int(row["id"]) for row in jobs}
    outreach = [row for row in rows["outreach"] if int(row.get("job_id") or 0) in job_ids]
    outreach_ids = {int(row["id"]) for row in outreach}
    events = [
        row
        for row in rows["events"]
        if row.get("platform") == "workua"
        or int(row.get("job_id") or 0) in job_ids
        or int(row.get("outreach_id") or 0) in outreach_ids
    ]
    return jobs, outreach, events


def _gate_status(gate: str, jobs: list[dict[str, Any]], outreach: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
    if gate == "discover":
        return "ready"
    if gate == "normalize":
        return "complete" if jobs else "pending"
    if gate == "score":
        return "complete" if any(int(row.get("score") or 0) for row in jobs) else ("seen" if jobs else "pending")
    if gate == "draft":
        return "complete" if outreach else "pending"
    if gate == "review_approval":
        if not outreach:
            return "pending"
        if any(row.get("approved_to_submit") and row.get("final_submit_allowed") for row in outreach):
            return "ready"
        if any(row.get("approved_to_prepare") or row.get("approved_to_submit") for row in outreach):
            return "active"
        return "blocked_waiting_owner"
    if gate == "final_gated_apply_manual_handoff":
        if any(str(row.get("status") or "") in TERMINAL_STATUSES for row in jobs):
            return "complete"
        if any(row.get("approved_to_submit") and row.get("final_submit_allowed") for row in outreach):
            return "manual_handoff_ready"
        return "blocked_waiting_owner" if outreach else "pending"
    if gate == "status_tracking":
        return "active" if events else "pending"
    return "pending"


def build_workua_progress_snapshot(db_path: Path = DEFAULT_DB_PATH, limit: int = 100) -> ProgressSnapshot:
    jobs, outreach, events = _workua_rows(db_path, limit)
    nodes: list[ProgressNode] = []
    edges: list[ProgressEdge] = []
    gate_statuses = {
        gate_id: _gate_status(gate_id, jobs, outreach, events)
        for gate_id, _label, _description in PIPELINE_GATES
    }
    for index, (gate_id, label, description) in enumerate(PIPELINE_GATES):
        nodes.append(
            ProgressNode(
                id=f"workua:{gate_id}",
                label=label,
                kind="gate" if gate_id in {"review_approval", "final_gated_apply_manual_handoff"} else "stage",
                status=gate_statuses[gate_id],
                data={
                    "platform": "workua",
                    "order": index + 1,
                    "description": description,
                    "owner_approval_required": gate_id in {"review_approval", "final_gated_apply_manual_handoff"},
                },
            )
        )
        if index:
            previous = PIPELINE_GATES[index - 1][0]
            edges.append(
                ProgressEdge(
                    source=f"workua:{previous}",
                    target=f"workua:{gate_id}",
                    label="next",
                    status="available" if gate_statuses[previous] in {"complete", "ready", "active"} else "pending",
                )
            )

    for job in jobs:
        job_id = int(job["id"])
        title = str(job.get("title") or "Untitled")
        company = str(job.get("company") or "")
        nodes.append(
            ProgressNode(
                id=f"workua:job:{job_id}",
                label=(f"{company} - {title}" if company else title)[:160],
                kind="job",
                status=str(job.get("status") or "observed"),
                data={
                    "platform": "workua",
                    "source_url": job.get("source_url", ""),
                    "score": int(job.get("score") or 0),
                    "fit_tags": job.get("fit_tags", []),
                    "risk_flags": job.get("risk_flags", []),
                },
            )
        )
        edges.append(ProgressEdge(source="workua:normalize", target=f"workua:job:{job_id}", label="observed", status="seen"))
        if int(job.get("score") or 0):
            edges.append(ProgressEdge(source=f"workua:job:{job_id}", target="workua:score", label="scored", status="complete"))

    for draft in outreach:
        outreach_id = int(draft["id"])
        job_id = int(draft["job_id"])
        approved_to_submit = bool(draft.get("approved_to_submit"))
        final_submit_allowed = bool(draft.get("final_submit_allowed"))
        status = str(draft.get("approval_status") or "needs_owner_review")
        if approved_to_submit and final_submit_allowed:
            status = "manual_handoff_ready"
        nodes.append(
            ProgressNode(
                id=f"workua:outreach:{outreach_id}",
                label=f"Work.ua Draft #{outreach_id}",
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
        edges.append(ProgressEdge(source=f"workua:job:{job_id}", target=f"workua:outreach:{outreach_id}", label="draft", status="active"))
        edges.append(
            ProgressEdge(
                source=f"workua:outreach:{outreach_id}",
                target="workua:review_approval",
                label="review gate",
                status=status,
            )
        )
        edges.append(
            ProgressEdge(
                source=f"workua:outreach:{outreach_id}",
                target="workua:final_gated_apply_manual_handoff",
                label="manual handoff gate",
                status="ready" if approved_to_submit and final_submit_allowed else "blocked_waiting_owner",
            )
        )

    return ProgressSnapshot(
        schema="job.workua_progress_snapshot.langgraph.v0",
        generated_at=utcish_now(),
        nodes=nodes,
        edges=edges,
        event_tail=events[-20:],
    )


def pipeline_gate_data() -> list[dict[str, Any]]:
    return [
        {
            "id": gate_id,
            "label": label,
            "description": description,
            "platform": "workua",
            "requires_owner_approval": gate_id in {"review_approval", "final_gated_apply_manual_handoff"},
        }
        for gate_id, label, description in PIPELINE_GATES
    ]


def snapshot_to_dict(snapshot: ProgressSnapshot) -> dict[str, Any]:
    return {
        "schema": snapshot.schema,
        "generated_at": snapshot.generated_at,
        "nodes": [asdict(node) for node in snapshot.nodes],
        "edges": [asdict(edge) for edge in snapshot.edges],
        "event_tail": snapshot.event_tail,
    }
