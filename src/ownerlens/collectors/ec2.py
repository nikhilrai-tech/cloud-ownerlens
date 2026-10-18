from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from ..models import Resource
from ..pricing import PriceBook, os_for_instance
from .base import metric_points, tags_to_dict, window
from .ebs import volume_cost

_TRANSITION_TS = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)")


def parse_stopped_since(reason: Optional[str]) -> Optional[datetime]:
    """StateTransitionReason looks like 'User initiated (2025-03-02 10:15:00 GMT)'."""
    m = _TRANSITION_TS.search(reason or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _volume_costs(ec2, instances: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
    """Monthly EBS cost and total GB per instance id, summed over its attached volumes."""
    vol_to_inst: dict[str, str] = {}
    for inst in instances:
        for bdm in inst.get("BlockDeviceMappings", []):
            vol_id = bdm.get("Ebs", {}).get("VolumeId")
            if vol_id:
                vol_to_inst[vol_id] = inst["InstanceId"]
    costs: dict[str, float] = {}
    gbs: dict[str, int] = {}
    ids = list(vol_to_inst)
    for i in range(0, len(ids), 100):
        for vol in ec2.describe_volumes(VolumeIds=ids[i:i + 100])["Volumes"]:
            inst_id = vol_to_inst[vol["VolumeId"]]
            costs[inst_id] = costs.get(inst_id, 0.0) + volume_cost(vol["Size"], vol.get("VolumeType", "gp3"))
            gbs[inst_id] = gbs.get(inst_id, 0) + vol["Size"]
    return costs, gbs


def collect(session, region: str, lookback_days: int = 14, prices: Optional[PriceBook] = None) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    start, end = window(lookback_days)

    instances: list[dict] = []
    for page in ec2.get_paginator("describe_instances").paginate():
        for reservation in page["Reservations"]:
            instances += [i for i in reservation["Instances"]
                          if i["State"]["Name"] not in ("terminated", "shutting-down")]
    ebs_costs, ebs_gbs = _volume_costs(ec2, instances)

    out: list[Resource] = []
    for inst in instances:
        state = inst["State"]["Name"]
        tags = tags_to_dict(inst.get("Tags"))
        profile_arn = (inst.get("IamInstanceProfile") or {}).get("Arn", "")
        res = Resource(
            id=inst["InstanceId"],
            type="ec2:instance",
            region=region,
            # LaunchTime resets on stop/start, so this is "last started", not "created"
            created_at=inst.get("LaunchTime"),
            tags=tags,
            state=state,
            # a stopped instance costs its EBS volumes; a running one costs compute, which we don't price yet
            monthly_cost_usd=round(ebs_costs[inst["InstanceId"]], 2) if inst["InstanceId"] in ebs_costs else None,
            details={
                "instance_type": inst.get("InstanceType"),
                "key_name": inst.get("KeyName"),
                "iam_profile": profile_arn.split("/")[-1] if profile_arn else None,
                "vpc_id": inst.get("VpcId"),
                "subnet_id": inst.get("SubnetId"),
                "image_id": inst.get("ImageId"),
                "ebs_gb": ebs_gbs.get(inst["InstanceId"]),
                "ebs_volume_ids": [m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if m.get("Ebs")],
                "stopped_since": parse_stopped_since(inst.get("StateTransitionReason")) if state == "stopped" else None,
            },
        )
        if state == "running" and prices is not None:
            os_name = os_for_instance(inst.get("PlatformDetails"))
            compute = prices.monthly(inst.get("InstanceType", ""), region, os_name) if os_name else None
            if compute is not None:
                res.details["compute_cost_usd"] = compute
                res.monthly_cost_usd = round((res.monthly_cost_usd or 0.0) + compute, 2)
        if state == "running":
            dims = [{"Name": "InstanceId", "Value": res.id}]
            avg = metric_points(cw, "AWS/EC2", "CPUUtilization", dims, "Average", start, end)
            mx = metric_points(cw, "AWS/EC2", "CPUUtilization", dims, "Maximum", start, end)
            net_in = metric_points(cw, "AWS/EC2", "NetworkIn", dims, "Sum", start, end)
            net_out = metric_points(cw, "AWS/EC2", "NetworkOut", dims, "Sum", start, end)
            if avg:
                res.metrics["cpu_avg"] = sum(avg) / len(avg)
                res.metrics["cpu_max"] = max(mx) if mx else max(avg)
            if net_in or net_out:
                days = max(len(net_in), len(net_out), 1)
                res.metrics["net_mb_per_day"] = (sum(net_in) + sum(net_out)) / days / 1e6
        out.append(res)
    return out
