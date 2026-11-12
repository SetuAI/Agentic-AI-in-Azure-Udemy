"""
================================================================================
graph/build_graph.py  -  GRAPH PART 3: THE WIRING (what runs in what order)
================================================================================

PURPOSE
-------
nodes.py has 7 steps. But a list of steps isn't a pipeline yet. Something has
to say: which step runs FIRST, which runs NEXT, and where the run can take a
different road.

That's this file. It connects the 7 nodes into ONE GRAPH, and gives back a
graph we can run with one line:

    graph.invoke(state, thread)

THE KEY WORDS IN THIS FILE
--------------------------
    GRAPH               the whole pipeline: the nodes, and the arrows between them
    NODE                one step (from nodes.py)
    EDGE                an arrow: "after this node, run that node"
    CONDITIONAL EDGE    a FORK in the road: after this node, look at the
                        state, and pick ONE of several next nodes
    ROUTE FUNCTION      the small function that looks at the state and names
                        the road to take
    START, END          LangGraph's markers for where a run begins and ends
    CHECKPOINTER        saves the state after every step, so a paused run
                        can be picked up again later
    INTERRUPT           tells LangGraph to PAUSE just before a given node

THE GRAPH
---------
                    input_guardrail          safety, then topic
                          |
                +---------+---------+
             blocked              allowed
                |                    |
                |                retrieve       Azure AI Search, hybrid, per year
                |                    |
                |                 rerank        semantic ranker scores, 20 -> 5
                |                    |
                |                generate       Azure OpenAI
                |                    |
                |             output_guardrail  format, then grounding
                |                    |
                |          +---------+---------+
                |      grounded            needs review
                |          |                    |
                |      finalise         [ GRAPH PAUSES ]
                |          |                    |
                |          |             human_review
                +----------+---------+----------+
                                     |
                                    END

    Two forks:
        after input_guardrail     blocked → END       allowed → retrieve
        after output_guardrail    grounded → finalise  needs review → PAUSE

THE PAUSE (human-in-the-loop)
-----------------------------
interrupt_before=["human_review"] makes LangGraph STOP just before that node and
return control to the caller, with the state saved by a CHECKPOINTER. Later the
caller writes the reviewer's decision into the state and resumes. Nothing sits
blocked while waiting -- the wait can be a second or a week.

    How the web app (UI/server.py) and run_query.py use it:

        1. graph.invoke(state, thread)
               runs until the end, OR until the pause
        2. graph.get_state(thread).next
               ("human_review",) means: paused, waiting for a person
        3. graph.update_state(thread, {"review_decision": "approved", ...})
               writes the reviewer's decision into the SAVED state
        4. graph.invoke(None, thread)
               None means: don't start again; carry on from the pause

    "thread" is {"configurable": {"thread_id": "<a unique id>"}}. The thread
    id is how the checkpointer finds THIS run's saved state among many.

WHY ONE APP SERVICE INSTANCE
----------------------------
Our checkpointer is MemorySaver: it keeps paused runs in the app's MEMORY.
So the app must run as ONE copy on Azure App Service. With two copies, the
review click could reach the copy that doesn't have the paused run.

WHERE IT FITS
-------------
    graph/state.py        what the state holds
    graph/nodes.py        what each step does with the state
    graph/build_graph.py  THIS FILE: which step runs after which

    run_query.py and UI/server.py call build_graph() to get the graph.

WHAT IT NEEDS IN .env
---------------------
    Nothing to BUILD the graph. Running it needs the same settings as the
    nodes: AZURE_OPENAI_* and AZURE_SEARCH_*.

HOW TO RUN (from the project folder)
------------------------------------
    python graph/build_graph.py        (prints the graph as a Mermaid diagram -- no Azure calls)

    Copy the output into https://mermaid.live to see it as a picture.

EXPECTED OUTPUT
---------------
    A Mermaid diagram, starting with:
        ---
        config:
          flowchart:
            curve: linear
        ---
        graph TD;
            __start__([<p>__start__</p>]):::first
            input_guardrail(input_guardrail)
            ...
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from langgraph.checkpoint.memory import MemorySaver
# A CHECKPOINTER that keeps saved states in the app's memory.
# It saves the state after every step. That's what lets a paused run be
# picked up again, when the reviewer decides.
# Note: memory is lost when the app restarts. That's fine for this course.

from langgraph.graph import END, START, StateGraph
# From LangGraph:
#   StateGraph    the builder: we add nodes and edges to it, then compile it
#   START         the marker for where every run begins
#   END           the marker for where a run finishes

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the graph/ folder. But config.py, telemetry.py and our
# other folders are one folder ABOVE it. So we add the project folder to the
# front of sys.path, so the imports below can find them.
#   Path(__file__)   this file
#   .parent          its folder (graph/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. We use one setting from it:
#   config.INTERRUPT_ON_LOW_GROUNDING   True: pause for review when an answer
#                                       isn't grounded. False: never pause.

from graph.nodes import (
    finalise_node,
    generate_node,
    human_review_node,
    input_guardrail_node,
    output_guardrail_node,
    rerank_node,
    retrieve_node,
)
# The 7 nodes, from nodes.py. (Listed in alphabetical order, not run order.
# The ORDER they run in is set by the edges, below.)

from graph.state import GraphState
# The SHAPE of our state, from state.py.

from guardrails.factual import GROUNDED
# The verdict that means "no review needed". The second fork compares
# against it.

from telemetry import tracer
# The TRACER from telemetry.py. It records each step in Application Insights:
# its name, and how long it took.
# If monitoring is switched off (no connection string in .env), it does
# nothing at all. So the graph works the same either way.


# ── RECORD EACH STEP IN APPLICATION INSIGHTS ──────────────────────────────────

def traced(name: str, node):
    # Takes a node's name and the node itself. Gives back a WRAPPED node: it
    # does exactly the same job, and also records the step in Application
    # Insights.
    """NEW: wrap one node so each run of it is recorded as a span called node.<name>, with its duration."""
    # The function's one-line description.
    #
    # A SPAN is one timed record: "node.retrieve took 1.8 seconds". In
    # Application Insights, we can then see every step of every run, and
    # which step was slow.

    def run(state: GraphState) -> dict:
        # The wrapped node. LangGraph calls THIS instead of the real node.
        # It takes the state and gives back a dictionary, exactly like any node.

        with tracer.start_as_current_span(f"node.{name}"):
            # Open a span called, for example, "node.retrieve".
            # "with" means: the span stays open while the code inside runs,
            # and closes (recording the time taken) when it finishes, even
            # if the node fails.

            return node(state)
            # Run the REAL node, unchanged, and hand back exactly what it
            # gave back.

    return run
    # Give back the wrapped node: same input, same output, plus a span.


# ── THE TWO ROUTE FUNCTIONS (the two forks) ───────────────────────────────────
# A route function LOOKS at the state and NAMES the road to take.
# It changes nothing in the state. It only gives back a road name.

def route_after_input_guardrail(state: GraphState) -> str:
    # FORK 1, after input_guardrail. Gives back "allowed" or "blocked".
    """Read the state and name the next route. Changes nothing."""
    # The function's one-line description.

    return "allowed" if state["input_allowed"] else "blocked"
    # input_guardrail_node set input_allowed:
    #   True    →  "allowed"   →  go on to retrieve
    #   False   →  "blocked"   →  end the run (the refusal is already the
    #                             final_answer)


def route_after_output_guardrail(state: GraphState) -> str:
    # FORK 2, after output_guardrail. Gives back "grounded" or "needs_review".
    """Grounded answers finish; everything else goes to a person (unless review is switched off)."""
    # The function's one-line description.

    if not config.INTERRUPT_ON_LOW_GROUNDING:
        # Is human review switched OFF in config.py?

        return "grounded"
        # Then treat every answer as fine, and finish. Never pause.
        # (Useful when testing. In our project it's True, so review is on.)

    return "grounded" if state["grounding_verdict"] == GROUNDED else "needs_review"
    # output_guardrail_node set grounding_verdict:
    #   "grounded"       →  "grounded"       →  finalise
    #   anything else    →  "needs_review"   →  pause, then human_review


# ── BUILD THE GRAPH ───────────────────────────────────────────────────────────

def build_graph(enable_interrupt: bool = True, checkpointer=None):
    # Builds the whole pipeline, and gives back a graph we can run.
    #   enable_interrupt   True (normal): pause before human_review.
    #                      False: never pause.
    #   checkpointer       where to save states. Left out → a new MemorySaver.
    """Assemble the pipeline and return a runnable graph."""
    # The function's one-line description.

    builder = StateGraph(GraphState)
    # Start an empty graph, whose state has the shape of GraphState
    # (from state.py).

    # ── STEP 1 · ADD THE 7 NODES ──────────────────────────────────────────────
    # Each node gets a NAME (the text), and the function to run.
    # Each function is wrapped in traced(...), so every step is recorded in
    # Application Insights.

    builder.add_node("input_guardrail", traced("input_guardrail", input_guardrail_node))
    # Node 1: the input guardrail (safety, then topic).

    builder.add_node("retrieve", traced("retrieve", retrieve_node))
    # Node 2: hybrid search, per year.

    builder.add_node("rerank", traced("rerank", rerank_node))
    # Node 3: keep the best 5 per year.

    builder.add_node("generate", traced("generate", generate_node))
    # Node 4: gpt-5.1 writes the answer.

    builder.add_node("output_guardrail", traced("output_guardrail", output_guardrail_node))
    # Node 5: the output guardrail (format, then grounding).

    builder.add_node("human_review", traced("human_review", human_review_node))
    # Node 6: apply the reviewer's decision.

    builder.add_node("finalise", traced("finalise", finalise_node))
    # Node 7: the checked draft becomes the final answer.

    # ── STEP 2 · ADD THE EDGES (the arrows) ───────────────────────────────────

    builder.add_edge(START, "input_guardrail")
    # The first arrow: every run starts with the input guardrail.

    builder.add_conditional_edges(
        # FORK 1: after input_guardrail, the run can go two ways.

        "input_guardrail",
        # After this node...

        route_after_input_guardrail,
        # ...ask this route function which road to take...

        {"allowed": "retrieve", "blocked": END},
        # ...and follow the matching road:
        #   "allowed"  →  retrieve
        #   "blocked"  →  END (the run finishes; the refusal is already the
        #                 final_answer)
    )

    builder.add_edge("retrieve", "rerank")
    # A straight arrow: after retrieve, always rerank.

    builder.add_edge("rerank", "generate")
    # After rerank, always generate.

    builder.add_edge("generate", "output_guardrail")
    # After generate, always the output guardrail.

    builder.add_conditional_edges(
        # FORK 2: after output_guardrail, the run can go two ways.

        "output_guardrail",
        # After this node...

        route_after_output_guardrail,
        # ...ask this route function...

        {"grounded": "finalise", "needs_review": "human_review"},
        # ...and follow the matching road:
        #   "grounded"      →  finalise
        #   "needs_review"  →  human_review (the graph PAUSES before it)
    )

    builder.add_edge("finalise", END)
    # After finalise, the run finishes.

    builder.add_edge("human_review", END)
    # After human_review, the run finishes too.
    # So every road ends at END.

    # ── STEP 3 · COMPILE ──────────────────────────────────────────────────────

    return builder.compile(
        # Turn the description (nodes + edges) into a graph we can RUN.
        # Compiling also checks the wiring: for example, a road that points
        # to a node that doesn't exist would be an error here.

        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
        # Where to save the state after every step.
        # If a checkpointer was passed in, use it; otherwise, a new MemorySaver.
        # Without a checkpointer, the graph couldn't pause and resume.

        interrupt_before=["human_review"] if enable_interrupt else [],
        # THE PAUSE: stop just BEFORE the human_review node.
        # The run hands control back to the caller, with the state saved.
        # The caller writes in the reviewer's decision, and resumes.
        # If enable_interrupt is False, the list is empty: never pause.
    )


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python graph/build_graph.py
    # When run_query.py or the web app imports this file, nothing below runs.

    print(build_graph().get_graph().draw_mermaid())
    # Build the graph, and print it as a MERMAID diagram: a text format that
    # describes boxes and arrows.
    #   build_graph()     the compiled graph
    #   .get_graph()      its structure: nodes and edges
    #   .draw_mermaid()   that structure, written as Mermaid text
    # No question is run, so there's no Azure call.
    # Paste the output into https://mermaid.live to see the picture: a good
    # way to check the wiring matches the diagram at the top of this file.