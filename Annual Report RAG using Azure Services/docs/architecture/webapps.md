Deploy the Annual Report RAG App on Azure Web Apps

---

## What are we doing?

Right now, our app runs only on our laptop. We open `http://localhost:8000`, and ask a question.

But only **we** can use it. When we close the laptop, the app is gone.

So, we will put the app on **Azure**. Azure will keep it running all the time, and give it an internet address:

```
https://annual-report-rag-app.azurewebsites.net
```

The Azure service that does this is called **Azure App Service**. The app we create inside it is called a **Web App**.

In simple words: **we give Azure our code. Azure installs it, starts it, and keeps it running.**

---

## How to use this file

- Open the **VS Code terminal**, inside the project folder.
- Run the commands **one by one**, from top to bottom.
- Under each command, there is a table. It breaks the command into its parts, and says what each part does.

A few words we'll see again and again:

| Word        | Meaning                                                                          |
| ----------- | -------------------------------------------------------------------------------- |
| `az`      | the **Azure CLI**: the tool that lets us control Azure by typing commands  |
| `-g`      | short for **resource group**: the folder in Azure where our resources live |
| `-n`      | short for **name**                                                         |
| `-o none` | **print nothing**. The command works quietly                               |
| `-o tsv`  | print **plain text**, with no quotes or brackets                           |
| `--query` | pick **only the part** of the answer we want                               |
| `$NAME`   | use the value saved in the variable `NAME`                                     |

---

# Part 1 · Set up

## Step 1 · Save the names

```bash
RG=annual-rg-1
SEARCH=annual-report-search-1
PLAN=annual-report-plan
APP=annual-report-rag-app
INSIGHTS=annual-report-insights-1
LOC=westus2
```

We save six names in **variables**. A variable is a short name that holds a value. So later, we can type `$APP` instead of `annual-report-rag-app`.

| Part                                  | What it does                                                                                           |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `RG=annual-rg-1`                    | our **resource group**: the folder in Azure that holds everything for this project               |
| `SEARCH=annual-report-search-1`     | our **Azure AI Search** service, where our index lives                                           |
| `PLAN=annual-report-plan`           | the **App Service plan**: the machine our app will run on. We create it in Step 4                |
| `APP=annual-report-rag-app`         | our **web app's name**. It also becomes its address: `annual-report-rag-app.azurewebsites.net` |
| `INSIGHTS=annual-report-insights-1` | our **Application Insights**: Azure's monitoring screens. We already have it                     |
| `LOC=westus2`                       | the **region**: which Azure data centre the machine runs in. Here, West US 2                     |

These variables live **only in this terminal window**. If you open a new window, run this step again.

---

## Step 2 · Sign in to Azure

```bash
az login --use-device-code
```

| Part                  | What it does                                                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `az login`          | sign in to our Azure account                                                                                                               |
| `--use-device-code` | sign in with a **code**. The terminal shows a code and a web address. We open the address in any browser, type the code, and sign in |

When the terminal asks us to pick a subscription, we type the number next to **Tarka Upskilling** (usually `1`) and press **Enter**.

From now on, every `az` command runs as **us**.

---

## Step 3 · Check which account we're using

```bash
az account show --query "{subscription:name, user:user.name}" -o table
```

| Part                                              | What it does                                                                       |
| ------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `az account show`                               | show the account we're signed in to                                                |
| `--query "{subscription:name, user:user.name}"` | pick only two things: the **subscription** name, and the **user** name |
| `-o table`                                      | print them as a neat **table**                                               |

We should see **Tarka Upskilling**. Everything we create will be billed to this subscription.

---

# Part 2 · Create the web app

## Step 4 · Create the machine (the App Service plan)

```bash
az appservice plan create -g $RG -n $PLAN --location $LOC --is-linux --sku B1
```

| Part                          | What it does                                                               |
| ----------------------------- | -------------------------------------------------------------------------- |
| `az appservice plan create` | create an **App Service plan**: the machine our app runs on          |
| `-g $RG`                    | put it in our resource group, `annual-rg-1`                              |
| `-n $PLAN`                  | call it `annual-report-plan`                                             |
| `--location $LOC`           | put it in the **West US 2** region                                   |
| `--is-linux`                | make it a **Linux** machine. Python apps run on Linux in App Service |
| `--sku B1`                  | pick the **Basic B1** size and price: 1 CPU and 1.75 GB of memory    |

Why B1? It's the smallest size that can stay **always on**. The free size puts the app to sleep after 20 minutes.

**This machine costs money every hour, from now on**, even if nobody uses the app. Part 8 shows how to delete it.

---

## Step 5 · Create the web app on that machine

```bash
az webapp create -g $RG -p $PLAN -n $APP --runtime "PYTHON:3.12"
```

| Part                        | What it does                                                   |
| --------------------------- | -------------------------------------------------------------- |
| `az webapp create`        | create a **web app**                                     |
| `-g $RG`                  | in our resource group                                          |
| `-p $PLAN`                | on the machine we made in Step 4 (`-p` means **plan**) |
| `-n $APP`                 | call it `annual-report-rag-app`                              |
| `--runtime "PYTHON:3.12"` | get **Python 3.12** ready for our code                   |

Now the web app exists, with its address. But it's **empty**. Our code goes in later, in Part 6.

---

## Step 6 · Tell Azure how to start our app

```bash
az webapp config set -g $RG -n $APP --startup-file "bash startup.sh" --always-on true \
    --http20-enabled true --ftps-state Disabled \
    --generic-configurations '{"healthCheckPath": "/health"}' -o none
```

| Part                                                          | What it does                                                                                                                                              |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `az webapp config set`                                      | change our web app's **settings**                                                                                                                   |
| `--startup-file "bash startup.sh"`                          | every time the app starts, run our file `startup.sh`. That file runs `python -u UI/server.py`, our web server                                         |
| `--always-on true`                                          | keep the app **awake** all the time, so the first question of the day isn't slow                                                                    |
| `--http20-enabled true`                                     | use **HTTP/2**: a newer, faster way for browsers to talk to the app                                                                                 |
| `--ftps-state Disabled`                                     | switch off **FTP**, an old way of uploading files. We don't use it, so we close it                                                                  |
| `--generic-configurations '{"healthCheckPath": "/health"}'` | Azure will visit the page `/health` every minute. Our server answers `{"status": "ok"}`. If it ever stops answering, Azure **restarts** the app |
| `\`                                                         | the command continues on the next line                                                                                                                    |
| `-o none`                                                   | print nothing                                                                                                                                             |

---

## Step 7 · Allow only secure (https) connections

```bash
az webapp update -g $RG -n $APP --https-only true -o none
```

| Part                  | What it does                                                                           |
| --------------------- | -------------------------------------------------------------------------------------- |
| `az webapp update`  | change our web app                                                                     |
| `--https-only true` | allow **https** only. If someone types `http://`, they're sent to `https://` |
| `-o none`           | print nothing                                                                          |

`https` means the connection is **encrypted**. Nobody in between can read it. This matters, because people will **sign in** to our app.

---

## Step 8 · Run only one copy of the app

```bash
az appservice plan update -g $RG -n $PLAN --number-of-workers 1 -o none
```

| Part                          | What it does                              |
| ----------------------------- | ----------------------------------------- |
| `az appservice plan update` | change our machine's settings             |
| `-n $PLAN`                  | the machine called `annual-report-plan` |
| `--number-of-workers 1`     | run exactly **one copy** of our app |
| `-o none`                   | print nothing                             |

Why one copy? When an answer needs **human review**, our graph **pauses**, and keeps that paused answer in the app's **memory**. With two copies, the reviewer's click could reach the copy that doesn't have it. With one copy, it always finds it.

---

# Part 3 · Connect monitoring

## Step 9 · Add the monitoring commands to `az`

```bash
az extension add --name application-insights --upgrade
```

| Part                            | What it does                                                       |
| ------------------------------- | ------------------------------------------------------------------ |
| `az extension add`            | add **extra commands** to the Azure CLI                      |
| `--name application-insights` | the extra commands for **Application Insights** (monitoring) |
| `--upgrade`                   | update them to the latest version                                  |

---

## Step 10 · Get the monitoring address

```bash
AI_CONN=$(az monitor app-insights component show \
    -g $RG --app $INSIGHTS --query connectionString -o tsv)
```

| Part                                                                                                              | What it does                                |
| ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `AI_CONN=$( ... )` | run the command inside `$( )`, and **save its answer** in the variable `AI_CONN` |                                             |
| `az monitor app-insights component show`                                                                        | show our Application Insights resource      |
| `--app $INSIGHTS`                                                                                               | the one called `annual-report-insights-1` |
| `--query connectionString`                                                                                      | pick only its **connection string**   |
| `-o tsv`                                                                                                        | as plain text                               |

A **connection string** is the address our app uses to **send** its monitoring data: how long each question took, and any errors.

It's saved, not printed. So it never shows on screen.

---

# Part 4 · Give the app its keys

On our laptop, our code reads its keys from the `.env` file. But we **never upload `.env`**. Keys should never travel inside our code.

So, we copy the values from `.env` into the web app's **app settings**. An app setting is a `NAME=value` pair saved on the web app. On Azure, our code reads app settings **the same way** it reads `.env` on the laptop.

We copy only what the web app **needs**. It only **asks questions**. So it doesn't need the Blob Storage key (it never reads the PDFs), and it gets a **read-only** Search key instead of the admin key.

## Step 11 · Make an empty list

```bash
SETTINGS=()
```

| Part          | What it does                        |
| ------------- | ----------------------------------- |
| `SETTINGS=` | make a variable called `SETTINGS` |
| `()`        | make it an **empty list**     |

We'll fill this list with the `NAME=value` lines from `.env`, then send them to Azure all at once.

---

## Step 12 · Read `.env` into the list

```bash
while IFS= read -r line; do SETTINGS+=("$line"); done < <(python -c "
from dotenv import dotenv_values
skip = {'AZURE_SEARCH_API_KEY', 'AZURE_STORAGE_CONNECTION_STRING', 'AZURE_STORAGE_CONTAINER',
        'LOCAL_PDF_DIR', 'APPLICATIONINSIGHTS_CONNECTION_STRING'}
for key, value in dotenv_values('.env').items():
    if value and key not in skip:
        print(f'{key}={value}')
")
```

This command has two halves. The **second half** (the Python) reads `.env`. The **first half** (the `while` loop) puts each line into our list.

**The Python half:**

| Part                                                 | What it does                                                                                                                                                                               |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `python -c "..."`                                  | run the short Python program written inside the quotes                                                                                                                                     |
| `from dotenv import dotenv_values`                 | bring in the tool that reads `.env` files                                                                                                                                                |
| `skip = {...}`                                     | the five settings we **don't** copy: the admin Search key, the two Blob Storage settings, the laptop's PDF folder, and the monitoring address (we set that one ourselves in Step 16) |
| `for key, value in dotenv_values('.env').items():` | go through `.env`, one setting at a time                                                                                                                                                 |
| `if value and key not in skip:`                    | keep it only if it **has a value**, and it's **not** in the skip list                                                                                                          |
| `print(f'{key}={value}')`                          | print it as `NAME=value`                                                                                                                                                                 |

**The bash half:**

| Part                      | What it does                                                          |
| ------------------------- | --------------------------------------------------------------------- |
| `< <( ... )`            | take everything the Python program prints, and feed it to the loop    |
| `while ... do ... done` | repeat, once for each line                                            |
| `IFS= read -r line`     | read **one line**, exactly as it is, into the variable `line` |
| `SETTINGS+=("$line")`   | **add** that line to our list                                   |

Nothing is printed on screen. So no key ever shows in the terminal.

---

## Step 13 · Count the list

```bash
echo "${#SETTINGS[@]} settings ready"
```

| Part                     | What it does                              |
| ------------------------ | ----------------------------------------- |
| `echo`                 | print something                           |
| `${#SETTINGS[@]}`      | the **number of items** in the list |
| `"... settings ready"` | the words printed after the number        |

We should see **8 settings ready**: the OpenAI endpoint, key and API version, the three model names, the Search endpoint, and the index name.

---

## Step 14 · Save the list on the web app

```bash
az webapp config appsettings set -g $RG -n $APP \
    --settings "${SETTINGS[@]}" SCM_DO_BUILD_DURING_DEPLOYMENT=true WEBSITES_PORT=8000 -o none
```

| Part                                    | What it does                                                                                                                    |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `az webapp config appsettings set`    | save **app settings** on our web app                                                                                      |
| `--settings "${SETTINGS[@]}"`         | save **every item** in our list                                                                                           |
| `SCM_DO_BUILD_DURING_DEPLOYMENT=true` | one extra setting: every time we upload code, Azure runs `pip install -r requirements.txt`. That installs our Python packages |
| `WEBSITES_PORT=8000`                  | one more: tell Azure our server listens on **port 8000**                                                                  |
| `-o none`                             | print nothing                                                                                                                   |

---

## Step 15 · Give the app a read-only Search key

```bash
QUERY_KEY=$(az search query-key list -g $RG --service-name $SEARCH --query "[0].key" -o tsv)
```

| Part                         | What it does                                      |
| ---------------------------- | ------------------------------------------------- |
| `QUERY_KEY=$( ... )`       | save the answer in the variable `QUERY_KEY`     |
| `az search query-key list` | list the **query keys** of a Search service |
| `--service-name $SEARCH`   | our Search service, `annual-report-search-1`    |
| `--query "[0].key"`        | take the **first** key from the list        |
| `-o tsv`                   | as plain text                                     |

A **query key** can **search** the index, but it **can't change or delete** anything. On the laptop, we needed the admin key to **build** the index. The web app only searches, so this safer key is enough.

```bash
az webapp config appsettings set -g $RG -n $APP --settings AZURE_SEARCH_API_KEY="$QUERY_KEY" -o none
unset QUERY_KEY
```

| Part                                             | What it does                                                               |
| ------------------------------------------------ | -------------------------------------------------------------------------- |
| `az webapp config appsettings set`             | save an app setting                                                        |
| `--settings AZURE_SEARCH_API_KEY="$QUERY_KEY"` | save the query key under the name our code reads: `AZURE_SEARCH_API_KEY` |
| `-o none`                                      | print nothing                                                              |
| `unset QUERY_KEY`                              | **delete** the key from the terminal, now that Azure has it          |

---

## Step 16 · Switch monitoring on

```bash
az webapp config appsettings set -g $RG -n $APP \
    --settings APPLICATIONINSIGHTS_CONNECTION_STRING="$AI_CONN" -o none
```

| Part                                                            | What it does                                                            |
| --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `az webapp config appsettings set`                            | save an app setting                                                     |
| `--settings APPLICATIONINSIGHTS_CONNECTION_STRING="$AI_CONN"` | save the monitoring address from Step 10, under the name our code reads |
| `-o none`                                                     | print nothing                                                           |

When the app starts, our file `telemetry.py` sees this setting, and starts sending timings and errors to Application Insights. It never sends the questions, the answers, or user names.

---

## Step 17 · Check the settings arrived

```bash
az webapp config appsettings list -g $RG -n $APP --query "[].name" -o tsv | sort
```

| Part                                  | What it does                                                                |
| ------------------------------------- | --------------------------------------------------------------------------- |
| `az webapp config appsettings list` | list our web app's settings                                                 |
| `--query "[].name"`                 | show only their **names**, never their values. So it's safe on screen |
| `-o tsv`                            | as plain text                                                               |
| `\| sort`                            | put them in **A to Z** order                                          |

We should see these 12 names:

```
APPLICATIONINSIGHTS_CONNECTION_STRING
AZURE_OPENAI_API_KEY
AZURE_OPENAI_API_VERSION
AZURE_OPENAI_EMBEDDING_DEPLOYMENT
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_GENERATION_DEPLOYMENT
AZURE_OPENAI_GUARDRAIL_DEPLOYMENT
AZURE_SEARCH_API_KEY
AZURE_SEARCH_ENDPOINT
AZURE_SEARCH_INDEX
SCM_DO_BUILD_DURING_DEPLOYMENT
WEBSITES_PORT
```

---

# Part 5 · Add Microsoft sign-in

Our app uses **our** Azure OpenAI and Search, which **we** pay for. So we don't want anyone in the world using it.

So, we add **Microsoft sign-in**. Only people from **our organisation** can open the app.

And here's the nice part: **App Service does the sign-in for us.** We don't write any login code.

## Step 18 · Save three values we need

```bash
HOST=$(az webapp show -g $RG -n $APP --query defaultHostName -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
CALLBACK="https://$HOST/.auth/login/aad/callback"
echo $CALLBACK
```

| Part                                                          | What it does                                                                                                                            |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `HOST=$(az webapp show ... --query defaultHostName -o tsv)` | save our app's **address**: `annual-report-rag-app.azurewebsites.net`                                                           |
| `TENANT=$(az account show --query tenantId -o tsv)`         | save our organisation's **ID**. It tells Microsoft whose people may sign in                                                       |
| `CALLBACK="https://$HOST/.auth/login/aad/callback"`         | save the **return address**: where Microsoft sends people back to, after they sign in. App Service already has this page built in |
| `echo $CALLBACK`                                            | print it, so we can see it                                                                                                              |

We should see: `https://annual-report-rag-app.azurewebsites.net/.auth/login/aad/callback`

---

## Step 19 · Register our app with Microsoft

```bash
CLIENT_ID=$(az ad app create --display-name $APP --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "$CALLBACK" --enable-id-token-issuance true --query appId -o tsv)
echo $CLIENT_ID
```

| Part                                | What it does                                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------------- |
| `CLIENT_ID=$( ... )`              | save the answer in the variable `CLIENT_ID`                                      |
| `az ad app create`                | create an **app registration**: an entry that tells Microsoft our app exists |
| `--display-name $APP`             | call it `annual-report-rag-app`                                                  |
| `--sign-in-audience AzureADMyOrg` | only people in **our own organisation** can sign in                          |
| `--web-redirect-uris "$CALLBACK"` | after sign-in, send people back to our return address from Step 18                 |
| `--enable-id-token-issuance true` | let Microsoft send our app **proof** of who signed in (called an ID token)   |
| `--query appId -o tsv`            | pick only the registration's **ID**, as plain text                           |
| `echo $CLIENT_ID`                 | print the ID                                                                       |

This ID is called the **client ID**. It's an ID, not a password, so it's fine to show.

---

## Step 20 · Create its identity in our organisation

```bash
az ad sp create --id $CLIENT_ID -o none
```

| Part                | What it does                                                                               |
| ------------------- | ------------------------------------------------------------------------------------------ |
| `az ad sp create` | create a **service principal**: our app's identity **inside** our organisation |
| `--id $CLIENT_ID` | for the app registration from Step 19                                                      |
| `-o none`         | print nothing                                                                              |

The registration **describes** our app. The service principal is what lets it **actually sign people in** in our organisation.

---

## Step 21 · Create the sign-in password, and save it on the web app

Paste **all three lines together**, and run them once.

```bash
SECRET=$(az ad app credential reset --id $CLIENT_ID --display-name "app-service-sign-in" --years 1 --append --query password -o tsv)
az webapp config appsettings set -g $RG -n $APP --settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET" -o none
unset SECRET
```

**Line 1:**

| Part                                     | What it does                                                                             |
| ---------------------------------------- | ---------------------------------------------------------------------------------------- |
| `SECRET=$( ... )`                      | save the answer in the variable `SECRET`                                               |
| `az ad app credential reset`           | create a new **client secret**: a password App Service uses to finish each sign-in |
| `--id $CLIENT_ID`                      | for our app registration                                                                 |
| `--display-name "app-service-sign-in"` | a label, so we know what this secret is for                                              |
| `--years 1`                            | it works for **one year**                                                          |
| `--append`                             | **add** it, without deleting any older secret                                      |
| `--query password -o tsv`              | pick only the password, as plain text                                                    |

**Line 2:**

| Part                                                              | What it does                                      |
| ----------------------------------------------------------------- | ------------------------------------------------- |
| `az webapp config appsettings set`                              | save an app setting                               |
| `--settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET"` | save the password on the web app, under this name |

**Line 3:**

| Part             | What it does                                                           |
| ---------------- | ---------------------------------------------------------------------- |
| `unset SECRET` | **delete** the password from the terminal, now that Azure has it |

The password is **never printed**. It lives in only two places: Microsoft's records, and our web app's settings.

---

## Step 22 · Check the password was saved

```bash
az webapp config appsettings list -g $RG -n $APP \
    --query "[?name=='MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'].value | [0]" -o tsv | tr -d '\n' | wc -c
```

| Part                                                                          | What it does                                                     |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `az webapp config appsettings list`                                         | list our web app's settings                                      |
| `--query "[?name=='MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'].value \| [0]"` | find the setting with this name, and take its **value**    |
| `-o tsv`                                                                    | as plain text                                                    |
| `\| tr -d '\n'`                                                              | remove the line break at the end                                 |
| `\| wc -c`                                                                   | **count the characters**, instead of printing the password |

We should see a number around **40**. That means the password is saved, and we never had to look at it.

---

## Step 23 · Switch to the newer sign-in settings

```bash
az webapp auth config-version upgrade -g $RG -n $APP -o none
```

| Part                       | What it does                                               |
| -------------------------- | ---------------------------------------------------------- |
| `az webapp auth`         | the web app's **sign-in** settings                   |
| `config-version upgrade` | move them to the **newer format** (called version 2) |
| `-o none`                | print nothing                                              |

App Service has an old format and a new format for sign-in settings. The next two commands only work with the **new** one. So we switch first.

---

## Step 24 · Tell App Service how to sign people in

```bash
az webapp auth microsoft update -g $RG -n $APP --client-id $CLIENT_ID \
    --client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET \
    --issuer "https://login.microsoftonline.com/$TENANT/v2.0" --yes -o none
```

| Part                                                                      | What it does                                                                                                   |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `az webapp auth microsoft update`                                       | set up **Microsoft sign-in** on our web app                                                              |
| `--client-id $CLIENT_ID`                                                | use our app registration from Step 19                                                                          |
| `--client-secret-setting-name MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` | the **name** of the setting that holds the password (from Step 21). We give the name, never the password |
| `--issuer "https://login.microsoftonline.com/$TENANT/v2.0"`             | people sign in through **our organisation's** Microsoft sign-in page                                     |
| `--yes`                                                                 | don't ask "are you sure?"                                                                                      |
| `-o none`                                                               | print nothing                                                                                                  |

---

## Step 25 · Switch sign-in on

```bash
az webapp auth update -g $RG -n $APP --enabled true --action RedirectToLoginPage \
    --redirect-provider azureactivedirectory --require-https true \
    --excluded-paths '["/health"]' -o none
```

| Part                                         | What it does                                                                                                     |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `az webapp auth update`                    | change the sign-in settings                                                                                      |
| `--enabled true`                           | **switch sign-in on**                                                                                      |
| `--action RedirectToLoginPage`             | anyone not signed in is **sent to the sign-in page**                                                       |
| `--redirect-provider azureactivedirectory` | that sign-in page is **Microsoft's**                                                                       |
| `--require-https true`                     | sign-in only happens over a **secure** connection                                                          |
| `--excluded-paths '["/health"]'`           | keep **one page open** without sign-in: `/health`. Azure visits it every minute, and Azure can't sign in |
| `-o none`                                  | print nothing                                                                                                    |

---

## Step 26 · Check sign-in is on

```bash
az webapp auth show -g $RG -n $APP \
    --query "{signin_on:properties.platform.enabled, open_paths:properties.globalValidation.excludedPaths}" -o json
```

| Part                                            | What it does                                                                 |
| ----------------------------------------------- | ---------------------------------------------------------------------------- |
| `az webapp auth show`                         | show the sign-in settings                                                    |
| `--query "{signin_on: ..., open_paths: ...}"` | pick two things: is sign-in **on**, and which pages are **open** |
| `-o json`                                     | print them in JSON format                                                    |

We should see:

```json
{
  "open_paths": ["/health"],
  "signin_on": true
}
```

---

# Part 6 · Send our code to Azure

## Step 27 · Deploy

```bash
RESOURCE_GROUP=$RG APP_NAME=$APP bash azure_setup/deploy-app.sh
```

| Part                               | What it does                               |
| ---------------------------------- | ------------------------------------------ |
| `RESOURCE_GROUP=$RG`             | hand our resource group name to the script |
| `APP_NAME=$APP`                  | hand our web app name to the script        |
| `bash azure_setup/deploy-app.sh` | run our **deploy script**            |

The script does three things:

1. **Zips** our project. It leaves out `.env` (our keys), `data/` (the PDFs), `tests/`, and `.venv/`.
2. **Uploads** the zip. Azure then installs our packages, and runs `startup.sh`. **The first time takes 5 to 10 minutes.**
3. **Waits** for `/health` to answer.

The terminal may print **`504 GatewayTimeout`**. That's normal for a first deploy. It only means the terminal stopped waiting. Azure **keeps working** in the background. Step 28 checks the real result.

---

## Step 28 · Check the deploy finished

```bash
az webapp log deployment list -g $RG -n $APP \
    --query "[0].{status:status, complete:complete, active:active, end:end_time}" -o table
```

| Part                              | What it does                                                                                                                    |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `az webapp log deployment list` | list our deploys                                                                                                                |
| `--query "[0].{...}"`           | take the **latest** deploy, and pick four things: its status, whether it's complete, whether it's live, and when it ended |
| `-o table`                      | print them as a table                                                                                                           |

We should see **status 4** and **active True**. Status 4 means the deploy **succeeded**. Active True means this is the code now running.

Run it again after a minute, until it shows status 4.

---

# Part 7 · Check the live app

## Step 29 · Check the app is running

```bash
curl https://$HOST/health
```

| Part                     | What it does                                                     |
| ------------------------ | ---------------------------------------------------------------- |
| `curl`                 | visit a web page from the terminal, and print what it sends back |
| `https://$HOST/health` | our app's `/health` page                                       |

We should see: `{"status": "ok"}`

That means our server is running on Azure.

---

## Step 30 · Check sign-in is protecting the app

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://$HOST/api/me
```

| Part                     | What it does                                                             |
| ------------------------ | ------------------------------------------------------------------------ |
| `curl`                 | visit a web page from the terminal                                       |
| `-s`                   | **silent**: don't show progress                                    |
| `-o /dev/null`         | throw away the page itself                                               |
| `-w "%{http_code}\n"`  | print only the **response code**: a number that says what happened |
| `https://$HOST/api/me` | a page that needs sign-in                                                |

We should see **302** or **401**. Both mean: **"you're not signed in, so you can't see this."** So sign-in is working.

---

## Step 31 · Open the app

```bash
open https://$HOST
```

| Part              | What it does                                                     |
| ----------------- | ---------------------------------------------------------------- |
| `open`          | open something on a Mac. For a web address, it opens the browser |
| `https://$HOST` | our app's address                                                |

The Microsoft sign-in page appears first. We sign in. The first time, Microsoft asks for permission: click **Accept**.

Then:

- the top right shows **our name**, and a **Sign out** link
- we choose **Infosys**, and ask: *"How did the company describe its currency risk?"*
- we get an answer, with the FY2025 and FY2026 extracts, and the grounding verdict

**Our app is now live on Azure.** It works exactly like it did on the laptop.

---

## Step 32 · Watch the app's logs live

```bash
az webapp log config -g $RG -n $APP --docker-container-logging filesystem -o none
```

| Part                                      | What it does                                |
| ----------------------------------------- | ------------------------------------------- |
| `az webapp log config`                  | change the web app's **log** settings |
| `--docker-container-logging filesystem` | **save** everything our app prints    |
| `-o none`                               | print nothing                               |

```bash
az webapp log tail -g $RG -n $APP
```

| Part                   | What it does                                        |
| ---------------------- | --------------------------------------------------- |
| `az webapp log tail` | show our app's output **live**, as it happens |

Now ask a question in the browser. We'll see a line like `QUERY by <name> [Infosys] How did the company...` appear.

Press **Ctrl + C** to stop watching. The app keeps running.

---

# Part 8 · Later

## After changing the code

```bash
RESOURCE_GROUP=$RG APP_NAME=$APP bash azure_setup/deploy-app.sh
```

The same command as Step 27. It replaces only the **code**. The settings and sign-in stay as they are.

---

## When you're done: stop paying

The machine (the plan) costs money **every hour it exists**. So, after recording, delete the web app and the plan.

```bash
az webapp delete -g $RG -n $APP
az appservice plan delete -g $RG -n $PLAN --yes
```

| Part                                          | What it does                                              |
| --------------------------------------------- | --------------------------------------------------------- |
| `az webapp delete -g $RG -n $APP`           | delete the **web app**: its code and settings       |
| `az appservice plan delete -g $RG -n $PLAN` | delete the **machine**. This is what stops the bill |
| `--yes`                                     | don't ask "are you sure?"                                 |

```bash
az ad app delete --id $(az ad app list --display-name $APP --query "[0].appId" -o tsv)
```

| Part                                                              | What it does                                                                    |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `az ad app list --display-name $APP --query "[0].appId" -o tsv` | find our app registration's ID by its name                                      |
| `az ad app delete --id $( ... )`                                | delete that **app registration**, and its password, from our organisation |

Our Search index, Azure OpenAI and Application Insights all **stay**. So we can deploy again any time, starting from Step 1.
