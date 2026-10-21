"""
tests/test_azure_openai.py
==========================

WHAT THIS FILE IS FOR
---------------------
This file checks that our Python code can talk to the two AI models we
deployed on Azure:

    1. gpt-5.1                 -> the chat model. It reads a question and
                                  writes text back. Later in the project it
                                  writes SQL and writes the commentary.

    2. text-embedding-3-small  -> the embedding model. It turns a piece of
                                  text into a list of 1,536 numbers. Later
                                  in the project those numbers are stored in
                                  pgvector and used to find the right tables
                                  and the right scheme names for a question.

If both tests in this file pass, we know three things are true:
    - the web address (endpoint) in .env is correct
    - the key in .env is correct and still valid
    - both models are deployed and answering

WHY WE TEST THIS BEFORE WRITING ANY PIPELINE CODE
-------------------------------------------------
Every later step (choosing tables, writing SQL, writing commentary) depends
on these two models. If the connection is broken, every later step fails
with confusing errors that look like bugs in our own code. Testing the
connection alone, first, means that if something breaks later, we already
know the models themselves are fine.

WHERE THE SETTINGS COME FROM
----------------------------
This file does not contain the key or the address. It reads four settings
from the .env file in the project folder:

    AZURE_OPENAI_ENDPOINT  -> the web address of our Azure OpenAI resource
    AZURE_OPENAI_API_KEY   -> the secret key that proves the request is ours
    CHAT_DEPLOYMENT        -> the name we gave the chat model deployment
    EMBED_DEPLOYMENT       -> the name we gave the embedding model deployment

Keeping these in .env (and not in the code) means:
    - the key never ends up on GitHub, because .gitignore blocks .env
    - the client can point the same code at their own Azure resource just
      by changing .env, without editing any Python file

HOW TO RUN
----------
From the project folder, in the terminal:

    uv run pytest tests/test_azure_openai.py -v

    -v  means "verbose": pytest prints each test's name and whether it
        PASSED or FAILED, instead of just a dot per test.

WHAT IT COSTS
-------------
Each run sends two tiny requests (a few dozen tokens in total).
The cost is a small fraction of one rupee.
"""

# ---------------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------------
# An import brings in code that someone else has already written, so we do
# not have to write it ourselves.

# "os" is part of Python itself (no install needed).
# We use it to read settings (environment variables) such as the endpoint
# and the key.
import os

# "load_dotenv" comes from the python-dotenv package, which we installed in
# Stage 0 with "uv add python-dotenv".
# It opens the .env file and makes each line in it available to os.environ,
# exactly as if we had typed "export NAME=value" in the terminal.
from dotenv import load_dotenv

# "OpenAI" comes from the openai package, which we installed with
# "uv add openai".
# It is the official Python client for sending requests to OpenAI-style
# model endpoints. Azure OpenAI accepts the same request format, so we can
# use this same client to talk to our Azure resource.
from openai import OpenAI


# ---------------------------------------------------------------------------
# STEP 1: LOAD THE SETTINGS FROM .env
# ---------------------------------------------------------------------------
# This must run BEFORE we read any setting below.
# If we skip this line, os.environ will not contain AZURE_OPENAI_ENDPOINT
# (or the other three), and the code below will stop with a KeyError.
load_dotenv()


# ---------------------------------------------------------------------------
# STEP 2: CREATE THE CLIENT (THE CONNECTION TO AZURE)
# ---------------------------------------------------------------------------
# The client is the object that knows where to send requests and which key
# to attach to them. We create it once here, and both tests below use it.
#
# Two settings are needed:
#
#   base_url -> WHERE to send requests.
#               Our endpoint looks like:
#                   https://aoai-fundgraph-dev-cl01.openai.azure.com/
#               Azure expects OpenAI-style requests at the path
#                   /openai/v1/
#               added to the end of that address. So the final address is:
#                   https://aoai-fundgraph-dev-cl01.openai.azure.com/openai/v1/
#
#   api_key  -> the secret key. Azure checks it on every request and rejects
#               the request if it is missing or wrong.
client = OpenAI(
    # os.environ["AZURE_OPENAI_ENDPOINT"] reads the endpoint from .env.
    #
    # .rstrip("/") removes a trailing "/" if there is one. We do this
    # because we add "/openai/v1/" ourselves on the next part of the line.
    # Without rstrip, an endpoint ending in "/" would produce a double
    # slash ("...azure.com//openai/v1/"), which Azure does not accept.
    #
    # + "/openai/v1/" adds Azure's standard path for OpenAI-style requests.
    base_url=os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1/",

    # Reads the secret key from .env.
    # We use os.environ[...] with square brackets on purpose: if the setting
    # is missing, Python stops immediately with a clear error naming the
    # missing setting. That is better than silently sending a request with
    # no key and getting a confusing "401 Unauthorized" error from Azure.
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)


# ---------------------------------------------------------------------------
# TEST 1: THE CHAT MODEL ANSWERS
# ---------------------------------------------------------------------------
# pytest finds tests by name: any function whose name starts with "test_"
# is run as a test. It passes if it finishes without an error, and fails if
# any "assert" line inside it is False.
def test_chat_model_answers():
    """
    Sends one short message to gpt-5.1 and checks the reply contains "OK".

    Why this exact message:
    We ask the model to reply with a single known word. That makes the test
    easy to check: we do not need to judge the quality of an answer, only
    whether the word came back. It also keeps the request tiny, so it is
    fast and costs almost nothing.
    """

    # client.chat.completions.create(...) sends a chat request to the model
    # and waits for the reply. "reply" holds everything Azure sends back.
    reply = client.chat.completions.create(
        # "model" is the DEPLOYMENT NAME, not the model's own name.
        # On Azure these can differ: we could have named the deployment
        # "sql-writer" while the model inside it is gpt-5.1.
        # We named ours gpt-5.1, and CHAT_DEPLOYMENT in .env holds that name.
        model=os.environ["CHAT_DEPLOYMENT"],

        # "messages" is the conversation sent to the model, as a list.
        # Each message has:
        #   role    -> who is speaking. "user" means us, asking a question.
        #   content -> the actual text of the message.
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
    )

    # The model's text is several levels inside the reply:
    #
    #   reply.choices                    -> a list of answers. We asked for
    #                                       one, so the list has one item.
    #   reply.choices[0]                 -> that first (and only) answer.
    #   reply.choices[0].message.content -> the text the model wrote.
    #
    # "assert" checks that something is True. If it is False, the test
    # FAILS and pytest prints what was compared.
    #
    # We check "OK" is IN the reply rather than that the reply EQUALS "OK".
    # Models sometimes add punctuation or a space ("OK." or " OK"). Checking
    # with "in" stops the test from failing for a reason that does not
    # matter to us.
    assert "OK" in reply.choices[0].message.content


# ---------------------------------------------------------------------------
# TEST 2: THE EMBEDDING MODEL RETURNS 1,536 NUMBERS
# ---------------------------------------------------------------------------
def test_embedding_has_1536_dimensions():
    """
    Sends one scheme name to text-embedding-3-small and checks that exactly
    1,536 numbers come back.

    Why 1,536 matters:
    In Step 5, these numbers are stored in pgvector inside Postgres. The
    table column that stores them is created with a fixed size of 1,536.
    If the model returned any other size, every insert into that column
    would fail.

    1,536 also matters for speed. pgvector's fast search index (HNSW) works
    on up to 2,000 numbers per item. text-embedding-3-small returns 1,536,
    which fits. The larger model, text-embedding-3-large, returns 3,072,
    which does not fit, and that is why we chose the small one.

    Why a scheme name as the test input:
    "Sahyadri Credit Risk Fund" is the kind of text we will actually embed
    later (scheme names, issuer names, table notes). Using a realistic input
    keeps the test close to real use.
    """

    # client.embeddings.create(...) sends text to the embedding model and
    # waits for the numbers to come back.
    result = client.embeddings.create(
        # The deployment name of the embedding model, read from .env.
        model=os.environ["EMBED_DEPLOYMENT"],

        # The text to turn into numbers.
        input="Sahyadri Credit Risk Fund",
    )

    # The numbers are several levels inside the result:
    #
    #   result.data                 -> a list, one entry per input text.
    #                                  We sent one text, so it has one entry.
    #   result.data[0]              -> the entry for our one text.
    #   result.data[0].embedding    -> the list of numbers for that text.
    #
    # len(...) counts how many numbers are in the list.
    # The test passes only if that count is exactly 1,536.
    assert len(result.data[0].embedding) == 1536