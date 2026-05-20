# Prow Nightly Monitor

Automated monitoring of OpenShift Prow periodic CI jobs with AI-powered
deep investigation using Cursor CLI agent. Analyzes failures, identifies
root causes, writes code fixes, and opens PRs automatically.

**Dashboard:** https://aabughosh.github.io/prow-nightly-monitor/
**Run History:** https://aabughosh.github.io/prow-nightly-monitor/history.html

## What it does

Every weekday at noon, the cron runs a full pipeline:

1. **Fetches Prow data** — all nightly jobs matching the filter, with step-specific logs from GCS
2. **Downloads relevant artifacts** — JUnit results, matrix diffs, ss dumps, gather-extra resources (pods, endpoints, services, nodes)
3. **Classifies failures** — matrix mismatch, test failure, build error, or infrastructure issue
4. **Runs Cursor CLI agent** on each failure with full tool access:
   - Reads the source code alongside CI evidence files
   - Searches the codebase (`grep`, `find`)
   - Fetches full step logs from Prow via `curl`
   - Writes code fixes directly to files
5. **Opens PRs** on the upstream repo for real error fixes (skips warnings/infra)
6. **Generates a dashboard** with formatted AI analysis, fix patches, and PR links

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────┐
│  Prow API   │────▸│  monitor.py  │────▸│ inject_claude.py │────▸│ Dashboard│
│ + GCS logs  │     │  fetch/class │     │  Cursor agent    │     │ + PRs    │
└─────────────┘     └──────────────┘     └─────────────────┘     └──────────┘
                           │                      │
                     results.json           ci-evidence/
                     (artifacts)        (prompt + artifacts)
```

**Cron entry** (`run-skill.sh`):
1. `monitor.py` → fetch + classify + generate `results.json` (no AI)
2. Clone target repo → `/tmp/ci-investigate`
3. `inject_claude.py` → run Cursor agent per failure → write AI summaries + open PRs
4. `monitor.py --render-only` → re-render HTML with AI data

## Cursor CLI Agent

Each failure gets a deep investigation via `cursor agent --trust --yolo --print`:

- **Evidence files** dumped to `ci-evidence/` (logs, junit, matrix diffs, ss dumps, port map)
- **Prompt** tailored to failure category (matrix mismatch, test, infra, build)
- **Full tool access** — the agent reads files, runs shell commands, searches code, writes fixes
- **Fix patches** captured via `git diff > ci-evidence/fix.patch`
- **PRs** opened on upstream via fork (push to fork → PR against upstream)

### PR Rules

| Condition | Action |
|-----------|--------|
| Real error (matrix mismatch, test failure, build) | Open PR |
| Warning or infra issue | Analyze only, no PR |
| LOW severity | Analyze only, no PR |
| Duplicate patch (same fix as another job) | Skip PR |

## Quick Start — Local Cron

### 1. Clone

```bash
git clone https://github.com/aabughosh/prow-nightly-monitor.git
```

### 2. Configure

Edit `run-skill.sh` or set environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `JOB_FILTER` | `network-flow-matrix` | Prow job name pattern to track |
| `MIN_VERSION` | `4.21` | Minimum OCP version |
| `TARGET_REPO` | `openshift-kni/commatrix` | Repo to clone for agent context |
| `UPSTREAM_REPO` | `openshift-kni/commatrix` | Where PRs are opened |
| `FORK_OWNER` | `aabughosh` | Your GitHub username (PRs pushed here) |
| `OPEN_PRS` | `true` | Set to `false` to disable PR creation |

### 3. Prerequisites

- **Cursor IDE** installed (with CLI at `/Applications/Cursor.app/...`)
- **Cursor CLI authenticated**: `cursor agent login`
- **GitHub CLI authenticated**: `gh auth login`
- **Python 3.9+** with `requests`: `pip3 install requests`

### 4. Register cron

```bash
crontab -e
# Add:
0 12 * * 1-5 /bin/bash ~/Documents/GitHub/prow-nightly-monitor/run-skill.sh
```

### 5. Run manually

```bash
./run-skill.sh
# Or just the monitor (no AI):
python3 monitor.py
open public/index.html
```

## Failure Classification

| Category | Badge | Meaning |
|----------|-------|---------|
| MATRIX MISMATCH | 🟠 HIGH | Documented matrix doesn't match actual ports |
| TEST FAILURE | 🔴 MEDIUM | A specific test failed — shows test name, file, error |
| BUILD ERROR | 🔴 HIGH | Code does not compile |
| INFRA | 🟡 LOW | Cluster setup, timeout, node issues — likely transient |
| UNKNOWN | ⚫ MEDIUM | Could not classify |

## Dashboard Features

- **Formatted AI Analysis** — markdown rendered as HTML (tables, headers, code blocks)
- **Fix Patches** — expandable diff view of agent-generated fixes
- **PR Links** — direct links to opened PRs (green badge)
- **Matrix Diff** — ports to add/remove with ss socket state
- **JUnit Failures** — test case details from JUnit XML
- **Trend Chart** — pass/fail history across runs
- **Run History** — archived daily dashboards at `/runs/<date>/`

## Using it for other projects

Change `JOB_FILTER` and `TARGET_REPO` to monitor any Prow job:

| Team | JOB_FILTER | TARGET_REPO |
|------|-----------|-------------|
| commatrix | `network-flow-matrix` | `openshift-kni/commatrix` |
| PTP | `ptp-operator` | `openshift/ptp-operator` |
| CNF | `cnf-features` | `openshift-kni/cnf-features-deploy` |
| SR-IOV | `sriov` | `k8snetworkplumbingwg/sriov-network-operator` |

## Cursor Skill

A Cursor skill is included at `skill/SKILL.md` for on-demand interactive analysis.

## File Structure

```
prow-nightly-monitor/
  run-skill.sh            # Cron entry point — orchestrates the full pipeline
  monitor.py              # Fetches Prow data, classifies failures, generates dashboard
  inject_claude.py         # Runs Cursor CLI agent per failure, opens PRs
  skill/SKILL.md          # Cursor skill for on-demand analysis
  public/                 # Generated output
    index.html            # Latest dashboard
    results.json          # Latest results (with AI summaries)
    history.html          # Run history index
    history.json          # Trend data
    runs/<date>/          # Archived daily dashboards
```
