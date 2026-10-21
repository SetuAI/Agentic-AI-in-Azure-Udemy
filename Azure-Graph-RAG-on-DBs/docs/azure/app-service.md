# Azure App Service

## What it is used for
App Service runs the project's web server (`api/main.py`) on Azure, so the demo page and API are available at `https://<app-name>.azurewebsites.net` without your laptop being on.

| Term | Meaning |
|---|---|
| **App Service plan** | The machine the app runs on, and its price (e.g. B1: 1 core, 1.75 GB memory). Billed while it exists. |
| **Web app** | Your application, running on the plan. Has its own address, settings and logs. |
| **App settings** | The web app's environment variables. They do the job `.env` does on your laptop. |
| **Startup command** | What App Service runs to start the app. Here: `bash startup.sh`. |

**Two ways to set it up.** Part A below does everything with one script (recommended). Part B does the same in the portal, step by step, so you understand each piece. Deploying the code (Part C) always uses a script.

Set up Postgres, Azure OpenAI and Content Safety first, and check the app works on your laptop.

---

## Part A — Set up with the script (recommended)

1. Install the Azure CLI and sign in:
   ```bash
   brew install azure-cli
   az login
   ```
   A browser window opens; sign in with your Azure account. If you have several subscriptions, choose the right one:
   ```bash
   az account set --subscription "<subscription name or id>"
   ```
2. From the project folder:
   ```bash
   RESOURCE_GROUP=rg-fundgraph APP_NAME=fundgraph-demo PG_SERVER=fundgraph-pg \
       bash infra/azure/create-app-service.sh
   ```
   | Variable | Meaning |
   |---|---|
   | `RESOURCE_GROUP` | The resource group to use |
   | `APP_NAME` | Unique across Azure; becomes `https://fundgraph-demo.azurewebsites.net` |
   | `PG_SERVER` | Your Postgres server name, without `.postgres.database.azure.com` |
   | `LOCATION` (optional) | Defaults to `eastus` |
   | `PG_RESOURCE_GROUP` (optional) | If Postgres is in a different resource group |

3. The script creates the plan and web app, copies `.env` into app settings, opens the Postgres firewall for the app, and switches on Entra ID sign-in (see entra-id.md). Go to Part C.

---

## Part B — Set up in the portal

### B1. Create the web app
1. Go to https://portal.azure.com, search **App Services**, click **+ Create → Web App**.

#### Basics tab
| Field | What to enter | Why |
|---|---|---|
| Subscription | Your subscription | |
| Resource group | `rg-fundgraph` | |
| Name | e.g. `fundgraph-demo` | Unique; becomes the web address. If **Secure unique default hostname** is on, Azure adds a random part (e.g. `fundgraph-app-<random>.centralindia-01.azurewebsites.net`). Untick it to keep the simple `<name>.azurewebsites.net` address. |
| Publish | **Code** | We upload Python code, not a container |
| Runtime stack | **Python 3.13** | Match the Python on your laptop (`python --version` in the project's `.venv`). The project needs 3.12 or later. |
| Operating System | **Linux** | Python apps run on Linux |
| Region | The **same region as your Postgres server** (e.g. `Central India`) | The agent calls the database several times per question, so keeping them close makes answers faster. If the plan is not available there, pick the nearest region. |
| Linux Plan | **Create new**, name `fundgraph-demo-plan` | |
| Pricing plan | **Basic B1** (click **Explore pricing plans** to find it) | Smallest plan with **Always On**. Free F1 sleeps after 20 minutes and has too little memory. |

#### Database tab
Leave **Create a database** unticked (Postgres already exists).

#### Deployment tab
Leave **Continuous deployment** disabled. Leave **Basic authentication** as it is.

#### Networking tab
**Enable public access**: **On**.

#### Monitoring tab
**Enable Application Insights**: **No**. The app sends its own data using the connection string (see application-insights.md); switching it on here as well can record each request twice.

Click **Review + create** → **Create**. Then **Go to resource**.

### B2. App settings (the `.env` values)
1. On the web app, open **Settings → Environment variables**.
2. On the **App settings** tab, click **+ Add** for each line of your `.env` that has a value:

| Name | Value |
|---|---|
| `DATA_DB_URL` | same as `.env` |
| `AGENT_DB_URL` | same as `.env` |
| `AZURE_OPENAI_ENDPOINT` | same as `.env` |
| `AZURE_OPENAI_API_KEY` | same as `.env` |
| `CHAT_DEPLOYMENT` | same as `.env` |
| `EMBED_DEPLOYMENT` | same as `.env` |
| `CONTENT_SAFETY_ENDPOINT` | same as `.env` |
| `CONTENT_SAFETY_KEY` | same as `.env` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | same as `.env` |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` — makes App Service install `requirements.txt` when code is uploaded |

3. Click **Apply** at the bottom, then **Confirm**. The app restarts.

Anyone with access to this web app in the portal can read these values. Give portal access only to people who may see the keys.

### B3. Startup command, Always On, HTTPS
1. Open **Settings → Configuration → General settings**.
2. **Startup Command**: `bash startup.sh`
3. **Always on**: **On** (keeps the app loaded, so the first question is not slow)
4. **HTTPS Only**: **On** (if shown here; otherwise under **Settings → Configuration** or **Settings → TLS/SSL**)
5. Click **Save** → **Continue**.

### B4. Health check
1. Open **Monitoring → Health check**.
2. **Enable**, path `/health`, click **Save**.

App Service calls `/health` regularly and restarts the app if it stops answering.

### B5. Let the app reach Postgres
The web app connects to Postgres from a set of Azure addresses (often 20 to 30 of them). Each must be allowed in Postgres's firewall.
1. On the web app, open **Settings → Properties**. Copy every address under **Outbound IP addresses** and **Additional outbound IP addresses**.
2. Open your Postgres server → **Settings → Networking**.
3. For each address, click **+ Add a firewall rule**: name `app-1`, `app-2`, ...; start and end IP both set to the address.
4. Click **Save**.

**With many addresses, the terminal is quicker.** This adds one rule per address (after `az login`; run it in `bash`, not `zsh`, so the list splits into separate addresses). Postgres applies firewall changes one at a time, so 30 rules take about 10 minutes:
```bash
bash -c '
IPS=$(az webapp show -g fundgraph-rg-1 -n fundgraph-app --query possibleOutboundIpAddresses -o tsv | tr "," " ")
i=0
for ip in $IPS; do
  i=$((i+1))
  az postgres flexible-server firewall-rule create -g fundgraph-rg-1 --server-name fundgraphdbexample1 \
      --name "app-$i" --start-ip-address $ip --end-ip-address $ip -o none
done
echo "added $i rules"'
```
Replace `fundgraph-rg-1`, `fundgraph-app` and `fundgraphdbexample1` with your resource group, web app and Postgres server names.

The box **"Allow public access from any Azure service within Azure to this server"** on the Networking page also works and is one click, but it lets in every Azure customer's services, not only yours. The per-address rules are safer.

### B6. Sign-in
Follow entra-id.md.

---

## Part C — Deploy the code

Deploying means sending the project's code to the web app. One script does it: **`infra/azure/deploy-app.sh`**. Run it after setup (Part A or Part B), and again every time you change the code.

### C1. Before the first deploy, check
- [ ] The web app exists, and its **Environment variables** match your `.env` (B2), including `SCM_DO_BUILD_DURING_DEPLOYMENT` = `true`
- [ ] **Startup Command** is `bash startup.sh`, and **Always on** is On (B3)
- [ ] **Health check** path is `/health` (B4)
- [ ] Postgres allows the app's outbound addresses (B5)
- [ ] You are signed in to the Azure CLI: `az login` (and `az account set --subscription "<name>"` if you have several)

### C2. Run the deploy
From the project folder on your laptop:
```bash
cd /path/to/azure-fundgraph-rag-on-db
RESOURCE_GROUP=fundgraph-rg-1 APP_NAME=fundgraph-app bash infra/azure/deploy-app.sh
```
| Part of the command | Meaning |
|---|---|
| `RESOURCE_GROUP=fundgraph-rg-1` | The resource group the web app is in |
| `APP_NAME=fundgraph-app` | The web app's name (as shown in **App Services**, not the full address) |
| `bash infra/azure/deploy-app.sh` | Runs the deploy script |

Use your own resource group and app name.

### C3. What the script does
1. **Writes `requirements.txt` from `uv.lock`**, so Azure installs exactly the same package versions as your laptop.
2. **Zips the project**, leaving out `.env`, `.venv`, caches, `tests/`, `evals/`, `infra/` and the `.md` files. Your secrets never leave your laptop this way; on Azure they come from the web app's environment variables.
3. **Uploads the zip** (`az webapp deploy --type zip`). Because `SCM_DO_BUILD_DURING_DEPLOYMENT` is `true`, App Service installs the packages from `requirements.txt`. This takes 3 to 5 minutes the first time, and less afterwards, because packages are cached.
4. **Starts the app** with `startup.sh`, which runs:
   ```bash
   python -m uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
   ```
   App Service sets `PORT`. One worker, because conversation history is held in memory (see "Important limits").
5. **Waits up to 5 minutes for `/health`** to answer, then prints the address.

### C4. If the deploy says `504 GatewayTimeout`
On the first deploy, the upload request can time out while Azure is still installing packages. The script then stops with:
```
ERROR: Deployment failed. ... Error Code : HTTP_504 ... Raw Error : 504.0 GatewayTimeout
```
**This usually does not mean the deploy failed.** The install carries on in Azure. Check the real result:
```bash
az webapp log deployment list -g fundgraph-rg-1 -n fundgraph-app \
    --query "[0].{status:status, complete:complete, active:active, end:end_time}" -o table
```
| `status` | Meaning |
|---|---|
| `4`, with `active` = `True` | Succeeded, and this version is live |
| `3` | Failed: open **Deployment → Deployment Center → Logs** to see why |
| `0`, `1` or `2` | Still running: wait a minute and check again |

The same information is in the portal: web app → **Deployment → Deployment Center → Logs**.

### C5. Check the live site
1. Find the address: web app → **Overview** → **Default domain**, or run:
   ```bash
   az webapp show -g fundgraph-rg-1 -n fundgraph-app --query defaultHostName -o tsv
   ```
2. Health (no sign-in needed):
   ```bash
   curl https://<default-domain>/health
   ```
   Expected: `{"status":"ok"}`.
3. Open `https://<default-domain>` in a browser. With sign-in on (entra-id.md), you are sent to the Microsoft sign-in page first. Sign in with your work account.
4. You should see:
   - your name and **Sign out** at the top right,
   - **Records per table** showing the row total (proof the app reached Postgres), and
   - an answer when you ask a question (proof it reached Azure OpenAI and Content Safety).

### C6. Updating the app later
After any code change, run the same command again:
```bash
RESOURCE_GROUP=fundgraph-rg-1 APP_NAME=fundgraph-app bash infra/azure/deploy-app.sh
```
Settings in the portal (environment variables, startup command, sign-in, firewall) stay as they are; only the code is replaced. The app restarts, so conversation history is cleared.

### Example: the deployment done for this project
| Item | Value |
|---|---|
| Resource group | `fundgraph-rg-1` |
| Web app | `fundgraph-app`, Python 3.13, Linux, plan B1, Central India |
| Address | `https://fundgraph-app-<random>.centralindia-01.azurewebsites.net` (shown under **Overview → Default domain**) |
| Postgres | `fundgraphdbexample1` (Central India); 31 firewall rules `app-1` to `app-31` for the app's outbound addresses |
| Startup command | `bash startup.sh`; Always On on; health check `/health` |
| Sign-in | Microsoft (Entra ID), current tenant only, `/health` excluded |
| Deploy command | `RESOURCE_GROUP=fundgraph-rg-1 APP_NAME=fundgraph-app bash infra/azure/deploy-app.sh` |
| Result | Upload returned 504, but the deployment succeeded (status 4, active) in about 4 minutes |

---

## Viewing logs

| What | Where |
|---|---|
| Live output of the app (prints, errors) | Portal → web app → **Monitoring → Log stream**. First switch on **Monitoring → App Service logs → Application logging: File System → Save**. |
| Same, from the terminal | `az webapp log tail -n fundgraph-demo -g rg-fundgraph` |
| Deployment and package install log | **Deployment → Deployment Center → Logs** |
| Automatic diagnosis of common problems | **Diagnose and solve problems** |

---

## Important limits for this project

- **Keep it at 1 instance** (**Settings → Scale out** stays at 1) and 1 worker (`startup.sh`). Conversation history is held in the app's memory; a second instance would not see it, and follow-up questions would lose their context. This lasts until Stage 8 moves history into Postgres.
- **History is lost on restart.** Every deploy or settings change restarts the app.
- **Cost**: the B1 plan is billed every hour it exists, even if the app is stopped. To stop the charge, delete the plan: **App Service plans → fundgraph-demo-plan → Delete**.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Application Error` page, or `/health` never answers after deploy | App failed to start | Check **Log stream**. Common causes: an app setting missing (e.g. `KeyError: 'DATA_DB_URL'`), or packages not installed (`SCM_DO_BUILD_DURING_DEPLOYMENT` not `true`). |
| `ModuleNotFoundError` | Packages not installed | Set `SCM_DO_BUILD_DURING_DEPLOYMENT=true`, redeploy |
| Questions fail with `connection timeout` | Postgres firewall does not allow the app | Part B5 (or re-run the setup script) |
| Page loads but every question returns `401` | Sign-in switched off or misconfigured | entra-id.md |
| Follow-up questions do not use the previous question | More than one instance, or the app restarted | Keep 1 instance; see "Important limits" |
| First question after a while is slow | Always On is off | Part B3 |
