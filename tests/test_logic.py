from datetime import datetime, timedelta, timezone

from ownerlens.collectors.cloudtrail import principal_from_identity
from ownerlens.inference import infer_owner
from ownerlens.models import Resource
from ownerlens.scoring import score_resource

OLD = datetime.now(timezone.utc) - timedelta(days=200)


def test_identity_iam_user():
    assert principal_from_identity({"type": "IAMUser", "userName": "rahul"}) == ("rahul", False)


def test_identity_sso_human():
    ui = {"type": "AssumedRole", "arn": "arn:aws:sts::1:assumed-role/AWSReservedSSO_Admin_abc/rahul@co.com"}
    assert principal_from_identity(ui) == ("rahul@co.com", False)


def test_identity_pipeline_is_automation():
    ui = {"type": "AssumedRole", "arn": "arn:aws:sts::1:assumed-role/terraform-ci/run-42"}
    assert principal_from_identity(ui) == ("terraform-ci/run-42", True)


def test_owner_tag_beats_automation_creator():
    r = Resource("i-1", "ec2:instance", "us-east-1", tags={"Owner": "Priya@co.com"},
                 creator="terraform-ci/run-1", creator_is_automation=True)
    g = infer_owner(r)
    assert g.owner == "priya@co.com"
    assert g.confidence == 0.8


def test_agreeing_signals_raise_confidence():
    r = Resource("i-1", "ec2:instance", "us-east-1", tags={"owner": "a@co.com"}, creator="a@co.com")
    assert infer_owner(r).confidence == 0.9  # 1 - 0.2*0.5


def test_no_evidence_means_unknown_owner():
    g = infer_owner(Resource("vol-1", "ebs:volume", "us-east-1"))
    assert g.owner is None and g.confidence == 0.0


def test_unattached_volume_is_cleanup_candidate():
    r = Resource("vol-1", "ebs:volume", "us-east-1", state="available", created_at=OLD)
    z = score_resource(r)
    assert z.score == 90 and z.action == "propose-cleanup"


def test_attached_volume_is_kept():
    r = Resource("vol-1", "ebs:volume", "us-east-1", state="in-use", created_at=OLD)
    assert score_resource(r).action == "keep"


def test_prod_is_never_proposed_for_cleanup():
    r = Resource("vol-1", "ebs:volume", "us-east-1", state="available", created_at=OLD, tags={"env": "prod"})
    z = score_resource(r)
    assert z.risk == "high" and z.action == "ask-owner"


def test_idle_running_instance():
    r = Resource("i-1", "ec2:instance", "us-east-1", state="running", created_at=OLD,
                 metrics={"cpu_avg": 0.8, "cpu_max": 3.0, "net_mb_per_day": 2.0})
    assert score_resource(r).action == "propose-cleanup"


def test_busy_running_instance_is_kept():
    r = Resource("i-1", "ec2:instance", "us-east-1", state="running", created_at=OLD,
                 metrics={"cpu_avg": 35.0, "cpu_max": 90.0, "net_mb_per_day": 900.0})
    assert score_resource(r).action == "keep"


def test_parse_stopped_since():
    from ownerlens.collectors.ec2 import parse_stopped_since
    d = parse_stopped_since("User initiated (2025-03-02 10:15:00 GMT)")
    assert d == datetime(2025, 3, 2, 10, 15, tzinfo=timezone.utc)
    assert parse_stopped_since("") is None and parse_stopped_since(None) is None


def test_stopped_instance_scored_from_stop_date_not_launch_date():
    recent_stop = datetime.now(timezone.utc) - timedelta(days=5)
    r = Resource("i-1", "ec2:instance", "us-east-1", state="stopped", created_at=OLD,
                 details={"stopped_since": recent_stop})
    z = score_resource(r)
    assert z.score == 55  # no +10 "old" bonus: it was only stopped 5 days ago
    assert "since" in z.reasons[0]


def test_hints_collected_without_affecting_owner():
    r = Resource("i-1", "ec2:instance", "us-east-1", tags={"Name": "etl-worker"},
                 details={"key_name": "data-team-key", "iam_profile": "etl-role"})
    g = infer_owner(r)
    assert g.owner is None
    assert "Name tag: etl-worker" in g.hints and "key pair: data-team-key" in g.hints


PEOPLE = frozenset({"dave", "erin", "bob"})


def test_name_tag_person_becomes_evidence():
    r = Resource("i-1", "ec2:instance", "us-east-1", tags={"Name": "bob-dev-instance"})
    g = infer_owner(r, PEOPLE)
    assert g.owner == "bob" and g.confidence == 0.45 and g.conflict is None


def test_name_tag_and_owner_tag_agree_without_conflict():
    r = Resource("i-1", "ec2:instance", "us-east-1",
                 tags={"Name": "bob-dev-instance", "Owner": "bob"})
    g = infer_owner(r, PEOPLE)
    assert g.owner == "bob" and g.conflict is None and g.confidence > 0.8


def test_creator_vs_name_tag_person_is_flagged_as_conflict():
    r = Resource("i-1", "ec2:instance", "us-east-1", tags={"Name": "dave-personal"},
                 creator="erin")
    g = infer_owner(r, PEOPLE)
    assert g.conflict and "dave" in g.conflict and "erin" in g.conflict


def test_unknown_names_are_not_matched():
    r = Resource("i-1", "ec2:instance", "us-east-1", tags={"Name": "llm"})
    assert infer_owner(r, PEOPLE).owner is None


def test_unassociated_eip_is_cleanup_candidate():
    assert score_resource(Resource("eipalloc-1", "ec2:eip", "us-east-1", state="unassociated")).action == "propose-cleanup"
    assert score_resource(Resource("eipalloc-2", "ec2:eip", "us-east-1", state="associated")).action == "keep"


def test_idle_nat_and_empty_lb():
    nat = Resource("nat-1", "ec2:nat-gateway", "us-east-1", metrics={"gb_per_day": 0.0})
    assert score_resource(nat).action == "propose-cleanup"
    busy = Resource("nat-2", "ec2:nat-gateway", "us-east-1", metrics={"gb_per_day": 12.0})
    assert score_resource(busy).action == "keep"
    lb = Resource("lb", "elb:load-balancer", "us-east-1", metrics={"registered_targets": 0})
    assert score_resource(lb).score == 75
    unknown = Resource("lb2", "elb:load-balancer", "us-east-1")
    assert score_resource(unknown).action == "keep"  # targets unknown => never guess


def test_rds_rules():
    stopped = Resource("db", "rds:instance", "us-east-1", state="stopped")
    assert score_resource(stopped).action == "ask-owner"
    idle = Resource("db", "rds:instance", "us-east-1", state="available", metrics={"max_connections": 0.0})
    assert score_resource(idle).score == 70
    used = Resource("db", "rds:instance", "us-east-1", state="available", metrics={"max_connections": 4.0})
    assert score_resource(used).action == "keep"


def test_snapshot_only_flagged_when_orphaned_and_unused():
    orphan = Resource("snap-1", "ebs:snapshot", "us-east-1", created_at=OLD,
                      details={"source_volume_gone": True, "used_by_ami": False, "source_volume": "vol-x"})
    assert score_resource(orphan).action == "propose-cleanup"
    ami_backed = Resource("snap-2", "ebs:snapshot", "us-east-1", created_at=OLD,
                          details={"source_volume_gone": True, "used_by_ami": True})
    assert score_resource(ami_backed).action == "keep"
    live_source = Resource("snap-3", "ebs:snapshot", "us-east-1", created_at=OLD,
                           details={"source_volume_gone": False, "used_by_ami": False})
    assert score_resource(live_source).action == "keep"


def test_kubernetes_managed_lb_is_never_cleanup_and_prod_cluster_is_high_risk():
    lb = Resource("k8s-x", "elb:load-balancer", "ap-south-1", created_at=OLD, metrics={"registered_targets": 0},
                  tags={"elbv2.k8s.aws/cluster": "shop-prod", "ingress.k8s.aws/stack": "shop/shop-ingress"})
    z = score_resource(lb)
    assert z.action == "ask-owner" and z.risk == "high"
    assert any("Kubernetes cluster 'shop-prod'" in r for r in z.reasons)
    hints = infer_owner(lb).hints
    assert "Kubernetes cluster: shop-prod" in hints and any("shop/shop-ingress" in h for h in hints)


def test_non_prod_cluster_resource_still_capped():
    lb = Resource("k8s-y", "elb:load-balancer", "ap-south-1", created_at=OLD, metrics={"registered_targets": 0},
                  tags={"elbv2.k8s.aws/cluster": "dev-cluster"})
    z = score_resource(lb)
    assert z.action == "ask-owner" and z.risk != "high"
