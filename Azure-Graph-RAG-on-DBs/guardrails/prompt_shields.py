"""
guardrails/prompt_shields.py
============================

WHAT THIS FILE IS FOR
---------------------
Checks a user's question with Azure AI Content Safety's Prompt Shields
BEFORE anything else happens. Prompt Shields detects attempts to override
the AI's instructions ("ignore your rules", "you are now in developer
mode", ...). A flagged question is stopped: no model sees it, no SQL is written.

This is the first step of the agent (guard_input). The same request is
tested on its own in tests/test_content_safety.py.

WHAT IT DOES NOT CATCH
----------------------
A plain request to change data, e.g. "delete the transactions table", is
not an instruction override, so Prompt Shields may let it through. That is
caught later, twice: the SQL-writing model is told to refuse, and the SQL
check allows SELECT statements only.
"""

# json: builds the request body and reads the reply.
import json

# os: reads CONTENT_SAFETY_ENDPOINT and CONTENT_SAFETY_KEY.
import os

# urllib.request: Python's built-in way to make an HTTP call (no extra package needed).
import urllib.request

# The Content Safety REST API version this code was written for.
API_VERSION = "2024-09-01"


def check(question: str, fail_open: bool) -> dict:
    """
    Returns {"attack": bool, "checked": bool, "note": str}.
    checked is False when Content Safety could not be reached; then attack
    follows fail_open (see config/agent.yaml).
    """
    # The Content Safety resource's URL (None if not set).
    endpoint = os.environ.get("CONTENT_SAFETY_ENDPOINT")
    # Its key (None if not set).
    key = os.environ.get("CONTENT_SAFETY_KEY")
    # Not configured: block or allow according to fail_open, and say why.
    if not endpoint or not key:
        return {"attack": not fail_open, "checked": False, "note": "Content Safety not configured"}

    # Build the HTTP request: URL, JSON body, POST method and headers (the key goes in a header).
    request = urllib.request.Request(
        endpoint.rstrip("/") + f"/contentsafety/text:shieldPrompt?api-version={API_VERSION}",
        data=json.dumps({"userPrompt": question, "documents": []}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": key},
    )
    # Anything that goes wrong inside "try" jumps to "except" below.
    try:
        # Send it; give up after 10 seconds.
        with urllib.request.urlopen(request, timeout=10) as response:
            # Read the JSON reply and take out the true/false verdict.
            attack = json.load(response)["userPromptAnalysis"]["attackDetected"]
        # Return the verdict and a note for the steps list.
        return {"attack": bool(attack), "checked": True,
                "note": "Prompt attack detected" if attack else "No prompt attack detected"}
    # Could not check: block or allow according to fail_open.
    except Exception as error:   # network failure, timeout, bad key, ...
        return {"attack": not fail_open, "checked": False, "note": f"Check failed: {error}"}