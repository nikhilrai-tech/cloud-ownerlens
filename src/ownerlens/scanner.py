from __future__ import annotations

from .collectors import cloudtrail, data, ebs, ec2, iam, network, s3
from .inference import infer_owner
from .models import Decision, Finding, Resource
from .pricing import PriceBook
from .scoring import score_resource


def scan_resources(resources: list[Resource], min_score: int = 0,
                   decisions: dict[str, Decision] | None = None,
                   people: frozenset[str] = frozenset()) -> list[Finding]:
    """Score + infer owners for resources collected elsewhere (GCP/Azure). No cloud calls here."""
    decisions = decisions or {}
    scored = [(r, score_resource(r)) for r in resources]
    findings = [Finding(r, infer_owner(r, people), z, decisions.get(r.id))
                for r, z in scored if z.score >= max(min_score, 1)]
    findings.sort(key=lambda f: (f.zombie.score, f.resource.monthly_cost_usd or 0), reverse=True)
    return findings


def default_regions(session) -> list[str]:
    ec2c = session.client("ec2", region_name="us-east-1")
    return sorted(r["RegionName"] for r in ec2c.describe_regions()["Regions"])


def scan(session, regions: list[str], lookback_days: int = 14,
         use_cloudtrail: bool = True, min_score: int = 0,
         decisions: dict[str, Decision] | None = None) -> list[Finding]:
    resources: list[Resource] = []
    prices = PriceBook(session)  # EC2 compute price; any lookup that fails just leaves cost unknown
    for region in regions:
        resources += ec2.collect(session, region, lookback_days, prices)
        resources += ebs.collect(session, region)
        resources += network.collect_eips(session, region)
        resources += network.collect_nat_gateways(session, region, lookback_days)
        resources += network.collect_load_balancers(session, region)
        resources += data.collect_rds(session, region, lookback_days)
        resources += data.collect_snapshots(session, region)
    resources += [b for b in s3.collect(session) if b.region in regions]

    # CloudTrail is slow (rate limited), so only look up resources that already look unused.
    scored = [(r, score_resource(r)) for r in resources]
    scored = [(r, z) for r, z in scored if z.score >= max(min_score, 1)]
    decisions = decisions or {}
    if use_cloudtrail:  # also for kept resources: the creator is still our best owner evidence
        cloudtrail.enrich(session, [r for r, _ in scored])

    people = frozenset(iam.list_people(session))
    findings = [Finding(r, infer_owner(r, people), z, decisions.get(r.id)) for r, z in scored]
    findings.sort(key=lambda f: (f.zombie.score, f.resource.monthly_cost_usd or 0), reverse=True)
    return findings
