# Microsoft Entra ID sign-in (App Service Authentication)

## What it is used for
Microsoft Entra ID (formerly Azure Active Directory) is Azure's identity service: the accounts people use to sign in to Microsoft 365, Teams and the Azure portal.

App Service has built-in sign-in, often called **Easy Auth**. When it is switched on:
1. Anyone opening the app who is not signed in is sent to the Microsoft sign-in page.
2. After signing in, App Service passes the user's identity to the app in request headers (`X-MS-CLIENT-PRINCIPAL-ID`, `X-MS-CLIENT-PRINCIPAL-NAME`).
3. The app (`api/main.py`) uses that identity to keep each user's conversations separate, and refuses `/query` requests that arrive without it.

No sign-in code is in the project itself; App Service handles it before a request reaches the app. On your laptop there is no sign-in.

| Term | Meaning |
|---|---|
| **Tenant** | Your organisation's Entra ID directory: its users and groups |
| **App registration** | An entry in your tenant that represents this app, so Entra ID knows where to send users after sign-in |
| **Client ID** | The app registration's id |
| **Client secret** | A password App Service uses to complete sign-in. It expires and must be renewed. |
| **Redirect URI** | Where Entra ID sends the user after sign-in: `https://<app>.azurewebsites.net/.auth/login/aad/callback` |

---

## Who can set this up

Creating an app registration needs permission in your tenant. In many company tenants ordinary users cannot do it. If the steps below fail with a permissions error, ask a tenant administrator to do Part A, or give you the **Application Developer** role (Entra ID → **Roles and administrators**).

If you used `infra/azure/create-app-service.sh`, Part A is already done; go to Part B to check it.

---

## Part A — Switch on sign-in in the portal

1. Go to https://portal.azure.com → **App Services** → your web app (e.g. `fundgraph-demo`).
2. In the left menu open **Settings → Authentication**.
3. Click **Add identity provider**.

### Basics
| Field | What to select | Why |
|---|---|---|
| Identity provider | **Microsoft** | Entra ID accounts |
| Tenant type | **Workforce configuration (current tenant)** | Your organisation's accounts |
| App registration type | **Create new app registration** | Creates the app registration for you, with the correct redirect URI |
| Name | e.g. `fundgraph-demo` | Shown on the sign-in consent page |
| Client secret expiration | e.g. **180 days** | When it expires, sign-in stops working until it is renewed (Part D) |
| Supported account types | **Current tenant - Single tenant** | Only people in your organisation can sign in |

### Additional checks
| Field | What to select |
|---|---|
| Client application requirement | **Allow requests only from this application itself** |
| Identity requirement | **Allow requests from any identity** (Part C shows how to limit this to chosen people) |
| Tenant requirement | **Allow requests only from the issuer tenant** |

### App Service authentication settings
| Field | What to select | Why |
|---|---|---|
| Restrict access | **Require authentication** | Nobody uses the app without signing in |
| Unauthenticated requests | **HTTP 302 Found redirect: recommended for websites** | Sends people to the sign-in page instead of showing an error |
| Token store | Ticked | Lets App Service keep the user signed in |

### Permissions
Leave the default (**User.Read**). It lets the app read the signed-in user's name and email.

Click **Add**. The page now lists **Microsoft** under identity providers, and **App Service authentication** shows **Enabled**.

### Leave `/health` open
App Service's health check calls `/health` without signing in, so that path must stay open. The portal has no field for this; run once from your terminal (after `az login`):
```bash
az webapp auth update --name fundgraph-demo --resource-group rg-fundgraph --excluded-paths '["/health"]'
```

---

## Part B — Test sign-in

1. Open a private or incognito browser window (so you are not already signed in).
2. Go to `https://fundgraph-demo.azurewebsites.net`.
3. You are sent to the Microsoft sign-in page. Sign in with an account from your organisation.
4. The first time, a **Permissions requested** page appears. Click **Accept**.
5. The demo page opens. Your name appears at the top right with a **Sign out** link.

Other checks:
| Open | Expected |
|---|---|
| `https://fundgraph-demo.azurewebsites.net/health` (private window, not signed in) | `{"status":"ok"}` — health stays open |
| `https://fundgraph-demo.azurewebsites.net/me` (signed in) | Your id and name, as the app sees them |
| `https://fundgraph-demo.azurewebsites.net/.auth/me` (signed in) | Everything App Service knows about your sign-in |
| `https://fundgraph-demo.azurewebsites.net/.auth/logout` | Signs you out |

---

## Part C — Allow only chosen people (recommended)

By default, everyone in your organisation can sign in. To limit it to named people or a group:

1. In the portal search **Microsoft Entra ID** and open it.
2. Open **Manage → Enterprise applications**. Clear the filter, search `fundgraph-demo`, and open it.
3. Open **Manage → Properties**. Set **Assignment required?** to **Yes**. Click **Save**.
4. Open **Manage → Users and groups** → **+ Add user/group** → **Users and groups: None Selected** → pick people or a group → **Select** → **Assign**.

Anyone not assigned now sees an error after signing in and cannot reach the app.

---

## Part D — Renew the client secret before it expires

When the secret expires, everyone gets an error at sign-in.

1. **Microsoft Entra ID → Manage → App registrations → All applications** → `fundgraph-demo`.
2. **Manage → Certificates & secrets** shows each secret's **Expires** date.
3. Click **+ New client secret**, add a description and expiry, click **Add**. Copy the **Value** straight away (it is shown only once).
4. Go to the web app → **Settings → Environment variables**. Edit `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`, paste the new value, **Apply** → **Confirm**.
5. Test sign-in (Part B), then delete the old secret in **Certificates & secrets**.

Re-running `infra/azure/create-app-service.sh` also creates and stores a new secret.

---

## What the app does with the identity

In `api/main.py`:
- `/query` reads the user's id from the `X-MS-CLIENT-PRINCIPAL-ID` header. App Service sets this header itself and removes any copy a caller tries to send, so it cannot be faked from outside.
- Each conversation is stored under the user's id, so one user cannot continue or read another user's conversation.
- On App Service (detected by the `WEBSITE_SITE_NAME` setting, which Azure always sets), a request without the header gets `401 Sign-in required`. This protects the data if sign-in is ever switched off by mistake.
- `/me` returns the signed-in user, for the page header.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AADSTS50011: The redirect URI ... does not match` | The app registration's redirect URI is wrong (e.g. the app was renamed) | Entra ID → App registrations → app → **Authentication** → set the web redirect URI to `https://<app>.azurewebsites.net/.auth/login/aad/callback` |
| `AADSTS7000222: The provided client secret keys are expired` | Secret expired | Part D |
| `AADSTS50105: ... not assigned to a role for the application` | Assignment required is on and the user is not assigned | Part C step 4 |
| Page loads but every question returns `401 Sign-in required` | Authentication is off, or set to **Allow unauthenticated access** | Part A: **Restrict access** must be **Require authentication** |
| App Service health check reports the app unhealthy | `/health` is behind sign-in | Run the `--excluded-paths` command in Part A |
| You cannot create the app registration | No permission in the tenant | See "Who can set this up" |
