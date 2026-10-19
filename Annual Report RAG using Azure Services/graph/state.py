"""
================================================================================
GRAPH PART 1 - THE STATE (everything that flows through the pipeline)
================================================================================

LangGraph passes ONE dictionary from step to step. Each step (node) reads what
it needs from it and returns only the fields it changed. This file lists every
field, in the order they get filled in -- reading it top to bottom is like
watching a question travel through the pipeline.

HOW TO RUN
----------
    python graph/state.py
================================================================================
"""

from typing import Dict, List, Optional, TypedDict  # TypedDict: a dictionary with named, typed keys


class GraphState(TypedDict):
    """Every field the pipeline can hold."""
    # --- filled in by the caller ---
    question: str                               # what the user asked
    company: str                                # "Infosys" or "Eternal"

    # --- input guardrails (safety, then topic) ---
    input_allowed: bool                         # False = the question was refused
    input_block_reason: Optional[str]           # why it was refused (for the logs)

    # --- retrieval (Azure AI Search) ---
    candidate_chunks: Optional[Dict[str, List[Dict]]]  # ~20 per year, in hybrid-search order
    reranked_chunks: Optional[Dict[str, List[Dict]]]   # 5 per year, in semantic-ranker order

    # --- generation (Azure OpenAI) ---
    draft_answer: Optional[str]                 # the model's answer, before any check

    # --- output guardrails (format, then grounding) ---
    grounding_verdict: Optional[str]            # "grounded", "needs_review" or "ungrounded"
    grounding_score: Optional[float]            # share of numbers that traced back
    unsupported_items: Optional[List[str]]      # exactly what failed -- shown to the reviewer
    review_required: bool                       # True = the graph will pause for a person

    # --- human review ---
    review_decision: Optional[str]              # "approved" or "rejected", written in by the reviewer
    review_note: Optional[str]                  # the reviewer's corrected answer, if they wrote one

    # --- the result ---
    final_answer: Optional[str]                 # what the user actually sees


def create_initial_state(question: str, company: str) -> GraphState:
    """A fresh state. Every field is set (to None if empty), so reading one never raises KeyError."""
    return GraphState(
        question=question,                      # from the caller
        company=company,                        # from the caller
        input_allowed=True,                     # allowed until a guardrail says otherwise
        input_block_reason=None,                # nothing blocked yet
        candidate_chunks=None,                  # nothing retrieved yet
        reranked_chunks=None,                   # nothing reranked yet
        draft_answer=None,                      # nothing generated yet
        grounding_verdict=None,                 # nothing checked yet
        grounding_score=None,                   # nothing measured yet
        unsupported_items=None,                 # nothing flagged yet
        review_required=False,                  # no review needed until a check says so
        review_decision=None,                   # no reviewer yet
        review_note=None,                       # no correction yet
        final_answer=None,                      # no answer yet
    )


if __name__ == "__main__":                      # only when run directly
    for key, value in create_initial_state("How did revenue change?", "Infosys").items():  # a fresh state
        print(f"  {key:<20} {value!r}")         # !r shows None and strings clearly
