##############################################################################
# agent/steps/build_query.py
#
# PURPOSE:
#   Steps 4 to 8 of the agent's eleven steps. Together they take what
#   Part 1 worked out about the question, and turn it into SQL that has
#   been checked and run.
#
#     STEP 4  select_tables    which tables does the question need?
#     STEP 5  find_join_path   how do those tables join? (asks the graph)
#     STEP 6  write_sql        the strong model writes one SELECT query
#     STEP 7  check_sql        is the SQL safe? (checked before it runs)
#     STEP 8  run_sql          run it on mf_data, read only
#
# WHAT EACH STEP ADDS TO THE STATE:
#     step 4  ->  tables, table_picks
#     step 5  ->  join_path
#     step 6  ->  sql, sql_explanation, attempts
#     step 7  ->  checks, sql_tables  (and error, if a check failed)
#     step 8  ->  result               (or error, if the query failed)
#
# THE RETRY LOOP:
#   If step 7 or step 8 fails, it sets "error" in the state. graph.py (Part 3)
#   sees that and sends the agent back to step 6. Step 6 then includes the
#   failed SQL and the error in its prompt, and the model fixes its own SQL.
#   This happens at most max_sql_retries times (2, in agent.yaml), so at
#   most 3 attempts in total.
#
#       step 6 ──> step 7 ──> step 8 ──> on to Part 3
#         ▲          │          │
#         └── error ─┴── error ─┘   (at most 2 times)
#
# WHERE A QUESTION CAN STOP IN THIS FILE:
#   step 6   the model refuses to write SQL     -> status "blocked"
#   steps 7 and 8 never stop the question themselves. They set "error",
#   and graph.py decides: retry, or give up after the last retry.
#
# HOW TO USE:
#   Never run directly. agent/graph.py calls the five steps in order.
#
# TRY IT YOURSELF — continue from Part 1 (from the project folder):
#       uv run python
#       >>> from agent.steps import understand as u, build_query as bq
#       >>> state = {"question": "Why did net flows in sahyadri credit fund drop in March 2024?"}
#       >>> for step in (u.guard_input, u.understand, u.match_entities,
#       ...              bq.select_tables, bq.find_join_path, bq.write_sql,
#       ...              bq.check_sql, bq.run_sql):
#       ...     state.update(step(state))
#       >>> print(state["sql"])
#       >>> state["result"]["rows"][:5]
##############################################################################


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
# The module's own description, kept for help(build_query).


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import json
# Turns Python values into JSON text. Used once: to write the question's
# time range into the prompt, e.g. {"start": "2024-03-01", "end": "2024-03-31"}.

from datetime import date, datetime
# Python's date types. PostgreSQL sends dates back as these. _plain() at the
# bottom of the file needs to recognise them, to turn them into text.

from decimal import Decimal
# PostgreSQL sends "numeric" values (money amounts, percentages) back as
# Decimal — an exact number type. _plain() turns them into ordinary numbers.

from agent import context
# The shared settings and connections (agent/context.py, from Part 1).
# This file uses:
#   context.agent_config()   the settings from agent.yaml
#   context.metrics()        the approved metric definitions
#   context.table_notes()    the approved notes for every table
#   context.agent_conn()     a connection to agent_meta
#   context.adapter()        the connection to mf_data

from agent.steps.understand import describe_entities
# From Part 1. Writes the matched names — "sahyadri credit fund" ->
# "Sahyadri Credit Risk Fund" — as JSON text for the SQL prompt.

from guardrails import sql_checks
# The four structural SQL checks. Step 7 calls sql_checks.check().

from metadata import llm, retrieval
# Two files from Part 4:
#   llm         talks to Azure OpenAI. Step 6 uses the strong model.
#   retrieval   the searches. Step 4 uses search_tables(); step 5 uses
#               find_join_path().


# ═════════════════════════════════════════════════════════════════════════════
# STEP 4 — select_tables
# ═════════════════════════════════════════════════════════════════════════════

def select_tables(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 4."""
    # THE JOB: decide which tables the SQL will need.
    #
    # Tables come from FOUR places, in order of how sure we are:
    #   1. entity   the matched names from step 3. "Sahyadri Credit Risk Fund"
    #               is a scheme, so we certainly need "schemes".
    #   2. metric   the approved metrics from step 2. net_flows lists
    #               transactions, scheme_plans and schemes in metrics.yaml.
    #   3. need     each "data_needed" phrase from step 2, searched on its own
    #   4. search   the whole question, searched against the table notes
    #
    # 1 and 2 are certain. 3 and 4 are good guesses, so they come after.
    # Each table is recorded with WHY it was picked — the "source" and the
    # "reason" — and the demo page in Part 3 shows that list.

    config = context.agent_config()
    # The settings from agent.yaml.

    question = state["understanding"]["standalone_question"]
    # The full question from step 2. For a follow-up like "now only March",
    # this is the complete rewritten question, which is what we want to
    # search with.

    picks = [{"table": e["table"], "source": "entity", "reason": e["text"]} for e in state["entities"]]
    # SOURCE 1 — one pick for each matched name from step 3.
    # For our demo:
    #   [{"table": "schemes", "source": "entity", "reason": "sahyadri credit fund"}]

    for name in state["understanding"]["metrics"]:
        # SOURCE 2 — for each approved metric step 2 said the question needs.
        # For our demo there is one: "net_flows".

        picks += [{"table": t, "source": "metric", "reason": name} for t in context.metrics()[name]["tables"]]
        # Add one pick for each table that metric lists in metrics.yaml.
        # For net_flows:
        #   transactions, scheme_plans, schemes   — each with reason "net_flows"

    # If approved metrics or matched names already say which tables are
    # needed, search only adds a couple more. Otherwise search does the work.

    k = 2 if picks else config["search_top_k"]
    # How many tables to take from searching the whole question:
    #   if names or metrics already gave us tables  -> just 2 more
    #   if they gave us nothing                     -> search_top_k (4) from agent.yaml
    #
    # WHY: when we already know the main tables, a long search list only adds
    # tables the SQL doesn't need. When we know nothing, search has to do
    # all the work, so it gets more room.

    with context.agent_conn() as conn:
        # Open a connection to agent_meta, where the table notes' searchable
        # data lives. It closes by itself when the block ends.

        found = retrieval.search_tables(conn, question, k=k)
        # SOURCE 4 — search the whole question against the 20 table notes.
        # Returns the k closest, e.g. [("transactions", 0.52), ("schemes", 0.47)].
        # (Added to picks at the end, below, because it's the least certain.)

        # Searching the whole question blurs its parts together: in "which schemes hold
        # downgraded bonds, and their redemptions", the ratings table ranked only 5th.
        # Each kind of data the question needs, searched on its own, finds its table
        # clearly, so the best table for each one is added too.
        #
        # In plain words: a question that asks about two things at once gets
        # a search result that is a bit of both, and neither table scores
        # well. Searching each part on its own finds each table clearly.

        for phrase in state["understanding"].get("data_needed", []):
            # SOURCE 3 — each short phrase from step 2, e.g.
            # "bond credit rating downgrades", "net flows by scheme".

            best = retrieval.search_tables(conn, phrase, k=1)
            # The single best table for this one phrase.

            if best and best[0][1] >= config["phrase_min_score"]:
                # Only if its score is at least phrase_min_score (0.3) from
                # agent.yaml. Below that, the phrase didn't really match any
                # table, so nothing is added.
                #
                # best[0] is the one result; [1] is its score.

                picks.append({"table": best[0][0], "source": "need", "reason": phrase, "score": round(best[0][1], 3)})
                # Record the table, the phrase that found it, and its score.

    picks += [{"table": name, "source": "search", "score": round(score, 3)} for name, score in found]
    # Now add the whole-question search results, LAST.

    # Tables the matched names and metrics need come first: they are
    # certain. Search results fill in the rest.

    tables = list(dict.fromkeys(p["table"] for p in picks))
    # The final list of tables, with each table once, in the order first picked.
    #
    # dict.fromkeys() makes a dictionary whose keys are the table names. A
    # dictionary can't hold the same key twice, and it keeps the order keys
    # were added. list() turns the keys back into a list.
    #
    # For our demo it starts:  ["schemes", "transactions", "scheme_plans", ...]
    # "schemes" was picked three times (entity, metric, maybe search) but
    # appears once.

    return {"tables": tables, "table_picks": _one_per_table(picks), "_detail": ", ".join(tables)}
    # tables        the list step 5 will join
    # table_picks   each table once, with every reason it was picked — for
    #               the demo page
    # _detail       a short note for the steps list: the table names


def _one_per_table(picks: list[dict]) -> list[dict]:
    # The _ at the start means "only used inside this file".
    # Merges several picks of the same table into one.
    """
    One pick per table, in first-picked order. A table needed by several
    names or metrics keeps the first source, with every reason joined, and
    its search score if search also found it.
    """
    # For example, these three picks:
    #   {"table": "schemes", "source": "entity", "reason": "sahyadri credit fund"}
    #   {"table": "schemes", "source": "metric", "reason": "net_flows"}
    #   {"table": "schemes", "source": "search", "score": 0.47}
    # become one:
    #   {"table": "schemes", "source": "entity",
    #    "reason": "sahyadri credit fund, net_flows", "score": 0.47, ...}

    merged = {}
    # table name -> its merged pick.

    for p in picks:
        # Go through the picks in the order they were made.

        m = merged.setdefault(p["table"], {"table": p["table"], "source": p["source"], "reasons": []})
        # Get this table's merged entry. If this is the first time we've seen
        # the table, setdefault() creates the entry first, using THIS pick's
        # source. So a table keeps the source of its first, most certain pick.

        if p.get("reason") and p["reason"] not in m["reasons"]:
            # If this pick has a reason we haven't recorded yet ...
            m["reasons"].append(p["reason"])
            # ... add it.

        if "score" in p:
            # If this pick came with a search score ...
            m["score"] = p["score"]
            # ... keep it.

    return [{**m, "reason": ", ".join(m.pop("reasons"))} for m in merged.values()]
    # Turn each entry's list of reasons into one piece of text.
    #
    #   m.pop("reasons")     take the list out of the entry
    #   ", ".join(...)       join it into "sahyadri credit fund, net_flows"
    #   {**m, "reason": ...} copy the entry's other keys, and add "reason"
    #
    # A small detail: Python copies **m BEFORE it runs m.pop(), so the copy
    # still contains "reasons" (the list) as well as "reason" (the text).
    # Nothing reads "reasons" afterwards, so it does no harm — it's just
    # an extra key on the demo page's data.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 5 — find_join_path
# ═════════════════════════════════════════════════════════════════════════════

def find_join_path(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 5."""
    # THE JOB: work out exactly how the tables from step 4 join together.
    #
    # The model is NOT asked to work out joins. A wrong join gives wrong
    # numbers that look right, and nobody notices. So the joins come from
    # the graph Part 4 built, where every link was checked by a person.

    graph = context.agent_config().get("graph_name", "schema_graph")
    # The graph's name. agent.yaml doesn't set graph_name today, so .get()
    # falls back to "schema_graph" — the name Part 4 gave it.

    with context.agent_conn() as conn:
        # Open a connection to agent_meta, where the graph lives.

        try:
            # find_join_path raises an error if some table can't be reached.
            # "try" catches that, instead of crashing the whole agent.

            path = retrieval.find_join_path(conn, state["tables"], graph=graph)
            # Ask the graph for the shortest set of joins that connects every
            # table. Returns:
            #   {"tables": [every table needed, including in-between ones],
            #    "joins":  [{"from_table", "from_column", "to_table", "to_column", ...}, ...]}
            #
            # "tables" can be LONGER than step 4's list. If two tables have no
            # direct link, the route goes through other tables, and those are
            # added too.

        except ValueError as error:
            # Some table couldn't be connected to the others.

            # Some table could not be connected. Carry on with what can be
            # joined; the SQL model is told which joins are available.

            path = {"tables": state["tables"], "joins": [], "note": str(error)}
            # Carry on with no joins, and keep the error as a note.
            #
            # WHY CARRY ON, NOT STOP: many questions only need one table,
            # and the model might still write a correct query. If it can't,
            # step 7 or step 8 will fail and the retry loop takes over.

    joins = [f"{j['from_table']}.{j['from_column']} = {j['to_table']}.{j['to_column']}" for j in path["joins"]]
    # Each join written as readable text, for the steps list:
    #   "transactions.plan_id = scheme_plans.plan_id"

    return {"join_path": path, "_detail": "; ".join(joins) or "No joins needed"}
    # join_path   the whole result, used by step 6
    # _detail     the joins as text, or "No joins needed" for a one-table query


# ═════════════════════════════════════════════════════════════════════════════
# STEP 6 — write_sql
# ═════════════════════════════════════════════════════════════════════════════

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
# The instructions given to the STRONG model in step 6.
#
# EACH RULE, AND THE MISTAKE IT PREVENTS:
#   1  one SELECT, never modify data
#        stops a query that changes data. If the question asks for a change,
#        the model must reply with no SQL and a reason — the model's own
#        refusal, before step 7 ever sees anything.
#   2  only these tables, only these joins
#        stops the model inventing a table or guessing a join. Step 7
#        checks the tables again, in case it ignores this rule.
#   3  use the exact names
#        stops WHERE scheme_name = 'sahyadri credit fund', which finds nothing
#   4  always filter transactions by date
#        keeps the query fast (only the months needed are read). It even
#        says what to do when the question gives no period, and warns
#        that IS NOT NULL doesn't count. Step 7 checks this too.
#   5  follow the metric definition exactly
#        net flows is calculated the business's way, every time
#   6  no extra filters
#        stops the model adding conditions nobody asked for, such as
#        "only active distributors", which would quietly change the answer
#   7  a small, sorted, rounded result
#        the database does the adding up, not the model. 50 rows is plenty
#        for a person to read.
#
# The last line asks for JSON with two keys: the SQL, and one sentence on
# what it calculates. That sentence is shown on the demo page, and given to
# the commentary step in Part 3.


def _schema_text(tables: list[str]) -> str:
    # Builds the "Tables:" part of the SQL prompt.
    """The approved notes for the given tables, written out for the model."""
    # THE JOB: show the model the approved notes for the chosen tables ONLY,
    # not all 20. Less to read means fewer mistakes, and a cheaper call.
    #
    # The result looks like:
    #   Table credit_rating_history: Credit rating events ... One row per ...
    #       agency (text): The rating agency that gave the rating
    #       rtg (text): The rating given, e.g. AAA
    #       rtg_dt (date): The date the agency gave the rating
    #       ...
    #
    # This is WHERE PART 4 REACHES THE SQL. The model doesn't have to guess
    # that rtg_dt is the rating date. The approved note tells it.

    notes = context.table_notes()
    # All 20 approved notes (loaded once, then kept — see context.py).

    parts = []
    # One block of text per table.

    for name in tables:
        # For each table on the join path (from step 5).

        if name not in notes:
            # A table without an approved note is skipped.
            continue
            # So a draft note nobody has checked can never reach the model.

        note = notes[name]
        # This table's description, grain, and column notes.

        columns = "\n".join(f"    {c} ({t}){' [personal data]' if pii else ''}: {d}"
                            for c, t, d, pii in note["columns"])
        # One line per column:   name (type): description
        # Personal-data columns get " [personal data]" added, e.g.
        #     pan (text) [personal data]: The investor's PAN (tax ID)
        #
        # Each column note is (c, t, d, pii):
        #   c = column name, t = type, d = description, pii = personal data?
        #
        # Only the column's DESCRIPTION reaches the model — never any real
        # value from a personal-data column.

        parts.append(f"Table {name}: {note['description']} {note['grain']}.\n{columns}")
        # The table's description, its grain (what one row represents), then
        # its columns.

    return "\n\n".join(parts)
    # All the tables, with a blank line between each.


def write_sql(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 6. Also used for retries: the previous attempt and its error are included."""
    # THE JOB: give the strong model everything it needs, and get one SELECT back.
    #
    # WHY THE STRONG MODEL: writing correct SQL across several joins is the
    # hardest job in the whole agent. This is where the better model is
    # worth its cost. (Step 2 used the small one.)
    #
    # The message to the model is built from six parts, every one of them
    # worked out by an earlier step:
    #   Question                     step 2
    #   Time range                   step 2
    #   Exact values for names       step 3
    #   Approved metric definitions  metrics.yaml, for the metrics step 2 found
    #   Join conditions              step 5
    #   Tables                       the approved notes, for step 5's tables

    understanding = state["understanding"]
    # What step 2 worked out.

    path = state["join_path"]
    # What step 5 found.

    joins = "\n".join(f"  {j['from_table']}.{j['from_column']} = {j['to_table']}.{j['to_column']}"
                      for j in path["joins"]) or "  (single table, no joins)"
    # The join conditions, one per line, e.g.
    #   transactions.plan_id = scheme_plans.plan_id
    #   scheme_plans.scheme_id = schemes.scheme_id
    # or, for a one-table query, "(single table, no joins)".
    # The "or" picks the second text when the joined text is empty.

    metric_text = "\n".join(f"  {m}: {context.metrics()[m]['definition']}" for m in understanding["metrics"]) or "  (none)"
    # The full DEFINITION of every metric the question needs, from metrics.yaml.
    # In Part 1, step 2 saw only each metric's name and description. Here the
    # model gets the complete rule, e.g. for net_flows:
    #   SUM(CASE WHEN txn_type IN ('Redemption', 'Switch Out') THEN -amount ELSE amount END)
    #   over transactions WHERE txn_status = 'Completed' ...
    # or "(none)" if the question needs no metric.

    history = state.get("history") or []
    # The earlier questions in this conversation ([] for the first question).

    prompt = [
        # The message to the model, as a list of parts. They're joined with
        # blank lines when sent.

        f"Question: {understanding['standalone_question']}",
        # The complete question.

        f"Time range: {json.dumps(understanding.get('time_range'))}",
        # The period from step 2, as JSON — or null if there isn't one.

        f"Exact values for names in the question:\n{describe_entities(state['entities'])}",
        # The matched names, e.g. "sahyadri credit fund" -> "Sahyadri Credit Risk Fund".

        f"Approved metric definitions:\n{metric_text}",
        # The rules to calculate by.

        f"Join conditions:\n{joins}",
        # The only joins the model may use.

        f"Tables:\n{_schema_text(path['tables'])}",
        # The approved notes for every table on the join path — including any
        # in-between tables the route passes through.
    ]

    if history:
        # A follow-up question, such as "now show only March and April".
        prompt.append(f"Previous question's SQL (the user may be refining it):\n{history[-1]['sql']}")
        # Show the model the last question's SQL, so it can change it rather
        # than starting from nothing.

    if state.get("error"):
        # A RETRY. Step 7 or step 8 failed, and graph.py sent us back here.
        prompt.append(f"Your previous SQL failed. Fix it.\nPrevious SQL:\n{state['sql']}\nProblem: {state['error']}")
        # Show the model the SQL that failed, and exactly why. For example:
        #   Problem: transactions must be filtered by a date range on txn_date (e.g. >= and <)
        #
        # THIS IS THE WHOLE RETRY MECHANISM. There's no separate "fix" step.
        # The same step runs again, with more information.

    answer = llm.chat_json("strong", SQL_PROMPT, "\n\n".join(prompt))
    # Send the rules and the message to the strong model.
    # The reply comes back as a dictionary:
    #   {"sql": "WITH ... SELECT ...", "explanation": "Calculates monthly net flows ..."}
    # or, for a refusal:
    #   {"sql": null, "refusal": "This asks to delete data"}

    attempts = state.get("attempts", 0) + 1
    # Count this attempt. Step 1 set attempts to 0, so the first try is 1,
    # a retry is 2, and so on. graph.py compares this with max_sql_retries.

    if not answer.get("sql"):
        # No SQL came back — the model refused (rule 1 of the prompt).

        return {"status": "blocked", "attempts": attempts, "sql": None,
                "block_reason": f"Refused: {answer.get('refusal', 'not a read-only question about this data')}",
                "_detail": "Model refused to write SQL"}
        # status "blocked"   graph.py ends the run here
        # block_reason       the model's own reason, or a default if it gave none
        #
        # This is the SECOND check for "delete all rows". Step 2 should
        # already have stopped it. If it ever didn't, the model refuses here.

    return {"sql": answer["sql"].strip().rstrip(";"), "sql_explanation": answer.get("explanation"),
            "attempts": attempts, "error": None, "_detail": answer.get("explanation", "")}
    # The SQL, cleaned up:
    #   .strip()        remove spaces and blank lines at either end
    #   .rstrip(";")    remove a ";" at the end
    #
    # WHY REMOVE THE ";": step 7 puts "EXPLAIN (FORMAT JSON) " in front of
    # the SQL. That works with or without a ";", but keeping the SQL without
    # one means it's always in the same form, wherever it's used next.
    #
    # "error": None clears the previous error. Without this, a successful
    # retry would still look like a failure to graph.py.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 7 — check_sql
# ═════════════════════════════════════════════════════════════════════════════

def check_sql(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 7."""
    # THE JOB: check the SQL is safe, BEFORE it runs. Two kinds of check:
    #   1. structural   sql_checks.py: valid, one statement, read only,
    #                   known tables, date filter. No database needed.
    #   2. cost         PostgreSQL's EXPLAIN: how much work would it be?
    #                   Needs the database, but doesn't run the query.

    config = context.agent_config()
    # The settings from agent.yaml.

    checks = sql_checks.check(state["sql"], set(context.table_notes()), config["date_filter_required"])
    # Run the four structural checks.
    #
    #   state["sql"]                    the SQL from step 6
    #   set(context.table_notes())      the known tables: the 20 that have
    #                                   approved notes. set() of a dictionary
    #                                   gives its keys — the table names.
    #   config["date_filter_required"]  {"transactions": "txn_date"}
    #
    # checks is a list of results, one per check.

    if all(c["passed"] for c in checks):
        # Only if EVERY structural check passed.

        # Only a query that passed every structural check is shown to
        # EXPLAIN. EXPLAIN plans the query without running it.
        #
        # WHY ONLY THEN: EXPLAIN sends the SQL to the real database. We never
        # send a query that failed a safety check to the database, not even
        # to plan it.

        try:
            # EXPLAIN itself can fail, e.g. on a column that doesn't exist.

            cost = context.adapter().plan_estimate(state["sql"])
            # Ask PostgreSQL how it would run this query, without running it.
            # Returns {"total_cost": 12345.6, "rows": 40}.
            #
            # total_cost is PostgreSQL's own measure of work, not seconds.
            # rows is how many rows it expects the query to return.

            ok = cost["total_cost"] <= config["run"]["max_plan_cost"]
            # Is it within the limit? max_plan_cost is 2,000,000 in agent.yaml.

            checks.append({"check": "plan_cost", "passed": ok,
                           "detail": f"Estimated cost {cost['total_cost']:,.0f}, ~{cost['rows']:,} rows"
                                     + ("" if ok else f" (limit {config['run']['max_plan_cost']:,})")})
            # Add the cost check to the list, in the same shape as the others.
            #
            # The detail reads, for example:
            #   "Estimated cost 12,346, ~40 rows"
            # and, if over the limit, the limit is added on the end:
            #   "Estimated cost 9,500,000, ~1,200,000 rows (limit 2,000,000)"
            #
            #   :,.0f   a number with commas and no decimals
            #   :,      a number with commas

        except Exception as error:
            # EXPLAIN failed. The most common reason: the SQL uses a column
            # or table name that doesn't exist.

            checks.append({"check": "plan_cost", "passed": False, "detail": f"EXPLAIN failed: {error}"})
            # Count it as a failed check, with PostgreSQL's own error message,
            # e.g. 'EXPLAIN failed: column "scheme_nm" does not exist'.
            #
            # A HIDDEN BENEFIT OF EXPLAIN: it catches wrong column names
            # BEFORE the query runs. The message goes back to step 6, and the
            # model fixes the name.

    failed = [c for c in checks if not c["passed"]]
    # The checks that failed. An empty list means everything passed.

    update = {"checks": checks, "sql_tables": sql_checks.tables_in(state["sql"]),
              "_detail": "All checks passed" if not failed else failed[0]["detail"]}
    # What this step returns:
    #   checks       every check and its result — shown on the demo page
    #   sql_tables   the tables the SQL REALLY reads (step 4 picked tables,
    #                but the model may not have used all of them)
    #   _detail      "All checks passed", or the first failure's message

    if failed:
        # At least one check failed.

        update["error"] = "; ".join(c["detail"] for c in failed)
        # Every failure message, joined into one text. Setting "error" is
        # what tells graph.py to go back to step 6. This exact text is what
        # the model reads on the retry, so it learns about every problem at once.

        update["failed_sql"] = _failed(state, "check_sql", update["error"])
        # Keep a record of this failed attempt, for the demo page.

    return update
    # Note: this step never sets status. It only reports. graph.py decides
    # whether to retry or give up.


def _failed(state: dict, stage: str, error: str) -> list[dict]:
    # A helper used by both step 7 and step 8.
    """The failed attempts so far, plus this one (shown on the demo page)."""
    # THE JOB: keep a list of every SQL attempt that failed, so the demo page
    # can show "attempt 1 failed because ..., attempt 2 passed".

    return (state.get("failed_sql") or []) + [
        {"attempt": state["attempts"], "stage": stage, "sql": state["sql"], "error": error}]
    # The earlier failures (or an empty list), plus one new entry:
    #   attempt   which attempt this was: 1, 2 or 3
    #   stage     which step it failed at: "check_sql" or "run_sql"
    #   sql       the SQL that failed
    #   error     why
    #
    # It returns a NEW list rather than adding to the old one. Each step
    # returns only what it changed, and LangGraph replaces the old value.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 8 — run_sql
# ═════════════════════════════════════════════════════════════════════════════

def run_sql(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 8."""
    # THE JOB: run the checked SQL on mf_data, and keep the rows.
    #
    # Three limits are applied by PostgreSQL itself, in run_readonly():
    #   read only    any change to the data is refused
    #   15 seconds   a slower query is cancelled
    #   5,000 rows   at most this many come back

    run = context.agent_config()["run"]
    # The run limits from agent.yaml: timeout_ms, row_cap, max_plan_cost.

    try:
        # The query can still fail here, even after passing step 7 — for
        # example, it takes too long, or divides by zero.

        columns, rows = context.adapter().run_readonly(state["sql"], timeout_ms=run["timeout_ms"], row_cap=run["row_cap"])
        # Run the query. Returns two things:
        #   columns   the column names, e.g. ["month", "net_flows"]
        #   rows      the rows, e.g. [(date(2024, 3, 1), Decimal("-12345.67")), ...]

    except Exception as error:
        # The query failed while running.

        return {"error": f"Query failed: {error}", "failed_sql": _failed(state, "run_sql", f"Query failed: {error}"),
                "_detail": f"Failed: {str(error)[:120]}"}
        # Set "error", so graph.py sends the agent back to step 6 with
        # PostgreSQL's message, e.g. "canceling statement due to statement timeout".
        #
        # _detail keeps only the first 120 characters, because PostgreSQL's
        # error messages can be long, and this is just a note for the steps list.

    return {"result": {"columns": columns, "rows": [[_plain(v) for v in row] for row in rows]},
            "_detail": f"{len(rows):,} rows returned"}
    # The result: the column names, and every row with every value made plain
    # (see _plain() below).
    #
    # This is the end of Part 2. Part 3 turns these rows into an answer.


def _plain(value):
    # Converts one value from the database into a plain, simple value.
    """
    Turn database values into plain JSON-friendly ones: Decimal -> float,
    dates -> "YYYY-MM-DD" text. The result travels to the web page as JSON,
    and is stored in the agent's memory, both of which need plain values.
    """
    # WHY THIS IS NEEDED: PostgreSQL sends money as Decimal and dates as
    # date objects. JSON — the format the web page and the agent's memory
    # use — has no Decimal and no date. Trying to turn them into JSON gives
    # an error. So we convert them here, once.

    if isinstance(value, Decimal):
        # An exact decimal, e.g. Decimal("12345.67").
        return float(value)
        # -> 12345.67, an ordinary number.

    if isinstance(value, (date, datetime)):
        # A date, e.g. date(2024, 3, 1).
        return value.isoformat()
        # -> "2024-03-01", as text.

    return value
    # Anything else — text, whole numbers, True/False, None — is already
    # plain, so it's returned unchanged.