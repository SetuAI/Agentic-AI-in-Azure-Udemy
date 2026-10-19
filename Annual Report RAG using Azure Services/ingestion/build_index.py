"""
================================================================================
INGESTION STEP 3 - CHUNKS INTO AN AZURE AI SEARCH INDEX
================================================================================

WHAT THIS FILE DOES
-------------------
    1. creates the index -- the "table" in Azure AI Search that holds our chunks
    2. turns every chunk into an embedding (a list of 1,536 numbers) with Azure OpenAI
    3. uploads chunk text + labels + embedding into the index

WHAT AN INDEX DEFINITION SAYS
-----------------------------
Before anything is uploaded, Azure needs to know the shape of each document and
what it may do with each field:

    id            the unique key of each chunk
    text          SEARCHABLE   -- keyword search reads it
    company       FILTERABLE   -- "only Infosys"
    fiscal_year   FILTERABLE   -- "only FY2025": the filter this project depends on
    embedding     a VECTOR     -- similarity search compares these numbers

It also holds two pieces of setup:

    vector search      HOW to search the vectors (HNSW: a fast approximate method)
    semantic ranker    WHICH field Azure's reranking model should read (text)

HOW TO RUN
----------
    python ingestion/build_index.py      (or run_ingestion.py, which does all steps)
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths
from typing import Dict, List                   # type hints

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # index name, embedding size, deployment names
from azure_clients import openai_client, search_client, search_index_client  # the Azure services used here

EMBEDDING_BATCH_SIZE = 100                      # chunks embedded per API call: fewer calls, well inside request limits
UPLOAD_BATCH_SIZE = 500                         # chunks uploaded per call: Azure accepts up to 1,000 per batch


# ==============================================================================
# SECTION 1 - THE INDEX DEFINITION
# ==============================================================================

def build_index_definition():
    """Describe the index: its fields, how vectors are searched, and the semantic ranker setup."""
    from azure.search.documents.indexes.models import (  # the building blocks of an index definition
        HnswAlgorithmConfiguration,             # the vector search method
        SearchableField,                        # a text field keyword search can read
        SearchField,                            # a general field -- we use it for the vector
        SearchFieldDataType,                    # the data types Azure understands
        SearchIndex,                            # the index itself
        SemanticConfiguration,                  # the semantic ranker's setup
        SemanticField,                          # one field the semantic ranker reads
        SemanticPrioritizedFields,              # which fields matter most to the semantic ranker
        SemanticSearch,                         # the container for semantic configurations
        SimpleField,                            # a plain field: filter, sort or return, but not search
        VectorSearch,                           # the container for vector settings
        VectorSearchProfile,                    # ties a vector field to an algorithm
    )

    fields = [                                  # every field each chunk will have
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),          # unique key, e.g. Infosys_FY2025_42
        SearchableField(name="text", type=SearchFieldDataType.String),              # the chunk text, keyword-searchable
        SimpleField(name="company", type=SearchFieldDataType.String, filterable=True),      # filter by company
        SimpleField(name="fiscal_year", type=SearchFieldDataType.String, filterable=True),  # filter by year
        SimpleField(name="year_label", type=SearchFieldDataType.String),            # returned for display only
        SimpleField(name="source_file", type=SearchFieldDataType.String),           # returned for display only
        SimpleField(name="position", type=SearchFieldDataType.Int32),               # where in the report it sits
        SearchField(                                                                # the embedding
            name="embedding",                                                       # field name
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),        # a list of decimal numbers
            searchable=True,                                                        # vectors must be searchable
            vector_search_dimensions=config.EMBEDDING_DIMENSIONS,                   # exactly 1,536 numbers each
            vector_search_profile_name="default-vector-profile",                    # use the profile defined below
        ),
    ]

    vector_search = VectorSearch(               # how vector similarity search runs
        algorithms=[HnswAlgorithmConfiguration(name="default-hnsw")],  # HNSW: fast, approximate nearest neighbours
        profiles=[VectorSearchProfile(          # a profile links a vector field to an algorithm
            name="default-vector-profile",      # the name the embedding field refers to
            algorithm_configuration_name="default-hnsw",  # use the HNSW setup above
        )],
    )

    semantic_search = SemanticSearch(           # Azure's built-in reranking model
        configurations=[SemanticConfiguration(  # one named setup
            name=config.SEMANTIC_CONFIG_NAME,   # the name queries will ask for
            prioritized_fields=SemanticPrioritizedFields(  # tell the ranker what to read
                content_fields=[SemanticField(field_name="text")],  # read the chunk text
            ),
        )],
    )

    return SearchIndex(                         # put it all together
        name=config.AZURE_SEARCH_INDEX,         # e.g. "annual-reports"
        fields=fields,                          # the fields above
        vector_search=vector_search,            # the vector setup above
        semantic_search=semantic_search,        # the semantic ranker setup above
    )


def create_index() -> None:
    """Delete any old version of the index, then create it fresh."""
    index_client = search_index_client()        # the admin-side client

    if config.AZURE_SEARCH_INDEX in index_client.list_index_names():  # an old index from a previous run?
        index_client.delete_index(config.AZURE_SEARCH_INDEX)         # remove it, so no stale chunks remain

    index_client.create_index(build_index_definition())  # create the new, empty index


# ==============================================================================
# SECTION 2 - EMBEDDINGS
# ==============================================================================

def embed_texts(texts: List[str], verbose: bool = True) -> List[List[float]]:
    """Turn a list of texts into embeddings with Azure OpenAI, in batches."""
    embeddings = []                             # results collect here, in the same order as texts
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):  # 0, 100, 200, ... one batch at a time
        batch = texts[start:start + EMBEDDING_BATCH_SIZE]     # the next 100 texts
        response = openai_client().embeddings.create(        # one API call for the whole batch
            model=config.EMBEDDING_DEPLOYMENT,  # on Azure this is the DEPLOYMENT name, not the model name
            input=batch,                        # the texts to embed
        )
        embeddings.extend(item.embedding for item in response.data)  # one list of 1,536 numbers per text
        if verbose:                             # show progress -- this step takes a while
            print(f"    embedded {min(start + EMBEDDING_BATCH_SIZE, len(texts)):>6,} / {len(texts):,}")
    return embeddings                           # same length and order as texts


# ==============================================================================
# SECTION 3 - UPLOADING
# ==============================================================================

def to_search_document(chunk: Dict, embedding: List[float]) -> Dict:
    """Combine one chunk and its embedding into the shape the index expects."""
    return {                                    # every key must match a field in the index definition
        "id": f"{chunk['company']}_{chunk['fiscal_year']}_{chunk['position']}",  # unique, and only letters/digits/_
        "text": chunk["text"],                  # the content
        "company": chunk["company"],            # filterable label
        "fiscal_year": chunk["fiscal_year"],    # filterable label
        "year_label": chunk["year_label"],      # display label
        "source_file": chunk["source_file"],    # display label
        "position": chunk["position"],          # place in the report
        "embedding": embedding,                 # the 1,536 numbers
    }


def build_index(chunks: List[Dict], verbose: bool = True) -> int:
    """Create the index and fill it with every chunk. Returns how many were uploaded."""
    if verbose:                                 # announce each stage
        print(f"  creating index '{config.AZURE_SEARCH_INDEX}'")
    create_index()                              # a fresh, empty index

    if verbose:                                 # announce the slow part
        print(f"  embedding {len(chunks):,} chunks with '{config.EMBEDDING_DEPLOYMENT}'")
    embeddings = embed_texts([c["text"] for c in chunks], verbose)  # one embedding per chunk

    documents = [to_search_document(c, e) for c, e in zip(chunks, embeddings)]  # pair each chunk with its embedding

    uploaded = 0                                # running count of successful uploads
    for start in range(0, len(documents), UPLOAD_BATCH_SIZE):  # 500 documents per call
        batch = documents[start:start + UPLOAD_BATCH_SIZE]     # the next 500
        results = search_client().upload_documents(documents=batch)  # send them to the index
        uploaded += sum(1 for r in results if r.succeeded)     # count only the ones Azure accepted
    if verbose:                                 # final count
        print(f"  uploaded {uploaded:,} of {len(documents):,} chunks")
    return uploaded                             # the caller can check nothing was lost


if __name__ == "__main__":                      # only when run directly
    from ingestion.chunk import chunk_all_documents  # earlier steps, imported only for this demo
    from ingestion.load_pdfs import load_all_pdfs    # (uses the markdown cache if present)

    build_index(chunk_all_documents(load_all_pdfs()))  # load -> chunk -> embed -> upload
