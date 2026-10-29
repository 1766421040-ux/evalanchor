"""Small, explicitly budgeted OpenAI Responses adapter (stdlib only).

Pricing/parameters checked against official documentation on 2026-10-29.
Dollar reservations are conservative estimates, not an account billing cap.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .contracts import ContractError, loads, validate_suite
from .providers import NoRedirect, ProviderError

MODEL = "gpt-6-luna"
BASE_URL = "https://api.openai.com/v1"
ENDPOINT = BASE_URL + "/responses"
MAX_RESPONSE_BYTES = 1_000_000
MAX_PROMPT_BYTES = 4096
PRICING_DATE = "2026-10-29"
INPUT_RATE = 0.10
CACHED_INPUT_RATE = 0.01
OUTPUT_RATE = 0.50
# Reserve every input token at the published 12h cache-write price, even though
# the requests do not ask for 12h caching. No assumed cache discount in budgets.
CONSERVATIVE_INPUT_RATE = 0.25


def load_api_key(env_file: str | Path = ".env.local", *, environ=None) -> str:
    """Read one literal key; never source shell, expand variables, or print it."""
    environment = os.environ if environ is None else environ
    value = environment.get("OPENAI_API_KEY")
    if not value:
        path = Path(env_file)
        if not path.is_file():
            raise ContractError("OPENAI_API_KEY is absent; configure it locally in .env.local or the environment")
        if path.stat().st_size > 65536:
            raise ContractError("Local environment file exceeds 64 KiB")
        matches = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*)", line)
            if match:
                matches.append(match.group(1).strip())
        if len(matches) != 1:
            raise ContractError("Local environment file must define OPENAI_API_KEY exactly once")
        value = matches[0]
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,4096}", value):
        raise ContractError("OPENAI_API_KEY must be a nonempty literal token; shell expressions are unsupported")
    return value


def _validate_messages(messages: list[dict]) -> int:
    if not isinstance(messages, list) or not messages:
        raise ContractError("Responses input requires nonempty messages")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ContractError("Responses input contains only role/content, never labels or references")
        if message["role"] not in {"system", "developer", "user", "assistant"} or not isinstance(message["content"], str):
            raise ContractError("Invalid Responses message role/content")
    size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise ContractError("Prompt exceeds the 4096-byte smoke-test limit; no truncation performed")
    return size


def estimate_request(messages: list[dict], max_output_tokens: int = 256) -> dict:
    if type(max_output_tokens) is not int or max_output_tokens not in {256, 512}:
        raise ContractError("Smoke output cap must be 256 or 512 tokens")
    size = _validate_messages(messages)
    reserved_input_tokens = size + 2048
    estimate = (reserved_input_tokens * CONSERVATIVE_INPUT_RATE + max_output_tokens * OUTPUT_RATE) / 1_000_000
    return {"prompt_utf8_bytes": size, "reserved_input_tokens": reserved_input_tokens, "max_output_tokens": max_output_tokens, "conservative_estimate_usd": estimate}


def budget_plan(suite: dict, *, repeats: int = 1, max_output_tokens: int = 256) -> dict:
    validate_suite(suite)
    if type(repeats) is not int or repeats not in {1, 2}:
        raise ContractError("Smoke test permits one or two repeats")
    calls = len(suite["cases"]) * repeats
    if calls not in {16, 32}:
        raise ContractError("OpenAI smoke test requires exactly 16 or 32 planned calls")
    requests = [estimate_request(case["messages"], max_output_tokens) for case in suite["cases"]]
    cost = sum(r["conservative_estimate_usd"] for r in requests) * repeats
    if cost > 0.05:
        raise ContractError("Conservative estimate exceeds the $0.05 experiment allowance")
    return {
        "model": MODEL,
        "endpoint": ENDPOINT,
        "planned_calls": calls,
        "repeats": repeats,
        "retries": 0,
        "reasoning_effort": "none",
        "max_output_tokens": max_output_tokens,
        "service_tier": "default",
        "max_reserved_input_tokens_per_call": max(r["reserved_input_tokens"] for r in requests),
        "conservative_estimate_usd": round(cost, 8),
        "budget_allowance_usd": 0.05,
        "pricing_checked_on": PRICING_DATE,
        "input_reserve_usd_per_million": CONSERVATIVE_INPUT_RATE,
        "output_usd_per_million": OUTPUT_RATE,
        "budget_note": "UTF-8 byte count plus 2048 framing tokens per input, priced at the 12h cache-write rate. This is a conservative estimate, not an account billing cap; failed requests may incur unknown charges.",
    }


def _count(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid {name}")
    return value


def parse_response(raw: bytes, *, max_output_tokens: int) -> dict:
    """Preserve native response, model, incomplete status, and exact text."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError("OpenAI response exceeds 1 MB")
    try:
        body = loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Response is not an object")
        for field in ("id", "model"):
            if not isinstance(body.get(field), str) or not body[field]:
                raise ValueError(f"Missing {field}")
        status = body.get("status")
        if status not in {"completed", "incomplete"}:
            raise ValueError("Response status is not completed or incomplete")
        if not isinstance(body.get("output"), list):
            raise ValueError("Missing output items")
        fragments = []
        for item in body["output"]:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise ValueError("Invalid output item")
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ValueError("Unsupported output item; this adapter permits no tools")
            for content in item["content"]:
                if not isinstance(content, dict):
                    raise ValueError("Invalid output content")
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    fragments.append(content["text"])
                elif content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                    fragments.append(content["refusal"])
                else:
                    raise ValueError("Invalid text or refusal content")
        if not fragments and status == "completed":
            raise ValueError("Completed response has no text or refusal")
        usage = body.get("usage")
        normalized_usage = {}
        cost = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("Invalid usage")
            input_tokens = _count(usage.get("input_tokens"), "input tokens")
            output_tokens = _count(usage.get("output_tokens"), "output tokens")
            total = _count(usage.get("total_tokens"), "total tokens")
            if total != input_tokens + output_tokens or output_tokens > max_output_tokens:
                raise ValueError("Usage totals contradict request/output limit")
            normalized_usage = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": total, "native": copy.deepcopy(usage)}
            details = usage.get("input_tokens_details")
            if details is not None:
                if not isinstance(details, dict):
                    raise ValueError("Invalid input token details")
                for key in ("cached_tokens", "cache_write_tokens"):
                    if key in details:
                        _count(details[key], key)
                cached = details.get("cached_tokens")
                written = details.get("cache_write_tokens")
                if cached is not None and cached > input_tokens or written is not None and written > input_tokens:
                    raise ValueError("Cache usage exceeds input tokens")
                # Cache-write retention can change its price. Exact billing is
                # left unknown unless the server explicitly says no writes.
                if cached is not None and written == 0 and body["model"] == MODEL and body.get("service_tier") == "default":
                    cost = ((input_tokens - cached) * INPUT_RATE + cached * CACHED_INPUT_RATE + output_tokens * OUTPUT_RATE) / 1_000_000
            output_details = usage.get("output_tokens_details")
            if output_details is not None:
                if not isinstance(output_details, dict):
                    raise ValueError("Invalid output token details")
                if "reasoning_tokens" in output_details and _count(output_details["reasoning_tokens"], "reasoning tokens") > output_tokens:
                    raise ValueError("Reasoning usage exceeds output tokens")
        return {"output": "".join(fragments), "usage": normalized_usage, "cost_usd": cost, "response": body, "cached": False}
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        # No raw response/body in error strings; errors can echo credentials.
        raise ProviderError("Malformed or unsupported OpenAI Responses body") from exc


class OpenAIResponsesProvider:
    """Official endpoint only, no retries/tools, maximum 32 network attempts."""

    def __init__(self, config: dict, *, api_key: str | None = None, max_calls: int = 32, budget_usd: float = 0.05, opener=None):
        if config.get("model") != MODEL or config.get("base_url", BASE_URL).rstrip("/") != BASE_URL:
            raise ContractError("Budgeted provider supports only gpt-6-luna at the official OpenAI endpoint")
        if config.get("api_key_env", "OPENAI_API_KEY") != "OPENAI_API_KEY":
            raise ContractError("Budgeted provider reads only OPENAI_API_KEY")
        if type(max_calls) is not int or not 1 <= max_calls <= 32:
            raise ContractError("Provider call cap must be an integer in [1,32]")
        if type(budget_usd) not in (int, float) or not math.isfinite(budget_usd) or not 0 < budget_usd <= 0.05:
            raise ContractError("Budget allowance must be in (0,0.05] USD")
        self.config = copy.deepcopy(config)
        self.max_output_tokens = config.get("max_tokens", 256)
        estimate_request([{"role": "user", "content": "validation"}], self.max_output_tokens)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._api_key or not re.fullmatch(r"[A-Za-z0-9_-]{10,4096}", self._api_key):
            raise ContractError("A local literal OPENAI_API_KEY is required for execution")
        self.max_calls = max_calls
        self.budget_usd = budget_usd
        self.calls = 0
        self.reserved_usd = 0.0
        self._halt_reason = None
        self._lock = threading.Lock()
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def generate(self, messages: list[dict], case_id: str, repeat: int) -> dict:
        reserve = estimate_request(messages, self.max_output_tokens)["conservative_estimate_usd"]
        with self._lock:
            if self._halt_reason:
                raise ProviderError(self._halt_reason)
            if self.calls >= self.max_calls or self.reserved_usd + reserve > self.budget_usd + 1e-12:
                raise ProviderError("Experiment call/reservation limit reached; request was not sent")
            self.calls += 1
            self.reserved_usd += reserve
        payload = {"model": MODEL, "input": copy.deepcopy(messages), "reasoning": {"effort": "none"}, "max_output_tokens": self.max_output_tokens, "store": False, "stream": False, "truncation": "disabled", "service_tier": "default"}
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._api_key}, method="POST")
        try:
            with self.opener.open(request, timeout=self.config.get("timeout", 60)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            with self._lock:
                self._halt_reason = f"OpenAI experiment halted after HTTP {exc.code}; subsequent request not sent"
            raise ProviderError(f"OpenAI HTTP {exc.code}; no automatic retry", retryable=False) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            with self._lock:
                self._halt_reason = "OpenAI experiment halted after transport failure; subsequent request not sent"
            raise ProviderError(f"OpenAI transport failure ({type(exc).__name__}); no automatic retry", retryable=False) from exc
        try:
            result = parse_response(raw, max_output_tokens=self.max_output_tokens)
        except ProviderError:
            with self._lock:
                self._halt_reason = "OpenAI experiment halted after unsupported response; subsequent request not sent"
            raise
        result["request"] = {"endpoint": ENDPOINT, **payload}
        return result
