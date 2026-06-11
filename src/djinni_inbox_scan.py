#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from djinni_csv_apply import CdpTab, cdp_json
from job_apply_config import ROOT, settings
from vacancy_pipeline import score_vacancy


INBOX_URL = "https://djinni.co/my/inbox/"
DEFAULT_OUTPUT = ROOT / "data" / "job_waves" / "djinni_inbox_offers.jsonl"

PROFILE_ON_PATTERNS = [
    "увімкнути профіль",
    "включити профіль",
    "turn on profile",
    "activate profile",
    "open profile",
]


@dataclass(frozen=True)
class InboxOffer:
    schema: str
    observed_at: str
    source_site: str
    source_type: str
    source_url: str
    thread_url: str
    title: str
    company: str
    snippet: str
    location: str
    score: int
    recommendation: str
    reason: str
    approved_to_reject: bool = False
    reject_executed: bool = False


def open_tab(endpoint: str, url: str) -> CdpTab:
    import urllib.parse

    encoded = urllib.parse.quote(url, safe="")
    tab_info = cdp_json(endpoint, f"/json/new?{encoded}", method="PUT")
    return CdpTab(tab_info["webSocketDebuggerUrl"])


def wait_for_page(tab: CdpTab, seconds: float = 2.0) -> None:
    tab.call("Runtime.enable")
    tab.call("Page.enable")
    tab.call("Page.bringToFront")
    time.sleep(seconds)


def inspect_inbox(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const clean = text => (text || "").replace(/\s+/g, " ").trim();
  const body = clean(document.body ? document.body.innerText : "");
  const anchors = Array.from(document.querySelectorAll("a[href]")).filter(visible);
  const offerLinks = [];
  for (const a of anchors) {
    const href = new URL(a.getAttribute("href"), location.href).href;
    if (!href.includes("/my/inbox/") && !href.includes("/jobs/")) continue;
    const root = a.closest("article, li, tr, .list-jobs__item, .card, .conversation, .thread, .row") || a.parentElement;
    const text = clean(root ? root.innerText : a.innerText);
    if (!text || text.length < 20) continue;
    offerLinks.push({
      href,
      anchorText: clean(a.innerText),
      text: text.slice(0, 1800),
    });
  }
  const seen = new Set();
  const offers = [];
  for (const item of offerLinks) {
    const key = item.href + "|" + item.text.slice(0, 120);
    if (seen.has(key)) continue;
    seen.add(key);
    offers.push(item);
    if (offers.length >= 30) break;
  }
  const buttons = Array.from(document.querySelectorAll("button, a, input[type='submit'], input[type='button']"))
    .filter(visible)
    .map((e, idx) => ({
      idx,
      tag: e.tagName,
      text: clean(e.innerText || e.value || e.getAttribute("aria-label") || e.title),
    }))
    .filter(x => x.text);
  return {
    url: location.href,
    title: document.title,
    bodyStart: body.slice(0, 2000),
    offers,
    buttons,
  };
})()
"""
    )


def split_title_company(text: str, anchor_text: str) -> tuple[str, str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return anchor_text or "Djinni inbox offer", "", text[:700]
    title = anchor_text.strip() or lines[0][:140]
    company = ""
    for line in lines[1:6]:
        low = line.lower()
        if any(marker in low for marker in ["company", "компан", "від ", "from "]):
            company = line[:120]
            break
    if not company and len(lines) > 1:
        company = lines[1][:120]
    return title, company, " ".join(lines[:12])[:900]


def score_offer(title: str, company: str, snippet: str, location: str) -> tuple[int, str, str]:
    row = {
        "title": title,
        "company": company,
        "summary": snippet,
        "location": location,
        "fit_tags": [],
        "requirements": [snippet],
        "risk_flags": [],
    }
    score = score_vacancy(row)
    text = " ".join([title, company, snippet, location]).lower()
    penalties: list[str] = []
    for token in ["junior", "trainee", "wordpress", "php only", "sales manager", "recruiter"]:
        if token in text:
            score -= 12
            penalties.append(token)
    positives = [t for t in ["python", "ai", "llm", "agent", "rag", "automation", "fastapi", "backend"] if t in text]
    if score >= 22:
        return score, "digest", f"relevant signals: {', '.join(positives) or 'general match'}"
    if score <= 5:
        return score, "reject_candidate", f"low score; penalties: {', '.join(penalties) or 'weak technical match'}"
    return score, "review", f"medium score; signals: {', '.join(positives) or 'unclear'}"


def normalize_offer(raw: dict[str, Any]) -> InboxOffer:
    title, company, snippet = split_title_company(str(raw.get("text", "")), str(raw.get("anchorText", "")))
    location = ""
    score, recommendation, reason = score_offer(title, company, snippet, location)
    href = str(raw.get("href", ""))
    return InboxOffer(
        schema="job.djinni_inbox_offer.v0",
        observed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        source_site="djinni_inbox",
        source_type="inbound_offer",
        source_url=href,
        thread_url=href,
        title=title,
        company=company,
        snippet=snippet,
        location=location,
        score=score,
        recommendation=recommendation,
        reason=reason,
    )


def write_jsonl(path: Path, rows: list[InboxOffer]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")


def maybe_turn_on_profile(tab: CdpTab, execute: bool) -> dict[str, Any]:
    patterns = PROFILE_ON_PATTERNS
    expression = f"""
(() => {{
  const patterns = {json.dumps(patterns)};
  const execute = {json.dumps(execute)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const clean = text => (text || "").replace(/\\s+/g, " ").trim();
  const candidates = Array.from(document.querySelectorAll("button, a, input[type='submit'], input[type='button']"))
    .filter(visible)
    .map(e => ({{ element: e, text: clean(e.innerText || e.value || e.getAttribute("aria-label") || e.title) }}))
    .filter(x => patterns.some(p => x.text.toLowerCase().includes(p)));
  if (!candidates.length) return {{found: false, clicked: false}};
  const picked = candidates[0];
  if (execute) {{
    picked.element.scrollIntoView({{block: "center"}});
    picked.element.click();
  }}
  return {{found: true, clicked: execute, text: picked.text}};
}})()
"""
    return tab.eval(expression)


def scan_inbox(output: Path = DEFAULT_OUTPUT, execute_profile_toggle: bool = False, delay_sec: float = 2.0) -> dict[str, Any]:
    cfg = settings()
    tab: CdpTab | None = None
    try:
        tab = open_tab(cfg.cdp_endpoint, INBOX_URL)
        wait_for_page(tab, delay_sec)
        profile = maybe_turn_on_profile(tab, execute=execute_profile_toggle)
        if execute_profile_toggle and profile.get("clicked"):
            time.sleep(2.0)
        state = inspect_inbox(tab)
        rows = [normalize_offer(raw) for raw in state.get("offers", [])]
        write_jsonl(output, rows)
        return {
            "ok": True,
            "url": state.get("url"),
            "profile": profile,
            "offers_found": len(rows),
            "digest": sum(1 for row in rows if row.recommendation == "digest"),
            "review": sum(1 for row in rows if row.recommendation == "review"),
            "reject_candidate": sum(1 for row in rows if row.recommendation == "reject_candidate"),
            "output": str(output),
        }
    finally:
        if tab:
            tab.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan Djinni inbound offers into a local review queue.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--execute-profile-toggle", action="store_true")
    parser.add_argument("--i-understand-this-changes-djinni-profile", action="store_true")
    args = parser.parse_args()
    if args.execute_profile_toggle and not args.i_understand_this_changes_djinni_profile:
        raise SystemExit("--execute-profile-toggle requires --i-understand-this-changes-djinni-profile")
    result = scan_inbox(
        output=args.output,
        execute_profile_toggle=args.execute_profile_toggle,
        delay_sec=args.delay_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
