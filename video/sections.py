"""The walkthrough, section by section: what is said and what is shown.

The single source for the narration and the storyboard (build.py writes those from here),
so the script, the voice and the picture cannot drift apart. It
follows "The Spike" (spike/index.html), which plays first as the cold open. The parts follow
the brief's video checklist: baseline demonstration, the chosen challenge, technical deep
dive, scope and trade-offs, results and validation.

A visual is ("capture", beat) for real footage filmed by capture.py, or ("card", name) for
a scene in walkthrough/index.html. With several visuals, all but the last get a share of the
section's narration (0.4 = the first 40% of it) and the last fills the rest, so the picture
follows any reading pace.

marks point at the footage when the narration gets there: (words in the text, region, label).
A region is found in the captured frames by build.py: reply, reply_first (its first line),
reply_trace ("How I read that"), ticket, ticket_saved, ticket_now, ticket_waiting, note.
"""

SECTIONS = [
    # ------------------------------------------------------------------ the challenge
    dict(id="S01", part="Opening", title="From the film to the app", visual=[("card", "title")],
         text="That film was scripted, but every ticket, rule and price in it came from "
              "MenuMind's code. Here is the real app, on a live model."),
    dict(id="S02", part="Challenge", title="The brief, and my challenge", visual=[("card", "brief")],
         text="The brief: recommend from a real menu, remember each customer, and adapt when they "
              "change their mind. For my challenge I chose memory over voice, scale or "
              "integrations, because in food, memory mistakes are the costly ones: an allergy "
              "forgotten, a one-night wish kept forever, an allergy dropped on a misread sentence."),
    # ------------------------------------------------------------------ the baseline, live
    dict(id="S03", part="Live demo", title="A customer signs up", visual=[("capture", "signup")],
         marks=[("order ticket", "ticket", "The order ticket: what MenuMind remembers")],
         text="Maya signs up. On the right is her order ticket: what MenuMind remembers, every "
              "visit, and just this conversation."),
    dict(id="S04", part="Live demo", title="Talking normally", visual=[("capture", "spicy")],
         marks=[("first line", "reply_first", "Written by the app, not the model"),
                ("Dishes come from the menu", "reply", "Names and prices come from the menu"),
                ("How I read that", "reply_trace", "Each claim, and the rule that decided it")],
         text="She just talks. The app saves what she said, and its first line says so. Dishes "
              "come from the menu, and 'How I read that' shows each claim and the rule behind it."),
    dict(id="S05", part="Live demo", title="An allergy", visual=[("capture", "allergy")],
         marks=[("hard filter", "ticket_saved", "Saved for every visit"),
                ("satay and pad thai", "reply", "Flagged by the app, never recommended")],
         text="An allergy is a hard filter in code: the model may only recommend dishes that pass "
              "it, so satay and pad thai are never suggested."),
    dict(id="S06", part="Live demo", title="A question saves nothing", visual=[("capture", "question")],
         marks=[("saves nothing", "ticket_saved", "Nothing new saved"),
                ("the warning", "reply", "The warning is the app's, not the model's")],
         text="A question saves nothing, and the warning beside the pad thai is the app's."),
    dict(id="S07", part="Live demo", title="Not sure", visual=[("capture", "hedged")],
         marks=[("avoided now", "ticket_now", "Avoided now, for this conversation"),
                ("just tonight", "reply_first", "Kept for tonight only")],
         text="Unsure about shellfish? It's avoided now, and the app asks before saving it. She "
              "says just tonight."),
    dict(id="S08", part="Live demo", title="Just tonight", visual=[("capture", "tonight")],
         marks=[("means this conversation", "reply_first", "Just this conversation"),
                ("beside the saved value", "ticket_now", "Each beside the saved value it overrides")],
         text="'Tonight' means this conversation: no spice, and fifteen dollars, each shown "
              "beside the saved value."),
    dict(id="S09", part="Live demo", title="Coming back", visual=[("capture", "return")],
         marks=[("saved preferences are back", "ticket_saved", "Saved: still here"),
                ("tonight's exceptions are gone", "ticket_now", "Tonight's changes: gone")],
         text="Maya signs out and back in, to a new conversation: her saved preferences are back, "
              "and tonight's exceptions are gone."),
    dict(id="S10", part="Live demo", title="Changing a preference",
         visual=[("capture", "remove_ask", 0.65), ("capture", "remove_yes")],
         marks=[("the app's own words", "reply", "The app's own question"),
                ("peanuts stay out", "ticket_waiting", "Waiting for her answer: peanut still avoided"),
                ("still follow what she saved", "reply", "Recommended from what she saved")],
         text="Removing an allergy needs her yes, asked in the app's own words; until then, peanuts "
              "stay out. She says yes, and her picks still follow what she saved."),
    dict(id="S11", part="Live demo", title="Another customer", visual=[("capture", "sam")],
         marks=[("sees none of Maya's memory", "ticket", "Sam's ticket: empty")],
         text="A second customer, Sam, signs in elsewhere and sees none of Maya's memory; the "
              "server won't open her conversations to him."),
    # ------------------------------------------------------------------ technical deep dive
    dict(id="S12", part="Deep dive", title="How it works", visual=[("card", "architecture")],
         text="The design in one line: the model describes, code decides. The model turns "
              "messages into claims and writes the reply. Code decides what is saved, keeps every "
              "change in an event log, filters the menu, and lets the model pick only verified "
              "dishes."),
    dict(id="S13", part="Deep dive", title="One conflict, end to end", visual=[("card", "conflict")],
         text="The hard case: Maya says she's no longer allergic. The model reads it, but can't "
              "write memory. Rule R10 asks first, so peanut stays enforced. The question is bound "
              "to this conversation and her log, so a stale yes is refused. Her yes appends one "
              "confirmed event; nothing is overwritten."),
    dict(id="S14", part="Deep dive", title="Grounded replies", visual=[("card", "grounding")],
         text="Replies are grounded the same way: the model returns dish IDs, code checks them "
              "against her restrictions and writes names and prices from the menu. Twice wrong, "
              "and the app answers from verified data; unreadable, and nothing is saved."),
    dict(id="S15", part="Deep dive", title="Menu data",
         visual=[("card", "recipes", 0.72), ("capture", "menu_note")],
         marks=[("Cross-contact", "note", "Shown to every customer, in the menu")],
         text="And the menu only loads if every dish's allergens match its ingredients, recipes "
              "inside recipes included. Cross-contact isn't covered, and the app says so."),
    # ------------------------------------------------------------------ scope and trade-offs
    dict(id="S16", part="Trade-offs", title="Scope and trade-offs", visual=[("card", "tradeoffs")],
         text="What I left out was deliberate: learning preferences from behaviour, a clock for "
              "'tonight', and running on several servers. I rejected letting the model write "
              "memory directly, because nothing it saved could be audited. With more time: one "
              "transaction per message, concurrency control for more servers, and verified "
              "supplier recipes."),
    # ------------------------------------------------------------------ results and validation
    dict(id="S17", part="Validation", title="Tests and evidence",
         visual=[("card", "testing", 0.45), ("card", "evidence")],
         text="Validation: a hundred and ninety-six tests without a model, and an acceptance test "
              "over HTTP across a restart. On the real model, fourteen scripted conversations, run "
              "three times each, all passed, with no unsafe recommendation in seventy-five turns. "
              "A limited set: counts, not rates."),
    dict(id="S18", part="Validation", title="What the live runs caught", visual=[("card", "lessons")],
         text="The live runs earned their cost: they caught 'nuts' saved as tree nuts only, and a "
              "rule of mine that dropped a companion's allergy. Each was fixed, and re-run."),
    dict(id="S19", part="Close", title="Thank you", visual=[("card", "close")],
         text="The guarantee is narrow, and strong: the model can describe; code decides. Thank "
              "you for watching."),
]
