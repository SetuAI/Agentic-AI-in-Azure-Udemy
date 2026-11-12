"""
================================================================================
UI/server.py  -  THE WEB SERVER: OUR PIPELINE, IN A WEB PAGE (laptop or Azure)
================================================================================

PURPOSE
-------
This file is the WEB SERVER for our app. It does two jobs:

    1. It sends the web page, UI/index.html, to the browser.
    2. It runs our LangGraph pipeline when the page asks a question, and
       sends the results back as JSON.

The page talks to it through these addresses (called ENDPOINTS):

    POST /api/query    {"question": "...", "company": "Infosys"}
                       → runs the graph. Sends back: the chunks before and
                         after reranking, the grounding checks, and the answer
                         (or "paused": true, if a person must review it)

    POST /api/review   {"thread_id": "...", "decision": "approved" or "rejected",
                        "note": "an optional corrected answer"}
                       → resumes a paused run, and sends back the final
                         answer, with the name of the person who reviewed it

    GET  /api/config   our companies, years and top-k settings, for the page
    GET  /api/me       who is signed in ("local" on a laptop)
    GET  /health       {"status": "ok"}. Azure visits this every minute

WHAT PROBLEM DOES IT SOLVE
--------------------------
run_query.py runs the pipeline in the TERMINAL. That's fine for us, but not
for a user. A user wants a web page: pick a company, type a question, click a
button, and see the answer.

So this file puts the SAME pipeline behind a web page. It also adds what a
real app on the internet needs: sign-in, a health check, monitoring, and
serving only the files it should.

THE KEY WORDS IN THIS FILE
--------------------------
    WEB SERVER     a program that waits for requests from browsers, and sends
                   back a page or some data
    REQUEST        one message from the browser, e.g. "GET /api/me"
    GET, POST      the two kinds of request we use.
                   GET  = "give me something"   (the page, the settings)
                   POST = "here's some data, do something with it"
                          (a question, a review decision)
    JSON           a text format for data: {"name": "value", ...}
    HEADER         an extra line of information sent with a request, e.g.
                   who is signed in
    STATUS CODE    a number sent back with every reply, saying what happened:
                   200 = OK, 400 = bad input, 401 = not signed in,
                   404 = not found, 500 = something broke on the server
    THREAD ID      the unique id of one graph run. The page keeps it, so a
                   review can resume the right paused run

NO WEB FRAMEWORK
----------------
We use Python's own built-in web server (http.server), not a framework like
FastAPI. So there's nothing extra to install, and every line is visible.

WHAT THIS FILE ADDS FOR AZURE APP SERVICE
-----------------------------------------
    1. WHERE IT LISTENS. On Azure, it listens on 0.0.0.0 and the PORT Azure
       gives it. On a laptop, it still listens on 127.0.0.1:8000, as before.
    2. /health, open to everyone, for App Service's health check.
    3. SIGN-IN. On Azure, App Service makes people sign in with Microsoft
       BEFORE a request reaches this file. Then it adds two headers that name
       the person:
           X-MS-CLIENT-PRINCIPAL-ID     their unique id
           X-MS-CLIENT-PRINCIPAL-NAME   usually their email address
       App Service removes these headers from anything a user sends, and adds
       its own. So on Azure, we can trust them. This file uses them to:
           - record WHO approved or rejected each paused answer
           - refuse /api/* on Azure if they're missing, because that means
             sign-in was switched off by mistake
       On a laptop, there's no sign-in, and everyone is "local".
    4. ONLY index.html is sent from the UI/ folder. Otherwise, the built-in
       server would hand out ANY file in the folder, including server.py.
    5. MONITORING. Each request is recorded as a span in Application Insights
       (see telemetry.py). Python's built-in server isn't recorded
       automatically, so this file does it.

WHERE IT FITS
-------------
    browser (UI/index.html)
        │  POST /api/query, POST /api/review
        ▼
    UI/server.py              THIS FILE
        │  GRAPH.invoke(...)
        ▼
    graph/build_graph.py      the 7 nodes: guardrails, search, rerank, gpt-5.1

WHAT IT NEEDS IN .env
---------------------
    The AZURE_OPENAI_* and AZURE_SEARCH_* settings (for the pipeline).
    APPLICATIONINSIGHTS_CONNECTION_STRING is optional: monitoring is off without it.

HOW TO RUN (from the project folder)
------------------------------------
    python UI/server.py              then open http://localhost:8000
    python UI/server.py 8080         use a different port

    On Azure, startup.sh runs it, and Azure sets PORT.

EXPECTED OUTPUT
---------------
    
    Annual-report RAG UI on http://127.0.0.1:8000   (Ctrl+C to stop)

    Then, one line per question and per review, for example:
    QUERY  by local  [Infosys]  How did the company describe its currency risk?
    REVIEW approved by local  thread 3f2a9c1e-...

    The built-in server also prints one line per request, with its status code:
    127.0.0.1 - - [11/Nov/2026 12:43:42] "GET /api/me HTTP/1.1" 200 -
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import json
# "json" turns JSON text into Python dictionaries, and back again.
# The page sends its question as JSON, and we send our results back as JSON.

import os
# "os" reads ENVIRONMENT VARIABLES: settings the system gives our program.
# We read two:
#   PORT                which port to listen on (Azure sets it)
#   WEBSITE_SITE_NAME   set ONLY on Azure App Service. So it tells us
#                       whether we're on Azure or on a laptop

import sys
# "sys" gives us:
#   sys.argv   the words typed after "python UI/server.py" (an optional port)
#   sys.path   the list of folders Python looks in when we write "import ..."

import traceback
# "traceback" prints the full details of an error to the log, so we can see
# exactly which line went wrong.

import uuid
# "uuid" makes a unique id for each graph run: the THREAD ID.

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
# Python's own built-in web server:
#   SimpleHTTPRequestHandler   handles ONE request. It can already send files
#                              from a folder. We build our Handler on top of it.
#   ThreadingHTTPServer        the server itself. "Threading" means it can
#                              handle several requests at the same time. So
#                              one slow question doesn't block everyone else.

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

UI_DIR = Path(__file__).parent
# This file's folder: UI/. The web page, index.html, is in here.

sys.path.insert(0, str(UI_DIR.parent))
# Add the PROJECT folder (one level above UI/) to the front of sys.path.
# Then the imports below can find telemetry.py, config.py and graph/.

import telemetry
# Our monitoring file: the setup() function, and the shared tracer.

telemetry.setup()
# Switch monitoring on, FIRST, before anything else.
#
# Why first? Because setup() must run BEFORE any Azure client is created.
# Otherwise those clients would exist without recording switched on.
# If APPLICATIONINSIGHTS_CONNECTION_STRING is empty, this does nothing.

from opentelemetry.trace import SpanKind
# SpanKind says what KIND of work a span records.
# We use SpanKind.SERVER: "a request that arrived at our server". Application
# Insights then lists it under REQUESTS (not under dependencies).

import config
# Our settings file. From it we use:
#   config.COMPANIES             ["Eternal", "Infosys"]
#   config.FISCAL_YEARS          ["FY2025", "FY2026"]
#   config.VECTOR_SEARCH_TOP_K   20: candidates per year
#   config.RERANK_TOP_K          5: chunks kept per year

from graph.build_graph import build_graph
# The function that builds our whole pipeline.

from graph.state import create_initial_state
# The function that makes a fresh, empty state for a new question.


# ── SET UP ONCE, WHEN THE SERVER STARTS ───────────────────────────────────────

GRAPH = build_graph()
# Build the pipeline ONCE, and keep it for every request.
#
# Here, we can notice why this matters: the graph's checkpointer (MemorySaver)
# keeps PAUSED runs in memory. A question pauses on one request, and the
# review arrives on a later request. Both must use the SAME graph, so the
# review can find the paused run.

ON_APP_SERVICE = bool(os.environ.get("WEBSITE_SITE_NAME"))
# Are we running on Azure App Service?
#   os.environ.get("WEBSITE_SITE_NAME")   App Service ALWAYS sets this
#                                         (to our app's name). A laptop never does.
#   bool(...)                             turn it into True or False
# So: True on Azure, False on a laptop.

PAGES = {"/", "/index.html"}
# The ONLY two addresses that send a file from UI/. Both mean the page.
# Anything else in UI/ (like server.py itself) is never sent.
# The { } makes a SET: a group that can quickly answer "is this in here?"


# ── TURNING A GRAPH STATE INTO WHAT THE PAGE NEEDS ────────────────────────────

def chunk_key(chunk: dict) -> str:
    # Takes one chunk. Gives back a short id for it, like "FY2025:850".
    """A stable id for a chunk: its year plus its position in the report."""
    # The function's one-line description.

    return f"{chunk['fiscal_year']}:{chunk['position']}"
    # Its year and its position in the report, joined by ":".
    # The page uses this id to match each chunk across the "before" (hybrid)
    # and "after" (semantic) columns.


def describe_retrieval(state: dict) -> dict:
    # Takes the state after a run. Gives back, for each year: every candidate
    # in hybrid order, and where the semantic ranker put it (if it was kept).
    # The page uses this to draw the before-and-after reranking columns.
    """Per year: every candidate in hybrid order, with where the semantic ranker put it (if kept)."""
    # The function's one-line description.

    years = {}
    # An empty dictionary. We'll add one entry per year.

    for year in sorted((state.get("candidate_chunks") or {}).keys()):
        # Go through each year that has candidates, in order: FY2025, FY2026.
        #   state.get("candidate_chunks")   the 20 per year from search, or None
        #   or {}                           if None (e.g. the question was
        #                                   blocked), use an empty dictionary,
        #                                   so the loop simply doesn't run
        #   .keys()                         the years
        #   sorted(...)                     in order

        candidates = state["candidate_chunks"][year]
        # This year's 20 candidates, in HYBRID order.

        kept = (state.get("reranked_chunks") or {}).get(year, [])
        # This year's 5 kept chunks, in SEMANTIC order. An empty list if none.

        semantic_rank = {chunk_key(c): i + 1 for i, c in enumerate(kept)}
        # A lookup: for each KEPT chunk, its semantic position (1 to 5).
        #   {"FY2025:850": 1, "FY2025:1402": 2, ...}
        # A chunk that wasn't kept isn't in this lookup at all.

        years[year] = {
            # This year's entry, with two parts.

            "candidates": [
                # Part 1: one small dictionary per candidate, in hybrid order.

                {
                    "key": chunk_key(c),
                    # its short id, e.g. "FY2025:850"

                    "hybrid_rank": i + 1,
                    # its position in hybrid search: 1 to 20

                    "semantic_rank": semantic_rank.get(chunk_key(c)),
                    # its position after the semantic ranker: 1 to 5, or
                    # None if the ranker dropped it (not in the best 5)

                    "hybrid_score": c["score"],
                    # the hybrid (RRF) score, around 0.03

                    "reranker_score": c["reranker_score"],
                    # the semantic ranker's score, 0 to 4

                    "position": c["position"],
                    # where in the report it came from

                    "year_label": c["year_label"],
                    # how the report writes its year, e.g. "FY 2024-25"

                    "text": c["text"],
                    # the chunk's text
                }
                for i, c in enumerate(candidates)
                # do that for every candidate, with its position i (0 to 19)
            ],

            "kept_keys": [chunk_key(c) for c in kept],
            # Part 2: the ids of the 5 kept chunks, best first.
        }

    return years
    # e.g. {"FY2025": {"candidates": [...20...], "kept_keys": [...5...]}, "FY2026": {...}}


def describe(state: dict, thread_id: str) -> dict:
    # Takes the state after a run, and its thread id. Gives back EVERYTHING
    # the page needs to show, as one dictionary. This is what goes back to
    # the browser, as JSON.
    """Everything the page needs from one graph state."""
    # The function's one-line description.

    paused = bool(GRAPH.get_state({"configurable": {"thread_id": thread_id}}).next)
    # Is this run PAUSED, waiting for a person?
    #   GRAPH.get_state(...)   the saved state of this run
    #   .next                  the node waiting to run next:
    #                          ("human_review",) when paused, () when finished
    #   bool(...)              True if something is waiting, False if not

    return {
        # One dictionary, with every field the page shows.

        "thread_id": thread_id,
        # the run's id. The page sends it back with a review decision.

        "question": state["question"],
        # what was asked

        "company": state["company"],
        # which company

        "input_allowed": state["input_allowed"],
        # did the question pass the input guardrails?

        "input_block_reason": state["input_block_reason"],
        # if not, why

        "years": describe_retrieval(state),
        # the before-and-after reranking, per year

        "draft_answer": state["draft_answer"],
        # gpt-5.1's answer, before any check

        "grounding_verdict": state["grounding_verdict"],
        # grounded, needs_review or ungrounded

        "grounding_score": state["grounding_score"],
        # the share of numbers that traced back

        "unsupported_items": state["unsupported_items"] or [],
        # exactly what failed. An empty list if nothing did.

        "paused": paused,
        # True means: waiting for a person. The page then shows the review buttons.

        "review_decision": state["review_decision"],
        # approved, rejected, or nothing yet

        "final_answer": state["final_answer"],
        # what the user sees
    }


# ── WHO IS ASKING? ────────────────────────────────────────────────────────────

def signed_in_user(headers) -> dict | None:
    # Takes the request's headers. Gives back who is asking:
    #   on Azure, signed in   → {"id": ..., "name": their email}
    #   on a laptop           → {"id": "local", "name": "local"}
    #   on Azure, NOT signed in → None, which means: refuse the request
    # "dict | None" means: a dictionary, or None.
    """Who is asking: the user App Service signed in; "local" on a laptop; None = refuse (Azure, no sign-in)."""
    # The function's one-line description.

    user_id = headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    # Read the person's unique id from the header App Service adds after
    # sign-in. On a laptop, this header is never there, so it's None.

    if user_id:
        # Is there an id? Then the person signed in, on Azure.

        name = headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or user_id
        # Read their name, usually their email address.
        # "or user_id": if the name header is missing, use the id instead.

        return {"id": user_id, "name": name}
        # Give back who they are.

    if ON_APP_SERVICE:
        # No sign-in headers, but we ARE on Azure?

        return None
        # Then sign-in must be switched off, or broken. So we refuse the
        # request, rather than answering a stranger.

    return {"id": "local", "name": "local"}
    # We're on a laptop. There's no sign-in here, so everyone is "local".


# ── THE TWO ACTIONS: ASK A QUESTION, AND REVIEW AN ANSWER ─────────────────────

def run_query(body: dict, user: dict) -> dict:
    # Runs ONE question through the pipeline.
    #   body   the JSON the page sent: {"question": "...", "company": "..."}
    #   user   who is asking (from signed_in_user)
    # Gives back everything the page needs (from describe).

    question = (body.get("question") or "").strip()
    # The question.
    #   body.get("question")   the question, or None if it's missing
    #   or ""                  if missing, use empty text instead
    #   .strip()               remove spaces at the start and end

    company = body.get("company") or config.COMPANIES[0]
    # The company. If none was sent, use the first company in our list.

    if not question:
        # Is the question empty?

        raise ValueError("Please type a question.")
        # Then stop, with a message for the user.
        # A ValueError becomes a "400: bad input" reply (see handle_post below).

    if company not in config.COMPANIES:
        # Is it one of our companies?

        raise ValueError(f"Unknown company: {company}")
        # No: stop, with a message.

    thread_id = str(uuid.uuid4())
    # A brand-new unique id for this run. The checkpointer saves the run
    # under this id, so a later review can find it.

    print(f"QUERY  by {user['name']}  [{company}]  {question[:80]}")
    # One line in the server log for every question: who asked, which
    # company, and the first 80 characters of the question.
    # On Azure, we see these lines with: az webapp log tail

    state = GRAPH.invoke(create_initial_state(question, company), {"configurable": {"thread_id": thread_id}})
    # RUN THE PIPELINE.
    #   create_initial_state(...)        a fresh state with the question and company
    #   {"configurable": {...}}          the thread: which run this is
    # It stops at the end, or at the pause before human review.

    return describe(state, thread_id)
    # Give back everything the page needs, including "paused".


def resume_review(body: dict, user: dict) -> dict:
    # Resumes a PAUSED run, with the reviewer's decision.
    #   body   {"thread_id": "...", "decision": "approved"/"rejected", "note": "..."}
    #   user   who is reviewing

    thread_id = body.get("thread_id")
    # Which run to resume.

    decision = body.get("decision")
    # The reviewer's decision.

    if decision not in ("approved", "rejected"):
        # Is it one of the two allowed decisions?

        raise ValueError("decision must be 'approved' or 'rejected'")
        # No: stop, with a message.

    thread = {"configurable": {"thread_id": thread_id}}
    # The thread for this run, in the shape LangGraph expects.

    if not GRAPH.get_state(thread).next:
        # Is this run actually waiting for review?
        # If .next is empty, it's not paused: it finished, or it doesn't exist.

        raise ValueError("That run is not waiting for review (the server may have restarted).")
        # Then stop, with a message.
        # The most common reason: the server restarted, and MemorySaver
        # forgot every paused run, because it keeps them only in memory.

    GRAPH.update_state(thread, {"review_decision": decision, "review_note": (body.get("note") or "").strip() or None})
    # WRITE the reviewer's decision into the run's saved state:
    #   review_decision   "approved" or "rejected"
    #   review_note       the corrected answer, if they wrote one.
    #                     (body.get("note") or "")  the note, or empty text
    #                     .strip()                  remove spaces at the ends
    #                     or None                   empty text becomes None

    state = GRAPH.invoke(None, thread)
    # RESUME the run.
    #   None     don't start again: carry on from the pause
    # Now human_review_node runs, and sets the final answer.

    print(f"REVIEW {decision} by {user['name']}  thread {thread_id}")
    # One line in the server log: the decision, WHO made it, and which run.

    result = describe(state, thread_id)
    # Everything the page needs, after the review.

    result["reviewed_by"] = user["name"]
    # Add the reviewer's name, so the page can show who approved or rejected it.

    return result
    # Send it back to the page.


# ── THE REQUEST HANDLER: ONE OBJECT PER REQUEST ───────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    # Handles every request that reaches our server.
    # It's built ON TOP of Python's SimpleHTTPRequestHandler, which can
    # already send files. We add our own addresses, sign-in checks and
    # monitoring.
    # Python creates a new Handler for each request, and calls do_GET,
    # do_POST or do_HEAD, depending on the kind of request.

    def __init__(self, *args, **kwargs):
        # Runs when a Handler is created, for each request.
        #   *args, **kwargs   whatever Python passes in. We pass them on unchanged.

        super().__init__(*args, directory=str(UI_DIR), **kwargs)
        # Set up the built-in handler, with one change: send files from the
        # UI/ folder (not from wherever the server was started).
        #   super()   the built-in SimpleHTTPRequestHandler we're building on

    def send_response(self, code, message=None):
        # Runs every time we send a reply's status code (200, 404, ...).
        """Remember the status code of every response, so the monitoring span can record it."""
        # The function's one-line description.

        self.status_code = code
        # Remember the code. traced_request() reads it after the reply is sent.

        super().send_response(code, message)
        # Then send it as usual, with the built-in handler.

    def traced_request(self, method: str, handle) -> None:
        # Runs ONE request inside a monitoring SPAN, so it appears in
        # Application Insights as a request, with its time and status code.
        #   method   "GET" or "POST"
        #   handle   the function that does the real work
        """Run one request inside a span, so it appears in Application Insights as a request."""
        # The function's one-line description.

        path = self.path.split("?")[0]
        # The address, without any "?..." part on the end.
        #   "/api/query?x=1".split("?")   → ["/api/query", "x=1"]
        #   [0]                           → "/api/query"

        with telemetry.tracer.start_as_current_span(f"{method} {path}", kind=SpanKind.SERVER) as span:
            # Open a span called, for example, "POST /api/query".
            #   kind=SpanKind.SERVER   mark it as a request that ARRIVED at our
            #                          server, so it's listed under REQUESTS
            #   with ... as span       the span stays open while the code inside
            #                          runs, and closes (recording the time)
            #                          when it finishes
            # Every pipeline step and every Azure call made inside it is
            # recorded UNDER this span. So, in Transaction search, one question
            # shows as one request, with all its steps inside.

            handle()
            # Do the real work: send the page, or run the question.

            span.set_attribute("http.request.method", method)
            # Label the span with the kind of request: GET or POST.

            span.set_attribute("url.path", path)
            # Label it with the address, e.g. /api/query.

            span.set_attribute("http.response.status_code", getattr(self, "status_code", 0))
            # Label it with the status code we sent: 200, 400, 401, 500...
            #   getattr(self, "status_code", 0)   read self.status_code (saved
            #                                     by send_response above), or 0
            #                                     if no reply was sent

    # ── GET requests ──────────────────────────────────────────────────────────

    def do_GET(self):
        # Runs for every GET request.

        if self.path == "/health":
            # Is it Azure's health check?

            return self.send_json({"status": "ok"})
            # Answer straight away: {"status": "ok"}.
            # Here, we can notice two things:
            #   - it's open to everyone (sign-in excludes /health too)
            #   - it's NOT recorded. Azure visits it every minute, so
            #     recording it would fill Application Insights with noise.

        self.traced_request("GET", self.handle_get)
        # Every other GET is recorded, and handled by handle_get below.

    def handle_get(self):
        # The real work for a GET request.

        if self.path in PAGES:
            # Is it the page itself ("/" or "/index.html")?
            # No sign-in check here: on Azure, App Service already made the
            # person sign in before the request reached us.

            self.path = "/index.html"
            # "/" and "/index.html" both mean the page.

            return super().do_GET()
            # Let the built-in handler send the file UI/index.html.

        if not self.path.startswith("/api/"):
            # Any other address that isn't one of our /api/ addresses?
            # e.g. /server.py, or /something.txt

            return self.send_json({"error": "not found"}, 404)
            # Refuse it: 404, not found.
            # This is what stops the server from handing out server.py, or
            # any other file in UI/.

        user = signed_in_user(self.headers)
        # Who is asking?

        if user is None:
            # On Azure, without sign-in?

            return self.send_json({"error": "Sign-in required"}, 401)
            # Refuse: 401, not signed in.

        if self.path == "/api/me":
            # The page asks: who is signed in?

            return self.send_json(user)
            # Send back the person's id and name. The page shows the name at
            # the top right.

        if self.path == "/api/config":
            # The page asks for our settings.

            return self.send_json({"companies": config.COMPANIES, "fiscal_years": config.FISCAL_YEARS,
                                   "candidates_per_year": config.VECTOR_SEARCH_TOP_K,
                                   "kept_per_year": config.RERANK_TOP_K})
            # Send back: the companies (for the drop-down), the years, and
            # the two numbers 20 and 5 (for the labels on the page).

        return self.send_json({"error": "not found"}, 404)
        # Any other /api/ address: 404, not found.

    def do_HEAD(self):
        # Runs for HEAD requests.
        """HEAD is GET without a body; allow it only for the page, the same as GET."""
        # The function's one-line description.
        #
        # A HEAD request asks for the reply's HEADERS only, without the page
        # itself. Some tools use it to check a page exists.

        if self.path in PAGES:
            # Is it the page?

            self.path = "/index.html"
            # Both addresses mean index.html.

            return super().do_HEAD()
            # Let the built-in handler answer it.

        self.send_response(404)
        # Anything else: 404, not found.

        self.end_headers()
        # Finish the reply. (A HEAD reply has headers only, no body.)

    # ── POST requests ─────────────────────────────────────────────────────────

    def do_POST(self):
        # Runs for every POST request.

        self.traced_request("POST", self.handle_post)
        # Every POST is recorded, and handled by handle_post below.

    def handle_post(self):
        # The real work for a POST request.

        routes = {"/api/query": run_query, "/api/review": resume_review}
        # Which function handles which address:
        #   /api/query    → run_query       (ask a question)
        #   /api/review   → resume_review   (approve or reject a paused answer)

        if self.path not in routes:
            # Is it some other address?

            return self.send_json({"error": "not found"}, 404)
            # Refuse: 404.

        user = signed_in_user(self.headers)
        # Who is asking?

        if user is None:
            # On Azure, without sign-in?

            return self.send_json({"error": "Sign-in required"}, 401)
            # Refuse: 401.

        try:
            # "try" means: run the code below, and if something goes wrong,
            # jump to the matching "except", instead of crashing the server.

            length = int(self.headers.get("Content-Length") or 0)
            # How many bytes of data the page sent.
            #   Content-Length   a header that says the size of the data
            #   or 0             if it's missing, assume nothing was sent

            body = json.loads(self.rfile.read(length) or b"{}")
            # Read exactly that many bytes, and turn the JSON into a dictionary.
            #   self.rfile.read(length)   read the data the page sent
            #   or b"{}"                  if nothing was sent, use an empty JSON object
            #   json.loads(...)           JSON text → Python dictionary

            self.send_json(routes[self.path](body, user))
            # Call the right function for this address, with the data and the
            # user. Then send its result back as JSON.
            #   routes[self.path]        run_query or resume_review
            #   (body, user)             call it with the data and who's asking

        except ValueError as exc:
            # A ValueError means BAD INPUT: an empty question, an unknown
            # company, a run that isn't paused...

            self.send_json({"error": str(exc)}, 400)
            # Send back the message as it is, with 400: bad input.
            # These messages were written for the user, so it's fine to show them.

        except Exception as exc:
            # Anything else: something broke on the server.

            traceback.print_exc()
            # Print the full error details to the server log, so we can find the line.

            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            # Send back the error's type and message, with 500: server error.

    # ── SENDING A JSON REPLY ──────────────────────────────────────────────────

    def send_json(self, payload: dict, status: int = 200):
        # Sends a dictionary back to the browser, as JSON.
        #   payload   the data to send
        #   status    the status code. Left out → 200 (OK)

        data = json.dumps(payload, default=str).encode("utf-8")
        # Turn the dictionary into JSON text, then into bytes.
        #   json.dumps(...)   dictionary → JSON text
        #   default=str       if a value can't be turned into JSON directly,
        #                     turn it into text first, instead of crashing
        #   .encode("utf-8")  text → bytes, which is what the network sends

        self.send_response(status)
        # Send the status code first, e.g. 200.

        self.send_header("Content-Type", "application/json")
        # Tell the browser the data is JSON.

        self.send_header("Content-Length", str(len(data)))
        # Tell the browser how many bytes are coming.

        self.end_headers()
        # Finish the headers.

        self.wfile.write(data)
        # Send the data itself.


# ── START THE SERVER ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python UI/server.py
    # (On Azure, startup.sh runs exactly this.)

    # The port comes from the command line, else from PORT (set by Azure), else 8000
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    # Which PORT to listen on. A port is a numbered door on the computer;
    # browsers knock on it to reach our server.
    #   sys.argv[1]                       a port typed after the file name, e.g. 8080
    #   os.environ.get("PORT", "8000")    otherwise, the PORT Azure gives us,
    #                                     or 8000 if there isn't one (a laptop)
    #   int(...)                          turn the text into a number

    # On Azure, listen on every network address (0.0.0.0) so Azure's front end can reach the app;
    # on a laptop, stay on 127.0.0.1 so only this computer can open it, as before
    host = "0.0.0.0" if ON_APP_SERVICE else "127.0.0.1"
    # Which network ADDRESS to listen on:
    #   0.0.0.0     every address. Azure's front end sends visitors to our
    #               app over the network, so it must be able to reach us.
    #   127.0.0.1   this computer only. On a laptop, nobody else on the
    #               Wi-Fi can open the app.

    print(f"Annual-report RAG UI on http://{host}:{port}   (Ctrl+C to stop)")
    # Print where the server is listening, e.g.
    #   Annual-report RAG UI on http://127.0.0.1:8000   (Ctrl+C to stop)

    ThreadingHTTPServer((host, port), Handler).serve_forever()
    # START the server, and keep it running until Ctrl+C.
    #   ThreadingHTTPServer((host, port), Handler)   a server on that address
    #                                                and port, using our Handler
    #                                                for every request
    #   .serve_forever()                             wait for requests, and
    #                                                handle each one, forever