import json

from ownerlens import cli


def test_summarize_is_dry_run_by_default(tmp_path, capsys):
    f = tmp_path / "findings.json"
    f.write_text(json.dumps([{
        "resource": {"id": "i-1", "type": "ec2:instance", "region": "r", "tags": {}, "details": {}, "monthly_cost_usd": 5.0},
        "owner": {"owner": None, "confidence": 0.0, "conflict": None},
        "zombie": {"score": 60, "action": "ask-owner", "risk": "low", "reasons": ["stopped"]}, "decision": None}]))
    out = tmp_path / "summary.md"
    assert cli.main(["summarize", "--from", str(f), "--out", str(out)]) == 0
    assert "DRY RUN" in capsys.readouterr().out and not out.exists()
    assert cli.main(["summarize", "--from", str(f), "--show-payload"]) == 0
    assert '"i-1"' in capsys.readouterr().out


def test_gcp_without_cli_gives_clear_error(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert cli.main(["scan", "--provider", "gcp"]) == 2
    assert "not installed" in capsys.readouterr().err


def test_notify_requires_channel_and_people():
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["notify"])
