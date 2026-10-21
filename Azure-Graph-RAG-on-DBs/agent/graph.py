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

import time
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from agent import context
from agent.steps import answer, build_query, understand

tracer = trace.get_tracer("fundgraph.agent")


class AgentState(TypedDict, total=False):
    """
    Everything the steps share. total=False: every field is optional, since
    each step fills in its own part as the question moves through.
    """
    question: str
    history: list[dict]          # earlier turns in this conversation (kept between questions)
    steps: list[dict]            # what each step did and how long it took (for the demo page)
    status: str | None           # completed | blocked | needs_clarification | failed
    block_reason: str | None
    prompt_shield: dict
    understanding: dict | None
    entities: list[dict]
    unmatched: list[dict]
    tables: list[str]
    table_picks: list[dict]      # why each table was picked: entity, metric or search (with score)
    join_path: dict | None
    sql: str | None
    sql_tables: list[str]        # the tables the final SQL actually reads
    sql_explanation: str | None
    checks: list[dict]
    attempts: int
    failed_sql: list[dict]       # SQL attempts that failed a check or the run, before the retry
    error: str | None
    result: dict | None
    summary: dict | None
    commentary: str | None
    _detail: Any


def _traced(name: str, step):
    """Wrap one step with timing, a steps-list entry and an OpenTelemetry span."""
    def run(state: AgentState) -> dict:
        started = time.perf_counter()
        with tracer.start_as_current_span(f"agent.{name}") as span:
            update = step(state)
            span.set_attribute("agent.step.detail", str(update.get("_detail", ""))[:500])
        ms = round((time.perf_counter() - started) * 1000)
        # The first step starts a fresh list; every later step appends to it.
        earlier = [] if name == "guard_input" else (state.get("steps") or [])
        update["steps"] = earlier + [{"step": name, "ms": ms, "detail": update.pop("_detail", "")}]
        return update
    return run


# ---------------------------------------------------------------------------
# Where to go after the steps that can branch
# ---------------------------------------------------------------------------
def _after_guard(state):
    return END if state.get("status") == "blocked" else "understand"


def _after_understand(state):
    # blocked = a request to change data, or off topic; needs_clarification = ask back.
    return END if state.get("status") in ("blocked", "needs_clarification") else "match_entities"


def _after_write(state):
    return END if state.get("status") == "blocked" else "check_sql"


def _retry_or(next_step: str):
    """After check_sql or run_sql: carry on if no error; otherwise retry, or stop when out of retries."""
    def decide(state):
        if not state.get("error"):
            return next_step
        if state["attempts"] <= context.agent_config()["max_sql_retries"]:
            return "write_sql"
        return "give_up"
    return decide


def _give_up(state):
    return {"status": "failed", "block_reason": f"Could not produce a working query after "
                                                f"{state['attempts']} attempts: {state['error']}"}


def build():
    graph = StateGraph(AgentState)
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
    for name, step in steps.items():
        graph.add_node(name, _traced(name, step))
    graph.add_node("give_up", _give_up)

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
    return graph.compile(checkpointer=InMemorySaver())


# Built once when the module is first imported, then reused for every question.
AGENT = build()


def ask(question: str, conversation_id: str) -> dict:
    """Run one question through the agent, within a conversation."""
    with tracer.start_as_current_span("agent.ask") as span:
        state = AGENT.invoke({"question": question},
                             config={"configurable": {"thread_id": conversation_id}})
        span.set_attribute("agent.status", str(state.get("status")))
        span.set_attribute("agent.tables", ",".join(state.get("tables") or []))
    return state
