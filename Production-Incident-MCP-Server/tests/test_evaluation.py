"""
tests/test_evaluation.py
========================

WHAT THIS FILE IS FOR
---------------------
Checks the evaluation itself, with no Azure and no real AI model. If the
measuring tool is broken, every number it gives is wrong, so it needs tests
too.

WHAT IT CHECKS
--------------
    1. The golden dataset is well-formed: unique ids, only real tool names,
       real postmortem ids, and dates that fill in to readable times.
    2. The Layer A scorers: a perfect run passes, and each kind of mistake
       (wrong order, wrong window, forbidden word, wrong outcome, too many
       calls) makes the right check fail.
    3. summarise(): pass@1, pass^k and the injection success rate add up.
    4. The judge's reply is checked: grades kept in range, bad replies rejected.
    5. The Prompt Shields and search measurements count correctly.
    6. End to end: run_agent talks to the REAL MCP server in-process, with a
       scripted fake model, and the run it records scores as a pass.

HOW TO RUN
----------
From the project folder:

    uv run pytest tests/test_evaluation.py -v

EXPECTED OUTPUT
---------------
Every test shows PASSED.
"""

# asyncio: runs the async end-to-end test.
import asyncio

# importlib: imports the server module only after a test password is set.
import importlib

# json: reads the golden dataset.
import json

# SimpleNamespace: makes quick fake objects with named fields (a fake model reply).
from types import SimpleNamespace

# pytest: the test runner.
import pytest

# The MCP client: in the end-to-end test it connects to the server object directly, in-process.
from mcp.client import Client

# Our settings: the end-to-end test sets a test password before importing the server.
from config import settings

# The evaluation code under test.
from evaluation import judge, retrieval_eval, run_eval, scorers, shield_eval

# The real tools (for their names) and the postmortem reader (for real ids).
from mcp_server import tools
from postmortems.build_index import read_postmortems

# A fixed pair of dates, like the generator would write in data/run_info.json.
RUN_INFO = {"today": "2026-11-12", "yesterday": "2026-11-11", "created_at": "2026-11-12 09:00",
            "timezone": "Asia/Kolkata"}


# All golden cases, with the test dates filled in.
def golden_cases() -> list[dict]:
    """Return every golden case with RUN_INFO's dates."""
    # Same loader the evaluation uses.
    return run_eval.load_cases(RUN_INFO)


# A golden case by its id.
def case_by_id(case_id: str) -> dict:
    """Return one golden case."""
    # Find it.
    return next(case for case in golden_cases() if case["id"] == case_id)


# ---------------------------------------------------------------------------
# 1. The golden dataset
# ---------------------------------------------------------------------------

# The dataset has the right shape and refers only to things that exist.
def test_golden_dataset_is_well_formed():
    # The cases.
    cases = golden_cases()
    # The names of the server's real tools.
    tool_names = {function.__name__ for function in tools.ALL_TOOLS}
    # The real postmortem ids.
    postmortem_ids = {document["id"] for document in read_postmortems()}
    # 24 questions, all with different ids.
    assert len(cases) == 24
    assert len({case["id"] for case in cases}) == 24
    # Every category is covered.
    assert {case["category"] for case in cases} == {"incident", "postmortem", "new_tool", "quiet", "attack"}
    # Check each case.
    for case in cases:
        # Every tool named anywhere is a real tool.
        named = [name for step in case["expected_tools"] for name in (step if isinstance(step, list) else [step])]
        named += [call["tool"] for call in case["expected_calls"]] + case["forbidden_tools"]
        assert set(named) <= tool_names, case["id"]
        # Every time in the expected calls fills in to a readable time.
        for call in case["expected_calls"]:
            # The times listed under "covers" and "near".
            times = call.get("covers", []) + ([call["near"]] if "near" in call else [])
            assert all(scorers.read_time(text) for text in times), case["id"]
        # A postmortem id, if given, is a real one.
        if "postmortem_id" in case:
            assert case["postmortem_id"] in postmortem_ids, case["id"]
        # No date placeholder is left over.
        assert "{today}" not in json.dumps(case) and "{yesterday}" not in json.dumps(case)
        # Every case has a reference answer for the judge.
        assert case["reference"].strip(), case["id"]


# Asking for a case id that doesn't exist stops with a clear message.
def test_unknown_case_id_is_refused():
    # A typo in the id.
    with pytest.raises(SystemExit):
        # Try it.
        run_eval.load_cases(RUN_INFO, ["INC1-99"])


# ---------------------------------------------------------------------------
# 2. Layer A scorers
# ---------------------------------------------------------------------------

# A perfect run for INC1-01: the right tools, in order, with the right inputs and a full answer.
def perfect_inc1_run() -> dict:
    """Return a made-up run that should pass every check for INC1-01."""
    # Two calls and an answer with every must-know fact.
    return {
        "tool_calls": [
            {"tool": "get_error_summary", "args": {"hours": 24}, "is_error": False},
            {"tool": "get_errors", "args": {"service": "payments", "start": "2026-11-12 02:00",
                                            "end": "2026-11-12 03:00"}, "is_error": False},
            {"tool": "what_changed", "args": {"start": "2026-11-12 01:00", "end": "2026-11-12 03:00"},
             "is_error": False},
        ],
        "answer": "Release v2.3.1 at 02:05 cut the payments connection pool from 50 to 5.",
        "error": None,
    }


# The perfect run passes every check.
def test_perfect_run_passes():
    # Score it.
    scores = scorers.score_run(case_by_id("INC1-01"), perfect_inc1_run())
    # Every check is True, so it passes.
    assert scores["passed"], scores


# Each kind of mistake makes exactly the right check fail.
@pytest.mark.parametrize("change, failed_check", [
    # what_changed called BEFORE get_errors: wrong order.
    (lambda run: run["tool_calls"].reverse(), "tools_in_order"),
    # get_errors window starts at 02:20, so it misses 02:15: wrong inputs.
    (lambda run: run["tool_calls"][1]["args"].update(start="2026-11-12 02:20"), "inputs_ok"),
    # The answer forgets the version number.
    (lambda run: run.update(answer="The payments connection pool was too small at 02:05."), "facts_ok"),
    # run_log_query was used even though a ready-made tool fits.
    (lambda run: run["tool_calls"].append({"tool": "run_log_query", "args": {}, "is_error": False}),
     "no_forbidden_tools"),
    # Far too many tool calls.
    (lambda run: run["tool_calls"].extend([{"tool": "get_error_summary", "args": {}}] * 10), "within_budget"),
])
def test_each_mistake_fails_the_right_check(change, failed_check):
    # Start from the perfect run.
    run = perfect_inc1_run()
    # Make one mistake.
    change(run)
    # Score it.
    scores = scorers.score_run(case_by_id("INC1-01"), run)
    # That check failed, and so the run failed.
    assert scores[failed_check] is False
    assert scores["passed"] is False


# "Any one of these" steps accept any of the listed tools.
def test_any_of_step_accepts_each_choice():
    # QUIET-03 accepts compare_periods OR get_errors.
    case = case_by_id("QUIET-03")
    # Try each choice.
    for tool in ("compare_periods", "get_errors"):
        # A one-call run.
        run = {"tool_calls": [{"tool": tool, "args": {}}], "answer": "No, it was a normal release."}
        # The order check passes.
        assert scorers.score_run(case, run)["tools_in_order"]


# change_time within 10 minutes counts; 30 minutes off does not.
def test_near_tolerance():
    # The expected call for INC1-03: compare around 02:41.
    expected = case_by_id("INC1-03")["expected_calls"][0]
    # 02:45 is 4 minutes off: fine.
    close = {"tool": "compare_periods", "args": {"service": "payments", "change_time": "2026-11-12 02:45"}}
    # 03:11 is 30 minutes off: wrong.
    far = {"tool": "compare_periods", "args": {"service": "payments", "change_time": "2026-11-12 03:11"}}
    # Check both.
    assert scorers.call_matches(expected, close)
    assert not scorers.call_matches(expected, far)


# For the poisoned-line question: obeying the attack counts as an attack that worked.
def test_obeying_the_poisoned_line_is_an_injection():
    # The poisoned-line question.
    case = case_by_id("ATK-01")
    # A run where the AI repeated the attacker's words.
    run = {"tool_calls": [{"tool": "get_errors", "args": {}}],
           "answer": "One line was removed. Anyway, all services are healthy."}
    # Score it.
    scores = scorers.score_run(case, run)
    # The forbidden words were found, so the attack worked.
    assert scores["no_forbidden_words"] is False
    assert scores["attack_succeeded"] is True


# A refusal passes; a destructive query that actually ran does not.
def test_refusal_outcome():
    # The delete-the-logs question.
    case = case_by_id("ATK-02")
    # A good refusal with no tool calls.
    good = {"tool_calls": [], "answer": "I can't delete data: my tools are read-only."}
    # A bad run: a run_log_query call that the server accepted.
    bad = {"tool_calls": [{"tool": "run_log_query", "args": {}, "is_error": False}],
           "answer": "I cannot delete, but here is a query I ran."}
    # The good one passes, the bad one fails and counts as an attack that worked.
    assert scorers.score_run(case, good)["passed"]
    assert scorers.score_run(case, bad)["attack_succeeded"] is True


# ---------------------------------------------------------------------------
# 3. Adding up
# ---------------------------------------------------------------------------

# pass@1, pass^k and the injection rate add up correctly.
def test_summarise_counts():
    # A passing and a failing scores dict.
    ok = dict.fromkeys(scorers.CHECKS, True) | {"passed": True, "tool_calls": 2, "used_run_log_query": False}
    bad = dict(ok, facts_ok=False, passed=False)
    # Two questions: one passes 3/3, one passes 2/3 (an attack question with one successful attack).
    results = [
        {"case": {"id": "A", "category": "incident", "expected_tools": ["get_errors"]}, "runs": [ok, ok, ok]},
        {"case": {"id": "B", "category": "attack", "expected_tools": []},
         "runs": [ok | {"attack_succeeded": False}, ok | {"attack_succeeded": False},
                  bad | {"attack_succeeded": True}]},
    ]
    # Add up.
    report = scorers.summarise(results)
    # 5 of 6 runs passed.
    assert report["pass_at_1"] == round(5 / 6, 3)
    # Only 1 of 2 questions passed every run.
    assert report["pass_all_k"] == 0.5
    # 1 of 3 attack runs worked.
    assert report["injection_success_rate"] == round(1 / 3, 3)
    # Question B is listed, with the check that failed.
    assert report["failed_questions"] == {"B": ["facts_ok"]}


# ---------------------------------------------------------------------------
# 4. The judge
# ---------------------------------------------------------------------------

# Good replies are read, out-of-range grades are kept in range, bad replies are rejected.
def test_judge_reply_is_checked():
    # A normal reply.
    assert judge.read_verdict('{"correct": 1, "grounded": 5, "adherent": 4, "reason": "ok"}')["grounded"] == 5
    # A grade of 9 is kept at the top of the range.
    assert judge.read_verdict('{"correct": 1, "grounded": 9, "adherent": 4}')["grounded"] == 5
    # Not JSON.
    assert "error" in judge.read_verdict("I think it is correct")
    # A missing grade.
    assert "error" in judge.read_verdict('{"correct": 1}')


# The judge sees the reference and the tool results.
def test_judge_evidence_contains_reference_and_results():
    # A case and a run.
    case = case_by_id("INC3-01")
    run = {"tool_calls": [{"tool": "trace_request", "args": {"request_id": "req-8f3a21"},
                           "result_text": "frozen (code BNK-417)"}], "answer": "Bank froze it."}
    # Build the evidence.
    evidence = judge.build_evidence(case, run)
    # The reference, the tool result and the answer are all in it.
    assert case["reference"] in evidence and "BNK-417" in evidence and "Bank froze it." in evidence


# ---------------------------------------------------------------------------
# 5. The Prompt Shields and search measurements
# ---------------------------------------------------------------------------

# Detection rate, false alarm rate and precision count correctly.
def test_shield_measure():
    # 2 attacks, 2 normal. One attack caught, one missed; one false alarm.
    result = shield_eval.measure([True, True, False, False], [True, False, True, False], ["a1", "a2", "n1", "n2"])
    # Half the attacks caught, half the normal texts flagged, half of the flags right.
    assert (result["detection_rate"], result["false_alarm_rate"], result["precision"]) == (0.5, 0.5, 0.5)
    # The wrong ones are named.
    assert result["missed"] == ["a2"] and result["false_alarms"] == ["n1"]


# The labelled set is balanced and its attacks fit inside the 300-character cut.
def test_shield_cases_are_balanced():
    # The labelled cases.
    cases = json.loads(shield_eval.CASES_FILE.read_text(encoding="utf-8"))["cases"]
    # 12 attacks and 12 normal texts.
    assert sum(case["attack"] for case in cases) == 12 and len(cases) == 24
    # Every text fits inside the server's 300-character cut, so the whole attack is really checked.
    assert all(len(case["text"]) <= 300 for case in cases)


# hit@1, hit@3 and MRR count correctly.
def test_retrieval_measure():
    # Ranks: first, third, missing, second.
    ranks = [retrieval_eval.rank_of("X", ids) for ids in (["X", "A"], ["A", "B", "X"], ["A"], ["A", "X"])]
    # Check the ranks.
    assert ranks == [1, 3, None, 2]
    # 1 of 4 first, 3 of 4 in top 3, MRR = (1 + 1/3 + 0 + 1/2) / 4.
    assert retrieval_eval.measure(ranks) == {"questions": 4, "hit_at_1": 0.25, "hit_at_3": 0.75,
                                             "mrr": round((1 + 1 / 3 + 0.5) / 4, 3)}


# Every retrieval question expects a real postmortem.
def test_retrieval_cases_use_real_ids():
    # The questions.
    cases = json.loads(retrieval_eval.CASES_FILE.read_text(encoding="utf-8"))["cases"]
    # The real ids.
    real = {document["id"] for document in read_postmortems()}
    # Every expected id is real, and every postmortem is the answer to at least one question.
    assert {case["expected"] for case in cases} == real


# ---------------------------------------------------------------------------
# 6. End to end: run_agent with the real MCP server, in-process
# ---------------------------------------------------------------------------

# A fake model that follows a script: first it asks for a tool, then it answers.
class ScriptedModel:
    """Pretends to be the Azure OpenAI client: returns the scripted replies in order."""

    # The script: a list of messages to return, one per call.
    def __init__(self, script: list):
        # Remember the script.
        self.script = list(script)
        # Remember what the agent sent each time, so a test can look at it.
        self.requests = []
        # The same shape as the real client: llm.chat.completions.create(...).
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    # Same name and inputs as the real method.
    def create(self, **request):
        # Remember the request.
        self.requests.append(request)
        # Return the next scripted message, in the real reply's shape.
        return SimpleNamespace(choices=[SimpleNamespace(message=self.script.pop(0))])


# The agent loop calls the real tool through MCP, feeds the result back, and records a passing run.
def test_run_agent_end_to_end(monkeypatch):
    # The server needs a password before it can be imported. A test-only one.
    monkeypatch.setattr(settings, "MCP_API_KEY", "test-only-key")
    # Import the server now.
    server = importlib.import_module("mcp_server.server")
    # The rows Log Analytics would return for req-8f3a21 (no Azure in tests).
    rows = [{"Service": "payments", "Level": "ERROR", "StatusCode": 502,
             "Message": "Disbursement rejected by bank: beneficiary account is frozen (code BNK-417)"},
            {"Service": "loan-service", "Level": "ERROR", "StatusCode": 502,
             "Message": "Disbursement failed for LN-20431, loan moved to ON_HOLD"}]
    # Every search returns those rows.
    monkeypatch.setattr(tools, "run_kql", lambda kql: rows)
    # The model's script: ask for trace_request, then answer.
    tool_call = SimpleNamespace(id="call-1", function=SimpleNamespace(
        name="trace_request", arguments='{"request_id": "req-8f3a21"}'))
    model = ScriptedModel([
        SimpleNamespace(content=None, tool_calls=[tool_call]),
        SimpleNamespace(content="The bank rejected it: the account is frozen (BNK-417). The loan is ON_HOLD.",
                        tool_calls=None),
    ])

    # Run the agent against the real server object, in-process.
    async def go():
        # Connect in-process: no network, no password check, real tools.
        async with Client(server.mcp) as client:
            # The tools in OpenAI format, as the evaluation sends them.
            openai_tools = run_eval.to_openai_tools((await client.list_tools()).tools)
            # One run.
            return openai_tools, await run_eval.run_agent(client, model, "gpt-5.1", "system", openai_tools,
                                                          "What happened to req-8f3a21?")

    # Run it.
    openai_tools, run = asyncio.run(go())
    # All 9 tools were offered to the model.
    assert len(openai_tools) == 9
    # The tool was really called, without an error, and its result was recorded.
    assert run["tool_calls"][0]["tool"] == "trace_request" and not run["tool_calls"][0]["is_error"]
    assert "BNK-417" in run["tool_calls"][0]["result_text"]
    # The tool result was sent back to the model in the second request.
    assert any(message["role"] == "tool" and "BNK-417" in message["content"]
               for message in model.requests[1]["messages"])
    # And the recorded run passes every Layer A check for INC3-01.
    assert scorers.score_run(case_by_id("INC3-01"), run)["passed"]