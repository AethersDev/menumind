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

## The challenge: memory that changes safely

- **Problem.** In food, memory mistakes are the costly ones: an allergy forgotten, a one-night
  wish kept forever, an allergy dropped because a sentence was misread. A model that writes
  its own memory makes each of these possible and leaves nothing to audit.
- **Why this one.** Memory and changing preferences are the brief's core (2.3, 2.4) and the
  place where a wrong answer can hurt a customer. Voice, scale or integrations would make the
  assistant faster or wider, not safer, so I chose memory over them.
- **What I built.** The model only describes what was said, as typed claims. Eleven ordered
  rules in code decide what is saved, asked or ignored (`SPEC.md` §5); every change is an
  append-only event; "tonight" lasts one conversation; an allergy is removed only after a yes
  to the app's own question, bound to that conversation; replies may only recommend dishes
  code has verified.
- **Left out on purpose.** Learning preferences from behaviour, a clock for "tonight", and
  running on several servers. Rejected: letting the model write memory directly, because
  nothing it saved could be audited. With more time: one transaction per message, concurrency
  control for several servers, and verified supplier recipes.
- **Validation.** 197 tests with no model calls; an acceptance test over HTTP across a server
  restart (15 of 15 checks); live runs on `gpt-5.4-mini` (42 of 42 conversations fully correct,
  no unsafe recommendation in 75 turns; see "Evidence with a real model"); and a review of
  every file (`AUDIT.md`), whose open issues are listed under limitations.

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

## Run it

You need Python 3.11 or newer and an OpenAI API key. The backend is FastAPI; memory is one
SQLite file, created on first start. Tested on macOS with Python 3.13.

```bash
git clone https://github.com/AethersDev/menumind menumind
cd menumind
python3 run.py                          # Windows: py run.py
```

`run.py` creates `.venv`, installs `requirements.txt` into it, asks for your OpenAI API key
(typed hidden, kept only in that process, never written to disk; press Enter to run with chat
off) and starts the app. Then open http://127.0.0.1:8000. The same command starts it again
later. On Debian or Ubuntu, `sudo apt install python3-venv` first. Tested from a fresh copy
on macOS; on Linux and Windows it uses the standard `venv` layout but was not run there.

Step by step instead:

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export OPENAI_API_KEY="your-api-key"   # Windows PowerShell: $env:OPENAI_API_KEY="your-api-key"
uvicorn server:app --port 8000
```

The hosted copy, https://menumind-production-4958.up.railway.app, runs the same app on gpt-5.4-mini: sign up with any
username (3 to 32 lowercase letters, digits, `.`, `_` or `-`) and a 4 to 12 digit PIN, and
chat. Use test details, not real personal data. It is shared and capped (300 messages a day in
total, 30 per customer per 10 minutes), so for a longer session run it locally as above.

**Model: `gpt-5.4-mini`** (OpenAI, Chat Completions), for every model call. It is the
default, so there is nothing to set: `LLM_PROVIDER` defaults to `openai`, and the OpenAI
default model is `gpt-5.4-mini` (`DEFAULT_MODELS` in `llm.py`). This is the configuration the
evaluation below was measured with. To pin it explicitly, also
`export EXTRACT_MODEL=gpt-5.4-mini REPLY_MODEL=gpt-5.4-mini`. To check the whole app end to
end over HTTP (it signs up two test customers and spends a few model calls):
`python acceptance.py --no-restart` in a second terminal.

On macOS the key can come from the Keychain instead:
`export OPENAI_API_KEY="$(security find-generic-password -s openai-api-key -w)"`.
To use Anthropic instead, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`; its default
model is `claude-haiku-5-5`. `.env.example` lists every setting.

Requests to OpenAI are sent with `store=false`, so they aren't kept for OpenAI's distillation
and evals products. That is separate from data sharing: if the OpenAI organization shares its
API traffic with OpenAI (the condition for OpenAI's free daily tokens), OpenAI receives every
message, allergies included. That suits a demo with test conversations, not real customers.

**Without an API key**, `python preview.py` runs the same app with a scripted model that
understands a handful of phrases (they print at startup). Use it for UI work.

The database is `data/menumind.db`. Stop the server and delete it to start fresh.

## A five-minute tour

The order ticket on the right shows what MenuMind remembers, split into "every visit" and
"just this conversation". Each reply has a "How I read that" section with the preferences it
heard and the rule that decided what to do with each. These phrases are the ones filmed for
the walkthrough video, with `gpt-5.4-mini`; the right column is the brief's requirement shown.

| # | Do this | You should see | Requirement |
|---|---|---|---|
| 1 | Create an account: any username, a 4 to 12 digit PIN | An empty ticket | |
| 2 | "I love spicy food, but I hate mushrooms." | The reply's first line, written by the app: "Every visit: spice: hot; dislikes mushroom." Dishes recommended with names and prices from the menu | 1, 2 |
| 3 | "I'm allergic to peanuts. What main would you suggest?" | "Every visit: avoiding peanut (allergy)." Chicken Satay, Shrimp Pad Thai and Tofu Pad Thai are never recommended | 2, 4 |
| 4 | "Does the pad thai have peanuts?" | An answer, with the dish marked "not suitable for you". Nothing is saved: it's a question | 1 |
| 5 | "Nothing spicy tonight, and keep it under $15." | "Just this conversation: ..." On the ticket: "Spice: none (saved: hot)", "Budget: up to $15.00 (saved: not set)" | 4 |
| 6 | Sign out, sign back in, click "New conversation" | Peanut allergy, spice and mushrooms still saved; tonight's spice and budget gone | 3 |
| 7 | "I'm no longer allergic to peanuts." | Only the app's question: "Should I stop treating peanut as an allergy? I'll keep avoiding it until you confirm." The allergy stays on the ticket | 4 |
| 8 | "yes" | "Every visit: no longer avoiding peanut." Nothing else changes | 4 |
| 9 | In a private window, create a second account | An empty ticket: nothing from the first customer | 5 |

Requirement 6, the interface, is this page; the hosted copy serves it too. Also
worth trying: "I think I might be allergic
to shellfish" (avoided now, and the app asks whether to save it), "I'm allergic to nuts"
(saved as both peanut and tree nut), and "Actually, I'm still allergic to peanuts" while a
removal question waits (the question is dropped, and the allergy stays).

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
turn that asks a question (a confirmation or a clarification), or asks it again after an
answer that was neither yes nor no, is answered by the app alone: what changed, then the
question, so a "yes" can only answer the app's question. In live runs the model said an
allergy removal had taken effect before the customer confirmed it (3 times), once asked
"Would you like me to suggest a starter?" in place of the app's question, so the customer's
"yes" to that removed the allergy, and, replying while the question waited, offered "If you
want, I can suggest something from the menu." just above it (3 of 3 in the final run).

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
| `run.py` | One command to install and start the app on macOS, Linux or Windows |
| `static/index.html` | The chat page |
| `SPEC.md` | The memory specification: rules, invariants, test layers |
| `test_*.py` | Tests (no model calls) |
| `eval_extractor.py`, `extractor_cases.json`, `confirmation_cases.json` | Model evaluation: extractor and yes/no classifier |
| `eval_conversations.py`, `conversation_cases.json` | Model evaluation: whole conversations through the app |
| `acceptance.py` | End-to-end acceptance test of a running server, over HTTP |
| `evidence/` | Raw reports of the final live run, behind the numbers in "Evidence with a real model", and of run 4 (`run4-*`) |
| `AUDIT.md`, `AUDIT_findings.json` | Review of every file: each finding with its reproduction, what was fixed, what is open |
| `Dockerfile`, `.dockerignore`, `requirements.txt`, `.env.example`, `pytest.ini` | The hosted image, dependencies, every setting with its default, test configuration |
| `video/` | The walkthrough: script (`sections.py`; `narration.md` and `storyboard.md` are the generated script and timeline), real-app capture (`capture.py`), the film page it is rendered from (`walkthrough/`) and the build, which narrates it with a local synthetic voice and checks the narration against the script by transcribing it back. Its cold open, "The Spike" (`spike/`), is a short film whose data is recorded by running this code on a scripted conversation (`spike_trace.py`), every frame rendered in 3D in Blender (`spike3d.py`, `spike_render.py`) |
| `fake_llm.py`, `preview.py` | Scripted model for tests and key-free preview |

## Tests and evaluation

```bash
pytest                                        # 197 tests, no API calls
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
`eval_extractor.py`, then `eval_conversations.py --repeats 3`. The raw reports are
`evidence/eval_report_gpt-5.4-mini.json` (extractor and yes/no classifier) and
`evidence/eval_report_conversations_gpt-5.4-mini.json` (conversations); run 4's are kept as
`evidence/run4-*.json`. The app has changed since this run: the fix in the table's last row,
and a re-check of each reply against memory saved meanwhile in another tab, which these
one-at-a-time runs never trigger. The run was not repeated after them.

| What | Result |
|---|---|
| Unsafe recommendations (a dish the customer's allergies or diet rule out) | 0 in 75 turns |
| Hard restrictions captured: enforced now, remembered | 10/10, 8/8 |
| Unsafe loosenings applied, proposed | 0, 0 |
| False "yes" readings of confirmation answers | 0 of 18 |
| Restrictions saved that nobody stated | 0 |
| Conversations, turns fully correct | 42/42, 75/75 |
| Fallback replies (model output unusable twice), model replies rejected | 0, 0 |
| Turns that ask a question, answered by the app alone | 9 of 12 (the 3 others asked again after "not sure", under the model's offer of a dish; fixed after this run) |
| Extractor decision accuracy, exact claims | 0.938, 0.781 |
| Yes/no classifier accuracy | 18/18 |
| Latency: extraction mean, p95; whole turn mean, p95, max | 2.1 s, 4.7 s; 2.5 s, 4.3 s, 11.0 s |

Misses in that run, none of them involving an allergy:

- In the extractor evaluation, "I used to be vegetarian." was read as "no longer vegetarian,
  in the past" once. The diet stays vegetarian, but the app then doesn't ask how the customer
  eats now. The same sentence was read correctly in all 3 conversation repeats, and the app
  asked.
- "It's not that I don't like mushrooms, I just don't want them today." was misread, so
  mushrooms are not avoided that evening.

The result took four rounds of fixes, plus one found while filming the walkthrough. Each run
was read in full, replies included:

| Run | What it found | Fix |
|---|---|---|
| 1 | "Nuts" saved as tree nut only (4 of 4); a correction to cashews saved as a like; "I love spicy food" saved as an ingredient; "I used to be vegetarian" read backwards; 6 replies called a saved change temporary | Extraction rules stated explicitly |
| 2 | A request for a main fell back to starters; 3 replies still called a saved change temporary; a friend's diet saved as the customer's | The app writes the saved/temporary line; the fallback keeps the model's valid dishes; third-party rule |
| 3 | 39/39 conversations, but the third-party rule made the extractor drop a companion's allergy (9/10) | Rule restated so companions count |
| 4 | 0 unsafe recommendations, 41/42 conversations; a spice phrasing, the mushroom sentence and one yes/no answer misread | |
| Filming | Asked to confirm an allergy removal, the model said the removal had happened (3 times across runs) and once asked its own yes/no question instead, so the customer's "yes" to that removed the allergy | A turn that raises a question is answered by the app alone |
| Final | The table above; in its replies, asked to reply while the question waited, the model offered a dish just above it, an offer a "yes" would answer (3 of 3, found in review) | A turn that asks again is answered by the app alone too (made after this run, not re-run; it changes that one turn per run) |

Limits: 32 phrasings, 18 answers and 14 conversations show that the model handles these, not
every phrasing, so the numbers are counts, not rates. Model output varies even with the same
code (in run 4 the spice sentence failed in 1 of 3 repeats; in the final run the past-diet
sentence was misread once of 4 readings), so use `--repeats` and rerun both evaluations after
any change to a prompt or model. Reply text is checked for typed dish names and prices, not
for every factual claim; the JSON reports keep every reply for reading.

## Hosting

The hosted copy, https://menumind-production-4958.up.railway.app, runs on Railway with a persistent volume at `/data` and the
model key set as a Railway variable (never in the repository): anyone can sign up and chat.
Chat is capped in `server.py` (`CHAT_PER_USER`: 30 messages per customer per 10 minutes;
`CHAT_TOTAL`: 300 a day across everyone, about 1.5M tokens), so a public URL cannot run up an
unbounded bill. Without the key, the page says chat is off and points to "Run it".

GitHub Pages only serves static files, and this app needs a running server (it holds the
memory database, and the API key when there is one). Push the repository to GitHub, then
connect it to a host that builds from a Dockerfile, for example Render, Fly.io or Railway:

1. Create a service from the GitHub repository, using the included `Dockerfile`.
2. Optional: attach a persistent volume mounted at `/data` (the database lives at
   `/data/menumind.db`). Without one, accounts reset whenever the service restarts.
3. Leave `OPENAI_API_KEY` unset for a key-less copy: `/api/health` then reports
   `"chat_enabled": false` and the page turns chat off. To enable chat, set it as a secret (or
   `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`). `COOKIE_SECURE=1` is already set for HTTPS.
4. Run a single instance (see limitations).
5. The app refuses request bodies over 16 KB itself; a cap at the proxy is a second line.
6. With a key set, run the acceptance test against the public URL, and restart the service
   when it asks:

   ```bash
   python acceptance.py --url https://your-app.example.com
   ```

   It signs up two customers and walks the main workflow: A's peanut allergy is saved and
   every recommendation avoids peanut; a new conversation and a server restart both keep it;
   removing it asks first, keeps avoiding peanut until A says yes, then removes only the
   allergy; customer B sees none of A's memory and cannot open A's conversation. It prints
   PASS or FAIL per check and exits 1 on any failure. If the database is not on a persistent
   disk, it stops at the restart. On a key-less copy it stops at once, since chat is off; run
   it locally instead, against a server started with your key: `python acceptance.py
   --no-restart`, or restart `uvicorn` when asked.

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
- **One server process.** Memory updates are serialized by a lock inside the process, and a
  reply is checked against memory as it stands when the reply is sent (another tab may have
  changed it meanwhile), so concurrent requests are safe on one instance. Several instances
  would need the optimistic concurrency described in SPEC §4.
- **Known issues, deferred.** A review of every file (AUDIT.md: 118 findings, 27 fixed) left
  these open as the most important: removing your own allergy also drops a companion's
  allergy set for this conversation only (`memory.py:436`); the evaluation's unsafe-recommendation count checks dishes against the app's own profile, so a
  missed allergy would not count (the final run captured every restriction, 10/10, so its 0
  holds); and a dish with no ingredients would load as vegan and allergen-free (no such dish
  on the menu).
- **Sign-in is deliberately simple.** Username and PIN, scrypt-hashed; 5 failed attempts
  per username lock it for 15 minutes; sessions last 30 days. The lock is per username, so
  anyone can lock a username out for 15 minutes, and there is no per-IP limit. That suits a
  demo, not a production deployment.
