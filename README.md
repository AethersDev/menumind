# MenuMind

**A grounded, memory-aware restaurant assistant.**

A neuro-symbolic conversational recommendation system combining LLM-based language
understanding, deterministic recommendation logic, and event-sourced preference memory.

MenuMind learns each customer's preferences from normal conversation, remembers them across
visits, and recommends real dishes from a fixed menu. It never invents dishes or prices, and
it never loosens an allergy or diet without asking. The demo serves the menu of a fictional
restaurant, Lantern Street Kitchen.

## Architecture

- **The LLM interprets natural language.** It turns a message into typed preference claims,
  classifies yes/no answers, and writes the reply text.
- **Deterministic rules authorize memory changes.** Each claim is applied, sent back as a
  confirmation question, sent back as a clarifying question, or ignored, according to fixed
  rules (`SPEC.md` §5).
- **An event-based memory system preserves evolving preferences.** Every authorized change is
  an append-only event; the current profile is computed from the log.
- **A deterministic recommendation engine selects eligible menu items.** Allergies and diet
  filter the menu; tastes and budget rank what is left.
- **A grounded response layer presents verified menu information.** The reply refers to
  dishes only by ID, and code fills in names and prices from the menu.

This is not a multi-agent system. The model is called for single, bounded tasks with fixed
output formats; it has no tools, takes no actions, and never decides what is stored or
recommended.

## Run it on macOS

Requires Python 3.11 or newer (`brew install python@3.12` if `python3 --version` is older).

```bash
cd menumind
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # then put your key in ANTHROPIC_API_KEY
set -a; source .env; set +a

uvicorn server:app --reload
open http://127.0.0.1:8000
```

Create an account with a username and a 4 to 12 digit PIN, then chat. The order ticket on
the right shows what MenuMind remembers, split into "every visit" and "just this
conversation". Each reply has a "How I read that" section showing the preferences it heard
and the rule that decided what to do with them.

**Without an API key**, `python preview.py` runs the same app with a scripted model that
understands a handful of phrases (they print at startup). Use it for UI work.

The server keeps everything in one SQLite file, `data/menumind.db`. Delete it to start fresh.

## Try the six requirements

| Requirement | Try this |
|---|---|
| 1. Natural conversation | "I love spicy food, but I hate mushrooms." |
| 2. Menu recommendations | "What should I get for a main under $15?" Names and prices come from `menu.json`. |
| 3. Long-term memory | Sign out, sign back in, start a new conversation: the ticket still lists your preferences. |
| 4. Updating preferences | "I don't like spicy food anymore." For allergies: "I'm no longer allergic to peanuts" gets a confirmation question first. |
| 5. Returning users, kept separate | Create a second account in a private window. Nothing from the first shows up. |
| 6. Deployed interface | This page, locally now; see deployment below. |

Other things worth trying: "Nothing spicy tonight" (lasts only for this conversation),
"I'm allergic to nuts" (saved as both peanut and tree nut), and "Does the pad thai have
peanuts?" (a question, so nothing is saved).

## How a message is handled

```
message
  -> extractor (model, structured output)   what was said: attribute, value, now/tonight/past, hedged?
  -> decide() + Conversation (code)         what is authorized: apply, ask to confirm, ask to clarify, ignore
  -> event log in SQLite (code)             append-only, per user; the profile is computed from it
  -> recommend() (code)                     allergies and diet filter the menu; tastes rank what's left
  -> reply model                            writes text that names dishes only as [[rec:ID]] / [[item:ID]]
  -> check_reply() + render() (code)        rejects unknown or unsafe dishes and typed prices,
                                            then fills in names and prices from the menu
```

If the reply model breaks a rule twice in a row, the app answers with a reply built only from
menu data.

## Files

| File | What it does |
|---|---|
| `memory.py` | Preference model, decision rules, event log, profile fold, confirmation lifecycle |
| `extractor.py` | Model calls: reading preferences from a message, classifying yes/no answers |
| `menu.json`, `menu.py` | The menu (19 dishes), validated on load against ingredient allergens and diets |
| `recommend.py` | Hard filtering, soft ranking, grounded rendering and reply checks |
| `agent.py` | One chat turn end to end |
| `accounts.py`, `server.py` | Username and PIN sign-in, HTTP API |
| `static/index.html` | The chat page |
| `SPEC.md` | The memory specification: rules, invariants, test layers |
| `test_*.py` | Tests (no model calls) |
| `eval_extractor.py`, `extractor_cases.json`, `confirmation_cases.json` | Model evaluation |
| `fake_llm.py`, `preview.py` | Scripted model for tests and key-free preview |

## Tests and evaluation

```bash
pytest                                        # 136 tests, no API calls
python eval_extractor.py                      # extractor + reply classifier, default model
python eval_extractor.py --model claude-sonnet-5-5 --price-in <USD/Mtok> --price-out <USD/Mtok>
```

The evaluation reports safety first, as counts: restrictions enforced and remembered, unsafe
loosenings applied and proposed, and false "yes" readings of confirmation answers. Then it
reports quality, latency and cost. SPEC §11 explains how the scores are computed and how to
choose between models.

## Deploying from GitHub

GitHub Pages only serves static files, and this app needs a running server (it holds the API
key and the memory database). Push the repository to GitHub, then connect it to a host that
builds from a Dockerfile and offers a persistent disk, for example Render, Fly.io or Railway:

1. Create a service from the GitHub repository, using the included `Dockerfile`.
2. Attach a persistent volume mounted at `/data` (the database lives at `/data/menumind.db`).
3. Set `ANTHROPIC_API_KEY` as a secret. `COOKIE_SECURE=1` is already set for HTTPS.
4. Run a single instance (see limitations).

## Limitations, stated plainly

- **Allergen data is only as good as `menu.json`.** The loader checks ingredients against
  known allergens, but cross-contamination is not modeled. Tell customers with severe
  allergies to confirm with staff.
- **Free text can still be wrong.** Dish names and prices can't be made up, because code
  inserts them. But the model's own sentences could still describe a dish inaccurately.
  Ingredient and allergen facts in the briefing come from the menu, and the prompt forbids
  anything else; this part is not mechanically checked.
- **"Tonight" means "this conversation".** Returning in a new conversation the same evening
  drops a temporary preference (SPEC §9).
- **One server process.** Memory updates are serialized by a lock inside the process, so
  concurrent requests are safe on one instance. Several instances would need the optimistic
  concurrency described in SPEC §4.
- **Sign-in is deliberately simple** (username and PIN, scrypt-hashed, no rate limiting).
  That suits a demo, not a production deployment.
