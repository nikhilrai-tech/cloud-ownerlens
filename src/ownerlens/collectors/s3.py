from __future__ import annotations

from botocore.exceptions import ClientError

from ..models import Resource
from .base import metric_points, tags_to_dict, window

S3_STANDARD_GB_MONTH = 0.023


def collect(session, home_region: str = "us-east-1") -> list[Resource]:
    """Buckets are global to list, but metrics/tags live in the bucket's own region."""
    s3 = session.client("s3", region_name=home_region)
    out: list[Resource] = []
    start, end = window(3)  # S3 storage metrics are daily; a few days is enough for "current size"

    for b in s3.list_buckets().get("Buckets", []):
        name = b["Name"]
        try:
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except ClientError:
            loc = home_region
        res = Resource(id=name, type="s3:bucket", region=loc, created_at=b.get("CreationDate"))

        try:
            res.tags = tags_to_dict(s3.get_bucket_tagging(Bucket=name)["TagSet"])
        except ClientError:
            pass  # NoSuchTagSet / AccessDenied: treat as untagged

        cw = session.client("cloudwatch", region_name=loc)
        size = metric_points(cw, "AWS/S3", "BucketSizeBytes",
                             [{"Name": "BucketName", "Value": name},
                              {"Name": "StorageType", "Value": "StandardStorage"}],
                             "Average", start, end)
        count = metric_points(cw, "AWS/S3", "NumberOfObjects",
                              [{"Name": "BucketName", "Value": name},
                               {"Name": "StorageType", "Value": "AllStorageTypes"}],
                              "Average", start, end)
        if size:
            res.metrics["size_bytes"] = size[-1]
            res.monthly_cost_usd = round(size[-1] / 1e9 * S3_STANDARD_GB_MONTH, 2)
        if count:
            res.metrics["object_count"] = count[-1]
        out.append(res)
    return out
