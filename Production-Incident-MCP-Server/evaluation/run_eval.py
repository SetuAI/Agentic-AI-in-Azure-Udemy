"""
evaluation/run_eval.py
======================

WHAT THIS FILE IS FOR
---------------------
Runs the whole golden dataset against the REAL MCP server, the same way an
AI app would use it, and scores every answer.

    golden_dataset.json ──► for each question, 3 times:
                               gpt-5.1 (playing the AI app) ──► MCP server ──► Azure
                               records every tool call, its inputs and results, and the answer
                          ──► Layer A: scorers.py (plain code checks)
                          ──► Layer B: judge.py   (gpt-5.1 grades it)   only with --judge
                          ──► a report on screen + a full JSON file in evaluation/results/

WHY 3 TIMES
-----------
An AI model doesn't answer the same way every time. One lucky run proves
little. So each question runs 3 times, and we report two numbers:
    pass@1  = the share of single runs that passed      (how good, on average)
    pass^3  = the share of questions that passed 3 of 3 (how reliable)

BEFORE YOU RUN IT
-----------------
    1. The logs are fresh: you ran the generator and the loader in the last
       few hours (the generator also wrote data/run_info.json).
    2. The MCP server is running (on your laptop, or EVAL_MCP_URL points to Azure).
    3. .env has AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT, and your
       Entra ID app has the "Cognitive Services OpenAI User" role on that resource.
    4. The evaluation libraries are installed:  uv sync --group eval

HOW TO RUN
----------
From the project folder:

    uv run python -m evaluation.run_eval                         (all questions, 3 runs each)
    uv run python -m evaluation.run_eval --judge                 (also ask the AI judge)
    uv run python -m evaluation.run_eval --cases INC1-01,ATK-01 --runs 1   (a quick try)

EXPECTED OUTPUT
---------------
One line per question, like:
    INC1-01  3/3 passed
    ATK-01   2/3 passed   failed checks: facts_ok
then the summary (pass@1, pass^3, each check, injection success rate) and
the path of the full results file.
"""

# argparse: reads the options typed after the command (--runs, --cases, --judge).
import argparse

# asyncio: runs the "async" code that talks to the MCP server.
import asyncio

# json: reads the golden dataset and run_info, and writes the results.
import json

# datetime: the current time, and how old the logs are. timedelta: "12 hours".
from datetime import datetime, timedelta

# Path: a simple way to work with folders and file names.
from pathlib import Path

# ZoneInfo: the local time zone (India time by default).
from zoneinfo import ZoneInfo

# Signs in to Azure, and turns that sign-in into tokens for Azure OpenAI (no key needed).
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# The MCP client: connects to an MCP server and calls its tools.
from mcp.client import Client

# The transport that talks to our server over HTTP.
from mcp.client.streamable_http import streamable_http_client

# Makes the HTTP connection, with our Authorization header on every request.
from mcp.shared._httpx_utils import create_mcp_http_client

# Our settings (server address, Azure OpenAI details, MCP_API_KEY, time zone).
from config import settings

# Our logger, to print progress with the time.
from config.logging_config import get_logger

# Layer A (code checks) and Layer B (the AI judge).
from evaluation import judge, scorers

# A logger named after this file.
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Fixed settings
# ---------------------------------------------------------------------------

# The folder this file is in.
FOLDER = Path(__file__).parent

# The golden dataset.
DATASET_FILE = FOLDER / "golden_dataset.json"

# Where the generator wrote which date counted as "today".
RUN_INFO_FILE = Path("data") / "run_info.json"

# Where the full results are saved.
RESULTS_FOLDER = FOLDER / "results"

# The scope a token must have to call Azure OpenAI.
OPENAI_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"

# The most model turns per question. A good investigation needs far fewer.
MAX_STEPS = 12

# Logs older than this may fall outside the tools' "last N hours" windows, so we warn.
STALE_AFTER = timedelta(hours=12)

# Each tool result kept in the results file is cut to this many characters.
MAX_SAVED_RESULT_CHARS = 4000


# ---------------------------------------------------------------------------
# Loading the golden dataset
# ---------------------------------------------------------------------------

# Reads the dataset and puts the real dates in place of {today} and {yesterday}.
def load_cases(run_info: dict, wanted_ids: list[str] | None = None) -> list[dict]:
    """Return the golden cases with real dates filled in (only the wanted ones, if given)."""
    # Read the file as text.
    text = DATASET_FILE.read_text(encoding="utf-8")
    # Replace the date placeholders everywhere at once.
    text = text.replace("{today}", run_info["today"]).replace("{yesterday}", run_info["yesterday"])
    # Read the JSON and take the list of cases.
    cases = json.loads(text)["cases"]
    # If some ids were asked for, keep only those.
    if wanted_ids:
        # Keep the asked-for cases.
        cases = [case for case in cases if case["id"] in wanted_ids]
        # Any id that doesn't exist is probably a typo.
        missing = set(wanted_ids) - {case["id"] for case in cases}
        # Stop with a clear message.
        if missing:
            # Name the unknown ids.
            raise SystemExit(f"Unknown case id(s): {', '.join(sorted(missing))}")
    # Give back the cases.
    return cases


# Reads data/run_info.json, and warns if the logs are old.
def load_run_info() -> dict:
    """Return the generator's run info, or stop with a clear message if it is missing."""
    # The generator writes this file. Without it, we don't know the dates.
    if not RUN_INFO_FILE.exists():
        # Tell the person what to run.
        raise SystemExit(f"{RUN_INFO_FILE} not found. Run: uv run python -m log_generator.generate")
    # Read it.
    run_info = json.loads(RUN_INFO_FILE.read_text(encoding="utf-8"))
    # When the logs were created, in local time.
    created = datetime.fromisoformat(run_info["created_at"]).replace(tzinfo=ZoneInfo(run_info["timezone"]))
    # How old they are now.
    age = datetime.now(ZoneInfo(run_info["timezone"])) - created
    # Old logs can fall outside "the last 24 hours" and make good answers look wrong.
    if age > STALE_AFTER:
        # Warn, but carry on.
        logger.warning("The logs were created %s ago. For fair results, generate and load fresh logs first.",
                       str(age).split(".")[0])
    # Give back the run info.
    return run_info


# ---------------------------------------------------------------------------
# Connecting to Azure OpenAI and to the MCP server
# ---------------------------------------------------------------------------

# Connects to Azure OpenAI, signed in with Entra ID (no key).
def make_llm():
    """Return an Azure OpenAI client."""
    # Imported here, so the rest of the project (and its tests) never need the openai library.
    from openai import AzureOpenAI

    # A function that hands out fresh Entra ID tokens for Azure OpenAI.
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), OPENAI_TOKEN_SCOPE)
    # The client.
    return AzureOpenAI(
        # The resource's address. Stop with a clear message if it is missing.
        azure_endpoint=settings.require("AZURE_OPENAI_ENDPOINT", settings.AZURE_OPENAI_ENDPOINT),
        # Which API version to call.
        api_version=settings.AZURE_OPENAI_API_VERSION,
        # Sign in with tokens instead of a key.
        azure_ad_token_provider=token_provider,
    )


# Turns the MCP server's tool list into the format the OpenAI API expects.
def to_openai_tools(mcp_tools) -> list[dict]:
    """Return one OpenAI "function" tool per MCP tool, with the same name, description and inputs."""
    # One entry per tool.
    return [{"type": "function",
             "function": {"name": tool.name,
                          "description": tool.description or "",
                          "parameters": tool.input_schema}}
            for tool in mcp_tools]


# Joins the text parts of a tool result into one string.
def result_text(result) -> str:
    """Return all text in an MCP tool result, joined by new lines."""
    # Keep only the parts that carry text.
    return "\n".join(part.text for part in result.content if getattr(part, "text", None))


# ---------------------------------------------------------------------------
# One question, one run: the AI app's loop
# ---------------------------------------------------------------------------

# Plays the AI app for one question: the model asks for tools, we call them, until it answers.
async def run_agent(client, llm, deployment: str, system_prompt: str, tools: list[dict], question: str) -> dict:
    """Return {"tool_calls": [...], "answer": "...", "error": None or text}."""
    # The conversation so far: the instructions, then the question.
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": question}]
    # Every tool call we make, in order.
    calls = []
    # Up to MAX_STEPS model turns.
    for _ in range(MAX_STEPS):
        # Ask the model what to do next. (A normal, blocking call: fine for an evaluation.)
        reply = llm.chat.completions.create(model=deployment, messages=messages, tools=tools)
        # The model's message.
        message = reply.choices[0].message
        # No tool calls means this is the final answer.
        if not message.tool_calls:
            # Done.
            return {"tool_calls": calls, "answer": message.content or "", "error": None}
        # Add the model's turn (with its tool requests) to the conversation.
        messages.append({"role": "assistant", "content": message.content,
                         "tool_calls": [{"id": call.id, "type": "function",
                                         "function": {"name": call.function.name,
                                                      "arguments": call.function.arguments}}
                                        for call in message.tool_calls]})
        # Run each requested tool.
        for call in message.tool_calls:
            # The inputs, sent by the model as JSON text.
            try:
                # Read them.
                args = json.loads(call.function.arguments or "{}")
            # The model wrote broken JSON.
            except json.JSONDecodeError:
                # Treat it as a failed call.
                args, text, is_error = {}, "Error: the tool inputs were not valid JSON.", True
            # The inputs are fine: call the tool on the MCP server.
            else:
                # Call it.
                result = await client.call_tool(call.function.name, args)
                # Its text, and whether the server said it failed.
                text, is_error = result_text(result), bool(result.is_error)
            # Record the call.
            calls.append({"tool": call.function.name, "args": args, "is_error": is_error,
                          "result_text": text[:MAX_SAVED_RESULT_CHARS]})
            # Give the result back to the model.
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
    # Too many turns: no answer.
    return {"tool_calls": calls, "answer": "", "error": f"no answer after {MAX_STEPS} steps"}


# The system prompt: the server's own instructions, plus the current time.
def make_system_prompt(instructions: str | None, run_info: dict) -> str:
    """Return the instructions an AI app would give its model."""
    # The current local time, so "this morning" and "yesterday" mean the right dates.
    now = datetime.now(ZoneInfo(run_info["timezone"]))
    # The server's instructions (an MCP app shows them to its model), then the time.
    return (f"{instructions or ''}\n\n"
            f"The current local time is {now:%Y-%m-%d %H:%M} ({run_info['timezone']}). "
            f"Answer the user in plain words, using the tools to find the facts.")


# ---------------------------------------------------------------------------
# The whole evaluation
# ---------------------------------------------------------------------------

# Runs every question k times, scores them, and saves the results.
async def evaluate(cases: list[dict], runs: int, use_judge: bool, run_info: dict) -> dict:
    """Return {"summary": ..., "results": [...]} for all cases."""
    # Connect to Azure OpenAI.
    llm = make_llm()
    # The model deployment's name.
    deployment = settings.AZURE_OPENAI_DEPLOYMENT
    # The HTTP connection, with our server password on every request.
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {settings.require('MCP_API_KEY', settings.MCP_API_KEY)}"})
    # All results, one item per case.
    results = []
    # Open the HTTP connection and the MCP session; both close at the end.
    async with http, Client(streamable_http_client(settings.EVAL_MCP_URL, http_client=http)) as client:
        # The server's tools, in OpenAI format.
        tools = to_openai_tools((await client.list_tools()).tools)
        # The system prompt, built once.
        system_prompt = make_system_prompt(client.instructions, run_info)
        # Say what we're about to do.
        logger.info("Connected to %s: %d tools. Running %d question(s) x %d run(s).",
                    settings.EVAL_MCP_URL, len(tools), len(cases), runs)
        # Each question.
        for case in cases:
            # This case's runs and scores.
            case_runs, case_scores, verdicts = [], [], []
            # Run it k times.
            for _ in range(runs):
                # One run. A failure (network, model error) is recorded, not fatal.
                try:
                    # Play the AI app.
                    run = await run_agent(client, llm, deployment, system_prompt, tools, case["question"])
                # Anything went wrong.
                except Exception as problem:  # noqa: BLE001 - one broken run must not stop the evaluation
                    # Record it as a broken run.
                    run = {"tool_calls": [], "answer": "", "error": f"{type(problem).__name__}: {problem}"}
                # Layer A.
                case_scores.append(scorers.score_run(case, run))
                # Layer B, if asked for.
                if use_judge:
                    # Ask the judge.
                    verdicts.append(judge.judge_run(llm, deployment, case, run))
                # Keep the run itself, for reading later.
                case_runs.append(run)
            # How many runs passed.
            passed = sum(scores["passed"] for scores in case_scores)
            # The checks that failed in any run.
            failed = sorted({name for scores in case_scores for name in scorers.CHECKS if not scores[name]})
            # One line per question.
            logger.info("%-8s %d/%d passed%s", case["id"], passed, runs,
                        f"   failed checks: {', '.join(failed)}" if failed else "")
            # Store everything for this case.
            results.append({"case": case, "runs": case_scores, "raw_runs": case_runs, "verdicts": verdicts})
    # Add it all up.
    summary = scorers.summarise(results)
    # Add the judge's averages, if it ran.
    if use_judge:
        # Every usable verdict.
        good = [verdict for result in results for verdict in result["verdicts"] if "error" not in verdict]
        # Averages, or None if the judge never gave a usable verdict.
        summary["judge"] = {
            "verdicts": len(good),
            "failed_verdicts": sum(len(result["verdicts"]) for result in results) - len(good),
            "correct_rate": scorers.share([verdict["correct"] for verdict in good]),
            "avg_grounded": round(sum(v["grounded"] for v in good) / len(good), 2) if good else None,
            "avg_adherent": round(sum(v["adherent"] for v in good) / len(good), 2) if good else None,
        }
    # Give back the summary and the details.
    return {"summary": summary, "results": results}


# Reads the options, runs the evaluation, prints the summary and saves the results.
def main() -> None:
    """Run the evaluation from the command line."""
    # The options.
    parser = argparse.ArgumentParser(description="Evaluate the MCP server with the golden dataset.")
    # How many times to run each question.
    parser.add_argument("--runs", type=int, default=3, help="runs per question (default 3)")
    # Only some questions, like INC1-01,ATK-01.
    parser.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    # Also ask the AI judge.
    parser.add_argument("--judge", action="store_true", help="also grade answers with the AI judge")
    # Read what was typed.
    options = parser.parse_args()
    # The dates, and a warning if the logs are old.
    run_info = load_run_info()
    # The questions.
    cases = load_cases(run_info, [cid.strip() for cid in options.cases.split(",") if cid.strip()])
    # Run everything.
    try:
        # The whole evaluation.
        report = asyncio.run(evaluate(cases, options.runs, options.judge, run_info))
    # The server refused us or couldn't be reached. These arrive wrapped in an ExceptionGroup.
    except* Exception as group:
        # Say what probably went wrong, then stop.
        raise SystemExit(f"Could not run the evaluation: {group.exceptions[0]!r}. "
                         f"Is the server running at {settings.EVAL_MCP_URL}, and is MCP_API_KEY right?") from None
    # Make sure the results folder exists.
    RESULTS_FOLDER.mkdir(exist_ok=True)
    # A file name with the date and time, like eval-20261112-0915.json.
    path = RESULTS_FOLDER / f"eval-{datetime.now():%Y%m%d-%H%M}.json"
    # Save everything.
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    # Print the summary.
    print(json.dumps(report["summary"], indent=2))
    # Say where the details are.
    logger.info("Full results saved to %s", path)


# Run main() only when this file is started directly (not when it is imported).
if __name__ == "__main__":
    # Start the evaluation.
    main()