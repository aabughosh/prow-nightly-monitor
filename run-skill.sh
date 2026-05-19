#!/bin/bash
# Daily Prow Nightly Monitor with Cursor CLI Claude analysis
# 1. Python bot fetches data from Prow + runs Groq/Cerebras analysis
# 2. Cursor CLI Claude does deep investigation on the results
# 3. Both outputs pushed to GitHub Pages

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting daily prow-nightly-monitor ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

# Step 1: Run the Python monitor to fetch fresh data
echo "$(date): Running Python monitor..." >> "$LOG_FILE"
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
python3 monitor.py >> "$LOG_FILE" 2>&1

# Step 2: Run Cursor CLI Claude for deep analysis of the results
echo "$(date): Running Cursor CLI Claude analysis..." >> "$LOG_FILE"
CLAUDE_REPORT=$("$CURSOR_CLI" agent --trust --print --output-format text \
  "Read the file public/results.json. It contains today's Prow nightly results. For each failed job, read the analysis section and provide a detailed investigation report. Focus on: 1) What specific tests failed 2) Root cause - WHY it failed 3) Are warnings vs failures properly distinguished 4) For matrix mismatches: explain the port analysis 5) Recommended actions. Write your report to public/claude-report.md" \
  2>>"$LOG_FILE")

if [ -f "$REPO_DIR/public/claude-report.md" ]; then
  echo "$(date): Claude report generated" >> "$LOG_FILE"
else
  echo "$CLAUDE_REPORT" > "$REPO_DIR/public/claude-report.md" 2>/dev/null
fi

# Step 3: Push to GitHub
echo "$(date): Pushing to GitHub..." >> "$LOG_FILE"
cd "$REPO_DIR"
git add public/ >> "$LOG_FILE" 2>&1
git commit -m "daily: monitor + Claude analysis $(date +%Y-%m-%d)" >> "$LOG_FILE" 2>&1
git push origin main >> "$LOG_FILE" 2>&1

echo "$(date): === Done ===" >> "$LOG_FILE"
