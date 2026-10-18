"""GCP collectors using the gcloud CLI (your existing login). Read-only; not yet run against a real project."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from ..models import Resource
from .cli_runner import run_json

# USD per GB-month, rough list prices for an estimate only
DISK_GB_MONTH = {"pd-standard": 0.04, "pd-balanced": 0.10, "pd-ssd": 0.17, "pd-extreme": 0.125}
UNUSED_STATIC_IP_MONTHLY = 7.30


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tail(url: Optional[str]) -> str:
    return (url or "").rsplit("/", 1)[-1]


def _cmd(*parts: str, project: Optional[str]) -> list[str]:
    return ["gcloud", *parts, "--format=json"] + ([f"--project={project}"] if project else [])


def collect(project: Optional[str] = None, runner: Optional[Callable[..., Any]] = None) -> list[Resource]:
    out: list[Resource] = []

    for d in run_json(_cmd("compute", "disks", "list", project=project), runner):
        if d.get("users"):
            continue  # attached
        dtype = _tail(d.get("type"))
        size = int(d.get("sizeGb", 0))
        out.append(Resource(
            id=d["name"], type="gcp:disk", region=_tail(d.get("zone")) or _tail(d.get("region")),
            created_at=_ts(d.get("creationTimestamp")), tags=d.get("labels") or {}, state="unattached",
            monthly_cost_usd=round(size * DISK_GB_MONTH.get(dtype, 0.10), 2),
            details={"name": d["name"], "size_gb": size, "disk_type": dtype}))

    for a in run_json(_cmd("compute", "addresses", "list", project=project), runner):
        if a.get("addressType", "EXTERNAL") != "EXTERNAL" or a.get("status") != "RESERVED":
            continue  # internal, or actually in use
        out.append(Resource(
            id=a["name"], type="gcp:address", region=_tail(a.get("region")) or "global",
            created_at=_ts(a.get("creationTimestamp")), tags=a.get("labels") or {}, state="unassociated",
            monthly_cost_usd=UNUSED_STATIC_IP_MONTHLY, details={"name": a["name"], "address": a.get("address")}))

    for i in run_json(_cmd("compute", "instances", "list", project=project), runner):
        if i.get("status") != "TERMINATED":
            continue
        disk_gb = sum(int(x.get("diskSizeGb", 0)) for x in i.get("disks", []))
        out.append(Resource(
            id=i["name"], type="gcp:instance", region=_tail(i.get("zone")),
            created_at=_ts(i.get("creationTimestamp")), tags=i.get("labels") or {}, state="stopped",
            monthly_cost_usd=round(disk_gb * 0.10, 2) if disk_gb else None,  # disks only; type unknown here
            details={"name": i["name"], "machine_type": _tail(i.get("machineType")), "disk_gb": disk_gb,
                     "stopped_since": _ts(i.get("lastStopTimestamp"))}))
    return out
