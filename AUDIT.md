# MenuMind audit

> Current follow-up: MM-005 and the reported MM-117 interleaving are independently verified fixed. MM-118 identifies mismatched submission evidence. Earlier sections retain historical findings; see the final verification section for current evidence and limits.

Status: complete (2026-10-14). 28 files × 10 categories = 280 cells, every one reviewed. 116 findings (13 high, 37 medium, 66 low); 24 fixed in this pass, 1 partly fixed, 1 kept by design, 90 open. Full evidence, reproductions and fixes for each finding are in [AUDIT_findings.json](AUDIT_findings.json).

Scope: the working tree's authored Python, HTML/embedded JavaScript and the two generated JavaScript payloads (pre-registered below). Dependencies, caches, binaries and media are excluded; configuration and fixtures were read as supporting evidence (one finding is in menu.json).

## Method

Six auditors worked in parallel, each on a slice of the inventory:

- **A1**: memory, menu, recommendation
- **A2**: agent, extractor, LLM adapter, accounts, server
- **A3**: chat page, app tests, acceptance, preview
- **A4**: evaluations, capture, narration script
- **A5**: The Spike film and its trace
- **A6**: 3D render pipeline, walkthrough build and page

For every file, each auditor wrote a note for all ten categories (clear, finding, or not applicable), and every finding carries evidence plus a reproduction: a script run on a scratch copy, a measurement, or a stated line of reasoning when nothing could be run. No live model calls, git commands or network access were used, and the read-only files (memory.py, SPEC.md, test_memory.py, extractor_cases.json) were not modified.

The lead then re-checked every high finding (re-running repros, reading the code, or viewing the frames), merged 3 duplicates found by two auditors, and re-graded 2 findings. 11 findings are marked *plausible*: the mechanism is shown, but the failure was not reproduced end to end.

Files changed after pre-registration (9): `agent.py`, `test_app.py`, `video/build.py`, `video/sections.py`, `video/spike/index.html`, `video/spike3d.py`, `video/spike_render.py`, `video/walkthrough/data.js`, `video/walkthrough/index.html`. The ongoing video work changed them before the audit ran (findings refer to that tree), and the fixes below changed the video files again (plus video/sections.py). Both hashes are recorded in the JSON inventory (`sha256`, `sha256_final`).

## The high findings

- **MM-001** `eval_conversations.py:53` — 'Unsafe recommendations' checks the app against itself: a missed allergy that leads to an unsafe dish scores 0 unsafe
- **MM-002** `memory.py:436` — A companion's conversation-scoped allergy or diet is dropped when the customer removes or loosens their own restriction on the same key
- **MM-003** `menu.py:134` — Latent: a dish with an empty ingredients list loads as vegan and allergen-free
- **MM-004** `static/index.html:352` — A reply that arrives after 'New conversation' (or sign-out and sign-in) is rendered into the new conversation, with the old conversation's temporary allergies and pending question on its ticket
- **MM-005** `agent.py:362` — while an allergy-removal question waits, the only guard against the model eliciting a 'yes' is a literal '?' check — **fixed**
- **MM-006** `video/build.py:155` — regions() reads a blank white loading frame as a full-height order ticket, and S03's spotlight rings the whole right column — **fixed**
- **MM-007** `video/sections.py:43` — S05 says the allergy filter is applied 'before the model sees a dish'; the code and the footage show the model sees every dish — **fixed**
- **MM-008** `video/spike/index.html:709` — Walled cards (S4, M5, M6) jump from 39% to 97% opacity in one frame at 81.333 s — **fixed**
- **MM-009** `video/spike/index.html:747` — The R7 decision slip never appears in a rendered frame, and renders at 14px in the player — **fixed**
- **MM-010** `video/spike/index.html:847` — Camera snaps back to the threads view during 'Order up.' and hard-cuts at the receipt (overlapping camera tweens) — **fixed**
- **MM-011** `video/walkthrough/index.html:266` — overlapping camera tweens snap the framed screen in a single frame, 10 times in the film — **fixed**
- **MM-012** `video/walkthrough/index.html:319` — spotlights use a stale region and stay put while the footage scrolls, so S07 frames the wrong element — **fixed**
- **MM-013** `video/walkthrough/index.html:586` — The acceptance card shows six invented PASS rows when the build gets no --acceptance output (as in the current data.js), badges every real line PASS (FAIL lines included), and prints 'link added at publishing' without --repo — **fixed**

## What to fix first

Everything high that ships in the video is fixed (below), and so is the agent.py yes-guard. What is left is in the app and its evidence.

1. **Video, needs your run**: the final build.py run with the live and repository links (the new capture and the acceptance run are done and checked).
2. **App safety, cheap**: menu.py:134 — reject a dish with no ingredients at load. (agent.py:362, the yes-guard, is fixed.)
3. **App safety, owner's decision** (memory.py is read-only): memory.py:436 — removing the customer's own allergy also drops a companion's conversation-scoped one.
4. **UI**: static/index.html:352 — a late reply lands in the next conversation; guard it with the conversation id.
5. **Evidence**: eval_conversations.py:53 — the 'unsafe recommendations' headline checks dishes against the app's own (possibly wrong) profile; check against the case's expected restrictions.

## Fixed in this pass

The video was changed (the Spike film, its 3D render, the walkthrough page, its build and the narration), because it ships as is; in the app, only the yes-guard (agent.py, with its test and the README), at the owner's request.

- **MM-005** `agent.py:362` (fixed): agent.py: a turn whose reply carries a waiting question (raised, or asked again) is answered by the app alone, so a 'yes' can only answer the app's question; the unreachable '?' check, the 'raised' flag and fallback_reply's 'asking' are gone, and the model's input is unchanged. test_the_app_asks_its_own_question_and_no_other now uses the live offer: it fails on the old agent.py and passes on the new one (196 passed); the repro shows only the app's question before the 'yes'. README updated. Owner's request, 2026-10-14.
- **MM-006** `video/build.py:155` (fixed): footage() computes regions only from the first frame that shows the app; regionAt() returns nothing before it. The S03 spot is now the ticket itself.
- **MM-007** `video/sections.py:43` (fixed): S05 now says 'An allergy is a hard filter in code: the model may only recommend dishes that pass it, so satay and pad thai are never suggested.' Read back by Whisper word for word (1.0).
- **MM-008** `video/spike/index.html:709` (fixed): One opacity tween per card (0.66 if walled, else 1); the second tween is gone.
- **MM-009** `video/spike/index.html:747` (fixed): Class renamed to 'slip decision' (CSS and element).
- **MM-010** `video/spike/index.html:847` (fixed): Drift lasts toks.length*3.2+.1 s, ending as 'Order up.' pulls back; the score's 'air' cue keeps dur 8 (cam(..., heard)). Cue list byte-identical (99 cues); no one-frame snaps in the film.
- **MM-011** `video/walkthrough/index.html:266` (fixed): Every camera move is collected, then laid down in time order: the screen's entrances and exits keep their times and a framing move waits for them; a framing move the next one starts before it gets halfway is dropped, any other is cut short to end as the next begins. Frame-by-frame probe: no one-frame snap in the film (10 before), and the screen is fully up through all of its footage.
- **MM-012** `video/walkthrough/index.html:319` (fixed): Regions are computed for every frame shown; a mark waits until its region is on screen and still for 1 s, and fades before the region moves. Probe: every visible spotlight matches regions() run on the frame actually on screen (0 frames off; 39 before this change).
- **MM-013** `video/walkthrough/index.html:586` (fixed): build.py requires --url, --repo and --acceptance, keeps only acceptance.py's PASS/FAIL lines and exits on any FAIL; the page shows each line's own status, all of them (15), and no placeholder rows or links.
- **MM-042** `video/sections.py:31` (partly fixed): S03 fixed with MM-006: the mark waits for the app's first frame and rings the ticket. S15's 'Cross-contact' mark still follows the cut from the recipes card to the footage (structural, about a second).
- **MM-043** `video/sections.py:105` (fixed): The claim is gone from the narration (S17) and from the testing card (its fault/test-fails panels and CAUGHT stamp).
- **MM-044** `video/spike/index.html:587` (fixed): Caption reads 'Every change goes on the spike.'
- **MM-045** `video/spike/index.html:786` (fixed): bottom: 'auto' added; the box is 56 px for the 40 px text (was 46 px).
- **MM-046** `video/spike/index.html:892` (fixed): Every row is computed from the trace (sessions, pending, pending_cleared, late_yes, mentioned vs MENU); 'Tests passing' throws unless the run passed. Values unchanged.
- **MM-047** `video/spike/index.html:895` (fixed): Row replaced by 'Dishes from the menu: 2 of 2' (computed); poster now says 'never invents a dish's name or price'.
- **MM-048** `video/spike_render.py:103` (fixed): ffmpeg writes MenuMind_The_Spike.part; it replaces the MP4 only after every frame is encoded and ffmpeg exits 0.
- **MM-049** `video/walkthrough/index.html:6` (fixed): The page throws if a font does not load (checked with Google Fonts blocked: not ready, 'font not loaded'), and build.py prints page errors instead of a bare timeout.
- **MM-050** `video/walkthrough/index.html:318` (fixed): A reply mark waits for the next reply to land (in whichever footage of the section it lands); any other mark shows at its words once its region is on screen, or once that next reply lands if the reply changes the region (S05, S07: the ticket). S11's mark now comes at its words (+6.7 s before).
- **MM-109** `video/build.py:15` (fixed): spike3d.py and spike_render.py docstrings say EEVEE; build.py's no longer promises voice_cues.md.
- **MM-110** `video/build.py:224` (fixed): layout() exits when a visual that is not the last has no share and no footage.
- **MM-111** `video/build.py:301` (fixed): stamp() rounds before splitting: 59.96 s prints 1:00.0.
- **MM-113** `video/sections.py:3` (fixed): Docstring now says build.py writes the narration and the storyboard from here.
- **MM-114** `video/sections.py:107` (fixed): S17 now says 'fourteen scripted conversations, run three times each, all passed'.
- **MM-115** `video/spike_render.py:94` (fixed): Blender runs with --python-exit-code 1.
- **MM-116** `video/walkthrough/index.html:723` (fixed): The page loads its fonts before building the timeline (async start). Wire geometry is identical on cold and warm loads.
- **MM-112** `video/build.py:354` (kept by design): The owner set the 6:00 cap on the narrated walkthrough, not on the joined film with the music-only Spike; the check measures what the cap covers.

Checks: the score's 99 cues, the duration (152.834 s) and the 11 chapters are byte-identical to the baseline; a frame-by-frame scan finds no one-frame camera snap (the pre-fix page has two of 421 px, used as the control); random-order seeks equal sequential ones; the 3D frames already rendered were kept only where film.json and every visible paper texture are unchanged (camera frames 3766-4086 and receipt frames 4117-4584 re-render). Walkthrough: a frame-by-frame probe of all 7,775 frames finds no one-frame camera snap and no spotlight off the region that regions() finds on the frame actually on screen; the three rewritten narration sections read back word for word (S17 drops its first word only in Whisper's transcript: Kokoro says it, checked on the clip's first 3 s); the screen is fully up through every piece of footage; all 19 marks appear. The probe ran on the owner's new capture and real acceptance.py run (15/15); only the two links in data.js are placeholders until the final build.

## Coverage ledger

F = finding, ✓ = reviewed and clear, – = not applicable (for example, a generated payload's test gap). Each cell's note is in the JSON (`coverage`).

| File | LOC | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---:|---|---|---|---|---|---|---|---|---|---|
| acceptance.py | 120 | ✓ | ✓ | F | F | ✓ | F | ✓ | ✓ | F | F |
| accounts.py | 129 | ✓ | ✓ | ✓ | ✓ | F | ✓ | F | ✓ | F | F |
| agent.py | 435 | ✓ | F | F | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | F |
| eval_conversations.py | 140 | ✓ | F | F | F | F | ✓ | ✓ | ✓ | F | F |
| eval_extractor.py | 328 | ✓ | F | F | F | ✓ | ✓ | F | F | F | F |
| extractor.py | 251 | ✓ | ✓ | F | ✓ | ✓ | ✓ | F | ✓ | ✓ | F |
| fake_llm.py | 59 | – | – | ✓ | ✓ | F | ✓ | ✓ | ✓ | ✓ | – |
| llm.py | 61 | ✓ | ✓ | ✓ | ✓ | – | ✓ | ✓ | ✓ | ✓ | ✓ |
| memory.py | 585 | ✓ | F | F | F | F | ✓ | F | ✓ | F | F |
| menu.py | 168 | ✓ | F | F | F | ✓ | ✓ | F | ✓ | F | F |
| preview.py | 31 | – | ✓ | ✓ | F | F | ✓ | ✓ | ✓ | ✓ | – |
| recommend.py | 159 | ✓ | F | ✓ | ✓ | ✓ | ✓ | ✓ | F | F | F |
| server.py | 209 | ✓ | ✓ | F | ✓ | ✓ | ✓ | ✓ | F | F | F |
| static/index.html | 438 | ✓ | ✓ | F | F | ✓ | F | F | F | F | – |
| test_app.py | 669 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | F |
| test_eval.py | 337 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | F |
| test_memory.py | 482 | – | ✓ | ✓ | F | ✓ | ✓ | ✓ | ✓ | ✓ | F |
| video/MenuMind_ The Spike/index.html | 976 | ✓ | F | F | F | ✓ | F | F | ✓ | F | F |
| video/build.py | 355 | F | F | F | F | ✓ | ✓ | F | F | F | F |
| video/capture.py | 336 | ✓ | F | F | F | ✓ | ✓ | ✓ | ✓ | F | – |
| video/sections.py | 115 | ✓ | ✓ | F | F | – | – | F | F | F | F |
| video/spike/index.html | 998 | ✓ | F | F | F | ✓ | F | F | ✓ | F | F |
| video/spike/trace.js | 1 | ✓ | F | – | ✓ | – | ✓ | ✓ | – | ✓ | F |
| video/spike3d.py | 253 | ✓ | F | F | F | ✓ | F | ✓ | F | F | F |
| video/spike_render.py | 138 | ✓ | ✓ | F | F | ✓ | F | ✓ | F | F | F |
| video/spike_trace.py | 151 | ✓ | F | F | ✓ | F | ✓ | ✓ | – | F | F |
| video/walkthrough/data.js | 1 | F | – | F | ✓ | ✓ | ✓ | F | – | F | – |
| video/walkthrough/index.html | 731 | F | F | F | F | ✓ | F | F | F | F | F |

Categories: 1 Stubs and placeholders; 2 Simplified or heuristic logic; 3 Silent failure; 4 Edge cases; 5 Memory safety; 6 Determinism; 7 Dead code; 8 Magic numbers; 9 API contracts; 10 Test gaps.

Cells: 127 with a finding, 135 clear, 18 not applicable.

## All findings

| ID | Severity | Where | Cat. | Finding | Status |
|---|---|---|---|---|---|
| MM-001 | high | `eval_conversations.py:53` | 2 | 'Unsafe recommendations' checks the app against itself: a missed allergy that leads to an unsafe dish scores 0 unsafe | open |
| MM-002 | high | `memory.py:436` | 2 | A companion's conversation-scoped allergy or diet is dropped when the customer removes or loosens their own restriction on the same key | open |
| MM-003 | high | `menu.py:134` | 3 | Latent: a dish with an empty ingredients list loads as vegan and allergen-free | open |
| MM-004 | high | `static/index.html:352` | 6 | A reply that arrives after 'New conversation' (or sign-out and sign-in) is rendered into the new conversation, with the old conversation's temporary allergies and pending question on its ticket | open |
| MM-005 | high | `agent.py:362` | 2 | while an allergy-removal question waits, the only guard against the model eliciting a 'yes' is a literal '?' check | fixed |
| MM-006 | high | `video/build.py:155` | 2 | regions() reads a blank white loading frame as a full-height order ticket, and S03's spotlight rings the whole right column | fixed |
| MM-007 | high | `video/sections.py:43` | 9 | S05 says the allergy filter is applied 'before the model sees a dish'; the code and the footage show the model sees every dish | fixed |
| MM-008 | high | `video/spike/index.html:709` | 6 | Walled cards (S4, M5, M6) jump from 39% to 97% opacity in one frame at 81.333 s | fixed |
| MM-009 | high | `video/spike/index.html:747` | 2 | The R7 decision slip never appears in a rendered frame, and renders at 14px in the player | fixed |
| MM-010 | high | `video/spike/index.html:847` | 6 | Camera snaps back to the threads view during 'Order up.' and hard-cuts at the receipt (overlapping camera tweens) | fixed |
| MM-011 | high | `video/walkthrough/index.html:266` | 6 | overlapping camera tweens snap the framed screen in a single frame, 10 times in the film | fixed |
| MM-012 | high | `video/walkthrough/index.html:319` | 2 | spotlights use a stale region and stay put while the footage scrolls, so S07 frames the wrong element | fixed |
| MM-013 | high | `video/walkthrough/index.html:586` | 3 | The acceptance card shows six invented PASS rows when the build gets no --acceptance output (as in the current data.js), badges every real line PASS (FAIL lines included), and prints 'link added at publishing' without --repo | fixed |
| MM-014 | medium | `acceptance.py:101` | 10 | 'recommendations still avoid peanut' cannot fail, and 'the allergy stays until confirmed' reads only the saved layer, so 'keeps avoiding peanuts until A answers yes' is never verified | open |
| MM-015 | medium | `agent.py:122` | 10 | documented app-written behaviors have no test: six mutants survive | open |
| MM-016 | medium | `eval_conversations.py:34` | 10 | The conversation scorer (check, summarize) has no tests | open |
| MM-017 | medium | `eval_conversations.py:78` | 3 | A turn where extraction failed counts as fully correct | open |
| MM-018 | medium | `eval_extractor.py:86` | 10 | The diet branches of restriction recall and spurious restrictions, and the latency quantiles, are untested | open |
| MM-019 | medium | `eval_extractor.py:149` | 3 | Invalid model output counts as an exact-claim match and a decision match on noop cases | open |
| MM-020 | medium | `eval_extractor.py:226` | 3 | Classifier failures score as correct 'unclear' answers, and the false-yes headline cannot detect an outage | open |
| MM-021 | medium | `memory.py:324` | 2 | A saved soft scalar (budget, spice, lean) can't be cleared unless the customer repeats its exact value *(plausible)* | open |
| MM-022 | medium | `memory.py:336` | 4 | Liking and disliking the same ingredient both stay saved; the ticket and the reasons contradict each other | open |
| MM-023 | medium | `memory.py:389` | 9 | A durable proposal whose only effect is on the active overlay is always rejected as stale when the customer says yes | open |
| MM-024 | medium | `memory.py:571` | 9 | Two proposals from one message: both questions are returned and shown, but 'yes' answers only the last | open |
| MM-025 | medium | `menu.py:97` | 2 | Liked and disliked ingredients don't match ingredients inside house recipes | open |
| MM-026 | medium | `recommend.py:95` | 2 | The 'typed price' and dish-name checks are easy to bypass | open |
| MM-027 | medium | `static/index.html:337` | 3 | If the conversation can't be started after sign-in, the error goes to the hidden sign-in form and the app shows a blank chat with no conversation | open |
| MM-028 | medium | `static/index.html:390` | 3 | Sign out ignores a failed logout request: it shows the sign-in screen while the session stays valid | open |
| MM-029 | medium | `test_app.py:504` | 10 | No test runs the chat page's JavaScript; the confirmed UI defects (stale reply, hidden errors, failed sign-out) are untested | open |
| MM-030 | medium | `test_app.py:545` | 10 | test_http_trace_keeps_internal_errors_out cannot detect leaks of reply_errors/rejected (raw model output and SDK error text) | open |
| MM-031 | medium | `test_eval.py:15` | 10 | eval_conversations.py, the scorer behind the README's safety headline, has no test | open |
| MM-032 | medium | `test_memory.py:403` | 10 | Confirmation-lifecycle gaps hid three of the defects above | open |
| MM-033 | medium | `video/MenuMind_ The Spike/index.html:264` | 2 | The original's inline TRACE is stale and partly not producible by MenuMind, but is labelled 'recorded output' | open |
| MM-034 | medium | `video/MenuMind_ The Spike/index.html:728` | 4 | Original still has the stretched VOID stamp that the maintained copy fixed | open |
| MM-035 | medium | `video/build.py:338` | 3 | the Whisper read-back gate passes one-word misreads (it passed 'Sauté' for 'satay') and never checks numbers | open |
| MM-036 | medium | `video/capture.py:216` | 3 | Per-beat checks do not cover several things the narration and mark labels say | open |
| MM-037 | medium | `video/capture.py:312` | 4 | The previous good footage is deleted before a new take succeeds, and non-Retake errors abort without a retake | open |
| MM-038 | medium | `video/spike/index.html:932` | 10 | No test covers the film's cue list, seek determinism or trace freshness | open |
| MM-039 | medium | `video/spike_render.py:93` | 6 | 3D frames are cached by count and file name only, so stale frames are reused after the film, textures, scene or engine change | open |
| MM-040 | medium | `video/spike_trace.py:132` | 2 | The REFUSED late 'yes' is staged through a memory call that the app's chat never makes | open |
| MM-041 | medium | `video/walkthrough/data.js:1` | 9 | data.js is stale against sections.py (S08's second mark is missing), and the next build.py run exits | open |
| MM-042 | medium | `video/sections.py:31` | 9 | The S03 'order ticket' mark lands before the app is on screen and takes its box from a blank frame | partly fixed |
| MM-043 | medium | `video/sections.py:105` | 9 | S17 'with faults injected to prove they catch regressions' has no evidence in the repo *(plausible)* | fixed |
| MM-044 | medium | `video/spike/index.html:587` | 2 | Caption 'Every decision goes on the spike.' is false for the code and for the film's own later scenes | fixed |
| MM-045 | medium | `video/spike/index.html:786` | 4 | REFUSED stamp box is squeezed: its border cuts through the text (the VOID fix was not applied here) | fixed |
| MM-046 | medium | `video/spike/index.html:892` | 2 | Receipt rows presented as recorded are literals or tautologies | fixed |
| MM-047 | medium | `video/spike/index.html:895` | 9 | 'Invented dishes served 0' and the poster's 'never makes up a dish' are not backed by any check | fixed |
| MM-048 | medium | `video/spike_render.py:103` | 3 | a failed composite leaves a valid, truncated MenuMind_The_Spike.mp4 that build.py accepts | fixed |
| MM-049 | medium | `video/walkthrough/index.html:6` | 3 | if Google Fonts is unreachable, the walkthrough renders in fallback fonts and still reports ready | fixed |
| MM-050 | medium | `video/walkthrough/index.html:318` | 2 | every mark waits for the first reply to land, so some appear seconds after their words, and S11's only after the narrator stops | fixed |
| MM-051 | low | `acceptance.py:86` | 3 | The restart wait loop crashes on a non-JSON or error response and has no deadline | open |
| MM-052 | low | `accounts.py:91` | 9 | the lockout message and README promise a 15-minute lock, but the window lifts 15 minutes after the oldest of the last five failures | open |
| MM-053 | low | `accounts.py:98` | 5 | each unauthenticated /api/login costs a 16 MiB scrypt and an in-memory entry per attacker-chosen username, with no cap *(plausible)* | open |
| MM-054 | low | `accounts.py:128` | 7 | Accounts.close() has no caller | open |
| MM-055 | low | `agent.py:290` | 3 | the confirmation notice ('Nothing was changed.' / 'The earlier question no longer applies...') is dropped when the same message raises a question | open |
| MM-056 | low | `eval_conversations.py:60` | 5 | Temporary databases are never removed, and the store leaks on an exception | open |
| MM-057 | low | `eval_conversations.py:87` | 9 | The README's 'Turns that ask a question, answered by the app alone: 12 of 12' is computed by no code | open |
| MM-058 | low | `eval_extractor.py:139` | 7 | The 'never crash' SpecError branch is unreachable from main() and erases the offending claims when it does run | open |
| MM-059 | low | `eval_extractor.py:281` | 8 | The report's caveat hard-codes '32 phrasings' | open |
| MM-060 | low | `eval_extractor.py:313` | 9 | --repeats does not repeat the yes/no classifier | open |
| MM-061 | low | `extractor.py:151` | 3 | parse_claims fills in a missing temporal, modality or evidence instead of rejecting the claim | open |
| MM-062 | low | `extractor.py:189` | 7 | dead surface: extract(retries=...) is never passed, and Extraction.raw is never read | open |
| MM-063 | low | `fake_llm.py:56` | 5 | FakeLLM.briefings grows without bound in the long-running preview server | open |
| MM-064 | low | `memory.py:148` | 3 | budget_max validation accepts NaN and inf at the memory boundary | open |
| MM-065 | low | `memory.py:266` | 9 | A conversation-scoped confirmation question promises a permanent change | open |
| MM-066 | low | `memory.py:267` | 7 | Unreachable branches and unused parameters | open |
| MM-067 | low | `memory.py:303` | 9 | R1 treats the unset default diet as a stored restriction | open |
| MM-068 | low | `memory.py:336` | 5 | The event log grows with no-op restatements, and each claim re-folds the whole history | open |
| MM-069 | low | `memory.py:518` | 9 | The event log stores the JSON text 'null' where SPEC §4 says SQL NULL | open |
| MM-070 | low | `menu.json:14` | 2 | The S3 description names a component missing from its ingredients | open |
| MM-071 | low | `menu.py:35` | 7 | INGREDIENTS['peanut_sauce'] can never be read | open |
| MM-072 | low | `menu.py:125` | 4 | The loader accepts NaN or inf prices and ids that the token grammar can't express | open |
| MM-073 | low | `preview.py:29` | 4 | Default DB_PATH is relative to the current directory | open |
| MM-074 | low | `recommend.py:56` | 8 | The ranking weights are unexplained | open |
| MM-075 | low | `recommend.py:72` | 2 | diet_lean is scored as a floor: an omnivore lean is a no-op, and a pescatarian lean boosts vegan dishes as much as fish | open |
| MM-076 | low | `recommend.py:86` | 9 | SPEC §2 says the budget filters; the code allows over-budget picks with a warning | open |
| MM-077 | low | `server.py:39` | 8 | the MAX_BODY_BYTES rationale ('~2 KB') is wrong for non-ASCII text, so a 2,000-character message can get a 413 | open |
| MM-078 | low | `server.py:85` | 3 | a model/provider mismatch or a revoked key reaches customers as 'try again or rephrase' while /api/health says chat is enabled *(plausible)* | open |
| MM-079 | low | `server.py:143` | 9 | rejected sign-ups (invalid PIN, taken username) use up the global registration budget | open |
| MM-080 | low | `server.py:178` | 10 | server and account behaviors with no test | open |
| MM-081 | low | `server.py:191` | 9 | chat rate limits are charged for requests that never reach the model | open |
| MM-082 | low | `static/index.html:73` | 8 | `.main { height: calc(100% - 64px) }` assumes a 64px header; the real header is 67px, or 122px when it wraps | open |
| MM-083 | low | `static/index.html:84` | 7 | Dead CSS rule .msg.note | open |
| MM-084 | low | `static/index.html:248` | 9 | api() treats `detail` as a string; FastAPI's 422 detail is a list, so the user sees '[object Object]' | open |
| MM-085 | low | `static/index.html:361` | 9 | The trace says the model's reply 'failed checks' when the model was in fact unreachable | open |
| MM-086 | low | `static/index.html:369` | 3 | Recovery from an ended conversation swallows its own failure, then says 'I started one' | open |
| MM-087 | low | `static/index.html:384` | 4 | Enter that commits an IME composition (Japanese, Chinese, Korean input) sends the half-typed message *(plausible)* | open |
| MM-088 | low | `static/index.html:394` | 4 | The ticket toggle's label and aria-expanded are wrong after the window narrows from desktop to phone width | open |
| MM-089 | low | `static/index.html:401` | 3 | A failed menu request is silent: the dialog never opens and the rejection is unhandled | open |
| MM-090 | low | `test_app.py:265` | 10 | '# still enforced' asserts the saved layer, not what is enforced now | open |
| MM-091 | low | `test_app.py:667` | 10 | The chat message limits (empty or whitespace, over 2,000 characters) have no test | open |
| MM-092 | low | `test_eval.py:328` | 10 | Prompt-hygiene test skips the yes/no classifier prompt, which contains confirmation eval case c18's reply verbatim | open |
| MM-093 | low | `test_memory.py:340` | 10 | Some rule outcomes run only inside the P6 sweep, which asserts only that nothing loosens | open |
| MM-094 | low | `video/capture.py:292` | 4 | serve() trusts any process already listening on port 8790 *(plausible)* | open |
| MM-095 | low | `video/capture.py:329` | 9 | Capture provenance is incomplete: model, attempt number, and the scripted-server menu refilm are not recorded | open |
| MM-096 | low | `video/sections.py:105` | 10 | Narrated counts are hard-coded and never checked against their sources | open |
| MM-097 | low | `video/spike/index.html:394` | 7 | Dead code | open |
| MM-098 | low | `video/spike/index.html:494` | 3 | decisionFor pairs events with decisions by index and silently falls back to decisions[0] | open |
| MM-099 | low | `video/spike/index.html:646` | 2 | Inter-title 'New server process' is simulated in the same Python process | open |
| MM-100 | low | `video/spike/index.html:726` | 2 | Held ticket prints 'durable, proposed' where real tickets print the recorded source | open |
| MM-101 | low | `video/spike/index.html:974` | 9 | texture() permanently hides the page, so any later frame() on that page reports every paper at opacity 0 | open |
| MM-102 | low | `video/spike/index.html:1001` | 3 | Swallowed audio errors and an unhandled build promise *(plausible)* | open |
| MM-103 | low | `video/spike3d.py:149` | 4 | stamp-texture mixing assumes at most 2 stamps, and stale texture files are picked up | open |
| MM-104 | low | `video/spike3d.py:341` | 8 | the 3D stacking re-derives the film page's layout constants, and the camera window weight is duplicated | open |
| MM-105 | low | `video/spike_trace.py:114` | 3 | The late-yes guard passes vacuously if no question was held; the temp DB directory leaks | open |
| MM-106 | low | `video/walkthrough/index.html:174` | 7 | dead identifiers and data.js fields nothing reads | open |
| MM-107 | low | `video/walkthrough/index.html:194` | 2 | narration cues assume an even reading pace by character *(plausible)* | open |
| MM-108 | low | `video/walkthrough/index.html:477` | 8 | facts and positions are hard-coded instead of read from data | open |
| MM-109 | low | `video/build.py:15` | 9 | docstrings describe outputs and engines the code does not produce | fixed |
| MM-110 | low | `video/build.py:224` | 4 | layout() gives a non-last card with no share zero seconds | fixed |
| MM-111 | low | `video/build.py:301` | 4 | stamp() prints '0:60.0' at a minute boundary | fixed |
| MM-112 | low | `video/build.py:354` | 3 | the 'OVER THE 6:00 LIMIT' warning measures the walkthrough without the 152.8 s Spike *(plausible)* | by design |
| MM-113 | low | `video/sections.py:3` | 7 | The docstring says build.py writes voice cues from here; it no longer does | fixed |
| MM-114 | low | `video/sections.py:107` | 9 | 'all forty-two scripted conversations' is 14 scripts run 3 times *(plausible)* | fixed |
| MM-115 | low | `video/spike_render.py:94` | 3 | Blender exits 0 when spike3d.py raises a Python exception *(plausible)* | fixed |
| MM-116 | low | `video/walkthrough/index.html:723` | 6 | the timeline is built before fonts load, so wire geometry differs between cold and warm renders (by 1.5 px / 3 px) | fixed |

## Limits

- Static and scripted review only. No load test was run (the accounts.py scrypt memory figure is reasoning), Blender was not run by the auditors, and no live model was called, so how often a real model triggers the model-dependent findings (for example agent.py:362) is unmeasured.
- Reproduction scripts live in the session's scratchpad, not in the repository.
- Severity: high = can put a wrong result in front of a customer or a reviewer (an allergy dropped, a false claim in the video, a broken safety metric); medium = wrong behaviour or evidence with a narrower path; low = robustness, clarity, hygiene.


## Independent post-fix verification — 2026-10-14

This follow-up preserves the preceding report and its provenance. It independently verifies **MM-005 as fixed** and adds **MM-117, CRITICAL / CONFIRMED**, which remains reproducible after that fix. The preceding multi-auditor methods, frame probes and other fixed statuses are inherited report content; this follow-up does not certify that it performed them.

**MM-005:** `agent.py:289` now says `if questions:` and sets `message, picks = "", []`, bypassing the reply model whenever the app asks or repeats a question. `test_app.py:273` exercises the original offer-without-a-question-mark failure and asserts that the unclear-answer turn makes no further responder call. The full current suite passed: **196 passed in 1.74s**.

### MM-117 | Category 6 | CRITICAL | CONFIRMED | effort M

**Stale reply can recommend an allergen saved while the model call was pending.**

Exact current code in `agent.py:284`:

```python
        with self._lock:
            profile, pending = conv.profile(), conv.pending
        recs = recommend(self.menu, profile.active)
```

The snapshot is reused in `agent.py:295`:

```python
            message, picks = self._reply(state, text, profile, recs, changes, notice, trace)
        reply, mentioned = render(message, picks, self.menu, recs, profile.active["budget_max"])
```

And returned at `agent.py:306`:

```python
        return TurnResult(reply, mentioned, changes, question, profile_view(profile), trace)
```

**Minimal trigger:** one Agent, two conversations for the same user. Pause the first reply-model call after it receives its briefing. Save an asserted peanut allergy through the second conversation and wait for that turn to finish. Release the first model call with `{"message":"Try this.","recommend":["S4"]}`. The in-memory threaded reproduction returned:

```json
{"current_allergens":["peanut"],"returned":[{"id":"S4","name":"Chicken Satay","price":9.0,"kind":"rec","warnings":[]}]}
```

**Falsification:** the memory operations are locked, but the reply call is outside that lock. `_reply` checks the old `recs`; `render` also receives that old set. The later lock only updates history. The HTTP caller does not revalidate. Two tabs/conversations bypass the single-page busy flag. No multi-process deployment is needed. This contradicts the recommendation guarantee at `README.md:10` and the one-instance concurrency assurance at `README.md:302`. The database remains correct; the response is stale and unsafe.

**Proposed fix:** bind each response to an event-log version, recheck after the model call, and discard/recompute stale recommendations, prose and profile. Make the final validation/publication decision atomic relative to memory writes. Add the controlled two-conversation regression above.

**Backlog update:** MM-117 takes precedence over the earlier list. Combined report now contains **117 findings: 1 critical, 13 high, 37 medium, 66 low; 91 open, 24 fixed, 1 partly fixed, 1 by design**. These totals retain the preceding report's classifications; only MM-005 and the new finding are certified by this focused post-fix verification. The agent/category-6 cell now includes MM-117.

**Other checks:** MM-004's stale response was reproduced by executing the actual page script with controlled fetch promises: Bob's page displayed Alice's reply and allergy. MM-023's active-only removal still raised `StaleConfirmation`; MM-028 still showed the sign-in screen after an injected logout network failure. The JavaScript reproductions used a minimal DOM in Node, not a full browser. Ruff F/B returned eight diagnostics, none establishing another defect after inspecting callers (FastAPI dependency defaults, construction-matched zips, and a synchronously evaluated Pillow lambda). Vulture at 80% confidence returned no findings. No source was modified, no live model was called, and no deployment or full video render was performed in this follow-up.

## MM-117 fixed — 2026-10-14

Confirmed by the lead with the same interleaving (in memory, two conversations for one customer): the first reply returned Chicken Satay with no warning while peanut was saved.

**Fix (agent.py):** once the reply model returns, the turn re-reads the profile under the same lock as memory writes. If what is enforced has changed, the model's picks are re-checked against the current recommendations, and if they fail, the reply is built from verified current data (the existing fallback). The reply, its dishes and the returned profile all answer to that state; a change after it is ordered after this reply.

**Proof:** `test_a_reply_honours_an_allergy_saved_while_it_was_written` reproduces the interleaving; it fails on the previous agent.py and passes now (197 passed). The repro now returns peanut-free dishes with `fallback: true`. The Spike's recorded test count, the README and the walkthrough narration now say 197.

Totals: 117 findings (1 critical, 13 high, 37 medium, 66 low); 90 open, 25 fixed, 1 partly fixed, 1 by design.


## Submission-handoff verification — 2026-10-14

This is a focused follow-up, not a new exhaustive audit. **MM-117 is independently verified fixed for its reported interleaving:** the original two-conversation reproduction now returns S1/D3/S2 with `fallback: true`, avoiding peanut after it is saved. **197 tests passed in 1.97s.** Earlier statements that MM-117 remained open describe the earlier source revision.

### MM-118 | Category 9 | HIGH | CONFIRMED | effort S

**The submitted evaluation reports do not support the README's final-run figures.**

- `README.md:234`: `| Conversations, turns fully correct | 42/42, 75/75 |`.
- `evidence/eval_report_conversations_gpt-5.4-mini.json:5`: `"cases_correct": 41,`; line 7: `"turns_correct": 74,`; line 11: `"reply_rejected_at_least_once": 1,`. Recomputing the result from its rows gives 41/42. The failed row is req4-spice-update, repeat 2: `durable spice_tolerance is None, expected 'none'`.
- `README.md:237-238` claims extractor accuracies 0.938/0.781 and classifier accuracy 18/18. The tracked extractor report contains 0.969/0.812 and classifier accuracy 0.944 (17/18).

**Falsification:** the README explicitly distinguishes run 4 from the final run. The root `eval_report_conversations_gpt-5.4-mini.json` does contain 42/42 and 75/75; the root extractor report also matches the README's final figures. These root reports are ignored by Git. Both files in `evidence/` are tracked and contain different results. Thus the final run is not alleged to be invented; its evidence is missing from the proposed submission. The pasted five-file staging command does not repair this mismatch.

**Minimal trigger:** clone the current GitHub revision and compare the README figures with the two reports under `evidence/`. **Proposed fix:** publish and clearly label the final reports, preserve older runs under distinct names if retained, and bind each README number to the intended report. Source/report SHA256 values are recorded in the JSON finding.

**Other verified handoff facts:** GitHub HEAD is `05876ef1896faacbc9b30d936d22d0481d0628a4`; run.py is still untracked and four named documentation/video-page files are modified. Health returned `ok: true` and `chat_enabled: true`; Railway listed menumind-volume mounted at `/data`, Ready. These do not establish live model-call success or continuity throughout the review period. Menu counts match: 19 dishes, 3 categories, 12 prices, 4 diets, 4 spice levels, 18 dishes sharing ingredients. MP4 probe: 409.6 seconds, 69,695,018 bytes. The assignment recommends 8–12 minutes; 6:49.6 is shorter, but the recommendation is not a mandatory minimum.

**Remaining isolation defect:** MM-004 still reproduces in the actual page script with a minimal DOM and controlled fetch promises: a delayed Alice reply is rendered into Bob's page. `static/index.html:363-364` executes `add("agent", html); renderTicket(r.profile, r.question);` without checking the current account/conversation against the request. This is a Node reproduction, not a fresh full-browser test.

**Limits:** a common-pattern secret scan of the 46 intended working-tree files found no matches; this is not a comprehensive secret/history audit. Drive permissions, actual live chat, a fresh-machine run.py installation, and the complete final video were not verified. No commit, push, upload, source edit, or evidence replacement was performed.

## MM-118 fixed — 2026-10-14

Confirmed: `evidence/` held run 4's reports (41/42) while the README quoted the final run (42/42); the final reports existed only as gitignored copies at the repository root. `evidence/` now holds the final run's reports, and run 4's are kept as `evidence/run4-*.json`. Every figure in the README's evidence table was recomputed from `evidence/`. All match except "turns that ask a question, answered by the app alone": the final run shows **9 of 12**, not 12 of 12. The 3 others are the re-asks after "not sure", where the model's offer of a dish sat above the question (MM-005, fixed after the run). The README now says 9 of 12 (MM-057, partly fixed), names the report files, and says the app changed after the run, which was not repeated.

Totals: 118 findings; 89 open, 26 fixed, 2 partly fixed, 1 by design.

## MM-004 fixed — 2026-10-14

The chat page now records the conversation and the signed-in customer when a message is sent, and drops a reply (or an error) that comes back after either changed. Checked in a real browser (Playwright, the app with its scripted model): with Alice's reply held in flight while she signed out and Bob signed up, her reply and allergy appeared on Bob's page before the fix and do not after; Bob's own replies still appear. Removed from the README's known issues.

Totals: 118 findings; 88 open, 27 fixed, 2 partly fixed, 1 by design.
