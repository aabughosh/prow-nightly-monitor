#!/bin/bash
# Daily Prow Investigation — two-step approach
# 1. Python fetches fresh data from Prow (can make HTTP requests)
# 2. Claude analyzes the fresh results and generates HTML dashboard
# Cron: 0 12 * * 1-5 (noon weekdays)

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
OUTPUT="$REPO_DIR/public/claude-dashboard.html"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting daily run ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

# Step 1: Python fetches FRESH data from Prow
echo "$(date): Fetching fresh Prow data..." >> "$LOG_FILE"
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
pip3 install requests -q 2>/dev/null
python3 monitor.py >> "$LOG_FILE" 2>&1
echo "$(date): Fresh data fetched" >> "$LOG_FILE"

# Step 2: Claude analyzes the FRESH results.json
echo "$(date): Claude analyzing fresh results..." >> "$LOG_FILE"
"$CURSOR_CLI" agent --trust --print --output-format text \
"Read public/results.json — it was JUST generated with today's fresh data from Prow.

For each failed job in the results:

1. Read the analysis.ai_summary, analysis.investigation, analysis.matrix_diff, analysis.artifacts
2. Read the analysis.junit_failures for test step details
3. Check analysis.category (matrix_mismatch, test_failure, infra, etc)

For MATRIX MISMATCH failures:
- Show which ports need to be ADDED, REMOVED, or INVESTIGATED
- If artifacts.ss_findings exists, show the ss output for each port
- Classify ports by range: 32768-60999 = ephemeral (OS-assigned, changes on reboot)
- Explain WHY the port has no EndpointSlice

For INFRA failures:
- Show the root cause
- Say if it's transient (retry) or systemic

For TEST failures:
- Show exact test name and error
- Distinguish warnings from failures

Generate a COMPLETE self-contained HTML dashboard to public/claude-dashboard.html:
- Dark theme (background #0d1117, text #e1e4e8, cards #161b22, borders #30363d)
- Header: Prow Nightly Monitor — Claude Investigation, timestamp
- Stats: total, passed (green #3fb950), failed (red #f85149), pending (blue #58a6ff), pass rate
- Category cards: count per failure type (matrix orange #f0883e, test red, infra yellow #d29922)
- Table with ALL jobs: status, version, job name (linked to Prow URL), duration
- For failed jobs: detailed investigation section with all findings
- Expandable details sections using HTML details/summary tags
- Footer with link to main dashboard and GitHub repo
- Make it look professional and modern" \
>> "$LOG_FILE" 2>&1

if [ -f "$OUTPUT" ]; then
  echo "$(date): Dashboard generated" >> "$LOG_FILE"
  open "$OUTPUT"

  # Push to GitHub Pages
  git add public/claude-dashboard.html >> "$LOG_FILE" 2>&1
  git commit -m "daily: Claude investigation $(date +%Y-%m-%d)" >> "$LOG_FILE" 2>&1
  git push origin main >> "$LOG_FILE" 2>&1
  /opt/homebrew/bin/gh workflow run monitor.yml --repo aabughosh/prow-nightly-monitor >> "$LOG_FILE" 2>&1
  echo "$(date): Published to GitHub Pages" >> "$LOG_FILE"
else
  echo "$(date): Dashboard not generated" >> "$LOG_FILE"
fi

echo "$(date): === Done ===" >> "$LOG_FILE"
