# Prow Nightly Monitor

Automated monitoring of OpenShift Prow periodic CI jobs with failure
analysis, AI-powered investigation, and fix suggestions. Publishes a
daily dashboard to GitHub Pages with full run history.

**Dashboard:** https://aabughosh.github.io/prow-nightly-monitor/
**Run History:** https://aabughosh.github.io/prow-nightly-monitor/history.html

## What it does

For each failed nightly job:

1. **Fetches step-specific logs** — not just the top-level CI runner output, but the actual test step logs from GCS artifacts
2. **Parses Ginkgo test results** — extracts the `Summarizing N Failures` section, `[FAIL]` blocks, and test file references
3. **Detects matrix mismatches** — identifies which ports need to be added, removed, or investigated (missing EndpointSlice)
4. **Classifies the failure** — matrix mismatch, test failure, build error, or infrastructure issue
5. **Runs AI analysis** — sends the extracted failure context to Groq (Llama 3.3 70B) for root cause analysis. Falls back to local Ollama if rate limited
6. **Generates an investigation report** — with severity, root cause, failed test names, source file references, and a concrete suggested fix
7. **Publishes a dashboard** — to GitHub Pages with filters, trend charts, and expandable details for each failure

## AI Providers

The tool supports multiple AI providers with automatic fallback:

| Provider | Model | How it's used |
|----------|-------|---------------|
| **Groq** (primary) | Llama 3.3 70B | Free, fast (1s/analysis), 100K tokens/day |
| **Ollama** (fallback) | Qwen 2.5 1.5B | Runs locally in GitHub Actions, no limits |
| **OpenAI** | GPT-4o-mini | Paid, high quality |
| **Anthropic** | Claude Sonnet | Paid, best quality |
| **Gemini** | Gemini 2.0 Flash | Free tier has rate limits |
| **Hugging Face** | Various | Free tier |

Set `GROQ_API_KEY` for the best free experience. Get one at https://console.groq.com/keys (free, no credit card).

## Quick start

### 1. Fork or clone this repo

```bash
git clone https://github.com/aabughosh/prow-nightly-monitor.git
```

### 2. Configure variables

Go to **Settings → Variables → Actions**:

| Variable | Value | Required |
|----------|-------|----------|
| `JOB_FILTER` | Job name pattern (e.g. `network-flow-matrix`) | Yes |
| `MIN_VERSION` | Minimum OCP version (e.g. `4.21`) | No |
| `AI_PROVIDER` | `auto` (default), `groq`, `openai`, `claude`, `gemini` | No |

### 3. Configure secrets

Go to **Settings → Secrets → Actions**:

| Secret | Value | Required |
|--------|-------|----------|
| `GROQ_API_KEY` | Groq API key (free) | Recommended |
| `OPENAI_API_KEY` | OpenAI API key | Optional |
| `ANTHROPIC_API_KEY` | Anthropic API key | Optional |
| `GEMINI_API_KEY` | Google Gemini API key | Optional |
| `HF_API_KEY` | Hugging Face token | Optional |

Ollama runs automatically as fallback — no key needed.

### 4. Enable GitHub Pages

Go to **Settings → Pages** → Source: **GitHub Actions**

### 5. Run it

Go to **Actions → Prow Nightly Monitor → Run workflow**

Dashboard: `https://<username>.github.io/prow-nightly-monitor/`
Run history: `https://<username>.github.io/prow-nightly-monitor/history.html`

## Failure classification

| Category | Badge | Meaning |
|----------|-------|---------|
| MATRIX MISMATCH | 🟠 HIGH | Documented matrix doesn't match actual ports — shows exactly which ports to add/remove |
| TEST FAILURE | 🔴 MEDIUM | A specific Ginkgo/Go test failed — shows test name, file, and error message |
| BUILD ERROR | 🔴 HIGH | Code does not compile |
| INFRA | 🟡 LOW | Cluster setup, timeout, node issues — likely transient, suggest retry |
| UNKNOWN | ⚫ MEDIUM | Could not classify — check logs |

## Investigation report

For each failure, the dashboard shows:

- **Severity** badge (CRITICAL / HIGH / MEDIUM / LOW)
- **Suggested Fix** — always visible, with specific actions (e.g. which CSV lines to add/remove)
- **Full Investigation Details** (expandable):
  - Root cause
  - Failed tests with names and error messages
  - Source file references (e.g. `validation_test.go:161`)
  - Raw error output
- **Matrix Diff Details** (for matrix mismatches):
  - Ports used but not documented
  - Ports in matrix but not in use
  - Ports open but missing EndpointSlice
- **AI Analysis** (expandable) — LLM-powered root cause analysis
- **Failed Tests** (expandable) — JUnit test case details

## Run history

Each daily run is archived with its own permanent URL:

- `/` — latest dashboard
- `/runs/2026-05-18/` — that day's dashboard
- `/history.html` — list of all past runs

## Cursor skill

A Cursor skill is included at `skill/SKILL.md` for on-demand analysis using Cursor's built-in Claude. Just type "check the nightlies" in Cursor chat.

## Using it for other projects

Change `JOB_FILTER` to match any Prow periodic job:

| Team | JOB_FILTER | What it monitors |
|------|-----------|------------------|
| commatrix | `network-flow-matrix` | Communication matrix nightlies |
| PTP | `ptp-operator` | PTP operator nightlies |
| CNF | `cnf-features` | CNF feature tests |
| SR-IOV | `sriov` | SR-IOV nightlies |

## Run locally

```bash
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export GROQ_API_KEY="gsk_..."  # optional
pip install requests
python monitor.py
open public/index.html
```

## File structure

```
prow-nightly-monitor/
  .github/workflows/
    monitor.yml         # GitHub Action — daily schedule + deploys to Pages
  monitor.py            # Main monitor script
  template.html         # Dashboard HTML template
  skill/SKILL.md        # Cursor skill for on-demand analysis
  public/               # Generated output
    index.html          # Latest dashboard
    results.json        # Latest results data
    history.html        # Run history index
    history.json        # Trend data
    runs/<date>/        # Archived daily dashboards
```
