"""
================================================================================
INGESTION STEP 0 - PUT THE PDFs IN BLOB STORAGE
================================================================================

Run this ONCE. It copies the four annual reports from your computer into an
Azure Blob Storage container. From then on, the pipeline reads them from the
cloud, never from your disk.

WHY BOTHER, WHEN THE FILES ARE ALREADY ON DISK?
-----------------------------------------------
Because in a real deployment there is no "your disk". The ingestion job runs on
a server, a container or a scheduled function, and the documents arrive from
somewhere else -- an upload form, another team, a nightly export. Blob Storage is
the usual "somewhere else" on Azure: cheap, durable, reachable from any service.

HOW TO RUN
----------
    python ingestion/upload_pdfs.py
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path joins folder and file names safely

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable, ahead of anything else

import config                                   # settings: which files, which folder, which container
from azure_clients import blob_container        # the client for our Blob Storage container


def upload_all() -> None:
    """Upload every annual report listed in config.DOCUMENTS."""
    container = blob_container()                # connect to the container named in config

    if not container.exists():                  # first run? the container may not be there yet
        container.create_container()            # create it -- private by default, nobody can read it anonymously

    for document in config.DOCUMENTS:           # go through the four reports one by one
        local_path = config.LOCAL_PDF_DIR / document["filename"]  # where the PDF sits on this computer

        if not local_path.exists():             # missing locally? say so and move on
            print(f"  SKIPPED   {document['filename']}  (not found in {config.LOCAL_PDF_DIR})")  # tell the user
            continue                            # skip to the next document

        if local_path.stat().st_size < 1000:    # a real report is megabytes; a tiny file is a Git LFS pointer
            print(f"  SKIPPED   {document['filename']}  (looks like a Git LFS pointer -- run 'git lfs pull')")
            continue                            # uploading a pointer file would be useless

        with open(local_path, "rb") as pdf_file:  # open the PDF in binary mode ("rb" = read bytes)
            container.upload_blob(              # send it to Blob Storage
                name=document["filename"],      # the blob's name -- same as the file's name
                data=pdf_file,                  # the bytes to upload, streamed from disk
                overwrite=True,                 # replace an older copy instead of failing
            )

        size_mb = local_path.stat().st_size / 1_000_000  # file size in megabytes, for the printout
        print(f"  UPLOADED  {document['filename']}  ({size_mb:.1f} MB)")  # confirm each upload


if __name__ == "__main__":                      # only runs when this file is executed directly
    print(f"Uploading from {config.LOCAL_PDF_DIR} to container '{config.AZURE_STORAGE_CONTAINER}'")  # where from/to
    upload_all()                                # do the work
