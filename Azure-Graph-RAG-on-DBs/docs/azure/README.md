# Azure setup — start here

This folder explains, step by step, how to create and connect every Azure service this project uses. Each guide works through the Azure portal (the website at https://portal.azure.com) and ends with the exact values to put in `.env` and a test that proves the connection works.

Portal screens change from time to time. If a button has moved, use the search bar at the top of the portal and type the name of the page you are looking for.

---

## The services, and the order to set them up

| # | Guide | What the service does in this project | Needed for |
|---|---|---|---|
| 1 | [postgres.md](postgres.md) | Holds the fund data, the table notes, the embeddings and the join graph | Everything |
| 2 | [azure-openai.md](azure-openai.md) | The models that understand questions, write SQL and commentary, and make embeddings | Metadata pipeline, agent |
| 3 | [content-safety.md](content-safety.md) | Checks each question for prompt attacks | Agent (step 1) |
| 4 | [application-insights.md](application-insights.md) | Records every request and agent step, with timings | Optional monitoring |
| 5 | [app-service.md](app-service.md) | Hosts the API and demo page on the internet | Running outside your laptop |
| 6 | [entra-id.md](entra-id.md) | Makes users sign in with their work account before using the app | Running outside your laptop |

Do 1 to 4 first: with those, the whole project runs on your laptop against Azure. Do 5 and 6 when you want to host it.

---

## Before you start

### 1. An Azure account and subscription
1. Go to https://portal.azure.com and sign in.
   - No account yet: go to https://azure.microsoft.com/free and create one. It needs a phone number and a card for identity checks.
2. In the top search bar type **Subscriptions** and open it.
3. Check you have at least one subscription with status **Active**. Every resource you create is billed to a subscription.

Your role on the subscription must be **Owner** or **Contributor** to create resources. You can see it under **Subscriptions → your subscription → Access control (IAM) → View my access**.

### 2. A resource group
A resource group is a folder for related resources. Putting everything for this project in one group means you can see the total cost in one place, and delete everything at once when you are finished.

1. Search **Resource groups** → **+ Create**.
2. **Subscription**: your subscription.
3. **Resource group**: `rg-fundgraph`.
4. **Region**: `East US` (use the same region for every service in these guides; it keeps the services close together and avoids cross-region charges).
5. **Review + create** → **Create**.

### 3. Tools on your Mac
| Tool | Why | Install |
|---|---|---|
| `uv` | Runs the project's Python code | Already installed |
| Azure CLI (`az`) | Needed only for App Service deployment | `brew install azure-cli`, then `az login` |

You do **not** need Postgres or Docker installed locally.

### 4. The `.env` file
In the project folder:
```bash
cp .env.example .env
```
Each guide tells you which lines to fill in. The finished file looks like this:

```dotenv
DATA_DB_URL=postgresql://<admin>:<password>@<server>.postgres.database.azure.com:5432/mf_data?sslmode=require
AGENT_DB_URL=postgresql://<admin>:<password>@<server>.postgres.database.azure.com:5432/agent_meta?sslmode=require

AZURE_OPENAI_ENDPOINT=https://<openai-resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<key>
CHAT_DEPLOYMENT=gpt-5.1
EMBED_DEPLOYMENT=text-embedding-3-small

CONTENT_SAFETY_ENDPOINT=https://<content-safety-resource>.cognitiveservices.azure.com/
CONTENT_SAFETY_KEY=<key>

APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...;IngestionEndpoint=...

LANGSMITH_TRACING=false
```

`.env` holds secrets. It is listed in `.gitignore` and must never be committed or shared.

---

## Controlling cost

- **Cost Management**: search **Cost Management → Budgets → + Add**, scope `rg-fundgraph`, set a monthly amount, and add an email alert at 80%.
- **Stop what you are not using**: the Postgres server can be stopped from its Overview page (see postgres.md). App Service is billed while the plan exists, even when the app is stopped.
- **Delete everything when finished**: **Resource groups → rg-fundgraph → Delete resource group**. This removes every resource in it and stops all charges. It cannot be undone.
