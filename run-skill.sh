#!/bin/bash
# Daily Prow Monitor + Weekly AI Analysis
# Daily (Mon-Fri noon): fetch data + basic analysis + Slack
# Weekly (Monday noon): also run Cursor CLI for deep AI analysis (no PRs)
set -euo pipefail

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
LOG_FILE="$REPO_DIR/skill-run.log"
MAX_LOG_SIZE=$((5 * 1024 * 1024))  # 5 MB

export PATH="/opt/homebrew/bin:/Applications/Cursor.app/Contents/Resources/app/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Rotate log if it gets too large
if [ -f "$LOG_FILE" ] && [ "$(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$MAX_LOG_SIZE" ]; then
    mv "$LOG_FILE" "$LOG_FILE.prev"
fi

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG_FILE"; }

# Determine if this is the weekly AI run (Monday or WEEKLY_AI=true)
DAY_OF_WEEK=$(date +%u)  # 1=Monday
WEEKLY_AI="${WEEKLY_AI:-false}"
if [ "$DAY_OF_WEEK" = "1" ]; then
    WEEKLY_AI="true"
fi

if [ "$WEEKLY_AI" = "true" ]; then
    log "=== Starting WEEKLY run (with AI analysis) ==="
else
    log "=== Starting daily run ==="
fi
cd "$REPO_DIR" || exit 1

# Clean stale results so they don't accumulate across runs
rm -f "$REPO_DIR/public/results.json"
log "Cleaned previous results.json"

# Step 1: Fetch Prow data + generate dashboard
log "Fetching Prow data..."
export JOB_FILTER="${JOB_FILTER:-network-flow-matrix}"
export MIN_VERSION="${MIN_VERSION:-4.21}"
export TARGET_REPO="${TARGET_REPO:-https://github.com/openshift-kni/commatrix.git}"
export UPSTREAM_REPO="${UPSTREAM_REPO:-openshift-kni/commatrix}"
export FORK_OWNER="${FORK_OWNER:-aabughosh}"
export OPEN_PRS="${OPEN_PRS:-true}"
export SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
export OUTPUT_DIR="$REPO_DIR/public"
export SKIP_AI="true"

pip3 install requests -q 2>/dev/null || true
if ! python3 monitor.py >> "$LOG_FILE" 2>&1; then
    log "ERROR: monitor.py failed — aborting"
    exit 1
fi

if [ ! -f "$REPO_DIR/public/results.json" ]; then
    log "ERROR: results.json not generated — aborting"
    exit 1
fi

log "Dashboard generated (without AI). results.json size: $(du -h "$REPO_DIR/public/results.json" | cut -f1)"

# Step 2: Run Cursor CLI AI analysis (weekly only — covers full week's failures)
if [ "$WEEKLY_AI" = "true" ]; then
    # Merge failures from the past 7 days into current results.json
    log "Merging weekly failures from past 7 days..."
    python3 -c "
import json, os, glob
from datetime import datetime, timedelta

results_file = '$REPO_DIR/public/results.json'
runs_dir = '$REPO_DIR/public/runs'
data = json.load(open(results_file))
current_jobs = {j['name']: j for j in data.get('jobs', [])}

# Collect failures from past 7 days
week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
for run_dir in sorted(glob.glob(f'{runs_dir}/*/results.json')):
    date = os.path.basename(os.path.dirname(run_dir))
    if date < week_ago:
        continue
    try:
        run_data = json.load(open(run_dir))
        for job in run_data.get('jobs', []):
            name = job.get('name', '')
            if job.get('state') == 'failure' and name not in current_jobs:
                current_jobs[name] = job
    except: pass

data['jobs'] = list(current_jobs.values())
with open(results_file, 'w') as f:
    json.dump(data, f, indent=2)
print(f'Merged: {len(data[\"jobs\"])} total jobs ({sum(1 for j in data[\"jobs\"] if j.get(\"state\")==\"failure\")} failures from past week)')
" >> "$LOG_FILE" 2>&1 || true

    log "Cloning $TARGET_REPO for agent context..."
    rm -rf /tmp/ci-investigate 2>/dev/null
    if [ -n "$TARGET_REPO" ]; then
        git clone --depth=1 "$TARGET_REPO" /tmp/ci-investigate >> "$LOG_FILE" 2>&1 || true
    else
        mkdir -p /tmp/ci-investigate
    fi

    cd /tmp
    if ! "$CURSOR_CLI" agent status >> "$LOG_FILE" 2>&1; then
        log "WARNING: Cursor CLI not authenticated — skipping AI analysis"
        log "Run: $CURSOR_CLI agent login"
    else
        log "Cursor CLI authenticated — running weekly AI analysis..."
        cd "$REPO_DIR"
        if ! python3 "$REPO_DIR/inject_claude.py" >> "$LOG_FILE" 2>&1; then
            log "WARNING: inject_claude.py had errors (see above)"
        fi

        log "Regenerating dashboard with AI analysis..."
        export RENDER_ONLY="true"
        python3 monitor.py >> "$LOG_FILE" 2>&1 || true
        unset RENDER_ONLY
    fi
else
    log "Skipping AI analysis (daily run — AI runs on Mondays only)"
fi

# Step 3: Copy dashboard to docs/ for GitHub Pages
log "Copying dashboard to docs/..."
mkdir -p "$REPO_DIR/public/cursor"
cp "$REPO_DIR/public/index.html" "$REPO_DIR/public/cursor/index.html"
cp "$REPO_DIR/public/results.json" "$REPO_DIR/public/cursor/results.json" 2>/dev/null || true
rm -rf "$REPO_DIR/docs"
cp -r "$REPO_DIR/public" "$REPO_DIR/docs"

# Step 4: Push dashboard to GitHub Pages
log "Pushing dashboard to GitHub Pages..."
cd "$REPO_DIR"
git add public/index.html public/results.json public/history.html public/history.json public/runs/ public/cursor/ docs/ 2>/dev/null
if git diff --cached --quiet; then
    log "No dashboard changes to push"
else
    git commit -m "dashboard: $(date '+%Y-%m-%d') nightly results" >> "$LOG_FILE" 2>&1
    git push origin main >> "$LOG_FILE" 2>&1 && log "Dashboard pushed to GitHub Pages" || log "WARNING: git push failed"
fi

# Step 5: Send Slack summary
if [ -n "$SLACK_WEBHOOK_URL" ]; then
    log "Sending Slack summary..."
    python3 -c "import sys; sys.path.insert(0,'$REPO_DIR'); from inject_claude import send_slack_summary; send_slack_summary()" >> "$LOG_FILE" 2>&1 || true
fi

# Step 6: Open the dashboard
open "$REPO_DIR/public/index.html"
log "=== Done ==="
