"""
tests/test_query_safety.py
==========================

WHAT THIS FILE IS FOR
---------------------
Checks the safety rules and the time handling in mcp_server/tools.py.
None of these tests need Azure: where a tool would search Log Analytics,
we swap in a fake Azure that just hands back made-up rows.

WHAT IT CHECKS
--------------
    1. Normal searches are allowed; management commands (".drop ...") are refused.
    2. Bad inputs (unknown app, badly shaped request ID, bad times) are refused
       BEFORE anything is sent to Azure.
    3. India time is changed to UTC correctly (02:00 IST = 20:30 UTC the day before).
    4. run_log_query never returns more than 500 rows, and says when it cut rows off.

HOW TO RUN
----------
From the project folder:

    uv run pytest tests/test_query_safety.py -v

EXPECTED OUTPUT
---------------
Every test shows PASSED.
"""

# datetime: a date plus a time. timezone: UTC.
from datetime import datetime, timezone

# SimpleNamespace: a quick way to make a small object with any fields we like (used for the fake Azure).
from types import SimpleNamespace

# pytest: the test runner. pytest.raises checks that an error happens.
import pytest

# LogsQueryStatus: the "it worked" / "it partly worked" labels Azure uses.
from azure.monitor.query import LogsQueryStatus

# ToolError: the error our tools raise for bad inputs.
from mcp.server.mcpserver.exceptions import ToolError

# Our settings, so we can fill in a pretend workspace ID.
from config import settings

# The tools we are testing.
from mcp_server import tools


# ---------------------------------------------------------------------------
# A fake Azure, so the tests run without an Azure account
# ---------------------------------------------------------------------------

# Pretends to be Azure's LogsQueryClient. It remembers the last search and returns made-up rows.
class FakeAzure:
    """Returns `row_count` made-up rows for any search."""

    # Called when we create the fake: how many rows it should hand back.
    def __init__(self, row_count: int):
        # Remember the number of rows to return.
        self.row_count = row_count
        # The last search we were asked to run (empty until the first search).
        self.last_query = ""

    # Same name and inputs as the real Azure method, so tools.py cannot tell the difference.
    def query_workspace(self, workspace_id, query, **options):
        # Remember the search, so a test can look at it.
        self.last_query = query
        # A pretend table with one column "n" and row_count rows (0, 1, 2, ...).
        table = SimpleNamespace(columns=["n"], rows=[[number] for number in range(self.row_count)])
        # A pretend "it worked" reply holding that table.
        return SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=[table])


# Swaps the real Azure for a fake one, for one test only. monkeypatch undoes the swap afterwards.
def use_fake_azure(monkeypatch, row_count: int) -> FakeAzure:
    """Make tools.py talk to a FakeAzure, and return that fake."""
    # Make the fake.
    fake = FakeAzure(row_count)
    # Every time tools.py asks for an Azure connection, give it the fake instead.
    monkeypatch.setattr(tools, "get_client", lambda: fake)
    # Fill in a pretend workspace ID, so the "is it empty?" check passes.
    monkeypatch.setattr(settings, "LOG_ANALYTICS_WORKSPACE_ID", "test-workspace")
    # Give the fake back, so the test can look at what it received.
    return fake


# ---------------------------------------------------------------------------
# 1. Which searches are allowed
# ---------------------------------------------------------------------------

# A normal search is allowed, and a ";" at the end is removed.
def test_normal_search_is_allowed():
    # Check a normal search with a trailing ";".
    cleaned = tools.check_query("AppLogs_CL | where Level == 'ERROR' | count;")
    # It comes back without the ";".
    assert cleaned == "AppLogs_CL | where Level == 'ERROR' | count"


# Every one of these must be refused. pytest runs the test once for each.
@pytest.mark.parametrize("bad_search", [
    # A management command on its own: it would delete a table.
    ".drop table AppLogs_CL",
    # A management command hidden after a normal search.
    "AppLogs_CL | take 5; .drop table AppLogs_CL",
    # Nothing at all.
    "   ",
])
def test_unsafe_or_empty_searches_are_refused(bad_search):
    # check_query must raise a ToolError for each of them.
    with pytest.raises(ToolError):
        # Try the bad search.
        tools.check_query(bad_search)


# ---------------------------------------------------------------------------
# 2. Bad inputs are refused before anything is sent to Azure
# ---------------------------------------------------------------------------

# A request ID with extra text in it is refused, and Azure is never called.
def test_bad_request_id_is_refused(monkeypatch):
    # Use the fake Azure, so we can check it was never called.
    fake = use_fake_azure(monkeypatch, row_count=0)
    # A request ID with extra text after it.
    with pytest.raises(ToolError):
        # Try the bad ID.
        tools.trace_request('req-8f3a21" or 1==1')
    # The fake never received a search.
    assert fake.last_query == ""


# An app name that is not one of our 4 is refused.
def test_unknown_service_is_refused():
    # "payroll" is not one of our apps.
    with pytest.raises(ToolError):
        # Try it.
        tools.get_errors("payroll", "2026-10-29 02:00", "2026-10-29 03:00")


# An end time before the start time is refused.
def test_end_before_start_is_refused():
    # 03:00 to 02:00 goes backwards.
    with pytest.raises(ToolError):
        # Try it.
        tools.what_changed("2026-10-29 03:00", "2026-10-29 02:00")


# ---------------------------------------------------------------------------
# 3. India time is changed to UTC correctly
# ---------------------------------------------------------------------------

# 02:00 in India is 20:30 UTC on the day before (India is 5 hours 30 minutes ahead).
def test_local_time_becomes_utc():
    # Change the India time to UTC.
    utc = tools.local_text_to_utc("2026-10-29 02:00")
    # Check the result.
    assert utc == datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc)


# The search sent to Azure uses the UTC time, not the India time.
def test_search_uses_utc_times(monkeypatch):
    # Use the fake Azure.
    fake = use_fake_azure(monkeypatch, row_count=0)
    # Ask for payments errors from 02:00 to 03:00 India time.
    tools.get_errors("payments", "2026-10-29 02:00", "2026-10-29 03:00")
    # The search must say 20:30 to 21:30 UTC on the 28th.
    assert "datetime(2026-10-28T20:30:00Z) .. datetime(2026-10-28T21:30:00Z)" in fake.last_query


# ---------------------------------------------------------------------------
# 4. The 500-row limit
# ---------------------------------------------------------------------------

# When there are more than 500 rows, only 500 come back, marked as cut off.
def test_results_are_cut_at_500_rows(monkeypatch):
    # The fake hands back 501 rows (what Azure returns when there are more than 500).
    fake = use_fake_azure(monkeypatch, row_count=501)
    # Run a search that matches every line.
    result = tools.run_log_query("AppLogs_CL")
    # Our tool asked Azure for at most 501 rows.
    assert fake.last_query.endswith("| take 501")
    # Only 500 rows come back ...
    assert len(result["rows"]) == 500
    # ... and the result says rows were cut off.
    assert result["truncated"] is True