from __future__ import annotations

import json
import time
from typing import Optional

from botocore.exceptions import ClientError

from ..models import Resource

# CloudTrail LookupEvents only covers the last 90 days and is limited to ~2 requests/sec.
CREATE_EVENTS = {
    "ec2:instance": {"RunInstances"},
    "ebs:volume": {"CreateVolume", "RunInstances"},  # root volumes are created by RunInstances
    "s3:bucket": {"CreateBucket"},
    "ec2:eip": {"AllocateAddress"},
    "ec2:nat-gateway": {"CreateNatGateway"},
    "elb:load-balancer": {"CreateLoadBalancer"},
    "rds:instance": {"CreateDBInstance", "RestoreDBInstanceFromDBSnapshot", "CreateDBInstanceReadReplica"},
    "ebs:snapshot": {"CreateSnapshot", "CreateSnapshots"},
}
THROTTLE_SECONDS = 0.55
MAX_PAGES = 3


def principal_from_identity(ui: dict) -> tuple[Optional[str], bool]:
    """Turn a CloudTrail userIdentity into (name, is_automation).

    SSO / federated humans show up as AssumedRole with an email-like session name.
    Pipelines (terraform-ci, GitHub OIDC roles, autoscaling) are flagged as automation:
    still useful evidence ("look in the terraform repo"), but not a person to ask.
    """
    kind = ui.get("type")
    if kind == "IAMUser":
        return ui.get("userName"), False
    if kind == "AssumedRole":
        parts = ui.get("arn", "").split("/")
        role = parts[1] if len(parts) > 2 else ""
        session = parts[-1]
        human = "@" in session or role.startswith("AWSReservedSSO")
        return (session, False) if human else (f"{role}/{session}", True)
    if kind == "Root":
        return "root", False
    if kind == "AWSService":
        return ui.get("invokedBy"), True
    return ui.get("arn"), True


def find_creator(ct, resource: Resource) -> tuple[Optional[str], bool]:
    wanted = CREATE_EVENTS.get(resource.type, set())
    oldest = None
    try:
        pages = ct.get_paginator("lookup_events").paginate(
            LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": resource.details.get("arn") or resource.id}],
            PaginationConfig={"MaxItems": 50 * MAX_PAGES, "PageSize": 50},
        )
        for page in pages:
            for ev in page.get("Events", []):
                if ev.get("EventName") in wanted:
                    if oldest is None or ev["EventTime"] < oldest["EventTime"]:
                        oldest = ev
    except ClientError:
        return None, False
    if oldest is None:
        return None, False
    detail = json.loads(oldest["CloudTrailEvent"])
    return principal_from_identity(detail.get("userIdentity", {}))


def enrich(session, resources: list[Resource]) -> None:
    """Fill resource.creator in place. One CloudTrail client per region."""
    clients: dict[str, object] = {}
    for res in resources:
        ct = clients.setdefault(res.region, session.client("cloudtrail", region_name=res.region))
        res.creator, res.creator_is_automation = find_creator(ct, res)
        time.sleep(THROTTLE_SECONDS)
