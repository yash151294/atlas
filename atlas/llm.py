"""One door to Claude. Every call is logged with tokens, cache hits and cost so the
Evals page can show what a transcript and an answer actually cost."""
import hashlib, os, time
from typing import Type, TypeVar
import anthropic
from pydantic import BaseModel
from .config import PARAMS
from . import store, llm_azure

_client = None
LOG = "llm_calls.jsonl"
# $/MTok input, output; cache write 1.25x input, cache read 0.1x input
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}
T = TypeVar("T", bound=BaseModel)


def provider() -> dict:
    """Where inference runs. Microsoft Foundry when ANTHROPIC_FOUNDRY_RESOURCE (or _BASE_URL) is set: Azure-hosted
    endpoint inside the tenant's subscription, billed through the Azure Marketplace, no training on the data."""
    if llm_azure.enabled():
        return dict(name="Azure OpenAI (in-tenant)", resource=os.environ["AZURE_OPENAI_ENDPOINT"].split("//")[-1].strip("/"),
                    region=os.environ.get("AZURE_REGION", "uksouth"), training="Microsoft product terms: not used to train models, not shared with OpenAI, processed in-region")
    if os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL"):
        return dict(name="Microsoft Foundry", resource=os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL"),
                    region=os.environ.get("FOUNDRY_REGION", ""), training="no training on customer data (Anthropic commercial terms via Azure Marketplace)")
    return dict(name="Anthropic API (first-party)", resource="api.anthropic.com", region="", training="no training on API data by default (commercial terms)")


def client():
    global _client
    if _client is None:
        if provider()["name"] == "Microsoft Foundry":
            _client = anthropic.AnthropicFoundry()   # reads ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE / _BASE_URL
        else:
            _client = anthropic.Anthropic()
    return _client


def deployment(model: str) -> str:
    """On Foundry the `model` field is the deployment name; map via FOUNDRY_DEPLOYMENTS='claude-opus-5=atlas-opus,claude-sonnet-5=atlas-sonnet'."""
    m = dict(kv.split("=") for kv in os.environ.get("FOUNDRY_DEPLOYMENTS", "").split(",") if "=" in kv)
    return m.get(model, model) if provider()["name"] == "Microsoft Foundry" else model


def _cost(model, u) -> float:
    i, o = PRICES.get(model, (5.0, 25.0))
    return ((u.input_tokens or 0) * i + (u.output_tokens or 0) * o
            + (u.cache_creation_input_tokens or 0) * i * 1.25
            + (u.cache_read_input_tokens or 0) * i * 0.1) / 1e6


def _log(purpose, model, resp, ms, prompt_version=""):
    u = resp.usage
    rec = dict(ts=time.time(), purpose=purpose, model=model, ms=ms, prompt_version=prompt_version,
               provider="Microsoft Foundry" if provider()["name"] == "Microsoft Foundry" else "Anthropic API (first-party)",   # this branch never runs on Azure OpenAI
               input=u.input_tokens, output=u.output_tokens,
               cache_write=u.cache_creation_input_tokens or 0, cache_read=u.cache_read_input_tokens or 0,
               cost_usd=round(_cost(model, u), 6))
    store.append_line(LOG, rec)
    return rec


def complete(user: str, *, system: str = "", purpose: str, schema: Type[T] | None = None,
             cache_system: bool = True, model: str | None = None, effort: str | None = None,
             max_tokens: int | None = None):
    """Text or structured completion. System prompt is cached by default (stable prefix first)."""
    model = model or PARAMS["llm"]["model"]
    pv = hashlib.sha256((system or "").encode()).hexdigest()[:10]   # the prompt is part of the audit trail: which words produced this output
    kw = dict(model=deployment(model), max_tokens=max_tokens or PARAMS["llm"]["max_tokens"],
              output_config={"effort": effort or PARAMS["llm"]["effort"]},
              messages=[{"role": "user", "content": user}])
    if system:
        block = {"type": "text", "text": system}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        kw["system"] = [block]
    t0 = time.time()
    if llm_azure.enabled() and os.environ.get("AZURE_OPENAI_FOR", "answer").split(",").count(purpose.split(".")[0]):
        out, u = llm_azure.complete(user, system=system, schema=schema, max_tokens=kw["max_tokens"], effort=kw["output_config"]["effort"])   # the configured budget, not the raw arg: gpt-5 reasoning tokens count against it
        i, o = llm_azure.PRICES.get(u["model"], (0.25, 2.0))
        rec = dict(ts=time.time(), purpose=purpose, model=u["model"], ms=int((time.time() - t0) * 1000), provider=provider()["name"], prompt_version=pv,
                   input=u["input"], output=u["output"], cache_write=0, cache_read=u["cache_read"], cost_usd=round((u["input"] * i + u["output"] * o) / 1e6, 6))
        store.append_line(LOG, rec)
        return out
    if schema is not None:
        resp = client().messages.parse(output_format=schema, **kw)
        _log(purpose, model, resp, int((time.time() - t0) * 1000), pv)
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"refusal: {resp.stop_details}")
        return resp.parsed_output
    resp = client().messages.create(**kw)
    _log(purpose, model, resp, int((time.time() - t0) * 1000), pv)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refusal: {resp.stop_details}")
    return "".join(b.text for b in resp.content if b.type == "text")


def calls(purpose_prefix: str = "") -> list[dict]:
    return [r for r in store.read_lines(LOG) if r["purpose"].startswith(purpose_prefix)]
