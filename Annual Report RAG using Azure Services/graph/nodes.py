"""
================================================================================
graph/nodes.py  -  GRAPH PART 2: THE NODES (what each step does)
================================================================================

PURPOSE
-------
This file holds the 7 STEPS of our pipeline. In LangGraph, each step is
called a NODE.

WHAT IS A NODE?
---------------
A node is a Python function with one fixed shape:

    def some_node(state) -> dict:
        return {"field_i_changed": new_value}

It does three things:
    1. READS what it needs from the state
    2. does ONE job
    3. gives back ONLY the fields it changed

LangGraph then writes those fields into the state, and moves on.

Here, we can notice two things a node NEVER does:
    - it never calls another node
    - it never decides what runs next
That's the job of build_graph.py.

WHY DOES THAT MATTER?
---------------------
Because then each step can be changed on its own. For example, this project
first used Chroma for search, then Azure AI Search. Only retrieve_node's
helper changed. Nothing else had to.

THE 7 NODES
-----------
    1  input_guardrail    safety.py, then topic.py           question allowed?
    2  retrieve           search.py, once per year           20 chunks per year
    3  rerank             rerank.py, once per year           best 5 per year
    4  generate           gpt-5.1 writes the comparison      the draft answer
    5  output_guardrail   format.py, then factual.py         does it need review?
    6  human_review       apply the reviewer's decision      (after the pause)
    7  finalise           the checked draft becomes final    (no review needed)

    Which Azure services each one uses:
        input_guardrail    Azure OpenAI, only if topic.py needs an embedding
        retrieve           Azure AI Search + Azure OpenAI (to embed the question)
        rerank             none: the semantic scores came back with the search
        generate           Azure OpenAI (gpt-5.1)
        output_guardrail   Azure OpenAI, only if factual.py needs Check 2
        human_review       none
        finalise           none

WHERE IT FITS
-------------
    graph/state.py        what the state holds
    graph/nodes.py        THIS FILE: what each step does with the state
    graph/build_graph.py  which step runs after which

WHAT IT NEEDS IN .env
---------------------
    The AZURE_OPENAI_* and AZURE_SEARCH_* settings, used by the steps it
    calls. This file has no demo of its own: the nodes run inside the graph.
    To run them all:   python run_query.py

CHANGE FROM THE ORIGINAL FILE (for gpt-5.1)
-------------------------------------------
    generate_node used to send temperature=0.0. gpt-5.1 rejects it, so the
    call failed every time. That one line is removed. Nothing else in the
    code has changed.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import Dict, List
# These are TYPE HINTS. They only DESCRIBE what goes in and what comes out.
#   List[Dict]   a list of dictionaries, like the 5 chunks of one year

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the graph/ folder. But config.py and our other folders
# (guardrails/, retrieval/) are one folder ABOVE it. So we add the project
# folder to the front of sys.path, so the imports below can find them.
#   Path(__file__)   this file
#   .parent          its folder (graph/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. We use one setting from it:
#   config.GENERATION_DEPLOYMENT   the model that writes the answer (gpt-5.1)

from azure_clients import openai_client
# A ready-made connection to Azure OpenAI. generate_node uses it to write
# the answer.

from graph.state import GraphState
# The SHAPE of our state, from state.py. Every node takes a GraphState.

from guardrails.factual import GROUNDED, NEEDS_REVIEW, check_grounding, collect_source_texts
# From factual.py (output guardrail 2):
#   GROUNDED, NEEDS_REVIEW   two of the verdicts
#   check_grounding          the fact check
#   collect_source_texts     turns the chunks into plain texts, split by year

from guardrails.format import check_format
# From format.py (output guardrail 1): the four format rules.

from guardrails.safety import BLOCK, check_safety, refusal_message
# From safety.py (input guardrail 1):
#   BLOCK             the "stop" verdict
#   check_safety      the personal data and advice check
#   refusal_message   picks the right message for a blocked question

from guardrails.topic import REFUSAL_MESSAGE, check_topic
# From topic.py (input guardrail 2):
#   REFUSAL_MESSAGE   the message for an off-topic question
#   check_topic       the keyword and similarity check

from retrieval.rerank import rerank
# From rerank.py: keeps the best 5 of 20, by semantic score.

from retrieval.search import search_both_years
# From search.py: hybrid search, once for each year.


# ==============================================================================
# NODE 1 - INPUT GUARDRAIL
# ==============================================================================
# The first step of every run. It decides whether the question may continue.
# If not, it writes the refusal as the final answer, and the run stops.

def input_guardrail_node(state: GraphState) -> dict:
    # Takes the state. Gives back whether the question is allowed, and, if
    # not, why, and the message the user sees.
    """Refuse unsafe or off-topic questions before anything costs money."""
    # The function's one-line description.

    safety = check_safety(state["question"])
    # INPUT GUARDRAIL 1: safety.py. Personal data? A request for advice?
    # Free: no Azure call.

    if safety.verdict == BLOCK:
        # Did safety.py block it?

        return {"input_allowed": False,
                "input_block_reason": safety.reason,
                "final_answer": refusal_message(safety)}
        # Then STOP here. Give back three fields:
        #   input_allowed       False: build_graph.py sees this and ends the run
        #   input_block_reason  why, for the logs, e.g. "question contains
        #                       personal data (PAN)"
        #   final_answer        the polite message the user sees
        #
        # Here, we can notice: topic.py never runs for this question. So a PAN
        # in the question never reaches Azure OpenAI.

    topic = check_topic(state["question"])
    # INPUT GUARDRAIL 2: topic.py. Is the question about the annual reports?
    # Free if it has a finance keyword; otherwise one small embedding call.

    if topic.verdict == BLOCK:
        # Did topic.py block it?

        return {"input_allowed": False,
                "input_block_reason": topic.reason,
                "final_answer": REFUSAL_MESSAGE}
        # Then STOP here, the same way: not allowed, why, and the message
        # the user sees.

    return {"input_allowed": True}
    # It passed both checks. Give back just one field: allowed.
    # Every other field stays as it was.


# ==============================================================================
# NODES 2 AND 3 - RETRIEVE, THEN RERANK
# ==============================================================================
# Two nodes, not one, so the state holds BOTH the hybrid order and the semantic
# order -- and you can see exactly what the semantic ranker changed.
# ==============================================================================

def retrieve_node(state: GraphState) -> dict:
    # Takes the state. Gives back about 20 chunks per year, from Azure AI Search.
    """Hybrid search inside each fiscal year separately."""
    # The function's one-line description.

    return {"candidate_chunks": search_both_years(state["question"], state["company"])}
    # Run search_both_years() from search.py: the filter, hybrid search and
    # the semantic ranker, once for FY2025 and once for FY2026.
    # Store the result in candidate_chunks:
    #   {"FY2025": [20 chunks], "FY2026": [20 chunks]}


def rerank_node(state: GraphState) -> dict:
    # Takes the state. Gives back the best 5 chunks per year.
    """Keep each year's best 5 by semantic ranker score -- each year separately, so neither year dominates."""
    # The function's one-line description.

    return {"reranked_chunks": {
        # Build a new dictionary for reranked_chunks, with the same keys
        # (the years), but shorter lists.

        year: rerank(candidates)
        # For each year: run rerank() from rerank.py, which keeps the 5 with
        # the highest semantic score.

        for year, candidates in state["candidate_chunks"].items()
        # Go through candidate_chunks one year at a time:
        #   ("FY2025", [20 chunks]), then ("FY2026", [20 chunks])
    }}
    # So:  {"FY2025": [5 chunks], "FY2026": [5 chunks]}
    #
    # Why rerank EACH YEAR separately? If we pooled all 40 chunks and kept the
    # top 10, one year could take most of the places. Ranking within each
    # year guarantees both years get 5.


# ==============================================================================
# NODE 4 - GENERATE
# ==============================================================================
# Now gpt-5.1 writes the answer, from the 5 + 5 chunks.

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
# The instructions we send to gpt-5.1. The triple quotes """ ... """ let one
# text run across many lines. (The "\" at the end of the first line joins it
# with the next line.)
#
# Let's read what each part does, and which guardrail checks it:
#   "Use ONLY the extracts"            → factual.py checks this
#   "If they don't contain enough,
#    say so plainly"                   → format.py ALLOWS this honest answer
#   "If nothing changed, say that"     → format.py Rule 4 accepts "unchanged"
#   "A figure must appear in the
#    extracts. Do not calculate"       → factual.py Check 1 looks for every
#                                        number in the chunks
#   "Do not give investment advice"    → format.py Rule 2 checks this
#   "Structure: FY2025, FY2026,
#    what changed"                     → format.py Rule 3 checks both years;
#                                        factual.py uses these headings to
#                                        know each line's year
#
# So, here, we can notice: the prompt ASKS for each rule, and a guardrail
# CHECKS it. That's the pattern from Video 9.
#
# The words in { } are blanks: company, the two years' extracts, and the
# question. generate_node fills them in.


def format_extracts(results: List[Dict]) -> str:
    # Takes one year's 5 chunks. Gives back ONE text, with each chunk numbered:
    #   [1] first chunk's text
    #
    #   [2] second chunk's text
    #   ...
    """Number each chunk [1], [2]... -- or say plainly that this year found nothing."""
    # The function's one-line description.

    if not results:
        # Did this year find NO chunks at all?

        return "(no relevant extracts found for this year)"
        # Then say so, in plain words. Otherwise the model would see an empty
        # section, and might fill the gap from its own general knowledge.

    return "\n\n".join(f"[{n}] {r['text']}" for n, r in enumerate(results, start=1))
    # Number each chunk, and join them with a blank line between each one.
    #   enumerate(results, start=1)   goes through the chunks, counting
    #                                 from 1: (1, chunk), (2, chunk), ...
    #   f"[{n}] {r['text']}"          "[1] " followed by the chunk's text
    #   "\n\n".join(...)              a blank line between chunks


def generate_node(state: GraphState) -> dict:
    # Takes the state. Gives back gpt-5.1's answer, as the draft_answer.
    """Write the comparison with Azure OpenAI."""
    # The function's one-line description.

    from openai import BadRequestError
    # The error Azure OpenAI raises when a request is rejected, including when
    # its CONTENT FILTER blocks it. Imported here, because only this node
    # needs it.

    reranked = state["reranked_chunks"]
    # The 5 best chunks per year, from rerank_node.

    prompt = GENERATION_PROMPT.format(
        # Fill in the blanks in the prompt.

        company=state["company"],
        # {company}  →  e.g. "Infosys"

        question=state["question"],
        # {question}  →  the user's question

        fy2025_extracts=format_extracts(reranked.get("FY2025", [])),
        # {fy2025_extracts}  →  FY2025's 5 chunks, numbered.
        #   .get("FY2025", [])   if FY2025 is missing, use an empty list,
        #                        instead of crashing

        fy2026_extracts=format_extracts(reranked.get("FY2026", [])),
        # {fy2026_extracts}  →  FY2026's 5 chunks, numbered.
    )

    try:
        # "try" means: run the code below, and if a problem happens, jump to
        # the "except" part, instead of crashing.

        response = openai_client().chat.completions.create(
            # ONE call to Azure OpenAI: send the prompt, get the answer.
            #
            # CHANGE FOR gpt-5.1: this call used to also send temperature=0.0
            # (to make the answer the same every time). gpt-5.1 rejects that
            # setting, so it's removed.

            model=config.GENERATION_DEPLOYMENT,
            # Which deployment: the generation deployment from .env (gpt-5.1).

            messages=[{"role": "user", "content": prompt}],
            # The message we send: ONE user message, holding the full prompt.
        )

        return {"draft_answer": response.choices[0].message.content.strip()}
        # Take the answer text, and store it as the DRAFT.
        #   response.choices[0]   the first (and only) reply
        #   .message.content      its text
        #   .strip()              remove spaces at the start and end
        # It's a draft, because the output guardrails haven't checked it yet.

    except BadRequestError as error:
        # Azure rejected the request.
        #
        # Every Azure OpenAI request passes through Azure's CONTENT FILTER,
        # which blocks harmful content. On rare occasions, it blocks a
        # perfectly normal annual report chunk too.

        if "content_filter" not in str(error):
            # Was it rejected for some OTHER reason, like a wrong setting?

            raise
            # Then that's a real bug in our code. "raise" lets the error
            # surface, so we see it and fix it.

        # The filter blocked the prompt or the answer. We do not crash: the
        # format check will flag this draft, and a reviewer will see it.
        return {"draft_answer": "(The Azure OpenAI content filter blocked this answer.)"}
        # It was the content filter. So we don't crash. We store a short note
        # as the draft instead.
        # That note mentions neither year, so format.py will FAIL it, and the
        # run goes to a person. Nothing is hidden.


# ==============================================================================
# NODE 5 - OUTPUT GUARDRAIL
# ==============================================================================
# The draft is written. Now we check it, before the user sees it.

def output_guardrail_node(state: GraphState) -> dict:
    # Takes the state. Gives back the verdict, and whether a person must review
    # the answer. It doesn't decide where the run goes next: build_graph.py
    # reads the verdict and does that.
    """Format first (free), then grounding. Records a verdict; build_graph.py routes on it."""
    # The function's one-line description.

    shape = check_format(state["draft_answer"])
    # OUTPUT GUARDRAIL 1: format.py. Length, no advice, both years, says
    # what changed. Free: no Azure call.

    if not shape.passed:
        # Did any format rule break?

        return {"grounding_verdict": NEEDS_REVIEW,
                "grounding_score": 0.0,
                "unsupported_items": [f"format: {p}" for p in shape.problems],
                "review_required": True}
        # Then send it to a person, straight away. Give back:
        #   grounding_verdict   NEEDS_REVIEW
        #   grounding_score     0.0: no number check was done
        #   unsupported_items   every broken rule, each starting with
        #                       "format: ", e.g.
        #                       "format: answer does not mention FY2025"
        #   review_required     True: the graph will pause
        #
        # Here, we can notice: factual.py never runs for this answer. So we
        # don't spend a grounding call on an answer that's already going to
        # review.

    decision = check_grounding(
        # OUTPUT GUARDRAIL 2: factual.py. Is every fact in the right year's
        # chunks? Check 1 is free; Check 2 makes one Azure call, if needed.

        state["draft_answer"],
        # The answer to check.

        collect_source_texts(state["reranked_chunks"]),
        # The chunks it was written from, as plain texts, still split by
        # year. factual.py needs the split, to catch wrong-year numbers.
    )

    return {"grounding_verdict": decision.verdict,
            "grounding_score": decision.score,
            "unsupported_items": decision.unsupported_items,
            "review_required": decision.verdict != GROUNDED}
    # Give back what the grounding check found:
    #   grounding_verdict   grounded, needs_review or ungrounded
    #   grounding_score     the share of numbers that traced back
    #   unsupported_items   exactly what failed, for the reviewer
    #   review_required     True for anything that is NOT grounded
    #                       ( != means "is not equal to" )


# ==============================================================================
# NODES 6 AND 7 - HUMAN REVIEW, FINALISE
# ==============================================================================
# Two ways to finish a run that passed the input guardrail:
#   needs review → the graph PAUSES, a person decides, then human_review runs
#   grounded     → finalise runs straight away

def human_review_node(state: GraphState) -> dict:
    # Takes the state, AFTER a reviewer has written their decision into it.
    # Gives back the final answer, based on that decision.
    """
    Runs AFTER a person has decided. The pause itself is not here -- LangGraph
    stops BEFORE this node (see build_graph.py) and waits for the decision.
    """
    # The function's description.
    #
    # Here, we can notice: this node doesn't do the PAUSING. LangGraph stops
    # the run just BEFORE this node (set in build_graph.py). While it's
    # stopped, the web app writes review_decision and review_note into the
    # state. Then the run is resumed, and THIS node runs.

    decision = state.get("review_decision")
    # The reviewer's decision: "approved" or "rejected".
    # .get(...) gives back None if it's missing, instead of crashing.

    if decision == "approved":
        # Did the reviewer approve it?

        return {"final_answer": state.get("review_note") or state["draft_answer"]}
        # Then the user sees:
        #   the reviewer's corrected answer, if they wrote one, or
        #   the draft as written, if they didn't
        # ("A or B" gives back A if it has a value, otherwise B.)

    return {"final_answer": (
        "I wasn't able to answer that reliably from the annual reports. "
        "Please try rephrasing, or refer to the reports directly."
    )}
    # Otherwise (rejected, or no decision at all), the user sees an honest
    # message instead.
    # So, here, we can notice the rule: an answer that failed a check is
    # NEVER shown unless a person approved it.


def finalise_node(state: GraphState) -> dict:
    # Takes the state. Gives back the draft as the final answer.
    """The grounded path: the draft becomes the final answer. One exit point for every route."""
    # The function's one-line description.

    return {"final_answer": state["draft_answer"]}
    # The draft passed every check. So it becomes the final answer, as written.
    #
    # Why a whole node, just to copy one field? So EVERY route through the
    # graph ends by filling in final_answer. The web app then only ever reads
    # one field, final_answer, whatever route the run took.