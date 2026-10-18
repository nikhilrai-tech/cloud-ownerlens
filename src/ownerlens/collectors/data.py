"""RDS instances and EBS snapshots."""
from __future__ import annotations

from ..models import Resource
from .base import metric_points, tags_to_dict, window

SNAPSHOT_GB_MONTH = 0.05  # upper bound: snapshots are incremental, real cost is usually lower
RDS_GB_MONTH = 0.115  # gp2/gp3 storage only, instance hours are not priced yet


def collect_rds(session, region: str, lookback_days: int = 14) -> list[Resource]:
    rds = session.client("rds", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    start, end = window(lookback_days)
    out = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            res = Resource(
                id=db["DBInstanceIdentifier"], type="rds:instance", region=region,
                created_at=db.get("InstanceCreateTime"),
                tags=tags_to_dict(db.get("TagList")), state=db["DBInstanceStatus"],
                details={"engine": db.get("Engine"), "class": db.get("DBInstanceClass"),
                         "storage_gb": db.get("AllocatedStorage"), "multi_az": db.get("MultiAZ")},
            )
            if db.get("AllocatedStorage"):
                res.monthly_cost_usd = round(db["AllocatedStorage"] * RDS_GB_MONTH, 2)
            if res.state == "available":
                dims = [{"Name": "DBInstanceIdentifier", "Value": res.id}]
                conns = metric_points(cw, "AWS/RDS", "DatabaseConnections", dims, "Maximum", start, end)
                if conns:
                    res.metrics["max_connections"] = max(conns)
            out.append(res)
    return out


def collect_snapshots(session, region: str) -> list[Resource]:
    """Snapshots whose source volume is gone and that no AMI uses."""
    ec2 = session.client("ec2", region_name=region)
    in_use_by_ami: set[str] = set()
    for img in ec2.describe_images(Owners=["self"])["Images"]:
        for bdm in img.get("BlockDeviceMappings", []):
            snap = bdm.get("Ebs", {}).get("SnapshotId")
            if snap:
                in_use_by_ami.add(snap)

    existing_volumes = {v["VolumeId"] for p in ec2.get_paginator("describe_volumes").paginate()
                        for v in p["Volumes"]}
    out = []
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            vol = s.get("VolumeId", "")
            source_gone = bool(vol) and vol != "vol-ffffffff" and vol not in existing_volumes
            out.append(Resource(
                id=s["SnapshotId"], type="ebs:snapshot", region=region,
                created_at=s.get("StartTime"), tags=tags_to_dict(s.get("Tags")),
                state=s["State"], monthly_cost_usd=round(s["VolumeSize"] * SNAPSHOT_GB_MONTH, 2),
                details={"size_gb": s["VolumeSize"], "source_volume": vol, "source_volume_gone": source_gone,
                         "used_by_ami": s["SnapshotId"] in in_use_by_ami,
                         "description": (s.get("Description") or "")[:80]},
            ))
    return out
