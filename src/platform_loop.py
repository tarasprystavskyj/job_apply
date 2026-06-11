from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from job_platforms.orchestrator import main as orchestrator_main


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--once" in args:
        args.remove("--once")
    return orchestrator_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
