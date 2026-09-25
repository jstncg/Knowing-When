"""One schema-constrained Claude call with no thinking and a bounded output.

Extraction (app.extraction) and verification (app.verify) share this wire
shape, so neither can spend its output budget on thinking and return nothing.
The older research call (providers.research) still makes its own request.
Docs checked 2026-09-22: structured outputs use output_config.format; Sonnet 5
and Opus 5 accept thinking {"type": "disabled"} at effort "low".
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from . import providers

SMALL_MODEL = "claude-sonnet-5"
_MESSAGES = "https://api.anthropic.com/v1/messages"


def output_schema(model: type[BaseModel]) -> dict:
    """Anthropic's grammar subset; full Pydantic constraints still run locally.

    Verified against platform.claude.com/docs/en/build-with-claude/structured-outputs
    on 2026-09-21. Unsupported length/numeric constraints become descriptions;
    this only adapts generation, never weakens acceptance validation.
    """
    unsupported = {
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "maxItems",
        "pattern",
    }

    def adapt(value):
        if isinstance(value, list):
            return [adapt(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: adapt(item) for key, item in value.items() if key not in unsupported
        }
        if result.get("type") == "object":
            result["additionalProperties"] = False  # required by the API for every object
        limits = [f"{key}={value[key]}" for key in unsupported if key in value]
        if limits:
            result["description"] = (
                value.get("description", "")
                + " Constraints: "
                + "; ".join(sorted(limits))
            ).strip()
        return result

    return adapt(model.model_json_schema())


def small_model(settings: dict) -> str:
    return settings.get("small_model") or SMALL_MODEL


def _user(content: dict, shared: dict | None, cache: bool = True):
    text = json.dumps(content, ensure_ascii=False)
    if shared is None:
        return text
    facts = json.dumps(shared, ensure_ascii=False)
    first = providers.cached(facts) if cache else {"type": "text", "text": facts}
    return [first, {"type": "text", "text": text}]


async def ask(
    settings: dict,
    *,
    system: str,
    content: dict,
    output: type[BaseModel],
    shared: dict | None = None,
    cache: bool = True,
    model: str | None = None,
    max_tokens: int = 1500,
    budget: providers.Budget | None = None,
) -> tuple[BaseModel, dict]:
    """Return the validated model output and the provider's usage block.

    `content` is serialized as the user turn; callers put untrusted text under
    named keys so the system prompt can say what is data. The system prompt
    is marked for caching (providers.cached); a prompt under the model's
    minimum is simply not cached. `shared` is data several calls repeat (one
    person's facts, asked about more than once): it goes before `content` and
    is marked too. cache=False marks neither.
    """
    if not settings.get("anthropic_key"):
        raise providers.ProviderError(
            "Add an Anthropic API key in Settings to run model steps."
        )
    payload = {
        "model": model or small_model(settings),
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": output_schema(output)},
        },
        "system": [providers.cached(system)] if cache else system,
        "messages": [{"role": "user", "content": _user(content, shared, cache)}],
    }
    data = await providers._post(
        _MESSAGES,
        settings["anthropic_key"],
        payload,
        "Anthropic",
        budget or providers.Budget(settings),
        settings.get("anthropic_workspace_id"),
    )
    if data.get("stop_reason") == "max_tokens":
        raise providers.ProviderError(
            f"{output.__name__} step reached its output limit; nothing was accepted."
        )
    if data.get("stop_reason") == "refusal":
        raise providers.ProviderError(
            f"{output.__name__} step was declined by the provider; nothing was accepted."
        )
    text = "".join(
        c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"
    )
    try:
        parsed = output.model_validate(json.loads(text))
    except (ValueError, TypeError, ValidationError):
        raise providers.ProviderError(
            f"{output.__name__} step returned an invalid object; nothing was accepted."
        ) from None
    return parsed, data.get("usage") or {}
