from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urlparse

from .models import ApprovalGate, DiscoveryQuery, OutreachDraft, PlatformCapabilities, ResumeArtifact, VacancyObservation


class UnsupportedAction(RuntimeError):
    pass


class SafetyGate:
    """Central gate for platform adapters.

    Default platform expansion is discovery/draft only. Preparing a form and
    final submission are separate gates so adapters cannot accidentally merge
    "owner reviewed" with "click final send".
    """

    @staticmethod
    def ensure_prepare_allowed(approval: ApprovalGate) -> None:
        if not approval.allows_prepare():
            raise PermissionError("prepare requires owner approval for the exact vacancy and payload")

    @staticmethod
    def ensure_final_submit_allowed(approval: ApprovalGate) -> None:
        if not approval.allows_final_submit():
            raise PermissionError("final submit requires approved_to_submit=true and final_submit_allowed=true")
        if not approval.approved_message:
            raise PermissionError("final submit requires the exact approved message text")

    @staticmethod
    def ensure_no_upload(upload_allowed: bool) -> None:
        if upload_allowed:
            raise PermissionError("resume upload requires a separate exact-resume approval gate")


class JobPlatformAdapter(ABC):
    capabilities: PlatformCapabilities

    def __init__(self, safety_gate: SafetyGate | None = None) -> None:
        self.safety_gate = safety_gate or SafetyGate()

    @property
    def platform_id(self) -> str:
        return self.capabilities.platform_id

    def can_handle_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == allowed or host.endswith("." + allowed) for allowed in self.capabilities.allowed_hosts)

    def normalize_url(self, url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"refusing non-http vacancy URL: {url}")
        if not self.can_handle_url(url):
            raise ValueError(f"{self.platform_id} adapter cannot handle URL: {url}")
        return parsed.geturl()

    @abstractmethod
    def discovery_urls(self, query: DiscoveryQuery) -> list[str]:
        """Return public search/listing URLs to inspect at low rate."""

    def normalize_vacancy(self, raw: dict) -> VacancyObservation:
        source_url = self.normalize_url(str(raw.get("source_url", "")))
        return VacancyObservation(
            source_site=self.platform_id,
            source_url=source_url,
            title=str(raw.get("title", "")).strip(),
            company=str(raw.get("company", "")).strip(),
            location=str(raw.get("location", "")).strip(),
            summary=str(raw.get("summary", "")).strip(),
            requirements=tuple(raw.get("requirements") or ()),
            fit_tags=tuple(raw.get("fit_tags") or ()),
            risk_flags=tuple(raw.get("risk_flags") or ()),
            status=str(raw.get("status", "observed")).strip() or "observed",
            published_hint=str(raw.get("published_hint", "")).strip(),
            salary_hint=str(raw.get("salary_hint", "")).strip(),
            employment_type=str(raw.get("employment_type", "")).strip(),
            language_hint=str(raw.get("language_hint", "")).strip(),
            source_query=str(raw.get("source_query", "")).strip(),
            raw=dict(raw.get("raw") or {}),
        )

    def draft_outreach(self, vacancy: VacancyObservation, resume: ResumeArtifact | None = None) -> OutreachDraft:
        company = vacancy.company or "team"
        title = vacancy.title or "this role"
        fit = ", ".join(vacancy.fit_tags[:5]) or "Python, AI automation, and reliable delivery"
        salutation = "Hi," if company.lower() == "team" else f"Hi {company} team,"
        return OutreachDraft(
            source_site=self.platform_id,
            source_url=vacancy.source_url,
            company=vacancy.company,
            title=vacancy.title,
            cover_letter=(
                f"{salutation}\n\n"
                f"I am interested in the \"{title}\" role. The strongest fit signals are: {fit}.\n\n"
                "I can bring practical Python automation, AI workflow integration, and senior delivery ownership "
                "to the role, with careful alignment to the team's delivery process.\n\n"
                "Best regards,\nTaras Prystavskyj"
            ),
            resume_display_name=resume.display_name if resume else "",
            submission_allowed=False,
            upload_allowed=False,
        )

    def prepare_application(self, draft: OutreachDraft, approval: ApprovalGate) -> dict:
        self.safety_gate.ensure_prepare_allowed(approval)
        raise UnsupportedAction(f"{self.platform_id} prepare flow is not implemented yet")

    def final_submit(self, draft: OutreachDraft, approval: ApprovalGate) -> dict:
        self.safety_gate.ensure_final_submit_allowed(approval)
        raise UnsupportedAction(f"{self.platform_id} final submit flow is not implemented yet")


class PlatformRegistry:
    def __init__(self, adapters: list[JobPlatformAdapter] | None = None) -> None:
        self._adapters: dict[str, JobPlatformAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: JobPlatformAdapter) -> None:
        self._adapters[adapter.platform_id] = adapter

    def get(self, platform_id: str) -> JobPlatformAdapter:
        return self._adapters[platform_id]

    def all(self) -> list[JobPlatformAdapter]:
        return list(self._adapters.values())

    def for_url(self, url: str) -> JobPlatformAdapter | None:
        for adapter in self._adapters.values():
            if adapter.can_handle_url(url):
                return adapter
        return None
