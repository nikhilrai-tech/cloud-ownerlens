from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Resource:
    id: str
    type: str  # "ec2:instance" | "ebs:volume" | "s3:bucket"
    region: str
    created_at: Optional[datetime] = None
    tags: dict[str, str] = field(default_factory=dict)
    state: Optional[str] = None
    monthly_cost_usd: Optional[float] = None  # rough estimate, None if unknown
    metrics: dict[str, float] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    # filled by the CloudTrail enrichment step
    creator: Optional[str] = None
    creator_is_automation: bool = False


@dataclass
class Evidence:
    signal: str  # e.g. "tag:owner", "cloudtrail:creator"
    owner: str
    weight: float  # 0..1, how much this signal alone is trusted
    detail: str


@dataclass
class OwnerGuess:
    owner: Optional[str]
    confidence: float  # 0..1
    evidence: list[Evidence] = field(default_factory=list)
    # clues that do not name an owner but narrow the search (Name tag, key pair, IAM profile, VPC...)
    hints: list[str] = field(default_factory=list)
    # set when two different people each look likely (e.g. creator != person named in the Name tag)
    conflict: Optional[str] = None


@dataclass
class ZombieScore:
    score: int  # 0..100, higher = more likely unused
    risk: str  # "low" | "medium" | "high" - blast radius if we are wrong
    reasons: list[str] = field(default_factory=list)
    action: str = "keep"  # "keep" | "ask-owner" | "propose-cleanup"
    # a cheaper way to keep the resource, when one exists (never an instruction to delete)
    suggestion: Optional[str] = None
    savings_usd: Optional[float] = None


@dataclass
class Decision:
    """A human's answer, recorded locally. 'keep' means: known and intentional, ask again at review_after."""
    resource_id: str
    decision: str
    by: str
    reason: str
    decided_at: str  # ISO date
    review_after: str  # ISO date


@dataclass
class Finding:
    resource: Resource
    owner: OwnerGuess
    zombie: ZombieScore
    decision: Optional[Decision] = None
