"""
================================================================================
run_ingestion.py  -  RUN THE WHOLE OF INGESTION, IN ONE COMMAND
================================================================================

PURPOSE
-------
This file runs the three main steps of ingestion, one after another, with a
clear heading for each:

    STEP 1  load_pdfs.py     PDFs → markdown        (from data/raw/ or Blob Storage)
    STEP 2  chunk.py         markdown → chunks      (on the laptop, no Azure)
    STEP 3  build_index.py   chunks → embeddings → Azure AI Search

At the end, it prints how long everything took, and what to run next.

WHAT PROBLEM DOES IT SOLVE
--------------------------
Each of the three files can be run on its own. But to build the index, they
must run in the RIGHT ORDER, and each one needs what the previous one made:

    load_pdfs.py  gives back  4 reports as markdown
          ↓
    chunk.py      takes those, and gives back  5,154 chunks
          ↓
    build_index.py  takes those, and fills the index

Running them by hand means three commands, in the right order, every time.
This file does it with ONE command, and passes each step's output straight to
the next step.

WHY upload_pdfs.py IS NOT HERE
------------------------------
Uploading the PDFs to Blob Storage is done ONCE, separately, before this.
After that, the PDFs are already in the cloud. So this file starts from
STEP 1, not from the upload.

    The full order is:
        python ingestion/upload_pdfs.py      once, to put the PDFs in Blob Storage
        python run_ingestion.py              this file: builds the index

WHERE IT FITS
-------------
    upload_pdfs.py     laptop → Blob Storage               (run once, before)
    run_ingestion.py ← YOU ARE HERE: runs the three steps below, in order
        load_pdfs.py       Blob Storage → markdown
        chunk.py           markdown → chunks
        build_index.py     chunks → embeddings → Azure AI Search
    run_query.py       ask a question                      (run after)

HOW TO RUN (from the project folder)
------------------------------------
    python run_ingestion.py

EXPECTED OUTPUT
---------------
    STEP 1  PDFs -> markdown  (Blob Storage + PyMuPDF4LLM, cached)
      infosys_fy2025.pdf      1,209,465 characters  (from cache)
      infosys_fy2026.pdf      1,287,275 characters  (from cache)
      eternal_fy2025.pdf        939,229 characters  (from cache)
      eternal_fy2026.pdf        988,304 characters  (from cache)

    STEP 2  markdown -> chunks  (runs locally)
      Infosys  FY2025   1402 chunks
      Infosys  FY2026   1488 chunks
      Eternal  FY2025   1120 chunks
      Eternal  FY2026   1144 chunks

    STEP 3  chunks -> Azure AI Search index 'annual-reports'
      creating index 'annual-reports'
      embedding 5,154 chunks with 'text-embedding-3-small'
        embedded    100 / 5,154
        ...
        embedded  5,154 / 5,154
      uploaded 5,154 of 5,154 chunks

    Done in ... seconds.
    Check the counts above: the two years of one company should be broadly similar.
    Next:  python run_query.py

Then, in the portal: Indexes → annual-reports → Document count = 5,154.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

import time
# "time" lets us read the clock.
# We use it to measure how long the whole of ingestion takes, from start to end.

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

sys.path.insert(0, str(Path(__file__).parent))
# Add the project folder to the front of sys.path, so Python can find our own
# files: config.py, and the ingestion/ folder.
#   Path(__file__)   this file:   .../run_ingestion.py
#   .parent          its folder:  .../  (the project folder)
#
# Here, we can notice ONE ".parent", not two like in the ingestion files.
# Why? Because this file sits directly IN the project folder, not inside a
# sub-folder. So its own folder already is the project folder.

import config
# Our settings file. We use one setting from it:
#   config.AZURE_SEARCH_INDEX   the index name, "annual-reports", for the STEP 3 heading.

from ingestion.build_index import build_index
# STEP 3: from ingestion/build_index.py, the function that creates the index,
# embeds every chunk, and uploads them all.
#
# "from ingestion.build_index import build_index" means:
#   go into the ingestion/ folder, open build_index.py, and bring in the
#   function called build_index.

from ingestion.chunk import chunk_all_documents
# STEP 2: from ingestion/chunk.py, the function that cuts every report into chunks.

from ingestion.load_pdfs import load_all_pdfs
# STEP 1: from ingestion/load_pdfs.py, the function that turns every PDF into markdown.
#
# Note: the imports are in alphabetical order (build_index, chunk, load_pdfs),
# not in the order the steps run. That's just a tidy habit. The ORDER THE
# STEPS RUN is decided inside main(), below.


# ── THE THREE STEPS, IN ORDER ─────────────────────────────────────────────────

def main() -> None:
    # The whole of ingestion, in one function.
    # "-> None" means it doesn't give anything back. It just does the work,
    # and prints what's happening.

    started = time.time()
    # Note the time RIGHT NOW, before anything starts.
    # time.time() gives the current time as a number of seconds. On its own,
    # the number means nothing. But at the end, we read the clock again, and
    # the DIFFERENCE between the two is how long ingestion took.

    # ── STEP 1 ────────────────────────────────────────────────────────────────

    print("STEP 1  PDFs -> markdown  (Blob Storage + PyMuPDF4LLM, cached)")
    # Print a heading, so we can see which step is running.

    documents = load_all_pdfs()
    # Load all four reports as markdown.
    # For each report, load_all_pdfs():
    #   - reads the saved markdown from data/processed/, if it's there (instant), or
    #   - gets the PDF (from data/raw/, or from Blob Storage), converts it with
    #     PyMuPDF4LLM, and saves the markdown for next time.
    #
    # It gives back a LIST of four dictionaries, one per report. Each has the
    # report's whole text, plus its labels: company, fiscal_year, year_label,
    # source_file. We keep that list in "documents", and hand it to STEP 2.

    # ── STEP 2 ────────────────────────────────────────────────────────────────

    print("\nSTEP 2  markdown -> chunks  (runs locally)")
    # Print the next heading.
    # "\n" at the start means: a new line first, so there's a blank line
    # between STEP 1's output and this heading. It makes the output easier to read.

    chunks = chunk_all_documents(documents)
    # Cut every report into chunks of about 1,200 characters.
    #
    # Here, we can notice how the steps CONNECT: we give it "documents",
    # which is exactly what STEP 1 gave back.
    #
    # It gives back ONE list of every chunk from all four reports: 5,154 in our
    # run. Each chunk is its text, plus the labels it carried over from its
    # report. We keep that list in "chunks", and hand it to STEP 3.

    # ── STEP 3 ────────────────────────────────────────────────────────────────

    print(f"\nSTEP 3  chunks -> Azure AI Search index '{config.AZURE_SEARCH_INDEX}'")
    # Print the last heading, with the index name filled in.
    # The "f" before the quotes makes this an F-STRING: anything inside { }
    # is replaced by its value. So this prints:
    #   STEP 3  chunks -> Azure AI Search index 'annual-reports'

    build_index(chunks)
    # Create the index, embed every chunk, and upload them all.
    #
    # Again, the steps connect: we give it "chunks", which is exactly what
    # STEP 2 gave back.
    #
    # This is the slow step. It makes about 52 calls to Azure OpenAI (100
    # chunks each) for the embeddings, then 11 calls to Azure AI Search
    # (500 chunks each) for the upload.

    # ── THE END ───────────────────────────────────────────────────────────────

    print(f"\nDone in {time.time() - started:.0f} seconds.")
    # Print how long it all took.
    # Let's read the part inside { }:
    #   time.time()             read the clock again, now
    #   - started               minus the time we noted at the start
    #                           = the number of seconds ingestion took
    #   :.0f                    show it with no digits after the decimal point.
    #                           So 184.73 is shown as 185.

    print("Check the counts above: the two years of one company should be broadly similar.")
    # A reminder to look at the counts STEP 1 and STEP 2 printed.
    # If one year of a company has far fewer characters or chunks than the
    # other, that report probably didn't convert properly.

    print("Next:  python run_query.py")
    # Tell the user what to run next: ask a question.
    # The index is now full and ready to be searched.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python run_ingestion.py
    # If another file ever imports this one, nothing below runs.

    main()
    # Run all three steps, in order.