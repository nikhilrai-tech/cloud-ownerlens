from __future__ import annotations

import json
from dataclasses import asdict

from . import decisions as decisions_store
from .models import Finding


def _owner_cell(f: Finding) -> str:
    if not f.owner.owner:
        return "UNKNOWN"
    flag = " !conflict" if f.owner.conflict else ""
    return f"{f.owner.owner} ({f.owner.confidence:.0%}){flag}"


def to_json(findings: list[Finding]) -> str:
    return json.dumps([asdict(f) for f in findings], indent=2, default=str)


def to_table(findings: list[Finding]) -> str:
    if not findings:
        return "No suspicious resources found."
    rows = [("SCORE", "ACTION", "RISK", "TYPE", "ID", "REGION", "$/MO", "OWNER (confidence)")]
    for f in findings:
        r, z = f.resource, f.zombie
        cost = f"{r.monthly_cost_usd:.2f}" if r.monthly_cost_usd is not None else "?"
        label = "kept" if f.decision and f.decision.decision == "keep" else "pending-backup"
        action = (f"{label} until {f.decision.review_after}" if decisions_store.is_active(f.decision) else z.action)
        rows.append((str(z.score), action, z.risk, r.type, r.id, r.region, cost, _owner_cell(f)))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))

    known = sum(f.resource.monthly_cost_usd or 0 for f in findings)
    unowned = sum(1 for f in findings if not f.owner.owner)
    lines += ["", f"{len(findings)} suspicious resources, {unowned} with no inferable owner.",
              f"Estimated cost where known: ${known:,.2f}/month (on-demand list prices; RDS instance-hours not included)."]
    return "\n".join(lines)


def explain(f: Finding) -> str:
    r = f.resource
    lines = [f"{r.type} {r.id} ({r.region}), score {f.zombie.score}, action: {f.zombie.action}"]
    lines += [f"  - {reason}" for reason in f.zombie.reasons]
    if f.owner.evidence:
        lines.append(f"  owner guess: {_owner_cell(f)}")
        lines += [f"    * {e.detail} (weight {e.weight})" for e in f.owner.evidence]
    else:
        lines.append("  owner guess: none (no owner/team tag and no CloudTrail creator found; CloudTrail only covers 90 days)")
    if f.owner.conflict:
        lines.append(f"  CONFLICT: {f.owner.conflict}")
    if f.owner.hints:
        lines.append("  clues: " + "; ".join(f.owner.hints))
    return "\n".join(lines)
