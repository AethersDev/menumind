# MenuMind: Preference Memory Specification v1

Status: implementation contract.
Code: `memory.py` · Tests: `test_memory.py` · Extractor eval set: `extractor_cases.json`

---

## 0. Scope

**In scope:** the extractor output contract, authorization rules, the event log, the fold, conversation overlays, and safety invariants.

**Out of scope for v1:**
- **Feedback learning.** No `inferred` events are produced. The fold still enforces their precedence so learning can be added later without changing the semantics.
- **Clock-time scope** beyond a single conversation (§9).
- **Menu filtering, ranking, and grounded rendering.** Implemented outside this spec in `recommend.py`, and described in `README.md`.
- **A comparison of this design against Architecture A.** Optional, only if time remains.

## 1. Pipeline

```
utterance ──LLM extractor──▶ Claim[]                 what was said        (probabilistic)
Claim + active Profile ──decide()──▶ Decision         what is authorized   (deterministic)
Decision.events   ──EventStore.append()──▶ event log  append-only, per user
Decision.proposal ──▶ pending confirmation            conversation-local, never folded
event log ──fold()──▶ Profile {durable, active}       (deterministic)
```

The LLM never chooses an operation. It classifies the statement, and code decides what changes.

## 2. Preference model

The **canonical key** is `(attribute, subject)`. Most-recent-wins applies per canonical key, never per attribute category. Removing `allergen:peanut` leaves `allergen:sesame` untouched.

| attribute | kind | hardness | values / subjects | default |
|---|---|---|---|---|
| `diet` | scalar | hard | omnivore < pescatarian < vegetarian < vegan | omnivore |
| `allergen` | set | hard | peanut, tree_nut, sesame, shellfish, fish, dairy, egg, gluten, soy | ∅ |
| `spice_tolerance` | scalar | soft | none < mild < medium < hot | unset |
| `diet_lean` | scalar | soft | same values as `diet` | unset |
| `budget_max` | scalar | soft | positive number | unset |
| `disliked_ingredient` | set | soft | canonical ingredient names | ∅ |
| `liked_ingredient` | set | soft | canonical ingredient names | ∅ |

**Subjects must be canonical.**
- `allergen` subjects come from a closed vocabulary, validated at decide-time and write-time. If `peanuts` and `peanut` became two keys, a REMOVE would silently miss the stored restriction. Canonical form is what makes per-key supersession correct.
- Ingredient subjects are lowercase, underscore-separated, and singular. They will be validated against the menu's ingredient vocabulary once the menu exists.

**Hard attributes are filters.** The recommender can never relax them, and §5 governs any loosening.
- `budget_max` is applied as a filter by the recommender, but it is *soft for authorization*: loosening a budget needs no confirmation.
- Hard scalars are ordered by restrictiveness, which makes *tighten* and *loosen* computable. For example, vegan → vegetarian is a loosening.

## 3. Claim: the extractor output contract

| field | values |
|---|---|
| `attribute` | a key from §2 |
| `polarity` | `assert` \| `negate` |
| `subject` | canonical member (set attributes only) |
| `value` | scalar value (scalar attributes only; for `negate`, the value being negated) |
| `temporal` | `current` \| `conversation` \| `past` \| `aspirational` \| `hypothetical` |
| `modality` | `asserted` \| `hedged` |
| `evidence` | exact words supporting the claim |

**No confidence score is part of the contract.** See §6.

Classification guidance (the eval set in `extractor_cases.json` tests each of these):

- **Restriction vs. preference.** Identity or restriction language ("I'm vegetarian", "I can't eat", "allergic") maps to hard attributes. Preference language ("I'd prefer", "in the mood for", "trying to") maps to soft attributes: `diet_lean`, not `diet`.
- **`conversation`.** Bounded to this meal or visit: "tonight", "today", "for this order", or the restrictions of a companion who is eating now.
- **`past`.** Describes a former state only ("I used to be…"). If past phrasing comes with an explicit present statement ("I used to be allergic but I outgrew it"), the claim is `current` + `negate`.
- **`hedged`.** "might", "I think", "probably", "not sure".
- **No claims.** Questions ("Does the pad thai have peanuts?") and statements about absent third parties produce no claims.
- **Ambiguous subjects.** "nuts" produces a claim for each plausible canonical subject (`peanut`, `tree_nut`). Tightening is cheap.
- **Replacements.** If a statement names what replaces a diet ("I'm not vegetarian anymore; I eat everything"), emit only the replacement (`diet` assert `omnivore`). A bare negation ("I'm not vegetarian anymore") is emitted as `negate`, and the rules ask what replaces it.

## 4. Event schema

```
PreferenceEvent
  event_id         uuid
  user_id          server-issued; never supplied by the LLM
  session_id       server-issued per conversation
  sequence_number  per-user, strictly increasing, UNIQUE(user_id, sequence_number)
  attribute
  subject          NULL for scalars
  value            NULL for set members and for REMOVE
  operation        SET | REMOVE        the authorized transition
  scope            durable | conversation
  source           stated | confirmed | inferred
  evidence
  created_at       informational only
  expires_at       reserved; always NULL in v1
```

- **`operation`** records the authorized transition. The fold never reconstructs intent from the value.
- **`sequence_number`** is the only ordering key. It is assigned inside the append transaction. Events written in one batch share `created_at` and are ordered by sequence.
- **Inferred events** may only target soft attributes, and only with `durable` scope. Anything else is rejected at write time *and* at fold time. Conversation-scoped inference has no defined precedence in v1, so it is not representable.
- **The log is append-only.** Pending proposals are never events.

**Known limitation: concurrent read-decide-append (v1).** `append` is atomic, but the sequence *read profile → `decide()` → append* is not. If device B writes between device A's read and A's append, A's decision was made against an outdated profile. The exposure is narrow:
- Confirmations are already protected by §7.
- Explicit assertions write the stated value whatever the profile says.
- What remains: a hedged claim whose tighten/loosen classification flips between the two reads. For example, A sees vegan, B switches to omnivore, and A's hedged "I think I'm vegetarian" is then treated as a tightening and applied without a question.

**Status in the app:** `agent.Agent` runs every memory step (handle, answer, profile read) under one process-wide lock, so on a single server process the read → decide → append sequence is atomic and this gap is closed. It reopens if the app runs as several processes or instances. The fix then is optimistic concurrency confined to `Conversation.handle`: read the profile together with `last_sequence`, pass that number to `append` as an expected value, and on a mismatch re-read and re-run `decide()`.

## 5. Decision rules (`decide`)

Rules are evaluated in order. A claim's scope is `conversation` if `temporal = conversation`, otherwise `durable`.

Outcomes:
- **APPLY:** write events.
- **CONFIRM:** propose a specific change and ask yes/no.
- **CLARIFY:** ask an open question; the answer arrives as a new utterance and is decided like any other.
- **NOOP:** no change.

| # | Condition | Outcome |
|---|---|---|
| R1 | `past`, hard, asserts a restriction the **durable** profile holds | allergen: **CONFIRM** a durable REMOVE · diet: **CLARIFY** the replacement |
| R2 | otherwise `past` or `hypothetical` | NOOP |
| R3 | `aspirational`, `diet`, `assert` | APPLY `diet_lean` SET (durable) |
| R4 | `aspirational`, any other hard attribute | NOOP |
| R5 | `negate` of a scalar | value not held → NOOP · held, soft → falls through to R6 (unset) · held, hard → **CLARIFY** the replacement |
| R6 | soft attribute (including aspirational soft) | APPLY at claim scope |
| R7 | hard attribute, no effect (§5.1) | NOOP |
| R8 | hard **tighten**, asserted *or* conversation-scoped | APPLY at claim scope |
| R9 | hard **tighten**, hedged, durable | APPLY a conversation-scoped change now; **CONFIRM** the durable change only if the durable profile lacks it |
| R10 | hard **loosen**, hedged, *or* the attribute is `allergen` | **CONFIRM** (no events) |
| R11 | hard **loosen**, asserted, not `allergen` | APPLY |

Every NOOP, CONFIRM, and CLARIFY is shown to the user in the conversation. None of them is silent.

### 5.1 Which state a claim is judged against

- **Conversation claims** are compared with the **active** profile, because that is all they can change.
- **Durable claims** are compared with **both** the durable and the active profile, and the effects combine as: any loosening → *loosen*; otherwise any tightening → *tighten*; otherwise *none*.

Two cases this rule exists for:
- "Vegetarian for this meal", then "I'm vegetarian permanently": active already says vegetarian, but durable doesn't. That's a tightening, so the durable write happens. Comparing against active alone made it a NOOP.
- Durable vegetarian, then "meat tonight", then "I'm vegetarian": durable is already right, but the overlay controls what's enforced now. That's a tightening of active, so an event is written and the overlay is overridden.

### 5.2 Negation never chooses a replacement

`REMOVE` on a hard scalar would reset it to the default (`omnivore`). "I'm not vegan anymore" says nothing about whether the user is now vegetarian or omnivore, so R1 and R5 ask instead. A diet changes only through an assertion that names the new value.

## 6. Authorization comes from the operation, not from confidence

LLM confidence is not calibrated. A model can be 0.99 confident that "I used to be vegetarian" is a `past` statement, and still nothing in that tells you what the user wants now. So **R1–R11 key on the semantic operation and its risk, never on a score.**

R10 goes one step further than "explicit changes may remove": **allergen removals always require confirmation.** Whether a statement is "explicit" is itself an LLM classification. For allergens the costs are lopsided: one extra question versus serving an allergen. Making confirmation unconditional removes extractor accuracy from the safety path for the highest-severity key.

The switch is `ALWAYS_CONFIRM_LOOSENING` in `memory.py`. Diet loosening is not in that set.

## 7. Confirmation lifecycle

Implemented by `Conversation` and `resolve()` in `memory.py`.

- **Binding.** A `Pending` proposal carries a `proposal_id`, its `user_id` and `session_id`, the proposed change, and `base_sequence` (the user's last sequence number when the question was asked). It is not persisted in v1 and is abandoned when the conversation ends.
- **One at a time.** A new proposal replaces the pending one. An answer that names a replaced or unknown `proposal_id` is rejected.
- **Invalidation.** Any newer claim on the same canonical key clears the pending proposal, whether or not it writes an event. This matters because a re-assertion of a stored allergy is R7 and writes nothing, so no event-based check could see it.
- **Freshness.** On *yes*, `resolve()` rejects the answer (`StaleConfirmation`) if:
  - it comes from a different user or conversation;
  - any event touched the proposal's key after `base_sequence` (for example, a change made from another device); or
  - the proposed change would no longer change anything.
- **Affirm:** the proposal is appended as an event with `source = confirmed`.
- **Deny:** nothing is appended. An R9 conversation overlay stays in place.
- Until a proposal is resolved, it does not change the profile. The existing restriction stays enforced.
- **Handling a reply while a question is pending:**
  1. The reply goes through the extractor first. Any claim on the pending key invalidates the question. For example, "no, I'm still allergic" is an R7 re-assertion, and "yep, I outgrew it" replaces the question with a new one on the same key.
  2. If a question is still pending, `classify_reply()` labels the reply `yes`, `no`, or `unclear`.
  3. `yes` and `no` resolve the question. `unclear` changes nothing: the question stays pending and is asked again.
  4. Any model or parsing failure counts as `unclear`.
- The reply classifier is evaluated separately against `confirmation_cases.json` (§11).

## 8. Fold semantics

`fold(events, user_id, session_id) → Profile {durable, active}`

1. **Reject** any event whose `user_id` differs from the requested one (`IsolationError`), duplicate sequence numbers, and inferred events that target a hard attribute or have conversation scope (`AuthorityError`).
2. **Sort** by `sequence_number`.
3. **For each canonical key:**
   - *Durable value:* the last explicit (`stated` | `confirmed`) durable event. If there is none, the last inferred durable event.
   - *Active value:* the last explicit event among durable events and the conversation events of *this* `session_id`. If there is none, the last inferred durable event.
4. **Materialize:**
   - Set attribute: SET means the member is present; REMOVE means it is absent.
   - Scalar attribute: SET takes the value; REMOVE resets to the default.

Consequences:
- **Within a conversation, the latest statement wins regardless of scope.** "Nothing spicy tonight" followed by "actually, I love spicy, give me hot" leaves the active value at `hot`. An overlay does not shadow a later durable statement.
- **Across conversations, only durable events count.**
- **Explicit always beats inferred**, regardless of recency.

## 9. Session semantics

A session is one conversation. The server issues a `session_id` at conversation start. Conversation-scoped events are invisible to the fold in any other session.

**Documented limitation:** "Nothing spicy tonight", then the user closes the app and returns 30 minutes later: the overlay is gone. v1 does not model clock-time scope and makes no claim to. A test (`test_tonight_does_not_cross_conversations__v1_limitation`) pins this behavior so that changing it has to be deliberate.

Future extension: `scope = temporal`, with `expires_at` set by the decide step and `fold(…, now)` filtering on it.

## 10. Safety invariants

| | Property | Statement |
|---|---|---|
| P1 | Isolation | `fold` for user U reads only U's events. The store queries by `user_id`, and `fold` rejects foreign events. Session ids are not a privacy boundary: two users sharing a `session_id` see nothing of each other. |
| P2 | Persistence | Durable events survive process restart and new sessions. |
| P3 | Supersession | For each canonical key, the latest explicit event determines the value. Other keys of the same attribute are unaffected. |
| P4 | Scope | Conversation events never change the durable profile and are invisible in other sessions. |
| P5 | Authority | Inferred events never override explicit events, never target hard attributes, and are never conversation-scoped (v1). |
| P6 | Conservatism | No hard restriction, durable or active, is loosened except by an asserted `current`/`conversation` claim that names the new value on a non-allergen attribute, or by a fresh, confirmed proposal (§7). Negation never chooses a replacement. |

## 11. Test layers

| Layer | What it establishes | LLM? | Where |
|---|---|---|---|
| Unit (pure) | decide rules R1–R11; fold precedence; independence from input order; an exhaustive P6 sweep over every combination of temporal × modality × polarity × value, run from three starting states (durable only, looser overlay, stricter overlay) and checking both durable and active; the eval labels agree with the rules | No | `test_memory.py` |
| Integration (SQLite) | P1 including shared session ids; P2 across a store reopen; per-user sequencing; write-time P5 rejection; the §7 lifecycle, including a stale answer from a second device | No | `test_memory.py` |
| Integration (app) | Menu validation; hard filtering over all 2,048 diet × allergen combinations; reply checks and rendering; full chat turns with a scripted model; HTTP sign-in, isolation, and restart persistence | No | `test_app.py` |
| Scorer (pure) | Every metric below, checked against an oracle predictor and against single known mistakes; extractor plumbing via a fake client | No | `test_eval.py` |
| LLM eval | Extractor against `extractor_cases.json`; reply classifier against `confirmation_cases.json` | Yes | `eval_extractor.py` |

**What the pure tests cannot prove:** that the LLM steps produce the right output. That is the job of the LLM eval layer. The extractor and the reply classifier are the only probabilistic parts of the system.

**Extractor contract at runtime.** Output comes from structured outputs with a fixed JSON schema, then is validated against the memory vocabulary (`parse_claims`). On an API error or invalid output the call is retried once. If it is still invalid, nothing changes and the agent asks the user to rephrase.

**How the extractor is scored.** Each case runs through the real `Conversation` twice from the same starting profile: once with the labeled claims, once with the predicted claims. The restriction metrics compare the resulting profiles, so equivalent claim sets score the same. Every restriction counts separately, in two layers: *active* (enforced in this conversation) and *durable* (remembered for future visits). For example, "peanuts and sesame" predicted as peanut only is one restriction missed of two, in each layer.

| Metric | Definition |
|---|---|
| Restriction recall (**headline**) | Expected hard tightenings present in the predicted profile. Reported as counts (covered/total), separately for active and durable. |
| Unsafe loosenings applied (**headline**) | Restrictions held before and still expected after, but loosened in the predicted profile. |
| Unsafe loosenings proposed (**headline**) | Confirmation questions the prediction raised that the labels did not, which would loosen a restriction if answered *yes*. The profile is unchanged at that point, so profile comparison alone cannot see these. |
| Spurious restrictions | Hard restrictions added by the prediction that neither the starting profile nor the labels have. Over-restriction; not a safety failure. |
| Decision accuracy | The predicted run matches the labeled run on durable profile, active profile, **every** confirmation question raised (attribute, subject, operation, value, scope, counted with multiplicity, including any a later question replaced), and the clarify flag. |
| Exact claim accuracy | The predicted claim multiset equals the labels on `(attribute, polarity, subject, value, temporal, modality)`. |
| Invalid output rate | Cases whose output failed validation after the retry. They also count as missed. |
| Latency, tokens, cost | Wall-clock per extraction including retries (mean/p50/p95); input and output tokens; cost in USD when prices are passed with `--price-in` and `--price-out` (USD per million tokens). Prices aren't hard-coded, so they can't go stale. |

Current denominators: **10 active and 8 durable** restrictions across 32 cases. These are small. A perfect score shows the model handles these phrasings, not that it will catch every restriction in unfamiliar conversation. The report prints counts rather than rates for that reason.

**Model selection** (feeds §12.5):
- Both models clean on the safety metrics → choose the cheaper and faster one.
- One misses a restriction or makes an unsafe loosening the other doesn't → rerun those cases with `--repeats`. If the failure persists, choose the model that handles restrictions.
- Both fail the same case → fix the extraction contract or prompt before considering a larger model.

**Reply classifier.** Scored by accuracy and **false yes**: the number of replies predicted `yes` whose label is `no` or `unclear`. `yes` is the only answer that changes state, so false yes is the headline metric.

## 12. Open decisions

1. Allergen always-confirm: **on**. Flip with `ALWAYS_CONFIRM_LOOSENING`.
2. When a conversation ends: an explicit "new chat" from the user, or an idle timeout of N minutes. Pick N.
3. The ingredient vocabulary depends on the menu. Until then, ingredient subjects are normalized but not validated.
4. Soft past statements ("I used to love spicy food") are NOOP (R2). The agent may ask in conversation, but state does not change.
5. Extractor model: `claude-haiku-5-5` by default. Run the eval on `claude-sonnet-5-5` too, and switch only if Haiku shows unsafe loosening errors or misses restrictions that Sonnet catches. Accuracy differences that don't touch restrictions are not a reason to switch.
