"""
================================================================================
GRAPH PART 3 - THE WIRING (what runs in what order)
================================================================================

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

THE PAUSE
---------
interrupt_before=["human_review"] makes LangGraph STOP just before that node and
return control to the caller, with the state saved by a CHECKPOINTER. Later the
caller writes the reviewer's decision into the state and resumes. Nothing sits
blocked while waiting -- the wait can be a second or a week.

HOW TO RUN
----------
    python graph/build_graph.py        (prints the graph as a Mermaid diagram -- no Azure calls)
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths

from langgraph.checkpoint.memory import MemorySaver  # keeps paused runs in memory
from langgraph.graph import END, START, StateGraph   # the graph builder and its start/end markers

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # INTERRUPT_ON_LOW_GROUNDING
from graph.nodes import (                       # the seven steps
    finalise_node,
    generate_node,
    human_review_node,
    input_guardrail_node,
    output_guardrail_node,
    rerank_node,
    retrieve_node,
)
from graph.state import GraphState              # the shape of the state
from guardrails.factual import GROUNDED         # the verdict that means "no review needed"


def route_after_input_guardrail(state: GraphState) -> str:
    """Read the state and name the next route. Changes nothing."""
    return "allowed" if state["input_allowed"] else "blocked"


def route_after_output_guardrail(state: GraphState) -> str:
    """Grounded answers finish; everything else goes to a person (unless review is switched off)."""
    if not config.INTERRUPT_ON_LOW_GROUNDING:   # review switched off in config?
        return "grounded"                       # treat everything as approved
    return "grounded" if state["grounding_verdict"] == GROUNDED else "needs_review"


def build_graph(enable_interrupt: bool = True, checkpointer=None):
    """Assemble the pipeline and return a runnable graph."""
    builder = StateGraph(GraphState)            # a graph whose state has GraphState's shape

    builder.add_node("input_guardrail", input_guardrail_node)    # register each step under a name
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("rerank", rerank_node)
    builder.add_node("generate", generate_node)
    builder.add_node("output_guardrail", output_guardrail_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("finalise", finalise_node)

    builder.add_edge(START, "input_guardrail")  # every run starts with the input guardrail
    builder.add_conditional_edges(              # a fork: the route function picks the branch
        "input_guardrail",                      # after this node...
        route_after_input_guardrail,            # ...ask this function...
        {"allowed": "retrieve", "blocked": END},  # ...and follow the matching branch
    )
    builder.add_edge("retrieve", "rerank")      # straight line: retrieve -> rerank
    builder.add_edge("rerank", "generate")      # rerank -> generate
    builder.add_edge("generate", "output_guardrail")  # generate -> output guardrail
    builder.add_conditional_edges(              # the second fork
        "output_guardrail",
        route_after_output_guardrail,
        {"grounded": "finalise", "needs_review": "human_review"},
    )
    builder.add_edge("finalise", END)           # both branches end the run
    builder.add_edge("human_review", END)

    return builder.compile(                     # turn the description into something runnable
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),  # where paused runs are saved
        interrupt_before=["human_review"] if enable_interrupt else [],             # THE PAUSE
    )


if __name__ == "__main__":                      # only when run directly
    print(build_graph().get_graph().draw_mermaid())  # paste the output into https://mermaid.live for a picture
