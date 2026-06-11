from __future__ import annotations

import html
import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

from .base import JobPlatformAdapter
from .dou import DouAdapter
from .models import DiscoveryQuery, PlatformCapabilities, VacancyObservation
from .workua import WorkUaAdapter


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


def _clean_text(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text[:limit]


def _dedupe_keep_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
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
    return urllib.parse.urljoin(base_url or "https://robota.ua/", href)


class _AnchorCollector(HTMLParser):
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
        if "vacancy" not in href.lower():
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


class RobotaUaAdapter(JobPlatformAdapter):
    capabilities = PlatformCapabilities(
        platform_id="robotaua",
        display_name="Robota.ua",
        allowed_hosts=("robota.ua", "www.robota.ua"),
        supports_prepare_only=False,
        notes="Public discovery/draft template only; no apply/send automation.",
    )

    def discovery_urls(self, query: DiscoveryQuery) -> list[str]:
        text = urllib.parse.quote_plus(query.text)
        return [f"https://robota.ua/zapros/{text}"]

    def normalize_vacancy(self, raw: dict) -> VacancyObservation:
        source_url = self.normalize_url(str(raw.get("source_url", "")))
        title = _clean_text(raw.get("title"), limit=180)
        if not title:
            raise ValueError("Robota.ua vacancy requires a public title")
        company = _clean_text(raw.get("company"), limit=180)
        location = _clean_text(raw.get("location"), limit=180)
        summary = _clean_text(raw.get("summary"), limit=1000)
        text = " ".join([title, company, location, summary, " ".join(raw.get("requirements") or [])])
        provided_tags = [str(tag).strip().lower() for tag in raw.get("fit_tags") or [] if str(tag).strip()]
        fit_tags = _dedupe_keep_order(provided_tags + list(_infer_fit_tags(text)))
        provided_risks = [str(flag).strip() for flag in raw.get("risk_flags") or [] if str(flag).strip()]
        risk_flags = _dedupe_keep_order(provided_risks + list(_risk_flags_for_text(text)))
        return VacancyObservation(
            source_site=self.platform_id,
            source_url=source_url,
            title=title,
            company=company,
            location=location,
            summary=summary,
            requirements=tuple(_clean_text(item, limit=300) for item in raw.get("requirements") or () if _clean_text(item)),
            fit_tags=fit_tags,
            risk_flags=risk_flags,
            status=str(raw.get("status", "observed")).strip() or "observed",
            published_hint=_clean_text(raw.get("published_hint"), limit=80),
            salary_hint=_clean_text(raw.get("salary_hint"), limit=120),
            employment_type=_clean_text(raw.get("employment_type"), limit=80),
            language_hint=_clean_text(raw.get("language_hint"), limit=60),
            source_query=_clean_text(raw.get("source_query"), limit=120),
            raw=dict(raw.get("raw") or {}),
        )

    def extract_public_vacancies(self, markup: str, source_url: str, query: DiscoveryQuery | None = None) -> list[VacancyObservation]:
        """Extract public Robota.ua vacancy observations from saved/listing HTML.

        This parser intentionally uses only public page markup. It does not
        inspect browser state, cookies, account data, or application forms.
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

        anchors = _AnchorCollector(source_url)
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

