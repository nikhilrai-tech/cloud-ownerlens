import json
from types import SimpleNamespace

import pytest

from ownerlens import llm


def _finding(**over):
    f = {"resource": {"id": "i-1", "type": "ec2:instance", "region": "ap-south-1", "state": "stopped",
                      "tags": {"Name": "etl", "Secret": "do-not-send", "Owner": "alice"}, "monthly_cost_usd": 80.0,
                      "details": {"key_name": "k"}},
         "owner": {"owner": "alice", "confidence": 0.8, "conflict": None, "evidence": [], "hints": ["VPC: vpc-1"]},
         "zombie": {"score": 65, "action": "ask-owner", "risk": "medium", "reasons": ["stopped"], "savings_usd": 30.0},
         "decision": None}
    f.update(over)
    return f


def test_payload_is_minimal():
    payload = llm.build_payload([_finding()])
    text = json.dumps(payload)
    assert "do-not-send" not in text and "vpc-1" not in text and "key_name" not in text
    assert payload["total_monthly_cost_usd"] == 80.0 and payload["findings"][0]["name"] == "etl"


def test_payload_includes_owner_answer():
    d = {"decision": "delete-ok-after-backup", "by": "bob", "reason": "after backup", "review_after": "2026-10-25"}
    item = llm.build_payload([_finding(decision=d)])["findings"][0]
    assert item["owner_answer"]["kind"] == "delete-ok-after-backup"


class FakeClient:
    def __init__(self, response):
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return response
        self.beta = SimpleNamespace(messages=Messages())


def _resp(text="Headline.", stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)])


def test_summarize_uses_expected_request_shape():
    client = FakeClient(_resp("Do this first."))
    assert llm.summarize([_finding()], client) == "Do this first."
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == [llm.FALLBACK_BETA] and call["output_config"] == {"effort": "low"}
    assert "never deletes" in call["system"] and "i-1" in call["messages"][0]["content"]


def test_refusal_and_empty_responses_are_reported_not_hidden():
    with pytest.raises(llm.LLMUnavailable, match="declined"):
        llm.summarize([_finding()], FakeClient(_resp(stop="refusal")))
    with pytest.raises(llm.LLMUnavailable, match="no text"):
        llm.summarize([_finding()], FakeClient(SimpleNamespace(stop_reason="end_turn", content=[])))


def test_wrapper_marks_output_as_ai_written():
    out = llm.wrap_markdown("Body")
    assert "AI-written" in out and "Nothing in this file was acted on" in out
