from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import decisions as decisions_store
from . import llm, notify, report
from .backup import format_check, verify_instance_backup
from .guard import read_only_session
from .models import Finding
from .providers import azure, gcp
from .providers.cli_runner import CliMissing
from .reports import ScanMeta, write_bundle
from .scanner import default_regions, scan, scan_resources


def _add_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", choices=["aws", "gcp", "azure"], default="aws")
    p.add_argument("--profile", help="AWS profile name")
    p.add_argument("--regions", nargs="+", help="AWS regions to scan (default: all enabled regions)")
    p.add_argument("--project", help="GCP project id (default: gcloud's active project)")
    p.add_argument("--subscription", help="Azure subscription id or name (default: az's active subscription)")
    p.add_argument("--lookback-days", type=int, default=14, help="CloudWatch window for utilization (AWS)")
    p.add_argument("--min-score", type=int, default=40, help="hide resources scoring below this")
    p.add_argument("--no-cloudtrail", action="store_true", help="skip creator lookup (AWS; much faster)")
    p.add_argument("--decisions", default=decisions_store.DEFAULT_PATH,
                   help="file with recorded owner answers (default: ./decisions.json, ignored if missing)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ownerlens", description="Find cloud resources nobody owns. Read-only.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="inventory, infer owners, score unused resources")
    _add_scan_args(s)
    s.add_argument("--format", choices=["table", "json"], default="table")
    s.add_argument("--out-dir", help="also write the full report bundle (summary.md, csv, json, owner drafts, runbook) here")
    s.add_argument("--explain", action="store_true", help="print the evidence behind each finding")

    d = sub.add_parser("decide", help="record an owner's answer locally (does not touch any cloud)")
    d.add_argument("resource_id")
    d.add_argument("--decision", choices=list(decisions_store.KINDS), default="keep",
                   help="keep, or delete-ok-after-backup (owner agreed on condition a backup exists; nothing is deleted)")
    d.add_argument("--by", required=True, help="who answered")
    d.add_argument("--reason", required=True)
    d.add_argument("--review-days", type=int, default=90, help="ask again after this many days")
    d.add_argument("--file", default=decisions_store.DEFAULT_PATH)

    v = sub.add_parser("verify-backup", help="read-only: check an instance's volumes have completed EBS snapshots")
    v.add_argument("instance_id", nargs="?", help="omit with --pending to check everyone who agreed to 'delete after backup'")
    v.add_argument("--pending", action="store_true", help="check every instance in decisions.json awaiting backup")
    v.add_argument("--decisions", default=decisions_store.DEFAULT_PATH)
    v.add_argument("--profile")
    v.add_argument("--region", default="ap-south-1")

    n = sub.add_parser("notify", help="message owners by Slack or email (dry run unless --send)")
    _add_scan_args(n)
    n.add_argument("--channel", choices=["slack", "email"], required=True)
    n.add_argument("--people", required=True, help="people.json mapping owner -> {slack, email}")
    n.add_argument("--send", action="store_true", help="actually deliver; without it nothing leaves this machine")
    n.add_argument("--yes", action="store_true", help="skip the interactive 'type send' confirmation")

    m = sub.add_parser("summarize", help="AI-written executive summary from findings.json (opt-in, sends data to the Claude API)")
    m.add_argument("--from", dest="source", default="reports/findings.json")
    m.add_argument("--out", default="reports/executive_summary.md")
    m.add_argument("--model", default=llm.DEFAULT_MODEL)
    m.add_argument("--show-payload", action="store_true", help="print exactly what would be sent, then stop")
    m.add_argument("--send-to-llm", action="store_true", help="actually call the API (needs ANTHROPIC_API_KEY or `ant auth login`)")
    return p


def _collect(args) -> tuple[list[Finding], ScanMeta]:
    decs = decisions_store.load(args.decisions)
    if args.provider == "aws":
        session = read_only_session(profile_name=args.profile)  # refuses any non-read AWS call
        regions = args.regions or default_regions(session)
        print(f"Scanning {len(regions)} AWS region(s), read-only...", file=sys.stderr)
        findings = scan(session, regions, args.lookback_days, not args.no_cloudtrail, args.min_score, decs)
        account = session.client("sts").get_caller_identity()["Account"]
        return findings, ScanMeta(account=account, regions=regions, lookback_days=args.lookback_days,
                                  cloudtrail=not args.no_cloudtrail)
    print(f"Scanning {args.provider}, read-only, via its CLI...", file=sys.stderr)
    if args.provider == "gcp":
        resources, account = gcp.collect(args.project), f"gcp:{args.project or 'active project'}"
    else:
        resources, account = azure.collect(args.subscription), f"azure:{args.subscription or 'active subscription'}"
    findings = scan_resources(resources, args.min_score, decs)
    return findings, ScanMeta(account=account, regions=sorted({r.region for r in resources}), cloudtrail=False)


def _cmd_scan(args) -> int:
    findings, meta = _collect(args)
    if args.out_dir:
        for path in write_bundle(findings, meta, args.out_dir):
            print(f"wrote {path}", file=sys.stderr)
    if args.format == "json":
        print(report.to_json(findings))
    else:
        print(report.to_table(findings))
        if args.explain:
            print()
            for f in findings:
                print(report.explain(f), end="\n\n")
    return 0


def _cmd_notify(args) -> int:
    findings, _ = _collect(args)
    deliveries = notify.plan(findings, notify.load_people(args.people), args.channel)
    print(notify.format_plan(deliveries, args.channel, args.send))
    if not deliveries or not args.send:
        return 0
    todo = [d for d in deliveries if d.status == "planned"]
    if not todo:
        print("\nNobody has a contact in people.json for this channel; nothing to send.")
        return 1
    if not args.yes and input(f"\nSend {len(todo)} message(s) to the people above? Type 'send' to confirm: ").strip() != "send":
        print("Cancelled; nothing was sent.")
        return 1
    if args.channel == "slack":
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            print("SLACK_BOT_TOKEN is not set.", file=sys.stderr)
            return 2
        notify.send_slack(deliveries, token)
    else:
        notify.send_email(deliveries)
    print("\n" + notify.format_plan(deliveries, args.channel, True))
    return 0 if all(d.status in ("sent",) or d.status.startswith("skipped") for d in deliveries) else 1


def _cmd_summarize(args) -> int:
    findings = json.loads(Path(args.source).read_text())
    payload = llm.build_payload(findings)
    if args.show_payload:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if not args.send_to_llm:
        size = len(json.dumps(payload, default=str)) / 1024
        print(f"DRY RUN: would send {payload['count']} findings ({size:.1f} KB: ids, types, regions, costs, reasons, "
              f"owner guesses, Name tags, owner answers) to the Claude API using {args.model}.\n"
              f"  --show-payload   print exactly what would be sent\n"
              f"  --send-to-llm    do it")
        return 0
    try:
        body = llm.summarize(findings, model=args.model)
    except llm.LLMUnavailable as exc:
        print(f"Could not write the AI summary: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(llm.wrap_markdown(body, args.model))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def _cmd_verify_backup(args) -> int:
    session = read_only_session(profile_name=args.profile)
    if args.pending:
        ids = [d.resource_id for d in decisions_store.load(args.decisions).values()
               if d.decision == decisions_store.PENDING_DELETE and d.resource_id.startswith("i-")]
    elif args.instance_id:
        ids = [args.instance_id]
    else:
        print("give an instance id or --pending", file=sys.stderr)
        return 2
    checks = [verify_instance_backup(session, args.region, i) for i in ids]
    for c in checks:
        print(format_check(c), end="\n\n")
    if len(checks) > 1:
        print(f"SUMMARY: {sum(c.ok for c in checks)}/{len(checks)} instances fully backed up")
    return 0 if checks and all(c.ok for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "decide":
        d = decisions_store.record(args.file, args.resource_id, args.decision, args.by, args.reason, args.review_days)
        print(f"recorded: {d.resource_id} -> {d.decision} by {d.by}, review after {d.review_after} ({args.file})")
        return 0
    try:
        return {"scan": _cmd_scan, "notify": _cmd_notify, "summarize": _cmd_summarize,
                "verify-backup": _cmd_verify_backup}[args.command](args)
    except CliMissing as exc:
        print(f"{exc}. Install and log in to it first.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
