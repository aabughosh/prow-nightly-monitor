# Prow Nightly Monitor

Automated monitoring of OpenShift Prow periodic CI jobs with AI-powered
deep investigation using Cursor CLI agent. Analyzes failures, identifies
root causes, writes code fixes, and opens PRs automatically.

**Dashboard:** https://aabughosh.github.io/prow-nightly-monitor/
**AI Dashboard:** https://aabughosh.github.io/prow-nightly-monitor/cursor/
**Run History:** https://aabughosh.github.io/prow-nightly-monitor/history.html

## What it does

**Daily (Mon-Fri at noon):**
1. Fetches Prow data — all nightly jobs matching the filter, with step-specific logs from GCS
2. Downloads relevant artifacts — JUnit results, matrix diffs, ss dumps, gather-extra resources
3. Classifies failures — matrix mismatch, test failure, build error, or infrastructure issue
4. Generates a dashboard and pushes to GitHub Pages
5. Sends a Slack summary with failure details

**Weekly (Monday only) — AI deep analysis:**
6. Merges all failures from the past 7 days (Mon-Sun)
7. Clones the target repo for code context
8. Runs Cursor CLI agent on each failure — the agent reads the repo's README and docs first, then investigates
9. Opens PRs on the upstream repo for real error fixes (skips duplicates, warnings, infra)
10. Regenerates the dashboard with AI analysis included

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

**Schedule** (`run-skill.sh` via macOS `launchd`):
1. `monitor.py` → fetch + classify + generate `results.json` (no AI)
2. *[Monday only]* Merge past week's failures into results
3. *[Monday only]* Clone target repo → `/tmp/ci-investigate`
4. *[Monday only]* `inject_claude.py` → Cursor agent per failure → AI summaries + PRs
5. *[Monday only]* `monitor.py --render-only` → re-render HTML with AI data
6. Copy output to `docs/` for GitHub Pages
7. Git push to `main`
8. Send Slack notification

## Cursor CLI Agent

Each failure gets a deep investigation via `cursor agent --trust --yolo --print`:

- **Step 1:** Reads the target repo's README and docs to understand the project
- **Step 2:** Reviews CI evidence files (logs, junit, matrix diffs, ss dumps)
- **Step 3:** Decides what to do — fix code, report flake, or flag infra issue
- **Step 4:** Writes fixes directly and saves patch (`git diff > ci-evidence/fix.patch`)
- **Step 5:** Reports root cause, severity, and whether a fix was written

The agent is generic — it reads the repo docs and autonomously decides the right action based on the failure type and project structure.

### PR Rules

| Condition | Action |
|-----------|--------|
| Real error (matrix mismatch, test failure, build) | Open PR |
| Warning or infra issue | Analyze only, no PR |
| LOW severity | Analyze only, no PR |
| Duplicate patch (same fix as another job) | Skip PR |
| Similar PR already open for same issue | Skip PR |

PRs exclude `ci-evidence/` files — only actual code fixes are included.

## Quick Start — Local Setup (macOS)

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
| `SLACK_WEBHOOK_URL` | *(empty)* | Slack incoming webhook URL |
| `WEEKLY_AI` | `false` (auto `true` on Mondays) | Force AI analysis run |

### 3. Prerequisites

- **Cursor IDE** installed (with CLI at `/Applications/Cursor.app/...`)
- **Cursor CLI authenticated**: `cursor agent login`
- **GitHub CLI authenticated**: `gh auth login`
- **Python 3.9+** with `requests`: `pip3 install requests`
- **Full Disk Access** granted to `/bin/bash` (System Settings → Privacy)

### 4. Register with launchd (macOS)

Create `~/Library/LaunchAgents/com.prow-nightly-monitor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.prow-nightly-monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/path/to/prow-nightly-monitor/run-skill.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/Applications/Cursor.app/Contents/Resources/app/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/youruser</string>
        <key>SLACK_WEBHOOK_URL</key>
        <string>https://hooks.slack.com/services/...</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/tmp</string>
    <key>StandardOutPath</key>
    <string>/path/to/prow-nightly-monitor/launchd-out.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/prow-nightly-monitor/launchd-err.log</string>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.prow-nightly-monitor.plist
```

### 5. Run manually

```bash
./run-skill.sh
# Or force AI analysis on any day:
WEEKLY_AI=true ./run-skill.sh
# Or just the monitor (no AI):
python3 monitor.py
open public/index.html
```

## Dashboard Features

- **Formatted AI Analysis** — markdown rendered as HTML (tables, headers, code blocks)
- **Fix Patches** — expandable diff view of agent-generated fixes
- **PR Links** — direct links to opened PRs
- **JUnit Failures** — test case details from JUnit XML (when available)
- **Trend Chart** — pass/fail history across runs
- **Run History** — archived daily dashboards at `/runs/<date>/`
- **Slack Notifications** — daily summary with failure details and AI analysis (when available)

Features shown depend on the project — the agent adapts to whatever the repo provides.

## Using it for other projects

Change `JOB_FILTER` and `TARGET_REPO` to monitor any Prow job:

| Team | JOB_FILTER | TARGET_REPO |
|------|-----------|-------------|
| commatrix | `network-flow-matrix` | `openshift-kni/commatrix` |
| PTP | `ptp-operator` | `openshift/ptp-operator` |
| CNF | `cnf-features` | `openshift-kni/cnf-features-deploy` |
| SR-IOV | `sriov` | `k8snetworkplumbingwg/sriov-network-operator` |

The AI agent is generic — it reads each repo's README/docs first and adapts its investigation accordingly.

## File Structure

```
prow-nightly-monitor/
  run-skill.sh            # launchd entry point — orchestrates the full pipeline
  monitor.py              # Fetches Prow data, classifies failures, generates dashboard
  inject_claude.py        # Runs Cursor CLI agent per failure, opens PRs
  skill/SKILL.md          # Cursor skill for on-demand analysis
  public/                 # Generated output
    index.html            # Latest dashboard
    cursor/index.html     # AI-enhanced dashboard
    results.json          # Latest results (with AI summaries)
    history.html          # Run history index
    history.json          # Trend data
    runs/<date>/          # Archived daily dashboards
  docs/                   # GitHub Pages source (copy of public/)
```

## Cost

- **Daily runs (Tue-Fri):** Free — no AI, just Prow API calls
- **Weekly AI run (Monday):** ~$0.50-2.00 depending on number of failures (Cursor CLI usage)
