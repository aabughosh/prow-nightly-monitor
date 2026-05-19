#!/bin/bash
# Daily Prow Monitor with Claude AI (via Cursor CLI)
# Same dashboard as GitHub Pages, but Claude does the AI analysis
# Cron: 0 12 * * 1-5

REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting daily run ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

# Run monitor.py with Claude as the AI provider
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
export USE_CURSOR="true"
export GROQ_API_KEY=""
export CEREBRAS_API_KEY=""

echo "$(date): Running monitor with Claude..." >> "$LOG_FILE"
python3 monitor.py >> "$LOG_FILE" 2>&1
echo "$(date): Dashboard generated" >> "$LOG_FILE"

# Open in browser
open "$REPO_DIR/public/index.html"

echo "$(date): === Done ===" >> "$LOG_FILE"
