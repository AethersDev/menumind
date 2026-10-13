"""HTTP API and chat page.

Run locally:  uvicorn server:app --reload
Environment:  ANTHROPIC_API_KEY (required for chat), DB_PATH (default data/menumind.db),
              EXTRACT_MODEL, REPLY_MODEL (default claude-haiku-5-5), COOKIE_SECURE=1 behind HTTPS
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from accounts import AccountError, Accounts
from agent import Agent, ModelUnavailable, profile_view
from extractor import DEFAULT_MODEL
from memory import EventStore
from menu import DEFAULT_PATH, load_menu

HERE = Path(__file__).parent
COOKIE = "menumind_session"
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_MESSAGE = 2000


class Credentials(BaseModel):
    username: str
    pin: str


class ChatIn(BaseModel):
    conversation_id: str
    message: str


def _anthropic_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ModelUnavailable("ANTHROPIC_API_KEY is not set")
    import anthropic
    return anthropic.Anthropic()


def create_app(db_path: Optional[str] = None, client=None, menu_path=None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", str(HERE / "data" / "menumind.db"))
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store, accounts = EventStore(db_path), Accounts(db_path)
    menu = load_menu(menu_path or DEFAULT_PATH)
    agent = Agent(store, menu, client, client_factory=_anthropic_client,
                  extract_model=os.environ.get("EXTRACT_MODEL", DEFAULT_MODEL),
                  reply_model=os.environ.get("REPLY_MODEL", DEFAULT_MODEL))
    secure = os.environ.get("COOKIE_SECURE") == "1"

    app = FastAPI(title="MenuMind")
    app.state.agent, app.state.accounts = agent, accounts

    def user(request: Request) -> tuple[str, str]:
        found = accounts.session_user(request.cookies.get(COOKIE))
        if found is None:
            raise HTTPException(401, "Sign in to continue.")
        return found

    def conversation(cid: str) -> str:
        if not CONVERSATION_ID.match(cid):
            raise HTTPException(400, "Invalid conversation id.")
        return cid

    def signed_in(response: Response, user_id: str, username: str) -> dict:
        response.set_cookie(COOKIE, accounts.start_session(user_id), httponly=True,
                            samesite="lax", secure=secure, max_age=60 * 60 * 24 * 30)
        return {"username": username}

    @app.get("/")
    def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "chat_enabled": bool(os.environ.get("ANTHROPIC_API_KEY")) or client is not None}

    @app.get("/api/menu")
    def get_menu():
        return {"restaurant": menu.restaurant, "items": [i.public() for i in menu.items]}

    @app.post("/api/register")
    def register(body: Credentials, response: Response):
        try:
            user_id = accounts.register(body.username, body.pin)
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        return signed_in(response, user_id, body.username.strip().lower())

    @app.post("/api/login")
    def login(body: Credentials, response: Response):
        user_id = accounts.login(body.username, body.pin)
        if user_id is None:
            raise HTTPException(401, "That username and PIN don't match. Check them, or create an account.")
        return signed_in(response, user_id, body.username.strip().lower())

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        accounts.end_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.post("/api/conversations")
    def new_conversation(u=Depends(user)):
        return {"conversation_id": str(uuid.uuid4())}

    @app.get("/api/me")
    def me(conversation_id: Optional[str] = None, u=Depends(user)):
        user_id, username = u
        cid = conversation(conversation_id) if conversation_id else "none-yet-0"
        return {"username": username,
                "profile": profile_view(agent.profile(user_id, cid)),
                "question": agent.pending_question(user_id, cid) if conversation_id else None}

    @app.post("/api/chat")
    def chat(body: ChatIn, u=Depends(user)):
        user_id, _ = u
        cid = conversation(body.conversation_id)
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "Type a message first.")
        if len(text) > MAX_MESSAGE:
            raise HTTPException(400, f"Messages are limited to {MAX_MESSAGE} characters.")
        try:
            result = agent.turn(user_id, cid, text)
        except ModelUnavailable:
            raise HTTPException(503, "Chat is off: the server has no ANTHROPIC_API_KEY. "
                                     "Set it and restart the server.") from None
        return {"reply": result.reply, "mentioned": result.mentioned, "changes": result.changes,
                "question": result.question, "profile": result.profile, "trace": result.trace}

    return app


app = create_app() if os.environ.get("MENUMIND_NO_APP") != "1" else None
