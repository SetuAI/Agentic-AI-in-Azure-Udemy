"""
================================================================================
OFFLINE TESTS FOR HOSTING - the web server, sign-in and the health check
================================================================================

PURPOSE
-------
Checks the parts of UI/server.py that were added for Azure App Service, by
starting the real server on this computer and sending it real HTTP requests:

    /health             answers {"status": "ok"}
    the page            is served; other files in UI/ (server.py) are not
    /api/me             "local" on a laptop; the signed-in name on Azure
    sign-in             on Azure, /api/* is refused without the sign-in headers
    review              the reviewer's name comes back with the final answer
    monitoring          each request and each pipeline step is recorded as a span

Everything runs OFFLINE: no Azure account, no keys, no cost. The pipeline's
working steps are replaced with stubs, exactly as in test_guardrails_and_graph.py.

HOW TO RUN
----------
    python -m pytest tests/test_hosting.py -v

EXPECTED OUTPUT
---------------
    7 passed
================================================================================
"""

import json                                     # read the JSON the server sends back
import threading                                # run the server in the background while the test talks to it
import urllib.error                             # an HTTP error status (404, 401...) arrives as an exception
import urllib.request                           # Python's built-in way to send an HTTP request
import uuid                                     # unique run ids
from http.server import ThreadingHTTPServer     # the same server class UI/server.py uses

import pytest                                   # the test runner

from graph import build_graph as graph_module   # the wiring, with nodes we can swap out
from graph.state import create_initial_state    # a fresh state
from guardrails.factual import NEEDS_REVIEW     # the verdict that makes the graph pause
from UI import server                           # the web server being tested

SIGNED_IN = {"X-MS-CLIENT-PRINCIPAL-ID": "id-123",          # the two headers App Service adds after sign-in
             "X-MS-CLIENT-PRINCIPAL-NAME": "reviewer@tarka.example"}


@pytest.fixture
def base_url():
    """Start the real server on a free port, hand its address to the test, then stop it."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)  # port 0 = "pick any free port"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)  # run it in the background
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"            # the test runs here
    httpd.shutdown()                                                # stop the server afterwards


def call(url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    """Send one request; return (status code, JSON body or plain text). POST if a body is given."""
    data = json.dumps(body).encode() if body is not None else None  # a body means POST
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request) as response:            # send it and read the answer
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as error:                          # 4xx and 5xx arrive as exceptions
        status, text = error.code, error.read().decode()
    try:
        return status, json.loads(text)                              # most answers are JSON
    except json.JSONDecodeError:
        return status, text                                          # the page itself is HTML


def test_health_is_open(base_url):
    assert call(f"{base_url}/health") == (200, {"status": "ok"})


def test_page_is_served_but_other_files_are_not(base_url):
    status, page = call(f"{base_url}/")
    assert status == 200 and "<html" in page                         # the page
    assert call(f"{base_url}/server.py")[0] == 404                   # the server's own source code is NOT handed out


def test_laptop_user_is_local(base_url):
    assert call(f"{base_url}/api/me") == (200, {"id": "local", "name": "local"})


def test_azure_without_sign_in_is_refused(base_url, monkeypatch):
    monkeypatch.setattr(server, "ON_APP_SERVICE", True)              # pretend we are on App Service
    assert call(f"{base_url}/api/config")[0] == 401                  # no sign-in headers -> refused
    assert call(f"{base_url}/api/query", {"question": "q"})[0] == 401
    assert call(f"{base_url}/health")[0] == 200                      # health stays open


def test_azure_signed_in_user_is_named(base_url, monkeypatch):
    monkeypatch.setattr(server, "ON_APP_SERVICE", True)
    assert call(f"{base_url}/api/me", headers=SIGNED_IN) == (200, {"id": "id-123", "name": "reviewer@tarka.example"})


def test_review_records_who_decided(base_url, monkeypatch):
    """Pause a run (stubbed nodes), then approve it through the server, signed in as a reviewer."""
    monkeypatch.setattr(graph_module, "input_guardrail_node", lambda s: {"input_allowed": True})
    monkeypatch.setattr(graph_module, "retrieve_node", lambda s: {"candidate_chunks": {}})
    monkeypatch.setattr(graph_module, "rerank_node", lambda s: {"reranked_chunks": {}})
    monkeypatch.setattr(graph_module, "generate_node", lambda s: {"draft_answer": "draft"})
    monkeypatch.setattr(graph_module, "output_guardrail_node",
                        lambda s: {"grounding_verdict": NEEDS_REVIEW, "review_required": True})
    graph = graph_module.build_graph()                               # real wiring, stubbed steps
    monkeypatch.setattr(server, "GRAPH", graph)                      # the server uses this graph
    monkeypatch.setattr(server, "ON_APP_SERVICE", True)

    thread_id = str(uuid.uuid4())
    graph.invoke(create_initial_state("q", "Infosys"), {"configurable": {"thread_id": thread_id}})  # now paused

    status, result = call(f"{base_url}/api/review", {"thread_id": thread_id, "decision": "approved"},
                          headers=SIGNED_IN)
    assert status == 200
    assert result["reviewed_by"] == "reviewer@tarka.example"         # the reviewer's name came back
    assert result["final_answer"] == "draft" and not result["paused"]


def test_requests_and_steps_are_recorded(base_url, monkeypatch):
    """Monitoring: one span per request (with its status) and one per pipeline step, as Application Insights receives them."""
    from opentelemetry import trace                                  # the tracing API telemetry.py uses
    from opentelemetry.sdk.trace import TracerProvider               # a real tracer, instead of the default no-op
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # keeps spans in a list

    exporter = InMemorySpanExporter()                                # spans land here instead of in Azure
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)                              # telemetry.tracer now records for real

    monkeypatch.setattr(graph_module, "input_guardrail_node",        # a question the input guardrail blocks:
                        lambda s: {"input_allowed": False, "input_block_reason": "stub"})  # runs one step, then ends
    monkeypatch.setattr(server, "GRAPH", graph_module.build_graph())

    assert call(f"{base_url}/api/query", {"question": "q", "company": "Infosys"})[0] == 200
    names = {span.name: span for span in exporter.get_finished_spans()}
    assert "node.input_guardrail" in names                           # the pipeline step was recorded
    assert names["POST /api/query"].attributes["http.response.status_code"] == 200  # and the request, with its status