from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .base import JobPlatformAdapter
from .models import DiscoveryQuery, PlatformCapabilities, ProgressEdge, ProgressNode, ProgressSnapshot, ResumeArtifact, VacancyObservation, utcish_now


FIT_KEYWORDS = {
    "python": "python",
    "django": "django",
    "fastapi": "fastapi",
    "flask": "flask",
    "ai": "ai",
    "ml": "ml",
    "machine learning": "ml",
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
    ("discover", "Discover", "Public DOU/jobs and relocate.dou.ua URLs only"),
    ("normalize", "Normalize", "job.vacancy_observation.v0"),
    ("score", "Score", "Local public-text fit score"),
    ("draft", "Draft", "Local outreach draft"),
    ("review_approval", "Review Approval", "Owner must approve exact vacancy and message"),
    ("final_gated_apply", "Final Gated Apply / Manual Handoff", "No DOU submit automation in this slice"),
    ("status_tracking", "Status Tracking", "Shared DB jobs/outreach/events"),
]

TERMINAL_STATUSES = {"submitted", "submitted_success", "already_applied"}
BLOCKED_STATUSES = {"blocked", "blocked_validation", "error"}


def _clean_text(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text[:limit]


def _dedupe_keep_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _clean_text(value, limit=160)
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return tuple(result)


def _infer_fit_tags(*parts: str) -> tuple[str, ...]:
    text = " ".join(parts).lower()
    return _dedupe_keep_order([tag for token, tag in FIT_KEYWORDS.items() if token in text])


def _risk_flags_for_text(text: str) -> tuple[str, ...]:
    low = text.lower()
    flags = ["manual_handoff_only", "public_page_only"]
    for token in ["junior", "trainee", "intern", "стажер", "початків"]:
        if token in low:
            flags.append(f"seniority_risk:{token}")
    for token in ["wordpress", "php only", "affiliate", "sales manager", "recruiter"]:
        if token in low:
            flags.append(f"fit_risk:{token}")
    if "miltech" in low or "deftech" in low or "оборон" in low:
        flags.append("domain_review:defense")
    return _dedupe_keep_order(flags)


def _absolute_url(base_url: str, href: str) -> str:
    return urllib.parse.urljoin(base_url or "https://jobs.dou.ua/", href)


def _json_value(value: Any) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ("name", "text", "value"):
            if key in value:
                return _json_value(value[key])
    if isinstance(value, list):
        return ", ".join(filter(None, (_json_value(item) for item in value)))
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


class _DouVacancyParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.rows: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._field = ""
        self._li_depth = 0
        self._fallback_href = ""
        self._fallback_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        href = attrs_dict.get("href", "")
        low_tag = tag.lower()

        if low_tag == "li" and "l-vacancy" in classes:
            self._current = {"raw_extractor": "dou_l_vacancy"}
            self._li_depth = 1
            return

        if self._current is not None and low_tag == "li":
            self._li_depth += 1

        if self._current is not None:
            if low_tag == "a" and href:
                if "vt" in classes or "/vacancies/" in href:
                    self._current["source_url"] = _absolute_url(self.base_url, href)
                    self._field = "title"
                    return
                if "company" in classes:
                    self._field = "company"
                    return
            if low_tag in {"span", "div"} and classes & {"cities", "place"}:
                self._field = "location"
                return
            if low_tag in {"div", "p"} and classes & {"sh-info", "b-typo", "text"}:
                self._field = "summary"
                return
            if low_tag in {"strong", "span"} and "salary" in classes:
                self._field = "salary_hint"
                return
            if low_tag in {"div", "span"} and classes & {"date", "published"}:
                self._field = "published_hint"
                return

        if self._current is None and low_tag == "a" and href and "/vacancies/" in href:
            self._fallback_href = _absolute_url(self.base_url, href)
            self._fallback_text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field:
            existing = self._current.get(self._field, "")
            self._current[self._field] = f"{existing} {data}".strip()
        elif self._fallback_href:
            self._fallback_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        low_tag = tag.lower()
        if self._current is not None:
            if low_tag in {"a", "span", "div", "p", "strong"}:
                self._field = ""
            if low_tag == "li":
                self._li_depth -= 1
                if self._li_depth <= 0:
                    if self._current.get("source_url") and self._current.get("title"):
                        self.rows.append(dict(self._current))
                    self._current = None
                    self._field = ""
            return

        if low_tag == "a" and self._fallback_href:
            title = _clean_text(" ".join(self._fallback_text), limit=180)
            if title:
                self.rows.append(
                    {
                        "source_url": self._fallback_href,
                        "title": title,
                        "raw_extractor": "dou_anchor",
                    }
                )
            self._fallback_href = ""
            self._fallback_text = []


class DouAdapter(JobPlatformAdapter):
    capabilities = PlatformCapabilities(
        platform_id="dou",
        display_name="DOU / Relocate DOU",
        allowed_hosts=("dou.ua", "jobs.dou.ua", "relocate.dou.ua"),
        supports_prepare_only=False,
        supports_final_submit=False,
        notes="Public discovery/draft/status only; final apply is manual handoff until separately approved.",
    )

    def discovery_urls(self, query: DiscoveryQuery) -> list[str]:
        params: dict[str, str] = {}
        if query.text.strip():
            params["search"] = query.text.strip()
        jobs_url = "https://jobs.dou.ua/vacancies/"
        relocate_url = "https://relocate.dou.ua/jobs/"
        if params:
            query_string = urllib.parse.urlencode(params)
            jobs_url = f"{jobs_url}?{query_string}"
            relocate_url = f"{relocate_url}?{query_string}"
        return [jobs_url, relocate_url]

    def normalize_vacancy(self, raw: dict) -> VacancyObservation:
        source_url = self.normalize_url(str(raw.get("source_url", "")))
        title = _clean_text(raw.get("title"), limit=180)
        if not title:
            raise ValueError("DOU vacancy requires a public title")
        company = _clean_text(raw.get("company"), limit=180)
        location = _clean_text(raw.get("location"), limit=220)
        summary = _clean_text(raw.get("summary"), limit=1200)
        requirements = tuple(
            _clean_text(item, limit=300)
            for item in raw.get("requirements") or ()
            if _clean_text(item, limit=300)
        )
        text = " ".join([title, company, location, summary, " ".join(requirements)])
        provided_tags = [str(tag).strip().lower() for tag in raw.get("fit_tags") or [] if str(tag).strip()]
        fit_tags = _dedupe_keep_order(provided_tags + list(_infer_fit_tags(text)))
        provided_risks = [str(flag).strip() for flag in raw.get("risk_flags") or [] if str(flag).strip()]
        risk_flags = _dedupe_keep_order(provided_risks + list(_risk_flags_for_text(text)))
        raw_payload = dict(raw.get("raw") or {})
        if raw.get("raw_extractor"):
            raw_payload["extractor"] = str(raw["raw_extractor"])
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
        """Extract public DOU vacancy observations from saved/listing HTML."""

        raw_rows: list[dict[str, Any]] = []
        for obj in _iter_json_ld_objects(markup):
            type_values = obj.get("@type") if isinstance(obj.get("@type"), list) else [obj.get("@type")]
            if "JobPosting" not in [str(value) for value in type_values]:
                continue
            org = obj.get("hiringOrganization") or {}
            raw_rows.append(
                {
                    "source_url": _absolute_url(source_url, _json_value(obj.get("url")) or source_url),
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

        parser = _DouVacancyParser(source_url)
        parser.feed(markup)
        for row in parser.rows:
            row["source_query"] = query.text if query else ""
            raw_rows.append(row)

        seen: set[str] = set()
        observations: list[VacancyObservation] = []
        for raw in raw_rows:
            try:
                observation = self.normalize_vacancy(raw)
            except ValueError:
                continue
            key = observation.source_url.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            observations.append(observation)
        return observations


def score_dou_observation(observation: VacancyObservation | dict[str, Any]) -> int:
    data = observation.to_artifact() if isinstance(observation, VacancyObservation) else observation
    text = " ".join(
        [
            str(data.get("title", "")),
            str(data.get("company", "")),
            str(data.get("location", "")),
            str(data.get("summary", "")),
            " ".join(str(x) for x in data.get("fit_tags") or []),
            " ".join(str(x) for x in data.get("requirements") or []),
        ]
    ).lower()
    score = 0
    for token, points in {
        "python": 12,
        "ai": 10,
        "llm": 10,
        "rag": 8,
        "agent": 8,
        "automation": 8,
        "fastapi": 5,
        "django": 4,
        "backend": 4,
        "remote": 3,
        "віддалено": 3,
        "lviv": 4,
        "львів": 4,
    }.items():
        if token in text:
            score += points
    risk_flags = [str(flag).lower() for flag in data.get("risk_flags") or []]
    if any("seniority_risk" in flag for flag in risk_flags):
        score -= 10
    if any("fit_risk" in flag for flag in risk_flags):
        score -= 6
    if "manual_handoff_only" in risk_flags:
        score -= 1
    return score


def metadata_pending_resume() -> ResumeArtifact:
    return ResumeArtifact(display_name="owner_selected_resume_metadata_pending", status="needs_owner_review")


def upsert_dou_observations_to_db(
    observations: list[VacancyObservation],
    db_path: Path,
) -> list[int]:
    from shared_job_db import upsert_job

    job_ids: list[int] = []
    for observation in observations:
        job_ids.append(upsert_job(observation, score=score_dou_observation(observation), db_path=db_path))
    return job_ids


def build_dou_review_drafts(
    observations: list[VacancyObservation],
    db_path: Path,
    limit: int = 10,
) -> dict[str, Any]:
    from shared_job_db import add_outreach_draft, append_event, connect, upsert_job

    adapter = DouAdapter()
    resume = metadata_pending_resume()
    ranked = sorted(observations, key=score_dou_observation, reverse=True)[:limit]
    job_ids: list[int] = []
    outreach_ids: list[int] = []

    for observation in ranked:
        score = score_dou_observation(observation)
        job_id = upsert_job(observation, score=score, db_path=db_path)
        job_ids.append(job_id)
        with connect(db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM outreach WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if existing:
            outreach_id = int(existing["id"])
            append_event(
                "outreach_draft_reused",
                "dou",
                job_id,
                outreach_id,
                "needs_owner_review",
                {"score": score},
                db_path,
            )
        else:
            draft = adapter.draft_outreach(observation, resume)
            outreach_id = add_outreach_draft(job_id, draft, resume_id=None, db_path=db_path)
        outreach_ids.append(outreach_id)

    append_event(
        "dou_review_queue_built",
        "dou",
        None,
        None,
        "needs_owner_review",
        {"jobs": len(job_ids), "outreach": len(outreach_ids), "limit": limit},
        db_path,
    )
    return {"jobs": len(job_ids), "outreach": len(outreach_ids), "job_ids": job_ids, "outreach_ids": outreach_ids}


def build_dou_progress_snapshot(db_path: Path, limit: int = 100) -> ProgressSnapshot:
    from shared_job_db import fetch_progress_rows

    rows = fetch_progress_rows(db_path, limit=limit)
    jobs = [row for row in rows["jobs"] if row.get("platform") == "dou"]
    dou_job_ids = {int(row["id"]) for row in jobs}
    outreach = [row for row in rows["outreach"] if int(row.get("job_id") or 0) in dou_job_ids]
    events = [row for row in rows["events"] if row.get("platform") in {"", "dou"}]

    job_statuses = [str(row.get("status") or "") for row in jobs]
    outreach_statuses = [str(row.get("approval_status") or "") for row in outreach]
    has_scores = any(int(row.get("score") or 0) for row in jobs)
    has_approval = any(row.get("approved_to_prepare") or row.get("approved_to_submit") for row in outreach)
    has_final = any(row.get("approved_to_submit") and row.get("final_submit_allowed") for row in outreach)

    statuses = {
        "discover": "ready",
        "normalize": "complete" if jobs else "pending",
        "score": "complete" if has_scores else ("seen" if jobs else "pending"),
        "draft": "complete" if outreach else "pending",
        "review_approval": _review_status(outreach_statuses, has_approval),
        "final_gated_apply": _final_gate_status(job_statuses, has_final, bool(outreach)),
        "status_tracking": "active" if events else "pending",
    }

    nodes: list[ProgressNode] = []
    edges: list[ProgressEdge] = []
    for idx, (stage_id, label, description) in enumerate(PIPELINE_GATES):
        nodes.append(
            ProgressNode(
                id=stage_id,
                label=label,
                kind="gate" if stage_id in {"review_approval", "final_gated_apply"} else "stage",
                status=statuses[stage_id],
                data={
                    "platform": "dou",
                    "order": idx + 1,
                    "description": description,
                    "safe_actions_only": True,
                    "submission_automation_enabled": False,
                },
            )
        )
        if idx:
            previous = PIPELINE_GATES[idx - 1][0]
            edges.append(
                ProgressEdge(
                    source=previous,
                    target=stage_id,
                    label="next",
                    status="available" if statuses[previous] in {"ready", "seen", "complete", "active"} else "pending",
                )
            )

    for job in jobs:
        job_id = int(job["id"])
        title = str(job.get("title") or "Untitled")
        company = str(job.get("company") or "")
        nodes.append(
            ProgressNode(
                id=f"dou_job:{job_id}",
                label=(f"{company} - {title}" if company else title)[:160],
                kind="job",
                status=str(job.get("status") or "observed"),
                data={
                    "source_url": job.get("source_url", ""),
                    "score": int(job.get("score") or 0),
                    "fit_tags": job.get("fit_tags", []),
                    "risk_flags": job.get("risk_flags", []),
                },
            )
        )
        edges.append(ProgressEdge(source="normalize", target=f"dou_job:{job_id}", label="observed", status="seen"))
        edges.append(ProgressEdge(source=f"dou_job:{job_id}", target="score", label="scored", status="complete"))

    for draft in outreach:
        outreach_id = int(draft["id"])
        job_id = int(draft["job_id"])
        status = str(draft.get("approval_status") or "needs_owner_review")
        nodes.append(
            ProgressNode(
                id=f"dou_outreach:{outreach_id}",
                label=f"DOU Draft #{outreach_id}",
                kind="outreach_draft",
                status=status,
                data={
                    "job_id": job_id,
                    "approved_to_prepare": bool(draft.get("approved_to_prepare")),
                    "approved_to_submit": bool(draft.get("approved_to_submit")),
                    "final_submit_allowed": bool(draft.get("final_submit_allowed")),
                    "submission_allowed": bool(draft.get("submission_allowed")),
                    "upload_allowed": bool(draft.get("upload_allowed")),
                    "manual_handoff_required": True,
                },
            )
        )
        edges.append(ProgressEdge(source=f"dou_job:{job_id}", target=f"dou_outreach:{outreach_id}", label="draft", status="active"))
        edges.append(ProgressEdge(source=f"dou_outreach:{outreach_id}", target="review_approval", label="owner gate", status=status))
        edges.append(
            ProgressEdge(
                source="review_approval",
                target="final_gated_apply",
                label="manual handoff",
                status="blocked_waiting_owner" if not has_final else "ready",
            )
        )

    return ProgressSnapshot(
        schema="job.progress_snapshot.langgraph.v0",
        generated_at=utcish_now(),
        nodes=nodes,
        edges=edges,
        event_tail=[_event_tail_row(row) for row in events[-20:]],
    )


def _review_status(statuses: list[str], has_approval: bool) -> str:
    clean = {status for status in statuses if status}
    if clean & BLOCKED_STATUSES:
        return "blocked"
    if has_approval:
        return "ready"
    if clean:
        return "blocked_waiting_owner"
    return "pending"


def _final_gate_status(job_statuses: list[str], has_final: bool, has_outreach: bool) -> str:
    clean = {status for status in job_statuses if status}
    if clean & TERMINAL_STATUSES:
        return "complete"
    if clean & BLOCKED_STATUSES:
        return "blocked"
    if has_final:
        return "ready_manual_handoff"
    if has_outreach:
        return "blocked_waiting_owner"
    return "pending"


def _event_tail_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key in {"id", "event_type", "platform", "status", "payload", "created_at"}}


def progress_gate_artifact(snapshot: ProgressSnapshot) -> dict[str, Any]:
    data = snapshot.to_dict()
    data["pipeline_gates"] = [asdict(ProgressNode(id=stage_id, label=label, kind="gate_spec", data={"description": description})) for stage_id, label, description in PIPELINE_GATES]
    return data
