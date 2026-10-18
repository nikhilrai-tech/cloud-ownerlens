"""Azure collectors using the az CLI (your existing login). Read-only; not yet run against a real subscription."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from ..models import Resource
from .cli_runner import run_json

DISK_GB_MONTH = {"Standard_LRS": 0.04, "StandardSSD_LRS": 0.075, "Premium_LRS": 0.135, "UltraSSD_LRS": 0.12}
STATIC_PUBLIC_IP_MONTHLY = 3.65


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cmd(*parts: str, subscription: Optional[str]) -> list[str]:
    return ["az", *parts, "-o", "json"] + (["--subscription", subscription] if subscription else [])


def collect(subscription: Optional[str] = None, runner: Optional[Callable[..., Any]] = None) -> list[Resource]:
    out: list[Resource] = []

    for d in run_json(_cmd("disk", "list", subscription=subscription), runner):
        if d.get("diskState") != "Unattached" or d.get("managedBy"):
            continue
        sku = (d.get("sku") or {}).get("name", "Standard_LRS")
        size = int(d.get("diskSizeGb") or d.get("diskSizeGB") or 0)
        out.append(Resource(
            id=d["name"], type="azure:disk", region=d.get("location", ""), created_at=_ts(d.get("timeCreated")),
            tags=d.get("tags") or {}, state="unattached",
            monthly_cost_usd=round(size * DISK_GB_MONTH.get(sku, 0.10), 2),
            details={"name": d["name"], "size_gb": size, "sku": sku, "resource_group": d.get("resourceGroup")}))

    for p in run_json(_cmd("network", "public-ip", "list", subscription=subscription), runner):
        if p.get("ipConfiguration") or p.get("natGateway"):
            continue  # attached to something
        out.append(Resource(
            id=p["name"], type="azure:public-ip", region=p.get("location", ""), tags=p.get("tags") or {},
            state="unassociated", monthly_cost_usd=STATIC_PUBLIC_IP_MONTHLY,
            details={"name": p["name"], "address": p.get("ipAddress"), "resource_group": p.get("resourceGroup")}))

    for v in run_json(_cmd("vm", "list", "-d", subscription=subscription), runner):
        power = (v.get("powerState") or "").lower()
        if power not in ("vm deallocated", "vm stopped"):
            continue
        out.append(Resource(
            id=v["name"], type="azure:vm", region=v.get("location", ""), tags=v.get("tags") or {}, state="stopped",
            details={"name": v["name"], "power_state": v.get("powerState"),
                     "deallocated": power == "vm deallocated", "resource_group": v.get("resourceGroup"),
                     "vm_size": (v.get("hardwareProfile") or {}).get("vmSize")}))
    return out
