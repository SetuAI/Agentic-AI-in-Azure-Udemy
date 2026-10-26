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

# json: turns the summary into text for the prompt.
import json

# Shared config.
from agent import context

# Calls Azure OpenAI.
from metadata import llm


# STEP 9 ---------------------------------------------------------------
def summarise(state: dict) -> dict:
    """Step 9."""
    # The query result from run_sql.
    columns, rows = state["result"]["columns"], state["result"]["rows"]
    # How many rows the model may see (20).
    n = context.agent_config()["summary_rows"]

    # Totals for number columns, when there is more than one row to add up.
    # column name -> total, filled in below.
    totals = {}
    # Only add up when there is more than one row.
    if len(rows) > 1:
        # i = column position.
        for i, column in enumerate(columns):
            # The column's number values (True/False count as numbers in Python, so they are excluded).
            values = [r[i] for r in rows if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)]
            # Total only columns where EVERY row holds a number.
            if values and len(values) == len(rows):
                # The total, rounded to 2 decimals.
                totals[column] = round(sum(values), 2)

    # Everything the model will see about the result.
    summary = {
        "row_count": len(rows),
        "columns": columns,
        # The first n rows, each as {column: value}.
        "first_rows": [dict(zip(columns, r)) for r in rows[:n]],
        "column_totals": totals,
        # True if there were more rows than shown.
        "truncated": len(rows) > n,
    }
    # Return the summary for LangGraph to merge.
    return {"summary": summary, "_detail": f"{len(rows):,} rows, {len(totals)} numeric totals"}


# The instructions given to the strong model in step 10.
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


# STEP 10 --------------------------------------------------------------
def write_commentary(state: dict) -> dict:
    """Step 10."""
    # The model gets the question, what the SQL calculates, and the summary as JSON
    # (default=str turns dates and decimals into text so json can write them).
    prompt = (f"Question: {state['understanding']['standalone_question']}\n"
              f"What the query calculates: {state.get('sql_explanation')}\n\n"
              f"Result summary:\n{json.dumps(state['summary'], indent=1, default=str)}")
    # Call the strong model; it replies with {"commentary": "..."}.
    answer = llm.chat_json("strong", COMMENTARY_PROMPT, prompt)
    # Store the answer and mark the question as completed.
    return {"commentary": answer.get("commentary", ""), "status": "completed", "_detail": "Commentary written"}


# STEP 11 --------------------------------------------------------------
def remember(state: dict) -> dict:
    """Step 11. Keep the last 6 turns: enough for follow-ups, small enough to stay cheap."""
    # Earlier turns plus this one (its full question, SQL and row count).
    history = (state.get("history") or []) + [{
        "question": state["understanding"]["standalone_question"],
        "sql": state["sql"],
        "row_count": state["summary"]["row_count"],
    }]
    # [-6:] keeps only the last 6 turns.
    return {"history": history[-6:], "_detail": f"{len(history[-6:])} turn(s) in this conversation"}