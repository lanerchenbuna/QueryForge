"""Local report confinement, expiry and reversible operator cleanup."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from uuid import uuid4


class ReportStore:
    def __init__(self, root: str | Path, retention_days: int = 30):
        if not 1 <= retention_days <= 3650:
            raise ValueError("Report retention must be between 1 and 3650 days")
        self.root = Path(root).expanduser().resolve()
        self.retention_days = retention_days

    def paths(self, run_id: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id or ""):
            raise ValueError("run_id contains unsafe path characters")
        paths = (self.root / f"{run_id}.html", self.root / f"{run_id}.manifest.json")
        if any(path.is_symlink() or path.resolve().parent != self.root for path in paths):
            raise ValueError("Report path escapes the configured directory")
        return paths

    def report_path(self, run_id: str, *, now: float | None = None) -> Path:
        path, _ = self.paths(run_id)
        if not path.is_file():
            raise ValueError(f"Report does not exist for run_id {run_id!r}")
        if path.stat().st_mtime <= (time.time() if now is None else now) - self.retention_days * 86400:
            raise ValueError(f"Report expired for run_id {run_id!r}")
        return path

    def expire(self, *, dry_run: bool = True, now: float | None = None) -> dict:
        """Quarantine expired HTML/manifest pairs; never delete retained evidence."""
        cutoff = (time.time() if now is None else now) - self.retention_days * 86400
        expired, skipped = [], []
        for html in sorted(self.root.glob("*.html")):
            try:
                paths = self.paths(html.stem)
                if html.stat().st_mtime > cutoff:
                    continue
                if not dry_run:
                    quarantine = self.root / ".expired"
                    if quarantine.is_symlink():
                        raise ValueError("Unsafe report quarantine directory")
                    destination = quarantine / f"{html.stem}_{uuid4().hex}"
                    destination.mkdir(parents=True)
                    for path in paths:
                        if path.is_file():
                            os.replace(path, destination / path.name)
                expired.append(html.stem)
            except (OSError, ValueError) as exc:
                skipped.append({"run_id": html.stem, "reason": str(exc)})
        return {"dry_run": dry_run, "expired": expired, "skipped": skipped, "action": "preview" if dry_run else "quarantined"}
