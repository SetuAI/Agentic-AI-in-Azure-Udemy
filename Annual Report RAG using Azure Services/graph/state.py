"""
================================================================================
graph/state.py  -  GRAPH PART 1: THE STATE
================================================================================

PURPOSE
-------
This file describes the STATE: the ONE dictionary that travels through our
whole pipeline, from the question to the final answer.

It lists every field the pipeline can fill in, in the order they get filled.
So reading this file from top to bottom is like watching one question travel
through the pipeline.

WHAT IS "STATE"?
----------------
Our pipeline has 7 steps (input guardrail, retrieve, rerank, generate, output
guardrail, human review, finalise). Each step needs something from an earlier
step:

    rerank    needs the 20 chunks that retrieve found
    generate  needs the 5 chunks that rerank kept
    ...

So the steps need a way to hand things to each other.

THE STATE is that way. It is one dictionary. LangGraph gives it to each step.
Each step READS what it needs from it, does its job, and gives back ONLY the
fields it changed. LangGraph then updates the dictionary, and gives it to the
next step.

In simple words: the state is the pipeline's shared notebook. Every step
reads from it, and writes its result into it.

WHAT PROBLEM DOES THIS FILE SOLVE
---------------------------------
If every step just made up its own field names, one typo ("reranked_chunk"
instead of "reranked_chunks") would break the pipeline in a way that's hard
to find. This file writes down every field ONCE, with its name and type. So
everyone (and every step) agrees on what the state holds.

WHERE IT FITS
-------------
    graph/state.py        THIS FILE: what the state holds
    graph/nodes.py        what each step does with the state
    graph/build_graph.py  which step runs after which

HOW TO RUN (from the project folder)
------------------------------------
    python graph/state.py

    No Azure call. It just prints a fresh, empty state.

EXPECTED OUTPUT
---------------
      question             'How did revenue change?'
      company              'Infosys'
      input_allowed        True
      input_block_reason   None
      candidate_chunks     None
      reranked_chunks      None
      draft_answer         None
      grounding_verdict    None
      grounding_score      None
      unsupported_items    None
      review_required      False
      review_decision      None
      review_note          None
      final_answer         None
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

from typing import Dict, List, Optional, TypedDict
# These are TYPE HINTS. They DESCRIBE what each field holds.
#   TypedDict       a dictionary whose KEYS are fixed and named in advance,
#                   each with its own type. That's exactly what our state is.
#   Dict, List      a dictionary, a list
#   Optional[...]   "this thing, or nothing (None)". Most fields start as
#                   None, because the step that fills them hasn't run yet.


# ── THE STATE: EVERY FIELD, IN THE ORDER IT GETS FILLED ───────────────────────

class GraphState(TypedDict):
    # The SHAPE of our state: which keys it has, and what type each one holds.
    # "(TypedDict)" means: this class describes a dictionary.
    # At runtime, the state is still a plain Python dictionary. This class
    # only DESCRIBES it, for LangGraph and for anyone reading the code.
    """Every field the pipeline can hold."""
    # The class's one-line description.

    # --- filled in by the caller ---
    question: str
    # The user's question, e.g. "How did revenue change?".
    # Filled in by whoever starts the run: run_query.py or the web app.

    company: str
    # Which company the question is about.
    # Also filled in by the caller.

    # --- input guardrails (safety, then topic) ---
    input_allowed: bool
    # Did the question pass safety.py and topic.py?
    #   True    yes, carry on to search
    #   False   no, the run stops here
    # Filled in by: input_guardrail_node.

    input_block_reason: Optional[str]
    # If the question was refused, WHY. For example
    # "question asks for investment advice".
    # This is for the logs. The user sees a polite message instead.
    # Filled in by: input_guardrail_node.

    # --- retrieval (Azure AI Search) ---
    candidate_chunks: Optional[Dict[str, List[Dict]]]
    # The chunks search.py found: about 20 per year, in hybrid order.
    #   {"FY2025": [20 chunks], "FY2026": [20 chunks]}
    # Each chunk is a dictionary: its text, company, year, and both scores.
    # Filled in by: retrieve_node.

    reranked_chunks: Optional[Dict[str, List[Dict]]]
    # The best 5 per year, after rerank.py sorted them by semantic score.
    #   {"FY2025": [5 chunks], "FY2026": [5 chunks]}
    # Filled in by: rerank_node.
    #
    # Here, we can notice: we KEEP both candidate_chunks and reranked_chunks.
    # So we can always compare them, and see what the semantic ranker changed.

    # --- generation (Azure OpenAI) ---
    draft_answer: Optional[str]
    # gpt-5.1's answer, BEFORE the output guardrails have checked it.
    # That's why it's called a DRAFT. It may never reach the user.
    # Filled in by: generate_node.

    # --- output guardrails (format, then grounding) ---
    grounding_verdict: Optional[str]
    # The result of format.py and factual.py:
    #   "grounded"       everything traced back to the chunks
    #   "needs_review"   something didn't, or we couldn't tell
    #   "ungrounded"     the answer is unusable, e.g. empty
    # Filled in by: output_guardrail_node.

    grounding_score: Optional[float]
    # The share of the answer's numbers that traced back, from 0 to 1.
    #   1.0 = every number traced back
    # Filled in by: output_guardrail_node.

    unsupported_items: Optional[List[str]]
    # EXACTLY what failed, e.g.
    #   ["21.1 (attributed to FY2026, found only in another year)"]
    # The human reviewer sees this list, so they know where to look.
    # Filled in by: output_guardrail_node.

    review_required: bool
    # Does a person need to check this answer?
    #   True    the graph will PAUSE before human_review
    #   False   the answer goes straight to the user
    # Filled in by: output_guardrail_node.

    # --- human review ---
    review_decision: Optional[str]
    # The reviewer's decision: "approved" or "rejected".
    # Here, we can notice: NO NODE fills this in. It's written into the
    # state from OUTSIDE the graph, by the web app (or run_query.py), while
    # the graph is paused. Then the graph is resumed.

    review_note: Optional[str]
    # If the reviewer FIXED the answer, their corrected text goes here.
    # Also written in from outside the graph, while it's paused.

    # --- the result ---
    final_answer: Optional[str]
    # The answer the user actually sees. Every route through the graph
    # ends by filling this in:
    #   refused question   → the refusal message      (input_guardrail_node)
    #   grounded answer    → the draft, as written    (finalise_node)
    #   reviewed answer    → approved or rejected     (human_review_node)


# ── A FRESH, EMPTY STATE ──────────────────────────────────────────────────────

def create_initial_state(question: str, company: str) -> GraphState:
    # Takes the question and the company. Gives back a brand-new state, with
    # EVERY field set: the question and company filled in, everything else
    # empty or at its starting value.
    # run_query.py and the web app call this to start every run.
    """A fresh state. Every field is set (to None if empty), so reading one never raises KeyError."""
    # The function's one-line description.
    #
    # Why set EVERY field, even the empty ones? Because if a key were
    # missing, reading it, like state["draft_answer"], would crash with a
    # KeyError. With every key set (to None if empty), reading it is always safe.

    return GraphState(
        # Build the dictionary, one field per line.

        question=question,
        # The user's question.

        company=company,
        # The company.

        input_allowed=True,
        # Start as True. Only a guardrail can change it to False.

        input_block_reason=None,
        # Nothing has been refused yet.

        candidate_chunks=None,
        # Search hasn't run yet.

        reranked_chunks=None,
        # Rerank hasn't run yet.

        draft_answer=None,
        # gpt-5.1 hasn't written anything yet.

        grounding_verdict=None,
        # The output guardrails haven't run yet.

        grounding_score=None,
        # No score yet.

        unsupported_items=None,
        # Nothing has failed yet.

        review_required=False,
        # Start as False. Only the output guardrail can change it to True.

        review_decision=None,
        # No reviewer has decided anything.

        review_note=None,
        # No correction yet.

        final_answer=None,
        # Nothing to show the user yet.
    )


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python graph/state.py
    # When the pipeline imports this file, nothing below runs.

    for key, value in create_initial_state("How did revenue change?", "Infosys").items():
        # Make a fresh state for a demo question, then go through it one
        # field at a time.
        #   .items()   gives each key and its value together

        print(f"  {key:<20} {value!r}")
        # Print the field's name and its value, one per line:
        #   question             'How did revenue change?'
        #   input_allowed        True
        #
        #   :<20   left-align the name in 20 characters, so the values line up
        #   !r     print the value as Python would write it: text in quotes,
        #          None as None. So we can SEE that a field is empty.