#!/usr/bin/env python3
r"""
Prepare or submit explicitly approved DOU applications from a CSV file.

This script attaches to an already-running Chrome with remote debugging enabled
and uses only visible DOU vacancy pages/forms. It does not read .env, cookies,
browser profile files, saved passwords, or local CV/resume files.

Default mode is validation-only. Browser form preparation requires:

- CSV row: site=dou, DOU vacancy URL, exact message, resume_policy,
  linkedin_policy, approved_to_submit=true, final_submit_allowed=true
- CLI flag: --prepare --i-understand-this-fills-dou-form

Real submission additionally requires:

- CLI flags: --execute --i-understand-this-sends-dou-applications
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
from typing import Any

import websocket


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "data" / "job_waves" / "dou_submission_attempts.jsonl"
DEFAULT_RESUME_DIR = Path(os.environ.get("JOB_APPLY_RESUME_DIR", r"C:\python_scripts\projects_search\my_resumes"))
APPROVED_RESUME_ARTIFACT_DIRS = (ROOT / "data" / "resumes", ROOT / "data" / "job_waves", ROOT / "data" / "approved_artifacts")
DOU_UPLOAD_MARKER_ATTR = "data-job-apply-dou-resume-upload"

TRUTHY = {"1", "true", "yes", "y", "allowed", "approve", "approved"}
DOU_HOSTS = {"dou.ua", "jobs.dou.ua", "relocate.dou.ua"}
RESUME_POLICIES = {"no_resume", "use_site_profile_resume", "upload_file"}
LINKEDIN_POLICIES = {"omit", "fill_url", "use_site_profile"}
RESUME_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf"}


@dataclass(frozen=True)
class DouApplicationRow:
    row_number: int
    site: str
    url: str
    title: str
    company: str
    message: str
    linkedin: str
    linkedin_policy: str
    resume_policy: str
    approved_resume_name: str
    approved_resume_path: str
    upload_allowed: bool
    approved_to_submit: bool
    final_submit_allowed: bool
    answers: dict[int, str]


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


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def require_columns(row: dict[str, str], row_number: int, columns: list[str]) -> None:
    missing = [name for name in columns if name not in row]
    if missing:
        raise ValueError(f"CSV row {row_number}: missing columns: {', '.join(missing)}")


def read_rows(csv_path: Path) -> list[DouApplicationRow]:
    required = [
        "site",
        "url",
        "message",
        "linkedin_policy",
        "resume_policy",
        "upload_allowed",
        "approved_to_submit",
        "final_submit_allowed",
    ]
    rows: list[DouApplicationRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, 2):
            require_columns(row, row_number, required)
            answers = {idx: (row.get(f"answer_{idx}") or "").strip() for idx in range(1, 9)}
            message_file = (row.get("message_file") or "").strip()
            if message_file:
                raise ValueError(f"CSV row {row_number}: DOU live rows must carry exact message text in the message column")
            rows.append(
                DouApplicationRow(
                    row_number=row_number,
                    site=(row.get("site") or "").strip().lower(),
                    url=(row.get("url") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    company=(row.get("company") or "").strip(),
                    message=(row.get("message") or "").strip(),
                    linkedin=(row.get("linkedin") or "").strip(),
                    linkedin_policy=(row.get("linkedin_policy") or "").strip().lower(),
                    resume_policy=(row.get("resume_policy") or "").strip().lower(),
                    approved_resume_name=(row.get("approved_resume_name") or "").strip(),
                    approved_resume_path=(row.get("approved_resume_path") or row.get("resume_path") or "").strip(),
                    upload_allowed=truthy(row.get("upload_allowed")),
                    approved_to_submit=truthy(row.get("approved_to_submit")),
                    final_submit_allowed=truthy(row.get("final_submit_allowed")),
                    answers=answers,
                )
            )
    return rows


def is_public_dou_vacancy_page(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and host in DOU_HOSTS
        and "/vacancies/" in parsed.path
    )


def is_exact_dou_vacancy_url(url: str) -> bool:
    if not is_public_dou_vacancy_page(url):
        return False
    path = urllib.parse.urlparse(url).path
    return bool(re.search(r"/vacancies/\d+/?$", path))


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


def resolve_resume_upload_path(row: DouApplicationRow, resume_dir: Path | None = None) -> Path | None:
    if row.resume_policy != "upload_file":
        return None
    exact_name = row.approved_resume_name.strip()
    if not exact_name:
        raise ValueError("approved_resume_name is required when resume_policy=upload_file")
    if Path(exact_name).name != exact_name:
        raise ValueError("approved_resume_name must be an exact file name, not a path")
    if Path(exact_name).suffix.lower() not in RESUME_FILE_EXTENSIONS:
        raise ValueError("approved_resume_name must be a PDF/DOC/DOCX/RTF resume file")
    if not row.approved_resume_path:
        raise ValueError("approved_resume_path is required when resume_policy=upload_file")

    roots = resolved_resume_roots(resume_dir)
    raw = Path(row.approved_resume_path).expanduser()
    candidate = raw if raw.is_absolute() else ROOT / raw
    if candidate.name != exact_name:
        raise ValueError("approved_resume_path basename must match approved_resume_name exactly")
    resolved = candidate.resolve(strict=False)
    if not any(is_relative_to(resolved, root) or resolved == root for root in roots):
        raise ValueError("approved resume path must stay inside the resume directory or approved artifact directory")
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"approved resume file was not found: {resolved}")
    return resolved


def validate_row(row: DouApplicationRow) -> list[str]:
    errors: list[str] = []
    if row.site != "dou":
        errors.append("site must be dou")
    if not is_exact_dou_vacancy_url(row.url):
        errors.append("url must be an exact https DOU vacancy URL ending in /vacancies/<id>/")
    if not row.message:
        errors.append("exact message is required in message")
    elif len(row.message) < 40:
        errors.append("message looks too short for an approved application")
    if row.resume_policy not in RESUME_POLICIES:
        errors.append("resume_policy must be no_resume, use_site_profile_resume, or upload_file")
    if row.linkedin_policy not in LINKEDIN_POLICIES:
        errors.append("linkedin_policy must be omit, fill_url, or use_site_profile")
    if row.linkedin_policy == "fill_url":
        if not row.linkedin:
            errors.append("linkedin is required when linkedin_policy=fill_url")
        elif not row.linkedin.startswith("https://www.linkedin.com/in/"):
            errors.append("linkedin must be a public LinkedIn profile URL")
    if row.linkedin_policy != "fill_url" and row.linkedin:
        errors.append("linkedin must be empty unless linkedin_policy=fill_url")
    if row.resume_policy == "upload_file":
        if not row.upload_allowed:
            errors.append("upload_allowed=true is required when resume_policy=upload_file")
        if not row.approved_resume_name:
            errors.append("approved_resume_name is required when resume_policy=upload_file")
        if row.upload_allowed and row.approved_resume_name:
            try:
                resolve_resume_upload_path(row)
            except ValueError as exc:
                errors.append(str(exc))
    elif row.upload_allowed:
        errors.append("upload_allowed must be false unless resume_policy=upload_file")
    if row.resume_policy != "upload_file" and row.approved_resume_name:
        errors.append("approved_resume_name must be empty unless resume_policy=upload_file")
    if row.resume_policy != "upload_file" and row.approved_resume_path:
        errors.append("approved_resume_path must be empty unless resume_policy=upload_file")
    if not row.approved_to_submit:
        errors.append("approved_to_submit must be true")
    if not row.final_submit_allowed:
        errors.append("final_submit_allowed must be true")
    return errors


def cdp_json(endpoint: str, path: str, method: str = "GET") -> Any:
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def normalized_url_without_fragment(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(fragment="", query=parsed.query)).rstrip("/")


def open_tab(endpoint: str, url: str) -> CdpTab:
    target = normalized_url_without_fragment(url)
    for page in cdp_json(endpoint, "/json/list"):
        if page.get("type") != "page" or not page.get("webSocketDebuggerUrl"):
            continue
        if normalized_url_without_fragment(str(page.get("url") or "")) == target:
            return CdpTab(page["webSocketDebuggerUrl"])
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
  const textOf = e => (e.innerText || e.value || e.getAttribute("aria-label") || e.getAttribute("title") || "").trim();
  const body = document.body ? document.body.innerText : "";
  const controls = Array.from(document.querySelectorAll("button,a,input[type=button],input[type=submit]"))
    .filter(visible)
    .map(e => ({tag: e.tagName, text: textOf(e), href: e.href || "", id: e.id || "", className: String(e.className || "")}))
    .filter(e => /\u0432\u0456\u0434\u0433\u0443\u043a\u043d|\u0432\u0456\u0434\u043f\u0440\u0430\u0432|\u043d\u0430\u0434\u0456\u0441\u043b|\u0440\u0435\u0437\u044e\u043c\u0435|apply|send cv/i.test(`${e.text} ${e.href} ${e.id} ${e.className}`))
    .slice(0, 20);
  const forms = Array.from(document.querySelectorAll("form,.modal,.popup,.b-popup,.application-form"))
    .filter(visible)
    .map(e => ({
      tag: e.tagName,
      id: e.id || "",
      className: String(e.className || ""),
      text: (e.innerText || "").slice(0, 300),
      textareas: e.querySelectorAll("textarea").length,
      fileInputs: e.querySelectorAll("input[type=file]").length,
      submitButtons: e.querySelectorAll("button,input[type=submit]").length
    }))
    .filter(e => e.textareas || e.fileInputs || /\u0432\u0456\u0434\u0433\u0443\u043a\u043d|\u0432\u0456\u0434\u043f\u0440\u0430\u0432|\u0440\u0435\u0437\u044e\u043c\u0435|cover|message|apply/i.test(`${e.id} ${e.className} ${e.text}`))
    .slice(0, 10);
  return {
    url: location.href,
    title: document.title,
    body_start: body.slice(0, 1200),
    already_applied: location.href.includes("?sent") ||
      location.href.includes("sent#replied-id") ||
      forms.some(e => String(e.className || "").toLowerCase().includes("sent")) ||
      controls.some(e =>
        /\u0432\u0438 \u0432\u0456\u0434\u0433\u0443\u043a\u043d\u0443\u043b\u0438\u0441|\u0432\u0456\u0434\u0433\u0443\u043a \u043d\u0430\u0434\u0456\u0441\u043b|application sent|already applied/i.test(e.text) ||
        /\bsent\b|(^|\s)replied(\s|$)/i.test(e.className)
      ),
    has_application_surface: forms.length > 0,
    application_surfaces: forms,
    applyish_controls: controls,
    alerts: Array.from(document.querySelectorAll(".error,.errors,.alert,.invalid-feedback,.text-danger"))
      .map(e => e.innerText).filter(Boolean).slice(0, 20)
  };
})()
"""
    )


def open_application_surface(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const surfaces = Array.from(document.querySelectorAll("form,.modal,.popup,.b-popup,.application-form"))
    .filter(visible)
    .filter(e => e.querySelector("textarea") || e.querySelector("input[type=file]"));
  if (surfaces.length) return {ok: true, opened: false, reason: "application surface already visible"};
  const controls = Array.from(document.querySelectorAll("button,a,input[type=button]")).filter(visible);
  const button = controls.find(e => {
    const text = (e.innerText || e.value || e.getAttribute("aria-label") || e.getAttribute("title") || "").trim();
    return /\u0432\u0456\u0434\u0433\u0443\u043a\u043d|\u0432\u0456\u0434\u043f\u0440\u0430\u0432|\u043d\u0430\u0434\u0456\u0441\u043b\u0430\u0442\u0438 \u0440\u0435\u0437\u044e\u043c\u0435|send cv|apply/i.test(`${text} ${e.id || ""} ${e.className || ""}`);
  });
  if (!button) return {ok: false, reason: "no visible non-submit application opener"};
  const href = button.href || "";
  const className = String(button.className || "");
  if (/replied-external/i.test(className) || /\/goto\/vacancy/i.test(href)) {
    return {ok: false, reason: "external application link", external_url: href, className, text: (button.innerText || button.value || "").trim()};
  }
  button.scrollIntoView({block: "center"});
  button.click();
  return {ok: true, opened: true, text: (button.innerText || button.value || "").trim()};
})()
"""
    )


def attach_resume_file(tab: CdpTab, row: DouApplicationRow) -> dict[str, Any]:
    resume_path = resolve_resume_upload_path(row)
    if resume_path is None:
        return {"ok": True, "skipped": True}
    if not hasattr(tab, "call"):
        return {"ok": False, "reason": "CDP tab.call is required for file attachment"}

    prepare = tab.eval(
        f"""
(() => {{
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const input = document.querySelector("#reply_file") || document.querySelector('input[type="file"][name*="reply"]') ||
    document.querySelector('input[type="file"]');
  if (!input) return {{ok: false, reason: "no DOU resume file input found"}};
  input.setAttribute({json.dumps(DOU_UPLOAD_MARKER_ATTR)}, "1");
  input.scrollIntoView({{block: "center"}});
  return {{
    ok: true,
    selector: input.id === "reply_file" ? "#reply_file" : "fallback-file-input",
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
            "expression": f"document.querySelector('input[type=\"file\"][{DOU_UPLOAD_MARKER_ATTR}=\"1\"]')",
            "returnByValue": False,
        },
    )
    object_id = remote.get("result", {}).get("result", {}).get("objectId")
    if not object_id:
        return {"ok": False, "reason": "DOU resume file input object was not available", "prepare": prepare}
    set_result = tab.call("DOM.setFileInputFiles", {"objectId": object_id, "files": [str(resume_path)]})
    verify = verify_attached_resume(tab, row)
    if not verify.get("ok"):
        document = tab.call("DOM.getDocument", {"depth": -1, "pierce": True})
        root_id = document.get("result", {}).get("root", {}).get("nodeId")
        if root_id:
            query = tab.call(
                "DOM.querySelector",
                {"nodeId": root_id, "selector": f'input[type="file"][{DOU_UPLOAD_MARKER_ATTR}="1"]'},
            )
            node_id = query.get("result", {}).get("nodeId")
            if node_id:
                set_result = tab.call("DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(resume_path)]})
                verify = verify_attached_resume(tab, row)
    return {"ok": bool(verify.get("ok")), "path": str(resume_path), "prepare": prepare, "set_result": set_result, "verify": verify}


def verify_attached_resume(tab: CdpTab, row: DouApplicationRow) -> dict[str, Any]:
    return tab.eval(
        f"""
(() => {{
  const input = document.querySelector('input[type="file"][{DOU_UPLOAD_MARKER_ATTR}="1"]') ||
    document.querySelector("#reply_file");
  if (!input) return {{ok: false, reason: "marked DOU resume file input disappeared"}};
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


def fill_form(tab: CdpTab, row: DouApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)},
    answers: {json.dumps(row.answers)}
  }};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const surfaces = Array.from(document.querySelectorAll("form,.modal,.popup,.b-popup,.application-form"))
    .filter(visible)
    .filter(e => e.querySelector("textarea") || e.querySelector("input[type=file]"));
  const root = surfaces[0];
  if (!root) return {{ok: false, reason: "no application surface"}};

  const textareas = Array.from(root.querySelectorAll("textarea")).filter(visible);
  const messageField = textareas.find(e => /message|letter|cover|about|comment/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}}`)) || textareas[0];
  if (!messageField) return {{ok: false, reason: "no visible message textarea"}};
  messageField.value = expected.message;
  messageField.dispatchEvent(new Event("input", {{bubbles: true}}));
  messageField.dispatchEvent(new Event("change", {{bubbles: true}}));

  let linkedinFilled = null;
  if (expected.linkedinPolicy === "fill_url") {{
    const fields = Array.from(root.querySelectorAll("input,textarea")).filter(visible);
    const linkedinField = fields.find(e => /linkedin|profile|social|url|contact|contacts/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`));
    if (linkedinField) {{
      linkedinField.value = expected.linkedin;
      linkedinField.dispatchEvent(new Event("input", {{bubbles: true}}));
      linkedinField.dispatchEvent(new Event("change", {{bubbles: true}}));
      linkedinFilled = {{name: linkedinField.name || "", id: linkedinField.id || "", value: linkedinField.value}};
    }} else {{
      linkedinFilled = {{skipped: true, reason: "no visible optional LinkedIn/profile field"}};
    }}
  }}

  const filledAnswers = [];
  for (const [idx, value] of Object.entries(expected.answers)) {{
    if (!value) continue;
    const candidates = Array.from(root.querySelectorAll(`#answer_text_${{CSS.escape(idx)}}, textarea[name*="${{idx}}"], input[name*="${{idx}}"]`)).filter(visible);
    const answer = candidates[0];
    if (answer) {{
      answer.value = value;
      answer.dispatchEvent(new Event("input", {{bubbles: true}}));
      answer.dispatchEvent(new Event("change", {{bubbles: true}}));
      filledAnswers.push(`answer_${{idx}}`);
    }}
  }}

  const fileInputs = Array.from(root.querySelectorAll("input[type=file]")).filter(visible);
  const fileInputsWithFiles = fileInputs.filter(e => e.files && e.files.length > 0).length;
  const submit = Array.from(root.querySelectorAll("button,input[type=submit]")).filter(visible)
    .find(e => /\u0432\u0456\u0434\u043f\u0440\u0430\u0432|\u043d\u0430\u0434\u0456\u0441\u043b|submit|send|apply/i.test(`${{e.innerText || e.value || ""}} ${{e.name || ""}} ${{e.id || ""}} ${{e.className || ""}}`));
  return {{
    ok: true,
    rootTag: root.tagName,
    rootId: root.id || "",
    messageLength: messageField.value.length,
    linkedinFilled,
    fileInputs: fileInputs.map(e => ({{name: e.name || "", id: e.id || ""}})),
    fileInputsWithFiles,
    filledAnswers,
    submitText: submit ? (submit.innerText || submit.value || "").trim() : null,
    submitDisabled: submit ? !!submit.disabled : null
  }};
}})()
"""
    return tab.eval(expression)


def validate_before_submit(tab: CdpTab, row: DouApplicationRow) -> dict[str, Any]:
    expression = f"""
(() => {{
  const expected = {{
    message: {json.dumps(row.message)},
    linkedin: {json.dumps(row.linkedin)},
    linkedinPolicy: {json.dumps(row.linkedin_policy)},
    resumePolicy: {json.dumps(row.resume_policy)},
    approvedResumeName: {json.dumps(row.approved_resume_name)},
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
  const surfaces = Array.from(document.querySelectorAll("form,.modal,.popup,.b-popup,.application-form"))
    .filter(visible)
    .filter(e => e.querySelector("textarea") || e.querySelector("input[type=file]"));
  const root = surfaces[0];
  if (!root) return {{ok: false, errors: ["no application surface"], details}};

  const textareas = Array.from(root.querySelectorAll("textarea")).filter(visible);
  const messageField = textareas.find(e => /message|letter|cover|about|comment/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}}`)) || textareas[0];
  details.messageLength = messageField ? messageField.value.length : null;
  if (!messageField || norm(messageField.value) !== norm(expected.message)) {{
    errors.push("message was not transmitted to form");
  }}

  if (expected.linkedinPolicy === "fill_url") {{
    const linkedinFields = Array.from(root.querySelectorAll("input,textarea")).filter(visible)
      .filter(e => /linkedin|profile|social|url|contact|contacts/i.test(`${{e.name}} ${{e.id}} ${{e.placeholder}} ${{e.getAttribute("aria-label") || ""}}`));
    details.linkedinFields = linkedinFields.map(e => ({{name: e.name || "", id: e.id || "", value: e.value || ""}}));
    if (!linkedinFields.length) {{
      details.linkedinOptionalSkipped = true;
    }} else if (!linkedinFields.some(e => norm(e.value) === norm(expected.linkedin))) {{
      errors.push("linkedin was not transmitted to form");
    }}
  }}

  const fileInputs = Array.from(root.querySelectorAll("input[type=file]")).filter(visible);
  const fileNames = fileInputs.flatMap(e => Array.from(e.files || []).map(file => file.name));
  details.visibleFileInputs = fileInputs.length;
  details.fileNames = fileNames;
  details.hasReplyFileInput = fileInputs.some(e => e.id === "reply_file" || /reply|cv|resume|file/i.test(`${{e.name}} ${{e.id}}`));
  if (expected.resumePolicy !== "upload_file") {{
    if (fileInputs.some(e => e.value)) errors.push("resume file input has a value but resume upload is not approved");
    if (details.hasReplyFileInput && fileNames.length === 0) errors.push("DOU form requires a resume file but row resume_policy is not upload_file");
  }}
  if (expected.resumePolicy === "upload_file") {{
    const matched = fileNames.includes(expected.approvedResumeName);
    details.approvedResumeMatched = matched;
    if (!matched) errors.push("approved resume file was not attached to DOU form");
  }}

  const requiredEmpty = Array.from(root.querySelectorAll("input,textarea,select")).filter(visible)
    .filter(e => e.required || e.hasAttribute("required") || e.getAttribute("aria-required") === "true")
    .filter(e => e.type !== "file")
    .filter(e => !norm(e.value))
    .map(e => ({{tag: e.tagName, type: e.type || "", name: e.name || "", id: e.id || "", placeholder: e.placeholder || ""}}));
  details.requiredEmpty = requiredEmpty;
  if (requiredEmpty.length) errors.push("visible required fields are empty");

  const submit = Array.from(root.querySelectorAll("button,input[type=submit]")).filter(visible)
    .find(e => /\u0432\u0456\u0434\u043f\u0440\u0430\u0432|\u043d\u0430\u0434\u0456\u0441\u043b|submit|send|apply/i.test(`${{e.innerText || e.value || ""}} ${{e.name || ""}} ${{e.id || ""}} ${{e.className || ""}}`));
  details.submitText = submit ? (submit.innerText || submit.value || "").trim() : null;
  details.submitDisabled = submit ? !!submit.disabled : null;
  if (!submit) errors.push("no visible final submit button");
  if (submit && submit.disabled) errors.push("final submit button disabled");

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
  const surfaces = Array.from(document.querySelectorAll("form,.modal,.popup,.b-popup,.application-form"))
    .filter(visible)
    .filter(e => e.querySelector("textarea") || e.querySelector("input[type=file]"));
  const root = surfaces[0];
  if (!root) return {ok: false, submitted: false, reason: "no application surface"};
  const submit = Array.from(root.querySelectorAll("button,input[type=submit]")).filter(visible)
    .find(e => /\u0432\u0456\u0434\u043f\u0440\u0430\u0432|\u043d\u0430\u0434\u0456\u0441\u043b|submit|send|apply/i.test(`${e.innerText || e.value || ""} ${e.name || ""} ${e.id || ""} ${e.className || ""}`));
  if (!submit) return {ok: false, submitted: false, reason: "no visible final submit button"};
  if (submit.disabled) return {ok: false, submitted: false, reason: "final submit button disabled"};
  submit.scrollIntoView({block: "center"});
  submit.click();
  return {ok: true, submitted: true, text: (submit.innerText || submit.value || "").trim()};
})()
"""
    )


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def base_event_for(row: DouApplicationRow, mode: str) -> dict[str, Any]:
    return {
        "schema": "job.dou_submission_attempt.v0",
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": mode,
        "row_number": row.row_number,
        "site": row.site,
        "source_url": row.url,
        "title": row.title,
        "company": row.company,
        "message_length": len(row.message),
        "linkedin_policy": row.linkedin_policy,
        "resume_policy": row.resume_policy,
        "approved_resume_name": row.approved_resume_name,
        "upload_allowed": row.upload_allowed,
        "approved_to_submit": row.approved_to_submit,
        "final_submit_allowed": row.final_submit_allowed,
    }


def process_row(row: DouApplicationRow, endpoint: str, mode: str, log_path: Path, delay: float) -> int:
    errors = validate_row(row)
    base_event = base_event_for(row, mode)
    if errors:
        append_log(log_path, {**base_event, "result": "blocked_validation", "errors": errors})
        print(f"row {row.row_number}: blocked: {'; '.join(errors)}")
        return 2
    if mode == "dry-run":
        append_log(log_path, {**base_event, "result": "dry_run_ok"})
        print(f"row {row.row_number}: dry-run ok: {row.url}")
        return 0

    tab: CdpTab | None = None
    try:
        tab = open_tab(endpoint, row.url)
        wait_for_page(tab, delay)
        before = inspect_state(tab)
        if before.get("already_applied"):
            append_log(log_path, {**base_event, "result": "already_applied", "before": before})
            print(f"row {row.row_number}: already applied: {row.url}")
            return 0
        opened = open_application_surface(tab)
        time.sleep(1.0)
        if not opened.get("ok"):
            state = inspect_state(tab)
            result = "blocked_external_application_link" if opened.get("reason") == "external application link" else "blocked_no_application_surface"
            append_log(log_path, {**base_event, "result": result, "before": before, "opened": opened, "state": state})
            print(f"row {row.row_number}: blocked: {opened.get('reason')}")
            return 3

        filled = fill_form(tab, row)
        if not filled.get("ok"):
            append_log(log_path, {**base_event, "result": "blocked_fill_failed", "before": before, "opened": opened, "filled": filled})
            print(f"row {row.row_number}: fill failed: {filled.get('reason')}")
            return 4

        resume_upload: dict[str, Any] | None = None
        if row.resume_policy == "upload_file":
            resume_upload = attach_resume_file(tab, row)
            time.sleep(1.0)
            if not resume_upload.get("ok"):
                append_log(log_path, {**base_event, "result": "blocked_resume_upload", "before": before, "opened": opened, "filled": filled, "resume_upload": resume_upload})
                print(f"row {row.row_number}: blocked: {resume_upload.get('reason', 'resume upload failed')}")
                return 4

        presubmit = validate_before_submit(tab, row)
        if not presubmit.get("ok"):
            append_log(log_path, {**base_event, "result": "blocked_presubmit_validation", "before": before, "opened": opened, "filled": filled, "resume_upload": resume_upload, "presubmit": presubmit})
            print(f"row {row.row_number}: blocked before submit: {'; '.join(presubmit.get('errors', []))}")
            return 4

        if mode == "prepare":
            append_log(log_path, {**base_event, "result": "pre_submit_validation_ok", "before": before, "opened": opened, "filled": filled, "resume_upload": resume_upload, "presubmit": presubmit})
            print(f"row {row.row_number}: pre-submit validation ok; final submit not clicked: {row.url}")
            return 0

        submitted = submit_form(tab)
        time.sleep(3.0)
        after = inspect_state(tab)
        after_url = str(after.get("url") or "").lower()
        after_surfaces = after.get("application_surfaces") if isinstance(after.get("application_surfaces"), list) else []
        sent_surface = any("sent" in str(surface.get("className") or "").lower() for surface in after_surfaces if isinstance(surface, dict))
        result = "submit_clicked_unconfirmed"
        body = str(after.get("body_start") or "").lower()
        if (
            "?sent" in after_url
            or "sent#replied-id" in after_url
            or sent_surface
            or any(token in body for token in ["\u0434\u044f\u043a\u0443", "\u0432\u0456\u0434\u0433\u0443\u043a \u043d\u0430\u0434\u0456\u0441\u043b", "\u0432\u0438 \u0432\u0456\u0434\u0433\u0443\u043a\u043d\u0443\u043b\u0438\u0441", "application sent", "thank you"])
        ):
            result = "submitted_success"
        append_log(log_path, {**base_event, "result": result, "before": before, "opened": opened, "filled": filled, "resume_upload": resume_upload, "presubmit": presubmit, "submitted": submitted, "after": after})
        print(f"row {row.row_number}: {result}: {row.url}")
        return 0 if result == "submitted_success" else 5
    except Exception as exc:
        append_log(log_path, {**base_event, "result": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"row {row.row_number}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 10
    finally:
        if tab:
            tab.close()


def public_smoke(url: str, endpoint: str, log_path: Path, delay: float) -> int:
    if not is_public_dou_vacancy_page(url):
        print("public smoke URL must be an https DOU vacancy/listing URL", file=sys.stderr)
        return 2
    event = {
        "schema": "job.dou_submission_attempt.v0",
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "public-smoke",
        "source_url": url,
        "site": "dou",
    }
    tab: CdpTab | None = None
    try:
        tab = open_tab(endpoint, url)
        wait_for_page(tab, delay)
        state = inspect_state(tab)
        append_log(log_path, {**event, "result": "public_smoke_ok", "state": state})
        print(f"public smoke ok: controls={len(state.get('applyish_controls') or [])} surfaces={len(state.get('application_surfaces') or [])} log={log_path}")
        return 0
    except Exception as exc:
        append_log(log_path, {**event, "result": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"public smoke error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 10
    finally:
        if tab:
            tab.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or submit explicitly approved DOU applications from CSV.")
    parser.add_argument("--csv", type=Path, help="CSV with approved DOU application rows.")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to process; 0 means all.")
    parser.add_argument("--prepare", action="store_true", help="Fill and validate the DOU form, stopping before final submit.")
    parser.add_argument("--execute", action="store_true", help="Actually click final DOU submit buttons for approved rows.")
    parser.add_argument("--public-smoke-url", help="Open one public DOU vacancy and inspect apply surfaces without typing or submitting.")
    parser.add_argument("--i-understand-this-fills-dou-form", action="store_true")
    parser.add_argument("--i-understand-this-sends-dou-applications", action="store_true")
    args = parser.parse_args(argv)

    if args.public_smoke_url:
        if args.csv or args.prepare or args.execute:
            print("--public-smoke-url cannot be combined with CSV prepare/execute modes.", file=sys.stderr)
            return 2
        return public_smoke(args.public_smoke_url, args.cdp_endpoint, args.log, args.delay_sec)

    if args.prepare and args.execute:
        print("Use either --prepare or --execute, not both.", file=sys.stderr)
        return 2
    if not args.csv:
        print("--csv is required unless --public-smoke-url is used.", file=sys.stderr)
        return 2
    if args.prepare and not args.i_understand_this_fills_dou_form:
        print("Refusing prepare without --i-understand-this-fills-dou-form.", file=sys.stderr)
        return 2
    if args.execute and not args.i_understand_this_sends_dou_applications:
        print("Refusing execute without --i-understand-this-sends-dou-applications.", file=sys.stderr)
        return 2

    mode = "execute" if args.execute else "prepare" if args.prepare else "dry-run"
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
        time.sleep(max(args.delay_sec, 1.0) if mode != "dry-run" else 0)
    print(f"Done. mode={mode} rows={len(rows)} failures={failures} log={args.log}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
