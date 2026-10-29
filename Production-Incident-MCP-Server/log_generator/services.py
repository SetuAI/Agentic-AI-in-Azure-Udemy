"""
log_generator/services.py
=========================

WHAT THIS FILE IS FOR
---------------------
Describes "Sahyadri Bank" (an imaginary bank): its 4 apps, the requests
customers send, and the messages each app writes in its log on a NORMAL day.
generate.py reads this file to create 42 hours of everyday logs.

THE 4 APPS (we call them "services")
------------------------------------
    api-gateway    -> the front door. Every customer request enters here first,
                      and it writes one line when the request is finished.
    loan-service   -> handles loan applications, loan status and approvals.
    payments       -> takes EMI payments and sends loan money to bank accounts.
    notifications  -> sends SMS messages (OTPs, confirmations) to customers.

HOW ONE REQUEST FLOWS
---------------------
    customer -> api-gateway -> loan-service -> payments
                                           -> notifications
Each app the request passes through writes one log line. Then api-gateway
writes the last line with the final result (for example 200 = success).

HOW TO RUN
----------
You never run this file directly. generate.py imports it.

EXPECTED OUTPUT
---------------
Nothing. This file only holds lists of facts.
"""

# The 4 apps, in the order a request usually passes through them.
SERVICES = ["api-gateway", "loan-service", "payments", "notifications"]

# ---------------------------------------------------------------------------
# ROUTES: the kinds of requests customers send.
# ---------------------------------------------------------------------------
# Every route has:
#   endpoint -> what the customer asked for (the web address, with GET or POST)
#   weight   -> how common it is. A weight of 30 is 3 times as common as 10.
#   steps    -> the apps the request passes through after api-gateway, and the
#               line each app writes. Words in {curly brackets} are filled in
#               with made-up values, such as a loan number.
ROUTES = [
    # A customer asks for a one-time password (OTP) by SMS. The most common request.
    {"endpoint": "POST /otp/send", "weight": 30,
     # Only notifications is involved: it sends the SMS.
     "steps": [("notifications", "OTP sent by SMS to customer {customer}")]},

    # A customer checks the status of their loan.
    {"endpoint": "GET /loans/status", "weight": 25,
     # Only loan-service is involved: it looks the loan up.
     "steps": [("loan-service", "Loan status checked for {loan}")]},

    # A customer pays their monthly EMI (loan instalment).
    {"endpoint": "POST /payments/emi", "weight": 25,
     # Only payments is involved: it takes the money.
     "steps": [("payments", "EMI payment of Rs {amount} received for {loan}")]},

    # A customer applies for a new loan.
    {"endpoint": "POST /loans/apply", "weight": 12,
     # First loan-service saves the application, then notifications sends a confirmation SMS.
     "steps": [("loan-service", "New loan application {loan} saved"),
               ("notifications", "Application confirmation SMS sent to customer {customer}")]},

    # The bank sends an approved loan's money to the customer's bank account.
    {"endpoint": "POST /loans/disburse", "weight": 8,
     # First loan-service approves it, then payments sends the money.
     "steps": [("loan-service", "Loan {loan} approved, starting disbursement"),
               ("payments", "Rs {amount} sent to customer bank account for {loan}")]},
]

# ---------------------------------------------------------------------------
# The nightly EMI auto-debit job.
# ---------------------------------------------------------------------------
# Every night between 02:00 and 03:00 (India time), the bank collects EMIs
# automatically from customers who signed up for it. This makes a lot of
# payments requests at night. Incident 1 (see incidents.py) happens during
# this job.
AUTODEBIT_ROUTE = {"endpoint": "POST /payments/emi-autodebit", "weight": 0,
                   # Only payments is involved: it collects the EMI.
                   "steps": [("payments", "Auto-debit of Rs {amount} collected for {loan}")]}

# The hour (India time) the job starts. 2 means 02:00.
AUTODEBIT_START_HOUR = 2

# How many auto-debit requests the job sends every minute while it runs.
AUTODEBIT_PER_MINUTE = 20

# ---------------------------------------------------------------------------
# NORMAL_FAILURES: small problems that happen every day, even when all is well.
# ---------------------------------------------------------------------------
# Real systems always have a few failures: a wrong PIN, a declined card.
# These are background "noise". The planted incidents must stand out from them.
# Every entry is (level, message, status code).
#   level       -> how serious it is: WARN (a customer mistake) or ERROR (our side failed)
#   status code -> the standard web number for the result (4xx = request problem, 5xx = server problem)
NORMAL_FAILURES = {
    # Problems loan-service sometimes has.
    "loan-service": [
        # The customer typed a PAN (tax ID) that does not match their name.
        ("WARN", "Application rejected: PAN does not match applicant name", 422),
        # The credit bureau (the company that gives credit scores) was slow to reply.
        ("ERROR", "Credit bureau did not reply within 5 seconds", 504),
    ],
    # Problems payments sometimes has.
    "payments": [
        # The customer's bank account did not have enough money.
        ("WARN", "EMI payment declined: insufficient balance", 402),
        # The partner bank's system had a short hiccup.
        ("ERROR", "Bank gateway connection reset, payment not completed", 502),
    ],
    # Problems notifications sometimes has.
    "notifications": [
        # The customer's mobile number is not valid.
        ("WARN", "SMS not sent: invalid mobile number", 400),
    ],
}

# The chance that any one step fails in a normal way. 0.01 means 1 in 100.
NORMAL_FAILURE_RATE = 0.01

# ---------------------------------------------------------------------------
# How long each app usually takes to do its part, in milliseconds (1000 ms = 1 second).
# ---------------------------------------------------------------------------
# Every entry is (fastest, slowest). The generator picks a number in between.
DURATION_MS = {
    # api-gateway adds a little time of its own on top of the other apps.
    "api-gateway": (5, 20),
    # loan-service reads and writes the loan database.
    "loan-service": (40, 180),
    # payments talks to banks, so it is the slowest.
    "payments": (120, 600),
    # notifications hands the SMS to the SMS company.
    "notifications": (60, 250),
}

# ---------------------------------------------------------------------------
# How busy the bank is at different times of day (India time).
# ---------------------------------------------------------------------------
# The most customer requests that can start in one minute at night (00:00 to 06:59).
NIGHT_MAX_PER_MINUTE = 2

# The most customer requests that can start in one minute during the day (07:00 to 23:59).
DAY_MAX_PER_MINUTE = 8

# The hour the day starts. Before this hour counts as night.
DAY_STARTS_AT_HOUR = 7