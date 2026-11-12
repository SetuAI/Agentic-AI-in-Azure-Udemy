# Hosting on Azure Web App — every command, explained

This guide puts the app on the internet with **Azure App Service**, protected by a Microsoft sign-in. **Everything is done from the terminal on your laptop**, from creating the App Service to opening the live app. Each command is followed by 2–3 lines saying what it does. Run them **in order**, top to bottom.

**The names this project uses** :

| What              | Name                                                       |
| ----------------- | ---------------------------------------------------------- |
| Resource group    | `fundgraph-rg-1`                                         |
| Region            | `centralindia` (Central India)                           |
| App Service plan  | `fundgraph-plan` (Basic B1, Linux)                       |
| Web app           | `fundgraph-app` (Python 3.13)                            |
| PostgreSQL server | `fundgraphdbexample1`                                    |
| Azure OpenAI      | `graph-rag-001-resource`                                 |
| Content Safety    | `fundgraphprojexample`                                   |
| Live address      | `https://fundgraph-app.azurewebsites.net` (after Step 6) |

> **About secrets.** The database password and API keys live only in the project's `.env` file. No command below makes you type or paste them: Step 8 reads them straight from `.env` and stores them in Azure. Never copy keys into this file or any other file that might be shared or committed.

> **Already have a web app called `fundgraph-app` from an earlier try?** Delete it first (Step 18), or pick a new name in Step 1, such as `fundgraph-app-tarka`. Web app names must be unique across all of Azure.

---

## Part 1 · Get ready

### Step 1 · Use bash, go to the project, set the names

```bash
bash
```

Switches the terminal to the **bash** shell. A few commands below (the lists in Steps 8 and 10) only work properly in bash, not in zsh, the Mac's default.

```bash
cd /Volumes/Work/Udemy/Agentic-AI-in-Azure-Udemy/Azure-Graph-RAG-on-DBs
```

Moves into the project folder. The deploy script, `startup.sh` and the `.env` file are found from here.

```bash
RG=fundgraph-rg-1
LOC=centralindia
PLAN=fundgraph-plan
APP=fundgraph-app
PG=fundgraphdbexample1
```

Saves the five names as short **variables**, so later commands can say `$APP` instead of the full name. They last only for this terminal window: open a new window, and you must run these lines again. If they're empty, you'll see `argument --resource-group/-g: expected one argument`.

---

### Step 2 · Sign in to Azure

```bash
az login --use-device-code
```

Opens a browser so you can sign in to Azure. Two things you may see:

- **"This form will be sent in a way that is not secure"**: click **Send anyway**. The browser is only handing the result back to `az` on your own Mac (`http://localhost`), so nothing leaves your computer unencrypted.
- **"Select a subscription and tenant"** back in the terminal: type the number next to **Tarka Upskilling** (usually `1`) and press **Enter**, with nothing else on the line. Anything else you type or paste here is rejected with `Invalid selection`.

No browser, or it won't open? Use `az login --use-device-code` instead. It prints a code; enter it at https://microsoft.com/devicelogin on any device.

```bash
az account show --query "{subscription:name, user:user.name}" -o table
```

Prints which subscription and user you're signed in as. Check it says **Tarka Upskilling**, because everything from here on is billed to it.

---

### Step 3 · Make sure the database is running

```bash
az postgres flexible-server show -g $RG -n $PG --query state -o tsv
```

Prints the database server's state. **Ready** means it's running. **Stopped** means you paused it to save money.

```bash
az postgres flexible-server start -g $RG -n $PG
```

Only if it said **Stopped**. Starts the server again; this takes a few minutes. Without a running database, every question fails.

---

## Part 2 · Create the App Service

### Step 4 · Check Python 3.13 is available, and the name is free

```bash
az webapp list-runtimes --os-type linux --query "[?contains(@, 'PYTHON')]" -o tsv
```

Lists the Python versions App Service supports on Linux. You should see `PYTHON:3.13`. The project needs 3.12 or later.

```bash
az rest --method post \
    --url "https://management.azure.com/subscriptions/$(az account show --query id -o tsv)/providers/Microsoft.Web/checkNameAvailability?api-version=2022-03-01" \
    --body "{\"name\": \"$APP\", \"type\": \"Microsoft.Web/sites\"}" --query nameAvailable
```

Asks Azure whether your web app name is still free. `true` means you can use it. `false` means it's taken somewhere in Azure: change `APP` in Step 1.

---

### Step 5 · Create the App Service plan (the machine and its price)

```bash
az appservice plan create -g $RG -n $PLAN --location $LOC --is-linux --sku B1
```

Creates the **plan**: the Linux machine the app runs on, in Central India, at the **Basic B1** price (1 CPU core, 1.75 GB memory).

Why B1: it's the smallest plan with **Always On**. The Free plan (F1) puts the app to sleep after 20 minutes and doesn't have enough memory for our packages. **The plan is billed every hour it exists, from now on.**

---

### Step 6 · Create the web app on that plan

```bash
az webapp create -g $RG -p $PLAN -n $APP --runtime "PYTHON:3.13"
```

Creates the **web app** on the plan, with Python 3.13. Azure sets up an empty app at `https://fundgraph-app.azurewebsites.net`. There's no code in it yet; that comes in Step 12.

```bash
az webapp show -g $RG -n $APP --query "{state:state, address:defaultHostName}" -o json
```

Confirms it was created. You should see `"state": "Running"` and the address.

---

### Step 7 · Tell Azure how to start the app and keep it healthy

```bash
az webapp config set -g $RG -n $APP --startup-file "bash startup.sh" --always-on true \
    --http20-enabled true --ftps-state Disabled \
    --generic-configurations '{"healthCheckPath": "/health"}' -o none
```

Sets five things at once:

- **Startup command** `bash startup.sh`: runs the uvicorn web server with one worker. See `startup.sh`.
- **Always On**: keeps the app loaded, so the first question of the day isn't slow.
- **HTTP/2**: the newer, faster way browsers talk to the app.
- **FTP off**: we upload code as a zip, so the old FTP upload route is just an extra way in that we don't need.
- **Health check** `/health`: Azure calls this page regularly and restarts the app if it stops answering.

```bash
az webapp update -g $RG -n $APP --https-only true -o none
```

Turns on **HTTPS only**, so anyone who types `http://` is sent to the encrypted `https://` address.

```bash
az appservice plan update -g $RG -n $PLAN --number-of-workers 1 -o none
```

Makes sure only **one copy** of the app runs. Conversations are kept in the app's memory, so with two copies, a follow-up question could land on the copy that never saw the first question.

---

## Part 3 · Give the app its settings

### Step 8 · Copy the `.env` values into the web app

```bash
SETTINGS=()
```

Creates an empty **list** called `SETTINGS`. Each item will be one `NAME=value` line from `.env`.

```bash
while IFS= read -r line; do SETTINGS+=("$line"); done < <(uv run python -c "
from dotenv import dotenv_values
for key, value in dotenv_values('.env').items():
    if value:
        print(f'{key}={value}')
")
```

A small Python program reads `.env` and prints every line that has a value. The `while` loop puts each line into the list. Using Python means values containing `;` or `=`, like the Application Insights connection string, arrive in one piece. Nothing is shown on screen, so no keys appear.

```bash
az webapp config appsettings set -g $RG -n $APP --settings "${SETTINGS[@]}" SCM_DO_BUILD_DURING_DEPLOYMENT=true -o none
```

Stores every item in the list as an **app setting** on the web app. On Azure, the app reads app settings exactly as it reads `.env` on your laptop. The extra `SCM_DO_BUILD_DURING_DEPLOYMENT=true` tells Azure to install the Python packages each time you upload code.

```bash
echo "${#SETTINGS[@]} settings copied"
```

Prints how many settings were copied (about 9–10). If it says `0`, you're not in the project folder, or `.env` is empty.

---

### Step 9 · Check the settings arrived

```bash
az webapp config appsettings list -g $RG -n $APP --query "[].name" -o tsv | sort
```

Prints only the **names** of the settings, never their values, so it's safe to show on screen. You should see `DATA_DB_URL`, `AGENT_DB_URL`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `CHAT_DEPLOYMENT`, `EMBED_DEPLOYMENT`, `CONTENT_SAFETY_ENDPOINT`, `CONTENT_SAFETY_KEY`, `APPLICATIONINSIGHTS_CONNECTION_STRING` and `SCM_DO_BUILD_DURING_DEPLOYMENT`.

```bash
az webapp config show -g $RG -n $APP \
    --query "{runtime:linuxFxVersion, startup:appCommandLine, alwaysOn:alwaysOn, health:healthCheckPath, ftps:ftpsState}" -o json
```

Confirms Step 7: Python 3.13, `bash startup.sh`, Always On `true`, health check `/health`, and FTP `Disabled`.

---

## Part 4 · Let the app reach the database

### Step 10 · Add the web app's addresses to the database firewall

```bash
IPS=$(az webapp show -g $RG -n $APP --query possibleOutboundIpAddresses -o tsv | tr "," " ")
```

Gets the list of internet addresses (**IPs**) the web app may use when it connects out, and saves them in `IPS`. `tr` swaps the commas for spaces so the loop below can go through them one by one.

```bash
echo "$IPS" | wc -w
```

Counts the addresses, usually 20–30. This is how many firewall rules the next step will add.

```bash
i=0
for ip in $IPS; do
  i=$((i+1))
  az postgres flexible-server firewall-rule create -g $RG --server-name $PG \
      --name "app-$i" --start-ip-address $ip --end-ip-address $ip -o none
  echo "rule app-$i added for $ip"
done
```

The database only lets in addresses on its **firewall** list. So far that's your laptop and Cloud Shell. This loop adds one rule per web app address, named `app-1`, `app-2`, and so on. **It takes about 10 minutes**, because the database applies rules one at a time. Skip it, and the page loads but every question times out.

```bash
az postgres flexible-server firewall-rule list -g $RG --server-name $PG -o table
```

Lists every firewall rule. You should see your laptop's rules plus all the `app-N` ones.

---

## Part 5 · Switch on Microsoft sign-in

`api/main.py` refuses questions on Azure unless the user has signed in, so this part is required, not optional.

> **You need permission to register apps** in your Microsoft Entra ID directory. If a command below fails with `Insufficient privileges`, ask your Azure administrator, or do this part in the portal instead (**Web app → Settings → Authentication → Add identity provider → Microsoft**), then go straight to the last two commands of this step.

### Step 11 · Register the app and switch sign-in on

```bash
HOST=$(az webapp show -g $RG -n $APP --query defaultHostName -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
CALLBACK="https://$HOST/.auth/login/aad/callback"
echo $CALLBACK
```

Saves three things the next commands need: the app's address, your organisation's directory ID (the **tenant**), and the **callback** address. The callback is where Microsoft sends people back to after they sign in.

```bash
CLIENT_ID=$(az ad app create --display-name $APP --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "$CALLBACK" --enable-id-token-issuance true --query appId -o tsv)
echo $CLIENT_ID
```

Creates an **app registration**: an entry in your organisation's directory that represents our app, so Microsoft knows where to send people after sign-in. `AzureADMyOrg` means only people in your own organisation can sign in. It prints the registration's ID, the **client ID**.

```bash
az ad sp create --id $CLIENT_ID -o none
```

Creates the app registration's identity inside your directory (a **service principal**). Without it, sign-in fails with an "application not found" error.

```bash
SECRET=$(az ad app credential reset --id $CLIENT_ID --display-name "app-service-sign-in" \
    --years 1 --append --query password -o tsv)
az webapp config appsettings set -g $RG -n $APP \
    --settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET" -o none
unset SECRET
```

Creates a **client secret**, a password App Service uses to finish each sign-in, valid for one year. It's stored straight into an app setting and never printed. `unset` then removes it from the terminal's memory.

```bash
az webapp auth microsoft update -g $RG -n $APP --client-id $CLIENT_ID \
    --client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET \
    --issuer "https://login.microsoftonline.com/$TENANT/v2.0" --yes -o none
```

Tells App Service which app registration to use, where to find its secret, and which directory people sign in from.

```bash
az webapp auth update -g $RG -n $APP --enabled true --action RedirectToLoginPage \
    --redirect-provider azureactivedirectory --require-https true \
    --excluded-paths '["/health"]' -o none
```

**Switches sign-in on.** Anyone who isn't signed in is sent to the Microsoft sign-in page. The one exception is `/health`: Azure's health check can't sign in, so that page must stay open to everyone.

```bash
az webapp auth show -g $RG -n $APP \
    --query "{signin_on:properties.platform.enabled, open_paths:properties.globalValidation.excludedPaths}" -o json
```

Confirms the result: `signin_on` should be `true`, and `open_paths` should be `["/health"]`.

---

## Part 6 · Send the code and go live

### Step 12 · Deploy the code

```bash
RESOURCE_GROUP=$RG APP_NAME=$APP bash infra/azure/deploy-app.sh
```

Runs the deploy script (`infra/azure/deploy-app.sh`), which prints four stages:

1. writes `requirements.txt` from `uv.lock`, so Azure installs exactly the package versions you use locally
2. zips the code, leaving out `.env`, `.venv`, `tests/`, `evals/`, `infra/` and the `.md` files
3. uploads the zip; Azure installs the packages and runs `startup.sh`. **3–5 minutes the first time.**
4. calls `/health` every 10 seconds, for up to 5 minutes, and prints `Live: https://...` once it answers

---

### Step 13 · Only if Step 12 ended with `504 GatewayTimeout`

```bash
az webapp log deployment list -g $RG -n $APP \
    --query "[0].{status:status, complete:complete, active:active, end:end_time}" -o table
```

Sometimes the upload takes longer than `az` is willing to wait, so it prints 504, but Azure carries on in the background. This shows the real result of the latest deployment:

- `status` **4** with `active` **True**: it worked
- **3**: it failed. Look at **Deployment Center → Logs** in the portal
- **0, 1 or 2**: still running. Wait a minute and run it again

---

### Step 14 · Check the live app

```bash
HOST=$(az webapp show -g $RG -n $APP --query defaultHostName -o tsv); echo $HOST
```

Saves the app's address in `HOST` and prints it: `fundgraph-app.azurewebsites.net`.

```bash
curl https://$HOST/health
```

Calls the health page. It should print `{"status":"ok"}`, because `/health` is open to everyone (Step 11).

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://$HOST/me
```

Calls a protected page without signing in, and prints only the response code. **302** or **401** is correct: it proves sign-in is blocking strangers. **200** would mean sign-in is off.

```bash
open https://$HOST
```

Opens the app in your browser (Mac only). You're sent to the Microsoft sign-in page first. The very first time, a **Permissions requested** page appears: click **Accept**. After signing in, check three things:

- your **name** is at the top right, not "local"
- **Records per table** shows about 1.1 million, which proves the app reached the database
- a question gets an **answer**, which proves it reached Azure OpenAI and Content Safety

---

### Step 15 · Watch the live logs

```bash
az webapp log config -g $RG -n $APP --application-logging filesystem --level information -o none
```

Turns on saving of the app's logs. You only need to do this once.

```bash
az webapp log tail -g $RG -n $APP
```

Streams the app's output to your terminal as it happens: every request and every error. Ask a question in the browser and watch it appear. Press **Ctrl + C** to stop watching. The app keeps running.

---

## Part 7 · Day to day

### Step 16 · After you change the code

```bash
RESOURCE_GROUP=$RG APP_NAME=$APP bash infra/azure/deploy-app.sh
```

Same as Step 12. Only the code is replaced; the settings, firewall rules and sign-in stay as they are. The app restarts, so any conversation history is cleared.

Changed `.env` instead? Run **Step 8** again. Changing app settings restarts the app by itself.

---

### Step 17 · Restart or pause

```bash
az webapp restart -g $RG -n $APP
```

Restarts the web app. It's quick, and useful if the app seems stuck. The code and settings don't change.

```bash
az postgres flexible-server restart -g $RG -n $PG
```

Restarts the database server, which takes a few minutes. While it restarts, questions fail.

```bash
az postgres flexible-server stop -g $RG -n $PG
```

**Pauses** the database, so compute charges stop; storage is still billed. Azure starts a stopped server again by itself after 7 days. Start it with the `start` command from Step 3.

---

### Step 18 · Stop paying: delete everything this guide created

```bash
az webapp delete -g $RG -n $APP
az appservice plan delete -g $RG -n $PLAN --yes
```

**Permanently deletes** the web app, then its plan. Stopping the app is not enough: the plan is billed every hour it exists, even when the app is stopped. Deleting the plan is what ends that charge.

```bash
az ad app delete --id $(az ad app list --display-name $APP --query "[0].appId" -o tsv)
```

Deletes the app registration from Step 11, so it doesn't stay behind in your directory.

```bash
for r in $(az postgres flexible-server firewall-rule list -g $RG --server-name $PG --query "[?starts_with(name, 'app-')].name" -o tsv); do
  az postgres flexible-server firewall-rule delete -g $RG --server-name $PG --name $r --yes -o none
  echo "removed $r"
done
```

Removes the `app-N` firewall rules from Step 10. The web app is gone, so its addresses no longer need to reach the database. This also takes a few minutes.

To host again later, start from Step 5.

---

## Running locally instead (no hosting)

```bash
uv run uvicorn api.main:app --port 8000 --reload
```

Runs the same app on your laptop at http://localhost:8000. It uses the same Azure database and models, through `.env`. `--reload` restarts it whenever you save a Python file. Stop it with **Ctrl + C**.

```bash
pkill -f "uvicorn api.main:app"
```

If you get `[Errno 48] Address already in use`, another copy of the app is still holding port 8000. This stops every copy, and then you can start it again.

---

## Quick reference

| Step | Command                                      | Why                                                             |
| ---- | -------------------------------------------- | --------------------------------------------------------------- |
| 1    | `bash`, `cd`, names                      | one shell, the right folder, short names                        |
| 2    | `az login`                                 | sign in (pick Tarka Upskilling at the prompt)                   |
| 3    | `postgres ... show/start`                  | the database is running                                         |
| 4    | `list-runtimes`, `checkNameAvailability` | Python 3.13 exists, and the name is free                        |
| 5    | `appservice plan create --sku B1`          | the machine and its price                                       |
| 6    | `webapp create --runtime PYTHON:3.13`      | the web app                                                     |
| 7    | `config set`, `update`, `plan update`  | startup, Always On, HTTP/2, no FTP, health check, HTTPS, 1 copy |
| 8    | `appsettings set` from `.env`            | the app gets its keys and database addresses                    |
| 9    | `appsettings list`, `config show`        | check they arrived                                              |
| 10   | firewall loop                                | the app can reach PostgreSQL                                    |
| 11   | `ad app create` … `auth update`         | Microsoft sign-in on, `/health` open                          |
| 12   | `deploy-app.sh`                            | send the code                                                   |
| 13   | `log deployment list`                      | only after a 504                                                |
| 14   | `curl` + `open`                          | check it's live                                                 |
| 15   | `log tail`                                 | watch it live                                                   |
| 16   | `deploy-app.sh`                            | after any code change                                           |
| 17   | `restart` / `stop`                       | restart or pause                                                |
| 18   | `delete`                                   | stop the bill and clean up                                      |

---

## If something goes wrong

| You see                                                     | Cause                                                                   | Fix                                                                                                                                                                 |
| ----------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Invalid selection` over and over                         | You typed commands into the `az login` subscription prompt            | Type `1`, press Enter, then run commands at the normal prompt                                                                                                     |
| `argument --resource-group/-g: expected one argument`     | The names are empty (new terminal)                                      | Run the name lines from Step 1 again                                                                                                                                |
| `nameAvailable: false`, or `Website ... already exists` | The web app name is taken                                               | Change `APP` in Step 1, or delete the old app (Step 18)                                                                                                           |
| Plan can't be created in this region                        | B1 isn't available in Central India right now                           | Set `LOC=southindia` in Step 1, and re-run from Step 5                                                                                                            |
| `Linux Runtime 'PYTHON\|3.13' is not supported`            | The Azure CLI is older than the runtime                                 | `brew upgrade azure-cli`, or use `PYTHON:3.12`                                                                                                                  |
| `bad substitution` or odd errors in Steps 8 or 10         | Not running in bash                                                     | Type `bash`, then Step 1 again                                                                                                                                    |
| `Insufficient privileges` in Step 11                      | You can't register apps in your directory                               | Ask your Azure administrator, or use the portal (see the note in Part 5)                                                                                            |
| `Application Error` page                                  | The app didn't start                                                    | Step 15: look for `KeyError` (a missing setting, so redo Step 8) or `ModuleNotFoundError` (packages not installed, so check `SCM_DO_BUILD_DURING_DEPLOYMENT`) |
| The page loads, but questions time out                      | The database firewall doesn't allow the app, or the database is stopped | Step 3, then Step 10                                                                                                                                                |
| `/health` fails, and Azure keeps restarting the app       | `/health` is behind sign-in                                           | Re-run the `auth update` command in Step 11                                                                                                                       |
| Sign-in shows `AADSTS50011` (redirect URI does not match) | The callback address in the app registration is wrong                   | `az ad app update --id $CLIENT_ID --web-redirect-uris "https://$HOST/.auth/login/aad/callback"`                                                                   |
| Sign-in shows `AADSTS7000222` (secret expired)            | The client secret is more than a year old                               | Re-run the `credential reset` command in Step 11                                                                                                                  |
| Every question returns `Sign-in required`                 | Sign-in is off                                                          | Redo Step 11                                                                                                                                                        |
| Follow-up questions lose context                            | The app restarted, or more than 1 copy is running                       | Re-run the `plan update --number-of-workers 1` command in Step 7                                                                                                  |
| `[Errno 48] Address already in use` (local)               | Another copy of the app holds port 8000                                 | `pkill -f "uvicorn api.main:app"`                                                                                                                                 |
