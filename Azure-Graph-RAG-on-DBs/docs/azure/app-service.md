# Deploy Azure GraphRAG on Databases to Azure Web Apps

Follow the steps in order. Run every command in a terminal, from the project folder.

Before you start, make sure that:

- Postgres, Azure OpenAI and Content Safety already exist.
- The app works on your laptop (`uv run uvicorn api.main:app` answers questions).
- Your `.env` file is filled in.

Mac and Linux: use the normal Terminal. Windows: use **Git Bash**, not PowerShell.

---

## Part 1: Set your names

### Step 1: Save your names in the terminal

```bash
RG=fundgraph-rg-1
APP=fundgraph-app
PLAN=fundgraph-app-plan
PG=fundgraphdbexample1
LOC=centralindia
```

What each line means:

- `RG=fundgraph-rg-1` saves the name of your resource group. A resource group is the folder in Azure that holds all the services of this project.
- `APP=fundgraph-app` saves the name of your web app. It becomes part of the web address, so it must be unique in all of Azure.
- `PLAN=fundgraph-app-plan` saves the name of the App Service plan. The plan is the machine your app runs on.
- `PG=fundgraphdbexample1` saves the name of your Postgres server, without `.postgres.database.azure.com`.
- `LOC=centralindia` saves the region. Use the **same region as your Postgres server**, because the app calls the database several times per question.

These names are only remembered in **this** terminal window. If you open a new window, run Step 1 again.

---

## Part 2: Sign in

### Step 2: Sign in to Azure

```bash
az login --use-device-code
```

- `az` is the Azure command-line tool.
- `login` opens a browser window. Sign in with your Azure account there.

### Step 3: Check which subscription is active

```bash
az account show --query name -o tsv
```

- `account show` shows the subscription you are using now.
- `--query name` picks only its name.
- `-o tsv` prints it as plain text.

If it's the wrong one, run Step 4. If it's the right one, skip to Step 5.

### Step 4: Pick the right subscription

```bash
az account set --subscription "<your subscription name>"
```

- `account set` changes the active subscription.
- `--subscription "..."` is the name you want. Keep the quotes.

---

## Part 3: Create the web app

### Step 5: Create the resource group

```bash
az group create --name $RG --location $LOC
```

- `group create` creates a resource group.
- `--name $RG` uses the name from Step 1.
- `--location $LOC` puts it in your region.

If it already exists, nothing changes. That's fine.

### Step 6: Create the App Service plan

```bash
az appservice plan create --name $PLAN --resource-group $RG --location $LOC --is-linux --sku B1
```

- `appservice plan create` creates the machine your app runs on.
- `--name $PLAN` gives it the name from Step 1.
- `--resource-group $RG` puts it in your resource group.
- `--location $LOC` puts it in your region.
- `--is-linux` makes it a Linux machine. Python apps run on Linux.
- `--sku B1` picks the Basic B1 size: 1 core and 1.75 GB of memory. It's the smallest size with **Always On**. The Free size sleeps after 20 minutes and has too little memory.

### Step 7: Check that Python 3.13 is available

```bash
az webapp list-runtimes --os linux --query "[].config" | grep -i python
```

- `webapp list-runtimes` lists the languages App Service can run.
- `--os linux` shows the Linux ones only.
- `--query "[].config"` shows only the runtime names.
- `| grep -i python` keeps only the lines with "python".

You should see `PYTHON|3.13` in the list.

### Step 8: Create the web app

```bash
az webapp create --name $APP --resource-group $RG --plan $PLAN --runtime "PYTHON:3.13"
```

- `webapp create` creates your web app.
- `--name $APP` gives it the name from Step 1.
- `--resource-group $RG` puts it in your resource group.
- `--plan $PLAN` runs it on the plan from Step 6.
- `--runtime "PYTHON:3.13"` runs Python 3.13. It must match the Python on your laptop. The project needs 3.12 or later.

---

## Part 4: Settings

### Step 9: Load your `.env` values into the terminal

```bash
set -a; source .env; set +a
```

- `set -a` means: every value read from now on can be used by the next commands.
- `source .env` reads every line of your `.env` file.
- `set +a` switches the first part off again.

Now `$DATA_DB_URL`, `$AZURE_OPENAI_ENDPOINT` and the others hold your real values. Nothing is printed on screen.

### Step 10: Copy the settings into the web app

```bash
az webapp config appsettings set --name $APP --resource-group $RG --output none --settings \
  DATA_DB_URL="$DATA_DB_URL" \
  AGENT_DB_URL="$AGENT_DB_URL" \
  AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
  AZURE_OPENAI_API_KEY="$AZURE_OPENAI_API_KEY" \
  CHAT_DEPLOYMENT="$CHAT_DEPLOYMENT" \
  EMBED_DEPLOYMENT="$EMBED_DEPLOYMENT" \
  CONTENT_SAFETY_ENDPOINT="$CONTENT_SAFETY_ENDPOINT" \
  CONTENT_SAFETY_KEY="$CONTENT_SAFETY_KEY" \
  APPLICATIONINSIGHTS_CONNECTION_STRING="$APPLICATIONINSIGHTS_CONNECTION_STRING" \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

- `webapp config appsettings set` saves settings in the web app. On Azure, these do the job that `.env` does on your laptop.
- `--output none` stops Azure from printing the settings back. This keeps your keys off the screen.
- `--settings` is followed by one `NAME="value"` pair per line.
- `DATA_DB_URL` is the address of the `mf_data` database (the business data).
- `AGENT_DB_URL` is the address of the `agent_meta` database (the agent's notes, links, vectors and graph).
- `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` connect to Azure OpenAI.
- `CHAT_DEPLOYMENT` is your gpt-5.1 deployment name. `EMBED_DEPLOYMENT` is your embedding deployment name.
- `CONTENT_SAFETY_ENDPOINT` and `CONTENT_SAFETY_KEY` connect to Content Safety.
- `APPLICATIONINSIGHTS_CONNECTION_STRING` sends the app's traces to Application Insights.
- `SCM_DO_BUILD_DURING_DEPLOYMENT=true` tells App Service to install `requirements.txt` when you upload the code.
- Each `\` at the end of a line means "the command continues on the next line".

Anyone with access to this web app in the portal can read these values, so give portal access only to people who may see the keys.

### Step 11: Check the settings were saved

```bash
az webapp config appsettings list --name $APP --resource-group $RG --query "[].name" -o tsv
```

- `appsettings list` lists the saved settings.
- `--query "[].name"` shows **only the names**, not the values, so no key appears on screen.

You should see all 10 names from Step 10.

### Step 12: Set the startup command, Always On and the health check

```bash
az webapp config set --name $APP --resource-group $RG --startup-file "bash startup.sh" --always-on true --generic-configurations '{"healthCheckPath": "/health"}'
```

- `webapp config set` changes how the web app runs.
- `--startup-file "bash startup.sh"` is the command App Service runs to start the app. `startup.sh` starts the web server with exactly 1 worker.
- `--always-on true` keeps the app loaded, so the first question isn't slow.
- `--generic-configurations '{"healthCheckPath": "/health"}'` makes App Service call `/health` regularly, and restart the app if it stops answering.

### Step 13: Turn on HTTPS only

```bash
az webapp update --name $APP --resource-group $RG --https-only true
```

- `webapp update` changes the web app.
- `--https-only true` sends every `http://` visitor to the safe `https://` address.

---

## Part 5: Let the app reach Postgres

The web app calls Postgres from a set of Azure addresses, often 20 to 30 of them. Postgres blocks every address it doesn't know, so we allow each one.

### Step 14: Add one firewall rule per address

```bash
bash -c '
IPS=$(az webapp show -g '"$RG"' -n '"$APP"' --query possibleOutboundIpAddresses -o tsv | tr "," " ")
i=0
for ip in $IPS; do
  i=$((i+1))
  az postgres flexible-server firewall-rule create -g '"$RG"' --name '"$PG"' --rule-name "app-$i" --start-ip-address $ip --end-ip-address $ip -o none
done
echo "added $i rules"'
```

- `bash -c '...'` runs the block in bash. This matters on a Mac, where the default terminal is zsh and would not split the address list correctly.
- `az webapp show ... --query possibleOutboundIpAddresses` gets every address the web app may call out from.
- `tr "," " "` turns the commas between the addresses into spaces, so the loop can go through them one by one.
- `for ip in $IPS; do ... done` repeats the next command once for each address.
- `firewall-rule create` adds one rule to Postgres.
- `--rule-name "app-$i"` names the rules `app-1`, `app-2`, and so on.
- `--start-ip-address $ip --end-ip-address $ip` allows exactly that one address.
- `echo "added $i rules"` prints how many rules were added.

Postgres adds rules one at a time, so 30 rules take about 10 minutes. Wait until you see `added 31 rules` (or your number).

The Postgres Networking page also has a box called *"Allow public access from any Azure service within Azure to this server"*. It's one click, but it lets in every Azure customer's services, not only yours. The per-address rules are safer.

---

## Part 6: Sign-in with Entra ID

These steps put a Microsoft sign-in page in front of your app. Only people in your own organisation can open it. `/health` stays open, so App Service can still check the app.

### Step 15: Save your tenant ID and the sign-in return address

```bash
TENANT=$(az account show --query tenantId -o tsv)
HOST=$(az webapp show --name $APP --resource-group $RG --query defaultHostName -o tsv)
CALLBACK="https://$HOST/.auth/login/aad/callback"
```

- `TENANT=...` saves your Entra ID tenant ID. A tenant is your organisation's account in Entra ID.
- `HOST=...` saves your web app's address, for example `fundgraph-app-<random>.centralindia-01.azurewebsites.net`.
- `CALLBACK=...` is the address Entra ID sends people back to after they sign in.

### Step 16: Create the app registration

```bash
CLIENT_ID=$(az ad app create --display-name $APP --sign-in-audience AzureADMyOrg --web-redirect-uris $CALLBACK --enable-id-token-issuance true --query appId -o tsv)
```

- `ad app create` creates an **app registration**: your web app's identity in Entra ID.
- `--display-name $APP` names it after your web app.
- `--sign-in-audience AzureADMyOrg` allows only accounts from your own organisation.
- `--web-redirect-uris $CALLBACK` is the return address from Step 15.
- `--enable-id-token-issuance true` lets Entra ID send back proof of who signed in.
- `--query appId -o tsv` saves only its ID into `CLIENT_ID`.

### Step 17: Create its service principal

```bash
az ad sp create --id $CLIENT_ID
```

- `ad sp create` creates the app registration's working identity inside your organisation. Sign-in doesn't work without it.

### Step 18: Create a secret and save it in the web app

Run this **once**. Each run creates a new secret.

```bash
SECRET=$(az ad app credential reset --id $CLIENT_ID --display-name app-service-sign-in --years 1 --append --query password -o tsv)
az webapp config appsettings set --name $APP --resource-group $RG --output none --settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET"
```

- `ad app credential reset` creates a secret (a password) for the app registration.
- `--years 1` makes it valid for 1 year.
- `--append` keeps any older secret working.
- `--query password -o tsv` saves only the secret into `SECRET`. It is never printed.
- The second line stores the secret as a web app setting called `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`.

### Step 19: Check the secret is not empty

```bash
echo -n "$SECRET" | wc -c
```

- `echo -n "$SECRET"` passes the secret along without printing it.
- `wc -c` counts its characters.

You should see a number around **40**. If you see **0**, the secret is empty: run Step 18 again.

### Step 20: Upgrade the sign-in settings to the current version

```bash
az webapp auth config-version upgrade --name $APP --resource-group $RG
```

- `auth config-version upgrade` moves the web app's sign-in settings to version 2. The next step only works with version 2. If it says it is already upgraded, that's fine.

### Step 21: Connect sign-in to the app registration

```bash
az webapp auth microsoft update --name $APP --resource-group $RG --client-id $CLIENT_ID --client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET --issuer "https://login.microsoftonline.com/$TENANT/v2.0" --yes
```

- `auth microsoft update` sets up Microsoft sign-in for the web app.
- `--client-id $CLIENT_ID` uses the app registration from Step 16.
- `--client-secret-setting-name ...` tells it which setting holds the secret from Step 18.
- `--issuer "https://login.microsoftonline.com/$TENANT/v2.0"` accepts sign-ins from your organisation only.
- `--yes` answers "yes" to the confirmation question.

### Step 22: Switch sign-in on

```bash
az webapp auth update --name $APP --resource-group $RG --enabled true --action RedirectToLoginPage --redirect-provider azureactivedirectory --require-https true --excluded-paths '["/health"]'
```

- `auth update --enabled true` switches sign-in on.
- `--action RedirectToLoginPage` sends anyone not signed in to the Microsoft sign-in page.
- `--redirect-provider azureactivedirectory` uses Microsoft (Entra ID) for that page.
- `--require-https true` allows sign-in over https only.
- `--excluded-paths '["/health"]'` keeps `/health` open, so App Service can still check the app.

---

## Part 7: Deploy the code

### Step 23: Run the deploy script

```bash
RESOURCE_GROUP=$RG APP_NAME=$APP bash infra/azure/deploy-app.sh
```

- `RESOURCE_GROUP=$RG` tells the script your resource group.
- `APP_NAME=$APP` tells it your web app's name.
- `bash infra/azure/deploy-app.sh` runs the script. It does 4 things:
  1. writes `requirements.txt` from `uv.lock`, so Azure installs exactly the same package versions as your laptop,
  2. zips the project, leaving out `.env`, `.venv`, `tests/`, `evals/`, `infra/` and the `.md` files (your secrets never leave your laptop),
  3. uploads the zip, after which App Service installs the packages (3 to 5 minutes the first time),
  4. waits up to 5 minutes for `/health` to answer, then prints the address.

**If it stops with `504 GatewayTimeout`:** this usually doesn't mean the deploy failed. The upload request timed out while Azure was still installing packages. Go to Step 24.

### Step 24: Check the real deploy result

```bash
az webapp log deployment list --name $APP --resource-group $RG --query "[0].{status:status, active:active, end:end_time}" -o table
```

- `log deployment list` lists your deploys.
- `[0]` picks the latest one.
- `{status, active, end}` shows its status, whether it's live, and when it finished.

What the status means:

| `status`                        | Meaning                                                                                  |
| --------------------------------- | ---------------------------------------------------------------------------------------- |
| `4`, with `active` = `True` | It worked, and this version is live                                                      |
| `0`, `1` or `2`             | Still running. Wait a minute and run Step 24 again                                       |
| `3`                             | It failed. Open the portal: web app → **Deployment → Deployment Center → Logs** |

---

## Part 8: Check the live site

### Step 25: Check the health address

```bash
curl https://$HOST/health
```

- `curl` calls an address and prints the answer.
- `/health` needs no sign-in.

You should see: `{"status":"ok"}`.

### Step 26: Open the site in a browser

```bash
echo https://$HOST
```

- This prints your site's address. Open it in a browser.

You should see, in order:

1. the Microsoft sign-in page; sign in with your work account,
2. a **Sign out** link at the top right,
3. **Records per table** showing the row total (proof the app reached Postgres),
4. an answer when you ask a question (proof it reached Azure OpenAI and Content Safety).

---

## Updating the app later

After any code change, run Step 1 (if it's a new terminal window), then Step 23 again:

```bash
RESOURCE_GROUP=$RG APP_NAME=$APP bash infra/azure/deploy-app.sh
```

Only the code is replaced. Settings, startup command, sign-in and firewall rules stay as they are. The app restarts, so conversation history is cleared.

---

## Viewing logs

### Step 27: Switch on app logging (once)

```bash
az webapp log config --name $APP --resource-group $RG --application-logging filesystem
```

- `log config` sets up logging.
- `--application-logging filesystem` saves the app's printed output, so you can read it.

### Step 28: Watch the live output

```bash
az webapp log tail --name $APP --resource-group $RG
```

- `log tail` shows the app's output as it happens. Press `Ctrl + C` to stop.

---

## Important limits

- **Keep 1 instance and 1 worker.** Conversation history is kept in the app's memory. A second instance would not see it, and follow-up questions would lose their context. Don't change **Settings → Scale out** from 1.
- **History is lost on every restart.** Every deploy or settings change restarts the app.
- **Cost:** the B1 plan is billed every hour it exists, even when the app is stopped. To stop the charge, delete the plan:

```bash
az appservice plan delete --name $PLAN --resource-group $RG --yes
```

- `appservice plan delete` deletes the plan, and the web app on it stops working.
- `--yes` skips the confirmation question.

---

## If something goes wrong

| What you see                                             | Cause                                      | Fix                                                                    |
| -------------------------------------------------------- | ------------------------------------------ | ---------------------------------------------------------------------- |
| `Application Error` page, or `/health` never answers | The app didn't start                       | Step 28. Look for a missing setting (e.g. `KeyError: 'DATA_DB_URL'`) |
| `ModuleNotFoundError`                                  | Packages weren't installed                 | Check `SCM_DO_BUILD_DURING_DEPLOYMENT` in Step 11, then Step 23      |
| Questions fail with `connection timeout`               | Postgres blocks the app                    | Step 14                                                                |
| `auth microsoft update` fails with an "auth v1" error  | Sign-in settings are on the old version    | Step 20, then Step 21                                                  |
| Sign-in fails after the Microsoft page                   | The secret is empty                        | Step 19. If it shows 0, run Step 18 again                              |
| Every question returns `401`                           | Sign-in is off or set up wrongly           | Steps 20 to 22                                                         |
| Follow-up questions forget the previous one              | More than 1 instance, or the app restarted | See "Important limits"                                                 |
| The first question after a while is slow                 | Always On is off                           | Step 12                                                                |
| A command says the name isn't set, or uses a blank name  | New terminal window                        | Run Step 1 again (and Step 9 before Step 10)                           |
