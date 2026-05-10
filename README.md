# Prow Nightly Monitor

Automated monitoring of OpenShift Prow periodic CI jobs with failure
classification and AI-powered analysis. Generates an HTML dashboard
published to GitHub Pages.

## What it does

1. **Fetches** periodic job results from Prow for your configured job pattern
2. **Filters** by OCP version (e.g. 4.21+)
3. **Classifies** failures automatically:
   - **INFRA** — cluster install timeout, node not ready, quota exceeded
   - **TEST FAILURE** — a real test assertion failed
   - **BUILD ERROR** — code does not compile
   - **MATRIX MISMATCH** — communication matrix ports changed (commatrix specific)
   - **UNKNOWN** — needs investigation
4. **AI analysis** (optional) — sends failure logs to an LLM for a detailed
   root cause summary and recommended action
5. **Publishes** an HTML dashboard to GitHub Pages with:
   - Pass/fail statistics
   - Filter by version and status
   - Failure classification badges
   - AI analysis expandable for each failure
   - Links to Prow/Spyglass logs

## Dashboard

The dashboard shows:

```
┌──────────────────────────────────────────────────┐
│  Total: 24  │  Passed: 18  │  Failed: 5  │ ⏳: 1 │
├──────────────────────────────────────────────────┤
│ Filter: [All] [4.21] [4.22] [4.23] [Failed]     │
├──────┬────┬──────────┬─────┬─────────────┬───────┤
│Status│ Ver│ Job      │ Dur │ Analysis    │Started│
├──────┼────┼──────────┼─────┼─────────────┼───────┤
│ ✅   │4.22│ aws-ovn..│ 85m │             │ 05-09 │
│ ❌   │4.21│ upgrade..│ 45m │ INFRA:      │ 05-09 │
│      │    │          │     │ cluster     │       │
│      │    │          │     │ timeout     │       │
│      │    │          │     │ ▸ AI Analysis│       │
│ ✅   │4.23│ metal-..│120m │             │ 05-09 │
└──────┴────┴──────────┴─────┴─────────────┴───────┘
```

## Quick start

### 1. Fork or clone this repo

```bash
git clone https://github.com/aabughosh/prow-nightly-monitor.git
```

### 2. Configure variables

Go to **Settings → Variables → Actions** on your GitHub repo:

| Variable | Value | Required |
|----------|-------|----------|
| `JOB_FILTER` | Job name pattern (e.g. `network-flow-matrix`) | Yes |
| `MIN_VERSION` | Minimum OCP version (e.g. `4.21`) | No |
| `AI_MODEL` | LLM model name (default: `gpt-4o-mini`) | No |

### 3. Configure secrets (optional, for AI analysis)

Go to **Settings → Secrets → Actions**:

| Secret | Value | Required |
|--------|-------|----------|
| `OPENAI_API_KEY` | Your OpenAI API key | No (AI analysis disabled without it) |

### 4. Enable GitHub Pages

Go to **Settings → Pages** → Source: **GitHub Actions**

### 5. Run it

Go to **Actions → Prow Nightly Monitor → Run workflow**

The dashboard will be published at `https://<username>.github.io/prow-nightly-monitor/`

## Run locally

```bash
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
# Optional: export OPENAI_API_KEY="sk-..."

pip install requests
python monitor.py

# Open public/index.html in your browser
```

## Using it for other projects

Change `JOB_FILTER` to match any Prow job pattern:

| Team | JOB_FILTER | What it monitors |
|------|-----------|------------------|
| commatrix | `network-flow-matrix` | Communication matrix nightlies |
| PTP | `ptp-operator` | PTP operator nightlies |
| CNF | `cnf-features` | CNF feature tests |
| SR-IOV | `sriov` | SR-IOV nightlies |

Any Prow periodic job can be monitored — just change the filter.

## Failure classification

| Category | Badge | Meaning |
|----------|-------|---------|
| INFRA | 🟡 | Cluster setup, timeout, node issues — not your bug |
| TEST FAILURE | 🔴 | A real test failed — needs investigation |
| BUILD ERROR | 🔴 | Code does not compile |
| MATRIX MISMATCH | 🟠 | Expected vs actual matrix differs (commatrix) |
| UNKNOWN | ⚫ | Could not classify — check logs |

## AI analysis

When `OPENAI_API_KEY` is set, the monitor sends the last 4000 characters of
each failure log to the configured LLM and asks:

1. What is the root cause?
2. Is this infra or a real failure?
3. What is the recommended action?

The AI summary appears as an expandable section under each failed job.

## File structure

```
prow-nightly-monitor/
  .github/workflows/
    monitor.yml       # GitHub Action — runs on schedule + deploys to Pages
  monitor.py          # The monitor script
  README.md           # This file
  public/             # Generated output (HTML dashboard + JSON results)
```
