from __future__ import annotations

from ..models import Resource
from .base import tags_to_dict

# USD per GB-month, us-east-1 list price. Only used for a rough "waste" estimate.
GB_MONTH_PRICE = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}


def volume_cost(size_gb: int, volume_type: str) -> float:
    return round(size_gb * GB_MONTH_PRICE.get(volume_type, 0.10), 2)


def collect(session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out: list[Resource] = []
    for page in ec2.get_paginator("describe_volumes").paginate():
        for vol in page["Volumes"]:
            size = vol["Size"]
            vtype = vol.get("VolumeType", "gp3")
            out.append(Resource(
                id=vol["VolumeId"],
                type="ebs:volume",
                region=region,
                created_at=vol.get("CreateTime"),
                tags=tags_to_dict(vol.get("Tags")),
                state=vol["State"],  # available == attached to nothing
                monthly_cost_usd=volume_cost(size, vtype),
                details={
                    "size_gb": size,
                    "volume_type": vtype,
                    "attached_to": [a["InstanceId"] for a in vol.get("Attachments", [])],
                },
            ))
    return out
