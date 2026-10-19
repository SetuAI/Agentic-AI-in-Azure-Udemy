"""
================================================================================
OUTPUT GUARDRAIL 1 - FORMAT (is the answer shaped like a comparison?)
================================================================================

Checks the SHAPE of the draft answer before the grounding check reads it:

    1. sensible length    not empty, not a runaway essay
    2. no advice          no "we recommend buying"
    3. both years         FY2025 and FY2026 are both mentioned
    4. says what changed  or says plainly that nothing did

An answer can be perfectly grounded and still useless here -- for example, it
describes FY2026 accurately and never mentions FY2025. That is not a comparison.

It is free (pure pattern matching), so it runs before grounding: a badly shaped
answer goes to review without spending an Azure call on it.

Each rule is a small function that returns a problem in plain English, or None.
ALL rules run, so the reviewer sees every problem, not just the first.

HOW TO RUN
----------
    python guardrails/format.py
================================================================================
"""

import re                                       # regular expressions
import sys                                      # sys.path lets us import from the folder above
from dataclasses import dataclass, field        # small data class for the result
from pathlib import Path                        # Path builds file paths
from typing import Callable, List, Optional     # type hints

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # the fiscal years
from guardrails.factual import year_labels      # reuse the year-label patterns, so both checks agree

MIN_ANSWER_LENGTH = 40                          # characters: shorter is not a real answer
MAX_ANSWER_LENGTH = 6000                        # characters: longer has usually drifted from the extracts

ADVICE_PHRASES = re.compile(                    # signs the model gave advice despite being told not to
    r"\b(we|i) (would )?(recommend|suggest|advise) (buying|selling|holding|investing)|"
    r"\b(good|attractive|strong) (investment|buy)\b|\b(buy|sell) rating\b",
    re.IGNORECASE,
)

CHANGE_WORDS = re.compile(                      # signs the answer addresses change -- or says there was none
    r"\b(chang|increas|decreas|rose|fell|grew|declin|improv|differ|"
    r"same|similar|unchanged|consistent|no meaningful|remained)",
    re.IGNORECASE,
)

CANNOT_ANSWER = re.compile(                     # an honest "the reports don't say" -- must not be punished
    r"\b(do not|does not|don't|doesn't) (contain|provide|include|mention)|"
    r"\bnot enough information\b|\bno relevant extracts\b",
    re.IGNORECASE,
)


def names_year(text: str, fiscal_year: str) -> bool:
    """True if the text names this fiscal year in any of its usual forms."""
    return any(re.search(p, text, re.IGNORECASE) for p in year_labels(fiscal_year))


def sensible_length(text: str) -> Optional[str]:
    """Rule 1: not too short, not too long."""
    if len(text.strip()) < MIN_ANSWER_LENGTH:   # too short?
        return "answer is too short to be a real answer"
    if len(text) > MAX_ANSWER_LENGTH:           # too long?
        return f"answer is longer than {MAX_ANSWER_LENGTH} characters"
    return None                                 # fine


def no_advice(text: str) -> Optional[str]:
    """Rule 2: no investment advice."""
    return "answer gives investment advice" if ADVICE_PHRASES.search(text) else None


def covers_both_years(text: str) -> Optional[str]:
    """Rule 3: both years are mentioned (skipped for an honest 'cannot answer')."""
    if CANNOT_ANSWER.search(text):              # the model said the reports don't cover it
        return None                             # that is allowed
    missing = [y for y in config.FISCAL_YEARS if not names_year(text, y)]  # years never mentioned
    return f"answer does not mention {' or '.join(missing)}" if missing else None


def says_what_changed(text: str) -> Optional[str]:
    """Rule 4: the answer says what changed, or that nothing did."""
    if CANNOT_ANSWER.search(text):              # honest "cannot answer" is allowed
        return None
    return None if CHANGE_WORDS.search(text) else "answer does not say what changed, or that nothing did"


RULES: List[Callable[[str], Optional[str]]] = [  # every rule, run in this order
    sensible_length,
    no_advice,
    covers_both_years,
    says_what_changed,
]


@dataclass
class FormatDecision:
    """The result of the format check."""
    passed: bool                                # True if every rule held
    problems: List[str] = field(default_factory=list)  # one sentence per broken rule


def check_format(answer: str) -> FormatDecision:
    """Run every rule and collect every problem."""
    problems = [problem for rule in RULES if (problem := rule(answer))]  # := saves each result while testing it
    return FormatDecision(passed=not problems, problems=problems)       # passed only if the list is empty


if __name__ == "__main__":                      # only when run directly
    for a in [                                  # a few examples
        "In FY2025 revenue was 1,53,670 crore. In FY2026 it was 1,62,990 crore, an increase.",
        "In FY2026 revenue was 1,62,990 crore, which increased from the prior year.",
        "The extracts do not contain enough information to answer this question.",
    ]:
        d = check_format(a)                     # run the rules
        print(f"  {'PASS' if d.passed else 'FAIL'}  {a[:60]}  {d.problems}")  # verdict and any problems
