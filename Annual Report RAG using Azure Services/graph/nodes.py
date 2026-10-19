"""
================================================================================
GRAPH PART 2 - THE NODES (what each step does)
================================================================================

A node is a function with one shape:

    def some_node(state) -> dict:
        return {"field_i_changed": new_value}

It reads the state, does ONE job, and returns only the fields it changed.
Nodes never call each other and never decide what runs next -- that lives in
build_graph.py. So a step can be swapped (Chroma -> Azure AI Search, local
cross-encoder -> semantic ranker) without touching its neighbours.

    input_guardrail   safety, then topic                 (Azure OpenAI only if needed)
    retrieve          hybrid search, per year            (Azure AI Search + Azure OpenAI)
    rerank            semantic ranker scores, 20 -> 5    (no new call -- scores came with search)
    generate          write the comparison               (Azure OpenAI, gpt-4o)
    output_guardrail  format, then grounding             (Azure OpenAI only if needed)
    human_review      apply the reviewer's decision
    finalise          approve an answer that needed no review
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths
from typing import Dict, List                   # type hints

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # deployment names
from azure_clients import openai_client         # Azure OpenAI, for writing the answer
from graph.state import GraphState              # the shape of the state
from guardrails.factual import GROUNDED, NEEDS_REVIEW, check_grounding, collect_source_texts  # grounding check
from guardrails.format import check_format      # format check
from guardrails.safety import BLOCK, check_safety, refusal_message  # safety check
from guardrails.topic import REFUSAL_MESSAGE, check_topic  # topic check
from retrieval.rerank import rerank             # semantic-ranker cut, 20 -> 5
from retrieval.search import search_both_years  # hybrid search in each year


# ==============================================================================
# NODE 1 - INPUT GUARDRAIL
# ==============================================================================

def input_guardrail_node(state: GraphState) -> dict:
    """Refuse unsafe or off-topic questions before anything costs money."""
    safety = check_safety(state["question"])    # pattern check -- free, no Azure call
    if safety.verdict == BLOCK:                 # personal data or an advice request?
        return {"input_allowed": False,         # stop here
                "input_block_reason": safety.reason,          # why, for the logs
                "final_answer": refusal_message(safety)}      # what the user sees

    topic = check_topic(state["question"])      # keyword check (free), then similarity (one embedding call)
    if topic.verdict == BLOCK:                  # off topic?
        return {"input_allowed": False,         # stop here
                "input_block_reason": topic.reason,           # why, for the logs
                "final_answer": REFUSAL_MESSAGE}              # what the user sees

    return {"input_allowed": True}              # passed both checks


# ==============================================================================
# NODES 2 AND 3 - RETRIEVE, THEN RERANK
# ==============================================================================
# Two nodes, not one, so the state holds BOTH the hybrid order and the semantic
# order -- and you can see exactly what the semantic ranker changed.
# ==============================================================================

def retrieve_node(state: GraphState) -> dict:
    """Hybrid search inside each fiscal year separately."""
    return {"candidate_chunks": search_both_years(state["question"], state["company"])}  # {"FY2025": [...], ...}


def rerank_node(state: GraphState) -> dict:
    """Keep each year's best 5 by semantic ranker score -- each year separately, so neither year dominates."""
    return {"reranked_chunks": {                # same keys, shorter lists
        year: rerank(candidates)                # 20 -> 5 within this year
        for year, candidates in state["candidate_chunks"].items()
    }}


# ==============================================================================
# NODE 4 - GENERATE
# ==============================================================================

GENERATION_PROMPT = """You answer questions about {company}'s annual reports by \
comparing two financial years.

Use ONLY the extracts below. If they do not contain enough information to answer,
say so plainly rather than filling the gap from general knowledge.

If the extracts show no meaningful change between the two years, say that. Do not
manufacture a change because the question implies one.

When you state a figure, it must appear in the extracts. Do not calculate,
estimate or round beyond what is written.

Do not give investment advice or opinions on whether the company is a good
investment. Describe what the reports say.

Structure your answer as:
- what FY2025 said
- what FY2026 said
- what changed, or that nothing meaningful changed

=== FY2025 EXTRACTS ===
{fy2025_extracts}

=== FY2026 EXTRACTS ===
{fy2026_extracts}

QUESTION: {question}"""


def format_extracts(results: List[Dict]) -> str:
    """Number each chunk [1], [2]... -- or say plainly that this year found nothing."""
    if not results:                             # nothing retrieved for this year?
        return "(no relevant extracts found for this year)"  # say so, so the model does not fill the gap itself
    return "\n\n".join(f"[{n}] {r['text']}" for n, r in enumerate(results, start=1))  # numbered extracts


def generate_node(state: GraphState) -> dict:
    """Write the comparison with Azure OpenAI."""
    from openai import BadRequestError          # the error Azure raises when its content filter blocks a request

    reranked = state["reranked_chunks"]         # the 5 best chunks per year
    prompt = GENERATION_PROMPT.format(          # fill in the template
        company=state["company"],               # which company
        question=state["question"],             # what was asked
        fy2025_extracts=format_extracts(reranked.get("FY2025", [])),  # first year's evidence
        fy2026_extracts=format_extracts(reranked.get("FY2026", [])),  # second year's evidence
    )

    try:
        response = openai_client().chat.completions.create(  # one call to Azure OpenAI
            model=config.GENERATION_DEPLOYMENT, # the capable deployment (gpt-4o)
            temperature=0.0,                    # same extracts -> same answer, so checks are reproducible
            messages=[{"role": "user", "content": prompt}],  # one instruction message
        )
        return {"draft_answer": response.choices[0].message.content.strip()}  # the answer text

    except BadRequestError as error:            # Azure-specific: every request passes through a CONTENT FILTER
        if "content_filter" not in str(error):  # some other bad request? that is a real bug -- let it surface
            raise
        # The filter blocked the prompt or the answer. We do not crash: the
        # format check will flag this draft, and a reviewer will see it.
        return {"draft_answer": "(The Azure OpenAI content filter blocked this answer.)"}


# ==============================================================================
# NODE 5 - OUTPUT GUARDRAIL
# ==============================================================================

def output_guardrail_node(state: GraphState) -> dict:
    """Format first (free), then grounding. Records a verdict; build_graph.py routes on it."""
    shape = check_format(state["draft_answer"]) # pattern rules -- no Azure call
    if not shape.passed:                        # badly shaped?
        return {"grounding_verdict": NEEDS_REVIEW,  # send to a person...
                "grounding_score": 0.0,             # ...without spending a grounding call
                "unsupported_items": [f"format: {p}" for p in shape.problems],  # every broken rule
                "review_required": True}

    decision = check_grounding(                 # numbers (free), then claims (one Azure call) if needed
        state["draft_answer"],                  # the answer to check
        collect_source_texts(state["reranked_chunks"]),  # its sources, still split by year
    )
    return {"grounding_verdict": decision.verdict,          # grounded / needs_review / ungrounded
            "grounding_score": decision.score,              # share of numbers traced
            "unsupported_items": decision.unsupported_items,  # what failed
            "review_required": decision.verdict != GROUNDED}  # anything but grounded needs a person


# ==============================================================================
# NODES 6 AND 7 - HUMAN REVIEW, FINALISE
# ==============================================================================

def human_review_node(state: GraphState) -> dict:
    """
    Runs AFTER a person has decided. The pause itself is not here -- LangGraph
    stops BEFORE this node (see build_graph.py) and waits for the decision.
    """
    decision = state.get("review_decision")     # written into the state by the reviewer
    if decision == "approved":                  # approved: show their correction, or the draft as written
        return {"final_answer": state.get("review_note") or state["draft_answer"]}
    return {"final_answer": (                   # rejected, or (unexpectedly) no decision: never show an unreviewed answer
        "I wasn't able to answer that reliably from the annual reports. "
        "Please try rephrasing, or refer to the reports directly."
    )}


def finalise_node(state: GraphState) -> dict:
    """The grounded path: the draft becomes the final answer. One exit point for every route."""
    return {"final_answer": state["draft_answer"]}  # copy the checked draft across
