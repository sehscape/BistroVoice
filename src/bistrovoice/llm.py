"""Thin client for any OpenAI-compatible chat endpoint (Groq by default).

Responses can be cached on disk, keyed by the exact request. The evaluation
uses this so a rerun costs no API quota; the latency stored with each cached
response is the one measured on the original call.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from .config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0  # served from the provider's prompt cache
    wait_ms: float = 0.0  # time spent waiting on provider rate limits (not latency)
    cached: bool = False  # served from our local eval cache


class LLMError(RuntimeError):
    pass


class QuotaExhausted(LLMError):
    """The provider's daily token allowance is used up."""


class LLMClient:
    def __init__(self, settings: Settings, cache_path: str | Path | None = None,
                 temperature: float = 0.0, max_tokens: int = 1024, max_wait_s: float = 3.0):
        if not settings.llm_api_key and "localhost" not in settings.llm_base_url:
            raise LLMError("No LLM key: set GROQ_API_KEY in .env (free at console.groq.com/keys)")
        self.model = settings.llm_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Live calls wait at most a few seconds for a rate limit (then the agent
        # apologises); the evaluation passes a long wait and just runs slower.
        self.max_wait_s = max_wait_s
        self.quota_exhausted = False
        self.client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key or "none",
                             timeout=20, max_retries=0)
        self.cache = None
        if cache_path:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            self.cache = sqlite3.connect(str(cache_path), check_same_thread=False)
            self.cache.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT)")

    def _key(self, messages: list[dict], tools: list[dict]) -> str:
        blob = json.dumps({"model": self.model, "t": self.temperature, "m": messages, "tools": tools},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        key = self._key(messages, tools) if self.cache else None
        if key:
            row = self.cache.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
            if row:
                data = json.loads(row[0])
                data["tool_calls"] = [ToolCall(**tc) for tc in data["tool_calls"]]
                return LLMReply(**data, cached=True)

        reply = self._call(messages, tools)

        if key:
            data = {"content": reply.content, "ms": reply.ms, "cached_tokens": reply.cached_tokens,
                    "wait_ms": reply.wait_ms,
                    "prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens,
                    "tool_calls": [tc.__dict__ for tc in reply.tool_calls]}
            self.cache.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, json.dumps(data)))
            self.cache.commit()
        return reply

    def _extra(self) -> dict:
        # Reasoning models think before answering. On a phone call that is
        # dead air, so keep it short (gpt-oss) or off (qwen3).
        if self.model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": "low"}
        if "qwen3" in self.model:
            return {"reasoning_effort": "none"}
        return {}

    def _call(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        """One completion. Rate-limit waits are excluded from the measured time.

        On Groq's free tier the per-minute token limit is the bottleneck. Waiting
        for it is a quota artefact, not model latency, so it is reported
        separately (wait_ms) instead of being mixed into the latency numbers.
        """
        temperature = self.temperature
        wait_ms = 0.0
        bad_tool_calls = 0
        timeouts = 0
        for _attempt in range(12):
            start = time.perf_counter()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=tools, tool_choice="auto",
                    temperature=temperature, max_tokens=self.max_tokens, **self._extra(),
                )
            except RateLimitError as err:
                if "per day" in str(err).lower():
                    self.quota_exhausted = True
                    raise QuotaExhausted(str(err)) from err
                if self.max_wait_s <= 0:
                    raise LLMError(str(err)) from err
                pause = min(self.max_wait_s, _retry_after(err))
                time.sleep(pause)
                wait_ms += pause * 1000
                continue
            except APIStatusError as err:
                # Groq returns 400 "tool_use_failed" when a model writes a
                # malformed tool call; one retry at a higher temperature fixes most.
                malformed = err.status_code == 400 and any(
                    k in str(err) for k in ("tool_use_failed", "tool call", "parse"))
                if malformed and bad_tool_calls == 0:
                    bad_tool_calls += 1
                    temperature = max(temperature, 0.4)
                    continue
                if err.status_code >= 500 and _attempt < 2:
                    time.sleep(1)
                    continue
                raise LLMError(str(err)) from err
            except (APITimeoutError, APIConnectionError) as err:
                if timeouts == 0:  # one retry for a dropped or stuck request
                    timeouts += 1
                    continue
                raise LLMError(str(err)) from err
            except Exception as err:
                raise LLMError(str(err)) from err
            ms = (time.perf_counter() - start) * 1000
            msg = resp.choices[0].message
            calls = [ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}")
                     for tc in (msg.tool_calls or [])]
            usage = resp.usage
            details = getattr(usage, "prompt_tokens_details", None)
            return LLMReply(content=(msg.content or "").strip(), tool_calls=calls, ms=ms,
                            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                            cached_tokens=getattr(details, "cached_tokens", 0) or 0,
                            wait_ms=wait_ms)
        raise LLMError("gave up after repeated rate limits / invalid tool calls")


def _retry_after(err: RateLimitError) -> float:
    headers = getattr(err.response, "headers", {}) or {}
    try:
        return float(headers.get("retry-after", 5)) + 0.5
    except (TypeError, ValueError):
        return 5.0
