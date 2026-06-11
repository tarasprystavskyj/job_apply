#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from djinni_csv_apply import CdpTab, cdp_json
from djinni_inbox_scan import wait_for_page
from job_apply_config import ROOT, settings


DEFAULT_OUTPUT = ROOT / "data" / "job_waves" / "recruiter_responses.jsonl"
DEFAULT_THREAD_URLS: list[str] = []


@dataclass(frozen=True)
class RecruiterResponse:
    schema: str
    observed_at: str
    source_site: str
    thread_url: str
    company: str
    role: str
    status: str
    recruiter_message: str
    evidence: str
    likely_lessons: list[str]
    option_a: str
    option_b: str
    suggested_thank_you_reply: str


def configured_thread_urls() -> list[str]:
    settings()
    raw = os.environ.get("JOB_APPLY_RECRUITER_RESPONSE_THREADS", "")
    urls = [url.strip() for url in raw.split(",") if url.strip()]
    merged = list(dict.fromkeys(urls + DEFAULT_THREAD_URLS))
    return [url for url in merged if url.startswith("https://djinni.co/my/inbox/")]


def open_tab(endpoint: str, url: str) -> CdpTab:
    import urllib.parse

    encoded = urllib.parse.quote(url, safe="")
    tab_info = cdp_json(endpoint, f"/json/new?{encoded}", method="PUT")
    return CdpTab(tab_info["webSocketDebuggerUrl"])


def inspect_thread(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const clean = text => (text || "").replace(/\s+/g, " ").trim();
  const body = clean(document.body ? document.body.innerText : "");
  const areas = Array.from(document.querySelectorAll("article, .message, .conversation, .card, li, main, section"))
    .map(e => clean(e.innerText))
    .filter(t => t.length > 30)
    .slice(0, 20);
  return {
    url: location.href,
    title: document.title,
    body: body.slice(0, 8000),
    areas,
  };
})()
"""
    )


def classify_response(thread_url: str, state: dict[str, Any]) -> RecruiterResponse:
    body = str(state.get("body", ""))
    low = body.lower()
    company, role = parse_company_role(state, body)
    status = "review"
    if any(token in low for token in ["відхилено", "can't offer", "cannot offer", "not move forward", "unfortunately"]):
        status = "rejected"
    elif any(token in low for token in ["interview", "call", "meeting", "next step", "технічн", "співбесід"]):
        status = "positive_or_action_needed"
    recruiter_message = extract_recruiter_message(body)
    lessons = infer_lessons(body, recruiter_message)
    recruiter_name = parse_recruiter_name(body)
    thank_you_reply = draft_thank_you_reply(company=company, role=role, recruiter_name=recruiter_name)
    return RecruiterResponse(
        schema="job.recruiter_response.v0",
        observed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        source_site="djinni",
        thread_url=thread_url,
        company=company,
        role=role,
        status=status,
        recruiter_message=recruiter_message,
        evidence=evidence_excerpt(body),
        likely_lessons=lessons,
        option_a=(
            "Консервативний таргетинг: пріоритезувати Python AI automation, LLM workflow, internal tooling, "
            "backend automation і product-engineering ролі; понизити пріоритет вакансій, де явно потрібен "
            "великий enterprise production ownership у ML/RAG/graph."
        ),
        option_b=(
            "Посилення позиціонування: продовжувати подаватись на senior AI ролі, але переписати профіль/CV/"
            "cover letters так, щоб першими йшли конкретні LLM/RAG/multi-agent deliverables, вимірювані "
            "системи і production-like ownership; пом'якшити caveats, якщо рекрутер прямо не питає."
        ),
        suggested_thank_you_reply=thank_you_reply,
    )


def parse_company_role(state: dict[str, Any], body: str) -> tuple[str, str]:
    title = str(state.get("title", ""))
    company = title.split("—", 1)[0].strip() if "—" in title else ""
    role = ""
    markers = ["Senior Python AI Engineer", "Python Engineer", "AI Engineer", "Backend Engineer"]
    for marker in markers:
        if marker in body:
            role = marker
            break
    return company, role


def parse_recruiter_name(body: str) -> str:
    # Djinni conversation text usually contains the recruiter card near the
    # company name. Keep this conservative to avoid inventing a name.
    known_names = ["Valeriia", "Валерія", "Валерия"]
    lowered = body.lower()
    for name in known_names:
        if name.lower() in lowered:
            return name
    return ""


def draft_thank_you_reply(company: str = "", role: str = "", recruiter_name: str = "") -> str:
    greeting = f"Hi {recruiter_name}," if recruiter_name else "Hi,"
    company_part = f" at {company}" if company else ""
    role_part = f" for {role}" if role else ""
    return (
        f"{greeting}\n\n"
        f"Thank you for the quick response and for reviewing my application{role_part}. "
        "I appreciate your time. "
        f"If a future role{company_part} is a closer fit for my Python, AI automation, "
        "and engineering background, I would be glad to reconnect.\n\n"
        "Best regards,\n"
        "Taras"
    )


def extract_recruiter_message(body: str) -> str:
    markers = [
        "Thanks for your interest.",
        "At this moment",
        "Unfortunately",
        "Дякуємо",
    ]
    for marker in markers:
        idx = body.find(marker)
        if idx >= 0:
            return body[idx : idx + 600]
    return body[-600:]


def evidence_excerpt(body: str) -> str:
    for token in ["Ваш відгук на цю позицію відхилено", "can't offer", "відхилено"]:
        idx = body.lower().find(token.lower())
        if idx >= 0:
            start = max(0, idx - 220)
            return body[start : idx + 360]
    return body[:700]


def infer_lessons(body: str, recruiter_message: str) -> list[str]:
    text = " ".join([body, recruiter_message]).lower()
    lessons: list[str] = []
    if any(token in text for token in ["not led large", "not managed a large-scale", "not built dedicated graph"]):
        lessons.append("Відповіді могли занадто підкреслити прогалини для senior enterprise AI ролей.")
    if any(token in text for token in ["rag", "graph", "semantic", "multi-agent", "llm"]):
        lessons.append("Рекрутери шукають конкретні докази LLM/RAG/multi-agent/semantic-systems досвіду.")
    if "can't offer" in text or "відхилено" in text:
        lessons.append("Це радше сигнал про таргетинг/позиціонування, а не детальний технічний фідбек.")
    if not lessons:
        lessons.append("Конкретної причини від рекрутера не знайдено; враховувати як нейтральний сигнал.")
    return lessons


def scan_recruiter_responses(
    thread_urls: list[str] | None = None,
    output: Path = DEFAULT_OUTPUT,
    delay_sec: float = 2.0,
) -> dict[str, Any]:
    cfg = settings()
    urls = thread_urls or configured_thread_urls()
    rows: list[RecruiterResponse] = []
    for url in urls:
        tab: CdpTab | None = None
        try:
            tab = open_tab(cfg.cdp_endpoint, url)
            wait_for_page(tab, delay_sec)
            state = inspect_thread(tab)
            rows.append(classify_response(url, state))
        finally:
            if tab:
                tab.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "ok": True,
        "output": str(output),
        "responses_found": len(rows),
        "rejected": sum(1 for row in rows if row.status == "rejected"),
        "positive_or_action_needed": sum(1 for row in rows if row.status == "positive_or_action_needed"),
    }


def latest_response_summary(path: Path = DEFAULT_OUTPUT) -> str:
    if not path.exists():
        return "Відповіді рекрутерів: ще не сканувались."
    rows = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return "Відповіді рекрутерів: немає розпізнаних відповідей."
    rejected = [row for row in rows if row.get("status") == "rejected"]
    latest = rows[-1]
    lines = [
        f"Відповіді рекрутерів: {len(rows)} діалог(ів), відмов={len(rejected)}.",
    ]
    if rejected:
        lines.append(f"Остання відмова: {latest.get('company') or 'unknown company'} - {latest.get('role') or 'unknown role'}.")
        lessons = latest.get("likely_lessons") or []
        if lessons:
            lines.append("Висновок: " + lessons[0])
        lines.append("Варіант A: " + str(latest.get("option_a", "")))
        lines.append("Варіант B: " + str(latest.get("option_b", "")))
        if latest.get("suggested_thank_you_reply") or latest.get("status") == "rejected":
            lines.append("Драфт подяки рекрутеру підготовлено; відправка тільки після окремого approval.")
    return "\n".join(lines)


def latest_response_for_thread(thread_url: str, path: Path = DEFAULT_OUTPUT) -> dict[str, Any] | None:
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("thread_url") == thread_url:
            latest = row
    return latest


def draft_thank_you_for_thread(thread_url: str, path: Path = DEFAULT_OUTPUT) -> str:
    row = latest_response_for_thread(thread_url, path)
    if row and row.get("suggested_thank_you_reply"):
        return str(row["suggested_thank_you_reply"])
    if row:
        return draft_thank_you_reply(company=str(row.get("company", "")), role=str(row.get("role", "")))
    return draft_thank_you_reply()


def prepare_or_send_thank_you(
    thread_url: str,
    message: str,
    *,
    execute_send: bool = False,
    delay_sec: float = 2.0,
) -> dict[str, Any]:
    cfg = settings()
    tab: CdpTab | None = None
    try:
        tab = open_tab(cfg.cdp_endpoint, thread_url)
        wait_for_page(tab, delay_sec)
        expression = f"""
(() => {{
  const message = {json.dumps(message)};
  const executeSend = {json.dumps(execute_send)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const clean = text => (text || "").replace(/\\s+/g, " ").trim();
  const clickByText = patterns => {{
    const controls = Array.from(document.querySelectorAll("button,a,input[type=button],input[type=submit]")).filter(visible);
    for (const control of controls) {{
      const text = clean(control.innerText || control.value || control.getAttribute("aria-label") || "").toLowerCase();
      if (patterns.some(pattern => text.includes(pattern))) {{
        control.scrollIntoView({{block: "center"}});
        control.click();
        return {{clicked: true, text}};
      }}
    }}
    return {{clicked: false}};
  }};
  const opened = clickByText(["reply", "відповісти", "написати"]);
  const textarea = Array.from(document.querySelectorAll("textarea")).filter(visible).pop();
  const editable = Array.from(document.querySelectorAll("[contenteditable=true]")).filter(visible).pop();
  const target = textarea || editable;
  if (!target) {{
    return {{ok: false, filled: false, sent: false, reason: "no visible reply field", opened}};
  }}
  target.scrollIntoView({{block: "center"}});
  target.focus();
  if (textarea) {{
    textarea.value = message;
    textarea.dispatchEvent(new Event("input", {{bubbles: true}}));
    textarea.dispatchEvent(new Event("change", {{bubbles: true}}));
  }} else {{
    editable.innerText = message;
    editable.dispatchEvent(new InputEvent("input", {{bubbles: true, inputType: "insertText", data: message}}));
    editable.dispatchEvent(new Event("change", {{bubbles: true}}));
  }}
  if (!executeSend) {{
    return {{ok: true, filled: true, sent: false, dry_run: true, opened, messageLength: message.length}};
  }}
  const submitted = clickByText(["send", "відправити", "надіслати"]);
  return {{ok: submitted.clicked, filled: true, sent: submitted.clicked, submitted, opened, messageLength: message.length}};
}})()
"""
        return tab.eval(expression)
    finally:
        if tab:
            tab.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan visible Djinni recruiter responses and classify outcomes.")
    parser.add_argument("--thread-url", action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--draft-thank-you", action="store_true", help="Print the suggested thank-you reply for a thread.")
    parser.add_argument("--prepare-thank-you", action="store_true", help="Fill the reply box but do not send.")
    parser.add_argument("--send-thank-you", action="store_true", help="Send the thank-you reply to the recruiter.")
    parser.add_argument("--message", default="", help="Exact thank-you message to prepare/send.")
    parser.add_argument("--i-understand-this-sends-recruiter-message", action="store_true")
    args = parser.parse_args()
    if args.send_thank_you and not args.i_understand_this_sends_recruiter_message:
        print("Refusing send without --i-understand-this-sends-recruiter-message.", file=sys.stderr)
        return 2
    if args.draft_thank_you or args.prepare_thank_you or args.send_thank_you:
        if len(args.thread_url) != 1:
            print("Exactly one --thread-url is required for thank-you operations.", file=sys.stderr)
            return 2
        thread_url = args.thread_url[0]
        message = args.message or draft_thank_you_for_thread(thread_url, args.output)
        if args.draft_thank_you:
            print(message)
            return 0
        result = prepare_or_send_thank_you(
            thread_url,
            message,
            execute_send=bool(args.send_thank_you),
            delay_sec=args.delay_sec,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    result = scan_recruiter_responses(thread_urls=args.thread_url or None, output=args.output, delay_sec=args.delay_sec)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(latest_response_summary(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
