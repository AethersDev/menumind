"""Preference memory core for the restaurant ordering agent. Implements SPEC.md v1.

    utterance --(LLM extractor)--> Claim --decide()--> Decision
    Decision.events --EventStore.append()--> event log --fold()--> Profile

decide(), resolve() and fold() are pure. EventStore is the only I/O.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Optional


# --------------------------------------------------------------------------- enums

class Kind(str, Enum):
    SCALAR = "scalar"
    SET = "set"


class Hardness(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class Polarity(str, Enum):
    ASSERT = "assert"
    NEGATE = "negate"


class Temporal(str, Enum):
    CURRENT = "current"
    CONVERSATION = "conversation"
    PAST = "past"
    ASPIRATIONAL = "aspirational"
    HYPOTHETICAL = "hypothetical"


class Modality(str, Enum):
    ASSERTED = "asserted"
    HEDGED = "hedged"


class Op(str, Enum):
    SET = "SET"
    REMOVE = "REMOVE"


class Scope(str, Enum):
    DURABLE = "durable"
    CONVERSATION = "conversation"


class Source(str, Enum):
    STATED = "stated"
    CONFIRMED = "confirmed"
    INFERRED = "inferred"


EXPLICIT_SOURCES = (Source.STATED, Source.CONFIRMED)


# ----------------------------------------------------------------- preference model (§2)

DIET_ORDER = ("omnivore", "pescatarian", "vegetarian", "vegan")  # least -> most restrictive
SPICE_ORDER = ("none", "mild", "medium", "hot")
ALLERGENS = ("peanut", "tree_nut", "sesame", "shellfish", "fish", "dairy", "egg", "gluten", "soy")


@dataclass(frozen=True)
class AttrSpec:
    kind: Kind
    hardness: Hardness
    values: Optional[tuple] = None    # allowed scalar values (ordered for hard scalars)
    subjects: Optional[tuple] = None  # closed set vocabulary; None = free text, normalized
    default: Any = None               # scalar value when unset / after REMOVE
    numeric: bool = False


ATTRIBUTES: dict[str, AttrSpec] = {
    "diet": AttrSpec(Kind.SCALAR, Hardness.HARD, values=DIET_ORDER, default="omnivore"),
    "allergen": AttrSpec(Kind.SET, Hardness.HARD, subjects=ALLERGENS),
    "spice_tolerance": AttrSpec(Kind.SCALAR, Hardness.SOFT, values=SPICE_ORDER),
    "diet_lean": AttrSpec(Kind.SCALAR, Hardness.SOFT, values=DIET_ORDER),
    "budget_max": AttrSpec(Kind.SCALAR, Hardness.SOFT, numeric=True),
    "disliked_ingredient": AttrSpec(Kind.SET, Hardness.SOFT),
    "liked_ingredient": AttrSpec(Kind.SET, Hardness.SOFT),
}

# §6 policy switch: loosening these always needs confirmation, even when explicit.
ALWAYS_CONFIRM_LOOSENING = frozenset({"allergen"})


class SpecError(ValueError):
    """Input violates the spec (unknown attribute, non-canonical subject, bad value)."""


class AuthorityError(SpecError):
    """An inferred event targets a hard attribute (P5)."""


class IsolationError(SpecError):
    """Events from another user reached the fold (P1)."""


class StaleConfirmation(SpecError):
    """A confirmation answer no longer applies to the current state (§7)."""


def _spec(attribute: str) -> AttrSpec:
    try:
        return ATTRIBUTES[attribute]
    except KeyError:
        raise SpecError(f"unknown attribute {attribute!r}") from None


def canonical_subject(attribute: str, subject: Optional[str]) -> Optional[str]:
    spec = _spec(attribute)
    if spec.kind is Kind.SCALAR:
        if subject is not None:
            raise SpecError(f"{attribute} is scalar; it takes no subject")
        return None
    if not subject or not subject.strip():
        raise SpecError(f"{attribute} is set-valued; a subject is required")
    s = subject.strip().lower().replace(" ", "_").replace("-", "_")
    if spec.subjects is not None and s not in spec.subjects:
        raise SpecError(f"non-canonical {attribute} subject {subject!r}; allowed: {spec.subjects}")
    return s


def _check_value(attribute: str, value: Any, *, required: bool) -> None:
    spec = _spec(attribute)
    if spec.kind is Kind.SET:
        if value is not None:
            raise SpecError(f"{attribute} is set-valued; use subject, not value")
        return
    if value is None:
        if required:
            raise SpecError(f"{attribute} needs a value")
        return
    if spec.values is not None and value not in spec.values:
        raise SpecError(f"invalid {attribute} value {value!r}; allowed: {spec.values}")
    if spec.numeric and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
        raise SpecError(f"{attribute} must be a positive number")


# ------------------------------------------------------------------------- records

@dataclass(frozen=True)
class Claim:
    """Extractor (LLM) output: what was said. Never what to do (§3)."""
    attribute: str
    polarity: Polarity
    subject: Optional[str] = None
    value: Any = None
    temporal: Temporal = Temporal.CURRENT
    modality: Modality = Modality.ASSERTED
    evidence: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        return cls(
            attribute=d["attribute"],
            polarity=Polarity(d["polarity"]),
            subject=d.get("subject"),
            value=d.get("value"),
            temporal=Temporal(d.get("temporal", "current")),
            modality=Modality(d.get("modality", "asserted")),
            evidence=d.get("evidence", ""),
        )


@dataclass(frozen=True)
class Draft:
    """An authorized state transition, not yet sequenced or stored."""
    attribute: str
    op: Op
    scope: Scope
    source: Source
    subject: Optional[str] = None
    value: Any = None
    evidence: str = ""


@dataclass(frozen=True)
class Event:
    event_id: str
    user_id: str
    session_id: str
    sequence_number: int
    attribute: str
    subject: Optional[str]
    value: Any
    operation: Op
    scope: Scope
    source: Source
    evidence: str
    created_at: str
    expires_at: Optional[str] = None  # reserved (§9); always None in v1

    @property
    def key(self) -> tuple[str, Optional[str]]:
        return (self.attribute, self.subject)


@dataclass(frozen=True)
class Decision:
    events: tuple[Draft, ...] = ()
    proposal: Optional[Draft] = None
    question: Optional[str] = None
    rule: str = ""
    reason: str = ""

    @property
    def outcome(self) -> str:
        if self.events and self.proposal:
            return "apply+confirm"
        if self.events:
            return "apply"
        if self.proposal:
            return "confirm"
        if self.question:
            return "clarify"  # a question with no proposed change; the answer is a new utterance
        return "noop"


@dataclass(frozen=True)
class Profile:
    durable: dict
    active: dict


# ------------------------------------------------------------------- decide (§5)

def _holds(attribute: str, subject: Optional[str], value: Any, state: dict) -> bool:
    if _spec(attribute).kind is Kind.SET:
        return subject in state[attribute]
    return state[attribute] == value


def _hard_effect(attribute: str, op: Op, subject: Optional[str], value: Any, state: dict) -> str:
    spec = _spec(attribute)
    if spec.kind is Kind.SET:
        present = subject in state[attribute]
        if op is Op.SET:
            return "none" if present else "tighten"
        return "loosen" if present else "none"
    new = value if op is Op.SET else spec.default
    cur_i, new_i = spec.values.index(state[attribute]), spec.values.index(new)
    return "tighten" if new_i > cur_i else "loosen" if new_i < cur_i else "none"


def _question(d: Draft, state: dict) -> str:
    if d.attribute == "allergen":
        name = d.subject.replace("_", " ")
        if d.op is Op.SET:
            return f"Should I save {name} as an allergy for future visits?"
        return f"Should I stop treating {name} as an allergy? I'll keep avoiding it until you confirm."
    if d.attribute == "diet":
        if d.op is Op.SET:
            return f"Should I save {d.value} as your diet going forward?"
        return f"Should I stop limiting recommendations to {state['diet']} dishes?"
    return f"Should I update your {d.attribute.replace('_', ' ')}?"


def _replacement_question(attribute: str, value: Any) -> str:
    return (f"Got it, you're no longer {value}. How do you eat now? "
            f"Until you tell me, I'll keep recommendations {value}.")


def _combine(effects: Iterable[str]) -> str:
    effects = set(effects)
    return "loosen" if "loosen" in effects else "tighten" if "tighten" in effects else "none"


def decide(claim: Claim, profile: Profile) -> Decision:
    """Map one extractor claim to an authorized decision. Pure."""
    attr = claim.attribute
    spec = _spec(attr)
    subject = canonical_subject(attr, claim.subject)
    _check_value(attr, claim.value, required=True)
    active, durable = profile.active, profile.durable
    hard = spec.hardness is Hardness.HARD

    def draft(op: Op, scope: Scope, value: Any = None, attribute: str = attr) -> Draft:
        return Draft(attribute, op, scope, Source.STATED, subject if attribute == attr else None,
                     value, claim.evidence)

    def confirm(d: Draft, rule: str, reason: str, events: tuple = ()) -> Decision:
        return Decision(events=events, proposal=d, question=_question(d, active), rule=rule, reason=reason)

    def clarify(rule: str, reason: str) -> Decision:
        return Decision(question=_replacement_question(attr, claim.value), rule=rule, reason=reason)

    # R1, R2: past / hypothetical never change state directly
    if claim.temporal in (Temporal.PAST, Temporal.HYPOTHETICAL):
        if (hard and claim.temporal is Temporal.PAST and claim.polarity is Polarity.ASSERT
                and _holds(attr, subject, claim.value, durable)):
            if spec.kind is Kind.SET:
                return confirm(draft(Op.REMOVE, Scope.DURABLE), "R1",
                               "past statement about an active hard restriction")
            return clarify("R1", "past statement about the stored diet; its replacement is unknown")
        return Decision(rule="R2", reason=f"{claim.temporal.value} statement; no state change")

    # R3, R4: aspirations are soft at most
    if claim.temporal is Temporal.ASPIRATIONAL and hard:
        if attr == "diet" and claim.polarity is Polarity.ASSERT:
            return Decision(events=(draft(Op.SET, Scope.DURABLE, claim.value, attribute="diet_lean"),),
                            rule="R3", reason="dietary aspiration recorded as a soft lean")
        return Decision(rule="R4", reason="aspiration cannot change a hard restriction")

    scope = Scope.CONVERSATION if claim.temporal is Temporal.CONVERSATION else Scope.DURABLE
    # §5.1: a durable write is judged against both what persists and what is enforced now;
    # a conversation write only against what is enforced now.
    views = (active,) if scope is Scope.CONVERSATION else (durable, active)

    # R5: scalar negation
    if spec.kind is Kind.SCALAR and claim.polarity is Polarity.NEGATE:
        if not any(v[attr] == claim.value for v in views):
            return Decision(rule="R5", reason="negated value is not currently held")
        if hard:  # negation names no replacement; never assume the default
            return clarify("R5", "negating a diet does not say what replaces it")

    if claim.polarity is Polarity.ASSERT:
        op, value = Op.SET, (claim.value if spec.kind is Kind.SCALAR else None)
    else:
        op, value = Op.REMOVE, None

    # R6: soft preferences apply
    if not hard:
        return Decision(events=(draft(op, scope, value),), rule="R6", reason="soft preference")

    effect = _combine(_hard_effect(attr, op, subject, value, v) for v in views)
    if effect == "none":  # R7
        return Decision(rule="R7", reason="no change to the hard restriction")

    if effect == "tighten":
        if claim.modality is Modality.ASSERTED or scope is Scope.CONVERSATION:  # R8
            return Decision(events=(draft(op, scope, value),), rule="R8", reason="tightening applied")
        now = (draft(op, Scope.CONVERSATION, value),)  # R9
        if _hard_effect(attr, op, subject, value, durable) == "none":
            return Decision(events=now, rule="R9", reason="hedged restatement of a stored restriction")
        return confirm(draft(op, Scope.DURABLE, value), "R9",
                       "hedged tightening: enforced now, durable change needs confirmation", events=now)

    # loosen
    if claim.modality is Modality.HEDGED or attr in ALWAYS_CONFIRM_LOOSENING:  # R10
        return confirm(draft(op, scope, value), "R10", "loosening a hard restriction needs confirmation")
    return Decision(events=(draft(op, scope, value),), rule="R11", reason="explicit loosening applied")


@dataclass(frozen=True)
class Pending:
    """A proposal awaiting the user's answer, bound to who, where, what, and when (§7)."""
    proposal_id: str
    user_id: str
    session_id: str
    draft: Draft
    question: str
    base_sequence: int  # the user's last sequence_number when the question was asked

    @property
    def key(self) -> tuple[str, Optional[str]]:
        return (self.draft.attribute, self.draft.subject)


def resolve(pending: Pending, affirmed: bool, *, user_id: str, session_id: str,
            events: Iterable[Event]) -> tuple[Draft, ...]:
    """Turn an answer to a pending proposal into authorized drafts, or refuse a stale one.

    Rejects the answer if it comes from another user or conversation, if any event has
    touched the proposal's key since the question was asked, or if the proposed change
    would no longer change anything."""
    if (user_id, session_id) != (pending.user_id, pending.session_id):
        raise StaleConfirmation("confirmation belongs to a different user or conversation")
    if not affirmed:
        return ()
    events = list(events)
    if any(e.key == pending.key and e.sequence_number > pending.base_sequence for e in events):
        raise StaleConfirmation("the preference changed after the question was asked")
    d = pending.draft
    now = fold(events, user_id=user_id, session_id=session_id)
    target = now.durable if d.scope is Scope.DURABLE else now.active
    if _hard_effect(d.attribute, d.op, d.subject, d.value, target) == "none":
        raise StaleConfirmation("the proposed change no longer changes anything")
    return (replace(d, source=Source.CONFIRMED, evidence=f"confirmed: {d.evidence}"),)


# --------------------------------------------------------------------- fold (§8)

def _materialize(winners: Iterable[Event]) -> dict:
    state: dict[str, Any] = {}
    members: dict[str, set] = {}
    for a, s in ATTRIBUTES.items():
        if s.kind is Kind.SET:
            members[a] = set()
        else:
            state[a] = s.default
    for e in winners:
        if ATTRIBUTES[e.attribute].kind is Kind.SET:
            if e.operation is Op.SET:
                members[e.attribute].add(e.subject)
        else:
            state[e.attribute] = e.value if e.operation is Op.SET else ATTRIBUTES[e.attribute].default
    state.update({a: frozenset(m) for a, m in members.items()})
    return state


def fold(events: Iterable[Event], *, user_id: str, session_id: str) -> Profile:
    """Deterministically compute durable and active profiles. Pure."""
    ordered = sorted(events, key=lambda e: e.sequence_number)
    seen: set[int] = set()
    for e in ordered:
        if e.user_id != user_id:
            raise IsolationError(f"event {e.event_id} belongs to another user")
        if e.sequence_number in seen:
            raise SpecError(f"duplicate sequence_number {e.sequence_number}")
        seen.add(e.sequence_number)
        if e.source is Source.INFERRED and _spec(e.attribute).hardness is Hardness.HARD:
            raise AuthorityError(f"inferred event {e.event_id} targets hard attribute {e.attribute}")
        if e.source is Source.INFERRED and e.scope is Scope.CONVERSATION:
            raise AuthorityError(f"inferred event {e.event_id} is conversation-scoped (unsupported in v1)")

    durable_explicit: dict = {}
    active_explicit: dict = {}
    inferred: dict = {}
    for e in ordered:
        if e.scope is Scope.DURABLE:
            if e.source in EXPLICIT_SOURCES:
                durable_explicit[e.key] = e
                active_explicit[e.key] = e
            else:
                inferred[e.key] = e
        elif e.session_id == session_id:
            active_explicit[e.key] = e

    def winners(explicit: dict) -> Iterable[Event]:
        merged = dict(inferred)
        merged.update(explicit)  # explicit beats inferred regardless of recency
        return merged.values()

    return Profile(durable=_materialize(winners(durable_explicit)),
                   active=_materialize(winners(active_explicit)))


# --------------------------------------------------------------------- store (§4)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    attribute       TEXT NOT NULL,
    subject         TEXT,
    value           TEXT,
    operation       TEXT NOT NULL CHECK (operation IN ('SET', 'REMOVE')),
    scope           TEXT NOT NULL CHECK (scope IN ('durable', 'conversation')),
    source          TEXT NOT NULL CHECK (source IN ('stated', 'confirmed', 'inferred')),
    evidence        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    UNIQUE (user_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS events_by_user ON events (user_id, sequence_number);
"""


def _validate_draft(d: Draft) -> None:
    spec = _spec(d.attribute)
    if canonical_subject(d.attribute, d.subject) != d.subject:
        raise SpecError(f"non-canonical subject {d.subject!r}")
    _check_value(d.attribute, d.value, required=(d.op is Op.SET and spec.kind is Kind.SCALAR))
    if d.op is Op.REMOVE and d.value is not None:
        raise SpecError("REMOVE carries no value")
    if d.source is Source.INFERRED and spec.hardness is Hardness.HARD:
        raise AuthorityError(f"inferred event may not target hard attribute {d.attribute}")
    if d.source is Source.INFERRED and d.scope is Scope.CONVERSATION:
        raise AuthorityError("conversation-scoped inferred events are unsupported in v1")


class EventStore:
    """Append-only, per-user event log on SQLite. The only component with I/O."""

    def __init__(self, path: str):
        # Shared across server threads; callers serialize access (agent.Agent holds a lock).
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False,
                                   timeout=10)
        self._db.executescript(_SCHEMA)

    def append(self, user_id: str, session_id: str, drafts: Iterable[Draft],
               *, now: Optional[datetime] = None) -> list[Event]:
        if not user_id or not session_id:
            raise SpecError("user_id and session_id are required")
        drafts = list(drafts)
        for d in drafts:
            _validate_draft(d)
        if not drafts:
            return []
        ts = (now or datetime.now(timezone.utc)).isoformat()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            (last,) = self._db.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) FROM events WHERE user_id = ?",
                (user_id,)).fetchone()
            out = []
            for i, d in enumerate(drafts, start=1):
                e = Event(str(uuid.uuid4()), user_id, session_id, last + i, d.attribute, d.subject,
                          d.value, d.op, d.scope, d.source, d.evidence, ts)
                self._db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e.event_id, e.user_id, e.session_id, e.sequence_number, e.attribute, e.subject,
                     json.dumps(e.value), e.operation.value, e.scope.value, e.source.value,
                     e.evidence, e.created_at, e.expires_at))
                out.append(e)
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return out

    def events_for(self, user_id: str) -> list[Event]:
        rows = self._db.execute(
            "SELECT event_id, user_id, session_id, sequence_number, attribute, subject, value,"
            " operation, scope, source, evidence, created_at, expires_at"
            " FROM events WHERE user_id = ? ORDER BY sequence_number", (user_id,)).fetchall()
        return [Event(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6]), Op(r[7]), Scope(r[8]),
                      Source(r[9]), r[10], r[11], r[12]) for r in rows]

    def last_sequence(self, user_id: str) -> int:
        (last,) = self._db.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) FROM events WHERE user_id = ?",
            (user_id,)).fetchone()
        return last

    def profile(self, user_id: str, session_id: str) -> Profile:
        return fold(self.events_for(user_id), user_id=user_id, session_id=session_id)

    def close(self) -> None:
        self._db.close()


# ------------------------------------------------------------- conversation layer (§7)

class Conversation:
    """Wires decide -> append -> pending confirmation for one user and one conversation.

    Holds at most one pending confirmation. Any newer claim on the same canonical key
    invalidates it, including claims that write nothing (e.g. an R7 re-assertion)."""

    def __init__(self, store: EventStore, user_id: str, session_id: str):
        self.store, self.user_id, self.session_id = store, user_id, session_id
        self.pending: Optional[Pending] = None

    def profile(self) -> Profile:
        return self.store.profile(self.user_id, self.session_id)

    def handle(self, claims: Iterable[Claim]) -> list[Decision]:
        out = []
        for c in claims:
            key = (c.attribute, canonical_subject(c.attribute, c.subject))
            if self.pending is not None and key == self.pending.key:
                self.pending = None  # superseded by a newer statement
            d = decide(c, self.profile())
            self.store.append(self.user_id, self.session_id, d.events)
            if d.proposal is not None:  # one at a time: a new question replaces the old one
                self.pending = Pending(str(uuid.uuid4()), self.user_id, self.session_id,
                                       d.proposal, d.question,
                                       self.store.last_sequence(self.user_id))
            out.append(d)
        return out

    def answer(self, proposal_id: str, affirmed: bool) -> list[Event]:
        p = self.pending
        if p is None or p.proposal_id != proposal_id:
            raise StaleConfirmation("that question is no longer pending")
        self.pending = None
        drafts = resolve(p, affirmed, user_id=self.user_id, session_id=self.session_id,
                         events=self.store.events_for(self.user_id))
        return self.store.append(self.user_id, self.session_id, drafts)
