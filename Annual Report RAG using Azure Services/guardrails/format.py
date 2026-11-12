"""
================================================================================
guardrails/format.py  -  OUTPUT GUARDRAIL 1: FORMAT
================================================================================

PURPOSE
-------
This file checks the SHAPE of the ANSWER that gpt-5.1 wrote, before we show it
to the user. It has four rules:

    RULE 1  SENSIBLE LENGTH   not empty or too short, and not a runaway essay
    RULE 2  NO ADVICE         no "we recommend buying"
    RULE 3  BOTH YEARS        FY2025 and FY2026 are both mentioned
    RULE 4  WHAT CHANGED      it says what changed, or says plainly that
                              nothing did

If every rule holds, the answer PASSES, and moves on to the grounding check
(factual.py). If any rule breaks, the answer goes to a PERSON for review, with
a list of exactly which rules broke.

WHAT PROBLEM DOES IT SOLVE
--------------------------
Our app has one job: COMPARE two years. So an answer can be completely
correct, and still be useless.

For example: "In FY2026, revenue was 1,62,990 crore." Every word of it may be
true. But it never mentions FY2025. So it's not a comparison. That's not the
answer the user asked for.

The grounding check (factual.py) can't catch this. It only asks "is each
statement backed by the reports?" And this statement IS backed by the
reports. So we need a separate check for the SHAPE of the answer.

And Rule 2 is a second line of defence. safety.py already blocks questions
that ASK for advice. But the model could still slip advice into an answer to
a fair question. Rule 2 catches that.

HOW DOES IT CHECK
-----------------
With REGULAR EXPRESSIONS (patterns of text), and by counting characters. So:
    - NO call to Azure
    - free, and instant
    - the same answer every time, for the same text

That's why it runs BEFORE the grounding check, which can cost an Azure call.
A badly shaped answer goes to review without spending that call.

And here's a nice detail: ALL four rules always run. So if an answer breaks
two rules, the reviewer sees BOTH problems, not just the first one.

THE HONEST "I CAN'T ANSWER" IS ALLOWED
---------------------------------------
Sometimes the reports simply don't cover the question. Then the right answer
is "The extracts do not contain enough information to answer this."
That answer mentions neither year, and says nothing changed. But it's honest,
and it's exactly what we told the model to do. So Rules 3 and 4 skip it.

WHERE IT FITS
-------------
    search → rerank → generate (gpt-5.1 writes the answer)
                          │
                          ▼
                      format.py    ← THIS FILE   (output guardrail 1: free, runs first)
                          │
                          ▼
                      factual.py                 (output guardrail 2)

    graph/nodes.py → output_guardrail_node() calls check_format() first.

WHAT IT NEEDS IN .env
---------------------
    Nothing. It doesn't call any Azure service.

HOW TO RUN (from the project folder)
------------------------------------
    python guardrails/format.py

EXPECTED OUTPUT
---------------
    PASS  In FY2025 revenue was 1,53,670 crore. In FY2026 it was 1,62,  []
    FAIL  In FY2026 revenue was 1,62,990 crore, which increased from t  ['answer does not mention FY2025']
    PASS  The extracts do not contain enough information to answer thi  []
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import re
# "re" is Python's REGULAR EXPRESSIONS module: it finds patterns in text.
# Rules 2, 3 and 4 are all patterns.

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from dataclasses import dataclass, field
# "@dataclass" is a shortcut for writing small classes that only hold data.
# We use it for FormatDecision, below: the result of this check.
# "field" lets each result start with its OWN empty list of problems (see below).

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import Callable, List, Optional
# These are TYPE HINTS. They only DESCRIBE what goes in and what comes out.
# Python doesn't check them.
#   Callable[[str], Optional[str]]   "a function that takes a text, and gives
#                                    back a text or None". All four rules
#                                    look like that.
#   List                             a list
#   Optional[str]                    "a text, or nothing (None)"

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the guardrails/ folder. But config.py is one folder
# ABOVE it. So we add the project folder to the front of sys.path, so the
# imports below can find it.
#   Path(__file__)   this file
#   .parent          its folder (guardrails/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. We use one setting from it:
#   config.FISCAL_YEARS   ["FY2025", "FY2026"]: the years Rule 3 looks for

from guardrails.factual import year_labels
# A function from factual.py. It gives back the different ways a year can be
# written: "FY2025", "FY 2025", "FY25", "2024-25".
# Why borrow it, and not write our own? So this check and the grounding check
# recognise a year in EXACTLY the same way. They can never disagree about
# whether an answer mentions FY2025.


# ── RULE 1 · THE LENGTH LIMITS ────────────────────────────────────────────────

MIN_ANSWER_LENGTH = 40
# The shortest a real answer can be: 40 characters.
# Anything shorter, like "Yes." or "Revenue increased.", can't be a proper
# comparison of two years.

MAX_ANSWER_LENGTH = 6000
# The longest a sensible answer should be: 6,000 characters.
# A much longer answer has usually wandered away from the 10 extracts we
# gave the model, and started adding things of its own.


# ── RULE 2 · SIGNS OF INVESTMENT ADVICE ───────────────────────────────────────

ADVICE_PHRASES = re.compile(
    # Phrases that show the model GAVE advice, even though our prompt told it
    # not to. re.compile(...) prepares the pattern once, here.

    r"\b(we|i) (would )?(recommend|suggest|advise) (buying|selling|holding|investing)|"
    # "we recommend buying", "I would suggest selling", ...
    #   \b             a WORD BOUNDARY: the start of a word
    #   (we|i)         "we" OR "i". The "|" means OR.
    #   (would )?      an optional "would ". The "?" means "zero or one".
    #   The "|" at the very end means: OR the next pattern below.

    r"\b(good|attractive|strong) (investment|buy)\b|\b(buy|sell) rating\b",
    # "a good investment", "an attractive buy", "a buy rating", ...
    # Python joins these two lines of text into ONE pattern.

    re.IGNORECASE,
    # Ignore capital letters, so "We Recommend Buying" is caught too.
)


# ── RULE 4 · SIGNS THE ANSWER TALKS ABOUT CHANGE ──────────────────────────────

CHANGE_WORDS = re.compile(
    # Words that show the answer says WHAT CHANGED, or says that NOTHING did.

    r"\b(chang|increas|decreas|rose|fell|grew|declin|improv|differ|"
    # Words about change. Here, we can notice: some are only the START of a
    # word. "chang" matches "change", "changed" and "changing". "increas"
    # matches "increase", "increased" and "increasing". So one short piece
    # covers every form of the word.

    r"same|similar|unchanged|consistent|no meaningful|remained)",
    # Words that say nothing changed: "remained the same", "unchanged",
    # "no meaningful change". That's an acceptable answer too, and our
    # prompt tells the model to say it when it's true.

    re.IGNORECASE,
    # Ignore capital letters.
)


# ── THE HONEST "I CAN'T ANSWER" ───────────────────────────────────────────────

CANNOT_ANSWER = re.compile(
    # Phrases that mean "the reports don't cover this".
    # Our prompt TELLS the model to say this when the extracts aren't enough.
    # So we must never punish it for doing so.

    r"\b(do not|does not|don't|doesn't) (contain|provide|include|mention)|"
    # "the extracts do not contain...", "the report doesn't mention...", ...

    r"\bnot enough information\b|\bno relevant extracts\b",
    # "not enough information", "no relevant extracts"

    re.IGNORECASE,
    # Ignore capital letters.
)


# ── A SMALL HELPER ────────────────────────────────────────────────────────────

def names_year(text: str, fiscal_year: str) -> bool:
    # Takes the answer and one year, like "FY2025". Gives back True if the
    # answer mentions that year in ANY of its usual forms, otherwise False.
    """True if the text names this fiscal year in any of its usual forms."""
    # The function's one-line description.

    return any(re.search(p, text, re.IGNORECASE) for p in year_labels(fiscal_year))
    # Let's read it from the inside out:
    #   year_labels(fiscal_year)   the patterns for this year: FY2025,
    #                              FY 2025, FY25, 2024-25, ...
    #   re.search(p, text, ...)    is this pattern somewhere in the answer?
    #   any(...)                   True if AT LEAST ONE pattern was found
    #
    # So "In 2024-25, revenue was..." counts as mentioning FY2025.


# ── THE FOUR RULES ────────────────────────────────────────────────────────────
# Each rule is a small function. It takes the answer, and gives back:
#   a sentence describing the problem, if the rule is BROKEN, or
#   None, if the rule HOLDS.
# Writing every rule the same way means we can run them all in one loop.

def sensible_length(text: str) -> Optional[str]:
    # RULE 1: the answer is not too short, and not too long.
    """Rule 1: not too short, not too long."""
    # The function's one-line description.

    if len(text.strip()) < MIN_ANSWER_LENGTH:
        # Is it shorter than 40 characters?
        #   .strip()   ignore spaces at the start and end, so an answer of
        #              only spaces counts as empty
        #   len(...)   count the characters

        return "answer is too short to be a real answer"
        # Too short: describe the problem.

    if len(text) > MAX_ANSWER_LENGTH:
        # Is it longer than 6,000 characters?

        return f"answer is longer than {MAX_ANSWER_LENGTH} characters"
        # Too long: describe the problem.

    return None
    # Neither: the length is fine. No problem.


def no_advice(text: str) -> Optional[str]:
    # RULE 2: the answer gives no investment advice.
    """Rule 2: no investment advice."""
    # The function's one-line description.

    return "answer gives investment advice" if ADVICE_PHRASES.search(text) else None
    # If any advice phrase is found in the answer, describe the problem.
    # Otherwise, None: no problem.
    # (The one-line "A if condition else B" form is called a CONDITIONAL
    # EXPRESSION.)


def covers_both_years(text: str) -> Optional[str]:
    # RULE 3: the answer mentions BOTH FY2025 and FY2026.
    """Rule 3: both years are mentioned (skipped for an honest 'cannot answer')."""
    # The function's one-line description.

    if CANNOT_ANSWER.search(text):
        # First: is this an honest "the reports don't cover it" answer?

        return None
        # Then skip this rule. An honest "I can't answer" doesn't need to
        # mention both years.

    missing = [y for y in config.FISCAL_YEARS if not names_year(text, y)]
    # Make a list of the years the answer NEVER mentions.
    # Go through "FY2025" and "FY2026", and keep each one the answer doesn't
    # name. So:
    #   both mentioned    →  []            (an empty list)
    #   only FY2026       →  ["FY2025"]

    return f"answer does not mention {' or '.join(missing)}" if missing else None
    # If any year is missing, describe the problem, e.g.
    #   "answer does not mention FY2025"
    # If none is missing, None: no problem.


def says_what_changed(text: str) -> Optional[str]:
    # RULE 4: the answer says what changed, or that nothing did.
    """Rule 4: the answer says what changed, or that nothing did."""
    # The function's one-line description.

    if CANNOT_ANSWER.search(text):
        # An honest "the reports don't cover it"?

        return None
        # Then skip this rule too.

    return None if CHANGE_WORDS.search(text) else "answer does not say what changed, or that nothing did"
    # If the answer uses any change word ("increased", "unchanged", ...),
    # None: no problem.
    # If it uses none, describe the problem.


RULES: List[Callable[[str], Optional[str]]] = [
    # ALL the rules, in one list, in the order they run.
    # Here, we can notice: these are the FUNCTIONS themselves, without
    # brackets after them. We're not calling them here. We're just listing
    # them, so check_format() below can call each one in turn.
    # To add a fifth rule, we'd write a new function and add it here.

    sensible_length,
    # Rule 1

    no_advice,
    # Rule 2

    covers_both_years,
    # Rule 3

    says_what_changed,
    # Rule 4
]


# ── THE RESULT OF THE CHECK ───────────────────────────────────────────────────

@dataclass
# Turn the class below into a dataclass: Python writes the code that creates it.

class FormatDecision:
    # One result of the format check. It holds two pieces of information.
    """The result of the format check."""
    # The class's one-line description.

    passed: bool
    # True if EVERY rule held. False if any rule broke.

    problems: List[str] = field(default_factory=list)
    # One sentence for each rule that broke. Empty if they all held.
    #
    # Why field(default_factory=list), and not just "= []"? Because "= []"
    # would make every FormatDecision SHARE one single list. Then a problem
    # added to one result would show up in all of them.
    # default_factory=list gives each result its OWN new, empty list.


# ── ALL FOUR RULES TOGETHER ───────────────────────────────────────────────────

def check_format(answer: str) -> FormatDecision:
    # The main function of this file. graph/nodes.py calls this one.
    # Takes the answer. Runs every rule. Gives back PASS or FAIL, with every
    # problem found.
    """Run every rule and collect every problem."""
    # The function's one-line description.

    problems = [problem for rule in RULES if (problem := rule(answer))]
    # Run each rule, and keep every problem it reports.
    # Let's read it piece by piece:
    #   for rule in RULES          go through the four rules
    #   rule(answer)               run this rule on the answer: a problem
    #                              sentence, or None
    #   problem := rule(answer)    the "walrus" operator ( := ). It SAVES the
    #                              result in "problem" AND tests it, in one go.
    #   if (...)                   keep it only if it's a real problem (None
    #                              counts as "nothing", so it's skipped)
    #
    # So, for an answer that only mentions FY2026:
    #   problems = ["answer does not mention FY2025"]

    return FormatDecision(passed=not problems, problems=problems)
    # passed is True only if the problems list is EMPTY.
    #   not []         →  True   (no problems: PASS)
    #   not ["..."]    →  False  (any problem: FAIL)


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python guardrails/format.py
    # When the pipeline imports this file, nothing below runs.

    for a in [
        # Three demo answers, one for each kind of result.

        "In FY2025 revenue was 1,53,670 crore. In FY2026 it was 1,62,990 crore, an increase.",
        # Both years, and says what changed ("an increase"). Should PASS.

        "In FY2026 revenue was 1,62,990 crore, which increased from the prior year.",
        # Says what changed, but never mentions FY2025. Should FAIL Rule 3.

        "The extracts do not contain enough information to answer this question.",
        # An honest "I can't answer". Rules 3 and 4 skip it. Should PASS.
    ]:
        d = check_format(a)
        # Run all four rules on this answer.

        print(f"  {'PASS' if d.passed else 'FAIL'}  {a[:60]}  {d.problems}")
        # Print PASS or FAIL, the first 60 characters of the answer, and the
        # list of problems:
        #   FAIL  In FY2026 revenue was 1,62,990 crore, which increased from t  ['answer does not mention FY2025']
        #
        #   a[:60]   the first 60 characters, so each line stays short