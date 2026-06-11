from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


def utcish_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass(frozen=True)
class PlatformCapabilities:
    platform_id: str
    display_name: str
    allowed_hosts: tuple[str, ...]
    supports_public_discovery: bool = True
    supports_draft_generation: bool = True
    supports_prepare_only: bool = False
    supports_final_submit: bool = False
    notes: str = ""


@dataclass(frozen=True)
class DiscoveryQuery:
    text: str
    location: str = ""
    remote: bool | None = None
    limit: int = 25
    source_url: str = ""


@dataclass(frozen=True)
class VacancyObservation:
    source_site: str
    source_url: str
    title: str
    company: str = ""
    location: str = ""
    summary: str = ""
    requirements: tuple[str, ...] = ()
    fit_tags: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    status: str = "observed"
    published_hint: str = ""
    salary_hint: str = ""
    employment_type: str = ""
    language_hint: str = ""
    source_query: str = ""
    observed_at: str = field(default_factory=utcish_now)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_artifact(self) -> dict[str, Any]:
        data = asdict(self)
        raw = data.pop("raw", {})
        data.update(
            {
                "schema": "job.vacancy_observation.v0",
                "requirements": list(self.requirements),
                "fit_tags": list(self.fit_tags),
                "risk_flags": list(self.risk_flags),
            }
        )
        if raw:
            data["raw"] = raw
        return data


@dataclass(frozen=True)
class ResumeArtifact:
    """Metadata only; do not store private resume/CV text in this model."""

    display_name: str
    artifact_path: str = ""
    source_kind: str = "local_metadata"
    content_digest: str = ""
    tags: tuple[str, ...] = ()
    status: str = "available"


@dataclass(frozen=True)
class ApprovalGate:
    approved_to_prepare: bool = False
    approved_to_submit: bool = False
    final_submit_allowed: bool = False
    approved_resume_name: str = ""
    approved_message: str = ""
    approved_by: str = ""
    approved_at: str = ""

    def allows_prepare(self) -> bool:
        return self.approved_to_prepare or self.approved_to_submit

    def allows_final_submit(self) -> bool:
        return self.approved_to_submit and self.final_submit_allowed


@dataclass(frozen=True)
class OutreachDraft:
    source_site: str
    source_url: str
    company: str = ""
    title: str = ""
    channel: str = "application_form"
    message_language: str = "en"
    cover_letter: str = ""
    resume_display_name: str = ""
    approval_status: str = "needs_owner_review"
    submission_allowed: bool = False
    upload_allowed: bool = False
    created_at: str = field(default_factory=utcish_now)

    def to_artifact(self) -> dict[str, Any]:
        return {
            "schema": "job.application_draft.v0",
            "created_at": self.created_at,
            "vacancy_url": self.source_url,
            "source_site": self.source_site,
            "company": self.company,
            "title": self.title,
            "resume_artifact": self.resume_display_name,
            "message_language": self.message_language,
            "cover_letter": self.cover_letter,
            "personal_data_included": bool(self.cover_letter),
            "submission_allowed": self.submission_allowed,
            "upload_allowed": self.upload_allowed,
            "approval_status": self.approval_status,
            "channel": self.channel,
        }


@dataclass(frozen=True)
class ProgressNode:
    id: str
    label: str
    kind: str
    status: str = "pending"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressEdge:
    source: str
    target: str
    label: str = ""
    status: str = "pending"


@dataclass(frozen=True)
class ProgressSnapshot:
    schema: str
    generated_at: str
    nodes: list[ProgressNode]
    edges: list[ProgressEdge]
    event_tail: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "nodes": [asdict(node) for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
            "event_tail": self.event_tail,
        }
