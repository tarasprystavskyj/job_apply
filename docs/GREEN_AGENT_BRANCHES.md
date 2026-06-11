# GREEN Agent Branch Rules

GREEN platform agents work in feature branches and keep live job actions gated.

## Branch Ownership

- Work only on the branch assigned to the agent.
- Keep edits inside the assigned scope. For GREEN Architect, that is
  orchestration, docs, and tests unless a tiny import change is unavoidable.
- Do not revert unrelated changes. Treat unexpected edits as another agent's or
  the owner's work.

## Required Checks

Every feature branch must run tests for touched functionality before commit.
For this orchestrator slice, the minimum safe checks are:

```powershell
python -m unittest discover -s tests
python -m compileall -q src
python src\job_platforms\orchestrator.py --help
python src\platform_loop.py --once --help
```

Subagent code modifications require the same rule: inspect the diff, run the
targeted tests for touched functionality, then commit only after tests pass.

## Live Action Gates

- Discovery, local normalization, scoring, review-only drafts, status rows, and
  progress snapshots are allowed.
- Submit, apply, send, upload, reply, reject, report-hire, profile-save, and
  account changes remain blocked unless the owner approves the exact vacancy,
  exact message, exact resume, and final action.
- Blanket approval is not enough for final job submissions.

## Blockers

Blockers should be surfaced to the user and Telegram where relevant. If a site
requires profile updates, include the profile update URL and offer to prepare a
draft, but do not save profile fields without exact final approval.

## GREEN Orchestrator

The minimal eMVP entry points are:

```powershell
python src\job_platforms\orchestrator.py --platform workua --query "Python AI" --db tmp\green.sqlite3
python src\job_platforms\orchestrator.py --platform workua --query "Python AI" --public-html "workua|tmp\workua.html|https://www.work.ua/jobs-python/" --snapshot-out tmp\progress.json
python src\platform_loop.py --once --platform dou --query "Python backend" --no-draft
```

The orchestrator is run-once by default. It does not watch, fetch private pages,
read browser state, upload resumes, or submit applications.
