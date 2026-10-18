from __future__ import annotations

import re

from .models import Evidence, OwnerGuess, Resource

OWNER_TAG_KEYS = ("owner", "created-by", "createdby", "creator", "contact", "email")
TEAM_TAG_KEYS = ("team", "squad", "project", "service", "app", "application")

W_OWNER_TAG = 0.80
W_TEAM_TAG = 0.50
W_CREATOR_HUMAN = 0.50
W_CREATOR_AUTOMATION = 0.15
W_NAME_TAG_PERSON = 0.45
CONFLICT_MIN_CONFIDENCE = 0.4


def _person_key(owner: str) -> str:
    """'Rahul@co.com' and 'rahul' are the same person for grouping purposes."""
    return owner.split("@")[0].strip().lower()


def gather_evidence(res: Resource, known_people: frozenset[str] = frozenset()) -> list[Evidence]:
    ev: list[Evidence] = []
    lowered = {k.lower(): (k, v) for k, v in res.tags.items() if v}

    for key in OWNER_TAG_KEYS:
        if key in lowered:
            real_key, value = lowered[key]
            ev.append(Evidence(f"tag:{real_key}", value.strip().lower(), W_OWNER_TAG,
                               f"tag {real_key}={value}"))
            break
    for key in TEAM_TAG_KEYS:
        if key in lowered:
            real_key, value = lowered[key]
            ev.append(Evidence(f"tag:{real_key}", value.strip().lower(), W_TEAM_TAG,
                               f"tag {real_key}={value}"))
            break
    name = lowered.get("name", (None, ""))[1] or res.details.get("name", "")
    if name and known_people:
        for token in re.split(r"[-_. ]+", name.lower()):
            if token in known_people:
                ev.append(Evidence("name-tag:person", token, W_NAME_TAG_PERSON,
                                   f"Name tag '{name}' mentions known user '{token}'"))
                break
    if res.creator:
        weight = W_CREATOR_AUTOMATION if res.creator_is_automation else W_CREATOR_HUMAN
        kind = "automation" if res.creator_is_automation else "person"
        ev.append(Evidence("cloudtrail:creator", res.creator.strip().lower(), weight,
                           f"created by {res.creator} ({kind}, per CloudTrail)"))
    return ev


def gather_hints(res: Resource) -> list[str]:
    hints: list[str] = []
    name = next((v for k, v in res.tags.items() if k.lower() == "name" and v), None)
    if name:
        hints.append(f"Name tag: {name}")
    stack = res.tags.get("ingress.k8s.aws/stack")
    if stack:
        hints.append(f"Kubernetes ingress stack (namespace/name): {stack}")
    cluster = next((v for k, v in res.tags.items() if k in ("elbv2.k8s.aws/cluster", "eks:cluster-name")), None)
    if cluster:
        hints.append(f"Kubernetes cluster: {cluster}")
    d = res.details
    for label, key in (("key pair", "key_name"), ("IAM instance profile", "iam_profile"),
                       ("VPC", "vpc_id"), ("subnet", "subnet_id"), ("AMI", "image_id")):
        if d.get(key):
            hints.append(f"{label}: {d[key]}")
    return hints


def infer_owner(res: Resource, known_people: frozenset[str] = frozenset()) -> OwnerGuess:
    """Group evidence per candidate owner and combine as independent signals:
    confidence = 1 - prod(1 - weight). Two weak agreeing signals beat one weak signal,
    and confidence never reaches 1.0, which is intentional: we only ever *infer*."""
    evidence = gather_evidence(res, known_people)
    by_person: dict[str, list[Evidence]] = {}
    for e in evidence:
        by_person.setdefault(_person_key(e.owner), []).append(e)

    scored: list[tuple[float, str]] = []
    for items in by_person.values():
        miss = 1.0
        for e in items:
            miss *= 1.0 - e.weight
        # display the most specific spelling (an email beats a bare name)
        shown = max((e.owner for e in items), key=lambda o: "@" in o)
        scored.append((1.0 - miss, shown))
    scored.sort(reverse=True)

    best_conf, best_owner = scored[0] if scored else (0.0, None)
    conflict = None
    if len(scored) > 1 and scored[1][0] >= CONFLICT_MIN_CONFIDENCE:
        conflict = f"{best_owner} ({best_conf:.0%}) vs {scored[1][1]} ({scored[1][0]:.0%}): confirm who actually owns it"
    return OwnerGuess(owner=best_owner, confidence=round(best_conf, 2), evidence=evidence,
                      hints=gather_hints(res), conflict=conflict)
