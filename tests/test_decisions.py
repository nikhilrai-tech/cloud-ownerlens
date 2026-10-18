from datetime import date, datetime, timedelta, timezone

from ownerlens import decisions
from ownerlens.inference import infer_owner
from ownerlens.models import Finding, Resource
from ownerlens.reports import ScanMeta, owner_requests_md, summary_md
from ownerlens.scoring import score_resource

LONG_AGO = datetime.now(timezone.utc) - timedelta(days=200)


def test_record_load_and_replace(tmp_path):
    f = tmp_path / "d.json"
    decisions.record(f, "i-1", "keep", "alice", "future work", 90, today=date(2026, 1, 1))
    decisions.record(f, "i-1", "keep", "alice", "still needed", 30, today=date(2026, 2, 1))
    loaded = decisions.load(f)
    assert list(loaded) == ["i-1"]
    assert loaded["i-1"].reason == "still needed" and loaded["i-1"].review_after == "2026-03-03"
    assert decisions.load(tmp_path / "missing.json") == {}


def test_keep_expires_on_review_date(tmp_path):
    d = decisions.record(tmp_path / "d.json", "i-1", "keep", "a", "r", 10, today=date(2026, 1, 1))
    assert decisions.is_active(d, today=date(2026, 1, 10))
    assert not decisions.is_active(d, today=date(2026, 1, 11))
    assert not decisions.is_active(None)


def _stopped(id_="i-1", cost=80.0, gb=1000):
    return Resource(id_, "ec2:instance", "ap-south-1", state="stopped", created_at=LONG_AGO,
                    monthly_cost_usd=cost, tags={"Owner": "alice"},
                    details={"ebs_gb": gb, "stopped_since": LONG_AGO})


def test_stopped_instance_gets_snapshot_suggestion():
    z = score_resource(_stopped())
    assert z.savings_usd == 30.0 and "snapshot" in z.suggestion
    assert z.action == "ask-owner"  # a suggestion never upgrades the action


def test_tiny_volumes_get_no_suggestion():
    z = score_resource(_stopped(cost=0.64, gb=8))
    assert z.suggestion is None and z.savings_usd is None


def _finding(res, decision=None):
    return Finding(res, infer_owner(res), score_resource(res), decision)


def test_kept_resources_leave_owner_requests_but_stay_in_summary(tmp_path):
    f = tmp_path / "d.json"
    kept = _finding(_stopped("i-kept"), decisions.record(f, "i-kept", "keep", "alice", "future work", 90))
    open_ = _finding(_stopped("i-open"))
    meta = ScanMeta(account="1", regions=["ap-south-1"])

    drafts = owner_requests_md([kept, open_])
    assert "i-open" in drafts and "i-kept" not in drafts

    summary = summary_md([kept, open_], meta)
    assert "kept on purpose" in summary and "future work" in summary
    assert "Cheaper ways to keep stopped instances" in summary and "i-kept" in summary


def test_expired_keep_returns_to_open_lists(tmp_path):
    f = tmp_path / "d.json"
    old = decisions.record(f, "i-1", "keep", "alice", "later", 30, today=date.today() - timedelta(days=60))
    finding = _finding(_stopped("i-1"), old)
    assert "i-1" in owner_requests_md([finding])
    assert "Review due" in summary_md([finding], ScanMeta(account="1"))


def test_pending_delete_is_hidden_from_requests_and_listed_with_checklist(tmp_path):
    f = tmp_path / "d.json"
    d = decisions.record(f, "i-ash", decisions.PENDING_DELETE, "bob", "will back up tomorrow, then delete is ok", 7)
    finding = _finding(_stopped("i-ash"), d)
    assert "i-ash" not in owner_requests_md([finding])
    summary = summary_md([finding], ScanMeta(account="1"))
    assert "Owner agreed to deletion, once a backup exists" in summary
    assert "verify-backup" in summary and "Nothing has been deleted" in summary


def test_runbook_lists_pending_instances_without_delete_commands(tmp_path):
    from ownerlens.reports import runbook_md
    res = _stopped("i-ash", cost=64.0, gb=800)
    res.details["ebs_volume_ids"] = ["vol-aaa"]
    d = decisions.record(tmp_path / "d.json", "i-ash", decisions.PENDING_DELETE, "bob", "backup first", 7)
    text = runbook_md([_finding(res, d)])
    assert "i-ash" in text and "bob" in text and "800" in text
    assert "aws ec2 create-snapshot --region ap-south-1 --volume-id vol-aaa" in text
    assert "delete-volume" not in text and "terminate-instances" not in text
    assert "No owner has agreed" in runbook_md([_finding(_stopped("i-x"))])
