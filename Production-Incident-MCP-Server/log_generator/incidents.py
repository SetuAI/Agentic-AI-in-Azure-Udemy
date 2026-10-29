"""
log_generator/incidents.py
==========================

WHAT THIS FILE IS FOR
---------------------
Holds the 3 problems ("incidents") we hide inside the normal logs, plus the
list of app releases ("deployments"). The demo questions in the README have
answers only because these are planted here.

THE 3 PLANTED INCIDENTS
-----------------------
    1. Bad deployment (today, 02:05 to 02:41 India time)
       payments v2.3.1 is released at 02:05. It cuts the database connection
       pool from 50 to 5. The nightly EMI auto-debit job is running, so
       payments runs out of connections and requests time out. At 02:41 the
       on-call engineer rolls back to v2.3.0, and errors stop.
       Demo question: "Why did payments fail last night?"

    2. Outside SMS company down (yesterday, 14:00 to 16:00 India time)
       The SMS company "QuickSMS" stops working for 2 hours. notifications
       cannot send OTPs. No release happened, so the cause is outside the bank.
       Demo question: "Why are customers saying they didn't get OTPs yesterday?"

    3. One customer's failed request (yesterday, 11:32 India time)
       Request req-8f3a21 passes api-gateway and loan-service, then fails in
       payments because the customer's bank account is frozen.
       Demo question: "Trace request req-8f3a21 and tell me where it failed."

HOW TIMES ARE WRITTEN HERE
--------------------------
Every time is (day, hour, minute), in India time:
    day 0  = today
    day -1 = yesterday
generate.py turns these into real dates when you run it, so the logs are
always "fresh" (the last 42 hours) whenever a student runs the project.

HOW TO RUN
----------
You never run this file directly. generate.py and the tests import it.

EXPECTED OUTPUT
---------------
Nothing. This file only holds facts.
"""

# ---------------------------------------------------------------------------
# Incident 1: the bad payments deployment
# ---------------------------------------------------------------------------
INCIDENT_BAD_DEPLOY = {
    # A short name, used in test messages.
    "name": "Bad payments deployment",
    # The app that fails.
    "service": "payments",
    # When the errors start: today at 02:10 (5 minutes after the release at 02:05).
    "starts": (0, 2, 10),
    # When the errors stop: today at 02:41 (the moment of the rollback).
    "ends": (0, 2, 41),
    # The error message payments writes. It says what went wrong in plain words.
    "message": "Timed out waiting for a database connection (pool size 5, all 5 in use)",
    # How serious it is.
    "level": "ERROR",
    # 504 = "the server waited too long and gave up".
    "status": 504,
    # Each failed request waits 30 seconds (30,000 ms) before giving up.
    "duration_ms": 30000,
    # 9 out of 10 payments requests fail during this time. A few still get lucky.
    "failure_rate": 0.9,
}

# ---------------------------------------------------------------------------
# Incident 2: the outside SMS company is down
# ---------------------------------------------------------------------------
INCIDENT_SMS_OUTAGE = {
    # A short name, used in test messages.
    "name": "SMS provider outage",
    # The app that fails.
    "service": "notifications",
    # When the errors start: yesterday at 14:00.
    "starts": (-1, 14, 0),
    # When the errors stop: yesterday at 16:00.
    "ends": (-1, 16, 0),
    # The error message notifications writes. It names the outside company.
    "message": "SMS provider QuickSMS returned 503 Service Unavailable",
    # How serious it is.
    "level": "ERROR",
    # 503 = "the service is not available right now".
    "status": 503,
    # Each failed SMS waits 3 seconds for QuickSMS before giving up.
    "duration_ms": 3000,
    # 85 out of 100 SMS messages fail during this time.
    "failure_rate": 0.85,
}

# Both incidents in one list, so generate.py can check them in a loop.
INCIDENTS = [INCIDENT_BAD_DEPLOY, INCIDENT_SMS_OUTAGE]

# ---------------------------------------------------------------------------
# Incident 3: one customer's failed request, written line by line
# ---------------------------------------------------------------------------
TRACED_REQUEST = {
    # The request's ID. Every log line of this request carries this ID.
    "request_id": "req-8f3a21",
    # What the customer asked for: send the loan money to their bank account.
    "endpoint": "POST /loans/disburse",
    # When the request started: yesterday at 11:32:07.
    "starts": (-1, 11, 32, 7),
    # The lines, in order. Each one is (app, level, message, status code, time taken in ms).
    "lines": [
        # 1. loan-service approves the loan. All good so far.
        ("loan-service", "INFO", "Loan LN-20431 approved, starting disbursement", 200, 85),
        # 2. payments tries to send the money, but the bank refuses it.
        ("payments", "ERROR",
         "Disbursement rejected by bank: beneficiary account is frozen (code BNK-417)", 502, 1840),
        # 3. loan-service hears about the failure and puts the loan on hold.
        ("loan-service", "ERROR", "Disbursement failed for LN-20431, loan moved to ON_HOLD", 502, 40),
        # 4. api-gateway tells the customer the request failed.
        ("api-gateway", "ERROR", "POST /loans/disburse -> 502", 502, 1975),
    ],
}

# ---------------------------------------------------------------------------
# Deployments: every new version of an app released in the last 42 hours
# ---------------------------------------------------------------------------
# Every entry is (day, hour, minute, app, version, released by, change note).
# Only two of these matter for the incidents (today 02:05 and 02:41).
# The rest are normal releases, so the AI has to pick the right one.
DEPLOYMENTS = [
    # Yesterday 10:30: loan-service gets a new field on the application form.
    (-1, 10, 30, "loan-service", "v4.1.0", "anita.s", "Add co-applicant field to loan application"),
    # Yesterday 17:10: notifications gets new SMS wording (after the SMS outage, not before).
    (-1, 17, 10, "notifications", "v1.8.2", "vikram.p", "Update OTP SMS template text"),
    # Yesterday 17:45: payments v2.3.0, a normal and healthy release.
    (-1, 17, 45, "payments", "v2.3.0", "rohan.k", "Add UPI reference number to EMI receipts"),
    # Yesterday 18:30: api-gateway allows more OTP requests per minute.
    (-1, 18, 30, "api-gateway", "v3.0.4", "anita.s", "Raise rate limit for /otp/send"),
    # Today 02:05: THE BAD RELEASE. It shrinks the database connection pool.
    (0, 2, 5, "payments", "v2.3.1", "rohan.k",
     "Reduce DB connection pool size from 50 to 5 to cut database cost"),
    # Today 02:41: THE FIX. The on-call engineer goes back to the previous version.
    (0, 2, 41, "payments", "v2.3.0", "oncall.priya", "Rollback: restore DB connection pool size to 50"),
]