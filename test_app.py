"""Menu, recommender, agent and server tests. No model is called: FakeLLM scripts it."""
import itertools
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ["MENUMIND_NO_APP"] = "1"  # don't build the module-level app on import

import anthropic
import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

import accounts as accounts_module
import agent as agent_module
import llm
from accounts import LOGIN_FAILURES_ALLOWED, LOGIN_WINDOW_SECONDS, SESSION_SECONDS, Accounts
from agent import (CONVERSATION_IDLE_SECONDS, MAX_CONVERSATIONS_PER_USER, Agent,
                   UnknownConversation)
from fake_llm import FakeLLM, claim
from memory import ALLERGENS, DIET_ORDER, EventStore, fold
from menu import HOUSE_RECIPES, MenuError, load_menu, parse_menu
from recommend import check_reply, hard_violations, recommend, render
from server import create_app

MENU = load_menu()
EMPTY = fold([], user_id="u", session_id="s").active
FAILED_CLOSED = "haven't updated your profile or generated recommendations"


def active(**kw):
    return {**EMPTY, **kw}


def says(message, *picks):
    """A reply-model answer in the structured format."""
    return json.dumps({"message": message, "recommend": list(picks)})


def api_down(*_, **__):
    raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))


# ========================================================================= menu

def test_menu_meets_assignment_requirements():
    items = MENU.items
    assert len(items) >= 12
    assert {i.category for i in items} == {"starter", "main", "dessert"}
    assert max(i.price for i in items) - min(i.price for i in items) >= 10
    assert any(i.diet in ("vegetarian", "vegan") for i in items)
    assert any(i.diet in ("omnivore", "pescatarian") for i in items)
    assert len({i.spice for i in items}) >= 3
    assert all(i.ingredients for i in items)
    # comparable pairs that differ in one dimension
    pairs = {("S1", "S2"): "spice", ("M3", "M4"): "spice", ("M1", "M2"): "diet", ("M5", "M6"): "diet"}
    for (a, b), field in pairs.items():
        assert getattr(MENU.get(a), field) != getattr(MENU.get(b), field)


def _menu_with(**override):
    base = {"id": "X1", "name": "Test Dish", "category": "main", "price": 10, "diet": "vegan",
            "spice": "none", "ingredients": ["rice"], "allergens": [], "description": "."}
    return {"restaurant": "T", "items": [{**base, **override}]}


@pytest.mark.parametrize("override, message", [
    ({"ingredients": ["rice", "fish_sauce"], "allergens": ["fish"]}, "labeled vegan but contains fish_sauce"),
    ({"ingredients": ["peanut"]}, "peanut carries ['peanut']"),
    ({"allergens": ["nuts"]}, "unknown allergens"),
    ({"spice": "extra hot"}, "spice"),
    ({"ingredients": ["Soy Sauce"], "allergens": ["soy", "gluten"]}, "non-canonical ingredient"),
    ({"ingredients": ["rice", "shrimp_paste"]}, "unclassified ingredient 'shrimp_paste'"),
    ({"allergens": ["peanut"]}, "lists allergens ['peanut'] that no ingredient carries"),
    ({"diet": "vegetarian"}, "labeled vegetarian but its ingredients are vegan"),
])
def test_inconsistent_menu_does_not_load(override, message):
    with pytest.raises(MenuError, match=re.escape(message)):
        parse_menu(_menu_with(**override))


def test_compound_ingredient_is_judged_by_its_recipe(monkeypatch):
    monkeypatch.setitem(HOUSE_RECIPES, "test_paste", ("garlic", "shrimp"))
    with pytest.raises(MenuError) as e:
        parse_menu(_menu_with(ingredients=["rice", "test_paste"]))
    assert "test_paste carries ['shellfish']" in str(e.value)
    assert "labeled vegan but contains test_paste" in str(e.value)


def test_duplicate_ids_rejected():
    data = _menu_with()
    data["items"].append(dict(data["items"][0], name="Other"))
    with pytest.raises(MenuError, match="duplicate id"):
        parse_menu(data)


# ================================================================== recommender

def test_hard_constraints_hold_for_every_diet_and_allergen_combination():
    """2,048 profiles: no allowed dish ever violates a hard constraint, and every dish is
    either allowed or excluded."""
    for diet in DIET_ORDER:
        for r in range(len(ALLERGENS) + 1):
            for allergens in itertools.combinations(ALLERGENS, r):
                a = active(diet=diet, allergen=frozenset(allergens))
                recs = recommend(MENU, a)
                for c in recs.candidates + recs.over_budget:
                    assert not hard_violations(c.item, a), (diet, allergens, c.item.id)
                assert len(recs.allowed_ids()) + len(recs.excluded) == len(MENU.items)


def test_spice_and_dislikes_shape_ranking():
    recs = recommend(MENU, active(spice_tolerance="hot", disliked_ingredient=frozenset({"mushroom"})))
    top = [c.item for c in recs.candidates[:3]]
    assert all(i.spice == "hot" for i in top)
    assert all(not i.has_ingredient("mushroom") for i in top)
    ranked = [c.item.id for c in recs.candidates]
    assert ranked.index("M7") > ranked.index("M9")  # mushroom udon sinks


def test_budget_splits_candidates():
    recs = recommend(MENU, active(budget_max=10))
    assert all(c.item.price <= 10 for c in recs.candidates)
    assert all(c.item.price > 10 for c in recs.over_budget)


@pytest.mark.parametrize("message, picks, problem", [
    ("Try this.", ["Z9"], "unknown item id Z9"),
    ("Try this.", ["S4"], "recommended S4, which the customer must avoid"),
    ("Try [[rec:M9]].", [], "put recommendations in"),
    ("Try [[item:Z9]].", [], "unknown item id Z9"),
    ("Only $12.", [], "typed a price"),
    ("Try the Spicy Chicken Bowl.", [], "typed the dish name"),
    ("Options.", ["M9", "M2", "D3", "S1"], "at most 3"),
    ("Options.", ["M9", "M9"], "at most 3"),
])
def test_check_reply_rejects(message, picks, problem):
    recs = recommend(MENU, active(allergen=frozenset({"peanut"})))
    assert any(problem in e for e in check_reply(message, picks, MENU, recs))


def test_render_uses_menu_names_prices_and_warnings():
    a = active(allergen=frozenset({"peanut"}), budget_max=10)
    recs = recommend(MENU, a)
    message = "The [[item:S4]] has peanut."
    assert check_reply(message, ["M9"], MENU, recs) == []
    text, mentioned = render(message, ["M9"], MENU, recs, a["budget_max"])
    assert text == ("The **Chicken Satay** ($9.00) [not suitable for you: contains peanut] has peanut."
                    "\n\n- **Chana Masala** ($12.00) [over your $10.00 budget]")
    assert [m["id"] for m in mentioned] == ["S4", "M9"]


# ======================================================================== agent

SPICY = "I love spicy food, but I hate mushrooms."
LLM_CLAIMS = {
    SPICY: [claim("spice_tolerance", value="hot"), claim("disliked_ingredient", subject="mushroom")],
    "I don't like spicy food anymore.": [claim("spice_tolerance", value="none")],
    "I'm vegetarian.": [claim("diet", value="vegetarian")],
    "I'm allergic to peanuts.": [claim("allergen", subject="peanut")],
    "I'm no longer allergic to peanuts.": [claim("allergen", "negate", subject="peanut")],
    "I'm allergic to soy.": [claim("allergen", subject="soy")],
    "Meat is fine tonight.": [claim("diet", value="omnivore", temporal="conversation")],
    "Vegetarian, sesame allergy, no mushrooms.": [
        claim("diet", value="vegetarian"), claim("allergen", subject="sesame"),
        {**claim("disliked_ingredient", subject="mushroom"), "evidence": None}],
    "I'm vegan and allergic to peanuts.": [claim("diet", value="vegan"),
                                           claim("allergen", subject="peanut")],
    "No peanut allergy now, and I'm not vegan anymore.": [
        claim("allergen", "negate", subject="peanut"), claim("diet", "negate", value="vegan")],
    "I hate mushrooms.": [claim("disliked_ingredient", subject="mushrooms")],
    "Mushroom is fine now.": [claim("disliked_ingredient", "negate", subject="mushroom")],
    "Love tomatoes and anchovies.": [claim("liked_ingredient", subject="tomatoes"),
                                     claim("liked_ingredient", subject="anchovies")],
    "Something is wrong with me.": "invalid",
    "yes, sure": "invalid",
}


def make_agent(tmp_path, **llm):
    fake = FakeLLM(claims=LLM_CLAIMS, **llm)
    return Agent(EventStore(str(tmp_path / "db.sqlite")), MENU, fake), fake


def test_natural_preferences_become_grounded_recommendations(tmp_path):
    agent, fake = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, SPICY)
    assert r.profile["remembered"] == ["Spice: hot", "Dislikes: mushroom"]
    assert r.mentioned, "expected a recommendation"
    for m in r.mentioned:
        item = MENU.get(m["id"])
        assert m["name"] == item.name and m["price"] == item.price
        assert f"**{item.name}** (${item.price:.2f})" in r.reply
        assert not item.has_ingredient("mushroom")


def test_memory_survives_new_conversation_and_restart(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.turn("alice", agent.start_conversation("alice"), SPICY)
    agent.store.close()
    fresh, fake = make_agent(tmp_path)  # new process, same database
    assert fresh.profile("alice").durable["spice_tolerance"] == "hot"
    fresh.turn("alice", fresh.start_conversation("alice"), "What do you recommend?")
    saved = fake.briefings[-1]["profile"]["saved"]
    assert (saved["spice_tolerance"], saved["disliked_ingredient"]) == ("hot", ["mushroom"])


def test_preference_update_changes_recommendations(tmp_path):
    agent, fake = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, SPICY)
    r = agent.turn("alice", c1, "I don't like spicy food anymore.")
    assert "Spice: none" in r.profile["remembered"]
    assert all(MENU.get(c["id"]).spice == "none" for c in fake.briefings[-1]["recommendable"][:3])


def test_users_are_isolated(tmp_path):
    agent, fake = make_agent(tmp_path)
    alice = agent.start_conversation("alice")
    agent.turn("alice", alice, "I'm allergic to peanuts.")
    with pytest.raises(UnknownConversation):  # a conversation id belongs to one user
        agent.turn("bob", alice, "What do you recommend?")
    agent.turn("bob", agent.start_conversation("bob"), "What do you recommend?")
    bob = fake.briefings[-1]
    assert bob["profile"]["saved"]["allergen"] == bob["profile"]["enforced_now"]["allergen"] == []
    assert bob["unsuitable"] == []  # alice's peanut allergy excludes nothing for bob
    assert len(bob["recommendable"]) == len(MENU.items)  # the model can offer any category


def test_allergy_removal_needs_confirmation(tmp_path):
    agent, _ = make_agent(tmp_path, replies={"yes": "yes"})
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm allergic to peanuts.")
    r = agent.turn("alice", c1, "I'm no longer allergic to peanuts.")
    assert r.question and "peanut" in r.question
    assert "Allergies: peanut" in r.profile["remembered"]  # still enforced
    r = agent.turn("alice", c1, "yes")
    assert r.trace["confirmation"] == "yes"
    assert all(not line.startswith("Allergies") for line in r.profile["remembered"])
    assert r.question is None


def test_unclear_answer_changes_nothing_and_asks_again(tmp_path):
    agent, _ = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm allergic to peanuts.")
    agent.turn("alice", c1, "I'm no longer allergic to peanuts.")
    r = agent.turn("alice", c1, "hmm, not sure")
    assert r.trace["confirmation"] == "unclear" and r.question
    assert "Allergies: peanut" in r.profile["remembered"]


def test_recommended_dishes_are_rendered_by_code(tmp_path):
    """The model picks dishes by ID; code writes each one's menu name, price and reasons,
    once: a dish the message already names is not listed again (seen in live replies)."""
    agent, _ = make_agent(tmp_path, responder=lambda b, m: says("Two good fits.", "M2", "D3"))
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, SPICY)
    assert r.reply == ("Two good fits.\n\n"
                       "- **Green Curry with Tofu** ($14.00): matches your spice level (hot)\n"
                       "- **Coconut Lime Sorbet** ($6.00)")
    assert [(m["id"], m["kind"]) for m in r.mentioned] == [("M2", "rec"), ("D3", "rec")]
    agent.client.responder = lambda b, m: says("Try [[item:M2]].", "M2", "D3")
    r = agent.turn("alice", c1, "What do you recommend?")
    assert r.reply == "Try **Green Curry with Tofu** ($14.00).\n\n- **Coconut Lime Sorbet** ($6.00)"
    assert [(m["id"], m["kind"]) for m in r.mentioned] == [("M2", "rec"), ("D3", "rec")]


def test_unsafe_recommendation_is_rejected_then_rewritten(tmp_path):
    attempts = iter([says("You'll love this.", "S4"), says("Try this instead.", "M9")])
    agent, fake = make_agent(tmp_path, responder=lambda b, m: next(attempts))
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, "I'm allergic to peanuts.")
    assert fake.calls["respond"] == 2 and not r.trace["fallback"]
    assert [m["id"] for m in r.mentioned] == ["M9"]
    assert "must avoid" in r.trace["reply_errors"][0][0]


def test_rule_breaking_model_falls_back_to_menu_data(tmp_path):
    agent, fake = make_agent(tmp_path, responder=lambda b, m: says("The Lobster Roll is $30."))
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, "I'm vegetarian.")
    assert r.trace["fallback"] and fake.calls["respond"] == 2
    assert "$30" not in r.reply and r.mentioned
    assert all(MENU.get(m["id"]).diet in ("vegetarian", "vegan") for m in r.mentioned)


def test_unreadable_preference_fails_closed(tmp_path):
    agent, fake = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, "Something is wrong with me.")
    assert r.trace["extract_error"] and FAILED_CLOSED in r.reply
    assert r.mentioned == [] and r.profile["remembered"] == [] and fake.calls["respond"] == 0


def test_extraction_outage_recommends_nothing(tmp_path):
    """Audit #1, case A: with the API down a stated allergy can't be read, so nothing is
    recommended (before, the fallback offered edamame to a soy-allergic customer)."""
    down = SimpleNamespace(messages=SimpleNamespace(create=api_down))
    agent = Agent(EventStore(str(tmp_path / "db.sqlite")), MENU, down)
    r = agent.turn("alice", agent.start_conversation("alice"), "I'm allergic to soy.")
    assert FAILED_CLOSED in r.reply and r.mentioned == [] and r.changes == []


def test_extraction_failure_leaves_a_pending_question_open(tmp_path):
    """Case A with a question pending: an unread reply might restate the allergy (SPEC §7
    step 1), so it is not classified as an answer."""
    agent, fake = make_agent(tmp_path, replies={"yes, sure": "yes"})
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm allergic to peanuts.")
    agent.turn("alice", c1, "I'm no longer allergic to peanuts.")
    r = agent.turn("alice", c1, "yes, sure")
    assert "Allergies: peanut" in r.profile["remembered"] and "peanut" in r.question
    assert fake.calls["classify"] == 0


def test_narration_outage_falls_back_to_grounded_dishes(tmp_path):
    """Audit #1, case B: the allergy is saved and only the reply model fails, so the reply is
    built from verified data and avoids soy."""
    agent, _ = make_agent(tmp_path, responder=api_down)
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, "I'm allergic to soy.")
    assert r.trace["fallback"] and "Noted: avoiding soy (allergy)." in r.reply
    assert r.mentioned and all("soy" not in MENU.get(m["id"]).allergens for m in r.mentioned)


def test_fallback_keeps_the_stale_confirmation_notice(tmp_path):
    agent, _ = make_agent(tmp_path, replies={"yes": "yes"}, responder=api_down)
    c1 = agent.start_conversation("alice")
    c2 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm allergic to peanuts.")
    agent.turn("alice", c1, "I'm no longer allergic to peanuts.")
    agent.turn("alice", c2, "I'm no longer allergic to peanuts.")
    agent.turn("alice", c2, "yes")  # removed from the second conversation
    r = agent.turn("alice", c1, "yes")
    assert "earlier question no longer applies" in r.reply


def test_temporary_loosening_is_shown_and_briefed(tmp_path):
    """Audit #3: 'meat is fine tonight' over a saved vegetarian diet is shown as a change,
    and the reply model gets the enforced profile, not display lines."""
    agent, fake = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm vegetarian.")
    r = agent.turn("alice", c1, "Meat is fine tonight.")
    assert r.profile == {"remembered": ["Diet: vegetarian"],
                         "this_conversation": ["Diet: omnivore (saved: vegetarian)"]}
    brief = fake.briefings[-1]["profile"]
    assert (brief["enforced_now"]["diet"], brief["saved"]["diet"]) == ("omnivore", "vegetarian")


def test_invalid_claim_rejects_the_whole_message(tmp_path):
    """A bad third claim used to fail after the first two were saved. The whole batch is
    validated before anything is applied."""
    agent, _ = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    r = agent.turn("alice", c1, "Vegetarian, sesame allergy, no mushrooms.")
    assert FAILED_CLOSED in r.reply and r.profile["remembered"] == []


def test_plural_ingredients_map_to_the_menu_word(tmp_path):
    """A live gpt-5.4-mini run returned 'mushrooms'. Stored as said, it matched no dish and a
    later statement about 'mushroom' missed it. Only plurals of menu words change."""
    agent, _ = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    assert agent.turn("alice", c1, "I hate mushrooms.").profile["remembered"] == \
        ["Dislikes: mushroom"]
    recs = recommend(MENU, agent.profile("alice", c1).active)
    assert any("dislike" in why for c in recs.candidates if c.item.id == "M7" for why in c.reasons)
    assert agent.turn("alice", c1, "Mushroom is fine now.").profile["remembered"] == []
    assert agent.turn("alice", c1, "Love tomatoes and anchovies.").profile["remembered"] == \
        ["Likes: anchovies, tomato"]  # no menu word 'anchovy', so it stays as said


@pytest.mark.parametrize("output", ["{not json", '{"message": "hi"}', '["a list"]',
                                    '{"message": 5, "recommend": []}', ""])
def test_malformed_model_output_falls_back(tmp_path, output):
    """A successful API call with unusable output gets the grounded fallback, not a 500."""
    agent, fake = make_agent(tmp_path, responder=lambda b, m: output)
    r = agent.turn("alice", agent.start_conversation("alice"), "I'm allergic to peanuts.")
    assert r.trace["fallback"] and fake.calls["respond"] == 2 and r.mentioned
    assert all("peanut" not in MENU.get(m["id"]).allergens for m in r.mentioned)


def test_question_awaiting_an_answer_is_the_pending_one(tmp_path):
    """Audit #11: a clarifying question asked after a confirmation must not be reported as
    the question awaiting yes or no."""
    agent, _ = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm vegan and allergic to peanuts.")
    r = agent.turn("alice", c1, "No peanut allergy now, and I'm not vegan anymore.")
    assert r.question == agent.pending_question("alice", c1) and "peanut" in r.question


def test_conversation_ids_are_issued_and_user_bound(tmp_path):
    agent, fake = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    for user, cid in (("bob", c1), ("alice", "made-up-0001")):
        with pytest.raises(UnknownConversation):
            agent.turn(user, cid, "What do you recommend?")
    assert fake.calls["extract"] == 0  # rejected before any model call


def test_idle_conversation_ends_but_saved_preferences_stay(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(agent_module, "monotonic", lambda: now[0])
    agent, _ = make_agent(tmp_path)
    c1 = agent.start_conversation("alice")
    agent.turn("alice", c1, "I'm allergic to peanuts.")
    now[0] += CONVERSATION_IDLE_SECONDS + 1
    with pytest.raises(UnknownConversation):
        agent.turn("alice", c1, "What do you recommend?")
    assert agent.profile("alice").durable["allergen"] == {"peanut"}


def test_open_conversations_are_capped_per_user_and_in_total(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path)
    ids = [agent.start_conversation("alice") for _ in range(MAX_CONVERSATIONS_PER_USER + 1)]
    with pytest.raises(UnknownConversation):
        agent.profile("alice", ids[0])  # the least recently used one was dropped
    assert all(agent.profile("alice", cid) for cid in ids[1:])
    monkeypatch.setattr(agent_module, "MAX_CONVERSATIONS", 2)
    bob, carol, dave = (agent.start_conversation(u) for u in ("bob", "carol", "dave"))
    with pytest.raises(UnknownConversation):
        agent.profile("bob", bob)
    assert agent.profile("carol", carol) and agent.profile("dave", dave)


def test_absorbed_model_failures_are_logged(tmp_path, caplog):
    agent, _ = make_agent(tmp_path, responder=api_down)
    c1 = agent.start_conversation("alice")
    with caplog.at_level(logging.WARNING):
        agent.turn("alice", c1, "Something is wrong with me.")
        agent.turn("alice", c1, "I'm allergic to soy.")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "extraction failed" in logged and "reply model failed" in logged
    assert "soy" not in logged  # customer text is not logged


# ======================================================================= server

@pytest.fixture
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "srv.sqlite"), client=FakeLLM(claims=LLM_CLAIMS))


def signup(app, name, pin="1234"):
    c = TestClient(app)
    assert c.post("/api/register", json={"username": name, "pin": pin}).status_code == 200
    return c, c.post("/api/conversations").json()["conversation_id"]


def test_page_and_menu_are_served(app):
    c = TestClient(app)
    page = c.get("/").text
    assert "Lantern Street Kitchen" in page and "MenuMind" in page
    assert len(c.get("/api/menu").json()["items"]) == len(MENU.items)


def test_sign_in_flow(app):
    c, _ = signup(app, "alice")
    assert c.get("/api/me").json()["username"] == "alice"
    other = TestClient(app)
    assert other.post("/api/login", json={"username": "alice", "pin": "9999"}).status_code == 401
    assert other.post("/api/login", json={"username": "ALICE", "pin": "1234"}).status_code == 200
    assert other.post("/api/register", json={"username": "alice", "pin": "1234"}).status_code == 400
    assert other.post("/api/register", json={"username": "x", "pin": "12"}).status_code == 400
    c.post("/api/logout")
    assert c.get("/api/me").status_code == 401


def test_chat_requires_sign_in(app):
    assert TestClient(app).post("/api/chat", json={"conversation_id": "abcdefgh-1", "message": "hi"}).status_code == 401


def test_chat_round_trip_and_isolation(app):
    a, cid = signup(app, "alice")
    r = a.post("/api/chat", json={"conversation_id": cid, "message": "I'm allergic to peanuts."}).json()
    assert "Allergies: peanut" in r["profile"]["remembered"]
    b, _ = signup(app, "bob", "4321")
    assert b.get(f"/api/me?conversation_id={cid}").status_code == 404  # bob guesses alice's id
    assert b.post("/api/chat", json={"conversation_id": cid, "message": "hi"}).status_code == 404
    assert b.get("/api/me").json()["profile"] == {"remembered": [], "this_conversation": []}


def test_only_issued_conversation_ids_are_accepted(app):
    a, _ = signup(app, "alice")
    assert a.post("/api/chat", json={"conversation_id": "AAAAAAAA-made-up",
                                     "message": "hi"}).status_code == 404
    assert a.get("/api/me?conversation_id=none-yet-0").status_code == 404


def test_http_trace_keeps_internal_errors_out(app):
    a, cid = signup(app, "alice")
    trace = a.post("/api/chat", json={"conversation_id": cid,
                                      "message": "Something is wrong with me."}).json()["trace"]
    assert set(trace) <= {"claims", "decisions", "confirmation", "fallback"}


def test_unexpected_error_is_sanitized(tmp_path):
    def broken(**_):
        raise RuntimeError("secret-internal-detail")
    app = create_app(db_path=str(tmp_path / "srv.sqlite"),
                     client=SimpleNamespace(messages=SimpleNamespace(create=broken)))
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/api/register", json={"username": "alice", "pin": "1234"}).status_code == 200
    cid = c.post("/api/conversations").json()["conversation_id"]
    r = c.post("/api/chat", json={"conversation_id": cid, "message": "hi"})
    assert r.status_code == 500 and "ticket shows what is saved" in r.json()["detail"]
    assert "secret" not in r.text


def test_login_attempts_are_limited_per_username(app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(accounts_module, "monotonic", lambda: now[0])
    signup(app, "alice")
    c = TestClient(app)
    for _ in range(LOGIN_FAILURES_ALLOWED):
        assert c.post("/api/login", json={"username": "alice", "pin": "0000"}).status_code == 401
    assert c.post("/api/login", json={"username": "alice", "pin": "1234"}).status_code == 429
    now[0] += LOGIN_WINDOW_SECONDS + 1
    assert c.post("/api/login", json={"username": "alice", "pin": "1234"}).status_code == 200


def test_sessions_expire_and_are_cleaned_up(tmp_path, monkeypatch):
    acc = Accounts(str(tmp_path / "acc.sqlite"))
    old = acc.start_session(acc.register("alice", "1234"))
    later = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS + 1)
    monkeypatch.setattr(accounts_module, "_now", lambda: later)
    assert acc.session_user(old) is None
    acc.start_session(acc.login("alice", "1234"))
    assert acc._db.execute("SELECT COUNT(*) FROM sessions WHERE token = ?", (old,)).fetchone() == (0,)


@pytest.mark.parametrize("pin", ["12", "1234\n", "١٢٣٤", "1" * 13])
def test_register_rejects_bad_pins(app, pin):
    assert TestClient(app).post("/api/register",
                                json={"username": "alice", "pin": pin}).status_code == 400


def test_oversized_credentials_are_rejected_before_hashing(app):
    r = TestClient(app).post("/api/login", json={"username": "a" * 1000, "pin": "1234"})
    assert r.status_code == 422


@pytest.mark.parametrize("provider", sorted(llm.KEY_ENV))
def test_model_clients_have_bounded_timeout_and_retries(provider, monkeypatch):
    monkeypatch.setenv(llm.KEY_ENV[provider], "test-key")
    client = llm.make_client(provider)
    sdk = getattr(client, "sdk", client)  # the OpenAI adapter wraps its SDK client
    assert (sdk.timeout, sdk.max_retries) == (llm.MODEL_TIMEOUT_S, llm.MODEL_RETRIES)


def test_openai_errors_also_fall_back(tmp_path):
    def openai_down(*_, **__):
        raise openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com"))
    agent, _ = make_agent(tmp_path, responder=openai_down)
    r = agent.turn("alice", agent.start_conversation("alice"), "I'm allergic to soy.")
    assert r.trace["fallback"] and "Noted: avoiding soy (allergy)." in r.reply


def test_returning_user_sees_saved_preferences(app):
    a, cid = signup(app, "alice")
    a.post("/api/chat", json={"conversation_id": cid, "message": SPICY})
    again = TestClient(app)
    again.post("/api/login", json={"username": "alice", "pin": "1234"})
    new_cid = again.post("/api/conversations").json()["conversation_id"]
    assert again.get(f"/api/me?conversation_id={new_cid}").json()["profile"]["remembered"] == \
        ["Spice: hot", "Dislikes: mushroom"]


def test_chat_without_api_key_is_503(tmp_path, monkeypatch):
    for key in llm.KEY_ENV.values():
        monkeypatch.delenv(key, raising=False)
    app = create_app(db_path=str(tmp_path / "nokey.sqlite"))
    c, cid = signup(app, "alice")
    r = c.post("/api/chat", json={"conversation_id": cid, "message": "hi"})
    assert r.status_code == 503 and llm.KEY_ENV[llm.PROVIDER] in r.json()["detail"]
    assert c.get("/api/me").status_code == 200  # memory still readable


def test_bad_conversation_id_rejected(app):
    c, _ = signup(app, "alice")
    assert c.post("/api/chat", json={"conversation_id": "../x", "message": "hi"}).status_code == 400
