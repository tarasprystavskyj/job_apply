from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from job_apply_config import ROOT, settings


SUPPORTED_EXT = {".pdf", ".txt", ".md"}
PRIVATE_INDEX = ROOT / "data" / "private" / "resume_index.json"


def list_resumes(resume_dir: Path | None = None) -> list[dict[str, Any]]:
    base = resume_dir or settings().resume_dir
    if not base.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(base.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXT:
            continue
        records.append(
            {
                "name": path.name,
                "path": str(path),
                "suffix": path.suffix.lower(),
                "size": path.stat().st_size,
                "mtime": int(path.stat().st_mtime),
                "tags": infer_tags(path.name),
                "text_excerpt": extract_text(path)[:4000],
            }
        )
    return records


def infer_tags(name: str) -> list[str]:
    low = name.lower()
    tags: list[str] = []
    for token in ["python", "ai", "automation", "business", "analyst", "crypto", "manager", "js"]:
        if token in low:
            tags.append(token)
    return tags or ["general"]


def build_resume_index(output: Path | None = None) -> Path:
    out = output or PRIVATE_INDEX
    data = {
        "schema": "job.resume_index.v0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "resume_dir": str(settings().resume_dir),
        "resumes": list_resumes(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def extract_text(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".pdf":
        try:
            import fitz  # PyMuPDF

            parts: list[str] = []
            with fitz.open(path) as doc:
                for page in doc[:8]:
                    parts.append(page.get_text("text"))
            return "\n".join(parts).strip()
        except Exception as exc:
            return f"[pdf_text_unavailable: {type(exc).__name__}]"
    return ""


if __name__ == "__main__":
    print(build_resume_index())
