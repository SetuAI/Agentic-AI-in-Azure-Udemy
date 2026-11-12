"""
================================================================================
guardrails/safety.py  -  INPUT GUARDRAIL 1: SAFETY
================================================================================

PURPOSE
-------
This file checks the user's QUESTION, before anything else in the pipeline
runs. It looks for two things:

    1. PERSONAL DATA       a PAN, an Aadhaar number, an email address, or a
                           phone number, typed into the question
    2. INVESTMENT ADVICE   a question like "Should I buy Infosys shares?"

If it finds either one, the question is BLOCKED, and the user sees a polite
message saying why. If it finds neither, the question is ALLOWED, and moves
on to the topic check (topic.py).

WHAT PROBLEM DOES IT SOLVE
--------------------------
Problem 1: PERSONAL DATA.
A question about annual reports never needs a PAN or a phone number. If one is
there, the user pasted it by mistake. And if we let it through, a copy of it
goes to every later step: Azure OpenAI, Azure AI Search, and our logs.
So we stop it at the very first step. Then it goes nowhere.

Problem 2: INVESTMENT ADVICE.
"Should I buy Infosys?" names a company that is in our reports. So the topic
check (topic.py) would happily let it through. But we must not give buy or
sell advice. So this file blocks it.

HOW DOES IT CHECK
-----------------
With REGULAR EXPRESSIONS (regex): a short way of describing a pattern of text,
like "5 letters, then 4 digits, then 1 letter". Python then searches the
question for that pattern.

So this check:
    - makes NO call to Azure
    - costs nothing
    - takes less than a millisecond
    - gives the same answer every time, for the same question

WHERE IT FITS
-------------
    question
       │
       ▼
    safety.py   ← THIS FILE   (input guardrail 1: free, runs first)
       │
       ▼
    topic.py                  (input guardrail 2)
       │
       ▼
    search → rerank → generate → format.py → factual.py

    graph/nodes.py → input_guardrail_node() calls check_safety() first.

WHAT IT NEEDS IN .env
---------------------
    Nothing. It doesn't call any Azure service.

HOW TO RUN (from the project folder)
------------------------------------
    python guardrails/safety.py

EXPECTED OUTPUT
---------------
    ALLOW  How did Infosys's revenue change between the two years?
           no personal data or advice request found
    BLOCK  Should I buy Infosys shares now?
           question asks for investment advice
    BLOCK  Is Eternal a good investment for the long term?
           question asks for investment advice
    BLOCK  My PAN is ABCDE1234F, how did revenue change?
           question contains personal data (PAN)
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import re
# "re" is Python's REGULAR EXPRESSIONS module.
# A regular expression (regex) describes a PATTERN of text, for example
# "10 digits, starting with 6, 7, 8 or 9". Then re can search a question for it.
# Every check in this file is a regex.

from dataclasses import dataclass
# "@dataclass" is a shortcut for writing small classes that only hold data.
# We write the names of the fields, and Python writes the rest for us.
# We use it for SafetyDecision, below: the result of this check.

from typing import List, Optional
# These are TYPE HINTS. They only DESCRIBE what goes in and what comes out.
# Python doesn't check them.
#   List              a list, like our list of advice patterns
#   Optional[...]     "this thing, or nothing (None)"


# ── THE TWO POSSIBLE VERDICTS ─────────────────────────────────────────────────

ALLOW = "allow"
# The question is safe. It may continue to the next step.
# We store the word in a NAME (ALLOW), so every file uses exactly the same
# spelling. A typo like "alow" would then be an error, not a silent bug.

BLOCK = "block"
# The question stops here. The user sees a refusal message.


# ── THE RESULT OF THE CHECK ───────────────────────────────────────────────────

@dataclass
# Turn the class below into a dataclass: Python writes the code that creates
# it, so we can just write SafetyDecision(BLOCK, "advice", "...").

class SafetyDecision:
    # One result of the safety check. It holds three pieces of information.
    """The result of the safety check."""
    # The class's one-line description.

    verdict: str
    # ALLOW or BLOCK.

    check: str
    # WHICH check made the decision: "personal_data", "advice" or "safety".
    # refusal_message(), below, uses this to pick the right message.

    reason: str
    # A short explanation, for the logs.
    # Important: it names the KIND of data ("PAN"), never the data itself
    # ("ABCDE1234F"). Otherwise the personal data would end up in our logs,
    # which is exactly what we're trying to stop.


# ── CHECK 1 · THE PERSONAL DATA PATTERNS ──────────────────────────────────────

# Each entry: (name for the log, pattern). \b is a word boundary, so a pattern
# never matches in the middle of a longer word or number.
PERSONAL_DATA_PATTERNS = [
    # A list of pairs. Each pair is:
    #   (a name for the logs, the pattern that finds it)
    #
    # A few regex pieces we'll see below:
    #   \b       a WORD BOUNDARY: the start or end of a word or number.
    #            So a pattern can't match in the middle of a longer number.
    #   \d       any one digit, 0 to 9
    #   {4}      exactly 4 of the thing before it. So \d{4} is "4 digits".
    #   [A-Z]    any one capital letter
    #   \s?      an optional space ("?" means "zero or one")
    #
    # re.compile(...) turns the pattern into a ready-to-use object ONCE, here.
    # Then every question reuses it, instead of re-reading the pattern each time.

    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)),
    # A PAN card number: 5 letters, then 4 digits, then 1 letter.
    #   e.g.  ABCDE1234F
    # re.IGNORECASE: also catch it typed in small letters, like abcde1234f.

    ("Aadhaar number", re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b")),
    # An Aadhaar number: 12 digits, often written in three groups of 4.
    #   e.g.  2345 6789 0123   or   234567890123
    #   [2-9]    the first digit is 2 to 9 (Aadhaar never starts with 0 or 1)
    #   \d{3}    then 3 more digits: that's the first group of 4
    #   \s?      an optional space
    #   \d{4}    the next group of 4 digits, and so on

    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # An email address: something @ something . something
    #   e.g.  name@example.com
    #   [\w.+-]+   one or more letters, digits, dots, plus or minus signs
    #   @          the @ sign
    #   [\w-]+     the domain name, like "example"
    #   \.         a real dot ("\." means a dot, not "any character")
    #   [\w.-]+    the ending, like "com" or "co.in"

    ("phone number", re.compile(r"(?:\+91[\s-]?)?\b[6-9]\d{9}\b")),
    # An Indian mobile number: 10 digits, starting with 6, 7, 8 or 9.
    #   e.g.  9876543210   or   +91 9876543210
    #   (?:\+91[\s-]?)?   an optional "+91", then an optional space or dash.
    #                     (?: ... ) just groups things; the last ? makes the
    #                     whole group optional.
    #   [6-9]\d{9}        a digit from 6 to 9, then 9 more digits
]


# ── CHECK 2 · THE INVESTMENT ADVICE PATTERNS ──────────────────────────────────

# Phrases that ask for a recommendation. Words like "investment" or "dividend"
# are NOT here -- "how did the dividend change?" is a perfectly good question.
ADVICE_PATTERNS: List[re.Pattern] = [
    # A list of patterns that ASK FOR A RECOMMENDATION.
    #
    # Here, we can notice what's NOT in this list: single words like
    # "investment" or "dividend". Why? Because "How did the dividend change?"
    # is a perfectly good question about the reports. We only block PHRASES
    # that ask us what to do: "should I buy", "worth buying", and so on.

    re.compile(p, re.IGNORECASE) for p in [
        # A LIST COMPREHENSION: go through each pattern p in the list below,
        # and compile it, ignoring capital letters. So "Should I BUY" is
        # caught as well as "should i buy".

        r"\bshould (i|we) (buy|sell|invest|hold|short)\b",
        # "should I buy", "should we sell", "should I invest", ...
        #   (i|we)   "i" OR "we".  The "|" means OR.

        r"\b(is|are) \w+(\s\w+)? (a )?(good|bad|safe) (buy|investment|bet)\b",
        # "is Eternal a good investment", "are Infosys shares a safe bet", ...
        #   \w+          one word, like the company name
        #   (\s\w+)?     an optional second word, like "shares"
        #   (a )?        an optional "a "

        r"\bworth (buying|investing|selling)\b",
        # "worth buying", "worth investing", "worth selling"

        r"\b(buy|sell|hold) (or|vs\.?) (buy|sell|hold)\b",
        # "buy or sell", "hold vs sell", "buy vs. hold"
        #   vs\.?    "vs" with an optional dot after it

        r"\b(target price|price target|stock tip)\b",
        # price targets and stock tips

        r"\bwhich (stock|share|company) should\b",
        # "which stock should I buy", "which company should I invest in"
    ]
]


# ── WHAT THE USER SEES WHEN A QUESTION IS BLOCKED ─────────────────────────────

PERSONAL_DATA_REFUSAL = (
    # The message for a question that contains personal data.
    # The brackets let us write one long text across several lines. Python
    # joins the pieces into one text.

    "Your question seems to contain personal information (such as a PAN, Aadhaar "
    "number, email or phone number). Please remove it and ask again -- it isn't "
    "needed to answer questions about the annual reports."
    # It tells the user WHAT the problem is, and HOW to fix it. It never
    # repeats the personal data back to them.
)

ADVICE_REFUSAL = (
    # The message for a question that asks for investment advice.

    "I can't give investment advice or say whether to buy or sell shares. I can "
    "tell you what the annual reports say -- for example how revenue, margins or "
    "risks changed between the two years."
    # It says what we CAN'T do, and then what we CAN do. So the user knows
    # how to ask a question that will work.
)


# ── CHECK 1 · FIND PERSONAL DATA ──────────────────────────────────────────────

def check_personal_data(question: str) -> Optional[SafetyDecision]:
    # Takes the question. Gives back either:
    #   a SafetyDecision with BLOCK, if personal data is found, or
    #   None, which means "nothing found here"
    """BLOCK if the question contains personal data; None if it does not."""
    # The function's one-line description.

    for name, pattern in PERSONAL_DATA_PATTERNS:
        # Go through the four pairs, one at a time:
        #   ("PAN", pattern), ("Aadhaar number", pattern), ...
        # Each pair is split into its two parts: name and pattern.

        if pattern.search(question):
            # Does this pattern appear ANYWHERE in the question?
            # .search() looks through the whole text. It gives back a match
            # if it finds one, or None if it doesn't.

            return SafetyDecision(BLOCK, "personal_data", f"question contains personal data ({name})")
            # Found one. So BLOCK, and stop checking: one is enough.
            # The reason names the kind, e.g. "(PAN)", not the PAN itself.

    return None
    # We went through all four patterns, and none matched.
    # None means: "this check found nothing; let the next check decide".


# ── CHECK 2 · FIND A REQUEST FOR ADVICE ───────────────────────────────────────

def check_advice(question: str) -> Optional[SafetyDecision]:
    # Takes the question. Gives back a SafetyDecision with BLOCK if it asks
    # for investment advice, or None if it doesn't.
    """BLOCK if the question asks for investment advice; None if it does not."""
    # The function's one-line description.

    for pattern in ADVICE_PATTERNS:
        # Go through the six advice patterns, one at a time.

        if pattern.search(question):
            # Does this phrase appear anywhere in the question?

            return SafetyDecision(BLOCK, "advice", "question asks for investment advice")
            # Yes. So BLOCK, and stop checking.

    return None
    # None of the six phrases matched. Nothing found.


# ── BOTH CHECKS TOGETHER ──────────────────────────────────────────────────────

def check_safety(question: str) -> SafetyDecision:
    # The main function of this file. graph/nodes.py calls this one.
    # Takes the question. ALWAYS gives back a SafetyDecision: ALLOW or BLOCK.
    """Run both checks. Personal data first: if both apply, that is what the user most needs to hear."""
    # The function's one-line description.
    #
    # Why personal data FIRST? If a question has BOTH problems, like
    # "My PAN is ABCDE1234F, should I buy Infosys?", the personal data is the
    # more urgent one to tell the user about.

    return (
        # The brackets let us write one expression across several lines.
        #
        # This uses Python's "or" in a useful way:
        #   A or B or C   gives back the FIRST one that isn't "nothing".
        # And None counts as "nothing".

        check_personal_data(question)
        # First: a BLOCK decision if personal data was found, otherwise None...

        or check_advice(question)
        # ...if that was None, then: a BLOCK decision if advice was asked for,
        # otherwise None...

        or SafetyDecision(ALLOW, "safety", "no personal data or advice request found")
        # ...if both were None, then nothing was found. So ALLOW.
    )


# ── PICK THE RIGHT MESSAGE ────────────────────────────────────────────────────

def refusal_message(decision: SafetyDecision) -> str:
    # Takes a BLOCK decision. Gives back the message the user should see.
    # graph/nodes.py calls this when check_safety() blocks a question.
    """The message the user sees for a blocked question."""
    # The function's one-line description.

    return PERSONAL_DATA_REFUSAL if decision.check == "personal_data" else ADVICE_REFUSAL
    # If the personal data check blocked it, show the personal data message.
    # Otherwise, show the advice message.
    # This one-line "A if condition else B" form is called a CONDITIONAL
    # EXPRESSION.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python guardrails/safety.py
    # When the pipeline imports this file, nothing below runs.

    for q in [
        # Four demo questions: one that should pass, and three that should
        # be blocked, each for a different reason.

        "How did Infosys's revenue change between the two years?",
        # A normal question. Should be ALLOWED.

        "Should I buy Infosys shares now?",
        # Asks for advice: matches "should I buy". Should be BLOCKED.

        "Is Eternal a good investment for the long term?",
        # Asks for advice: matches "is ... a good investment". Should be BLOCKED.

        "My PAN is ABCDE1234F, how did revenue change?",
        # A fair question, but it contains a PAN. Should be BLOCKED.
        # (ABCDE1234F is a made-up example, not a real PAN.)
    ]:
        d = check_safety(q)
        # Run the safety check on this question.

        print(f"  {d.verdict.upper():6} {q}\n         {d.reason}")
        # Print the verdict, the question, and on the next line, the reason:
        #   BLOCK  Should I buy Infosys shares now?
        #            question asks for investment advice
        #
        #   .upper()   "block" → "BLOCK", so it stands out
        #   :6         pad it to 6 characters, so the questions line up
        #   \n         start a new line for the reason