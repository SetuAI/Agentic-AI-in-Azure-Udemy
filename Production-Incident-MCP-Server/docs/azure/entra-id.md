
# Microsoft Entra ID — who is allowed in

## What it does in this project

Entra ID decides **who** may do **what** in Azure. We use two identities:

| Identity                                            | Used where        | Allowed to                                          | Role                                                                                                   |
| --------------------------------------------------- | ----------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| **App registration** `incident-logs-loader` | Your laptop       | Upload logs, and search them while you test locally | **Monitoring Metrics Publisher** on the DCR, and **Log Analytics Reader** on the workspace |
| **Managed identity** of the web app           | Azure App Service | Search logs, nothing else                           | **Log Analytics Reader** on the workspace                                                        |

So the server on Azure can **read** logs but can never change or delete them, and it holds no password at all. The managed identity is set up in [app-service.md](app-service.md).

How the code signs in: every file uses `DefaultAzureCredential()`. It looks for a way to sign in, in order:

1. If `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` are set (your `.env`), it signs in as the app registration.
2. If not, and it runs on Azure, it signs in as the web app's managed identity.

> Portal screens change from time to time. If a button has a slightly different name, look for the closest match.

---

## Step 1 — Create the app registration

1. In the Azure portal, search for **Microsoft Entra ID** → **App registrations** → **New registration**.
2. **Name:** `incident-logs-loader`. Leave everything else as it is → **Register**.
3. On its **Overview** page, copy:
   - **Application (client) ID** → `AZURE_CLIENT_ID` in `.env`
   - **Directory (tenant) ID** → `AZURE_TENANT_ID` in `.env`

## Step 2 — Create its secret (its password)

1. In the app registration: **Certificates & secrets** → **Client secrets** → **New client secret**.
2. Description: `laptop`. Expiry: the shortest that suits you → **Add**.
3. Copy the **Value** column straight away into `AZURE_CLIENT_SECRET`. Azure shows it only once. (The "Secret ID" column is not the secret.)

## Step 3 — Allow it to upload logs

1. Search for **Data collection rules** → open `dcr-incident-logs`.
2. **Access control (IAM)** → **Add** → **Add role assignment**.
3. Role: **Monitoring Metrics Publisher** → **Next**.
4. **Select members** → search `incident-logs-loader` → **Select** → **Review + assign**.

If you have a second rule for `Deployments_CL`, do the same on that rule.

## Step 4 — Allow it to search logs (for testing on your laptop)

1. Open your Log Analytics workspace → **Access control (IAM)** → **Add role assignment**.
2. Role: **Log Analytics Reader** → **Next**.
3. **Select members** → `incident-logs-loader` → **Review + assign**.

## Wait a few minutes

New role assignments can take up to about 10 minutes to start working. If the loader or a tool says "Forbidden" or "AuthorizationFailed" right after this step, wait and try again.
