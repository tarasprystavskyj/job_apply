from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_platforms.base import JobPlatformAdapter, PlatformRegistry
from job_platforms.models import DiscoveryQuery, ProgressSnapshot, VacancyObservation
from job_platforms.progress import build_progress_snapshot
from job_platforms.registry import default_registry
from shared_job_db import DEFAULT_DB_PATH, add_outreach_draft, append_event, connect, set_status, upsert_job


@dataclass(frozen=True)
class PublicHtmlInput:
    platform_id: str
    path: Path
    source_url: str


@dataclass(frozen=True)
class OrchestratorConfig:
    query: DiscoveryQuery
    platform_ids: tuple[str, ...] = ()
    raw_vacancies_path: Path | None = None
    public_html: tuple[PublicHtmlInput, ...] = ()
    db_path: Path = DEFAULT_DB_PATH
    limit: int = 25
    draft: bool = True
    snapshot_out: Path | None = None


@dataclass(frozen=True)
class OrchestratorResult:
    discovery_urls: dict[str, list[str]]
    observed_jobs: int = 0
    drafts: int = 0
    statuses: int = 0
    blockers: list[dict[str, Any]] = field(default_factory=list)
    snapshot: ProgressSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        snapshot = self.snapshot
        data["snapshot"] = snapshot.to_dict() if snapshot else None
        return data


def run_once(
    config: OrchestratorConfig,
    registry: PlatformRegistry | None = None,
) -> OrchestratorResult:
    """Run one bounded platform pass.

    This orchestrator never fetches pages, watches in a loop, submits, uploads,
    sends messages, or changes account state. It only asks adapters for public
    discovery URLs and persists caller-provided public vacancy artifacts.
    """

    registry = registry or default_registry()
    adapters = _selected_adapters(registry, config.platform_ids)
    discovery_urls: dict[str, list[str]] = {}
    observed_jobs = 0
    drafts = 0
    statuses = 0
    blockers: list[dict[str, Any]] = []

    for adapter in adapters:
        try:
            discovery_urls[adapter.platform_id] = adapter.discovery_urls(config.query)
            append_event(
                "platform_discovery_planned",
                adapter.platform_id,
                status="planned",
                payload={"query": asdict(config.query), "urls": discovery_urls[adapter.platform_id]},
                db_path=config.db_path,
            )
        except Exception as exc:  # keep other adapters running
            blockers.append(_blocker(adapter.platform_id, "discovery_planning_failed", str(exc)))

    observations = _load_observations(config, registry)
    for observation in observations[: max(config.limit, 0)]:
        adapter = registry.get(observation.source_site)
        try:
            job_id = upsert_job(observation, score=_score_observation(observation), db_path=config.db_path)
            observed_jobs += 1
            statuses += 1
            set_status(
                "job",
                job_id,
                observation.status or "observed",
                reason="green orchestrator run-once observation",
                payload={"platform": observation.source_site, "source_url": observation.source_url},
                db_path=config.db_path,
            )
            if config.draft:
                outreach_id = _ensure_review_draft(adapter, observation, job_id, config.db_path)
                if outreach_id:
                    drafts += 1
                    statuses += 1
                    set_status(
                        "outreach",
                        outreach_id,
                        "needs_owner_review",
                        reason="green orchestrator review-only draft",
                        payload={"submit_upload_send_allowed": False},
                        db_path=config.db_path,
                    )
        except Exception as exc:
            blockers.append(_blocker(observation.source_site, "observation_persist_failed", str(exc), observation.source_url))

    for item in blockers:
        append_event(
            "platform_orchestrator_blocker",
            str(item.get("platform") or ""),
            status="blocked",
            payload=item,
            db_path=config.db_path,
        )

    snapshot = build_progress_snapshot(config.db_path, limit=max(config.limit, 1))
    if config.snapshot_out:
        config.snapshot_out.parent.mkdir(parents=True, exist_ok=True)
        config.snapshot_out.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    return OrchestratorResult(
        discovery_urls=discovery_urls,
        observed_jobs=observed_jobs,
        drafts=drafts,
        statuses=statuses,
        blockers=blockers,
        snapshot=snapshot,
    )


def _selected_adapters(registry: PlatformRegistry, platform_ids: tuple[str, ...]) -> list[JobPlatformAdapter]:
    if not platform_ids:
        return registry.all()
    return [registry.get(platform_id) for platform_id in platform_ids]


def _load_observations(config: OrchestratorConfig, registry: PlatformRegistry) -> list[VacancyObservation]:
    observations: list[VacancyObservation] = []
    if config.raw_vacancies_path:
        rows = _read_json_rows(config.raw_vacancies_path)
        for raw in rows:
            platform_id = str(raw.get("source_site") or raw.get("platform") or "")
            adapter = registry.get(platform_id) if platform_id else _adapter_for_raw_url(registry, raw)
            observations.append(adapter.normalize_vacancy(raw))

    for html_input in config.public_html:
        adapter = registry.get(html_input.platform_id)
        extractor = getattr(adapter, "extract_public_vacancies", None)
        if not callable(extractor):
            raise ValueError(f"{html_input.platform_id} adapter does not expose public HTML extraction")
        markup = html_input.path.read_text(encoding="utf-8")
        observations.extend(extractor(markup, html_input.source_url, config.query))
    return _dedupe_observations(observations)


def _read_json_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    loaded = json.loads(text)
    if isinstance(loaded, list):
        return [dict(row) for row in loaded]
    if isinstance(loaded, dict) and isinstance(loaded.get("vacancies"), list):
        return [dict(row) for row in loaded["vacancies"]]
    raise ValueError("raw vacancies must be a JSON list, JSONL rows, or {\"vacancies\": [...]}")


def _adapter_for_raw_url(registry: PlatformRegistry, raw: dict[str, Any]) -> JobPlatformAdapter:
    source_url = str(raw.get("source_url") or "")
    adapter = registry.for_url(source_url)
    if adapter is None:
        raise ValueError(f"no registered adapter can handle raw vacancy URL: {source_url}")
    return adapter


def _dedupe_observations(observations: list[VacancyObservation]) -> list[VacancyObservation]:
    seen: set[str] = set()
    result: list[VacancyObservation] = []
    for observation in observations:
        key = observation.source_url.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(observation)
    return result


def _score_observation(observation: VacancyObservation) -> int:
    score = len(observation.fit_tags) * 8
    if "python" in observation.fit_tags:
        score += 15
    if "ai" in observation.fit_tags or "llm" in observation.fit_tags:
        score += 10
    if observation.company:
        score += 3
    if observation.summary:
        score += 2
    score -= len(observation.risk_flags)
    return max(score, 0)


def _ensure_review_draft(
    adapter: JobPlatformAdapter,
    observation: VacancyObservation,
    job_id: int,
    db_path: Path,
) -> int | None:
    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM outreach WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    if existing:
        outreach_id = int(existing["id"])
        append_event(
            "platform_orchestrator_draft_reused",
            observation.source_site,
            job_id,
            outreach_id,
            "needs_owner_review",
            {"source_url": observation.source_url},
            db_path,
        )
        return 0

    draft = adapter.draft_outreach(observation)
    if draft.submission_allowed or draft.upload_allowed:
        raise PermissionError("registered adapter produced a draft with submit/upload enabled")
    outreach_id = add_outreach_draft(job_id, draft, resume_id=None, db_path=db_path)
    append_event(
        "platform_orchestrator_manual_review_required",
        observation.source_site,
        job_id,
        outreach_id,
        "needs_owner_review",
        {"reason": "Owner must approve exact vacancy/message/resume before any live action."},
        db_path,
    )
    return outreach_id


def _blocker(platform: str, code: str, message: str, source_url: str = "") -> dict[str, Any]:
    return {
        "platform": platform,
        "code": code,
        "message": message,
        "source_url": source_url,
        "telegram_user_surface": True,
        "profile_update_link_relevant": "profile" in message.lower(),
    }


def _parse_public_html(values: list[str]) -> tuple[PublicHtmlInput, ...]:
    parsed: list[PublicHtmlInput] = []
    for value in values:
        parts = value.split("|", 2)
        if len(parts) != 3:
            raise ValueError("--public-html must use platform|path|source_url")
        platform_id, path, source_url = parts
        parsed.append(PublicHtmlInput(platform_id=platform_id, path=Path(path), source_url=source_url))
    return tuple(parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one safe GREEN platform orchestration pass and emit a progress snapshot.",
        epilog=(
            "Examples:\n"
            "  python src\\job_platforms\\orchestrator.py --platform workua --query \"Python AI\" --db tmp\\green.sqlite3\n"
            "  python src\\job_platforms\\orchestrator.py --platform workua --query \"Python AI\" "
            "--public-html \"workua|tmp\\workua.html|https://www.work.ua/jobs-python/\" --snapshot-out tmp\\progress.json\n"
            "  python src\\platform_loop.py --once --platform dou --query \"Python backend\" --no-draft"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--platform", action="append", default=[], help="Registered platform id. Repeatable. Defaults to all.")
    parser.add_argument("--query", default="Python AI automation", help="Public discovery query text.")
    parser.add_argument("--location", default="", help="Optional public discovery location.")
    parser.add_argument("--remote", action="store_true", help="Mark the discovery query as remote-preferred metadata.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum observations to persist in this run.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Shared SQLite DB path.")
    parser.add_argument("--raw-vacancies", type=Path, help="Public vacancy JSON/JSONL to normalize and persist.")
    parser.add_argument(
        "--public-html",
        action="append",
        default=[],
        metavar="PLATFORM|PATH|SOURCE_URL",
        help="Parse one saved public listing/detail HTML artifact. Repeatable.",
    )
    parser.add_argument("--snapshot-out", type=Path, help="Optional path for progress snapshot JSON.")
    parser.add_argument("--no-draft", action="store_true", help="Persist observations only; skip review draft creation.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = OrchestratorConfig(
        query=DiscoveryQuery(text=args.query, location=args.location, remote=True if args.remote else None, limit=args.limit),
        platform_ids=tuple(args.platform),
        raw_vacancies_path=args.raw_vacancies,
        public_html=_parse_public_html(args.public_html),
        db_path=args.db,
        limit=args.limit,
        draft=not args.no_draft,
        snapshot_out=args.snapshot_out,
    )
    result = run_once(config)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 1 if result.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
