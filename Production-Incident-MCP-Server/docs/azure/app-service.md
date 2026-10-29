
# Azure App Service — where the server runs

## What it does in this project

- It runs our MCP server on the internet, at `https://<your-app-name>.azurewebsites.net/mcp`.
- VS Code, Claude Code and the Claude desktop app connect to that address.
- It runs the Python code straight from a zip file: no Docker needed.

What happens when you deploy:

```
app.zip ──► App Service ──► installs requirements.txt ──► runs startup.sh ──► server listens on /mcp
```

What happens on every question:

```
AI app ──► https://<app>.azurewebsites.net/mcp ──► API key check ──► tool
       ──► signs in with the managed identity ──► searches Log Analytics ──► answer back to the AI app
```

> Portal screens change from time to time. If a button has a slightly different name, look for the closest match.

---

## Step 1 — Create the web app

1. In the Azure portal, search for **App Services** → **Create** → **Web App**.
2. **Resource group:** the same one, `rg-incident-logs`.
3. **Name:** for example `incident-desk-<your-initials>`. It must be unique across Azure, because it becomes the web address.
4. **Publish:** Code. **Runtime stack:** Python 3.12. **Operating system:** Linux.
5. **Region:** the same region as the workspace.
6. **Pricing plan:** Basic B1 is enough. (Free F1 also works for testing, but it goes to sleep and its first answer is slow.)
7. **Review + create** → **Create**.

## Step 2 — Switch on the managed identity

The managed identity is the web app's own Azure identity. Azure manages its password, so we never see or store one.

1. Open the web app → **Settings → Identity** → **System assigned** → **Status: On** → **Save** → **Yes**.
2. Open your Log Analytics workspace → **Access control (IAM)** → **Add role assignment**.
3. Role: **Log Analytics Reader** → **Next**.
4. **Assign access to:** Managed identity → **Select members** → pick **App Service** → pick your web app → **Select** → **Review + assign**.

## Step 3 — Add the settings

Open the web app → **Settings → Environment variables** → **App settings**. Add each one with **+ Add**:

| Name                               | Value                                                        |
| ---------------------------------- | ------------------------------------------------------------ |
| `LOG_ANALYTICS_WORKSPACE_ID`     | same as in your `.env`                                     |
| `MCP_API_KEY`                    | same as in your `.env` (or a new one just for Azure)       |
| `DISPLAY_TIMEZONE`               | `Asia/Kolkata`                                             |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` (tells App Service to install `requirements.txt`) |

Then **Apply** → **Confirm**.

**Do not add** `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` or `AZURE_CLIENT_SECRET` here. If they are set, the code signs in as the app registration instead of the managed identity. (A bare `AZURE_CLIENT_ID` is even worse: the code would look for a *different* managed identity with that ID and fail.)

## Step 4 — Set the startup command

1. **Settings → Configuration** → **General settings**.
2. **Startup Command:** `bash startup.sh`
3. **Save**.

## Step 5 — Upload the code

The server needs only 4 things: `config/`, `mcp_server/`, `requirements.txt` and `startup.sh`. Leave out `.env` (its secrets must never leave your laptop), `data/`, the generator and the tests.

From the project folder, make the zip:

- Mac / Linux:
  ```
  zip -r app.zip config mcp_server requirements.txt startup.sh -x "*__pycache__*"
  ```
- Windows (PowerShell):
  ```
  Compress-Archive -Path config, mcp_server, requirements.txt, startup.sh -DestinationPath app.zip -Force
  ```

Then upload it. This is the one step the portal cannot do for you; it needs the Azure CLI (`az login` first):

```
az webapp deploy --resource-group rg-incident-logs --name <your-app-name> --src-path app.zip --type zip
```

It takes 2 to 5 minutes while App Service installs the libraries.

## Step 6 — Check it works

1. Without a key, the server must refuse you:
   ```
   curl -i -X POST https://<your-app-name>.azurewebsites.net/mcp
   ```

   Expected: `HTTP/1.1 401` and `{"error":"Missing or wrong API key"}`.
2. With the MCP Inspector (`npx @modelcontextprotocol/inspector`): choose **Streamable HTTP**, enter `https://<your-app-name>.azurewebsites.net/mcp`, add the header `Authorization` = `Bearer <your MCP_API_KEY>`, and **Connect**. **List Tools** must show 5 tools. Run `get_error_summary` with `hours` = 48.
3. If something fails, open the web app → **Monitoring → Log stream** to see the server's own log lines.

## Step 7 — Point your AI app at Azure

In the file you copied from `clients/`, change `http://127.0.0.1:8000/mcp` to `https://<your-app-name>.azurewebsites.net/mcp`. For the Claude desktop app, also remove `"--allow-http"`.

## Cost

Basic B1 is billed by the hour for as long as the **App Service plan** exists, even when nobody uses it, and even if you press **Stop** on the web app. To stop paying between recordings, either change the plan to **Free F1** (**App Service plan → Scale up**), or delete the plan and create it again later.
