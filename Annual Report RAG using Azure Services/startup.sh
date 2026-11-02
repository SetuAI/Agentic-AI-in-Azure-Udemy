#!/bin/bash
# =============================================================================
# startup.sh  -  how Azure App Service starts the app
# =============================================================================
#
# PURPOSE
#   App Service runs this file every time the web app starts: after each
#   deploy, after a restart, and after a settings change. It is set as the
#   web app's "startup command" (see hosting.md, Step 7).
#
# WHAT IT DOES
#   Starts UI/server.py, the same server you run on your laptop. On Azure:
#     - App Service sets PORT; server.py reads it
#     - App Service sets WEBSITE_SITE_NAME; server.py sees it and listens on
#       0.0.0.0, so Azure's front end can reach it
#     - App Service has already installed requirements.txt and switched on
#       that Python environment, so plain "python" has every package
#
# WHY "-u"
#   -u = unbuffered. Python normally holds printed lines back and writes them
#   in batches. With -u, every print (QUERY ..., REVIEW ... by ...) appears in
#   the log stream immediately, which is what you want when watching it live.
#
# ONE PROCESS ONLY
#   Paused reviews are kept in this one process's memory (MemorySaver in
#   graph/build_graph.py). Keep the web app at ONE instance, or a reviewer's
#   "Approve" could reach a copy that never saw the paused answer.
#
# EXPECTED OUTPUT (in the log stream)
#   Application Insights: sending traces as "annual-report-rag"
#   Annual-report RAG UI on http://0.0.0.0:8000   (Ctrl+C to stop)
# =============================================================================

python -u UI/server.py