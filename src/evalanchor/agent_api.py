"""Budgeted, replayable native Responses function calls for OrderOps.

Every dispatched request has an immutable identity and a durable reservation.
An uncertain network outcome is never silently retried. Credentials never enter
the serializable request, response receipt, exception text, or replay cache.
"""
from __future__ import annotations

import copy
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

from .contracts import ContractError, canonical, fingerprint, loads, read_json, write_json
from .providers import NoRedirect

ENDPOINT = "https://api.openai.com/v1/responses"
PRICING = {
    "gpt-6-luna": {"input": .10, "cached": .01, "write_upper": .25, "output": .50},
    "gpt-6.1-terra": {"input": 1., "cached": .05, "write_upper": 2.50, "output": 5.},
}
PRICING_CHECKED = "2026-10-29"


class AgentAPIError(RuntimeError):
    pass


def make_request(model, messages, tools, max_output_tokens=512):
    if model not in PRICING:
        raise ContractError("Model has no verified price in the bounded experiment registry")
    if type(max_output_tokens) is not int or not 128 <= max_output_tokens <= 1024:
        raise ContractError("Agent output cap must be an integer between 128 and 1024")
    if not isinstance(messages, list) or not isinstance(tools, list):
        raise ContractError("Responses input and tools must be lists")
    body = {"model": model, "input": copy.deepcopy(messages), "tools": copy.deepcopy(tools),
            "reasoning": {"effort": "none"}, "max_output_tokens": max_output_tokens,
            "parallel_tool_calls": False, "store": False, "stream": False,
            "truncation": "disabled", "service_tier": "default"}
    if len(canonical(body).encode()) > 120000:
        raise ContractError("Agent request exceeds the frozen 120KB context allowance; no truncation")
    return body


def reservation_estimate(body):
    rate = PRICING[body["model"]]
    # All UTF-8 bytes (including complete tool schemas and accumulated history)
    # plus generous protocol framing, at the highest verified cache-write rate.
    tokens = len(canonical(body).encode()) + 2048
    return (tokens * rate["write_upper"] + body["max_output_tokens"] * rate["output"]) / 1e6


def response_usage(body, requested_model, output_cap):
    """Return usage and conservative price estimate, preserving unknown fields."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return {}, None
    values = [usage.get(x) for x in ("input_tokens", "output_tokens", "total_tokens")]
    if any(type(v) is not int or not 0 <= v <= 1000000 for v in values):
        return {}, None
    inp, out, total = values
    if inp + out != total:
        return {}, None
    normalized = {"input_tokens": inp, "output_tokens": out, "total_tokens": total}
    details = usage.get("input_tokens_details", {})
    if not isinstance(details, dict):
        return normalized, None
    cached, written = details.get("cached_tokens"), details.get("cache_write_tokens")
    if any(type(v) is not int or not 0 <= v <= inp for v in (cached, written)):
        return normalized, None
    if cached + written > inp or body.get("model") != requested_model or body.get("service_tier") != "default":
        return normalized, None
    rate = PRICING[requested_model]
    cost = ((inp - cached - written) * rate["input"] + cached * rate["cached"]
            + written * rate["write_upper"] + out * rate["output"]) / 1e6
    normalized.update(cached_tokens=cached, cache_write_tokens=written)
    return normalized, cost


def validate_response(body):
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise AgentAPIError("Response is missing its native identifier")
    if body.get("status") not in ("completed", "incomplete") or not isinstance(body.get("output"), list):
        raise AgentAPIError("Unsupported Responses status or output shape")
    for item in body["output"]:
        if not isinstance(item, dict) or item.get("type") not in ("function_call", "message", "reasoning"):
            raise AgentAPIError("Unsupported native output item")
        if item["type"] == "function_call":
            for key in ("name", "arguments", "call_id"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise AgentAPIError("Malformed function call envelope")
    return body


def budget_contract_error(native, body):
    usage, cost = response_usage(native,body['model'],body['max_output_tokens'])
    if cost is None:
        return 'unverifiable_model_usage_or_price'
    if usage['output_tokens']>body['max_output_tokens']:
        return 'server_output_exceeded_requested_cap'
    if usage['input_tokens']>len(canonical(body).encode())+2048:
        return 'observed_input_exceeded_reserved_token_bound'
    return None


class ResponsesClient:
    kind = 'native_responses_tool_execution'
    def __init__(self, *, api_key, ledger, cache_dir, timeout=60, opener=None):
        if not isinstance(api_key, str) or not api_key or "\n" in api_key:
            raise ContractError("A literal local API credential is required")
        self._key = api_key
        self.ledger = ledger
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def generate(self, *, request_key, model, messages, tools, max_output_tokens=512):
        body = make_request(model, messages, tools, max_output_tokens)
        digest = fingerprint(body)
        path = self.cache_dir / (fingerprint(request_key) + ".json")
        existing = self.ledger.lookup(request_key)
        if path.exists():
            saved = read_json(path)
            if saved.get("request_key") != request_key or saved.get("request_hash") != digest:
                raise ContractError("Request replay cache identity changed")
            if saved.get("request") != body or saved.get("response_hash") != fingerprint(saved.get("response")):
                raise ContractError("Request replay cache content mismatch")
            if existing is None:
                raise ContractError("A cached paid response requires its original budget ledger")
            usage, cost = response_usage(saved["response"], model, max_output_tokens)
            self._settle(existing["reservation_id"], cost, saved["response"])
            issue=budget_contract_error(saved['response'],body)
            if issue:
                self.ledger.halt(issue)
                raise AgentAPIError('Campaign halted: '+issue)
            validate_response(saved["response"])
            return {**saved, "usage": usage, "estimated_cost_usd": cost, "replayed": True}
        if existing is not None:
            # A reservation with no local response may already have been billed.
            # Never replay an HTTP request merely because its worker was lost.
            if existing.get('state')=='dispatched':
                self.ledger.mark_unknown(existing['reservation_id'],'lost_local_response_receipt')
            raise AgentAPIError("Prior request has no replayable receipt; refused to dispatch it again")
        upper = reservation_estimate(body)
        reservation_id = self.ledger.reserve(request_key, upper, {
            "model": model, "request_hash": digest, "max_output_tokens": max_output_tokens,
            "pricing_checked": PRICING_CHECKED})
        dispatched = False
        started = time.perf_counter()
        try:
            self.ledger.mark_dispatched(reservation_id)
            dispatched = True
            request = urllib.request.Request(ENDPOINT, data=canonical(body).encode(),
                headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}, method="POST")
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise AgentAPIError("Responses body exceeds the 2MB receipt limit")
            try:
                native = loads(raw)
            except (ValueError,UnicodeError) as exc:
                self.ledger.halt('unverifiable_non_json_response')
                raise AgentAPIError('Malformed remote JSON response; campaign halted') from None
            if not isinstance(native, dict):
                raise AgentAPIError("Responses body must be an object")
            saved = {"request_key": request_key, "request_hash": digest, "request": body,
                     "reservation_id": reservation_id, "response": native, "response_hash": fingerprint(native),
                     "latency_ms": (time.perf_counter() - started) * 1000}
            # The receipt precedes settlement: a crash can recover accounting
            # from it without issuing a second model request.
            write_json(path, saved)
            usage, cost = response_usage(native, model, max_output_tokens)
            self._settle(reservation_id, cost, native)
            issue=budget_contract_error(native,body)
            if issue:
                self.ledger.halt(issue)
                raise AgentAPIError('Campaign halted: '+issue)
            validate_response(native)
            return {**saved, "usage": usage, "estimated_cost_usd": cost, "replayed": False}
        except BaseException as exc:
            if dispatched:
                # A successful saved receipt is reconciled on replay; do not
                # replace an already-settled actual value with an unknown value.
                state = self.ledger.lookup(request_key)
                if state and state.get("state") not in ("settled", "unknown"):
                    self.ledger.mark_unknown(reservation_id, type(exc).__name__)
            else:
                self.ledger.cancel_unstarted(reservation_id, "dispatch_not_started")
            if isinstance(exc, urllib.error.HTTPError):
                raise AgentAPIError(f"OpenAI HTTP {exc.code}; no automatic retry") from None
            if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
                raise AgentAPIError("OpenAI transport or local receipt failure; no automatic retry") from None
            raise

    def _settle(self, reservation_id, cost, native):
        if cost is None:
            self.ledger.mark_unknown(reservation_id, "usage_or_price_not_verifiable")
        else:
            self.ledger.settle(reservation_id, cost, receipt={
                "response_id": native.get("id"), "response_hash": fingerprint(native),
                "pricing_basis": "published token rates; cache writes conservatively priced at 12h upper rate"})
