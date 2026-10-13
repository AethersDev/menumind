"""LLM steps: utterance -> Claim[] (SPEC §3) and confirmation reply -> yes/no/unclear (§7).

Both use structured outputs: the model's reply must follow a JSON schema (llm.py sends it to
whichever provider is configured). Whatever the model returns is then validated against
memory.py's vocabulary. Invalid output is reported, never repaired by guessing: the runtime
policy is no state change and a request to rephrase.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from llm import DEFAULT_MODEL, MODEL_ERRORS
from memory import ATTRIBUTES, Claim, Modality, Temporal, _check_value, canonical_subject

MAX_TOKENS = 4096  # headroom for models that think by default

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------ extraction

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "attribute": {"type": "string", "enum": list(ATTRIBUTES)},
                    "polarity": {"type": "string", "enum": ["assert", "negate"]},
                    "subject": {"type": ["string", "null"]},
                    "value": {"anyOf": [{"type": "string"}, {"type": "number"},
                                        {"type": "null"}]},
                    "temporal": {"type": "string", "enum": [t.value for t in Temporal]},
                    "modality": {"type": "string", "enum": [m.value for m in Modality]},
                    "evidence": {"type": "string"},
                },
                "required": ["attribute", "polarity", "subject", "value", "temporal",
                             "modality", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

# The examples here must never be copied from extractor_cases.json (test_eval.py checks).
EXTRACT_SYSTEM = """\
You convert one message from a restaurant customer into preference claims. You record only \
what the message says about the customer's food preferences and restrictions. You never \
decide what to do with them.

Return {"claims": [...]}. Return an empty list when the message states no preference: \
questions about dishes, small talk, orders, and statements about people who are not eating \
with the customer.

ATTRIBUTES
- diet (hard): omnivore | pescatarian | vegetarian | vegan. Only for identity or restriction \
language ("I'm vegan", "I don't eat meat").
- allergen (hard, set): subject is one of peanut, tree_nut, sesame, shellfish, fish, dairy, \
egg, gluten, soy. Map specific foods: cashew/almond/walnut/pistachio -> tree_nut; \
shrimp/crab/lobster -> shellfish; milk/cheese/cream -> dairy; wheat -> gluten. "Nuts" that \
names no particular nut means both peanut and tree_nut: emit both claims, because customers \
often count peanuts as nuts. When the customer corrects which food they react to ("not X, \
it's Y"), Y is an allergen, never a like.
- spice_tolerance (soft): none | mild | medium | hot. Loving or wanting spicy food is assert \
"hot". Express dislike of spice as assert "none", never as negate. Spice is never an \
ingredient.
- diet_lean (soft): same values as diet. For preference language ("I'd prefer", "in the mood \
for", "leaning toward").
- budget_max (soft): a positive number of dollars.
- disliked_ingredient, liked_ingredient (soft, set): subject is the ingredient, lowercase, \
singular, spaces replaced by underscores. A customer who no longer minds a food they disliked \
negates disliked_ingredient; that is not a like.

FIELDS
- polarity: "assert" = the value or membership holds; "negate" = it does not or no longer holds. \
With temporal past, polarity describes the past state: "I used to be vegan" is diet assert \
vegan past.
- subject: required for allergen, disliked_ingredient, liked_ingredient; null otherwise.
- value: required for diet, diet_lean, spice_tolerance, budget_max (for negate, the value \
being negated); null for set attributes.
- temporal:
  current: true now and going forward (default).
  conversation: limited to this meal, visit or order ("tonight", "today", "this time"), or \
a restriction of someone eating with the customer now.
  past: only describes how things used to be ("I used to ..."). If the message also says how \
things are now ("... but I outgrew it"), describe the present instead, as current.
  aspirational: something the customer is trying to do ("trying to cut back on ...").
  hypothetical: conditional, counterfactual, or a possible future ("if I ...", "I might start").
- modality: "hedged" if the customer is unsure ("I think", "might", "maybe", "not sure"); \
otherwise "asserted".
- evidence: the exact words the claim rests on.

RULES
- If a diet change names the new diet, emit only the new diet as assert. If it only says \
what the customer no longer is, emit negate with the old value.
- Never infer a preference from a question or an order. "Does this have sesame?" is not an \
allergy.
- Claims are only about the customer and anyone eating with them now. Another person's \
diet or allergy is never the customer's, even when the message compares the two.
- One claim per fact.

EXAMPLES
"No shrimp for me, I react badly to it. Something mild tonight." ->
  allergen assert shellfish current asserted; spice_tolerance assert mild conversation asserted
"Can you keep it under 25 dollars this time? Also I can't stand cilantro." ->
  budget_max assert 25 conversation asserted; disliked_ingredient assert cilantro current asserted
"Is the curry vegan?" -> no claims
"""


@dataclass
class Extraction:
    claims: list[Claim]
    error: Optional[str] = None
    raw: Any = None
    stats: dict = field(default_factory=dict)  # latency_s, input_tokens, output_tokens, calls


_ENUM_FIELDS = ("attribute", "polarity", "temporal", "modality")


def parse_claims(payload: Any) -> Extraction:
    """Validate a model payload against the memory vocabulary. Pure.

    The payload is accepted or rejected whole, so one bad claim never lets the others
    through: a message is applied completely or not at all."""
    try:
        claims = []
        for item in payload["claims"]:
            d = dict(item)
            for k in _ENUM_FIELDS:  # enums may come back in another case
                if isinstance(d.get(k), str):
                    d[k] = d[k].strip().lower()
            spec = ATTRIBUTES[d["attribute"]]
            v = d.get("value")
            if spec.values is not None and isinstance(v, str):
                d["value"] = v.strip().lower()
            if spec.numeric and isinstance(v, float) and not math.isfinite(v):  # NaN, Infinity
                raise ValueError(f"{d['attribute']} must be a finite number")
            if spec.numeric and isinstance(v, float) and v.is_integer():
                d["value"] = int(v)
            c = Claim.from_dict(d)
            if not isinstance(c.evidence, str):  # the event log requires it (NOT NULL)
                raise TypeError("evidence must be text")
            _check_value(c.attribute, c.value, required=True)
            claims.append(replace(c, subject=canonical_subject(c.attribute, c.subject)))
        return Extraction(claims, raw=payload)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        return Extraction([], error=f"{type(e).__name__}: {e}", raw=payload)


class _Stats:
    """Accumulates latency and token usage across calls, retries included."""

    def __init__(self):
        self.d = {"latency_s": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

    def call(self, client, model: str, system: str, user: str, schema: dict) -> Any:
        """The parsed JSON reply. Raises one of MODEL_ERRORS when the call fails and
        ValueError when the reply is not JSON."""
        t0 = time.perf_counter()
        try:
            resp = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        finally:
            self.d["latency_s"] = round(self.d["latency_s"] + time.perf_counter() - t0, 4)
            self.d["calls"] += 1
        usage = getattr(resp, "usage", None)
        self.d["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        self.d["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise ValueError(f"no text in the reply (stop_reason {getattr(resp, 'stop_reason', None)})")
        return json.loads(text)


def extract(utterance: str, *, client, model: str = DEFAULT_MODEL, retries: int = 1) -> Extraction:
    """Utterance -> validated claims. Retries once when the output is invalid. API errors
    are not retried here: the client's own retries already covered the transient ones."""
    stats = _Stats()
    last = Extraction([], error="not attempted")
    for _ in range(retries + 1):
        try:
            payload = stats.call(client, model, EXTRACT_SYSTEM, utterance, CLAIM_SCHEMA)
        except MODEL_ERRORS as e:
            last = Extraction([], error=f"{type(e).__name__}: {e}")
            break
        except ValueError as e:  # not JSON: ask again
            last = Extraction([], error=f"{type(e).__name__}: {e}")
            continue
        last = parse_claims(payload)
        if last.error is None:
            break
    last.stats = stats.d
    return last


# ------------------------------------------------------------------ confirmation replies

REPLY_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no", "unclear"]}},
    "required": ["answer"],
    "additionalProperties": False,
}

REPLY_SYSTEM = """\
A restaurant assistant asked the customer a yes/no question about changing their saved \
preferences. Classify the customer's reply to that question.
- yes: clearly agrees to exactly the change asked about.
- no: clearly declines it, or asks to keep things as they are. Limiting the change to now \
("only this time") declines saving it for future visits.
- unclear: anything else, including hedges ("I guess", "probably"), questions back, a \
different change than the one asked about, or no answer.
When in doubt, answer unclear. "unclear" changes nothing; the assistant will ask again.
Other preferences mentioned in the reply are handled separately; classify only the answer \
to the question.
"""

REPLY_ANSWERS = ("yes", "no", "unclear")


def classify_reply_with_stats(question: str, reply: str, *, client,
                              model: str = DEFAULT_MODEL) -> tuple[str, dict]:
    """Return (yes | no | unclear, stats). A failed call or unusable output is 'unclear',
    which changes nothing (SPEC §7). Errors in our own code still raise."""
    stats = _Stats()
    try:
        payload = stats.call(client, model, REPLY_SYSTEM,
                             f"Question: {question}\nReply: {reply}", REPLY_SCHEMA)
        answer = str(payload["answer"]).strip().lower()
    except (*MODEL_ERRORS, ValueError, KeyError, TypeError) as e:
        log.warning("reply classification failed, treated as unclear: %s", type(e).__name__)
        return "unclear", stats.d
    return (answer if answer in REPLY_ANSWERS else "unclear"), stats.d


def classify_reply(question: str, reply: str, *, client, model: str = DEFAULT_MODEL) -> str:
    return classify_reply_with_stats(question, reply, client=client, model=model)[0]
