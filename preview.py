"""Run the app with a scripted model, for UI work without an API key.

    python preview.py        then open http://127.0.0.1:8000

Only the phrases below are understood as preferences; everything else gets a generic
suggestion. Use the real server (uvicorn server:app) for actual conversations.
"""
import os

os.environ["MENUMIND_NO_APP"] = "1"

import uvicorn  # noqa: E402

from fake_llm import FakeLLM, claim  # noqa: E402
from server import create_app  # noqa: E402

PHRASES = {
    "I love spicy food, but I hate mushrooms.": [
        claim("spice_tolerance", value="hot"), claim("disliked_ingredient", subject="mushroom")],
    "I'm vegetarian.": [claim("diet", value="vegetarian")],
    "I'm allergic to peanuts.": [claim("allergen", subject="peanut")],
    "I'm no longer allergic to peanuts.": [claim("allergen", "negate", subject="peanut")],
    "I don't like spicy food anymore.": [claim("spice_tolerance", value="none")],
    "Keep it under $15.": [claim("budget_max", value=15, temporal="conversation")],
}

if __name__ == "__main__":
    print("Preview mode. Understood phrases:\n  " + "\n  ".join(PHRASES))
    app = create_app(db_path=os.environ.get("DB_PATH", "data/preview.db"),
                     client=FakeLLM(claims=PHRASES, replies={"yes": "yes", "no": "no"}))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
