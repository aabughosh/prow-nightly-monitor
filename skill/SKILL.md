---
name: prow-nightly-monitor
description: >-
  Check Prow nightly CI job results, analyze failures, and investigate root causes.
  Use when the user asks about nightly CI status, test failures, Prow jobs,
  commatrix nightlies, or CI investigation.
---

# Prow Nightly Monitor Skill

## When to use

The user asks about:
- Nightly CI results or Prow job status
- Why a nightly failed
- Commatrix / network-flow-matrix test results
- CI failure investigation or root cause analysis

## Step 1: Fetch job results from Prow

Use the Shell tool to fetch the latest periodic job results from Prow.
Replace `FILTER` with the job pattern the user wants (default: `network-flow-matrix`).

```bash
curl -s "https://prow.ci.openshift.org/prowjobs.js?type=periodic&job=FILTER" | python3 -c "
import json, sys
data = json.load(sys.stdin)
jobs = {}
for item in data.get('items', []):
    spec = item.get('spec', {})
    status = item.get('status', {})
    name = spec.get('job', '')
    state = status.get('state', 'unknown')
    start = status.get('startTime', '')
    url = status.get('url', '')
    if name not in jobs or start > jobs[name]['start']:
        jobs[name] = {'name': name, 'state': state, 'start': start, 'url': url}
for j in sorted(jobs.values(), key=lambda x: x['name']):
    emoji = {'success':'PASS','failure':'FAIL','error':'ERROR','pending':'PENDING'}.get(j['state'],'?')
    print(f\"{emoji} | {j['name'][-70:]} | {j['url']}\")
"
```

## Step 2: For each failed job, fetch the JUnit XML

```bash
# Extract job_path and build_id from the Prow URL, then:
curl -s "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs/JOB_PATH/BUILD_ID/artifacts/junit_operator.xml"
```

Parse the XML to find which test steps failed (look for `<failure>` elements).

## Step 3: Fetch the actual test step logs

For each failed step found in JUnit, fetch its build-log.txt:

```bash
# The step name comes from the JUnit test case name:
# "Run multi-stage test WORKFLOW - WORKFLOW-STEP container test"
# Step log is at:
curl -s "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs/JOB_PATH/BUILD_ID/artifacts/WORKFLOW/STEP/build-log.txt"
```

## Step 4: Analyze the failure

Read the step log and look for:

1. **Ginkgo test failures**: Lines containing `[FAIL]` followed by the test name
2. **Matrix mismatches**:
   - "ports are documented but are not used" — stale entries in the documented matrix
   - "ports are used but are not documented" — new ports missing from the matrix
   - "the following ports are not used" — ports in matrix but not open on nodes
   - "ports are used but don't have an endpointslice" — ports open but no EndpointSlice resource
3. **Go test failures**: Lines with `--- FAIL: TestName`
4. **Error messages**: Lines with `Error:`, `panic:`, `fatal:`
5. **Infrastructure issues**: Cluster install failures, timeouts, node not ready (but only if no specific test failures are found — these often appear in normal test flow)

## Step 5: Present the investigation

For each failed job, present a structured report:

### Report format

```
## Job: <job-name>
**Status:** FAIL
**Prow URL:** <link>
**Version:** <OCP version from job name>

### Failed Tests
- [step-name] `Test Name Here`
  Error: <actual error message from the log>

### Root Cause
<What specifically caused the failure based on the log analysis>

### Suggested Fix
<Concrete actionable steps>

### Severity
CRITICAL / HIGH / MEDIUM / LOW
```

### Classification rules

| Category | Pattern | Severity | Suggested Fix |
|---|---|---|---|
| Matrix mismatch | "ports are documented but are not used" / "ports are used but are not documented" | HIGH | List which ports to ADD/REMOVE from the documented matrix |
| EndpointSlice mismatch | "ports are used but don't have an endpointslice" | HIGH | Investigate the port — may need a static entry or new EndpointSlice |
| Ports not in use | "the following ports are not used" | MEDIUM | These ports are in the matrix but not open on nodes — may need removal or may be platform-specific |
| Test failure | Ginkgo `[FAIL]` with specific test name | MEDIUM | Show the test name and assertion error, suggest investigating the specific test |
| Build error | "build.*fail", "compile.*error" | HIGH | Show the compilation error, suggest local reproduction |
| Infrastructure | Cluster install timeout, node not ready (with NO test failures found) | LOW | Suggest retry, check cloud quotas |

### Important

- Always fetch the **step-specific log** (not just the top-level build-log.txt) — that's where the actual test output is
- "Node NotReady" often appears in normal test flow (e.g., commatrix nftables reboot test). Only classify as infra if no specific test failure is found
- Show the **actual test names** and **actual error messages** — never show raw timestamps or log metadata
- The dashboard is at: https://aabughosh.github.io/prow-nightly-monitor/
- The source repo is at: https://github.com/openshift-kni/commatrix
