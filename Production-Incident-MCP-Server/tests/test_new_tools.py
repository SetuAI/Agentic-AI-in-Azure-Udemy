"""
tests/test_new_tools.py
=======================

WHAT THIS FILE IS FOR
---------------------
Checks the 3 newer tools (get_slow_endpoints, compare_periods,
find_root_service) and the smarter spike rule in get_error_summary.
None of these tests need Azure.

WHAT IT CHECKS
--------------
    1. The tools send the right search to Azure (with UTC times), and
       refuse bad inputs before anything is sent.
    2. compare_periods puts the "before" and "after" numbers side by side,
       and works out how much the error rate moved.
    3. On the generated logs:
         - the smarter spike rule still finds both incidents, and finds
           nothing in loan-service, which has only everyday noise
         - the first app to fail in the bad-deployment window is payments,
           never api-gateway
         - the error rate of payments drops sharply after the rollback

    The checks in part 3 redo the searches' logic in plain Python on the
    generated files. They prove the DATA supports the demo answers.

HOW TO RUN
----------
From the project folder:

    uv run pytest tests/test_new_tools.py -v

EXPECTED OUTPUT
---------------
Every test shows PASSED.
"""

# json: reads the generated JSON files.
import json

# math.ceil: rounds a number UP to a whole number (2.1 becomes 3), the same as KQL's ceiling().
import math

# statistics.median: the middle value of a list of numbers.
from statistics import median

# defaultdict: a dictionary that starts every new key at a default value (here, 0).
from collections import defaultdict

# datetime: a date plus a time. timedelta: a length of time. timezone: UTC.
from datetime import datetime, timedelta, timezone

# SimpleNamespace: a quick way to make a small object with any fields we like (used for the fake Azure).
from types import SimpleNamespace

# pytest: the test runner. pytest.raises checks that an error happens.
import pytest

# LogsQueryStatus: the "it worked" label Azure uses.
from azure.monitor.query import LogsQueryStatus

# ToolError: the error our tools raise for bad inputs.
from mcp.server.mcpserver.exceptions import ToolError

# Our settings, so we can fill in a pretend workspace ID.
from config import settings

# The generator and the incidents, for the checks on generated data.
from log_generator import generate, incidents

# The search rules (bucket size, spike rule) and the tools we are testing.
from mcp_server import queries, tools


# ---------------------------------------------------------------------------
# A fake Azure that hands back the rows a test gives it
# ---------------------------------------------------------------------------

# Pretends to be Azure's LogsQueryClient. It remembers the last search and returns the given rows.
class FakeAzure:
    """Returns the given columns and rows for any search."""

    # Called when we create the fake: the columns and rows it should hand back.
    def __init__(self, columns: list, rows: list):
        # Remember the columns.
        self.columns = columns
        # Remember the rows.
        self.rows = rows
        # The last search we were asked to run (empty until the first search).
        self.last_query = ""

    # Same name and inputs as the real Azure method, so tools.py cannot tell the difference.
    def query_workspace(self, workspace_id, query, **options):
        # Remember the search, so a test can look at it.
        self.last_query = query
        # A pretend table holding the given columns and rows.
        table = SimpleNamespace(columns=self.columns, rows=self.rows)
        # A pretend "it worked" reply holding that table.
        return SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=[table])


# Swaps the real Azure for a fake one, for one test only.
def use_fake_azure(monkeypatch, columns=("n",), rows=()) -> FakeAzure:
    """Make tools.py talk to a FakeAzure, and return that fake."""
    # Make the fake.
    fake = FakeAzure(list(columns), list(rows))
    # Every time tools.py asks for an Azure connection, give it the fake instead.
    monkeypatch.setattr(tools, "get_client", lambda: fake)
    # Fill in a pretend workspace ID, so the "is it empty?" check passes.
    monkeypatch.setattr(settings, "LOG_ANALYTICS_WORKSPACE_ID", "test-workspace")
    # Give the fake back, so the test can look at what it received.
    return fake


# ---------------------------------------------------------------------------
# 1. The tools send the right searches
# ---------------------------------------------------------------------------

# get_slow_endpoints counts each request once (api-gateway's line) and uses UTC times.
def test_slow_endpoints_search(monkeypatch):
    # Use the fake Azure.
    fake = use_fake_azure(monkeypatch)
    # Ask for the slowest endpoints from 02:00 to 03:00 India time.
    tools.get_slow_endpoints("2026-10-29 02:00", "2026-10-29 03:00")
    # The search reads only api-gateway's lines, which hold each request's total time.
    assert 'Service == "api-gateway"' in fake.last_query
    # It ranks endpoints by their p95 time.
    assert "percentile(DurationMs, 95)" in fake.last_query
    # The times are in UTC: 02:00 IST is 20:30 UTC the day before.
    assert "datetime(2026-10-28T20:30:00Z) .. datetime(2026-10-28T21:30:00Z)" in fake.last_query


# find_root_service keeps only each request's earliest error.
def test_root_service_search(monkeypatch):
    # Use the fake Azure.
    fake = use_fake_azure(monkeypatch)
    # Ask for the root services from 02:00 to 03:00 India time.
    tools.find_root_service("2026-10-29 02:00", "2026-10-29 03:00")
    # arg_min(TimeGenerated, ...) by RequestId = the earliest error line of each request.
    assert "arg_min(TimeGenerated, Service, Message) by RequestId" in fake.last_query


# compare_periods searches from (moment - minutes) to (moment + minutes), split at the moment.
def test_compare_periods_window(monkeypatch):
    # Use the fake Azure.
    fake = use_fake_azure(monkeypatch)
    # Compare 30 minutes on each side of 02:41 India time (20:11 UTC the day before).
    tools.compare_periods("payments", "2026-10-29 02:41", 30)
    # The search starts 30 minutes before the moment: 20:41 UTC.
    assert "TimeGenerated >= datetime(2026-10-28T20:41:00Z)" in fake.last_query
    # It ends 30 minutes after the moment: 21:41 UTC.
    assert "TimeGenerated < datetime(2026-10-28T21:41:00Z)" in fake.last_query
    # Lines before the moment are labelled "before".
    assert 'iff(TimeGenerated < datetime(2026-10-28T21:11:00Z), "before", "after")' in fake.last_query


# compare_periods refuses an unknown app before anything is sent to Azure.
def test_compare_periods_refuses_unknown_service(monkeypatch):
    # Use the fake Azure, so we can check it was never called.
    fake = use_fake_azure(monkeypatch)
    # "payroll" is not one of our apps.
    with pytest.raises(ToolError):
        # Try it.
        tools.compare_periods("payroll", "2026-10-29 02:41")
    # The fake never received a search.
    assert fake.last_query == ""


# ---------------------------------------------------------------------------
# 2. compare_periods puts the two sides together
# ---------------------------------------------------------------------------

# The "before" and "after" rows come back under their names, with the change worked out.
def test_compare_periods_result(monkeypatch):
    # The fake hands back one row per side, the way the real search does.
    use_fake_azure(monkeypatch,
                   columns=["Period", "Lines", "Errors", "P95Ms", "ErrorRatePct"],
                   rows=[["before", 600, 540, 30000, 90.0], ["after", 300, 3, 450, 1.0]])
    # Compare around the rollback.
    result = tools.compare_periods("payments", "2026-10-29 02:41", 30)
    # The "before" side is the bad one.
    assert result["before"]["ErrorRatePct"] == 90.0
    # The "after" side is the healthy one.
    assert result["after"]["ErrorRatePct"] == 1.0
    # The error rate fell by 89 percentage points.
    assert result["error_rate_change_points"] == -89.0


# A side with no lines at all comes back as zeros, not as an error.
def test_compare_periods_empty_side(monkeypatch):
    # The fake hands back only the "before" row.
    use_fake_azure(monkeypatch,
                   columns=["Period", "Lines", "Errors", "P95Ms", "ErrorRatePct"],
                   rows=[["before", 10, 0, 300, 0.0]])
    # Compare around a moment with nothing after it.
    result = tools.compare_periods("payments", "2026-10-29 02:41", 30)
    # The empty side has 0 lines.
    assert result["after"]["Lines"] == 0


# ---------------------------------------------------------------------------
# 3. The generated logs support the demo answers
# ---------------------------------------------------------------------------

# A "fixture": pytest runs this once, and hands its result to every test that asks for "generated".
@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """Run the generator into a temporary folder; return (log lines, today)."""
    # A fresh temporary folder that pytest deletes afterwards.
    folder = tmp_path_factory.mktemp("data")
    # Point the generator at that folder instead of data/.
    generate.DATA_DIR = folder
    # Run the generator.
    generate.main()
    # Read the log lines back.
    lines = json.loads((folder / "app_logs.json").read_text(encoding="utf-8"))
    # Work out "today" the same way the generator did.
    today = generate.find_today(datetime.now(generate.LOCAL_TZ))
    # Hand both to the tests.
    return lines, today


# Reads the TimeGenerated text of a row back into a date and time.
def time_of(row: dict) -> datetime:
    """Return the row's TimeGenerated as a datetime (UTC)."""
    # fromisoformat reads text like 2026-10-28T20:40:07.123Z ("Z" means UTC).
    return datetime.fromisoformat(row["TimeGenerated"].replace("Z", "+00:00"))


# The size of one bucket, as a length of time (the same 10 minutes the search uses).
BUCKET = timedelta(minutes=int(queries.BUCKET.rstrip("m")))


# Redoes the spike search in Python: which (app, bucket start) pairs are spikes.
def find_spikes(lines: list) -> set:
    """Return the set of (service, bucket_start) pairs that the spike rule calls spikes."""
    # The error lines only.
    errors = [row for row in lines if row["Level"] == "ERROR"]
    # The first and last moment in the logs, to lay out every bucket (empty ones too).
    first, last = min(map(time_of, lines)), max(map(time_of, lines))
    # How many buckets the logs cover.
    bucket_count = int((last - first) / BUCKET) + 1
    # Count errors per app per bucket number.
    counts = defaultdict(lambda: [0] * bucket_count)
    # Go through every error line.
    for row in errors:
        # Which bucket it falls in.
        index = int((time_of(row) - first) / BUCKET)
        # Add one error to that app's bucket.
        counts[row["Service"]][index] += 1
    # The pairs we call spikes.
    spikes = set()
    # Go through each app's buckets.
    for service, buckets in counts.items():
        # The app's typical bucket: the median, with the empty buckets counted as 0.
        typical = median(buckets)
        # The rule: at least SPIKE_MULTIPLIER times typical, and never fewer than SPIKE_MIN_ERRORS.
        threshold = max(queries.SPIKE_MIN_ERRORS, math.ceil(queries.SPIKE_MULTIPLIER * typical))
        # Keep every bucket at or over the threshold.
        spikes |= {(service, first + index * BUCKET) for index, count in enumerate(buckets) if count >= threshold}
    # Give back the spikes.
    return spikes


# The smarter spike rule finds both incidents.
def test_spike_rule_finds_both_incidents(generated):
    # The generated lines and "today".
    lines, today = generated
    # The spikes the rule finds.
    spikes = find_spikes(lines)
    # Go through the two incidents that make many errors.
    for incident in incidents.INCIDENTS:
        # The incident's start and end, in UTC.
        starts = generate.to_time(today, incident["starts"]).astimezone(timezone.utc)
        ends = generate.to_time(today, incident["ends"]).astimezone(timezone.utc)
        # The spikes of this incident's app inside its window.
        inside = [moment for service, moment in spikes
                  if service == incident["service"] and starts - BUCKET <= moment < ends]
        # There must be at least one.
        assert inside, f"{incident['name']}: no spike found"


# The smarter spike rule finds nothing in loan-service, which has only everyday noise.
def test_spike_rule_ignores_everyday_noise(generated):
    # The generated lines.
    lines, _ = generated
    # No loan-service bucket may be a spike.
    assert not [moment for service, moment in find_spikes(lines) if service == "loan-service"]


# In the bad-deployment window, failures start in payments, never in api-gateway.
def test_root_service_is_payments_not_gateway(generated):
    # The generated lines and "today".
    lines, today = generated
    # The bad deployment's window, in UTC.
    incident = incidents.INCIDENT_BAD_DEPLOY
    starts = generate.to_time(today, incident["starts"]).astimezone(timezone.utc)
    ends = generate.to_time(today, incident["ends"]).astimezone(timezone.utc)
    # For each failed request in the window: its earliest error line.
    first_error = {}
    # Go through the error lines, oldest first.
    for row in sorted((r for r in lines if r["Level"] == "ERROR"), key=time_of):
        # Only lines inside the window count.
        if starts <= time_of(row) <= ends:
            # Keep only the first error of each request (setdefault ignores later ones).
            first_error.setdefault(row["RequestId"], row["Service"])
    # Count failed requests per starting app.
    roots = defaultdict(int)
    # Go through the starting apps.
    for service in first_error.values():
        # Add one.
        roots[service] += 1
    # payments started the most failures.
    assert max(roots, key=roots.get) == "payments"
    # api-gateway never started one: it only reports them.
    assert roots.get("api-gateway", 0) == 0


# Around the rollback, the payments error rate drops sharply.
def test_rollback_lowers_payments_error_rate(generated):
    # The generated lines and "today".
    lines, today = generated
    # The rollback moment (the incident's end), in UTC.
    rollback = generate.to_time(today, incidents.INCIDENT_BAD_DEPLOY["ends"]).astimezone(timezone.utc)
    # 30 minutes on each side, the tool's default.
    window = timedelta(minutes=30)
    # The payments lines on each side of the rollback.
    before = [r for r in lines if r["Service"] == "payments" and rollback - window <= time_of(r) < rollback]
    after = [r for r in lines if r["Service"] == "payments" and rollback <= time_of(r) < rollback + window]
    # The error rate on each side, as a percentage.
    rate = lambda rows: 100 * sum(r["Level"] == "ERROR" for r in rows) / len(rows)
    # Before the rollback, most payments lines are errors.
    assert rate(before) > 50
    # After it, almost none are.
    assert rate(after) < 10