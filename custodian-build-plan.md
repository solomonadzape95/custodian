# Custodian — Build Plan

An autonomous data steward for DataHub. A custodian both **protects** what matters and **clears** what's dead — so this agent does both, plus takes **inventory** on demand. One engine underneath (walk the lineage graph, judge what you find, write the judgment back), three commands on top.

---

## The pitch (say this in the first 20 seconds of the video)

> Every company's data estate has fires, rot, and blind spots. Custodian is an agent that reads DataHub to understand how everything's connected, then investigates what broke, sweeps out what's dead, and inventories what's sensitive — always writing its findings back to the graph so the next person or agent inherits the knowledge.

That last clause is deliberate — it's almost word-for-word the hackathon's own framing about writing results back so the next person or agent inherits context.

---

## The three commands

| Command | What it does | Reads | Writes |
|---|---|---|---|
| `custodian investigate <table>` | Data broke — explain impact + likely cause, write a post-mortem | downstream lineage, upstream lineage, owners, queries | `save_document` (post-mortem), `add_tags` (`incident-impacted`) |
| `custodian sweep` | Data died — find dead assets, propose deprecation for human approval | usage stats, owners, downstream lineage | `propose_lifecycle_stage` → deprecated (governed proposal) |
| `custodian diligence <domain>` | Data blind spot — inventory ownership + PII + lineage for a set of assets | search, entities, owners, tags, lineage | `save_document` (inventory report), `add_structured_properties` (review stamp) |

Keep `investigate` as the star. `sweep` and `diligence` are the "and it also does this" beats. Don't split effort evenly — polish `investigate` until it shines, then add the other two.

---

## What you're actually building

Not a website. A **Python CLI agent** that talks to DataHub through the MCP server. The interface judges see is mostly the **real DataHub UI reacting live** to your agent (tags appearing, proposals queuing, documents attaching), shown in your demo video. Optionally a tiny read-only dashboard page, but that's a stretch goal, not core.

Stack:
- Python 3.10+
- The DataHub MCP server (self-hosted open-source version is free and works with DataHub Core — no Cloud account needed)
- An agent framework of your choice (LangChain / LangGraph / Google ADK all have native DataHub integration, or call the MCP tools directly). Simplest path: drive the MCP tools directly from a Python script; add a framework only if you want the agent to reason about tool order itself.
- `click` or `typer` for the CLI verbs

---

## The write tools you'll actually use (grounded in the real MCP server)

These exist in the DataHub MCP server's mutation tool set — you're not inventing them:

- `save_document` — save standalone docs (insights, decisions, post-mortems, reports) to DataHub's knowledge base
- `add_tags` / `remove_tags` — tag entities or individual columns
- `add_structured_properties` — typed custom fields (string, number, date, URN, rich text) on any entity or column
- `propose_lifecycle_stage` — submit a proposal to change an entity's lifecycle stage (e.g. → deprecated), pending human approval
- `accept_or_reject_proposals` — the human side of the loop

Read tools you'll lean on: `search`, `get_entities`, `get_lineage`, `get_lineage_paths_between`, `get_dataset_queries`, `list_schema_fields`.

> The `propose_*` path is a strategic asset: it lets you demo "agent suggests → human approves → change lands." A governance company loves that loop far more than an agent that mutates the catalog directly. Build `sweep` around it on purpose.

---

## Setup — get to a running demo environment

1. **Run DataHub locally.** Install the CLI and start the quickstart:
   ```
   python3 -m pip install --upgrade acryl-datahub
   datahub docker quickstart
   ```
   This gives you a local DataHub with a UI at `http://localhost:9002`.

2. **Load sample data.** DataHub ships with a built-in demo/sample metadata source, and the hackathon Resources tab provides sample datasets. Ingest those so your graph has a few hundred tables, dashboards, owners, and lineage arrows to work with.

3. **Connect the MCP server** (self-hosted):
   - Install `uv`
   - Point your agent at `mcp-server-datahub` with env vars `DATAHUB_GMS_URL` (e.g. `http://localhost:8080`) and `DATAHUB_GMS_TOKEN` (a personal access token from the UI)
   - Mutation tools need `TOOLS_IS_MUTATION_ENABLED=true` — set this or your writes won't work.

4. **Plant a broken table** (this is your demo gold — see next section).

---

## Planting an incident for the `investigate` demo

You need a believable failure on camera. Set one up in your sample graph:

- Pick a chain like `raw_orders → orders_clean → revenue_daily → Exec Revenue Overview`.
- The "incident": upstream, a column feeding `revenue_daily` (say `order_total`) gets renamed/nulled, so `revenue_daily` silently reports zero and the exec dashboard flatlines.
- You don't need real broken data — you need the *metadata* to reflect it: mark `orders_clean` as having a recent schema change / freshness anomaly, or simply narrate it. The agent's job is to trace the blast radius through lineage, not to inspect actual rows.

The demo beat: "revenue dashboard flatlined this morning — run `custodian investigate revenue_daily`."

---

## Command logic, step by step

### `custodian investigate <table>`

1. Resolve the table URN (`search` / `get_entities`).
2. **Downstream walk** (`get_lineage`, direction downstream, multi-hop): everything that depends on this table = the blast radius. Collect dashboards, models, downstream tables.
3. **Upstream walk** (`get_lineage`, direction upstream): trace back toward the origin. Use `get_lineage_paths_between` to get the exact path and any transformation/SQL context between the broken table and a suspected root cause.
4. Pull owners of every impacted asset (so the post-mortem can tag who needs to know) and recent queries (`get_dataset_queries`) to show what was affected.
5. **Write back:**
   - `save_document`: a formatted post-mortem (template below), attached under the incident/table.
   - `add_tags`: tag each impacted asset `incident-impacted` so they're findable.

### `custodian sweep`

1. `search` across the catalog for candidate dead assets.
2. For each, gather the death signals: zero/low recent queries (`get_dataset_queries` / usage stats), no owner (`get_entities`), nothing downstream in lineage (`get_lineage` downstream is empty → safe to retire), stale freshness.
3. Score "deadness." Above a threshold → **file a proposal**, don't delete:
   - `propose_lifecycle_stage` → `deprecated`, with a reason string.
4. Demo the human side: open DataHub's proposals view, `accept_or_reject_proposals` a few live.

### `custodian diligence <domain>`

1. `search` scoped to the domain/platform (e.g. everything in the `marketing` domain, or a specific data source).
2. For each asset: owner, PII/sensitivity tags, lineage in/out, description completeness.
3. Classify: which assets hold PII, which are unowned, where sensitive data flows across boundaries.
4. **Write back:**
   - `save_document`: an inventory / due-diligence report (ownership map + PII inventory + lineage summary + gaps).
   - `add_structured_properties`: stamp each reviewed asset with e.g. `diligence_reviewed: <date>` and `sensitivity: <level>` so the review state lives in the graph and the next audit inherits it.

---

## Post-mortem document template (what `investigate` writes)

```
# Incident Post-Mortem: <table> — <date>

## Summary
<one line: what broke, what it affected>

## Blast Radius (downstream)
- <dashboard/model/table> — owner: <name>
- ...

## Likely Root Cause (upstream)
Traced from <broken table> back to <root asset> via:
<lineage path with transformations>

## Impacted Owners to Notify
- <name> (<asset>)

## Recommended Actions
- <e.g. restore column, backfill, add freshness assertion>

_Generated by Custodian. Impacted assets tagged `incident-impacted`._
```

Ship a filled-in example of this in your `examples/` folder — judges reward seeing output quality without running anything.

---

## Repo structure

```
custodian/
├── LICENSE                  # Apache 2.0 — REQUIRED, must show in the About box
├── README.md                # what it is, setup, the three commands, a GIF
├── custodian/
│   ├── __init__.py
│   ├── cli.py               # click/typer: investigate, sweep, diligence
│   ├── datahub_client.py    # MCP connection + thin wrappers over the tools
│   ├── lineage.py           # upstream/downstream walk helpers
│   ├── investigate.py
│   ├── sweep.py
│   └── diligence.py
├── examples/                # OPTIONAL but high-value
│   ├── sample-postmortem.md
│   ├── sample-diligence-report.md
│   └── sample-sweep-proposals.md
├── demo/
│   └── setup_incident.py    # script that plants the broken table
└── requirements.txt
```

---

## Submission checklist (from the hackathon rules)

- [ ] Public repo, **Apache 2.0 license file**, visible in the repo's About section — they call this out specifically; it's an easy disqualifier to avoid.
- [ ] Project URL judges can test — the repo with clear setup instructions counts.
- [ ] Text description (features, tech, data used).
- [ ] Demo video **under 3 minutes**, public on YouTube/Vimeo, showing it actually working.
- [ ] `examples/` folder with sample outputs (optional, but most teams skip it — free differentiation).
- [ ] Opt into the feedback survey for the bonus prize ($50 × 10).

---

## 3-minute video script (shot by shot)

**0:00–0:20 — Hook.** "Data estates have fires, rot, and blind spots. Meet Custodian — one agent that handles all three, and writes everything back to DataHub." Show the DataHub graph.

**0:20–1:20 — investigate (the star).** "This morning the exec revenue dashboard flatlined." Run `custodian investigate revenue_daily`. Show terminal tracing downstream then upstream. Cut to DataHub: the new post-mortem document attached, impacted assets now tagged. Read the root-cause line aloud.

**1:20–2:05 — sweep.** "Custodian also clears dead weight — safely." Run `custodian sweep`. "Found 12 dead tables. It doesn't delete — it proposes." Cut to proposals view; approve two live. Emphasize: agent suggests, human controls.

**2:05–2:40 — diligence.** "And on demand, it inventories a domain." Run `custodian diligence marketing`. Show the generated report: ownership map, PII inventory, gaps. Note the review stamp written back to each asset.

**2:40–3:00 — Close.** "Investigate, sweep, diligence — one custodian, protecting and cleaning your data estate, and every finding lives in the graph for the next person or agent. Open source, Apache 2.0." Repo URL on screen.

---

## Honest priorities if time runs short

1. `investigate` working end-to-end with a real post-mortem written back — this alone is a complete, compelling submission.
2. The planted-incident demo setup — without a live before/after, the video is weaker.
3. `sweep` via the proposal loop — high impact, low code, strong governance story.
4. `examples/` folder — cheap, differentiating.
5. `diligence` — the "range" beat; add it if 1–4 are solid.
6. Optional dashboard page — only if everything else shines.

Finish beats novelty. One command done impressively plus evidence of range beats three done adequately.
