"""
================================================================================
ingestion/chunk.py  -  CUT EACH REPORT INTO SMALL LABELLED PIECES
================================================================================

PURPOSE
-------
This file cuts each report's markdown into small pieces called CHUNKS.

A CHUNK is one small piece of a report, about 1,200 characters long.
That's roughly one or two paragraphs.

Every chunk also carries LABELS: which company, which year, which PDF, and
where in the report it came from.

WHAT PROBLEM DOES IT SOLVE
--------------------------
One report is about 9 to 13 lakh characters of text. We can't send all of
that to the model with every question. It would be slow, expensive, and the
model would struggle to find the few lines that matter.

So, we cut each report into small chunks. Later, when a question comes in, we
search for the few chunks that answer it, and send ONLY those to the model.

And the labels matter just as much. The FY2025 and FY2026 reports use almost
the same words. The only way to keep the two years apart is the fiscal_year
label on every chunk. Search uses it to look inside ONE year at a time.

HOW THE CUTTING WORKS
---------------------
We aim for about 1,200 characters per chunk. But we don't cut blindly at
character 1,200, because that would cut sentences in half.

Instead, we try to cut at the most natural place, in this order:
    1. at a heading            ("# Risk Factors")
    2. at a sub-heading        ("## Currency Risk")
    3. at a sub-sub-heading
    4. at a paragraph break
    5. at a line break
    6. at the end of a sentence
    7. between two words
    8. anywhere, only if nothing else works

And neighbouring chunks share 200 characters (the OVERLAP). So a sentence that
sits right on the boundary appears in both chunks, and is never lost.

This step runs on the laptop. No Azure service is involved.

WHERE IT FITS
-------------
    upload_pdfs.py     laptop → Blob Storage
    load_pdfs.py       Blob Storage → markdown
    chunk.py         ← YOU ARE HERE: markdown → chunks
    build_index.py     chunks → embeddings → Azure AI Search

HOW TO RUN (from the project folder)
------------------------------------
    python ingestion/chunk.py

EXPECTED OUTPUT
---------------
      Infosys  FY2025   1402 chunks
      Infosys  FY2026   1488 chunks
      Eternal  FY2025   1120 chunks
      Eternal  FY2026   1144 chunks
5,154 chunks in total. The two years of each company should be close.
Nothing is saved, and nothing is sent to Azure: this run only shows the counts.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in for imports.

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import Dict, List
# TYPE HINTS. They only describe what goes in and what comes out of a function.
#   Dict   a dictionary, like one chunk: {"text": ..., "company": ...}
#   List   a list, like all the chunks of a report

from langchain_text_splitters import RecursiveCharacterTextSplitter
# This is the tool that does the actual cutting. It comes from LangChain.
#
# Why "Recursive"? Because it tries the first cut point in our list (a heading).
# If a piece is still bigger than 1,200 characters, it tries the next cut point
# (a sub-heading) on that piece. Then the next. And so on, until every piece fits.

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside ingestion/, but config.py is one folder above.
# So we add the project folder to the front of sys.path, so "import config" works.

import config
# Our settings. From it we use three numbers:
#   config.CHUNK_SIZE         1200   characters we aim for in each chunk
#   config.CHUNK_OVERLAP      200    characters shared between neighbouring chunks
#   config.MIN_CHUNK_LENGTH   100    anything shorter than this is thrown away


# ── WHERE WE PREFER TO CUT ────────────────────────────────────────────────────

# Where the splitter prefers to cut, best first. PyMuPDF4LLM writes
# markdown headings, so cutting at a heading keeps each section together.
MARKDOWN_SEPARATORS = [
    # The list of cut points, in order of preference.
    # The splitter always tries the FIRST one it can use.
    #
    # Here, we can notice why load_pdfs.py turned the PDFs into markdown:
    # the first three cut points are markdown headings. Without markdown,
    # there would be no "#" to cut at.

    "\n# ",
    # 1. A top-level heading, like "# Risk Factors".
    # "\n" means a new line. So this is: a new line that starts with "# ".

    "\n## ",
    # 2. A sub-heading, like "## Currency Risk".

    "\n### ",
    # 3. A sub-sub-heading.

    "\n\n",
    # 4. A blank line, which means a paragraph break.

    "\n",
    # 5. A single line break.

    ". ",
    # 6. The end of a sentence: a full stop followed by a space.

    " ",
    # 7. A space between two words.

    "",
    # 8. Anywhere at all. The last resort, used only if a piece has no spaces
    #    and is still too long, which almost never happens in real text.
]


# ── THE SPLITTER ──────────────────────────────────────────────────────────────

def build_splitter() -> RecursiveCharacterTextSplitter:
    # Creates the cutting tool, with our settings. Gives it back, ready to use.
    """Create the splitter with our size, overlap and preferred cut points."""
    # The function's one-line description.

    return RecursiveCharacterTextSplitter(
        # Create the splitter. Each setting below tells it how to cut.

        chunk_size=config.CHUNK_SIZE,
        # Aim for about 1,200 characters per chunk.
        # Why 1,200? Small enough that each chunk is about one topic.
        # Big enough that it still has enough context to make sense on its own.

        chunk_overlap=config.CHUNK_OVERLAP,
        # Neighbouring chunks share 200 characters.
        # So the end of chunk 5 is repeated at the start of chunk 6.
        # This way, a sentence that falls on the boundary is never cut out.

        separators=MARKDOWN_SEPARATORS,
        # Our list of cut points: headings first, then paragraphs, then sentences.

        length_function=len,
        # How to measure a chunk's size. "len" counts characters.

        is_separator_regex=False,
        # Our cut points are plain text, like "\n# ". They are not special
        # search patterns. So we tell the splitter to read them as plain text.
    )


# ── ONE REPORT INTO CHUNKS ────────────────────────────────────────────────────

def chunk_document(loaded_document: Dict) -> List[Dict]:
    # Takes ONE report (from load_pdfs.py). Gives back a list of its chunks.
    """Cut one report into chunks, each carrying the report's labels."""
    # The function's one-line description.

    splitter = build_splitter()
    # Create the cutting tool, with our settings.

    pieces = splitter.split_text(loaded_document["text"])
    # This is the line that actually cuts.
    # It takes the report's whole markdown, and gives back a list of pieces of text.
    # For example, for Infosys FY2025, about 1,400 pieces.

    chunks = []
    # An empty list. Each finished chunk will be added here.

    for position, text in enumerate(pieces):
        # Go through the pieces, one at a time.
        # "enumerate" gives us each piece together with its number: 0, 1, 2, 3...
        # That number is the chunk's POSITION: where in the report it came from.

        text = text.strip()
        # Remove any spaces or blank lines at the very start and very end of the piece.

        if len(text) < config.MIN_CHUNK_LENGTH:
            # Is this piece shorter than 100 characters?
            # That's usually a page number, a lone heading, or a stray line.
            # It has no useful meaning on its own.

            continue
            # So we drop it, and move on to the next piece.
            # Note: its position number is skipped too. That's fine. Positions
            # just need to be unique and in order, not without gaps.

        chunks.append({
            # Now we build ONE chunk: the text, plus the report's labels.
            # And add it to our list.

            "text": text,
            # The chunk's content: about 1,200 characters of the report.

            "company": loaded_document["company"],
            # e.g. "Infosys". Copied from the report onto every chunk.

            "fiscal_year": loaded_document["fiscal_year"],
            # e.g. "FY2025". The most important label.
            # This is what lets search look inside ONE year only.

            "year_label": loaded_document["year_label"],
            # e.g. "FY 2024-25". How the report writes its own year.

            "source_file": loaded_document["source_file"],
            # e.g. "infosys_fy2025.pdf". Which PDF this chunk came from.

            "position": position,
            # e.g. 42. Where in the report this chunk sits.
            # Together with the company and year, it gives every chunk a unique
            # id later: Infosys_FY2025_42.
        })

    return chunks
    # Hand back every usable chunk of this report.


# ── ALL FOUR REPORTS ──────────────────────────────────────────────────────────

def chunk_all_documents(loaded_documents: List[Dict], verbose: bool = True) -> List[Dict]:
    # Takes all four reports. Gives back ONE combined list of every chunk.
    # verbose=True means: print one line per report.
    """Chunk every report and return one combined list."""
    # The function's one-line description.

    all_chunks = []
    # An empty list for the chunks of all four reports.

    for document in loaded_documents:
        # Go through the four reports, one at a time.

        chunks = chunk_document(document)
        # Cut this report into chunks.

        all_chunks.extend(chunks)
        # Add all of them to the combined list.
        # "extend" adds each chunk one by one. ("append" would add the whole
        # list as a single item, which is not what we want.)

        if verbose:
            # If we were asked to print progress...

            print(f"  {document['company']:8} {document['fiscal_year']}  {len(chunks):>5} chunks")
            # ...print how many chunks this report gave. For example:
            #   Infosys  FY2025   1402 chunks
            # This is a quick health check: the two years of one company
            # should give roughly the same number of chunks.

    return all_chunks
    # Hand back every chunk from every report. In our run: 5,154.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python ingestion/chunk.py

    from ingestion.load_pdfs import load_all_pdfs
    # To cut the reports, we first need them as markdown. So we bring in
    # load_all_pdfs from the previous step.
    # It's imported here, not at the top, because it's only needed for this demo.

    chunk_all_documents(load_all_pdfs(verbose=False))
    # Read the four reports (from the saved markdown, so it's instant),
    # cut each into chunks, and print the count per report.
    # verbose=False keeps load_all_pdfs quiet, so we only see the chunk counts.