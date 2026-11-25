"""OpenAI-compatible LLM access shared by every localization stage.

The endpoint, credentials and model are exactly the ones the repair half uses:
``config.Config`` reads them from ``LLM_API_KEY`` / ``LLM_BASE_URL`` /
``LLM_MODEL``, and this module builds its client from those same constants.
So both halves of the system always run against one LLM configuration.

``LLMHelper`` keeps the call signature the original localization scripts were
written against (``llm_helper.query(messages, number_of_result=...)`` returning
``(completion, seconds)`` with ``completion.choices[0].message.content``), which
is why the stage scripts only had their bootstrap block rewritten.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import requests
from openai import OpenAI, OpenAIError

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config as repo_config  # noqa: E402  (repair-half configuration: Config.LLM_*)

_client = None


def get_client() -> OpenAI:
    """The shared OpenAI client (built once, same settings as the repair half)."""
    global _client
    if _client is None:
        if not repo_config.Config.LLM_API_KEY:
            raise RuntimeError(
                "LLM_API_KEY is not set. Export LLM_API_KEY (and, if needed, "
                "LLM_BASE_URL / LLM_MODEL) before running localization."
            )
        _client = OpenAI(
            api_key=repo_config.Config.LLM_API_KEY,
            base_url=repo_config.Config.LLM_BASE_URL,
            timeout=300.0,
        )
    return _client


def _wrap(content: str):
    """Mimic ``chat.completions.create`` response shape used by the stages."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
    )


def query(messages, number_of_result: int = 1, temperature: float = None, retries: int = 3):
    """Raw chat completion.

    Returns ``(completion, seconds)``; ``completion.choices[0].message.content``
    holds the text (empty string when every attempt failed).
    """
    client = get_client()
    if temperature is None:
        temperature = 0.0
    last_error = None
    started = datetime.now()
    for attempt in range(retries):
        try:
            completion = client.chat.completions.create(
                model=repo_config.Config.LLM_MODEL,
                messages=messages,
                temperature=temperature,
                n=number_of_result,
            )
            return completion, (datetime.now() - started).total_seconds()
        except (requests.exceptions.Timeout, OpenAIError) as exc:
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM request failed after {retries} attempts: {last_error}")


def call_text(prompt: str, system_prompt: str = None, temperature: float = 0.0,
              retries: int = 3) -> str:
    """Convenience wrapper for a single user turn; returns '' on total failure."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    try:
        completion, _ = query(messages, temperature=temperature, retries=retries)
    except RuntimeError as exc:
        print(f"[llm] {exc}")
        return ""
    if not completion.choices:
        return ""
    return completion.choices[0].message.content or ""


class LLMHelper:
    """Drop-in replacement for the original ``utils.llm_helper.LLMHelper``."""

    def __init__(self, *args, **kwargs):
        # The original constructor took (llm_setting, http_proxy); the values
        # now come from config.Config, so the arguments are accepted and ignored.
        pass

    def query(self, prompt, number_of_result: int = 1, temperature: float = None):
        return query(prompt, number_of_result=number_of_result, temperature=temperature)
