#!/usr/bin/env python3
r"""
Prepare and submit explicitly approved Robota.ua applications from CSV.

Default mode is validation-only. Browser preparation and final submission are
separate live actions:

- CSV row: site=robotaua, Robota.ua vacancy URL, exact message, resume policy,
  LinkedIn policy, approved_to_submit=true, final_submit_allowed=true
- Pre-submit CLI: --pre-submit --i-understand-this-prepares-robotaua-application
- Final submit CLI: --execute --i-understand-this-submits-robotaua-application

This script attaches to an already-running Chrome with remote debugging enabled.
It does not read cookies, browser profile files, saved passwords, or local CV
contents. It uploads resume files only when the CSV row names the exact resume,
sets upload_allowed=true, and passes the platform-specific live action gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import websocket


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "data" / "job_waves" / "robotaua_submission_attempts.jsonl"
DEFAULT_RESUME_DIR = Path(os.environ.get("JOB_APPLY_RESUME_DIR", r"C:\python_scripts\projects_search\my_resumes"))
APPROVED_RESUME_ARTIFACT_DIRS = (ROOT / "data" / "resumes", ROOT / "data" / "job_waves", ROOT / "data" / "approved_artifacts")
ROBOTAU_ATTACH_SELECTOR = (
    "body > app-root > div > alliance-apply-page-shell > div > alliance-apply-page-new > "
    "div > div:nth-child(2) > div > lib-attach-apply-block > div > label > div"
)
ROBOTAU_COVER_LETTER_SELECTOR = (
    "body > app-root > div > alliance-apply-page-shell > div > alliance-apply-page-new > "
    "div > div:nth-child(2) > div > alliance-apply-cover-letter > div"
)
ROBOTAU_FINAL_SEND_SELECTOR = (
    "body > app-root > div > alliance-apply-page-shell > div > alliance-apply-page-new > "
    "div > div.fixed-btn-wrapper > div > santa-button-spinner > div > santa-button > button"
)
ROBOTAU_UPLOAD_MARKER_ATTR = "data-job-apply-robotaua-resume-upload"

TRUTHY = {"1", "true", "yes", "y", "allowed", "approve", "approved"}
RESUME_POLICIES = {"no_resume", "use_site_resume", "upload_exact_resume"}
LINKEDIN_POLICIES = {"include_linkedin", "omit_linkedin"}
ROBOTAU_HOSTS = {"robota.ua", "www.robota.ua"}
RESUME_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf"}


Mode = Literal["dry_run", "pre_submit", "submit"]


@dataclass(frozen=True)
class RobotauaApplicationRow:
    row_number: int
    site: str
    url: str
    title: str
    company: str
    message: str
    salary_usd: str
    linkedin: str
    linkedin_policy: str
    resume_policy: str
    approved_resume_name: str
    upload_allowed: bool
    answers: dict[int, str]
    approved_to_submit: bool
    final_submit_allowed: bool
    approved_resume_path: str = ""


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


def open_job_tab(endpoint: str, url: str) -> CdpTab:
    encoded = urllib.parse.quote(url, safe="")
    tab_info = cdp_json(endpoint, f"/json/new?{encoded}", method="PUT")
    return CdpTab(tab_info["webSocketDebuggerUrl"])


def wait_for_page(tab: Any, seconds: float = 2.0) -> None:
    if hasattr(tab, "call"):
        tab.call("Runtime.enable")
        tab.call("Page.enable")
        tab.call("Page.bringToFront")
    time.sleep(seconds)


def wait_for_interactive_state(tab: Any, seconds: float = 8.0) -> dict[str, Any]:
    deadline = time.time() + seconds
    state = inspect_state(tab)
    while time.time() < deadline:
        if state.get("body_start") or state.get("controls") or state.get("fields") or state.get("login_required"):
            return state
        time.sleep(1.0)
        state = inspect_state(tab)
    return state


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def require_columns(row: dict[str, str], row_number: int, columns: list[str]) -> None:
    missing = [name for name in columns if name not in row]
    if missing:
        raise ValueError(f"CSV row {row_number}: missing columns: {', '.join(missing)}")


def load_message(row: dict[str, str], csv_path: Path) -> str:
    message = (row.get("message") or "").strip()
    message_file = (row.get("message_file") or "").strip()
    if message and message_file:
        raise ValueError("Use either message or message_file, not both.")
    if not message_file:
        return message
    path = Path(message_file)
    if not path.is_absolute():
        path = csv_path.parent / path
    resolved = path.resolve()
    if ROOT not in resolved.parents and resolved != ROOT:
        raise ValueError(f"message_file must stay inside workspace: {resolved}")
    return resolved.read_text(encoding="utf-8").strip()


def read_rows(csv_path: Path) -> list[RobotauaApplicationRow]:
    required = [
        "site",
        "url",
        "message",
        "resume_policy",
        "linkedin_policy",
        "approved_to_submit",
        "final_submit_allowed",
    ]
    rows: list[RobotauaApplicationRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, 2):
            require_columns(row, row_number, required)
            answers = {idx: (row.get(f"answer_{idx}") or "").strip() for idx in range(1, 9)}
            rows.append(
                RobotauaApplicationRow(
                    row_number=row_number,
                    site=(row.get("site") or "").strip().lower(),
                    url=(row.get("url") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    company=(row.get("company") or "").strip(),
                    message=load_message(row, csv_path),
                    salary_usd=(row.get("salary_usd") or "").strip(),
                    linkedin=(row.get("linkedin") or "").strip(),
                    linkedin_policy=(row.get("linkedin_policy") or "").strip().lower(),
                    resume_policy=(row.get("resume_policy") or "").strip().lower(),
                    approved_resume_name=(row.get("approved_resume_name") or "").strip(),
                    upload_allowed=truthy(row.get("upload_allowed")),
                    answers=answers,
                    approved_to_submit=truthy(row.get("approved_to_submit")),
                    final_submit_allowed=truthy(row.get("final_submit_allowed")),
                    approved_resume_path=(row.get("approved_resume_path") or row.get("resume_path") or "").strip(),
                )
            )
    return rows


def is_robotaua_vacancy_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in ROBOTAU_HOSTS and bool(re.search(r"/vacancy\d+", parsed.path, re.I))


def application_form_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not is_robotaua_vacancy_url(url):
        return url
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query["newApply"] = ["true"]
    query_string = urllib.parse.urlencode(query, doseq=True)
    if re.search(r"/apply/?$", parsed.path, re.I):
        return urllib.parse.urlunparse(parsed._replace(query=query_string))
    path = parsed.path.rstrip("/") + "/apply"
    return urllib.parse.urlunparse(parsed._replace(path=path, query=query_string))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolved_resume_roots(resume_dir: Path | None = None) -> tuple[Path, ...]:
    base = resume_dir or DEFAULT_RESUME_DIR
    roots = [base, *APPROVED_RESUME_ARTIFACT_DIRS]
    return tuple(root.expanduser().resolve(strict=False) for root in roots)


def resolve_resume_upload_path(row: RobotauaApplicationRow, resume_dir: Path | None = None) -> Path | None:
    if row.resume_policy != "upload_exact_resume":
        return None
    exact_name = row.approved_resume_name.strip()
    if not exact_name:
        raise ValueError("approved_resume_name is required when resume_policy=upload_exact_resume")
    if Path(exact_name).name != exact_name:
        raise ValueError("approved_resume_name must be an exact file name, not a path")
    if Path(exact_name).suffix.lower() not in RESUME_FILE_EXTENSIONS:
        raise ValueError("approved_resume_name must be a PDF/DOC/DOCX/RTF resume file")

    roots = resolved_resume_roots(resume_dir)
    if row.approved_resume_path:
        raw = Path(row.approved_resume_path).expanduser()
        candidate = raw if raw.is_absolute() else ROOT / raw
        if candidate.name != exact_name:
            raise ValueError("approved_resume_path basename must match approved_resume_name exactly")
        candidates = [candidate]
    else:
        candidates = [roots[0] / exact_name]

    checked: list[str] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        checked.append(str(resolved))
        if not any(is_relative_to(resolved, root) or resolved == root for root in roots):
            raise ValueError("approved resume path must stay inside the resume directory or approved artifact directory")
        if resolved.exists() and resolved.is_file():
            return resolved
    raise ValueError(f"approved resume file was not found: {', '.join(checked)}")


def validate_row(row: RobotauaApplicationRow, mode: Mode) -> list[str]:
    errors: list[str] = []
    if row.site != "robotaua":
        errors.append("site must be robotaua")
    if not is_robotaua_vacancy_url(row.url):
        errors.append("url must be an https Robota.ua vacancy URL")
    if not row.message:
        errors.append("exact message is required")
    elif len(row.message) < 40:
        errors.append("exact message looks too short")
    if row.resume_policy not in RESUME_POLICIES:
        errors.append("resume_policy must be no_resume, use_site_resume, or upload_exact_resume")
    if row.linkedin_policy not in LINKEDIN_POLICIES:
        errors.append("linkedin_policy must be include_linkedin or omit_linkedin")
    if row.linkedin_policy == "include_linkedin" and not row.linkedin.startswith("https://www.linkedin.com/in/"):
        errors.append("linkedin_policy=include_linkedin requires a public LinkedIn profile URL")
    if row.linkedin_policy == "omit_linkedin" and row.linkedin:
        errors.append("linkedin must be empty when linkedin_policy=omit_linkedin")
    if row.resume_policy == "no_resume":
        if row.approved_resume_name:
            errors.append("approved_resume_name must be empty when resume_policy=no_resume")
        if row.upload_allowed:
            errors.append("upload_allowed must be false when resume_policy=no_resume")
    if row.resume_policy == "use_site_resume":
        if not row.approved_resume_name:
            errors.append("approved_resume_name is required when resume_policy=use_site_resume")
        if row.upload_allowed:
            errors.append("upload_allowed must be false when using an already available site resume")
    if row.resume_policy == "upload_exact_resume":
        if not row.approved_resume_name:
            errors.append("approved_resume_name is required when resume_policy=upload_exact_resume")
        if not row.upload_allowed:
            errors.append("upload_allowed=true is required when resume_policy=upload_exact_resume")
        if row.approved_resume_name and row.upload_allowed:
            try:
                resolve_resume_upload_path(row)
            except ValueError as exc:
                errors.append(str(exc))
    if row.salary_usd and (not row.salary_usd.isdigit() or not (1 <= int(row.salary_usd) <= 100000)):
        errors.append("salary_usd must be a sane number when provided")
    if not row.approved_to_submit:
        errors.append("approved_to_submit must be true")
    if not row.final_submit_allowed:
        errors.append("final_submit_allowed must be true")
    return errors


def inspect_state(tab: Any) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const body = document.body ? document.body.innerText : "";
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const controls = Array.from(document.querySelectorAll("button,a,input[type=submit],input[type=button]"))
    .filter(visible)
    .map((e, idx) => ({
      idx,
      tag: e.tagName,
      text: (e.innerText || e.value || e.getAttribute("aria-label") || "").trim(),
      className: String(e.className || ""),
      id: e.id || "",
      name: e.name || "",
      href: e.href || ""
    }))
    .filter(e => {
      const text = e.text.toLowerCase();
      return text.includes("відгук") || text.includes("надісл") || text.includes("apply") ||
        text.includes("отклик") || text.includes("відправ") || text.includes("send");
    })
    .slice(0, 20);
  const fields = Array.from(document.querySelectorAll("textarea,input,select"))
    .filter(visible)
    .map(e => ({
      tag: e.tagName,
      type: e.type || "",
      name: e.name || "",
      id: e.id || "",
      placeholder: e.placeholder || "",
      valueLength: e.value ? String(e.value).length : 0,
      required: !!e.required || e.getAttribute("aria-required") === "true"
    }))
    .slice(0, 40);
  return {
    url: location.href,
    title: document.title,
    body_start: body.slice(0, 1200),
    already_applied: /вже відгук|уже отклик|already applied|відгук надіслано|response sent/i.test(body),
    login_required: /увійдіть|войти|sign in|login/i.test(body),
    inactive: /вакансія закрита|вакансия закрыта|inactive|archived/i.test(body),
    controls,
    fields,
    alerts: Array.from(document.querySelectorAll(".alert,.toast,.error,.invalid,.text-danger,[role=alert]"))
      .map(e => e.innerText).filter(Boolean).slice(0, 20)
  };
})()
"""
    )


def open_application_form(tab: Any) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const hasMessageField = () => Array.from(document.querySelectorAll("textarea"))
    .some(e => visible(e) && !/search|пошук|поиск/i.test(`${e.name} ${e.id} ${e.placeholder}`));
  if (hasMessageField()) return {ok: true, opened: false, reason: "message form already visible"};
  const candidates = Array.from(document.querySelectorAll("button,a,input[type=button]")).filter(visible);
  const button = candidates.find(e => {
    const text = (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase();
    return text.includes("відгук") || text.includes("отклик") || text.includes("apply");
  });
  if (!button) return {ok: false, reason: "no visible apply/open-form control"};
  if (button.tagName === "INPUT" && String(button.type).toLowerCase() === "submit") {
    return {ok: false, reason: "refusing to use submit input as open-form control"};
  }
  button.scrollIntoView({block: "center"});
  button.click();
  return {ok: true, opened: true, text: (button.innerText || button.value || "").trim()};
})()
"""
    )


def fill_form(tab: Any, row: RobotauaApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    salary: {json.dumps(row.salary_usd)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)},
    uploadAllowed: {json.dumps(row.upload_allowed)},
    answers: {json.dumps(row.answers)}
  }};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const norm = value => (value || "").trim();
  const textareas = Array.from(document.querySelectorAll("textarea")).filter(e =>
    visible(e) && !/search|пошук|поиск/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}}`)
  );
  const message = textareas[0];
  if (!message) return {{ok: false, reason: "no visible message textarea"}};
  message.value = expected.message;
  message.dispatchEvent(new Event("input", {{bubbles: true}}));
  message.dispatchEvent(new Event("change", {{bubbles: true}}));

  const filledAnswers = [];
  for (const [idx, value] of Object.entries(expected.answers)) {{
    if (!value) continue;
    const extra = textareas[Number(idx)] || document.querySelector(`#answer_text_${{CSS.escape(idx)}}`);
    if (extra && extra !== message) {{
      extra.value = value;
      extra.dispatchEvent(new Event("input", {{bubbles: true}}));
      extra.dispatchEvent(new Event("change", {{bubbles: true}}));
      filledAnswers.push(`answer_${{idx}}`);
    }}
  }}

  const linkedinInputs = Array.from(document.querySelectorAll("input")).filter(e =>
    visible(e) && /linkedin|linked|лінкед|линкед/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`)
  );
  if (expected.linkedinPolicy === "include_linkedin" && linkedinInputs.length) {{
    linkedinInputs[0].value = expected.linkedin;
    linkedinInputs[0].dispatchEvent(new Event("input", {{bubbles: true}}));
    linkedinInputs[0].dispatchEvent(new Event("change", {{bubbles: true}}));
  }}

  const salaryInputs = Array.from(document.querySelectorAll("input")).filter(e =>
    visible(e) && /salary|зарп|зп|оплат/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`)
  );
  if (expected.salary && salaryInputs.length) {{
    salaryInputs[0].value = expected.salary;
    salaryInputs[0].dispatchEvent(new Event("input", {{bubbles: true}}));
    salaryInputs[0].dispatchEvent(new Event("change", {{bubbles: true}}));
  }}

  const fileInputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(visible);
  const selects = Array.from(document.querySelectorAll("select")).filter(visible);
  const selectedResumeText = selects.flatMap(select => Array.from(select.options || [])
    .filter(option => option.selected)
    .map(option => option.text || ""));

  return {{
    ok: true,
    messageLength: message.value.length,
    linkedinFields: linkedinInputs.length,
    linkedin: linkedinInputs[0] ? linkedinInputs[0].value : "",
    salaryFields: salaryInputs.length,
    salary: salaryInputs[0] ? salaryInputs[0].value : "",
    fileInputs: fileInputs.length,
    selectedResumeText,
    filledAnswers
  }};
}})()
"""
    result = tab.eval(expression)
    if not result.get("ok"):
        return result
    if row.resume_policy == "upload_exact_resume":
        upload = attach_resume_file(tab, row)
        result["resumeUpload"] = upload
        if not upload.get("ok"):
            return {"ok": False, "reason": upload.get("reason", "resume upload failed"), "baseFill": result}
    return result


def attach_resume_file(tab: Any, row: RobotauaApplicationRow) -> dict[str, Any]:
    resume_path = resolve_resume_upload_path(row)
    if resume_path is None:
        return {"ok": True, "skipped": True}
    if not hasattr(tab, "call"):
        return {"ok": False, "reason": "CDP tab.call is required for file attachment"}

    prepare = tab.eval(
        f"""
(() => {{
  const attachSelector = {json.dumps(ROBOTAU_ATTACH_SELECTOR)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const attach = document.querySelector(attachSelector);
  if (attach && visible(attach)) {{
    attach.scrollIntoView({{block: "center"}});
    attach.click();
  }}
  const scope = attach ? (attach.closest("lib-attach-apply-block") || attach.closest("form") || document) : document;
  const input = scope.querySelector('input[type="file"]') ||
    document.querySelector('lib-attach-apply-block input[type="file"]') ||
    document.querySelector('input[type="file"]');
  if (!input) return {{ok: false, reason: "no resume file input found", attachSelectorFound: !!attach}};
  input.setAttribute({json.dumps(ROBOTAU_UPLOAD_MARKER_ATTR)}, "1");
  return {{
    ok: true,
    attachSelectorFound: !!attach,
    accept: input.accept || "",
    multiple: !!input.multiple,
    visibleInput: visible(input)
  }};
}})()
"""
    )


def page_requires_resume_method(state: dict[str, Any]) -> bool:
    text = str(state.get("body_start") or "")
    return "Оберіть спосіб відгуку" in text and (
        "Прикріпити файл з резюме" in text or "Створити резюме" in text
    )


def open_cover_letter(tab: Any) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  if (Array.from(document.querySelectorAll("textarea")).some(visible)) {
    return {ok: true, opened: false, reason: "textarea already visible"};
  }
  const candidates = Array.from(document.querySelectorAll("button,a,label,div,p,span"))
    .filter(visible)
    .filter(e => /Додати супровідний лист|cover letter|супровід/i.test(e.innerText || e.textContent || ""));
  if (!candidates.length) return {ok: false, reason: "no cover-letter opener"};
  const picked = candidates[0];
  picked.scrollIntoView({block: "center"});
  picked.click();
  return {ok: true, opened: true, text: (picked.innerText || picked.textContent || "").trim().slice(0, 120)};
})()
""".replace("__ROBOTAU_FINAL_SEND_SELECTOR__", json.dumps(ROBOTAU_FINAL_SEND_SELECTOR))
    )
    if not prepare.get("ok"):
        return prepare

    remote = tab.call(
        "Runtime.evaluate",
        {
            "expression": f"document.querySelector('input[type=\"file\"][{ROBOTAU_UPLOAD_MARKER_ATTR}=\"1\"]')",
            "returnByValue": False,
        },
    )
    object_id = remote.get("result", {}).get("result", {}).get("objectId")
    if not object_id:
        return {"ok": False, "reason": "resume file input object was not available", "prepare": prepare}
    set_result = tab.call("DOM.setFileInputFiles", {"objectId": object_id, "files": [str(resume_path)]})
    verify = tab.eval(
        f"""
(() => {{
  const input = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]');
  if (!input) return {{ok: false, reason: "marked resume file input disappeared"}};
  const namesBeforeEvents = Array.from(input.files || []).map(file => file.name);
  input.dispatchEvent(new Event("input", {{bubbles: true}}));
  input.dispatchEvent(new Event("change", {{bubbles: true}}));
  const names = Array.from(input.files || []).map(file => file.name);
  const bodyText = document.body ? document.body.innerText : "";
  const visibleFileName = bodyText.includes({json.dumps(row.approved_resume_name)});
  return {{
    ok: namesBeforeEvents.includes({json.dumps(row.approved_resume_name)}) || names.includes({json.dumps(row.approved_resume_name)}) || visibleFileName,
    fileNamesBeforeEvents: namesBeforeEvents,
    fileNames: names,
    visibleFileName,
    expectedName: {json.dumps(row.approved_resume_name)}
  }};
}})()
"""
    )
    if not verify.get("ok"):
        document = tab.call("DOM.getDocument", {"depth": -1, "pierce": True})
        root_id = document.get("result", {}).get("root", {}).get("nodeId")
        if root_id:
            query = tab.call(
                "DOM.querySelector",
                {"nodeId": root_id, "selector": f'input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]'},
            )
            node_id = query.get("result", {}).get("nodeId")
            if node_id:
                set_result = tab.call("DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(resume_path)]})
                verify = tab.eval(
                    f"""
(() => {{
  const input = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]');
  if (!input) return {{ok: false, reason: "marked resume file input disappeared"}};
  const namesBeforeEvents = Array.from(input.files || []).map(file => file.name);
  input.dispatchEvent(new Event("input", {{bubbles: true}}));
  input.dispatchEvent(new Event("change", {{bubbles: true}}));
  const names = Array.from(input.files || []).map(file => file.name);
  const bodyText = document.body ? document.body.innerText : "";
  const visibleFileName = bodyText.includes({json.dumps(row.approved_resume_name)});
  return {{
    ok: namesBeforeEvents.includes({json.dumps(row.approved_resume_name)}) || names.includes({json.dumps(row.approved_resume_name)}) || visibleFileName,
    fileNamesBeforeEvents: namesBeforeEvents,
    fileNames: names,
    visibleFileName,
    expectedName: {json.dumps(row.approved_resume_name)}
  }};
}})()
"""
                )
    if not verify.get("ok") and hasattr(tab, "ws") and hasattr(tab, "_next_id"):
        try:
            chooser = set_files_via_file_chooser_event(tab, resume_path)
        except Exception as exc:
            chooser = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if chooser.get("ok"):
            set_result = chooser.get("set_result", set_result)
            verify = verify_attached_resume(tab, row)
    return {"ok": bool(verify.get("ok")), "path": str(resume_path), "prepare": prepare, "set_result": set_result, "verify": verify}


def set_files_via_file_chooser_event(tab: Any, resume_path: Path) -> dict[str, Any]:
    tab.call("Page.setInterceptFileChooserDialog", {"enabled": True})
    msg_id = tab._next_id
    tab._next_id += 1
    expression = f"""
(() => {{
  const input = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]') ||
    document.querySelector('lib-attach-apply-block input[type="file"]') ||
    document.querySelector('input[type="file"]');
  if (!input) return {{ok: false, reason: "no file input to click"}};
  input.click();
  return {{ok: true}};
}})()
"""
    tab.ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}))
    event_params: dict[str, Any] | None = None
    eval_result: dict[str, Any] | None = None
    deadline = time.time() + 5
    while time.time() < deadline and (event_params is None or eval_result is None):
        msg = json.loads(tab.ws.recv())
        if msg.get("id") == msg_id:
            eval_result = msg
        elif msg.get("method") == "Page.fileChooserOpened":
            event_params = msg.get("params") or {}
    if not event_params:
        return {"ok": False, "reason": "file chooser event was not observed", "eval_result": eval_result}
    backend_node_id = event_params.get("backendNodeId")
    if not backend_node_id:
        return {"ok": False, "reason": "file chooser event did not include backendNodeId", "event": event_params}
    set_result = tab.call("DOM.setFileInputFiles", {"backendNodeId": backend_node_id, "files": [str(resume_path)]})
    return {"ok": True, "event": event_params, "set_result": set_result}


def verify_attached_resume(tab: Any, row: RobotauaApplicationRow) -> dict[str, Any]:
    return tab.eval(
        f"""
(() => {{
  const input = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]') ||
    document.querySelector('lib-attach-apply-block input[type="file"]') ||
    document.querySelector('input[type="file"]');
  if (!input) return {{ok: false, reason: "resume file input disappeared"}};
  const namesBeforeEvents = Array.from(input.files || []).map(file => file.name);
  input.dispatchEvent(new Event("input", {{bubbles: true}}));
  input.dispatchEvent(new Event("change", {{bubbles: true}}));
  const names = Array.from(input.files || []).map(file => file.name);
  const bodyText = document.body ? document.body.innerText : "";
  const visibleFileName = bodyText.includes({json.dumps(row.approved_resume_name)});
  return {{
    ok: namesBeforeEvents.includes({json.dumps(row.approved_resume_name)}) || names.includes({json.dumps(row.approved_resume_name)}) || visibleFileName,
    fileNamesBeforeEvents: namesBeforeEvents,
    fileNames: names,
    visibleFileName,
    expectedName: {json.dumps(row.approved_resume_name)}
  }};
}})()
"""
    )


def validate_before_submit(tab: Any, row: RobotauaApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    salary: {json.dumps(row.salary_usd)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)},
    uploadAllowed: {json.dumps(row.upload_allowed)},
    answers: {json.dumps(row.answers)}
  }};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const norm = value => (value || "").trim();
  const errors = [];
  const details = {{}};
  const textareas = Array.from(document.querySelectorAll("textarea")).filter(e =>
    visible(e) && !/search|пошук|поиск/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}}`)
  );
  const message = textareas[0];
  details.messageLength = message ? message.value.length : null;
  if (!message || norm(message.value) !== norm(expected.message)) errors.push("exact message was not transmitted to form");

  const linkedinInputs = Array.from(document.querySelectorAll("input")).filter(e =>
    visible(e) && /linkedin|linked|лінкед|линкед/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`)
  );
  details.linkedinFields = linkedinInputs.length;
  details.linkedin = linkedinInputs[0] ? linkedinInputs[0].value : "";
  if (expected.linkedinPolicy === "include_linkedin" && linkedinInputs.length && norm(linkedinInputs[0].value) !== norm(expected.linkedin)) {{
    errors.push("linkedin was not transmitted to form");
  }}

  const salaryInputs = Array.from(document.querySelectorAll("input")).filter(e =>
    visible(e) && /salary|зарп|зп|оплат/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`)
  );
  details.salaryFields = salaryInputs.length;
  details.salary = salaryInputs[0] ? salaryInputs[0].value : "";
  if (expected.salary && salaryInputs.length && norm(salaryInputs[0].value) !== norm(expected.salary)) {{
    errors.push("salary_usd was not transmitted to form");
  }}

  const visibleFileInputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(visible);
  const uploadInput = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]') ||
    document.querySelector('lib-attach-apply-block input[type="file"]') ||
    document.querySelector('input[type="file"]');
  details.fileInputs = visibleFileInputs.length;
  details.uploadFileNames = uploadInput ? Array.from(uploadInput.files || []).map(file => file.name) : [];
  details.uploadFileNameVisible = (document.body ? document.body.innerText : "").includes(expected.approvedResumeName);
  if (visibleFileInputs.length && !expected.uploadAllowed) errors.push("visible resume upload input is present but upload_allowed is false");
  if (expected.resumePolicy === "upload_exact_resume") {{
    if (!expected.uploadAllowed) errors.push("upload_allowed is false for upload_exact_resume");
    if (!details.uploadFileNames.includes(expected.approvedResumeName) && !details.uploadFileNameVisible) {{
      errors.push("approved resume file is not attached");
    }}
  }}

  const selects = Array.from(document.querySelectorAll("select")).filter(visible);
  details.selectedResumeText = selects.flatMap(select => Array.from(select.options || [])
    .filter(option => option.selected)
    .map(option => option.text || ""));
  if (expected.resumePolicy === "use_site_resume" && expected.approvedResumeName) {{
    const selected = details.selectedResumeText.join("\\n");
    if (selects.length && !selected.includes(expected.approvedResumeName)) {{
      errors.push("selected site resume does not match approved_resume_name");
    }}
  }}

  for (let idx = 1; idx <= 8; idx += 1) {{
    const expectedValue = expected.answers[String(idx)] || "";
    if (!expectedValue) continue;
    const answer = textareas[idx] || document.querySelector(`#answer_text_${{idx}}`);
    if (answer && norm(answer.value) !== norm(expectedValue)) errors.push(`answer_${{idx}} was not transmitted to form`);
  }}

  const submitControls = Array.from(document.querySelectorAll("button,input[type=submit]")).filter(visible)
    .filter(e => {{
      const text = (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase();
      return text.includes("надісл") || text.includes("відправ") || text.includes("send") ||
        text.includes("отправ") || text.includes("відгук") || text.includes("apply");
    }});
  const exactSubmit = document.querySelector({json.dumps(ROBOTAU_FINAL_SEND_SELECTOR)});
  if (exactSubmit && visible(exactSubmit) && !submitControls.includes(exactSubmit)) submitControls.unshift(exactSubmit);
  details.submitControls = submitControls.map(e => ({{
    tag: e.tagName,
    text: (e.innerText || e.value || "").trim(),
    disabled: !!e.disabled
  }})).slice(0, 10);
  if (!submitControls.length) errors.push("no final submit control found");
  if (submitControls[0] && submitControls[0].disabled) errors.push("final submit control is disabled");
  return {{ok: errors.length === 0, errors, details}};
}})()
"""
    return tab.eval(expression)


def submit_form(tab: Any) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const exactSelector = __ROBOTAU_FINAL_SEND_SELECTOR__;
  const exactButton = document.querySelector(exactSelector);
  if (exactButton && visible(exactButton)) {
    if (exactButton.disabled) return {ok: false, submitted: false, reason: "final submit control disabled", selector: exactSelector};
    exactButton.scrollIntoView({block: "center"});
    exactButton.click();
    return {ok: true, submitted: true, selector: exactSelector, text: (exactButton.innerText || exactButton.value || "").trim()};
  }
  const buttons = Array.from(document.querySelectorAll("button,input[type=submit]")).filter(visible)
    .filter(e => {
      const text = (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase();
      return text.includes("надісл") || text.includes("відправ") || text.includes("send") ||
        text.includes("отправ") || text.includes("відгук") || text.includes("apply");
    });
  const button = buttons[0];
  if (!button) return {ok: false, submitted: false, reason: "no final submit control"};
  if (button.disabled) return {ok: false, submitted: false, reason: "final submit control disabled"};
  button.scrollIntoView({block: "center"});
  button.click();
  return {ok: true, submitted: true, text: (button.innerText || button.value || "").trim()};
})()
""".replace("__ROBOTAU_FINAL_SEND_SELECTOR__", json.dumps(ROBOTAU_FINAL_SEND_SELECTOR))
    )


# Robota.ua's apply page is an SPA whose actionable controls can be labels or
# divs rather than native buttons. Keep these overrides near the process path so
# the live worker uses the robust flow while preserving the older JS above for
# auditability.
def inspect_state(tab: Any) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const body = document.body ? document.body.innerText : "";
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const textOf = e => (e.innerText || e.textContent || e.value || e.getAttribute("aria-label") || "").trim();
  const relevant = e => {
    const text = textOf(e).toLowerCase();
    const meta = `${e.id || ""} ${e.className || ""} ${e.getAttribute("role") || ""} ${e.tagName || ""}`.toLowerCase();
    return text.includes("\u0432\u0456\u0434\u0433\u0443\u043a") ||
      text.includes("\u043d\u0430\u0434\u0456\u0441\u043b") ||
      text.includes("\u043f\u0440\u0438\u043a\u0440\u0456\u043f") ||
      text.includes("\u0440\u0435\u0437\u044e\u043c\u0435") ||
      text.includes("\u0441\u0443\u043f\u0440\u043e\u0432\u0456\u0434") ||
      text.includes("\u043f\u0440\u043e\u0434\u043e\u0432\u0436") ||
      text.includes("\u0441\u0442\u0432\u043e\u0440\u0438\u0442\u0438") ||
      text.includes("\u043e\u0442\u043a\u043b\u0438\u043a") ||
      text.includes("apply") || text.includes("send") || text.includes("resume") ||
      meta.includes("attach") || meta.includes("apply");
  };
  const controls = Array.from(document.querySelectorAll("button,a,input[type=submit],input[type=button],label,[role=button],[tabindex],lib-attach-apply-block div"))
    .filter(visible)
    .filter(relevant)
    .map((e, idx) => ({
      idx,
      tag: e.tagName,
      text: textOf(e),
      className: String(e.className || ""),
      id: e.id || "",
      name: e.name || "",
      href: e.href || ""
    }))
    .slice(0, 30);
  const fields = Array.from(document.querySelectorAll("textarea,input,select"))
    .filter(e => visible(e) || String(e.type || "").toLowerCase() === "file")
    .map(e => ({
      tag: e.tagName,
      type: e.type || "",
      name: e.name || "",
      id: e.id || "",
      placeholder: e.placeholder || "",
      valueLength: e.value ? String(e.value).length : 0,
      required: !!e.required || e.getAttribute("aria-required") === "true"
    }))
    .slice(0, 60);
  return {
    url: location.href,
    title: document.title,
    body_start: body.slice(0, 1200),
    already_applied: /\u0432\u0436\u0435 \u0432\u0456\u0434\u0433\u0443\u043a|already applied|response sent/i.test(body),
    login_required: /\u0443\u0432\u0456\u0439\u0434\u0456\u0442\u044c|\u0432\u043e\u0439\u0442\u0438|sign in|login/i.test(body),
    inactive: /\u0432\u0430\u043a\u0430\u043d\u0441\u0456\u044f \u0437\u0430\u043a\u0440\u0438\u0442\u0430|inactive|archived/i.test(body),
    controls,
    fields,
    alerts: Array.from(document.querySelectorAll(".alert,.toast,.error,.invalid,.text-danger,[role=alert]"))
      .map(e => e.innerText).filter(Boolean).slice(0, 20)
  };
})()
""".replace("__ROBOTAU_FINAL_SEND_SELECTOR__", json.dumps(ROBOTAU_FINAL_SEND_SELECTOR))
    )


def open_application_form(tab: Any, row: RobotauaApplicationRow | None = None) -> dict[str, Any]:
    resume_policy = row.resume_policy if row else ""
    return tab.eval(
        f"""
(() => {{
  const expected = {{resumePolicy: {json.dumps(resume_policy)}}};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const textOf = e => (e ? (e.innerText || e.textContent || e.value || e.getAttribute("aria-label") || "").trim() : "");
  const body = document.body ? document.body.innerText : "";
  const lowBody = body.toLowerCase();
  const hasMessageField = () => Array.from(document.querySelectorAll("textarea"))
    .some(e => visible(e) && !/search|\\u043f\\u043e\\u0448\\u0443\\u043a|\\u043f\\u043e\\u0438\\u0441\\u043a/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}}`));
  if (hasMessageField()) return {{ok: true, opened: false, reason: "message form already visible"}};
  const methodPage = lowBody.includes("\\u043e\\u0431\\u0435\\u0440\\u0456\\u0442\\u044c \\u0441\\u043f\\u043e\\u0441\\u0456\\u0431") ||
    lowBody.includes("\\u043f\\u0440\\u0438\\u043a\\u0440\\u0456\\u043f\\u0438\\u0442\\u0438 \\u0444\\u0430\\u0439\\u043b") ||
    lowBody.includes("\\u0441\\u0442\\u0432\\u043e\\u0440\\u0438\\u0442\\u0438 \\u0440\\u0435\\u0437\\u044e\\u043c\\u0435");
  const controls = Array.from(document.querySelectorAll("button,a,input[type=button],label,[role=button],[tabindex],lib-attach-apply-block div"))
    .filter(visible);
  const findByText = (...needles) => controls.find(e => {{
    const text = textOf(e).toLowerCase();
    const meta = `${{e.id || ""}} ${{e.className || ""}} ${{e.tagName || ""}}`.toLowerCase();
    return needles.some(needle => text.includes(needle) || meta.includes(needle));
  }});
  if (methodPage && expected.resumePolicy === "no_resume") {{
    return {{ok: false, reason: "blocked_policy_requires_resume", policy: expected.resumePolicy, body_start: body.slice(0, 500)}};
  }}
  if (methodPage && expected.resumePolicy === "upload_exact_resume") {{
    const attach = document.querySelector("lib-attach-apply-block label") ||
      document.querySelector("lib-attach-apply-block div") ||
      findByText("\\u043f\\u0440\\u0438\\u043a\\u0440\\u0456\\u043f", "\\u0444\\u0430\\u0439\\u043b", "attach", "upload");
    if (!attach) return {{ok: false, reason: "no attach-resume apply method control", body_start: body.slice(0, 500)}};
    attach.scrollIntoView({{block: "center"}});
    attach.click();
    return {{ok: true, opened: true, reason: "attach-resume method selected", text: textOf(attach)}};
  }}
  const button = controls.find(e => {{
    const text = textOf(e).toLowerCase();
    return text.includes("\\u0432\\u0456\\u0434\\u0433\\u0443\\u043a") ||
      text.includes("\\u043e\\u0442\\u043a\\u043b\\u0438\\u043a") ||
      text.includes("apply");
  }});
  if (!button) return {{ok: false, reason: "no visible apply/open-form control", body_start: body.slice(0, 500)}};
  if (button.tagName === "INPUT" && String(button.type).toLowerCase() === "submit") {{
    return {{ok: false, reason: "refusing to use submit input as open-form control"}};
  }}
  button.scrollIntoView({{block: "center"}});
  button.click();
  return {{ok: true, opened: true, text: textOf(button)}};
}})()
"""
    )


def page_requires_resume_method(state: dict[str, Any]) -> bool:
    text = str(state.get("body_start") or "").lower()
    has_method_prompt = (
        "\u043e\u0431\u0435\u0440\u0456\u0442\u044c \u0441\u043f\u043e\u0441\u0456\u0431 \u0432\u0456\u0434\u0433\u0443\u043a\u0443" in text
        or "РћР±РµСЂС–С‚СЊ СЃРїРѕСЃС–Р± РІС–РґРіСѓРєСѓ".lower() in text
    )
    has_resume_method = (
        "\u043f\u0440\u0438\u043a\u0440\u0456\u043f\u0438\u0442\u0438 \u0444\u0430\u0439\u043b \u0437 \u0440\u0435\u0437\u044e\u043c\u0435" in text
        or "\u0441\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u0440\u0435\u0437\u044e\u043c\u0435" in text
        or "РџСЂРёРєСЂС–РїРёС‚Рё С„Р°Р№Р» Р· СЂРµР·СЋРјРµ".lower() in text
        or "РЎС‚РІРѕСЂРёС‚Рё СЂРµР·СЋРјРµ".lower() in text
    )
    return has_method_prompt and has_resume_method


def open_cover_letter(tab: Any) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const exactSelector = __ROBOTAU_COVER_LETTER_SELECTOR__;
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  if (Array.from(document.querySelectorAll("textarea")).some(visible)) {
    return {ok: true, opened: false, reason: "textarea already visible"};
  }
  const textOf = e => (e.innerText || e.textContent || e.value || e.getAttribute("aria-label") || "").trim();
  const exact = document.querySelector(exactSelector);
  if (exact && visible(exact)) {
    exact.scrollIntoView({block: "center"});
    exact.click();
    return {ok: true, opened: true, selector: exactSelector, text: textOf(exact).slice(0, 120)};
  }
  const candidates = Array.from(document.querySelectorAll("button,a,label,div,p,span,[role=button],[tabindex]"))
    .filter(visible)
    .filter(e => {
      const text = textOf(e).toLowerCase();
      return text.includes("\u0434\u043e\u0434\u0430\u0442\u0438 \u0441\u0443\u043f\u0440\u043e\u0432\u0456\u0434") ||
        text.includes("\u0441\u0443\u043f\u0440\u043e\u0432\u0456\u0434\u043d") ||
        text.includes("cover letter");
    });
  if (!candidates.length) return {ok: false, reason: "no cover-letter opener"};
  const picked = candidates[0];
  picked.scrollIntoView({block: "center"});
  picked.click();
  return {ok: true, opened: true, text: textOf(picked).slice(0, 120)};
})()
""".replace("__ROBOTAU_COVER_LETTER_SELECTOR__", json.dumps(ROBOTAU_COVER_LETTER_SELECTOR))
    )


def attach_resume_file(tab: Any, row: RobotauaApplicationRow) -> dict[str, Any]:
    resume_path = resolve_resume_upload_path(row)
    if resume_path is None:
        return {"ok": True, "skipped": True}
    if not hasattr(tab, "call"):
        return {"ok": False, "reason": "CDP tab.call is required for file attachment"}

    prepare = tab.eval(
        f"""
(() => {{
  const attachSelector = {json.dumps(ROBOTAU_ATTACH_SELECTOR)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const textOf = e => (e ? (e.innerText || e.textContent || e.value || e.getAttribute("aria-label") || "").trim() : "");
  const controls = Array.from(document.querySelectorAll("label,[role=button],[tabindex],lib-attach-apply-block div,button,input[type=button]"))
    .filter(visible);
  const attach = document.querySelector(attachSelector) ||
    document.querySelector("lib-attach-apply-block label") ||
    document.querySelector("lib-attach-apply-block div") ||
    controls.find(e => {{
      const text = textOf(e).toLowerCase();
      return text.includes("\\u043f\\u0440\\u0438\\u043a\\u0440\\u0456\\u043f") ||
        text.includes("\\u0444\\u0430\\u0439\\u043b") ||
        text.includes("attach") ||
        text.includes("upload");
    }});
  if (attach && visible(attach)) {{
    attach.scrollIntoView({{block: "center"}});
    attach.click();
  }}
  const scope = attach ? (attach.closest("lib-attach-apply-block") || attach.closest("form") || document) : document;
  const input = scope.querySelector('input[type="file"]') ||
    document.querySelector('lib-attach-apply-block input[type="file"]') ||
    document.querySelector('input[type="file"]');
  if (!input) return {{ok: false, reason: "no resume file input found", attachSelectorFound: !!attach}};
  input.setAttribute({json.dumps(ROBOTAU_UPLOAD_MARKER_ATTR)}, "1");
  return {{
    ok: true,
    attachSelectorFound: !!attach,
    accept: input.accept || "",
    multiple: !!input.multiple,
    visibleInput: visible(input)
  }};
}})()
"""
    )
    if not prepare.get("ok"):
        return prepare

    remote = tab.call(
        "Runtime.evaluate",
        {
            "expression": f"document.querySelector('input[type=\"file\"][{ROBOTAU_UPLOAD_MARKER_ATTR}=\"1\"]')",
            "returnByValue": False,
        },
    )
    object_id = remote.get("result", {}).get("result", {}).get("objectId")
    if not object_id:
        return {"ok": False, "reason": "resume file input object was not available", "prepare": prepare}
    set_result = tab.call("DOM.setFileInputFiles", {"objectId": object_id, "files": [str(resume_path)]})
    verify = tab.eval(
        f"""
(() => {{
  const input = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]');
  if (!input) return {{ok: false, reason: "marked resume file input disappeared"}};
  const namesBeforeEvents = Array.from(input.files || []).map(file => file.name);
  input.dispatchEvent(new Event("input", {{bubbles: true}}));
  input.dispatchEvent(new Event("change", {{bubbles: true}}));
  const names = Array.from(input.files || []).map(file => file.name);
  const bodyText = document.body ? document.body.innerText : "";
  const visibleFileName = bodyText.includes({json.dumps(row.approved_resume_name)});
  return {{
    ok: namesBeforeEvents.includes({json.dumps(row.approved_resume_name)}) || names.includes({json.dumps(row.approved_resume_name)}) || visibleFileName,
    fileNamesBeforeEvents: namesBeforeEvents,
    fileNames: names,
    visibleFileName,
    expectedName: {json.dumps(row.approved_resume_name)}
  }};
}})()
"""
    )
    if not verify.get("ok"):
        document = tab.call("DOM.getDocument", {"depth": -1, "pierce": True})
        root_id = document.get("result", {}).get("root", {}).get("nodeId")
        if root_id:
            query = tab.call(
                "DOM.querySelector",
                {"nodeId": root_id, "selector": f'input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]'},
            )
            node_id = query.get("result", {}).get("nodeId")
            if node_id:
                set_result = tab.call("DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(resume_path)]})
                verify = tab.eval(
                    f"""
(() => {{
  const input = document.querySelector('input[type="file"][{ROBOTAU_UPLOAD_MARKER_ATTR}="1"]');
  if (!input) return {{ok: false, reason: "marked resume file input disappeared"}};
  const namesBeforeEvents = Array.from(input.files || []).map(file => file.name);
  input.dispatchEvent(new Event("input", {{bubbles: true}}));
  input.dispatchEvent(new Event("change", {{bubbles: true}}));
  const names = Array.from(input.files || []).map(file => file.name);
  const bodyText = document.body ? document.body.innerText : "";
  const visibleFileName = bodyText.includes({json.dumps(row.approved_resume_name)});
  return {{
    ok: namesBeforeEvents.includes({json.dumps(row.approved_resume_name)}) || names.includes({json.dumps(row.approved_resume_name)}) || visibleFileName,
    fileNamesBeforeEvents: namesBeforeEvents,
    fileNames: names,
    visibleFileName,
    expectedName: {json.dumps(row.approved_resume_name)}
  }};
}})()
"""
                )
    chooser: dict[str, Any] | None = None
    if not verify.get("ok") and hasattr(tab, "ws") and hasattr(tab, "_next_id"):
        try:
            chooser = set_files_via_file_chooser_event(tab, resume_path)
        except Exception as exc:
            chooser = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if chooser.get("ok"):
            set_result = chooser.get("set_result", set_result)
            verify = verify_attached_resume(tab, row)
    result = {"ok": bool(verify.get("ok")), "path": str(resume_path), "prepare": prepare, "set_result": set_result, "verify": verify}
    if chooser is not None:
        result["chooser"] = chooser
    return result


def fill_form(tab: Any, row: RobotauaApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    salary: {json.dumps(row.salary_usd)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)},
    uploadAllowed: {json.dumps(row.upload_allowed)},
    answers: {json.dumps(row.answers)}
  }};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const textOf = e => (e ? (e.innerText || e.textContent || e.value || e.getAttribute("aria-label") || "").trim() : "");
  const usableTextareas = () => Array.from(document.querySelectorAll("textarea")).filter(e =>
    visible(e) && !/search|\\u043f\\u043e\\u0448\\u0443\\u043a|\\u043f\\u043e\\u0438\\u0441\\u043a/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}}`)
  );
  const exactCoverSelector = {json.dumps(ROBOTAU_COVER_LETTER_SELECTOR)};
  const exactCover = document.querySelector(exactCoverSelector);
  let coverSelectorUsed = false;
  const coverControls = Array.from(document.querySelectorAll("button,label,[role=button],[tabindex],div,a,input[type=button]"))
    .filter(visible)
    .filter(e => {{
      const text = textOf(e).toLowerCase();
      return text.includes("\\u0434\\u043e\\u0434\\u0430\\u0442\\u0438 \\u0441\\u0443\\u043f\\u0440\\u043e\\u0432\\u0456\\u0434") ||
        text.includes("\\u0441\\u0443\\u043f\\u0440\\u043e\\u0432\\u0456\\u0434\\u043d") ||
        text.includes("cover letter");
    }});
  if (!usableTextareas().length && exactCover && visible(exactCover)) {{
    exactCover.scrollIntoView({{block: "center"}});
    exactCover.click();
    coverSelectorUsed = true;
  }}
  if (!coverSelectorUsed && !usableTextareas().length && coverControls.length) {{
    coverControls[0].scrollIntoView({{block: "center"}});
    coverControls[0].click();
  }}
  const textareas = usableTextareas();
  const message = textareas[0];
  if (!message) return {{ok: false, reason: "no visible message textarea", coverLetterControls: coverControls.map(textOf).slice(0, 5)}};
  message.value = expected.message;
  message.dispatchEvent(new Event("input", {{bubbles: true}}));
  message.dispatchEvent(new Event("change", {{bubbles: true}}));

  const filledAnswers = [];
  for (const [idx, value] of Object.entries(expected.answers)) {{
    if (!value) continue;
    const extra = textareas[Number(idx)] || document.querySelector(`#answer_text_${{CSS.escape(idx)}}`);
    if (extra && extra !== message) {{
      extra.value = value;
      extra.dispatchEvent(new Event("input", {{bubbles: true}}));
      extra.dispatchEvent(new Event("change", {{bubbles: true}}));
      filledAnswers.push(`answer_${{idx}}`);
    }}
  }}

  const linkedinInputs = Array.from(document.querySelectorAll("input")).filter(e =>
    visible(e) && /linkedin|linked|\\u043b\\u0456\\u043d\\u043a\\u0435\\u0434/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`)
  );
  if (expected.linkedinPolicy === "include_linkedin" && linkedinInputs.length) {{
    linkedinInputs[0].value = expected.linkedin;
    linkedinInputs[0].dispatchEvent(new Event("input", {{bubbles: true}}));
    linkedinInputs[0].dispatchEvent(new Event("change", {{bubbles: true}}));
  }}

  const salaryInputs = Array.from(document.querySelectorAll("input")).filter(e =>
    visible(e) && /salary|\\u0437\\u0430\\u0440\\u043f|\\u0437\\u043f|\\u043e\\u043f\\u043b\\u0430\\u0442/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`)
  );
  if (expected.salary && salaryInputs.length) {{
    salaryInputs[0].value = expected.salary;
    salaryInputs[0].dispatchEvent(new Event("input", {{bubbles: true}}));
    salaryInputs[0].dispatchEvent(new Event("change", {{bubbles: true}}));
  }}

  const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'));
  const selects = Array.from(document.querySelectorAll("select")).filter(visible);
  const selectedResumeText = selects.flatMap(select => Array.from(select.options || [])
    .filter(option => option.selected)
    .map(option => option.text || ""));

  return {{
    ok: true,
    messageLength: message.value.length,
    linkedinFields: linkedinInputs.length,
    linkedin: linkedinInputs[0] ? linkedinInputs[0].value : "",
    salaryFields: salaryInputs.length,
    salary: salaryInputs[0] ? salaryInputs[0].value : "",
    fileInputs: fileInputs.length,
    selectedResumeText,
    filledAnswers,
    coverLetterExpanded: !!coverControls.length || coverSelectorUsed,
    coverLetterSelectorUsed: coverSelectorUsed
  }};
}})()
"""
    result = tab.eval(expression)
    if not result.get("ok"):
        return result
    if row.resume_policy == "upload_exact_resume":
        upload = attach_resume_file(tab, row)
        result["resumeUpload"] = upload
        if not upload.get("ok"):
            return {"ok": False, "reason": upload.get("reason", "resume upload failed"), "baseFill": result}
    return result


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def process_row(
    row: RobotauaApplicationRow,
    endpoint: str,
    mode: Mode,
    log_path: Path,
    delay: float,
    tab_factory: Callable[[str, str], Any] = open_job_tab,
) -> int:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    errors = validate_row(row, mode)
    base_event = {
        "schema": "job.robotaua_submission_attempt.v0",
        "attempted_at": started_at,
        "row_number": row.row_number,
        "site": row.site,
        "source_url": row.url,
        "title": row.title,
        "company": row.company,
        "linkedin_policy": row.linkedin_policy,
        "resume_policy": row.resume_policy,
        "approved_resume_name": row.approved_resume_name,
        "upload_allowed": row.upload_allowed,
        "approved_to_submit": row.approved_to_submit,
        "final_submit_allowed": row.final_submit_allowed,
        "mode": mode,
    }
    if errors:
        append_log(log_path, {**base_event, "result": "blocked_validation", "errors": errors})
        print(f"row {row.row_number}: blocked: {'; '.join(errors)}")
        return 2
    if mode == "dry_run":
        append_log(log_path, {**base_event, "result": "dry_run_ok"})
        print(f"row {row.row_number}: dry-run ok: {row.url}")
        return 0

    tab: Any | None = None
    try:
        tab = tab_factory(endpoint, application_form_url(row.url))
        wait_for_page(tab, delay)
        before = wait_for_interactive_state(tab)
        if before.get("already_applied"):
            append_log(log_path, {**base_event, "result": "already_applied", "before": before})
            print(f"row {row.row_number}: already applied: {row.url}")
            return 0
        if before.get("inactive"):
            append_log(log_path, {**base_event, "result": "blocked_inactive", "before": before})
            print(f"row {row.row_number}: blocked: inactive vacancy")
            return 3
        if before.get("login_required"):
            append_log(log_path, {**base_event, "result": "blocked_login_required", "before": before})
            print(f"row {row.row_number}: blocked: login required")
            return 3

        resume_upload: dict[str, Any] | None = None
        if row.resume_policy == "no_resume" and page_requires_resume_method(before):
            append_log(log_path, {**base_event, "result": "blocked_policy_requires_resume", "before": before})
            print(f"row {row.row_number}: blocked: Robota.ua requires resume/apply method")
            return 3
        if row.resume_policy == "upload_exact_resume":
            resume_upload = attach_resume_file(tab, row)
            time.sleep(1.0)
            if not resume_upload.get("ok"):
                append_log(log_path, {**base_event, "result": "blocked_resume_upload", "before": before, "resume_upload": resume_upload})
                print(f"row {row.row_number}: blocked: {resume_upload.get('reason')}")
                return 4
            cover = open_cover_letter(tab)
            time.sleep(1.0)
            opened = {"ok": True, "opened": False, "reason": "upload method page ready after resume attach", "cover": cover}
        else:
            cover = {}
            opened = open_application_form(tab, row)
            time.sleep(1.0)
            if not opened.get("ok"):
                after_open = inspect_state(tab)
                append_log(
                    log_path,
                    {**base_event, "result": "blocked_no_apply_form", "opened": opened, "cover": cover, "resume_upload": resume_upload, "state": after_open},
                )
                print(f"row {row.row_number}: blocked: {opened.get('reason')}")
                return 3

        filled = fill_form(tab, row)
        if not filled.get("ok"):
            append_log(log_path, {**base_event, "result": "blocked_fill_failed", "opened": opened, "filled": filled})
            print(f"row {row.row_number}: fill failed: {filled.get('reason')}")
            return 4

        presubmit = validate_before_submit(tab, row)
        if not presubmit.get("ok"):
            append_log(log_path, {**base_event, "result": "blocked_presubmit_validation", "opened": opened, "filled": filled, "presubmit": presubmit})
            print(f"row {row.row_number}: blocked before submit: {'; '.join(presubmit.get('errors', []))}")
            return 4
        if mode == "pre_submit":
            append_log(log_path, {**base_event, "result": "pre_submit_ok_no_final_click", "before": before, "opened": opened, "filled": filled, "presubmit": presubmit})
            print(f"row {row.row_number}: pre-submit ok, final click skipped: {row.url}")
            return 0

        submitted = submit_form(tab)
        time.sleep(3.0)
        after = inspect_state(tab)
        success = bool(after.get("already_applied") or re.search(r"відгук|response|sent|applied", str(after.get("body_start", "")), re.I))
        result = "submitted_success" if success else "submit_clicked_unconfirmed"
        append_log(log_path, {**base_event, "result": result, "before": before, "opened": opened, "filled": filled, "presubmit": presubmit, "submitted": submitted, "after": after})
        print(f"row {row.row_number}: {result}: {row.url}")
        return 0 if success else 5
    except Exception as exc:
        append_log(log_path, {**base_event, "result": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"row {row.row_number}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 10
    finally:
        if tab and hasattr(tab, "close"):
            tab.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or submit explicitly approved Robota.ua application rows.")
    parser.add_argument("--csv", type=Path, required=True, help="CSV with approved Robota.ua application rows.")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to process; 0 means all.")
    parser.add_argument("--pre-submit", action="store_true", help="Open/fill/validate the form but skip final click.")
    parser.add_argument("--execute", action="store_true", help="Click Robota.ua final submit for approved rows.")
    parser.add_argument("--i-understand-this-prepares-robotaua-application", action="store_true")
    parser.add_argument("--i-understand-this-submits-robotaua-application", action="store_true")
    args = parser.parse_args(argv)

    if args.pre_submit and args.execute:
        print("Use either --pre-submit or --execute, not both.", file=sys.stderr)
        return 2
    mode: Mode = "submit" if args.execute else ("pre_submit" if args.pre_submit else "dry_run")
    if mode == "pre_submit" and not args.i_understand_this_prepares_robotaua_application:
        print("Refusing pre-submit without --i-understand-this-prepares-robotaua-application.", file=sys.stderr)
        return 2
    if mode == "submit" and not args.i_understand_this_submits_robotaua_application:
        print("Refusing execute without --i-understand-this-submits-robotaua-application.", file=sys.stderr)
        return 2

    rows = read_rows(args.csv)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No rows found.", file=sys.stderr)
        return 1

    failures = 0
    for row in rows:
        rc = process_row(row, args.cdp_endpoint, mode, args.log, args.delay_sec)
        if rc:
            failures += 1
        time.sleep(max(args.delay_sec, 1.0))
    print(f"Done. rows={len(rows)} failures={failures} log={args.log}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
