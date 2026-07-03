"""MiniMax (Mavis) LLM client with graceful degradation.

Wraps the OpenAI-compatible chat completions endpoint at
https://api.minimax.io/v1. Two key behaviours:

1. `is_available()` lets each node decide whether to call the LLM or
   fall back to a deterministic template. Network outage / key revoked /
   model down should not block the weekly batch.

2. Safe-key logging: never log the full key.

Reference: ~/.mavis/agents/mavis/memory/llm-api-patterns.md
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MODEL = "MiniMax-Text-01"


def _mask_key(key: str | None) -> str:
    if not key:
        return "<unset>"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}  ({len(key)} chars)"


class LLMClient:
    """Thin wrapper over the MiniMax chat completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        disabled: bool | None = None,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.base_url = base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
        self.disabled = (
            disabled
            if disabled is not None
            else os.getenv("LLM_DISABLED", "0") == "1"
        )

        log.info(
            "llm.init base_url=%s model=%s key=%s disabled=%s",
            self.base_url,
            self.model,
            _mask_key(self.api_key),
            self.disabled,
        )

        self._client: OpenAI | None = (
            OpenAI(api_key=self.api_key, base_url=self.base_url)
            if (not self.disabled and self.api_key)
            else None
        )

    def is_available(self) -> bool:
        return self._client is not None and not self.disabled

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> str:
        """Synchronous chat completion. Raises on any failure."""
        if not self.is_available():
            raise RuntimeError("LLM disabled or API key missing")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        log.info("llm.call model=%s json=%s", self.model, json_mode)
        resp = self._client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    def complete_json(
        self, system: str, user: str, *, temperature: float = 0.2
    ) -> dict[str, Any]:
        """Complete + parse JSON. Strips ```json fences if present."""
        text = self.complete(system, user, json_mode=True, temperature=temperature)
        text = _strip_code_fence(text)
        return json.loads(text)


def _strip_code_fence(text: str) -> str:
    """Some models wrap their JSON output in ```json ... ``` even when
    response_format is set. Strip it before json.loads."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "LLMClient"]