"""
Offline tests for the guardrails and the human-review pause.
"""

import uuid                                     # unique run ids

import pytest                                   # the test runner

from graph import build_graph as graph_module   # the wiring, with nodes we can swap out
from graph.state import create_initial_state    # a fresh state
from guardrails import factual                  # grounding
from guardrails.factual import GROUNDED, NEEDS_REVIEW, GroundingDecision, check_grounding, check_numbers
from guardrails.format import check_format      # format
from guardrails.safety import ALLOW, BLOCK, check_safety  # safety
from guardrails import topic                    # topic

SOURCES = {
    "FY2025": ["Revenue stood at 1,53,670 crore. Operating margin was 21.1 per cent."],
    "FY2026": ["Revenue stood at 1,62,990 crore. Operating margin was 20.8 per cent."],
}


# --- grounding ---------------------------------------------------------------

def test_wrong_year_figure_is_caught():
    d = check_numbers("In FY2026 the operating margin was 21.1 per cent.", SOURCES)
    assert d.verdict == NEEDS_REVIEW and "attributed to FY2026" in d.unsupported_items[0]


def test_heading_year_applies_to_lines_below():
    d = check_numbers("What FY2026 said:\n- Margin was 21.1 per cent.", SOURCES)
    assert d.verdict == NEEDS_REVIEW


def test_plain_statement_needs_no_azure_call(monkeypatch):
    monkeypatch.setattr(factual, "check_claims", lambda *a: pytest.fail("Azure was called"))
    assert check_grounding("In FY2025 revenue was 1,53,670 crore.", SOURCES).verdict == GROUNDED


def test_comparison_always_gets_the_claims_check(monkeypatch):
    calls = []
    monkeypatch.setattr(factual, "check_claims",
                        lambda a, s: calls.append(1) or GroundingDecision(NEEDS_REVIEW, "claims", "stub"))
    check_grounding("Margin rose from 21.1 per cent in FY2025 to 20.8 per cent in FY2026.", SOURCES)
    assert calls == [1]


# --- safety, topic, format ---------------------------------------------------

@pytest.mark.parametrize("q", ["Should I buy Infosys shares?", "My PAN is ABCDE1234F",
                               "Is Eternal a good investment?"])
def test_unsafe_questions_are_blocked(q):
    assert check_safety(q).verdict == BLOCK


def test_ordinary_question_is_safe():
    assert check_safety("How did the dividend change?").verdict == ALLOW


def test_keyword_question_needs_no_azure_call(monkeypatch):
    monkeypatch.setattr(topic, "check_by_similarity", lambda q: pytest.fail("Azure was called"))
    assert topic.check_topic("How did revenue change?").verdict == topic.ALLOW


def test_one_year_answer_fails_format():
    assert check_format("In FY2026 revenue was 1,62,990 crore, which increased.").problems == \
        ["answer does not mention FY2025"]


# --- the pause -----------------------------------------------------------------

def stub_nodes(monkeypatch, verdict):
    """Replace every working node with a stub; keep the real wiring, interrupt and checkpointer."""
    monkeypatch.setattr(graph_module, "input_guardrail_node", lambda s: {"input_allowed": True})
    monkeypatch.setattr(graph_module, "retrieve_node", lambda s: {"candidate_chunks": {}})
    monkeypatch.setattr(graph_module, "rerank_node", lambda s: {"reranked_chunks": {}})
    monkeypatch.setattr(graph_module, "generate_node", lambda s: {"draft_answer": "draft"})
    monkeypatch.setattr(graph_module, "output_guardrail_node",
                        lambda s: {"grounding_verdict": verdict, "review_required": verdict != GROUNDED})


def test_unverified_answer_pauses_then_resumes(monkeypatch):
    stub_nodes(monkeypatch, NEEDS_REVIEW)
    graph = graph_module.build_graph()
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    graph.invoke(create_initial_state("q", "Infosys"), thread)
    assert graph.get_state(thread).next == ("human_review",)       # paused
    graph.update_state(thread, {"review_decision": "approved", "review_note": "fixed"})
    assert graph.invoke(None, thread)["final_answer"] == "fixed"   # resumed with the reviewer's text


def test_grounded_answer_does_not_pause(monkeypatch):
    stub_nodes(monkeypatch, GROUNDED)
    graph = graph_module.build_graph()
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    assert graph.invoke(create_initial_state("q", "Infosys"), thread)["final_answer"] == "draft"
