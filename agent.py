"""One chat turn, end to end (SPEC §1, §7):

    message -> extractor -> Conversation.handle (memory)
            -> pending confirmation? classify the reply -> answer
            -> recommend (hard filter in code, soft ranking)
            -> reply model writes text with [[rec:ID]] / [[item:ID]] tokens
            -> check_reply (retry once) -> render names and prices from the menu
            -> deterministic fallback reply if the model still breaks the rules

Memory work runs under a lock, so within this process the read-decide-append sequence is
atomic (this closes the SPEC §4 concurrency gap for a single-server deployment). Model calls
run outside the lock.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Optional

from extractor import DEFAULT_MODEL, classify_reply, extract
from memory import (Conversation, Decision, Draft, EventStore, Op, Profile, Scope,
                    StaleConfirmation)
from menu import Menu
from recommend import Recommendations, check_reply, recommend, render

HISTORY_MESSAGES = 8
RECOMMENDABLE_LIMIT = 8

REPLY_SYSTEM = """\
You are the ordering assistant at {restaurant}. You help one customer choose dishes from \
this menu. Each turn you receive the customer's message and a JSON briefing from the app.

RULES
1. Refer to dishes only with tokens. Use [[rec:ID]] to recommend a dish, and only with IDs \
from "recommendable" or "over_budget". Use [[item:ID]] to talk about any other menu dish, for \
example to answer a question about it. Never write a dish name, a price, or "$" yourself; the \
app replaces each token with the dish's name and price.
2. Use only facts from "menu": ingredients, allergens, spice, diet, description. Never invent \
dishes, ingredients, prices, discounts, or availability. If something is not on the menu, \
say so.
3. Never recommend a dish listed in "unsuitable". If the customer asks about one, mention it \
with [[item:ID]]; the app adds the warning.
4. If "questions_to_ask" is not empty, ask those questions, keeping their meaning.
5. If "changes" is not empty, acknowledge them briefly in plain words.
6. When the customer wants a suggestion, recommend one to three dishes and say why, using \
their "reasons". Respect any category they ask for (starter, main, dessert). If nothing fits, \
say which preference rules out the rest.
7. If "notice" is present, follow it.
8. Be brief: at most five sentences, plain text, no headings.
"""


class ModelUnavailable(RuntimeError):
    """No model client can be created (for example, no API key)."""


@dataclass
class TurnResult:
    reply: str
    mentioned: list
    changes: list
    question: Optional[str]
    profile: dict
    trace: dict = field(default_factory=dict)


@dataclass
class _ConvState:
    conv: Conversation
    history: list = field(default_factory=list)  # model-facing messages, tokens unrendered


# ------------------------------------------------------------------------ descriptions

def _scope_note(d: Draft) -> str:
    return " (this conversation)" if d.scope is Scope.CONVERSATION else ""


def describe(d: Draft) -> str:
    name = (d.subject or "").replace("_", " ")
    removing = d.op is Op.REMOVE
    text = {
        "allergen": f"no longer avoiding {name}" if removing else f"avoiding {name} (allergy)",
        "diet": "diet cleared" if removing else f"diet: {d.value}",
        "spice_tolerance": "spice preference cleared" if removing else f"spice: {d.value}",
        "diet_lean": "lean cleared" if removing else f"leaning {d.value}",
        "budget_max": "budget cleared" if removing else f"budget up to ${d.value:.2f}"
        if isinstance(d.value, (int, float)) else "budget",
        "disliked_ingredient": f"no longer dislikes {name}" if removing else f"dislikes {name}",
        "liked_ingredient": f"no longer likes {name}" if removing else f"likes {name}",
    }[d.attribute]
    return text + _scope_note(d)


def _lines(state: dict) -> list[str]:
    out = []
    if state["diet"] != "omnivore":
        out.append(f"Diet: {state['diet']}")
    if state["allergen"]:
        out.append("Allergies: " + ", ".join(sorted(a.replace("_", " ") for a in state["allergen"])))
    if state["spice_tolerance"] is not None:
        out.append(f"Spice: {state['spice_tolerance']}")
    if state["diet_lean"] is not None:
        out.append(f"Leaning {state['diet_lean']}")
    if state["budget_max"] is not None:
        out.append(f"Budget: up to ${state['budget_max']:.2f}")
    if state["disliked_ingredient"]:
        out.append("Dislikes: " + ", ".join(sorted(w.replace("_", " ") for w in state["disliked_ingredient"])))
    if state["liked_ingredient"]:
        out.append("Likes: " + ", ".join(sorted(w.replace("_", " ") for w in state["liked_ingredient"])))
    return out


def profile_view(p: Profile) -> dict:
    remembered = _lines(p.durable)
    return {"remembered": remembered,
            "this_conversation": [x for x in _lines(p.active) if x not in remembered]}


# ------------------------------------------------------------------------------ agent

class Agent:
    def __init__(self, store: EventStore, menu: Menu, client=None, *, client_factory=None,
                 extract_model: str = DEFAULT_MODEL, reply_model: str = DEFAULT_MODEL):
        self.store, self.menu = store, menu
        self._client, self._client_factory = client, client_factory
        self.extract_model, self.reply_model = extract_model, reply_model
        self._lock = threading.Lock()
        self._convs: dict[tuple[str, str], _ConvState] = {}
        self._system = REPLY_SYSTEM.format(restaurant=menu.restaurant)

    @property
    def client(self):
        """Created on first use, so the app can start (and show memory) without an API key."""
        if self._client is None:
            if self._client_factory is None:
                raise ModelUnavailable("no model client configured")
            self._client = self._client_factory()
        return self._client

    def _state(self, user_id: str, conversation_id: str) -> _ConvState:
        key = (user_id, conversation_id)  # user-scoped: another user's id never matches
        if key not in self._convs:
            self._convs[key] = _ConvState(Conversation(self.store, user_id, conversation_id))
        return self._convs[key]

    def profile(self, user_id: str, conversation_id: str) -> Profile:
        with self._lock:
            return self._state(user_id, conversation_id).conv.profile()

    def pending_question(self, user_id: str, conversation_id: str) -> Optional[str]:
        with self._lock:
            p = self._state(user_id, conversation_id).conv.pending
            return p.question if p else None

    def turn(self, user_id: str, conversation_id: str, text: str) -> TurnResult:
        trace: dict = {}
        ext = extract(text, client=self.client, model=self.extract_model)
        trace["claims"] = [_claim_dict(c) for c in ext.claims]
        trace["extract_error"] = ext.error

        with self._lock:
            state = self._state(user_id, conversation_id)
            conv = state.conv
            before = conv.pending.proposal_id if conv.pending else None
            decisions: list[Decision] = conv.handle(ext.claims) if ext.error is None else []
            still_pending = conv.pending if conv.pending and conv.pending.proposal_id == before else None

        changes = [describe(d) for dec in decisions for d in dec.events]
        questions = [dec.question for dec in decisions if dec.question]
        trace["decisions"] = [{"rule": d.rule, "outcome": d.outcome, "reason": d.reason}
                              for d in decisions]
        notice = None
        if ext.error is not None:
            notice = ("The app could not read a preference from the customer's message. If the "
                      "message seems to state a preference, ask them to rephrase it; otherwise "
                      "answer normally.")

        if still_pending is not None:  # §7: the reply answers the earlier question
            verdict = classify_reply(still_pending.question, text, client=self.client,
                                     model=self.extract_model)
            trace["confirmation"] = verdict
            if verdict in ("yes", "no"):
                with self._lock:
                    try:
                        events = conv.answer(still_pending.proposal_id, verdict == "yes")
                        changes += [describe(e) for e in _drafts(events)]
                        if verdict == "no":
                            changes.append("kept as it was")
                    except StaleConfirmation:
                        notice = "The earlier question no longer applies; nothing was changed."
            else:
                questions.append(still_pending.question)

        with self._lock:
            profile = conv.profile()
        recs = recommend(self.menu, profile.active)
        question = questions[-1] if questions else None

        raw, errors, fallback = self._reply(state, text, profile, recs, changes, questions, notice)
        reply, mentioned = render(raw, self.menu, recs, profile.active["budget_max"])
        trace.update(reply_errors=errors, fallback=fallback)
        with self._lock:
            state.history += [{"role": "user", "content": text},
                              {"role": "assistant", "content": raw}]
            state.history = state.history[-HISTORY_MESSAGES:]
        return TurnResult(reply, mentioned, changes, question, profile_view(profile), trace)

    # ------------------------------------------------------------------ reply model

    def _briefing(self, text: str, profile: Profile, recs: Recommendations, changes, questions,
                  notice) -> str:
        def cand(c):
            return {"id": c.item.id, "name": c.item.name, "category": c.item.category,
                    "spice": c.item.spice, "diet": c.item.diet, "reasons": list(c.reasons)}
        brief = {
            "customer_message": text,
            "profile": profile_view(profile),
            "changes": changes,
            "questions_to_ask": questions,
            "recommendable": [cand(c) for c in recs.candidates[:RECOMMENDABLE_LIMIT]],
            "over_budget": [cand(c) for c in recs.over_budget[:3]],
            "unsuitable": [{"id": i, "name": self.menu.get(i).name, "why": why}
                           for i, why in recs.excluded.items()],
            "menu": [i.public() for i in self.menu.items],
        }
        if notice:
            brief["notice"] = notice
        return json.dumps(brief)

    def _call(self, messages: list) -> str:
        resp = self.client.messages.create(model=self.reply_model, max_tokens=2048,
                                           system=self._system, messages=messages)
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    def _reply(self, state: _ConvState, text, profile, recs, changes, questions, notice):
        messages = state.history + [{"role": "user", "content": self._briefing(
            text, profile, recs, changes, questions, notice)}]
        errors: list = []
        for attempt in range(2):
            try:
                raw = self._call(messages)
            except Exception as e:  # model unavailable: fall back
                errors.append(f"{type(e).__name__}: {e}")
                break
            problems = check_reply(raw, self.menu, recs)
            if not problems and raw:
                return raw, errors, False
            errors.append(problems or ["empty reply"])
            messages = messages + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": "That reply broke these rules: " + "; ".join(
                    problems or ["empty reply"]) + ". Rewrite it following the rules."}]
        return fallback_reply(recs, changes, questions), errors, True


def fallback_reply(recs: Recommendations, changes: list, questions: list) -> str:
    """A reply built only from verified data, used when the model can't produce a valid one."""
    parts = []
    if changes:
        parts.append("Noted: " + "; ".join(changes) + ".")
    if recs.candidates:
        parts.append("A few options that fit: " + ", ".join(
            f"[[rec:{c.item.id}]]" for c in recs.candidates[:3]) + ".")
    else:
        parts.append("Nothing on the menu fits every preference you've given me.")
    parts += questions
    return " ".join(parts)


def _drafts(events) -> list[Draft]:
    return [Draft(e.attribute, e.operation, e.scope, e.source, e.subject, e.value, e.evidence)
            for e in events]


def _claim_dict(c) -> dict:
    return {"attribute": c.attribute, "polarity": c.polarity.value, "subject": c.subject,
            "value": c.value, "temporal": c.temporal.value, "modality": c.modality.value,
            "evidence": c.evidence}
