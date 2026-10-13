"""HTTP API and chat page.

Run locally:  uvicorn server:app --reload
Environment:  LLM_PROVIDER (openai, the default, or anthropic) and its OPENAI_API_KEY or
              ANTHROPIC_API_KEY (required for chat), EXTRACT_MODEL, REPLY_MODEL (default per
              provider, see llm.py), DB_PATH (default data/menumind.db), COOKIE_SECURE=1 behind HTTPS
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from time import monotonic
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from accounts import SESSION_SECONDS, AccountError, Accounts, TooManyAttempts
from agent import Agent, ModelUnavailable, UnknownConversation, profile_view
from llm import DEFAULT_MODEL, KEY_ENV, PROVIDER, make_client
from memory import EventStore
from menu import DEFAULT_PATH, load_menu

HERE = Path(__file__).parent
COOKIE = "menumind_session"
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_MESSAGE = 2000
PUBLIC_TRACE = ("claims", "decisions", "confirmation", "fallback")  # no error text
ENDED = "This conversation has ended. Start a new one."
# A public demo pays for every model call, so chat and sign-ups are bounded: (count, seconds).
CHAT_PER_USER = (30, 10 * 60)
CHAT_TOTAL = (300, 24 * 60 * 60)  # ~1.5M tokens a day at the measured ~5k per turn
REGISTRATIONS = (50, 60 * 60)
MAX_BODY_BYTES = 16 * 1024        # the largest real request (a 2,000-character message) is ~2 KB


class _Window:
    """Sliding-window limit: allow() records a hit if the key is under its limit."""

    def __init__(self, limit: tuple[int, int]):
        self.count, self.seconds = limit
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = monotonic()
            self._hits = {k: v for k, v in self._hits.items() if now - v[-1] < self.seconds}
            recent = [t for t in self._hits.get(key, ()) if now - t < self.seconds]
            if len(recent) >= self.count:
                return False
            self._hits[key] = recent + [now]
            return True


class Credentials(BaseModel):  # length caps stop oversized input before any hashing
    username: str = Field(max_length=64)
    pin: str = Field(max_length=32)


class ChatIn(BaseModel):
    conversation_id: str
    message: str


def _model_client():
    if not os.environ.get(KEY_ENV[PROVIDER]):
        raise ModelUnavailable(f"{KEY_ENV[PROVIDER]} is not set")
    return make_client(PROVIDER)


def create_app(db_path: Optional[str] = None, client=None, menu_path=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db_path = db_path or os.environ.get("DB_PATH", str(HERE / "data" / "menumind.db"))
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store, accounts = EventStore(db_path), Accounts(db_path)
    menu = load_menu(menu_path or DEFAULT_PATH)
    agent = Agent(store, menu, client, client_factory=_model_client,
                  extract_model=os.environ.get("EXTRACT_MODEL") or DEFAULT_MODEL,  # "" is unset
                  reply_model=os.environ.get("REPLY_MODEL") or DEFAULT_MODEL)
    secure = os.environ.get("COOKIE_SECURE") == "1"

    app = FastAPI(title="MenuMind")
    app.state.agent, app.state.accounts = agent, accounts

    per_user, total, sign_ups = _Window(CHAT_PER_USER), _Window(CHAT_TOTAL), _Window(REGISTRATIONS)

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        # a chunked body has no declared size, so it could not be bounded before reading
        size = request.headers.get("content-length", "0")
        if "transfer-encoding" in request.headers or not size.isdigit() \
                or int(size) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "That request is too large."}, status_code=413)
        return await call_next(request)

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # the server logs the traceback; the client gets no internals. A failure partway
        # through a message may have saved part of it, so don't claim nothing was saved.
        return JSONResponse({"detail": "Something went wrong. Some changes may not have been "
                                       "saved; the ticket shows what is saved."}, status_code=500)

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
                            samesite="lax", secure=secure, max_age=SESSION_SECONDS)
        return {"username": username}

    @app.get("/")
    def index():
        return FileResponse(HERE / "static" / "index.html")

    started_at = datetime.now(timezone.utc).isoformat()  # tells one server start from the next

    @app.get("/api/health")
    def health():
        return {"ok": True, "started_at": started_at,
                "chat_enabled": bool(os.environ.get(KEY_ENV[PROVIDER])) or client is not None}

    @app.get("/api/menu")
    def get_menu():
        return {"restaurant": menu.restaurant, "items": [i.public() for i in menu.items]}

    @app.post("/api/register")
    def register(body: Credentials, response: Response):
        if not sign_ups.allow("all"):
            raise HTTPException(429, "Too many new accounts right now. Try again in an hour.")
        try:
            user_id = accounts.register(body.username, body.pin)
        except AccountError as e:
            raise HTTPException(400, str(e)) from None
        return signed_in(response, user_id, body.username.strip().lower())

    @app.post("/api/login")
    def login(body: Credentials, response: Response):
        try:
            user_id = accounts.login(body.username, body.pin)
        except TooManyAttempts as e:
            raise HTTPException(429, str(e)) from None
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
        return {"conversation_id": agent.start_conversation(u[0])}

    @app.get("/api/me")
    def me(conversation_id: Optional[str] = None, u=Depends(user)):
        user_id, username = u
        cid = conversation(conversation_id) if conversation_id else None
        try:
            return {"username": username,
                    "profile": profile_view(agent.profile(user_id, cid)),
                    "question": agent.pending_question(user_id, cid) if cid else None}
        except UnknownConversation:
            raise HTTPException(404, ENDED) from None

    @app.post("/api/chat")
    def chat(body: ChatIn, u=Depends(user)):
        user_id, _ = u
        cid = conversation(body.conversation_id)
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "Type a message first.")
        if len(text) > MAX_MESSAGE:
            raise HTTPException(400, f"Messages are limited to {MAX_MESSAGE} characters.")
        if not per_user.allow(user_id):
            raise HTTPException(429, "You're sending messages quickly. Wait a few minutes, then try again.")
        if not total.allow("all"):
            raise HTTPException(429, "The demo has reached today's message limit. Please try again tomorrow.")
        try:
            result = agent.turn(user_id, cid, text)
        except ModelUnavailable:
            raise HTTPException(503, f"Chat is off: the server has no {KEY_ENV[PROVIDER]}. "
                                     "Set it and restart the server.") from None
        except UnknownConversation:
            raise HTTPException(404, ENDED) from None
        return {"reply": result.reply, "mentioned": result.mentioned, "changes": result.changes,
                "question": result.question, "profile": result.profile,
                "trace": {k: result.trace[k] for k in PUBLIC_TRACE if k in result.trace}}

    return app


app = create_app() if os.environ.get("MENUMIND_NO_APP") != "1" else None
