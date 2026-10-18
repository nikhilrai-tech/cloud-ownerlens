"""Hard read-only guarantee.

Every AWS call made through a session built here is checked *before* it is sent.
Only operations named Describe*/Get*/List*/Lookup* are allowed, so even a future bug (or a
careless edit) in a collector cannot delete, stop, tag or modify anything: the call raises
ReadOnlyViolation instead of reaching AWS.
"""
from __future__ import annotations

import boto3

ALLOWED_PREFIXES = ("Describe", "Get", "List", "Lookup")


class ReadOnlyViolation(RuntimeError):
    pass


def _check(model, **_kwargs):
    if not model.name.startswith(ALLOWED_PREFIXES):
        raise ReadOnlyViolation(
            f"ownerlens is read-only; refusing to call {model.service_model.service_name}:{model.name}")


def read_only_session(**session_kwargs) -> boto3.Session:
    session = boto3.Session(**session_kwargs)
    session.events.register("before-parameter-build.*.*", _check)
    return session
