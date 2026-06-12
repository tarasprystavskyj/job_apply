#!/usr/bin/env python3
r"""
Prepare or submit explicitly approved Work.ua applications from a CSV artifact.

The script attaches to an already-running Chrome with remote debugging enabled
only for browser prepare/submit modes. It does not read cookies, browser
profile files, saved passwords, .env files, or local resume contents.

Default mode is dry-run validation. Browser preparation requires:

- CSV row: site=workua, Work.ua vacancy URL, exact message, resume/linkedin
  policy columns, approved_to_submit=true, final_submit_allowed=true
- CLI flag: --prepare-browser --i-understand-this-prepares-workua-application

Final submit additionally requires:

- CLI flags: --execute --i-understand-this-submits-workua-application

Resume file upload is intentionally not implemented. Rows with
resume_policy=upload_resume are blocked unless upload_allowed=true and an exact
approved_resume_name is present, and still stop before upload because no file
path is accepted by this adapter.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websocket


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "data" / "job_waves" / "workua_submission_attempts.jsonl"

TRUTHY = {"1", "true", "yes", "y", "allowed", "approve", "approved"}
RESUME_POLICIES = {"no_upload", "no_resume", "use_workua_profile", "use_selected_resume", "upload_resume"}
LINKEDIN_POLICIES = {"no_linkedin", "include_in_message", "fill_field"}


@dataclass(frozen=True)
class WorkUaApplicationRow:
    row_number: int
    site: str
    url: str
    title: str
    company: str
    message: str
    resume_policy: str
    linkedin_policy: str
    linkedin: str
    approved_resume_name: str
    upload_allowed: bool
    approved_to_submit: bool
    final_submit_allowed: bool


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def require_columns(row: dict[str, str], row_number: int, columns: list[str]) -> None:
    missing = [name for name in columns if name not in row]
    if missing:
        raise ValueError(f"CSV row {row_number}: missing columns: {', '.join(missing)}")


def read_rows(csv_path: Path) -> list[WorkUaApplicationRow]:
    required = [
        "site",
        "url",
        "message",
        "resume_policy",
        "linkedin_policy",
        "upload_allowed",
        "approved_to_submit",
        "final_submit_allowed",
    ]
    rows: list[WorkUaApplicationRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, 2):
            require_columns(row, row_number, required)
            rows.append(
                WorkUaApplicationRow(
                    row_number=row_number,
                    site=(row.get("site") or "").strip().lower(),
                    url=(row.get("url") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    company=(row.get("company") or "").strip(),
                    message=(row.get("message") or "").strip(),
                    resume_policy=(row.get("resume_policy") or "").strip().lower(),
                    linkedin_policy=(row.get("linkedin_policy") or "").strip().lower(),
                    linkedin=(row.get("linkedin") or "").strip(),
                    approved_resume_name=(row.get("approved_resume_name") or "").strip(),
                    upload_allowed=truthy(row.get("upload_allowed")),
                    approved_to_submit=truthy(row.get("approved_to_submit")),
                    final_submit_allowed=truthy(row.get("final_submit_allowed")),
                )
            )
    return rows


def validate_row(row: WorkUaApplicationRow, live_action: bool) -> list[str]:
    errors: list[str] = []
    if row.site != "workua":
        errors.append("site must be workua")
    if not re.match(r"^https://(www\.)?work\.ua/jobs/\d+/?(?:[?#].*)?$", row.url):
        errors.append("url must be a public Work.ua vacancy URL")
    if not row.message:
        errors.append("message is required and must be exact row data")
    if row.resume_policy not in RESUME_POLICIES:
        errors.append(f"resume_policy must be one of: {', '.join(sorted(RESUME_POLICIES))}")
    if row.linkedin_policy not in LINKEDIN_POLICIES:
        errors.append(f"linkedin_policy must be one of: {', '.join(sorted(LINKEDIN_POLICIES))}")
    if row.linkedin_policy in {"include_in_message", "fill_field"}:
        if not row.linkedin.startswith("https://www.linkedin.com/in/"):
            errors.append("linkedin must be a public LinkedIn profile URL for this linkedin_policy")
    if row.linkedin_policy == "include_in_message" and row.linkedin and row.linkedin not in row.message:
        errors.append("linkedin_policy=include_in_message requires the exact linkedin URL inside message")
    if row.resume_policy == "use_selected_resume" and not row.approved_resume_name:
        errors.append("approved_resume_name is required when resume_policy=use_selected_resume")
    if row.resume_policy == "upload_resume":
        if not row.upload_allowed:
            errors.append("resume_policy=upload_resume requires upload_allowed=true")
        if not row.approved_resume_name:
            errors.append("resume_policy=upload_resume requires exact approved_resume_name")
        errors.append("resume file upload is not implemented by this Work.ua adapter")
    elif row.upload_allowed:
        errors.append("upload_allowed=true is only valid with resume_policy=upload_resume")
    if live_action and not row.approved_to_submit:
        errors.append("approved_to_submit must be true for browser prepare/execute")
    if live_action and not row.final_submit_allowed:
        errors.append("final_submit_allowed must be true for browser prepare/execute")
    return errors


class CdpTab:
    def __init__(self, websocket_url: str) -> None:
        self.ws = websocket.create_connection(websocket_url, timeout=8, suppress_origin=True)
        self._next_id = 1

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                return msg

    def eval(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in result.get("result", {}):
            desc = result["result"]["exceptionDetails"].get("text", "JS exception")
            raise RuntimeError(desc)
        return result.get("result", {}).get("result", {}).get("value")


def cdp_json(endpoint: str, path: str, method: str = "GET") -> Any:
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def open_tab(endpoint: str, url: str) -> CdpTab:
    encoded = urllib.parse.quote(url, safe="")
    tab_info = cdp_json(endpoint, f"/json/new?{encoded}", method="PUT")
    return CdpTab(tab_info["webSocketDebuggerUrl"])


def wait_for_page(tab: CdpTab, seconds: float = 2.0) -> None:
    tab.call("Runtime.enable")
    tab.call("Page.enable")
    tab.call("Page.bringToFront")
    time.sleep(seconds)


def inspect_state(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const body = document.body ? document.body.innerText.toLowerCase() : "";
  const textOf = e => (e.innerText || e.value || e.getAttribute("aria-label") || "").trim();
  const controls = Array.from(document.querySelectorAll("a,button,input[type=button],input[type=submit]"))
    .filter(visible)
    .map(e => ({
      tag: e.tagName,
      text: textOf(e).slice(0, 120),
      href: e.href || "",
      type: e.type || "",
      inForm: !!e.closest("form")
    }))
    .filter(e => {
      const low = e.text.toLowerCase();
      return low.includes("apply") ||
        low.includes("\u0432\u0456\u0434\u0433\u0443\u043a") ||
        low.includes("\u043d\u0430\u0434\u0456\u0441\u043b") ||
        e.href.includes("/apply");
    })
    .slice(0, 20);
  return {
    url: location.href,
    title: document.title,
    has_form: !!document.querySelector("form"),
    textarea_count: document.querySelectorAll("textarea").length,
    file_input_count: document.querySelectorAll("input[type=file]").length,
    already_applied: body.includes("\u0432\u0436\u0435 \u0432\u0456\u0434\u0433\u0443\u043a") ||
      body.includes("already applied"),
    auth_required: body.includes("\u0443\u0432\u0456\u0439\u0434\u0456\u0442\u044c") ||
      body.includes("\u0437\u0430\u0440\u0435\u0454\u0441\u0442\u0440") ||
      body.includes("sign in") ||
      body.includes("log in"),
    apply_controls: controls
  };
})()
"""
    )


def navigate_to_apply_form(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const hasUsefulForm = () =>
    Array.from(document.querySelectorAll("form")).some(form =>
      form.querySelector("textarea,input[type=email],input[type=tel],input[type=file]"));
  if (hasUsefulForm()) return {ok: true, navigated: false, reason: "application-like form already visible"};

  const candidates = Array.from(document.querySelectorAll("a[href]"))
    .filter(visible)
    .map(a => ({
      href: a.href,
      text: (a.innerText || a.getAttribute("aria-label") || "").trim()
    }))
    .filter(a => {
      const low = a.text.toLowerCase();
      return a.href.includes("/apply") ||
        low.includes("apply") ||
        low.includes("\u0432\u0456\u0434\u0433\u0443\u043a");
    });
  if (!candidates.length) return {ok: false, reason: "no safe apply navigation link"};
  location.href = candidates[0].href;
  return {ok: true, navigated: true, href: candidates[0].href, text: candidates[0].text.slice(0, 120)};
})()
"""
    )


def fill_form(tab: CdpTab, row: WorkUaApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)}
  }};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const forms = Array.from(document.querySelectorAll("form")).filter(visible);
  const form = forms.find(f => f.querySelector("textarea,input[type=email],input[type=tel],input[type=file]")) || forms[0];
  if (!form) return {{ok: false, reason: "no visible application form"}};

  const textareas = Array.from(form.querySelectorAll("textarea")).filter(visible);
  const messageField = textareas.find(t => {{
    const info = `${{t.name}} ${{t.id}} ${{t.placeholder || ""}}`.toLowerCase();
    return info.includes("message") || info.includes("letter") || info.includes("comment") ||
      info.includes("\u043b\u0438\u0441\u0442") || info.includes("\u043f\u043e\u0432\u0456\u0434");
  }}) || textareas[0];
  if (!messageField) return {{ok: false, reason: "no visible message textarea"}};
  messageField.value = expected.message;
  messageField.dispatchEvent(new Event("input", {{bubbles: true}}));
  messageField.dispatchEvent(new Event("change", {{bubbles: true}}));

  let linkedinFilled = false;
  if (expected.linkedinPolicy === "fill_field") {{
    const inputs = Array.from(form.querySelectorAll("input")).filter(visible);
    const linkedInField = inputs.find(input => {{
      const info = `${{input.name}} ${{input.id}} ${{input.placeholder || ""}}`.toLowerCase();
      return info.includes("linkedin") || info.includes("linked in") || info.includes("link");
    }});
    if (!linkedInField) return {{ok: false, reason: "linkedin_policy=fill_field but no visible linkedin input"}};
    linkedInField.value = expected.linkedin;
    linkedInField.dispatchEvent(new Event("input", {{bubbles: true}}));
    linkedInField.dispatchEvent(new Event("change", {{bubbles: true}}));
    linkedinFilled = true;
  }}

  const fileInputs = Array.from(form.querySelectorAll("input[type=file]"));
  const fileInputsWithFiles = fileInputs.filter(input => input.files && input.files.length > 0).length;
  if ((expected.resumePolicy === "no_upload" || expected.resumePolicy === "no_resume") && fileInputsWithFiles) {{
    return {{ok: false, reason: "resume policy forbids upload but a file input already has files"}};
  }}

  let approvedResumeMatched = false;
  if (expected.resumePolicy === "use_selected_resume") {{
    const selectedTexts = [];
    for (const select of Array.from(form.querySelectorAll("select"))) {{
      selectedTexts.push(...Array.from(select.selectedOptions || []).map(o => o.text || ""));
    }}
    for (const checked of Array.from(form.querySelectorAll("input[type=radio]:checked,input[type=checkbox]:checked"))) {{
      const label = checked.closest("label") || (checked.id ? document.querySelector(`label[for="${{CSS.escape(checked.id)}}"]`) : null);
      if (label) selectedTexts.push(label.innerText || "");
    }}
    approvedResumeMatched = selectedTexts.some(text => text.includes(expected.approvedResumeName));
    if (!approvedResumeMatched) {{
      return {{ok: false, reason: "selected Work.ua resume/profile does not match approved_resume_name"}};
    }}
  }}

  return {{
    ok: true,
    messageLength: messageField.value.length,
    linkedinFilled,
    fileInputCount: fileInputs.length,
    fileInputsWithFiles,
    approvedResumeMatched
  }};
}})()
"""
    return tab.eval(expression)


def validate_before_submit(tab: CdpTab, row: WorkUaApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)}
  }};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const norm = value => (value || "").trim();
  const errors = [];
  const details = {{}};
  const forms = Array.from(document.querySelectorAll("form")).filter(visible);
  const form = forms.find(f => f.querySelector("textarea,input[type=email],input[type=tel],input[type=file]")) || forms[0];
  if (!form) return {{ok: false, errors: ["no visible application form"], details}};

  const textareas = Array.from(form.querySelectorAll("textarea")).filter(visible);
  const messageMatches = textareas.some(t => norm(t.value) === norm(expected.message));
  details.messageLength = expected.message.length;
  if (!messageMatches) errors.push("exact approved message was not transmitted to a visible textarea");

  if (expected.linkedinPolicy === "include_in_message" && !expected.message.includes(expected.linkedin)) {{
    errors.push("linkedin_policy=include_in_message but approved message does not contain linkedin");
  }}
  if (expected.linkedinPolicy === "fill_field") {{
    const inputs = Array.from(form.querySelectorAll("input")).filter(visible);
    const linkedinMatches = inputs.some(input => norm(input.value) === norm(expected.linkedin));
    details.linkedinFilled = linkedinMatches;
    if (!linkedinMatches) errors.push("linkedin was not transmitted to a visible field");
  }}

  const fileInputs = Array.from(form.querySelectorAll("input[type=file]"));
  const fileInputsWithFiles = fileInputs.filter(input => input.files && input.files.length > 0).length;
  details.fileInputCount = fileInputs.length;
  details.fileInputsWithFiles = fileInputsWithFiles;
  if ((expected.resumePolicy === "no_upload" || expected.resumePolicy === "no_resume") && fileInputsWithFiles) {{
    errors.push("resume policy forbids upload but file input has files");
  }}
  if (expected.resumePolicy === "use_selected_resume") {{
    const selectedTexts = [];
    for (const select of Array.from(form.querySelectorAll("select"))) {{
      selectedTexts.push(...Array.from(select.selectedOptions || []).map(o => o.text || ""));
    }}
    for (const checked of Array.from(form.querySelectorAll("input[type=radio]:checked,input[type=checkbox]:checked"))) {{
      const label = checked.closest("label") || (checked.id ? document.querySelector(`label[for="${{CSS.escape(checked.id)}}"]`) : null);
      if (label) selectedTexts.push(label.innerText || "");
    }}
    const matched = selectedTexts.some(text => text.includes(expected.approvedResumeName));
    details.approvedResumeMatched = matched;
    if (!matched) errors.push("selected Work.ua resume/profile does not match approved_resume_name");
  }}

  const submit = Array.from(form.querySelectorAll("button,input[type=submit]")).filter(visible).find(button => {{
    const text = (button.innerText || button.value || "").toLowerCase();
    return text.includes("submit") || text.includes("send") || text.includes("apply") ||
      text.includes("\u043d\u0430\u0434\u0456\u0441\u043b") ||
      text.includes("\u0432\u0456\u0434\u0433\u0443\u043a");
  }});
  details.hasSubmitButton = !!submit;
  details.submitDisabled = submit ? submit.disabled : null;
  if (!submit) errors.push("no visible submit/send button in application form");
  if (submit && submit.disabled) errors.push("submit/send button is disabled");
  return {{ok: errors.length === 0, errors, details}};
}})()
"""
    return tab.eval(expression)


def submit_form(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const forms = Array.from(document.querySelectorAll("form")).filter(visible);
  const form = forms.find(f => f.querySelector("textarea,input[type=email],input[type=tel],input[type=file]")) || forms[0];
  if (!form) return {ok: false, submitted: false, reason: "no visible application form"};
  const button = Array.from(form.querySelectorAll("button,input[type=submit]")).filter(visible).find(item => {
    const text = (item.innerText || item.value || "").toLowerCase();
    return text.includes("submit") || text.includes("send") || text.includes("apply") ||
      text.includes("\u043d\u0430\u0434\u0456\u0441\u043b") ||
      text.includes("\u0432\u0456\u0434\u0433\u0443\u043a");
  });
  if (!button) return {ok: false, submitted: false, reason: "no submit/send button"};
  if (button.disabled) return {ok: false, submitted: false, reason: "submit/send button disabled"};
  button.scrollIntoView({block: "center"});
  button.click();
  return {ok: true, submitted: true};
})()
"""
    )


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def base_event(row: WorkUaApplicationRow, live_action: bool, execute: bool) -> dict[str, Any]:
    return {
        "schema": "job.workua_submission_attempt.v0",
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "row_number": row.row_number,
        "site": row.site,
        "source_url": row.url,
        "title": row.title,
        "company": row.company,
        "message_sha256": hashlib.sha256(row.message.encode("utf-8")).hexdigest() if row.message else "",
        "message_length": len(row.message),
        "resume_policy": row.resume_policy,
        "linkedin_policy": row.linkedin_policy,
        "linkedin_present": bool(row.linkedin),
        "approved_resume_name_present": bool(row.approved_resume_name),
        "upload_allowed": row.upload_allowed,
        "approved_to_submit": row.approved_to_submit,
        "final_submit_allowed": row.final_submit_allowed,
        "browser_prepare": live_action,
        "execute": execute,
    }


def process_row(
    row: WorkUaApplicationRow,
    endpoint: str,
    live_action: bool,
    execute: bool,
    log_path: Path,
    delay: float,
) -> int:
    errors = validate_row(row, live_action=live_action)
    event = base_event(row, live_action=live_action, execute=execute)
    if errors:
        append_log(log_path, {**event, "result": "blocked_validation", "errors": errors})
        print(f"row {row.row_number}: blocked: {'; '.join(errors)}")
        return 2
    if not live_action:
        append_log(log_path, {**event, "result": "dry_run_ok"})
        print(f"row {row.row_number}: dry-run ok: {row.url}")
        return 0

    tab: CdpTab | None = None
    try:
        tab = open_tab(endpoint, row.url)
        wait_for_page(tab, delay)
        before = inspect_state(tab)
        if before.get("already_applied"):
            append_log(log_path, {**event, "result": "already_applied", "before": before})
            print(f"row {row.row_number}: already applied: {row.url}")
            return 0

        navigation = navigate_to_apply_form(tab)
        time.sleep(max(delay, 1.0))
        if not navigation.get("ok"):
            state = inspect_state(tab)
            append_log(log_path, {**event, "result": "blocked_no_apply_navigation", "before": before, "navigation": navigation, "state": state})
            print(f"row {row.row_number}: blocked: {navigation.get('reason')}")
            return 3

        filled = fill_form(tab, row)
        if not filled.get("ok"):
            append_log(log_path, {**event, "result": "blocked_fill_failed", "before": before, "navigation": navigation, "filled": filled})
            print(f"row {row.row_number}: fill failed: {filled.get('reason')}")
            return 4

        presubmit = validate_before_submit(tab, row)
        if not presubmit.get("ok"):
            append_log(log_path, {**event, "result": "blocked_presubmit_validation", "before": before, "navigation": navigation, "filled": filled, "presubmit": presubmit})
            print(f"row {row.row_number}: blocked before submit: {'; '.join(presubmit.get('errors', []))}")
            return 4

        if not execute:
            append_log(log_path, {**event, "result": "prepared_presubmit_ok", "before": before, "navigation": navigation, "filled": filled, "presubmit": presubmit})
            print(f"row {row.row_number}: prepared and stopped before final submit: {row.url}")
            return 0

        submitted = submit_form(tab)
        time.sleep(max(delay, 2.0))
        after = inspect_state(tab)
        success = bool(after.get("already_applied"))
        result = "submitted_success" if success else "submit_clicked_unconfirmed"
        append_log(log_path, {**event, "result": result, "before": before, "navigation": navigation, "filled": filled, "presubmit": presubmit, "submitted": submitted, "after": after})
        print(f"row {row.row_number}: {result}: {row.url}")
        return 0 if success else 5
    except Exception as exc:
        append_log(log_path, {**event, "result": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"row {row.row_number}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 10
    finally:
        if tab:
            tab.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or submit explicitly approved Work.ua applications from CSV.")
    parser.add_argument("--csv", type=Path, required=True, help="CSV with approved Work.ua application rows.")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to process; 0 means all.")
    parser.add_argument("--prepare-browser", action="store_true", help="Open Chrome, navigate to the application form, fill fields, and stop before final submit.")
    parser.add_argument("--execute", action="store_true", help="Click the final Work.ua submit/send button after pre-submit validation.")
    parser.add_argument("--i-understand-this-prepares-workua-application", action="store_true")
    parser.add_argument("--i-understand-this-submits-workua-application", action="store_true")
    args = parser.parse_args(argv)

    if args.execute:
        args.prepare_browser = True
    if args.prepare_browser and not args.i_understand_this_prepares_workua_application:
        print("Refusing browser prepare without --i-understand-this-prepares-workua-application.", file=sys.stderr)
        return 2
    if args.execute and not args.i_understand_this_submits_workua_application:
        print("Refusing execute without --i-understand-this-submits-workua-application.", file=sys.stderr)
        return 2

    rows = read_rows(args.csv)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No rows found.", file=sys.stderr)
        return 1

    failures = 0
    for row in rows:
        rc = process_row(
            row,
            args.cdp_endpoint,
            live_action=bool(args.prepare_browser),
            execute=bool(args.execute),
            log_path=args.log,
            delay=args.delay_sec,
        )
        if rc:
            failures += 1
        time.sleep(max(args.delay_sec, 1.0) if args.prepare_browser else 0)
    print(f"Done. rows={len(rows)} failures={failures} log={args.log}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
