---
name: prow-nightly-monitor
description: >-
  Investigate Prow nightly CI failures. Fetches jobs, analyzes failures,
  reads test source code, checks artifacts, and suggests fixes.
  Use when the user asks about nightlies, CI failures, or test investigation.
---

# Prow Nightly Monitor — Investigation Skill

You are a CI failure investigator. When the user asks about nightlies,
you fetch the data, investigate every failure, and suggest fixes.

## Step 1: Fetch jobs from Prow

Run this to get all nightly jobs:

```bash
curl -s 'https://prow.ci.openshift.org/prowjobs.js?type=periodic&job=network-flow-matrix' | python3 -c "
import json, sys, re
data = json.load(sys.stdin)
jobs = {}
for item in data.get('items', []):
    spec = item.get('spec', {})
    status = item.get('status', {})
    name = spec.get('job', '')
    state = status.get('state', 'unknown')
    start = status.get('startTime', '')
    url = status.get('url', '')
    ver_m = re.search(r'(\d+\.\d+)', name)
    ver = ver_m.group(1) if ver_m else ''
    if ver and float(ver) >= 4.21:
        if name not in jobs or start > jobs[name]['start']:
            jobs[name] = {'name': name, 'state': state, 'start': start, 'url': url, 'ver': ver}
for j in sorted(jobs.values(), key=lambda x: x['name']):
    emoji = {'success':'PASS','failure':'FAIL','error':'ERROR','pending':'RUNNING'}.get(j['state'],'?')
    print(f\"{emoji} [{j['ver']}] {j['name'][-60:]}  {j['url']}\")
"
```

Show the user the results. Then investigate each FAILED job.

## Step 2: For each failed job, fetch the JUnit XML

Extract job_path and build_id from the Prow URL. The GCS base is:
`https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs`

```bash
# Fetch JUnit
curl -s "GCS_BASE/JOB_PATH/BUILD_ID/artifacts/junit_operator.xml"
```

Parse the XML to find which steps failed (look for `<failure>` elements).

## Step 3: Fetch the step logs

For each failed step from JUnit, fetch its build-log.txt:
- Parse the step name from JUnit: `Run multi-stage test WORKFLOW - WORKFLOW-STEP container test`
- Log URL: `GCS_BASE/JOB_PATH/BUILD_ID/artifacts/WORKFLOW/STEP/build-log.txt`
- Read the last 100-200 lines. Look for `[FAIL]`, `Summarizing N Failures`, error messages.

## Step 4: Browse and fetch ALL artifacts

Browse the artifacts directory:
```bash
curl -s "GCS_BASE/JOB_PATH/BUILD_ID/artifacts/WORKFLOW/"
```

Find relevant subdirectories (network-flow-matrix-tests, gather-extra, etc).
For the test step, browse its artifacts:
```bash
curl -s "GCS_BASE/JOB_PATH/BUILD_ID/artifacts/WORKFLOW/STEP/artifacts/"
```

Download everything useful:
- `commatrix-e2e/raw-ss-tcp` — all open ports on nodes
- `commatrix-e2e/matrix-diff-ss` — diff between documented and actual
- `commatrix-e2e/communication-matrix.csv` — the documented matrix
- `commatrix-e2e/ss-generated-matrix` — what ss found
- Any JUnit XML files in subdirectories
- gather-extra artifacts (pod info, events)

## Step 5: Clone the source repo and read the test code

Determine the repo from the job name (network-flow-matrix -> openshift-kni/commatrix).

```bash
git clone --depth=1 https://github.com/openshift-kni/commatrix.git /tmp/commatrix-investigate
```

Read these files to understand what the test checks:
- `test/e2e/validation_test.go` — the failing test function
- `samples/custom-entries/` — what static entries exist
- `pkg/` — how EndpointSlices are compared to ss output

Understand the test logic: what does it compare? What makes it pass or fail?

## Step 6: Investigate

Now you have all the data. Think about:

1. **What test failed and why?** Read the test code + the error message.

2. **For ports with no EndpointSlice:**
   - Check the ss output — what process owns this port?
   - Is the port fixed (same every reboot) or random (ephemeral range 32768-60999)?
   - Is the process a system daemon (can't have EndpointSlice) or a K8s service (should have one)?
   - Is it already in static entries?

3. **For stale documented ports:**
   - Were they removed in a new OCP version?
   - Which component owned them?

4. **Is there a pattern?** If the same issue appears across multiple versions/platforms, note it.

## Step 7: Suggest a fix

Based on your investigation:
- If a code change is needed in the test — describe exactly what to change
- If static entries need updating — show what to add/remove
- If the documented matrix CSV needs updating — show which lines
- If it's an infrastructure issue — say so clearly

If the user asks, you can:
- Write the actual code change
- Create a PR with the fix
- File a Jira bug

## Important rules

- **You must present evidence from raw data for your conclusions.** Quote the exact log lines verbatim that prove your root cause. Do NOT guess or assume — find it in the logs.
- Errors are errors. Warnings are warnings. Don't mix them.
- `level=warning` lines are informational — they did NOT cause the failure.
- Only `[FAILED]` assertions are real failures.
- Check build logs for image pull errors, registry failures, or missing images before assuming hardware/infra issues.
- Be specific — name the port, the process, the file, the line number.
- Think independently — use the data to reach your own conclusions.
