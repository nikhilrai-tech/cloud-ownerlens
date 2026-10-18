import boto3
import pytest
from moto import mock_aws

from ownerlens.backup import format_check, verify_instance_backup
from ownerlens.guard import ReadOnlyViolation, read_only_session


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")


def _instance(ec2):
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    inst = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]
    return inst["InstanceId"], inst["BlockDeviceMappings"][0]["Ebs"]["VolumeId"]


@mock_aws
def test_missing_then_present_snapshot():
    setup = boto3.Session(region_name="us-east-1").client("ec2")
    iid, vol = _instance(setup)
    session = read_only_session(region_name="us-east-1")

    before = verify_instance_backup(session, "us-east-1", iid)
    assert not before.ok and before.volumes[0].latest_snapshot is None
    assert "NOT backed up" in format_check(before) and "MISSING" in format_check(before)

    setup.create_snapshot(VolumeId=vol)
    after = verify_instance_backup(session, "us-east-1", iid)
    assert after.ok and "RESULT: every volume has a completed snapshot" in format_check(after)


@mock_aws
def test_verify_cannot_create_snapshots_itself():
    setup = boto3.Session(region_name="us-east-1").client("ec2")
    _, vol = _instance(setup)
    session = read_only_session(region_name="us-east-1")
    with pytest.raises(ReadOnlyViolation):
        session.client("ec2").create_snapshot(VolumeId=vol)
