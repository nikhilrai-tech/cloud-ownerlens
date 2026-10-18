"""Read-only check: does every EBS volume of an instance have a completed snapshot?"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class VolumeBackup:
    volume_id: str
    size_gb: int
    latest_snapshot: Optional[str]
    snapshot_time: Optional[datetime]
    completed: bool


@dataclass
class BackupCheck:
    instance_id: str
    state: str
    volumes: list[VolumeBackup]

    @property
    def ok(self) -> bool:
        return bool(self.volumes) and all(v.completed for v in self.volumes)


def verify_instance_backup(session, region: str, instance_id: str) -> BackupCheck:
    ec2 = session.client("ec2", region_name=region)
    inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    vol_ids = [m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if m.get("Ebs")]
    sizes = {v["VolumeId"]: v["Size"] for v in ec2.describe_volumes(VolumeIds=vol_ids)["Volumes"]} if vol_ids else {}

    out = []
    for vid in vol_ids:
        snaps = []
        for page in ec2.get_paginator("describe_snapshots").paginate(
                OwnerIds=["self"], Filters=[{"Name": "volume-id", "Values": [vid]}]):
            snaps += page["Snapshots"]
        snaps.sort(key=lambda s: s["StartTime"], reverse=True)
        latest = snaps[0] if snaps else None
        out.append(VolumeBackup(vid, sizes.get(vid, 0), latest["SnapshotId"] if latest else None,
                                latest["StartTime"] if latest else None,
                                bool(latest) and latest["State"] == "completed"))
    return BackupCheck(instance_id, inst["State"]["Name"], out)


def format_check(check: BackupCheck, stopped_since: Optional[datetime] = None) -> str:
    lines = [f"Backup check for {check.instance_id} (state: {check.state})"]
    for v in check.volumes:
        if v.completed:
            lines.append(f"  OK       {v.volume_id} ({v.size_gb} GB): {v.latest_snapshot} at {v.snapshot_time:%Y-%m-%d %H:%M} UTC")
        elif v.latest_snapshot:
            lines.append(f"  PENDING  {v.volume_id} ({v.size_gb} GB): {v.latest_snapshot} is not completed yet")
        else:
            lines.append(f"  MISSING  {v.volume_id} ({v.size_gb} GB): no snapshot found")
    lines.append("")
    lines.append("RESULT: every volume has a completed snapshot." if check.ok
                 else "RESULT: NOT backed up as EBS snapshots. Do not delete yet.")
    lines.append("Note: this only sees EBS snapshots in this account/region. A backup copied elsewhere "
                 "(S3, laptop, another account) cannot be verified here; ask the owner to confirm it.")
    return "\n".join(lines)
