# Prow Nightly Monitor

Automated monitoring of OpenShift Prow periodic CI jobs with AI-powered
deep investigation using Cursor CLI agent. Analyzes failures, identifies
root causes with verbatim log evidence, fingerprints recurring issues,
and publishes dashboards daily.

**PTP Dashboard:** https://aabughosh.github.io/prow-nightly-monitor/projects/ptp-operator/
**Commatrix Dashboard:** https://aabughosh.github.io/prow-nightly-monitor/projects/commatrix/

## What it does

**Daily at midnight (including weekends):**
1. Fetches Prow data — all nightly jobs matching each project's filter
2. Downloads artifacts — JUnit results, test_results.json, build logs, matrix diffs
3. Classifies failures — test failure, infra issue, build error, matrix mismatch
4. Extracts individual failed test names from test_results.json / JUnit XML
5. Fingerprints each issue — groups same failures across versions, skips recurring known issues
6. **AI deep analysis** (Cursor CLI) — one call per failed job, covering all its tests
7. Generates dashboards with Summary column (TL;DR + issue class) and pushes to GitHub Pages
8. Sends Slack notification with summary

## Key Features

- **Per-test AI analysis** — each failed test gets individual root cause, evidence, and fix
- **Evidence-based** — AI must quote verbatim log lines as proof (no guessing)
- **Fingerprinting** — recurring issues are detected and skipped (no redundant analysis)
- **Multi-project** — supports commatrix, PTP, CNF, SR-IOV (configured in `projects.json`)
- **Version-aware** — different OCP versions get separate analyses when failures differ
- **Smart grouping** — tests from the same job share one AI call (no duplicates)
- **Known Issues page** — tracks all unique fingerprinted issues with history
- **Summary column** — at-a-glance TL;DR + issue class + severity + failure count
- **CI framework detection** — checks `finished.json` + build-log to distinguish project failures from CI framework failures (MonitorTest, operator-state-analyzer)

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│  Prow API   │────▸│  monitor.py  │────▸│ inject_claude.py │────▸│  Dashboard   │
│ + GCS logs  │     │  fetch/class │     │  Cursor agent    │     │  + GitHub    │
└─────────────┘     └──────────────┘     └─────────────────┘     │    Pages     │
                           │                      │               └──────────────┘
                     results.json           ci-evidence/
                     (artifacts)        (prompt + evidence)
                                               │
                                      fingerprints.json
                                      (recurring issue DB)
```

**Schedule** (`prow-wrapper.sh` via macOS `launchd`, daily at midnight):
1. For each project in `projects.json`:
   - `monitor.py` → fetch + classify + extract tests + generate dashboard
   - Clone target repo + related repos for AI context
   - `inject_claude.py` → fingerprint issues → AI analysis per job → update results
   - `monitor.py --render-only` → re-render HTML with AI data
2. Copy output to `docs/` for GitHub Pages
3. Git push to `main`
4. Send Slack notification

## AI Analysis

Each failed job gets a deep investigation via `cursor agent --trust --yolo --print`:

1. **Determine failure source** — reads `ci-operator-build-log.txt` and `finished.json` first
2. **Gathers evidence** — downloads relevant artifacts, filters to essential files
3. **Searches source code** — clones target + related repos, greps for test functions
4. **Analyzes each test** — provides Evidence (verbatim log quote), Root Cause, Breaking PR, Fix
5. **Outputs structured format** — TL;DR, per-test sections, issue class, severity

### Evidence Rule

The AI **must present verbatim log quotes** as proof for every conclusion. It cannot guess or assume root causes. For example, if a DaemonSet wasn't created, it must find the actual error (e.g., image pull failure from registry.ci) rather than assuming hardware issues.

### Fingerprinting

Issues are fingerprinted by `hash(test_name + error_msg + category)`. This allows:
- Skipping re-analysis of recurring known issues
- Tracking issue history (first seen, last seen, occurrence count)
- Grouping same failures across OCP versions into one analysis
- A dedicated "Known Issues" dashboard page

## Projects

Configured in `projects.json`:

| Project | Job Filter | Min Version | Related Repos |
|---------|-----------|-------------|---------------|
| commatrix | `network-flow-matrix` | 4.21 | — |
| ptp-operator | `e2e-telco5g-ptp` | 4.17 | linuxptp-daemon, cloud-event-proxy |
| cnf-features-deploy | `cnf-features` | 4.17 | — |
| sriov-network-operator | `sriov` | 4.17 | — |

## Quick Start

### 1. Clone

```bash
git clone https://github.com/aabughosh/prow-nightly-monitor.git
cd prow-nightly-monitor
```

### 2. Prerequisites

- **Cursor CLI** installed and authenticated (`cursor agent login`)
- **GitHub CLI** authenticated (`gh auth login`)
- **Python 3.9+** with `requests`: `pip3 install requests`

### 3. Run manually

```bash
./run-skill.sh
# Or just fetch data (no AI):
python3 monitor.py
```

### 4. Set up daily cron (macOS launchd)

The `prow-wrapper.sh` script is the entry point for launchd:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.prow-nightly-monitor.plist
```

To ensure the Mac wakes for the midnight run:
```bash
sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00
```

## File Structure

```
prow-nightly-monitor/
  prow-wrapper.sh         # launchd entry point
  run-skill.sh            # Orchestrates the full pipeline per project
  monitor.py              # Fetches Prow data, classifies, generates dashboard
  inject_claude.py        # AI analysis — fingerprinting, Cursor agent, results
  fingerprint.py          # Issue fingerprinting and recurring detection
  projects.json           # Multi-project configuration
  template.html           # Dashboard HTML template
  skill/SKILL.md          # Cursor skill for on-demand manual analysis
  public/                 # Generated output
    projects/<name>/      # Per-project dashboards
      index.html          # Latest dashboard
      results.json        # Latest results (with AI summaries)
      issues.html         # Known Issues page
      history.html        # Run history
      runs/<date>/        # Archived daily dashboards
    fingerprints.json     # Recurring issue database
    history.json          # Trend data
  docs/                   # GitHub Pages source (copy of public/)
```

## Dashboard Columns

| Column | Shows |
|--------|-------|
| Status | pass/fail/error |
| Ver | OCP version |
| Job | Job name (linked to Prow) |
| Dur | Duration |
| Summary | Issue class badge + severity + AI TL;DR + failure count |
| Analysis | Per-test details + expandable full AI analysis |
| Started | Timestamp |

## Cost

- **Daily runs:** ~$0.50-2.00 per day (Cursor CLI usage for AI analysis)
- Recurring issues are fingerprinted and skipped — cost decreases over time as the DB grows
