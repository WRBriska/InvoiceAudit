"""
llm_client.py — the language-model boundary.

The LLM does TWO things and nothing else:
  1. normalize_line(): map a free-text invoice line description to a catalog code
     (e.g. "lift-gate svc" -> "LIFTGATE"). This is fuzzy classification — the part
     a language model is genuinely good at and rules are brittle at.
  2. summarize_findings(): turn the deterministic findings into a readable note.

It never computes or judges money. That keeps the audit's numbers reproducible
regardless of which client is plugged in.

Two implementations:
  - AnthropicClient: real calls (used in production / when ANTHROPIC_API_KEY is set).
  - MockClient: deterministic, offline. For structured synthetic invoices the code
    is already present, so the mock is a near-passthrough — which is exactly why
    the eval's *numbers* don't depend on the LLM at all.
"""

import json
import os


CATALOG_HINT = ["LIFTGATE", "RESIDENTIAL", "DETENTION", "REDELIVERY", "INSIDE", "LIMITED"]


class LLMClient:
    def normalize_line(self, description, raw_code=None):
        raise NotImplementedError

    def summarize_findings(self, invoice_id, findings):
        raise NotImplementedError


class MockClient(LLMClient):
    """Offline, deterministic. Used for sandbox runs and CI."""

    _ALIASES = {
        "liftgate": "LIFTGATE", "lift-gate": "LIFTGATE", "lift gate": "LIFTGATE", "lg": "LIFTGATE",
        "residential": "RESIDENTIAL", "resi": "RESIDENTIAL",
        "detention": "DETENTION", "wait time": "DETENTION",
        "redelivery": "REDELIVERY", "re-delivery": "REDELIVERY", "second attempt": "REDELIVERY",
        "inside": "INSIDE", "inside delivery": "INSIDE",
        "limited": "LIMITED", "limited access": "LIMITED",
    }

    def normalize_line(self, description, raw_code=None):
        if raw_code in CATALOG_HINT:
            return raw_code, 0.99
        d = (description or "").lower()
        for alias, code in self._ALIASES.items():
            if alias in d:
                return code, 0.9
        return raw_code or "MISC", 0.4  # low confidence -> triage will catch it

    def summarize_findings(self, invoice_id, findings):
        if not findings:
            return "No discrepancies found; invoice reconciles to contract."
        parts = [f"{f['type']} on {f['line_ref']} (+${f['dollar_impact']:.2f})" for f in findings]
        return "Flagged: " + "; ".join(parts) + "."


class AnthropicClient(LLMClient):
    """Real calls. Requires ANTHROPIC_API_KEY. Math still never goes through here."""

    def __init__(self, model="claude-haiku-4-5-20251001"):
        from anthropic import Anthropic
        self.client = Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model

    def normalize_line(self, description, raw_code=None):
        prompt = (
            "Map this freight invoice line description to exactly one code from "
            f"{CATALOG_HINT} or MISC if none fit. Reply as JSON "
            '{"code": "...", "confidence": 0.0-1.0}. No prose.\n'
            f'Description: "{description}"'
        )
        msg = self.client.messages.create(
            model=self.model, max_tokens=60,
            messages=[{"role": "user", "content": prompt}])
        try:
            text = "".join(b.text for b in msg.content if b.type == "text")
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            return data.get("code", raw_code or "MISC"), float(data.get("confidence", 0.5))
        except Exception:
            return raw_code or "MISC", 0.3  # parse failure -> low confidence -> triage

    def summarize_findings(self, invoice_id, findings):
        msg = self.client.messages.create(
            model=self.model, max_tokens=200,
            messages=[{"role": "user", "content":
                "Write one concise sentence summarizing these freight-audit findings "
                f"for {invoice_id}. Do not invent numbers.\n{json.dumps(findings)}"}])
        return "".join(b.text for b in msg.content if b.type == "text").strip()


def get_client():
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicClient()
        except Exception:
            pass
    return MockClient()
