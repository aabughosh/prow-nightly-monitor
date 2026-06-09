#!/bin/bash
# Daily Prow Monitor + Weekly AI Analysis — runs ALL projects from projects.json
# Daily (Mon-Fri noon): fetch data + basic analysis + Slack per project
# Weekly (Monday noon): also run Cursor CLI for deep AI analysis
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
    log "=== Starting WEEKLY run (with AI analysis) — ALL projects ==="
else
    log "=== Starting daily run — ALL projects ==="
fi
cd "$REPO_DIR" || exit 1

pip3 install requests -q 2>/dev/null || true

export FORK_OWNER="${FORK_OWNER:-aabughosh}"
export OPEN_PRS="${OPEN_PRS:-true}"
export SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
export SKIP_AI="true"

# Read all projects from projects.json and run each one
REPO_DIR="$REPO_DIR" python3 <<'PYEOF'
import json, os, subprocess, sys

repo_dir = os.environ['REPO_DIR']
projects_file = os.path.join(repo_dir, 'projects.json')
projects = json.load(open(projects_file))

# Output project names, one per line
for name in projects:
    print(name)
PYEOF

# Get project list
PROJECT_LIST=$(REPO_DIR="$REPO_DIR" python3 -c "
import json, os
repo_dir = os.environ['REPO_DIR']
projects = json.load(open(os.path.join(repo_dir, 'projects.json')))
for name in projects:
    print(name)
")

for PROJECT_NAME in $PROJECT_LIST; do
    log "--- Processing project: $PROJECT_NAME ---"

    # Read project config
    eval "$(REPO_DIR="$REPO_DIR" PROJECT_NAME="$PROJECT_NAME" python3 <<'PYEOF'
import json, os

repo_dir = os.environ['REPO_DIR']
project_name = os.environ['PROJECT_NAME']
projects = json.load(open(os.path.join(repo_dir, 'projects.json')))
p = projects[project_name]

print(f'export JOB_FILTER="{p["job_filter"]}"')
print(f'export MIN_VERSION="{p.get("min_version", "")}"')
print(f'export TARGET_REPO="{p["target_repo"]}"')
print(f'export UPSTREAM_REPO="{p["upstream_repo"]}"')
PYEOF
    )"

    PROJECT_OUTPUT="$REPO_DIR/public/projects/$PROJECT_NAME"
    mkdir -p "$PROJECT_OUTPUT"
    export OUTPUT_DIR="$PROJECT_OUTPUT"

    # Clean stale results
    rm -f "$PROJECT_OUTPUT/results.json"

    # Fetch Prow data + generate dashboard
    log "  Fetching Prow data (filter: $JOB_FILTER)..."
    if ! python3 monitor.py >> "$LOG_FILE" 2>&1; then
        log "  ERROR: monitor.py failed for $PROJECT_NAME — skipping"
        continue
    fi

    if [ ! -f "$PROJECT_OUTPUT/results.json" ]; then
        log "  ERROR: results.json not generated for $PROJECT_NAME — skipping"
        continue
    fi

    log "  Dashboard generated. results.json size: $(du -h "$PROJECT_OUTPUT/results.json" | cut -f1)"

    # Weekly AI analysis
    if [ "$WEEKLY_AI" = "true" ]; then
        # Merge failures from the past 7 days
        log "  Merging weekly failures from past 7 days..."
        REPO_DIR="$REPO_DIR" PROJECT_OUTPUT="$PROJECT_OUTPUT" python3 <<'PYEOF' >> "$LOG_FILE" 2>&1 || true
import json, os, glob
from datetime import datetime, timedelta

project_output = os.environ['PROJECT_OUTPUT']
results_file = os.path.join(project_output, 'results.json')
runs_dir = os.path.join(project_output, 'runs')
data = json.load(open(results_file))
current_jobs = {j['name']: j for j in data.get('jobs', [])}

week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
for run_file in sorted(glob.glob(os.path.join(runs_dir, '*', 'results.json'))):
    date = os.path.basename(os.path.dirname(run_file))
    if date < week_ago:
        continue
    try:
        run_data = json.load(open(run_file))
        for job in run_data.get('jobs', []):
            name = job.get('name', '')
            if job.get('state') == 'failure' and name not in current_jobs:
                current_jobs[name] = job
    except:
        pass

data['jobs'] = list(current_jobs.values())
with open(results_file, 'w') as f:
    json.dump(data, f, indent=2)
failures = sum(1 for j in data['jobs'] if j.get('state') == 'failure')
print(f'  Merged: {len(data["jobs"])} total jobs ({failures} failures from past week)')
PYEOF

        log "  Cloning $TARGET_REPO for agent context..."
        rm -rf /tmp/ci-investigate 2>/dev/null
        git clone --depth=1 "$TARGET_REPO" /tmp/ci-investigate >> "$LOG_FILE" 2>&1 || true

        cd /tmp
        if "$CURSOR_CLI" agent status >> "$LOG_FILE" 2>&1; then
            log "  Running AI analysis for $PROJECT_NAME..."
            cd "$REPO_DIR"
            if ! python3 "$REPO_DIR/inject_claude.py" >> "$LOG_FILE" 2>&1; then
                log "  WARNING: inject_claude.py had errors for $PROJECT_NAME"
            fi

            log "  Regenerating dashboard with AI analysis..."
            export RENDER_ONLY="true"
            python3 monitor.py >> "$LOG_FILE" 2>&1 || true
            unset RENDER_ONLY
        else
            log "  WARNING: Cursor CLI not authenticated — skipping AI for $PROJECT_NAME"
        fi
        cd "$REPO_DIR"
    fi
done

# Also keep the main commatrix dashboard at root + cursor/ for backward compatibility
if [ -d "$REPO_DIR/public/projects/commatrix" ]; then
    cp "$REPO_DIR/public/projects/commatrix/index.html" "$REPO_DIR/public/index.html" 2>/dev/null || true
    cp "$REPO_DIR/public/projects/commatrix/results.json" "$REPO_DIR/public/results.json" 2>/dev/null || true
    cp "$REPO_DIR/public/projects/commatrix/history.html" "$REPO_DIR/public/history.html" 2>/dev/null || true
    cp "$REPO_DIR/public/projects/commatrix/history.json" "$REPO_DIR/public/history.json" 2>/dev/null || true
    cp -r "$REPO_DIR/public/projects/commatrix/runs" "$REPO_DIR/public/runs" 2>/dev/null || true
    # Copy to cursor/ so relative links work
    mkdir -p "$REPO_DIR/public/cursor"
    cp "$REPO_DIR/public/projects/commatrix/index.html" "$REPO_DIR/public/cursor/index.html" 2>/dev/null || true
    cp "$REPO_DIR/public/projects/commatrix/results.json" "$REPO_DIR/public/cursor/results.json" 2>/dev/null || true
    cp "$REPO_DIR/public/projects/commatrix/history.html" "$REPO_DIR/public/cursor/history.html" 2>/dev/null || true
    cp "$REPO_DIR/public/projects/commatrix/history.json" "$REPO_DIR/public/cursor/history.json" 2>/dev/null || true
    cp -r "$REPO_DIR/public/projects/commatrix/runs" "$REPO_DIR/public/cursor/runs" 2>/dev/null || true
fi

# Generate index page linking all projects
REPO_DIR="$REPO_DIR" python3 <<'PYEOF' >> "$LOG_FILE" 2>&1 || true
import json, os
from datetime import datetime

repo_dir = os.environ['REPO_DIR']
projects = json.load(open(os.path.join(repo_dir, 'projects.json')))

html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prow Nightly Monitor</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #0d1117; color: #c9d1d9; }
h1 { color: #58a6ff; border-bottom: 1px solid #21262d; padding-bottom: 12px; }
.project-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin: 16px 0; transition: border-color 0.2s; }
.project-card:hover { border-color: #58a6ff; }
.project-card h2 { margin: 0 0 8px 0; }
.project-card h2 a { color: #58a6ff; text-decoration: none; }
.project-card h2 a:hover { text-decoration: underline; }
.project-card p { color: #8b949e; margin: 0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; margin-left: 8px; }
.badge-filter { background: #1f6feb33; color: #58a6ff; }
.updated { color: #484f58; font-size: 13px; margin-top: 8px; }
</style>
</head><body>
<h1>Prow Nightly Monitor</h1>
<p>Automated CI failure monitoring with AI-powered investigation.</p>
"""

for name, conf in projects.items():
    results_path = os.path.join(repo_dir, 'public', 'projects', name, 'results.json')
    status = ""
    if os.path.exists(results_path):
        try:
            data = json.load(open(results_path))
            jobs = data.get('jobs', [])
            passed = sum(1 for j in jobs if j.get('state') == 'success')
            failed = sum(1 for j in jobs if j.get('state') == 'failure')
            status = f"<span class='updated'>Latest: {passed} passed, {failed} failed</span>"
        except:
            pass

    html += f"""<div class="project-card">
<h2><a href="projects/{name}/">{name}</a> <span class="badge badge-filter">{conf['job_filter']}</span></h2>
<p>{conf.get('description', '')}</p>
{status}
</div>
"""

html += f"""<p class="updated">Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</p>
</body></html>"""

with open(os.path.join(repo_dir, 'public', 'cursor', 'index.html'), 'w') as f:
    f.write(html)
print(f'Generated project index with {len(projects)} projects')
PYEOF

# Copy to docs/ for GitHub Pages
log "Copying to docs/..."
rm -rf "$REPO_DIR/docs"
cp -r "$REPO_DIR/public" "$REPO_DIR/docs"

# Push to GitHub Pages
log "Pushing to GitHub Pages..."
cd "$REPO_DIR"
git add public/ docs/ 2>/dev/null
if git diff --cached --quiet; then
    log "No changes to push"
else
    git commit -m "dashboard: $(date '+%Y-%m-%d') nightly results (all projects)" >> "$LOG_FILE" 2>&1
    git push origin main >> "$LOG_FILE" 2>&1 && log "Pushed to GitHub Pages" || log "WARNING: git push failed"
fi

# Send Slack summary (for the primary project)
if [ -n "$SLACK_WEBHOOK_URL" ]; then
    log "Sending Slack summary..."
    export JOB_FILTER="network-flow-matrix"
    export OUTPUT_DIR="$REPO_DIR/public/projects/commatrix"
    REPO_DIR="$REPO_DIR" python3 <<'PYEOF' >> "$LOG_FILE" 2>&1 || true
import sys, os
sys.path.insert(0, os.environ['REPO_DIR'])
from inject_claude import send_slack_summary
send_slack_summary()
PYEOF
fi

log "=== Done (all projects) ==="
