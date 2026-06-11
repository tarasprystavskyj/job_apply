from __future__ import annotations

import csv
import html
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from djinni_inbox_scan import scan_inbox
from job_apply_config import ROOT, settings
from job_platforms.progress import build_progress_snapshot
from resume_index import build_resume_index
from vacancy_pipeline import build_candidate_batch, candidate_summary


STATE_PATH = ROOT / "data" / "job_waves" / "web_state.json"
SUBMISSION_LOG = ROOT / "data" / "job_waves" / "djinni_csv_submission_attempts.jsonl"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"latest_batch": "", "status": "idle"}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_supported_submit_row(row: dict[str, str]) -> bool:
    return (row.get("site") or "").strip().lower() == "djinni" and (row.get("url") or "").startswith("https://djinni.co/jobs/")


def update_batch_approvals(batch: Path, selected: set[int] | None = None, approve_all: bool = False) -> tuple[int, int]:
    with batch.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    changed = 0
    skipped = 0
    for idx, row in enumerate(rows):
        if not is_supported_submit_row(row):
            row["approved_to_submit"] = "false"
            row["final_submit_allowed"] = "false"
            skipped += 1
            continue
        should_approve = approve_all or (selected is not None and idx in selected)
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
    return sum(
        1
        for row in rows
        if is_supported_submit_row(row)
        and row.get("approved_to_submit") == "true"
        and row.get("final_submit_allowed") == "true"
    )


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


def render_page() -> str:
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
        link = ""
        if url.startswith(("http://", "https://")):
            link = f"<a href='{html.escape(url)}' target='_blank' rel='noreferrer'>open</a>"
        return (
            f"<article class='node status-{status}'>"
            f"<div class='node-top'><code>{node_id}</code><span>{status}</span></div>"
            f"<h3>{label}</h3>"
            f"<p>{html.escape(' | '.join(str(part) for part in meta_parts if part))}</p>"
            f"{link}"
            "</article>"
        )

    pipeline_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "pipeline_stage")
    job_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "job")
    draft_html = "\n".join(node_card(node) for node in nodes if node.get("kind") == "outreach_draft")
    if not job_html:
        job_html = "<p class='empty'>No shared job rows yet.</p>"
    if not draft_html:
        draft_html = "<p class='empty'>No outreach drafts yet.</p>"

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
        if path == "/":
            self.respond_html(render_page())
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
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace")) if length else {}
        if path == "/scan":
            build_resume_index()
            inbox_note = scan_inbox_status(execute_profile_toggle=False)
            batch = build_candidate_batch(limit=10)
            state = load_state()
            state["latest_batch"] = str(batch)
            state["status"] = f"built batch: {batch}; {inbox_note}"
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
                state["status"] = f"approved all supported Djinni rows: {changed}; skipped review-only rows: {skipped}"
            elif path == "/clear-approvals":
                changed, skipped = update_batch_approvals(batch, selected=set())
                state["status"] = f"cleared approvals; review-only rows skipped: {skipped}"
            else:
                selected = {int(v) for v in form.get("row", []) if str(v).isdigit()}
                changed, skipped = update_batch_approvals(batch, selected=selected)
                state["status"] = f"saved selected approvals: {changed}; review-only rows skipped: {skipped}"
            save_state(state)
            self.redirect("/")
            return
        if path == "/send":
            state = load_state()
            batch = state.get("latest_batch")
            if not batch:
                self.send_error(400, "No latest batch")
                return
            approved_count = count_approved_rows(Path(batch))
            if approved_count == 0:
                state["status"] = "send blocked: no approved Djinni rows"
                save_state(state)
                self.redirect("/")
                return
            send_log = ROOT / "data" / "job_waves" / "web_send_last.log"
            log_handle = send_log.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "src" / "djinni_csv_apply.py"),
                    "--csv",
                    batch,
                    "--execute",
                    "--i-understand-this-submits-applications",
                ],
                cwd=str(ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            state["status"] = f"send started for {approved_count} approved row(s); refresh this page and check Submission Log Tail"
            state["last_send_pid"] = proc.pid
            state["last_send_log"] = str(send_log)
            save_state(state)
            self.redirect("/")
            return
        if path == "/bot-note":
            note = ROOT / "data" / "job_waves" / "telegram_bot_status.txt"
            note.write_text("Telegram bot skeleton is ready. Run manually: python src\\job_apply_telegram_bot.py\n", encoding="utf-8")
            self.redirect("/")
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
