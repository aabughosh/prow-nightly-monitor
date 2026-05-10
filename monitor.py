#!/usr/bin/env python3
"""Prow Nightly Monitor — track periodic CI jobs, analyze failures with AI.

Fetches periodic job results from Prow, classifies failures, optionally
uses an LLM to analyze failure logs, and generates an HTML dashboard.

Environment variables:
    JOB_FILTER        - Job name pattern to match (e.g. "network-flow-matrix")
    MIN_VERSION       - Minimum OCP version to track (e.g. "4.21"), optional
    OPENAI_API_KEY    - OpenAI API key for AI failure analysis (optional)
    AI_MODEL          - Model to use (default: "gpt-4o-mini")
    PROW_URL          - Prow instance URL (default: https://prow.ci.openshift.org)
    OUTPUT_DIR        - Where to write the HTML dashboard (default: ./public)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import xml.etree.ElementTree as ET

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("prow-monitor")

PROW_URL = os.environ.get("PROW_URL", "https://prow.ci.openshift.org").rstrip("/")
JOB_FILTER = os.environ.get("JOB_FILTER", "network-flow-matrix")
MIN_VERSION = os.environ.get("MIN_VERSION", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AUTO_FIX = os.environ.get("AUTO_FIX", "false").lower() == "true"
TARGET_REPO = os.environ.get("TARGET_REPO", "")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./public"))
GCS_BASE = "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs"


# ---------------------------------------------------------------------------
# Prow API
# ---------------------------------------------------------------------------

def fetch_prow_jobs() -> list[dict]:
    """Fetch periodic jobs from Prow and filter by JOB_FILTER."""
    log.info("Fetching jobs from Prow (filter: %s)", JOB_FILTER)
    resp = requests.get(
        f"{PROW_URL}/prowjobs.js",
        params={"type": "periodic", "job": JOB_FILTER},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])

    jobs = []
    for item in items:
        spec = item.get("spec", {})
        status = item.get("status", {})
        job_name = spec.get("job", "")

        if JOB_FILTER.lower() not in job_name.lower():
            continue

        jobs.append({
            "name": job_name,
            "state": status.get("state", "unknown"),
            "start_time": status.get("startTime", ""),
            "completion_time": status.get("completionTime", ""),
            "url": status.get("url", ""),
            "build_id": status.get("build_id", ""),
        })

    log.info("Found %d matching jobs", len(jobs))
    return jobs


def extract_version(job_name: str) -> str:
    """Extract OCP version from job name (e.g. '4.21' from '...-nightly-4.21-...')."""
    match = re.search(r"(\d+\.\d+)", job_name)
    return match.group(1) if match else ""


def filter_by_version(jobs: list[dict]) -> list[dict]:
    """Filter jobs by minimum OCP version."""
    if not MIN_VERSION:
        return jobs

    min_parts = [int(x) for x in MIN_VERSION.split(".")]
    filtered = []
    for job in jobs:
        version = extract_version(job["name"])
        if not version:
            filtered.append(job)
            continue
        ver_parts = [int(x) for x in version.split(".")]
        if ver_parts >= min_parts:
            filtered.append(job)
    return filtered


def get_latest_per_job(jobs: list[dict]) -> list[dict]:
    """Keep only the most recent run for each unique job name."""
    latest = {}
    for job in jobs:
        name = job["name"]
        if name not in latest or job["start_time"] > latest[name]["start_time"]:
            latest[name] = job
    return sorted(latest.values(), key=lambda j: j["name"])


def compute_duration(job: dict) -> str:
    """Compute human-readable duration."""
    start = job.get("start_time", "")
    end = job.get("completion_time", "")
    if not start or not end:
        return "running..." if job.get("state") == "pending" else "?"
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = e - s
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes}m"
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Log fetching
# ---------------------------------------------------------------------------

def get_build_log_url(job: dict) -> str:
    """Construct the GCS URL for the build log."""
    url = job.get("url", "")
    match = re.search(r"/logs/(.+)/(\d+)$", url)
    if match:
        job_path = match.group(1)
        build_id = match.group(2)
        return f"{GCS_BASE}/{job_path}/{build_id}/build-log.txt"
    return ""


def fetch_failure_log(job: dict, max_lines: int = 200) -> str:
    """Download the last N lines of the build log for a failed job."""
    log_url = get_build_log_url(job)
    if not log_url:
        return ""

    try:
        resp = requests.get(log_url, timeout=30)
        if resp.status_code != 200:
            return f"(Could not fetch log: HTTP {resp.status_code})"
        lines = resp.text.splitlines()
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        return "\n".join(tail)
    except Exception as e:
        return f"(Error fetching log: {e})"


# ---------------------------------------------------------------------------
# JUnit XML parsing (inspired by Micky's ptp-ci-triage approach)
# ---------------------------------------------------------------------------

def get_junit_url(job: dict) -> str:
    """Construct a URL to find JUnit XML artifacts."""
    url = job.get("url", "")
    match = re.search(r"/logs/(.+)/(\d+)$", url)
    if match:
        job_path = match.group(1)
        build_id = match.group(2)
        return f"{GCS_BASE}/{job_path}/{build_id}/artifacts/"
    return ""


def fetch_junit_results(job: dict) -> list[dict]:
    """Try to fetch and parse JUnit XML results for a job.

    Returns a list of test failure dicts with name, classname, message, duration.
    """
    url = job.get("url", "")
    match = re.search(r"/logs/(.+)/(\d+)$", url)
    if not match:
        return []

    job_path = match.group(1)
    build_id = match.group(2)

    junit_paths = [
        f"{GCS_BASE}/{job_path}/{build_id}/artifacts/junit_operator.xml",
        f"{GCS_BASE}/{job_path}/{build_id}/artifacts/e2e-report/junit.xml",
        f"{GCS_BASE}/{job_path}/{build_id}/artifacts/test-results/junit.xml",
    ]

    for junit_url in junit_paths:
        try:
            resp = requests.get(junit_url, timeout=15)
            if resp.status_code == 200 and "<?xml" in resp.text[:100]:
                return _parse_junit_xml(resp.text)
        except Exception:
            continue
    return []


def _parse_junit_xml(xml_text: str) -> list[dict]:
    """Parse JUnit XML and extract test failures."""
    failures = []
    try:
        root = ET.fromstring(xml_text)
        for testcase in root.iter("testcase"):
            failure = testcase.find("failure")
            error = testcase.find("error")
            if failure is not None or error is not None:
                elem = failure if failure is not None else error
                failures.append({
                    "name": testcase.get("name", "unknown"),
                    "classname": testcase.get("classname", ""),
                    "time": testcase.get("time", ""),
                    "message": (elem.get("message", "") or elem.text or "")[:500],
                    "type": elem.get("type", ""),
                })
    except ET.ParseError:
        pass
    return failures


# ---------------------------------------------------------------------------
# Trend tracking (pass/fail history over time)
# ---------------------------------------------------------------------------

HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "./public/history.json"))


def load_history() -> dict:
    """Load historical pass/fail data."""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return {"runs": []}


def save_history(history: dict) -> None:
    """Save historical data."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    runs = history.get("runs", [])
    if len(runs) > 30:
        history["runs"] = runs[-30:]
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def update_history(history: dict, jobs: list[dict]) -> dict:
    """Add today's results to history."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing_dates = [r["date"] for r in history.get("runs", [])]
    if today in existing_dates:
        history["runs"] = [r for r in history["runs"] if r["date"] != today]

    run_data = {
        "date": today,
        "total": len(jobs),
        "passed": sum(1 for j in jobs if j["state"] == "success"),
        "failed": sum(1 for j in jobs if j["state"] in ("failure", "error")),
        "pending": sum(1 for j in jobs if j["state"] == "pending"),
        "by_version": {},
    }

    for job in jobs:
        version = extract_version(job["name"])
        if version not in run_data["by_version"]:
            run_data["by_version"][version] = {"passed": 0, "failed": 0}
        if job["state"] == "success":
            run_data["by_version"][version]["passed"] += 1
        elif job["state"] in ("failure", "error"):
            run_data["by_version"][version]["failed"] += 1

    history.setdefault("runs", []).append(run_data)
    return history


def generate_trend_html(history: dict) -> str:
    """Generate HTML for the trend chart using simple CSS bars."""
    runs = history.get("runs", [])[-14:]
    if not runs:
        return ""

    versions = sorted(set(
        v for r in runs for v in r.get("by_version", {})
    ))

    bars_html = ""
    for run in runs:
        date = run["date"][5:]
        total = max(run.get("total", 1), 1)
        passed = run.get("passed", 0)
        failed = run.get("failed", 0)
        pass_pct = int(passed / total * 100)
        fail_pct = int(failed / total * 100)

        bars_html += f"""
        <div style="text-align:center;flex:1;min-width:40px">
          <div style="height:100px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center">
            <div style="width:24px;background:#dc3545;height:{fail_pct}px;border-radius:2px 2px 0 0" title="{failed} failed"></div>
            <div style="width:24px;background:#28a745;height:{pass_pct}px" title="{passed} passed"></div>
          </div>
          <div style="font-size:10px;color:#666;margin-top:4px">{date}</div>
        </div>"""

    version_trends = ""
    for version in versions:
        dots = ""
        for run in runs:
            vdata = run.get("by_version", {}).get(version, {})
            passed = vdata.get("passed", 0)
            failed = vdata.get("failed", 0)
            if failed > 0:
                dots += '<span style="color:#dc3545;font-size:18px" title="failed">●</span>'
            elif passed > 0:
                dots += '<span style="color:#28a745;font-size:18px" title="passed">●</span>'
            else:
                dots += '<span style="color:#ccc;font-size:18px" title="no data">○</span>'
        version_trends += f'<div style="margin:4px 0"><strong>{version}:</strong> {dots}</div>'

    return f"""
    <div style="background:white;padding:20px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin:16px 0">
      <h2 style="margin-top:0">Trend (last 14 days)</h2>
      <div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:16px">
        {bars_html}
      </div>
      <h3>Pass/Fail by Version</h3>
      {version_trends}
    </div>"""


# ---------------------------------------------------------------------------
# AI failure analysis
# ---------------------------------------------------------------------------

INFRA_PATTERNS = [
    (r"cluster.*install.*(?:timed? ?out|fail)", "Cluster installation failed (infra issue)"),
    (r"node.*NotReady", "Node not ready (infra issue)"),
    (r"context deadline exceeded", "Timeout — likely infra or slow cluster"),
    (r"error.*creating.*cluster", "Cluster creation error (infra issue)"),
    (r"unable to connect to the server", "Cannot connect to cluster (infra issue)"),
    (r"etcd.*not.*ready", "etcd not ready (infra issue)"),
    (r"pod.*CrashLoopBackOff", "Pod crash loop — could be infra or real bug"),
    (r"quota.*exceeded", "Resource quota exceeded (infra issue)"),
    (r"lease.*expired", "Lease expired (infra issue)"),
]


def classify_failure(log_text: str) -> tuple[str, str]:
    """Quick pattern-based classification. Returns (category, reason)."""
    for pattern, reason in INFRA_PATTERNS:
        if re.search(pattern, log_text, re.IGNORECASE):
            return "infra", reason

    if re.search(r"FAIL.*Test", log_text):
        return "test_failure", "Test assertion failed"
    if re.search(r"go.*build.*fail|compile.*error", log_text, re.IGNORECASE):
        return "build_error", "Build/compile error"
    if re.search(r"matrix.*mismatch|unexpected.*port|expected.*port", log_text, re.IGNORECASE):
        return "matrix_mismatch", "Communication matrix mismatch — ports changed"

    return "unknown", "Unknown failure — needs investigation"


def ai_analyze_failure(job: dict, log_text: str) -> str:
    """Use an LLM to analyze the failure log and produce a summary."""
    if not OPENAI_API_KEY:
        return ""

    log_truncated = log_text[-8000:] if len(log_text) > 8000 else log_text

    prompt = f"""You are a senior CI failure analyst for OpenShift. Analyze this failed CI job and provide a detailed, structured investigation report.

Job: {job['name']}
State: {job['state']}

Provide your analysis in this exact format:

**Failed Tests:**
- List each test that failed with its full name
- If no specific test names visible, say "Test names not found in logs"

**Failure Messages:**
- Quote the exact error messages from the logs (1-2 lines each)

**Root Cause:**
- What specifically caused the failure? Be precise (e.g. "port 9443 missing from expected matrix" not just "test failed")

**Classification:**
- INFRA (cluster setup, timeout, network, node issues — not a code bug)
- TEST_FAILURE (a real test assertion failed — code or config needs fixing)
- BUILD_ERROR (compilation or dependency issue)
- FLAKE (intermittent failure, likely passes on retry)
- MATRIX_MISMATCH (expected vs actual communication matrix differs)

**Recommended Action:**
- Specific steps to fix (e.g. "update expected matrix in docs/stable/", "bump dependency X to vY.Z", "retry — likely a flake")

**Severity:** CRITICAL / HIGH / MEDIUM / LOW

Log (last portion):
{log_truncated}
"""

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.2,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
            log.warning("AI analysis failed: HTTP %d", resp.status_code)
            return ""
    except Exception as e:
        log.warning("AI analysis error: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Auto-fix PR creation
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check, timeout=300)


def attempt_auto_fix(job: dict, category: str, log_text: str,
                     ai_summary: str) -> str:
    """Try to auto-fix the failure and create a PR. Returns PR URL or empty string."""
    if not AUTO_FIX or not GITHUB_TOKEN:
        return ""

    repo_url = TARGET_REPO
    if not repo_url:
        repo_url = _guess_repo_from_job(job["name"])
    if not repo_url:
        log.info("  Cannot determine target repo for auto-fix")
        return ""

    if category == "infra":
        log.info("  Infra failure — no auto-fix needed")
        return ""

    version = extract_version(job["name"])
    branch = f"release-{version}" if version else "main"

    tmpdir = tempfile.mkdtemp(prefix="prow-fix-")
    try:
        auth_url = repo_url
        if GITHUB_TOKEN and "github.com" in repo_url:
            auth_url = repo_url.replace(
                "https://github.com/",
                f"https://x-access-token:{GITHUB_TOKEN}@github.com/",
            )

        clone_result = _run(["git", "clone", "--depth=50", "--branch", branch,
                             auth_url, tmpdir], check=False)
        if clone_result.returncode != 0:
            for fallback in ["main", "master"]:
                clone_result = _run(["git", "clone", "--depth=50", "--branch",
                                     fallback, auth_url, tmpdir], check=False)
                if clone_result.returncode == 0:
                    branch = fallback
                    break
            if clone_result.returncode != 0:
                log.warning("  Could not clone %s", repo_url)
                return ""

        _run(["git", "config", "user.email", "prow-monitor@redhat.com"],
             cwd=tmpdir, check=False)
        _run(["git", "config", "user.name", "Prow Nightly Monitor"],
             cwd=tmpdir, check=False)

        fix_branch = f"fix-nightly-{version}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        _run(["git", "checkout", "-b", fix_branch], cwd=tmpdir)

        fixed = False
        fix_description = ""

        if category == "build_error" or "go.mod" in log_text.lower():
            result = _run(["go", "mod", "tidy"], cwd=tmpdir, check=False)
            if result.returncode == 0:
                diff = _run(["git", "diff", "--stat"], cwd=tmpdir, check=False)
                if diff.stdout.strip():
                    fixed = True
                    fix_description = "Run go mod tidy to fix dependency issues"

        if not fixed:
            govulncheck_result = _run(
                ["govulncheck", "./..."], cwd=tmpdir, check=False
            )
            if "Fixed in:" in govulncheck_result.stdout:
                fix_match = re.search(
                    r"Module:\s+(\S+).*?Found in:\s+(\S+).*?Fixed in:\s+\S+@(v[\d.]+[\w.-]*)",
                    govulncheck_result.stdout, re.DOTALL,
                )
                if fix_match:
                    pkg = fix_match.group(1)
                    fixed_ver = fix_match.group(3)
                    _run(["go", "get", f"{pkg}@{fixed_ver}"], cwd=tmpdir, check=False)
                    _run(["go", "mod", "tidy"], cwd=tmpdir, check=False)
                    vendor_dir = Path(tmpdir) / "vendor"
                    if vendor_dir.exists():
                        _run(["go", "mod", "vendor"], cwd=tmpdir, check=False)
                    diff = _run(["git", "diff", "--stat"], cwd=tmpdir, check=False)
                    if diff.stdout.strip():
                        fixed = True
                        fix_description = f"Bump {pkg} to {fixed_ver} (vulnerability fix)"

        if not fixed:
            log.info("  Could not determine an auto-fix for this failure")
            return ""

        _run(["git", "add", "-A"], cwd=tmpdir)
        _run(["git", "commit", "-m", f"fix: {fix_description}\n\nAuto-fix by prow-nightly-monitor for job:\n{job['name']}"],
             cwd=tmpdir, check=False)

        push_result = _run(["git", "push", "--force", "origin", fix_branch],
                           cwd=tmpdir, check=False)
        if push_result.returncode != 0:
            log.error("  Push failed: %s", push_result.stderr[:200])
            return ""

        repo_slug = repo_url.replace("https://github.com/", "")
        pr_result = _run(
            ["gh", "pr", "create",
             "--head", fix_branch,
             "--title", f"fix: {fix_description}",
             "--body", f"## Auto-fix by Prow Nightly Monitor\n\n"
                       f"**Job:** {job['name']}\n"
                       f"**Status:** {job['state']}\n"
                       f"**Fix:** {fix_description}\n\n"
                       f"**Prow URL:** {job['url']}\n\n"
                       f"{'**AI Analysis:** ' + ai_summary if ai_summary else ''}\n\n"
                       f"---\n*Created automatically by [prow-nightly-monitor]"
                       f"(https://github.com/aabughosh/prow-nightly-monitor)*",
             "--repo", repo_slug],
            cwd=tmpdir, check=False,
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
            log.info("  PR created: %s", pr_url)
            return pr_url
        else:
            log.error("  PR creation failed: %s", pr_result.stderr[:200])
            return ""

    except Exception as e:
        log.warning("  Auto-fix error: %s", e)
        return ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _guess_repo_from_job(job_name: str) -> str:
    """Guess the GitHub repo from the Prow job name."""
    if "network-flow-matrix" in job_name:
        return "https://github.com/openshift-kni/commatrix"
    if "ptp" in job_name:
        return "https://github.com/openshift/ptp-operator"
    if "cnf-features" in job_name:
        return "https://github.com/openshift-kni/cnf-features-deploy"
    if "sriov" in job_name:
        return "https://github.com/k8snetworkplumbingwg/sriov-network-operator"
    return ""


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

STATE_EMOJI = {
    "success": "✅",
    "failure": "❌",
    "error": "⚠️",
    "pending": "⏳",
    "aborted": "🚫",
}

STATE_COLOR = {
    "success": "#d4edda",
    "failure": "#f8d7da",
    "error": "#fff3cd",
    "pending": "#cce5ff",
    "aborted": "#e2e3e5",
}


def generate_html(jobs: list[dict], analyses: dict[str, dict],
                   trend_html: str = "") -> str:
    """Generate the HTML dashboard."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total = len(jobs)
    passed = sum(1 for j in jobs if j["state"] == "success")
    failed = sum(1 for j in jobs if j["state"] in ("failure", "error"))
    pending = sum(1 for j in jobs if j["state"] == "pending")

    versions = sorted(set(extract_version(j["name"]) for j in jobs if extract_version(j["name"])))

    rows = []
    for job in jobs:
        state = job["state"]
        emoji = STATE_EMOJI.get(state, "❓")
        color = STATE_COLOR.get(state, "#ffffff")
        version = extract_version(job["name"])
        duration = compute_duration(job)
        name_short = job["name"].replace("periodic-ci-openshift-release-main-nightly-", "")
        url = job["url"]

        analysis = analyses.get(job["name"], {})
        category = analysis.get("category", "")
        reason = analysis.get("reason", "")
        ai_summary = analysis.get("ai_summary", "")

        analysis_html = ""
        if state in ("failure", "error"):
            cat_badge = {
                "infra": '<span style="background:#ffc107;padding:2px 8px;border-radius:4px;font-size:12px">INFRA</span>',
                "test_failure": '<span style="background:#dc3545;color:white;padding:2px 8px;border-radius:4px;font-size:12px">TEST FAILURE</span>',
                "build_error": '<span style="background:#dc3545;color:white;padding:2px 8px;border-radius:4px;font-size:12px">BUILD ERROR</span>',
                "matrix_mismatch": '<span style="background:#fd7e14;color:white;padding:2px 8px;border-radius:4px;font-size:12px">MATRIX MISMATCH</span>',
                "unknown": '<span style="background:#6c757d;color:white;padding:2px 8px;border-radius:4px;font-size:12px">UNKNOWN</span>',
            }.get(category, "")

            analysis_html = f"{cat_badge} {reason}"

            junit_failures = analysis.get("junit_failures", [])
            if junit_failures:
                test_list = "".join(
                    f'<li><code>{f["name"]}</code><br><small style="color:#666">{f["message"][:200]}</small></li>'
                    for f in junit_failures[:5]
                )
                more = f"<li><em>...and {len(junit_failures)-5} more</em></li>" if len(junit_failures) > 5 else ""
                analysis_html += f'<details><summary>Failed Tests ({len(junit_failures)})</summary><ul style="font-size:13px;margin:8px 0">{test_list}{more}</ul></details>'

            if ai_summary:
                analysis_html += f'<details><summary>AI Analysis</summary><p style="font-size:13px;color:#333;margin:8px 0">{ai_summary}</p></details>'

            pr_url_fix = analysis.get("pr_url", "")
            if pr_url_fix:
                analysis_html += f'<div style="margin-top:4px"><span style="background:#28a745;color:white;padding:2px 8px;border-radius:4px;font-size:12px">AUTO-FIX</span> <a href="{pr_url_fix}" target="_blank">PR Created</a></div>'

        rows.append(f"""
        <tr style="background:{color}">
            <td>{emoji} {state}</td>
            <td><strong>{version}</strong></td>
            <td><a href="{url}" target="_blank" title="{job['name']}">{name_short}</a></td>
            <td>{duration}</td>
            <td>{analysis_html}</td>
            <td>{job['start_time'][:16] if job['start_time'] else ''}</td>
        </tr>""")

    version_filter_buttons = " ".join(
        f'<button onclick="filterVersion(\'{v}\')" style="margin:2px;padding:4px 12px;border:1px solid #ccc;border-radius:4px;cursor:pointer">{v}</button>'
        for v in versions
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prow Nightly Monitor — {JOB_FILTER}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #f5f5f5; }}
  h1 {{ color: #333; }}
  .stats {{ display: flex; gap: 16px; margin: 16px 0; }}
  .stat {{ background: white; padding: 16px 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; }}
  .stat-num {{ font-size: 32px; font-weight: bold; }}
  .stat-label {{ font-size: 14px; color: #666; }}
  .green {{ color: #28a745; }}
  .red {{ color: #dc3545; }}
  .blue {{ color: #007bff; }}
  .gray {{ color: #6c757d; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-top: 16px; }}
  th {{ background: #343a40; color: white; padding: 12px; text-align: left; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #eee; }}
  a {{ color: #007bff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  details {{ margin-top: 4px; }}
  summary {{ cursor: pointer; color: #007bff; font-size: 13px; }}
  .filters {{ margin: 12px 0; }}
  .footer {{ margin-top: 20px; font-size: 12px; color: #999; }}
</style>
</head>
<body>
<h1>Prow Nightly Monitor — <code>{JOB_FILTER}</code></h1>
<p>Last updated: {now}</p>

<div class="stats">
  <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat"><div class="stat-num green">{passed}</div><div class="stat-label">Passed</div></div>
  <div class="stat"><div class="stat-num red">{failed}</div><div class="stat-label">Failed</div></div>
  <div class="stat"><div class="stat-num blue">{pending}</div><div class="stat-label">Pending</div></div>
</div>

{trend_html}

<div class="filters">
  <strong>Filter by version:</strong>
  <button onclick="filterVersion('')" style="margin:2px;padding:4px 12px;border:1px solid #ccc;border-radius:4px;cursor:pointer">All</button>
  {version_filter_buttons}
  &nbsp;
  <strong>Status:</strong>
  <button onclick="filterState('')" style="margin:2px;padding:4px 12px;border:1px solid #ccc;border-radius:4px;cursor:pointer">All</button>
  <button onclick="filterState('failure')" style="margin:2px;padding:4px 12px;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#f8d7da">Failed</button>
  <button onclick="filterState('success')" style="margin:2px;padding:4px 12px;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#d4edda">Passed</button>
</div>

<table id="jobsTable">
<thead>
<tr>
  <th>Status</th>
  <th>Version</th>
  <th>Job</th>
  <th>Duration</th>
  <th>Analysis</th>
  <th>Started</th>
</tr>
</thead>
<tbody>
{"".join(rows)}
</tbody>
</table>

<div class="footer">
  Powered by <a href="https://github.com/aabughosh/prow-nightly-monitor">prow-nightly-monitor</a> |
  Data from <a href="{PROW_URL}/?type=periodic&job=*{JOB_FILTER}*">Prow</a> |
  AI analysis: {'enabled (' + AI_MODEL + ')' if OPENAI_API_KEY else 'disabled (set OPENAI_API_KEY to enable)'}
</div>

<script>
function filterVersion(v) {{
  const rows = document.querySelectorAll('#jobsTable tbody tr');
  rows.forEach(row => {{
    const version = row.cells[1].textContent.trim();
    row.style.display = (!v || version === v) ? '' : 'none';
  }});
}}
function filterState(s) {{
  const rows = document.querySelectorAll('#jobsTable tbody tr');
  rows.forEach(row => {{
    const state = row.cells[0].textContent.trim().toLowerCase();
    row.style.display = (!s || state.includes(s)) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Prow Nightly Monitor starting")
    log.info("Job filter: %s", JOB_FILTER)
    log.info("Min version: %s", MIN_VERSION or "(all)")
    log.info("AI analysis: %s", "enabled" if OPENAI_API_KEY else "disabled")

    jobs = fetch_prow_jobs()
    jobs = filter_by_version(jobs)
    jobs = get_latest_per_job(jobs)
    log.info("Tracking %d unique jobs", len(jobs))

    analyses = {}
    failed_jobs = [j for j in jobs if j["state"] in ("failure", "error")]
    log.info("Analyzing %d failed jobs", len(failed_jobs))

    for job in failed_jobs:
        log.info("Fetching log for: %s", job["name"])
        log_text = fetch_failure_log(job)

        category, reason = classify_failure(log_text)
        log.info("  Classification: %s — %s", category, reason)

        log.info("  Fetching JUnit results...")
        junit_failures = fetch_junit_results(job)
        if junit_failures:
            log.info("  Found %d test failures in JUnit", len(junit_failures))
            if category == "unknown":
                category = "test_failure"
                reason = f"{len(junit_failures)} test(s) failed: {junit_failures[0]['name'][:60]}"

        ai_summary = ""
        if OPENAI_API_KEY and log_text and not log_text.startswith("("):
            log.info("  Running AI analysis...")
            ai_summary = ai_analyze_failure(job, log_text)
            if ai_summary:
                log.info("  AI: %s", ai_summary[:100])

        pr_url = ""
        if AUTO_FIX and category not in ("infra",):
            log.info("  Attempting auto-fix...")
            pr_url = attempt_auto_fix(job, category, log_text, ai_summary)

        analyses[job["name"]] = {
            "category": category,
            "reason": reason,
            "ai_summary": ai_summary,
            "junit_failures": junit_failures,
            "pr_url": pr_url,
            "log_snippet": log_text[-500:] if log_text else "",
        }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    history = load_history()
    history = update_history(history, jobs)
    save_history(history)
    trend_html = generate_trend_html(history)
    log.info("Trend history updated (%d runs)", len(history.get("runs", [])))

    html = generate_html(jobs, analyses, trend_html)
    html_path = OUTPUT_DIR / "index.html"
    html_path.write_text(html)
    log.info("Dashboard written to %s", html_path)

    results_path = OUTPUT_DIR / "results.json"
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_filter": JOB_FILTER,
        "total_jobs": len(jobs),
        "passed": sum(1 for j in jobs if j["state"] == "success"),
        "failed": sum(1 for j in jobs if j["state"] in ("failure", "error")),
        "jobs": [
            {
                "name": j["name"],
                "version": extract_version(j["name"]),
                "state": j["state"],
                "duration": compute_duration(j),
                "url": j["url"],
                "analysis": analyses.get(j["name"], {}),
            }
            for j in jobs
        ],
    }
    results_path.write_text(json.dumps(results, indent=2))
    log.info("Results JSON written to %s", results_path)

    print(f"\n{'='*60}")
    print(f"Prow Nightly Monitor — {JOB_FILTER}")
    print(f"{'='*60}")
    for j in jobs:
        emoji = STATE_EMOJI.get(j["state"], "?")
        version = extract_version(j["name"])
        duration = compute_duration(j)
        analysis = analyses.get(j["name"], {})
        reason = analysis.get("reason", "")
        line = f"  {emoji} [{version}] {j['name'][:60]} ({duration})"
        if reason:
            line += f" — {reason}"
        print(line)
    print(f"{'='*60}")
    print(f"Dashboard: {html_path.resolve()}")


if __name__ == "__main__":
    main()
