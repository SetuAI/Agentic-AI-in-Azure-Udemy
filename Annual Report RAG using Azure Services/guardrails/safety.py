"""
================================================================================
INPUT GUARDRAIL 1 - SAFETY (personal data, investment advice)
================================================================================

Checks the question BEFORE anything else -- before any Azure service is called:

    1. Personal data       a PAN, Aadhaar number, email address or phone number
    2. Investment advice   "should I buy Infosys shares?"

Personal data has no place in a question about annual reports. If it is there,
the user pasted it by mistake, and every later step (Azure OpenAI, the logs)
would receive a copy. Stopping it first means it goes nowhere.

Investment advice is ON topic -- "Should I buy Infosys?" names the company -- so
the topic check would let it through. This check refuses it.

Both are simple patterns, so this runs in microseconds, costs nothing, and gives
the same answer every time.

HOW TO RUN
----------
    python guardrails/safety.py
================================================================================
"""

import re                                       # regular expressions: describe a text pattern, then search for it
from dataclasses import dataclass               # @dataclass writes __init__ for small data classes
from typing import List, Optional               # type hints

ALLOW = "allow"                                 # the question may continue
BLOCK = "block"                                 # the question stops here


@dataclass
class SafetyDecision:
    """The result of the safety check."""
    verdict: str                                # ALLOW or BLOCK
    check: str                                  # which check decided: "personal_data", "advice" or "safety"
    reason: str                                 # for the logs -- names the KIND of data, never the data itself


# Each entry: (name for the log, pattern). \b is a word boundary, so a pattern
# never matches in the middle of a longer word or number.
PERSONAL_DATA_PATTERNS = [
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)),       # 5 letters, 4 digits, 1 letter
    ("Aadhaar number", re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b")),  # 12 digits, never starting with 0 or 1
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),     # something@something.something
    ("phone number", re.compile(r"(?:\+91[\s-]?)?\b[6-9]\d{9}\b")),     # Indian mobile: 10 digits starting 6-9
]

# Phrases that ask for a recommendation. Words like "investment" or "dividend"
# are NOT here -- "how did the dividend change?" is a perfectly good question.
ADVICE_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [     # compile each pattern once, case-insensitive
        r"\bshould (i|we) (buy|sell|invest|hold|short)\b",                         # "should I buy"
        r"\b(is|are) \w+(\s\w+)? (a )?(good|bad|safe) (buy|investment|bet)\b",     # "is Eternal a good investment"
        r"\bworth (buying|investing|selling)\b",                                   # "worth buying"
        r"\b(buy|sell|hold) (or|vs\.?) (buy|sell|hold)\b",                         # "buy or sell"
        r"\b(target price|price target|stock tip)\b",                              # price targets and tips
        r"\bwhich (stock|share|company) should\b",                                 # "which stock should I"
    ]
]

PERSONAL_DATA_REFUSAL = (                       # what the user sees if personal data is found
    "Your question seems to contain personal information (such as a PAN, Aadhaar "
    "number, email or phone number). Please remove it and ask again -- it isn't "
    "needed to answer questions about the annual reports."
)

ADVICE_REFUSAL = (                              # what the user sees if advice is requested
    "I can't give investment advice or say whether to buy or sell shares. I can "
    "tell you what the annual reports say -- for example how revenue, margins or "
    "risks changed between the two years."
)


def check_personal_data(question: str) -> Optional[SafetyDecision]:
    """BLOCK if the question contains personal data; None if it does not."""
    for name, pattern in PERSONAL_DATA_PATTERNS:  # try each kind of personal data
        if pattern.search(question):              # found one?
            return SafetyDecision(BLOCK, "personal_data", f"question contains personal data ({name})")
    return None                                   # nothing found


def check_advice(question: str) -> Optional[SafetyDecision]:
    """BLOCK if the question asks for investment advice; None if it does not."""
    for pattern in ADVICE_PATTERNS:             # try each advice phrase
        if pattern.search(question):            # found one?
            return SafetyDecision(BLOCK, "advice", "question asks for investment advice")
    return None                                 # nothing found


def check_safety(question: str) -> SafetyDecision:
    """Run both checks. Personal data first: if both apply, that is what the user most needs to hear."""
    return (
        check_personal_data(question)           # a decision if personal data was found, else None...
        or check_advice(question)               # ...then a decision if advice was requested, else None...
        or SafetyDecision(ALLOW, "safety", "no personal data or advice request found")  # ...else allow
    )


def refusal_message(decision: SafetyDecision) -> str:
    """The message the user sees for a blocked question."""
    return PERSONAL_DATA_REFUSAL if decision.check == "personal_data" else ADVICE_REFUSAL


if __name__ == "__main__":                      # only when run directly
    for q in [                                  # a few examples, allowed and blocked
        "How did Infosys's revenue change between the two years?",
        "Should I buy Infosys shares now?",
        "Is Eternal a good investment for the long term?",
        "My PAN is ABCDE1234F, how did revenue change?",
    ]:
        d = check_safety(q)                     # run the check
        print(f"  {d.verdict.upper():6} {q}\n         {d.reason}")  # show the verdict and the reason
