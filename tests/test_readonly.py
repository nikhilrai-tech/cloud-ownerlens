"""The project's core promise: it can never change anything in AWS."""
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from ownerlens.guard import ReadOnlyViolation, read_only_session

SRC = Path(__file__).parent.parent / "src" / "ownerlens"
# boto3 client methods that mutate state all start with one of these verbs
MUTATING = re.compile(r"\.(delete|terminate|stop|start|modify|create|put|release|disassociate|associate|"
                      r"attach|detach|reboot|revoke|authorize|update|tag|untag|run|deregister|"
                      r"register|allocate|reset|restore|copy|import|enable|disable)_\w+\(")


def test_source_contains_no_mutating_aws_calls():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "guard.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if MUTATING.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, offenders


@mock_aws
def test_guard_blocks_mutating_calls(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    s = read_only_session(region_name="us-east-1")
    ec2 = s.client("ec2")
    vol = boto3.Session(region_name="us-east-1").client("ec2").create_volume(AvailabilityZone="us-east-1a", Size=1)

    ec2.describe_volumes()  # reads are fine
    for call in (lambda: ec2.delete_volume(VolumeId=vol["VolumeId"]),
                 lambda: ec2.create_tags(Resources=[vol["VolumeId"]], Tags=[{"Key": "a", "Value": "b"}]),
                 lambda: s.client("s3").create_bucket(Bucket="nope"),
                 lambda: s.client("ec2").terminate_instances(InstanceIds=["i-123"])):
        with pytest.raises(ReadOnlyViolation):
            call()
    # and the volume really is still there
    assert len(ec2.describe_volumes()["Volumes"]) == 1
