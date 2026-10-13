"""One chat turn, end to end (SPEC §1, §7):

    message -> extractor -> Conversation.handle (memory)
            -> pending confirmation? classify the reply -> answer
            -> recommend (hard filter in code, soft ranking)
            -> reply model returns {"message", "recommend": [IDs]}
            -> check_reply (retry once) -> render names, prices and reasons from the menu
            -> deterministic fallback reply if the model fails or still breaks the rules

If the extractor fails, the turn stops before memory: nothing is saved, no pending question
is answered and nothing is recommended, because the unread message may state a restriction.

Memory work runs under a lock, so within this process the read-decide-append sequence is
atomic (this closes the SPEC §4 concurrency gap for a single-server deployment). Model calls
run outside the lock.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Optional

from extractor import MAX_TOKENS, classify_reply, extract
from llm import DEFAULT_MODEL, MODEL_ERRORS
from memory import (ATTRIBUTES, Claim, Conversation, Decision, Draft, EventStore, Kind, Op,
                    Profile, Scope, StaleConfirmation)
from menu import Menu
from recommend import MAX_PICKS, Recommendations, check_reply, recommend, render

HISTORY_MESSAGES = 8
RECOMMENDABLE_LIMIT = 8
CONVERSATION_IDLE_SECONDS = 3 * 60 * 60  # a conversation ends after this long unused (SPEC §12.2)
MAX_CONVERSATIONS_PER_USER = 5          # open at once; starting another drops the oldest
MAX_CONVERSATIONS = 1000                # in memory across all users

log = logging.getLogger(__name__)

_EXTRACT_FAILED = ("I couldn't process your dietary preferences right now, so I haven't updated "
                   "your profile or generated recommendations. Please try again or rephrase "
                   "your message.")

REPLY_SYSTEM = """\
You are the ordering assistant at {restaurant}. You help one customer choose dishes from \
this menu. Each turn you receive the customer's message and a JSON briefing from the app.

Answer in JSON. "message" is what you say to the customer. "recommend" lists the IDs of the \
dishes you recommend this turn, or is empty when they don't want a suggestion. The app shows \
each recommended dish under your message with its name, price and reasons.

RULES
1. Recommend at most three dishes, only with IDs from "recommendable" or "over_budget". \
Respect any category the customer asks for (starter, main, dessert). If nothing fits, \
recommend nothing and say which preference rules out the rest.
2. In "message", never write a dish name, a price, or "$". To talk about a specific dish, for \
example to answer a question about it, write [[item:ID]]; the app replaces it with the dish's \
name and price.
3. Use only facts from "menu": ingredients, allergens, spice, diet, description. Never invent \
dishes, ingredients, prices, discounts, or availability. If something is not on the menu, \
say so.
4. Never recommend a dish listed in "unsuitable". If the customer asks about one, mention it \
with [[item:ID]]; the app adds the warning.
5. "profile" has what is enforced now ("enforced_now") and what is saved for future visits \
("saved"). Where they differ, the difference holds only for this conversation.
6. If "questions_to_ask" is not empty, ask those questions, keeping their meaning.
7. If "changes" is not empty, acknowledge them briefly in plain words.
8. If "notice" is present, pass it on to the customer.
9. Be brief: at most five sentences, plain text, no headings. Don't repeat what the app \
lists for the recommended dishes.
"""

TURN_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"},
                   "recommend": {"type": "array", "items": {"type": "string"}}},
    "required": ["message", "recommend"],
    "additionalProperties": False,
}


class ModelUnavailable(RuntimeError):
    """No model client can be created (for example, no API key)."""


class UnknownConversation(LookupError):
    """The conversation was not issued to this user, or it has ended."""


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
    last_used: float
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


_LABELS = {"diet": "Diet", "allergen": "Allergies", "spice_tolerance": "Spice",
           "diet_lean": "Leaning", "budget_max": "Budget", "disliked_ingredient": "Dislikes",
           "liked_ingredient": "Likes"}


def _show(attribute: str, value) -> str:
    if isinstance(value, frozenset):
        return ", ".join(sorted(v.replace("_", " ") for v in value)) or "none"
    if value is None:
        return "not set"
    return f"up to ${value:.2f}" if attribute == "budget_max" else value


def _unset(attribute: str):
    spec = ATTRIBUTES[attribute]
    return frozenset() if spec.kind is Kind.SET else spec.default


def profile_view(p: Profile) -> dict:
    """Display lines: what is saved, and every value this conversation changes, shown with the
    saved value it overrides. Computed from the profiles, so a loosening shows too."""
    return {
        "remembered": [f"{_LABELS[a]}: {_show(a, p.durable[a])}"
                       for a in ATTRIBUTES if p.durable[a] != _unset(a)],
        "this_conversation": [f"{_LABELS[a]}: {_show(a, p.active[a])} (saved: {_show(a, p.durable[a])})"
                              for a in ATTRIBUTES if p.active[a] != p.durable[a]],
    }


def _plain(state: dict) -> dict:
    return {a: sorted(v) if isinstance(v, frozenset) else v for a, v in state.items()}


# ------------------------------------------------------------------------------ agent

class Agent:
    def __init__(self, store: EventStore, menu: Menu, client=None, *, client_factory=None,
                 extract_model: str = DEFAULT_MODEL, reply_model: str = DEFAULT_MODEL):
        self.store, self.menu = store, menu
        self._client, self._client_factory = client, client_factory
        self.extract_model, self.reply_model = extract_model, reply_model
        self._lock = threading.Lock()
        self._convs: OrderedDict[tuple[str, str], _ConvState] = OrderedDict()  # oldest use first
        self._system = REPLY_SYSTEM.format(restaurant=menu.restaurant)
        self._words = frozenset(w for i in menu.items for ing in i.ingredients
                                for w in (ing, *ing.split("_")))  # "shiitake_mushroom", "mushroom"

    @property
    def client(self):
        """Created on first use, so the app can start (and show memory) without an API key."""
        if self._client is None:
            if self._client_factory is None:
                raise ModelUnavailable("no model client configured")
            self._client = self._client_factory()
        return self._client

    def start_conversation(self, user_id: str) -> str:
        """Issue a conversation id bound to this user. Drops ended conversations, then the
        least recently used ones past the limits. Saved preferences are never affected."""
        cid = str(uuid.uuid4())
        with self._lock:
            now = monotonic()
            for key in [k for k, s in self._convs.items()
                        if now - s.last_used > CONVERSATION_IDLE_SECONDS]:
                del self._convs[key]
            mine = [k for k in self._convs if k[0] == user_id]
            for key in mine[:max(0, len(mine) + 1 - MAX_CONVERSATIONS_PER_USER)]:
                del self._convs[key]
            while len(self._convs) >= MAX_CONVERSATIONS:
                self._convs.popitem(last=False)
            self._convs[(user_id, cid)] = _ConvState(Conversation(self.store, user_id, cid), now)
        return cid

    def _state(self, user_id: str, conversation_id: str) -> _ConvState:
        """Call with self._lock held."""
        key = (user_id, conversation_id)  # user-scoped: another user's id never matches
        state, now = self._convs.get(key), monotonic()
        if state is None or now - state.last_used > CONVERSATION_IDLE_SECONDS:
            self._convs.pop(key, None)
            raise UnknownConversation(conversation_id)
        state.last_used = now
        self._convs.move_to_end(key)
        return state

    def profile(self, user_id: str, conversation_id: Optional[str] = None) -> Profile:
        """The profile in a conversation, or only what is saved when none is given."""
        with self._lock:
            if conversation_id is None:
                return self.store.profile(user_id, "")  # "" matches no conversation's events
            return self._state(user_id, conversation_id).conv.profile()

    def pending_question(self, user_id: str, conversation_id: str) -> Optional[str]:
        with self._lock:
            p = self._state(user_id, conversation_id).conv.pending
            return p.question if p else None

    def turn(self, user_id: str, conversation_id: str, text: str) -> TurnResult:
        trace: dict = {}
        with self._lock:
            state = self._state(user_id, conversation_id)  # unknown ids fail before any model call
        ext = extract(text, client=self.client, model=self.extract_model)
        claims = [_menu_word(c, self._words) for c in ext.claims]
        trace["claims"] = [_claim_dict(c) for c in claims]
        trace["extract_error"] = ext.error

        with self._lock:
            conv = state.conv
            if ext.error is not None:  # fail closed: no change, no answer, no recommendation
                log.warning("extraction failed user=%s: %s", user_id, ext.error)
                pending = conv.pending
                return TurnResult(_EXTRACT_FAILED, [], [], pending.question if pending else None,
                                  profile_view(conv.profile()), trace)
            before = conv.pending.proposal_id if conv.pending else None
            decisions: list[Decision] = conv.handle(claims)
            still_pending = conv.pending if conv.pending and conv.pending.proposal_id == before else None

        changes = [describe(d) for dec in decisions for d in dec.events]
        questions = [dec.question for dec in decisions if dec.question]
        trace["decisions"] = [{"rule": d.rule, "outcome": d.outcome, "reason": d.reason}
                              for d in decisions]
        notice = None

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
                        log.info("stale confirmation user=%s", user_id)
                        notice = "The earlier question no longer applies; nothing was changed."
            else:
                questions.append(still_pending.question)

        with self._lock:
            profile, pending = conv.profile(), conv.pending
        recs = recommend(self.menu, profile.active)
        question = pending.question if pending else None  # the one awaiting yes or no

        message, picks, errors, fallback = self._reply(state, text, profile, recs, changes,
                                                       questions, notice)
        reply, mentioned = render(message, picks, self.menu, recs, profile.active["budget_max"])
        trace.update(reply_errors=errors, fallback=fallback)
        with self._lock:
            state.history += [{"role": "user", "content": text},
                              {"role": "assistant",
                               "content": json.dumps({"message": message, "recommend": picks})}]
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
            "profile": {"enforced_now": _plain(profile.active), "saved": _plain(profile.durable)},
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
        resp = self.client.messages.create(
            model=self.reply_model, max_tokens=MAX_TOKENS, system=self._system,
            messages=messages, output_config={"format": {"type": "json_schema",
                                                         "schema": TURN_SCHEMA}})
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    def _reply(self, state: _ConvState, text, profile, recs, changes, questions, notice):
        """(message, picks, errors, fallback): two attempts by the model, then a reply built
        from verified data. Memory is already settled, so a failure here only costs prose."""
        messages = state.history + [{"role": "user", "content": self._briefing(
            text, profile, recs, changes, questions, notice)}]
        errors: list = []
        for _ in range(2):
            try:
                raw = self._call(messages)
            except MODEL_ERRORS as e:
                log.warning("reply model failed, using the fallback: %s", type(e).__name__)
                errors.append(f"{type(e).__name__}: {e}")
                break
            try:
                message, picks = _parse(raw)
            except (ValueError, KeyError, TypeError) as e:  # not the requested JSON shape
                problems = [f"unreadable reply ({type(e).__name__}: {e})"]
            else:
                problems = check_reply(message, picks, self.menu, recs)
                if not message.strip():
                    problems.append("empty message")
            if not problems:
                return message, picks, errors, False
            log.warning("reply model output rejected: %s", "; ".join(problems))
            errors.append(problems)
            messages = messages + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": "That reply broke these rules: " + "; ".join(problems)
                 + ". Rewrite it following the rules."}]
        return (*fallback_reply(recs, changes, questions, notice), errors, True)


def _parse(raw: str) -> tuple[str, list]:
    data = json.loads(raw)
    message, picks = data["message"], data["recommend"]
    if not (isinstance(message, str) and isinstance(picks, list)
            and all(isinstance(p, str) for p in picks)):
        raise TypeError("message must be text and recommend a list of IDs")
    return message, picks


def fallback_reply(recs: Recommendations, changes: list, questions: list,
                   notice: Optional[str]) -> tuple[str, list]:
    """(message, picks) built only from verified data, for when the model can't produce a
    valid reply."""
    parts = [notice] if notice else []
    if changes:
        parts.append("Noted: " + "; ".join(changes) + ".")
    parts += questions
    parts.append("A few options that fit:" if recs.candidates
                 else "Nothing on the menu fits every preference you've given me.")
    return " ".join(parts), [c.item.id for c in recs.candidates[:MAX_PICKS]]


_PLURALS = (("ies", "y"), ("es", ""), ("s", ""))  # berries, tomatoes, mushrooms


def _menu_word(c: Claim, words: frozenset) -> Claim:
    """A plural ingredient whose singular is a menu word becomes that word ("mushrooms" ->
    "mushroom"). Stored as said, it would match no dish, and a later statement about the same
    food would land on a different key. Anything else passes unchanged."""
    s = c.subject
    if c.attribute not in ("disliked_ingredient", "liked_ingredient") or s in words:
        return c
    return next((replace(c, subject=s[:-len(end)] + add) for end, add in _PLURALS
                 if s.endswith(end) and s[:-len(end)] + add in words), c)


def _drafts(events) -> list[Draft]:
    return [Draft(e.attribute, e.operation, e.scope, e.source, e.subject, e.value, e.evidence)
            for e in events]


def _claim_dict(c) -> dict:
    return {"attribute": c.attribute, "polarity": c.polarity.value, "subject": c.subject,
            "value": c.value, "temporal": c.temporal.value, "modality": c.modality.value,
            "evidence": c.evidence}
