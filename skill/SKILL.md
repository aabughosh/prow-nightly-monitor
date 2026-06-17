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

## Supported Projects

| Project | Job Filter | Repo | Min Version |
|---------|-----------|------|-------------|
| commatrix | `network-flow-matrix` | openshift-kni/commatrix | 4.21 |
| PTP | `e2e-telco5g-ptp` | openshift/ptp-operator | 4.17 |

Configuration is in `projects.json`. PTP also has related repos: `linuxptp-daemon`, `cloud-event-proxy`.

## Step 1: Fetch jobs from Prow

Run this to get all nightly jobs (adjust the job filter as needed):

```bash
curl -s 'https://prow.ci.openshift.org/prowjobs.js?type=periodic&job=e2e-telco5g-ptp' | python3 -c "
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
    if ver and float(ver) >= 4.17:
        if name not in jobs or start > jobs[name]['start']:
            jobs[name] = {'name': name, 'state': state, 'start': start, 'url': url, 'ver': ver}
for j in sorted(jobs.values(), key=lambda x: x['name']):
    emoji = {'success':'PASS','failure':'FAIL','error':'ERROR','pending':'RUNNING'}.get(j['state'],'?')
    print(f\"{emoji} [{j['ver']}] {j['name'][-60:]}  {j['url']}\")
"
```

Show the user the results. Then investigate each FAILED job.

## Step 2: Determine the REAL failure source

Before investigating test code, determine WHERE the failure actually is:

1. **Fetch the top-level build log** (`build-log.txt`) — it shows all job steps, which passed, which failed.
2. **Check `finished.json`** in the test step. If it says `"passed":true`, the project's own tests passed!
3. **If project tests passed** but the job still failed → failure is from the CI framework (MonitorTest, operator-state-analyzer, lease-checker) — NOT from project code.
4. **Check for image pull errors** — if a DaemonSet was never created, look for registry.ci image failures in the build log before assuming hardware issues.

## Step 3: Fetch test results

For PTP, try `test_results.json` first (most detailed individual test names):
```
GCS_BASE/JOB_PATH/BUILD_ID/artifacts/WORKFLOW/STEP/artifacts/test_results.json
```

Fallback to JUnit XML:
```bash
curl -s "GCS_BASE/JOB_PATH/BUILD_ID/artifacts/junit_operator.xml"
```

Parse the XML/JSON to find which specific tests failed.

## Step 4: Fetch step logs and artifacts

For each failed step, fetch its build-log.txt:
- Log URL: `GCS_BASE/JOB_PATH/BUILD_ID/artifacts/WORKFLOW/STEP/build-log.txt`
- Read the last 200 lines. Look for `[FAIL]`, `Summarizing N Failures`, error messages.

Browse and download relevant artifacts:
- JUnit XML files, test_results.json
- For commatrix: `raw-ss-tcp`, `matrix-diff-ss`, `communication-matrix.csv`
- For PTP: `test_results_*.xml`, ptp4l/phc2sys logs
- `finished.json` — did the test step pass or fail?
- `ci-operator-build-log.txt` — overall job execution flow

## Step 5: Clone source repos and investigate

Clone the target repo and any related repos:

```bash
git clone --depth=1 https://github.com/openshift/ptp-operator.git /tmp/ci-investigate
git clone --depth=1 https://github.com/openshift/linuxptp-daemon.git /tmp/ci-investigate/linuxptp-daemon
```

Search the test code for the exact failing test function, recent commits, and relevant helpers.

## Step 6: Analyze each failed test individually

For EACH failed test, determine:
1. **What exact error occurred?** (quote the log verbatim)
2. **What function/file/line** in the source code is responsible?
3. **Is it a code regression**, infra issue, or flake?
4. **What PR/commit** introduced or broke this?
5. **What is the fix?**

## Step 7: Output format

For each test, provide:
- **Duration:** how long it ran
- **Error:** one-line error message
- **Evidence:** verbatim log quote proving the root cause (max 2 lines)
- **Root Cause:** 2-3 sentences referencing function/file/line
- **Breaking PR/Commit:** link or "Unknown"
- **Source File:** GitHub link
- **Is it a flake?** yes/no with evidence
- **Suggested Fix:** 1-2 sentences

Then summarize:
- **TL;DR:** one sentence (max 15 words)
- **Relation Between Failures:** common root cause?
- **Overall Issue Class:** infra_timeout | infra_other | test_regression | test_flake | test_failure | matrix_mismatch | build_error | unknown
- **Overall Severity:** CRITICAL / HIGH / MEDIUM / LOW

## Important rules

- **You must present evidence from raw data for your conclusions.** Quote the exact log lines verbatim that prove your root cause. Do NOT guess or assume — find it in the logs.
- **Check build logs for image pull errors, registry failures, or missing images** before assuming hardware/infra issues.
- Errors are errors. Warnings are warnings. Don't mix them.
- `level=warning` lines are informational — they did NOT cause the failure.
- Only `[FAILED]` assertions are real failures.
- If project tests passed (`finished.json` says success) but job failed, classify as `infra_other` and explain what CI component actually failed.
- Be specific — name the test, the process, the file, the line number.
- Think independently — use the data to reach your own conclusions.
- Cover ALL failed tests individually. Do not skip any.

## Fingerprinting

The system tracks recurring issues via fingerprints (hash of test_name + error_msg + category). Known issues are stored in `public/fingerprints.json`. If an issue recurs, the system reuses previous analysis instead of re-analyzing.

## Dashboard

Results are published to: https://aabughosh.github.io/prow-nightly-monitor/projects/ptp-operator/

The dashboard shows:
- Status, version, job name, duration
- **Summary column**: issue class + severity + TL;DR + failure count
- **Analysis column**: per-test details, expandable full AI analysis
- Known Issues page with fingerprint history
