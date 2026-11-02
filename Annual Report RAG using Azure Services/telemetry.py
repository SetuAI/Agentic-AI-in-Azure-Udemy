"""
================================================================================
MONITORING - SEND WHAT THE APP DOES TO AZURE APPLICATION INSIGHTS
================================================================================

PURPOSE
-------
Switches on Azure Application Insights for the whole app, with one call:

    telemetry.setup()

and gives every other file one shared TRACER, which they use to record their
own steps:

    with telemetry.tracer.start_as_current_span("node.retrieve"):
        ...

WHAT PROBLEM DOES IT SOLVE
--------------------------
A question takes 10 to 30 seconds. Without monitoring, nobody can tell where
those seconds go, or which questions failed, once the app runs on Azure and
not on your laptop. With it, Application Insights shows:

    every request        POST /api/query, POST /api/review (from UI/server.py)
    every pipeline step  node.input_guardrail, node.retrieve, node.rerank,
                         node.generate, node.output_guardrail, ...
                         (from graph/build_graph.py)
    every call out       Azure OpenAI (chat and embeddings) and Azure AI Search,
                         recorded automatically, with how long each took

You see them in the Azure portal: Application map, Search (one question,
step by step), Performance, Failures and Logs.

WHEN IT SWITCHES ON
-------------------
Only if APPLICATIONINSIGHTS_CONNECTION_STRING has a value (in .env on your
laptop, or in the web app's app settings on Azure). Without it, setup() does
nothing, the spans cost nothing, and the app runs exactly as before. So the
tests, and anyone who hasn't created Application Insights, are not affected.

WHY TWO EXTRA PIECES ARE SWITCHED ON BY HAND
--------------------------------------------
    1. The web server. FastAPI apps are recorded automatically, but this
       project uses Python's built-in http.server, which nothing records by
       itself. UI/server.py therefore opens one span per request, using the
       tracer below.
    2. Azure OpenAI calls. The openai library sends its requests with a
       library called httpx, which Azure's package does not record by itself.
       HTTPXClientInstrumentor switches that on.
    Azure AI Search calls need nothing extra: Azure's own SDKs are recorded
    automatically.

HOW TO RUN
----------
Never run directly. UI/server.py calls setup() once, at the very top, before
any Azure client is created.

EXPECTED OUTPUT (printed when the server starts)
------------------------------------------------
    Application Insights: sending traces as "annual-report-rag"
or, without a connection string:
    Application Insights: not configured, skipping
================================================================================
"""

import os                                       # os.environ reads and sets environment variables

from opentelemetry import trace                 # OpenTelemetry: the standard for recording what a program does

import config  # noqa: F401                     # imported only so it reads .env first; then the connection string below is visible

SERVICE_NAME = "annual-report-rag"              # the name this app appears under in Application Insights (the box on the map)

tracer = trace.get_tracer("annual_report_rag")  # the one tracer every file uses to open spans; costs nothing until setup() runs


def setup() -> bool:
    """Switch on Application Insights if a connection string is set. Returns True if it was switched on."""
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")  # read after config loaded .env
    if not connection_string:                   # empty or missing?
        print("Application Insights: not configured, skipping")  # say so in the server log
        return False                            # nothing switched on; every span is a cheap no-op

    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)  # label this app on the Application map (only if not already set)

    from azure.monitor.opentelemetry import configure_azure_monitor  # imported here: only needed when monitoring is on
    configure_azure_monitor()                   # connect OpenTelemetry to Application Insights; reads the connection string itself
                                                # it also records every Azure SDK call, so Azure AI Search appears on the map

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # records calls made with the httpx library
    HTTPXClientInstrumentor().instrument()      # from now on, every Azure OpenAI request is recorded as a dependency
                                                # must run BEFORE the openai client is built; azure_clients.py builds it lazily, so it is

    print(f'Application Insights: sending traces as "{SERVICE_NAME}"')  # confirm in the server log
    return True                                 # monitoring is on