"""Write the full report bundle to a directory. Files only: nothing is sent anywhere."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import decisions as decisions_store
from . import report as terminal_report
from .models import Finding

COVERED = "EC2 instances, EBS volumes, EBS snapshots, S3 buckets, Elastic IPs, NAT gateways, load balancers (ALB/NLB), RDS instances"
NOT_COVERED = "RDS instance-hours, Reserved/Savings Plan discounts, Lambda, ECS/EKS, DynamoDB, CloudFront, classic ELB, resources older than CloudTrail's 90 days for creator lookup"


@dataclass
class ScanMeta:
    account: str = "unknown"
    regions: list[str] = field(default_factory=list)
    lookback_days: int = 14
    cloudtrail: bool = True
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _cost(f: Finding) -> float:
    return f.resource.monthly_cost_usd or 0.0


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(c.replace("|", "/") for c in row) + " |" for row in rows]
    return "\n".join(out)


def _row(f: Finding) -> list[str]:
    r = f.resource
    owner = f.owner.owner or "UNKNOWN"
    if f.owner.owner:
        owner += f" ({f.owner.confidence:.0%})" + (" ⚠ conflict" if f.owner.conflict else "")
    return [str(f.zombie.score), f.zombie.action, f.zombie.risk, r.type, f"`{r.id}`", r.region,
            _money(r.monthly_cost_usd) if r.monthly_cost_usd is not None else "?", owner]


HEADERS = ["Score", "Action", "Risk", "Type", "ID", "Region", "$/mo", "Owner guess"]


def _split(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    acknowledged = [f for f in findings if decisions_store.is_active(f.decision)]
    return [f for f in findings if f not in acknowledged], acknowledged


def _kept_sections(all_findings: list[Finding], open_: list[Finding], acknowledged: list[Finding]) -> list[str]:
    pending = [f for f in acknowledged if f.decision.decision == decisions_store.PENDING_DELETE]
    acknowledged = [f for f in acknowledged if f.decision.decision == "keep"]
    out = []
    if pending:
        out += ["## Owner agreed to deletion, once a backup exists", "",
                "Nothing has been deleted. A person must (1) get the owner's confirmation that the backup is done, "
                "(2) run `ownerlens verify-backup <id>` and see RESULT: every volume has a completed snapshot, "
                "(3) delete it themselves.", "",
                _md_table(["ID", "Owner", "Said", "Review after", "$/mo"], [
                    [f"`{f.resource.id}`", f.decision.by, f.decision.reason, f.decision.review_after, _money(_cost(f))]
                    for f in pending]), ""]
    out += ["## Kept on purpose", ""]
    if acknowledged:
        out += [_md_table(["ID", "Type", "Answered by", "Reason", "Review after", "$/mo"], [
            [f"`{f.resource.id}`", f.resource.type, f.decision.by, f.decision.reason, f.decision.review_after,
             _money(_cost(f))] for f in acknowledged]), ""]
    else:
        out += ["_No answers recorded yet. Record one with `ownerlens decide <id> --by <person> --reason ...`._", ""]
    due = [f for f in open_ if f.decision and f.decision.decision in decisions_store.KINDS]
    if due:
        out += ["**Review due** (the 'keep' has expired, so these are back in the lists above):", ""]
        out += [f"- `{f.resource.id}`: kept by {f.decision.by} until {f.decision.review_after}" for f in due] + [""]

    savers = [f for f in all_findings if f.zombie.suggestion]
    out += ["## Cheaper ways to keep stopped instances", ""]
    if savers:
        out += ["For instances that are intentionally kept: copy the disks into snapshots and remove the idle volumes, "
                "so you pay for used blocks only. This report does not do it; it needs the owner's yes.", "",
                _md_table(["ID", "Owner guess", "Status", "Now $/mo", "Saves up to $/mo"], [
                    [f"`{f.resource.id}`", f.owner.owner or "UNKNOWN",
                     "kept on purpose" if decisions_store.is_active(f.decision) else "open",
                     _money(_cost(f)), _money(f.zombie.savings_usd or 0)] for f in
                    sorted(savers, key=lambda x: -(x.zombie.savings_usd or 0))]),
                "", f"Total possible: **{_money(sum(f.zombie.savings_usd or 0 for f in savers))}/month** "
                    f"(conservative: snapshots are priced at the full volume size).", ""]
    else:
        out += ["_None._", ""]
    return out


def summary_md(all_findings: list[Finding], meta: ScanMeta) -> str:
    findings, acknowledged = _split(all_findings)
    by_action: dict[str, list[Finding]] = defaultdict(list)
    by_type: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_action[f.zombie.action].append(f)
        by_type[f.resource.type].append(f)
    total = sum(_cost(f) for f in findings)
    unowned = [f for f in findings if not f.owner.owner]
    conflicts = [f for f in findings if f.owner.conflict]
    old_stopped = [f for f in findings if f.resource.details.get("stopped_since")
                   and (datetime.now(timezone.utc) - f.resource.details["stopped_since"]).days > 90]

    lines = [
        "# ownerlens: Cloud waste & ownership report", "",
        f"- **Account:** {meta.account}",
        f"- **Generated:** {meta.generated_at:%Y-%m-%d %H:%M} UTC",
        f"- **Regions scanned:** {len(meta.regions)} ({', '.join(meta.regions)})",
        f"- **Utilization window:** {meta.lookback_days} days; CloudTrail creator lookup: {'on' if meta.cloudtrail else 'off'}",
        "- **Mode:** read-only. Nothing was stopped, deleted, modified or tagged.", "",
        "## Headline", "",
        f"- **{len(findings)}** resources look unused or abandoned.",
        f"- **{len(unowned)}** of them have no inferable owner; **{len(conflicts)}** have conflicting owner evidence.",
        f"- Estimated cost of these resources: **{_money(total)}/month** "
        f"(on-demand list prices: EC2 compute for running instances, EBS, S3 storage, EIP, NAT, ELB, snapshots; RDS instance-hours are not included, so real spend may be higher).",
        f"- {len(old_stopped)} stopped instance(s) have been stopped for more than 90 days.",
        f"- {len(acknowledged)} more resource(s) ({_money(sum(map(_cost, acknowledged)))}/month) already have an owner "
        f"answer (kept on purpose, or agreed to deletion after a backup) and are listed separately below.", "",
        "## By recommended action", "",
        _md_table(["Action", "Count", "$/mo", "Meaning"], [
            ["propose-cleanup", str(len(by_action["propose-cleanup"])), _money(sum(map(_cost, by_action["propose-cleanup"]))),
             "strong signals; still needs the owner's yes"],
            ["ask-owner", str(len(by_action["ask-owner"])), _money(sum(map(_cost, by_action["ask-owner"]))),
             "looks unused; confirm with a human first (always the case for prod)"],
            ["keep", str(len(by_action["keep"])), _money(sum(map(_cost, by_action["keep"]))), "below threshold"],
        ]), "",
        "## By resource type", "",
        _md_table(["Type", "Count", "$/mo"], [
            [t, str(len(fs)), _money(sum(map(_cost, fs)))] for t, fs in sorted(by_type.items(), key=lambda kv: -sum(map(_cost, kv[1])))
        ] or [["-", "0", "$0.00"]]), "",
        "## Top 15 by score, then cost", "",
        _md_table(HEADERS, [_row(f) for f in findings[:15]]) if findings else "_Nothing suspicious found._", "",
    ]
    lines += _kept_sections(all_findings, findings, acknowledged)
    lines += [
        "## What was and was not checked", "",
        f"- **Covered:** {COVERED}.",
        f"- **Not covered:** {NOT_COVERED}.",
        "- **Owner inference** is a guess: tags, CloudTrail creator (last 90 days) and people named in Name tags. "
        "Creator is not always owner; conflicts are flagged.", "",
        "## Other files in this bundle", "",
        "- `findings.csv` / `findings.json`: every finding with full evidence",
        "- `unowned.md`: resources with no owner guess, with clues for triage",
        "- `owner_requests.md`: one draft message per person (drafts only, nothing was sent)",
        "- `runbook.md`: step-by-step hand-over for owners who agreed to 'delete after backup'",
    ]
    return "\n".join(lines) + "\n"


def unowned_md(findings: list[Finding]) -> str:
    items = [f for f in findings if not f.owner.owner]
    lines = ["# Resources with no inferable owner", "",
             "No owner/team tag, no known person in the Name tag, and no CloudTrail creator in the last 90 days.",
             "Use the clues (Name tag, key pair, IAM profile, VPC) to find whoever knows these.", ""]
    if not items:
        return "\n".join(lines + ["_None: every finding has an owner guess._"]) + "\n"
    for f in items:
        r = f.resource
        lines += [f"## `{r.id}` ({r.type}, {r.region})", "",
                  f"- Score {f.zombie.score}, action **{f.zombie.action}**, risk {f.zombie.risk}, "
                  f"cost {_money(r.monthly_cost_usd) + '/mo' if r.monthly_cost_usd is not None else 'unknown'}"]
        lines += [f"- {reason}" for reason in f.zombie.reasons]
        if f.owner.hints:
            lines.append("- Clues: " + "; ".join(f.owner.hints))
        lines.append("")
    return "\n".join(lines)


def owner_message(owner: str, fs: list[Finding]) -> str:
    """The plain-text message for one person. Used for the draft file and for notify."""
    total = sum(map(_cost, fs))
    lines = [f"Hi {owner.split('@')[0]},", "",
             f"A cloud-cost review found {len(fs)} resource(s) that look unused and are probably yours "
             f"(about {_money(total)}/month where priced). Nothing has been touched.", ""]
    for f in fs:
        r = f.resource
        why = f.zombie.reasons[0] if f.zombie.reasons else "looks idle"
        warn = f"  [NOTE: {f.owner.conflict}]" if f.owner.conflict else ""
        lines.append(f"  - {r.type} {r.id} ({r.region}): {why}{warn}")
    lines += ["", "For each one, please reply with:",
              "  KEEP        (and add an `Owner` tag so it stops showing up here)",
              "  STOP-OK     (fine to stop it; you can restart later)",
              "  DELETE-OK   (fine to delete; take a snapshot first if it holds data)",
              "  NOT-MINE    (tell us who it belongs to)", "", "Thanks!"]
    return "\n".join(lines)


def open_owner_groups(findings: list[Finding]) -> dict[str, list[Finding]]:
    """Findings still waiting for an answer, grouped by inferred owner (biggest spender first)."""
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.owner.owner and not decisions_store.is_active(f.decision):
            groups[f.owner.owner].append(f)
    return dict(sorted(groups.items(), key=lambda kv: -sum(map(_cost, kv[1]))))


def owner_requests_md(findings: list[Finding]) -> str:
    groups = open_owner_groups(findings)
    lines = ["# Draft messages to owners", "",
             "Drafts only. Review before sending; ownerlens never sends or changes anything itself "
             "(`ownerlens notify` sends only with --send).", ""]
    if not groups:
        return "\n".join(lines + ["_No owners could be inferred, or everyone has already answered._"]) + "\n"
    for owner, fs in groups.items():
        lines += [f"## To: {owner}", "", "```text", owner_message(owner, fs), "```", ""]
    return "\n".join(lines)


def runbook_md(findings: list[Finding]) -> str:
    """Hand-over note for the people who agreed to 'delete after backup'. Text only: it does not run anything."""
    pending = [f for f in findings if f.decision and f.decision.decision == decisions_store.PENDING_DELETE]
    lines = ["# Runbook: backup first, then (a person) deletes", "",
             "Everything below is done by people. ownerlens only reads and reports; it never deletes or snapshots.", ""]
    if not pending:
        return "\n".join(lines + ["_No owner has agreed to 'delete after backup' yet._"]) + "\n"

    total_gb = sum(f.resource.details.get("ebs_gb") or 0 for f in pending)
    lines += [f"{len(pending)} instance(s), {total_gb:,} GB of volumes in total. "
              f"Full snapshots cost up to ~{_money(total_gb * 0.05)}/month (upper bound; snapshots store used blocks only, "
              f"and are incremental). Owners can instead copy just the files they need.", "",
              "## Steps", "",
              "1. **Owner:** decide what is important and back it up (EBS snapshot of the volumes, or copy the files elsewhere).",
              "2. **Owner:** tell the team in writing that the backup is done and where it is.",
              "3. **Anyone:** run `ownerlens verify-backup --pending` and check the instance shows "
              "`RESULT: every volume has a completed snapshot`. (A backup stored outside EBS can't be verified by the tool, "
              "so rely on the owner's written confirmation.)",
              "4. **A person with access:** delete the instance and, separately, its leftover volumes, through your usual "
              "change process. Volumes are not recovered once deleted.",
              "5. Re-run `ownerlens scan --out-dir reports` and confirm the instance is gone.", "",
              "If nothing happens, each answer expires on its review date and the instance returns to the open list.", "",
              "## Instances", ""]
    for f in sorted(pending, key=lambda x: -(x.resource.details.get("ebs_gb") or 0)):
        r, d = f.resource, f.decision
        vols = r.details.get("ebs_volume_ids") or []
        lines += [f"### `{r.id}` ({r.region}), owner: {d.by}", "",
                  f"- Name tag: {r.tags.get('Name', '-')}; stopped since "
                  f"{r.details['stopped_since']:%Y-%m-%d}" if r.details.get("stopped_since") else f"- Name tag: {r.tags.get('Name', '-')}",
                  f"- Answer: \"{d.reason}\" (review after {d.review_after})",
                  f"- Volumes: {r.details.get('ebs_gb', '?')} GB total, about {_money(_cost(f))}/month today"]
        if vols:
            lines += ["- To snapshot a volume (owner, if that is the chosen backup):", "", "  ```bash"]
            lines += [f"  aws ec2 create-snapshot --region {r.region} --volume-id {v} "
                      f"--description \"backup before deleting {r.id}\"" for v in vols]
            lines += ["  ```"]
        lines.append("")
    return "\n".join(lines)


def findings_csv(findings: list[Finding], path: Path) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "action", "risk", "type", "id", "region", "state", "monthly_cost_usd",
                    "owner", "owner_confidence", "conflict", "creator", "decision", "review_after",
                    "suggestion", "reasons", "clues"])
        for f in findings:
            r = f.resource
            w.writerow([f.zombie.score, f.zombie.action, f.zombie.risk, r.type, r.id, r.region, r.state or "",
                        "" if r.monthly_cost_usd is None else r.monthly_cost_usd,
                        f.owner.owner or "", f.owner.confidence, f.owner.conflict or "", r.creator or "",
                        f.decision.decision if f.decision else "", f.decision.review_after if f.decision else "",
                        f.zombie.suggestion or "", " | ".join(f.zombie.reasons), " | ".join(f.owner.hints)])


def write_bundle(findings: list[Finding], meta: ScanMeta, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "summary.md": summary_md(findings, meta),
        "unowned.md": unowned_md(findings),
        "owner_requests.md": owner_requests_md(findings),
        "runbook.md": runbook_md(findings),
        "findings.json": terminal_report.to_json(findings),
    }
    paths = []
    for name, text in files.items():
        p = out / name
        p.write_text(text)
        paths.append(p)
    csv_path = out / "findings.csv"
    findings_csv(findings, csv_path)
    paths.append(csv_path)
    return paths
