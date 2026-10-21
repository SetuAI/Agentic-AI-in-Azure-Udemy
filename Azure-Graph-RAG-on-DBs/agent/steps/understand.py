"""
agent/steps/understand.py
=========================

STEPS 1 TO 3 OF THE AGENT
-------------------------
    guard_input      Prompt Shields checks the question for attacks.
    understand       The small model works out what is being asked: the
                     names in it, the dates, which approved metrics apply,
                     and, for a follow-up, the full question it stands for.
    match_entities   Each name is matched to an exact database value through
                     pgvector ("sahyadri credit fund" -> "Sahyadri Credit Risk Fund").

Each step receives the agent's state (a dict) and returns only the fields
it changes. LangGraph merges those into the state for the next step.
"""

import json

from agent import context
from guardrails import prompt_shields
from metadata import llm, retrieval

# The window the data covers. Given to the model so that "last month" or
# "latest" mean something definite.
DATA_START, DATA_END = "2021-09-01", "2026-08-31"


def guard_input(state: dict) -> dict:
    """Step 1. Also clears everything left over from the previous turn, keeping only history."""
    result = prompt_shields.check(state["question"], context.agent_config()["prompt_shields"]["fail_open"])
    fresh = {
        # Per-turn fields start empty for every new question.
        "status": None, "block_reason": None, "understanding": None, "entities": [],
        "unmatched": [], "tables": [], "table_picks": [], "join_path": None, "sql": None, "sql_tables": [], "sql_explanation": None,
        "checks": [], "attempts": 0, "failed_sql": [], "error": None, "result": None, "summary": None,
        "commentary": None, "prompt_shield": result, "_detail": result["note"],
    }
    if result["attack"]:
        fresh.update(status="blocked", block_reason="Prompt Shields flagged this question as a prompt attack. No SQL was written.")
    return fresh


UNDERSTAND_PROMPT = """You read questions about an Indian mutual fund company's database and work out
what is being asked, before any SQL is written. Reply with a JSON object with exactly these keys:

  "request_type":        "question"    -> a request to look at or analyse the data (almost everything)
                         "data_change" -> asks to delete, update, insert, create, drop or otherwise change data
                         "off_topic"   -> not about this company's data at all
  "standalone_question": the question rewritten to make sense on its own. If it is a follow-up
                         (e.g. "now only debt funds"), combine it with the previous question.
  "intent":              one sentence: what the user wants to know
  "entities":            names of specific things in the question, as a list of
                         {"text": the name as written, "kind": one of scheme, issuer, group, sector,
                         distributor, manager, category, city, benchmark}. Empty list if none.
  "time_range":          {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} or null if no period is implied
  "metrics":             names from the approved metrics list that this question needs (may be empty)
  "data_needed":         each separate kind of data the answer needs, as a short plain phrase (2 to 6
                         words, no names or dates), e.g. ["bond credit rating downgrades",
                         "bonds held by each scheme", "redemptions by scheme"]. Used to find tables.
  "needs_clarification": true ONLY if the question is impossible to answer as written, for example it
                         refers to "that fund" with no previous question to say which. Otherwise false.
  "clarification_question": the question to ask back, or null

Rules:
- "Why did X happen?" questions ARE answerable: answer with the data around the event (the figures
  for that period and the ones before it, and related activity such as redemptions). Do not ask for
  clarification because the cause cannot be proven; the commentary will describe timing, not cause.
- When a name or period is broad, assume the reasonable reading instead of asking.
- The data covers {start} to {end}. Treat "latest", "now" and "this month" as {end}, and
  "last month", "last year" relative to {end}."""


def understand(state: dict) -> dict:
    """Step 2. One call to the small model."""
    metric_list = "\n".join(f"  - {name}: {m['description']}" for name, m in context.metrics().items())
    history = state.get("history") or []
    previous = (f"Previous question: {history[-1]['question']}\nPrevious SQL:\n{history[-1]['sql']}\n\n"
                if history else "")
    answer = llm.chat_json(
        "small",
        UNDERSTAND_PROMPT.replace("{start}", DATA_START).replace("{end}", DATA_END),
        f"Approved metrics:\n{metric_list}\n\n{previous}Question: {state['question']}",
    )
    # Keep only metric names that really exist in metrics.yaml.
    answer["metrics"] = [m for m in answer.get("metrics", []) if m in context.metrics()]
    answer["data_needed"] = [d for d in answer.get("data_needed") or [] if isinstance(d, str) and d.strip()][:6]
    update = {"understanding": answer, "_detail": answer.get("intent", "")}

    # Requests to change data, or unrelated requests, stop here: no tables
    # are searched and no SQL is written. The SQL check (SELECT only) and
    # the READ ONLY database session are still there behind this, in case
    # one ever slips through.
    kind = answer.get("request_type", "question")
    if kind == "data_change":
        update.update(status="blocked", _detail="Request to change data",
                      block_reason="This assistant only reads data. Requests to delete, update or create "
                                   "data are refused, and no SQL was written.")
    elif kind == "off_topic":
        update.update(status="blocked", _detail="Not about this data",
                      block_reason="This question is not about the fund database, so it was not answered.")
    elif answer.get("needs_clarification"):
        update.update(status="needs_clarification")
    return update


def match_entities(state: dict) -> dict:
    """Step 3. Look each name up in its table's column, via pgvector."""
    config = context.agent_config()
    lookups = config["entity_columns"]
    matched, unmatched = [], []
    with context.agent_conn() as conn:
        for entity in state["understanding"].get("entities", []):
            if entity.get("kind") not in lookups:
                unmatched.append(entity)
                continue
            table, column = lookups[entity["kind"]]
            best = retrieval.match_values(conn, entity["text"], k=1, table=table, column=column)
            if best and best[0][3] >= config["entity_min_score"]:
                matched.append({"text": entity["text"], "kind": entity["kind"], "table": table,
                                "column": column, "value": best[0][2], "score": round(best[0][3], 3)})
            else:
                unmatched.append(entity)
    detail = "; ".join(f"'{m['text']}' -> {m['value']}" for m in matched) or "No names to match"
    return {"entities": matched, "unmatched": unmatched, "_detail": detail}


def describe_entities(entities: list[dict]) -> str:
    """Used by later steps' prompts."""
    return json.dumps([{k: e[k] for k in ("text", "table", "column", "value")} for e in entities], indent=1)
