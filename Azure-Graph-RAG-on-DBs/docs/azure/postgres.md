# Azure Database for PostgreSQL — Flexible Server

## What it is used for
One Postgres server holds two databases:

| Database | Holds |
|---|---|
| `mf_data` | The 20 fund tables (schemes, NAVs, investors, transactions, ...) |
| `agent_meta` | Table notes, links, embeddings (pgvector extension) and the join graph (Apache AGE extension) |

The project needs two extensions turned on: **pgvector** (`vector`) for embeddings, and **Apache AGE** (`age`) for the graph. Both are supported by Azure, but must be switched on by you (Part 3).

---

## Part 1 — Create the server

1. Go to https://portal.azure.com.
2. In the top search bar type **Azure Database for PostgreSQL flexible servers** and open it.
3. Click **+ Create**. If asked to choose a type, choose **Flexible server**.

### Basics tab
| Field | What to enter | Why |
|---|---|---|
| Subscription | Your subscription | |
| Resource group | `rg-fundgraph` | Keeps all project resources together |
| Server name | e.g. `fundgraph-pg` | Must be unique across Azure. Becomes `fundgraph-pg.postgres.database.azure.com` |
| Region | `East US` | Same region as the other services |
| PostgreSQL version | The newest version that supports AGE (see the check below) | |
| Workload type | **Development** | Cheapest options; enough for this project |
| Compute + storage | Click **Configure server** → **Burstable**, **Standard_B2s** (2 vCores, 4 GiB). Storage **32 GiB**. Leave **Storage autogrow** on. Click **Save**. | B1ms (1 vCore) also works but query answers are slower |
| Availability zone | No preference | |
| High availability | **Disabled** | Doubles the cost; not needed for development |

**Checking AGE support for the version.** AGE is not offered on every Postgres version. Before choosing, search the web for "Azure Database for PostgreSQL flexible server extensions" and open Microsoft's list of supported extensions. Find `age` and note which versions list it. Pick the newest of those.

#### Authentication (still on the Basics tab)
| Field | What to enter |
|---|---|
| Authentication method | **PostgreSQL authentication only** (the project connects with a user name and password) |
| Administrator login | e.g. `fundgraph_admin`. Names such as `admin`, `postgres`, `azure_superuser` are not allowed. |
| Password / Confirm password | A strong password. Write it down; you will need it for `.env`. |

Click **Next: Networking**.

### Networking tab
| Field | What to select | Why |
|---|---|---|
| Connectivity method | **Public access (allowed IP addresses)** | Lets your laptop connect over the internet, but only from addresses you allow |
| Firewall rules | Click **+ Add current client IP address** | Allows your laptop's current internet address |
| Allow public access from any Azure service | Leave **unticked** | Ticking it allows every Azure customer's services, not just yours. The App Service guide adds exact rules instead. |

### Remaining tabs
- **Security**: leave the defaults (data is encrypted with a Microsoft-managed key).
- **Tags**: optional. e.g. `project = fundgraph`.

Click **Review + create**, check the estimated cost, then **Create**. Creation takes 5 to 15 minutes. When it says **Your deployment is complete**, click **Go to resource**.

---

## Part 2 — Note the connection details

On the server's **Overview** page, copy:

| Item | Where | Example |
|---|---|---|
| Server name (host) | **Server name** field | `fundgraph-pg.postgres.database.azure.com` |
| Admin user | **Administrator login** field | `fundgraph_admin` |
| Port | Always | `5432` |

---

## Part 3 — Allow pgvector and AGE (server parameters)

Azure only lets you create an extension after it has been allow-listed on the server.

1. On the server's left menu, open **Settings → Server parameters**.
2. In the search box type `azure.extensions`.
3. Click the **VALUE** dropdown and tick **AGE** and **VECTOR**. Leave anything already ticked as it is.
4. Clear the search box and type `shared_preload_libraries`.
5. Open its **VALUE** dropdown and tick **AGE**. Leave anything already ticked (e.g. `pg_cron`, `pg_stat_statements`) as it is.
6. Click **Save** at the top.
7. A message says a restart is needed (because of `shared_preload_libraries`). Click **Save and Restart**, or go to **Overview → Restart**. Wait until **Status** shows **Ready** again (1 to 3 minutes).

AGE must be in `shared_preload_libraries` because it hooks into Postgres as the server starts. Without the restart, `CREATE EXTENSION age` succeeds, but every graph query fails later.

---

## Part 4 — Fill in `.env`

Add two lines, using your server name, admin login and password:

```dotenv
DATA_DB_URL=postgresql://fundgraph_admin:<password>@fundgraph-pg.postgres.database.azure.com:5432/mf_data?sslmode=require
AGENT_DB_URL=postgresql://fundgraph_admin:<password>@fundgraph-pg.postgres.database.azure.com:5432/agent_meta?sslmode=require
```

| Part | Meaning |
|---|---|
| `fundgraph_admin:<password>` | The administrator login and password from Part 1 |
| `fundgraph-pg.postgres.database.azure.com` | The server name from Part 2 |
| `5432` | Postgres's port |
| `mf_data` / `agent_meta` | Which database to connect to. They do not exist yet; Part 5 creates them. |
| `sslmode=require` | Azure refuses unencrypted connections |

**If the password contains special characters** (`@ : / ? # % &` and similar), they must be encoded or the URL breaks. Get the encoded form with:
```bash
uv run python -c "from urllib.parse import quote; print(quote('your-password-here', safe=''))"
```
and paste the output in place of the password in both lines.

---

## Part 5 — Create the databases and extensions

From the project folder:
```bash
uv run python scripts/pg_setup.py
```
Expected output:
```
Connected: PostgreSQL <version>
  database mf_data: created
  database agent_meta: created
  agent_meta: pgvector and AGE ready
Done. Next: uv run python dataplane/schema/apply_schema.py
```
If it stops with **Server parameters not ready**, it lists what is missing; go back to Part 3.

Then check the connection with the tests:
```bash
uv run pytest tests/test_stage0_setup.py -v
```
Both tests should pass.

---

## Part 6 — Build and load the data

```bash
uv run python dataplane/schema/apply_schema.py      # the 20 empty tables
uv run python -m dataplane.generator.run            # generate and load the data (a few minutes)
uv run pytest tests/test_stage1_schema.py tests/test_stage2_data.py -v
```

---

## Looking at the data in the portal (optional)

Azure does not include a full query editor for Postgres. To browse tables with a screen, install a free client such as **pgAdmin** or **DBeaver**, or the **PostgreSQL** extension for VS Code, and connect with:

| Field | Value |
|---|---|
| Host | `fundgraph-pg.postgres.database.azure.com` |
| Port | `5432` |
| Database | `mf_data` |
| User / Password | your admin login and password |
| SSL mode | `require` |

---

## Day-to-day

| Task | Where |
|---|---|
| Stop the server to save money | **Overview → Stop**. Storage is still billed. Azure restarts a stopped server automatically after 7 days. |
| Start it again | **Overview → Start** |
| Your home or office IP changed | **Settings → Networking → + Add current client IP address → Save** |
| See CPU, memory, connections | **Monitoring → Metrics** |
| Change size | **Settings → Compute + storage** (causes a short restart) |

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `connection timeout` / `could not connect to server` | Your IP is not in the firewall rules, or the server is stopped | Part "Day-to-day": add your IP; check **Status** on Overview |
| `password authentication failed` | Wrong login or password, or special characters not encoded | Check Part 4; reset the password under **Overview → Reset password** if needed |
| `extension "age" is not allow-listed` | Part 3 not done | Add AGE and VECTOR to `azure.extensions` |
| `function cypher does not exist` or AGE errors at query time | `shared_preload_libraries` lacks AGE, or the server was not restarted | Part 3 steps 4 to 7 |
| `SSL connection is required` | `?sslmode=require` missing from the URL | Add it to both URLs |
