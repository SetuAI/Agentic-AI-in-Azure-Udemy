"""
================================================================================
INGESTION STEP 2 - MARKDOWN TO LABELLED CHUNKS
================================================================================

A report is hundreds of pages. We cannot send all of it to the model, so we cut
it into small pieces (CHUNKS) and later retrieve only the relevant ones.

Every chunk keeps the labels of the report it came from -- company and fiscal
year. That label is what lets Azure AI Search look inside ONE year at a time.
The two years' reports use almost the same wording, so without that filter the
search cannot tell them apart.

This step runs on your computer. No Azure service is involved.

HOW TO RUN
----------
    python ingestion/chunk.py
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths
from typing import Dict, List                   # type hints

from langchain_text_splitters import RecursiveCharacterTextSplitter  # cuts text at the most natural break it can find

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # chunk size, overlap, minimum length

# Where the splitter prefers to cut, best first. PyMuPDF4LLM writes
# markdown headings, so cutting at a heading keeps each section together.
MARKDOWN_SEPARATORS = [
    "\n# ",                                     # top-level heading  ("# Risk Factors")
    "\n## ",                                    # sub-heading        ("## Currency Risk")
    "\n### ",                                   # sub-sub-heading
    "\n\n",                                     # blank line -- a paragraph break
    "\n",                                       # a single line break
    ". ",                                       # the end of a sentence
    " ",                                        # a space between words
    "",                                         # anywhere at all -- the last resort
]


def build_splitter() -> RecursiveCharacterTextSplitter:
    """Create the splitter with our size, overlap and preferred cut points."""
    return RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,           # aim for about 1,200 characters per chunk
        chunk_overlap=config.CHUNK_OVERLAP,     # repeat 200 characters between neighbours
        separators=MARKDOWN_SEPARATORS,         # try headings first, then paragraphs, then sentences
        length_function=len,                    # measure size in characters
        is_separator_regex=False,               # the separators above are plain text, not patterns
    )


def chunk_document(loaded_document: Dict) -> List[Dict]:
    """Cut one report into chunks, each carrying the report's labels."""
    splitter = build_splitter()                 # a fresh splitter
    pieces = splitter.split_text(loaded_document["text"])  # the actual cutting

    chunks = []                                 # results collect here
    for position, text in enumerate(pieces):    # enumerate gives each piece its number: 0, 1, 2...
        text = text.strip()                     # remove spaces and blank lines at either end
        if len(text) < config.MIN_CHUNK_LENGTH: # too short to be useful (a page number, a lone heading)?
            continue                            # drop it
        chunks.append({                         # one chunk = text + labels
            "text": text,                                   # the content
            "company": loaded_document["company"],          # e.g. "Infosys"
            "fiscal_year": loaded_document["fiscal_year"],  # e.g. "FY2025" -- the search filter
            "year_label": loaded_document["year_label"],    # e.g. "FY 2024-25"
            "source_file": loaded_document["source_file"],  # e.g. "infosys_fy2025.pdf"
            "position": position,                           # where in the report it came from
        })
    return chunks                               # every usable chunk of this report


def chunk_all_documents(loaded_documents: List[Dict], verbose: bool = True) -> List[Dict]:
    """Chunk every report and return one combined list."""
    all_chunks = []                             # results from every report
    for document in loaded_documents:           # one report at a time
        chunks = chunk_document(document)       # cut it up
        all_chunks.extend(chunks)               # add its chunks to the combined list
        if verbose:                             # print a count per report
            print(f"  {document['company']:8} {document['fiscal_year']}  {len(chunks):>5} chunks")
    return all_chunks                           # every chunk from every report


if __name__ == "__main__":                      # only when run directly
    from ingestion.load_pdfs import load_all_pdfs  # imported here: only needed for this demo

    chunk_all_documents(load_all_pdfs(verbose=False))  # load (from cache if possible), chunk, print counts
