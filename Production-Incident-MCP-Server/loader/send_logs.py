"""
loader/send_logs.py
===================

WHAT THIS FILE IS FOR
---------------------
Uploads the JSON files made by log_generator/generate.py into the two
tables in your Log Analytics workspace:

    data/app_logs.json      ->  AppLogs_CL      (every log line)
    data/deployments.json   ->  Deployments_CL  (every app release)

WHAT PROBLEM IT SOLVES
----------------------
In a real company, apps send their logs to Azure by themselves. Our apps
are imaginary, so this script plays their part and sends the logs for them.

HOW IT WORKS, STEP BY STEP
--------------------------
    1. Sign in to Azure as our Entra ID app (the ID and secret in .env).
    2. Connect to the "logs ingestion endpoint" (the web address that
       accepts logs).
    3. For each file, send its rows through the data collection rule (DCR).
       The DCR is the Azure rule that checks the rows and puts them in the
       right table.

BEFORE YOU RUN IT
-----------------
    - Run the generator first, so data/ has the files.
    - Create both tables in the Azure portal  (docs/azure/log-analytics.md).
    - Give the Entra ID app the "Monitoring Metrics Publisher" role on
      the DCR  (docs/azure/entra-id.md).
    - Upload within 6 hours of running the generator. Azure changes the
      time of any log line more than 2 days older than the upload, so this
      script refuses rows older than 47 hours.

HOW TO RUN
----------
From the project folder:

    uv run python -m loader.send_logs

EXPECTED OUTPUT
---------------
    Uploading 19,948 rows from data/app_logs.json to stream Custom-AppLogs_CL
    Done: data/app_logs.json
    Uploading 6 rows from data/deployments.json to stream Custom-Deployments_CL
    Done: data/deployments.json
    All logs sent. They can take 5 to 15 minutes to appear in Log Analytics.

Azure needs a few minutes to make new rows searchable, and the very first
upload to a new table can take a little longer.
"""

# json: reads the JSON files back into Python lists.
import json

# datetime: a date plus a time. timedelta: a length of time. timezone: UTC.
from datetime import datetime, timedelta, timezone

# Path: a simple way to work with file names.
from pathlib import Path

# DefaultAzureCredential: signs in to Azure. On your laptop it uses the
# AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET values from .env.
from azure.identity import DefaultAzureCredential

# LogsIngestionClient: Azure's ready-made helper for sending logs into Log Analytics.
from azure.monitor.ingestion import LogsIngestionClient

# Our settings (the endpoint, the DCR ID and the stream names).
from config import settings

# Our logger, to print progress messages with the time.
from config.logging_config import get_logger

# A logger named after this file.
logger = get_logger(__name__)

# Azure keeps a log line's own time only if it is at most 2 days older than the upload.
# Older lines get the upload time instead, which would put them at the wrong time.
# We stop a little before that limit, at 47 hours, to be safe.
MAX_LOG_AGE = timedelta(hours=47)

# The two uploads to do: which file goes through which rule and stream (the "door" into a table).
UPLOADS = [
    # Every log line goes to the AppLogs_CL table.
    (Path("data/app_logs.json"), settings.DCR_IMMUTABLE_ID, settings.APP_LOGS_STREAM),
    # Every release goes to the Deployments_CL table (usually through the same rule).
    (Path("data/deployments.json"), settings.DEPLOYMENTS_DCR_IMMUTABLE_ID, settings.DEPLOYMENTS_STREAM),
]


# Stops the upload if the oldest row is too old for Azure to keep its time.
def check_age(rows: list, path: Path) -> None:
    """Stop with a clear message if any row is older than MAX_LOG_AGE."""
    # Find the oldest time in the file. Each time ends in "Z" (UTC); fromisoformat needs "+00:00".
    oldest = min(datetime.fromisoformat(row["TimeGenerated"].replace("Z", "+00:00")) for row in rows)
    # How old that row is right now.
    age = datetime.now(timezone.utc) - oldest
    # If it is too old, Azure would change its time, so stop before uploading anything.
    if age > MAX_LOG_AGE:
        # Stop the program, and say how to fix it.
        raise RuntimeError(f"{path} has rows {age.total_seconds() / 3600:.0f} hours old. Azure would "
                           f"replace their times. Run the generator again, then upload straight away.")


# Runs the whole upload.
def main() -> None:
    """Upload both JSON files to Log Analytics."""
    # The web address that accepts logs. Stop with a clear message if it is missing from .env.
    endpoint = settings.require("LOGS_INGESTION_ENDPOINT", settings.LOGS_INGESTION_ENDPOINT)
    # The ID of the data collection rule. Stop with a clear message if it is missing.
    settings.require("DCR_IMMUTABLE_ID", settings.DCR_IMMUTABLE_ID)

    # Sign in to Azure. This uses the Entra ID app's details from .env.
    credential = DefaultAzureCredential()
    # Connect to the logs ingestion endpoint as that signed-in app.
    client = LogsIngestionClient(endpoint=endpoint, credential=credential)

    # Do each upload in turn.
    for path, rule_id, stream in UPLOADS:
        # If the generator has not been run, the file is missing: stop and say so.
        if not path.exists():
            # Stop the program with a message that says what to run first.
            raise FileNotFoundError(f"{path} not found. Run: uv run python -m log_generator.generate")
        # Read the file into a Python list of rows.
        rows = json.loads(path.read_text(encoding="utf-8"))
        # Make sure no row is too old for Azure to keep its time.
        check_age(rows, path)
        # Say what is about to happen.
        logger.info("Uploading %s rows from %s to stream %s", f"{len(rows):,}", path, stream)
        # Send the rows. The helper splits them into small batches (Azure accepts up to 1 MB per call).
        client.upload(rule_id=rule_id, stream_name=stream, logs=rows)
        # Say this file is done.
        logger.info("Done: %s", path)

    # Remind the reader that Azure needs a few minutes before the rows can be searched.
    logger.info("All logs sent. They can take 5 to 15 minutes to appear in Log Analytics.")


# Run main() only when this file is started directly (not when it is imported).
if __name__ == "__main__":
    # Start the upload.
    main()