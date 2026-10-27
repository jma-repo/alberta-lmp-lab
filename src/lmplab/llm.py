"""Client for a local OpenAI-compatible model server (LM Studio, Ollama, or vLLM).

Two safety properties, as in the claims-fraud-mlops project this is ported from: the endpoint
must be on this machine (submission data and market data never leave it), and every caller has
an offline path, so the pipeline still runs end to end when no model server is up.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "http://127.0.0.1:1234/v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
CHAT_PREFERENCE = ["qwen2.5-7b-instruct", "qwen2.5-3b-instruct", "llama-3.2-3b-instruct"]


class LLMUnavailable(RuntimeError):
    """No local model server, or no chat model loaded."""


def assert_local(base: str) -> str:
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported LLM URL scheme: {parsed.scheme!r}")
    if (parsed.hostname or "") not in LOCAL_HOSTS:
        raise ValueError(f"Refusing non-local inference host: {parsed.hostname}")
    return base.rstrip("/")


def disabled() -> bool:
    return os.environ.get("LMPLAB_NO_LLM", "").strip() not in ("", "0", "false")


class LocalLLM:
    def __init__(self, base: str | None = None):
        self.base = assert_local(base or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE)
        self._model: str | None = None

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self._request("POST", path, payload, timeout)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        if disabled():
            raise LLMUnavailable("LLM calls are disabled (LMPLAB_NO_LLM)")
        try:
            response = httpx.request(method, self.base + path, json=payload, timeout=timeout, follow_redirects=False)
        except httpx.HTTPError as error:
            raise LLMUnavailable(f"Local model server not reachable at {self.base}: {error.__class__.__name__}") from error
        if response.status_code != 200:
            raise LLMUnavailable(f"Local model server returned HTTP {response.status_code}")
        return response.json()

    def model(self) -> str:
        if self._model is None:
            ids = [m["id"] for m in self._request("GET", "/models", None, 3.0).get("data", []) if isinstance(m.get("id"), str)]
            chat = [i for i in ids if "embed" not in i.lower()]
            override = os.environ.get("LLM_CHAT_MODEL")
            self._model = (override if override in chat else None) or next((p for p in CHAT_PREFERENCE if p in chat), None) \
                or (chat[0] if chat else None)
            if not self._model:
                raise LLMUnavailable("No chat model loaded in the local model server")
        return self._model

    def chat(self, system: str, user: str, max_tokens: int = 400, temperature: float = 0.2) -> str:
        message = self.complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                max_tokens=max_tokens, temperature=temperature)
        text = (message.get("content") or "").split("</think>")[-1].replace("<think>", "").strip()
        if not text:
            raise LLMUnavailable("The model returned an empty answer")
        return text

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                 max_tokens: int = 600, temperature: float = 0.1) -> dict[str, Any]:
        """One chat completion; returns the assistant message (with `tool_calls` when the model asks for tools)."""
        payload: dict[str, Any] = {"model": self.model(), "messages": messages, "max_tokens": max_tokens,
                                   "temperature": temperature}
        if tools:
            payload["tools"] = tools
        # Large local models that reason before answering can take minutes per call.
        data = self._post("/chat/completions", payload, float(os.environ.get("LLM_TIMEOUT_S", "900")))
        message = (data.get("choices") or [{}])[0].get("message") or {}
        for call in message.get("tool_calls") or []:
            arguments = call.get("function", {}).get("arguments")
            if isinstance(arguments, str):
                try:
                    call["function"]["parsed"] = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    call["function"]["parsed"] = {}
        return message

    def status(self) -> dict[str, Any]:
        try:
            return {"endpoint": self.base, "ready": True, "model": self.model()}
        except LLMUnavailable as error:
            return {"endpoint": self.base, "ready": False, "model": None, "reason": str(error)}
