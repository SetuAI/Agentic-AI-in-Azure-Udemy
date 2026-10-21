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

import json
import os
import urllib.request

API_VERSION = "2024-09-01"


def check(question: str, fail_open: bool) -> dict:
    """
    Returns {"attack": bool, "checked": bool, "note": str}.
    checked is False when Content Safety could not be reached; then attack
    follows fail_open (see config/agent.yaml).
    """
    endpoint = os.environ.get("CONTENT_SAFETY_ENDPOINT")
    key = os.environ.get("CONTENT_SAFETY_KEY")
    if not endpoint or not key:
        return {"attack": not fail_open, "checked": False, "note": "Content Safety not configured"}

    request = urllib.request.Request(
        endpoint.rstrip("/") + f"/contentsafety/text:shieldPrompt?api-version={API_VERSION}",
        data=json.dumps({"userPrompt": question, "documents": []}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            attack = json.load(response)["userPromptAnalysis"]["attackDetected"]
        return {"attack": bool(attack), "checked": True,
                "note": "Prompt attack detected" if attack else "No prompt attack detected"}
    except Exception as error:   # network failure, timeout, bad key, ...
        return {"attack": not fail_open, "checked": False, "note": f"Check failed: {error}"}