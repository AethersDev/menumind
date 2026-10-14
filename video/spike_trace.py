"""Record the data shown in "The Spike" by running MenuMind's code on the film's script.

    .venv/bin/python video/spike_trace.py        writes video/spike/trace.js

The customer's lines, the claims the extractor returns and the reply model's drafts are
scripted (fake_llm.py stands in for the model). Everything else is what agent.py, memory.py
and recommend.py produce: tickets, rules, rankings, walls, the rendered reply and the refusal
of a late yes. The second visit runs in a new Agent on the reopened store, as after a restart.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import Agent  # noqa: E402
from fake_llm import FakeLLM, claim  # noqa: E402
from memory import EventStore, Scope, StaleConfirmation, fold  # noqa: E402
from menu import load_menu  # noqa: E402
from recommend import recommend  # noqa: E402

USER = "alice"
VISITS = [[  # (customer line, claims the extractor returns, model outputs in order)
    ("I love spicy food, but I hate mushrooms.",
     [claim("spice_tolerance", value="hot", evidence="love spicy food"),
      claim("disliked_ingredient", subject="mushroom", evidence="hate mushrooms")],
     [{"message": "Plenty here has real heat, and none of it needs mushrooms.", "recommend": []}]),
    ("Oh, and I'm allergic to peanuts.",
     [claim("allergen", subject="peanut", evidence="allergic to peanuts")],
     [{"message": "Thanks for telling me. What are you in the mood for?", "recommend": []}]),
    ("Nothing spicy tonight, though. Long day.",
     [claim("spice_tolerance", value="none", temporal="conversation", evidence="Nothing spicy tonight")],
     [{"message": "Something gentle tonight, then.", "recommend": []}]),
], [
    ("I'm no longer allergic to peanuts.",
     [claim("allergen", "negate", subject="peanut", evidence="no longer allergic to peanuts")],
     []),  # the app asks this turn's question itself; the model is not called
    ("Wait, no. I'm definitely still allergic.",
     [claim("allergen", subject="peanut", evidence="definitely still allergic")],
     [{"message": "Understood. Peanuts stay off your menu.", "recommend": []}]),
    ("So what should I get?", [],
     [{"message": "Try the lobster roll, only $30. Or the satay, it's a favorite.", "recommend": ["S4"]},
      {"message": "Hot, no mushrooms, no peanuts: two that fit.", "recommend": ["M3", "M2"]}]),
]]


def event(e) -> dict:
    return {"seq": e.sequence_number, "session": e.session_id, "attribute": e.attribute,
            "subject": e.subject, "value": e.value, "op": e.operation.value, "scope": e.scope.value,
            "source": e.source.value, "evidence": e.evidence, "event_id": e.event_id,
            "created_at": e.created_at}


def recs_dict(r) -> dict:
    row = lambda c: {"id": c.item.id, "score": c.score, "reasons": list(c.reasons)}  # noqa: E731
    return {"ranked": [row(c) for c in r.candidates], "over_budget": [row(c) for c in r.over_budget],
            "excluded": r.excluded}


def reading(events, session: str) -> list:
    """Why each event does or doesn't count on this visit, checked against fold() below."""
    out = []
    for e in events:
        later = [x for x in events if x.key == e.key and x.sequence_number > e.sequence_number
                 and (x.scope is Scope.DURABLE or x.session_id == session)]
        counted = (e.scope is Scope.DURABLE or e.session_id == session) and not later
        why = ("Latest word on this" if counted else "Belongs to another conversation"
               if e.scope is Scope.CONVERSATION and e.session_id != session
               else f"Replaced by #{later[-1].sequence_number:04d}")
        out.append({"seq": e.sequence_number, "counted": counted, "why": why})
    kept = [e for e, r in zip(events, out) if r["counted"]]
    if fold(kept, user_id=USER, session_id=session).active != fold(events, user_id=USER, session_id=session).active:
        sys.exit("the replay's reading disagrees with fold()")
    return out


def main() -> None:
    claims = {line: found for visit in VISITS for line, found, _ in visit}
    outputs = {line: [json.dumps(o) for o in outs] for visit in VISITS for line, _, outs in visit}
    drafts: dict = {line: [] for line in outputs}

    def responder(brief, _messages):
        line = brief["customer_message"]
        drafts[line].append(outputs[line][len(drafts[line])])
        return drafts[line][-1]

    menu = load_menu(ROOT / "menu.json")
    db = Path(tempfile.mkdtemp()) / "spike.db"
    trace = {"menu": [i.public() for i in menu.items], "restaurant": menu.restaurant, "user": USER,
             "sessions": {}, "turns": []}
    stale = None
    for v, visit in enumerate(VISITS, start=1):
        store = EventStore(str(db))  # a new process each visit: only the event log carries over
        agent = Agent(store, menu, FakeLLM(claims=claims, responder=responder))
        cid = agent.start_conversation(USER)
        conv = agent._convs[(USER, cid)].conv  # the film shows the held proposal's details
        trace["sessions"][f"visit{v}"] = cid
        if v == 1:
            trace["initial_recs"] = recs_dict(recommend(menu, agent.profile(USER).active))
        else:
            events = store.events_for(USER)
            trace["visit2_fold"] = {"events": [event(e) for e in events], "reading": reading(events, cid),
                                    "recs": recs_dict(recommend(menu, agent.profile(USER, cid).active))}
        for line, _, _ in visit:
            before = store.last_sequence(USER)
            had = conv.pending
            r = agent.turn(USER, cid, line)
            p = conv.pending
            stale = p.proposal_id if p else stale
            trace["turns"].append({
                "session": cid, "text": line,
                "claims": [{**c, "span": [line.index(c["evidence"]), line.index(c["evidence"]) + len(c["evidence"])]}
                           for c in r.trace["claims"]],
                "decisions": r.trace["decisions"],
                "new_events": [event(e) for e in store.events_for(USER) if e.sequence_number > before],
                "changes": r.changes, "question": r.question,
                "pending": p and {"proposal_id": p.proposal_id, "question": p.question,
                                  "draft": {"attribute": p.draft.attribute, "subject": p.draft.subject,
                                            "op": p.draft.op.value, "scope": p.draft.scope.value,
                                            "evidence": p.draft.evidence}},
                "pending_cleared": had is not None and p is None,
                "profile": r.profile, "recs": recs_dict(recommend(menu, agent.profile(USER, cid).active)),
                "reply": r.reply, "mentioned": r.mentioned, "reply_errors": r.trace["reply_errors"],
                "fallback": r.trace["fallback"], "drafts": drafts[line]})
        if v == 2:
            try:
                conv.answer(stale, True)
                sys.exit("a late yes to the voided question was accepted")
            except StaleConfirmation as e:
                trace["late_yes"] = {"proposal_id": stale, "refused": str(e),
                                     "events_after": len(store.events_for(USER))}
            trace["final_events"] = [event(e) for e in store.events_for(USER)]
        store.close()

    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    if tests.returncode:
        sys.exit("the test suite fails; the film's receipt reports it\n" + tests.stdout[-2000:])
    trace["tests"] = tests.stdout.strip().splitlines()[-1].strip("= ")
    out = ROOT / "video" / "spike" / "trace.js"
    out.parent.mkdir(exist_ok=True)
    out.write_text(f"const TRACE = {json.dumps(trace)};\n")
    print(out, "·", trace["tests"])


if __name__ == "__main__":
    main()
