import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from ownerlens import report
from ownerlens.collectors.cloudtrail import find_creator
from ownerlens.models import Resource
from ownerlens.scanner import scan


@pytest.fixture(autouse=True)
def fake_aws_env(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@mock_aws
def test_scan_finds_orphan_volume_and_empty_bucket():
    session = boto3.Session(region_name="us-east-1")
    ec2 = session.client("ec2")
    vol = ec2.create_volume(
        AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3",
        TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": "owner", "Value": "rahul@co.com"}]}],
    )
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=10)
    session.client("s3").create_bucket(Bucket="forgotten-bucket")

    findings = scan(session, ["us-east-1"], use_cloudtrail=False, min_score=40)
    by_id = {f.resource.id: f for f in findings}

    owned = by_id[vol["VolumeId"]]
    assert owned.owner.owner == "rahul@co.com"
    assert owned.resource.monthly_cost_usd == 8.0
    assert any(f.owner.owner is None for f in findings)  # the untagged volume
    assert "forgotten-bucket" in by_id  # no metrics => "activity unknown" is surfaced, low score

    out = report.to_table(findings)
    assert "vol-" in out and "UNKNOWN" in out


class StubCloudTrail:
    """moto does not implement LookupEvents, so feed find_creator canned events."""

    def __init__(self, events):
        self.events = events

    def get_paginator(self, name):
        assert name == "lookup_events"
        events = self.events

        class P:
            def paginate(self, **kwargs):
                return [{"Events": events}]
        return P()


def _event(name, when, identity):
    return {"EventName": name, "EventTime": when, "CloudTrailEvent": json.dumps({"userIdentity": identity})}


def test_find_creator_picks_oldest_create_event():
    now = datetime.now(timezone.utc)
    ct = StubCloudTrail([
        _event("CreateVolume", now, {"type": "IAMUser", "userName": "later-user"}),
        _event("CreateVolume", now - timedelta(days=30), {"type": "IAMUser", "userName": "original-creator"}),
        _event("AttachVolume", now - timedelta(days=60), {"type": "IAMUser", "userName": "ignored"}),
    ])
    res = Resource("vol-1", "ebs:volume", "us-east-1")
    assert find_creator(ct, res) == ("original-creator", False)


def test_find_creator_without_events_returns_none():
    assert find_creator(StubCloudTrail([]), Resource("vol-1", "ebs:volume", "us-east-1")) == (None, False)


def test_find_creator_survives_access_denied():
    class Denied:
        def get_paginator(self, name):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "LookupEvents")
    assert find_creator(Denied(), Resource("vol-1", "ebs:volume", "us-east-1")) == (None, False)


@mock_aws
def test_report_bundle_is_written(tmp_path):
    from ownerlens.reports import ScanMeta, write_bundle
    session = boto3.Session(region_name="us-east-1")
    session.client("ec2").create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    session.client("ec2").allocate_address(Domain="vpc")  # test setup against moto, not product code
    findings = scan(session, ["us-east-1"], use_cloudtrail=False, min_score=40)

    paths = write_bundle(findings, ScanMeta(account="123", regions=["us-east-1"], cloudtrail=False), tmp_path)
    names = {p.name for p in paths}
    assert names == {"summary.md", "unowned.md", "owner_requests.md", "runbook.md", "findings.json", "findings.csv"}
    summary = (tmp_path / "summary.md").read_text()
    assert "Nothing was stopped, deleted" in summary and "ec2:eip" in summary and "ebs:volume" in summary
    assert "vol-" in (tmp_path / "unowned.md").read_text()
    assert (tmp_path / "findings.csv").read_text().count("\n") == len(findings) + 1
