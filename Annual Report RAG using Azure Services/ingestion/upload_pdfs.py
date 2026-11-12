"""
================================================================================
ingestion/upload_pdfs.py  -  PUT THE FOUR PDFs IN BLOB STORAGE
================================================================================

PURPOSE
-------
This file copies our four annual reports from the laptop into Azure Blob
Storage. It is the very first step of ingestion.

    data/raw/ on the laptop  ──upload──>  Blob Storage, container "annual-reports"

WHAT PROBLEM DOES IT SOLVE
--------------------------
Right now, the four PDFs exist in only one place: one folder on one laptop.
So only that laptop can build the index. A teammate can't. Another machine
can't. And if the laptop is wiped, the files are gone.

So, we put the files in the cloud. Once they are in Blob Storage, any machine
with the connection string can get the same four files, and build the same
index.

WHERE IT FITS
-------------
    upload_pdfs.py   ← YOU ARE HERE: laptop → Blob Storage (run once)
    load_pdfs.py       Blob Storage → markdown
    chunk.py           markdown → chunks
    build_index.py     chunks → embeddings → Azure AI Search

WHAT IT NEEDS IN .env
---------------------
    AZURE_STORAGE_CONNECTION_STRING   which storage account, and its key
    AZURE_STORAGE_CONTAINER           the container name, e.g. annual-reports
    LOCAL_PDF_DIR                     where the PDFs are on the laptop, e.g. ./data/raw

HOW TO RUN (from the project folder)
------------------------------------
    python ingestion/upload_pdfs.py

EXPECTED OUTPUT
---------------
    Uploading from .../data/raw to container 'annual-reports'
      UPLOADED  infosys_fy2025.pdf  (8.2 MB)
      UPLOADED  infosys_fy2026.pdf  (13.9 MB)
      UPLOADED  eternal_fy2025.pdf  (234.0 MB)
      UPLOADED  eternal_fy2026.pdf  (138.4 MB)

Then open the container in the Azure portal: the four PDFs are there.
Running it again is safe: the files are simply replaced.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us access to Python's own settings.
# We need one of them, sys.path: the list of folders Python looks in when we
# write "import something".

from pathlib import Path
# "Path" lets us work with file and folder paths.
# For example, Path("data") / "raw" gives us the path data/raw, and it works
# the same way on Windows, Mac and Linux.

sys.path.insert(0, str(Path(__file__).parent.parent))
# Now, we have a problem. This file lives inside the ingestion/ folder.
# But config.py and azure_clients.py live one folder above, in the project
# folder. Python won't find them on its own.
#
# So, we add the project folder to the front of sys.path. Let's read it from
# the inside out:
#   __file__                  this file:          .../ingestion/upload_pdfs.py
#   Path(__file__).parent     its folder:         .../ingestion
#   .parent                   one folder up:      .../  (the project folder)
#   str(...)                  turned into text, because sys.path holds text
#   sys.path.insert(0, ...)   put it FIRST in the list, so Python looks there first
#
# Now the two imports below work.

import config
# Our settings file, config.py. From it we use:
#   config.DOCUMENTS                 the four reports, and each one's file name
#   config.LOCAL_PDF_DIR             the laptop folder that holds the PDFs
#   config.AZURE_STORAGE_CONTAINER   the container name, for the printout
# config.py also reads our .env file the moment it is imported.

from azure_clients import blob_container
# A small helper from azure_clients.py.
# blob_container() connects to our storage account using the connection string,
# and hands back our one container, ready to use.


# ── THE UPLOAD ────────────────────────────────────────────────────────────────

def upload_all() -> None:
    # This function uploads every report listed in config.DOCUMENTS.
    # "-> None" means it doesn't return anything. It just does the work.
    """Upload every annual report listed in config.DOCUMENTS."""
    # The function's one-line description.

    container = blob_container()
    # Step 1: connect to our container in Blob Storage.
    # From now on, "container" is how we talk to it.

    if not container.exists():
        # Step 2: check the container is really there.
        # On a brand-new storage account, it might not exist yet.

        container.create_container()
        # So, if it doesn't exist, we create it.
        # A new container is PRIVATE by default: nobody can read it without the key.

    for document in config.DOCUMENTS:
        # Step 3: go through the four reports, one at a time.
        # Each "document" is a small dictionary from config.py, for example:
        #   {"filename": "infosys_fy2025.pdf", "company": "Infosys",
        #    "fiscal_year": "FY2025", "year_label": "FY 2024-25"}

        local_path = config.LOCAL_PDF_DIR / document["filename"]
        # Build the full path to this PDF on the laptop.
        # For example: data/raw  +  infosys_fy2025.pdf  →  data/raw/infosys_fy2025.pdf

        if not local_path.exists():
            # Is the file actually there?

            print(f"  SKIPPED   {document['filename']}  (not found in {config.LOCAL_PDF_DIR})")
            # If not, we say so clearly, with the file name and the folder we looked in.
            # Usually this means the file name is spelled differently.

            continue
            # "continue" means: skip the rest of this loop, and go to the next report.
            # So one missing file doesn't stop the other three.

        if local_path.stat().st_size < 1000:
            # Now, another check. Is the file suspiciously small?
            # local_path.stat().st_size is the file's size in bytes.
            # A real annual report is several megabytes. Fewer than 1,000 bytes
            # means it isn't a real PDF.
            #
            # Why would that happen? If the PDFs were stored on GitHub with Git
            # LFS (a GitHub feature for big files), cloning the repo can give you
            # a tiny text "pointer" instead of the real file.

            print(f"  SKIPPED   {document['filename']}  (looks like a Git LFS pointer -- run 'git lfs pull')")
            # So we skip it, and tell the user exactly how to fetch the real file.

            continue
            # Uploading a pointer would be useless. Go to the next report.

        with open(local_path, "rb") as pdf_file:
            # Step 4: open the PDF.
            # "rb" means Read Bytes. A PDF is not plain text, so we read it as raw bytes.
            # "with" closes the file for us automatically when we're done.

            container.upload_blob(
                # Send this file to Blob Storage. The three settings below say how.

                name=document["filename"],
                # The name it gets in the container. We keep the same name as on the laptop.

                data=pdf_file,
                # The file itself. It is streamed from the disk, piece by piece,
                # so even the 234 MB report doesn't have to fit in memory at once.

                overwrite=True,
                # If a file with this name is already there, replace it.
                # Without this, running the script twice would fail with "already exists".
            )

        size_mb = local_path.stat().st_size / 1_000_000
        # Step 5: work out the file's size in megabytes, for the printout.
        # Bytes divided by 1,000,000 gives megabytes. (1_000_000 is just
        # 1000000, written with underscores so it's easy to read.)

        print(f"  UPLOADED  {document['filename']}  ({size_mb:.1f} MB)")
        # Confirm this upload, e.g. "UPLOADED  infosys_fy2025.pdf  (8.2 MB)".
        # ":.1f" means: show the number with one digit after the decimal point.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # This line means: run the code below ONLY when this file is run directly,
    # with "python ingestion/upload_pdfs.py".
    # If another file imports this one, nothing below runs.

    print(f"Uploading from {config.LOCAL_PDF_DIR} to container '{config.AZURE_STORAGE_CONTAINER}'")
    # First, say where we upload FROM and TO, so we can check both before anything happens.

    upload_all()
    # Then do the work.