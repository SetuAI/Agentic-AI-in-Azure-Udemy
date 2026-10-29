"""
tests/test_planted_incidents.py
===============================

WHAT THIS FILE IS FOR
---------------------
Checks that the log generator really hides the 3 incidents in the logs, so
the demo questions have answers to find. It runs the generator into a
temporary folder and reads the files back. No Azure is needed.

WHAT IT CHECKS
--------------
    1. Bad deployment: many payments timeouts, all inside today's 02:10-02:41
       window, with the v2.3.1 release just before and the rollback right after.
    2. SMS outage: many QuickSMS errors, all inside yesterday's 14:00-16:00
       window, and NO release during it (so the cause is outside the bank).
    3. Request req-8f3a21: 4 lines, and the first failure is in payments.

HOW TO RUN
----------
From the project folder:

    uv run pytest tests/test_planted_incidents.py -v

EXPECTED OUTPUT
---------------
Every test shows PASSED.
"""

# json: reads the generated JSON files.
import json

# datetime: reads the time text in each row back into a date and time. timedelta: a length of time.
from datetime import datetime, timedelta

# pytest: the test runner.
import pytest

# The generator, which we run, and the incidents, which hold the expected answers.
from log_generator import generate, incidents


# A "fixture": pytest runs this once, and hands its result to every test that asks for "logs".
@pytest.fixture(scope="module")
def logs(tmp_path_factory):
    """Run the generator into a temporary folder; return (log lines, deployments, today)."""
    # A fresh temporary folder that pytest deletes afterwards.
    folder = tmp_path_factory.mktemp("data")
    # Point the generator at that folder instead of data/ (so the real data/ is not touched).
    generate.DATA_DIR = folder
    # Run the generator.
    generate.main()
    # Read the log lines back.
    lines = json.loads((folder / "app_logs.json").read_text(encoding="utf-8"))
    # Read the deployments back.
    deployments = json.loads((folder / "deployments.json").read_text(encoding="utf-8"))
    # Work out "today" the same way the generator did.
    today = generate.find_today(datetime.now(generate.LOCAL_TZ))
    # Hand all three to the tests.
    return lines, deployments, today


# Reads the TimeGenerated text of a row back into a date and time.
def time_of(row: dict) -> datetime:
    """Return the row's TimeGenerated as a datetime."""
    # fromisoformat reads text like 2026-10-28T20:40:07.123Z ("Z" means UTC).
    return datetime.fromisoformat(row["TimeGenerated"].replace("Z", "+00:00"))


# Finds the real start and end time of an incident.
def window_of(incident: dict, today: datetime) -> tuple[datetime, datetime]:
    """Return the incident's (start, end) as real dates and times."""
    # Turn the (day, hour, minute) tuples into real times.
    return generate.to_time(today, incident["starts"]), generate.to_time(today, incident["ends"])


# The latest moment an incident's error line can be written.
def last_error_time(incident: dict, ends: datetime) -> datetime:
    """Return the end time plus one failure's waiting time."""
    # A failed request writes its line when its wait is over (30 seconds for a timeout).
    # So a request that started just before the end is logged just after it.
    return ends + timedelta(milliseconds=incident["duration_ms"])


# ---------------------------------------------------------------------------
# 1. The bad payments deployment
# ---------------------------------------------------------------------------

# The bad deployment made plenty of payments timeouts, and all of them sit inside its time window.
def test_bad_deploy_errors_are_inside_their_window(logs):
    # Unpack what the fixture gave us.
    lines, _, today = logs
    # The incident's details.
    incident = incidents.INCIDENT_BAD_DEPLOY
    # Its real start and end.
    starts, ends = window_of(incident, today)
    # Every line carrying this incident's message.
    errors = [row for row in lines if row["Message"] == incident["message"]]
    # There must be plenty of them: a clear spike, not a handful.
    assert len(errors) > 100
    # The latest moment an error line can be written.
    latest = last_error_time(incident, ends)
    # Every one of them must be from payments and inside the window.
    assert all(row["Service"] == "payments" and starts <= time_of(row) <= latest for row in errors)


# The bad release (v2.3.1) comes before the errors, and the rollback ends them.
def test_bad_release_and_rollback_bracket_the_errors(logs):
    # Unpack what the fixture gave us.
    _, deployments, today = logs
    # The incident's real start and end.
    starts, ends = window_of(incidents.INCIDENT_BAD_DEPLOY, today)
    # The bad release (v2.3.1).
    bad = next(row for row in deployments if row["Version"] == "v2.3.1")
    # The rollback (the last payments release, going back to v2.3.0).
    rollback = [row for row in deployments if row["Service"] == "payments"][-1]
    # The bad release happens before the errors start.
    assert time_of(bad) < starts
    # The rollback happens exactly when the errors stop.
    assert time_of(rollback) == ends and rollback["Version"] == "v2.3.0"


# ---------------------------------------------------------------------------
# 2. The SMS company outage
# ---------------------------------------------------------------------------

# The SMS outage made plenty of QuickSMS errors, and no app was released while it lasted.
def test_sms_outage_has_errors_and_no_release(logs):
    # Unpack what the fixture gave us.
    lines, deployments, today = logs
    # The incident's details.
    incident = incidents.INCIDENT_SMS_OUTAGE
    # Its real start and end.
    starts, ends = window_of(incident, today)
    # Every line carrying this incident's message.
    errors = [row for row in lines if row["Message"] == incident["message"]]
    # There must be plenty of them.
    assert len(errors) > 50
    # The latest moment an error line can be written.
    latest = last_error_time(incident, ends)
    # Every one of them must be from notifications and inside the window.
    assert all(row["Service"] == "notifications" and starts <= time_of(row) <= latest for row in errors)
    # No app was released during the outage, so the cause cannot be a release.
    assert not [row for row in deployments if starts <= time_of(row) < ends]


# ---------------------------------------------------------------------------
# 3. The planted request req-8f3a21
# ---------------------------------------------------------------------------

# Request req-8f3a21 has its 4 lines, and payments is where it first failed.
def test_traced_request_fails_in_payments(logs):
    # Unpack what the fixture gave us.
    lines, _, _ = logs
    # Every line of the planted request, in time order (the file is already sorted by time).
    trace = [row for row in lines if row["RequestId"] == incidents.TRACED_REQUEST["request_id"]]
    # It has exactly the 4 hand-written lines.
    assert len(trace) == 4
    # The first line that is an error.
    first_failure = next(row for row in trace if row["Level"] == "ERROR")
    # It must be payments: that is where the request really broke.
    assert first_failure["Service"] == "payments"