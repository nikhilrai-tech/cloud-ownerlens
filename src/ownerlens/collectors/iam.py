from __future__ import annotations

from botocore.exceptions import ClientError


def list_people(session) -> set[str]:
    """IAM user names, used only to recognise people in Name tags. Empty set if not permitted."""
    try:
        iam = session.client("iam")
        names: set[str] = set()
        for page in iam.get_paginator("list_users").paginate():
            names.update(u["UserName"].lower() for u in page["Users"])
        return names
    except ClientError:
        return set()
