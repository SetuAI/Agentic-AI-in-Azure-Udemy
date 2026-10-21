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

import json
from datetime import date, datetime
from decimal import Decimal

from agent import context
from agent.steps.understand import describe_entities
from guardrails import sql_checks
from metadata import llm, retrieval


def select_tables(state: dict) -> dict:
    """Step 4."""
    config = context.agent_config()
    question = state["understanding"]["standalone_question"]
    # Each pick records why the table is there, for the demo page's trace.
    picks = [{"table": e["table"], "source": "entity", "reason": e["text"]} for e in state["entities"]]
    for name in state["understanding"]["metrics"]:
        picks += [{"table": t, "source": "metric", "reason": name} for t in context.metrics()[name]["tables"]]
    # If approved metrics or matched names already say which tables are
    # needed, search only adds a couple more. Otherwise search does the work.
    k = 2 if picks else config["search_top_k"]
    with context.agent_conn() as conn:
        found = retrieval.search_tables(conn, question, k=k)
        # Searching the whole question blurs its parts together: in "which schemes hold
        # downgraded bonds, and their redemptions", the ratings table ranked only 5th.
        # Each kind of data the question needs, searched on its own, finds its table
        # clearly, so the best table for each one is added too.
        for phrase in state["understanding"].get("data_needed", []):
            best = retrieval.search_tables(conn, phrase, k=1)
            if best and best[0][1] >= config["phrase_min_score"]:
                picks.append({"table": best[0][0], "source": "need", "reason": phrase, "score": round(best[0][1], 3)})
    picks += [{"table": name, "source": "search", "score": round(score, 3)} for name, score in found]
    # Tables the matched names and metrics need come first: they are
    # certain. Search results fill in the rest.
    tables = list(dict.fromkeys(p["table"] for p in picks))
    return {"tables": tables, "table_picks": _one_per_table(picks), "_detail": ", ".join(tables)}


def _one_per_table(picks: list[dict]) -> list[dict]:
    """
    One pick per table, in first-picked order. A table needed by several
    names or metrics keeps the first source, with every reason joined, and
    its search score if search also found it.
    """
    merged = {}
    for p in picks:
        m = merged.setdefault(p["table"], {"table": p["table"], "source": p["source"], "reasons": []})
        if p.get("reason") and p["reason"] not in m["reasons"]:
            m["reasons"].append(p["reason"])
        if "score" in p:
            m["score"] = p["score"]
    return [{**m, "reason": ", ".join(m.pop("reasons"))} for m in merged.values()]


def find_join_path(state: dict) -> dict:
    """Step 5."""
    graph = context.agent_config().get("graph_name", "schema_graph")
    with context.agent_conn() as conn:
        try:
            path = retrieval.find_join_path(conn, state["tables"], graph=graph)
        except ValueError as error:
            # Some table could not be connected. Carry on with what can be
            # joined; the SQL model is told which joins are available.
            path = {"tables": state["tables"], "joins": [], "note": str(error)}
    joins = [f"{j['from_table']}.{j['from_column']} = {j['to_table']}.{j['to_column']}" for j in path["joins"]]
    return {"join_path": path, "_detail": "; ".join(joins) or "No joins needed"}


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
    notes = context.table_notes()
    parts = []
    for name in tables:
        if name not in notes:
            continue
        note = notes[name]
        columns = "\n".join(f"    {c} ({t}){' [personal data]' if pii else ''}: {d}"
                            for c, t, d, pii in note["columns"])
        parts.append(f"Table {name}: {note['description']} {note['grain']}.\n{columns}")
    return "\n\n".join(parts)


def write_sql(state: dict) -> dict:
    """Step 6. Also used for retries: the previous attempt and its error are included."""
    understanding = state["understanding"]
    path = state["join_path"]
    joins = "\n".join(f"  {j['from_table']}.{j['from_column']} = {j['to_table']}.{j['to_column']}"
                      for j in path["joins"]) or "  (single table, no joins)"
    metric_text = "\n".join(f"  {m}: {context.metrics()[m]['definition']}" for m in understanding["metrics"]) or "  (none)"
    history = state.get("history") or []

    prompt = [
        f"Question: {understanding['standalone_question']}",
        f"Time range: {json.dumps(understanding.get('time_range'))}",
        f"Exact values for names in the question:\n{describe_entities(state['entities'])}",
        f"Approved metric definitions:\n{metric_text}",
        f"Join conditions:\n{joins}",
        f"Tables:\n{_schema_text(path['tables'])}",
    ]
    if history:
        prompt.append(f"Previous question's SQL (the user may be refining it):\n{history[-1]['sql']}")
    if state.get("error"):
        prompt.append(f"Your previous SQL failed. Fix it.\nPrevious SQL:\n{state['sql']}\nProblem: {state['error']}")

    answer = llm.chat_json("strong", SQL_PROMPT, "\n\n".join(prompt))
    attempts = state.get("attempts", 0) + 1
    if not answer.get("sql"):
        return {"status": "blocked", "attempts": attempts, "sql": None,
                "block_reason": f"Refused: {answer.get('refusal', 'not a read-only question about this data')}",
                "_detail": "Model refused to write SQL"}
    return {"sql": answer["sql"].strip().rstrip(";"), "sql_explanation": answer.get("explanation"),
            "attempts": attempts, "error": None, "_detail": answer.get("explanation", "")}


def check_sql(state: dict) -> dict:
    """Step 7."""
    config = context.agent_config()
    checks = sql_checks.check(state["sql"], set(context.table_notes()), config["date_filter_required"])

    if all(c["passed"] for c in checks):
        # Only a query that passed every structural check is shown to
        # EXPLAIN. EXPLAIN plans the query without running it.
        try:
            cost = context.adapter().plan_estimate(state["sql"])
            ok = cost["total_cost"] <= config["run"]["max_plan_cost"]
            checks.append({"check": "plan_cost", "passed": ok,
                           "detail": f"Estimated cost {cost['total_cost']:,.0f}, ~{cost['rows']:,} rows"
                                     + ("" if ok else f" (limit {config['run']['max_plan_cost']:,})")})
        except Exception as error:
            checks.append({"check": "plan_cost", "passed": False, "detail": f"EXPLAIN failed: {error}"})

    failed = [c for c in checks if not c["passed"]]
    update = {"checks": checks, "sql_tables": sql_checks.tables_in(state["sql"]),
              "_detail": "All checks passed" if not failed else failed[0]["detail"]}
    if failed:
        update["error"] = "; ".join(c["detail"] for c in failed)
        update["failed_sql"] = _failed(state, "check_sql", update["error"])
    return update


def _failed(state: dict, stage: str, error: str) -> list[dict]:
    """The failed attempts so far, plus this one (shown on the demo page)."""
    return (state.get("failed_sql") or []) + [
        {"attempt": state["attempts"], "stage": stage, "sql": state["sql"], "error": error}]


def run_sql(state: dict) -> dict:
    """Step 8."""
    run = context.agent_config()["run"]
    try:
        columns, rows = context.adapter().run_readonly(state["sql"], timeout_ms=run["timeout_ms"], row_cap=run["row_cap"])
    except Exception as error:
        return {"error": f"Query failed: {error}", "failed_sql": _failed(state, "run_sql", f"Query failed: {error}"),
                "_detail": f"Failed: {str(error)[:120]}"}
    return {"result": {"columns": columns, "rows": [[_plain(v) for v in row] for row in rows]},
            "_detail": f"{len(rows):,} rows returned"}


def _plain(value):
    """
    Turn database values into plain JSON-friendly ones: Decimal -> float,
    dates -> "YYYY-MM-DD" text. The result travels to the web page as JSON,
    and is stored in the agent's memory, both of which need plain values.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value