##############################################################################
# agent/graph.py
#
# PURPOSE:
#   Connects the agent's 11 steps into ONE agent, and runs a question
#   through it.
#
#   In Parts 1 and 2, we ran each step by hand, in Python, one at a time.
#   This file does that automatically, using a library called LangGraph.
#   It also makes the decisions we made by hand:
#     - stop here, or carry on?
#     - the SQL failed: try again, or give up?
#
#   Everything outside the agent uses ONE function from this file:
#       from agent.graph import ask
#       state = ask("Why did net flows in Sahyadri Credit Risk Fund drop in March 2024?", "conv-1")
#   The web API in api/main.py calls it, and so do the Stage 6 tests.
#
# THE ROUTE A QUESTION TAKES:
#
#   guard_input ──attack────────────────────────────────────────→ END (blocked)
#       ↓
#   understand ──change data / off topic────────────────────────→ END (blocked)
#              ──needs clarification────────────────────────────→ END
#       ↓
#   match_entities → select_tables → find_join_path → write_sql ──refused──→ END (blocked)
#                                                       ↑   ↓
#                                 fail, retries left ───┤  check_sql ──fail, no retries──→ END (failed)
#                                                       │   ↓
#                                 fail, retries left ───┘  run_sql ──fail, no retries───→ END (failed)
#                                                           ↓
#                                 summarise → write_commentary → remember → END (completed)
#
#   Every question ends in one of four ways:
#     completed             answered
#     blocked               stopped on purpose: an attack, a request to
#                           change data, off topic, or the model refused
#     needs_clarification   the question couldn't be answered as typed
#     failed                the SQL failed 3 times
#
# WHY A FIXED ROUTE:
#   Many agents let the model decide what to do next, step by step. This
#   one doesn't. The order is always the same, and only the few decisions
#   above can change it. That makes the agent predictable: the same
#   question always goes through the same steps, and when something goes
#   wrong you know exactly which step to look at.
#
# TWO MORE THINGS THIS FILE DOES:
#   TIMING AND TRACING
#     Every step is timed, and its note is added to a "steps" list. The
#     demo page shows that list — every step, what it did, how long it took.
#     Each step is also sent to Azure Application Insights, so you can see
#     the agent working in the Azure portal.
#   MEMORY BETWEEN QUESTIONS
#     The state is kept between questions in the same conversation. That's
#     how follow-up questions work.
#
# HOW TO TEST IT:
#       uv run pytest tests/test_stage6_agent.py -v
#   18 passed. The demo questions, a follow-up, an attack that's blocked,
#   and "delete all rows" never running. Takes 2 to 4 minutes, because it
#   calls Azure for every question.
#
# TRY IT YOURSELF (from the project folder):
#       uv run python
#       >>> from agent.graph import ask
#       >>> state = ask("Which distributor has the highest transaction rejection rate?", "demo-1")
#       >>> state["status"]
#       'completed'
#       >>> print(state["commentary"])
#       >>> for s in state["steps"]: print(s["step"], s["ms"], "ms -", s["detail"])
##############################################################################


"""
agent/graph.py
==============

WHAT THIS FILE IS FOR
---------------------
Wires the 11 steps into ONE LangGraph workflow with a fixed order
(tradeoff 10), and runs a question through it.

    from agent.graph import ask
    result = ask("Why did net flows in Sahyadri Credit Risk Fund drop in March 2024?", "conv-1")

THE ROUTE THROUGH THE STEPS
---------------------------
    guard_input ──attack──────────────────────────────────────────→ END (blocked)
        ↓
    understand ──change data / off topic─────────────────────────→ END (blocked)
               ──needs clarification──────────────────────────────→ END
        ↓
    match_entities → select_tables → find_join_path → write_sql ──refused──→ END (blocked)
                                                         ↑   ↓
                                   fail, retries left ───┤  check_sql ──fail, no retries──→ END (failed)
                                                         │   ↓
                                   fail, retries left ───┘  run_sql ──fail, no retries───→ END (failed)
                                                             ↓
                                   summarise → write_commentary → remember → END (completed)

TRACING
-------
Every step is wrapped by _traced(), which:
    - times the step, and adds a line to state["steps"] (shown on the demo page)
    - opens an OpenTelemetry span, which Application Insights receives
      (api/telemetry.py switches that on)
LangSmith needs no code here: with LANGSMITH_TRACING=true in .env,
LangGraph sends every step to LangSmith by itself.

MEMORY
------
InMemorySaver keeps each conversation's state between questions, keyed by
conversation_id ("thread_id" in LangGraph). That is what makes follow-up
questions work. It lives in the server's memory, so it is lost on restart;
Stage 8 replaces it with a Postgres-backed saver.
"""
# The module's own description, kept for help(graph).


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import time
# Used to measure how long each step takes, in milliseconds.

from typing import Any, TypedDict
# Two helpers for describing types:
#   TypedDict   describes a dictionary with named keys, each of a known type.
#               Used below to describe the state.
#   Any         "any type at all". Used for _detail, which can hold anything.

from langgraph.checkpoint.memory import InMemorySaver
# Keeps each conversation's state between questions, in the server's memory.
# This is what makes follow-up questions work.

from langgraph.graph import END, START, StateGraph
# The three things we build the agent from:
#   StateGraph   the agent itself: a set of steps, and arrows between them
#   START        the fixed entry point. The first arrow starts here.
#   END          the fixed exit point. Any arrow to END stops the run.

from opentelemetry import trace
# OpenTelemetry: a standard way to record what a program is doing, step by
# step. Each recorded step is called a SPAN. Azure Application Insights
# receives these spans, so the agent's steps show up in the Azure portal.

from agent import context
# The shared settings (agent/context.py). Used once: to read max_sql_retries.

from agent.steps import answer, build_query, understand
# The three files that hold the 11 steps:
#   understand    steps 1 to 3    (Part 1)
#   build_query   steps 4 to 8    (Part 2)
#   answer        steps 9 to 11   (Part 3)


tracer = trace.get_tracer("fundgraph.agent")
# The object that creates spans. "fundgraph.agent" is the name the spans
# are grouped under in Application Insights.
#
# If tracing hasn't been switched on (api/telemetry.py does that when the
# web app starts), spans cost almost nothing and go nowhere. So this works
# the same in a test or in Python by hand.


# ═════════════════════════════════════════════════════════════════════════════
# THE STATE — the one dictionary every step shares
# ═════════════════════════════════════════════════════════════════════════════

class AgentState(TypedDict, total=False):
    # Describes the state: every key any step reads or writes, and its type.
    #
    # LangGraph needs this to know what the state looks like. It's also the
    # one place to see EVERY piece of information the agent works with.
    """
    Everything the steps share. total=False: every field is optional, since
    each step fills in its own part as the question moves through.
    """
    # total=False means no key is required. At the start, the state holds
    # only "question". Each step adds its own keys as the question moves along.

    # ── from the user ──
    question: str
    # The question exactly as the user typed it.

    history: list[dict]          # earlier turns in this conversation (kept between questions)
    # Filled in by step 11. Each entry: the complete question, its SQL, its row count.

    # ── for the demo page ──
    steps: list[dict]            # what each step did and how long it took (for the demo page)
    # Filled in by _traced() below, never by a step itself.

    # ── how it ended ──
    status: str | None           # completed | blocked | needs_clarification | failed
    # None while the question is still going.

    block_reason: str | None
    # Why it was blocked or failed — the message the user sees.

    # ── Part 1: steps 1 to 3 ──
    prompt_shield: dict
    # Step 1: the Content Safety result.

    understanding: dict | None
    # Step 2: the nine-field form — intent, names, dates, metrics, data needed.

    entities: list[dict]
    # Step 3: names matched to exact database values.

    unmatched: list[dict]
    # Step 3: names that couldn't be matched.

    # ── Part 2: steps 4 to 8 ──
    tables: list[str]
    # Step 4: the tables chosen.

    table_picks: list[dict]      # why each table was picked: entity, metric or search (with score)
    # Step 4: the reason for each table.

    join_path: dict | None
    # Step 5: the joins, from the graph.

    sql: str | None
    # Step 6: the SQL.

    sql_tables: list[str]        # the tables the final SQL actually reads
    # Step 7: may be fewer than step 4 chose.

    sql_explanation: str | None
    # Step 6: one sentence on what the SQL calculates.

    checks: list[dict]
    # Step 7: every safety check and its result.

    attempts: int
    # How many times SQL has been written. The retry decision reads this.

    failed_sql: list[dict]       # SQL attempts that failed a check or the run, before the retry
    # Steps 7 and 8: every failed attempt, for the demo page.

    error: str | None
    # Steps 7 and 8: the last error. None means all is well.
    # THIS ONE KEY drives the retry loop — see _retry_or() below.

    result: dict | None
    # Step 8: the column names and rows.

    # ── Part 3: steps 9 to 11 ──
    summary: dict | None
    # Step 9: row count, totals, first rows.

    commentary: str | None
    # Step 10: the answer in plain English.

    _detail: Any
    # A short note each step leaves for the steps list. _traced() takes it
    # out before the state is saved, so it never stays in the state.


# ═════════════════════════════════════════════════════════════════════════════
# TIMING AND TRACING — wrapped around every step
# ═════════════════════════════════════════════════════════════════════════════

def _traced(name: str, step):
    # Takes a step's name and its function. Returns a NEW function that runs
    # the step, with timing and tracing around it.
    """Wrap one step with timing, a steps-list entry and an OpenTelemetry span."""
    # WHY WRAP INSTEAD OF ADDING CODE TO EACH STEP: the eleven steps stay
    # clean — they only do their own job. The timing and tracing are written
    # once, here, and added to every step the same way.

    def run(state: AgentState) -> dict:
        # The wrapped step. LangGraph calls THIS, not the step directly.

        started = time.perf_counter()
        # Note the start time. perf_counter() is Python's most precise clock
        # for measuring how long something takes.

        with tracer.start_as_current_span(f"agent.{name}") as span:
            # Open a span named after the step, e.g. "agent.write_sql". It
            # closes by itself when this block ends, and its timing goes to
            # Application Insights.

            update = step(state)
            # RUN THE REAL STEP. It returns the keys it changed.

            span.set_attribute("agent.step.detail", str(update.get("_detail", ""))[:500])
            # Attach the step's note to the span, so it's visible in Azure.
            # At most 500 characters, to keep the trace small.

        ms = round((time.perf_counter() - started) * 1000)
        # How long the step took, in whole milliseconds.
        # (perf_counter() counts in seconds, so x 1000.)

        earlier = [] if name == "guard_input" else (state.get("steps") or [])
        # The steps list so far.
        #
        # guard_input is the FIRST step of every question, so it starts a
        # fresh list. Without this, a second question in the same
        # conversation would add its steps onto the first question's list —
        # because the state is kept between questions.

        update["steps"] = earlier + [{"step": name, "ms": ms, "detail": update.pop("_detail", "")}]
        # Add one line for this step: its name, how long it took, and its note.
        #
        # update.pop("_detail", "") takes the note OUT of the update. So the
        # note goes into the steps list, but isn't saved as a key in the state.

        return update
        # Give LangGraph the step's changes, plus the new steps list.

    return run
    # Return the wrapped function. It remembers "name" and "step" from when
    # it was made — Python calls a function like this a CLOSURE.


# ═════════════════════════════════════════════════════════════════════════════
# THE DECISIONS — where to go after the steps that can branch
#
# Each function below reads the state, and returns the NAME of the next
# step — or END to stop. LangGraph calls it right after the step finishes.
# ═════════════════════════════════════════════════════════════════════════════

def _after_guard(state):
    # After step 1, guard_input.
    return END if state.get("status") == "blocked" else "understand"
    # Content Safety found an attack -> stop.
    # Otherwise -> step 2.


def _after_understand(state):
    # After step 2, understand.

    # blocked = a request to change data, or off topic; needs_clarification = ask back.
    return END if state.get("status") in ("blocked", "needs_clarification") else "match_entities"
    # A request to change data, off topic, or unclear -> stop.
    # Otherwise -> step 3.


def _after_write(state):
    # After step 6, write_sql.
    return END if state.get("status") == "blocked" else "check_sql"
    # The model refused to write SQL -> stop.
    # Otherwise -> step 7.


def _retry_or(next_step: str):
    # Makes the decision function used after step 7 AND after step 8.
    # They decide the same way; only the "carry on" step is different:
    #   after check_sql  ->  next_step is "run_sql"
    #   after run_sql    ->  next_step is "summarise"
    """After check_sql or run_sql: carry on if no error; otherwise retry, or stop when out of retries."""

    def decide(state):
        # The decision itself. THIS IS THE RETRY LOOP.

        if not state.get("error"):
            # No error — the check passed, or the query ran.
            return next_step
            # Carry on.

        if state["attempts"] <= context.agent_config()["max_sql_retries"]:
            # There was an error, and retries are left.
            return "write_sql"
            # Go BACK to step 6. Step 6 sees the error in the state, and
            # puts it in the model's prompt so the model can fix the SQL.

        return "give_up"
        # Out of retries.

    # HOW MANY ATTEMPTS, EXACTLY: max_sql_retries is 2 in agent.yaml.
    #   attempt 1 fails  ->  1 <= 2  ->  retry
    #   attempt 2 fails  ->  2 <= 2  ->  retry
    #   attempt 3 fails  ->  3 >  2  ->  give up
    # So at most 3 attempts: the first try, plus 2 retries.

    return decide
    # Return the decision function.


def _give_up(state):
    # The last step when every SQL attempt failed.
    return {"status": "failed", "block_reason": f"Could not produce a working query after "
                                                f"{state['attempts']} attempts: {state['error']}"}
    # status "failed", and a message saying how many attempts were made and
    # the last error. The user sees this instead of an answer.
    #
    # Note: this isn't wrapped with _traced(). It only writes a message, so
    # it doesn't need timing, and it doesn't appear in the steps list.


# ═════════════════════════════════════════════════════════════════════════════
# BUILDING THE AGENT
# ═════════════════════════════════════════════════════════════════════════════

def build():
    # Describes all 11 steps and the arrows between them, then turns that
    # description into a runnable agent.

    graph = StateGraph(AgentState)
    # An empty agent, whose state has the AgentState shape.

    steps = {
        # Every step's NAME, and the function that does it.
        # The names are what the arrows below refer to.

        "guard_input": understand.guard_input,
        # Step 1: check for attacks.

        "understand": understand.understand,
        # Step 2: work out what is being asked.

        "match_entities": understand.match_entities,
        # Step 3: match names to exact database values.

        "select_tables": build_query.select_tables,
        # Step 4: choose the tables.

        "find_join_path": build_query.find_join_path,
        # Step 5: find how to join them.

        "write_sql": build_query.write_sql,
        # Step 6: write the SQL.

        "check_sql": build_query.check_sql,
        # Step 7: check the SQL.

        "run_sql": build_query.run_sql,
        # Step 8: run it, read only.

        "summarise": answer.summarise,
        # Step 9: summarise the result.

        "write_commentary": answer.write_commentary,
        # Step 10: explain it in plain English.

        "remember": answer.remember,
        # Step 11: save the question for follow-ups.
    }

    for name, step in steps.items():
        # For each of the 11 steps:
        graph.add_node(name, _traced(name, step))
        # Add it to the agent, wrapped with timing and tracing.
        # LangGraph calls each step a NODE.

    graph.add_node("give_up", _give_up)
    # The 12th node: the one that reports a failure. Not traced.

    # ── THE ARROWS ──
    # LangGraph calls each arrow an EDGE. Two kinds:
    #   add_edge(a, b)                       always go from a to b
    #   add_conditional_edges(a, decide, [...])
    #                                        after a, ask "decide" where to go.
    #                                        The list names every place it
    #                                        could go, so LangGraph can check
    #                                        the agent is complete.

    graph.add_edge(START, "guard_input")
    # Every question starts at step 1.

    graph.add_conditional_edges("guard_input", _after_guard, ["understand", END])
    # After step 1: step 2, or stop.

    graph.add_conditional_edges("understand", _after_understand, ["match_entities", END])
    # After step 2: step 3, or stop.

    graph.add_edge("match_entities", "select_tables")
    # Step 3 -> step 4, always.

    graph.add_edge("select_tables", "find_join_path")
    # Step 4 -> step 5, always.

    graph.add_edge("find_join_path", "write_sql")
    # Step 5 -> step 6, always.

    graph.add_conditional_edges("write_sql", _after_write, ["check_sql", END])
    # After step 6: step 7, or stop if the model refused.

    graph.add_conditional_edges("check_sql", _retry_or("run_sql"), ["run_sql", "write_sql", "give_up"])
    # After step 7: step 8 if every check passed; back to step 6 if not and
    # retries are left; give up if not.

    graph.add_conditional_edges("run_sql", _retry_or("summarise"), ["summarise", "write_sql", "give_up"])
    # After step 8: step 9 if the query ran; back to step 6 if it failed and
    # retries are left; give up if not.

    graph.add_edge("summarise", "write_commentary")
    # Step 9 -> step 10, always.

    graph.add_edge("write_commentary", "remember")
    # Step 10 -> step 11, always.

    graph.add_edge("remember", END)
    # Step 11 -> finished. The question is answered.

    graph.add_edge("give_up", END)
    # After giving up -> finished, with status "failed".

    return graph.compile(checkpointer=InMemorySaver())
    # compile() turns the description into a runnable agent. It also checks
    # every arrow points to a real step.
    #
    # checkpointer=InMemorySaver() keeps each conversation's state after
    # every run, in the server's memory. The next question in the same
    # conversation starts from that saved state — which is why "history"
    # is still there for a follow-up.
    #
    # THE CATCH: it's in the server's memory. Restart the server, and every
    # conversation is forgotten. Stage 8 will save it in PostgreSQL instead
    # (see config/memory.yaml).


AGENT = build()
# Build the agent ONCE, when this file is first imported, and keep it.
# Every question reuses this same agent. Building it for every question
# would waste time, and would throw away the saved conversations.


# ═════════════════════════════════════════════════════════════════════════════
# ask() — THE ONE FUNCTION EVERYTHING ELSE USES
# ═════════════════════════════════════════════════════════════════════════════

def ask(question: str, conversation_id: str) -> dict:
    # Takes:
    #   question          what the user typed
    #   conversation_id   which conversation this belongs to. Any text; the
    #                     same id means "a follow-up in the same conversation".
    # Returns: the final state — everything every step worked out.
    """Run one question through the agent, within a conversation."""

    with tracer.start_as_current_span("agent.ask") as span:
        # One span around the WHOLE question. The eleven step spans sit
        # inside it, so Application Insights shows them as one run.

        state = AGENT.invoke({"question": question},
                             config={"configurable": {"thread_id": conversation_id}})
        # RUN THE AGENT.
        #
        #   {"question": question}
        #       the starting state: just the question. Everything else is
        #       either filled in by the steps, or carried over from the
        #       saved conversation.
        #
        #   config={"configurable": {"thread_id": conversation_id}}
        #       which saved conversation to continue. LangGraph calls a
        #       conversation a "thread". A new id starts a fresh conversation;
        #       the same id continues one, so its "history" is there.
        #
        # invoke() runs step after step, following the arrows, until it
        # reaches END. Then it returns the final state.

        span.set_attribute("agent.status", str(state.get("status")))
        # Record how it ended — completed, blocked, failed — on the span.

        span.set_attribute("agent.tables", ",".join(state.get("tables") or []))
        # And which tables were chosen. Both are then searchable in
        # Application Insights, e.g. "show me every failed question".

    return state
    # The final state. The caller reads what it needs from it:
    #   state["status"]       how it ended
    #   state["commentary"]   the answer
    #   state["result"]       the rows
    #   state["sql"]          the SQL
    #   state["steps"]        every step, with its time