"""
config/logging_config.py
========================

WHAT THIS FILE IS FOR
---------------------
Sets up "logging" for our own Python code: the messages our scripts print
while they run, such as "Uploaded 30,412 log lines".

WHAT PROBLEM IT SOLVES
----------------------
print() shows only the text. A logger also shows the time, how serious the
message is (INFO, WARNING, ERROR) and which file wrote it. That makes it
easy to see what happened, and in what order.

HOW TO RUN
----------
You never run this file directly. Every script calls it once at the start:

    from config.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Starting")

EXPECTED OUTPUT
---------------
Lines like:
    2026-10-29 20:45:01 | INFO    | loader.send_logs | Starting
"""

# logging: Python's built-in tool for writing messages with a time and a level.
import logging

# The shape of every message: time | level | file name | the message itself.
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

# The shape of the time: year-month-day hour:minute:second.
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Azure's libraries print a lot of detail about every web call. We only want their warnings.
logging.getLogger("azure").setLevel(logging.WARNING)

# Same for httpx, the library that makes the web calls.
logging.getLogger("httpx").setLevel(logging.WARNING)


# Every file calls this to get its own logger, named after the file.
def get_logger(name: str) -> logging.Logger:
    """Return a logger that prints time, level, file name and message."""
    # Set up the shared format once. If it is already set up, this line does nothing.
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)
    # Give back a logger with this file's name, so each message shows where it came from.
    return logging.getLogger(name)