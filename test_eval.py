"""Checks the scorer and the extractor plumbing without calling a model.

The scorer reports the numbers the extractor will be judged by, so it is tested against
predictors whose errors are known exactly: an oracle, and single targeted mistakes.
"""
import inspect
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from openai.resources.chat.completions import Completions

from eval_extractor import cost_usd, evaluate_confirmations, evaluate_extractor, load, render
from agent import TURN_SCHEMA
from extractor import (CLAIM_SCHEMA, EXTRACT_SYSTEM, MAX_TOKENS, REPLY_SCHEMA, classify_reply,
                       classify_reply_with_stats, extract, parse_claims)
from llm import OpenAIClient
from memory import ATTRIBUTES, Claim, Temporal

CASES = load("extractor_cases.json")["cases"]
API_DOWN = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
BY_UTTERANCE = {c["utterance"]: c for c in CASES}


def oracle(overrides=None):
    """Predict the labels, except for utterances in `overrides` (utterance -> (claims, error))."""
    overrides = overrides or {}

    def predict(utterance):
        if utterance in overrides:
            return overrides[utterance]
        return [Claim.from_dict(c) for c in BY_UTTERANCE[utterance]["expected_claims"]], None
    return predict


def claim(**kw):
    d = {"temporal": "current", "modality": "asserted", **kw}
    return Claim.from_dict(d)


# ------------------------------------------------------------------------- scorer

def test_oracle_scores_perfectly():
    s = evaluate_extractor(CASES, oracle())["summary"]
    assert s["exact_claim_accuracy"] == 1.0 and s["decision_accuracy"] == 1.0
    assert s["invalid_output_rate"] == 0.0
    assert s["unsafe_loosening_errors"] == {"applied": 0, "proposed": 0}
    assert s["spurious_restrictions"] == 0 and s["decision_mismatches"] == []
    for layer in ("active", "durable"):
        assert s["restriction_recall"][layer]["recall"] == 1.0
    # Denominators, counted by hand from the labels: diet x3, allergens x7 enforced now;
    # the hedged (alg-15) and companion (alg-16) allergies are not remembered.
    assert s["restriction_recall"]["active"]["total"] == 10
    assert s["restriction_recall"]["durable"]["total"] == 8


def test_missed_restrictions_are_counted_individually():
    """'peanuts and sesame' predicted as peanut only: 1 of 2 restrictions missed, per layer."""
    u = "I'm allergic to peanuts and sesame."
    s = evaluate_extractor(CASES, oracle({u: ([claim(attribute="allergen", polarity="assert",
                                                     subject="peanut")], None)}))["summary"]
    assert sorted(s["missed"]) == [["alg-11-two-allergens", "active", "allergen", "sesame"],
                                   ["alg-11-two-allergens", "durable", "allergen", "sesame"]]
    assert s["exact_claim_accuracy"] < 1.0 and s["decision_accuracy"] < 1.0
    full = evaluate_extractor(CASES, oracle())["summary"]["restriction_recall"]["active"]["total"]
    assert s["restriction_recall"]["active"]["covered"] == full - 1


def test_unsafe_loosening_is_detected():
    """'Not vegetarian anymore' extracted as an explicit switch to omnivore loosens the diet
    without the replacement question."""
    u = "I'm not vegetarian anymore."
    s = evaluate_extractor(CASES, oracle({u: ([claim(attribute="diet", polarity="assert",
                                                     value="omnivore")], None)}))["summary"]
    assert s["unsafe_loosening_errors"] == {"applied": 2, "proposed": 0}  # active and durable
    assert {tuple(x[:2]) for x in s["unsafe_applied"]} == {("veg-06-not-anymore", "active"),
                                                           ("veg-06-not-anymore", "durable")}


def test_unsafe_proposal_is_caught_when_profiles_are_identical():
    """Expected NOOP; predicted claim raises a removal question. Nothing has changed yet, so
    the profiles match, but a 'yes' would remove the allergy."""
    u = "Still allergic to peanuts, by the way."
    s = evaluate_extractor(CASES, oracle({u: ([claim(attribute="allergen", polarity="negate",
                                                     subject="peanut")], None)}))["summary"]
    assert s["unsafe_loosening_errors"] == {"applied": 0, "proposed": 1}
    assert s["unsafe_proposed"] == [["alg-19-still-allergic", "allergen", "peanut", "REMOVE",
                                     None, "durable"]]
    assert s["decision_mismatches"] == ["alg-19-still-allergic"]


def test_proposal_content_matters_not_just_its_presence():
    """Both runs ask a removal question, but about different allergens."""
    u = "I'm no longer allergic to peanuts."
    s = evaluate_extractor(CASES, oracle({u: ([claim(attribute="allergen", polarity="negate",
                                                     subject="sesame")], None)}))["summary"]
    assert [x[2] for x in s["unsafe_proposed"]] == ["sesame"]
    assert s["decision_mismatches"] == ["alg-12-no-longer-peanut"]


def test_replaced_proposal_is_still_counted():
    """Two removal questions in one utterance: the second replaces the first as the pending
    question, so the final pending state matches the labels. The first must still count."""
    u = "I'm no longer allergic to peanuts."
    wrong_then_right = [claim(attribute="allergen", polarity="negate", subject="sesame"),
                        claim(attribute="allergen", polarity="negate", subject="peanut")]
    s = evaluate_extractor(CASES, oracle({u: (wrong_then_right, None)}))["summary"]
    assert [x[2] for x in s["unsafe_proposed"]] == ["sesame"]
    assert s["decision_mismatches"] == ["alg-12-no-longer-peanut"]


def test_inferred_allergy_from_a_question_is_spurious():
    u = "Is the pad thai made with peanuts?"
    s = evaluate_extractor(CASES, oracle({u: ([claim(attribute="allergen", polarity="assert",
                                                     subject="peanut")], None)}))["summary"]
    assert s["spurious_restrictions"] == 2
    assert s["unsafe_loosening_errors"] == {"applied": 0, "proposed": 0}


def test_hedge_lost_saves_an_allergy_without_asking():
    u = "I think I might be allergic to shellfish."
    s = evaluate_extractor(CASES, oracle({u: ([claim(attribute="allergen", polarity="assert",
                                                     subject="shellfish")], None)}))["summary"]
    assert s["missed"] == []  # still enforced now
    assert s["spurious"] == [["alg-15-might-be-allergic", "durable", "allergen", "shellfish"]]
    assert s["decision_accuracy"] < 1.0  # the confirmation question was skipped


def test_invalid_output_counts_as_invalid_and_as_missed():
    u = "I'm allergic to peanuts and sesame."
    s = evaluate_extractor(CASES, oracle({u: ([], "ValueError: bad")}))["summary"]
    assert s["invalid_output_rate"] == round(1 / len(CASES), 3)
    assert len(s["missed"]) == 4


def test_repeats_multiply_runs():
    assert evaluate_extractor(CASES, oracle(), repeats=3)["summary"]["runs"] == 3 * len(CASES)


def test_latency_tokens_cost_and_report():
    base = oracle()

    def timed(u):
        return (*base(u), {"latency_s": 0.5, "input_tokens": 100, "output_tokens": 20})
    ext = evaluate_extractor(CASES, timed)
    s = ext["summary"]
    assert s["latency_s"] == {"mean": 0.5, "p50": 0.5, "p95": 0.5}
    assert s["tokens"] == {"input": 100 * len(CASES), "output": 20 * len(CASES)}
    assert cost_usd({"input": 2_000_000, "output": 1_000_000}, 1.0, 5.0) == 7.0
    assert cost_usd({"input": 1, "output": 1}, None, 5.0) is None

    conf = evaluate_confirmations(load("confirmation_cases.json"), lambda q, r: ("unclear", {}))
    text = render({"model": "m", "tokens": s["tokens"], "cost_usd": None,
                   "extractor": ext, "confirmations": conf})
    assert "enforced now     10/10" in text and "remembered       8/8" in text
    assert "n/a (pass --price-in/--price-out)" in text


def test_confirmation_false_yes_is_the_headline():
    data = load("confirmation_cases.json")
    s = evaluate_confirmations(data, lambda q, r: "yes")["summary"]
    non_yes = sum(c["expected"] != "yes" for c in data["cases"])
    assert s["false_yes"] == non_yes and len(s["false_yes_ids"]) == non_yes
    assert evaluate_confirmations(data, lambda q, r: "unclear")["summary"]["false_yes"] == 0


# ------------------------------------------------------------------- parse_claims

def _item(**kw):
    return {"attribute": "allergen", "polarity": "assert", "subject": None, "value": None,
            "temporal": "current", "modality": "asserted", "evidence": "x", **kw}


def test_parse_normalizes_case_and_numbers():
    x = parse_claims({"claims": [
        _item(attribute="Allergen", polarity="ASSERT", subject="Peanut", temporal="Current"),
        _item(attribute="budget_max", value=15.0, temporal="conversation"),
        _item(attribute="spice_tolerance", value="Mild")]})
    assert x.error is None
    assert [c.subject for c in x.claims] == ["peanut", None, None]
    assert x.claims[1].value == 15 and x.claims[2].value == "mild"


@pytest.mark.parametrize("payload", [
    {"claims": [_item(subject="peanuts")]},                     # non-canonical allergen
    {"claims": [_item(attribute="diet", subject="vegan")]},     # scalar given a subject
    {"claims": [_item(attribute="diet", value="keto")]},        # value outside vocabulary
    {"claims": [_item(attribute="budget_max", value="15")]},    # number as string
    {"claims": [_item(subject="peanut", evidence=None)]},       # evidence must be text
    {"claims": [_item(attribute="budget_max", value=float("nan"))]},  # not a number
    {"claims": [_item(attribute="budget_max", value=float("inf"))]},  # not finite
    {"claims": [{"attribute": "allergen"}]},                    # missing fields
    {"nope": []},
    "not an object",
])
def test_parse_rejects_invalid_output(payload):
    x = parse_claims(payload)
    assert x.error and x.claims == []


# ------------------------------------------------------- model plumbing (fake client)

class FakeClient:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        blocks = [SimpleNamespace(type="thinking", thinking="..."),
                  SimpleNamespace(type="text", text=out if isinstance(out, str) else json.dumps(out))]
        return SimpleNamespace(content=blocks,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=3))


def test_extract_uses_structured_output_and_retries_once():
    good = {"claims": [_item(subject="sesame")]}
    fake = FakeClient("{not json", good)
    x = extract("I'm allergic to sesame", client=fake, model="m")
    assert x.error is None and x.claims[0].subject == "sesame" and len(fake.calls) == 2
    fmt = fake.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"] is CLAIM_SCHEMA
    assert fake.calls[0]["model"] == "m"
    assert x.stats["calls"] == 2 and x.stats["input_tokens"] == 20 and x.stats["output_tokens"] == 6
    assert x.stats["latency_s"] >= 0


def test_reply_stats_are_reported():
    answer, stats = classify_reply_with_stats("q?", "yes", client=FakeClient({"answer": "yes"}))
    assert answer == "yes" and stats["calls"] == 1 and stats["input_tokens"] == 10


def test_extract_reports_error_after_retry():
    fake = FakeClient({"claims": [_item(subject="peanuts")]}, API_DOWN)
    x = extract("...", client=fake)
    assert x.claims == [] and "APIConnectionError" in x.error and len(fake.calls) == 2


def test_extract_leaves_api_retries_to_the_client():
    """The SDK already retried what is retryable; a second call would double the wait."""
    fake = FakeClient(API_DOWN, {"claims": []})
    x = extract("...", client=fake)
    assert "APIConnectionError" in x.error and len(fake.calls) == 1


@pytest.mark.parametrize("output, expected", [
    ({"answer": "YES"}, "yes"), ({"answer": "no"}, "no"), ({"answer": "maybe"}, "unclear"),
    (API_DOWN, "unclear"), ("{bad", "unclear"), (["a list"], "unclear"),
])
def test_classify_reply_defaults_to_unclear(output, expected):
    assert classify_reply("q?", "r", client=FakeClient(output)) == expected


def test_a_bug_is_not_hidden_as_unclear():
    with pytest.raises(RuntimeError):
        classify_reply("q?", "r", client=FakeClient(RuntimeError("bug in our code")))


# ------------------------------------------------------------------ OpenAI adapter

class FakeOpenAI:
    """Records Chat Completions calls; answers each with the next scripted message text."""

    def __init__(self, *texts, finish_reason="stop"):
        self.texts, self.calls, self.finish_reason = list(texts), [], finish_reason
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        message = SimpleNamespace(content=self.texts.pop(0), refusal=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=self.finish_reason)],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))


def test_openai_adapter_sends_a_strict_schema_and_stores_nothing():
    sdk = FakeOpenAI(json.dumps({"claims": [_item(subject="sesame")]}))
    x = extract("I'm allergic to sesame", client=OpenAIClient(sdk), model="m")
    assert x.error is None and x.claims[0].subject == "sesame" and x.stats["input_tokens"] == 7
    call = sdk.calls[0]
    assert call["model"] == "m" and call["messages"] == [
        {"role": "developer", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": "I'm allergic to sesame"}]
    assert call["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "reply", "schema": CLAIM_SCHEMA, "strict": True}}
    assert call["store"] is False and call["max_completion_tokens"] == MAX_TOKENS


def test_truncated_openai_output_counts_as_invalid():
    """Cut off at the token limit: the partial text is not passed on as an answer."""
    sdk = FakeOpenAI('{"claims": [', '{"claims": [', finish_reason="length")
    x = extract("...", client=OpenAIClient(sdk), model="m")
    assert "stop_reason length" in x.error and len(sdk.calls) == 2


def test_openai_adapter_matches_the_installed_sdk():
    params = inspect.signature(Completions.create).parameters
    assert {"model", "messages", "max_completion_tokens", "response_format", "store"} <= set(params)


def _strict(schema: dict) -> bool:
    """OpenAI strict mode: objects list every property as required and forbid extras; a
    type array is at most [T, "null"] (wider unions use anyOf)."""
    t = schema.get("type")
    if isinstance(t, list) and (len(t) > 2 or (len(t) == 2 and "null" not in t)):
        return False
    if t == "object" and (schema.get("additionalProperties") is not False
                          or set(schema.get("required", [])) != set(schema["properties"])):
        return False
    children = list(schema.get("properties", {}).values()) + schema.get("anyOf", [])
    children += [schema["items"]] if "items" in schema else []
    return all(_strict(c) for c in children)


@pytest.mark.parametrize("schema", [CLAIM_SCHEMA, REPLY_SCHEMA, TURN_SCHEMA],
                         ids=["claims", "yes-no", "reply"])
def test_schemas_fit_openai_strict_mode(schema):
    assert _strict(schema)


# ------------------------------------------------------------------ prompt hygiene

def test_prompt_contains_no_eval_utterances():
    prompt = EXTRACT_SYSTEM.lower()
    leaked = [c["id"] for c in CASES if c["utterance"].lower().rstrip(".") in prompt]
    assert leaked == []


def test_schema_vocabulary_matches_memory():
    item = CLAIM_SCHEMA["properties"]["claims"]["items"]["properties"]
    assert item["attribute"]["enum"] == list(ATTRIBUTES)
    assert set(item["temporal"]["enum"]) == {t.value for t in Temporal}
