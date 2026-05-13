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
import time
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "auto")
AI_MODEL = os.environ.get("AI_MODEL", "")
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
            if resp.status_code == 200 and ("<testsuites" in resp.text[:200] or "<?xml" in resp.text[:200]):
                results = _parse_junit_xml(resp.text)
                if results:
                    return results
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
                msg_text = elem.get("message", "") or elem.text or ""
                failures.append({
                    "name": testcase.get("name", "unknown"),
                    "classname": testcase.get("classname", ""),
                    "time": testcase.get("time", ""),
                    "message": msg_text[:4000],
                    "type": elem.get("type", ""),
                })
    except ET.ParseError:
        pass
    return failures


# ---------------------------------------------------------------------------
# Step-specific log fetching (actual test output, not CI runner output)
# ---------------------------------------------------------------------------

def _extract_step_info(junit_name: str) -> tuple[str, str]:
    """Extract workflow and step names from a JUnit test case name.

    Example input:
      "Run multi-stage test aws-ovn-network-flow-matrix -
       aws-ovn-network-flow-matrix-network-flow-matrix-tests container test"
    Returns: ("aws-ovn-network-flow-matrix", "network-flow-matrix-tests")
    """
    m = re.match(r"Run multi-stage test (\S+) - (\S+) container test", junit_name)
    if not m:
        return "", ""
    workflow = m.group(1)
    step_full = m.group(2)
    prefix = workflow + "-"
    step_short = step_full[len(prefix):] if step_full.startswith(prefix) else step_full
    return workflow, step_short


def fetch_failed_step_logs(job: dict, junit_failures: list[dict],
                           max_lines: int = 500) -> dict[str, str]:
    """Fetch the build-log.txt for each failed CI step identified in JUnit.

    Returns a dict mapping step_short_name -> log_text.
    """
    url = job.get("url", "")
    m = re.search(r"/logs/(.+)/(\d+)$", url)
    if not m:
        return {}

    job_path, build_id = m.group(1), m.group(2)
    step_logs: dict[str, str] = {}

    for jf in junit_failures:
        workflow, step_short = _extract_step_info(jf.get("name", ""))
        if not workflow or not step_short:
            continue
        if step_short in step_logs:
            continue

        step_log_url = (
            f"{GCS_BASE}/{job_path}/{build_id}/artifacts/"
            f"{workflow}/{step_short}/build-log.txt"
        )
        try:
            resp = requests.get(step_log_url, timeout=20)
            if resp.status_code == 200:
                lines = resp.text.splitlines()
                tail = lines[-max_lines:] if len(lines) > max_lines else lines
                step_logs[step_short] = "\n".join(tail)
                log.info("  Fetched step log: %s (%d lines)", step_short, len(lines))
        except Exception as exc:
            log.debug("  Could not fetch step log %s: %s", step_short, exc)

    return step_logs


# ---------------------------------------------------------------------------
# Matrix diff parsing (commatrix-specific)
# ---------------------------------------------------------------------------

def parse_matrix_diff(log_text: str) -> dict:
    """Parse commatrix port differences from log output.

    Detects lines like:
      'the following ports are documented but are not used:\\n...'
      'the following ports are used but are not documented:\\n...'

    Returns dict with is_matrix_mismatch, undocumented_ports, stale_ports, summary.
    """
    result: dict = {
        "is_matrix_mismatch": False,
        "undocumented_ports": [],
        "stale_ports": [],
        "summary": "",
    }

    stale_match = re.search(
        r"ports are documented but are not used:\s*\\n((?:[^\n\"]+\\n)*)",
        log_text,
    )
    if not stale_match:
        stale_match = re.search(
            r"ports are documented but are not used:\s*\n((?:.*\n)*?)\s*(?:\"|$)",
            log_text,
        )
    if stale_match:
        raw = stale_match.group(1).replace("\\n", "\n").strip()
        result["stale_ports"] = [
            line.strip() for line in raw.splitlines() if line.strip()
        ]

    undoc_match = re.search(
        r"ports are used but are not documented:\s*\\n((?:[^\n\"]+\\n)*)",
        log_text,
    )
    if not undoc_match:
        undoc_match = re.search(
            r"ports are used but are not documented:\s*\n((?:.*\n)*?)\s*(?:\"|{|$)",
            log_text,
        )
    if undoc_match:
        raw = undoc_match.group(1).replace("\\n", "\n").strip()
        result["undocumented_ports"] = [
            line.strip() for line in raw.splitlines() if line.strip()
        ]

    if result["stale_ports"] or result["undocumented_ports"]:
        result["is_matrix_mismatch"] = True
        parts = []
        if result["undocumented_ports"]:
            parts.append(f"{len(result['undocumented_ports'])} port(s) used but not documented")
        if result["stale_ports"]:
            parts.append(f"{len(result['stale_ports'])} port(s) documented but no longer used")
        result["summary"] = "; ".join(parts)

    return result


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
    """Generate HTML for the trend chart — dark theme with links."""
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
        pass_pct = min(int(passed / total * 100), 100)
        fail_pct = min(int(failed / total * 100), 100)
        prow_link = f'{PROW_URL}/?type=periodic&job=*{JOB_FILTER}*'

        bars_html += (
            f'<a href="{prow_link}" target="_blank" style="text-decoration:none;text-align:center;flex:1;min-width:40px">'
            f'<div style="height:80px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center">'
            f'<div style="font-size:10px;color:#8b949e;margin-bottom:2px">{passed}/{total}</div>'
            f'<div style="width:28px;background:#f85149;height:{fail_pct}px;border-radius:3px 3px 0 0" title="{failed} failed"></div>'
            f'<div style="width:28px;background:#3fb950;height:{max(pass_pct, 2)}px;border-radius:0 0 3px 3px" title="{passed} passed"></div>'
            f'</div>'
            f'<div style="font-size:10px;color:#484f58;margin-top:6px">{date}</div>'
            f'</a>'
        )

    version_trends = ""
    for version in versions:
        dots = ""
        for run in runs:
            vdata = run.get("by_version", {}).get(version, {})
            p = vdata.get("passed", 0)
            f = vdata.get("failed", 0)
            if f > 0:
                dots += f'<span style="display:inline-block;width:14px;height:14px;background:#f85149;border-radius:50%;margin:0 2px" title="{run["date"]}: {f} failed"></span>'
            elif p > 0:
                dots += f'<span style="display:inline-block;width:14px;height:14px;background:#3fb950;border-radius:50%;margin:0 2px" title="{run["date"]}: {p} passed"></span>'
            else:
                dots += f'<span style="display:inline-block;width:14px;height:14px;background:#21262d;border:1px solid #30363d;border-radius:50%;margin:0 2px" title="{run["date"]}: no data"></span>'
        version_trends += (
            f'<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #21262d">'
            f'<span class="version-badge" style="min-width:50px;text-align:center">{version}</span>'
            f'<div>{dots}</div>'
            f'</div>'
        )

    return f"""
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-bottom:24px">
      <h2 style="font-size:16px;color:#f0f6fc;margin-bottom:16px">Trend (last 14 days)</h2>
      <div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:24px;padding:12px;background:#0d1117;border-radius:8px">
        {bars_html}
      </div>
      <h3 style="font-size:14px;color:#8b949e;margin-bottom:12px;text-transform:uppercase;letter-spacing:1px">Pass/Fail by Version</h3>
      {version_trends}
    </div>"""


# ---------------------------------------------------------------------------
# Test layer auto-detection
# ---------------------------------------------------------------------------

LAYER_COLORS = [
    "#e91e63", "#9c27b0", "#3f51b5", "#009688", "#ff5722",
    "#795548", "#607d8b", "#4caf50", "#ff9800", "#673ab7",
]

INFRA_LAYER_PATTERNS = [
    (r"cluster.*install|creating.*cluster|bootstrap|waiting for cluster", "Cluster Setup"),
    (r"upgrade.*fail|upgrade.*timeout|clusterversion.*error|from.*stable.*to", "Upgrade"),
    (r"must-gather|clusteroperator.*degraded", "Cluster Health"),
]


def categorize_test_layer(log_text: str, job_name: str,
                          junit_failures: list[dict] | None = None,
                          category: str = "") -> tuple[str, str]:
    """Auto-detect which test layer the failure is in.

    Priority: JUnit/test-specific data first, then sig tags, then job name.
    Infra patterns are checked ONLY if nothing else matches, to avoid
    mis-labelling test failures as "Cluster Health" due to phrases like
    "must-gather" or "Node NotReady" that appear in normal CI cleanup output.
    """
    if category == "matrix_mismatch":
        return "matrix_validation", "Matrix Validation"

    if junit_failures:
        test_steps = set()
        for f in junit_failures:
            name = f.get("name", "")
            _, step_short = _extract_step_info(name)
            if step_short:
                test_steps.add(step_short)
            sig_match = re.search(r"\[sig-([^\]]+)\]", name)
            if sig_match:
                test_steps.add(f"sig-{sig_match.group(1)}")
            cn = f.get("classname", "")
            if cn:
                parts = cn.split(".")
                test_steps.add(parts[0] if parts else cn)

        if test_steps:
            label = ", ".join(sorted(test_steps))[:60]
            return "test_suite", label

    sig_matches = re.findall(r"\[sig-([^\]]+)\]", log_text)
    if sig_matches:
        sigs = sorted(set(sig_matches))[:3]
        label = ", ".join(f"sig-{s}" for s in sigs)
        return "sig_test", label

    step_match = re.findall(r"(?:step|container|pod).*?[\"']([^\"']+)[\"'].*(?:fail|error)", log_text, re.IGNORECASE)
    if step_match:
        label = step_match[-1][:50]
        return "ci_step", label

    for pattern, label in INFRA_LAYER_PATTERNS:
        if re.search(pattern, log_text, re.IGNORECASE):
            return label.lower().replace(" ", "_"), label

    job_parts = job_name.split("-")
    keywords = [p for p in job_parts if p not in
                ("periodic", "ci", "openshift", "release", "main", "nightly",
                 "e2e", "ovn", "from", "stable", "upgrade", "aws", "metal",
                 "ipi", "bm", "single", "node")]
    if keywords:
        label = "-".join(keywords[-3:])[:40]
        return "job_specific", label

    return "unknown", "Unknown"


def get_layer_badge(layer_name: str, layer_label: str = "") -> str:
    """Generate an HTML badge for any test layer."""
    if not layer_label:
        layer_label = layer_name.replace("_", " ").title()

    color_idx = hash(layer_name) % len(LAYER_COLORS)
    color = LAYER_COLORS[color_idx]

    return (f'<span style="background:{color};color:white;'
            f'padding:2px 8px;border-radius:4px;font-size:11px">'
            f'{layer_label}</span>')


# ---------------------------------------------------------------------------
# AI failure analysis
# ---------------------------------------------------------------------------

INFRA_PATTERNS = [
    (r"cluster.*install.*(?:timed? ?out|fail)", "Cluster installation failed"),
    (r"node.*NotReady", "Node not ready"),
    (r"context deadline exceeded", "Timeout — context deadline exceeded"),
    (r"error.*creating.*cluster", "Cluster creation error"),
    (r"unable to connect to the server", "Cannot connect to cluster API"),
    (r"etcd.*not.*ready", "etcd not ready"),
    (r"pod.*CrashLoopBackOff", "Pod crash loop"),
    (r"quota.*exceeded", "Resource quota exceeded"),
    (r"lease.*expired", "Lease expired"),
    (r"failed to pull image", "Image pull failed"),
    (r"ImagePullBackOff", "Image pull backoff"),
    (r"timed out waiting for the condition", "Timed out waiting for condition"),
    (r"no suitable.*node", "No suitable node for scheduling"),
    (r"i/o timeout", "Network I/O timeout"),
    (r"connection refused", "Connection refused"),
    (r"connection reset by peer", "Connection reset by peer"),
    (r"TLS handshake timeout", "TLS handshake timeout"),
    (r"insufficient.*(?:cpu|memory|resources)", "Insufficient cluster resources"),
    (r"cloud provider.*error", "Cloud provider error"),
    (r"aws.*error|ec2.*error", "AWS infrastructure error"),
    (r"failed to create.*machine", "Machine creation failed"),
    (r"clusteroperator.*degraded", "Cluster operator degraded"),
    (r"must-gather", "Cluster in error state (must-gather triggered)"),
]

TEST_PATTERNS = [
    (r"FAIL:\s+(Test\S+)", "test_failure"),
    (r"FAIL\s+\[.*?\]\s+(.+?)(?:\s+\[)", "test_failure"),
    (r"\[FAIL\]\s+(.+)", "test_failure"),
    (r"Error:.*?expected.*?(?:but got|to equal|to match)", "test_failure"),
    (r"(?:assert|expect).*?fail", "test_failure"),
]


def classify_failure(log_text: str,
                     matrix_diff: dict | None = None) -> tuple[str, str]:
    """Pattern-based classification with extracted details. Returns (category, reason).

    Priority order: matrix mismatch > build error > test failure > infra > unknown.
    Infra patterns are checked LAST because generic phrases like "Node NotReady"
    often appear in normal test flow (e.g. commatrix nftables reboot test).
    """
    if matrix_diff and matrix_diff.get("is_matrix_mismatch"):
        return "matrix_mismatch", f"Matrix mismatch: {matrix_diff['summary']}"

    matrix_patterns = [
        r"ports are (?:documented but are not used|used but are not documented)",
        r"generated communication matrix should be equal to documented",
        r"matrix.*(?:mismatch|not equal|differ)",
        r"unexpected.*port|expected.*port.*missing",
    ]
    for pat in matrix_patterns:
        if re.search(pat, log_text, re.IGNORECASE):
            diff = parse_matrix_diff(log_text)
            if diff["is_matrix_mismatch"]:
                return "matrix_mismatch", f"Matrix mismatch: {diff['summary']}"
            return "matrix_mismatch", "Communication matrix mismatch — ports changed"

    if re.search(r"go.*build.*fail|compile.*error|cannot find package", log_text, re.IGNORECASE):
        err_match = re.search(r"(.*(?:build|compile|cannot find).*)", log_text, re.IGNORECASE)
        detail = err_match.group(1).strip()[:150] if err_match else ""
        return "build_error", f"Build error: {detail}" if detail else "Build/compile error"

    for pattern, _ in TEST_PATTERNS:
        match = re.search(pattern, log_text, re.IGNORECASE)
        if match:
            test_name = match.group(1).strip()[:100] if match.lastindex else ""
            return "test_failure", f"Test failed: {test_name}" if test_name else "Test assertion failed"

    if re.search(r"FAIL", log_text):
        fail_lines = [l.strip() for l in log_text.splitlines() if "FAIL" in l and len(l.strip()) > 5]
        if fail_lines:
            return "test_failure", f"Test failed: {fail_lines[-1][:150]}"

    for pattern, reason in INFRA_PATTERNS:
        if re.search(pattern, log_text, re.IGNORECASE):
            return "infra", reason

    error_lines = [l.strip() for l in log_text.splitlines()
                   if re.search(r"(?:error|fatal|panic):", l, re.IGNORECASE)
                   and len(l.strip()) > 10]
    if error_lines:
        last_errors = error_lines[-3:]
        summary = "; ".join(e[:100] for e in last_errors)
        return "error", f"Errors found: {summary[:250]}"

    return "unknown", _extract_last_meaningful_lines(log_text)


def _extract_last_meaningful_lines(log_text: str) -> str:
    """Extract the last few meaningful lines from the log as a fallback summary."""
    lines = [l.strip() for l in log_text.splitlines()
             if l.strip() and len(l.strip()) > 15
             and not l.strip().startswith(("#", "//", "---"))]
    if not lines:
        return "No meaningful output found in logs"
    last_lines = lines[-5:]
    return "Last log lines: " + " | ".join(l[:80] for l in last_lines)[:300]


def _get_ai_provider() -> tuple[str, str, str]:
    """Determine which AI provider to use. Returns (provider, api_key, model)."""
    if AI_PROVIDER == "claude" and ANTHROPIC_API_KEY:
        return "claude", ANTHROPIC_API_KEY, AI_MODEL or "claude-sonnet-4-20250514"
    if AI_PROVIDER == "openai" and OPENAI_API_KEY:
        return "openai", OPENAI_API_KEY, AI_MODEL or "gpt-4o-mini"
    if AI_PROVIDER == "gemini" and GEMINI_API_KEY:
        return "gemini", GEMINI_API_KEY, AI_MODEL or "gemini-2.0-flash"
    if ANTHROPIC_API_KEY:
        return "claude", ANTHROPIC_API_KEY, AI_MODEL or "claude-sonnet-4-20250514"
    if GEMINI_API_KEY:
        return "gemini", GEMINI_API_KEY, AI_MODEL or "gemini-2.0-flash"
    if OPENAI_API_KEY:
        return "openai", OPENAI_API_KEY, AI_MODEL or "gpt-4o-mini"
    return "", "", ""


def ai_analyze_failure(job: dict, log_text: str) -> str:
    """Use an LLM (Claude or OpenAI) to analyze the failure log."""
    provider, api_key, model = _get_ai_provider()
    if not provider:
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
        if provider == "claude":
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["content"][0]["text"].strip()
            else:
                log.warning("Claude analysis failed: HTTP %d — %s", resp.status_code, resp.text[:200])
                return ""
        elif provider == "gemini":
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 800, "temperature": 0.2},
                },
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
                return ""
            else:
                log.warning("Gemini analysis failed: HTTP %d — %s", resp.status_code, resp.text[:200])
                return ""
        else:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
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
                log.warning("OpenAI analysis failed: HTTP %d", resp.status_code)
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
    """Generate the HTML dashboard using template."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total = len(jobs)
    passed = sum(1 for j in jobs if j["state"] == "success")
    failed = sum(1 for j in jobs if j["state"] in ("failure", "error"))
    pending = sum(1 for j in jobs if j["state"] == "pending")
    pass_rate = int(passed / max(passed + failed, 1) * 100)
    rate_color = "green" if pass_rate >= 80 else "yellow" if pass_rate >= 50 else "red"

    ai_provider, _, ai_model = _get_ai_provider()
    ai_status = f"{ai_provider} ({ai_model})" if ai_provider else "disabled"

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
                "infra": '<span class="badge badge-infra">INFRA</span>',
                "test_failure": '<span class="badge badge-test">TEST FAILURE</span>',
                "build_error": '<span class="badge badge-build">BUILD ERROR</span>',
                "matrix_mismatch": '<span class="badge badge-matrix">MATRIX MISMATCH</span>',
                "error": '<span class="badge badge-error">ERROR</span>',
                "unknown": '<span class="badge badge-unknown">UNKNOWN</span>',
            }.get(category, "")

            layer_badge = get_layer_badge(analysis.get("layer", ""), analysis.get("layer_label", ""))
            analysis_html = f"{layer_badge} {cat_badge} {reason}"

            mdiff = analysis.get("matrix_diff", {})
            if mdiff.get("is_matrix_mismatch"):
                diff_html = '<div style="font-size:13px;margin:8px 0">'
                if mdiff.get("undocumented_ports"):
                    diff_html += '<div style="margin-bottom:8px"><strong style="color:#f85149">Ports used but NOT documented (need to add):</strong><ul style="margin:4px 0;padding-left:16px">'
                    for p in mdiff["undocumented_ports"][:10]:
                        diff_html += f'<li><code style="color:#f0883e">{p}</code></li>'
                    diff_html += '</ul></div>'
                if mdiff.get("stale_ports"):
                    diff_html += '<div><strong style="color:#d29922">Ports documented but no longer used (can remove):</strong><ul style="margin:4px 0;padding-left:16px">'
                    for p in mdiff["stale_ports"][:10]:
                        diff_html += f'<li><code style="color:#8b949e">{p}</code></li>'
                    diff_html += '</ul></div>'
                diff_html += '</div>'
                analysis_html += f'<details><summary>Matrix Diff Details</summary><div>{diff_html}</div></details>'

            junit_failures = analysis.get("junit_failures", [])
            if junit_failures:
                test_list = "".join(
                    f'<li><code>{f["name"][:120]}</code><br><small style="color:#8b949e">{f["message"][:200]}</small></li>'
                    for f in junit_failures[:5]
                )
                more = f"<li><em>...and {len(junit_failures)-5} more</em></li>" if len(junit_failures) > 5 else ""
                analysis_html += f'<details><summary>Failed Tests ({len(junit_failures)})</summary><div><ul style="font-size:13px;margin:8px 0">{test_list}{more}</ul></div></details>'

            if ai_summary:
                analysis_html += f'<details><summary>AI Analysis</summary><div style="font-size:13px;color:#c9d1d9;margin:8px 0;white-space:pre-wrap">{ai_summary}</div></details>'

            pr_url_fix = analysis.get("pr_url", "")
            if pr_url_fix:
                analysis_html += f'<div style="margin-top:4px"><span class="badge badge-fix">AUTO-FIX</span> <a href="{pr_url_fix}" target="_blank">PR Created</a></div>'

        rows.append(
            f'<tr data-state="{state}">'
            f'<td>{emoji} {state}</td>'
            f'<td><span class="version-badge">{version}</span></td>'
            f'<td><a href="{url}" target="_blank" title="{job["name"]}">{name_short}</a></td>'
            f'<td class="duration">{duration}</td>'
            f'<td>{analysis_html}</td>'
            f'<td class="duration">{job["start_time"][:16] if job["start_time"] else ""}</td>'
            f'</tr>'
        )

    version_buttons = " ".join(
        f'<button class="filter-btn" onclick="filterVersion(\'{v}\', this)">{v}</button>'
        for v in versions
    )

    template_path = Path(__file__).parent / "template.html"
    if template_path.exists():
        html = template_path.read_text()
    else:
        log.warning("template.html not found, using basic output")
        html = "<html><body><h1>Prow Monitor</h1>{{TABLE_ROWS}}</body></html>"

    replacements = {
        "{{JOB_FILTER}}": JOB_FILTER,
        "{{NOW}}": now,
        "{{AI_STATUS}}": ai_status,
        "{{TOTAL}}": str(total),
        "{{PASSED}}": str(passed),
        "{{FAILED}}": str(failed),
        "{{PENDING}}": str(pending),
        "{{PASS_RATE}}": str(pass_rate),
        "{{RATE_COLOR}}": rate_color,
        "{{TREND_HTML}}": trend_html,
        "{{VERSION_BUTTONS}}": version_buttons,
        "{{TABLE_ROWS}}": "\n".join(rows),
        "{{PROW_URL}}": PROW_URL,
    }
    for key, value in replacements.items():
        html = html.replace(key, value)

    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Prow Nightly Monitor starting")
    log.info("Job filter: %s", JOB_FILTER)
    log.info("Min version: %s", MIN_VERSION or "(all)")
    ai_provider, _, ai_model = _get_ai_provider()
    log.info("AI analysis: %s", f"{ai_provider} ({ai_model})" if ai_provider else "disabled")

    jobs = fetch_prow_jobs()
    jobs = filter_by_version(jobs)
    jobs = get_latest_per_job(jobs)
    log.info("Tracking %d unique jobs", len(jobs))

    analyses = {}
    failed_jobs = [j for j in jobs if j["state"] in ("failure", "error")]
    log.info("Analyzing %d failed jobs", len(failed_jobs))

    for job in failed_jobs:
        log.info("Fetching log for: %s", job["name"])
        build_log = fetch_failure_log(job)

        log.info("  Fetching JUnit results...")
        junit_failures = fetch_junit_results(job)

        step_logs: dict[str, str] = {}
        if junit_failures:
            log.info("  Found %d test failures in JUnit:", len(junit_failures))
            for jf in junit_failures[:5]:
                test_name = jf.get("name", "?")[:80]
                msg_preview = jf.get("message", "")[:100].replace("\n", " ")
                log.info("    - %s: %s", test_name, msg_preview)

            log.info("  Fetching step-specific logs...")
            step_logs = fetch_failed_step_logs(job, junit_failures)

        analysis_log = "\n".join(step_logs.values()) if step_logs else build_log

        matrix_diff = parse_matrix_diff(analysis_log)
        if not matrix_diff["is_matrix_mismatch"]:
            for jf in junit_failures:
                matrix_diff = parse_matrix_diff(jf.get("message", ""))
                if matrix_diff["is_matrix_mismatch"]:
                    break

        if matrix_diff["is_matrix_mismatch"]:
            log.info("  Matrix diff detected: %s", matrix_diff["summary"])
            category = "matrix_mismatch"
            reason = f"Matrix mismatch: {matrix_diff['summary']}"
        elif junit_failures:
            real_test_failures = [
                f for f in junit_failures
                if "container test" in f.get("name", "").lower()
            ]
            if not real_test_failures:
                real_test_failures = [
                    f for f in junit_failures
                    if "test" in f.get("name", "").lower()
                ]

            if real_test_failures:
                category, reason = classify_failure(analysis_log, matrix_diff)
                if category == "unknown":
                    f0 = real_test_failures[0]
                    ginkgo_match = re.search(
                        r"\[FAIL\]\s*(.+?)(?:\n|$)", f0.get("message", "")
                    )
                    if ginkgo_match:
                        fail_detail = ginkgo_match.group(1).strip()[:150]
                    else:
                        fail_detail = f0.get("name", "")[:100]
                    category = "test_failure"
                    reason = f"Test failed: {fail_detail}"
            else:
                category, reason = classify_failure(analysis_log, matrix_diff)
                if category == "unknown" and junit_failures:
                    category = "test_failure"
                    reason = f"{len(junit_failures)} step(s) failed: {junit_failures[0]['name'][:60]}"
        else:
            category, reason = classify_failure(analysis_log, matrix_diff)

        layer_name, layer_label = categorize_test_layer(
            analysis_log, job["name"], junit_failures, category,
        )
        log.info("  Layer: %s (%s), Classification: %s — %s",
                 layer_label, layer_name, category, reason[:80])

        ai_log = analysis_log if analysis_log else build_log
        ai_summary = ""
        if (OPENAI_API_KEY or ANTHROPIC_API_KEY or GEMINI_API_KEY) and ai_log and not ai_log.startswith("("):
            log.info("  Running AI analysis (waiting 10s for rate limit)...")
            time.sleep(10)
            ai_summary = ai_analyze_failure(job, ai_log)
            if ai_summary:
                log.info("  AI: %s", ai_summary[:200])
            else:
                log.info("  AI failed, retrying in 30s...")
                time.sleep(30)
                ai_summary = ai_analyze_failure(job, ai_log)
                if ai_summary:
                    log.info("  AI (retry): %s", ai_summary[:200])
                else:
                    log.warning("  AI analysis unavailable, using pattern classification only")

        pr_url = ""
        if AUTO_FIX and category not in ("infra",):
            log.info("  Attempting auto-fix...")
            pr_url = attempt_auto_fix(job, category, analysis_log, ai_summary)

        analyses[job["name"]] = {
            "category": category,
            "reason": reason,
            "layer": layer_name,
            "layer_label": layer_label,
            "ai_summary": ai_summary,
            "junit_failures": junit_failures,
            "matrix_diff": matrix_diff if matrix_diff.get("is_matrix_mismatch") else {},
            "pr_url": pr_url,
            "log_snippet": analysis_log[-500:] if analysis_log else "",
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
