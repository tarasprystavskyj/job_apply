from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FunctionNode:
    id: str
    label: str
    group: str
    shape: str
    status: str
    detail: str
    owner: str = "system"


@dataclass(frozen=True)
class FunctionEdge:
    source: str
    target: str
    label: str


def function_graph() -> dict[str, object]:
    nodes = [
        FunctionNode(
            "goal",
            "MVP job application copilot",
            "product",
            "diamond",
            "active",
            "Single local system that discovers relevant jobs, prepares tailored messages, asks the owner for approval, and submits only through approved gated flows.",
        ),
        FunctionNode(
            "source_scan",
            "All-sites scan",
            "discovery",
            "box",
            "active",
            "Refreshes every configured source: Djinni inbox/recruiter responses, DOU public listings, Work.ua public listings, Robota.ua public listings, then rebuilds the candidate CSV.",
        ),
        FunctionNode(
            "djinni",
            "Djinni",
            "platform",
            "box",
            "active",
            "Reads approved public job rows, scans owner-visible inbox, detects profile blockers, and supports gated final application submit.",
        ),
        FunctionNode(
            "workua",
            "Work.ua",
            "platform",
            "box",
            "active",
            "Discovers public vacancies, prepares review rows, supports resume/profile update work, and uses gated browser submit automation for approved exact URLs.",
        ),
        FunctionNode(
            "dou",
            "DOU",
            "platform",
            "box",
            "active",
            "Converts listings into exact vacancy URLs, drafts outreach, and routes exact approved rows through the DOU CSV adapter.",
        ),
        FunctionNode(
            "robotaua",
            "Robota.ua",
            "platform",
            "box",
            "active",
            "Discovers public vacancies and handles the live apply form with explicit resume upload and cover-letter gates.",
        ),
        FunctionNode(
            "resume_index",
            "Resume index",
            "data",
            "cylinder",
            "active",
            "Maintains local resume metadata and tags for matching. Private content is not read or uploaded unless a bounded task and approval require it.",
        ),
        FunctionNode(
            "shared_db",
            "Shared job DB",
            "data",
            "cylinder",
            "active",
            "SQLite source of jobs, outreach drafts, status events, blockers, and progress graph rows shared by web and Telegram.",
        ),
        FunctionNode(
            "message_qa",
            "Message QA gate",
            "safety",
            "hex",
            "active",
            "Blocks low-quality cover letters: other people's names, leaked file names, agentic phrases, duplicated salutations, contaminated titles, or missing exact approval.",
        ),
        FunctionNode(
            "approval",
            "Approval gates",
            "safety",
            "hex",
            "active",
            "Every live send requires exact URL, exact message, salary/profile/resume policy, row approval, and final-submit permission.",
        ),
        FunctionNode(
            "blockers",
            "Blocker loop",
            "operations",
            "octagon",
            "active",
            "Classifies blockers, writes unresolved blocker artifacts, proposes manual and automatic resolution paths, and surfaces them in progress and Telegram.",
        ),
        FunctionNode(
            "web",
            "Web interface",
            "interface",
            "round",
            "active",
            "Local UI for scanning, reviewing, approving, preparing, sending, viewing sent applications, blockers, progress, and this function graph.",
        ),
        FunctionNode(
            "telegram",
            "Telegram bot",
            "interface",
            "round",
            "active",
            "Parallel owner interface for daily 10:00 scans, status, approvals, blocker notices, auto-reply notices, and all-site approved sends.",
        ),
        FunctionNode(
            "decision_telegram_batch_flow",
            "Decision: Telegram batch flow",
            "product",
            "diamond",
            "active",
            "Owner decision: Telegram should support a simple flow where /scan creates awareness and /approve_and_send_latest approves and sends all supported application rows. Web UI remains the detailed per-vacancy review surface. The product goal is to help applicants and recruiters meet in the middle: high-fit, user-aware applications instead of low-quality mass spam.",
            "owner",
        ),
        FunctionNode(
            "evidence_agentic_consent_flow",
            "Evidence: consent and recruiter trust",
            "safety",
            "hex",
            "active",
            "Decision evidence: agentic UX research favors intent previews before sensitive actions, human-in-the-loop pauses for high-impact operations, durable audit logs, and specific confirmation copy. Reddit/recruiter signals are mixed: applicants want time savings, recruiters dislike low-quality AI mass applications. Therefore batch send is allowed with awareness, supported application rows, logs, duplicate guards, and a web link for item-by-item review.",
            "research",
        ),
        FunctionNode(
            "progress",
            "Progress graph",
            "observability",
            "box",
            "active",
            "LangGraph-style progress view with stages, jobs, outreach drafts, blockers, edges, and recent events.",
        ),
        FunctionNode(
            "sent",
            "Sent applications",
            "observability",
            "box",
            "active",
            "Aggregates successful or post-submit-inferred application events across all platform logs.",
        ),
        FunctionNode(
            "testing",
            "Cross-testing",
            "quality",
            "hex",
            "active",
            "Changed functionality must be tested across affected flows before commit: unit tests, compile checks, browser UI smoke, and live-safe dry/prepare paths.",
        ),
        FunctionNode(
            "graph_tz",
            "Graph as ТЗ",
            "product",
            "diamond",
            "planned",
            "Evolve this function map into the main source of truth for requirements, owner decisions, blockers, and agent assignments.",
        ),
        FunctionNode(
            "graph_module_migration",
            "Graph module migration task",
            "product",
            "box",
            "planned",
            "Pending task for agent 019ebd34-fb58-7d23-be7f-cc456828deef: move reusable graph/task-tracking implementation to the upper-level standalone module/project, then keep this repository's graph as a project-specific instance of that module. Current resume attempts are blocked by active agent thread limit.",
            "graph-agent",
        ),
    ]
    edges = [
        FunctionEdge("goal", "source_scan", "discovers"),
        FunctionEdge("source_scan", "djinni", "refreshes"),
        FunctionEdge("source_scan", "workua", "refreshes"),
        FunctionEdge("source_scan", "dou", "refreshes"),
        FunctionEdge("source_scan", "robotaua", "refreshes"),
        FunctionEdge("source_scan", "resume_index", "matches"),
        FunctionEdge("source_scan", "shared_db", "persists"),
        FunctionEdge("shared_db", "web", "feeds"),
        FunctionEdge("shared_db", "telegram", "feeds"),
        FunctionEdge("telegram", "decision_telegram_batch_flow", "implements"),
        FunctionEdge("evidence_agentic_consent_flow", "decision_telegram_batch_flow", "supports"),
        FunctionEdge("decision_telegram_batch_flow", "approval", "defines batch gate"),
        FunctionEdge("shared_db", "progress", "feeds"),
        FunctionEdge("message_qa", "approval", "guards"),
        FunctionEdge("approval", "djinni", "permits final send"),
        FunctionEdge("approval", "workua", "permits final send"),
        FunctionEdge("approval", "dou", "permits final send"),
        FunctionEdge("approval", "robotaua", "permits final send"),
        FunctionEdge("blockers", "web", "surfaces"),
        FunctionEdge("blockers", "telegram", "notifies"),
        FunctionEdge("progress", "blockers", "tracks"),
        FunctionEdge("sent", "web", "reports"),
        FunctionEdge("testing", "web", "verifies"),
        FunctionEdge("testing", "telegram", "verifies"),
        FunctionEdge("graph_tz", "goal", "defines"),
        FunctionEdge("graph_tz", "graph_module_migration", "delegates"),
    ]
    return {
        "schema": "job.function_graph.v0",
        "nodes": [asdict(node) for node in nodes],
        "edges": [asdict(edge) for edge in edges],
        "groups": {
            "product": {"label": "Product / ТЗ", "color": "#256f55"},
            "discovery": {"label": "Discovery", "color": "#2f855a"},
            "platform": {"label": "Platforms", "color": "#3a9f71"},
            "data": {"label": "Data", "color": "#437f97"},
            "safety": {"label": "Safety gates", "color": "#b7791f"},
            "operations": {"label": "Operations", "color": "#c05621"},
            "interface": {"label": "Interfaces", "color": "#5a67d8"},
            "observability": {"label": "Observability", "color": "#718096"},
            "quality": {"label": "Quality", "color": "#805ad5"},
        },
    }
