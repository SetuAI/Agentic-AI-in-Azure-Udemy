
# Azure AI Search — the past postmortems

## What it does in this project

The log tools answer *"what broke, and why?"*. But an on-call engineer also wants to know: *"has this happened before, and what fixed it last time?"* That answer isn't in the logs. It's in old **postmortems**: the short reports a team writes after each incident.

So we put our 8 made-up postmortems (in the `postmortems/` folder) into **Azure AI Search**, and give the AI a tool to search them: `search_postmortems`.

```
Once, at setup (your laptop):
  postmortems/PM-*.md ──► build_index.py ──► Azure AI Search index "postmortems"

Every question:
  AI app ──► search_postmortems("payments database connection pool timeouts")
         ──► keyword search ──► semantic ranker puts the best match first
         ──► Prompt Shields checks the text ──► the top 3 postmortems go back to the AI
```

**No embeddings, no Azure OpenAI.** We use **keyword search plus the semantic ranker**. The semantic ranker reads the question and each postmortem together, and puts the best match first. For 8 short reports, that finds the right one, and the project keeps zero model cost on Azure.

> Portal screens change from time to time. If a button has a slightly different name, look for the closest match.

---

## Step 1 — Create the search service

1. In the Azure portal, search for **AI Search** → **Create**.
2. **Resource group:** the same one, `rg-incident-logs`.
3. **Service name:** for example `srch-incident-logs-<your-initials>`. It becomes the web address, so it must be unique.
4. **Location:** the same region as the workspace.
5. **Pricing tier:** **Free** is enough for 8 postmortems.
6. **Review + create** → **Create**.

## Step 2 — Allow Entra ID sign-in

Our code signs in with Entra ID, not with a key. A new search service accepts only keys, so switch Entra ID on:

1. Open the search service → **Settings → Keys**.
2. **API access control:** choose **Both** (keys and role-based access control) → **Save**.

## Step 3 — Switch on the semantic ranker

1. **Settings → Semantic ranker**.
2. Choose **Free**. It allows a limited number of semantic searches each month, which is plenty for this course.

## Step 4 — Give our two identities their roles

Open the search service → **Access control (IAM)** → **Add** → **Add role assignment**, once for each row:

| Role                                    | Assign to                                     | Why                                                                                       |
| --------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Search Service Contributor**    | the app registration `incident-logs-loader` | lets `build_index.py` create the index                                                  |
| **Search Index Data Contributor** | the app registration `incident-logs-loader` | lets `build_index.py` upload the postmortems, and lets you test the tool on your laptop |
| **Search Index Data Reader**      | the web app's **managed identity**      | lets the server on Azure **search** the index, and nothing more                     |

For the managed identity, choose **Assign access to: Managed identity** → **App Service** → your web app. Do that row after the web app exists, in [app-service.md](app-service.md).

New roles can take up to 10 minutes to start working.

## Step 5 — Copy the address into `.env`

Search service → **Overview** → copy the **Url**:

```
AI_SEARCH_ENDPOINT=https://srch-incident-logs-ab.search.windows.net
POSTMORTEM_INDEX=postmortems
```

## Step 6 — Build the index

From the project folder:

```
uv run python -m postmortems.build_index
```

**Expected:**

```
Read 8 postmortems from postmortems/
Index 'postmortems' is ready
Uploaded 8 of 8 postmortems
```

Check it in the portal: search service → **Indexes** → `postmortems`. **Document count** shows **8**. (It can take a minute to update.)

## Step 7 — Test the tool

Start the server, open the MCP Inspector, and run:

- **Tool:** `search_postmortems`
- **query:** `payments database connection pool timeouts`
- **service:** `payments`

**Expected:** the first result is **PM-2026-03 · Payments ran out of database connections after a config change**, with the highest `relevance` (the semantic ranker's score, from 0 to 4). Its `fix` says the rollback restored the pool size.

Then ask your AI app: *"Has this payments problem happened before, and what fixed it?"* A good answer names PM-2026-03 from March, says the same kind of change (a smaller connection pool during the auto-debit job) caused it, and that the fix was a rollback, plus the lesson: load-test pool changes with the auto-debit traffic.

---

## The postmortems, and which incident each one matches

| Postmortem | About                                                        | Matches planted incident    |
| ---------- | ------------------------------------------------------------ | --------------------------- |
| PM-2026-03 | payments ran out of database connections after a pool change | **1. Bad deployment** |
| PM-2025-11 | OTPs not delivered during a QuickSMS outage                  | **2. SMS outage**     |
| PM-2026-01 | disbursements rejected: beneficiary account frozen (BNK-417) | **3. req-8f3a21**     |
| PM-2025-09 | credit bureau timeouts (normal background noise)             | —                          |
| PM-2026-05 | OTPs blocked by a wrong api-gateway rate limit               | —                          |
| PM-2025-12 | double debits by the auto-debit job                          | —                          |
| PM-2026-07 | wrong amount in an SMS template                              | —                          |
| PM-2026-08 | loan status page slow after the table grew                   | —                          |

The 5 non-matching ones are there on purpose: the search has to pick the **right** one, not just any one.

To add your own postmortem: copy one file, keep the same headings, give it a new `PM-` id, and run `build_index.py` again.

---

## If something goes wrong

| What you see                                        | Cause                                                    | Fix                                                       |
| --------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------- |
| `AI_SEARCH_ENDPOINT is empty`                     | not in `.env` (or in the web app's settings)           | Step 5                                                    |
| `Forbidden` / `403`                             | Entra ID sign-in isn't allowed, or a role is missing     | Steps 2 and 4, then wait 10 minutes                       |
| `SemanticQueriesNotAvailable` or a semantic error | the semantic ranker is off                               | Step 3                                                    |
| `The index 'postmortems' was not found`           | the index wasn't built                                   | Step 6                                                    |
| `Uploaded 0 of 8`                                 | the app registration can create the index but not upload | the **Search Index Data Contributor** row in Step 4 |

## Cost

The **Free** tier costs nothing and holds up to 50 MB, far more than 8 postmortems. The semantic ranker's **Free** plan has a monthly limit on searches, which is plenty for recording.
