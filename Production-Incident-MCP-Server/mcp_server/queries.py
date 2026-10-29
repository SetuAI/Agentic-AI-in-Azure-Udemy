"""
mcp_server/queries.py
=====================

WHAT THIS FILE IS FOR
---------------------
Holds the ready-made searches (queries) behind 4 of our 5 tools. They are
written in KQL, the search language of Log Analytics. Each function below
fills in the details (an app name, a time range) and returns the finished
search as text. tools.py then sends that text to Azure.

WHAT PROBLEM IT SOLVES
----------------------
The common questions ("which app had errors?", "what changed?") should
always be answered the same, tested way. So we write those searches once,
here, instead of letting the AI write them fresh every time.

A 30-SECOND KQL LESSON
----------------------
A KQL search starts with a table name, then passes the rows through steps.
Each step starts with "|" and keeps, changes or counts the rows:

    AppLogs_CL                          <- start with every log line
    | where Level == "ERROR"            <- keep only the errors
    | summarize Errors = count()        <- count them,
        by Service                      <- one count per app

IMPORTANT: SAFE INPUTS ONLY
---------------------------
These functions put values straight into the search text. So they must only
be given values that tools.py has already checked (a known app name, a real
date, a request ID in the right shape). Never pass the AI's raw text here.

HOW TO RUN
----------
You never run this file directly. tools.py imports it.

EXPECTED OUTPUT
---------------
Each function returns one KQL search as text.
"""

# datetime: a date plus a time.
from datetime import datetime

# The size of each time "bucket" when we count errors: 10 minutes.
BUCKET = "10m"

# A 10-minute bucket with at least this many errors counts as a "spike" (an unusual jump).
SPIKE_MIN_ERRORS = 5


# Turns a Python date and time (already in UTC) into a KQL date, like datetime(2026-10-29T02:00:00Z).
def kql_time(moment: datetime) -> str:
    """Return the time written the way KQL expects it."""
    # Write the time as year-month-dayThour:minute:second, add Z for UTC, and wrap it in datetime(...).
    return f"datetime({moment:%Y-%m-%dT%H:%M:%S}Z)"


# Search 1a: how many errors each app had in the last few hours.
def errors_per_service(hours: int) -> str:
    """Total errors per app in the last `hours` hours."""
    # The search, as one block of text. Step by step:
    #   ago(24h)          -> "24 hours before now"
    #   where Level ...   -> keep only error lines
    #   summarize ... by  -> one row per app, with its error count
    #   order by ... desc -> the app with the most errors first
    return f"""
AppLogs_CL
| where TimeGenerated > ago({hours}h)
| where Level == "ERROR"
| summarize Errors = count() by Service
| order by Errors desc
"""


# Search 1b: the 10-minute buckets where an app had an unusual number of errors.
def error_spikes(hours: int) -> str:
    """Every 10-minute bucket where one app had SPIKE_MIN_ERRORS or more errors."""
    # The search, as one block of text.
    # bin(TimeGenerated, 10m) rounds every time down to its 10-minute bucket,
    # so 02:13 and 02:17 both land in the 02:10 bucket and are counted together.
    return f"""
AppLogs_CL
| where TimeGenerated > ago({hours}h)
| where Level == "ERROR"
| summarize Errors = count() by Service, BucketStart = bin(TimeGenerated, {BUCKET})
| where Errors >= {SPIKE_MIN_ERRORS}
| order by BucketStart asc, Service asc
"""


# Search 2: the most common error messages from one app in a time range.
def top_errors(service: str, start: datetime, end: datetime) -> str:
    """The 10 most common error messages for one app between start and end (UTC)."""
    # The search, as one block of text. For each different message we keep:
    #   Count            -> how many times it happened
    #   FirstSeen        -> the first time it happened
    #   LastSeen         -> the last time it happened
    #   ExampleRequestId -> one request that hit it, so the AI can trace that request next
    return f"""
AppLogs_CL
| where TimeGenerated between ({kql_time(start)} .. {kql_time(end)})
| where Service == "{service}" and Level == "ERROR"
| summarize Count = count(), FirstSeen = min(TimeGenerated), LastSeen = max(TimeGenerated),
            ExampleRequestId = take_any(RequestId) by Message
| top 10 by Count
"""


# Search 3: every line of one request, across all apps, in time order.
def request_trace(request_id: str) -> str:
    """All log lines for one request ID, oldest first."""
    # The search, as one block of text.
    # project = keep only these columns (the request ID and endpoint are the same on every line).
    return f"""
AppLogs_CL
| where RequestId == "{request_id}"
| project TimeGenerated, Service, Level, Message, StatusCode, DurationMs
| order by TimeGenerated asc
"""


# Search 4: every release in a time range.
def deployments_between(start: datetime, end: datetime) -> str:
    """Every deployment between start and end (UTC), oldest first."""
    # The search, as one block of text.
    return f"""
Deployments_CL
| where TimeGenerated between ({kql_time(start)} .. {kql_time(end)})
| project TimeGenerated, Service, Version, DeployedBy, ChangeNote
| order by TimeGenerated asc
"""