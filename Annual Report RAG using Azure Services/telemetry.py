"""
================================================================================
telemetry.py  -  MONITORING: SEND WHAT THE APP DOES TO AZURE APPLICATION INSIGHTS
================================================================================

PURPOSE
-------
This file switches on Azure Application Insights for the whole app, with
one call:

    telemetry.setup()

And it gives every other file one shared TRACER, which they use to record
their own steps:

    with telemetry.tracer.start_as_current_span("node.retrieve"):
        ...

WHAT PROBLEM DOES IT SOLVE
--------------------------
A question takes several seconds to answer. On the laptop, we can watch the
terminal. But once the app runs on Azure, we can't see inside it. So if a
question is slow, or fails, nobody can tell WHERE the time went, or WHAT
went wrong.

With this file switched on, Application Insights shows us:

    every request        POST /api/query, POST /api/review   (from UI/server.py)
    every pipeline step  node.input_guardrail, node.retrieve, node.rerank,
                         node.generate, node.output_guardrail, ...
                                                             (from graph/build_graph.py)
    every call out       Azure OpenAI (chat and embeddings) and Azure AI Search,
                         recorded automatically, with how long each one took

We see all of this in the Azure portal: Application map, Transaction search
(one question, step by step), Performance, Failures and Logs.

THE KEY WORDS IN THIS FILE
--------------------------
    APPLICATION INSIGHTS   Azure's monitoring service. It stores and shows what
                           our app did, and how long each part took.
    OPENTELEMETRY          the standard, free toolkit for recording what a
                           program does. Our code records with OpenTelemetry;
                           Azure's package sends the records to Application
                           Insights.
    SPAN                   ONE timed record of ONE piece of work. For example:
                           "node.retrieve took 1.8 seconds".
    TRACER                 the object that CREATES spans.
    DEPENDENCY             a call our app makes to another service, like Azure
                           OpenAI. On the Application map, each dependency is
                           a circle our app's arrows point to.
    CONNECTION STRING      the address our app sends its records to. It comes
                           from the Application Insights resource in Azure.

WHEN IT SWITCHES ON
-------------------
ONLY if the setting APPLICATIONINSIGHTS_CONNECTION_STRING has a value: in .env
on the laptop, or in the web app's app settings on Azure.

Without it, setup() does nothing, every span costs nothing, and the app runs
exactly as before. So the tests, and anyone who hasn't created Application
Insights, are not affected at all.

WHY TWO EXTRA PIECES ARE SWITCHED ON BY HAND
--------------------------------------------
Azure's package records many things by itself. But not these two:

    1. OUR WEB SERVER. Popular frameworks like FastAPI are recorded
       automatically. But this project uses Python's built-in http.server,
       which nothing records by itself. So UI/server.py opens one span per
       request itself, using the tracer from this file.

    2. AZURE OPENAI CALLS. The openai library sends its requests using another
       library called httpx. Azure's package doesn't record httpx by itself.
       So we switch that on here, with HTTPXClientInstrumentor.

    Azure AI Search calls need nothing extra: Azure's own SDKs are recorded
    automatically.

WHERE IT FITS
-------------
    UI/server.py          calls telemetry.setup() once, at the very top
    graph/build_graph.py  uses telemetry.tracer: one span per pipeline step
    UI/server.py          uses telemetry.tracer: one span per request

HOW TO RUN
----------
Never run this file directly. UI/server.py calls setup() once, at the very
top, BEFORE any Azure client is created.

EXPECTED OUTPUT (printed when the server starts)
------------------------------------------------
    Application Insights: sending traces as "annual-report-rag"

or, without a connection string:

    Application Insights: not configured, skipping
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import os
# "os" lets us read and set ENVIRONMENT VARIABLES: named settings that the
# operating system keeps for a running program, like
# APPLICATIONINSIGHTS_CONNECTION_STRING.
#   os.environ.get(...)          read one
#   os.environ.setdefault(...)   set one, only if it isn't set already

from opentelemetry import trace
# OPENTELEMETRY: the standard toolkit for recording what a program does.
# From it, we use "trace": the part that creates SPANS (timed records of
# each piece of work).

import config  # noqa: F401
# Our settings file.
#
# Here, we can notice: we never USE config in this file. So why import it?
# Because importing config.py READS THE .env FILE into the environment
# variables. Then, a few lines below, os.environ can see
# APPLICATIONINSIGHTS_CONNECTION_STRING, even on the laptop.
#
# "# noqa: F401" is a note for code-checking tools (linters). F401 means
# "imported but never used". noqa means "no quality assurance warning here":
# don't warn about it, because we import it on purpose.


# ── THE NAME, AND THE SHARED TRACER ───────────────────────────────────────────

SERVICE_NAME = "annual-report-rag"
# The name our app appears under in Application Insights.
# It's the name on the big circle in the Application map, and the name
# printed in the server log when monitoring starts.

tracer = trace.get_tracer("annual_report_rag")
# The ONE tracer every file uses to create spans.
#   trace.get_tracer(...)   give us a tracer, labelled "annual_report_rag"
#
# Other files use it like this:
#   with telemetry.tracer.start_as_current_span("node.retrieve"):
#       ... the work ...
#
# Here's a nice detail: until setup() switches monitoring on, this tracer
# does NOTHING. Each span it creates is empty, and costs almost nothing. So
# the graph and the server can always use it, whether monitoring is on or off.


# ── SWITCH MONITORING ON ──────────────────────────────────────────────────────

def setup() -> bool:
    # Switches Application Insights on, if a connection string is set.
    # Gives back True if monitoring was switched on, or False if it wasn't.
    # UI/server.py calls this ONCE, when the server starts.
    """Switch on Application Insights if a connection string is set. Returns True if it was switched on."""
    # The function's one-line description.

    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    # Read the connection string: the address to send our records to.
    #   On the laptop:  it comes from .env (loaded by "import config" above)
    #   On Azure:       it comes from the web app's app settings
    # .get(...) gives back None if it isn't set, instead of crashing.

    if not connection_string:
        # Is it empty, or missing?

        print("Application Insights: not configured, skipping")
        # Then say so in the server log, so we know monitoring is OFF.

        return False
        # And stop here. Nothing is switched on.
        # Every span from the tracer above stays an empty, almost-free record.

    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)
    # Set the environment variable OTEL_SERVICE_NAME to "annual-report-rag".
    # OpenTelemetry reads this variable to know what our app is called.
    # That's the name on our circle in the Application map.
    #   setdefault   set it ONLY if it isn't set already. So if someone set a
    #                different name in the app settings, we keep theirs.

    from azure.monitor.opentelemetry import configure_azure_monitor
    # Azure's package that connects OpenTelemetry to Application Insights.
    # It's imported HERE, inside the function, because it's only needed when
    # monitoring is on. When it's off, we never load it at all.

    configure_azure_monitor()
    # CONNECT our app to Application Insights.
    # It reads the connection string by itself, from the environment variable.
    #
    # From now on, it also records every call made with Azure's own SDKs, by
    # itself. That's how Azure AI Search appears on the Application map,
    # without any extra code from us.
    # (It also checks in with Azure Monitor for its own settings. That's the
    # small "settings...azure.com" circle on the map.)

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    # A tool that records every web request made with the httpx library.
    # The openai library uses httpx to talk to Azure OpenAI.

    HTTPXClientInstrumentor().instrument()
    # Switch it on. From now on, every Azure OpenAI request (embeddings and
    # gpt-5.1) is recorded as a DEPENDENCY, with how long it took.
    #
    # Here, we can notice the ORDER matters: this must run BEFORE the openai
    # client is created. Otherwise, that client would already exist without
    # recording switched on.
    # That's fine in our project: azure_clients.py only creates the openai
    # client the first time it's needed, which is after setup() has run.

    print(f'Application Insights: sending traces as "{SERVICE_NAME}"')
    # Confirm in the server log that monitoring is ON, and under which name:
    #   Application Insights: sending traces as "annual-report-rag"
    # On Azure, we see this line with: az webapp log tail

    return True
    # Monitoring is on.