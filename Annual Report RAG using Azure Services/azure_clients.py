"""
================================================================================
azure_clients.py  -  ONE PLACE THAT CONNECTS TO EACH AZURE SERVICE
================================================================================

PURPOSE
-------
This file builds the CONNECTIONS to our three Azure services. Every other file
asks this one for a connection, instead of building its own.

A CLIENT is a ready-made Python object that knows how to talk to one Azure
service: where it is, which key to send, and how to format each request.

This file gives four of them:

    openai_client()         Azure OpenAI       chat answers, claim checks, embeddings
    search_index_client()   Azure AI Search    ADMIN side: create and delete the index
    search_client()         Azure AI Search    DATA side: upload chunks, run searches
    blob_container()        Blob Storage       our container of PDFs

WHAT PROBLEM DOES IT SOLVE
--------------------------
Imagine every file built its own connection. Then the endpoints and keys would
be read in ten different files. To change how we log in, you'd have to change
all ten. And every single call would open a brand-new connection, which is slow.

So, we build each connection in ONE place, here. That gives us three things:

    1. Keys and endpoints are used in one file only.
    2. To switch to passwordless login later (Microsoft Entra ID), we change
       only this file.
    3. Each connection is built ONCE, and then reused for every call.

TWO IDEAS USED IN EVERY FUNCTION
--------------------------------
    1. BUILT ONCE, REUSED (@lru_cache)
       The first time you ask for a client, it's built. Every time after
       that, you get back the SAME one, instantly.

    2. BUILT ONLY WHEN NEEDED (lazy loading)
       Nothing connects when this file is imported. A client is built only
       when a function is called. So a lesson that only uses Azure OpenAI
       works even before Azure AI Search or Blob Storage exist.

WHERE EACH CLIENT IS USED
-------------------------
    openai_client()         build_index.py, search.py, topic.py, nodes.py, factual.py
    search_index_client()   build_index.py   (create the index)
    search_client()         build_index.py   (upload chunks), search.py (search)
    blob_container()        upload_pdfs.py, load_pdfs.py

HOW TO USE
----------
Never run directly. Other files do, for example:

    from azure_clients import openai_client
    openai_client().embeddings.create(...)

TRY IT YOURSELF (from the project folder)
-----------------------------------------
    python -c "from azure_clients import openai_client; print(openai_client() is openai_client())"

EXPECTED OUTPUT
---------------
    True
The same client came back both times: it was built once, and reused.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

from functools import lru_cache
# "lru_cache" REMEMBERS what a function returned.
# We put it above each function below, as "@lru_cache(maxsize=None)".
# The first call runs the function and builds the client. Every later call
# skips the function and hands back the client it remembered.
# So each connection is built only once per run.

import config
# Our settings file. It has already read .env, so from it we get every
# endpoint, key and name these clients need.
# We also use config.require(), which stops with a clear message if a
# setting is missing.


# ── AZURE OPENAI ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
# Build this client once, then hand back the same one on every later call.
# maxsize=None means: no limit on how much it remembers. We only ever have one.

def openai_client():
    # Gives back the client for Azure OpenAI.
    # One client does all three jobs: writing the answer, checking claims, and
    # turning text into embeddings. They're all on the same Foundry resource.
    """Azure OpenAI: chat answers, guardrail checks and embeddings."""
    # The function's one-line description.

    from openai import AzureOpenAI
    # The client from the "openai" library, made for Azure.
    # It has the same methods as the normal OpenAI client, like
    # .chat.completions.create() and .embeddings.create(). But it sends requests
    # to OUR Azure resource, not to OpenAI's public website.
    #
    # Why import it here, inside the function, and not at the top? So that
    # simply importing this file never needs the "openai" package. It's only
    # needed when someone actually asks for this client.

    config.require("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY")
    # Before connecting, check the two settings we can't do without.
    # If either is missing from .env, stop here with a clear message that names
    # it, instead of a confusing error from deep inside the library.

    return AzureOpenAI(
        # Build the client, and hand it back. Three settings tell it how to connect.

        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
        # WHERE: our Foundry resource's address,
        # e.g. https://annual-report-rag-test-resource.openai.azure.com/
        #
        # Important: only the address, ending at ".com/". The client adds the
        # rest of the path itself (which deployment, which version). If .env
        # already has "/openai/v1" on the end, the path is added twice, and
        # every call fails with "404 Resource not found".

        api_key=config.AZURE_OPENAI_API_KEY,
        # WHO: our key. It's sent with every request, to prove we're allowed in.

        api_version=config.AZURE_OPENAI_API_VERSION,
        # HOW: which version of Azure's calling format to use.
        # For gpt-5.1 we use 2025-04-01-preview, set in .env.
    )


# ── AZURE AI SEARCH, ADMIN SIDE ───────────────────────────────────────────────
# Azure AI Search has two kinds of client, for two different jobs:
#   SearchIndexClient   manages the INDEX itself: create it, delete it
#   SearchClient        works with the ROWS inside one index: upload, search
# One manages the table. The other manages what's in it.

@lru_cache(maxsize=None)
# Build once, reuse for the whole run.

def search_index_client():
    # Gives back the ADMIN-side client for Azure AI Search.
    # Only build_index.py uses it, to delete the old index and create a new one.
    """Azure AI Search, admin side: create and delete indexes."""
    # The function's one-line description.

    from azure.core.credentials import AzureKeyCredential
    # AzureKeyCredential wraps our key in the form the Azure libraries expect.
    # In simple words: we can't just pass the key as plain text. We put it inside
    # this small object, and the library sends it correctly with every request.

    from azure.search.documents.indexes import SearchIndexClient
    # The client that manages index DEFINITIONS: which indexes exist, and what
    # fields they have.

    config.require("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY")
    # Check the search address and key are in .env. If not, stop with a clear message.

    return SearchIndexClient(
        # Build the client, and hand it back.

        endpoint=config.AZURE_SEARCH_ENDPOINT,
        # WHERE: our search service's address,
        # e.g. https://annual-report-search-1.search.windows.net

        credential=AzureKeyCredential(config.AZURE_SEARCH_API_KEY),
        # WHO: our key, wrapped for the library.
        # This must be the ADMIN key. A read-only query key can't create or
        # delete an index, so build_index.py would fail with "403 Forbidden".
    )


# ── AZURE AI SEARCH, DATA SIDE ────────────────────────────────────────────────

@lru_cache(maxsize=None)
# Build once, reuse for the whole run.
# This matters most here: every question runs two searches (one per year),
# and both use this same client.

def search_client():
    # Gives back the DATA-side client for Azure AI Search, connected to our one index.
    # build_index.py uses it to upload chunks. search.py uses it to search.
    """Azure AI Search, data side: upload chunks and run queries against our index."""
    # The function's one-line description.

    from azure.core.credentials import AzureKeyCredential
    # The same key wrapper as above.

    from azure.search.documents import SearchClient
    # The client that reads and writes the DOCUMENTS (the rows) in one index.

    config.require("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY")
    # Check the search address and key are in .env.

    return SearchClient(
        # Build the client, and hand it back.

        endpoint=config.AZURE_SEARCH_ENDPOINT,
        # WHERE: our search service's address.

        index_name=config.AZURE_SEARCH_INDEX,
        # WHICH INDEX inside the service: "annual-reports".
        # Here, we can notice the difference from the admin client. That one
        # works with the whole service. This one is tied to ONE index.

        credential=AzureKeyCredential(config.AZURE_SEARCH_API_KEY),
        # WHO: our key.
        # On the laptop, the admin key (it can upload and search).
        # On the web app, a read-only query key is enough, because the web app
        # only searches.
    )


# ── BLOB STORAGE ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
# Build once, reuse for the whole run.

def blob_container():
    # Gives back our ONE container in Blob Storage: the one holding the four PDFs.
    # upload_pdfs.py uses it to upload. load_pdfs.py uses it to download.
    """Azure Blob Storage: the container that holds the annual report PDFs."""
    # The function's one-line description.

    from azure.storage.blob import BlobServiceClient
    # The client for a whole storage ACCOUNT.
    # Remember the structure: storage account → container → blob (file).
    # This client works at the top level, the account.

    config.require("AZURE_STORAGE_CONNECTION_STRING")
    # Check the connection string is in .env. If not, stop with a clear message.

    service = BlobServiceClient.from_connection_string(
        # Connect to the storage account, using the connection string.
        # Why a connection string, and not an address plus a key like the others?
        # Because Blob Storage gives us both in ONE line: the account name AND
        # its key. So one setting is enough.

        config.AZURE_STORAGE_CONNECTION_STRING
        # The connection string itself, from .env.
    )

    return service.get_container_client(config.AZURE_STORAGE_CONTAINER)
    # Now narrow down from the whole account to our ONE container,
    # e.g. "annual-reports". And hand that back.
    # So the files that use this never have to think about the account at all.
    # They just get the container, and upload or download.