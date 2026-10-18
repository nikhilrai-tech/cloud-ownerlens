"""On-demand EC2 compute price lookup via the AWS Pricing API (read-only, served from us-east-1)."""
from __future__ import annotations

import json
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError

HOURS_PER_MONTH = 730

# The Pricing API filters by location *name*, not region code.
REGION_NAMES = {
    "us-east-1": "US East (N. Virginia)", "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)", "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)", "sa-east-1": "South America (Sao Paulo)",
    "eu-west-1": "EU (Ireland)", "eu-west-2": "EU (London)", "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)", "eu-north-1": "EU (Stockholm)", "eu-south-1": "EU (Milan)",
    "ap-south-1": "Asia Pacific (Mumbai)", "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)", "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)", "ap-northeast-3": "Asia Pacific (Osaka)",
}


class PriceBook:
    """Caches lookups; returns None whenever the price can't be determined (never guesses)."""

    def __init__(self, session):
        self._client = session.client("pricing", region_name="us-east-1")
        self._cache: dict[tuple[str, str, str], Optional[float]] = {}

    def hourly(self, instance_type: str, region: str, os_name: str = "Linux") -> Optional[float]:
        key = (instance_type, region, os_name)
        if key not in self._cache:
            self._cache[key] = self._fetch(instance_type, region, os_name)
        return self._cache[key]

    def monthly(self, instance_type: str, region: str, os_name: str = "Linux") -> Optional[float]:
        h = self.hourly(instance_type, region, os_name)
        return round(h * HOURS_PER_MONTH, 2) if h is not None else None

    def _fetch(self, instance_type: str, region: str, os_name: str) -> Optional[float]:
        location = REGION_NAMES.get(region)
        if not location:
            return None
        filters = {"instanceType": instance_type, "location": location, "operatingSystem": os_name,
                   "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used"}
        try:
            resp = self._client.get_products(
                ServiceCode="AmazonEC2", MaxResults=10,
                Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()])
            for raw in resp.get("PriceList", []):
                for term in json.loads(raw).get("terms", {}).get("OnDemand", {}).values():
                    for dim in term["priceDimensions"].values():
                        usd = float(dim["pricePerUnit"].get("USD", 0))
                        if usd > 0:
                            return usd
        except (ClientError, BotoCoreError, ValueError, KeyError):
            return None
        return None


def os_for_instance(platform_details: Optional[str]) -> Optional[str]:
    """Only Linux and Windows have a plain on-demand price; anything else (RHEL, SQL...) is left unpriced."""
    text = (platform_details or "Linux/UNIX")
    if text.startswith("Linux/UNIX"):
        return "Linux"
    if text == "Windows":
        return "Windows"
    return None
