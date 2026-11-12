#!/usr/bin/env bash
# =============================================================================
# FILE        : setup_project.sh
# PROJECT     : Moderating Comments at Scale on Azure (FDE project)
#
# PURPOSE     : Creates the full folder and file layout for the project in one
#               go, plus a filled-in .env.example, .gitignore and requirements.txt.
#
# PROBLEM IT SOLVES:
#               The project has 8 folders and ~45 files. Creating them by hand
#               is slow and easy to get wrong. This script creates all of them
#               with the exact names used in the lessons.
#
# HOW TO RUN  : bash setup_project.sh
#               (run it from the folder where you want the project to be created)
#
# EXPECTED OUTPUT:
#               A new folder "comment-moderation-at-scale" with every sub-folder
#               and empty file in place, and the message "Project created" at the end.
#               Python files are empty - we fill them lesson by lesson.
# =============================================================================

set -e                                   # stop the script the moment any command fails, so we never end up with a half-built project

PROJECT="comment-moderation-at-scale"    # the name of the main project folder

if [ -d "$PROJECT" ]; then               # check whether a folder with this name already exists
  echo "Folder '$PROJECT' already exists. Delete or rename it first."   # tell the user why we are stopping
  exit 1                                 # stop here so we never overwrite someone's existing work
fi

mkdir "$PROJECT"                         # create the main project folder
cd "$PROJECT"                            # move inside it, so every path below is created inside the project

# -----------------------------------------------------------------------------
# Step 1: create all folders
# -----------------------------------------------------------------------------
mkdir -p 00_fde_docs                     # FDE documents: client brief, design brief, policy, runbook
mkdir -p 01_data/output                  # data scripts, and the output folder where generated data is saved
mkdir -p 02_concepts                     # small one-idea files that teach each Azure service on its own
mkdir -p 03_evaluation/reports           # baseline evaluation code, and the folder where reports are saved
mkdir -p 04_pipeline/stage2_agent        # the real application, with a sub-folder for the LangGraph agent
mkdir -p 05_review_ui/templates          # the moderator review app, with a folder for its HTML page
mkdir -p 06_simulation                   # scripts that send normal traffic and the viral spike
mkdir -p 07_shadow_mode                  # shadow mode run and the comparison with human decisions
mkdir -p 08_deploy                       # Dockerfiles and manual deployment steps

# -----------------------------------------------------------------------------
# Step 2: create all empty files (they are filled in lesson by lesson)
# -----------------------------------------------------------------------------
touch README.md                          # project overview, how to run, results

touch 00_fde_docs/client_brief.md        # who Khabar Live is and what problem they have
touch 00_fde_docs/agent_design_brief.md  # the 7-field agent design brief, filled in for this client
touch 00_fde_docs/moderation_policy.md   # the client's moderation rules (HS-01, HS-02, SP-01 ...)
touch 00_fde_docs/runbook.md             # handover guide for the client's team

touch 01_data/generate_articles.py       # creates about 50 news articles
touch 01_data/generate_comments.py       # creates about 5,000 labelled comments and the eval set
touch 01_data/generate_blocklist.py      # creates the regional slur blocklist
touch 01_data/generate_spike.py          # creates the 10,000-comments-per-minute spike file
touch 01_data/output/.gitkeep            # empty marker so git keeps the output folder even when it has no data

touch 02_concepts/01_content_safety_basics.py     # one Content Safety call, read the scores
touch 02_concepts/02_custom_blocklist.py          # create and use a custom blocklist
touch 02_concepts/03_event_hubs_send_receive.py   # send and read one message on Event Hubs
touch 02_concepts/04_service_bus_queue.py         # put and take one message on Service Bus
touch 02_concepts/05_cosmos_db_write_read.py      # save and read one decision in Cosmos DB
touch 02_concepts/06_langgraph_tool_agent.py      # a tiny LangGraph agent with one tool

touch 03_evaluation/run_baseline.py      # runs Content Safety alone on the eval set
touch 03_evaluation/metrics.py           # accuracy, missed abuse, wrong removals
touch 03_evaluation/reports/.gitkeep     # keeps the reports folder in git until the first report is written

touch 04_pipeline/config.py              # reads .env and holds the thresholds
touch 04_pipeline/schemas.py             # the comment and decision formats
touch 04_pipeline/stage1_fast_check.py   # Stage 1: Content Safety + blocklist
touch 04_pipeline/stage2_agent/tools.py  # agent tools: get_thread, get_user_history, get_policy_rule
touch 04_pipeline/stage2_agent/prompts.py   # the agent's instructions
touch 04_pipeline/stage2_agent/graph.py     # the LangGraph agent itself
touch 04_pipeline/stage3_human_queue.py  # Stage 3: sends unclear comments to Service Bus
touch 04_pipeline/spike_guard.py         # viral detection and HOLD mode
touch 04_pipeline/decision_store.py      # writes every decision to Cosmos DB
touch 04_pipeline/telemetry.py           # sends counts and timings to Application Insights
touch 04_pipeline/consumer.py            # reads Event Hubs and runs the stages in order

touch 05_review_ui/app.py                # FastAPI server for moderators
touch 05_review_ui/templates/review.html # the Keep / Remove screen

touch 06_simulation/replay_normal_traffic.py   # sends normal comments to Event Hubs
touch 06_simulation/replay_viral_spike.py      # sends 10,000 comments in one minute

touch 07_shadow_mode/run_shadow.py             # pipeline runs and records, but takes no action
touch 07_shadow_mode/compare_with_humans.py    # produces the shadow mode report

touch 08_deploy/Dockerfile.agent         # container image for the pipeline and agent
touch 08_deploy/Dockerfile.review_ui     # container image for the review UI
touch 08_deploy/deploy_steps.md          # manual portal steps for Azure Container Apps

# -----------------------------------------------------------------------------
# Step 3: write .gitignore so secrets and junk never reach GitHub
# -----------------------------------------------------------------------------
cat > .gitignore <<'EOF'
# real secrets live here - never commit it
.env

# Python cache and virtual environments
__pycache__/
*.pyc
.venv/
venv/

# generated data and reports (students regenerate them by running the scripts)
01_data/output/*
!01_data/output/.gitkeep
03_evaluation/reports/*
!03_evaluation/reports/.gitkeep
EOF

# -----------------------------------------------------------------------------
# Step 4: write requirements.txt (Python packages the project needs)
# -----------------------------------------------------------------------------
cat > requirements.txt <<'EOF'
# Azure AI Content Safety - Stage 1 fast check and custom blocklist
azure-ai-contentsafety

# Azure Event Hubs - receives the stream of incoming comments
azure-eventhub

# Azure Service Bus - queue of comments waiting for a human moderator
azure-servicebus

# Azure Cosmos DB - stores every moderation decision
azure-cosmos

# Application Insights - counts, latency and cost per stage
azure-monitor-opentelemetry

# Stage 2 agent - LangGraph on Azure OpenAI
langgraph
langchain-openai

# Review UI
fastapi
uvicorn
jinja2

# reads the .env file
python-dotenv
EOF

# -----------------------------------------------------------------------------
# Step 5: write .env.example (copy it to .env and fill in your own values)
# The quotes around 'EOF' stop bash from treating $Default as a variable.
# -----------------------------------------------------------------------------
cat > .env.example <<'EOF'
# =============================================================================
# .env.example - Moderating Comments at Scale on Azure
# Copy this file to .env and fill in your own values from the Azure portal:
#   cp .env.example .env
# Never commit .env to GitHub. It is already listed in .gitignore.
# =============================================================================

# -----------------------------------------------------------------------------
# Azure OpenAI - used by the Stage 2 agent
# Portal: your Azure OpenAI resource -> Keys and Endpoint
# -----------------------------------------------------------------------------
AZURE_OPENAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<your-openai-key>
AZURE_OPENAI_API_VERSION=<api-version-shown-in-your-portal>
AZURE_OPENAI_CHAT_DEPLOYMENT=<your-chat-model-deployment-name>

# -----------------------------------------------------------------------------
# Azure AI Content Safety - used by Stage 1
# Portal: your Content Safety resource -> Keys and Endpoint
# -----------------------------------------------------------------------------
AZURE_CONTENT_SAFETY_ENDPOINT=https://<your-content-safety-resource>.cognitiveservices.azure.com/
AZURE_CONTENT_SAFETY_KEY=<your-content-safety-key>
AZURE_CONTENT_SAFETY_BLOCKLIST_NAME=khabar-live-regional-slurs

# -----------------------------------------------------------------------------
# Azure Event Hubs - receives the stream of incoming comments
# Portal: Event Hubs namespace -> Shared access policies -> connection string
# -----------------------------------------------------------------------------
EVENTHUB_CONNECTION_STRING=Endpoint=sb://<your-namespace>.servicebus.windows.net/;SharedAccessKeyName=<policy-name>;SharedAccessKey=<key>
EVENTHUB_NAME=comments-stream
EVENTHUB_CONSUMER_GROUP=$Default

# -----------------------------------------------------------------------------
# Azure Service Bus - queue of comments waiting for a human moderator
# Portal: Service Bus namespace -> Shared access policies -> connection string
# -----------------------------------------------------------------------------
SERVICEBUS_CONNECTION_STRING=Endpoint=sb://<your-namespace>.servicebus.windows.net/;SharedAccessKeyName=<policy-name>;SharedAccessKey=<key>
SERVICEBUS_QUEUE_NAME=human-review

# -----------------------------------------------------------------------------
# Azure Cosmos DB - stores every moderation decision
# Portal: your Cosmos DB account -> Keys
# -----------------------------------------------------------------------------
COSMOS_ENDPOINT=https://<your-cosmos-account>.documents.azure.com:443/
COSMOS_KEY=<your-cosmos-primary-key>
COSMOS_DATABASE=moderation
COSMOS_CONTAINER_DECISIONS=decisions

# -----------------------------------------------------------------------------
# Application Insights - dashboard for counts, latency and cost
# Portal: your Application Insights resource -> Overview -> Connection String
# -----------------------------------------------------------------------------
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=<key>;IngestionEndpoint=<endpoint>

# -----------------------------------------------------------------------------
# LangSmith (optional) - traces each step the Stage 2 agent takes
# -----------------------------------------------------------------------------
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=<your-langsmith-key>
LANGSMITH_PROJECT=comment-moderation-at-scale

# -----------------------------------------------------------------------------
# Thresholds - these are the business decisions agreed with the client
# -----------------------------------------------------------------------------
# Stage 1: Content Safety gives a severity score from 0 (safe) upward.
# Below this score -> KEEP straight away.
STAGE1_KEEP_BELOW_SEVERITY=2
# At or above this score -> REMOVE straight away.
STAGE1_REMOVE_AT_SEVERITY=6
# Anything in between goes to the Stage 2 agent.

# Stage 2: the agent must be at least this confident (0 to 1) to decide alone.
# Below this -> sent to a human.
STAGE2_MIN_CONFIDENCE=0.80

# Stage 3: how many comments per minute the moderators can handle.
# 12 moderators x 3 decisions per minute = 36.
MODERATOR_COUNT=12
DECISIONS_PER_MODERATOR_PER_MIN=3
HUMAN_QUEUE_LIMIT_PER_MIN=36

# Spike guard: an article counts as viral above this many comments per minute.
VIRAL_COMMENTS_PER_MIN=500

# -----------------------------------------------------------------------------
# Run mode
# live   -> decisions are acted on (comments kept, removed or held)
# shadow -> decisions are only recorded, no action taken
# -----------------------------------------------------------------------------
RUN_MODE=shadow
EOF

echo "Project created in ./$PROJECT"     # final message so the user knows everything worked
echo "Next: cd $PROJECT && cp .env.example .env, then fill in your Azure values."   # tell the user the very next step
