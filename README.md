# MenuMind

**A grounded, memory-aware restaurant assistant.**

A neuro-symbolic conversational recommendation system combining LLM-based language
understanding, deterministic recommendation logic, and event-sourced preference memory.

MenuMind learns each customer's preferences from normal conversation, remembers them across
visits, and recommends real dishes from a fixed menu. Recommended dishes, their names and
their prices always come from the menu and pass the customer's allergies and diet. An allergy
is never removed without a confirmed yes, and a diet only loosens when the customer names the
new one. The model's own sentences are checked only in part (see limitations). The demo serves
the menu of a fictional restaurant, Lantern Street Kitchen.

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
  eligible set and writes each dish's name and price from the menu, once: where the message
  mentions it, or in a list under the message with the ranking's reasons.

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

# the OpenAI key, read from the macOS Keychain (service "openai-api-key")
export OPENAI_API_KEY="$(security find-generic-password -s openai-api-key -w)"

uvicorn server:app --reload
open http://127.0.0.1:8000
```

Create an account with a username and a 4 to 12 digit PIN, then chat. The order ticket on
the right shows what MenuMind remembers, split into "every visit" and "just this
conversation"; a temporary change is listed with the saved value it overrides. Each reply has
a "How I read that" section showing the preferences it heard and the rule that decided what
to do with them.

The model provider is OpenAI by default: `gpt-5.4-mini` through Chat Completions for every
model call. To use Anthropic instead, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`;
its default model is `claude-haiku-5-5`. `EXTRACT_MODEL` and `REPLY_MODEL` override the model
per step, and `.env.example` lists the settings.

Requests to OpenAI are sent with `store=false`, so they aren't kept for OpenAI's distillation
and evals products. That is separate from data sharing: if the OpenAI organization shares its
API traffic with OpenAI (the condition for OpenAI's free daily tokens), OpenAI receives every
message, allergies included. That suits a demo with test conversations, not real customers.

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
                                            prices, then writes each recommended dish once
                                            (name, price; reasons when listed) from the menu
```

If the extractor fails, the app stops there: nothing is saved, no pending question is
answered, and nothing is recommended, because the unread message may have stated an allergy.
If only the reply model fails, or breaks a rule twice in a row, the app answers with a reply
built from verified data: the model's own dishes when only its wording broke a rule, or else
the top eligible dishes.

Every reply that changed a preference starts with a line the app writes, not the model:
"Every visit: ..." for what is saved and "Just this conversation: ..." for what is temporary,
matching the ticket. In live runs the model sometimes called a saved change temporary. A
turn that raises a question (a confirmation or a clarification) is answered by the app alone:
what changed, then the question. While the question waits, the model answers later messages
but may not ask anything itself. In live runs the model said an allergy removal had taken
effect before the customer confirmed it (3 times), and once asked "Would you like me to
suggest a starter?" in place of the app's question, so the customer's "yes" to that removed
the allergy.

## Files

| File | What it does |
|---|---|
| `memory.py` | Preference model, decision rules, event log, profile fold, confirmation lifecycle |
| `extractor.py` | Model calls: reading preferences from a message, classifying yes/no answers |
| `llm.py` | Model provider: OpenAI (default) or Anthropic behind one call shape, timeouts, retries |
| `menu.json`, `menu.py` | The menu (19 dishes). Every ingredient is classified (compound ones through declared house recipes), and each dish's allergens and diet must match its ingredients |
| `recommend.py` | Hard filtering, soft ranking, grounded rendering and reply checks |
| `agent.py` | One chat turn end to end |
| `accounts.py`, `server.py` | Username and PIN sign-in, HTTP API |
| `static/index.html` | The chat page |
| `SPEC.md` | The memory specification: rules, invariants, test layers |
| `test_*.py` | Tests (no model calls) |
| `eval_extractor.py`, `extractor_cases.json`, `confirmation_cases.json` | Model evaluation: extractor and yes/no classifier |
| `eval_conversations.py`, `conversation_cases.json` | Model evaluation: whole conversations through the app |
| `acceptance.py` | End-to-end acceptance test of a running server, over HTTP |
| `video/` | The walkthrough: script (`sections.py`), real-app capture, cards and build |
| `fake_llm.py`, `preview.py` | Scripted model for tests and key-free preview |

## Tests and evaluation

```bash
pytest                                        # 192 tests, no API calls
```

The tests use a scripted model. Evidence that MenuMind works with a real model comes from two
evaluations that call it (they spend tokens; export the key first, as above):

```bash
python eval_extractor.py                      # extractor + yes/no classifier, 32 + 18 cases
python eval_conversations.py --repeats 3      # 14 conversations through the whole app
python eval_extractor.py --model gpt-5.4-nano --price-in <USD/Mtok> --price-out <USD/Mtok>
```

`eval_extractor.py` reports safety first, as counts: restrictions enforced and remembered,
unsafe loosenings applied and proposed, and false "yes" readings of confirmation answers.
Then it reports quality, latency and cost. SPEC §11 explains how the scores are computed and
how to choose between models.

`eval_conversations.py` runs scripted conversations covering the six requirements and the
safety rules (allergy removal needs a yes, "tonight" lasts one conversation, a companion's
allergy holds for the conversation, a question saves nothing, a hedged allergy is enforced and
asked about). After each turn it checks what the
app did, not the model's wording: the saved and enforced profiles, the pending question, and
the recommended dishes, each of which is also checked against the customer's allergies and
diet. It reports unsafe recommendations first, then correct cases and turns, extraction
failures, fallback replies, rejected model replies, a heuristic count of replies that call a
saved change temporary, and latency. It keeps every reply in the JSON report for reading. Run with `--repeats` to see how often the model varies.

## Evidence with a real model

Measured on 2026-10-13 with `gpt-5.4-mini` (OpenAI, Chat Completions) for every model call:
`eval_extractor.py`, then `eval_conversations.py --repeats 3`. Later, the prepared ingredients
got declared house recipes, which corrected one dish label (Spicy Chicken Bowl: no gluten); no
evaluation case involves gluten, and prompts and logic did not change.

| What | Result |
|---|---|
| Unsafe recommendations (a dish the customer's allergies or diet rule out) | 0 in 75 turns |
| Hard restrictions captured: enforced now, remembered | 10/10, 8/8 |
| Unsafe loosenings applied, proposed | 0, 0 |
| False "yes" readings of confirmation answers | 0 of 18 |
| Restrictions saved that nobody stated | 0 |
| Conversations, turns fully correct | 41/42, 74/75 |
| Fallback replies (model output unusable twice) | 0 |
| Extractor decision accuracy, exact claims | 0.969, 0.812 |
| Yes/no classifier accuracy | 17/18 |
| Latency: extraction mean, p95; whole turn mean, p95, max | 1.2 s, 1.5 s; 2.5 s, 3.7 s, 5.0 s |

Misses in that run, none of them involving an allergy or diet:

- "I don't like spicy food anymore." cleared the spice preference instead of setting it to
  none in 1 of 3 conversations, so spicy dishes were not ranked down.
- "It's not that I don't like mushrooms, I just don't want them today." was read as a like.
- "Sure, and nothing spicy tonight." as the answer to an allergy-removal question was read as
  unclear, so the question stayed open.

The result took four runs. Each was read in full, replies included, and followed by a fix:

| Run | What it found | Fix |
|---|---|---|
| 1 | "Nuts" saved as tree nut only (4 of 4); a correction to cashews saved as a like; "I love spicy food" saved as an ingredient; "I used to be vegetarian" read backwards; 6 replies called a saved change temporary | Extraction rules stated explicitly |
| 2 | A request for a main fell back to starters; 3 replies still called a saved change temporary; a friend's diet saved as the customer's | The app writes the saved/temporary line; the fallback keeps the model's valid dishes; third-party rule |
| 3 | 39/39 conversations, but the third-party rule made the extractor drop a companion's allergy (9/10) | Rule restated so companions count |
| 4 | The table above | |

Limits: 32 phrasings, 18 answers and 14 conversations show that the model handles these, not
every phrasing, so the numbers are counts, not rates. Model output varies even with the same
code (in run 4 the spice sentence failed in 1 of 3 repeats), so use `--repeats` and rerun
both evaluations after any change to a prompt or model. Reply text is checked for typed dish names and prices, not
for every factual claim; the JSON reports keep every reply for reading.

## Deploying from GitHub

GitHub Pages only serves static files, and this app needs a running server (it holds the API
key and the memory database). Push the repository to GitHub, then connect it to a host that
builds from a Dockerfile and offers a persistent disk, for example Render, Fly.io or Railway:

1. Create a service from the GitHub repository, using the included `Dockerfile`.
2. Attach a persistent volume mounted at `/data` (the database lives at `/data/menumind.db`).
3. Set `OPENAI_API_KEY` as a secret (or `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`).
   `COOKIE_SECURE=1` is already set for HTTPS.
4. Run a single instance (see limitations).
5. Cap request bodies at the proxy if your host allows it; the app caps fields, not bodies.
6. Run the acceptance test against the public URL, and restart the service when it asks:

   ```bash
   python acceptance.py --url https://your-app.example.com
   ```

   It signs up two customers and walks the main workflow: A's peanut allergy is saved and
   every recommendation avoids peanut; a new conversation and a server restart both keep it;
   removing it asks first, keeps avoiding peanut until A says yes, then removes only the
   allergy; customer B sees none of A's memory and cannot open A's conversation. It prints
   PASS or FAIL per check and exits 1 on any failure. If the database is not on a persistent
   disk, it stops at the restart. Locally: `python acceptance.py --no-restart`, or restart
   `uvicorn` when asked.

Logs go to stderr. Model failures and rejected model replies are warnings, logged with user
ids but not message text (an extraction error may quote the value it rejected). Unexpected
errors are logged with a traceback and answered with a generic 500. Each model call times out
after 30 seconds and is retried once (`MODEL_TIMEOUT_S`, `MODEL_RETRIES` in `llm.py`);
tune them from the latency the evaluation reports.

## Limitations, stated plainly

- **Allergen and diet labels describe declared ingredients, not a real kitchen.** The loader
  rejects unclassified ingredients and labels that disagree with them. The twelve house-made
  sauces, pastes, buns, wrappers, oil, broth, chocolate and crackers have declared recipes in
  `menu.py`; a recipe may use another (teriyaki sauce uses the house soy sauce), and a dish's
  allergens and diet are derived from the basic ingredients. These are the fictional
  restaurant's own declarations, not facts about commercial products; a real restaurant would
  need verified formulations from its suppliers. Other ingredients are classified directly,
  including some prepared ones whose allergens come from a single source (udon noodles,
  fish sauce, paneer, cream cheese, garam masala, rice and glass noodles).
  Cross-contact is not modeled, so the app makes claims about declared recipes, not medical
  allergy safety. Tell customers with severe allergies to confirm with staff.
- **The model's message is not fully checked.** Recommended dishes are written by code from
  the eligible set, so the recommendation list never contains an ineligible dish. The
  message itself is checked for full dish names and typed prices, but a dish described in
  other words ("the shrimp noodles") or one that doesn't exist ("our lobster roll") gets past
  the check; only the prompt forbids it.
- **A message's changes are checked together but saved one by one.** Every claim in a message
  is validated before anything is saved, so invalid model output saves nothing. A database
  failure partway through can still save part of a message; the app then answers with an
  error and refreshes the ticket so it shows what was saved. Making each message one
  transaction needs a change to `memory.py`.
- **Conversations end.** A conversation ends after 3 hours unused, when its owner opens a
  sixth one (5 open per user), or when 1,000 are open on the server; the oldest go first.
  Conversation ids are issued by the server and bound to one user. Ending a conversation,
  or restarting the server, drops its temporary preferences, its unanswered question and its
  recent context; saved preferences stay. The page starts a new conversation when it finds
  the old one has ended.
- **"Tonight" means "this conversation".** Returning in a new conversation the same evening
  drops a temporary preference (SPEC §9).
- **One server process.** Memory updates are serialized by a lock inside the process, so
  concurrent requests are safe on one instance. Several instances would need the optimistic
  concurrency described in SPEC §4.
- **Sign-in is deliberately simple.** Username and PIN, scrypt-hashed; 5 failed attempts
  per username lock it for 15 minutes; sessions last 30 days. The lock is per username, so
  anyone can lock a username out for 15 minutes, and there is no per-IP limit. That suits a
  demo, not a production deployment.
