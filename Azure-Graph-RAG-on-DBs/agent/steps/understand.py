##############################################################################
# agent/steps/understand.py
#
# PURPOSE:
#   The first three of the agent's eleven steps. Together they answer:
#   "Is this question safe, what is it asking, and what are the exact names
#   in it?"
#
#     STEP 1  guard_input      Send the question to Content Safety. If it is
#                              an attack, stop.
#     STEP 2  understand       The small model reads the question and fills
#                              in a fixed form: the names, the dates, which
#                              approved metric it needs, and whether it is a
#                              normal question at all.
#     STEP 3  match_entities   Each name the user typed is matched to the
#                              exact value in the database:
#                              "sahyadri credit fund" -> "Sahyadri Credit Risk Fund"
#
#   There is also one small helper at the bottom, describe_entities(),
#   which 6B uses.
#
# HOW A STEP WORKS — THE ONE RULE FOR ALL ELEVEN STEPS:
#   The agent carries one dictionary from step to step, called the STATE.
#   It holds the question and everything worked out about it so far.
#
#   Every step:
#     1. receives the state
#     2. reads what it needs from it
#     3. returns ONLY the keys it changed
#
#   LangGraph (agent/graph.py, in 6C) then adds those keys into the state
#   and hands the state to the next step.
#
#   So after step 1 the state has a "prompt_shield" key, after step 2 an
#   "understanding" key, after step 3 an "entities" key.
#
# WHERE A QUESTION CAN STOP IN THIS FILE:
#   step 1   attack                       -> status "blocked"
#   step 2   a request to change data     -> status "blocked"
#   step 2   not about the fund data      -> status "blocked"
#   step 2   impossible to answer as typed -> status "needs_clarification"
#   Otherwise status stays None, and the question goes on to step 4 in 6B.
#
# HOW TO USE:
#   Never run directly. agent/graph.py calls the three steps in order.
#
# TRY IT YOURSELF — run the three steps by hand (from the project folder):
#       uv run python
#       >>> from agent.steps import understand as u
#       >>> state = {"question": "Why did net flows in sahyadri credit fund drop in March 2024?"}
#       >>> state.update(u.guard_input(state));    state["prompt_shield"]
#       >>> state.update(u.understand(state));     state["understanding"]
#       >>> state.update(u.match_entities(state)); state["entities"]
#   The last line should show the match to "Sahyadri Credit Risk Fund".
##############################################################################


"""
agent/steps/understand.py
=========================

STEPS 1 TO 3 OF THE AGENT
-------------------------
    guard_input      Prompt Shields checks the question for attacks.
    understand       The small model works out what is being asked: the
                     names in it, the dates, which approved metrics apply,
                     and, for a follow-up, the full question it stands for.
    match_entities   Each name is matched to an exact database value through
                     pgvector ("sahyadri credit fund" -> "Sahyadri Credit Risk Fund").

Each step receives the agent's state (a dict) and returns only the fields
it changes. LangGraph merges those into the state for the next step.
"""
# The module's own description, kept for help(understand).


# ── IMPORTS ──────────────────────────────────────────────────────────────────

import json
# Turns Python lists and dictionaries into JSON text.
# Used only by describe_entities() at the bottom of the file.

from agent import context
# The shared settings and connections (agent/context.py).
# This file uses:
#   context.agent_config()   the settings from agent.yaml
#   context.metrics()        the approved metric definitions
#   context.agent_conn()     a connection to agent_meta

from guardrails import prompt_shields
# The Content Safety check. Step 1 calls prompt_shields.check().

from metadata import llm, retrieval
# Two files from Part 4:
#   llm         the only file that talks to Azure OpenAI. Step 2 uses
#               llm.chat_json() to ask the small model.
#   retrieval   the searches. Step 3 uses retrieval.match_values() to find
#               the exact name.


# ── THE DATES THE DATA COVERS ────────────────────────────────────────────────

DATA_START, DATA_END = "2021-09-01", "2026-08-31"
# The first and last day of the data in mf_data. Two names set in one line.
#
# WHY THE MODEL NEEDS THESE: a user may type "last month" or "latest". The
# model does not know what today is in this database. Telling it the data
# ends on 2026-08-31 means "latest" becomes August 2026, and "last month"
# becomes July 2026 — the same answer every time.
#
# These are the dates the generator used in Part 3.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — guard_input
# ═════════════════════════════════════════════════════════════════════════════

def guard_input(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 1. Also clears everything left over from the previous turn, keeping only history."""
    # This step does TWO jobs:
    #   1. checks the question with Content Safety
    #   2. clears out everything left over from the previous question
    #
    # WHY JOB 2 IS NEEDED: in a conversation, the state is KEPT between
    # questions (that is how follow-up questions work). So when question 2
    # arrives, the state still holds question 1's SQL, tables and answer.
    # If those were not cleared, a later step could pick up question 1's
    # leftovers by mistake. The only thing kept is "history" — the list of
    # earlier questions — which this step does not touch.

    result = prompt_shields.check(state["question"], context.agent_config()["prompt_shields"]["fail_open"])
    # Ask Content Safety about the question.
    #
    #   state["question"]   the text the user typed
    #   context.agent_config()["prompt_shields"]["fail_open"]
    #                       true or false, from agent.yaml: what to do if
    #                       Content Safety cannot be reached
    #
    # result looks like:
    #   {"attack": False, "checked": True, "note": "No prompt attack detected"}

    fresh = {
        # Every key this step returns. Most are set to empty, which is the
        # "clear out the leftovers" job. Each key will be filled in by a
        # later step.

        "status": None, "block_reason": None, "understanding": None, "entities": [],
        # status         None = still going. Later set to blocked, failed,
        #                needs_clarification or completed.
        # block_reason   the message shown if the question is stopped
        # understanding  filled in by step 2
        # entities       filled in by step 3

        "unmatched": [], "tables": [], "table_picks": [], "join_path": None, "sql": None, "sql_tables": [], "sql_explanation": None,
        # unmatched      step 3: names that could not be matched
        # tables, table_picks, join_path         6B, steps 4 and 5
        # sql, sql_tables, sql_explanation       6B, step 6

        "checks": [], "attempts": 0, "failed_sql": [], "error": None, "result": None, "summary": None,
        # checks         6B, step 7
        # attempts       how many times SQL has been written. Starts at 0.
        # failed_sql     SQL that failed before a retry
        # error          the last error
        # result         6B, step 8: the rows the query returned
        # summary        6C, step 9

        "commentary": None, "prompt_shield": result, "_detail": result["note"],
        # commentary     6C, step 10: the answer in plain English
        # prompt_shield  THIS step's result — the one key filled in here
        # _detail        a short note shown in the steps list on the demo page,
        #                e.g. "No prompt attack detected"
    }

    if result["attack"]:
        # Content Safety flagged it as an attack.
        fresh.update(status="blocked", block_reason="Prompt Shields flagged this question as a prompt attack. No SQL was written.")
        # .update() changes two keys in fresh:
        #   status = "blocked"   graph.py sees this and ends the run here.
        #                        Steps 2 to 11 never run.
        #   block_reason         the message the user sees

    return fresh
    # Hand the keys back. LangGraph adds them into the state.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 2 — understand
# ═════════════════════════════════════════════════════════════════════════════

UNDERSTAND_PROMPT = """You read questions about an Indian mutual fund company's database and work out
what is being asked, before any SQL is written. Reply with a JSON object with exactly these keys:

  "request_type":        "question"    -> a request to look at or analyse the data (almost everything)
                         "data_change" -> asks to delete, update, insert, create, drop or otherwise change data
                         "off_topic"   -> not about this company's data at all
  "standalone_question": the question rewritten to make sense on its own. If it is a follow-up
                         (e.g. "now only debt funds"), combine it with the previous question.
  "intent":              one sentence: what the user wants to know
  "entities":            names of specific things in the question, as a list of
                         {"text": the name as written, "kind": one of scheme, issuer, group, sector,
                         distributor, manager, category, city, benchmark}. Empty list if none.
  "time_range":          {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} or null if no period is implied
  "metrics":             names from the approved metrics list that this question needs (may be empty)
  "data_needed":         each separate kind of data the answer needs, as a short plain phrase (2 to 6
                         words, no names or dates), e.g. ["bond credit rating downgrades",
                         "bonds held by each scheme", "redemptions by scheme"]. Used to find tables.
  "needs_clarification": true ONLY if the question is impossible to answer as written, for example it
                         refers to "that fund" with no previous question to say which. Otherwise false.
  "clarification_question": the question to ask back, or null

Rules:
- "Why did X happen?" questions ARE answerable: answer with the data around the event (the figures
  for that period and the ones before it, and related activity such as redemptions). Do not ask for
  clarification because the cause cannot be proven; the commentary will describe timing, not cause.
- When a name or period is broad, assume the reasonable reading instead of asking.
- The data covers {start} to {end}. Treat "latest", "now" and "this month" as {end}, and
  "last month", "last year" relative to {end}."""
# The instructions given to the small model in step 2. This is called the
# SYSTEM PROMPT: it tells the model what job it is doing and what shape to
# reply in.
#
# WHAT IT ASKS FOR: a form with nine fixed fields. Whatever the question,
# the reply always has the same nine keys, so the code after it always
# knows where to look.
#
#   request_type          is it a normal question, a request to change data,
#                         or not about this data at all?
#   standalone_question   the question rewritten so it makes sense on its own.
#                         Matters for follow-ups: "now only March" becomes
#                         "What were net flows for Sahyadri Credit Risk Fund
#                         in March 2024?"
#   intent                one sentence saying what the user wants
#   entities              the names in the question, and what kind each is
#                         (a scheme, a distributor, a city, ...)
#   time_range            the start and end dates, if the question has a period
#   metrics               which approved metrics apply, e.g. net_flows
#   data_needed           short phrases describing the data needed. 6B uses
#                         these to search for tables.
#   needs_clarification   true only if the question cannot be answered as typed
#   clarification_question  what to ask the user back, in that case
#
# THE THREE RULES AT THE END, AND WHY EACH ONE IS THERE:
#   "Why did X happen?" IS answerable
#       Without this rule, the model tends to say "the cause cannot be
#       known" and ask for clarification. The rule says: answer with the
#       figures around that time instead.
#   assume the reasonable reading instead of asking
#       Without it, the model asks back too often, which is annoying for users.
#   the data covers {start} to {end}
#       {start} and {end} are placeholders. They are replaced with
#       2021-09-01 and 2026-08-31 just before the prompt is sent.
#
# WHY "JSON" APPEARS IN THE PROMPT: llm.chat_json() asks Azure for a
# JSON-only reply, and Azure only accepts that setting if the word JSON is
# in the instructions.


def understand(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 2. One call to the small model."""
    # WHY THE SMALL MODEL: this step reads a question and fills in a form.
    # It does not need the strong model, and the small one is cheaper and
    # faster. The strong model is saved for writing SQL in 6B.
    # Which deployment "small" means is set in config/models.yaml.

    metric_list = "\n".join(f"  - {name}: {m['description']}" for name, m in context.metrics().items())
    # Build a list of every approved metric, one per line, to show the model.
    # The result looks like:
    #       - net_flows: Money into a scheme minus money out, from completed transactions.
    #       - redemptions: Money taken out of a scheme by investors.
    #       - aum: Assets under management, the total money a scheme manages at a month end.
    #       ...
    #
    # Read it in parts:
    #   context.metrics().items()      each (name, definition) pair from metrics.yaml
    #   f"  - {name}: {m['description']}"   one line of text for each pair
    #   "\n".join(...)                 all the lines joined, with a new line between
    #
    # Only the NAME and DESCRIPTION are sent, not the full SQL definition.
    # This step only needs to say WHICH metric applies. 6B uses the definition.

    history = state.get("history") or []
    # The earlier questions in this conversation.
    #
    #   state.get("history")   the list of earlier questions, or None if there is none
    #   or []                  if it is None, use an empty list instead
    #
    # For the first question in a conversation, this is [].

    previous = (f"Previous question: {history[-1]['question']}\nPrevious SQL:\n{history[-1]['sql']}\n\n"
                if history else "")
    # If there IS an earlier question, show the model the last one and its
    # SQL. If not, an empty string.
    #
    #   history[-1]   the last item in the list — the most recent question
    #
    # WHY THIS MATTERS — A FOLLOW-UP QUESTION:
    #   question 1: "What were monthly net flows for Sahyadri Credit Risk Fund in 2024?"
    #   question 2: "Now show only March and April"
    # Question 2 on its own does not say which fund, or what to show. With
    # the previous question in front of it, the model can rewrite question 2
    # as a complete question in "standalone_question".

    answer = llm.chat_json(
        # Ask the model, and get its reply back as a Python dictionary.
        # llm.chat_json() takes three things:

        "small",
        # 1. which model — the small one

        UNDERSTAND_PROMPT.replace("{start}", DATA_START).replace("{end}", DATA_END),
        # 2. the instructions, with {start} and {end} replaced by the real
        #    dates

        f"Approved metrics:\n{metric_list}\n\n{previous}Question: {state['question']}",
        # 3. the message itself: the metric list, the previous question (if
        #    any), and the question the user just typed. For our demo it
        #    looks like:
        #
        #       Approved metrics:
        #         - net_flows: Money into a scheme minus money out, ...
        #         ...
        #
        #       Question: Why did net flows in sahyadri credit fund drop in March 2024?
    )
    # For our demo question, answer comes back looking roughly like this
    # (the wording changes a little each run, because a model writes it):
    #
    #   {
    #     "request_type": "question",
    #     "standalone_question": "Why did net flows in sahyadri credit fund drop in March 2024?",
    #     "intent": "Find out why net flows into this fund fell in March 2024",
    #     "entities": [{"text": "sahyadri credit fund", "kind": "scheme"}],
    #     "time_range": {"start": "2024-01-01", "end": "2024-03-31"},
    #     "metrics": ["net_flows"],
    #     "data_needed": ["net flows by scheme", "bond credit rating downgrades", ...],
    #     "needs_clarification": false,
    #     "clarification_question": null
    #   }

    answer["metrics"] = [m for m in answer.get("metrics", []) if m in context.metrics()]
    # Keep only metric names that really exist in metrics.yaml.
    #
    # WHY: the model might invent one, for example "net_inflows". A metric
    # that does not exist would cause an error in 6B, when the code goes to
    # look up its definition. This line removes any such name.
    #
    #   answer.get("metrics", [])   the model's list, or [] if it left it out
    #   if m in context.metrics()   keep a name only if metrics.yaml has it

    answer["data_needed"] = [d for d in answer.get("data_needed") or [] if isinstance(d, str) and d.strip()][:6]
    # Clean up the data_needed list:
    #   isinstance(d, str)   keep only text (not numbers, not null)
    #   d.strip()            and only if it is not blank
    #   [:6]                 keep at most 6
    #
    # WHY AT MOST 6: in 6B each phrase is used for one search. A model that
    # returned 20 phrases would mean 20 searches for one question.

    update = {"understanding": answer, "_detail": answer.get("intent", "")}
    # What this step returns:
    #   understanding   the whole form the model filled in
    #   _detail         the one-sentence intent, for the steps list on the demo page

    # Requests to change data, or unrelated requests, stop here: no tables
    # are searched and no SQL is written. The SQL check (SELECT only) and
    # the READ ONLY database session are still there behind this, in case
    # one ever slips through.

    kind = answer.get("request_type", "question")
    # "question", "data_change" or "off_topic". If the model left the key
    # out, assume "question".

    if kind == "data_change":
        # For example: "Delete all rows from the transactions table".
        update.update(status="blocked", _detail="Request to change data",
                      block_reason="This assistant only reads data. Requests to delete, update or create "
                                   "data are refused, and no SQL was written.")
        # status = "blocked": graph.py ends the run. No tables are searched,
        # no SQL is written.
        #
        # The two pieces of text in quotes next to each other are joined
        # into one sentence by Python. That is just a way of splitting a
        # long sentence across two lines.

    elif kind == "off_topic":
        # For example: "What is the capital of France?"
        update.update(status="blocked", _detail="Not about this data",
                      block_reason="This question is not about the fund database, so it was not answered.")
        # Also blocked. The agent only answers questions about this data.

    elif answer.get("needs_clarification"):
        # The question cannot be answered as typed, for example "how did
        # that fund do?" as the very first question — there is no earlier
        # question to say which fund.
        update.update(status="needs_clarification")
        # graph.py ends the run, and the user is shown
        # answer["clarification_question"], the question asked back.

    return update
    # For a normal question, status was never set, so it stays None, and
    # graph.py moves on to step 3.


# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — match_entities
# ═════════════════════════════════════════════════════════════════════════════

def match_entities(state: dict) -> dict:
    # Takes the state, returns the keys it changed.
    """Step 3. Look each name up in its table's column, via pgvector."""
    # THE PROBLEM THIS STEP SOLVES:
    #   The user typed "sahyadri credit fund". The database holds
    #   "Sahyadri Credit Risk Fund". SQL needs the exact spelling:
    #       WHERE scheme_name = 'sahyadri credit fund'        -> 0 rows
    #       WHERE scheme_name = 'Sahyadri Credit Risk Fund'   -> the right rows
    #
    # HOW IT SOLVES IT:
    #   In Part 4, publish.py turned every real fund name, company name,
    #   city and so on into searchable data in meta_embeddings. This step
    #   searches that data for the stored name closest in meaning to what the
    #   user typed.

    config = context.agent_config()
    # The settings from agent.yaml.

    lookups = config["entity_columns"]
    # Where each kind of name lives, from agent.yaml. For example:
    #   "scheme"      -> ["schemes", "scheme_name"]
    #   "distributor" -> ["distributors", "dist_nm"]
    #   "city"        -> ["investors", "city"]
    #
    # Step 2 told us the KIND of each name. This tells us which table and
    # column to search for that kind.

    matched, unmatched = [], []
    # Two empty lists, set in one line:
    #   matched     names we found an exact value for
    #   unmatched   names we could not

    with context.agent_conn() as conn:
        # Open a connection to agent_meta, where the searchable data lives.
        # It closes by itself when this block ends.

        for entity in state["understanding"].get("entities", []):
            # Go through every name step 2 found. For our demo there is one:
            #   {"text": "sahyadri credit fund", "kind": "scheme"}
            # If step 2 found no names, the list is empty and the loop does
            # nothing.

            if entity.get("kind") not in lookups:
                # The model gave a kind that agent.yaml does not list, for
                # example "person". We do not know where to look it up.
                unmatched.append(entity)
                # Note it as not found ...
                continue
                # ... and skip to the next name.

            table, column = lookups[entity["kind"]]
            # Where to search. For a scheme: table = "schemes",
            # column = "scheme_name".

            best = retrieval.match_values(conn, entity["text"], k=1, table=table, column=column)
            # Search for the closest stored value. This is the Part 4 search
            # from retrieval.py.
            #
            #   entity["text"]          what the user typed: "sahyadri credit fund"
            #   k=1                     we want only the single best match
            #   table=..., column=...   search only fund names, not company
            #                           names or cities
            #
            # WHY LIMIT IT TO ONE COLUMN: "konkan" could match a distributor,
            # a city or a company. Step 2 said this name is a scheme, so we
            # only look among scheme names.
            #
            # best is a list with at most one item, shaped
            #   [(table, column, value, score)]
            # for our demo:
            #   [("schemes", "scheme_name", "Sahyadri Credit Risk Fund", 0.8...)]

            if best and best[0][3] >= config["entity_min_score"]:
                # Two conditions:
                #   best                     something was found at all
                #   best[0][3] >= 0.5        its score is at least
                #                            entity_min_score from agent.yaml
                #
                # best[0] is the one match; [3] is its 4th value, the score.
                # The score runs from 0 to 1: higher means closer in meaning.

                matched.append({"text": entity["text"], "kind": entity["kind"], "table": table,
                                "column": column, "value": best[0][2], "score": round(best[0][3], 3)})
                # Record the match:
                #   text     what the user typed       "sahyadri credit fund"
                #   kind     what kind of name         "scheme"
                #   table    where it was found        "schemes"
                #   column                             "scheme_name"
                #   value    THE EXACT DATABASE VALUE  "Sahyadri Credit Risk Fund"
                #            (best[0][2] is the 3rd value in the match)
                #   score    rounded to 3 decimals, for the demo page

            else:
                # Nothing found, or the closest match is too weak.
                unmatched.append(entity)
                # WHY SKIP A WEAK MATCH RATHER THAN USE IT: a wrong fund name
                # gives a confident answer about the WRONG fund, and the user
                # would not know. Better to have no match than a wrong one.

    detail = "; ".join(f"'{m['text']}' -> {m['value']}" for m in matched) or "No names to match"
    # The note for the demo page. One "typed -> exact" pair per match, for example:
    #   'sahyadri credit fund' -> Sahyadri Credit Risk Fund
    # If nothing matched, the joined text is empty, and "or" uses
    # "No names to match" instead.

    return {"entities": matched, "unmatched": unmatched, "_detail": detail}
    # What this step returns. "entities" now holds the exact values, and 6B
    # puts those into the SQL.
    #
    # Note: this step never stops the question. Even with no matches, the
    # agent carries on to step 4 — many questions have no names in them at
    # all, like "Which distributor has the highest rejection rate?"


# ── HELPER FOR 6B ────────────────────────────────────────────────────────────

def describe_entities(entities: list[dict]) -> str:
    # Takes the matched names from step 3, returns them as short JSON text.
    """Used by later steps' prompts."""
    # Not a step. 6B uses this to put the matched names into the prompt for
    # the model that writes the SQL, so the model uses the exact spelling.
    return json.dumps([{k: e[k] for k in ("text", "table", "column", "value")} for e in entities], indent=1)
    # For each matched name, keep only four keys — text, table, column, value
    # — and drop kind and score, which the SQL model does not need.
    #
    #   json.dumps(..., indent=1)   turn it into JSON text, one key per line,
    #                               which is easier for the model to read
    #
    # For our demo it produces:
    #   [
    #    {
    #     "text": "sahyadri credit fund",
    #     "table": "schemes",
    #     "column": "scheme_name",
    #     "value": "Sahyadri Credit Risk Fund"
    #    }
    #   ]