#!/usr/bin/env python3
r"""
Submit explicitly approved Djinni applications from a CSV file.

This script attaches to an already-running Chrome with remote debugging enabled
and uses only visible Djinni application forms. It does not read cookies,
browser profile files, saved passwords, or local CV files.

Default mode is dry-run. Rows must carry the same explicit structured gate
data in dry-run and execute mode. Real submission additionally requires CLI
execute flags:

- CSV row: approved_to_submit=true and final_submit_allowed=true
- CLI flags: --execute --i-understand-this-submits-applications
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websocket


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "data" / "job_waves" / "djinni_csv_submission_attempts.jsonl"

TRUTHY = {"1", "true", "yes", "y", "allowed", "approve", "approved"}


@dataclass(frozen=True)
class ApplicationRow:
    row_number: int
    site: str
    url: str
    title: str
    company: str
    message: str
    salary_usd: str
    linkedin: str
    resume_policy: str
    approved_resume_name: str
    answers: dict[int, str]
    approved_to_submit: bool
    final_submit_allowed: bool


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def require_columns(row: dict[str, str], row_number: int, columns: list[str]) -> None:
    missing = [name for name in columns if name not in row]
    if missing:
        raise ValueError(f"CSV row {row_number}: missing columns: {', '.join(missing)}")


def load_message(row: dict[str, str], csv_path: Path) -> str:
    message = (row.get("message") or "").strip()
    message_file = (row.get("message_file") or "").strip()
    if message_file:
        raise ValueError("message_file is not allowed; put the exact approved message in the CSV row.")
    return message


def read_rows(csv_path: Path) -> list[ApplicationRow]:
    required = [
        "site",
        "url",
        "message",
        "salary_usd",
        "linkedin",
        "resume_policy",
        "approved_to_submit",
        "final_submit_allowed",
    ]
    rows: list[ApplicationRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, 2):
            require_columns(row, row_number, required)
            answers = {
                idx: (row.get(f"answer_{idx}") or "").strip()
                for idx in range(1, 9)
            }
            rows.append(
                ApplicationRow(
                    row_number=row_number,
                    site=(row.get("site") or "").strip().lower(),
                    url=(row.get("url") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    company=(row.get("company") or "").strip(),
                    message=load_message(row, csv_path),
                    salary_usd=(row.get("salary_usd") or "").strip(),
                    linkedin=(row.get("linkedin") or "").strip(),
                    resume_policy=(row.get("resume_policy") or "").strip().lower(),
                    approved_resume_name=(row.get("approved_resume_name") or "").strip(),
                    answers=answers,
                    approved_to_submit=truthy(row.get("approved_to_submit")),
                    final_submit_allowed=truthy(row.get("final_submit_allowed")),
                )
            )
    return rows


def validate_row(row: ApplicationRow, execute: bool) -> list[str]:
    errors: list[str] = []
    if row.site != "djinni":
        errors.append("only site=djinni is supported by this script")
    if not row.url.startswith("https://djinni.co/jobs/"):
        errors.append("url must be a Djinni job URL")
    if not row.message:
        errors.append("message is required")
    if len(row.message) < 80:
        errors.append("message looks too short")
    if not row.salary_usd.isdigit():
        errors.append("salary_usd must be a number")
    elif not (1 <= int(row.salary_usd) <= 100000):
        errors.append("salary_usd is outside a sane range")
    if not row.linkedin:
        errors.append("linkedin is required")
    elif not row.linkedin.startswith("https://www.linkedin.com/in/"):
        errors.append("linkedin must be a public LinkedIn profile URL")
    if row.resume_policy not in {"no_resume", "use_selected_resume"}:
        errors.append("resume_policy must be no_resume or use_selected_resume")
    if row.resume_policy == "use_selected_resume" and not row.approved_resume_name:
        errors.append("approved_resume_name is required when resume_policy=use_selected_resume")
    if not row.approved_to_submit:
        errors.append("approved_to_submit must be true")
    if not row.final_submit_allowed:
        errors.append("final_submit_allowed must be true")
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
        value = result.get("result", {}).get("result", {}).get("value")
        return value


def cdp_json(endpoint: str, path: str, method: str = "GET") -> Any:
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def open_job_tab(endpoint: str, url: str) -> CdpTab:
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
  const body = document.body ? document.body.innerText : "";
  const form = document.querySelector("#apply_form");
  return {
    url: location.href,
    title: document.title,
    body_start: body.slice(0, 1200),
    already_applied: location.href.includes("applied=ok") || body.includes("Ви вже відгукнулись"),
    success: body.includes("Джин відправив ваш відгук роботодавцю"),
    has_apply_form: !!form,
    has_apply_button: !!document.querySelector(".js-inbox-toggle-reply-form"),
    alerts: Array.from(document.querySelectorAll(".alert,.toast-body,.invalid-feedback,.error,.text-danger"))
      .map(e => e.innerText).filter(Boolean).slice(0, 20)
  };
})()
"""
    )

def inspect_state_precise(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const body = document.body ? document.body.innerText : "";
  const form = document.querySelector("#apply_form");
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const applyishControls = Array.from(document.querySelectorAll("button,a,input[type=submit],input[type=button]"))
    .filter(visible)
    .map(e => ({
      tag: e.tagName,
      text: (e.innerText || e.value || e.getAttribute("aria-label") || "").trim(),
      className: String(e.className || ""),
      id: e.id || "",
      name: e.name || "",
      href: e.href || ""
    }))
    .filter(e => {
      const text = e.text.toLowerCase();
      return text.includes("відгук") || text.includes("apply") || text.includes("надіслати") ||
        e.className.includes("js-inbox-toggle-reply-form");
    })
    .slice(0, 10);
  const alreadyApplied = location.href.includes("applied=ok") ||
    body.includes("Ви вже відгукнулись") ||
    body.includes("Джин відправив ваш відгук");
  const success = body.includes("Джин відправив ваш відгук") ||
    body.includes("Djinni sent your application");
  const inactive = body.includes("Неактивна") ||
    body.includes("Ця вакансія зараз неактивна") ||
    body.includes("inactive vacancy");
  const profileUpdateRequired = body.includes("оновіть профіль") ||
    body.includes("update your profile");
  return {
    url: location.href,
    title: document.title,
    body_start: body.slice(0, 1200),
    already_applied: alreadyApplied,
    success,
    inactive,
    profile_update_required: profileUpdateRequired,
    has_apply_form: !!form,
    has_apply_button: !!document.querySelector(".js-inbox-toggle-reply-form"),
    applyish_controls: applyishControls,
    alerts: Array.from(document.querySelectorAll(".alert,.toast-body,.invalid-feedback,.error,.text-danger"))
      .map(e => e.innerText).filter(Boolean).slice(0, 20)
  };
})()
"""
    )


def classify_no_apply_state(state: dict[str, Any], opened: dict[str, Any] | None = None) -> str:
    if state.get("already_applied"):
        return "already applied"
    if state.get("inactive"):
        return "inactive vacancy"
    if state.get("profile_update_required"):
        return "profile update required before Djinni allows applying"
    controls = state.get("applyish_controls") or []
    if controls:
        return f"apply-like controls visible but unsupported selector: {controls[:3]}"
    if opened and opened.get("reason"):
        return str(opened["reason"])
    return "no visible apply button"


inspect_state = inspect_state_precise


def open_apply_form(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  if (document.querySelector("#apply_form")) return {ok: true, opened: false, reason: "form already open"};
  const visible = e => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const buttons = Array.from(document.querySelectorAll(".js-inbox-toggle-reply-form")).filter(visible);
  if (!buttons.length) return {ok: false, reason: "no visible apply button"};
  buttons[0].scrollIntoView({block: "center"});
  buttons[0].click();
  return {ok: true, opened: true, text: buttons[0].innerText.trim()};
})()
"""
    )


def fill_form(tab: CdpTab, row: ApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const message = {json.dumps(row.message)};
  const linkedinValue = {json.dumps(row.linkedin)};
  const salaryValue = {json.dumps(row.salary_usd)};
  const resumePolicy = {json.dumps(row.resume_policy)};
  const approvedResumeName = {json.dumps(row.approved_resume_name)};
  const answers = {json.dumps(row.answers)};
  const form = document.querySelector("#apply_form");
  if (!form) return {{ok: false, reason: "no apply_form"}};
  const textarea = form.querySelector('textarea[name="message"]');
  const linkedin = form.querySelector('input[name="linkedin"]');
  const salary = form.querySelector('input[name="salary_changed"]');
  const save = form.querySelector('input[name="save_msg_template"]');
  const cv = form.querySelector('select[name="cv_file_upload_id"]');
  if (!textarea) return {{ok: false, reason: "no message textarea"}};
  if (!salary) return {{ok: false, reason: "no salary field"}};

  textarea.value = message;
  textarea.dispatchEvent(new Event("input", {{bubbles: true}}));
  textarea.dispatchEvent(new Event("change", {{bubbles: true}}));

  if (linkedin) {{
    linkedin.value = linkedinValue;
    linkedin.dispatchEvent(new Event("input", {{bubbles: true}}));
    linkedin.dispatchEvent(new Event("change", {{bubbles: true}}));
  }}

  salary.value = salaryValue;
  salary.dispatchEvent(new Event("input", {{bubbles: true}}));
  salary.dispatchEvent(new Event("change", {{bubbles: true}}));

  if (save) {{
    save.checked = false;
    save.dispatchEvent(new Event("change", {{bubbles: true}}));
  }}

  const filledAnswers = [];
  for (const [idx, value] of Object.entries(answers)) {{
    if (!value) continue;
    const answer = form.querySelector(`#answer_text_${{CSS.escape(idx)}}`);
    if (answer) {{
      answer.value = value;
      answer.dispatchEvent(new Event("input", {{bubbles: true}}));
      answer.dispatchEvent(new Event("change", {{bubbles: true}}));
      filledAnswers.push(`answer_${{idx}}`);
    }}
  }}

  let selectedCv = [];
  if (cv && resumePolicy === "no_resume") {{
    cv.value = "";
    for (const option of cv.options) option.selected = option.value === "";
    cv.dispatchEvent(new Event("input", {{bubbles: true}}));
    cv.dispatchEvent(new Event("change", {{bubbles: true}}));
    if (cv.tomselect) cv.tomselect.setValue("", true);
    selectedCv = Array.from(cv.options).filter(o => o.selected).map(o => o.text);
  }} else if (cv && resumePolicy === "use_selected_resume") {{
    selectedCv = Array.from(cv.options).filter(o => o.selected).map(o => o.text);
    if (!selectedCv.some(text => text.includes(approvedResumeName))) {{
      return {{ok: false, reason: "selected resume does not match approved_resume_name", selectedCv}};
    }}
  }}

  const button = form.querySelector('button[name="job_apply"], #job_apply');
  return {{
    ok: true,
    messageLength: textarea.value.length,
    linkedin: linkedin ? linkedin.value : null,
    salary: salary.value,
    saveChecked: save ? save.checked : null,
    cvValue: cv ? cv.value : null,
    selectedCv,
    filledAnswers,
    submitText: button ? button.innerText.trim() : null,
    submitDisabled: button ? button.disabled : null
  }};
}})()
"""
    return tab.eval(expression)


def validate_before_submit(tab: CdpTab, row: ApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    salary: {json.dumps(row.salary_usd)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)},
    answers: {json.dumps(row.answers)}
  }};
  const form = document.querySelector("#apply_form");
  if (!form) return {{ok: false, errors: ["no apply_form"]}};

  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const norm = value => (value || "").trim();
  const errors = [];
  const details = {{}};

  const message = form.querySelector('textarea[name="message"]');
  details.messageLength = message ? message.value.length : null;
  if (!message || norm(message.value) !== norm(expected.message)) {{
    errors.push("message was not transmitted to form");
  }}

  const salary = form.querySelector('input[name="salary_changed"]');
  details.salary = salary ? salary.value : null;
  if (!salary || norm(salary.value) !== norm(expected.salary)) {{
    errors.push("salary_usd was not transmitted to form");
  }}

  const linkedinFields = Array.from(form.querySelectorAll('input[name="linkedin"]')).filter(visible);
  details.visibleLinkedinFields = linkedinFields.length;
  details.linkedin = linkedinFields[0] ? linkedinFields[0].value : null;
  if (expected.linkedin) {{
    if (!linkedinFields.length && expected.resumePolicy !== "use_selected_resume") {{
      errors.push("linkedin provided in CSV but no visible linkedin input is present");
    }} else if (linkedinFields.length && norm(linkedinFields[0].value) !== norm(expected.linkedin)) {{
      errors.push("linkedin was not transmitted to form");
    }}
  }}

  const cv = form.querySelector('select[name="cv_file_upload_id"]');
  details.cvValue = cv ? cv.value : null;
  details.selectedCv = cv ? Array.from(cv.options).filter(o => o.selected).map(o => o.text) : [];
  if (expected.resumePolicy === "no_resume" && cv && norm(cv.value) !== "") {{
    errors.push("resume_policy=no_resume but a resume remains selected");
  }}
  if (expected.resumePolicy === "use_selected_resume") {{
    if (!cv) {{
      errors.push("resume_policy=use_selected_resume but no resume selector is present");
    }} else if (!norm(cv.value)) {{
      errors.push("resume_policy=use_selected_resume but no resume is selected");
    }} else if (expected.approvedResumeName && !details.selectedCv.some(text => text.includes(expected.approvedResumeName))) {{
      errors.push("selected resume does not match approved_resume_name");
    }}
  }}

  const answerDetails = [];
  for (let idx = 1; idx <= 8; idx += 1) {{
    const answer = form.querySelector(`#answer_text_${{idx}}`);
    if (!answer || !visible(answer)) continue;
    const expectedValue = expected.answers[String(idx)] || "";
    const required = answer.required || answer.hasAttribute("required") || answer.getAttribute("aria-required") === "true";
    const value = norm(answer.value);
    answerDetails.push({{id: answer.id, required, hasValue: value.length > 0}});
    if (expectedValue && value !== norm(expectedValue)) {{
      errors.push(`answer_${{idx}} was not transmitted to form`);
    }}
    if (required && !value) {{
      errors.push(`required recruiter answer textarea answer_text_${{idx}} is empty`);
    }}
  }}
  details.answers = answerDetails;

  const button = form.querySelector('button[name="job_apply"], #job_apply');
  details.submitDisabled = button ? button.disabled : null;
  if (!button) errors.push("no submit button");
  if (button && button.disabled) errors.push("submit button disabled");

  return {{ok: errors.length === 0, errors, details}};
}})()
"""
    return tab.eval(expression)


def submit_form(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const form = document.querySelector("#apply_form");
  const button = form ? form.querySelector('button[name="job_apply"], #job_apply') : null;
  if (!button) return {ok: false, submitted: false, reason: "no submit button"};
  if (button.disabled) return {ok: false, submitted: false, reason: "submit button disabled"};
  button.scrollIntoView({block: "center"});
  button.click();
  return {ok: true, submitted: true, text: button.innerText.trim()};
})()
"""
    )


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def process_row(row: ApplicationRow, endpoint: str, execute: bool, log_path: Path, delay: float) -> int:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    errors = validate_row(row, execute)
    base_event = {
        "schema": "job.djinni_csv_submission_attempt.v0",
        "attempted_at": started_at,
        "row_number": row.row_number,
        "site": row.site,
        "source_url": row.url,
        "title": row.title,
        "company": row.company,
        "salary_expectation_usd": int(row.salary_usd) if row.salary_usd.isdigit() else row.salary_usd,
        "linkedin": row.linkedin,
        "resume_policy": row.resume_policy,
        "approved_resume_name": row.approved_resume_name,
        "approved_to_submit": row.approved_to_submit,
        "final_submit_allowed": row.final_submit_allowed,
        "execute": execute,
    }
    if errors:
        append_log(log_path, {**base_event, "result": "blocked_validation", "errors": errors})
        print(f"row {row.row_number}: blocked: {'; '.join(errors)}")
        return 2
    if not execute:
        append_log(log_path, {**base_event, "result": "dry_run_ok"})
        print(f"row {row.row_number}: dry-run ok: {row.url}")
        return 0

    tab: CdpTab | None = None
    try:
        tab = open_job_tab(endpoint, row.url)
        wait_for_page(tab, delay)
        before = inspect_state(tab)
        if before.get("already_applied"):
            append_log(log_path, {**base_event, "result": "already_applied", "before": before})
            print(f"row {row.row_number}: already applied: {row.url}")
            return 0

        opened = open_apply_form(tab)
        time.sleep(1.0)
        if not opened.get("ok"):
            after_open = inspect_state(tab)
            reason = classify_no_apply_state(after_open, opened)
            append_log(log_path, {**base_event, "result": "blocked_no_apply_form", "blocked_reason": reason, "opened": opened, "state": after_open})
            print(f"row {row.row_number}: blocked: {reason}")
            return 3

        filled = fill_form(tab, row)
        if not filled.get("ok"):
            append_log(log_path, {**base_event, "result": "blocked_fill_failed", "filled": filled})
            print(f"row {row.row_number}: fill failed: {filled.get('reason')}")
            return 4

        presubmit = validate_before_submit(tab, row)
        if not presubmit.get("ok"):
            append_log(log_path, {**base_event, "result": "blocked_presubmit_validation", "filled": filled, "presubmit": presubmit})
            print(f"row {row.row_number}: blocked before submit: {'; '.join(presubmit.get('errors', []))}")
            return 4

        submitted = submit_form(tab)
        time.sleep(3.0)
        after = inspect_state(tab)
        success = bool(after.get("success") or after.get("already_applied") or "applied=ok" in str(after.get("url", "")))
        result = "submitted_success" if success else "submit_clicked_unconfirmed"
        append_log(log_path, {**base_event, "result": result, "before": before, "opened": opened, "filled": filled, "submitted": submitted, "after": after})
        print(f"row {row.row_number}: {result}: {row.url}")
        return 0 if success else 5
    except Exception as exc:
        append_log(log_path, {**base_event, "result": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"row {row.row_number}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 10
    finally:
        if tab:
            tab.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit explicitly approved Djinni applications from CSV.")
    parser.add_argument("--csv", type=Path, required=True, help="CSV with approved Djinni application rows.")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to process; 0 means all.")
    parser.add_argument("--execute", action="store_true", help="Actually click Djinni submit buttons for approved rows.")
    parser.add_argument("--i-understand-this-submits-applications", action="store_true")
    args = parser.parse_args(argv)

    execute = bool(args.execute)
    if execute and not args.i_understand_this_submits_applications:
        print("Refusing execute without --i-understand-this-submits-applications.", file=sys.stderr)
        return 2

    rows = read_rows(args.csv)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No rows found.", file=sys.stderr)
        return 1

    failures = 0
    for row in rows:
        rc = process_row(row, args.cdp_endpoint, execute, args.log, args.delay_sec)
        if rc:
            failures += 1
        time.sleep(max(args.delay_sec, 1.0))
    print(f"Done. rows={len(rows)} failures={failures} log={args.log}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
