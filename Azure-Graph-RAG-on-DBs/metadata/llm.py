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

# json: turns the model's JSON reply text into a Python dict.
import json

# os: reads the endpoint, key and LANGSMITH_TRACING settings.
import os

# lru_cache: create the client and read the settings only once.
from functools import lru_cache

# Path: builds the path to config/models.yaml.
from pathlib import Path

# The official OpenAI client library; it also works with Azure OpenAI.
import openai

# yaml: reads config/models.yaml.
import yaml

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# metadata/llm.py -> project folder -> config/models.yaml.
MODELS_FILE = Path(__file__).resolve().parents[1] / "config" / "models.yaml"


# Read once, then reuse.
@lru_cache(maxsize=1)
# The contents of config/models.yaml.
def _settings() -> dict:
    """Read config/models.yaml once, then reuse it. lru_cache remembers the result."""
    # Open the file (closed automatically afterwards) ...
    with open(MODELS_FILE) as f:
        # ... and return its contents as a dict.
        return yaml.safe_load(f)


# Create once, then reuse.
@lru_cache(maxsize=1)
# The client used for every chat and embedding call.
def _client() -> openai.OpenAI:
    """Create the connection to Azure OpenAI once, then reuse it."""
    # Make sure .env has been read.
    load_dotenv()
    # Azure OpenAI's "v1" endpoint accepts the standard OpenAI client:
    # base_url = your endpoint + "/openai/v1/", api_key = your Azure key.
    client = openai.OpenAI(
        # Your endpoint + "/openai/v1/".
        base_url=os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1/",
        # Your Azure OpenAI key.
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
        # Try to wrap; any error jumps to "except".
        try:
            # Imported only when tracing is on.
            from langsmith.wrappers import wrap_openai
            # Same client, but every call is also sent to LangSmith.
            client = wrap_openai(client)
        # Wrapping failed: print why and use the plain client.
        except Exception as error:
            # Say why, in the log.
            print(f"LangSmith wrapping skipped: {error}")
    # The (possibly wrapped) client, cached for every later call.
    return client


# role = "small" or "strong"; system = instructions; user = the message.
def chat_json(role: str, system: str, user: str) -> dict:
    """
    Ask a chat model a question and get its answer as a Python dict.

    response_format={"type": "json_object"} makes the model reply with valid
    JSON only, with no extra text around it. The system message must mention
    JSON for this setting to be accepted.
    """
    # The contents of config/models.yaml.
    settings = _settings()
    # The request: which deployment (from the role), the two messages, and "reply in JSON".
    request = {
        # The deployment name for this role, from models.yaml.
        "model": settings["chat"][role],
        # The instructions (system) and the message (user).
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        # Reply with JSON only.
        "response_format": {"type": "json_object"},
    }
    # e.g. "low" (None if not set in models.yaml).
    effort = settings.get("reasoning_effort")
    # Try the request; if Azure rejects it, the "except" below handles it.
    try:
        # Send the request ("**request" spreads the dict into named arguments), adding
        # reasoning_effort only if one is set. The "\" continues the line onto the next.
        reply = _client().chat.completions.create(**request, reasoning_effort=effort) if effort \
            else _client().chat.completions.create(**request)
    # Azure rejected the request (HTTP 400).
    except openai.BadRequestError as error:
        # If this deployment does not accept reasoning_effort, retry once
        # without it rather than failing the whole pipeline.
        if "reasoning" not in str(error).lower():
            # A different problem: re-raise the error so the caller sees it.
            raise
        # Try once more without reasoning_effort.
        reply = _client().chat.completions.create(**request)

    # The model's reply text, without surrounding spaces.
    text = reply.choices[0].message.content.strip()
    # Belt and braces: if the model wrapped its JSON in ```json fences anyway,
    # remove them before parsing.
    if text.startswith("```"):
        # Strip the backticks and the word "json".
        text = text.strip("`").removeprefix("json").strip()
    # Parse the JSON text into a Python dict.
    return json.loads(text)


# Used by publish.py (to store vectors) and retrieval.py (to search).
def embed(texts: list[str]) -> list[list[float]]:
    """
    Turn each text into its embedding (1,536 numbers).
    Several texts are sent in one request, which is faster and cheaper than
    one request per text.
    """
    # One request for all the texts, using the embedding deployment from models.yaml.
    result = _client().embeddings.create(model=_settings()["embedding"], input=texts)
    # Azure returns them in the same order as sent; sort by index to be sure.
    return [item.embedding for item in sorted(result.data, key=lambda d: d.index)]