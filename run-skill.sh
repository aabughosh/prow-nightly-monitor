#!/bin/bash
# Daily Prow Monitor — Claude-powered, all results in ONE dashboard
# Cron: 0 12 * * 1-5

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting daily run ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

# Step 1: Fetch data + generate dashboard (no external AI)
echo "$(date): Fetching Prow data..." >> "$LOG_FILE"
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
export GROQ_API_KEY=""
export CEREBRAS_API_KEY=""
pip3 install requests -q 2>/dev/null
python3 monitor.py >> "$LOG_FILE" 2>&1

# Step 2: Clone commatrix for Claude context
rm -rf /tmp/commatrix-investigate 2>/dev/null
git clone --depth=1 https://github.com/openshift-kni/commatrix.git /tmp/commatrix-investigate 2>/dev/null

# Step 3: Run Claude on each failure, inject into results, regenerate dashboard
echo "$(date): Running Claude on failures..." >> "$LOG_FILE"
python3 "$REPO_DIR/inject_claude.py" >> "$LOG_FILE" 2>&1

# Step 4: Regenerate dashboard with Claude analysis
echo "$(date): Regenerating dashboard..." >> "$LOG_FILE"
python3 monitor.py >> "$LOG_FILE" 2>&1

# Step 5: Open
open "$REPO_DIR/public/index.html"
echo "$(date): === Done ===" >> "$LOG_FILE"
