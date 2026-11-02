"""
================================================================================
WEB UI - a small server for the annual-report pipeline (laptop or Azure)
================================================================================

Serves UI/index.html and exposes the LangGraph pipeline as JSON endpoints:

    POST /api/query    {"question": "...", "company": "Infosys"}
                       -> runs the graph; returns chunks before and after
                          reranking, the grounding checks and the answer
                          (or a "paused" flag if a human must review it)

    POST /api/review   {"thread_id": "...", "decision": "approved"|"rejected",
                        "note": "optional corrected answer"}
                       -> resumes a paused run and returns the final answer,
                          with the name of the person who reviewed it

    GET  /api/config   companies, years and top-k settings, for the page
    GET  /api/me       NEW: who is signed in ("local" on a laptop)
    GET  /health       NEW: {"status": "ok"}; Azure's health check calls this

No web framework: it uses Python's built-in http.server.

WHAT CHANGED FOR HOSTING ON AZURE APP SERVICE
---------------------------------------------
    1. It listens on 0.0.0.0 and the PORT Azure gives it. On a laptop it still
       listens on 127.0.0.1:8000, so nothing changes there.
    2. GET /health, open to everyone, for App Service's health check.
    3. SIGN-IN. On Azure, App Service makes people sign in with Microsoft
       BEFORE a request reaches this file, then adds two headers naming them:
           X-MS-CLIENT-PRINCIPAL-ID     their unique id
           X-MS-CLIENT-PRINCIPAL-NAME   usually their email address
       App Service removes these headers from anything a user sends and adds
       its own, so they can be trusted there. This file uses them to:
           - record WHO approved or rejected each paused answer
           - refuse /api/* on Azure if they are missing, which means sign-in
             was switched off by mistake
       On a laptop there is no sign-in, and everyone is "local".
    4. Only index.html is served from UI/. Before, the built-in server would
       also have handed out any other file in this folder, server.py included.
    5. MONITORING. Each request is recorded as a span in Application
       Insights (see telemetry.py). Python's built-in server is not recorded
       automatically, so this file does it.

HOW TO RUN
----------
    python UI/server.py              then open http://localhost:8000
    python UI/server.py 8080         a different port
On Azure, startup.sh runs it, and Azure sets PORT.

EXPECTED OUTPUT
---------------
    Application Insights: not configured, skipping
    Annual-report RAG UI on http://127.0.0.1:8000   (Ctrl+C to stop)
================================================================================
"""

import json                                     # request and response bodies
import os                                       # NEW: reads PORT and WEBSITE_SITE_NAME from the environment
import sys                                      # command-line port, import path
import traceback                                # send readable errors to the browser
import uuid                                     # a unique id for each run
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # built-in web server
from pathlib import Path                        # Path builds file paths

UI_DIR = Path(__file__).parent                  # this folder: UI/
sys.path.insert(0, str(UI_DIR.parent))          # make the project folder importable

import telemetry                                # NEW: Application Insights setup and the shared tracer

telemetry.setup()                               # NEW: switch monitoring on FIRST, before any Azure client is created
                                                # (does nothing if APPLICATIONINSIGHTS_CONNECTION_STRING is empty)

from opentelemetry.trace import SpanKind        # NEW: SERVER marks a span as an incoming request (the "requests" table)

import config                                   # companies, years, top-k settings
from graph.build_graph import build_graph       # the pipeline
from graph.state import create_initial_state    # a fresh state

GRAPH = build_graph()                           # one compiled graph; its MemorySaver keeps paused runs

ON_APP_SERVICE = bool(os.environ.get("WEBSITE_SITE_NAME"))  # NEW: True only on Azure App Service, which always sets this
PAGES = {"/", "/index.html"}                    # NEW: the only files served from UI/ ("/" means index.html)


def chunk_key(chunk: dict) -> str:
    """A stable id for a chunk: its year plus its position in the report."""
    return f"{chunk['fiscal_year']}:{chunk['position']}"


def describe_retrieval(state: dict) -> dict:
    """Per year: every candidate in hybrid order, with where the semantic ranker put it (if kept)."""
    years = {}
    for year in sorted((state.get("candidate_chunks") or {}).keys()):
        candidates = state["candidate_chunks"][year]                     # hybrid order
        kept = (state.get("reranked_chunks") or {}).get(year, [])        # semantic order
        semantic_rank = {chunk_key(c): i + 1 for i, c in enumerate(kept)}
        years[year] = {
            "candidates": [
                {
                    "key": chunk_key(c),
                    "hybrid_rank": i + 1,
                    "semantic_rank": semantic_rank.get(chunk_key(c)),   # None = dropped by the ranker
                    "hybrid_score": c["score"],
                    "reranker_score": c["reranker_score"],
                    "position": c["position"],
                    "year_label": c["year_label"],
                    "text": c["text"],
                }
                for i, c in enumerate(candidates)
            ],
            "kept_keys": [chunk_key(c) for c in kept],                   # semantic order, best first
        }
    return years


def describe(state: dict, thread_id: str) -> dict:
    """Everything the page needs from one graph state."""
    paused = bool(GRAPH.get_state({"configurable": {"thread_id": thread_id}}).next)
    return {
        "thread_id": thread_id,
        "question": state["question"],
        "company": state["company"],
        "input_allowed": state["input_allowed"],
        "input_block_reason": state["input_block_reason"],
        "years": describe_retrieval(state),
        "draft_answer": state["draft_answer"],
        "grounding_verdict": state["grounding_verdict"],
        "grounding_score": state["grounding_score"],
        "unsupported_items": state["unsupported_items"] or [],
        "paused": paused,                       # True = waiting for a human decision
        "review_decision": state["review_decision"],
        "final_answer": state["final_answer"],
    }


def signed_in_user(headers) -> dict | None:
    """NEW: who is asking. The user App Service signed in; "local" on a laptop; None = refuse (Azure, no sign-in)."""
    user_id = headers.get("X-MS-CLIENT-PRINCIPAL-ID")       # added by App Service after sign-in; never present on a laptop
    if user_id:                                             # signed in on Azure
        name = headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or user_id  # usually their email; fall back to the id
        return {"id": user_id, "name": name}
    if ON_APP_SERVICE:                                      # on Azure, but no sign-in headers
        return None                                         # sign-in is off or broken: refuse rather than answer a stranger
    return {"id": "local", "name": "local"}                 # on a laptop there is no sign-in: everyone is "local"


def run_query(body: dict, user: dict) -> dict:
    question = (body.get("question") or "").strip()
    company = body.get("company") or config.COMPANIES[0]
    if not question:
        raise ValueError("Please type a question.")
    if company not in config.COMPANIES:
        raise ValueError(f"Unknown company: {company}")
    thread_id = str(uuid.uuid4())
    print(f"QUERY  by {user['name']}  [{company}]  {question[:80]}")  # NEW: one log line per question (visible in the log stream)
    state = GRAPH.invoke(create_initial_state(question, company), {"configurable": {"thread_id": thread_id}})
    return describe(state, thread_id)


def resume_review(body: dict, user: dict) -> dict:
    thread_id = body.get("thread_id")
    decision = body.get("decision")
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    thread = {"configurable": {"thread_id": thread_id}}
    if not GRAPH.get_state(thread).next:
        raise ValueError("That run is not waiting for review (the server may have restarted).")
    GRAPH.update_state(thread, {"review_decision": decision, "review_note": (body.get("note") or "").strip() or None})
    state = GRAPH.invoke(None, thread)          # None = resume where it stopped
    print(f"REVIEW {decision} by {user['name']}  thread {thread_id}")  # NEW: who decided, in the log stream
    result = describe(state, thread_id)
    result["reviewed_by"] = user["name"]        # NEW: the reviewer's name goes back to the page with the final answer
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)  # serve files from UI/

    def send_response(self, code, message=None):
        """NEW: remember the status code of every response, so the monitoring span can record it."""
        self.status_code = code                 # read by traced_request() after the response is sent
        super().send_response(code, message)    # then send it as usual

    def traced_request(self, method: str, handle) -> None:
        """NEW: run one request inside a span, so it appears in Application Insights as a request."""
        path = self.path.split("?")[0]          # the address without any ?query part, e.g. /api/query
        with telemetry.tracer.start_as_current_span(f"{method} {path}", kind=SpanKind.SERVER) as span:
            handle()                            # do the real work: send the page or the JSON
            span.set_attribute("http.request.method", method)               # GET or POST
            span.set_attribute("url.path", path)                            # which address
            span.set_attribute("http.response.status_code", getattr(self, "status_code", 0))  # 200, 400, 401, 500...

    def do_GET(self):
        if self.path == "/health":              # NEW: Azure's health check; open to everyone, and not recorded (it runs every minute)
            return self.send_json({"status": "ok"})
        self.traced_request("GET", self.handle_get)  # everything else is recorded

    def handle_get(self):
        if self.path in PAGES:                  # NEW: the page itself; no sign-in check here, App Service already did it on Azure
            self.path = "/index.html"           # "/" and "/index.html" both mean the page
            return super().do_GET()             # let the built-in server send the file
        if not self.path.startswith("/api/"):   # NEW: any other file in UI/ (server.py, ...) is not served
            return self.send_json({"error": "not found"}, 404)
        user = signed_in_user(self.headers)     # NEW: who is asking
        if user is None:                        # NEW: on Azure without sign-in
            return self.send_json({"error": "Sign-in required"}, 401)
        if self.path == "/api/me":              # NEW: the page shows this name in its header
            return self.send_json(user)
        if self.path == "/api/config":
            return self.send_json({"companies": config.COMPANIES, "fiscal_years": config.FISCAL_YEARS,
                                   "candidates_per_year": config.VECTOR_SEARCH_TOP_K,
                                   "kept_per_year": config.RERANK_TOP_K})
        return self.send_json({"error": "not found"}, 404)

    def do_HEAD(self):
        """NEW: HEAD is GET without a body; allow it only for the page, the same as GET."""
        if self.path in PAGES:
            self.path = "/index.html"
            return super().do_HEAD()
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        self.traced_request("POST", self.handle_post)  # NEW: every POST is recorded

    def handle_post(self):
        routes = {"/api/query": run_query, "/api/review": resume_review}
        if self.path not in routes:
            return self.send_json({"error": "not found"}, 404)
        user = signed_in_user(self.headers)     # NEW: who is asking
        if user is None:                        # NEW: on Azure without sign-in
            return self.send_json({"error": "Sign-in required"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(routes[self.path](body, user))  # CHANGED: the user is passed on, for the logs and the reviewer's name
        except ValueError as exc:               # bad input: show the message as is
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:                # anything else: show it, and log the traceback
            traceback.print_exc()
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def send_json(self, payload: dict, status: int = 200):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    # CHANGED: the port comes from the command line, else from PORT (set by Azure), else 8000
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
    # NEW: on Azure, listen on every network address (0.0.0.0) so Azure's front end can reach the app;
    # on a laptop, stay on 127.0.0.1 so only this computer can open it, as before
    host = "0.0.0.0" if ON_APP_SERVICE else "127.0.0.1"
    print(f"Annual-report RAG UI on http://{host}:{port}   (Ctrl+C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()