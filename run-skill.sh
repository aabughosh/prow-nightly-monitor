#!/bin/bash
# Daily Prow Investigation
# 1. Python fetches FRESH data from Prow
# 2. Claude analyzes and outputs HTML to stdout
# 3. Script saves HTML and publishes
# Cron: 0 12 * * 1-5

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
OUTPUT="$REPO_DIR/public/claude-dashboard.html"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting daily run ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

# Step 1: Fetch FRESH data from Prow
echo "$(date): Fetching fresh Prow data..." >> "$LOG_FILE"
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
python3 monitor.py >> "$LOG_FILE" 2>&1
echo "$(date): Fresh results.json generated" >> "$LOG_FILE"

# Step 2: Claude reads fresh results and generates HTML
echo "$(date): Claude analyzing..." >> "$LOG_FILE"
"$CURSOR_CLI" agent --trust --print --output-format text \
"Read public/results.json (just generated with today's live Prow data).
Read template.html for the CSS styling — use the SAME dark theme and layout.

Generate public/claude-dashboard.html with:
- Same CSS as template.html (dark theme, Inter font, etc)
- Header: 'Claude Investigation — network-flow-matrix' + timestamp
- Stats row: total, passed, failed, pending, pass rate
- Category breakdown cards for failure types
- Table with ALL jobs from results.json
- For each failed job: show the investigation from results.json analysis field
  Include: failed tests, error messages, root cause, suggested fix
  Show matrix diff details if category is matrix_mismatch
  Show artifacts.ss_findings if available
- Expandable details sections for Investigation and AI Analysis
- Link back to main dashboard
- Footer with links

Write the complete HTML to public/claude-dashboard.html.
Make sure it uses TODAY's data from results.json, not cached data." \
>> "$LOG_FILE" 2>&1

# Check if file was updated
if [ -f "$OUTPUT" ] && [ "$(find "$OUTPUT" -mmin -5)" ]; then
  echo "$(date): Dashboard generated" >> "$LOG_FILE"
  open "$OUTPUT"

  git add public/claude-dashboard.html >> "$LOG_FILE" 2>&1
  git commit -m "daily: Claude investigation $(date +%Y-%m-%d)" >> "$LOG_FILE" 2>&1
  git push origin main >> "$LOG_FILE" 2>&1
  /opt/homebrew/bin/gh workflow run monitor.yml --repo aabughosh/prow-nightly-monitor >> "$LOG_FILE" 2>&1
  echo "$(date): Published to GitHub Pages" >> "$LOG_FILE"
else
  echo "$(date): Dashboard not updated" >> "$LOG_FILE"
fi

echo "$(date): === Done ===" >> "$LOG_FILE"
