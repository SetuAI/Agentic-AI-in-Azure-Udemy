"""
agent/steps/build_query.py
==========================

STEPS 4 TO 8 OF THE AGENT
-------------------------
    select_tables    Which tables: vector search on the table notes, plus the
                     tables the matched names and approved metrics need.
    find_join_path   How to join them: a route through the AGE graph.
    write_sql        The strong model writes one SELECT, using only those
                     tables, those joins, the exact names and the metric rules.
    check_sql        sqlglot checks (read only, known tables, date filter)
                     and Postgres's EXPLAIN cost estimate.
    run_sql          Runs it read-only, with a timeout and a row cap.

If check_sql or run_sql fails, the agent goes back to write_sql with the
error, at most max_sql_retries times (config/agent.yaml).
"""

# json: writes the time range into the prompt.
import json

# Date types, so _plain() can recognise them.
from datetime import date, datetime

# Postgres "numeric" values arrive as Decimal; _plain() turns them into floats.
from decimal import Decimal

# Shared config, notes and connections.
from agent import context

# Writes the matched names as JSON for the prompt.
from agent.steps.understand import describe_entities

# The structural SQL checks (read only, known tables, date filter).
from guardrails import sql_checks

# llm: calls Azure OpenAI. retrieval: table search and join paths.
from metadata import llm, retrieval


# STEP 4 ---------------------------------------------------------------
def select_tables(state: dict) -> dict:
    """Step 4."""
    # The agent settings.
    config = context.agent_config()
    # The full question (follow-ups already combined with the previous one).
    question = state["understanding"]["standalone_question"]
    # Each pick records why the table is there, for the demo page's trace.
    picks = [{"table": e["table"], "source": "entity", "reason": e["text"]} for e in state["entities"]]
    # Every approved metric the question needs adds its listed tables.
    for name in state["understanding"]["metrics"]:
        picks += [{"table": t, "source": "metric", "reason": name} for t in context.metrics()[name]["tables"]]
    # If approved metrics or matched names already say which tables are
    # needed, search only adds a couple more. Otherwise search does the work.
    # How many tables to take from the vector search.
    k = 2 if picks else config["search_top_k"]
    # Open a connection to agent_meta (table embeddings live there).
    with context.agent_conn() as conn:
        # The k tables whose notes are closest in meaning to the question: [(name, score), ...].
        found = retrieval.search_tables(conn, question, k=k)
        # Searching the whole question blurs its parts together: in "which schemes hold
        # downgraded bonds, and their redemptions", the ratings table ranked only 5th.
        # Each kind of data the question needs, searched on its own, finds its table
        # clearly, so the best table for each one is added too.
        for phrase in state["understanding"].get("data_needed", []):
            # The single best table for this phrase.
            best = retrieval.search_tables(conn, phrase, k=1)
            # Add it only if its score is high enough.
            if best and best[0][1] >= config["phrase_min_score"]:
                picks.append({"table": best[0][0], "source": "need", "reason": phrase, "score": round(best[0][1], 3)})
    # Search results come last.
    picks += [{"table": name, "source": "search", "score": round(score, 3)} for name, score in found]
    # Tables the matched names and metrics need come first: they are
    # certain. Search results fill in the rest.
    # dict.fromkeys removes duplicates while keeping the first-seen order.
    tables = list(dict.fromkeys(p["table"] for p in picks))
    # Return the chosen tables and why each was picked.
    return {"tables": tables, "table_picks": _one_per_table(picks), "_detail": ", ".join(tables)}


def _one_per_table(picks: list[dict]) -> list[dict]:
    """
    One pick per table, in first-picked order. A table needed by several
    names or metrics keeps the first source, with every reason joined, and
    its search score if search also found it.
    """
    # table name -> its merged pick.
    merged = {}
    # Go through the picks in order.
    for p in picks:
        # setdefault: get the table's entry, creating it (with this first source) if new.
        m = merged.setdefault(p["table"], {"table": p["table"], "source": p["source"], "reasons": []})
        # Collect every different reason.
        if p.get("reason") and p["reason"] not in m["reasons"]:
            m["reasons"].append(p["reason"])
        # Keep the search score, if any.
        if "score" in p:
            m["score"] = p["score"]
    # Turn the reasons list into one text ("**m" copies the other keys).
    return [{**m, "reason": ", ".join(m.pop("reasons"))} for m in merged.values()]


# STEP 5 ---------------------------------------------------------------
def find_join_path(state: dict) -> dict:
    """Step 5."""
    # The AGE graph's name ("schema_graph" unless agent.yaml says otherwise).
    graph = context.agent_config().get("graph_name", "schema_graph")
    with context.agent_conn() as conn:
        # try/except: if find_join_path raises a ValueError, run the except block instead of crashing.
        try:
            # The shortest set of joins connecting all selected tables.
            path = retrieval.find_join_path(conn, state["tables"], graph=graph)
        except ValueError as error:
            # Some table could not be connected. Carry on with what can be
            # joined; the SQL model is told which joins are available.
            path = {"tables": state["tables"], "joins": [], "note": str(error)}
    # Each join as readable text, e.g. "transactions.plan_id = scheme_plans.plan_id".
    joins = [f"{j['from_table']}.{j['from_column']} = {j['to_table']}.{j['to_column']}" for j in path["joins"]]
    # Return the join path for write_sql.
    return {"join_path": path, "_detail": "; ".join(joins) or "No joins needed"}


# The instructions given to the strong model in step 6.
SQL_PROMPT = """You write one PostgreSQL 17 query that answers a business question about an Indian mutual
fund company's database. Rules:

1. Exactly one SELECT statement (WITH ... SELECT is fine). Never modify data.
   If the question asks to change, delete or create anything, or is not a question about this data,
   reply {"sql": null, "refusal": "<short reason>"}.
2. Use ONLY the tables and columns listed below, and join them ONLY with the join conditions given.
3. For named things, filter with the exact database values given (e.g. s.scheme_name = 'exact value').
4. Any query on transactions MUST filter transactions.txn_date with a range, e.g.
   t.txn_date >= '2024-03-01' AND t.txn_date < '2024-04-01'. If the question names no period,
   use the full data window: t.txn_date BETWEEN '2021-09-01' AND '2026-08-31'.
   "txn_date IS NOT NULL" is not a date filter.
5. Where an approved metric definition is given, calculate it exactly that way.
6. Do not add filters the question did not ask for (e.g. only active distributors).
7. Aggregate in SQL and return a compact result (normally under 50 rows), sorted sensibly,
   with readable column aliases. Round money to 2 decimals and percentages to 2 decimals.

Reply with JSON: {"sql": "<the query>", "explanation": "<one sentence on what it calculates>"}"""


def _schema_text(tables: list[str]) -> str:
    """The approved notes for the given tables, written out for the model."""
    # All approved notes (cached).
    notes = context.table_notes()
    # One text block per table.
    parts = []
    # For each table on the join path:
    for name in tables:
        # Skip tables without approved notes.
        if name not in notes:
            continue
        # This table's notes.
        note = notes[name]
        # One line per column: name (type) [personal data]: description.
        columns = "\n".join(f"    {c} ({t}){' [personal data]' if pii else ''}: {d}"
                            for c, t, d, pii in note["columns"])
        # The table's description, its grain, then its columns.
        parts.append(f"Table {name}: {note['description']} {note['grain']}.\n{columns}")
    # All tables, separated by blank lines.
    return "\n\n".join(parts)


# STEP 6 ---------------------------------------------------------------
def write_sql(state: dict) -> dict:
    """Step 6. Also used for retries: the previous attempt and its error are included."""
    # What step 2 worked out.
    understanding = state["understanding"]
    # What step 5 found.
    path = state["join_path"]
    # The join conditions as text (or a note that there are none).
    joins = "\n".join(f"  {j['from_table']}.{j['from_column']} = {j['to_table']}.{j['to_column']}"
                      for j in path["joins"]) or "  (single table, no joins)"
    # The approved definition of every metric the question needs.
    metric_text = "\n".join(f"  {m}: {context.metrics()[m]['definition']}" for m in understanding["metrics"]) or "  (none)"
    # Earlier turns of this conversation.
    history = state.get("history") or []

    # The parts of the message sent to the model, joined with blank lines below.
    prompt = [
        f"Question: {understanding['standalone_question']}",
        f"Time range: {json.dumps(understanding.get('time_range'))}",
        f"Exact values for names in the question:\n{describe_entities(state['entities'])}",
        f"Approved metric definitions:\n{metric_text}",
        f"Join conditions:\n{joins}",
        f"Tables:\n{_schema_text(path['tables'])}",
    ]
    # A follow-up: show the previous SQL so the model can refine it.
    if history:
        prompt.append(f"Previous question's SQL (the user may be refining it):\n{history[-1]['sql']}")
    # A retry: show the failed SQL and what was wrong with it.
    if state.get("error"):
        prompt.append(f"Your previous SQL failed. Fix it.\nPrevious SQL:\n{state['sql']}\nProblem: {state['error']}")

    # Call the strong model; it replies with {"sql": ..., "explanation": ...}.
    answer = llm.chat_json("strong", SQL_PROMPT, "\n\n".join(prompt))
    # Count this attempt.
    attempts = state.get("attempts", 0) + 1
    # No SQL means the model refused: stop with the reason it gave.
    if not answer.get("sql"):
        return {"status": "blocked", "attempts": attempts, "sql": None,
                "block_reason": f"Refused: {answer.get('refusal', 'not a read-only question about this data')}",
                "_detail": "Model refused to write SQL"}
    # Store the SQL (trimmed, without a trailing ";") and clear any previous error.
    return {"sql": answer["sql"].strip().rstrip(";"), "sql_explanation": answer.get("explanation"),
            "attempts": attempts, "error": None, "_detail": answer.get("explanation", "")}


# STEP 7 ---------------------------------------------------------------
def check_sql(state: dict) -> dict:
    """Step 7."""
    # The agent settings.
    config = context.agent_config()
    # Run the structural checks; the known tables are the ones with approved notes.
    checks = sql_checks.check(state["sql"], set(context.table_notes()), config["date_filter_required"])

    if all(c["passed"] for c in checks):
        # Only a query that passed every structural check is shown to
        # EXPLAIN. EXPLAIN plans the query without running it.
        try:
            # Postgres's estimated cost and row count.
            cost = context.adapter().plan_estimate(state["sql"])
            # Within the cost limit?
            ok = cost["total_cost"] <= config["run"]["max_plan_cost"]
            # Add the cost check's result, e.g. "Estimated cost 12,345, ~40 rows".
            checks.append({"check": "plan_cost", "passed": ok,
                           "detail": f"Estimated cost {cost['total_cost']:,.0f}, ~{cost['rows']:,} rows"
                                     + ("" if ok else f" (limit {config['run']['max_plan_cost']:,})")})
        # EXPLAIN itself failed (e.g. a wrong column name): count it as a failed check.
        except Exception as error:
            checks.append({"check": "plan_cost", "passed": False, "detail": f"EXPLAIN failed: {error}"})

    # The checks that failed.
    failed = [c for c in checks if not c["passed"]]
    # What this step adds to the state, including the tables the SQL really reads.
    update = {"checks": checks, "sql_tables": sql_checks.tables_in(state["sql"]),
              "_detail": "All checks passed" if not failed else failed[0]["detail"]}
    # Any failure sets "error", so graph.py retries or gives up.
    if failed:
        update["error"] = "; ".join(c["detail"] for c in failed)
        update["failed_sql"] = _failed(state, "check_sql", update["error"])
    # Return the changes for LangGraph to merge.
    return update


def _failed(state: dict, stage: str, error: str) -> list[dict]:
    """The failed attempts so far, plus this one (shown on the demo page)."""
    # Earlier failures plus a new entry for this one.
    return (state.get("failed_sql") or []) + [
        {"attempt": state["attempts"], "stage": stage, "sql": state["sql"], "error": error}]


# STEP 8 ---------------------------------------------------------------
def run_sql(state: dict) -> dict:
    """Step 8."""
    # The run limits: timeout_ms, row_cap.
    run = context.agent_config()["run"]
    try:
        # Run the query read-only, with the time limit and the row cap.
        columns, rows = context.adapter().run_readonly(state["sql"], timeout_ms=run["timeout_ms"], row_cap=run["row_cap"])
    except Exception as error:
        # The query failed: record the error so graph.py retries or gives up.
        return {"error": f"Query failed: {error}", "failed_sql": _failed(state, "run_sql", f"Query failed: {error}"),
                "_detail": f"Failed: {str(error)[:120]}"}
    # Store the result, with every value made JSON-friendly.
    return {"result": {"columns": columns, "rows": [[_plain(v) for v in row] for row in rows]},
            "_detail": f"{len(rows):,} rows returned"}


def _plain(value):
    """
    Turn database values into plain JSON-friendly ones: Decimal -> float,
    dates -> "YYYY-MM-DD" text. The result travels to the web page as JSON,
    and is stored in the agent's memory, both of which need plain values.
    """
    # Exact decimal -> ordinary float.
    if isinstance(value, Decimal):
        return float(value)
    # Date -> "YYYY-MM-DD" text.
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    # Anything else is already plain.
    return value