# Custodian

**An autonomous data steward for DataHub.**

Every data estate has fires, rot, and blind spots. Custodian is an agent that
reads DataHub to understand how everything is connected, then investigates what
broke and sweeps out what's dead — always writing its findings back to the
graph, so the next person or agent inherits the knowledge.

Built for [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/)
— *Agents That Do Real Work*.

> **Runs on a free tier.** Custodian defaults to Google Gemini's free tier. You
> can clone this repo and run every command for **$0, with no credit card**.

---

## What it does

| Command | Situation | Reads | Writes back |
|---|---|---|---|
| `custodian investigate <table>` | Data broke | downstream + upstream lineage, owners, queries | `save_document` post-mortem, `add_tags` `incident-impacted` |
| `custodian sweep` | Data died | lineage, owners, descriptions, query history | `add_tags` `deprecation-candidate`, `add_structured_properties`, `save_document` report |
| `custodian approve` | A human decides | staged candidates | real `deprecation` aspect |
| `custodian doctor` | Before you demo | — | — |

### It is an agent, not a script

Custodian hands the DataHub MCP tools directly to the model and lets it decide
which lineage hops matter. A hard-coded traversal would work on one graph and
break on the next; the agent adapts to the shape of the estate it finds.

### The agent never retires anything

`sweep` only ever *stages*: it tags candidates, records its score and reasoning
as structured properties, and writes a report. Landing the retirement requires a
human running `custodian approve`, which emits the real `deprecation` aspect.

That split is enforced by the architecture rather than by prompt discipline. The
DataHub MCP server exposes no deprecation tool at all, so `approve` is the one
place Custodian reaches past MCP to the DataHub Python SDK. The agent
*cannot* deprecate an asset even if it decides it should.

### Deadness scoring works on DataHub Core

Usage-based ranking (`queryCountLast30DaysFeature`) is a DataHub Cloud feature,
so Custodian scores on signals that exist in the open-source product:

| Signal | Weight |
|---|---|
| No downstream lineage | 0.40 |
| No owner | 0.25 |
| No description | 0.15 |
| Stale last-operation time | 0.10 |
| No recent queries | 0.10 |

Downstream lineage dominates deliberately, and acts as a veto: an asset with a
live dependent is never a candidate, whatever its other signals say.

---

## Setup

### 1. Run DataHub locally

```bash
python3 -m pip install --upgrade acryl-datahub
datahub docker quickstart
```

The UI comes up at <http://localhost:9002>, GMS at <http://localhost:8080>.

> **DataHub must be 1.4.0 or newer.** Older servers do not register the MCP
> mutation tools *at all* — writes then silently no-op rather than failing.
> `custodian doctor` checks this for you.

Then load a realistic estate to work against:

```bash
datahub datapack load showcase-ecommerce
```

That's ~1,049 entities across Snowflake, Looker, PowerBI, Tableau, dbt, Spark,
Postgres and S3, with real cross-platform lineage — enough graph for the agent
to have to actually navigate something.

### 2. Get a Personal Access Token

DataHub UI → **Settings → Access Tokens** → generate. This is `DATAHUB_GMS_TOKEN`.

### 3. Get a free Gemini API key

<https://aistudio.google.com/apikey>

> Keep the key on a project with **billing disabled**. Enabling billing on a
> Gemini project deletes its free tier — every call becomes billable from the
> first token.

**Free-tier quota is granted per model, per day.** If you hit
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, that model is spent for
the day but others are not — switch `GEMINI_MODEL` rather than waiting:

```bash
GEMINI_MODEL=gemini-flash-lite-latest   # default
GEMINI_MODEL=gemini-flash-latest
```

Prefer the rolling `-latest` aliases over pinned names like
`gemini-2.0-flash`: quota is keyed to the resolved model, and pinned names on
some projects sit at a hard `limit: 0` while the alias still has budget.

### 4. Configure and install

```bash
cp .env.example .env      # then fill in the two keys
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Verify before you demo

```bash
custodian doctor
```

This checks config, starts the MCP server, and confirms the mutation tools
actually registered — the failure mode that otherwise looks like success.

---

## Usage

```bash
# Data broke — trace impact and cause, write a post-mortem
custodian investigate order_details \
  --context "Exec revenue dashboards flatlined this morning."

# Data died — stage dead assets for review
custodian sweep --limit 25

# Human lands the retirements
custodian approve
```

See [`examples/`](examples/) for real generated output.

## Trying it on the showcase estate

`demo/plant_incident.py` stages a realistic failure in `showcase-ecommerce`: an
ETL change starts writing `NULL` into `order_items.unit_price`, so everything
computing revenue from it silently reports zero and the dashboards flatline.

```bash
python demo/plant_incident.py    # stage it
python demo/plant_incident.py --clean
```

It plants **evidence, not the answer** — no tag says "this is the root cause".
The culprit carries the kind of note a real incident leaves behind, two hops
upstream of the table anyone would actually report. Finding it means walking
the lineage.

In this estate that walk is a real one: `analytics.order_details` has **34
upstream and 24 downstream** entities, and the blast radius crosses Snowflake,
dbt, Looker, PowerBI and Tableau.

To re-run the demo from clean:

```bash
python demo/reset.py     # removes Custodian's tags and deprecations only
```

`reset.py` reverts what *Custodian* wrote — it matches on Custodian's own actor
and note, so a deprecation set by a human is never silently undone.

---

## How it works

```
        ┌──────────────┐   MCP (stdio)   ┌─────────────────────┐
        │  Gemini      │ ◄─────────────► │ mcp-server-datahub  │
        │  free tier   │   tool calls    └──────────┬──────────┘
        └──────────────┘                            │ GraphQL/REST
              ▲                                     ▼
              │ prompt + tools                ┌───────────┐
    ┌─────────┴─────────┐                     │  DataHub  │
    │ investigate/sweep │                     └───────────┘
    └───────────────────┘                           ▲
                                                    │ deprecation aspect
                                          ┌─────────┴────────┐
                                          │ custodian approve│  (human-triggered,
                                          │  via DataHub SDK │   SDK not MCP)
                                          └──────────────────┘
```

`google-genai` accepts an MCP `ClientSession` directly as a tool and drives the
tool-use loop itself, so the DataHub tools reach the model unmodified.

**One gotcha worth documenting:** `google-genai` caps automatic function calling
at **10 remote calls** by default. A multi-hop lineage walk exceeds that, and the
SDK then stops and returns a truncated answer *with no error*. Custodian sets the
ceiling explicitly (`CUSTODIAN_MAX_TOOL_CALLS`, default 40) and warns you when a
run actually hits it, rather than presenting a half-finished investigation as
complete.

---

## Provider

Gemini is the default because it makes the project free to run. The provider
seam (`custodian/providers/base.py`) is a single Protocol — adding Claude or a
local model is a new file, not a rewrite.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
