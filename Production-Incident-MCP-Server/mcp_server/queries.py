"""
mcp_server/queries.py
=====================

WHAT THIS FILE IS FOR
---------------------
Holds the ready-made Log Analytics searches (queries) behind the log tools.
(run_log_query runs the AI's own KQL instead, and search_postmortems uses
Azure AI Search, not KQL.) They are written in KQL, the search language of
Log Analytics. Each function below
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

# A spike always needs at least this many errors in one 10-minute bucket, however quiet the app is.
SPIKE_MIN_ERRORS = 5

# A spike also needs this many times the app's TYPICAL errors per bucket. So 3 means "3 times its usual".
SPIKE_MULTIPLIER = 3


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


# Search 1b: the 10-minute buckets where an app had unusually many errors FOR THAT APP.
def error_spikes(hours: int) -> str:
    """Every 10-minute bucket where one app had far more errors than its own usual."""
    # The search, as one block of text. It works in 4 steps:
    #
    #   1. make-series ... step 10m   -> count each app's errors in every 10-minute bucket.
    #                                    "default=0" fills the quiet buckets with 0, so
    #                                    buckets with no errors still count as normal.
    #   2. mv-expand                  -> turn each app's list of buckets into one row per bucket.
    #   3. percentile(Errors, 50)     -> the app's TYPICAL bucket: the middle value (the median).
    #                                    A few bad buckets cannot drag the median up, so it
    #                                    stays a fair picture of a normal 10 minutes.
    #   4. Threshold                  -> a bucket is a spike if it has at least SPIKE_MULTIPLIER
    #                                    times the typical count, and never fewer than
    #                                    SPIKE_MIN_ERRORS errors. So a busy app needs more errors
    #                                    to count as a spike than a quiet app does.
    return f"""
let buckets = AppLogs_CL
    | where TimeGenerated > ago({hours}h)
    | where Level == "ERROR"
    | make-series Errors = count() default=0 on TimeGenerated from ago({hours}h) to now() step {BUCKET} by Service
    | mv-expand BucketStart = TimeGenerated to typeof(datetime), Errors to typeof(long);
let usual = buckets
    | summarize Typical = percentile(Errors, 50) by Service;
buckets
| join kind=inner usual on Service
| extend Threshold = max_of({SPIKE_MIN_ERRORS}, toint(ceiling({SPIKE_MULTIPLIER} * Typical)))
| where Errors >= Threshold
| project BucketStart, Service, Errors, Typical, Threshold
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

# Search 5: the slowest kinds of request (endpoints) in a time range.
def slow_endpoints(start: datetime, end: datetime) -> str:
    """The 10 endpoints with the slowest p95 time between start and end (UTC)."""
    # The search, as one block of text.
    # Why only api-gateway's lines? api-gateway writes ONE line per request, at the very end,
    # with the TOTAL time the request took across every app. So each request is counted once,
    # with its full time. (The other apps' lines only hold their own part of the time.)
    #   P95Ms   -> 95 out of 100 requests were faster than this. It shows the slow
    #              requests customers notice, which an average hides.
    #   AvgMs   -> the average time, for comparison.
    #   Errors5xx -> how many of those requests failed on the server side.
    return f"""
AppLogs_CL
| where TimeGenerated between ({kql_time(start)} .. {kql_time(end)})
| where Service == "api-gateway"
| summarize Requests = count(), AvgMs = round(avg(DurationMs)), P95Ms = round(percentile(DurationMs, 95)),
            Errors5xx = countif(StatusCode >= 500) by Endpoint
| top 10 by P95Ms
"""


# Search 6: one app's error rate and p95 time, before and after one moment.
def before_and_after(service: str, start: datetime, change: datetime, end: datetime) -> str:
    """Error rate and p95 time for one app, from start to change ("before") and change to end ("after")."""
    # The search, as one block of text.
    #   iff(...)      -> label each line "before" or "after", depending on which side of the moment it is.
    #   countif(...)  -> count only the error lines.
    #   ErrorRatePct  -> errors as a percentage of all lines, so busy and quiet periods can be compared.
    return f"""
AppLogs_CL
| where TimeGenerated >= {kql_time(start)} and TimeGenerated < {kql_time(end)}
| where Service == "{service}"
| extend Period = iff(TimeGenerated < {kql_time(change)}, "before", "after")
| summarize Lines = count(), Errors = countif(Level == "ERROR"), P95Ms = round(percentile(DurationMs, 95)) by Period
| extend ErrorRatePct = round(100.0 * Errors / Lines, 1)
"""


# Search 7: for every failed request, the FIRST app that failed.
def root_services(start: datetime, end: datetime) -> str:
    """For failed requests between start and end (UTC): which app failed first, and how often."""
    # The search, as one block of text. It works in 2 steps:
    #
    #   1. arg_min(TimeGenerated, ...) by RequestId -> for each failed request, keep only its
    #      EARLIEST error line. That line's app is where the failure STARTED.
    #      (api-gateway always writes its error last, after the app that really failed.)
    #   2. summarize ... by RootService -> count the failed requests per starting app, with
    #      one example request and one example message.
    return f"""
AppLogs_CL
| where TimeGenerated between ({kql_time(start)} .. {kql_time(end)})
| where Level == "ERROR"
| summarize arg_min(TimeGenerated, Service, Message) by RequestId
| summarize FailedRequests = count(), ExampleRequestId = take_any(RequestId),
            ExampleMessage = take_any(Message) by RootService = Service
| order by FailedRequests desc
"""