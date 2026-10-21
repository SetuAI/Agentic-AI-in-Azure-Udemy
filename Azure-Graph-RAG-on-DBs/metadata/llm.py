"""
metadata/llm.py
===============

WHAT THIS FILE IS FOR
---------------------
The one place that sends requests to our Azure OpenAI models. Other files
call two functions:

    chat_json(role, system, user) -> ask a chat model, get back a Python dict
    embed(texts)                  -> turn a list of texts into lists of 1,536 numbers

"role" is "small" or "strong". Which deployment that means is read from
config/models.yaml, so no other file ever names a model.

The connection works exactly like tests/test_azure_openai.py, which already
proved the endpoint and key are correct.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

import openai
import yaml
from dotenv import load_dotenv

MODELS_FILE = Path(__file__).resolve().parents[1] / "config" / "models.yaml"


@lru_cache(maxsize=1)
def _settings() -> dict:
    """Read config/models.yaml once, then reuse it. lru_cache remembers the result."""
    with open(MODELS_FILE) as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def _client() -> openai.OpenAI:
    """Create the connection to Azure OpenAI once, then reuse it."""
    load_dotenv()
    client = openai.OpenAI(
        base_url=os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1/",
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        # If Azure answers "too many requests" (our tokens-per-minute limit),
        # the client waits and tries again, up to 6 times, instead of failing.
        max_retries=6,
    )
    # With LANGSMITH_TRACING=true, wrap the client so every model call (its
    # prompt, reply, tokens and time) also appears in LangSmith, inside the
    # agent step that made it. If wrapping fails for any reason, carry on
    # with the plain client: tracing must never stop the agent working.
    if os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
        try:
            from langsmith.wrappers import wrap_openai
            client = wrap_openai(client)
        except Exception as error:
            print(f"LangSmith wrapping skipped: {error}")
    return client


def chat_json(role: str, system: str, user: str) -> dict:
    """
    Ask a chat model a question and get its answer as a Python dict.

    response_format={"type": "json_object"} makes the model reply with valid
    JSON only, with no extra text around it. The system message must mention
    JSON for this setting to be accepted.
    """
    settings = _settings()
    request = {
        "model": settings["chat"][role],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    effort = settings.get("reasoning_effort")
    try:
        reply = _client().chat.completions.create(**request, reasoning_effort=effort) if effort \
            else _client().chat.completions.create(**request)
    except openai.BadRequestError as error:
        # If this deployment does not accept reasoning_effort, retry once
        # without it rather than failing the whole pipeline.
        if "reasoning" not in str(error).lower():
            raise
        reply = _client().chat.completions.create(**request)

    text = reply.choices[0].message.content.strip()
    # Belt and braces: if the model wrapped its JSON in ```json fences anyway,
    # remove them before parsing.
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def embed(texts: list[str]) -> list[list[float]]:
    """
    Turn each text into its embedding (1,536 numbers).
    Several texts are sent in one request, which is faster and cheaper than
    one request per text.
    """
    result = _client().embeddings.create(model=_settings()["embedding"], input=texts)
    # Azure returns them in the same order as sent; sort by index to be sure.
    return [item.embedding for item in sorted(result.data, key=lambda d: d.index)]