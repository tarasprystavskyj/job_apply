#!/usr/bin/env python3
r"""
Guarded Work.ua resume edit automation.

Default mode is dry-run validation only. Browser prepare fills exact approved
fields in an already-running Chrome CDP session and stops before saving. Final
save requires both row/config gates and a separate CLI confirmation flag.

This tool does not read .env files, secrets, cookies, saved browser profiles,
or resume file contents. Optional file upload is limited to an exact approved
path/name and is passed to Chrome through CDP without inspecting file bytes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
DEFAULT_LOG = ROOT / "data" / "job_waves" / "workua_resume_update_attempts.jsonl"
DEFAULT_RESUME_DIR = Path(os.environ.get("JOB_APPLY_RESUME_DIR", r"C:\python_scripts\projects_search\my_resumes"))
APPROVED_RESUME_ARTIFACT_DIRS = (ROOT / "data" / "job_waves", ROOT / "data" / "approved_artifacts")

TRUTHY = {"1", "true", "yes", "y", "allowed", "approve", "approved"}
FILE_POLICIES = {"no_upload", "upload_exact_file"}
FIELD_KINDS = {"text", "textarea", "select", "checkbox", "radio"}
RESUME_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf"}


@dataclass(frozen=True)
class FieldUpdate:
    key: str
    value: str
    kind: str = "text"
    selector: str = ""
    name: str = ""
    label: str = ""
    allow_empty: bool = False


@dataclass(frozen=True)
class ResumeUpdateConfig:
    row_number: int
    site: str
    resume_edit_url: str
    field_updates: tuple[FieldUpdate, ...]
    approved_to_prepare: bool
    final_save_allowed: bool
    resume_file_policy: str = "no_upload"
    upload_allowed: bool = False
    approved_resume_name: str = ""
    approved_resume_path: str = ""


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def is_workua_resume_edit_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.netloc.lower() not in {"work.ua", "www.work.ua"}:
        return False
    if parsed.path.rstrip("/") != "/jobseeker/my/resumes/edit":
        return False
    query = urllib.parse.parse_qs(parsed.query)
    values = query.get("id") or []
    return len(values) == 1 and bool(re.fullmatch(r"\d+", values[0]))


def load_field_updates(value: Any) -> tuple[FieldUpdate, ...]:
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("@"):
            value = Path(value[1:]).read_text(encoding="utf-8-sig")
        value = json.loads(value) if value else []
    if isinstance(value, dict):
        value = value.get("field_updates", [])
    if not isinstance(value, list):
        raise ValueError("field_updates must be a list")

    updates: list[FieldUpdate] = []
    for idx, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"field update {idx} must be an object")
        updates.append(
            FieldUpdate(
                key=str(raw.get("key") or raw.get("name") or raw.get("selector") or f"field_{idx}").strip(),
                selector=str(raw.get("selector") or "").strip(),
                name=str(raw.get("name") or "").strip(),
                label=str(raw.get("label") or "").strip(),
                kind=str(raw.get("kind") or "text").strip().lower(),
                value=str(raw.get("value") if raw.get("value") is not None else ""),
                allow_empty=truthy(raw.get("allow_empty")),
            )
        )
    return tuple(updates)


def config_from_mapping(data: dict[str, Any], row_number: int = 1) -> ResumeUpdateConfig:
    return ResumeUpdateConfig(
        row_number=row_number,
        site=str(data.get("site") or "workua").strip().lower(),
        resume_edit_url=str(data.get("resume_edit_url") or data.get("url") or "").strip(),
        field_updates=load_field_updates(data.get("field_updates") or data.get("field_updates_json") or []),
        approved_to_prepare=truthy(data.get("approved_to_prepare")),
        final_save_allowed=truthy(data.get("final_save_allowed")),
        resume_file_policy=str(data.get("resume_file_policy") or "no_upload").strip().lower(),
        upload_allowed=truthy(data.get("upload_allowed")),
        approved_resume_name=str(data.get("approved_resume_name") or "").strip(),
        approved_resume_path=str(data.get("approved_resume_path") or data.get("resume_path") or "").strip(),
    )


def read_json_configs(path: Path) -> list[ResumeUpdateConfig]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("configs"), list):
        items = data["configs"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]
    else:
        raise ValueError("JSON config must be an object, list, or {configs: [...]}")
    return [config_from_mapping(item, row_number=idx) for idx, item in enumerate(items, 1)]


def read_csv_configs(path: Path) -> list[ResumeUpdateConfig]:
    configs: list[ResumeUpdateConfig] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, 2):
            if "field_updates_json" not in row:
                raise ValueError(f"CSV row {row_number}: missing field_updates_json")
            configs.append(config_from_mapping(row, row_number=row_number))
    return configs


def validate_config(config: ResumeUpdateConfig, live_action: bool, execute_save: bool) -> list[str]:
    errors: list[str] = []
    if config.site != "workua":
        errors.append("site must be workua")
    if not is_workua_resume_edit_url(config.resume_edit_url):
        errors.append("resume_edit_url must be an exact Work.ua resume edit URL with id")
    if not config.field_updates and config.resume_file_policy == "no_upload":
        errors.append("at least one field update or approved resume file upload is required")

    seen_keys: set[str] = set()
    for idx, update in enumerate(config.field_updates, 1):
        if not update.key:
            errors.append(f"field update {idx}: key is required")
        if update.key in seen_keys:
            errors.append(f"field update {idx}: duplicate key {update.key!r}")
        seen_keys.add(update.key)
        locators = [bool(update.selector), bool(update.name), bool(update.label)]
        if sum(locators) != 1:
            errors.append(f"field update {idx}: exactly one of selector, name, or label is required")
        if update.kind not in FIELD_KINDS:
            errors.append(f"field update {idx}: kind must be one of: {', '.join(sorted(FIELD_KINDS))}")
        if update.value == "" and not update.allow_empty:
            errors.append(f"field update {idx}: empty value requires allow_empty=true")

    if config.resume_file_policy not in FILE_POLICIES:
        errors.append(f"resume_file_policy must be one of: {', '.join(sorted(FILE_POLICIES))}")
    if config.resume_file_policy == "upload_exact_file":
        if not config.upload_allowed:
            errors.append("resume_file_policy=upload_exact_file requires upload_allowed=true")
        if not config.approved_resume_name:
            errors.append("resume_file_policy=upload_exact_file requires approved_resume_name")
        if not config.approved_resume_path:
            errors.append("resume_file_policy=upload_exact_file requires approved_resume_path")
        elif Path(config.approved_resume_path).name != config.approved_resume_name:
            errors.append("approved_resume_path basename must match approved_resume_name")
        else:
            try:
                resolve_resume_upload_path(config)
            except (FileNotFoundError, ValueError) as exc:
                errors.append(str(exc))
    elif config.upload_allowed:
        errors.append("upload_allowed=true is only valid with resume_file_policy=upload_exact_file")

    if live_action and not config.approved_to_prepare:
        errors.append("approved_to_prepare must be true for browser prepare/save")
    if execute_save and not config.final_save_allowed:
        errors.append("final_save_allowed must be true for final save")
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


def inspect_resume_page(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const body = document.body ? document.body.innerText.toLowerCase() : "";
  return {
    url: location.href,
    title: document.title,
    formCount: document.querySelectorAll("form").length,
    visibleInputs: Array.from(document.querySelectorAll("input,textarea,select")).filter(visible).length,
    fileInputCount: document.querySelectorAll('input[type="file"]').length,
    authRequired: body.includes("log in") || body.includes("sign in") ||
      body.includes("\u0443\u0432\u0456\u0439\u0434\u0456\u0442\u044c") ||
      body.includes("\u0437\u0430\u0440\u0435\u0454\u0441\u0442\u0440"),
    saveControls: Array.from(document.querySelectorAll("button,input[type=submit]"))
      .filter(visible)
      .map(e => (e.innerText || e.value || "").trim())
      .filter(text => /save|update|зберег|онов/i.test(text))
      .slice(0, 10)
  };
})()
"""
    )


def apply_field_updates(tab: CdpTab, config: ResumeUpdateConfig) -> dict[str, Any]:
    updates = [
        {
            "key": update.key,
            "selector": update.selector,
            "name": update.name,
            "label": update.label,
            "kind": update.kind,
            "value": update.value,
        }
        for update in config.field_updates
    ]
    expression = f"""
(() => {{
  const updates = {json.dumps(updates)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const norm = value => (value || "").replace(/\\s+/g, " ").trim();
  const findByLabel = labelText => {{
    const exact = norm(labelText).toLowerCase();
    for (const label of Array.from(document.querySelectorAll("label")).filter(visible)) {{
      const text = norm(label.innerText || label.textContent).toLowerCase();
      if (text !== exact) continue;
      if (label.control) return label.control;
      const nested = label.querySelector("input,textarea,select");
      if (nested) return nested;
      const forId = label.getAttribute("for");
      if (forId) return document.getElementById(forId);
    }}
    return null;
  }};
  const findElement = update => {{
    if (update.selector) return document.querySelector(update.selector);
    if (update.name) return document.querySelector(`[name="${{CSS.escape(update.name)}}"]`);
    if (update.label) return findByLabel(update.label);
    return null;
  }};
  const setValue = (element, update) => {{
    if (!element) return {{ok: false, reason: "field not found"}};
    if (!visible(element)) return {{ok: false, reason: "field is not visible"}};
    element.scrollIntoView({{block: "center"}});
    const tag = element.tagName.toLowerCase();
    if (update.kind === "checkbox" || element.type === "checkbox") {{
      element.checked = /^(1|true|yes|y|checked)$/i.test(update.value);
    }} else if (update.kind === "radio" || element.type === "radio") {{
      const scope = element.form || document;
      const radios = Array.from(scope.querySelectorAll(`input[type="radio"][name="${{CSS.escape(element.name || "")}}"]`));
      const picked = radios.find(r => r.value === update.value) || element;
      picked.checked = true;
      element = picked;
    }} else if (tag === "select" || update.kind === "select") {{
      const option = Array.from(element.options || []).find(o => o.value === update.value || norm(o.text) === norm(update.value));
      if (!option) return {{ok: false, reason: "select option not found"}};
      element.value = option.value;
    }} else {{
      element.value = update.value;
    }}
    element.dispatchEvent(new Event("input", {{bubbles: true}}));
    element.dispatchEvent(new Event("change", {{bubbles: true}}));
    return {{ok: true, tag, type: element.type || "", key: update.key, valueLength: update.value.length}};
  }};
  const results = updates.map(update => setValue(findElement(update), update));
  return {{ok: results.every(result => result.ok), results}};
}})()
"""
    return tab.eval(expression)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolved_resume_roots() -> tuple[Path, ...]:
    roots = [DEFAULT_RESUME_DIR, *APPROVED_RESUME_ARTIFACT_DIRS]
    return tuple(root.expanduser().resolve(strict=False) for root in roots)


def resolve_resume_upload_path(config: ResumeUpdateConfig) -> Path | None:
    if config.resume_file_policy != "upload_exact_file":
        return None
    if Path(config.approved_resume_name).name != config.approved_resume_name:
        raise ValueError("approved_resume_name must be an exact file name, not a path")
    if Path(config.approved_resume_name).suffix.lower() not in RESUME_FILE_EXTENSIONS:
        raise ValueError("approved_resume_name must be a PDF/DOC/DOCX/RTF resume file")
    path = Path(config.approved_resume_path).expanduser()
    candidate = path if path.is_absolute() else ROOT / path
    resolved = candidate.resolve(strict=False)
    if path.name != config.approved_resume_name:
        raise ValueError("approved_resume_path basename must match approved_resume_name")
    if not any(is_relative_to(resolved, root) or resolved == root for root in resolved_resume_roots()):
        raise ValueError("approved resume path must stay inside the resume directory or approved artifact directory")
    if not resolved.is_file():
        raise FileNotFoundError(f"approved resume file does not exist: {resolved}")
    return resolved


def attach_resume_file(tab: CdpTab, config: ResumeUpdateConfig) -> dict[str, Any]:
    resume_path = resolve_resume_upload_path(config)
    if resume_path is None:
        return {"ok": True, "skipped": True}
    marker = "data-job-apply-workua-resume-upload"
    prepare = tab.eval(
        f"""
(() => {{
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const input = Array.from(document.querySelectorAll('input[type="file"]')).find(visible) ||
    document.querySelector('input[type="file"]');
  if (!input) return {{ok: false, reason: "no resume file input found"}};
  input.setAttribute({json.dumps(marker)}, "1");
  return {{ok: true, visibleInput: visible(input), accept: input.accept || "", multiple: !!input.multiple}};
}})()
"""
    )
    if not prepare.get("ok"):
        return prepare
    remote = tab.call(
        "Runtime.evaluate",
        {
            "expression": f"document.querySelector('input[type=\"file\"][{marker}=\"1\"]')",
            "returnByValue": False,
        },
    )
    object_id = remote.get("result", {}).get("result", {}).get("objectId")
    if not object_id:
        return {"ok": False, "reason": "resume file input object was not available", "prepare": prepare}
    tab.call("DOM.setFileInputFiles", {"objectId": object_id, "files": [str(resume_path)]})
    verify = tab.eval(
        f"""
(() => {{
  const input = document.querySelector('input[type="file"][{marker}="1"]');
  if (!input) return {{ok: false, reason: "marked resume file input disappeared"}};
  input.dispatchEvent(new Event("input", {{bubbles: true}}));
  input.dispatchEvent(new Event("change", {{bubbles: true}}));
  const names = Array.from(input.files || []).map(file => file.name);
  return {{ok: names.includes({json.dumps(config.approved_resume_name)}), fileNames: names}};
}})()
"""
    )
    return {"ok": bool(verify.get("ok")), "prepare": prepare, "verify": verify, "approvedResumeName": resume_path.name}


def validate_before_save(tab: CdpTab, config: ResumeUpdateConfig) -> dict[str, Any]:
    updates = [
        {
            "key": update.key,
            "selector": update.selector,
            "name": update.name,
            "label": update.label,
            "kind": update.kind,
            "value": update.value,
        }
        for update in config.field_updates
    ]
    expression = f"""
(() => {{
  const updates = {json.dumps(updates)};
  const expectedResumeName = {json.dumps(config.approved_resume_name)};
  const resumePolicy = {json.dumps(config.resume_file_policy)};
  const visible = e => {{
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  }};
  const norm = value => (value || "").replace(/\\s+/g, " ").trim();
  const findByLabel = labelText => {{
    const exact = norm(labelText).toLowerCase();
    for (const label of Array.from(document.querySelectorAll("label")).filter(visible)) {{
      const text = norm(label.innerText || label.textContent).toLowerCase();
      if (text !== exact) continue;
      if (label.control) return label.control;
      const nested = label.querySelector("input,textarea,select");
      if (nested) return nested;
      const forId = label.getAttribute("for");
      if (forId) return document.getElementById(forId);
    }}
    return null;
  }};
  const findElement = update => {{
    if (update.selector) return document.querySelector(update.selector);
    if (update.name) return document.querySelector(`[name="${{CSS.escape(update.name)}}"]`);
    if (update.label) return findByLabel(update.label);
    return null;
  }};
  const errors = [];
  const details = [];
  for (const update of updates) {{
    const element = findElement(update);
    if (!element) {{
      errors.push(`${{update.key}}: field not found`);
      continue;
    }}
    let actual = element.value || "";
    if (element.type === "checkbox" || update.kind === "checkbox") actual = element.checked ? "true" : "false";
    if (norm(actual) !== norm(update.value)) errors.push(`${{update.key}}: exact approved value not present`);
    details.push({{key: update.key, valueLength: update.value.length}});
  }}
  const fileNames = Array.from(document.querySelectorAll('input[type="file"]'))
    .flatMap(input => Array.from(input.files || []).map(file => file.name));
  if (resumePolicy === "upload_exact_file" && !fileNames.includes(expectedResumeName)) {{
    errors.push("exact approved resume file is not attached");
  }}
  const save = Array.from(document.querySelectorAll("button,input[type=submit]")).filter(visible).find(button => {{
    const text = (button.innerText || button.value || "").toLowerCase();
    return /save|update|зберег|онов/.test(text);
  }});
  if (!save) errors.push("no visible save/update button found");
  if (save && save.disabled) errors.push("save/update button is disabled");
  return {{ok: errors.length === 0, errors, details, fileNames, hasSaveButton: !!save}};
}})()
"""
    return tab.eval(expression)


def click_save(tab: CdpTab) -> dict[str, Any]:
    return tab.eval(
        r"""
(() => {
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  const button = Array.from(document.querySelectorAll("button,input[type=submit]")).filter(visible).find(item => {
    const text = (item.innerText || item.value || "").toLowerCase();
    return /save|update|зберег|онов/.test(text);
  });
  if (!button) return {ok: false, saved: false, reason: "no save/update button"};
  if (button.disabled) return {ok: false, saved: false, reason: "save/update button disabled"};
  button.scrollIntoView({block: "center"});
  button.click();
  return {ok: true, saved: true};
})()
"""
    )


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def base_event(config: ResumeUpdateConfig, live_action: bool, execute_save: bool) -> dict[str, Any]:
    return {
        "schema": "job.workua_resume_update_attempt.v0",
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "row_number": config.row_number,
        "site": config.site,
        "resume_edit_url": config.resume_edit_url,
        "field_count": len(config.field_updates),
        "field_updates": [
            {
                "key": update.key,
                "kind": update.kind,
                "locator": "selector" if update.selector else "name" if update.name else "label",
                "value_length": len(update.value),
                "value_sha256": hashlib.sha256(update.value.encode("utf-8")).hexdigest(),
            }
            for update in config.field_updates
        ],
        "resume_file_policy": config.resume_file_policy,
        "upload_allowed": config.upload_allowed,
        "approved_resume_name_present": bool(config.approved_resume_name),
        "approved_to_prepare": config.approved_to_prepare,
        "final_save_allowed": config.final_save_allowed,
        "browser_prepare": live_action,
        "execute_save": execute_save,
    }


def process_config(
    config: ResumeUpdateConfig,
    endpoint: str,
    live_action: bool,
    execute_save: bool,
    log_path: Path,
    delay: float,
) -> int:
    errors = validate_config(config, live_action=live_action, execute_save=execute_save)
    event = base_event(config, live_action=live_action, execute_save=execute_save)
    if errors:
        append_log(log_path, {**event, "result": "blocked_validation", "errors": errors})
        print(f"row {config.row_number}: blocked: {'; '.join(errors)}")
        return 2
    if not live_action:
        append_log(log_path, {**event, "result": "dry_run_ok"})
        print(f"row {config.row_number}: dry-run ok: {config.resume_edit_url}")
        return 0

    tab: CdpTab | None = None
    try:
        tab = open_tab(endpoint, config.resume_edit_url)
        wait_for_page(tab, delay)
        before = inspect_resume_page(tab)
        if before.get("authRequired"):
            append_log(log_path, {**event, "result": "blocked_auth_required", "before": before})
            print(f"row {config.row_number}: blocked: Work.ua login required")
            return 3

        filled = apply_field_updates(tab, config)
        if not filled.get("ok"):
            append_log(log_path, {**event, "result": "blocked_fill_failed", "before": before, "filled": filled})
            print(f"row {config.row_number}: fill failed")
            return 4

        upload = attach_resume_file(tab, config)
        if not upload.get("ok"):
            append_log(log_path, {**event, "result": "blocked_upload_failed", "before": before, "filled": filled, "upload": upload})
            print(f"row {config.row_number}: upload failed: {upload.get('reason')}")
            return 4

        presave = validate_before_save(tab, config)
        if not presave.get("ok"):
            append_log(log_path, {**event, "result": "blocked_presave_validation", "before": before, "filled": filled, "upload": upload, "presave": presave})
            print(f"row {config.row_number}: blocked before save: {'; '.join(presave.get('errors', []))}")
            return 4

        if not execute_save:
            append_log(log_path, {**event, "result": "prepared_presave_ok", "before": before, "filled": filled, "upload": upload, "presave": presave})
            print(f"row {config.row_number}: prepared and stopped before final save: {config.resume_edit_url}")
            return 0

        saved = click_save(tab)
        time.sleep(max(delay, 2.0))
        after = inspect_resume_page(tab)
        append_log(log_path, {**event, "result": "save_clicked_unconfirmed", "before": before, "filled": filled, "upload": upload, "presave": presave, "saved": saved, "after": after})
        print(f"row {config.row_number}: save_clicked_unconfirmed: {config.resume_edit_url}")
        return 0 if saved.get("ok") else 5
    except Exception as exc:
        append_log(log_path, {**event, "result": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"row {config.row_number}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 10
    finally:
        if tab:
            tab.close()


def load_configs_from_args(args: argparse.Namespace) -> list[ResumeUpdateConfig]:
    sources = [bool(args.json), bool(args.csv), bool(args.resume_edit_url)]
    if sum(sources) != 1:
        raise ValueError("provide exactly one of --json, --csv, or --resume-edit-url")
    if args.json:
        return read_json_configs(args.json)
    if args.csv:
        return read_csv_configs(args.csv)
    updates = load_field_updates(args.field_updates_json or [])
    return [
        ResumeUpdateConfig(
            row_number=1,
            site="workua",
            resume_edit_url=args.resume_edit_url,
            field_updates=updates,
            approved_to_prepare=bool(args.approved_to_prepare),
            final_save_allowed=bool(args.final_save_allowed),
            resume_file_policy=args.resume_file_policy,
            upload_allowed=bool(args.upload_allowed),
            approved_resume_name=args.approved_resume_name or "",
            approved_resume_path=args.approved_resume_path or "",
        )
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded Work.ua resume edit prepare/save automation.")
    parser.add_argument("--json", type=Path, help="JSON config object, list, or {configs: [...]} with exact approved updates.")
    parser.add_argument("--csv", type=Path, help="CSV with field_updates_json and gate columns.")
    parser.add_argument("--resume-edit-url", help="Exact Work.ua resume edit URL, e.g. https://www.work.ua/jobseeker/my/resumes/edit/?id=123")
    parser.add_argument("--field-updates-json", default="", help="JSON list of field updates, or @path-to-json.")
    parser.add_argument("--approved-to-prepare", action="store_true", help="Direct-CLI row gate for browser prepare/save.")
    parser.add_argument("--final-save-allowed", action="store_true", help="Direct-CLI row gate for final save.")
    parser.add_argument("--resume-file-policy", default="no_upload", choices=sorted(FILE_POLICIES))
    parser.add_argument("--upload-allowed", action="store_true")
    parser.add_argument("--approved-resume-name", default="")
    parser.add_argument("--approved-resume-path", default="")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=0, help="Optional max configs to process; 0 means all.")
    parser.add_argument("--prepare-browser", action="store_true", help="Fill exact approved fields in browser and stop before save.")
    parser.add_argument("--execute-save", action="store_true", help="Click Work.ua final save/update after pre-save validation.")
    parser.add_argument("--i-understand-this-prepares-workua-resume-update", action="store_true")
    parser.add_argument("--i-understand-this-saves-workua-resume-update", action="store_true")
    args = parser.parse_args(argv)

    if args.execute_save:
        args.prepare_browser = True
    if args.prepare_browser and not args.i_understand_this_prepares_workua_resume_update:
        print("Refusing browser prepare without --i-understand-this-prepares-workua-resume-update.", file=sys.stderr)
        return 2
    if args.execute_save and not args.i_understand_this_saves_workua_resume_update:
        print("Refusing execute-save without --i-understand-this-saves-workua-resume-update.", file=sys.stderr)
        return 2

    try:
        configs = load_configs_from_args(args)
    except Exception as exc:
        print(f"Config error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        configs = configs[: args.limit]
    if not configs:
        print("No configs found.", file=sys.stderr)
        return 1

    failures = 0
    for config in configs:
        rc = process_config(
            config,
            endpoint=args.cdp_endpoint,
            live_action=bool(args.prepare_browser),
            execute_save=bool(args.execute_save),
            log_path=args.log,
            delay=args.delay_sec,
        )
        if rc:
            failures += 1
        time.sleep(max(args.delay_sec, 1.0) if args.prepare_browser else 0)
    print(f"Done. configs={len(configs)} failures={failures} log={args.log}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
