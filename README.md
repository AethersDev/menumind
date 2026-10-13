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
- **A grounded response layer presents verified menu information.** The model returns the
  dishes it recommends as IDs, separately from its message. Code accepts only IDs from the
  eligible set and writes each dish's name, price and reasons from the menu.

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
conversation"; a temporary change is listed with the saved value it overrides. Each reply has
a "How I read that" section showing the preferences it heard and the rule that decided what
to do with them.

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
  -> reply model (structured output)        {"message": text, "recommend": [dish IDs]}
  -> check_reply() + render() (code)        rejects ineligible or unknown IDs, typed names and
                                            prices, then writes each recommended dish (name,
                                            price, reasons) from the menu
```

If the extractor fails, the app stops there: nothing is saved, no pending question is
answered, and nothing is recommended, because the unread message may have stated an allergy.
If only the reply model fails, or breaks a rule twice in a row, the app answers with a reply
built from verified data: the changes it saved and the top eligible dishes.

## Files

| File | What it does |
|---|---|
| `memory.py` | Preference model, decision rules, event log, profile fold, confirmation lifecycle |
| `extractor.py` | Model calls: reading preferences from a message, classifying yes/no answers |
| `menu.json`, `menu.py` | The menu (19 dishes). Every ingredient is classified (compound ones through declared house recipes), and each dish's allergens and diet must match its ingredients |
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
pytest                                        # 152 tests, no API calls
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

- **Allergen and diet labels describe declared ingredients, not a real kitchen.** The loader
  rejects unclassified ingredients and labels that disagree with them. Compound ingredients
  (green curry paste, chili-garlic oil, dark chocolate, kombu broth) are judged by the house
  recipes declared in `menu.py`, not by every commercial version. Cross-contact is not
  modeled, so the app makes claims about declared recipes, not medical allergy safety. Tell
  customers with severe allergies to confirm with staff.
- **The model's message is not fully checked.** Recommended dishes are written by code from
  the eligible set, so the recommendation list never contains an ineligible dish. The
  message itself is checked for
  full dish names and typed prices, but a dish described in other words ("the shrimp
  noodles") gets past the check; only the prompt forbids it.
- **"Tonight" means "this conversation".** Returning in a new conversation the same evening
  drops a temporary preference (SPEC §9).
- **One server process.** Memory updates are serialized by a lock inside the process, so
  concurrent requests are safe on one instance. Several instances would need the optimistic
  concurrency described in SPEC §4.
- **Sign-in is deliberately simple** (username and PIN, scrypt-hashed, no rate limiting).
  That suits a demo, not a production deployment.
