"""Azure OpenAI path: models and embeddings served from a resource in the tenant's own subscription and region
(UK South here). Data is processed under the Microsoft product terms: not used to train models, not shared with
OpenAI, stays in the region. Activated by env vars; AZURE_OPENAI_FOR lists the call purposes routed here
(answer,plan,qa,verify in the demo; detect stays on Claude, measured: gpt-5-mini lost eight points of redaction recall); the rest use llm.py's Claude path.
Plain REST on the retrying session (no SDK): the same code runs on a laptop, in a container and inside a Fabric notebook.
  AZURE_OPENAI_ENDPOINT=https://<account>.openai.azure.com/  AZURE_OPENAI_API_KEY=...  (or Entra via the credential chain)
  AZURE_OPENAI_CHAT=atlas-chat  AZURE_OPENAI_EMBED=atlas-embed  AZURE_REGION=uksouth"""
import json, os
from typing import Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
PRICES = {"gpt-5-mini": (0.25, 2.0), "gpt-4o-mini": (0.15, 0.6), "text-embedding-3-small": (0.02, 0.0)}
API = "2024-10-21"


def enabled() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_ENDPOINT"))


def _headers() -> dict:
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    if key:
        return {"api-key": key, "Content-Type": "application/json"}
    from .store import token
    return {"Authorization": f"Bearer {token('https://cognitiveservices.azure.com/.default')}", "Content-Type": "application/json"}


def _post(deployment: str, op: str, body: dict, timeout: int = 180) -> dict:
    from .store import http
    url = f"{os.environ['AZURE_OPENAI_ENDPOINT'].rstrip('/')}/openai/deployments/{deployment}/{op}?api-version={API}"
    r = http.post(url, headers=_headers(), json=body, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"azure openai {op}: {r.status_code} {r.text[:600]}")
    return r.json()


def complete(user: str, *, system: str = "", schema: Type[T] | None = None, max_tokens: int | None = None, effort: str | None = None):
    """Returns (parsed_or_text, usage_dict). Structured output via JSON schema when a pydantic model is given."""
    deployment = os.environ.get("AZURE_OPENAI_CHAT", "atlas-chat")
    body = {"messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}], "max_completion_tokens": max_tokens or 4000}
    reasoning = effort or os.environ.get("AZURE_OPENAI_REASONING", "low")   # gpt-5 family: minimal | low | medium | high; the call's effort wins
    if reasoning != "none":
        body["reasoning_effort"] = reasoning
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}}
    r = _post(deployment, "chat/completions", body)
    choice = r["choices"][0]; text = choice["message"].get("content") or ""
    if not text and choice.get("finish_reason") == "length":
        raise RuntimeError(f"azure completion hit max_completion_tokens={body['max_completion_tokens']} before any output (reasoning tokens count); raise max_tokens")
    if choice.get("finish_reason") == "content_filter" or (not text and "content_filter" in json.dumps(r)):
        raise RuntimeError("content_filter: the provider's content filter blocked the request")
    u = r.get("usage", {})
    usage = dict(input=u.get("prompt_tokens", 0), output=u.get("completion_tokens", 0), cache_read=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0, model=r.get("model", deployment))
    return (_validate(schema, text) if schema is not None else text), usage


def _validate(schema, text: str):
    """Non-strict JSON mode lets the model omit a field it considers empty; fill each omission with the empty value of its type
    (the schemas mean '' or [] as unknown) and validate again, so one skipped key does not degrade a whole document."""
    from pydantic import ValidationError
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        data = json.loads(text); missing = [x for x in e.errors() if x["type"] == "missing"]
        if not missing: raise
        for err in missing:
            node = data
            for k in err["loc"][:-1]: node = node[k]
            node[err["loc"][-1]] = _empty(schema, err["loc"])
        return schema.model_validate(data)


def _empty(schema, loc):
    """Walk the pydantic model to the field at loc and return the empty value of its annotation."""
    model = schema
    for k in loc[:-1]:
        if isinstance(k, int): continue
        ann = model.model_fields[k].annotation
        model = getattr(ann, "__args__", (ann,))[0] if not hasattr(ann, "model_fields") else ann
    ann = model.model_fields[loc[-1]].annotation
    origin = getattr(ann, "__origin__", ann)
    return [] if origin is list else (0.0 if ann is float else (0 if ann is int else (False if ann is bool else "")))


def embed(texts: list[str]) -> list[list[float]]:
    deployment = os.environ.get("AZURE_OPENAI_EMBED", "atlas-embed")
    out = []
    for i in range(0, len(texts), 64):
        r = _post(deployment, "embeddings", {"input": texts[i:i + 64]})
        out += [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
    return out
