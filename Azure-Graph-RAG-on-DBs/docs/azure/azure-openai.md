# Azure OpenAI

## What it is used for
Two models, both called from `metadata/llm.py`:

| Model | Deployment name the code expects | Used for |
|---|---|---|
| `gpt-5.1` (chat) | `gpt-5.1` | Understanding the question, drafting table notes, writing SQL, writing commentary |
| `text-embedding-3-small` | `text-embedding-3-small` | Turning table notes, names and questions into 1,536 numbers for pgvector search |

**Resource vs deployment.** You first create an Azure OpenAI *resource* (the account, with an endpoint and keys). Inside it you create *deployments*: each deployment is one model, under a name you choose. The code calls a model by its **deployment name**, which is set in `config/models.yaml`.

---

## Part 1 — Create the Azure OpenAI resource

1. Go to https://portal.azure.com.
2. In the top search bar type **Azure OpenAI** and open it.
3. Click **+ Create**.

### Basics tab
| Field | What to enter | Why |
|---|---|---|
| Subscription | Your subscription | |
| Resource group | `rg-fundgraph` | |
| Region | `East US`, or another region where both models are available (see below) | |
| Name | e.g. `fundgraph-openai` | Unique across Azure. Becomes `https://fundgraph-openai.openai.azure.com/` |
| Pricing tier | **Standard S0** | The only tier; you pay per token used |

**Checking model availability.** Not every model is offered in every region. Search the web for "Azure OpenAI models region availability" and open Microsoft's model table. Check that `gpt-5.1` and `text-embedding-3-small` are both listed for your region under **Global Standard**.

### Network tab
- **Type**: **All networks, including the internet, can access this resource.** (Your laptop and App Service connect over the internet with a key.)

### Remaining tabs
Leave the defaults. **Review + submit** → **Create**. It takes about a minute. Then **Go to resource**.

---

## Part 2 — Deploy the two models

Deployments are managed in the Foundry portal, a separate site linked from the resource.

1. On the resource's **Overview** page click **Go to Foundry portal** (the button may say *Azure AI Foundry* or *Microsoft Foundry*). It opens https://ai.azure.com with your resource selected.
2. In the left menu open **Deployments** (it may be under **Shared resources** or **My assets → Models + endpoints**).
3. Click **+ Deploy model** → **Deploy base model**.

### Deployment 1: the chat model
1. Search `gpt-5.1`, select it, click **Confirm**.
2. Fill in:

| Field | What to enter | Why |
|---|---|---|
| Deployment name | `gpt-5.1` | Must match `config/models.yaml` exactly |
| Deployment type | **Global Standard** | Highest availability and quota |
| Tokens per minute rate limit | As high as your quota allows (at least 100K if possible). Click **Customize** to see it. | Drafting notes for 20 tables sends several requests at once; a low limit causes waiting |

3. Click **Deploy**.

### Deployment 2: the embedding model
1. **+ Deploy model → Deploy base model**, search `text-embedding-3-small`, **Confirm**.
2. **Deployment name**: `text-embedding-3-small`. **Deployment type**: **Global Standard** (or **Standard** if Global is not offered). Rate limit: as high as allowed.
3. Click **Deploy**.

Both should now show under **Deployments** with state **Succeeded**.

**If you choose different deployment names**, change them in two places:
- `config/models.yaml` (`chat.small`, `chat.strong`, `embedding`)
- `.env` (`CHAT_DEPLOYMENT`, `EMBED_DEPLOYMENT`)

---

## Part 3 — Copy the endpoint and key

1. Back in the Azure portal, on the resource, open **Resource Management → Keys and Endpoint**.
2. Copy **KEY 1** (click the copy icon; **Show keys** reveals it).
3. Copy **Endpoint**, e.g. `https://fundgraph-openai.openai.azure.com/`.

There are two keys so you can change one while the other is still in use. Either works.

---

## Part 4 — Fill in `.env`

```dotenv
AZURE_OPENAI_ENDPOINT=https://fundgraph-openai.openai.azure.com/
AZURE_OPENAI_API_KEY=<KEY 1>
CHAT_DEPLOYMENT=gpt-5.1
EMBED_DEPLOYMENT=text-embedding-3-small
```

| Variable | Read by |
|---|---|
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` | `metadata/llm.py` (all model calls) and the test |
| `CHAT_DEPLOYMENT`, `EMBED_DEPLOYMENT` | `tests/test_azure_openai.py` only. The app itself reads deployment names from `config/models.yaml`. |

---

## Part 5 — Test the connection

```bash
uv run pytest tests/test_azure_openai.py -v
```
Both tests should pass: the chat model returns JSON, and an embedding has 1,536 numbers.

---

## Day-to-day

| Task | Where |
|---|---|
| See token usage and cost | Azure portal → resource → **Monitoring → Metrics**, metric **Processed Inference Tokens**; or **Cost Management** |
| Raise a rate limit | Foundry portal → **Deployments** → deployment → **Edit** |
| See your quota per region | Foundry portal → **Quota** |
| Change a key (rotation) | **Keys and Endpoint → Regenerate Key 2**, put Key 2 in `.env`, then regenerate Key 1 |
| Change `reasoning_effort` (speed vs quality) | `config/models.yaml` |

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `401 Unauthorized` / `Access denied due to invalid subscription key` | Wrong key, or key from a different resource | Copy KEY 1 again (Part 3) |
| `404 DeploymentNotFound` / `The API deployment for this resource does not exist` | Deployment name in `config/models.yaml` or `.env` differs from the one in Foundry | Make the names match exactly (Part 2) |
| `404 Resource not found` | Endpoint wrong | Use the endpoint from **Keys and Endpoint**, ending in `.openai.azure.com/` |
| `429 Too Many Requests` | Tokens-per-minute limit reached | The code already retries 6 times. If it persists, raise the deployment's rate limit, or lower `notes.parallel_calls` in `config/metadata.yaml` |
| Model not in the list when deploying | Not available in this region, or no quota | Check the region table; create the resource in another region |
| `400` mentioning `reasoning_effort` | Model does not accept it | The code retries without it automatically; or remove `reasoning_effort` from `config/models.yaml` |
