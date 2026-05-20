#!/bin/bash
# Daily Prow Monitor — full pipeline
# 1. monitor.py fetches data + generates dashboard
# 2. cursor agent --print does deep investigation on each failure
# 3. Results saved to dashboard HTML
# Cron: 0 12 * * 1-5

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting daily run ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

# Step 1: Generate dashboard with Groq/Cerebras AI
echo "$(date): Generating dashboard..." >> "$LOG_FILE"
export JOB_FILTER="network-flow-matrix"
export MIN_VERSION="4.21"
export OUTPUT_DIR="$REPO_DIR/public"
pip3 install requests -q 2>/dev/null
python3 monitor.py >> "$LOG_FILE" 2>&1
echo "$(date): Dashboard ready" >> "$LOG_FILE"

# Step 2: Deep investigation — clone commatrix, give all data to Claude
echo "$(date): Running deep investigation..." >> "$LOG_FILE"

# Clone commatrix for Claude to read
rm -rf /tmp/commatrix-investigate 2>/dev/null
git clone --depth=1 https://github.com/openshift-kni/commatrix.git /tmp/commatrix-investigate 2>/dev/null

# Run Claude on the commatrix repo with the results
INVESTIGATION=$("$CURSOR_CLI" agent --trust --print --output-format text \
  "Read $REPO_DIR/public/results.json for today's failures. Then investigate:

For each FAILED job:
1. Read the error and warnings from the analysis field
2. Read test/e2e/validation_test.go to understand what the test checks
3. Read samples/custom-entries/ to see what static entries exist
4. For matrix mismatches: check the artifacts.ss_findings for port details
5. Determine: what process owns the failing port, can it have an EndpointSlice, is the port fixed or random

Keep it simple:
- What failed (test name)
- Error (exact message)  
- Warnings (separate, not the failure)
- Why (your analysis)
- What to do (one specific action)" \
  2>>"$LOG_FILE")

if [ -n "$INVESTIGATION" ]; then
  # Save as markdown
  echo "$INVESTIGATION" > "$REPO_DIR/public/claude-report.md"
  # Convert to HTML
  python3 "$REPO_DIR/md2html.py" "$REPO_DIR/public/claude-report.md" "$REPO_DIR/public/claude-report.html" 2>/dev/null
  echo "$(date): Claude investigation saved" >> "$LOG_FILE"
fi

# Step 3: Open dashboard
open "$REPO_DIR/public/index.html"

echo "$(date): === Done ===" >> "$LOG_FILE"
