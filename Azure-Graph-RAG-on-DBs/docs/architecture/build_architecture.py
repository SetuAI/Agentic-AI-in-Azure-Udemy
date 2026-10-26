"""
Builds the end-to-end architecture as an Excalidraw file, plus an SVG preview
drawn from the same elements (to check the layout before opening it in Excalidraw).

    python3 docs/architecture/build_architecture.py docs/architecture/architecture.excalidraw docs/architecture/architecture-preview.svg
"""
# HOW TO RUN (from the project folder) - regenerates the diagram files:
#   Windows (PowerShell):
#     uv run python docs/architecture/build_architecture.py docs/architecture/architecture.excalidraw docs/architecture/architecture-preview.svg
#   Mac / Linux:
#     uv run python docs/architecture/build_architecture.py docs/architecture/architecture.excalidraw docs/architecture/architecture-preview.svg
# Argument 1 = the Excalidraw file to write; argument 2 = the SVG preview to write.
# Open the .excalidraw file at https://excalidraw.com (File -> Open) to view or edit it.
#
# json: writes the Excalidraw file (it is JSON).
import json

# random: Excalidraw gives every shape a random "seed" for its hand-drawn look.
import random

# sys.argv: the file names given on the command line.
import sys

# escape: makes text safe inside the SVG (e.g. "<" -> "&lt;").
from html import escape

# pairwise([a, b, c]) gives (a, b), (b, c): used to draw arrows between consecutive steps.
from itertools import pairwise

# Path: writes the output files.
from pathlib import Path

# A fixed seed, so every run draws exactly the same diagram.
random.seed(7)
EL = []          # excalidraw elements
# A counter used to give every element a unique id.
_n = 0

# ---------------------------------------------------------------------------
# Palette (Excalidraw's own colours)
# ---------------------------------------------------------------------------
# Near-black, for normal text and lines.
INK = "#1e1e1e"
# Grey, for secondary text.
MUTED = "#495057"
# kind -> (stroke colour, fill colour) for each kind of box.
C = {
    "openai":  ("#6741d9", "#e5dbff"),   # Azure OpenAI
    "vector":  ("#0c8599", "#c3fae8"),   # pgvector
    "graph":   ("#c92a2a", "#ffe3e3"),   # Apache AGE
    "sql":     ("#1971c2", "#d0ebff"),   # PostgreSQL SQL
    "safety":  ("#e8590c", "#ffe8cc"),   # Content Safety
    "python":  ("#495057", "#f1f3f5"),   # plain Python
    "human":   ("#2f9e44", "#d3f9d8"),   # human step
    "azure":   ("#1864ab", "#e7f5ff"),   # Azure service frames
    "insight": ("#5c940d", "#e9fac8"),   # App Insights
    "entra":   ("#0b7285", "#e3fafc"),   # Entra ID
}


# Return a new unique id: "el1", "el2", ...
def _id():
    # "global" lets this function change the module-level counter _n.
    global _n
    # Increase the counter by one.
    _n += 1
    # Build the id text.
    return f"el{_n}"


# Create one Excalidraw element (rectangle, ellipse, text, arrow) with all the
# fields Excalidraw expects, add it to EL, and return it. **kw = optional extras.
def _base(kind, x, y, w, h, stroke=INK, bg="transparent", **kw):
    # Every field Excalidraw needs for one element (position, size, colours, style, ...).
    e = {
        "id": _id(), "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": stroke, "backgroundColor": bg, "fillStyle": "solid",
        "strokeWidth": kw.get("sw", 2), "strokeStyle": kw.get("style", "solid"),
        "roughness": kw.get("rough", 1), "opacity": kw.get("opacity", 100),
        "groupIds": [], "frameId": None,
        "roundness": {"type": 3} if kind == "rectangle" and kw.get("round", True) else None,
        "seed": random.randint(1, 2**31), "version": 1, "versionNonce": random.randint(1, 2**31),
        "isDeleted": False, "boundElements": [], "updated": 1, "link": None, "locked": False,
    }
    # Add it to the drawing.
    EL.append(e)
    # Return it so callers can change it further.
    return e


# Rough width and height of a text block: longest line x size x 0.56 wide, lines x 1.25 tall.
def _measure(text, size):
    # Split the text into lines.
    lines = text.split("\n")
    # Width from the longest line, height from the number of lines.
    return max(len(l) for l in lines) * size * 0.56, len(lines) * size * 1.25


# Place free text at (x, y). family: 1 = hand-drawn, 2 = normal, 3 = monospace.
def text(x, y, s, size=16, color=INK, family=2, align="left", w=None):
    # Its size.
    tw, th = _measure(s, size)
    # A text element ...
    e = _base("text", x, y, w or tw, th, stroke=color, sw=1, rough=0)
    # ... with the text-specific fields.
    e.update({"text": s, "originalText": s, "fontSize": size, "fontFamily": family,
              "textAlign": align, "verticalAlign": "top", "containerId": None,
              "lineHeight": 1.25, "autoResize": True})
    # Return the text element.
    return e


# A coloured box (rectangle or ellipse) with an optional centred label inside it.
def box(x, y, w, h, label="", kind="python", size=16, family=2, style="solid", sw=2,
        shape="rectangle", bold_bg=None, valign="middle", label_color=None):
    # The colours for this kind of box.
    stroke, bg = C[kind]
    # The box itself.
    e = _base(shape, x, y, w, h, stroke=stroke, bg=bold_bg or bg, style=style, sw=sw)
    # Add the label as a text element bound to the box (it moves with the box in Excalidraw).
    if label:
        # The label's size, used to centre it in the box.
        tw, th = _measure(label, size)
        # The label text element, centred, bound to the box (containerId).
        t = _base("text", x + (w - tw) / 2, y + (h - th) / 2, tw, th,
                  stroke=label_color or INK, sw=1, rough=0)
        # Its text-specific fields.
        t.update({"text": label, "originalText": label, "fontSize": size, "fontFamily": family,
                  "textAlign": "center", "verticalAlign": valign, "containerId": e["id"],
                  "lineHeight": 1.25, "autoResize": True})
        # Tell the box about its label.
        e["boundElements"].append({"type": "text", "id": t["id"]})
    # Return the box (its label is a separate element).
    return e


# A large, faint rectangle with a title (and optional subtitle): groups related boxes.
def frame(x, y, w, h, title, kind="azure", style="dashed", size=20, sub=None):
    # The frame's colours.
    stroke, bg = C[kind]
    # The frame rectangle, 55% opaque.
    f = _base("rectangle", x, y, w, h, stroke=stroke, bg=bg, style=style, sw=2, opacity=55)
    # Its title in the top-left corner.
    text(x + 16, y + 10, title, size=size, color=stroke, family=1)
    # Optional smaller subtitle under the title.
    if sub:
        # The subtitle, under the title.
        text(x + 16, y + 10 + size * 1.35, sub, size=13, color=MUTED)
    # Return the frame rectangle.
    return f


# Helper: where an arrow should touch a box's border.
def _edge_point(el, toward):
    """Point on el's border in the direction of point `toward`."""
    # The element's centre.
    cx, cy = el["x"] + el["width"] / 2, el["y"] + el["height"] / 2
    # Direction from the centre to the target point.
    dx, dy = toward[0] - cx, toward[1] - cy
    # Target is the centre itself: just return the centre.
    if dx == 0 and dy == 0:
        # (Return the centre.)
        return cx, cy
    # How far along the direction we can go before hitting the left/right side ...
    sx = (el["width"] / 2) / abs(dx) if dx else float("inf")
    # ... and before hitting the top/bottom side.
    sy = (el["height"] / 2) / abs(dy) if dy else float("inf")
    # Scale so the point lands on whichever edge (side or top/bottom) is hit first.
    s = min(sx, sy)
    # The point on the border.
    return cx + dx * s, cy + dy * s


# Draw an arrow from box a to box b, optionally with a label and waypoints.
def arrow(a, b, label=None, color=INK, style="solid", pts=None, start=None, end=None, size=13,
          label_at=None, both=False):
    """Arrow from element a to element b. pts = optional absolute waypoints between them."""
    # No waypoints given -> an empty list.
    pts = pts or []
    # The point the arrow heads for first (first waypoint, or b's centre).
    first_target = pts[0] if pts else (b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    # The point the arrow comes from last (last waypoint, or a's centre).
    last_source = pts[-1] if pts else (a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    # Start point: given, or on a's border facing the next point.
    p0 = start or _edge_point(a, first_target)
    # End point: given, or on b's border facing the previous point.
    p1 = end or _edge_point(b, last_source)
    # The full route: start, waypoints, end.
    path = [p0, *pts, p1]
    # All x values and all y values of the route.
    xs, ys = [p[0] for p in path], [p[1] for p in path]
    # The arrow element, sized to the route's bounding box.
    e = _base("arrow", p0[0], p0[1], max(xs) - min(xs), max(ys) - min(ys), stroke=color,
              style=style, sw=2, round=False)
    # Gently rounded bends.
    e["roundness"] = {"type": 2}
    # Excalidraw stores arrow points relative to the start point.
    e["points"] = [[px - p0[0], py - p0[1]] for px, py in path]
    # Fields Excalidraw expects on arrows.
    e["lastCommittedPoint"] = None
    # Bind both ends to their boxes, so the arrow follows them when moved.
    e["startBinding"] = {"elementId": a["id"], "focus": 0, "gap": 4}
    # (the end binding)
    e["endBinding"] = {"elementId": b["id"], "focus": 0, "gap": 4}
    # both=True draws an arrowhead at the start too.
    e["startArrowhead"] = "arrow" if both else None
    # Always an arrowhead at the end.
    e["endArrowhead"] = "arrow"
    # A straight/curved arrow, not an elbow connector.
    e["elbowed"] = False
    # Tell both boxes about the arrow.
    a["boundElements"].append({"type": "arrow", "id": e["id"]})
    # (and box b)
    b["boundElements"].append({"type": "arrow", "id": e["id"]})
    # Optional label on the arrow:
    if label:
        # Free text placed near the arrow's middle (or label_at), with a white backing box.
        if label_at:
            # Use the given label position.
            mx, my = label_at
        # Otherwise:
        else:
            # The middle waypoint, or halfway between start and end.
            mid = path[len(path) // 2] if len(path) > 2 else ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
            # Use it as the label position.
            mx, my = mid
        # Size of the label text.
        tw, th = _measure(label, size)
        # A white box behind the label, so the line does not run through the text.
        bgbox = _base("rectangle", mx - tw / 2 - 6, my - th / 2 - 3, tw + 12, th + 6,
                      stroke="transparent", bg="#ffffff", sw=1, rough=0)
        # White border too, so the backing box is invisible.
        bgbox["strokeColor"] = "#ffffff"
        # The label itself, centred on (mx, my).
        text(mx - tw / 2, my - th / 2, label, size=size, color=color, align="center")
    # Return the arrow.
    return e


# ===========================================================================
# LAYOUT
# ===========================================================================
# The diagram's main title and subtitle.
text(40, 24, "Azure GraphRAG on Databases — end-to-end architecture", size=36, family=1)
# One-line description under the title.
text(42, 76, "Plain-English questions over a 20-table mutual fund database. Tables are graph nodes, joins are hops. "
             "Every answer = a table + commentary + the exact, checked, read-only SQL.", size=16, color=MUTED)

# ---------------------------------------------------------------------------
# ① BUILD TIME (left)
# ---------------------------------------------------------------------------
# Position (x, y) and size (width, height) of the left "build time" frame.
AX, AY, AW, AH = 40, 130, 640, 1110
# The frame with its title and subtitle.
frame(AX, AY, AW, AH, "① BUILD TIME  ·  run once, then after changes", kind="python",
      sub="Your laptop  ·  uv run …")

# Section heading.
text(AX + 40, AY + 80, "Data plane  (dataplane/)", size=18, color=C["sql"][0], family=1)
# The three data-plane scripts, top to bottom, with an explanation to the right of each.
dp1 = box(AX + 40, AY + 115, 250, 62, "scripts/pg_setup.py", kind="sql", size=15)
# Step 2: apply_schema.py.
dp2 = box(AX + 40, AY + 207, 250, 62, "schema/apply_schema.py", kind="sql", size=15)
# Step 3: the data generator.
dp3 = box(AX + 40, AY + 299, 250, 62, "generator.run", kind="sql", size=15)
# What pg_setup.py does.
text(AX + 312, AY + 122, "Create mf_data + agent_meta,\nswitch on pgvector + AGE", size=14, color=MUTED)
# What apply_schema.py does.
text(AX + 312, AY + 214, "20 tables, keys, indexes,\n60 monthly partitions", size=14, color=MUTED)
# What the generator does.
text(AX + 312, AY + 300, "Synthetic AMC data + 6 planted\nstories → 1,132,667 rows (COPY)", size=14, color=MUTED)
# Arrows show the order to run them in.
arrow(dp1, dp2, color=C["sql"][0])
# (schema -> generator)
arrow(dp2, dp3, color=C["sql"][0])

# Section heading.
text(AX + 40, AY + 420, "Metadata pipeline  (metadata/)", size=18, color=C["openai"][0], family=1)
# The three metadata pipeline steps (build -> review -> publish).
mp1 = box(AX + 40, AY + 455, 250, 74, "metadata.build", kind="openai", size=15)
# Review (a person approves).
mp2 = box(AX + 40, AY + 569, 250, 74, "metadata.review\n👤 human approves", kind="human", size=15)
# Publish.
mp3 = box(AX + 40, AY + 683, 250, 74, "metadata.publish", kind="openai", size=15)
# What build does.
text(AX + 312, AY + 448, "Reads mf_data's catalog + samples\n→ links: 19 declared FKs + 3 hidden\n   (same name, value overlap)\n→ drafts notes with gpt-5.1", size=14, color=MUTED)
# What review does.
text(AX + 312, AY + 576, "Reject wrong links, approve the\nrest. Nothing guessed is used\nuntil a person approves it.", size=14, color=MUTED)
# What publish does.
text(AX + 312, AY + 683, "Approved notes + values →\nembeddings (text-embedding-3-small)\nApproved links → AGE graph", size=14, color=MUTED)
# build -> review
arrow(mp1, mp2, color=C["openai"][0])
# review -> publish
arrow(mp2, mp3, color=C["human"][0])

# A box summarising the config/ files and the tests.
box(AX + 40, AY + 810, 570, 120,
    "config/   models · agent · metrics (approved business definitions)\n"
    "generator · pii · metadata\n\n"
    "tests/   66 tests, stage by stage, against the real Azure services",
    kind="python", size=14)
# Heading for the list of discovered hidden links.
text(AX + 40, AY + 960, "Hidden links the pipeline discovered", size=16, color=C["graph"][0], family=1)
# The three hidden links, in monospace (family=3).
text(AX + 40, AY + 992,
     "credit_rating_history.sec_cd → securities.security_code\n"
     "transactions.arn_code → distributors.arn_no\n"
     "scheme_aum_monthly.scheme_id → schemes.scheme_id",
     size=13, color=INK, family=3)
# A note about the evidence.
text(AX + 40, AY + 1050, "each: 100% of values match, no foreign key declared", size=13, color=MUTED)

# ---------------------------------------------------------------------------
# ② RUN TIME (right)
# ---------------------------------------------------------------------------
# Position and size of the right "run time" frame.
BX, BY, BW, BH = 730, 130, 1530, 1110
# The frame with its title and subtitle.
frame(BX, BY, BW, BH, "② RUN TIME  ·  every question", kind="azure", style="dashed",
      sub="Azure subscription  ·  resource group fundgraph-rg-1")

SX, SY, SW_, SH = BX + 270, BY + 80, 760, 1010          # App Service frame: x 1000..1760
# The user, drawn as an ellipse.
user = box(BX + 40, BY + 62, 200, 62, "👤 User · browser", kind="python", size=16, shape="ellipse")
# Entra ID sign-in sits between the user and the app.
entra = box(BX + 40, SY + 70, 200, 110, "Microsoft Entra ID\nsign-in (Easy Auth)\nadds X-MS-CLIENT-\nPRINCIPAL headers", kind="entra", size=14)
# User -> Entra ID sign-in, labelled HTTPS.
arrow(user, entra, label="HTTPS", color=C["entra"][0], label_at=(BX + 175, (BY + 124 + SY + 70) / 2))

# The App Service frame (x 1000..1760), inside the run-time frame.
frame(SX, SY, SW_, SH, "Azure App Service  ·  Linux · Python 3.13 · B1", kind="azure", style="solid",
      sub="startup.sh → uvicorn api.main:app  ·  1 worker  ·  health check /health")
# The web page and the API, side by side inside App Service.
ui = box(SX + 30, SY + 85, 330, 78, "Web UI  (ui/index.html)\ntrace · graph · records · SQL", kind="python", size=14)
# The FastAPI box.
api = box(SX + 400, SY + 85, 330, 78, "FastAPI  (api/main.py)\n/query · /schema-graph · /table-counts", kind="python", size=14)
# A two-way arrow: the page calls the API and gets JSON back.
arrow(ui, api, color=INK, both=True)
# The y position shared by the horizontal arrows at this level.
uy = SY + 124
# Entra ID -> the web page (a straight horizontal arrow at height uy).
arrow(entra, ui, color=C["entra"][0], start=(BX + 240, uy), end=(SX + 30, uy))

GX, GY, GW, GH = SX + 30, SY + 195, 700, 800                # agent frame: x 1030..1730
frame(GX, GY, GW, GH, "LangGraph agent  ·  11 fixed steps  (agent/)", kind="python", style="solid", size=18)
# The 11 agent steps: (number + name, short description, colour kind).
steps = [
    ("1   guard_input", "Prompt Shields: block prompt attacks", "safety"),
    ("2   understand", "rewrite · intent · period · metrics · data needed", "openai"),
    ("3   match_entities", "names → exact DB values", "vector"),
    ("4   select_tables", "metrics + each data need + table search", "vector"),
    ("5   find_join_path", "shortest route  MATCH (a)-[*1..6]-(b)", "graph"),
    ("6   write_sql", "one SELECT: given tables, joins, definitions", "openai"),
    ("7   check_sql", "sqlglot rules + EXPLAIN cost", "sql"),
    ("8   run_sql", "READ ONLY · 15 s timeout · 5,000-row cap", "sql"),
    ("9   summarise", "trim the result for the model", "python"),
    ("10  write_commentary", "plain-English explanation", "openai"),
    ("11  remember", "keep question + SQL for follow-ups", "python"),
]
# The step boxes, in order.
step_el = []
# One box per step, 64 pixels apart vertically.
for i, (name, desc, kind) in enumerate(steps):
    # This step's y position.
    y = GY + 56 + i * 64
    # The step box: "1 · guard_input  —  description", coloured by what it uses.
    b = box(GX + 24, y, 652, 52, f"{name.split()[0]} · {name.split()[1]}  —  {desc}", kind=kind, size=14, family=3)
    # Keep it.
    step_el.append(b)
# An arrow from each step to the next.
for a, b in pairwise(step_el):
    # (step i -> step i+1)
    arrow(a, b, color=MUTED)
# A "lambda" is a tiny one-line function: cy(i) = the centre y of step i.
cy = lambda i: step_el[i]["y"] + 26                         # centre y of step i
SL, SR = GX + 24, GX + 676                                  # step box left / right edges

# API → step 1 (straight down)
arrow(api, step_el[0], color=INK, start=(SX + 565, SY + 163), end=(SX + 565, step_el[0]["y"]))
# retry loop on the left: run_sql / check_sql → write_sql
arrow(step_el[7], step_el[5], color=C["graph"][0], style="dashed",
      start=(SL, cy(7) - 10), pts=[(GX + 10, cy(7) - 10), (GX + 10, cy(5))], end=(SL, cy(5)))
# A note explaining the retry loop.
text(GX + 24, GY + GH - 34, "↺  check_sql or run_sql fails → back to write_sql with the error (at most 2 retries)",
     size=13, color=C["graph"][0])

# Right-hand column: Application Insights, Content Safety, Azure OpenAI, legend
# x position of the right-hand column of Azure services.
RX = 1820
# Application Insights box.
ai = box(RX, SY + 70, 400, 100, "Application Insights\nOpenTelemetry: a span per request\nand per agent step", kind="insight", size=14)
# Content Safety box, level with step 1.
cs = box(RX, cy(0) - 50, 400, 100, "Azure AI Content Safety\nPrompt Shields", kind="safety", size=16)
# Azure OpenAI box, below step 2.
oai = box(RX, cy(1) + 90, 400, 150, "Azure OpenAI  (resource endpoint)\n\ngpt-5.1: understand · SQL · commentary\ntext-embedding-3-small: 1,536-dim vectors", kind="openai", size=14)

# Arrows from the app/steps to Application Insights, Content Safety and Azure OpenAI.
arrow(api, ai, label="traces", color=C["insight"][0], style="dashed",
      start=(SX + 730, uy), pts=[(1790, uy)], end=(RX, uy), label_at=(1790, uy - 18))
# Step 1 -> Content Safety.
arrow(step_el[0], cs, label="screen question", color=C["safety"][0],
      start=(SR, cy(0)), end=(RX, cy(0)), label_at=((SR + RX) / 2 + 10, cy(0) - 18))
# Steps 2, 6, 10 -> Azure OpenAI.
arrow(step_el[1], oai, label="steps 2 · 6 · 10", color=C["openai"][0],
      start=(SR, cy(1)), pts=[(1792, cy(1)), (1792, oai["y"] + 75)], end=(RX, oai["y"] + 75),
      label_at=(1792, oai["y"] + 20))

# The colour legend, under the Azure OpenAI box.
LX, LY = RX, oai["y"] + 190
# Legend heading.
text(LX, LY, "Step colour = what it uses", size=16, family=1)
# (label, colour kind) for each legend entry.
legend = [("Azure OpenAI", "openai"), ("pgvector search", "vector"), ("Apache AGE graph", "graph"),
          ("PostgreSQL SQL", "sql"), ("Content Safety", "safety"), ("Python only", "python"),
          ("Human approval", "human")]
# Two columns of small coloured squares with labels.
for i, (lbl, kind) in enumerate(legend):
    # % = remainder (column 0 or 1); // = whole-number division (row).
    col, row = i % 2, i // 2
    # A small coloured square ...
    box(LX + col * 200, LY + 34 + row * 38, 26, 26, "", kind=kind, sw=2)
    # ... and its label.
    text(LX + 36 + col * 200, LY + 38 + row * 38, lbl, size=14)

# ---------------------------------------------------------------------------
# ③ DATA (bottom)
# ---------------------------------------------------------------------------
# Position and size of the bottom "data" frame (the PostgreSQL server).
DX, DY, DW, DH = 40, 1300, 2220, 420
# The PostgreSQL server frame with its settings as the subtitle.
frame(DX, DY, DW, DH, "③ Azure Database for PostgreSQL  ·  Flexible Server  ·  one server, two databases",
      kind="sql", style="solid",
      sub="PostgreSQL 16 · Burstable B1ms · Central India · azure.extensions = VECTOR, AGE · shared_preload_libraries ⊃ age · sslmode=require")

# The mf_data frame inside it, with one box per group of tables.
MX, MY, MW, MH = DX + 30, DY + 90, 1040, 300
# The mf_data frame.
mf = frame(MX, MY, MW, MH, "mf_data  —  business data, read-only to the agent  ·  20 tables · 1,132,667 rows",
           kind="sql", style="solid", size=17)
# The 4 groups of business tables (text shown in each box).
groups = [
    "Fund house\n\nschemes · scheme_plans\nscheme_categories\nbenchmarks · benchmark_values\nnav_history\nscheme_aum_monthly\nfund_managers\nscheme_manager_assignments",
    "Market\n\nsectors · issuer_groups\nissuers · securities\ncredit_rating_history\nportfolio_holdings",
    "Investors\n\ndistributors · investors\nfolios · sip_registrations\n\nPII: name, email, mobile,\nPAN, DOB, bank (masked)",
    "Transactions\n\ntransactions\n683,159 rows\n60 monthly partitions\n(date filter required)",
]
# The width of each of the 4 group boxes (minus margins and 3 gaps of 14).
gw = (MW - 50 - 3 * 14) / 4
# Draw the 4 group boxes side by side.
mf_boxes = [box(MX + 25 + i * (gw + 14), MY + 60, gw, 215, g, kind="sql", size=13) for i, g in enumerate(groups)]

# The agent_meta frame, to the right of mf_data, with its 4 stores.
KX, KY, KW, KH = MX + MW + 40, MY, DW - MW - 100, 300
# The agent_meta frame.
am = frame(KX, KY, KW, KH, "agent_meta  —  what the agent knows about mf_data", kind="openai", style="solid", size=17)
# Width of each of its 4 boxes.
kw = (KW - 50 - 3 * 14) / 4
# kx(i) = x position of box i.
kx = lambda i: KX + 25 + i * (kw + 14)
# Notes tables.
k1 = box(kx(0), KY + 60, kw, 215, "meta_tables\nmeta_columns\n\n20 + 124 approved\ndescriptions\n(PII flagged)", kind="python", size=13)
# Links table.
k2 = box(kx(1), KY + 60, kw, 215, "meta_edges\n\n22 links\n19 declared · 3 discovered\nwith evidence\nand confidence", kind="python", size=13)
# The AGE graph.
k3 = box(kx(2), KY + 60, kw, 215, "schema_graph\nApache AGE\n\n20 :Table nodes\n22 :JOINS edges\nCypher routes", kind="graph", size=13)
# The pgvector embeddings.
k4 = box(kx(3), KY + 60, kw, 215, "meta_embeddings\npgvector · HNSW\n\n1,415 vectors\n20 table notes\n1,395 values", kind="vector", size=13)

# ---------------------------------------------------------------------------
# Arrows between the layers
# ---------------------------------------------------------------------------
# The arrows below connect the layers; each uses waypoints so lines do not cross boxes.
CH = 1270                                                   # channel between run time and data
# Build: load data (down the left margin)
arrow(dp3, mf, label="load (COPY)", color=C["sql"][0],
      start=(dp3["x"], cy_dp := dp3["y"] + 31), pts=[(AX - 14, cy_dp), (AX - 14, MY + 150)],
      end=(MX, MY + 150), label_at=(AX + 60, CH))
# Build: publish into agent_meta
arrow(mp3, am, label="publish: embeddings + graph", color=C["openai"][0],
      start=(mp3["x"] + 250, mp3["y"] + 37),
      pts=[(AX + AW - 20, mp3["y"] + 37), (AX + AW - 20, CH + 12), (kx(1) + kw / 2, CH + 12)],
      end=(kx(1) + kw / 2, KY), label_at=(800, CH + 12))
# Run: read-only SQL (left lane, down into transactions)
# x position of the vertical lane for the SQL arrow.
lane_sql = 985
# run_sql (step 8) -> the transactions box.
arrow(step_el[7], mf_boxes[3], label="read-only SQL", color=C["sql"][0],
      start=(SL, cy(7) + 12), pts=[(lane_sql, cy(7) + 12)], end=(lane_sql, mf_boxes[3]["y"]),
      label_at=(lane_sql, CH - 22))
# Run: AGE route (inner right lane, straight down into schema_graph)
# x position of the lane for the graph arrow.
lane_graph = kx(2) + 50
# find_join_path (step 5) -> the AGE graph.
arrow(step_el[4], k3, label="Cypher route", color=C["graph"][0],
      start=(SR, cy(4)), pts=[(lane_graph, cy(4))], end=(lane_graph, k3["y"]),
      label_at=(lane_graph, CH - 22))
# Run: vector search (outer right lane, then right into meta_embeddings)
# x position of the lane for the vector-search arrow.
lane_vec = 1778
# select_tables (step 4) -> the embeddings.
arrow(step_el[3], k4, label="vector search (steps 3–4)", color=C["vector"][0],
      start=(SR, cy(3)), pts=[(lane_vec, cy(3)), (lane_vec, CH - 5), (kx(3) + kw / 2, CH - 5)],
      end=(kx(3) + kw / 2, k4["y"]), label_at=(kx(3) + kw / 2 - 40, CH - 25))

# The complete Excalidraw document: every element plus a white background.
doc = {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com",
       "elements": EL, "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": {}}
# Write it to the first file name given on the command line.
Path(sys.argv[1]).write_text(json.dumps(doc, indent=1, ensure_ascii=False))


# ===========================================================================
# SVG preview (same coordinates)
# ===========================================================================
# Draw the same elements as a simple SVG image (for a quick preview without Excalidraw).
def svg():
    # Every left and right edge ...
    xs = [e["x"] for e in EL] + [e["x"] + e["width"] for e in EL]
    # ... and every top and bottom edge ...
    ys = [e["y"] for e in EL] + [e["y"] + e["height"] for e in EL]
    # ... give the picture's total size (plus a 40-pixel margin).
    W, H = max(xs) + 40, max(ys) + 40
    # The SVG opening tag, a white background, and an arrowhead shape ("marker") reused by every arrow.
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'font-family="Helvetica, Arial, sans-serif"><rect width="100%" height="100%" fill="#fff"/>'),
           ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker></defs>')]
    # Excalidraw font family number -> an SVG font.
    fam = {1: "Comic Sans MS, cursive", 2: "Helvetica, Arial, sans-serif", 3: "Menlo, monospace"}
    # First pass: shapes and arrows (drawn first, so text sits on top).
    for e in EL:
        # Dashed lines: 8 pixels drawn, 6 pixels gap.
        dash = ' stroke-dasharray="8 6"' if e["strokeStyle"] == "dashed" else ""
        # Opacity as a fraction 0..1.
        op = e["opacity"] / 100
        # Rectangle -> <rect> (rounded corners if roundness is set).
        if e["type"] == "rectangle":
            # Transparent background -> no fill.
            fill = "none" if e["backgroundColor"] == "transparent" else e["backgroundColor"]
            # Write the <rect> tag.
            out.append(f'<rect x="{e["x"]}" y="{e["y"]}" width="{e["width"]}" height="{e["height"]}" rx="{12 if e["roundness"] else 0}" '
                       f'fill="{fill}" fill-opacity="{op}" stroke="{e["strokeColor"]}" stroke-width="{e["strokeWidth"]}"{dash}/>')
        # Ellipse -> <ellipse>.
        elif e["type"] == "ellipse":
            # Write the <ellipse> tag.
            out.append(f'<ellipse cx="{e["x"] + e["width"]/2}" cy="{e["y"] + e["height"]/2}" rx="{e["width"]/2}" ry="{e["height"]/2}" '
                       f'fill="{e["backgroundColor"]}" stroke="{e["strokeColor"]}" stroke-width="2"/>')
        # Arrow -> <polyline> through all its points, with arrowheads.
        elif e["type"] == "arrow":
            # Absolute "x,y" pairs of every point.
            p = " ".join(f'{e["x"] + px},{e["y"] + py}' for px, py in e["points"])
            # Arrowhead at the start only for two-way arrows.
            start = ' marker-start="url(#ah)"' if e["startArrowhead"] else ""
            # Write the <polyline> tag.
            out.append(f'<polyline points="{p}" fill="none" stroke="{e["strokeColor"]}" stroke-width="2"{dash}{start} marker-end="url(#ah)"/>')
    # Second pass: text.
    for e in EL:
        # Skip anything that is not text.
        if e["type"] != "text":
            # Skip it.
            continue
        # SVG has no multi-line text, so each line becomes its own <text>.
        lines = e["text"].split("\n")
        # The font size.
        size = e["fontSize"]
        # Centred text is anchored at the middle; other text at its left edge.
        if e["textAlign"] == "center":
            # Anchor at the middle.
            anchor, x = "middle", e["x"] + e["width"] / 2
        # Otherwise:
        else:
            # Anchor at the left edge.
            anchor, x = "start", e["x"]
        # One <text> per line, 1.25 x the font size apart.
        for i, line in enumerate(lines):
            # The baseline y of line i.
            y = e["y"] + size * (1 + i * 1.25)
            # Write the <text> tag (escaped).
            out.append(f'<text x="{x}" y="{y}" font-size="{size}" font-family="{fam[e["fontFamily"]]}" '
                       f'fill="{e["strokeColor"]}" text-anchor="{anchor}">{escape(line)}</text>')
    # Close the SVG.
    out.append("</svg>")
    # All the pieces as one text.
    return "\n".join(out)


# Write the SVG to the second file name given on the command line.
Path(sys.argv[2]).write_text(svg())
# Report how many elements were drawn.
print(len(EL), "elements")
