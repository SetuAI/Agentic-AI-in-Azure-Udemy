"""
Builds the end-to-end architecture of the Annual Report RAG pipeline as an
Excalidraw file, plus an SVG preview drawn from the same elements (to check the
layout before opening it in Excalidraw).

    python3 docs/architecture/build_architecture.py docs/architecture/architecture.excalidraw docs/architecture/architecture-preview.svg
"""
# HOW TO RUN (from the project folder) - regenerates the diagram files:
#   python docs/architecture/build_architecture.py docs/architecture/architecture.excalidraw docs/architecture/architecture-preview.svg
# Argument 1 = the Excalidraw file to write; argument 2 = the SVG preview to write.
# Open the .excalidraw file at https://excalidraw.com (☰ -> Open) to view or edit it.
# No packages needed: standard library only.
#
# The drawing helpers (box, frame, arrow, text, svg) are the same as in
# Azure-Graph-RAG-on-DBs/docs/architecture/build_architecture.py, so both
# projects' diagrams look alike. Only the palette and the LAYOUT section differ.
#
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
    "search":  ("#0c8599", "#c3fae8"),   # Azure AI Search
    "guard":   ("#e8590c", "#ffe8cc"),   # guardrail checks
    "python":  ("#495057", "#f1f3f5"),   # plain Python
    "human":   ("#2f9e44", "#d3f9d8"),   # human step
    "azure":   ("#1864ab", "#e7f5ff"),   # Azure service frames
    "blob":    ("#1971c2", "#d0ebff"),   # Blob Storage
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
text(40, 24, "Annual Report RAG on Azure — end-to-end architecture", size=36, family=1)
# One-line description under the title.
text(42, 76, "Ask how something changed between FY2025 and FY2026 of a company's annual report. Each year is searched "
             "separately, every figure is checked against that year's extracts, and unverified answers pause for a person.",
     size=16, color=MUTED)

# ---------------------------------------------------------------------------
# ① BUILD TIME (left)
# ---------------------------------------------------------------------------
# Position (x, y) and size (width, height) of the left "build time" frame.
AX, AY, AW, AH = 40, 130, 640, 1060
# The frame with its title and subtitle.
frame(AX, AY, AW, AH, "① BUILD TIME  ·  run once, then after changes", kind="python",
      sub="Your laptop  ·  python …")

# Section heading: creating the Azure resources.
text(AX + 40, AY + 80, "Set-up  (azure_setup/)", size=18, color=C["azure"][0], family=1)
# provision.sh creates everything in one resource group.
pv = box(AX + 40, AY + 115, 250, 62, "provision.sh", kind="azure", size=15, family=3)
# What it creates.
text(AX + 312, AY + 115, "Azure OpenAI (3 deployments),\nAI Search Basic + semantic ranker,\nBlob Storage  ·  teardown.sh deletes all", size=14, color=MUTED)

# Section heading: the ingestion pipeline.
text(AX + 40, AY + 220, "Ingestion  (ingestion/ · run_ingestion.py)", size=18, color=C["search"][0], family=1)
# The four ingestion scripts, top to bottom, with an explanation to the right of each.
i0 = box(AX + 40, AY + 255, 250, 62, "upload_pdfs.py", kind="blob", size=15, family=3)
# PDF -> markdown.
i1 = box(AX + 40, AY + 347, 250, 62, "load_pdfs.py", kind="python", size=15, family=3)
# markdown -> chunks.
i2 = box(AX + 40, AY + 439, 250, 62, "chunk.py", kind="python", size=15, family=3)
# chunks -> index.
i3 = box(AX + 40, AY + 531, 250, 62, "build_index.py", kind="search", size=15, family=3)
# What each one does.
text(AX + 312, AY + 255, "data/raw/*.pdf → Blob container\nannual-reports  (4 reports)", size=14, color=MUTED)
# (load_pdfs)
text(AX + 312, AY + 347, "PDF → markdown with PyMuPDF4LLM\n(free, local) · cached in data/processed/", size=14, color=MUTED)
# (chunk)
text(AX + 312, AY + 439, "~1,200 chars, 200 overlap, cut at\nheadings · labelled company + year", size=14, color=MUTED)
# (build_index)
text(AX + 312, AY + 531, "recreate index · embed 512 dims\n→ ~5,150 chunks uploaded", size=14, color=MUTED)
# Arrows show the order.
arrow(i0, i1, color=C["search"][0])
# (load -> chunk)
arrow(i1, i2, color=C["search"][0])
# (chunk -> index)
arrow(i2, i3, color=C["search"][0])

# Section heading: hosting.
text(AX + 40, AY + 650, "Hosting  (hosting.md)", size=18, color=C["azure"][0], family=1)
# deploy-app.sh sends the code to App Service.
dp = box(AX + 40, AY + 685, 250, 62, "deploy-app.sh", kind="azure", size=15, family=3)
# What it does.
text(AX + 40, AY + 755, "zip the code (never .env, data, tests) → az webapp deploy → wait for /health", size=14, color=MUTED)

# A box summarising config, clients and tests.
box(AX + 40, AY + 800, 570, 110,
    "config.py   every setting · secrets come from .env\n"
    "azure_clients.py   one client per Azure service\n\n"
    "tests/   offline: no Azure account, no keys, no cost",
    kind="python", size=14)
# The terminal alternative to the web UI.
box(AX + 40, AY + 935, 570, 80,
    "run_query.py   the same graph from the terminal;\nthe reviewer answers a / r at the keyboard",
    kind="human", size=14)

# ---------------------------------------------------------------------------
# ② RUN TIME (right)
# ---------------------------------------------------------------------------
# Position and size of the right "run time" frame.
BX, BY, BW, BH = 730, 130, 1530, 1060
# The frame with its title and subtitle.
frame(BX, BY, BW, BH, "② RUN TIME  ·  every question", kind="azure", style="dashed",
      sub="Azure subscription  ·  resource group rg-annual-report-rag")

SX, SY, SW_, SH = BX + 270, BY + 80, 760, 960           # App Service frame: x 1000..1760
# The user, drawn as an ellipse.
user = box(BX + 40, BY + 62, 200, 62, "👤 User · browser", kind="python", size=16, shape="ellipse")
# Entra ID sign-in sits between the user and the app.
entra = box(BX + 40, SY + 70, 200, 110, "Microsoft Entra ID\nsign-in (Easy Auth)\nadds X-MS-CLIENT-\nPRINCIPAL headers", kind="entra", size=14)
# User -> Entra ID sign-in, labelled HTTPS.
arrow(user, entra, label="HTTPS", color=C["entra"][0], label_at=(BX + 175, (BY + 124 + SY + 70) / 2))

# The App Service frame, inside the run-time frame.
appsvc = frame(SX, SY, SW_, SH, "Azure App Service  ·  Linux · Python 3.12 · B1", kind="azure", style="solid",
      sub="startup.sh → python -u UI/server.py  ·  1 instance  ·  health check /health")
# The web page and the server, side by side inside App Service.
ui = box(SX + 30, SY + 85, 330, 78, "Web UI  (UI/index.html)\nanswer · review · rerank chart · Sign out", kind="python", size=14)
# The server box.
api = box(SX + 400, SY + 85, 330, 78, "UI/server.py\n/api/query · /api/review · /api/me · /health", kind="python", size=14)
# A two-way arrow: the page calls the server and gets JSON back.
arrow(ui, api, color=INK, both=True)
# The y position shared by the horizontal arrows at this level.
uy = SY + 124
# Entra ID -> the web page.
arrow(entra, ui, color=C["entra"][0], start=(BX + 240, uy), end=(SX + 30, uy))

GX, GY, GW, GH = SX + 30, SY + 195, 700, 740                # graph frame: x 1030..1730
frame(GX, GY, GW, GH, "LangGraph pipeline  ·  7 steps  (graph/)", kind="python", style="solid", size=18)
# The five steps in a straight line: (name, short description, colour kind).
steps = [
    ("1 · input_guardrail", "safety (personal data, advice) + on topic?", "guard"),
    ("2 · retrieve", "hybrid search + semantic ranker, ONCE PER YEAR", "search"),
    ("3 · rerank", "keep the 5 best by semantic score, per year", "python"),
    ("4 · generate", "gpt-4o: FY2025 said · FY2026 said · what changed", "openai"),
    ("5 · output_guardrail", "format, then grounding: figures in the right year?", "guard"),
]
# The step boxes, in order.
step_el = []
# One box per step, 84 pixels apart vertically.
for i, (name, desc, kind) in enumerate(steps):
    # This step's y position.
    y = GY + 56 + i * 84
    # The step box, coloured by what it uses.
    step_el.append(box(GX + 24, y, 652, 56, f"{name}  —  {desc}", kind=kind, size=14, family=3))
# An arrow from each step to the next.
for a, b in pairwise(step_el):
    # (step i -> step i+1)
    arrow(a, b, color=MUTED)
# cy(i) = the centre y of step i.
cy = lambda i: step_el[i]["y"] + 28
SL, SR = GX + 24, GX + 676                                  # step box left / right edges

# The fork after step 5: grounded answers finish, the rest pause for a person.
FY = GY + 56 + 5 * 84 + 30
# Grounded -> finalise.
fin = box(GX + 24, FY, 310, 84, "6 · finalise\nanswer shown to the user", kind="python", size=14, family=3)
# Needs review -> the graph pauses before human_review.
hum = box(GX + 366, FY, 310, 84, "⏸ 7 · human_review\ngraph PAUSES (interrupt_before)\napprove · correct · reject", kind="human", size=14, family=3)
# The two branches, labelled.
arrow(step_el[4], fin, label="grounded", color=C["python"][0],
      start=(GX + 179, step_el[4]["y"] + 56), end=(GX + 179, FY), label_at=(GX + 179, FY - 15))
# (needs review)
arrow(step_el[4], hum, label="needs review", color=C["human"][0],
      start=(GX + 521, step_el[4]["y"] + 56), end=(GX + 521, FY), label_at=(GX + 521, FY - 15))
# Every run ends here.
end = box(GX + 250, FY + 108, 200, 46, "END", kind="python", size=15, shape="ellipse")
# Both branches finish at END.
arrow(fin, end, color=MUTED, start=(GX + 179, FY + 84), pts=[(GX + 179, FY + 131)], end=(GX + 250, FY + 131))
# (human_review -> END)
arrow(hum, end, color=MUTED, start=(GX + 521, FY + 84), pts=[(GX + 521, FY + 131)], end=(GX + 450, FY + 131))
# Blocked questions: a dashed line down the left side, from step 1 straight to END.
arrow(step_el[0], end, label="blocked", color=C["guard"][0], style="dashed",
      start=(SL, cy(0)), pts=[(GX + 10, cy(0)), (GX + 10, FY + 170), (GX + 350, FY + 170)],
      end=(GX + 350, FY + 154), label_at=(GX + 120, FY + 170))
# Notes explaining the refusal path and where paused runs are kept.
text(GX + 24, GY + GH - 44, "⤷  blocked at step 1 → polite refusal; nothing else runs", size=13, color=C["guard"][0])
# (the pause)
text(GX + 24, GY + GH - 24, "⏸  paused runs live in MemorySaver (server memory) until the reviewer answers in the UI",
     size=13, color=C["human"][0])

# Right-hand column: Application Insights, Azure OpenAI, legend.
RX = 1820
# Application Insights box.
ai = box(RX, SY + 70, 400, 110, "Application Insights\nOpenTelemetry: a span per request,\nper graph step and per Azure OpenAI call", kind="insight", size=14)
# Azure OpenAI box, level with the middle steps.
oai = box(RX, cy(1) - 20, 400, 200,
          "Azure OpenAI  (3 deployments)\n\ngpt-4o: write the answer (4)\ngpt-4o-mini: claims check (5)\n"
          "text-embedding-3-small, 512 dims:\nquestion vector (2) · topic check (1)", kind="openai", size=14)

# Server -> Application Insights.
arrow(api, ai, label="traces", color=C["insight"][0], style="dashed",
      start=(SX + 730, uy), pts=[(1790, uy)], end=(RX, uy), label_at=(1790, uy - 18))
# Steps 1, 2, 4, 5 -> Azure OpenAI.
arrow(step_el[3], oai, label="steps 1 · 2 · 4 · 5", color=C["openai"][0],
      start=(SR, cy(3)), pts=[(1792, cy(3)), (1792, oai["y"] + 150)], end=(RX, oai["y"] + 150),
      label_at=(1792, cy(3) + 22))

# The colour legend, under the Azure OpenAI box.
LX, LY = RX, oai["y"] + 250
# Legend heading.
text(LX, LY, "Step colour = what it uses", size=16, family=1)
# (label, colour kind) for each legend entry.
legend = [("Azure OpenAI", "openai"), ("Azure AI Search", "search"), ("Guardrail check", "guard"),
          ("Python only", "python"), ("Human review", "human"), ("Blob Storage", "blob")]
# Two columns of small coloured squares with labels.
for i, (lbl, kind) in enumerate(legend):
    # Column 0 or 1, and the row.
    col, row = i % 2, i // 2
    # A small coloured square ...
    box(LX + col * 200, LY + 34 + row * 38, 26, 26, "", kind=kind, sw=2)
    # ... and its label.
    text(LX + 36 + col * 200, LY + 38 + row * 38, lbl, size=14)

# ---------------------------------------------------------------------------
# ③ DATA (bottom)
# ---------------------------------------------------------------------------
# Position and size of the bottom "data" frame.
DX, DY, DW, DH = 40, 1240, 2220, 380
# The frame.
frame(DX, DY, DW, DH, "③ Data in Azure  ·  resource group rg-annual-report-rag  ·  eastus2",
      kind="azure", style="solid",
      sub="Keys and endpoints live in .env (laptop) or the web app's settings (Azure) — never in the code or the zip")

# Blob Storage frame, with the four reports.
MX, MY, MW, MH = DX + 30, DY + 90, 560, 260
# The Blob frame.
blob = frame(MX, MY, MW, MH, "Blob Storage  ·  container annual-reports", kind="blob", style="solid", size=17)
# The four PDFs.
pdfs = box(MX + 25, MY + 60, MW - 50, 170,
           "infosys_fy2025.pdf    infosys_fy2026.pdf\neternal_fy2025.pdf    eternal_fy2026.pdf\n\n"
           "private (no anonymous access)\nload_pdfs.py downloads any PDF\nmissing from data/raw/",
           kind="blob", size=13, family=3)

# AI Search frame, to the right, with what the index holds.
KX, KY, KW, KH = MX + MW + 40, MY, DW - MW - 100, 260
# The AI Search frame.
srch = frame(KX, KY, KW, KH, "Azure AI Search  ·  Basic  ·  index annual-reports  ·  ~5,150 chunks",
             kind="search", style="solid", size=17)
# Width of each of its 4 boxes.
kw = (KW - 50 - 3 * 14) / 4
# kx(i) = x position of box i.
kx = lambda i: KX + 25 + i * (kw + 14)
# The fields.
k1 = box(kx(0), KY + 60, kw, 170, "fields\n\nid · text (keyword)\ncompany · fiscal_year\n(filters)\nyear_label · position", kind="python", size=13)
# The vectors.
k2 = box(kx(1), KY + 60, kw, 170, "embedding\n\n512-dim vector\nHNSW\ntext-embedding-3-small", kind="openai", size=13)
# The hybrid query.
k3 = box(kx(2), KY + 60, kw, 170, "hybrid query\n\nkeyword + vector, merged\nfilter: company eq … and\nfiscal_year eq …\n20 candidates per year", kind="search", size=13)
# The semantic ranker.
k4 = box(kx(3), KY + 60, kw, 170, "semantic ranker\n\nannual-reports-semantic\nreads the text,\nscores 0–4", kind="search", size=13)

# ---------------------------------------------------------------------------
# Arrows between the layers
# ---------------------------------------------------------------------------
CH = 1215                                                   # channel between the top frames and the data frame
# Build: upload the PDFs (down the left margin into Blob).
arrow(i0, blob, label="upload", color=C["blob"][0],
      start=(i0["x"], cy_i0 := i0["y"] + 31), pts=[(AX - 14, cy_i0), (AX - 14, MY + 130)],
      end=(MX, MY + 130), label_at=(AX - 14, CH))
# Build: chunks + vectors into the index.
arrow(i3, srch, label="chunks + vectors", color=C["search"][0],
      start=(i3["x"] + 250, i3["y"] + 52),
      pts=[(AX + AW - 20, i3["y"] + 52), (AX + AW - 20, CH + 12), (kx(0) + kw / 2, CH + 12)],
      end=(kx(0) + kw / 2, KY), label_at=((AX + AW - 20 + kx(0) + kw / 2) / 2, CH + 12))
# Hosting: deploy-app.sh -> App Service.
arrow(dp, appsvc, label="zip deploy", color=C["azure"][0], style="dashed",
      start=(dp["x"] + 250, dp["y"] + 31), end=(SX, dp["y"] + 31), label_at=(855, dp["y"] + 31))
# Run: step 2 -> the hybrid query + semantic ranker (right lane, then down).
lane = 1778
# (the arrow)
arrow(step_el[1], k3, label="one query per year", color=C["search"][0],
      start=(SR, cy(1)), pts=[(lane, cy(1)), (lane, CH - 5), (kx(2) + kw / 2, CH - 5)],
      end=(kx(2) + kw / 2, KY), label_at=(kx(2) + kw / 2 + 40, CH - 25))
# API -> step 1 (straight down).
arrow(api, step_el[0], color=INK, start=(SX + 565, SY + 163), end=(SX + 565, step_el[0]["y"]))

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
