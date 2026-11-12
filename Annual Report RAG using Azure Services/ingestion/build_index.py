"""
================================================================================
ingestion/build_index.py  -  PUT EVERY CHUNK INTO AZURE AI SEARCH
================================================================================

PURPOSE
-------
This file takes all our chunks and puts them into Azure AI Search, so they can
be searched. It does three jobs, in this order:

    JOB 1  CREATE the index    an empty table in Azure AI Search, with our columns
    JOB 2  EMBED every chunk   turn each chunk's text into 512 numbers (Azure OpenAI)
    JOB 3  UPLOAD every chunk  its text, its labels and its 512 numbers, as one row

WHAT PROBLEM DOES IT SOLVE
--------------------------
After chunk.py, we have 5,154 chunks. But they only exist in the laptop's
memory while the program runs. And the only way to find one is to read all
5,154, one by one.

We need them stored in one place, where a question can find the right ones in
a fraction of a second, both by their exact WORDS and by their MEANING.

So, we put them in an INDEX in Azure AI Search.

WHAT AN INDEX IS
----------------
An INDEX is a table inside Azure AI Search.
    Every ROW is one chunk.
    Every COLUMN is one piece of information about the chunk.
In Azure AI Search, a column is called a FIELD, and a row is called a DOCUMENT.

If you have used Chroma before, an index does the same job as a COLLECTION.
But it has three differences:
    1. Its fields are fixed up front. We declare every field, its type, and
       what it's allowed to do, BEFORE anything is uploaded.
    2. It searches the exact words of the text as well as the meaning.
       That's what makes hybrid search possible.
    3. It holds the settings for the semantic ranker.

THE EIGHT FIELDS (COLUMNS) OF OUR INDEX
---------------------------------------
    field         example                 kind of field     what it can do
    -----------   ---------------------   ---------------   ----------------------------
    id            Infosys_FY2025_42       SimpleField       the row's unique name (key)
    text          "Revenue grew ..."      SearchableField   keyword search reads it
    company       Infosys                 SimpleField       filter: only this company
    fiscal_year   FY2025                  SimpleField       filter: only this year
    year_label    FY 2024-25              SimpleField       just stored, shown on the page
    source_file   infosys_fy2025.pdf      SimpleField       just stored, shown on the page
    position      42                      SimpleField       just stored
    embedding     [0.012, -0.034, ...]    SearchField       vector search compares it

THE THREE KINDS OF FIELD
------------------------
    SimpleField       a plain column. It stores a value, and can be used to
                      FILTER ("only FY2025"). It is NOT searched word by word.
    SearchableField   a text column that keyword search reads WORD BY WORD.
                      Searching "attrition" finds rows whose text contains it.
    SearchField       the general version, where we set every option
                      ourselves. We need it for the embedding, because a
                      list of 512 numbers needs extra settings.

WHERE IT FITS
-------------
    upload_pdfs.py     laptop → Blob Storage
    load_pdfs.py       Blob Storage → markdown
    chunk.py           markdown → chunks
    build_index.py   ← YOU ARE HERE: chunks → embeddings → Azure AI Search

WHAT IT NEEDS IN .env
---------------------
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_VERSION
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT     e.g. text-embedding-3-small
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY   (the ADMIN key: we create the index)
    AZURE_SEARCH_INDEX                    e.g. annual-reports

HOW TO RUN (from the project folder)
------------------------------------
    python ingestion/build_index.py       runs load → chunk → this file
    python run_ingestion.py               the same, with a heading for each step

EXPECTED OUTPUT
---------------
      creating index 'annual-reports'
      embedding 5,154 chunks with 'text-embedding-3-small'
        embedded    100 / 5,154
        embedded    200 / 5,154
        ...
        embedded  5,154 / 5,154
      uploaded 5,154 of 5,154 chunks
Then, in the portal: Indexes → annual-reports → Document count = 5,154.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import Dict, List
# These are TYPE HINTS. They only DESCRIBE what goes into a function and what
# comes out. Python doesn't check them. They're there to make the code easier
# to read.
#   Dict          a dictionary, like one chunk:  {"text": "...", "company": "Infosys"}
#   List          a list, like all the chunks:   [chunk1, chunk2, ...]
#   List[float]   a list of decimal numbers, like one embedding: [0.012, -0.034, ...]

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the ingestion/ folder. But config.py and
# azure_clients.py are one folder ABOVE it. Python won't find them on its own.
# So we add that folder (the project folder) to the front of sys.path.
#   Path(__file__)   this file
#   .parent          its folder (ingestion/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. From it we use:
#   config.AZURE_SEARCH_INDEX       the index name: "annual-reports"
#   config.EMBEDDING_DEPLOYMENT     the embedding deployment: "text-embedding-3-small"
#   config.EMBEDDING_DIMENSIONS     512: how many numbers in each embedding
#   config.SEMANTIC_CONFIG_NAME     "annual-reports-semantic": the name of the
#                                   semantic ranker's settings

from azure_clients import openai_client, search_client, search_index_client
# Three ready-made connections, from azure_clients.py:
#   openai_client()         Azure OpenAI: turns text into embeddings
#   search_index_client()   Azure AI Search, ADMIN side: creates and deletes the INDEX
#   search_client()         Azure AI Search, DATA side: uploads ROWS into the index
#
# Why two connections for Azure AI Search? Because they do two different jobs:
#   one builds the TABLE (the index and its columns),
#   the other fills the table with ROWS (our chunks).

EMBEDDING_BATCH_SIZE = 100
# How many chunks we send to Azure OpenAI in ONE call, to be embedded.
#
# We have two extreme options, and both are bad:
#   1 chunk per call      →  5,154 calls. Very slow.
#   all 5,154 in one call →  too big. One call has a size limit, so it fails.
# So we take the middle: 100 chunks per call. That's 52 calls in total.
#
# A BATCH is simply a group of items sent together in one go.

UPLOAD_BATCH_SIZE = 500
# How many rows we upload to Azure AI Search in ONE call.
# Azure accepts up to 1,000 rows per call. 500 keeps us comfortably below that.
# So 5,154 rows go up in 11 calls.


# ==============================================================================
# SECTION 1 - THE INDEX DEFINITION
# ==============================================================================
# Before we can upload a single row, Azure AI Search needs to know the SHAPE of
# the table:
#   - which columns (fields) it has
#   - what TYPE of value each one holds: text, a whole number, or a list of numbers
#   - what each one is ALLOWED to do: be searched, be filtered, or just be stored
# This description is called the INDEX DEFINITION.
#
# Think of the difference from Chroma: in Chroma you can add any label to any
# chunk at any time. Here, every column is declared first, and every row must
# fit those columns.

def build_index_definition():
    # This function BUILDS the description of our index, and gives it back.
    # It doesn't talk to Azure. It only prepares the description.
    # create_index(), further down, sends it to Azure.
    """Describe the index: its fields, how vectors are searched, and the semantic ranker setup."""
    # The function's one-line description.

    from azure.search.documents.indexes.models import (
        # These are the BUILDING BLOCKS for describing an index. Each one is a
        # Python class: a template we fill in to describe one piece of the index.
        #
        # Why are they imported here, inside the function, and not at the top
        # of the file? Because they're only needed while we build the
        # description. Importing them here keeps the top of the file light.

        HnswAlgorithmConfiguration,
        # The settings for HNSW: the method Azure uses to search the 512
        # numbers quickly. Explained in detail further down.

        SearchableField,
        # A TEXT column that keyword search reads WORD BY WORD.
        # When you search for "attrition", Azure looks inside this column for
        # that word. We use it for "text", the chunk itself.

        SearchField,
        # The GENERAL kind of column, where we set every option ourselves.
        # SimpleField and SearchableField are really just shortcuts for common
        # cases. The embedding is not a common case: it's a list of 512 numbers,
        # and it needs extra settings. So we use this general version for it.

        SearchFieldDataType,
        # The list of TYPES a column can hold. We use three:
        #   SearchFieldDataType.String   text, like "Infosys"
        #   SearchFieldDataType.Int32    a whole number, like 42
        #   SearchFieldDataType.Single   a decimal number, like 0.012
        # (Int32 means a whole number stored in 32 bits. Single means a decimal
        #  number stored in a compact form. You don't need to know more than that.)

        SearchIndex,
        # The INDEX itself. It holds everything else: the name, the columns,
        # the vector settings and the semantic ranker settings.

        SemanticConfiguration,
        # One named group of settings for the SEMANTIC RANKER.

        SemanticField,
        # Points the semantic ranker at ONE column it should read.

        SemanticPrioritizedFields,
        # The list of columns the semantic ranker should read, in order of importance.

        SemanticSearch,
        # Holds all the semantic ranker settings for this index.

        SimpleField,
        # A PLAIN column. It stores a value. It can be used to FILTER (for
        # example "only rows where company is Infosys"), and it's returned with
        # the results so we can show it. But it is NOT searched word by word.
        #
        # So what's the difference from SearchableField?
        #   SimpleField       "show me rows where company IS EXACTLY Infosys"
        #   SearchableField   "show me rows whose text CONTAINS the word attrition"

        VectorSearch,
        # Holds all the settings for VECTOR SEARCH: how the 512 numbers are searched.

        VectorSearchProfile,
        # A PROFILE connects a vector column to a search method.
        # The embedding column says which profile it uses; the profile says
        # which method (HNSW) to run.
    )

    fields = [
        # Now we list the COLUMNS of our table, one by one. Eight in total.
        # Every chunk we upload will have exactly these eight values.

        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        # COLUMN 1 · "id"
        #   SimpleField     a plain column: stored, not searched word by word
        #   name="id"       its name
        #   type=String     it holds text, e.g. "Infosys_FY2025_42"
        #   key=True        this is the KEY: the unique name of each row.
        #
        # What does KEY mean? Every index needs exactly ONE key column. No two
        # rows can have the same key. It's how Azure tells rows apart.
        # If we upload a row whose key already exists, the new row REPLACES
        # the old one. It never creates a duplicate.

        SearchableField(name="text", type=SearchFieldDataType.String),
        # COLUMN 2 · "text"
        #   SearchableField   a text column that keyword search reads word by word
        #   name="text"       its name
        #   type=String       it holds text: the chunk itself, about 1,200 characters
        #
        # This is the column keyword search looks inside. When a question has
        # the word "revenue", Azure finds the rows whose text contains "revenue".
        # The semantic ranker reads this column too.

        SimpleField(name="company", type=SearchFieldDataType.String, filterable=True),
        # COLUMN 3 · "company"
        #   SimpleField       a plain column
        #   type=String       it holds text, e.g. "Infosys"
        #   filterable=True   it can be used as a FILTER
        #
        # What does FILTERABLE mean? A filter narrows the search down BEFORE
        # anything is scored. With this, a search can say "look only at rows
        # where company is Infosys", and Eternal's rows are ignored completely.
        # Without filterable=True, Azure would refuse that filter.

        SimpleField(name="fiscal_year", type=SearchFieldDataType.String, filterable=True),
        # COLUMN 4 · "fiscal_year"
        #   SimpleField       a plain column
        #   type=String       it holds text, e.g. "FY2025"
        #   filterable=True   it can be used as a FILTER
        #
        # This is the most important setting in the whole index.
        # The FY2025 and FY2026 reports use almost the same words. Searching by
        # words or meaning alone can't tell them apart. But with this filter,
        # a search can say "look only at FY2025 rows". That's how every search
        # in this project keeps the two years apart.

        SimpleField(name="year_label", type=SearchFieldDataType.String),
        # COLUMN 5 · "year_label"
        #   a plain text column, e.g. "FY 2024-25"
        #   No filterable, no searching. It's just STORED, and sent back with
        #   each result, so the web page can show it next to the chunk.

        SimpleField(name="source_file", type=SearchFieldDataType.String),
        # COLUMN 6 · "source_file"
        #   a plain text column, e.g. "infosys_fy2025.pdf"
        #   Just stored, and shown on the page: which PDF this chunk came from.

        SimpleField(name="position", type=SearchFieldDataType.Int32),
        # COLUMN 7 · "position"
        #   a plain column holding a WHOLE NUMBER (Int32), e.g. 42
        #   Just stored: where in the report this chunk sits.
        #   It's a number, not text, because it IS a number.

        SearchField(
            # COLUMN 8 · "embedding": the chunk's 512 numbers.
            #
            # This is the column VECTOR SEARCH compares. It's not text, and
            # it's not one number. It's a LIST of 512 numbers. So it needs
            # settings that SimpleField and SearchableField don't have. That's
            # why we use the general SearchField, and set each option ourselves.

            name="embedding",
            # Its name.

            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            # Its type, read from the inside out:
            #   SearchFieldDataType.Single    one decimal number, like 0.012
            #   Collection(...)               a LIST of those numbers
            # So: a list of decimal numbers, like [0.012, -0.034, 0.051, ...]

            searchable=True,
            # It can be searched. For a vector column, that means vector
            # search is allowed to compare it with the question's 512 numbers.

            vector_search_dimensions=config.EMBEDDING_DIMENSIONS,
            # How many numbers EVERY embedding in this column must have: 512.
            # A DIMENSION is one number in the list. So 512 dimensions = 512 numbers.
            # If we try to upload an embedding with 1,536 numbers, Azure rejects it.

            vector_search_profile_name="default-vector-profile",
            # Which vector search settings to use for this column.
            # It's just a name here. We create the settings with this name a
            # few lines below, in "vector_search".
        ),
    ]

    vector_search = VectorSearch(
        # Now, the settings for VECTOR SEARCH: HOW the 512 numbers are searched.
        #
        # So, we have a problem. To find the chunks closest in meaning to a
        # question, the simple way is to compare the question's 512 numbers
        # with EVERY chunk's 512 numbers, one by one. That's 5,154 comparisons
        # for every search. It works, but it gets slower as the index grows.

        algorithms=[HnswAlgorithmConfiguration(name="default-hnsw")],
        # So we use HNSW.
        #
        # HNSW is a method that finds the closest vectors quickly, WITHOUT
        # comparing the question with every single chunk. When the index is
        # built, Azure links each chunk to its nearest neighbours. At search
        # time, it follows those links towards the closest chunks, and skips
        # most of the others.
        #
        # The trade-off: it's APPROXIMATE. Very rarely, it can miss a chunk
        # that a full comparison would have found. For search, that's a
        # good trade: much faster, almost always the same results.
        #
        # We give these settings a name: "default-hnsw".
        # (HNSW stands for Hierarchical Navigable Small World. You only need
        #  to remember what it does, not the name.)

        profiles=[VectorSearchProfile(
            # Now, the PROFILE. A profile connects a vector column to a method.
            # Remember, the embedding column above asked for a profile called
            # "default-vector-profile". This is where we create it.

            name="default-vector-profile",
            # The profile's name: the same name the embedding column asked for.

            algorithm_configuration_name="default-hnsw",
            # The method this profile uses: the "default-hnsw" settings we just named.
            #
            # So the chain is:
            #   embedding column → "default-vector-profile" → "default-hnsw" → HNSW
        )],
    )

    semantic_search = SemanticSearch(
        # Now, the settings for the SEMANTIC RANKER.
        #
        # The semantic ranker is Azure's own reranker. After hybrid search
        # finds the best 20 chunks, the ranker reads the QUESTION and EACH
        # CHUNK TOGETHER, and gives each chunk a score from 0 to 4:
        #   4 = answers the question completely,  0 = irrelevant.
        # We then keep the best 5.
        #
        # But the ranker needs to know one thing: which column to READ.

        configurations=[SemanticConfiguration(
            # One named group of ranker settings.

            name=config.SEMANTIC_CONFIG_NAME,
            # Its name: "annual-reports-semantic".
            # It's just a label we chose. What matters is that every search
            # asks for the ranker using this SAME name. retrieval/search.py does.

            prioritized_fields=SemanticPrioritizedFields(
                # The columns the ranker should read, in order of importance.

                content_fields=[SemanticField(field_name="text")],
                # Only ONE column: "text", the chunk itself.
                # The ranker never reads the company, the year or the 512 numbers.
                # Only the text has sentences worth judging.
            ),
        )],
    )

    return SearchIndex(
        # Finally, put all the pieces together into ONE index description,
        # and hand it back.

        name=config.AZURE_SEARCH_INDEX,
        # The index's name: "annual-reports".

        fields=fields,
        # The eight columns.

        vector_search=vector_search,
        # How to search the 512 numbers: HNSW.

        semantic_search=semantic_search,
        # The semantic ranker's settings: read the "text" column.
    )


def create_index() -> None:
    # This function CREATES the index in Azure AI Search, starting fresh every time.
    # "-> None" means it doesn't give anything back. It just does the work.
    """Delete any old version of the index, then create it fresh."""
    # The function's one-line description.

    index_client = search_index_client()
    # Connect to Azure AI Search on the ADMIN side, because we're creating an index.
    # This needs the ADMIN key from .env. A read-only query key can't create indexes.

    if config.AZURE_SEARCH_INDEX in index_client.list_index_names():
        # Let's read this:
        #   index_client.list_index_names()   ask Azure for the names of all
        #                                     indexes in our search service
        #   config.AZURE_SEARCH_INDEX in ...  is "annual-reports" one of them?
        # In other words: is there already an index from an earlier run?

        index_client.delete_index(config.AZURE_SEARCH_INDEX)
        # Yes? Then DELETE it first.
        #
        # Why not just add to it? Imagine we changed the chunk size from 1,200
        # to 800. The new run makes different chunks. If we kept the old index,
        # it would hold the old 1,200-character chunks AND the new ones, mixed
        # together. Starting from an empty index means it holds exactly what
        # THIS run produces, and nothing else.

    index_client.create_index(build_index_definition())
    # Now create the index. Read it from the inside out:
    #   build_index_definition()        build the description (Section 1)
    #   index_client.create_index(...)  send it to Azure, which creates the index
    # After this line, the index exists, with all eight columns. But it's
    # EMPTY: no rows yet.


# ==============================================================================
# SECTION 2 - EMBEDDINGS
# ==============================================================================
# An EMBEDDING is a list of numbers that represents the MEANING of a piece of
# text.
#
# The key idea: two pieces of text that MEAN the same thing get SIMILAR
# numbers, even if they use completely different words.
#   "How many people left the company?"     →  [0.21, -0.08, 0.33, ...]
#   "Attrition for the year was ..."        →  [0.19, -0.07, 0.35, ...]   close!
#   "Revenue grew in the quarter ..."       →  [-0.40, 0.52, 0.02, ...]   far
# That's how vector search finds the attrition chunk, even though the question
# never used the word "attrition".

def embed_texts(texts: List[str], verbose: bool = True) -> List[List[float]]:
    # Takes a list of texts. Gives back one embedding for each text, in the SAME order.
    #   texts:     List[str]           a list of texts, e.g. 5,154 chunk texts
    #   verbose:   bool = True         print progress? Yes, unless told otherwise
    #   returns:   List[List[float]]   a list of embeddings, where each embedding
    #                                  is itself a list of 512 decimal numbers
    """Turn a list of texts into embeddings with Azure OpenAI, in batches."""
    # The function's one-line description.

    embeddings = []
    # An empty list. Each embedding will be added here, in the same order as the texts.

    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        # Go through the texts in BATCHES of 100.
        #
        # range(0, len(texts), 100) counts from 0 up to the number of texts,
        # jumping by 100 each time. With 5,154 texts:
        #   0, 100, 200, 300, ... 5000, 5100
        # Each number is where the next batch STARTS.

        batch = texts[start:start + EMBEDDING_BATCH_SIZE]
        # Take the next 100 texts.
        # texts[a:b] is called SLICING: it takes the items from position a up
        # to (but not including) position b.
        #   start = 0    →  texts[0:100]      the first 100
        #   start = 100  →  texts[100:200]    the next 100
        #   start = 5100 →  texts[5100:5200]  only 54 are left, so we get those 54
        # Slicing past the end is fine in Python: it just stops at the last item.

        response = openai_client().embeddings.create(
            # Send this batch of 100 texts to Azure OpenAI, in ONE call.
            # Azure turns each text into 512 numbers, and sends them all back.

            model=config.EMBEDDING_DEPLOYMENT,
            # Which model to use. On Azure, this is our DEPLOYMENT NAME from
            # Foundry: "text-embedding-3-small". It must match the name in the
            # portal exactly, or Azure answers "404 Resource not found".

            input=batch,
            # The 100 texts to turn into numbers.

            dimensions=config.EMBEDDING_DIMENSIONS,
            # How many numbers we want per text: 512.
            #
            # This model normally gives 1,536 numbers. But it lets us ask for
            # fewer. We ask for 512:
            #   advantage:     the index is about 3 times smaller, and searches are faster
            #   disadvantage:  a small loss in accuracy
            # It MUST be 512, because the embedding column (Section 1) only
            # accepts exactly 512.
        )

        embeddings.extend(item.embedding for item in response.data)
        # Now take the numbers out of Azure's reply, and add them to our list.
        #   response.data                   the list of results, one per text,
        #                                   in the same order we sent them
        #   item.embedding                  the 512 numbers of one result
        #   item.embedding for item in ...  go through every result, and take
        #                                   its 512 numbers
        #   embeddings.extend(...)          add each one to our list
        #
        # extend vs append:
        #   append would add the whole batch as ONE item (a list inside a list).
        #   extend adds each embedding SEPARATELY. That's what we want.

        if verbose:
            # If we were asked to print progress...

            print(f"    embedded {min(start + EMBEDDING_BATCH_SIZE, len(texts)):>6,} / {len(texts):,}")
            # ...print how many we've done so far, e.g. "embedded    300 / 5,154".
            #
            # The parts:
            #   start + EMBEDDING_BATCH_SIZE   how many we've done after this batch
            #   min(..., len(texts))           but never more than the total.
            #                                  So the last line says 5,154, not 5,200.
            #   :>6,                           right-align in 6 characters, with commas
            #
            # Embedding is the slowest part of ingestion, so it's good to watch it move.

    return embeddings
    # Hand back all the embeddings: one per text, in the same order as the texts.


# ==============================================================================
# SECTION 3 - UPLOADING
# ==============================================================================
# Now we have two lists of the same length, in the same order:
#   chunks       5,154 chunks (text + labels), from chunk.py
#   embeddings   5,154 embeddings (512 numbers each), from Section 2
# We join each chunk with its own embedding into ONE row, and upload the rows.

def to_search_document(chunk: Dict, embedding: List[float]) -> Dict:
    # Takes ONE chunk and its embedding. Gives back ONE row, in the exact shape
    # the index expects.
    # In Azure AI Search, a row is called a DOCUMENT. That's why the function
    # is called "to_search_document".
    """Combine one chunk and its embedding into the shape the index expects."""
    # The function's one-line description.

    return {
        # One row, as a dictionary.
        # Here, we can notice: every KEY in this dictionary is one of the eight
        # column names from Section 1. They must match EXACTLY. If we sent a
        # key the index doesn't have, Azure would reject the row.

        "id": f"{chunk['company']}_{chunk['fiscal_year']}_{chunk['position']}",
        # COLUMN 1 · the row's unique key, built from three labels.
        #   "Infosys" + "_" + "FY2025" + "_" + "42"   →   "Infosys_FY2025_42"
        #
        # Why these three? Because together they're always unique: one company,
        # one year, one position in that report.
        # And why "_" between them? Because Azure only allows letters, digits,
        # "_", "-" and "=" in a key. No spaces.

        "text": chunk["text"],
        # COLUMN 2 · the chunk itself.

        "company": chunk["company"],
        # COLUMN 3 · e.g. "Infosys". Used as a filter.

        "fiscal_year": chunk["fiscal_year"],
        # COLUMN 4 · e.g. "FY2025". Used as a filter.

        "year_label": chunk["year_label"],
        # COLUMN 5 · e.g. "FY 2024-25". Shown on the page.

        "source_file": chunk["source_file"],
        # COLUMN 6 · e.g. "infosys_fy2025.pdf". Shown on the page.

        "position": chunk["position"],
        # COLUMN 7 · e.g. 42.

        "embedding": embedding,
        # COLUMN 8 · this chunk's 512 numbers, from Section 2.
    }


def build_index(chunks: List[Dict], verbose: bool = True) -> int:
    # The MAIN function of this file. It runs the three jobs in order:
    #   Job 1: create the index   Job 2: embed every chunk   Job 3: upload every row
    # Takes all the chunks. Gives back how many rows Azure accepted, so we can
    # check nothing was lost.
    """Create the index and fill it with every chunk. Returns how many were uploaded."""
    # The function's one-line description.

    if verbose:
        # Print each stage, so we can see where we are.

        print(f"  creating index '{config.AZURE_SEARCH_INDEX}'")
        # e.g. "creating index 'annual-reports'"

    create_index()
    # JOB 1 · create a fresh, empty index, with our eight columns.

    if verbose:
        # Announce the slow part.

        print(f"  embedding {len(chunks):,} chunks with '{config.EMBEDDING_DEPLOYMENT}'")
        # e.g. "embedding 5,154 chunks with 'text-embedding-3-small'"
        # len(chunks):,  means the number of chunks, written with commas.

    embeddings = embed_texts([c["text"] for c in chunks], verbose)
    # JOB 2 · turn every chunk into 512 numbers.
    #
    # [c["text"] for c in chunks] makes a new list with ONLY the text of each
    # chunk. This short form is called a LIST COMPREHENSION. It means:
    #   "for each chunk c in chunks, take c["text"]"
    # Why only the text? Because the embedding model turns TEXT into numbers.
    # It doesn't need the company or the year.

    documents = [to_search_document(c, e) for c, e in zip(chunks, embeddings)]
    # Now join each chunk with its OWN embedding, into one row.
    #
    # zip(chunks, embeddings) walks through both lists side by side:
    #   (chunk 1, embedding 1), (chunk 2, embedding 2), (chunk 3, embedding 3), ...
    # For each pair, to_search_document(c, e) builds one row.
    #
    # This is exactly why embed_texts keeps the same ORDER as the texts. If
    # the order were mixed up, a chunk about revenue could get the embedding
    # of a chunk about attrition.

    uploaded = 0
    # A COUNTER: how many rows Azure has accepted so far. It starts at zero.

    for start in range(0, len(documents), UPLOAD_BATCH_SIZE):
        # JOB 3 · upload the rows, 500 at a time.
        # range(0, 5154, 500) gives:  0, 500, 1000, ... 5000.  That's 11 batches.

        batch = documents[start:start + UPLOAD_BATCH_SIZE]
        # Take the next 500 rows (slicing, the same as in Section 2).
        # The last batch has only the remaining 154.

        results = search_client().upload_documents(documents=batch)
        # Send these 500 rows to the index, using the DATA-side client.
        # Azure stores them, and sends back ONE result per row, saying whether
        # that row was accepted.

        uploaded += sum(1 for r in results if r.succeeded)
        # Count the rows Azure accepted in this batch, and add them to our counter.
        #
        # Let's read it from the inside out:
        #   for r in results          go through each row's result
        #   if r.succeeded            keep only the ones Azure accepted
        #   1 for ...                 count 1 for each of those
        #   sum(...)                  add them all up
        #   uploaded += ...           add that to our running total
        #
        # Why not just count the rows we SENT? Because a row can be rejected,
        # for example if its embedding had the wrong number of values. Counting
        # only the accepted ones tells us the truth.

    if verbose:
        # Print the final count.

        print(f"  uploaded {uploaded:,} of {len(documents):,} chunks")
        # e.g. "uploaded 5,154 of 5,154 chunks".
        # If the two numbers are DIFFERENT, some rows were rejected. In our run
        # they're the same, so every chunk made it into the index.

    return uploaded
    # Hand back how many rows were accepted.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python ingestion/build_index.py
    # If another file imports this one (run_ingestion.py does), nothing below runs.

    from ingestion.chunk import chunk_all_documents
    # The previous step: cutting the reports into chunks.

    from ingestion.load_pdfs import load_all_pdfs
    # The step before that: reading the reports as markdown.
    # Both are imported here, not at the top, because only this demo needs them.

    build_index(chunk_all_documents(load_all_pdfs()))
    # The whole of ingestion in one line. Let's read it from the inside out:
    #   load_all_pdfs()             read the four reports as markdown (from the saved files)
    #   chunk_all_documents(...)    cut them into 5,154 chunks
    #   build_index(...)            create the index, embed every chunk, upload every row