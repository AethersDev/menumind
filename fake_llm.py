"""A scripted stand-in for the Anthropic client, for tests and offline UI work.

It routes by system prompt: the extractor gets claims from a lookup table, the reply
classifier gets answers from another, and the reply model gets a function of the briefing.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable, Optional

from extractor import EXTRACT_SYSTEM, REPLY_SYSTEM


def _resp(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def default_responder(brief: dict, messages: list) -> str:
    """Acknowledge changes, ask questions, recommend the top two allowed dishes."""
    parts = []
    if brief.get("changes"):
        parts.append("Got it.")
    recs = brief["recommendable"][:2]
    if recs:
        parts.append("Try " + " or ".join(f"[[rec:{r['id']}]]" for r in recs) + ".")
    parts += brief.get("questions_to_ask", [])
    return " ".join(parts) or "What would you like?"


def claim(attribute, polarity="assert", subject=None, value=None, temporal="current",
          modality="asserted", evidence=""):
    return {"attribute": attribute, "polarity": polarity, "subject": subject, "value": value,
            "temporal": temporal, "modality": modality, "evidence": evidence}


class FakeLLM:
    def __init__(self, claims: Optional[dict] = None, replies: Optional[dict] = None,
                 responder: Optional[Callable] = None):
        self.claims = claims or {}          # utterance -> list of claim dicts, or "invalid"
        self.replies = replies or {}        # customer reply -> yes | no | unclear
        self.responder = responder or default_responder
        self.briefings: list[dict] = []     # every briefing the reply model received
        self.calls = {"extract": 0, "classify": 0, "respond": 0}
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, system, messages, **_):
        user = messages[-1]["content"]
        if system == EXTRACT_SYSTEM:
            self.calls["extract"] += 1
            found = self.claims.get(user, [])
            return _resp("{not json" if found == "invalid" else json.dumps({"claims": found}))
        if system == REPLY_SYSTEM:
            self.calls["classify"] += 1
            reply = user.split("\nReply: ", 1)[1]
            return _resp(json.dumps({"answer": self.replies.get(reply, "unclear")}))
        self.calls["respond"] += 1
        try:
            brief = json.loads(user)
            self.briefings.append(brief)
        except json.JSONDecodeError:  # a retry instruction; reuse the last briefing
            brief = self.briefings[-1]
        return _resp(self.responder(brief, messages))
