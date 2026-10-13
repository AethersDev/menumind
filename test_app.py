"""Menu, recommender, agent and server tests. No model is called: FakeLLM scripts it."""
import itertools
import os
import re

os.environ["MENUMIND_NO_APP"] = "1"  # don't build the module-level app on import

import pytest
from fastapi.testclient import TestClient

from agent import Agent
from fake_llm import FakeLLM, claim
from memory import ALLERGENS, DIET_ORDER, EventStore, fold
from menu import MenuError, load_menu, parse_menu
from recommend import check_reply, hard_violations, recommend, render
from server import create_app

MENU = load_menu()
EMPTY = fold([], user_id="u", session_id="s").active


def active(**kw):
    return {**EMPTY, **kw}


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
])
def test_inconsistent_menu_does_not_load(override, message):
    with pytest.raises(MenuError, match=re.escape(message)):
        parse_menu(_menu_with(**override))


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


@pytest.mark.parametrize("raw, problem", [
    ("Try [[rec:Z9]].", "unknown item id Z9"),
    ("Try [[rec:S4]].", "recommended S4, which the customer must avoid"),
    ("Try [[rec:M3]], only $12.", "typed a price"),
    ("Try the Spicy Chicken Bowl.", "typed the dish name"),
])
def test_check_reply_rejects(raw, problem):
    recs = recommend(MENU, active(allergen=frozenset({"peanut"})))
    assert any(problem in e for e in check_reply(raw, MENU, recs))


def test_render_uses_menu_names_prices_and_warnings():
    a = active(allergen=frozenset({"peanut"}), budget_max=10)
    recs = recommend(MENU, a)
    raw = "Try [[rec:M9]]. The [[item:S4]] has peanut."
    assert check_reply(raw, MENU, recs) == []
    text, mentioned = render(raw, MENU, recs, a["budget_max"])
    assert "**Chana Masala** ($12.00) [over your $10.00 budget]" in text
    assert "**Chicken Satay** ($9.00) [not suitable for you: contains peanut]" in text
    assert [m["id"] for m in mentioned] == ["M9", "S4"]


# ======================================================================== agent

SPICY = "I love spicy food, but I hate mushrooms."
LLM_CLAIMS = {
    SPICY: [claim("spice_tolerance", value="hot"), claim("disliked_ingredient", subject="mushroom")],
    "I don't like spicy food anymore.": [claim("spice_tolerance", value="none")],
    "I'm vegetarian.": [claim("diet", value="vegetarian")],
    "I'm allergic to peanuts.": [claim("allergen", subject="peanut")],
    "I'm no longer allergic to peanuts.": [claim("allergen", "negate", subject="peanut")],
    "Something is wrong with me.": "invalid",
}


def make_agent(tmp_path, **llm):
    fake = FakeLLM(claims=LLM_CLAIMS, **llm)
    return Agent(EventStore(str(tmp_path / "db.sqlite")), MENU, fake), fake


def test_natural_preferences_become_grounded_recommendations(tmp_path):
    agent, fake = make_agent(tmp_path)
    r = agent.turn("alice", "c1", SPICY)
    assert r.profile["remembered"] == ["Spice: hot", "Dislikes: mushroom"]
    assert r.mentioned, "expected a recommendation"
    for m in r.mentioned:
        item = MENU.get(m["id"])
        assert m["name"] == item.name and m["price"] == item.price
        assert f"**{item.name}** (${item.price:.2f})" in r.reply
        assert not item.has_ingredient("mushroom")


def test_memory_survives_new_conversation_and_restart(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.turn("alice", "c1", SPICY)
    agent.store.close()
    fresh, fake = make_agent(tmp_path)  # new process, same database
    assert fresh.profile("alice", "c2").durable["spice_tolerance"] == "hot"
    fresh.turn("alice", "c2", "What do you recommend?")
    assert fake.briefings[-1]["profile"]["remembered"] == ["Spice: hot", "Dislikes: mushroom"]


def test_preference_update_changes_recommendations(tmp_path):
    agent, fake = make_agent(tmp_path)
    agent.turn("alice", "c1", SPICY)
    r = agent.turn("alice", "c1", "I don't like spicy food anymore.")
    assert "Spice: none" in r.profile["remembered"]
    assert all(MENU.get(c["id"]).spice == "none" for c in fake.briefings[-1]["recommendable"][:3])


def test_users_are_isolated(tmp_path):
    agent, fake = make_agent(tmp_path)
    agent.turn("alice", "c1", "I'm allergic to peanuts.")
    agent.turn("bob", "c1", "What do you recommend?")  # same conversation id, other user
    bob = fake.briefings[-1]
    assert bob["profile"] == {"remembered": [], "this_conversation": []}
    assert bob["unsuitable"] == []  # alice's peanut allergy excludes nothing for bob


def test_allergy_removal_needs_confirmation(tmp_path):
    agent, _ = make_agent(tmp_path, replies={"yes": "yes"})
    agent.turn("alice", "c1", "I'm allergic to peanuts.")
    r = agent.turn("alice", "c1", "I'm no longer allergic to peanuts.")
    assert r.question and "peanut" in r.question
    assert "Allergies: peanut" in r.profile["remembered"]  # still enforced
    r = agent.turn("alice", "c1", "yes")
    assert r.trace["confirmation"] == "yes"
    assert all(not line.startswith("Allergies") for line in r.profile["remembered"])
    assert r.question is None


def test_unclear_answer_changes_nothing_and_asks_again(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.turn("alice", "c1", "I'm allergic to peanuts.")
    agent.turn("alice", "c1", "I'm no longer allergic to peanuts.")
    r = agent.turn("alice", "c1", "hmm, not sure")
    assert r.trace["confirmation"] == "unclear" and r.question
    assert "Allergies: peanut" in r.profile["remembered"]


def test_unsafe_recommendation_is_rejected_then_rewritten(tmp_path):
    attempts = iter(["You'll love [[rec:S4]].", "Try [[rec:M9]]."])
    agent, fake = make_agent(tmp_path, responder=lambda b, m: next(attempts))
    r = agent.turn("alice", "c1", "I'm allergic to peanuts.")
    assert fake.calls["respond"] == 2 and not r.trace["fallback"]
    assert [m["id"] for m in r.mentioned] == ["M9"]
    assert "must avoid" in r.trace["reply_errors"][0][0]


def test_rule_breaking_model_falls_back_to_menu_data(tmp_path):
    agent, fake = make_agent(tmp_path, responder=lambda b, m: "The Lobster Roll is $30.")
    r = agent.turn("alice", "c1", "I'm vegetarian.")
    assert r.trace["fallback"] and fake.calls["respond"] == 2
    assert "$30" not in r.reply and r.mentioned
    assert all(MENU.get(m["id"]).diet in ("vegetarian", "vegan") for m in r.mentioned)


def test_unreadable_preference_asks_to_rephrase(tmp_path):
    agent, fake = make_agent(tmp_path)
    r = agent.turn("alice", "c1", "Something is wrong with me.")
    assert r.trace["extract_error"] and "rephrase" in fake.briefings[-1]["notice"]
    assert r.profile["remembered"] == []


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
    b = TestClient(app)
    b.post("/api/register", json={"username": "bob", "pin": "4321"})
    me = b.get(f"/api/me?conversation_id={cid}").json()  # bob guesses alice's conversation id
    assert me["profile"] == {"remembered": [], "this_conversation": []}


def test_returning_user_sees_saved_preferences(app):
    a, cid = signup(app, "alice")
    a.post("/api/chat", json={"conversation_id": cid, "message": SPICY})
    again = TestClient(app)
    again.post("/api/login", json={"username": "alice", "pin": "1234"})
    new_cid = again.post("/api/conversations").json()["conversation_id"]
    assert again.get(f"/api/me?conversation_id={new_cid}").json()["profile"]["remembered"] == \
        ["Spice: hot", "Dislikes: mushroom"]


def test_chat_without_api_key_is_503(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(db_path=str(tmp_path / "nokey.sqlite"))
    c, cid = signup(app, "alice")
    r = c.post("/api/chat", json={"conversation_id": cid, "message": "hi"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert c.get("/api/me").status_code == 200  # memory still readable


def test_bad_conversation_id_rejected(app):
    c, _ = signup(app, "alice")
    assert c.post("/api/chat", json={"conversation_id": "../x", "message": "hi"}).status_code == 400
