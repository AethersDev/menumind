# Voice cues

Record one file per section, named by its ID (`S01.wav`, `S02.m4a`, ...), into `video/voice/`.
Read the text exactly as written; the picture is timed to your voice, so pace is yours.

- Quiet room, soft furnishings; phone or Mac 20-30 cm from your mouth, the same place every take.
- Half a second of silence before and after each section. No music, no effects.
- Calm and plain, like explaining to a colleague. Breathe at the commas; don't rush the numbers.
- A take with a stumble: record the section again over the same file. One file per ID.
- Target length is the scratch reading at 160 words a minute; within about 20% either way is fine.

## S01 · Title

File `video/voice/S01.wav` · target ~11 s

> This is MenuMind: a restaurant assistant that learns what each customer likes and what they can't eat, remembers it between visits, and only ever recommends real dishes from the menu.

## S02 · The brief

File `video/voice/S02.wav` · target ~23 s

> The brief asked for six things: natural conversation, menu recommendations, long-term memory, updates to preferences, returning customers kept separate, and a deployed interface. For the advanced challenge I chose memory: preferences that change over time, where some changes are permanent, some last one evening, and some are dangerous to get wrong.

## S03 · A customer signs up

File `video/voice/S03.wav` · target ~14 s

> A customer, Maya, creates an account with a username and a PIN. On the right is her order ticket. It shows everything MenuMind remembers, in two parts: every visit, and just this conversation. For now, it's empty.

## S04 · Talking normally

File `video/voice/S04.wav` · target ~22 s

> Maya just talks: she loves spicy food, but hates mushrooms. The first line of the reply is written by the app, not by the model, and says exactly what was saved. The recommended dishes, with their names and prices, come from the menu. Under 'How I read that', you can see each claim the model extracted, and the rule that decided what to do with it.

## S05 · An allergy

File `video/voice/S05.wav` · target ~15 s

> Now an allergy, and a request for a main course. Allergies and diets are hard restrictions. They're applied in code before the model sees a single dish, so satay and pad thai can't be suggested. The model can only choose among dishes that have already passed.

## S06 · A question saves nothing

File `video/voice/S06.wav` · target ~10 s

> A question isn't a preference. Asking whether the pad thai has peanuts saves nothing. And when the reply mentions a dish Maya has to avoid, the warning next to it is added by the app.

## S06b · Not sure

File `video/voice/S06b.wav` · target ~16 s

> Uncertainty is handled too. When Maya says she thinks she might be allergic to shellfish, MenuMind avoids shellfish straight away, for this conversation, and asks whether to save it for future visits. She says no, just for tonight, so it stays temporary.

## S07 · Just tonight

File `video/voice/S07.wav` · target ~13 s

> 'Tonight' is not 'always'. Maya asks for nothing spicy tonight, and a budget of fifteen dollars. Both apply only to this conversation, and the ticket shows each one next to the saved value it overrides.

## S08 · Coming back

File `video/voice/S08.wav` · target ~12 s

> Maya signs out, and later signs back in. In a new conversation, her saved preferences are all here, while tonight's exceptions, the spice, the budget and the shellfish, are gone.

## S09 · Removing an allergy

File `video/voice/S09.wav` · target ~17 s

> Removing an allergy is the riskiest change, so MenuMind never does it on a single sentence. It asks first, and until Maya answers yes, peanuts stay excluded. Once she confirms, the allergy is removed, and nothing else she told MenuMind changes.

## S10 · Another customer

File `video/voice/S10.wav` · target ~9 s

> A second customer, Sam, signs up in another browser. He sees nothing of Maya's memory, and the server won't let him open her conversations.

## S11 · How it works

File `video/voice/S11.wav` · target ~28 s

> Here's how it works. The language model has three narrow jobs: reading a message into structured claims, reading a yes or a no, and writing the reply. It never decides what is stored or what is recommended. Code does. Fixed rules turn each claim into a change. Changes go into an append-only event log, and the profile is computed from that log. Recommendations are filtered and ranked in code, and the reply model can only pick from them, by ID.

## S12a · One conflict: the claim

File `video/voice/S12a.wav` · target ~16 s

> Let's follow one conflict from end to end. Maya had saved a peanut allergy, then said she no longer has it. This is what the extractor produced: an allergen, negated, current, stated plainly. It describes what was said. It doesn't decide anything.

## S12b · One conflict: the rule

File `video/voice/S12b.wav` · target ~20 s

> The decision is made by code, using eleven ordered rules. A lasting change is compared with both the saved profile and the one in force right now. Loosening an allergy always goes through rule R10: ask first, however certain the sentence sounds. The costs are lopsided: one extra question, against serving an allergen.

## S12c · One conflict: the question

File `video/voice/S12c.wav` · target ~13 s

> The question is held as a proposal tied to Maya, to this conversation, and to the last event number. If anything changes her peanut allergy from another device before she answers, her yes is rejected as out of date.

## S12d · One conflict: the log

File `video/voice/S12d.wav` · target ~11 s

> When she says yes, one event is appended and marked as confirmed. Nothing is overwritten: the log keeps the whole history, and the profile is computed from it the same way every time.

## S13 · Grounded replies

File `video/voice/S13.wav` · target ~26 s

> The reply model answers in a fixed format: a message, and the IDs of the dishes it recommends. Code rejects any dish outside the eligible set and any dish name or price the model types itself, then writes names and prices from the menu. If the model breaks a rule twice, the app answers from verified data instead. And if a message can't be read at all, MenuMind fails closed: nothing is saved, and nothing is recommended.

## S14 · Menu data

File `video/voice/S14.wav` · target ~23 s

> Recommendations are only as safe as the menu data, so the menu won't load unless every dish's allergens and diet match its ingredients. Prepared ingredients have declared house recipes, and recipes can contain other recipes. When one recipe contradicted a dish's label, the label was corrected, not the recipe. And the app tells customers that cross-contact in the kitchen isn't covered.

## S14b · Tests and acceptance

File `video/voice/S14b.wav` · target ~33 s

> Underneath, a hundred and ninety-five tests run without any model. They cover every decision rule, including a sweep of a hundred and forty claims from three starting states, which checks that no allergy or diet is ever loosened without a yes. Code fixes were checked twice: by a test that failed before the fix, and by breaking the fix on purpose to see a test catch it. And an acceptance test drives the running app over HTTP: memory across conversations and a server restart, confirmation before a removal, and customers kept apart.

## S15 · Evidence with a real model

File `video/voice/S15.wav` · target ~32 s

> The unit tests use a scripted model, so to show that this works with a real one, I built two live evaluations: thirty-two phrasings for the extractor, and fourteen scripted conversations, each run three times. I read every run in full and fixed what it found, over four rounds. In the final run, there were no unsafe recommendations in seventy-five turns, every allergy and diet was caught, and nothing was loosened without a yes. The misses that remain are documented, and none of them involves an allergy.

## S15b · What the live runs caught

File `video/voice/S15b.wav` · target ~24 s

> The live runs were worth it. In the first, 'I'm allergic to nuts' saved only tree nuts, every time; the extraction rules now say that nuts, with no nut named, means peanuts too. In the third, a rule I had just added made the extractor drop a companion's allergy. That run caught it, and the fourth confirmed the fix. Reading every run in full, replies included, is what found both.

## S16 · Limits

File `video/voice/S16.wav` · target ~18 s

> The limits are written down plainly. MenuMind runs as a single server instance. Its allergen data covers declared recipes, not cross-contact. And the wording of replies is checked, but not proven. What it does guarantee is narrower, and stronger: the model can describe, but code decides.

## S17 · Thank you

File `video/voice/S17.wav` · target ~1 s

> Thank you for watching.
