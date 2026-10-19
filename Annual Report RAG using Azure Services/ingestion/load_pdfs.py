"""
================================================================================
INGESTION STEP 1 - PDF (FROM BLOB STORAGE) TO MARKDOWN (VIA PYMUPDF4LLM)
================================================================================

WHAT THIS FILE DOES
-------------------
For each annual report:

    1. download it from Blob Storage          (skipped if already downloaded)
    2. convert it with PyMuPDF4LLM            (skipped if the markdown is cached)
    3. get back MARKDOWN -- headings, paragraphs and tables, in reading order
    4. save that markdown to data/processed/  (this is the cache)

WHY PYMUPDF4LLM (AND NOT AZURE DOCUMENT INTELLIGENCE)
-----------------------------------------------------
Azure AI Document Intelligence bills PER PAGE, and these reports run to hundreds
of pages each -- too expensive for a course. PyMuPDF4LLM is a free, open-source
library that runs on your own machine and writes LLM-friendly markdown,
including tables.

The trade-off: PyMuPDF4LLM reads the text layer inside the PDF. A page that is
only a scanned image has no text layer, so it comes out (nearly) empty. The
annual reports used here are digital PDFs, so this is fine.

WHY THE CACHE STILL HELPS
-------------------------
Converting a few hundred pages takes a minute or so per report. The markdown is
saved to data/processed/, and if that file exists the PDF is not converted
again. To force a fresh conversion, delete the .md file.

HOW TO RUN
----------
    python ingestion/load_pdfs.py
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths
from typing import Dict, List                   # type hints: what goes in and comes out

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # settings: documents and folders
from azure_clients import blob_container        # the one Azure service this step uses: Blob Storage


def download_pdf(document: Dict) -> Path:
    """Fetch one PDF from Blob Storage into data/raw/, unless it is already there."""
    local_path = config.RAW_PDF_DIR / document["filename"]  # where the downloaded copy will live

    if local_path.exists():                     # downloaded on an earlier run?
        return local_path                       # reuse it -- no need to download again

    config.RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)          # create data/raw/ on the first run
    blob = blob_container().get_blob_client(document["filename"])  # point at this one file in the container
    local_path.write_bytes(blob.download_blob().readall())         # download every byte and write it to disk
    return local_path                           # tell the caller where the file is


def analyse_pdf(pdf_path: Path) -> str:
    """Convert one PDF to markdown with PyMuPDF4LLM -- free, and it runs locally."""
    import pymupdf4llm                          # imported here so the rest of the file loads without it

    return pymupdf4llm.to_markdown(             # read every page and join them into one markdown string
        str(pdf_path),                          # the PDF on disk (downloaded from Blob Storage)
        show_progress=False,                    # keep the console quiet; we print our own summary
    )


def cache_path_for(document: Dict) -> Path:
    """Where the markdown for one report is cached, e.g. data/processed/Infosys_FY2025.md."""
    return config.PROCESSED_DIR / f"{document['company']}_{document['fiscal_year']}.md"  # one file per report


def load_single_pdf(document: Dict) -> Dict:
    """Return one report as markdown plus its labels, using the cache when possible."""
    cache_path = cache_path_for(document)       # where this report's markdown would be saved

    if cache_path.exists():                     # analysed on an earlier run?
        markdown = cache_path.read_text(encoding="utf-8")  # read the saved markdown -- free and instant
        source = "cache"                        # note where it came from, for the printout
    else:                                       # first time for this report
        pdf_path = download_pdf(document)       # get the PDF from Blob Storage
        markdown = analyse_pdf(pdf_path)        # convert it locally with PyMuPDF4LLM (free)
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)  # create data/processed/ if needed
        cache_path.write_text(markdown, encoding="utf-8")        # save it so we never pay for it twice
        source = "PyMuPDF4LLM"                  # note where it came from

    return {                                    # the shape chunk.py expects
        "text": markdown,                       # the document's content
        "company": document["company"],         # label carried onto every chunk
        "fiscal_year": document["fiscal_year"], # label carried onto every chunk -- the key search filter
        "year_label": document["year_label"],   # how the report writes its own year
        "source_file": document["filename"],    # which PDF it came from
        "loaded_from": source,                  # "cache" or "PyMuPDF4LLM"
    }


def load_all_pdfs(verbose: bool = True) -> List[Dict]:
    """Load every report in config.DOCUMENTS."""
    loaded = []                                 # results collect here
    for document in config.DOCUMENTS:           # the four reports, one at a time
        result = load_single_pdf(document)      # markdown + labels
        loaded.append(result)                   # keep it
        if verbose:                             # print progress unless told not to
            print(f"  {document['filename']:22} {len(result['text']):>10,} characters  "
                  f"(from {result['loaded_from']})")  # e.g. "infosys_fy2025.pdf  1,234,567 characters (from cache)"
    return loaded                               # all four documents


if __name__ == "__main__":                      # only when run directly
    load_all_pdfs()                             # download, convert or read from cache, and print a summary
