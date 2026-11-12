"""
================================================================================
ingestion/load_pdfs.py  -  TURN EACH PDF INTO MARKDOWN
================================================================================

PURPOSE
-------
This file turns each of our four PDFs into MARKDOWN text, and saves it.

For each report, it does this:

    1. Is the markdown already saved in data/processed/?
         yes → read it and stop (no download, no conversion)
         no  → carry on
    2. Is the PDF already in data/raw/?
         yes → use that copy
         no  → download it from Blob Storage into data/raw/
    3. PyMuPDF4LLM reads the PDF and turns it into markdown
    4. Save the markdown in data/processed/, e.g. Infosys_FY2025.md

WHAT PROBLEM DOES IT SOLVE
--------------------------
A PDF stores WHERE each word sits on a page. It does not store WHAT the text
is: a heading, a paragraph, or a table.

But the next step, chunk.py, needs to know where the headings are, so it can
cut the report at its own sections. And the factual guardrail needs every
table's columns kept apart, so it can tell an FY2025 number from an FY2026
number.

So, we turn each PDF into MARKDOWN. Markdown is plain text where headings are
marked with "#", and tables keep their rows and columns:

    ## Revenue
    | Particulars | FY2025 | FY2026 |
    |-------------|--------|--------|

WHY PYMUPDF4LLM, AND NOT AZURE DOCUMENT INTELLIGENCE
----------------------------------------------------
Azure has a service called Document Intelligence that can also read PDFs.
But it charges for every page, and annual reports have hundreds of pages.
PyMuPDF4LLM is free, and runs on our own laptop.

Note: PyMuPDF4LLM reads the text that is stored inside the PDF. A page that is
only a scanned picture has no stored text, so it comes out almost empty. Our
four reports are digital PDFs, so this is fine.

WHY SAVE THE MARKDOWN
---------------------
Converting a big report takes a while. So the markdown is saved in
data/processed/. The next time, it is read from there instantly. To convert a
report again, delete its .md file.

WHERE IT FITS
-------------
    upload_pdfs.py     laptop → Blob Storage
    load_pdfs.py     ← YOU ARE HERE: Blob Storage → markdown
    chunk.py           markdown → chunks
    build_index.py     chunks → embeddings → Azure AI Search

HOW TO RUN (from the project folder)
------------------------------------
    python ingestion/load_pdfs.py

EXPECTED OUTPUT
---------------
The first time (converted by PyMuPDF4LLM):
      infosys_fy2025.pdf      1,209,465 characters  (from PyMuPDF4LLM)
      infosys_fy2026.pdf      1,287,275 characters  (from PyMuPDF4LLM)
      eternal_fy2025.pdf        939,229 characters  (from PyMuPDF4LLM)
      eternal_fy2026.pdf        988,304 characters  (from PyMuPDF4LLM)
Every time after that, the same numbers, but "(from cache)".
And four .md files appear in data/processed/.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in for imports.

from pathlib import Path
# "Path" lets us build file paths, like data/raw/infosys_fy2025.pdf,
# that work on every operating system.

from typing import Dict, List
# These are TYPE HINTS. They only describe what a function takes and returns.
#   Dict   a dictionary, like {"company": "Infosys"}
#   List   a list, like [doc1, doc2, doc3, doc4]
# Python doesn't check them. They are there so a reader can understand the code faster.

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside ingestion/, but config.py and azure_clients.py are one
# folder above. So we add that folder (the project folder) to the front of
# sys.path, so the imports below can find them.
#   Path(__file__).parent.parent   = this file's folder, then one folder up

import config
# Our settings. From it we use:
#   config.DOCUMENTS       the four reports and their labels
#   config.RAW_PDF_DIR     data/raw/        where PDFs are kept on the laptop
#   config.PROCESSED_DIR   data/processed/  where the markdown is saved

from azure_clients import blob_container
# The helper that connects to our Blob Storage container.
# Blob Storage is the only Azure service this file uses.


# ── STEP A · GET THE PDF ──────────────────────────────────────────────────────

def download_pdf(document: Dict) -> Path:
    # Takes one report's details. Gives back where its PDF is on the laptop.
    """Fetch one PDF from Blob Storage into data/raw/, unless it is already there."""
    # The function's one-line description.

    local_path = config.RAW_PDF_DIR / document["filename"]
    # Where the PDF should be on the laptop.
    # For example: data/raw/infosys_fy2025.pdf

    if local_path.exists():
        # Is it already there?
        # On our laptop, yes: it's the same folder upload_pdfs.py uploaded from.

        return local_path
        # Then we don't download anything. We just use this copy.
        # "return" ends the function here, and hands back the path.

    # If we reach this line, the PDF is NOT on this machine.
    # That's the case on a teammate's laptop, or on a fresh machine.
    # So now we download it from Blob Storage.

    config.RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    # First, make sure the data/raw/ folder exists.
    #   parents=True    also create data/ if that's missing
    #   exist_ok=True   don't complain if the folder is already there

    blob = blob_container().get_blob_client(document["filename"])
    # Connect to our container, then point at ONE file inside it:
    # the blob with this report's file name.

    local_path.write_bytes(blob.download_blob().readall())
    # Let's read this from the inside out:
    #   blob.download_blob()   start downloading the file from Blob Storage
    #   .readall()             read every byte of it
    #   local_path.write_bytes(...)   write those bytes into data/raw/ on the laptop
    # So, after this line, the PDF is on this machine.

    return local_path
    # Hand back where the PDF now is.


# ── STEP B · PDF TO MARKDOWN ──────────────────────────────────────────────────

def analyse_pdf(pdf_path: Path) -> str:
    # Takes the path to a PDF. Gives back its whole content as markdown text.
    """Convert one PDF to markdown with PyMuPDF4LLM -- free, and it runs locally."""
    # The function's one-line description.

    import pymupdf4llm
    # The library that turns PDFs into markdown.
    # Why is it imported HERE, inside the function, and not at the top?
    # Because it's only needed when a PDF is actually converted. If every
    # report is already saved as markdown, this line never runs.

    return pymupdf4llm.to_markdown(
        # Read every page of the PDF, turn it into markdown, and join all the
        # pages into ONE long piece of text. Then hand that text back.
        # This is the slow part of the whole file.

        str(pdf_path),
        # Which PDF to read. The library wants the path as plain text, so we use str().

        show_progress=False,
        # Don't print a progress bar for every page. We print our own
        # one-line summary per report instead.
    )


# ── STEP C · WHERE THE MARKDOWN IS SAVED ──────────────────────────────────────

def cache_path_for(document: Dict) -> Path:
    # Takes one report's details. Gives back where its markdown is (or will be) saved.
    """Where the markdown for one report is cached, e.g. data/processed/Infosys_FY2025.md."""
    # The function's one-line description.

    return config.PROCESSED_DIR / f"{document['company']}_{document['fiscal_year']}.md"
    # The file name is made from the company and the year.
    # For example: data/processed/  +  "Infosys" + "_" + "FY2025" + ".md"
    #              →  data/processed/Infosys_FY2025.md
    # So each report gets its own file, and the name tells you which one it is.


# ── STEP D · ONE REPORT, START TO FINISH ──────────────────────────────────────

def load_single_pdf(document: Dict) -> Dict:
    # Takes one report's details. Gives back its markdown, plus its labels.
    """Return one report as markdown plus its labels, using the cache when possible."""
    # The function's one-line description.

    cache_path = cache_path_for(document)
    # Where this report's markdown is saved, e.g. data/processed/Infosys_FY2025.md

    if cache_path.exists():
        # Have we already converted this report on an earlier run?

        markdown = cache_path.read_text(encoding="utf-8")
        # Yes. So we just read the saved markdown. It's instant.
        # encoding="utf-8" makes sure special characters like ₹ are read correctly.

        source = "cache"
        # Remember where it came from, so the printout can say "(from cache)".

    else:
        # No. This is the first time for this report.

        pdf_path = download_pdf(document)
        # Get the PDF onto this machine: from data/raw/, or downloaded from Blob Storage.

        markdown = analyse_pdf(pdf_path)
        # Turn it into markdown with PyMuPDF4LLM.

        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        # Make sure the data/processed/ folder exists.

        cache_path.write_text(markdown, encoding="utf-8")
        # Save the markdown, so next time we can read it instantly instead of converting again.

        source = "PyMuPDF4LLM"
        # Remember where it came from, so the printout can say "(from PyMuPDF4LLM)".

    return {
        # Now we hand back ONE dictionary for this report.
        # This is exactly the shape chunk.py expects.

        "text": markdown,
        # The report's whole content, as markdown.

        "company": document["company"],
        # Which company, e.g. "Infosys".
        # This label will be copied onto every chunk of this report.

        "fiscal_year": document["fiscal_year"],
        # Which year, e.g. "FY2025".
        # This is the most important label. It's what lets us search ONE year at a time.

        "year_label": document["year_label"],
        # How the report itself writes its year, e.g. "FY 2024-25".
        # Shown on the web page next to each chunk.

        "source_file": document["filename"],
        # Which PDF it came from, e.g. "infosys_fy2025.pdf".

        "loaded_from": source,
        # "cache" or "PyMuPDF4LLM". Only used for the printout.
    }


# ── STEP E · ALL FOUR REPORTS ─────────────────────────────────────────────────

def load_all_pdfs(verbose: bool = True) -> List[Dict]:
    # Loads all four reports. Gives back a list of four dictionaries.
    # verbose=True means: print one line per report. chunk.py passes False to keep it quiet.
    """Load every report in config.DOCUMENTS."""
    # The function's one-line description.

    loaded = []
    # An empty list. The four results will be added to it, one by one.

    for document in config.DOCUMENTS:
        # Go through the four reports, one at a time.

        result = load_single_pdf(document)
        # Get this report's markdown and labels (Step D above).

        loaded.append(result)
        # Add it to the list.

        if verbose:
            # If we were asked to print progress...

            print(f"  {document['filename']:22} {len(result['text']):>10,} characters  "
                  f"(from {result['loaded_from']})")
            # ...print one line for this report. For example:
            #   infosys_fy2025.pdf      1,209,465 characters  (from cache)
            #
            # The parts inside {...}:
            #   :22     pad the file name to 22 characters, so the columns line up
            #   :>10,   right-align the number in 10 characters, with commas
            #
            # The character count is a quick health check: if one report shows
            # far fewer characters than the others, its PDF didn't convert well.

    return loaded
    # Hand back all four reports.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python ingestion/load_pdfs.py

    load_all_pdfs()
    # Load all four reports: from the saved markdown, or by converting the
    # PDF. Then print one line per report.