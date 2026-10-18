"""Deliver the owner messages by Slack DM or email.

Safe by default: `plan()` only builds the list of (person, message). Nothing leaves this machine unless the
caller passes send=True. Credentials come from the environment, never from files in the repo:

  Slack:  SLACK_BOT_TOKEN  (scopes: chat:write, im:write, users:read.email)
  Email:  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM

people.json maps an owner as the report names them to how to reach them:
  {"alice": {"slack": "U0123ABCD", "email": "alice@example.com"}}
`slack` may be a user id (U...) or an email address (looked up with users.lookupByEmail).
"""
from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional

from .models import Finding
from .reports import open_owner_groups, owner_message

SLACK_API = "https://slack.com/api/"
SUBJECT = "Cloud cost review: resources that look like yours"


@dataclass
class Delivery:
    owner: str
    address: Optional[str]  # slack id/email or email address; None = no contact info
    message: str
    status: str = "planned"  # planned | sent | skipped (no contact) | failed: <why>


def load_people(path: str | Path) -> dict[str, dict]:
    return {k.lower(): v for k, v in json.loads(Path(path).read_text()).items()}


def plan(findings: list[Finding], people: dict[str, dict], channel: str) -> list[Delivery]:
    out = []
    for owner, fs in open_owner_groups(findings).items():
        entry = people.get(owner.lower()) or people.get(owner.split("@")[0].lower()) or {}
        address = entry.get(channel)
        d = Delivery(owner, address, owner_message(owner, fs))
        if not address:
            d.status = "skipped (no contact in people.json)"
        out.append(d)
    return out


def _slack_call(method: str, payload: dict, token: str, post: Optional[Callable] = None) -> dict:
    if post:
        return post(method, payload, token)
    req = urllib.request.Request(
        SLACK_API + method, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read())


def send_slack(deliveries: list[Delivery], token: str, post: Optional[Callable] = None) -> None:
    for d in deliveries:
        if d.status != "planned":
            continue
        try:
            user = d.address
            if "@" in user:
                found = _slack_call("users.lookupByEmail", {"email": user}, token, post)
                if not found.get("ok"):
                    raise RuntimeError(found.get("error", "lookup failed"))
                user = found["user"]["id"]
            # posting to a user id opens/uses the DM with the bot
            res = _slack_call("chat.postMessage", {"channel": user, "text": d.message}, token, post)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "postMessage failed"))
            d.status = "sent"
        except Exception as exc:  # one bad recipient must not stop the others
            d.status = f"failed: {exc}"


def send_email(deliveries: list[Delivery], env: Optional[dict] = None, smtp_factory: Callable = smtplib.SMTP) -> None:
    env = env if env is not None else os.environ
    host, sender = env.get("SMTP_HOST"), env.get("SMTP_FROM")
    if not host or not sender:
        raise RuntimeError("set SMTP_HOST and SMTP_FROM (and SMTP_USER/SMTP_PASSWORD if your server needs login)")
    with smtp_factory(host, int(env.get("SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        if env.get("SMTP_USER"):
            smtp.login(env["SMTP_USER"], env.get("SMTP_PASSWORD", ""))
        for d in deliveries:
            if d.status != "planned":
                continue
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, d.address, SUBJECT
            msg.set_content(d.message)
            try:
                smtp.send_message(msg)
                d.status = "sent"
            except Exception as exc:
                d.status = f"failed: {exc}"


def format_plan(deliveries: list[Delivery], channel: str, sending: bool) -> str:
    head = f"{'SENDING' if sending else 'DRY RUN (nothing is sent; add --send to deliver)'} via {channel}"
    lines = [head, ""]
    for d in deliveries:
        lines.append(f"- {d.owner} -> {d.address or '(no contact)'}: {d.status}")
    return "\n".join(lines)
