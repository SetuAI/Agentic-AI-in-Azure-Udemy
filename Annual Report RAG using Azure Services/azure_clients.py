"""
================================================================================
AZURE CLIENTS - ONE PLACE THAT CONNECTS TO EACH SERVICE
================================================================================

Every other file asks this one for a client instead of building its own. That
gives us:

    one place where keys and endpoints are used
    one place to switch to passwordless login (Microsoft Entra ID) later
    one client per service, reused, instead of a new connection on every call

Each function builds its client the first time it is called and hands back the
same one afterwards. That pattern is called LAZY LOADING: importing this file
costs nothing, and a lesson that only uses Azure OpenAI never needs the Search
or Blob settings to exist.
================================================================================
"""

from functools import lru_cache             # lru_cache remembers a function's result, so each client is built once

import config                               # our settings: endpoints, keys, names


@lru_cache(maxsize=None)                    # build once, then return the same client on every later call
def openai_client():
    """Azure OpenAI: chat answers, guardrail checks and embeddings."""
    from openai import AzureOpenAI          # imported inside, so loading this file never requires the package

    config.require("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY")  # stop early if the settings are empty
    return AzureOpenAI(                     # the Azure flavour of the OpenAI client -- same methods, Azure address
        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,    # your resource's URL
        api_key=config.AZURE_OPENAI_API_KEY,            # your resource's key
        api_version=config.AZURE_OPENAI_API_VERSION,    # which version of the REST API to speak
    )


@lru_cache(maxsize=None)                    # one index-management client for the whole run
def search_index_client():
    """Azure AI Search, admin side: create and delete indexes."""
    from azure.core.credentials import AzureKeyCredential         # wraps a plain key so the SDK can send it
    from azure.search.documents.indexes import SearchIndexClient  # the client that manages index DEFINITIONS

    config.require("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY")  # stop early if the settings are empty
    return SearchIndexClient(                                        # connect to the search service
        endpoint=config.AZURE_SEARCH_ENDPOINT,                       # the service URL
        credential=AzureKeyCredential(config.AZURE_SEARCH_API_KEY),  # the admin key
    )


@lru_cache(maxsize=None)                    # one document client for the whole run
def search_client():
    """Azure AI Search, data side: upload chunks and run queries against our index."""
    from azure.core.credentials import AzureKeyCredential  # wraps a plain key
    from azure.search.documents import SearchClient        # the client that reads and writes DOCUMENTS

    config.require("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY")  # stop early if the settings are empty
    return SearchClient(                                             # connect to one specific index
        endpoint=config.AZURE_SEARCH_ENDPOINT,                       # the service URL
        index_name=config.AZURE_SEARCH_INDEX,                        # which index inside the service
        credential=AzureKeyCredential(config.AZURE_SEARCH_API_KEY),  # the key
    )


@lru_cache(maxsize=None)                    # one Blob container client for the whole run
def blob_container():
    """Azure Blob Storage: the container that holds the annual report PDFs."""
    from azure.storage.blob import BlobServiceClient     # the storage-account-level client

    config.require("AZURE_STORAGE_CONNECTION_STRING")    # stop early if the setting is empty
    service = BlobServiceClient.from_connection_string(  # connect using account name + key in one string
        config.AZURE_STORAGE_CONNECTION_STRING           # the string itself, from .env
    )
    return service.get_container_client(config.AZURE_STORAGE_CONTAINER)  # narrow down to our one container
