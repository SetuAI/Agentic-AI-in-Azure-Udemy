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

# time: measures how long each step takes.
import time

# Type helpers: Any = any type; TypedDict = a dict with named, typed keys.
from typing import Any, TypedDict

# Keeps each conversation's state in memory between questions.
from langgraph.checkpoint.memory import InMemorySaver

# StateGraph builds the workflow; START and END are its fixed entry and exit points.
from langgraph.graph import END, START, StateGraph

# OpenTelemetry: sends timing "spans" to Application Insights.
from opentelemetry import trace

# Shared config and connections.
from agent import context

# The three files that hold the 11 steps.
from agent.steps import answer, build_query, understand

# A tracer creates spans; the name shows up in Application Insights.
tracer = trace.get_tracer("fundgraph.agent")


# The "state": one dict passed from step to step. Each step returns the keys it changes.
class AgentState(TypedDict, total=False):
    """
    Everything the steps share. total=False: every field is optional, since
    each step fills in its own part as the question moves through.
    """
    # The user's question.
    question: str
    history: list[dict]          # earlier turns in this conversation (kept between questions)
    steps: list[dict]            # what each step did and how long it took (for the demo page)
    status: str | None           # completed | blocked | needs_clarification | failed
    # Why the question was blocked or failed (shown to the user).
    block_reason: str | None
    # The Prompt Shields result from guard_input.
    prompt_shield: dict
    # What understand worked out: intent, entities, metrics, data needed.
    understanding: dict | None
    # Names in the question matched to exact database values.
    entities: list[dict]
    # Names that could not be matched.
    unmatched: list[dict]
    # The tables selected for the SQL.
    tables: list[str]
    table_picks: list[dict]      # why each table was picked: entity, metric or search (with score)
    # How the selected tables join, found in the schema graph.
    join_path: dict | None
    # The SQL query that was written.
    sql: str | None
    sql_tables: list[str]        # the tables the final SQL actually reads
    # The model's plain-English explanation of the SQL.
    sql_explanation: str | None
    # The results of the SQL safety checks.
    checks: list[dict]
    # How many times SQL has been written so far.
    attempts: int
    failed_sql: list[dict]       # SQL attempts that failed a check or the run, before the retry
    # The last error (a failed check or a failed run); None when all is well.
    error: str | None
    # The query result: column names and rows.
    result: dict | None
    # Numbers worked out from the result (row count, totals, ...).
    summary: dict | None
    # The final plain-English answer.
    commentary: str | None
    # A short note each step leaves for the steps list (removed by _traced).
    _detail: Any


def _traced(name: str, step):
    """Wrap one step with timing, a steps-list entry and an OpenTelemetry span."""
    # The wrapped step: LangGraph calls this instead of the step itself.
    def run(state: AgentState) -> dict:
        # A precise start time.
        started = time.perf_counter()
        # Open a span named e.g. "agent.write_sql"; it closes at the end of the block.
        with tracer.start_as_current_span(f"agent.{name}") as span:
            # Run the real step; it returns the state keys it changed.
            update = step(state)
            # Attach the step's note to the span (at most 500 characters).
            span.set_attribute("agent.step.detail", str(update.get("_detail", ""))[:500])
        # How long the step took, in milliseconds.
        ms = round((time.perf_counter() - started) * 1000)
        # The first step starts a fresh list; every later step appends to it.
        earlier = [] if name == "guard_input" else (state.get("steps") or [])
        # Add this step's line; .pop removes "_detail" from the update so it is not stored in state.
        update["steps"] = earlier + [{"step": name, "ms": ms, "detail": update.pop("_detail", "")}]
        # LangGraph merges this dict into the state.
        return update
    # Return the wrapped function (a "closure" that remembers name and step).
    return run


# ---------------------------------------------------------------------------
# Where to go after the steps that can branch
# ---------------------------------------------------------------------------
# Blocked by Prompt Shields -> stop. Otherwise -> understand.
def _after_guard(state):
    return END if state.get("status") == "blocked" else "understand"


def _after_understand(state):
    # blocked = a request to change data, or off topic; needs_clarification = ask back.
    return END if state.get("status") in ("blocked", "needs_clarification") else "match_entities"


# write_sql refused (e.g. asked to change data) -> stop. Otherwise -> check_sql.
def _after_write(state):
    return END if state.get("status") == "blocked" else "check_sql"


def _retry_or(next_step: str):
    """After check_sql or run_sql: carry on if no error; otherwise retry, or stop when out of retries."""
    # The function LangGraph calls to choose the next step.
    def decide(state):
        # No error: go on to the next step.
        if not state.get("error"):
            return next_step
        # Retries left: go back and rewrite the SQL (the error is passed along).
        if state["attempts"] <= context.agent_config()["max_sql_retries"]:
            return "write_sql"
        # Out of retries.
        return "give_up"
    # Return the chooser function.
    return decide


# The final step when every SQL attempt failed: report why.
def _give_up(state):
    return {"status": "failed", "block_reason": f"Could not produce a working query after "
                                                f"{state['attempts']} attempts: {state['error']}"}


# Build and compile the workflow (done once, below).
def build():
    # An empty workflow whose state has the AgentState shape.
    graph = StateGraph(AgentState)
    # Step name -> the function that does it.
    steps = {
        "guard_input": understand.guard_input,
        "understand": understand.understand,
        "match_entities": understand.match_entities,
        "select_tables": build_query.select_tables,
        "find_join_path": build_query.find_join_path,
        "write_sql": build_query.write_sql,
        "check_sql": build_query.check_sql,
        "run_sql": build_query.run_sql,
        "summarise": answer.summarise,
        "write_commentary": answer.write_commentary,
        "remember": answer.remember,
    }
    # Add every step as a "node", wrapped with timing and tracing.
    for name, step in steps.items():
        graph.add_node(name, _traced(name, step))
    # give_up is not traced (it only writes a message).
    graph.add_node("give_up", _give_up)

    # Edges = arrows between steps. add_edge is always taken;
    # add_conditional_edges asks a function (e.g. _after_guard) which arrow to take,
    # and the list names every possible destination.
    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges("guard_input", _after_guard, ["understand", END])
    graph.add_conditional_edges("understand", _after_understand, ["match_entities", END])
    graph.add_edge("match_entities", "select_tables")
    graph.add_edge("select_tables", "find_join_path")
    graph.add_edge("find_join_path", "write_sql")
    graph.add_conditional_edges("write_sql", _after_write, ["check_sql", END])
    graph.add_conditional_edges("check_sql", _retry_or("run_sql"), ["run_sql", "write_sql", "give_up"])
    graph.add_conditional_edges("run_sql", _retry_or("summarise"), ["summarise", "write_sql", "give_up"])
    graph.add_edge("summarise", "write_commentary")
    graph.add_edge("write_commentary", "remember")
    graph.add_edge("remember", END)
    graph.add_edge("give_up", END)
    # compile() turns the description into a runnable agent; the checkpointer keeps conversation state.
    return graph.compile(checkpointer=InMemorySaver())


# Built once when the module is first imported, then reused for every question.
AGENT = build()


# Called by api/main.py for every question.
def ask(question: str, conversation_id: str) -> dict:
    """Run one question through the agent, within a conversation."""
    # One span around the whole question; every step's span sits inside it.
    with tracer.start_as_current_span("agent.ask") as span:
        # Run the workflow. thread_id picks which conversation's saved state to continue.
        state = AGENT.invoke({"question": question},
                             config={"configurable": {"thread_id": conversation_id}})
        # Record the outcome and the tables used, for Application Insights.
        span.set_attribute("agent.status", str(state.get("status")))
        span.set_attribute("agent.tables", ",".join(state.get("tables") or []))
    # The final state: status, SQL, result, commentary, steps, ...
    return state
