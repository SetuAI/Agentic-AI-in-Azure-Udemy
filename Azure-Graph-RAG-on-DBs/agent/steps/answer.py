##############################################################################
# agent/steps/answer.py
#
# PURPOSE:
#   The last three of the agent's eleven steps. They take the rows that
#   step 8 got from the database, and turn them into an answer a person
#   can read.
#
#     STEP 9   summarise          plain Python, NO model: count the rows,
#                                 add up the number columns, keep the first rows
#     STEP 10  write_commentary   the strong model explains the result in
#                                 3 to 5 plain sentences
# 3 : question , one sentence about what SQL calculates , summary
#     STEP 11  remember           save this question and its SQL, so a
#                                 follow-up question can build on it
#
# WHAT EACH STEP ADDS TO THE STATE:
#     step 9   ->  summary
#     step 10  ->  commentary, and status = "completed"
#     step 11  ->  history
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Step 8 hands back rows — maybe 12, maybe 5,000. A business user wants a
#   few sentences, not a table of numbers. But a model given 5,000 rows is
#   slow, expensive, and may invent numbers. So:
#     step 9 shrinks the result to a small summary, using plain Python
#     step 10 gives the model ONLY that summary, with strict rules
#
# HOW TO USE:
#   Never run directly. agent/graph.py calls the three steps in order,
#   after step 8.
#
# TRY IT YOURSELF — continue from Part 2 (from the project folder):
#       uv run python
#       >>> from pprint import pprint
#       >>> from agent.steps import understand as u, build_query as bq, answer as a
#       >>> state = {"question": "Which distributor has the highest transaction rejection rate?"}
#       >>> for step in (u.guard_input, u.understand, u.match_entities,
#       ...              bq.select_tables, bq.find_join_path, bq.write_sql,
#       ...              bq.check_sql, bq.run_sql,
#       ...              a.summarise, a.write_commentary, a.remember):
#       ...     state.update(step(state))
#       >>> pprint(state["summary"])
#       >>> print(state["commentary"])
#       >>> state["history"]
##############################################################################


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
# The module's own description, kept for help(answer).


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import json
# Turns the summary into JSON text, to put in the model's message.

from agent import context
# The shared settings (agent/context.py). Step 9 reads summary_rows from it.

from metadata import llm
# Talks to Azure OpenAI. Step 10 uses the strong model.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 9 — summarise
# ═════════════════════════════════════════════════════════════════════════════

def summarise(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 9."""
    # THE JOB: shrink the result into a small summary. This summary is ALL
    # the model will see of the result — never the full rows.
    #
    # WHY PLAIN PYTHON, NOT A MODEL: counting rows and adding up columns is
    # exact work. Python gets it right every time, instantly, for free. A
    # model might add wrongly. So the numbers are worked out here, and the
    # model in step 10 only describes them.

    columns, rows = state["result"]["columns"], state["result"]["rows"]
    # The result from step 8, split into its two parts:
    #   columns   e.g. ["dist_nm", "total_txns", "rejected", "rejection_rate_pct"]
    #   rows      e.g. [["Konkan Wealth Partners", 4210, 716, 17.01], ...]

    n = context.agent_config()["summary_rows"]
    # How many rows the model may see: 20, from agent.yaml.

    # Totals for number columns, when there is more than one row to add up.

    totals = {}
    # column name -> its total, filled in below.

    if len(rows) > 1:
        # Only add up when there's more than one row. With one row, the
        # "total" would just repeat the row.

        for i, column in enumerate(columns):
            # For each column. enumerate() gives its position (i) and name.

            values = [r[i] for r in rows if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)]
            # This column's values that are numbers.
            #
            #   isinstance(r[i], (int, float))   is it a whole or decimal number?
            #   not isinstance(r[i], bool)       but NOT True/False — in Python,
            #                                    True counts as the number 1, so
            #                                    it has to be left out by hand
            #
            # Money arrives as a number here, because step 8's _plain()
            # already turned Decimal into float.

            if values and len(values) == len(rows):
                # Only total a column where EVERY row holds a number. A text
                # column, or a number column with some empty values, is skipped.

                totals[column] = round(sum(values), 2)
                # The total, rounded to 2 decimals.

    # ⚠ A KNOWN LIMITATION OF THE TOTALS ABOVE — WORTH SHOWING STUDENTS:
    #   This adds up EVERY number column, without asking whether adding it up
    #   makes sense. For money and counts it does. For these it doesn't:
    #     a percentage column   14.67% + 13.66% + 260.86% = 289.19%  — meaningless
    #     a tier or ID column   tier 1 + 2 + 3 = 6                   — meaningless
    #   We saw this happen: the SIP cancellations answer reported "an
    #   aggregate percentage change of 289.19%". The real overall change was
    #   about +68.5%. The SQL was right and the table was right. The model
    #   was handed a meaningless total, and wrote it up confidently.
    #   A fix would be to skip columns whose names contain "pct", "rate",
    #   "_id" or "tier".

    summary = {
        # Everything the model will see about the result.

        "row_count": len(rows),
        # How many rows the query returned in total.

        "columns": columns,
        # The column names.

        "first_rows": [dict(zip(columns, r)) for r in rows[:n]],
        # The first 20 rows, each turned into a dictionary, e.g.
        #   {"dist_nm": "Konkan Wealth Partners", "rejection_rate_pct": 17.01, ...}
        #
        #   rows[:n]                  the first n rows
        #   zip(columns, r)           pairs each column name with its value
        #   dict(...)                 makes those pairs a dictionary
        #
        # A dictionary per row is easier for the model to read than a bare
        # list of values, because every number has its name next to it.

        "column_totals": totals,
        # The totals worked out above.

        "truncated": len(rows) > n,
        # True if there were more rows than the 20 shown. Step 10's prompt
        # tells the model: if this is True, don't claim to describe every row.
    }

    return {"summary": summary, "_detail": f"{len(rows):,} rows, {len(totals)} numeric totals"}
    # summary   for step 10, and for the demo page
    # _detail   a note for the steps list, e.g. "12 rows, 3 numeric totals"


# ═════════════════════════════════════════════════════════════════════════════
# STEP 10 — write_commentary
# ═════════════════════════════════════════════════════════════════════════════

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
# The instructions given to the STRONG model in step 10.
#
# EACH RULE, AND THE MISTAKE IT PREVENTS:
#   Lead with the answer
#       A business user wants "Konkan Wealth Partners has the highest rate",
#       not "The query returned 40 rows from the database ...".
#   Mention any filter or threshold
#       If the SQL only counted distributors with 100+ transactions, the
#       reader should know, or the answer could mislead them.
#   ONLY numbers from the summary
#       THE MOST IMPORTANT RULE. Stops the model inventing or estimating
#       figures. Every number in the answer must be checkable against the
#       table shown below it.
#   Lakh and crore
#       Indian readers think in lakh and crore, not millions. The rule gives
#       the exact conversion so the model doesn't get it wrong.
#   NEVER say one thing caused another
#       The data can show two things happened in the same month — a bond
#       downgrade and a jump in redemptions. It cannot prove one CAUSED the
#       other. Saying "because of" would claim more than the data shows.
#   An empty result
#       Say so plainly, rather than writing something that sounds like an answer.
#   Only the first rows shown
#       If the summary is truncated, don't describe rows the model never saw.
#
# The last line asks for JSON with one key: the explanation.


def write_commentary(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 10."""
    # THE JOB: turn the summary into 3 to 5 plain sentences.
    #
    # WHY THE STRONG MODEL: this is what the user actually reads. Clear,
    # careful wording is worth the better model here.

    # The model gets the question, what the SQL calculates, and the summary as JSON
    # (default=str turns dates and decimals into text so json can write them).

    prompt = (f"Question: {state['understanding']['standalone_question']}\n"
              # 1. The full question from step 2.

              f"What the query calculates: {state.get('sql_explanation')}\n\n"
              # 2. The one-sentence explanation step 6 wrote alongside the SQL,
              #    e.g. "Rejection rate per distributor, for distributors with
              #    at least 100 transactions". This is how the model knows
              #    about any filter or threshold, for rule 2.

              f"Result summary:\n{json.dumps(state['summary'], indent=1, default=str)}")
              # 3. The summary from step 9, as JSON text.
              #      indent=1      one item per line, easier for the model to read
              #      default=str   if anything can't be written as JSON, write
              #                    it as text instead of failing
    # Note what is NOT sent: the SQL itself, the table notes, and all but
    # the first 20 rows. The model only needs the result to explain it.

    answer = llm.chat_json("strong", COMMENTARY_PROMPT, prompt)
    # Send the rules and the message to the strong model.
    # The reply comes back as {"commentary": "Konkan Wealth Partners has ..."}

    return {"commentary": answer.get("commentary", ""), "status": "completed", "_detail": "Commentary written"}
    # commentary   the answer the user reads. If the model left the key out,
    #              an empty text rather than an error.
    # status       "completed" — the question has been answered. This is the
    #              ONLY place in the whole agent that sets status to completed.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 11 — remember
# ═════════════════════════════════════════════════════════════════════════════

def remember(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 11. Keep the last 6 turns: enough for follow-ups, small enough to stay cheap."""
    # THE JOB: save this question and its SQL, so the NEXT question in the
    # same conversation can build on it.
    #
    # HOW FOLLOW-UPS USE IT:
    #   question 1: "What were monthly net flows for Sahyadri Credit Risk Fund in 2024?"
    #   question 2: "Now show only March and April"
    # When question 2 arrives:
    #   step 2 sees question 1, and rewrites question 2 as a complete question
    #   step 6 sees question 1's SQL, and changes it instead of starting over
    #
    # WHERE IT'S KEPT: in the state, under "history". graph.py keeps the
    # state between questions in the same conversation (InMemorySaver), and
    # step 1 never clears "history" — it's the only thing that carries over.

    history = (state.get("history") or []) + [{
        # The earlier turns (or an empty list for the first question), plus
        # one new entry for this question:

        "question": state["understanding"]["standalone_question"],
        # The COMPLETE question — not what the user typed. "Now show only
        # March" on its own would be useless to the next follow-up.

        "sql": state["sql"],
        # The SQL that answered it.

        "row_count": state["summary"]["row_count"],
        # How many rows it returned.
    }]
    # Note what is NOT saved: the rows themselves, and the answer. Only the
    # question and the SQL. That keeps the memory small, and keeps any data
    # values out of it.

    return {"history": history[-6:], "_detail": f"{len(history[-6:])} turn(s) in this conversation"}
    # history[-6:] keeps only the LAST 6 turns.
    #
    # WHY 6: a follow-up almost always refers to the last question or two.
    # Steps 2 and 6 only show the model the MOST RECENT turn (history[-1]).
    # Keeping six is enough for that, and stops the history growing forever
    # in a long conversation.