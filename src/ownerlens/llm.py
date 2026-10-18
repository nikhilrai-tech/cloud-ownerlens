"""Optional LLM-written executive summary of a scan.

Opt-in only: nothing is sent to the Claude API unless `ownerlens summarize --send-to-llm` is used.
What is sent is exactly `build_payload()`: resource id/type/region/state, cost, score, reasons, the owner guess,
the Name tag and any recorded owner answer. No account id, no other tags, no credentials.

Needs the `llm` extra (`pip install -e '.[llm]'`) and ANTHROPIC_API_KEY (or `ant auth login`).
"""
from __future__ import annotations

import json
from typing import Any, Optional

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You write the short "cloud waste and ownership" brief that an infrastructure lead sends to engineering \
managers. You are given JSON produced by a read-only scanner.

Rules:
- Use only the data provided. Never invent resources, owners, costs or dates. Quote dollar figures exactly as given.
- The scanner never deletes anything and neither do you. Do not tell anyone to delete something unless its \
decision says the owner agreed AND a backup is confirmed; otherwise the next step is "ask the owner".
- Resources with risk "high" (production, Kubernetes-managed) must be described as needing a human decision.
- Be concrete and brief: a 2-3 sentence headline, then a prioritised list of what to do this week (largest saving \
or highest risk first), then a short "unclear or risky" section. Plain English, no hype, no emojis.
- Output Markdown."""


class LLMUnavailable(RuntimeError):
    pass


def build_payload(findings: list[dict]) -> dict:
    """Reduce findings.json entries to the minimum the model needs."""
    items = []
    for f in findings:
        r, o, z, d = f["resource"], f["owner"], f["zombie"], f.get("decision")
        items.append({
            "id": r["id"], "type": r["type"], "region": r["region"], "state": r.get("state"),
            "name": (r.get("tags") or {}).get("Name") or (r.get("details") or {}).get("name"),
            "monthly_cost_usd": r.get("monthly_cost_usd"),
            "score": z["score"], "action": z["action"], "risk": z["risk"], "reasons": z["reasons"],
            "saving_idea_usd_per_month": z.get("savings_usd"),
            "owner_guess": o.get("owner"), "owner_confidence": o.get("confidence"), "owner_conflict": o.get("conflict"),
            "owner_answer": ({"kind": d["decision"], "by": d["by"], "said": d["reason"], "review_after": d["review_after"]}
                             if d else None),
        })
    return {"findings": items,
            "total_monthly_cost_usd": round(sum(i["monthly_cost_usd"] or 0 for i in items), 2),
            "count": len(items)}


def summarize(findings: list[dict], client: Optional[Any] = None, model: str = DEFAULT_MODEL) -> str:
    try:
        import anthropic
    except ImportError as exc:  # keep the core tool dependency-free
        raise LLMUnavailable("install the extra first: pip install -e '.[llm]'") from exc

    client = client or anthropic.Anthropic()
    payload = json.dumps(build_payload(findings), indent=2, default=str)
    try:
        response = client.beta.messages.create(
            model=model, max_tokens=4000,
            betas=[FALLBACK_BETA], fallbacks="default",
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Scanner output:\n\n{payload}"}],
        )
    except anthropic.AuthenticationError as exc:
        raise LLMUnavailable("no valid credentials: set ANTHROPIC_API_KEY or run `ant auth login`") from exc
    except anthropic.RateLimitError as exc:
        raise LLMUnavailable("rate limited by the API; try again shortly") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMUnavailable(f"network error talking to the API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMUnavailable(f"API error {exc.status_code}: {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise LLMUnavailable("the model declined this request; use summary.md instead")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise LLMUnavailable("the model returned no text")
    return text


def wrap_markdown(body: str, model: str = DEFAULT_MODEL) -> str:
    return (f"# Executive summary (AI-written)\n\n"
            f"_Written by {model} from `findings.json`. It can misread data: check figures against `summary.md` "
            f"before forwarding. Nothing in this file was acted on._\n\n{body}\n")
