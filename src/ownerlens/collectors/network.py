"""Elastic IPs, NAT gateways, load balancers: cheap-looking resources that quietly bill 24/7."""
from __future__ import annotations

from botocore.exceptions import ClientError

from ..models import Resource
from .base import metric_points, tags_to_dict, window

EIP_MONTHLY = 3.65  # $0.005/h public IPv4 charge, applies whether attached or not since 2024; idle is pure waste
NAT_MONTHLY = 32.85  # $0.045/h, before data processing
LB_MONTHLY = 16.43  # $0.0225/h for ALB/NLB, before LCUs


def collect_eips(session, region: str) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    out = []
    for a in ec2.describe_addresses()["Addresses"]:
        associated = bool(a.get("AssociationId") or a.get("InstanceId") or a.get("NetworkInterfaceId"))
        out.append(Resource(
            id=a.get("AllocationId") or a["PublicIp"], type="ec2:eip", region=region,
            tags=tags_to_dict(a.get("Tags")), state="associated" if associated else "unassociated",
            monthly_cost_usd=EIP_MONTHLY, details={"public_ip": a.get("PublicIp")},
        ))
    return out


def collect_nat_gateways(session, region: str, lookback_days: int = 14) -> list[Resource]:
    ec2 = session.client("ec2", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    start, end = window(lookback_days)
    out = []
    for page in ec2.get_paginator("describe_nat_gateways").paginate():
        for nat in page["NatGateways"]:
            if nat["State"] != "available":
                continue
            res = Resource(
                id=nat["NatGatewayId"], type="ec2:nat-gateway", region=region,
                created_at=nat.get("CreateTime"), tags=tags_to_dict(nat.get("Tags")),
                state=nat["State"], monthly_cost_usd=NAT_MONTHLY,
                details={"vpc_id": nat.get("VpcId"), "subnet_id": nat.get("SubnetId")},
            )
            dims = [{"Name": "NatGatewayId", "Value": res.id}]
            sent = metric_points(cw, "AWS/NATGateway", "BytesOutToDestination", dims, "Sum", start, end)
            res.metrics["gb_per_day"] = (sum(sent) / max(len(sent), 1) / 1e9) if sent else 0.0
            out.append(res)
    return out


def collect_load_balancers(session, region: str) -> list[Resource]:
    elb = session.client("elbv2", region_name=region)
    out = []
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            arn = lb["LoadBalancerArn"]
            registered = 0
            try:
                for tg in elb.describe_target_groups(LoadBalancerArn=arn)["TargetGroups"]:
                    health = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                    registered += len(health["TargetHealthDescriptions"])
                tags = tags_to_dict(elb.describe_tags(ResourceArns=[arn])["TagDescriptions"][0].get("Tags"))
            except ClientError:
                tags = {}
                registered = -1  # unknown
            res = Resource(
                id=lb["LoadBalancerName"], type="elb:load-balancer", region=region,
                created_at=lb.get("CreatedTime"), tags=tags, state=lb["State"]["Code"],
                monthly_cost_usd=LB_MONTHLY,
                details={"arn": arn, "lb_type": lb.get("Type"), "scheme": lb.get("Scheme")},
            )
            if registered >= 0:
                res.metrics["registered_targets"] = registered
            out.append(res)
    return out
