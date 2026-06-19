from __future__ import annotations

import csv
import html
import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from blocker_loop import refresh_unresolved_blockers
from djinni_inbox_scan import scan_inbox
from dou_pipeline import DEFAULT_OBSERVATIONS_PATH as DOU_OBSERVATIONS_PATH
from dou_pipeline import run_once as run_dou_once
from function_graph import function_graph
from job_apply_config import ROOT, settings
from job_scan_sources import SUMMARY_PATH as ALL_SOURCES_SUMMARY_PATH
from job_scan_sources import format_scan_summary, run_all_sources_scan
from job_platforms.dou import is_dou_listing_url
from job_platforms.progress import build_progress_snapshot
from recruiter_response_scan import is_djinni_thread_url, message_digest, normalize_thread_url
from resume_index import build_resume_index
from vacancy_pipeline import OBSERVATION_PATHS, build_candidate_batch, candidate_summary, latest_submission_by_url, terminal_submission_state


STATE_PATH = ROOT / "data" / "job_waves" / "web_state.json"
FUNCTION_GRAPH_NOTES_PATH = ROOT / "data" / "job_waves" / "function_graph_notes.json"
WEB_RUNS_DIR = ROOT / "data" / "job_waves" / "web_runs"
SUBMISSION_LOGS = {
    "djinni": ROOT / "data" / "job_waves" / "djinni_csv_submission_attempts.jsonl",
    "workua": ROOT / "data" / "job_waves" / "workua_submission_attempts.jsonl",
    "dou": ROOT / "data" / "job_waves" / "dou_submission_attempts.jsonl",
    "robotaua": ROOT / "data" / "job_waves" / "robotaua_submission_attempts.jsonl",
}
SITE_LABELS = {
    "djinni": "Djinni",
    "workua": "Work.ua",
    "dou": "DOU",
    "robotaua": "Robota.ua",
}
DEFAULT_DOU_RESUME_PATH = ROOT / "data" / "resumes" / "Taras_Prystavskyj_AI_Automation_Resume_2026-06-11.pdf"
DEFAULT_ROBOTAUA_RESUME_PATH = DEFAULT_DOU_RESUME_PATH
ACTIONABLE_SITE_NAMES = set(SITE_LABELS)
REVIEW_FLOW_NAMES = {"djinniinbox"}
EXTRA_LIVE_COLUMNS = [
    "linkedin_policy",
    "message_policy",
    "profile_resume_only_allowed",
    "upload_allowed",
    "approved_resume_path",
    *[f"answer_{idx}" for idx in range(1, 9)],
]
BUTTON_HINTS = {
    "en": {
        "scan": "Scan fresh vacancies from configured sources, refresh Djinni inbox offers, rebuild the candidate batch, and do not send applications.",
        "scan_inbox": "Refresh only Djinni inbox offers and recruiter threads; no applications or replies are sent.",
        "profile_on": "Open Djinni profile controls and try to enable the candidate profile. This changes account visibility when the site allows it.",
        "bot_note": "Write the latest batch/status summary for the Telegram bot notification flow; it does not submit applications.",
        "progress": "Open the platform progress dashboard with agent branch/readiness status.",
        "function_graph": "Open the editable local function map for requirements notes, comments, and subtasks.",
        "sent": "Open the table of vacancies where an application was sent successfully or confirmed by post-submit evidence.",
        "save_selected": "Save only the checked rows as approved and clear approval from unchecked or blocked rows.",
        "approve_all": "Approve every currently supported application row and every review/reply row visible in this batch.",
        "clear_all": "Clear all row approvals in the current batch.",
        "send": "Submit applications for the checked/approved rows only. Site adapters still enforce their CSV approval gates and write logs.",
    },
    "uk": {
        "scan": "Знайти свіжі вакансії з налаштованих джерел, оновити Djinni inbox, зібрати новий список кандидатів і нічого не відправляти.",
        "scan_inbox": "Оновити лише пропозиції та переписки в Djinni inbox; заявки і відповіді не надсилаються.",
        "profile_on": "Відкрити керування профілем Djinni і спробувати увімкнути профіль кандидата. Це змінює видимість акаунта, якщо сайт дозволяє.",
        "bot_note": "Записати поточний статус і список для Telegram bot flow; заявки не надсилаються.",
        "progress": "Відкрити сторінку прогресу платформ і готовності агентів.",
        "sent": "Відкрити таблицю вакансій, куди заявка була успішно надіслана або підтверджена після submit.",
        "save_selected": "Зберегти лише відмічені рядки як підтверджені і зняти підтвердження з інших або заблокованих рядків.",
        "approve_all": "Підтвердити всі поточно підтримані рядки заявок і всі review/reply рядки в цьому списку.",
        "clear_all": "Зняти всі підтвердження у поточному списку.",
        "send": "Подати заявки лише на вибрані/підтверджені рядки. Адаптери сайтів все одно перевіряють approval gates у CSV і пишуть логи.",
    },
}
TEXT = {
    "en": {
        "app_title": "Job Apply Automation",
        "language": "Language",
        "agent_model": "Agent model",
        "locations": "locations",
        "latest_batch": "latest batch",
        "scan": "Scan all sources + build batch",
        "scan_inbox": "Refresh only Djinni inbox",
        "profile_on": "Enable Djinni profile",
        "bot_note": "Prepare Telegram bot status",
        "progress": "Platform progress graph",
        "function_graph": "Function map",
        "sent_page": "Sent applications",
        "status": "Status",
        "site_counts": "Site counts",
        "last_run_jobs": "Last run jobs",
        "save_selected": "Save selected",
        "approve_all": "Approve all supported + review rows",
        "clear_all": "Clear all",
        "send": "Submit selected applications",
        "ok": "OK",
        "company": "Company",
        "title": "Title",
        "url": "URL",
        "site": "Site",
        "supported": "Supported",
        "blocker": "Blocker",
        "recommendation": "Recommendation",
        "score": "Score",
        "approved": "Approved",
        "yes": "yes",
        "no": "no",
        "blocked": "blocked",
        "no_batch": "No batch yet.",
        "submission_log": "Submission Log Tail",
        "time": "Time",
        "result": "Result",
        "blocked_reason": "Blocked reason",
        "no_log": "No submission log yet.",
        "sent_title": "Sent Applications",
        "sent_empty": "No sent applications found yet.",
        "back": "Back",
    },
    "uk": {
        "app_title": "Автоматизація подачі заявок",
        "language": "Мова",
        "agent_model": "Модель агента",
        "locations": "локації",
        "latest_batch": "останній список",
        "scan": "Підібрати свіжі вакансії + Djinni inbox",
        "scan_inbox": "Оновити тільки Djinni inbox",
        "profile_on": "Увімкнути профіль Djinni",
        "bot_note": "Підготувати статус Telegram bot",
        "progress": "Граф прогресу платформ",
        "sent_page": "Надіслані заявки",
        "status": "Статус",
        "site_counts": "Лічильники сайтів",
        "last_run_jobs": "Останні запущені задачі",
        "save_selected": "Зберегти вибрані",
        "approve_all": "Підтвердити всі supported + review",
        "clear_all": "Зняти всі",
        "send": "Подати заявки на вибрані",
        "ok": "OK",
        "company": "Компанія",
        "title": "Назва",
        "url": "URL",
        "site": "Сайт",
        "supported": "Supported",
        "blocker": "Блокер",
        "recommendation": "Рекомендація",
        "score": "Оцінка",
        "approved": "Підтверджено",
        "yes": "так",
        "no": "ні",
        "blocked": "заблоковано",
        "no_batch": "Списку ще немає.",
        "submission_log": "Останні логи розсилки",
        "time": "Час",
        "result": "Результат",
        "blocked_reason": "Причина блокування",
        "no_log": "Логів розсилки ще немає.",
        "sent_title": "Надіслані заявки",
        "sent_empty": "Надісланих заявок ще не знайдено.",
        "back": "Назад",
    },
}
TEXT["uk"].update(
    {
        "scan": "Сканувати всі джерела + зібрати список",
        "function_graph": "Карта функціоналу",
    }
)
BUTTON_HINTS["uk"]["scan"] = (
    "Оновити всі налаштовані джерела: Djinni inbox, відповіді рекрутерів, DOU, Work.ua, Robota.ua; "
    "після цього зібрати новий CSV для перегляду і нічого не відправляти."
)
BUTTON_HINTS["uk"]["function_graph"] = BUTTON_HINTS["en"]["function_graph"]


def load_state() -> dict:
    state = {"latest_batch": "", "status": "idle"}
    if STATE_PATH.exists():
        state.update(json.loads(STATE_PATH.read_text(encoding="utf-8-sig")))
    return sync_state_with_scan_summary(state)


def sync_state_with_scan_summary(state: dict) -> dict:
    if not ALL_SOURCES_SUMMARY_PATH.exists():
        return state
    try:
        summary = json.loads(ALL_SOURCES_SUMMARY_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return state
    batch_text = str(summary.get("batch") or "")
    if not batch_text:
        return state
    batch = Path(batch_text)
    if not batch.exists():
        return state
    current_text = str(state.get("latest_batch") or "")
    current = Path(current_text) if current_text else None
    if current and current.exists() and current.stat().st_mtime >= batch.stat().st_mtime:
        return state
    synced = dict(state)
    synced["latest_batch"] = str(batch)
    synced["status"] = format_scan_summary(summary)
    return synced


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_function_graph_notes(path: Path | None = None) -> dict:
    path = path or FUNCTION_GRAPH_NOTES_PATH
    default = {"schema": "job.function_graph_notes.v0", "nodes": {}}
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(payload, dict):
        return default
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
    return {"schema": str(payload.get("schema") or default["schema"]), "nodes": nodes}


def save_function_graph_note(
    node_id: str,
    *,
    status_note: str = "",
    comment: str = "",
    subtask: str = "",
    path: Path | None = None,
) -> dict:
    path = path or FUNCTION_GRAPH_NOTES_PATH
    graph = function_graph()
    valid_node_ids = {str(node.get("id")) for node in graph["nodes"]}
    node_id = node_id.strip()
    if node_id not in valid_node_ids:
        raise ValueError(f"Unknown function graph node: {node_id}")
    payload = load_function_graph_notes(path)
    nodes = payload.setdefault("nodes", {})
    note = nodes.setdefault(node_id, {"status_note": "", "comments": [], "subtasks": []})
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    status_note = status_note.strip()
    comment = comment.strip()
    subtask = subtask.strip()
    if not any([status_note, comment, subtask]):
        return payload
    if not isinstance(note.get("comments"), list):
        note["comments"] = []
    if not isinstance(note.get("subtasks"), list):
        note["subtasks"] = []
    if status_note:
        note["status_note"] = status_note[:500]
    if comment:
        note["comments"].append({"created_at": now, "text": comment[:1000]})
    if subtask:
        note["subtasks"].append({"created_at": now, "status": "open", "text": subtask[:500]})
    note["updated_at"] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def function_graph_with_notes(path: Path | None = None) -> dict:
    graph = function_graph()
    graph["notes"] = load_function_graph_notes(path)
    return graph


def normalized_site(row: dict[str, str]) -> str:
    site = (row.get("site") or "").strip().lower().replace("-", "").replace("_", "").replace(".", "")
    if site == "djinniinbox":
        return site
    if site in {"djinni", "workua", "dou", "robotaua"}:
        return site
    if site in {"robotaua", "robotau"}:
        return "robotaua"
    url = (row.get("url") or "").strip()
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("djinni.co"):
        return "djinni"
    if host.endswith("work.ua"):
        return "workua"
    if host.endswith("dou.ua"):
        return "dou"
    if host.endswith("robota.ua"):
        return "robotaua"
    return site


def is_djinni_job_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == "djinni.co" and parsed.path.startswith("/jobs/")


def is_workua_job_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in {"work.ua", "www.work.ua"} and bool(re.search(r"^/jobs/\d+/?$", parsed.path))


def is_exact_dou_job_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in {"dou.ua", "jobs.dou.ua", "relocate.dou.ua"} and bool(
        re.search(r"/vacancies/\d+/?$", parsed.path)
    )


def is_robotaua_job_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in {"robota.ua", "www.robota.ua"} and bool(re.search(r"/vacancy\d+", parsed.path, re.I))


def supported_submit_site(row: dict[str, str]) -> str:
    site = normalized_site(row)
    url = (row.get("url") or "").strip()
    if site == "djinni" and is_djinni_job_url(url):
        return "djinni"
    if site == "workua" and is_workua_job_url(url):
        return "workua"
    if site == "dou" and is_exact_dou_job_url(url):
        return "dou"
    if site == "robotaua" and is_robotaua_job_url(url):
        return "robotaua"
    return ""


def submit_blocker(row: dict[str, str]) -> str:
    if supported_submit_site(row):
        return ""
    site = normalized_site(row)
    url = (row.get("url") or "").strip()
    if site == "djinniinbox":
        return "Use Djinni inbox review/reply flow"
    if site == "dou" and "dou.ua" in url:
        return "DOU requires exact vacancy URL ending /vacancies/<id>/"
    if site == "workua":
        return "Work.ua requires exact public URL like https://www.work.ua/jobs/<id>/"
    if site == "robotaua":
        return "Robota.ua requires exact public URL containing /vacancy<id>"
    if site == "djinni":
        return "Djinni requires exact public job URL"
    return "review only"


def is_review_send_row(row: dict[str, str]) -> bool:
    return normalized_site(row) == "djinniinbox" and is_djinni_thread_url((row.get("url") or "").strip())


def is_actionable_row(row: dict[str, str]) -> bool:
    return bool(supported_submit_site(row)) or is_review_send_row(row)


def is_supported_submit_row(row: dict[str, str]) -> bool:
    return bool(supported_submit_site(row))


def supported_label(row: dict[str, str]) -> str:
    site = supported_submit_site(row)
    if site:
        return SITE_LABELS[site]
    if normalized_site(row) == "djinniinbox":
        return "Djinni inbox"
    return "no"


def tooltip_attrs(text: str) -> str:
    escaped = html.escape(text, quote=True)
    return f'title="{escaped}" aria-label="{escaped}"'


def normalize_lang(lang: str | None) -> str:
    return "uk" if (lang or "").strip().lower() == "uk" else "en"


def text_for(lang: str, key: str) -> str:
    lang = normalize_lang(lang)
    return TEXT[lang].get(key, TEXT["en"].get(key, key))


def hint_for(lang: str, key: str) -> str:
    lang = normalize_lang(lang)
    return BUTTON_HINTS[lang].get(key, BUTTON_HINTS["en"].get(key, key))


def url_with_lang(path: str, lang: str) -> str:
    return f"{path}?lang={normalize_lang(lang)}"


def lang_from_request(path: str) -> str:
    query = parse_qs(urlparse(path).query)
    return normalize_lang((query.get("lang") or ["en"])[0])


def supported_cell_html(row: dict[str, str], lang: str = "en") -> str:
    site = supported_submit_site(row)
    if site:
        label = SITE_LABELS.get(site, site)
        return f"<span class='supported-check' {tooltip_attrs(label + ' final submit adapter is supported')}>&#10003;</span>"
    if is_review_send_row(row):
        return f"<span class='review-flow' {tooltip_attrs('Djinni inbox review/reply flow is supported')}>&#8635;</span>"
    return f"<span class='muted' {tooltip_attrs(submit_blocker(row))}>{html.escape(text_for(lang, 'no'))}</span>"


def ensure_live_fields(fields: list[str]) -> list[str]:
    result = list(fields)
    for field in EXTRA_LIVE_COLUMNS:
        if field not in result:
            result.append(field)
    return result


def normalize_row_for_site(row: dict[str, str], site: str, approve: bool = True) -> dict[str, str]:
    normalized = dict(row)
    normalized["site"] = site
    normalized["message_file"] = ""
    normalized["approved_to_submit"] = "true" if approve else "false"
    normalized["final_submit_allowed"] = "true" if approve else "false"
    normalized.setdefault("approved_resume_path", "")
    if site == "dou" and not has_exact_upload_gate(row, site) and DEFAULT_DOU_RESUME_PATH.exists():
        normalized["upload_allowed"] = "true"
        normalized["approved_resume_name"] = DEFAULT_DOU_RESUME_PATH.name
        normalized["approved_resume_path"] = str(DEFAULT_DOU_RESUME_PATH)
        normalized["resume_policy"] = "upload_file"
    elif site == "robotaua" and not has_exact_upload_gate(row, site) and DEFAULT_ROBOTAUA_RESUME_PATH.exists():
        normalized["upload_allowed"] = "true"
        normalized["approved_resume_name"] = DEFAULT_ROBOTAUA_RESUME_PATH.name
        normalized["approved_resume_path"] = str(DEFAULT_ROBOTAUA_RESUME_PATH)
        normalized["resume_policy"] = "upload_exact_resume"
    elif site == "workua" and not has_exact_upload_gate(row, site):
        normalized["upload_allowed"] = "false"
        normalized["approved_resume_name"] = "Work.ua profile resume"
        normalized["approved_resume_path"] = ""
        normalized["resume_policy"] = "use_workua_profile"
        normalized["message_policy"] = "profile_resume_only"
        normalized["profile_resume_only_allowed"] = "true"
    elif not has_exact_upload_gate(row, site):
        normalized["upload_allowed"] = "false"
        normalized["approved_resume_name"] = ""
        normalized["approved_resume_path"] = ""
        normalized["resume_policy"] = "no_resume"
    normalized.setdefault("salary_usd", "3000")
    linkedin = (normalized.get("linkedin") or "").strip()

    if site == "workua":
        if normalized.get("message_policy") == "profile_resume_only":
            normalized["linkedin"] = ""
            normalized["linkedin_policy"] = "no_linkedin"
        else:
            normalized["linkedin_policy"] = "fill_field" if linkedin else "no_linkedin"
    elif site == "dou":
        normalized["linkedin_policy"] = "fill_url" if linkedin else "omit"
    elif site == "robotaua":
        normalized["linkedin_policy"] = "include_linkedin" if linkedin else "omit_linkedin"
        if not linkedin:
            normalized["linkedin"] = ""
    else:
        normalized["linkedin_policy"] = normalized.get("linkedin_policy", "")

    for idx in range(1, 9):
        normalized.setdefault(f"answer_{idx}", "")
    return normalized


def has_exact_upload_gate(row: dict[str, str], site: str) -> bool:
    resume_policy = (row.get("resume_policy") or "").strip().lower()
    expected_policy = {
        "dou": "upload_file",
        "robotaua": "upload_exact_resume",
    }.get(site)
    if not expected_policy:
        return False
    return (
        resume_policy == expected_policy
        and (row.get("upload_allowed") or "").strip().lower() in {"1", "true", "yes", "y", "allowed", "approve", "approved"}
        and bool((row.get("approved_resume_name") or "").strip())
        and bool((row.get("approved_resume_path") or row.get("resume_path") or "").strip())
    )


def update_batch_approvals(batch: Path, selected: set[int] | None = None, approve_all: bool = False) -> tuple[int, int]:
    with batch.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = ensure_live_fields(list(reader.fieldnames or []))
        rows = list(reader)
    changed = 0
    skipped = 0
    for idx, row in enumerate(rows):
        if not is_actionable_row(row):
            row["approved_to_submit"] = "false"
            row["final_submit_allowed"] = "false"
            skipped += 1
            continue
        should_approve = approve_all or (selected is not None and idx in selected)
        site = supported_submit_site(row)
        if site:
            row.update(normalize_row_for_site(row, site, approve=should_approve))
        else:
            row["message_file"] = ""
            row["approved_to_submit"] = "true" if should_approve else "false"
            row["final_submit_allowed"] = "true" if should_approve else "false"
        if should_approve:
            changed += 1
    with batch.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return changed, skipped


def count_approved_rows(batch: Path) -> int:
    with batch.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    history = latest_submission_by_url()
    return sum(
        1
        for row in rows
        if is_actionable_row(row)
        and row.get("approved_to_submit") == "true"
        and row.get("final_submit_allowed") == "true"
        and not terminal_submission_state({"source_url": row.get("url", "")}, history)
    )


def approved_rows_by_site(batch: Path, skip_submission_history: bool = False) -> dict[str, list[dict[str, str]]]:
    with batch.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    history = latest_submission_by_url() if skip_submission_history else {}
    grouped = {site: [] for site in ACTIONABLE_SITE_NAMES}
    for row in rows:
        site = supported_submit_site(row)
        if not site:
            continue
        if skip_submission_history and terminal_submission_state({"source_url": row.get("url", "")}, history):
            continue
        if row.get("approved_to_submit") == "true" and row.get("final_submit_allowed") == "true":
            grouped[site].append(normalize_row_for_site(row, site, approve=True))
    return grouped


def approved_review_rows(batch: Path) -> list[dict[str, str]]:
    with batch.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    result = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not is_review_send_row(row):
            continue
        if row.get("approved_to_submit") == "true" and row.get("final_submit_allowed") == "true":
            message = (row.get("message") or "").strip()
            key = (normalize_thread_url(row.get("url") or ""), message_digest(message))
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(row))
    return result


def write_site_csv(site: str, rows: list[dict[str, str]], mode: str) -> Path:
    WEB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = WEB_RUNS_DIR / f"{site}_{mode}_{stamp}.csv"
    fieldnames = ensure_live_fields(
        [
            "site",
            "url",
            "title",
            "company",
            "message",
            "message_file",
            "salary_usd",
            "linkedin",
            "linkedin_policy",
            "resume_policy",
            "approved_resume_name",
            "approved_resume_path",
            "upload_allowed",
            "approved_to_submit",
            "final_submit_allowed",
            "source_type",
            "recommendation",
            "score",
            "reason",
        ]
    )
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def command_for_site(site: str, csv_path: Path, mode: str, log_path: Path) -> list[str]:
    script_by_site = {
        "djinni": ROOT / "src" / "djinni_csv_apply.py",
        "workua": ROOT / "src" / "workua_csv_apply.py",
        "dou": ROOT / "src" / "dou_csv_apply.py",
        "robotaua": ROOT / "src" / "robotaua_csv_apply.py",
    }
    cmd = [sys.executable, str(script_by_site[site]), "--csv", str(csv_path), "--log", str(log_path)]
    if mode == "dry-run":
        return cmd
    if mode == "prepare":
        if site == "djinni":
            return cmd
        if site == "workua":
            return [*cmd, "--prepare-browser", "--i-understand-this-prepares-workua-application"]
        if site == "dou":
            return [*cmd, "--prepare", "--i-understand-this-fills-dou-form"]
        if site == "robotaua":
            return [*cmd, "--pre-submit", "--i-understand-this-prepares-robotaua-application"]
    if mode == "submit":
        if site == "djinni":
            return [*cmd, "--execute", "--i-understand-this-submits-applications"]
        if site == "workua":
            return [
                *cmd,
                "--execute",
                "--i-understand-this-prepares-workua-application",
                "--i-understand-this-submits-workua-application",
            ]
        if site == "dou":
            return [*cmd, "--execute", "--i-understand-this-fills-dou-form", "--i-understand-this-sends-dou-applications"]
        if site == "robotaua":
            return [
                *cmd,
                "--execute",
                "--i-understand-this-prepares-robotaua-application",
                "--i-understand-this-submits-robotaua-application",
            ]
    raise ValueError(f"Unsupported run mode/site: {mode}/{site}")


def append_jsonl(path: Path, event: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def launch_review_runs(batch: Path, mode: str, stamp: str) -> list[dict[str, str | int]]:
    rows = approved_review_rows(batch)
    if not rows:
        return []
    log_path = WEB_RUNS_DIR / f"djinni_inbox_{mode}_{stamp}.log"
    jsonl_log = WEB_RUNS_DIR / f"djinni_inbox_{mode}_{stamp}.jsonl"
    if mode == "dry-run":
        for row in rows:
            append_jsonl(
                jsonl_log,
                {
                    "schema": "job.djinni_inbox_reply_attempt.v0",
                    "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "site": "djinni_inbox",
                    "source_url": row.get("url", ""),
                    "thread_url": normalize_thread_url(row.get("url", "")),
                    "company": row.get("company", ""),
                    "title": row.get("title", ""),
                    "result": "dry_run_ok",
                    "message_length": len(row.get("message", "")),
                    "message_digest": message_digest(row.get("message", "")),
                },
            )
        log_path.write_text(f"dry-run ok for {len(rows)} Djinni inbox review row(s)\n", encoding="utf-8")
        return [{"site": "djinni_inbox", "mode": mode, "rows": len(rows), "pid": 0, "stdout_log": str(log_path), "jsonl_log": str(jsonl_log)}]
    if mode == "prepare":
        action = "--prepare-thank-you"
    elif mode == "submit":
        action = "--send-thank-you"
    else:
        raise ValueError(f"Unsupported review mode: {mode}")
    wrapper = ROOT / "src" / "recruiter_response_scan.py"
    lines = []
    pids: list[int] = []
    for idx, row in enumerate(rows, start=1):
        message = (row.get("message") or "").strip()
        if not message:
            append_jsonl(
                jsonl_log,
                {
                    "schema": "job.djinni_inbox_reply_attempt.v0",
                    "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "site": "djinni_inbox",
                    "source_url": row.get("url", ""),
                    "thread_url": normalize_thread_url(row.get("url", "")),
                    "result": "blocked_validation",
                    "blocked_reason": "empty review message",
                },
            )
            continue
        cmd = [
            sys.executable,
            str(wrapper),
            "--thread-url",
            row.get("url", ""),
            action,
            "--message",
            message,
        ]
        if mode == "submit":
            cmd.append("--i-understand-this-sends-recruiter-message")
        row_log = WEB_RUNS_DIR / f"djinni_inbox_{mode}_{stamp}_{idx}.log"
        with row_log.open("w", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_handle, stderr=subprocess.STDOUT)
        pids.append(proc.pid)
        lines.append(f"row {idx}: pid={proc.pid} url={row.get('url', '')} log={row_log}")
        append_jsonl(
            jsonl_log,
            {
                "schema": "job.djinni_inbox_reply_attempt.v0",
                "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "site": "djinni_inbox",
                "source_url": row.get("url", ""),
                "thread_url": normalize_thread_url(row.get("url", "")),
                "company": row.get("company", ""),
                "title": row.get("title", ""),
                "result": "started",
                "mode": mode,
                "pid": proc.pid,
                "message_length": len(message),
                "message_digest": message_digest(message),
            },
        )
    log_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return [{"site": "djinni_inbox", "mode": mode, "rows": len(rows), "pid": pids[0] if pids else 0, "stdout_log": str(log_path), "jsonl_log": str(jsonl_log)}]


def launch_approved_runs(batch: Path, mode: str, include_review_runs: bool = True) -> list[dict[str, str | int]]:
    jobs: list[dict[str, str | int]] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for site, rows in sorted(approved_rows_by_site(batch, skip_submission_history=(mode == "submit")).items()):
        if not rows:
            continue
        csv_path = write_site_csv(site, rows, mode)
        log_path = WEB_RUNS_DIR / f"{site}_{mode}_{stamp}.log"
        jsonl_log = WEB_RUNS_DIR / f"{site}_{mode}_{stamp}.jsonl"
        cmd = command_for_site(site, csv_path, mode, jsonl_log)
        with log_path.open("w", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_handle, stderr=subprocess.STDOUT)
        jobs.append(
            {
                "site": site,
                "mode": mode,
                "rows": len(rows),
                "pid": proc.pid,
                "csv": str(csv_path),
                "stdout_log": str(log_path),
                "jsonl_log": str(jsonl_log),
            }
        )
    if include_review_runs:
        jobs.extend(launch_review_runs(batch, mode, stamp))
    return jobs


def log_tail(path: Path, limit: int = 12) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def infer_site_from_log_path(path: Path) -> str:
    stem = path.stem.lower()
    if stem.startswith("djinni_inbox"):
        return "djinni_inbox"
    for site in [*sorted(ACTIONABLE_SITE_NAMES), "djinni"]:
        if stem.startswith(site):
            return site
    return ""


def event_sort_key(event: dict) -> str:
    return str(event.get("attempted_at") or event.get("created_at") or event.get("_file_mtime") or "")


def event_has_submit_success_evidence(event: dict) -> bool:
    after = event.get("after")
    if not isinstance(after, dict):
        after = {}
    after_url = str(after.get("url") or "").lower()
    after_title = str(after.get("title") or "").lower()
    submitted = event.get("submitted")
    if isinstance(submitted, dict) and submitted.get("submitted") is True:
        if "/jobseeker/my/resumes/sent/" in after_url or "?sent" in after_url or "sent#replied-id" in after_url:
            return True
    success_markers = [
        "/jobseeker/my/resumes/sent/",
        "application-sent",
        "apply/success",
        "applied=true",
        "?sent",
        "sent#replied-id",
    ]
    if any(marker in after_url for marker in success_markers):
        return True
    surfaces = after.get("application_surfaces") if isinstance(after.get("application_surfaces"), list) else []
    if any("sent" in str(surface.get("className") or "").lower() for surface in surfaces if isinstance(surface, dict)):
        return True
    body = str(after.get("body_start") or "").lower()
    return "submitted" in after_title or "sent" in after_title or "ви відгукнулись" in body


def display_result(event: dict) -> str:
    result = str(event.get("result") or "")
    if result == "submit_clicked_unconfirmed" and event_has_submit_success_evidence(event):
        return "submitted_success_inferred"
    return result


def submission_log_events(limit: int = 30) -> list[dict]:
    events: list[dict] = []
    for log_site, log_path in SUBMISSION_LOGS.items():
        for item in log_tail(log_path, limit=8):
            item = dict(item)
            item.setdefault("site", log_site)
            item["_file_mtime"] = str(log_path.stat().st_mtime if log_path.exists() else "")
            events.append(item)
    if WEB_RUNS_DIR.exists():
        jsonl_files = sorted(WEB_RUNS_DIR.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:40]
        for log_path in jsonl_files:
            inferred_site = infer_site_from_log_path(log_path)
            for item in log_tail(log_path, limit=8):
                item = dict(item)
                if inferred_site:
                    item.setdefault("site", inferred_site)
                item["_file_mtime"] = str(log_path.stat().st_mtime)
                item["_log_file"] = log_path.name
                events.append(item)
    events.sort(key=event_sort_key, reverse=True)
    return events[:limit]


def sent_application_events(limit: int = 500) -> list[dict]:
    sent: dict[str, dict] = {}
    events: list[dict] = []
    for log_site, log_path in SUBMISSION_LOGS.items():
        if not log_path.exists():
            continue
        for item in log_tail(log_path, limit=100000):
            item = dict(item)
            item.setdefault("site", log_site)
            item["_file_mtime"] = str(log_path.stat().st_mtime)
            item["_log_file"] = log_path.name
            events.append(item)
    if WEB_RUNS_DIR.exists():
        for log_path in sorted(WEB_RUNS_DIR.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True):
            inferred_site = infer_site_from_log_path(log_path)
            for item in log_tail(log_path, limit=100000):
                item = dict(item)
                if inferred_site:
                    item.setdefault("site", inferred_site)
                item["_file_mtime"] = str(log_path.stat().st_mtime)
                item["_log_file"] = log_path.name
                events.append(item)
    for event in events:
        result = display_result(event)
        if result not in {"submitted_success", "submitted_success_inferred", "already_applied"}:
            continue
        url = str(event.get("source_url") or event.get("url") or "").strip()
        key = url.lower().rstrip("/") if url else f"event:{len(sent)}"
        event = dict(event)
        event["_display_result"] = result
        if key not in sent or event_sort_key(event) > event_sort_key(sent[key]):
            sent[key] = event
    return sorted(sent.values(), key=event_sort_key, reverse=True)


def render_language_switch(path: str, lang: str) -> str:
    other = "en" if normalize_lang(lang) == "uk" else "uk"
    return (
        f"<div class='lang-switch'>{html.escape(text_for(lang, 'language'))}: "
        f"<strong>{normalize_lang(lang).upper()}</strong> | "
        f"<a href='{html.escape(url_with_lang(path, other))}'>{other.upper()}</a></div>"
    )


FUNCTION_MAP_STYLE = """
    .graph-note-form { display: grid; grid-template-columns: 180px repeat(3, minmax(150px, 1fr)) auto; gap: 10px; align-items: end; margin: 18px 0; }
    .graph-note-form label { display: grid; gap: 4px; font-size: 13px; color: #52606d; }
    .graph-note-form select, .graph-note-form input { padding: 7px 9px; border: 1px solid #bcccdc; border-radius: 6px; min-width: 0; }
    .map-controls { display: flex; gap: 12px; margin: 18px 0; flex-wrap: wrap; align-items: center; }
    .map-controls select, .map-controls input { padding: 7px 9px; border: 1px solid #bcccdc; border-radius: 6px; }
    .function-layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 18px; align-items: start; }
    .mind-map { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 13px; }
    .map-node {
      min-height: 104px; text-align: left; border: 2px solid var(--group-color); background: #fff;
      border-left-width: 8px; border-radius: 8px; display: flex; flex-direction: column; justify-content: space-between;
      transition: transform .12s ease, box-shadow .12s ease; outline: none;
    }
    .map-node:hover, .map-node:focus, .map-node.selected { transform: translateY(-1px); box-shadow: 0 8px 18px rgba(31, 41, 51, .13); }
    .map-node span { font-weight: 700; line-height: 1.25; }
    .map-node small { color: #52606d; margin-top: 10px; }
    .shape-diamond { border-radius: 2px 18px 2px 18px; }
    .shape-round { border-radius: 999px; min-height: 112px; padding-left: 18px; }
    .shape-cylinder { border-radius: 22px 22px 8px 8px; background: linear-gradient(#f8fbff, #fff); }
    .shape-hex { clip-path: polygon(8% 0, 92% 0, 100% 50%, 92% 100%, 8% 100%, 0 50%); padding-left: 22px; padding-right: 22px; }
    .shape-octagon { clip-path: polygon(12% 0, 88% 0, 100% 12%, 100% 88%, 88% 100%, 12% 100%, 0 88%, 0 12%); padding: 16px 20px; }
    .status-planned { opacity: .78; }
    .detail-panel { position: sticky; top: 16px; border: 1px solid #bcccdc; border-radius: 8px; padding: 16px; background: #f8fbf8; }
    .detail-panel h2 { margin-top: 0; }
    .detail-panel dt { font-weight: 700; margin-top: 10px; }
    .detail-panel dd { margin: 2px 0 0; color: #52606d; }
    @media (max-width: 980px) { .graph-note-form { grid-template-columns: 1fr; } }
    @media (max-width: 860px) { .function-layout { grid-template-columns: 1fr; } .detail-panel { position: static; } }
"""


def render_page_shell(title: str, body: str, lang: str, extra_style: str = "") -> str:
    return f"""<!doctype html>
<html lang="{html.escape(normalize_lang(lang))}">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2933; }}
    main {{ max-width: 1280px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
    button, .button {{ padding: 8px 12px; border: 1px solid #8aa0b2; background: #f7fafc; border-radius: 6px; cursor: pointer; color: #1f2933; text-decoration: none; display: inline-block; }}
    button.primary, .button.primary {{ background: #1f7a4d; color: white; border-color: #1f7a4d; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 18px; }}
    th, td {{ text-align: left; padding: 9px 8px; border-bottom: 1px solid #d9e2ec; vertical-align: top; }}
    .bar {{ display: flex; gap: 8px; margin: 18px 0; flex-wrap: wrap; }}
    .muted {{ color: #66788a; }}
    .supported-check {{ color: #1f7a4d; font-weight: 700; }}
    .review-flow {{ color: #6b5b95; font-weight: 700; }}
    .status {{ padding: 10px 12px; background: #eef4f8; border: 1px solid #bcccdc; border-radius: 6px; margin: 12px 0; }}
    .lang-switch {{ white-space: nowrap; font-size: 14px; color: #52606d; }}
{extra_style}
  </style>
</head>
<body>
<main>{body}</main>
</body>
</html>"""


def event_summary(event: dict) -> str:
    status = str(event.get("status") or "")
    payload_text = str(event.get("payload_json") or "").strip()
    if not payload_text:
        return status
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return status or payload_text[:160]
    message = str(payload.get("message") or payload.get("reason") or "").strip()
    if message and status:
        return f"{status}: {message}"
    return message or status


def scan_inbox_status(execute_profile_toggle: bool = False) -> str:
    try:
        result = scan_inbox(execute_profile_toggle=execute_profile_toggle)
    except Exception as exc:
        return f"inbox scan failed: {type(exc).__name__}: {exc}"
    profile = result.get("profile") or {}
    profile_text = ""
    if profile.get("clicked"):
        profile_text = "; Djinni profile toggle clicked"
    elif profile.get("found"):
        profile_text = "; Djinni profile toggle available but not clicked"
    elif execute_profile_toggle:
        profile_text = "; Djinni profile toggle not found or already active"
    return (
        f"inbox offers: {result.get('offers_found', 0)} "
        f"(digest={result.get('digest', 0)}, review={result.get('review', 0)}, "
        f"reject_candidate={result.get('reject_candidate', 0)}){profile_text}"
    )


def remediate_dou_listing_observations(limit: int = 4) -> str:
    listings: list[str] = []
    seen: set[str] = set()
    for path in OBSERVATION_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("source_site", "")).lower() != "dou":
                continue
            url = str(row.get("source_url") or "")
            if not is_dou_listing_url(url) or url in seen:
                continue
            seen.add(url)
            listings.append(url)
            if len(listings) >= limit:
                break
        if len(listings) >= limit:
            break
    if not listings:
        listings = []
    try:
        summary = run_dou_remediation_to_temp(listings or None)
        remediations = summary.get("listing_remediations") or {}
        if int(remediations.get("exact_urls_extracted") or 0) == 0 and listings:
            fallback = run_dou_remediation_to_temp(None)
            fallback_remediations = fallback.get("listing_remediations") or {}
            if int(fallback_remediations.get("exact_urls_extracted") or 0) > 0:
                summary = fallback
    except Exception as exc:
        return f"DOU remediation failed: {type(exc).__name__}: {exc}"
    remediations = summary.get("listing_remediations") or {}
    extracted = int(remediations.get("exact_urls_extracted") or 0)
    blocked = int(remediations.get("blocked") or 0)
    return f"DOU remediation: listings={len(listings)}, exact_urls={extracted}, blocked={blocked}"


def run_dou_remediation_to_temp(urls: list[str] | None) -> dict[str, object]:
    WEB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = "listing" if urls else "default"
    observations_path = WEB_RUNS_DIR / f"dou_{prefix}_observations_{stamp}.jsonl"
    summary = run_dou_once(
        "Python AI",
        urls=urls,
        limit=10,
        max_pages=len(urls) if urls else 2,
        timeout=15,
        observations_path=observations_path,
        progress_path=WEB_RUNS_DIR / f"dou_{prefix}_progress_{stamp}.json",
        summary_path=WEB_RUNS_DIR / f"dou_{prefix}_summary_{stamp}.json",
        blockers_path=WEB_RUNS_DIR / f"dou_{prefix}_blockers_{stamp}.json",
        remediations_path=WEB_RUNS_DIR / f"dou_{prefix}_remediations_{stamp}.jsonl",
        persist=True,
    )
    if observations_path.exists() and observations_path.stat().st_size:
        merge_jsonl_by_source_url(observations_path, DOU_OBSERVATIONS_PATH)
    return summary


def merge_jsonl_by_source_url(source: Path, target: Path) -> None:
    merged: dict[str, dict] = {}
    for path in [target, source]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = str(row.get("source_url") or "")
            if url:
                merged[url.lower().rstrip("/")] = row
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as f:
        for row in merged.values():
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def render_all_sites_page(lang: str = "en") -> str:
    lang = normalize_lang(lang)
    state = load_state()
    batch = Path(state["latest_batch"]) if state.get("latest_batch") else None
    rows = candidate_summary(batch) if batch and batch.exists() else []
    row_html = []
    site_counts: dict[str, int] = {}
    supported_counts: dict[str, int] = {}
    for idx, row in enumerate(rows):
        site = normalized_site(row)
        supported_site = supported_submit_site(row)
        site_counts[site or "unknown"] = site_counts.get(site or "unknown", 0) + 1
        if supported_site:
            supported_counts[supported_site] = supported_counts.get(supported_site, 0) + 1
        approved = row.get("approved_to_submit") == "true" and row.get("final_submit_allowed") == "true"
        checkbox = (
            f"<input type='checkbox' name='row' value='{idx}' {'checked' if approved else ''}>"
            if supported_site or is_review_send_row(row)
            else f"<span class='muted'>{html.escape(text_for(lang, 'blocked'))}</span>"
        )
        row_html.append(
            "<tr>"
            f"<td>{checkbox}</td>"
            f"<td>{html.escape(row.get('company', ''))}</td>"
            f"<td>{html.escape(row.get('title', ''))}</td>"
            f"<td><a href='{html.escape(row.get('url', ''))}' target='_blank' rel='noreferrer'>open</a></td>"
            f"<td>{html.escape(row.get('site', ''))}</td>"
            f"<td>{supported_cell_html(row, lang)}</td>"
            f"<td>{html.escape(submit_blocker(row))}</td>"
            f"<td>{html.escape(row.get('recommendation', ''))}</td>"
            f"<td>{html.escape(row.get('score', ''))}</td>"
            f"<td>{html.escape(text_for(lang, 'yes') if approved else text_for(lang, 'no'))}</td>"
            "</tr>"
        )
    rows_html = "\n".join(row_html) or f"<tr><td colspan='10'>{html.escape(text_for(lang, 'no_batch'))}</td></tr>"

    log_rows = []
    for item in submission_log_events(limit=30):
        log_site = str(item.get("site") or infer_site_from_log_path(Path(str(item.get("_log_file") or ""))) or "")
        blocked_reason = str(item.get("blocked_reason", ""))
        if display_result(item) == "submitted_success_inferred" and not blocked_reason:
            blocked_reason = "confirmed by post-submit browser URL"
        log_rows.append(
            "<tr>"
            f"<td>{html.escape(SITE_LABELS.get(log_site, log_site))}</td>"
            f"<td>{html.escape(str(item.get('attempted_at') or item.get('created_at') or ''))}</td>"
            f"<td>{html.escape(str(item.get('company', '')))}</td>"
            f"<td>{html.escape(display_result(item))}</td>"
            f"<td>{html.escape(blocked_reason)}</td>"
            f"<td>{html.escape(str(item.get('source_url') or item.get('url') or ''))}</td>"
            "</tr>"
        )
    log_html = "\n".join(log_rows) or f"<tr><td colspan='6'>{html.escape(text_for(lang, 'no_log'))}</td></tr>"

    cfg = settings()
    model = html.escape(cfg.agent_model)
    locations = html.escape(cfg.location_preferences)
    batch_label = html.escape(str(batch or "none"))
    status = html.escape(state.get("status", "idle"))
    last_jobs = state.get("last_send_jobs") or []
    last_job_text = "none"
    if last_jobs:
        last_job_text = "; ".join(
            f"{job.get('site')} {job.get('mode')} pid={job.get('pid')} rows={job.get('rows')}" for job in last_jobs
        )
    counts_text = " | ".join(
        f"Djinni inbox: {site_counts.get(site, 0)} review / {site_counts.get(site, 0)} shown"
        if site == "djinniinbox"
        else f"{SITE_LABELS.get(site, site)}: {supported_counts.get(site, 0)} supported / {site_counts.get(site, 0)} shown"
        for site in sorted(set(site_counts) | ACTIONABLE_SITE_NAMES)
        if site in ACTIONABLE_SITE_NAMES or site_counts.get(site)
    ) or "empty"
    body = f"""
  <header>
    <div>
      <h1>{html.escape(text_for(lang, 'app_title'))}</h1>
      <div class="muted">{html.escape(text_for(lang, 'agent_model'))}: {model} | {html.escape(text_for(lang, 'locations'))}: {locations} | {html.escape(text_for(lang, 'latest_batch'))}: {batch_label}</div>
    </div>
    {render_language_switch("/", lang)}
  </header>

  <form class="bar" method="post" action="/scan">
    <button class="primary" type="submit" {tooltip_attrs(hint_for(lang, "scan"))}>{html.escape(text_for(lang, 'scan'))}</button>
    <button type="submit" formaction="/scan-inbox" {tooltip_attrs(hint_for(lang, "scan_inbox"))}>{html.escape(text_for(lang, 'scan_inbox'))}</button>
    <button type="submit" formaction="/profile-on" {tooltip_attrs(hint_for(lang, "profile_on"))}>{html.escape(text_for(lang, 'profile_on'))}</button>
  </form>

  <form class="bar" method="post" action="/bot-note">
    <button type="submit" {tooltip_attrs(hint_for(lang, "bot_note"))}>{html.escape(text_for(lang, 'bot_note'))}</button>
    <a class="button" href="/platform-progress" {tooltip_attrs(hint_for(lang, "progress"))}>{html.escape(text_for(lang, 'progress'))}</a>
    <a class="button" href="{html.escape(url_with_lang('/function-map', lang))}" title="Interactive requirements and feature graph">{html.escape(text_for(lang, 'function_graph'))}</a>
    <a class="button" href="{html.escape(url_with_lang('/sent-applications', lang))}" {tooltip_attrs(hint_for(lang, "sent"))}>{html.escape(text_for(lang, 'sent_page'))}</a>
  </form>

  <div class="status">
    <b>{html.escape(text_for(lang, 'status'))}:</b> {status}<br>
    <b>{html.escape(text_for(lang, 'site_counts'))}:</b> {html.escape(counts_text)}<br>
    <b>{html.escape(text_for(lang, 'last_run_jobs'))}:</b> {html.escape(last_job_text)}
  </div>

  <form method="post" action="/save-approvals">
    <div class="bar">
      <button type="submit" {tooltip_attrs(hint_for(lang, "save_selected"))}>{html.escape(text_for(lang, 'save_selected'))}</button>
      <button type="submit" formaction="/approve-all" {tooltip_attrs(hint_for(lang, "approve_all"))}>{html.escape(text_for(lang, 'approve_all'))}</button>
      <button type="submit" formaction="/clear-approvals" {tooltip_attrs(hint_for(lang, "clear_all"))}>{html.escape(text_for(lang, 'clear_all'))}</button>
      <button type="submit" formaction="/send" class="primary" {tooltip_attrs(hint_for(lang, "send"))}>{html.escape(text_for(lang, 'send'))}</button>
    </div>
    <table>
      <thead><tr><th>{html.escape(text_for(lang, 'ok'))}</th><th>{html.escape(text_for(lang, 'company'))}</th><th>{html.escape(text_for(lang, 'title'))}</th><th>{html.escape(text_for(lang, 'url'))}</th><th>{html.escape(text_for(lang, 'site'))}</th><th>{html.escape(text_for(lang, 'supported'))}</th><th>{html.escape(text_for(lang, 'blocker'))}</th><th>{html.escape(text_for(lang, 'recommendation'))}</th><th>{html.escape(text_for(lang, 'score'))}</th><th>{html.escape(text_for(lang, 'approved'))}</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </form>

  <h2>{html.escape(text_for(lang, 'submission_log'))}</h2>
  <table>
    <thead><tr><th>{html.escape(text_for(lang, 'site'))}</th><th>{html.escape(text_for(lang, 'time'))}</th><th>{html.escape(text_for(lang, 'company'))}</th><th>{html.escape(text_for(lang, 'result'))}</th><th>{html.escape(text_for(lang, 'blocked_reason'))}</th><th>{html.escape(text_for(lang, 'url'))}</th></tr></thead>
    <tbody>{log_html}</tbody>
  </table>
"""
    return render_page_shell(text_for(lang, "app_title"), body, lang)


def render_sent_applications_page(lang: str = "en") -> str:
    lang = normalize_lang(lang)
    rows = []
    for item in sent_application_events():
        log_site = str(item.get("site") or infer_site_from_log_path(Path(str(item.get("_log_file") or ""))) or "")
        url = str(item.get("source_url") or item.get("url") or "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('attempted_at') or item.get('created_at') or ''))}</td>"
            f"<td>{html.escape(SITE_LABELS.get(log_site, log_site))}</td>"
            f"<td>{html.escape(str(item.get('company', '')))}</td>"
            f"<td>{html.escape(str(item.get('title', '')))}</td>"
            f"<td><a href='{html.escape(url)}' target='_blank' rel='noreferrer'>open</a></td>"
            f"<td>{html.escape(str(item.get('_display_result') or display_result(item)))}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rows) or f"<tr><td colspan='6'>{html.escape(text_for(lang, 'sent_empty'))}</td></tr>"
    body = f"""
  <header>
    <div>
      <h1>{html.escape(text_for(lang, 'sent_title'))}</h1>
      <div class="muted"><a href="{html.escape(url_with_lang('/', lang))}">{html.escape(text_for(lang, 'back'))}</a></div>
    </div>
    {render_language_switch("/sent-applications", lang)}
  </header>
  <table>
    <thead><tr><th>{html.escape(text_for(lang, 'time'))}</th><th>{html.escape(text_for(lang, 'site'))}</th><th>{html.escape(text_for(lang, 'company'))}</th><th>{html.escape(text_for(lang, 'title'))}</th><th>{html.escape(text_for(lang, 'url'))}</th><th>{html.escape(text_for(lang, 'result'))}</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
"""
    return render_page_shell(text_for(lang, "sent_title"), body, lang)


def render_function_map_page(lang: str = "en") -> str:
    lang = normalize_lang(lang)
    graph = function_graph_with_notes()
    nodes = graph["nodes"]
    groups = graph["groups"]
    note_nodes = graph.get("notes", {}).get("nodes", {})
    group_options = "\n".join(
        f"<option value='{html.escape(group_id)}'>{html.escape(str(group.get('label', group_id)))}</option>"
        for group_id, group in groups.items()
    )
    node_options = "\n".join(
        f"<option value='{html.escape(str(node.get('id', '')))}'>{html.escape(str(node.get('label', node.get('id', ''))))}</option>"
        for node in nodes
    )
    cards = []
    for node in nodes:
        node_id = str(node.get("id", ""))
        note = note_nodes.get(node_id) if isinstance(note_nodes, dict) else None
        if not isinstance(note, dict):
            note = {}
        comments = note.get("comments") if isinstance(note.get("comments"), list) else []
        subtasks = note.get("subtasks") if isinstance(note.get("subtasks"), list) else []
        group = str(node.get("group", ""))
        shape = str(node.get("shape", "box"))
        status = str(node.get("status", ""))
        detail = str(node.get("detail", ""))
        label = str(node.get("label", ""))
        owner = str(node.get("owner", "system"))
        color = str(groups.get(group, {}).get("color", "#4a5568"))
        note_text = str(note.get("status_note") or "")
        note_badge = ""
        if note_text or comments or subtasks:
            note_badge = f"<small>{len(comments)} comments | {len(subtasks)} subtasks</small>"
        cards.append(
            f"<button class='map-node shape-{html.escape(shape)} status-{html.escape(status)}' "
            f"data-group='{html.escape(group)}' data-detail='{html.escape(detail, quote=True)}' "
            f"data-owner='{html.escape(owner, quote=True)}' data-status='{html.escape(status, quote=True)}' "
            f"data-note='{html.escape(note_text, quote=True)}' "
            f"style='--group-color:{html.escape(color)}' title='{html.escape(detail, quote=True)}'>"
            f"<span>{html.escape(label)}</span><small>{html.escape(group)} | {html.escape(status)}</small>"
            f"{note_badge}"
            "</button>"
        )
    edges = graph["edges"]
    edge_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(edge.get('source', '')))}</td>"
        f"<td>{html.escape(str(edge.get('label', '')))}</td>"
        f"<td>{html.escape(str(edge.get('target', '')))}</td>"
        "</tr>"
        for edge in edges
    )
    note_rows = []
    if isinstance(note_nodes, dict):
        labels_by_id = {str(node.get("id")): str(node.get("label") or node.get("id")) for node in nodes}
        for node_id, note in sorted(note_nodes.items()):
            if not isinstance(note, dict):
                continue
            comments = note.get("comments") if isinstance(note.get("comments"), list) else []
            subtasks = note.get("subtasks") if isinstance(note.get("subtasks"), list) else []
            last_comment = ""
            if comments and isinstance(comments[-1], dict):
                last_comment = str(comments[-1].get("text") or "")
            open_subtasks = [
                str(item.get("text") or "")
                for item in subtasks
                if isinstance(item, dict) and str(item.get("status") or "open") == "open"
            ]
            note_rows.append(
                "<tr>"
                f"<td>{html.escape(labels_by_id.get(str(node_id), str(node_id)))}</td>"
                f"<td>{html.escape(str(note.get('status_note') or ''))}</td>"
                f"<td>{html.escape(last_comment)}</td>"
                f"<td>{html.escape('; '.join(open_subtasks[:3]))}</td>"
                "</tr>"
            )
    notes_html = "\n".join(note_rows) or "<tr><td colspan='4'>No graph notes yet.</td></tr>"
    graph_json = html.escape(json.dumps(graph, ensure_ascii=False), quote=True)
    body = f"""
  <header>
    <div>
      <h1>{html.escape(text_for(lang, 'function_graph'))}</h1>
      <div class="muted">Interactive local feature graph; hover or focus a node to see details.</div>
    </div>
    <div class="bar">
      <a class="button" href="{html.escape(url_with_lang('/', lang))}">{html.escape(text_for(lang, 'back'))}</a>
      <a class="button" href="/function-map.json">JSON</a>
    </div>
  </header>
  <form class="graph-note-form" method="post" action="/function-map/notes">
    <input type="hidden" name="lang" value="{html.escape(lang)}">
    <label>Node <select name="node_id">{node_options}</select></label>
    <label>Status note <input name="status_note" maxlength="500" placeholder="decision, requirement, or blocker note"></label>
    <label>Comment <input name="comment" maxlength="1000" placeholder="owner/agent comment"></label>
    <label>Subtask <input name="subtask" maxlength="500" placeholder="next bounded task"></label>
    <button type="submit" {tooltip_attrs("Save a local graph note, comment, or open subtask without changing live application state.")}>Save graph note</button>
  </form>
  <section class="map-controls">
    <label>Group <select id="groupFilter"><option value="">All</option>{group_options}</select></label>
    <label>Text <input id="textFilter" type="search" placeholder="filter"></label>
  </section>
  <section class="function-layout" data-graph="{graph_json}">
    <div class="mind-map">{''.join(cards)}</div>
    <aside class="detail-panel">
      <h2 id="detailTitle">Hover a node</h2>
      <p id="detailText">Task details will appear here.</p>
      <dl><dt>Status</dt><dd id="detailStatus">-</dd><dt>Owner</dt><dd id="detailOwner">-</dd><dt>Note</dt><dd id="detailNote">-</dd></dl>
    </aside>
  </section>
  <section class="section">
    <h2>Requirements Notes</h2>
    <table><thead><tr><th>Node</th><th>Status note</th><th>Last comment</th><th>Open subtasks</th></tr></thead><tbody>{notes_html}</tbody></table>
  </section>
  <section class="section">
    <h2>Relationships</h2>
    <table><thead><tr><th>Source</th><th>Relation</th><th>Target</th></tr></thead><tbody>{edge_rows}</tbody></table>
  </section>
  <script>
    const nodes = Array.from(document.querySelectorAll('.map-node'));
    const title = document.getElementById('detailTitle');
    const text = document.getElementById('detailText');
    const status = document.getElementById('detailStatus');
    const owner = document.getElementById('detailOwner');
    const note = document.getElementById('detailNote');
    function showNode(node) {{
      title.textContent = node.querySelector('span').textContent;
      text.textContent = node.dataset.detail || '';
      status.textContent = node.dataset.status || '';
      owner.textContent = node.dataset.owner || '';
      note.textContent = node.dataset.note || '-';
      nodes.forEach(item => item.classList.toggle('selected', item === node));
    }}
    nodes.forEach(node => {{
      node.addEventListener('mouseenter', () => showNode(node));
      node.addEventListener('focus', () => showNode(node));
      node.addEventListener('click', () => showNode(node));
    }});
    function applyFilters() {{
      const group = document.getElementById('groupFilter').value;
      const needle = document.getElementById('textFilter').value.toLowerCase();
      nodes.forEach(node => {{
        const haystack = (node.textContent + ' ' + node.dataset.detail).toLowerCase();
        const visible = (!group || node.dataset.group === group) && (!needle || haystack.includes(needle));
        node.hidden = !visible;
      }});
    }}
    document.getElementById('groupFilter').addEventListener('change', applyFilters);
    document.getElementById('textFilter').addEventListener('input', applyFilters);
  </script>
"""
    return render_page_shell(text_for(lang, "function_graph"), body, lang, extra_style=FUNCTION_MAP_STYLE)


def render_page(lang: str = "en") -> str:
    return render_all_sites_page(lang)

    state = load_state()
    batch = Path(state["latest_batch"]) if state.get("latest_batch") else None
    rows = candidate_summary(batch) if batch and batch.exists() else []
    row_html = []
    for idx, row in enumerate(rows):
        supported = is_supported_submit_row(row)
        approved = row.get("approved_to_submit") == "true" and row.get("final_submit_allowed") == "true"
        checkbox = (
            f"<input type='checkbox' name='row' value='{idx}' {'checked' if approved else ''}>"
            if supported
            else "<span class='muted'>review only</span>"
        )
        row_html.append(
            "<tr>"
            f"<td>{checkbox}</td>"
            f"<td>{html.escape(row.get('company', ''))}</td>"
            f"<td>{html.escape(row.get('title', ''))}</td>"
            f"<td><a href='{html.escape(row.get('url', ''))}' target='_blank'>open</a></td>"
            f"<td>{html.escape(row.get('site', ''))}</td>"
            f"<td>{html.escape(row.get('recommendation', ''))}</td>"
            f"<td>{html.escape(row.get('score', ''))}</td>"
            f"<td>{'yes' if approved else 'no'}</td>"
            "</tr>"
        )
    rows_html = "\n".join(row_html) or "<tr><td colspan='8'>No batch yet.</td></tr>"

    log_rows = []
    for item in log_tail(SUBMISSION_LOG):
        log_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('attempted_at', '')))}</td>"
            f"<td>{html.escape(str(item.get('company', '')))}</td>"
            f"<td>{html.escape(str(item.get('result', '')))}</td>"
            f"<td>{html.escape(str(item.get('blocked_reason', '')))}</td>"
            f"<td>{html.escape(str(item.get('source_url', '')))}</td>"
            "</tr>"
        )
    log_html = "\n".join(log_rows) or "<tr><td colspan='5'>No submission log yet.</td></tr>"

    cfg = settings()
    model = html.escape(cfg.agent_model)
    locations = html.escape(cfg.location_preferences)
    batch_label = html.escape(str(batch or "none"))
    status = html.escape(state.get("status", "idle"))
    last_pid = html.escape(str(state.get("last_send_pid", "") or "none"))
    last_send_log = html.escape(state.get("last_send_log", "") or "none")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Job Apply Automation</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2933; }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    button, .button {{ padding: 8px 12px; border: 1px solid #8aa0b2; background: #f7fafc; border-radius: 6px; cursor: pointer; color: #1f2933; text-decoration: none; display: inline-block; }}
    button.primary, .button.primary {{ background: #1f7a4d; color: white; border-color: #1f7a4d; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 18px; }}
    th, td {{ text-align: left; padding: 9px 8px; border-bottom: 1px solid #d9e2ec; vertical-align: top; }}
    .bar {{ display: flex; gap: 8px; margin: 18px 0; flex-wrap: wrap; }}
    .muted {{ color: #66788a; }}
    .status {{ padding: 10px 12px; background: #eef4f8; border: 1px solid #bcccdc; border-radius: 6px; margin: 12px 0; }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Job Apply Automation</h1>
      <div class="muted">Agent model: {model} | locations: {locations} | latest batch: {batch_label}</div>
    </div>
  </header>

  <form class="bar" method="post" action="/scan">
    <button class="primary" type="submit">Підібрати свіжі вакансії + Djinni inbox</button>
    <button type="submit" formaction="/scan-inbox">Оновити тільки Djinni inbox</button>
    <button type="submit" formaction="/profile-on">Увімкнути профіль Djinni</button>
  </form>

  <form class="bar" method="post" action="/bot-note">
    <button type="submit">Підготувати Telegram bot status</button>
    <a class="button" href="/platform-progress">Platform progress graph</a>
  </form>

  <div class="status">
    <b>Status:</b> {status}<br>
    <b>Last send PID:</b> {last_pid}<br>
    <b>Last send log:</b> {last_send_log}
  </div>

  <form method="post" action="/save-approvals">
    <div class="bar">
      <button type="submit">Зберегти галочки</button>
      <button type="submit" formaction="/approve-all">Підтвердити всі Djinni</button>
      <button type="submit" formaction="/clear-approvals">Зняти всі</button>
      <button type="submit" formaction="/send" class="primary">Зробити розсилку approved CSV</button>
    </div>
    <table>
      <thead><tr><th>OK</th><th>Company</th><th>Title</th><th>URL</th><th>Site</th><th>Recommendation</th><th>Score</th><th>Approved</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </form>

  <h2>Submission Log Tail</h2>
  <table>
    <thead><tr><th>Time</th><th>Company</th><th>Result</th><th>Blocked reason</th><th>URL</th></tr></thead>
    <tbody>{log_html}</tbody>
  </table>
</main>
</body>
</html>"""


def render_platform_progress_page() -> str:
    refresh_unresolved_blockers()
    snapshot = build_progress_snapshot(limit=120).to_dict()
    nodes = snapshot.get("nodes", [])
    edges = snapshot.get("edges", [])
    events = snapshot.get("event_tail", [])

    counts: dict[str, int] = {}
    for node in nodes:
        status = str(node.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1

    def node_card(node: dict) -> str:
        node_id = html.escape(str(node.get("id", "")))
        label = html.escape(str(node.get("label", "")))
        kind = html.escape(str(node.get("kind", "")))
        status = html.escape(str(node.get("status", "pending")))
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        url = str(data.get("source_url") or "")
        score = data.get("score", "")
        meta_parts = [kind]
        if score not in {"", None}:
            meta_parts.append(f"score {score}")
        details = ""
        if node.get("kind") == "blocker":
            reason = str(data.get("blocked_reason") or "")
            category = str(data.get("category") or "")
            options = data.get("resolution_options") if isinstance(data.get("resolution_options"), list) else []
            option_text = " | ".join(str(item.get("label", "")) for item in options[:2] if isinstance(item, dict))
            details = (
                f"<p><b>{html.escape(category)}</b>: {html.escape(reason[:220])}</p>"
                f"<p>{html.escape(option_text)}</p>"
            )
        link = ""
        if url.startswith(("http://", "https://")):
            link = f"<a href='{html.escape(url)}' target='_blank' rel='noreferrer'>open</a>"
        return (
            f"<article class='node status-{status}'>"
            f"<div class='node-top'><code>{node_id}</code><span>{status}</span></div>"
            f"<h3>{label}</h3>"
            f"<p>{html.escape(' | '.join(str(part) for part in meta_parts if part))}</p>"
            f"{details}"
            f"{link}"
            "</article>"
        )

    pipeline_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "pipeline_stage")
    job_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "job")
    draft_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "outreach_draft")
    blocker_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "blocker")
    if not job_html:
        job_html = "<p class='empty'>No shared job rows yet.</p>"
    if not draft_html:
        draft_html = "<p class='empty'>No outreach drafts yet.</p>"
    if not blocker_html:
        blocker_html = "<p class='empty'>No unresolved blockers yet.</p>"

    edge_rows = []
    for edge in edges[:80]:
        edge_rows.append(
            "<tr>"
            f"<td>{html.escape(str(edge.get('source', '')))}</td>"
            f"<td>{html.escape(str(edge.get('target', '')))}</td>"
            f"<td>{html.escape(str(edge.get('label', '')))}</td>"
            f"<td>{html.escape(str(edge.get('status', '')))}</td>"
            "</tr>"
        )
    edge_html = "\n".join(edge_rows) or "<tr><td colspan='4'>No edges yet.</td></tr>"

    event_rows = []
    for event in events[:40]:
        event_rows.append(
            "<tr>"
            f"<td>{html.escape(str(event.get('created_at', '')))}</td>"
            f"<td>{html.escape(str(event.get('event_type', '')))}</td>"
            f"<td>{html.escape(str(event.get('platform', '')))}</td>"
            f"<td>{html.escape(str(event.get('job_id', '')))}</td>"
            f"<td>{html.escape(event_summary(event))}</td>"
            "</tr>"
        )
    event_html = "\n".join(event_rows) or "<tr><td colspan='5'>No events yet.</td></tr>"

    count_html = " ".join(
        f"<span class='pill status-{html.escape(status)}'>{html.escape(status)}: {count}</span>"
        for status, count in sorted(counts.items())
    ) or "<span class='pill'>empty</span>"

    generated_at = html.escape(str(snapshot.get("generated_at", "")))
    schema = html.escape(str(snapshot.get("schema", "")))
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Platform Progress Graph</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #193229; background: #f8fbf8; }}
    main {{ max-width: 1220px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    a.button {{ padding: 8px 12px; border: 1px solid #8fb6a2; background: #ffffff; border-radius: 6px; color: #145c39; text-decoration: none; }}
    .meta {{ color: #52675d; }}
    .counts {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 22px; }}
    .pill {{ display: inline-flex; padding: 5px 9px; border: 1px solid #b9d6c7; border-radius: 999px; background: #ffffff; font-size: 13px; }}
    .graph-row {{ display: grid; grid-template-columns: repeat(7, minmax(110px, 1fr)); gap: 10px; align-items: stretch; margin: 20px 0; }}
    .node {{ border: 1px solid #b9d6c7; background: #ffffff; border-radius: 8px; padding: 10px; min-height: 96px; box-shadow: 0 1px 2px rgba(21, 92, 57, 0.08); }}
    .node-top {{ display: flex; justify-content: space-between; gap: 8px; color: #52675d; font-size: 12px; }}
    .node h3 {{ font-size: 15px; line-height: 1.3; margin: 10px 0 6px; }}
    .node p {{ margin: 0 0 8px; color: #52675d; font-size: 13px; }}
    .status-complete {{ border-color: #42a66a; background: #edf9f0; }}
    .status-ready {{ border-color: #2f8a55; background: #e3f5ea; }}
    .status-active, .status-seen {{ border-color: #7fb98f; background: #f1f8f2; }}
    .status-blocked, .status-error {{ border-color: #bf6b6b; background: #fff1f1; }}
    .section {{ margin-top: 28px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; background: #ffffff; }}
    th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #d7e5dc; vertical-align: top; }}
    .empty {{ color: #52675d; }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Platform Progress Graph</h1>
      <div class="meta">Schema: {schema} | generated: {generated_at}</div>
    </div>
    <a class="button" href="/">Back to batches</a>
  </header>
  <div class="counts">{count_html}</div>
  <section class="section">
    <h2>Pipeline Gates</h2>
    <div class="graph-row">{pipeline_html}</div>
  </section>
  <section class="section">
    <h2>Jobs</h2>
    <div class="cards">{job_html}</div>
  </section>
  <section class="section">
    <h2>Outreach Drafts</h2>
    <div class="cards">{draft_html}</div>
  </section>
  <section class="section">
    <h2>Unresolved Blockers</h2>
    <div class="cards">{blocker_html}</div>
  </section>
  <section class="section">
    <h2>Edges</h2>
    <table><thead><tr><th>Source</th><th>Target</th><th>Label</th><th>Status</th></tr></thead><tbody>{edge_html}</tbody></table>
  </section>
  <section class="section">
    <h2>Event Tail</h2>
    <table><thead><tr><th>Time</th><th>Type</th><th>Platform</th><th>Job</th><th>Summary</th></tr></thead><tbody>{event_html}</tbody></table>
  </section>
</main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        lang = lang_from_request(self.path)
        if path == "/":
            self.respond_html(render_page(lang))
            return
        if path == "/sent-applications":
            self.respond_html(render_sent_applications_page(lang))
            return
        if path == "/function-map":
            self.respond_html(render_function_map_page(lang))
            return
        if path == "/function-map.json":
            self.respond_json(function_graph_with_notes())
            return
        if path == "/platform-progress":
            self.respond_html(render_platform_progress_page())
            return
        if path == "/platform-progress.json":
            self.respond_json(build_progress_snapshot(limit=120).to_dict())
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        lang = lang_from_request(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace")) if length else {}
        if path == "/scan":
            summary = run_all_sources_scan(limit=10, max_pages=1)
            batch = Path(str(summary.get("batch", "")))
            state = load_state()
            state["latest_batch"] = str(batch)
            state["status"] = format_scan_summary(summary)
            state["last_send_jobs"] = []
            save_state(state)
            self.redirect("/")
            return
        if path == "/scan-inbox":
            state = load_state()
            state["status"] = scan_inbox_status(execute_profile_toggle=False)
            save_state(state)
            self.redirect("/")
            return
        if path == "/profile-on":
            state = load_state()
            state["status"] = scan_inbox_status(execute_profile_toggle=True)
            save_state(state)
            self.redirect("/")
            return
        if path in {"/save-approvals", "/approve-all", "/clear-approvals"}:
            state = load_state()
            batch = Path(state["latest_batch"]) if state.get("latest_batch") else None
            if not batch or not batch.exists():
                self.send_error(400, "No latest batch")
                return
            if path == "/approve-all":
                changed, skipped = update_batch_approvals(batch, approve_all=True)
                state["status"] = f"approved all supported + Djinni inbox review rows: {changed}; blocked rows: {skipped}"
            elif path == "/clear-approvals":
                changed, skipped = update_batch_approvals(batch, selected=set())
                state["status"] = f"cleared approvals; blocked/review-only rows: {skipped}"
            else:
                selected = {int(v) for v in form.get("row", []) if str(v).isdigit()}
                changed, skipped = update_batch_approvals(batch, selected=selected)
                state["status"] = f"saved selected approvals: {changed}; blocked/review-only rows: {skipped}"
            save_state(state)
            self.redirect("/")
            return
        if path in {"/send-dry-run", "/prepare", "/send"}:
            state = load_state()
            batch_text = state.get("latest_batch")
            if not batch_text:
                self.send_error(400, "No latest batch")
                return
            batch = Path(batch_text)
            if not batch.exists():
                self.send_error(400, "Latest batch does not exist")
                return
            approved_count = count_approved_rows(batch)
            if approved_count == 0:
                state["status"] = "run blocked: no approved supported rows"
                save_state(state)
                self.redirect("/")
                return
            mode = {"send-dry-run": "dry-run", "prepare": "prepare", "send": "submit"}[path.lstrip("/")]
            jobs = launch_approved_runs(batch, mode)
            if not jobs:
                state["status"] = "run blocked: approved rows did not match supported site validators"
                state["last_send_jobs"] = []
            else:
                rows_started = sum(int(job["rows"]) for job in jobs)
                state["status"] = f"{mode} started for {rows_started} approved row(s) across {len(jobs)} site job(s)"
                state["last_send_jobs"] = jobs
            save_state(state)
            self.redirect("/")
            return
        if path == "/bot-note":
            note = ROOT / "data" / "job_waves" / "telegram_bot_status.txt"
            note.write_text("Telegram bot skeleton is ready. Run manually: python src\\job_apply_telegram_bot.py\n", encoding="utf-8")
            self.redirect("/")
            return
        if path == "/function-map/notes":
            try:
                save_function_graph_note(
                    (form.get("node_id") or [""])[0],
                    status_note=(form.get("status_note") or [""])[0],
                    comment=(form.get("comment") or [""])[0],
                    subtask=(form.get("subtask") or [""])[0],
                )
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            target_lang = normalize_lang((form.get("lang") or [lang])[0])
            self.redirect(url_with_lang("/function-map", target_lang))
            return
        self.send_error(404)

    def respond_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond_json(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, target: str) -> None:
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()


def main() -> int:
    host = "127.0.0.1"
    port = 8097
    print(f"Serving http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
