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
records every HTTP request to FastAPI, and every span our agent opens
(agent.ask, agent.write_sql, ...; see agent/graph.py).

Where to see it in Azure: Application Insights -> Transaction search
(each question as one request, with its steps inside it), and
Performance (how long requests take). Data appears after 1 to 3 minutes.

If the connection string is not set, nothing is sent, and the spans cost
nothing: the app runs exactly the same, just without Azure monitoring.
"""

import os

from dotenv import load_dotenv


def setup() -> bool:
    load_dotenv()
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        print("Application Insights: not configured, skipping")
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor
    # The service name is how this app is labelled in Application Insights.
    os.environ.setdefault("OTEL_SERVICE_NAME", "fundgraph-api")
    configure_azure_monitor()
    print("Application Insights: sending traces")
    return True