"""
agent/steps/answer.py
=====================

STEPS 9 TO 11 OF THE AGENT
--------------------------
    summarise         Plain Python, no model: row count, column totals and
                      the first rows. This is ALL the model sees of the result
                      (tradeoff 7), however many rows the query returned.
    write_commentary  The strong model explains the result in plain English,
                      using only numbers from the summary, and never claiming
                      one thing caused another.
    remember          Saves this turn (question and SQL) so a follow-up like
                      "now only debt funds" can build on it.
"""

import json

from agent import context
from metadata import llm


def summarise(state: dict) -> dict:
    """Step 9."""
    columns, rows = state["result"]["columns"], state["result"]["rows"]
    n = context.agent_config()["summary_rows"]

    # Totals for number columns, when there is more than one row to add up.
    totals = {}
    if len(rows) > 1:
        for i, column in enumerate(columns):
            values = [r[i] for r in rows if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)]
            if values and len(values) == len(rows):
                totals[column] = round(sum(values), 2)

    summary = {
        "row_count": len(rows),
        "columns": columns,
        "first_rows": [dict(zip(columns, r)) for r in rows[:n]],
        "column_totals": totals,
        "truncated": len(rows) > n,
    }
    return {"summary": summary, "_detail": f"{len(rows):,} rows, {len(totals)} numeric totals"}


COMMENTARY_PROMPT = """You explain a database query result to a business user at an Indian mutual fund
company, in 3 to 5 plain sentences, as an analyst would. Rules:

- Lead with the answer itself. Do not talk about "the query" or "the database".
- If the calculation applied a filter or threshold (e.g. at least 100 transactions), mention it briefly
  as context.
- Use ONLY numbers that appear in the result summary. Never invent or estimate figures.
- You may write rupee amounts in lakh or crore (1 lakh = 100,000; 1 crore = 10,000,000), rounded sensibly.
- Never say one thing caused another. The data shows timing, not cause: write "in the same month as",
  "at the same time as" or "alongside", never "because of" or "due to".
- If the result is empty, say so plainly and suggest what might be checked.
- If only the first rows are shown, do not claim to describe every row.

Reply with JSON: {"commentary": "<your explanation>"}"""


def write_commentary(state: dict) -> dict:
    """Step 10."""
    prompt = (f"Question: {state['understanding']['standalone_question']}\n"
              f"What the query calculates: {state.get('sql_explanation')}\n\n"
              f"Result summary:\n{json.dumps(state['summary'], indent=1, default=str)}")
    answer = llm.chat_json("strong", COMMENTARY_PROMPT, prompt)
    return {"commentary": answer.get("commentary", ""), "status": "completed", "_detail": "Commentary written"}


def remember(state: dict) -> dict:
    """Step 11. Keep the last 6 turns: enough for follow-ups, small enough to stay cheap."""
    history = (state.get("history") or []) + [{
        "question": state["understanding"]["standalone_question"],
        "sql": state["sql"],
        "row_count": state["summary"]["row_count"],
    }]
    return {"history": history[-6:], "_detail": f"{len(history[-6:])} turn(s) in this conversation"}