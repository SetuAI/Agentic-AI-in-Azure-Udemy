"""
mcp_server/tools.py
===================

WHAT THIS FILE IS FOR
---------------------
The tools our MCP server offers. An AI app (Claude, Claude Code, VS Code
Copilot) reads each tool's name and description, decides which ones to call
for your question, and calls them. Each tool searches the logs in Azure Log
Analytics and returns what it found.

    get_error_summary   how many errors each app had, and when they jumped
    get_errors          the most common error messages of one app in a time range
    trace_request       every step of one request, across all apps
    what_changed        every app release in a time range
    get_slow_endpoints  the slowest kinds of request in a time range
    compare_periods     one app's error rate and speed, before vs after a moment
    find_root_service   for failed requests, which app failed FIRST
    search_postmortems  past incident reports: has this happened before, and what fixed it?
    run_log_query       any other search, written by the AI itself (with safety checks)

WHO READS WHAT
--------------
    The text in triple quotes under each tool (its "docstring") -> read by the AI.
        It tells the AI what the tool does and when to use it.
    The lines starting with "#"                          -> read by you.
        They explain the code. The AI never sees them.

TIMES
-----
Azure stores every time in UTC. People in India think in IST (UTC + 5:30).
So the tools take times in IST from the AI, change them to UTC before
searching, and change the results back to IST before returning them.

SAFETY
------
    - Every value the AI gives is checked before it goes into a search.
    - run_log_query refuses "management commands" (KQL lines starting with
      a dot, which manage Azure instead of reading logs).
    - Every result is cut off at 500 rows, and every search is stopped
      after 30 seconds.
    - Every log text is checked by Azure AI Content Safety Prompt Shields
      (shields.py) before the AI sees it. Text that hides instructions for
      the AI is replaced. If the check can't run, the text is hidden.
    - Our Azure identity only has the "Log Analytics Reader" role, so even
      if a check were missed, it could never change or delete a log.

HOW TO RUN
----------
You never run this file directly. server.py imports it and offers these
tools to AI apps. The tests in tests/ also import it.

EXPECTED OUTPUT
---------------
Each tool returns a dictionary, which the AI app receives as JSON.
"""

# re: checks that text has the right shape (for example, a request ID like req-8f3a21).
import re

# datetime: a date plus a time. timedelta: a length of time. timezone: UTC.
from datetime import datetime, timedelta, timezone

# lru_cache: remembers a function's result, so we connect to Azure only once.
from functools import lru_cache

# Annotated: lets us attach a description to each tool input, for the AI to read.
from typing import Annotated

# ZoneInfo: knows the rules of each time zone, such as India time.
from zoneinfo import ZoneInfo

# HttpResponseError: the error Azure gives back when it refuses a search (for example, a typo in it).
from azure.core.exceptions import HttpResponseError

# DefaultAzureCredential: signs in to Azure. On your laptop it uses the Entra ID
# app in .env. On App Service it uses the app's own "managed identity" (no password).
from azure.identity import DefaultAzureCredential

# LogsQueryClient: Azure's helper for searching Log Analytics.
# LogsQueryStatus: tells us if a search worked fully, or only partly.
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

# ToolError: the error to raise when a tool cannot do its job for a reason we expected
# (a bad input, a search Azure refused). Its message is passed on to the AI, so the AI
# can fix its input and try again. Any other kind of error shows the AI only
# "Error executing tool", and the details stay in our own log.
from mcp.server.mcpserver.exceptions import ToolError

# Field: holds the description of one tool input.
from pydantic import Field

# Our settings (the workspace ID and the time zone).
from config import settings

# Our logger, to print a line for every tool call.
from config.logging_config import get_logger

# The ready-made KQL searches.
from mcp_server import queries

# Prompt Shields: checks every log text before the AI reads it.
from mcp_server import shields

# Searches the past postmortems in Azure AI Search.
from mcp_server import postmortems

# A logger named after this file.
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Fixed rules
# ---------------------------------------------------------------------------

# The 4 apps whose logs we hold (the same 4 as in log_generator/services.py).
KNOWN_SERVICES = ["api-gateway", "loan-service", "payments", "notifications"]

# The most rows any tool returns. More than this would flood the AI with text.
MAX_ROWS = 500

# The most seconds a search may run before Azure stops it.
QUERY_TIMEOUT_SECONDS = 30

# The furthest back any search may look: 7 days. This keeps searches fast and cheap.
SEARCH_WINDOW = timedelta(days=7)

# The shape of a request ID: "req-" then exactly 6 characters from 0-9 and a-f.
REQUEST_ID_SHAPE = re.compile(r"^req-[0-9a-f]{6}$")

# The shortest and longest search the AI may send to search_postmortems, in characters.
QUERY_MIN_CHARS, QUERY_MAX_CHARS = 3, 300

# Postmortem sections are longer than log lines, so they are cut at this length instead.
POSTMORTEM_MAX_CHARS = 1200

# The time format the AI must use for inputs, for example "2026-10-29 02:00".
TIME_FORMAT = "%Y-%m-%d %H:%M"

# Our display time zone (India time unless .env says otherwise).
LOCAL_TZ = ZoneInfo(settings.DISPLAY_TIMEZONE)


# ---------------------------------------------------------------------------
# Helpers: connecting to Azure and running a search
# ---------------------------------------------------------------------------

# Connects to Log Analytics once. @lru_cache makes later calls reuse the same connection.
@lru_cache
def get_client() -> LogsQueryClient:
    """Return one shared connection to Log Analytics."""
    # Sign in to Azure, and connect to Log Analytics as that signed-in identity.
    return LogsQueryClient(DefaultAzureCredential())


# Turns any date and time in a result into easy-to-read India time text.
def to_local_text(value):
    """Return datetimes as 'YYYY-MM-DD HH:MM:SS' in local time; leave other values as they are."""
    # Only dates and times need changing; numbers and words stay the same.
    if isinstance(value, datetime):
        # Change the time zone to India time, then write it as text.
        return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    # Not a date: give it back unchanged.
    return value


# Sends one KQL search to Azure and returns the rows as a list of dictionaries.
def run_kql(kql: str) -> list[dict]:
    """Run a KQL search on the workspace and return its rows."""
    # The workspace to search. Stop with a clear message if it is missing from .env.
    workspace_id = settings.require("LOG_ANALYTICS_WORKSPACE_ID", settings.LOG_ANALYTICS_WORKSPACE_ID)
    # Try the search. If Azure refuses it, we jump to "except" below.
    try:
        # Send the search to Azure, looking back at most 7 days, and stop it after 30 seconds.
        response = get_client().query_workspace(workspace_id, kql, timespan=SEARCH_WINDOW,
                                                server_timeout=QUERY_TIMEOUT_SECONDS)
    # Azure refused the search (a typo, an unknown column, a timeout ...).
    except HttpResponseError as error:
        # Pass Azure's reason back to the AI, so it can fix its search and try again.
        raise ToolError(f"Log Analytics refused the search: {error.message}") from error

    # If only part of the search worked, Azure still gives us the part that did.
    if response.status == LogsQueryStatus.PARTIAL:
        # Write a warning in our own log, so we can see it happened.
        logger.warning("Partial result: %s", response.partial_error)
        # Use the part that worked.
        tables = response.partial_data
    # If it fully worked, use the full result.
    else:
        # The full result.
        tables = response.tables

    # No tables at all means no rows: return an empty list.
    if not tables:
        # Nothing found.
        return []
    # A search returns one table; take it.
    table = tables[0]
    # Turn every row into a dictionary {column name: value}, with dates in India time.
    rows = [{column: to_local_text(value) for column, value in zip(table.columns, row)}
            for row in table.rows]
    # Check every log text with Prompt Shields, and replace any that hides instructions for the AI.
    return shields.protect(rows)


# ---------------------------------------------------------------------------
# Helpers: checking the AI's inputs before they go into a search
# ---------------------------------------------------------------------------

# Checks that an app name is one of our 4 apps.
def check_service(service: str) -> str:
    """Return the app name if it is known; otherwise stop with a helpful message."""
    # If it is not in our list, it could be a typo, or an attempt to sneak extra text into the search.
    if service not in KNOWN_SERVICES:
        # Stop, and tell the AI which names are allowed.
        raise ToolError(f"Unknown service '{service}'. Use one of: {', '.join(KNOWN_SERVICES)}")
    # It is safe to use.
    return service


# Reads a local time like "2026-10-29 02:00" and changes it to UTC.
def local_text_to_utc(text: str) -> datetime:
    """Return the UTC datetime for a local time written as 'YYYY-MM-DD HH:MM'."""
    # Try to read the text as a date and time.
    try:
        # strptime reads text into a date and time, using our fixed format.
        moment = datetime.strptime(text.strip(), TIME_FORMAT)
    # The text was not in the right shape (strptime raises ValueError for that).
    except ValueError as error:
        # Stop, and show the AI the right shape.
        raise ToolError(f"Time '{text}' must look like 2026-10-29 02:00 (local time).") from error
    # Mark it as India time, then change it to UTC (what Azure uses).
    return moment.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)


# Checks a time range: both times readable, and the end after the start.
def check_range(start: str, end: str) -> tuple[datetime, datetime]:
    """Return the (start, end) range in UTC, after checking it makes sense."""
    # Read the start time.
    start_utc = local_text_to_utc(start)
    # Read the end time.
    end_utc = local_text_to_utc(end)
    # The end must come after the start.
    if end_utc <= start_utc:
        # Stop and say why.
        raise ToolError("The end time must be after the start time.")
    # Give back both times, in UTC.
    return start_utc, end_utc


# Checks a search written by the AI. Kept separate so the tests can check it without Azure.
def check_query(kql: str) -> str:
    """Return the cleaned-up search, or stop if it is not allowed."""
    # Remove spaces and new lines at both ends, and any ";" left at the very end.
    cleaned = kql.strip().rstrip(";").strip()
    # An empty search is useless.
    if not cleaned:
        # Stop and say why.
        raise ToolError("The search is empty.")
    # A KQL text can hold several statements separated by ";". Look at each one.
    for statement in cleaned.split(";"):
        # A statement starting with "." is a management command (it manages Azure, it does not read logs).
        if statement.strip().startswith("."):
            # Refuse it. This tool only reads.
            raise ToolError("Management commands (starting with '.') are not allowed. Only read logs.")
    # The search is allowed.
    return cleaned


# ---------------------------------------------------------------------------
# THE 5 TOOLS
# ---------------------------------------------------------------------------

# Tool 1: an overview of errors, to find WHERE and WHEN something went wrong.
def get_error_summary(
    # The input: how many hours back to look. The description is shown to the AI.
    hours: Annotated[int, Field(description="How many hours back to look (1 to 168).", ge=1, le=168)] = 24,
) -> dict:
    """Start here for any 'what went wrong?' question.

    Returns the number of errors each service had in the last `hours` hours,
    and every 10-minute window where one service had far more errors than
    its own usual (a 'spike'). Each spike shows Errors, the service's Typical
    errors per window, and the Threshold it crossed. Pick `hours` so it covers
    the time the user asks about (for example 48 for anything 'yesterday').
    Use the spikes to pick a service and time range for get_errors,
    find_root_service and what_changed. Times are local time (IST).
    """
    # Write a line in our own log, so we can see which tools the AI calls.
    logger.info("get_error_summary(hours=%s)", hours)
    # Run both ready-made searches and return them together.
    return {
        # Remind the AI which time zone the times are in.
        "time_zone": settings.DISPLAY_TIMEZONE,
        # The time right now, so the AI can work out what "last night" or "yesterday" means.
        "now": datetime.now(LOCAL_TZ).strftime(TIME_FORMAT),
        # How far back we looked.
        "looked_back_hours": hours,
        # How we decide what a spike is, in words.
        "spike_rule": (f"a {queries.BUCKET} window with at least {queries.SPIKE_MULTIPLIER} times the "
                       f"service's typical errors per window, and never fewer than {queries.SPIKE_MIN_ERRORS}"),
        # Search 1a: errors per app.
        "errors_per_service": run_kql(queries.errors_per_service(hours)),
        # Search 1b: the spikes, oldest first.
        "spikes": run_kql(queries.error_spikes(hours))[:MAX_ROWS],
    }


# Tool 2: the actual error messages, to find WHAT went wrong.
def get_errors(
    # The input: which app.
    service: Annotated[str, Field(description=f"One of: {', '.join(KNOWN_SERVICES)}")],
    # The input: from when (local time).
    start: Annotated[str, Field(description="Start time, local time, like '2026-10-29 02:00'")],
    # The input: until when (local time).
    end: Annotated[str, Field(description="End time, local time, like '2026-10-29 03:00'")],
) -> dict:
    """The 10 most common error messages of one service between two times.

    Each message comes with how often it happened, when it was first and
    last seen, and one example request ID (pass it to trace_request to see
    that request's full path). Times are local time (IST).
    """
    # Write a line in our own log.
    logger.info("get_errors(service=%s, start=%s, end=%s)", service, start, end)
    # Check the app name and the time range before using them.
    service = check_service(service)
    # Change the times to UTC (and check them).
    start_utc, end_utc = check_range(start, end)
    # Run the ready-made search and return the result.
    return {
        # Echo the inputs back, so the AI's answer can quote them.
        "service": service, "from": start, "to": end, "time_zone": settings.DISPLAY_TIMEZONE,
        # The error messages, most common first.
        "errors": run_kql(queries.top_errors(service, start_utc, end_utc)),
    }


# Tool 3: one request's full journey, to find WHERE a single request failed.
def trace_request(
    # The input: the request ID.
    request_id: Annotated[str, Field(description="A request ID, like 'req-8f3a21'")],
) -> dict:
    """Every log line of one request, across all services, oldest first.

    Shows which services the request passed through, how long each took,
    and the first one that failed. Times are local time (IST).
    """
    # Write a line in our own log.
    logger.info("trace_request(request_id=%s)", request_id)
    # Clean up the ID: remove spaces and use small letters.
    request_id = request_id.strip().lower()
    # Check it has the right shape, so nothing extra can sneak into the search.
    if not REQUEST_ID_SHAPE.match(request_id):
        # Stop, and show the AI the right shape.
        raise ToolError("A request ID looks like 'req-' followed by 6 characters from 0-9 and a-f.")
    # Run the ready-made search.
    lines = run_kql(queries.request_trace(request_id))
    # Return what we found.
    return {
        # Echo the ID back.
        "request_id": request_id,
        # Whether any line was found at all (logs are kept for a limited time).
        "found": bool(lines),
        # Remind the AI which time zone the times are in.
        "time_zone": settings.DISPLAY_TIMEZONE,
        # Every line of the request, oldest first.
        "lines": lines,
    }


# Tool 4: releases, to find WHY something started going wrong.
def what_changed(
    # The input: from when (local time).
    start: Annotated[str, Field(description="Start time, local time, like '2026-10-29 01:00'")],
    # The input: until when (local time).
    end: Annotated[str, Field(description="End time, local time, like '2026-10-29 03:00'")],
) -> dict:
    """Every service deployment (new version released) between two times.

    Use it to check whether a release happened just before errors started.
    An empty list means nothing was released, so the cause is probably
    somewhere else (for example an outside company). Times are local time (IST).
    """
    # Write a line in our own log.
    logger.info("what_changed(start=%s, end=%s)", start, end)
    # Change the times to UTC (and check them).
    start_utc, end_utc = check_range(start, end)
    # Run the ready-made search and return the result.
    return {
        # Echo the inputs back.
        "from": start, "to": end, "time_zone": settings.DISPLAY_TIMEZONE,
        # The releases, oldest first.
        "deployments": run_kql(queries.deployments_between(start_utc, end_utc)),
    }


# Tool 5: the slowest kinds of request, to find WHERE customers wait the longest.
def get_slow_endpoints(
    # The input: from when (local time).
    start: Annotated[str, Field(description="Start time, local time, like '2026-10-28 00:00'")],
    # The input: until when (local time).
    end: Annotated[str, Field(description="End time, local time, like '2026-10-29 00:00'")],
) -> dict:
    """The 10 slowest endpoints (kinds of request) between two times.

    For each endpoint: how many requests, the average time, the p95 time
    (95 out of 100 requests were faster than this), and how many failed with
    a 5xx error. Times are the TOTAL time of each request, in milliseconds.
    Use it for 'which endpoint was slowest?' questions. Times are local time (IST).
    """
    # Write a line in our own log.
    logger.info("get_slow_endpoints(start=%s, end=%s)", start, end)
    # Change the times to UTC (and check them).
    start_utc, end_utc = check_range(start, end)
    # Run the ready-made search and return the result.
    return {
        # Echo the inputs back, so the AI's answer can quote them.
        "from": start, "to": end, "time_zone": settings.DISPLAY_TIMEZONE,
        # The endpoints, slowest p95 first.
        "endpoints": run_kql(queries.slow_endpoints(start_utc, end_utc)),
    }


# Tool 6: before vs after one moment, to check whether a change made things better or worse.
def compare_periods(
    # The input: which app.
    service: Annotated[str, Field(description=f"One of: {', '.join(KNOWN_SERVICES)}")],
    # The input: the moment to compare around, for example a release or a rollback.
    change_time: Annotated[str, Field(description="The moment to compare around, local time, like '2026-10-29 02:41'")],
    # The input: how many minutes to look at on each side of the moment.
    minutes: Annotated[int, Field(description="Minutes to compare on each side (5 to 360).", ge=5, le=360)] = 30,
) -> dict:
    """One service's error rate and p95 time, just before vs just after a moment.

    Compares the `minutes` before `change_time` with the `minutes` after it.
    Use it to check whether a release made things worse, or whether a
    rollback fixed them (for example, change_time = the rollback time from
    what_changed). Times are local time (IST).
    """
    # Write a line in our own log.
    logger.info("compare_periods(service=%s, change_time=%s, minutes=%s)", service, change_time, minutes)
    # Check the app name before using it.
    service = check_service(service)
    # Change the moment to UTC (and check it).
    change_utc = local_text_to_utc(change_time)
    # The length of each side, as a length of time.
    window = timedelta(minutes=minutes)
    # Run the ready-made search: from (moment - window) to (moment + window).
    rows = run_kql(queries.before_and_after(service, change_utc - window, change_utc, change_utc + window))
    # Put each side's row under its name, "before" or "after". A side with no lines stays empty.
    sides = {row["Period"]: row for row in rows}
    # Each side's numbers, or zeros if that side had no lines at all.
    before, after = (sides.get(name, {"Lines": 0, "Errors": 0, "ErrorRatePct": 0.0, "P95Ms": None})
                     for name in ("before", "after"))
    # Return both sides, plus the change in error rate, ready to quote.
    return {
        # Echo the inputs back.
        "service": service, "change_time": change_time, "minutes_each_side": minutes,
        # Remind the AI which time zone the times are in.
        "time_zone": settings.DISPLAY_TIMEZONE,
        # The numbers before the moment.
        "before": before,
        # The numbers after the moment.
        "after": after,
        # How the error rate moved, in percentage points (negative = fewer errors after).
        "error_rate_change_points": round(after["ErrorRatePct"] - before["ErrorRatePct"], 1),
    }


# Tool 7: which app failed first, so the app that only REPORTS failures is not blamed.
def find_root_service(
    # The input: from when (local time).
    start: Annotated[str, Field(description="Start time, local time, like '2026-10-29 02:00'")],
    # The input: until when (local time).
    end: Annotated[str, Field(description="End time, local time, like '2026-10-29 03:00'")],
) -> dict:
    """For every request that failed between two times, the app that failed FIRST.

    api-gateway reports every failure that happens further down, so its error
    counts always look high. This tool finds where each failure STARTED, and
    counts failed requests per starting app, with an example request ID and
    message. Use it before blaming an app. Times are local time (IST).
    """
    # Write a line in our own log.
    logger.info("find_root_service(start=%s, end=%s)", start, end)
    # Change the times to UTC (and check them).
    start_utc, end_utc = check_range(start, end)
    # Run the ready-made search and return the result.
    return {
        # Echo the inputs back.
        "from": start, "to": end, "time_zone": settings.DISPLAY_TIMEZONE,
        # One row per app where failures started, the most failed requests first.
        "root_services": run_kql(queries.root_services(start_utc, end_utc)),
    }


# Tool 8: past incident reports, to find whether this happened before and what fixed it.
def search_postmortems(
    # The input: what to look for, in plain words.
    query: Annotated[str, Field(description="What to look for, in plain words, like 'payments database connection pool timeouts'")],
    # The input: only postmortems that involve this app (optional).
    service: Annotated[str | None, Field(description=f"Optional. Only postmortems involving one of: {', '.join(KNOWN_SERVICES)}")] = None,
) -> dict:
    """Search past incident reports (postmortems): 'has this happened before, and what fixed it?'

    Returns up to 3 postmortems, best match first, each with its date, the
    services involved, the root cause, what fixed it, and the lessons
    learned. Use it AFTER the log tools have shown what broke, to describe
    the problem in your own words, for example 'payments ran out of database
    connections after a release'. Postmortems are older than the logs.
    """
    # Write a line in our own log.
    logger.info("search_postmortems(query=%r, service=%s)", query, service)
    # Remove spaces at both ends.
    query = query.strip()
    # The search must be a sensible length.
    if not QUERY_MIN_CHARS <= len(query) <= QUERY_MAX_CHARS:
        # Stop, and say what is allowed.
        raise ToolError(f"The search must be {QUERY_MIN_CHARS} to {QUERY_MAX_CHARS} characters long.")
    # If an app was given, it must be one of our 4 (it goes into the search filter).
    if service:
        # Check it (this stops with a message if it is unknown).
        service = check_service(service)
    # Try the search. If Azure AI Search refuses it, we jump to "except" below.
    try:
        # Search the index.
        found = postmortems.search(query, service)
    # Azure refused (wrong role, index missing, ...).
    except HttpResponseError as error:
        # Pass the reason back to the AI.
        raise ToolError(f"Azure AI Search refused the search: {error.message}") from error
    # Return what we found, checked by Prompt Shields like every other text.
    return {
        # Echo the inputs back.
        "query": query, "service": service,
        # How many postmortems matched.
        "found": len(found),
        # The postmortems, best match first.
        "postmortems": shields.protect(found, max_chars=POSTMORTEM_MAX_CHARS),
    }


# Tool 9: any other search, written by the AI. Powerful, so it has the most checks.
def run_log_query(
    # The input: the KQL search the AI wrote.
    kql: Annotated[str, Field(description="A read-only KQL query over AppLogs_CL or Deployments_CL")],
) -> dict:
    """Run your own read-only KQL query, ONLY for questions no other tool covers.

    Try get_slow_endpoints, compare_periods and find_root_service first.

    Tables:
      AppLogs_CL: TimeGenerated, Service, Level (INFO/WARN/ERROR), Message,
                  RequestId, Endpoint, StatusCode, DurationMs
      Deployments_CL: TimeGenerated, Service, Version, DeployedBy, ChangeNote
    TimeGenerated is stored in UTC: write any datetime(...) values in UTC.
    Results are shown in local time (IST), limited to 500 rows, and the
    query is stopped after 30 seconds. Management commands are refused.
    """
    # Write a line in our own log, with the full search, so we can review what the AI ran.
    logger.info("run_log_query(kql=%r)", kql)
    # Check the search is allowed (this stops with a message if not).
    cleaned = check_query(kql)
    # Ask Azure for one row more than the limit, so we can tell if there were more.
    limited = f"{cleaned}\n| take {MAX_ROWS + 1}"
    # Run the search.
    rows = run_kql(limited)
    # Return the result.
    return {
        # Remind the AI which time zone the times are in.
        "time_zone": settings.DISPLAY_TIMEZONE,
        # True if there were more rows than we are returning.
        "truncated": len(rows) > MAX_ROWS,
        # At most 500 rows.
        "rows": rows[:MAX_ROWS],
    }


# All the tools in one list, so server.py can add them in a loop.
ALL_TOOLS = [get_error_summary, get_errors, trace_request, what_changed,
             get_slow_endpoints, compare_periods, find_root_service, search_postmortems, run_log_query]