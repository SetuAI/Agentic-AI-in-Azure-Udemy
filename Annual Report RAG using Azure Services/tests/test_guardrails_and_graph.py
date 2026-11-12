"""
================================================================================
tests/test_guardrails_and_graph.py  -  OFFLINE TESTS FOR THE GUARDRAILS AND THE PAUSE
================================================================================

PURPOSE
-------
This file automatically checks that our four guardrails, and the human-review
pause, still behave the way we designed them. 12 tests in total.

WHAT IS A TEST?
---------------
A TEST is a small function that runs a piece of our code with a known input,
and checks the result is what we expect. For example:

    "In FY2026 the operating margin was 21.1 per cent."
        → factual.py must say NEEDS_REVIEW, because 21.1 is FY2025's figure.

If the result is what we expect, the test PASSES. If not, it FAILS, and
tells us exactly which check broke.

WHAT PROBLEM DOES IT SOLVE
--------------------------
Every time we change a file (like the gpt-5.1 fix in factual.py), we could
break something without noticing. Checking every guardrail by hand, every
time, would take ages.

With tests, ONE command checks all of it in under a second.

WHY "OFFLINE"?
--------------
These tests make NO call to Azure. So:
    - they cost nothing
    - they work with no internet, and no .env
    - they give the same result every time

How? Wherever our code WOULD call Azure, the test swaps that function for a
fake one first. This is called MONKEYPATCHING (see below).

THE KEY WORDS IN THIS FILE
--------------------------
    pytest          the tool that finds and runs our tests. Any function
                    whose name starts with "test_" is a test.
    assert          "this must be true". If it isn't, the test FAILS.
    monkeypatch     a pytest helper that swaps one of our functions for a
                    fake one, for ONE test only. It's put back afterwards.
    stub            a fake function that just gives back a fixed answer,
                    instead of doing real work (like calling Azure).
    parametrize     run the SAME test several times, with different inputs.

WHAT THE 12 TESTS CHECK
-----------------------
    factual.py     1  a FY2025 figure called FY2026 is caught
                   2  a heading's year applies to the lines below it
                   3  a plain statement is approved with NO Azure call
                   4  a "rose"/"fell" answer ALWAYS gets the claims check
    safety.py      5-7  three unsafe questions are blocked (one test, 3 inputs)
                   8  an ordinary question is allowed
    topic.py       9  a keyword question needs NO Azure call
    format.py     10  an answer that only mentions FY2026 fails
    the graph     11  an unverified answer PAUSES, then RESUMES with the
                      reviewer's text
                  12  a grounded answer does NOT pause

HOW TO RUN (from the project folder)
------------------------------------
    python -m pytest tests/test_guardrails_and_graph.py -q
        -q   quiet: one dot per test, then the total

    python -m pytest tests/test_guardrails_and_graph.py -v
        -v   verbose: each test's name, and PASSED or FAILED

EXPECTED OUTPUT
---------------
    With -q:
        ............                                                     [100%]
        12 passed in 0.30s

    With -v (one line per test; the % is how far through the run it is):
        tests/test_guardrails_and_graph.py::test_wrong_year_figure_is_caught PASSED [  8%]
        tests/test_guardrails_and_graph.py::test_heading_year_applies_to_lines_below PASSED [ 16%]
        ...
        tests/test_guardrails_and_graph.py::test_unsafe_questions_are_blocked[My PAN is ABCDE1234F] PASSED [ 50%]
        ...
        tests/test_guardrails_and_graph.py::test_grounded_answer_does_not_pause PASSED [100%]
        ============================== 12 passed in 0.26s ==============================
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import uuid
# "uuid" makes a unique id for each test run of the graph, like the real app does.

import pytest
# The test runner. We use three things from it:
#   pytest.fail(...)          make a test fail on purpose, with a message
#   @pytest.mark.parametrize  run one test with several inputs
#   monkeypatch               (given to a test automatically, when it asks for it)

from graph import build_graph as graph_module
# The build_graph.py FILE itself, under the short name graph_module.
# Why the whole file, and not just the build_graph function? Because the
# graph tests (11 and 12) swap the nodes INSIDE this file for stubs. To swap
# something inside a file, we need the file.

from graph.state import create_initial_state
# Makes a fresh state, to start a graph run.

from guardrails import factual
# The factual.py FILE itself. Tests 3 and 4 swap its check_claims function
# for a stub, so no Azure call is made.

from guardrails.factual import GROUNDED, NEEDS_REVIEW, GroundingDecision, check_grounding, check_numbers
# From factual.py:
#   GROUNDED, NEEDS_REVIEW   the verdicts we check for
#   GroundingDecision        to build a fake result in test 4
#   check_grounding          the full fact check (numbers, then claims if needed)
#   check_numbers            Check 1 only: numbers, year by year

from guardrails.format import check_format
# From format.py: the four format rules.

from guardrails.safety import ALLOW, BLOCK, check_safety
# From safety.py: the two verdicts, and the safety check.

from guardrails import topic
# The topic.py FILE itself. Test 9 swaps its check_by_similarity function for
# a stub, to prove it's never called.


# ── MADE-UP SOURCES ───────────────────────────────────────────────────────────

SOURCES = {
    # Two tiny MADE-UP sources, one per year. They stand in for the real chunks.
    # Small and fixed, so we always know exactly which number belongs to which year.

    "FY2025": ["Revenue stood at 1,53,670 crore. Operating margin was 21.1 per cent."],
    # FY2025: revenue 1,53,670, margin 21.1

    "FY2026": ["Revenue stood at 1,62,990 crore. Operating margin was 20.8 per cent."],
    # FY2026: revenue 1,62,990, margin 20.8
}


# --- grounding ---------------------------------------------------------------
# Tests 1 to 4: factual.py, output guardrail 2.

def test_wrong_year_figure_is_caught():
    # TEST 1. The mistake we most want to catch: a real number, from the wrong year.

    d = check_numbers("In FY2026 the operating margin was 21.1 per cent.", SOURCES)
    # Run Check 1 on an answer that calls 21.1 a FY2026 figure.
    # But in SOURCES, 21.1 is FY2025's margin.

    assert d.verdict == NEEDS_REVIEW and "attributed to FY2026" in d.unsupported_items[0]
    # It must say NEEDS_REVIEW, AND the first flagged item must explain why:
    #   "21.1 (attributed to FY2026, found only in another year)"
    # If either part is false, this test fails.


def test_heading_year_applies_to_lines_below():
    # TEST 2. A line with no year takes the year of the heading above it.

    d = check_numbers("What FY2026 said:\n- Margin was 21.1 per cent.", SOURCES)
    # The heading says FY2026. The line below it doesn't name a year, but it
    # IS about FY2026. And it uses 21.1, FY2025's figure.
    # (\n is a new line: the heading on one line, the bullet on the next.)

    assert d.verdict == NEEDS_REVIEW
    # So it must be caught, even though that line never says "FY2026" itself.


def test_plain_statement_needs_no_azure_call(monkeypatch):
    # TEST 3. A correct answer with no change words is approved by Check 1
    # alone, WITHOUT calling Azure.
    #   monkeypatch   asking for it here makes pytest hand us the helper

    monkeypatch.setattr(factual, "check_claims", lambda *a: pytest.fail("Azure was called"))
    # Swap factual.py's check_claims (Check 2, the Azure call) for a stub that
    # FAILS the test if it's ever called.
    #   lambda *a: ...   a tiny function that accepts any inputs (*a) and
    #                    runs pytest.fail(...)
    # So if Azure would have been called, we'll know.

    assert check_grounding("In FY2025 revenue was 1,53,670 crore.", SOURCES).verdict == GROUNDED
    # Run the FULL fact check. The number is right, for the right year, and
    # there's no "rose" or "fell". So it must be GROUNDED, and the stub must
    # never run.


def test_comparison_always_gets_the_claims_check(monkeypatch):
    # TEST 4. An answer that claims a change ALWAYS goes to Check 2, because
    # numbers alone can't tell "rose" from "fell".

    calls = []
    # An empty list. The stub below adds to it each time it's called, so we
    # can count the calls.

    monkeypatch.setattr(factual, "check_claims",
                        lambda a, s: calls.append(1) or GroundingDecision(NEEDS_REVIEW, "claims", "stub"))
    # Swap check_claims for a stub that:
    #   1. adds 1 to the calls list
    #   2. gives back a fake NEEDS_REVIEW result
    # (calls.append(1) gives back None, which counts as "nothing", so "or"
    # moves on and gives back the GroundingDecision.)

    check_grounding("Margin rose from 21.1 per cent in FY2025 to 20.8 per cent in FY2026.", SOURCES)
    # Both numbers are real, in the right years, so Check 1 passes.
    # But the answer says "rose", and 21.1 to 20.8 is actually a FALL.
    # Only reading the words (Check 2) can catch that.

    assert calls == [1]
    # So check_claims must have been called exactly ONCE.


# --- safety, topic, format ---------------------------------------------------
# Tests 5 to 10: the other three guardrails.

@pytest.mark.parametrize("q", ["Should I buy Infosys shares?", "My PAN is ABCDE1234F",
                               "Is Eternal a good investment?"])
# Run the test below THREE times, once for each question in this list.
# Each time, the question is handed in as "q".
# So this one test function counts as 3 tests (tests 5, 6 and 7).
def test_unsafe_questions_are_blocked(q):
    # TESTS 5-7. Two advice questions and one with a PAN must all be blocked.

    assert check_safety(q).verdict == BLOCK
    # safety.py must say BLOCK for this question.


def test_ordinary_question_is_safe():
    # TEST 8. A normal question must NOT be blocked.

    assert check_safety("How did the dividend change?").verdict == ALLOW
    # Here, we can notice why this test matters: "dividend" is a money word.
    # safety.py must not block it just for that. Only advice PHRASES count.


def test_keyword_question_needs_no_azure_call(monkeypatch):
    # TEST 9. A question with a finance keyword is allowed by topic.py's
    # Check 1, with NO Azure call.

    monkeypatch.setattr(topic, "check_by_similarity", lambda q: pytest.fail("Azure was called"))
    # Swap topic.py's Check 2 (the embedding call) for a stub that fails the
    # test if it's ever called.

    assert topic.check_topic("How did revenue change?").verdict == topic.ALLOW
    # "revenue" is a keyword. So it must be ALLOWED, and Check 2 must never run.


def test_one_year_answer_fails_format():
    # TEST 10. A true answer that only mentions ONE year is not a comparison.

    assert check_format("In FY2026 revenue was 1,62,990 crore, which increased.").problems == \
        ["answer does not mention FY2025"]
    # format.py must report EXACTLY one problem: FY2025 is missing.
    # (The "\" at the end of the first line just continues it on the next line.)


# --- the pause -----------------------------------------------------------------
# Tests 11 and 12: the graph, and the human-review pause.
#
# We don't want to search Azure or call gpt-5.1 just to test the WIRING.
# So we replace every node that does real work with a STUB: a fake node that
# gives back a fixed answer. The real edges, the real pause and the real
# checkpointer stay. So we test exactly the part we care about: does the graph
# pause when it should, and resume correctly?

def stub_nodes(monkeypatch, verdict):
    # Swaps the five working nodes in build_graph.py for stubs.
    #   verdict   what the fake output guardrail should report
    """Replace every working node with a stub; keep the real wiring, interrupt and checkpointer."""
    # The function's one-line description.

    monkeypatch.setattr(graph_module, "input_guardrail_node", lambda s: {"input_allowed": True})
    # Fake input guardrail: always allows the question.

    monkeypatch.setattr(graph_module, "retrieve_node", lambda s: {"candidate_chunks": {}})
    # Fake retrieve: no Azure AI Search, just an empty result.

    monkeypatch.setattr(graph_module, "rerank_node", lambda s: {"reranked_chunks": {}})
    # Fake rerank: an empty result.

    monkeypatch.setattr(graph_module, "generate_node", lambda s: {"draft_answer": "draft"})
    # Fake generate: no gpt-5.1, the draft is just the word "draft".

    monkeypatch.setattr(graph_module, "output_guardrail_node",
                        lambda s: {"grounding_verdict": verdict, "review_required": verdict != GROUNDED})
    # Fake output guardrail: reports whatever verdict the test asked for.
    # So a test can force the graph down either road at the second fork.
    #
    # Here, we can notice: human_review_node and finalise_node are NOT
    # replaced. They're the real ones, because they're what we're testing.


def test_unverified_answer_pauses_then_resumes(monkeypatch):
    # TEST 11. An answer that NEEDS REVIEW must pause the graph. Then, after
    # the reviewer approves a correction, the user must see that correction.

    stub_nodes(monkeypatch, NEEDS_REVIEW)
    # Swap in the stubs, with the output guardrail saying NEEDS_REVIEW.

    graph = graph_module.build_graph()
    # Build the graph. Because we swapped the nodes INSIDE build_graph.py
    # first, it's built with the stubs.

    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    # A unique thread id for this run, exactly like run_query.py.

    graph.invoke(create_initial_state("q", "Infosys"), thread)
    # Run the graph. It should STOP at the pause, before human_review.

    assert graph.get_state(thread).next == ("human_review",)
    # Check it's paused: the next node waiting to run must be human_review.

    graph.update_state(thread, {"review_decision": "approved", "review_note": "fixed"})
    # Play the reviewer: approve it, with the correction "fixed".
    # This writes into the SAVED state, exactly like run_query.py and the web app.

    assert graph.invoke(None, thread)["final_answer"] == "fixed"
    # Resume the graph (None = carry on, don't start again).
    # The user must see the reviewer's correction, "fixed", not the draft.


def test_grounded_answer_does_not_pause(monkeypatch):
    # TEST 12. A grounded answer must go straight to the user, with NO pause.

    stub_nodes(monkeypatch, GROUNDED)
    # Swap in the stubs, with the output guardrail saying GROUNDED.

    graph = graph_module.build_graph()
    # Build the graph with the stubs.

    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    # A unique thread id for this run.

    assert graph.invoke(create_initial_state("q", "Infosys"), thread)["final_answer"] == "draft"
    # Run the graph ONCE. With no pause, it goes all the way to END in one go,
    # through finalise. So the final answer must already be the draft, "draft".