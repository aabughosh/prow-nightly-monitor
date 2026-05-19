#!/bin/bash
cd "$HOME/Documents/GitHub/prow-nightly-monitor"

/Applications/Cursor.app/Contents/Resources/app/bin/cursor agent --trust --print --output-format text \
"You are a CI failure investigator. Do these steps:

1. Fetch today's Prow jobs by running this shell command:
   curl -s 'https://prow.ci.openshift.org/prowjobs.js?type=periodic&job=network-flow-matrix' > /tmp/prow-jobs.json

2. Parse the JSON to find the latest run per job, filter for versions 4.21+. Show passed/failed/pending.

3. For each FAILED job: extract job_path and build_id from the Prow URL. Then:
   - Fetch JUnit: curl the GCS URL at gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com for artifacts/junit_operator.xml
   - Find failed steps, fetch their build-log.txt
   - For matrix mismatches: fetch raw-ss-tcp from artifacts/commatrix-e2e/
   - Analyze: what failed, why, warnings vs failures, port ranges

4. Write public/claude-dashboard.html — a COMPLETE self-contained HTML dashboard:
   - Read template.html for the CSS style (dark theme)
   - Embed ALL data inline in the HTML, no JavaScript fetch
   - Stats: total, passed, failed, pending
   - Table with all jobs linked to Prow
   - Failed jobs: detailed investigation with test names, errors, root cause, fix suggestion
   - Make it professional"

echo "=== DONE ==="
