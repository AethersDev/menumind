"""Executable checks for SPEC.md §10 properties P1–P6, the confirmation lifecycle (§7),
fold determinism, and consistency of extractor_cases.json labels with the decision rules.

Unit + integration layers only: no LLM is called. Extractor accuracy is measured
separately against extractor_cases.json once the extractor exists.
"""
import json
import random
from pathlib import Path

import pytest

from memory import (
    ALWAYS_CONFIRM_LOOSENING, DIET_ORDER, AuthorityError, Claim, Conversation, Draft, Event,
    EventStore, IsolationError, Modality, Op, Pending, Polarity, Scope, Source, SpecError,
    StaleConfirmation, Temporal, decide, fold, resolve,
)

U_A, U_B = "user-a", "user-b"
CONV, PAST, ASPIRE, HYPO = (Temporal.CONVERSATION, Temporal.PAST, Temporal.ASPIRATIONAL,
                            Temporal.HYPOTHETICAL)
HEDGED = Modality.HEDGED
DUR, CNV = Scope.DURABLE, Scope.CONVERSATION
STATED, INFERRED, CONFIRMED = Source.STATED, Source.INFERRED, Source.CONFIRMED


def C(attribute, polarity="assert", **kw) -> Claim:
    return Claim(attribute=attribute, polarity=Polarity(polarity), **kw)


def D(attribute, op=Op.SET, scope=DUR, source=STATED, **kw) -> Draft:
    return Draft(attribute, op, scope, source, **kw)


def as_events(user, session, drafts, start=1):
    """Sequence drafts into events without a database (for pure tests)."""
    return [Event(f"e{start + i}", user, session, start + i, d.attribute, d.subject, d.value,
                  d.op, d.scope, d.source, d.evidence, "2026-10-13T00:00:00+00:00")
            for i, d in enumerate(drafts)]


class Chat(Conversation):
    """Test conveniences over the production conversation layer."""

    def say(self, *claims):
        out = self.handle(claims)
        return out[0] if len(out) == 1 else out

    def reply(self, affirmed: bool):
        return self.answer(self.pending.proposal_id, affirmed)

    def durable(self, attr):
        return self.profile().durable[attr]

    def active(self, attr):
        return self.profile().active[attr]

    def new_conversation(self, session):
        return Chat(self.store, self.user_id, session)


@pytest.fixture
def store(tmp_path):
    s = EventStore(str(tmp_path / "memory.db"))
    yield s
    s.close()


# ===================================================================== P1 Isolation

def test_users_do_not_share_state(store):
    a, b = Chat(store, U_A, "a1"), Chat(store, U_B, "b1")
    a.say(C("diet", value="vegetarian"), C("allergen", subject="peanut"))
    b.say(C("spice_tolerance", value="hot"))
    assert b.active("diet") == "omnivore" and b.active("allergen") == frozenset()
    assert a.active("spice_tolerance") is None
    assert a.active("allergen") == {"peanut"}


def test_shared_session_id_is_not_a_privacy_boundary(store):
    a, b = Chat(store, U_A, "same"), Chat(store, U_B, "same")
    a.say(C("allergen", subject="shellfish", temporal=CONV))
    assert b.active("allergen") == frozenset()


def test_fold_refuses_foreign_events():
    evs = as_events(U_A, "s", [D("diet", value="vegan")])
    with pytest.raises(IsolationError):
        fold(evs, user_id=U_B, session_id="s")


def test_sequence_numbers_are_per_user(store):
    store.append(U_A, "s", [D("spice_tolerance", value=v) for v in ("mild", "medium", "hot")])
    (e,) = store.append(U_B, "s", [D("diet", value="vegan")])
    assert e.sequence_number == 1


# =================================================================== P2 Persistence

def test_durable_survives_restart_and_overlay_does_not(tmp_path):
    path = str(tmp_path / "memory.db")
    s1 = EventStore(path)
    Chat(s1, U_A, "visit-1").say(C("diet", value="vegetarian"),
                                 C("spice_tolerance", value="none", temporal=CONV))
    s1.close()

    s2 = EventStore(path)  # process restart
    later = Chat(s2, U_A, "visit-2")
    assert later.active("diet") == "vegetarian"
    assert later.active("spice_tolerance") is None
    s2.close()


# =================================================================== P3 Supersession

def test_spice_transition_table(store):
    s1 = Chat(store, U_A, "s1")
    s1.say(C("spice_tolerance", value="hot", evidence="I love spicy food"))
    assert (s1.durable("spice_tolerance"), s1.active("spice_tolerance")) == ("hot", "hot")

    s2 = s1.new_conversation("s2")
    s2.say(C("spice_tolerance", value="none", temporal=CONV, evidence="Nothing spicy tonight"))
    assert (s2.durable("spice_tolerance"), s2.active("spice_tolerance")) == ("hot", "none")

    s3 = s2.new_conversation("s3")  # S2 has ended
    assert s3.active("spice_tolerance") == "hot"
    s3.say(C("spice_tolerance", value="none", evidence="I don't like spicy food anymore"))
    assert (s3.durable("spice_tolerance"), s3.active("spice_tolerance")) == ("none", "none")

    s4 = s3.new_conversation("s4")
    s4.say(C("spice_tolerance", value="medium", evidence="Actually, medium spice is fine"))
    assert (s4.durable("spice_tolerance"), s4.active("spice_tolerance")) == ("medium", "medium")


def test_removing_one_allergen_keeps_the_others(store):
    s1 = Chat(store, U_A, "s1")
    s1.say(C("allergen", subject="peanut"), C("allergen", subject="sesame"))
    s2 = s1.new_conversation("s2")
    d = s2.say(C("allergen", "negate", subject="peanut", evidence="no longer allergic to peanuts"))
    assert d.outcome == "confirm"
    assert s2.active("allergen") == {"peanut", "sesame"}  # unchanged until confirmed
    s2.reply(affirmed=True)
    assert s2.durable("allergen") == {"sesame"}


def test_diet_negation_without_replacement_asks(store):
    """Regression (bug 2): 'not vegan anymore' must not silently become omnivore."""
    c = Chat(store, U_A, "s")
    c.say(C("diet", value="vegan"))
    d = c.say(C("diet", "negate", value="vegan", evidence="I'm not vegan anymore"))
    assert d.outcome == "clarify" and d.rule == "R5" and "vegan" in d.question
    assert c.durable("diet") == "vegan" and c.pending is None


def test_diet_with_explicit_replacement_applies(store):
    c = Chat(store, U_A, "s")
    c.say(C("diet", value="vegetarian"))
    d = c.say(C("diet", value="omnivore", evidence="I'm not vegetarian anymore; I eat everything"))
    assert d.rule == "R11" and c.durable("diet") == "omnivore"


def test_negating_an_unheld_scalar_is_noop(store):
    d = Chat(store, U_A, "s").say(C("diet", "negate", value="vegan"))
    assert d.outcome == "noop" and d.rule == "R5"


def test_non_canonical_subject_is_rejected():
    with pytest.raises(SpecError):
        decide(C("allergen", subject="peanuts"), fold([], user_id=U_A, session_id="s"))


# ========================================================================= P4 Scope

def test_overlay_never_touches_durable(store):
    c = Chat(store, U_A, "s")
    c.say(C("diet", value="vegetarian", temporal=CONV), C("budget_max", value=15, temporal=CONV))
    assert c.durable("diet") == "omnivore" and c.durable("budget_max") is None
    assert c.active("diet") == "vegetarian" and c.active("budget_max") == 15


def test_overlay_does_not_block_durable_update(store):
    """Regression (bug 1): a temporary restriction must not make a permanent one a no-op."""
    c = Chat(store, U_A, "s")
    c.say(C("diet", value="vegetarian", temporal=CONV, evidence="Vegetarian for this meal"))
    d = c.say(C("diet", value="vegetarian", evidence="Actually, I'm vegetarian permanently"))
    assert d.outcome == "apply" and c.durable("diet") == "vegetarian"
    assert c.new_conversation("s2").active("diet") == "vegetarian"


def test_durable_restatement_overrides_looser_overlay(store):
    """A durable value that is already right still needs an event when an overlay controls
    what is enforced now."""
    c = Chat(store, U_A, "s")
    c.say(C("diet", value="vegetarian"))
    c.say(C("diet", value="omnivore", temporal=CONV, evidence="I'll have meat tonight"))
    assert c.active("diet") == "omnivore"
    d = c.say(C("diet", value="vegetarian", evidence="no wait, I'm vegetarian"))
    assert d.rule == "R8" and c.active("diet") == "vegetarian"


def test_tonight_does_not_cross_conversations__v1_limitation(store):
    """SPEC §9: 'tonight' is conversation-scoped. Returning 30 minutes later in a new
    conversation drops it. Pinned so that adding clock-time scope is a deliberate change."""
    c = Chat(store, U_A, "dinner")
    c.say(C("spice_tolerance", value="hot"))
    c.say(C("spice_tolerance", value="none", temporal=CONV, evidence="Nothing spicy tonight"))
    assert c.new_conversation("dinner-reopened").active("spice_tolerance") == "hot"


def test_later_durable_statement_beats_earlier_overlay_in_same_conversation(store):
    c = Chat(store, U_A, "s")
    c.say(C("spice_tolerance", value="none", temporal=CONV))
    c.say(C("spice_tolerance", value="hot", evidence="actually I love spicy, give me hot"))
    assert c.active("spice_tolerance") == "hot"


def test_later_overlay_beats_earlier_durable_in_same_conversation(store):
    c = Chat(store, U_A, "s")
    c.say(C("spice_tolerance", value="hot"))
    c.say(C("spice_tolerance", value="none", temporal=CONV))
    assert (c.durable("spice_tolerance"), c.active("spice_tolerance")) == ("hot", "none")


def test_companion_restriction_is_conversation_scoped(store):
    c = Chat(store, U_A, "s")
    d = c.say(C("allergen", subject="tree_nut", temporal=CONV,
                evidence="my daughter is allergic to tree nuts, she's eating with me"))
    assert d.rule == "R8" and c.active("allergen") == {"tree_nut"}
    assert c.new_conversation("s2").active("allergen") == frozenset()


# ===================================================================== P5 Authority

def test_inferred_never_overrides_explicit_even_when_newer():
    evs = as_events(U_A, "s", [D("spice_tolerance", value="none"),
                               D("spice_tolerance", source=INFERRED, value="hot")])
    p = fold(evs, user_id=U_A, session_id="s")
    assert p.durable["spice_tolerance"] == "none" == p.active["spice_tolerance"]


def test_inferred_fills_an_unset_soft_key():
    evs = as_events(U_A, "s", [D("liked_ingredient", source=INFERRED, subject="lamb")])
    assert fold(evs, user_id=U_A, session_id="s").active["liked_ingredient"] == {"lamb"}


@pytest.mark.parametrize("draft", [
    D("diet", source=INFERRED, value="omnivore"),                      # hard
    D("spice_tolerance", scope=CNV, source=INFERRED, value="mild"),    # bug 3: conversation
])
def test_store_rejects_unauthorized_inferred_events(store, draft):
    with pytest.raises(AuthorityError):
        store.append(U_A, "s", [draft])
    assert store.events_for(U_A) == []


@pytest.mark.parametrize("drafts", [
    [D("allergen", Op.REMOVE, source=INFERRED, subject="peanut")],
    [D("spice_tolerance", value="hot"),                                # bug 3 repro
     D("spice_tolerance", scope=CNV, source=INFERRED, value="mild")],
])
def test_fold_rejects_unauthorized_inferred_events(drafts):
    with pytest.raises(AuthorityError):
        fold(as_events(U_A, "s", drafts), user_id=U_A, session_id="s")


# ================================================================== P6 Conservatism

@pytest.mark.parametrize("setup, claim, rule", [
    (C("allergen", subject="peanut"),
     C("allergen", subject="peanut", temporal=PAST, evidence="I used to be allergic to peanuts"),
     "R1"),
    (C("allergen", subject="peanut"),
     C("allergen", "negate", subject="peanut", modality=HEDGED,
       evidence="My peanut allergy might be gone"), "R10"),
    (C("allergen", subject="peanut"),
     C("allergen", "negate", subject="peanut", evidence="I'm no longer allergic to peanuts"), "R10"),
    (C("diet", value="vegan"),
     C("diet", value="vegetarian", modality=HEDGED, evidence="I think I'm vegetarian now"), "R10"),
])
def test_uncertain_or_allergen_loosening_only_proposes(store, setup, claim, rule):
    c = Chat(store, U_A, "s")
    c.say(setup)
    before = c.profile()
    d = c.say(claim)
    assert d.outcome == "confirm" and d.rule == rule and d.question
    assert c.profile() == before  # a pending proposal is not state


def test_past_diet_statement_asks_for_replacement(store):
    c = Chat(store, U_A, "s")
    c.say(C("diet", value="vegetarian"))
    d = c.say(C("diet", value="vegetarian", temporal=PAST, evidence="I used to be vegetarian"))
    assert d.outcome == "clarify" and d.rule == "R1" and c.durable("diet") == "vegetarian"


def test_denied_confirmation_changes_nothing(store):
    c = Chat(store, U_A, "s")
    c.say(C("allergen", subject="peanut"))
    c.say(C("allergen", subject="peanut", temporal=PAST))
    c.reply(affirmed=False)
    assert c.durable("allergen") == {"peanut"}


def test_past_statement_about_inactive_restriction_is_noop(store):
    assert Chat(store, U_A, "s").say(C("diet", value="vegetarian", temporal=PAST)).outcome == "noop"


def test_hedged_tightening_is_enforced_now_and_asks(store):
    c = Chat(store, U_A, "s")
    d = c.say(C("allergen", subject="shellfish", modality=HEDGED,
                evidence="I think I might be allergic to shellfish"))
    assert d.outcome == "apply+confirm" and d.rule == "R9"
    assert c.active("allergen") == {"shellfish"} and c.durable("allergen") == frozenset()
    c.reply(affirmed=True)
    assert c.new_conversation("s2").active("allergen") == {"shellfish"}


def test_aspiration_never_becomes_a_restriction(store):
    c = Chat(store, U_A, "s")
    d = c.say(C("diet", value="vegetarian", temporal=ASPIRE, evidence="trying to eat less meat"))
    assert d.rule == "R3"
    assert c.durable("diet") == "omnivore" and c.durable("diet_lean") == "vegetarian"


def _loosened(before: dict, after: dict) -> bool:
    return (DIET_ORDER.index(after["diet"]) < DIET_ORDER.index(before["diet"])
            or not before["allergen"] <= after["allergen"])


SWEEP_BASES = {
    "durable only": [D("diet", value="vegan"), D("allergen", subject="peanut"),
                     D("allergen", subject="sesame")],
    "looser overlay": [D("diet", value="vegetarian"), D("allergen", subject="peanut"),
                       D("diet", scope=CNV, value="omnivore")],
    "stricter overlay": [D("diet", value="omnivore"), D("diet", scope=CNV, value="vegan"),
                         D("allergen", scope=CNV, subject="sesame")],
}


@pytest.mark.parametrize("base_name", SWEEP_BASES)
def test_exhaustive_no_unauthorized_loosening(base_name):
    """Sweep every temporal x modality x polarity x value for both hard attributes, over
    durable-only and overlay states. Any decision that loosens the durable OR active
    restriction must come from an asserted current/conversation claim on an attribute
    outside ALWAYS_CONFIRM_LOOSENING."""
    base_events = as_events(U_A, "s", SWEEP_BASES[base_name])
    base = fold(base_events, user_id=U_A, session_id="s")

    claims = []
    for t in Temporal:
        for m in Modality:
            for p in Polarity:
                claims += [C("diet", p.value, value=v, temporal=t, modality=m) for v in DIET_ORDER]
                claims += [C("allergen", p.value, subject=s, temporal=t, modality=m)
                           for s in ("peanut", "sesame", "shellfish")]
    assert len(claims) == 140

    permitted = 0
    for claim in claims:
        d = decide(claim, base)
        after = fold(base_events + as_events(U_A, "s", d.events, start=len(base_events) + 1),
                     user_id=U_A, session_id="s")
        if _loosened(base.active, after.active) or _loosened(base.durable, after.durable):
            permitted += 1
            assert claim.temporal in (Temporal.CURRENT, Temporal.CONVERSATION), claim
            assert claim.modality is Modality.ASSERTED, claim
            assert claim.attribute not in ALWAYS_CONFIRM_LOOSENING, claim
            assert claim.polarity is Polarity.ASSERT, claim  # negation never picks a replacement
    assert permitted > 0  # explicit diet replacement exists and is allowed


# ============================================== §7 Confirmation lifecycle (bug 4)

def test_newer_statement_invalidates_pending_removal(store):
    """Regression (bug 4): re-asserting an allergy after a removal question must stop a
    later 'yes' from removing it."""
    c = Chat(store, U_A, "s")
    c.say(C("allergen", subject="peanut"))
    c.say(C("allergen", "negate", subject="peanut", evidence="I'm no longer allergic to peanuts"))
    stale_id = c.pending.proposal_id
    d = c.say(C("allergen", subject="peanut", evidence="Actually, I definitely am allergic"))
    assert d.rule == "R7" and c.pending is None
    with pytest.raises(StaleConfirmation):
        c.answer(stale_id, affirmed=True)
    assert c.active("allergen") == {"peanut"}


def test_change_from_another_conversation_makes_confirmation_stale(store):
    phone = Chat(store, U_A, "phone")
    phone.say(C("allergen", subject="peanut"))
    phone.say(C("allergen", "negate", subject="peanut"))
    laptop = Chat(store, U_A, "laptop")  # same user, second device
    laptop.say(C("allergen", "negate", subject="peanut"))
    laptop.reply(affirmed=True)  # writes REMOVE peanut after the phone's question was asked
    laptop.say(C("allergen", subject="peanut", evidence="actually I am allergic"))  # re-added
    # State now looks exactly as when the phone asked, so only the freshness check
    # (key touched after base_sequence) can tell that the phone's 'yes' is stale.
    with pytest.raises(StaleConfirmation):
        phone.reply(affirmed=True)
    assert phone.durable("allergen") == {"peanut"}


def test_confirmation_is_bound_to_user_and_conversation(store):
    c = Chat(store, U_A, "s")
    c.say(C("allergen", subject="peanut"))
    c.say(C("allergen", "negate", subject="peanut"))
    p: Pending = c.pending
    for user, session in ((U_B, "s"), (U_A, "other")):
        with pytest.raises(StaleConfirmation):
            resolve(p, True, user_id=user, session_id=session, events=store.events_for(U_A))


def test_one_pending_at_a_time(store):
    c = Chat(store, U_A, "s")
    c.say(C("allergen", subject="peanut"), C("allergen", subject="sesame"))
    c.say(C("allergen", "negate", subject="peanut"))
    first = c.pending.proposal_id
    c.say(C("allergen", "negate", subject="sesame"))
    with pytest.raises(StaleConfirmation):
        c.answer(first, affirmed=True)  # replaced; answering it does nothing
    c.reply(affirmed=True)
    assert c.durable("allergen") == {"peanut"}


def test_proposal_that_no_longer_changes_anything_is_rejected():
    events = as_events(U_A, "s", [D("allergen", subject="peanut"),
                                  D("allergen", Op.REMOVE, source=CONFIRMED, subject="peanut")])
    p = Pending("p1", U_A, "s", D("allergen", Op.REMOVE, subject="peanut"), "?", base_sequence=2)
    with pytest.raises(StaleConfirmation):
        resolve(p, True, user_id=U_A, session_id="s", events=events)


# ==================================================================== determinism

def test_fold_is_independent_of_input_order():
    drafts = [D("spice_tolerance", value=v) for v in ("hot", "none", "medium")]
    drafts += [D("allergen", subject="peanut"), D("allergen", scope=CNV, subject="sesame"),
               D("allergen", Op.REMOVE, source=CONFIRMED, subject="peanut")]
    evs = as_events(U_A, "s", drafts)
    expected = fold(evs, user_id=U_A, session_id="s")
    rng = random.Random(7)
    for _ in range(50):
        shuffled = evs[:]
        rng.shuffle(shuffled)
        assert fold(shuffled, user_id=U_A, session_id="s") == expected
    assert expected.active["spice_tolerance"] == "medium"
    assert expected.active["allergen"] == {"sesame"} and expected.durable["allergen"] == frozenset()


def test_same_timestamp_is_ordered_by_sequence(store):
    evs = store.append(U_A, "s", [D("spice_tolerance", value="hot"),
                                  D("spice_tolerance", value="none")])
    assert evs[0].created_at == evs[1].created_at
    assert store.profile(U_A, "s").active["spice_tolerance"] == "none"


# ============================================ extractor_cases.json label consistency

CASES = json.loads((Path(__file__).parent / "extractor_cases.json").read_text())["cases"]


def _profile_drafts(d: dict):
    out = []
    for attr, v in d.items():
        if isinstance(v, list):
            out += [D(attr, subject=s) for s in v]
        else:
            out.append(D(attr, value=v))
    return out


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_labels_agree_with_rules(case):
    """Labels are the spec in example form: each expected claim, decided in order against
    the case's starting profile, must yield the labeled outcome."""
    claims = [Claim.from_dict(c) for c in case["expected_claims"]]
    assert len(claims) == len(case["expected_outcomes"])
    events = as_events(U_A, "s", _profile_drafts(case["profile"]))
    for claim, expected in zip(claims, case["expected_outcomes"]):
        d = decide(claim, fold(events, user_id=U_A, session_id="s"))
        assert d.outcome == expected, f"{case['id']}: {claim} -> {d.rule} {d.reason}"
        events += as_events(U_A, "s", d.events, start=len(events) + 1)
