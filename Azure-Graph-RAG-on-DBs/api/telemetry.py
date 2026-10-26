"""
api/telemetry.py
================

WHAT THIS FILE IS FOR
---------------------
Switches on Azure Application Insights for the whole app, with one call:

    telemetry.setup()      # must run BEFORE the FastAPI app is created

configure_azure_monitor() sets up OpenTelemetry, the standard for sending
traces, and points it at Application Insights using
APPLICATIONINSIGHTS_CONNECTION_STRING from .env. It then automatically
records every HTTP request to FastAPI, every outgoing HTTP call (Azure
OpenAI, Content Safety), and every span our agent opens (agent.ask,
agent.write_sql, ...; see agent/graph.py).

PostgreSQL is added separately: the Azure package only knows the older
psycopg2 library, and this project uses psycopg 3. PsycopgInstrumentor
records every query as a dependency, so PostgreSQL appears as its own
node in Application Map, with each query's duration. Only the SQL text
is recorded, never the values passed with it (such as embedding vectors).

Where to see it in Azure: Application Insights -> Transaction search
(each question as one request, with its steps inside it), and
Performance (how long requests take). Data appears after 1 to 3 minutes.

If the connection string is not set, nothing is sent, and the spans cost
nothing: the app runs exactly the same, just without Azure monitoring.
"""

# os: reads and sets environment variables.
import os

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv


# Returns True if monitoring was switched on, False if skipped.
def setup() -> bool:
    # Make sure .env has been read.
    load_dotenv()
    # No connection string (empty or missing): skip monitoring.
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        # Say so in the server log.
        print("Application Insights: not configured, skipping")
        # Nothing was set up.
        return False
    # Imported here, not at the top, so it only loads when monitoring is on.
    from azure.monitor.opentelemetry import configure_azure_monitor
    # The service name is how this app is labelled in Application Insights.
    os.environ.setdefault("OTEL_SERVICE_NAME", "fundgraph-api")
    # Connect OpenTelemetry to Application Insights (reads the connection string itself).
    configure_azure_monitor()
    # Must run before any database connection is opened: it wraps psycopg.connect.
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    # From now on every psycopg query is recorded as a dependency.
    PsycopgInstrumentor().instrument()
    # Confirm in the server log.
    print("Application Insights: sending traces")
    # Monitoring is on.
    return True