"""
log_generator/generate.py
=========================

WHAT THIS FILE IS FOR
---------------------
Creates 42 hours of realistic logs for Sahyadri Bank's 4 apps, with the 3
incidents from incidents.py hidden inside them. It saves them as JSON files
in the data/ folder. loader/send_logs.py then uploads those files to Azure.

WHAT PROBLEM IT SOLVES
----------------------
We need production logs to search, but we cannot use a real bank's logs.
So we create our own: mostly normal traffic, a few everyday failures, and
3 real-looking incidents whose answers we know in advance.

HOW IT WORKS, STEP BY STEP
--------------------------
    1. Find "now" in India time, and go back 42 hours.
    2. For every minute in those 42 hours:
         - start a few customer requests (more in the day, fewer at night)
         - between 02:00 and 03:00, also start the EMI auto-debit requests
    3. Each request passes through its apps. Each app writes one line.
       If an incident is active for that app at that moment, the line
       becomes the incident's error instead.
    4. Add the one hand-written request (req-8f3a21) and the deployments.
    5. Save everything to data/.

HOW TO RUN
----------
From the project folder:

    uv run python -m log_generator.generate

EXPECTED OUTPUT
---------------
About 20,000 log lines and 6 deployments, saved as:
    data/app_logs.json            (all the log lines)
    data/deployments.json         (all the releases)
    data/sample_app_logs.json     (20 lines, to upload in the Azure portal
    data/sample_deployments.json   when you create the tables)
and a short summary printed on screen, including how many errors each
incident created.
"""

# json: saves Python lists and dictionaries as JSON text files.
import json

# random: picks random numbers, so the logs look natural and not repeated.
import random

# datetime: a date plus a time. timedelta: a length of time, like "42 hours". timezone: UTC.
from datetime import datetime, timedelta, timezone

# Path: a simple way to work with folders and file names.
from pathlib import Path

# ZoneInfo: knows the rules of each time zone, such as India time (UTC + 5:30).
from zoneinfo import ZoneInfo

# Our settings, for the time zone name.
from config import settings

# Our logger, to print progress messages with the time.
from config.logging_config import get_logger

# The 4 apps, their requests and their normal messages.
from log_generator import services

# The 3 planted incidents and the deployments.
from log_generator import incidents

# A logger named after this file.
logger = get_logger(__name__)

# How many hours of logs to create. Why 42 and not a round 48?
# Azure only keeps a log's own time if it is at most 2 days (48 hours) older than
# the moment Azure receives it. Older lines get the upload time instead, which would
# put them at the wrong time. 42 hours leaves 6 hours to finish the upload, and still
# covers all 3 incidents.
HOURS_OF_LOGS = 42

# A fixed starting point for the random numbers, so every run makes similar logs.
RANDOM_SEED = 42

# The folder the JSON files are saved in.
DATA_DIR = Path("data")

# How many lines go into the small sample files used in the Azure portal.
SAMPLE_SIZE = 20

# India time (or whichever zone is set in .env).
LOCAL_TZ = ZoneInfo(settings.DISPLAY_TIMEZONE)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Works out which date counts as "today" for the incidents.
def find_today(now: datetime) -> datetime:
    """Return midnight of 'today'. Before 03:00, 'today' is still yesterday's date."""
    # Midnight at the start of the current date.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # If it is before 03:00, the bad deployment (02:05 to 02:41) may not have finished yet ...
    if now.hour < 3:
        # ... so use the day before, where it has fully happened.
        midnight = midnight - timedelta(days=1)
    # Give back the chosen midnight.
    return midnight


# Turns (day, hour, minute) or (day, hour, minute, second) from incidents.py into a real date and time.
def to_time(today: datetime, parts: tuple) -> datetime:
    """Turn (day, hour, minute[, second]) into a real date and time."""
    # Split the tuple into its parts. The seconds are optional, so default them to 0.
    day, hour, minute, *rest = parts
    # If seconds were given, use them; if not, use 0.
    second = rest[0] if rest else 0
    # Start at "today" midnight and move forward by the days, hours, minutes and seconds.
    return today + timedelta(days=day, hours=hour, minutes=minute, seconds=second)


# Turns a date and time into the text Azure expects: UTC time, ending in "Z".
def to_azure_time(moment: datetime) -> str:
    """Return the time as UTC text, like 2026-10-28T20:40:07.123Z."""
    # Change the time to UTC (the time zone Azure stores logs in).
    utc_moment = moment.astimezone(timezone.utc)
    # Write it as text with milliseconds, then replace "+00:00" with "Z" (both mean UTC).
    return utc_moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Makes a random request ID like "req-3c91fe".
def new_request_id(rng: random.Random) -> str:
    """Return a random request ID that is never the planted one."""
    # Keep trying until we get an ID that is not the hand-written request's ID.
    while True:
        # 6 random characters from 0-9 and a-f (a "hex" number), after "req-".
        request_id = f"req-{rng.getrandbits(24):06x}"
        # If it is different from the planted ID, it is safe to use.
        if request_id != incidents.TRACED_REQUEST["request_id"]:
            # Give it back.
            return request_id


# Fills the {customer}, {loan} and {amount} gaps in a message with made-up values.
def fill_message(template: str, rng: random.Random) -> str:
    """Return the message with made-up customer, loan and amount values."""
    # format() swaps each {name} for the value given with that name.
    return template.format(
        # A made-up customer number, like CUST-48213.
        customer=f"CUST-{rng.randint(10000, 99999)}",
        # A made-up loan number, like LN-31877.
        loan=f"LN-{rng.randint(10000, 99999)}",
        # A made-up amount between Rs 2,000 and Rs 50,000, written with commas.
        amount=f"{rng.randint(2000, 50000):,}",
    )


# Builds one log line as a dictionary. The keys are the column names in Azure.
def log_line(moment, service, level, message, request_id, endpoint, status, duration_ms) -> dict:
    """Return one log line with the 8 columns of the AppLogs_CL table."""
    # Each key becomes one column in the Azure table.
    return {
        # When it happened, in UTC. Azure needs this exact column name.
        "TimeGenerated": to_azure_time(moment),
        # Which app wrote the line.
        "Service": service,
        # How serious it is: INFO, WARN or ERROR.
        "Level": level,
        # What happened, in words.
        "Message": message,
        # The ID that links all lines of one request together.
        "RequestId": request_id,
        # What the customer asked for.
        "Endpoint": endpoint,
        # The result number (200 = success, 4xx/5xx = failure).
        "StatusCode": status,
        # How long this app took, in milliseconds.
        "DurationMs": duration_ms,
    }


# Finds the incident (if any) that is breaking this app at this moment.
def active_incident(service: str, moment: datetime, windows: list) -> dict | None:
    """Return the incident active for this app at this moment, or None."""
    # Look at every incident and its start and end time.
    for incident, starts, ends in windows:
        # It counts if it is for this app and the moment is inside its time window.
        if incident["service"] == service and starts <= moment < ends:
            # Give back that incident.
            return incident
    # No incident is active for this app right now.
    return None


# ---------------------------------------------------------------------------
# One customer request, from start to finish
# ---------------------------------------------------------------------------

# Creates all the log lines for one request.
def make_request(rng: random.Random, route: dict, start: datetime, windows: list) -> list:
    """Return (time, log line) pairs for one request through its apps."""
    # Every request gets its own ID, shared by all its lines.
    request_id = new_request_id(rng)
    # The list we fill with (time, line) pairs.
    lines = []
    # The clock for this request. It moves forward as each app does its work.
    clock = start
    # The final result. It stays 200 (success) unless an app fails.
    final_status = 200

    # Go through the apps this request passes, one after another.
    for service, template in route["steps"]:
        # Is an incident breaking this app right now?
        incident = active_incident(service, clock, windows)
        # If so, roll the dice: does this request fail because of it?
        if incident and rng.random() < incident["failure_rate"]:
            # Yes: use the incident's level, message, status and time taken.
            level, message = incident["level"], incident["message"]
            # The status and the (long) time the failure took.
            status, duration = incident["status"], incident["duration_ms"]
        # If not, roll the dice for an everyday failure (only apps that have them).
        elif service in services.NORMAL_FAILURES and rng.random() < services.NORMAL_FAILURE_RATE:
            # Pick one of this app's everyday failures at random.
            level, message, status = rng.choice(services.NORMAL_FAILURES[service])
            # It takes a normal amount of time.
            duration = rng.randint(*services.DURATION_MS[service])
        # Otherwise everything worked.
        else:
            # A normal, healthy line.
            level, message, status = "INFO", fill_message(template, rng), 200
            # It takes a normal amount of time.
            duration = rng.randint(*services.DURATION_MS[service])

        # Move the clock forward by the time this app took.
        clock = clock + timedelta(milliseconds=duration)
        # Save this app's line, stamped at the moment it finished.
        lines.append((clock, log_line(clock, service, level, message,
                                      request_id, route["endpoint"], status, duration)))
        # If this app failed, the request stops here; the next apps are never called.
        if status >= 400:
            # Remember the failure, so api-gateway reports it.
            final_status = status
            # Leave the loop.
            break

    # api-gateway adds a little time of its own at the end.
    clock = clock + timedelta(milliseconds=rng.randint(*services.DURATION_MS["api-gateway"]))
    # The total time, from the start of the request to now, in milliseconds.
    total_ms = int((clock - start).total_seconds() * 1000)
    # api-gateway's level: INFO for success, WARN for 4xx (request problem), ERROR for 5xx (server problem).
    level = "INFO" if final_status < 400 else ("WARN" if final_status < 500 else "ERROR")
    # Save api-gateway's last line, like "POST /payments/emi -> 200".
    lines.append((clock, log_line(clock, "api-gateway", level, f"{route['endpoint']} -> {final_status}",
                                  request_id, route["endpoint"], final_status, total_ms)))
    # Give back all the lines of this request.
    return lines


# ---------------------------------------------------------------------------
# The hand-written request (incident 3) and the deployments
# ---------------------------------------------------------------------------

# Creates the lines of request req-8f3a21 exactly as written in incidents.py.
def make_traced_request(today: datetime) -> list:
    """Return (time, log line) pairs for the planted request."""
    # The planted request's details.
    traced = incidents.TRACED_REQUEST
    # When it started.
    clock = to_time(today, traced["starts"])
    # The list we fill with (time, line) pairs.
    lines = []
    # Go through its lines in order.
    for service, level, message, status, duration in traced["lines"]:
        # api-gateway's line shows the TOTAL time, so it does not move the clock again.
        if service != "api-gateway":
            # Move the clock forward by the time this app took.
            clock = clock + timedelta(milliseconds=duration)
        # Save the line.
        lines.append((clock, log_line(clock, service, level, message,
                                      traced["request_id"], traced["endpoint"], status, duration)))
    # Give back the lines.
    return lines


# Creates one row per deployment for the Deployments_CL table.
def make_deployments(today: datetime) -> list:
    """Return (time, row) pairs for every deployment."""
    # The list we fill with (time, row) pairs.
    rows = []
    # Go through every deployment in incidents.py.
    for day, hour, minute, service, version, released_by, note in incidents.DEPLOYMENTS:
        # When it was released.
        moment = to_time(today, (day, hour, minute))
        # Each key becomes one column in the Deployments_CL table.
        rows.append((moment, {
            # When it was released, in UTC.
            "TimeGenerated": to_azure_time(moment),
            # Which app was released.
            "Service": service,
            # The new version number.
            "Version": version,
            # Who released it.
            "DeployedBy": released_by,
            # What changed, in their words.
            "ChangeNote": note,
        }))
    # Give back the rows.
    return rows


# ---------------------------------------------------------------------------
# The main program
# ---------------------------------------------------------------------------

# Saves a list of rows to a JSON file in the data/ folder.
def save(rows: list, file_name: str) -> None:
    """Write the rows to data/<file_name> as pretty JSON."""
    # The full path, like data/app_logs.json.
    path = DATA_DIR / file_name
    # Open the file for writing and save the rows with 2-space indents (easy to read).
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    # Say what was saved.
    logger.info("Saved %s rows to %s", f"{len(rows):,}", path)


# Runs the whole generator.
def main() -> None:
    """Create 42 hours of logs with the 3 incidents and save them to data/."""
    # The random number maker, started from our fixed seed.
    rng = random.Random(RANDOM_SEED)
    # The time right now, in India time.
    now = datetime.now(LOCAL_TZ)
    # The start of our 42 hours, rounded down to a whole minute.
    start = (now - timedelta(hours=HOURS_OF_LOGS)).replace(second=0, microsecond=0)
    # Which date counts as "today" for the incidents.
    today = find_today(now)
    # Show the dates, so you know what "today" and "yesterday" mean in your logs.
    logger.info("Creating logs from %s to %s (today = %s)",
                f"{start:%d %b %H:%M}", f"{now:%d %b %H:%M}", f"{today:%d %b}")

    # Each incident with its real start and end time.
    windows = [(incident, to_time(today, incident["starts"]), to_time(today, incident["ends"]))
               for incident in incidents.INCIDENTS]
    # The routes, and how common each one is, for random picking.
    routes = services.ROUTES
    # Just the weights, in the same order as the routes.
    weights = [route["weight"] for route in routes]

    # All (time, line) pairs we create.
    lines = []
    # Start at the first minute.
    minute = start
    # Step through every minute until now.
    while minute < now:
        # Is it day or night? This decides how busy the bank is.
        is_day = minute.hour >= services.DAY_STARTS_AT_HOUR
        # The most requests that can start in this minute.
        most = services.DAY_MAX_PER_MINUTE if is_day else services.NIGHT_MAX_PER_MINUTE
        # Pick how many customer requests start in this minute (somewhere from 0 to the most).
        for _ in range(rng.randint(0, most)):
            # A random second inside this minute.
            started = minute + timedelta(seconds=rng.uniform(0, 60))
            # Pick a route at random, with common routes picked more often.
            route = rng.choices(routes, weights=weights)[0]
            # Create the lines for this request and add them to the list.
            lines.extend(make_request(rng, route, started, windows))

        # During the nightly auto-debit hour (02:00 to 02:59), add the auto-debit requests too.
        if minute.hour == services.AUTODEBIT_START_HOUR:
            # The job sends a fixed number of requests every minute.
            for _ in range(services.AUTODEBIT_PER_MINUTE):
                # A random second inside this minute.
                started = minute + timedelta(seconds=rng.uniform(0, 60))
                # Create the lines for this auto-debit request.
                lines.extend(make_request(rng, services.AUTODEBIT_ROUTE, started, windows))

        # Move on to the next minute.
        minute = minute + timedelta(minutes=1)

    # Add the planted request req-8f3a21.
    lines.extend(make_traced_request(today))
    # Keep only lines inside our 42 hours (none in the future, none too old), sorted by time.
    lines = sorted((pair for pair in lines if start <= pair[0] <= now), key=lambda pair: pair[0])
    # Create the deployments, keep only those inside our 42 hours, sorted by time.
    deployments = sorted((pair for pair in make_deployments(today) if start <= pair[0] <= now),
                         key=lambda pair: pair[0])

    # Make sure the data/ folder exists (create it if it does not).
    DATA_DIR.mkdir(exist_ok=True)
    # Keep just the lines (drop the times we used for sorting).
    log_rows = [row for _, row in lines]
    # Same for the deployments.
    deployment_rows = [row for _, row in deployments]
    # Save all the log lines.
    save(log_rows, "app_logs.json")
    # Save all the deployments.
    save(deployment_rows, "deployments.json")
    # Save a few lines for creating the AppLogs_CL table in the portal.
    save(log_rows[:SAMPLE_SIZE], "sample_app_logs.json")
    # Save a few deployments for creating the Deployments_CL table in the portal.
    save(deployment_rows[:5], "sample_deployments.json")

    # Print how many errors each incident created, as a quick check that they are there.
    for incident, starts, ends in windows:
        # Count the lines that carry this incident's message.
        count = sum(1 for _, row in lines if row["Message"] == incident["message"])
        # Print the incident's name, its time window and the count.
        logger.info("%s: %s errors between %s and %s", incident["name"], count,
                    f"{starts:%d %b %H:%M}", f"{ends:%d %b %H:%M}")


# Run main() only when this file is started directly (not when it is imported).
if __name__ == "__main__":
    # Start the generator.
    main()