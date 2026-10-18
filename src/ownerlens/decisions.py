"""Local record of what owners answered. A JSON file next to where you run the tool; never touches AWS."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .models import Decision

DEFAULT_PATH = "decisions.json"


def load(path: str | Path = DEFAULT_PATH) -> dict[str, Decision]:
    p = Path(path)
    if not p.exists():
        return {}
    return {d["resource_id"]: Decision(**d) for d in json.loads(p.read_text())}


PENDING_DELETE = "delete-ok-after-backup"  # owner agreed, on condition that a backup exists first
KINDS = ("keep", PENDING_DELETE)


def is_active(d: Optional[Decision], today: Optional[date] = None) -> bool:
    """An answer silences the resource until its review date, then it comes back for a fresh one."""
    if d is None or d.decision not in KINDS:
        return False
    return date.fromisoformat(d.review_after) > (today or date.today())


def record(path: str | Path, resource_id: str, decision: str, by: str, reason: str,
           review_days: int = 90, today: Optional[date] = None) -> Decision:
    today = today or date.today()
    existing = load(path)
    d = Decision(resource_id, decision, by, reason, today.isoformat(),
                 (today + timedelta(days=review_days)).isoformat())
    existing[resource_id] = d  # latest answer wins
    Path(path).write_text(json.dumps([asdict(x) for x in existing.values()], indent=2) + "\n")
    return d
