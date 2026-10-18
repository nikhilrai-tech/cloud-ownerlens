from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def tags_to_dict(tags: Optional[list[dict]]) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in (tags or [])}


def window(lookback_days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=lookback_days), end


def metric_points(cw, namespace: str, metric: str, dims: list[dict], stat: str,
                  start: datetime, end: datetime, period: int = 86400) -> list[float]:
    """Datapoint values for one statistic; empty list when CloudWatch has nothing."""
    resp = cw.get_metric_statistics(
        Namespace=namespace, MetricName=metric, Dimensions=dims,
        StartTime=start, EndTime=end, Period=period, Statistics=[stat],
    )
    return [p[stat] for p in resp.get("Datapoints", [])]
