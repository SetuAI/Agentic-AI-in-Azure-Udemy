"""
================================================================================
BUILD THE SEARCH INDEX - run once, after uploading the PDFs
================================================================================

    Blob Storage --download--> Document Intelligence --markdown--> chunks
                                                                      |
                     Azure AI Search  <--upload--  Azure OpenAI embeddings

Before this, run:  python ingestion/upload_pdfs.py

HOW TO RUN
----------
    python run_ingestion.py
================================================================================
"""

import sys                                      # sys.path lets us import our own folders
import time                                     # time how long the whole thing takes
from pathlib import Path                        # Path builds file paths

sys.path.insert(0, str(Path(__file__).parent))  # make azure_based/ importable

import config                                   # settings, for the printout
from ingestion.build_index import build_index   # step 3: embed + upload to Azure AI Search
from ingestion.chunk import chunk_all_documents # step 2: markdown -> chunks
from ingestion.load_pdfs import load_all_pdfs   # step 1: Blob -> Document Intelligence -> markdown


def main() -> None:
    started = time.time()                       # note the start time

    print("STEP 1  PDFs -> markdown  (Blob Storage + Document Intelligence, cached)")
    documents = load_all_pdfs()                 # analyses only the reports not already cached

    print("\nSTEP 2  markdown -> chunks  (runs locally)")
    chunks = chunk_all_documents(documents)     # cut every report into labelled chunks

    print(f"\nSTEP 3  chunks -> Azure AI Search index '{config.AZURE_SEARCH_INDEX}'")
    build_index(chunks)                         # create the index, embed, upload

    print(f"\nDone in {time.time() - started:.0f} seconds.")  # total time
    print("Check the counts above: the two years of one company should be broadly similar.")
    print("Next:  python run_query.py")             # what to do next


if __name__ == "__main__":                      # only when run directly
    main()                                      # run all three steps
