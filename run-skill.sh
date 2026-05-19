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

# Step 1: Fetch FRESH data from Prow (NO AI — Claude does that)
echo "$(date): Fetching fresh Prow data..." >> "$LOG_FILE"
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
export GROQ_API_KEY=""
export CEREBRAS_API_KEY=""
export OPENAI_API_KEY=""
export GEMINI_API_KEY=""
export HF_API_KEY=""
export DEEPSEEK_API_KEY=""
python3 monitor.py >> "$LOG_FILE" 2>&1
echo "$(date): Fresh results.json generated (data only, no AI)" >> "$LOG_FILE"

# Step 2: Embed results into standalone HTML (no fetch — works as file://)
echo "$(date): Building claude-dashboard.html from results.json..." >> "$LOG_FILE"
python3 "$REPO_DIR/generate_claude_dashboard.py" --from-results "$REPO_DIR/public/results.json" >> "$LOG_FILE" 2>&1

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
