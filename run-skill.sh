#!/bin/bash
# Daily Prow Investigation — Cursor CLI Claude
# Claude does EVERYTHING: fetch, investigate, analyze, generate dashboard
# Cron: 0 12 * * 1-5 (noon weekdays)

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"
OUTPUT="$REPO_DIR/public/claude-dashboard.html"
LOG_FILE="$REPO_DIR/skill-run.log"

echo "$(date): === Starting Claude investigation ===" >> "$LOG_FILE"
cd "$REPO_DIR" || exit 1

"$CURSOR_CLI" agent --trust --print --output-format text \
"You are a senior CI failure investigator. Your job is to deeply investigate every failed nightly job.

STEP 1 — FETCH ALL JOBS:
Run: curl -s 'https://prow.ci.openshift.org/prowjobs.js?type=periodic&job=network-flow-matrix'
Parse the JSON. For each job, extract: name, state, startTime, url.
Keep only the latest run per job name. Filter for versions 4.21+.

STEP 2 — FOR EACH FAILED JOB, INVESTIGATE DEEPLY:
Extract the job_path and build_id from the Prow URL.
The GCS base is: https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs

For each failed job:

a) Fetch JUnit XML:
   curl the artifacts/junit_operator.xml — parse <failure> elements to find which steps failed

b) Fetch the STEP-SPECIFIC build-log.txt:
   The step name comes from JUnit: 'Run multi-stage test WORKFLOW - WORKFLOW-STEP container test'
   The log is at: artifacts/WORKFLOW/STEP/build-log.txt
   Read the LAST 100 lines. Look for [FAIL], Summarizing N Failures, error messages.

c) Browse the artifacts directory:
   curl the artifacts/WORKFLOW/STEP/artifacts/ listing
   Look for: commatrix-e2e/raw-ss-tcp, junit/*.xml, or other useful files

d) If there are ports with no EndpointSlice:
   Fetch the raw-ss-tcp file from artifacts/WORKFLOW/network-flow-matrix-tests/artifacts/commatrix-e2e/raw-ss-tcp
   Find the specific port in the ss output. Note the process name and PID.
   Check if the port is in Linux ephemeral range (32768-60999) — if so, it changes on reboot.

e) Distinguish WARNINGS from FAILURES:
   Lines with 'level=warning' are informational, NOT the failure cause.
   Only [FAILED] assertions are real failures.

f) For each failure, determine:
   - Exact test name that failed
   - Exact error message (quote it)
   - Root cause — WHY it failed, not just what happened
   - Is it infra (retry), matrix mismatch (update docs), or code bug (fix needed)?
   - Specific recommended action

STEP 3 — GENERATE A FULL HTML DASHBOARD:
Write to public/claude-dashboard.html a self-contained HTML page with:
- Dark theme: background #0d1117, text #e1e4e8, cards #161b22
- Header with title, timestamp, job filter
- Stats row: total jobs, passed (green), failed (red), pending (blue), pass rate
- Table with ALL jobs: status emoji, version, job name (link to Prow), duration, started
- For each FAILED job: expandable investigation section with:
  * Failed test name and file reference
  * Error message (quoted)
  * Warnings (listed separately, clearly labeled as 'not a failure')
  * Root cause explanation
  * ss output for missing EndpointSlice ports (with port range analysis)
  * Recommended action in a green box
- Passed jobs dimmed
- Modern, clean design similar to Grafana or GitHub

Be thorough. Check every artifact. Read every log. This is your investigation." \
>> "$LOG_FILE" 2>&1

if [ -f "$OUTPUT" ]; then
  echo "$(date): Dashboard generated, opening..." >> "$LOG_FILE"
  open "$OUTPUT"
else
  echo "$(date): Dashboard not generated" >> "$LOG_FILE"
fi

echo "$(date): === Done ===" >> "$LOG_FILE"
