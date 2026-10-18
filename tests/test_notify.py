import pytest

from ownerlens import decisions, notify
from ownerlens.inference import infer_owner
from ownerlens.models import Finding
from ownerlens.scoring import score_resource

from test_decisions import _stopped


def _f(rid, owner_tag, decision=None):
    res = _stopped(rid)
    res.tags = {"Owner": owner_tag}
    return Finding(res, infer_owner(res), score_resource(res), decision)


PEOPLE = {"alice": {"slack": "U111", "email": "alice@example.com"}, "asha": {"slack": "asha@example.com"}}


def test_plan_skips_people_without_contact_and_people_who_answered(tmp_path):
    answered = decisions.record(tmp_path / "d.json", "i-3", "keep", "carol", "later", 90)
    findings = [_f("i-1", "alice"), _f("i-2", "nobody-known"), _f("i-3", "carol", answered)]
    plan = {d.owner: d for d in notify.plan(findings, PEOPLE, "slack")}
    assert set(plan) == {"alice", "nobody-known"}  # carol already answered
    assert plan["alice"].status == "planned" and plan["alice"].address == "U111"
    assert plan["nobody-known"].status.startswith("skipped")
    assert "i-1" in plan["alice"].message and "Nothing has been touched" in plan["alice"].message


def test_dry_run_formatting_never_sends():
    text = notify.format_plan(notify.plan([_f("i-1", "alice")], PEOPLE, "slack"), "slack", sending=False)
    assert "DRY RUN" in text and "nothing is sent" in text


def test_slack_send_resolves_email_and_records_failures():
    calls = []

    def post(method, payload, token):
        calls.append((method, payload))
        if method == "users.lookupByEmail":
            return {"ok": True, "user": {"id": "UASHA"}}
        if payload["channel"] == "U111":
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True}

    ds = notify.plan([_f("i-1", "alice"), _f("i-2", "asha")], PEOPLE, "slack")
    notify.send_slack(ds, "xoxb-test", post)
    by = {d.owner: d for d in ds}
    assert by["asha"].status == "sent" and by["alice"].status == "failed: channel_not_found"
    assert ("chat.postMessage", {"channel": "UASHA", "text": by["asha"].message}) in calls


def test_email_needs_config_and_sends_via_smtp():
    class FakeSMTP:
        sent = []

        def __init__(self, host, port, timeout):
            self.args = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self): ...
        def login(self, u, p): ...

        def send_message(self, msg):
            FakeSMTP.sent.append((msg["To"], msg["Subject"]))

    ds = notify.plan([_f("i-1", "alice")], PEOPLE, "email")
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        notify.send_email(ds, env={}, smtp_factory=FakeSMTP)
    notify.send_email(ds, env={"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "ops@example.com"}, smtp_factory=FakeSMTP)
    assert ds[0].status == "sent" and FakeSMTP.sent == [("alice@example.com", notify.SUBJECT)]
