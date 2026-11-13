"""
evaluation/retrieval_eval.py
============================

WHAT THIS FILE IS FOR
---------------------
Measures the postmortem SEARCH on its own (Azure AI Search, keyword search
plus the semantic ranker), with 16 questions that each have one right
postmortem (evaluation/retrieval_cases.json).

This is the "retrieval" half of what RAGAS measures for a RAG app: did the
search bring back the right document, and how high up? We measure it
directly, because here we KNOW the one right answer for every question.

THE NUMBERS
-----------
    hit@1   the right postmortem came back FIRST            (the AI will surely read it)
    hit@3   the right postmortem was in the top 3           (the tool returns 3, so the AI sees it)
    MRR     mean reciprocal rank: 1 if first, 1/2 if second, 1/3 if third, 0 if missing,
            averaged over all questions. One number for "how high up, on average".

HOW TO RUN
----------
AI_SEARCH_ENDPOINT must be in .env, the index must be built
(postmortems/build_index.py), and your Entra ID app needs a Search Index
Data role. From the project folder:

    uv run python -m evaluation.retrieval_eval

EXPECTED OUTPUT
---------------
A line like:
    hit@1: <0 to 1>   hit@3: <0 to 1>   MRR: <0 to 1>   (16 questions)
plus one line for every question where the right postmortem was not first.
With only 8 short postmortems, hit@3 should be at or very near 1.0. These
numbers come from the live service, so we don't print fixed ones here.
"""

# json: reads the questions.
import json

# Path: a simple way to work with folders and file names.
from pathlib import Path

# Our logger, to print the results with the time.
from config.logging_config import get_logger

# The search module the server's tool uses: we test exactly the same code path.
from mcp_server import postmortems

# A logger named after this file.
logger = get_logger(__name__)

# The questions.
CASES_FILE = Path(__file__).parent / "retrieval_cases.json"


# Where the right id sits in the results: 1 for first, 2 for second, ... or None if missing.
def rank_of(expected: str, found_ids: list[str]) -> int | None:
    """Return the 1-based position of `expected` in `found_ids`, or None."""
    # Look through the results in order.
    for position, found in enumerate(found_ids, start=1):
        # Found it.
        if found == expected:
            # Its position.
            return position
    # Not in the results.
    return None


# Works out hit@1, hit@3 and MRR from the ranks. Pure: easy to test.
def measure(ranks: list[int | None]) -> dict:
    """Return hit@1, hit@3 and MRR for a list of ranks (None = not found)."""
    # How many questions.
    total = len(ranks)
    # No questions: no numbers.
    if not total:
        # Nothing to measure.
        return {"questions": 0, "hit_at_1": None, "hit_at_3": None, "mrr": None}
    # The numbers.
    return {
        "questions": total,
        # Found first.
        "hit_at_1": round(sum(1 for rank in ranks if rank == 1) / total, 3),
        # Found in the top 3.
        "hit_at_3": round(sum(1 for rank in ranks if rank is not None and rank <= 3) / total, 3),
        # 1/rank, or 0 when missing, averaged.
        "mrr": round(sum(1 / rank for rank in ranks if rank) / total, 3),
    }


# Runs every question through the real search and prints the numbers.
def main() -> None:
    """Measure the postmortem search."""
    # Read the questions.
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    # The rank of the right postmortem, for each question.
    ranks = []
    # Each question.
    for case in cases:
        # Search exactly as the tool does (top 3, semantic ranker, optional app filter).
        found_ids = [found["id"] for found in postmortems.search(case["query"], case["service"])]
        # Where the right one came.
        rank = rank_of(case["expected"], found_ids)
        # Remember it.
        ranks.append(rank)
        # If it wasn't first, show what came back instead.
        if rank != 1:
            # The question, the right answer, and what we got.
            logger.warning("%-60s expected %s, got %s", case["query"][:60], case["expected"], found_ids)
    # Work out the numbers.
    result = measure(ranks)
    # Print them.
    logger.info("hit@1: %s   hit@3: %s   MRR: %s   (%d questions)",
                result["hit_at_1"], result["hit_at_3"], result["mrr"], result["questions"])


# Run main() only when this file is started directly (not when it is imported).
if __name__ == "__main__":
    # Start the measurement.
    main()