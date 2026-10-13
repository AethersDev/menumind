"""Model provider: OpenAI by default, Anthropic kept as an alternative (LLM_PROVIDER=anthropic).

The app makes every model call in one shape, `client.messages.create(model, max_tokens,
system, messages, output_config)` with a JSON schema in output_config. That is the Anthropic
Messages API as is; OpenAIClient translates it to OpenAI Chat Completions with a strict JSON
schema. Either way the caller gets back `.content` text blocks, `.usage` and `.stop_reason`,
and failed calls raise one of MODEL_ERRORS.

Chat Completions rather than the Responses API: OpenAI's free daily tokens for shared traffic
are reported to cover Chat Completions, and at least one report says Responses is excluded.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import anthropic
import openai

KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
DEFAULT_MODELS = {"openai": "gpt-5.4-mini", "anthropic": "claude-haiku-5-5"}
PROVIDER = os.environ.get("LLM_PROVIDER", "openai")
if PROVIDER not in KEY_ENV:
    raise ValueError(f"LLM_PROVIDER must be one of {sorted(KEY_ENV)}, not {PROVIDER!r}")
DEFAULT_MODEL = DEFAULT_MODELS[PROVIDER]

MODEL_TIMEOUT_S = 30.0  # per model call; tune from the eval's measured p95 latency
MODEL_RETRIES = 1       # SDK retries of connection errors, rate limits and server errors
MODEL_ERRORS = (anthropic.APIError, openai.APIError)


class OpenAIClient:
    """An OpenAI SDK client behind the Messages-shaped call the app makes."""

    def __init__(self, sdk):
        self.sdk = sdk
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, model: str, max_tokens: int, system: str, messages: list,
                output_config: dict) -> SimpleNamespace:
        resp = self.sdk.chat.completions.create(
            model=model, max_completion_tokens=max_tokens,
            messages=[{"role": "developer", "content": system}, *messages],
            response_format={"type": "json_schema", "json_schema": {
                "name": "reply", "schema": output_config["format"]["schema"], "strict": True}},
            store=False)  # not kept for OpenAI's distillation and evals products
        choice = resp.choices[0]
        # only a finished answer counts: "length" is cut off, a refusal has no content
        text = choice.message.content if choice.finish_reason == "stop" else None
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)] if text else [],
            stop_reason=choice.finish_reason,
            usage=SimpleNamespace(input_tokens=resp.usage.prompt_tokens,
                                  output_tokens=resp.usage.completion_tokens))


def make_client(provider: str):
    """A client for `provider`, reading its API key from the environment."""
    if provider == "openai":
        return OpenAIClient(openai.OpenAI(timeout=MODEL_TIMEOUT_S, max_retries=MODEL_RETRIES))
    return anthropic.Anthropic(timeout=MODEL_TIMEOUT_S, max_retries=MODEL_RETRIES)
