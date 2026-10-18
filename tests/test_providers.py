import json
import subprocess

import pytest

from ownerlens.inference import infer_owner
from ownerlens.providers import azure, gcp
from ownerlens.providers.cli_runner import CliMissing, ReadOnlyCliViolation, check_read_only, run_json
from ownerlens.scoring import score_resource


class FakeRunner:
    """Pretends to be subprocess.run, returning canned JSON by command."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        line = " ".join(cmd[1:])
        hits = [k for k in self.table if k in line]
        data = self.table[max(hits, key=len)] if hits else []
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")


def test_cli_guard_allows_reads_and_blocks_writes():
    check_read_only(["gcloud", "compute", "disks", "list", "--format=json"])
    check_read_only(["az", "vm", "list", "-d"])
    for bad in (["gcloud", "compute", "disks", "delete", "d1"],
                ["az", "vm", "deallocate", "-n", "x"],
                ["az", "disk", "list", "&&", "az", "disk", "delete"],
                ["gcloud", "config", "set", "project", "p"],
                ["aws", "ec2", "describe-instances"],
                ["az", "vm", "show", "--set", "tags.a=b"]):
        with pytest.raises(ReadOnlyCliViolation):
            check_read_only(bad)


def test_missing_cli_has_a_clear_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(CliMissing):
        run_json(["gcloud", "compute", "disks", "list"])


GCP = {
    "compute disks list": [
        {"name": "orphan-disk", "zone": "https://x/zones/asia-south1-a", "sizeGb": "500", "creationTimestamp": "2025-01-01T00:00:00.000-08:00",
         "type": "https://x/diskTypes/pd-ssd", "labels": {"owner": "rahul"}},
        {"name": "boot-disk", "zone": "https://x/zones/asia-south1-a", "sizeGb": "20", "users": ["https://x/instances/vm1"]}],
    "compute addresses list": [
        {"name": "old-ip", "region": "https://x/regions/asia-south1", "status": "RESERVED", "addressType": "EXTERNAL", "address": "1.2.3.4"},
        {"name": "live-ip", "region": "https://x/regions/asia-south1", "status": "IN_USE", "addressType": "EXTERNAL"},
        {"name": "internal-ip", "status": "RESERVED", "addressType": "INTERNAL"}],
    "compute instances list": [
        {"name": "dead-vm", "zone": "https://x/zones/asia-south1-a", "status": "TERMINATED", "labels": {"team": "ml"},
         "lastStopTimestamp": "2025-03-01T10:00:00.000-08:00", "machineType": "https://x/machineTypes/n2-standard-4",
         "disks": [{"diskSizeGb": "100"}]},
        {"name": "running-vm", "zone": "https://x/zones/asia-south1-a", "status": "RUNNING"}],
}


def test_gcp_collects_only_unused_things():
    runner = FakeRunner(GCP)
    res = {r.id: r for r in gcp.collect("proj", runner)}
    assert set(res) == {"orphan-disk", "old-ip", "dead-vm"}
    assert res["orphan-disk"].monthly_cost_usd == 85.0 and res["orphan-disk"].region == "asia-south1-a"
    assert all("--project=proj" in c for c in runner.calls)
    assert score_resource(res["orphan-disk"]).action == "propose-cleanup"
    assert score_resource(res["old-ip"]).score == 90
    z = score_resource(res["dead-vm"])
    assert z.action == "ask-owner" and "since 2025-03-01" in z.reasons[0]
    assert infer_owner(res["orphan-disk"]).owner == "rahul"


AZ = {
    "disk list": [
        {"name": "lost-disk", "diskState": "Unattached", "managedBy": None, "diskSizeGb": 256, "sku": {"name": "Premium_LRS"},
         "location": "centralindia", "timeCreated": "2025-02-01T00:00:00+00:00", "tags": {"Owner": "asha"}},
        {"name": "os-disk", "diskState": "Attached", "managedBy": "/subscriptions/x/vm1", "diskSizeGb": 30}],
    "network public-ip list": [
        {"name": "ip-free", "ipConfiguration": None, "location": "centralindia", "ipAddress": "4.4.4.4"},
        {"name": "ip-used", "ipConfiguration": {"id": "nic"}, "location": "centralindia"}],
    "vm list -d": [
        {"name": "vm-stopped", "powerState": "VM stopped", "location": "centralindia"},
        {"name": "vm-dealloc", "powerState": "VM deallocated", "location": "centralindia"},
        {"name": "vm-run", "powerState": "VM running", "location": "centralindia"}],
}


def test_azure_collects_and_flags_billed_stopped_vm():
    runner = FakeRunner(AZ)
    res = {r.id: r for r in azure.collect("sub-1", runner)}
    assert set(res) == {"lost-disk", "ip-free", "vm-stopped", "vm-dealloc"}
    assert res["lost-disk"].monthly_cost_usd == round(256 * 0.135, 2)
    assert all("--subscription" in c for c in runner.calls)
    billed = score_resource(res["vm-stopped"])
    assert billed.score == 70 and any("NOT deallocated" in r for r in billed.reasons)
    assert score_resource(res["vm-dealloc"]).score == 55
    assert infer_owner(res["lost-disk"]).owner == "asha"
