from __future__ import annotations

from datetime import datetime, timezone

from .collectors.data import SNAPSHOT_GB_MONTH
from .models import Resource, ZombieScore

PROD_VALUES = {"prod", "production", "prd", "live"}
ENV_KEYS = ("env", "environment", "stage")

DATA_TYPES = {"s3:bucket", "rds:instance", "ebs:snapshot", "ebs:volume", "gcp:disk", "azure:disk"}

ASK_THRESHOLD = 40
CLEANUP_THRESHOLD = 70


def k8s_cluster(res: Resource) -> str | None:
    """Name of the Kubernetes/EKS cluster that owns this resource, from the tags the controllers add."""
    for key, value in res.tags.items():
        if key in ("elbv2.k8s.aws/cluster", "eks:cluster-name", "aws:eks:cluster-name"):
            return value
        if key.startswith("kubernetes.io/cluster/"):
            return key.split("/", 2)[2]
    return None


def _risk(res: Resource) -> str:
    env = next((v.lower() for k, v in res.tags.items() if k.lower() in ENV_KEYS), "")
    cluster = k8s_cluster(res)
    if env in PROD_VALUES or (cluster and any(p in cluster.lower() for p in PROD_VALUES | {"production"})):
        return "high"
    if res.type in DATA_TYPES:
        return "medium"  # deleting data is not reversible, whatever the env tag says
    return "low" if env else "medium"


def _age_days(res: Resource) -> float | None:
    """Days of inactivity-ish age: for stopped instances prefer 'stopped since' over LaunchTime."""
    since = res.details.get("stopped_since") or res.created_at
    if not since:
        return None
    return (datetime.now(timezone.utc) - since).total_seconds() / 86400


def score_resource(res: Resource) -> ZombieScore:
    score, reasons = 0, []

    if res.type == "ebs:volume":
        if res.state == "available":
            score += 80
            reasons.append("volume is not attached to any instance")
    elif res.type == "ec2:instance":
        if res.state == "stopped":
            score += 55
            since = res.details.get("stopped_since")
            when = f" since {since:%Y-%m-%d}" if since else ""
            reasons.append(f"instance is stopped{when} (still paying for its volumes)")
        elif res.state == "running":
            cpu_avg, cpu_max = res.metrics.get("cpu_avg"), res.metrics.get("cpu_max")
            if cpu_avg is not None and cpu_avg < 2 and (cpu_max or 0) < 10:
                score += 60
                reasons.append(f"CPU avg {cpu_avg:.1f}% / max {cpu_max:.1f}% over the lookback window")
            net = res.metrics.get("net_mb_per_day")
            if net is not None and net < 10:
                score += 20
                reasons.append(f"network ~{net:.1f} MB/day")
    elif res.type == "s3:bucket":
        count = res.metrics.get("object_count")
        if count == 0:
            score += 75
            reasons.append("bucket is empty")
        elif count is None:
            score += 15
            reasons.append("no storage metrics available, so activity is unknown")

    elif res.type in ("gcp:disk", "azure:disk"):
        if res.state == "unattached":
            score += 80
            reasons.append("disk is not attached to any VM")
    elif res.type in ("gcp:address", "azure:public-ip"):
        if res.state == "unassociated":
            score += 90
            reasons.append("reserved public IP is not attached to anything (billed while reserved)")
    elif res.type in ("gcp:instance", "azure:vm"):
        if res.state == "stopped":
            since = res.details.get("stopped_since")
            score += 55
            reasons.append("VM is stopped" + (f" since {since:%Y-%m-%d}" if since else "") + " (disks still billed)")
            if res.type == "azure:vm" and res.details.get("deallocated") is False:
                score += 15
                reasons.append("stopped but NOT deallocated: Azure still bills the compute for it")
    elif res.type == "ec2:eip":
        if res.state == "unassociated":
            score += 90
            reasons.append("Elastic IP is not associated with anything (billed hourly)")
    elif res.type == "ec2:nat-gateway":
        gb = res.metrics.get("gb_per_day")
        if gb is not None and gb < 0.01:
            score += 80
            reasons.append("NAT gateway carried no traffic in the lookback window")
        elif gb is not None and gb < 0.1:
            score += 40
            reasons.append(f"NAT gateway carried only {gb * 1000:.0f} MB/day")
    elif res.type == "elb:load-balancer":
        if res.metrics.get("registered_targets") == 0:
            score += 75
            reasons.append("load balancer has no registered targets")
    elif res.type == "rds:instance":
        if res.state == "stopped":
            score += 55
            reasons.append("database is stopped (AWS restarts it automatically after 7 days)")
        elif res.state == "available" and res.metrics.get("max_connections") == 0:
            score += 70
            reasons.append("zero client connections in the lookback window")
    elif res.type == "ebs:snapshot":
        d = res.details
        if d.get("source_volume_gone") and not d.get("used_by_ami"):
            score += 60
            reasons.append(f"source volume {d.get('source_volume')} no longer exists and no AMI uses this snapshot")

    age = _age_days(res)
    if score and age is not None and age > 90:
        score += 10
        what = "stopped for" if res.details.get("stopped_since") else "older than 90 days:"
        reasons.append(f"{what} {age:.0f}d")

    score = min(score, 100)
    risk = _risk(res)
    cluster = k8s_cluster(res)
    if cluster and score:
        # a controller owns this object: deleting it in AWS fights the controller or breaks a live ingress/service
        score = min(score, CLEANUP_THRESHOLD - 1)
        reasons.append(f"managed by Kubernetes cluster '{cluster}': change it through the cluster, never delete it in AWS")
    if risk == "high":  # never be pushy about prod, always a human decision
        score = min(score, CLEANUP_THRESHOLD - 1)
        if reasons:
            reasons.append("tagged production: capped at ask-owner")

    suggestion, savings = None, None
    gb = res.details.get("ebs_gb")
    if res.type == "ec2:instance" and res.state == "stopped" and gb and res.monthly_cost_usd:
        savings = round(res.monthly_cost_usd - gb * SNAPSHOT_GB_MONTH, 2)
        if savings >= 0.5:
            suggestion = (f"cheaper way to keep it: snapshot/AMI the {gb} GB of volumes and remove the volumes; "
                          f"saves up to ~${savings:.2f}/mo (usually more, snapshots only store used blocks). "
                          f"Restoring takes minutes. Needs the owner's yes.")
        else:
            suggestion, savings = None, None

    action = "propose-cleanup" if score >= CLEANUP_THRESHOLD else "ask-owner" if score >= ASK_THRESHOLD else "keep"
    return ZombieScore(score=score, risk=risk, reasons=reasons, action=action,
                       suggestion=suggestion, savings_usd=savings if suggestion else None)
