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
HF_API_KEY = os.environ.get("HF_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
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
    """For each unique job, keep the latest run AND the last completed run.

    If the latest run is still pending/triggered, we also include the most
    recent completed run so the user doesn't lose its analysis.
    """
    latest: dict[str, dict] = {}
    last_completed: dict[str, dict] = {}

    for job in jobs:
        name = job["name"]
        if name not in latest or job["start_time"] > latest[name]["start_time"]:
            latest[name] = job

        if job["state"] in ("success", "failure", "error", "aborted"):
            if name not in last_completed or job["start_time"] > last_completed[name]["start_time"]:
                last_completed[name] = job

    result = []
    seen_ids = set()
    for name, job in latest.items():
        result.append(job)
        job_id = f"{job['name']}_{job['start_time']}"
        seen_ids.add(job_id)

        if job["state"] in ("pending", "triggered"):
            completed = last_completed.get(name)
            if completed:
                comp_id = f"{completed['name']}_{completed['start_time']}"
                if comp_id not in seen_ids:
                    seen_ids.add(comp_id)
                    result.append(completed)

    return sorted(result, key=lambda j: (j["name"], j["start_time"]), reverse=False)


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

    Detects all variants of port mismatch messages:
      'ports are documented but are not used'
      'ports are used but are not documented'
      'ports are not used'  (shorter form)
      'ports are used but don't have an endpointslice'

    Returns dict with is_matrix_mismatch, undocumented_ports, stale_ports,
    no_endpointslice_ports, summary.
    """
    result: dict = {
        "is_matrix_mismatch": False,
        "undocumented_ports": [],
        "stale_ports": [],
        "no_endpointslice_ports": [],
        "summary": "",
    }

    def _extract_ports(pattern: str) -> list[str]:
        """Try both escaped-newline and real-newline variants."""
        m = re.search(pattern + r"\s*\\n((?:[^\n\"]+\\n)*)", log_text)
        if not m:
            m = re.search(pattern + r"\s*\n((?:.*\n)*?)\s*(?:\"|{|\[|$)", log_text)
        if not m:
            m = re.search(pattern + r"\s*\n\s*((?:\S+,\S+.*\n?)*)", log_text)
        if m:
            raw = m.group(1).replace("\\n", "\n").strip()
            return [l.strip() for l in raw.splitlines() if l.strip() and "," in l]
        return []

    stale_patterns = [
        r"ports are documented but are not used:",
        r"the following ports are not used:",
    ]
    for pat in stale_patterns:
        ports = _extract_ports(pat)
        if ports:
            result["stale_ports"].extend(ports)

    undoc_patterns = [
        r"ports are used but are not documented:",
    ]
    for pat in undoc_patterns:
        ports = _extract_ports(pat)
        if ports:
            result["undocumented_ports"].extend(ports)

    no_ep_patterns = [
        r"ports are used but don.t have an endpointslice:",
    ]
    for pat in no_ep_patterns:
        ports = _extract_ports(pat)
        if ports:
            result["no_endpointslice_ports"].extend(ports)

    has_mismatch = (
        result["stale_ports"]
        or result["undocumented_ports"]
        or result["no_endpointslice_ports"]
    )
    if has_mismatch:
        result["is_matrix_mismatch"] = True
        parts = []
        if result["undocumented_ports"]:
            parts.append(f"{len(result['undocumented_ports'])} port(s) used but not documented")
        if result["stale_ports"]:
            parts.append(f"{len(result['stale_ports'])} port(s) in matrix but not in use")
        if result["no_endpointslice_ports"]:
            parts.append(f"{len(result['no_endpointslice_ports'])} port(s) open but missing endpointslice")
        result["summary"] = "; ".join(parts)

    return result


# ---------------------------------------------------------------------------
# Parse actual test failures from logs (Ginkgo, Go test, generic)
# ---------------------------------------------------------------------------

def parse_test_failures_from_log(log_text: str) -> list[dict]:
    """Extract individual test failure names and messages from step logs.

    Priority order:
    1. Ginkgo "Summarizing N Failures" section (most reliable, always at end)
    2. Ginkgo [FAIL] blocks with error details
    3. Go test --- FAIL markers
    4. Generic error lines (last resort)

    For each failure, extracts: name, message, test_file, test_line.
    """
    clean_text = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    failures: list[dict] = []
    seen = set()

    # --- Priority 1: Ginkgo summary section (most reliable) ---
    # Look at the LAST part of the log for "Summarizing N Failures"
    summary_sections = re.findall(
        r"Summarizing \d+ Failure.*?\n(.*?)(?=\nRan \d+ of|\Z)",
        clean_text, re.DOTALL,
    )
    for block in summary_sections:
        current_name = ""
        current_file = ""
        for line in block.splitlines():
            stripped = line.strip()
            fail_m = re.match(r"\[FAIL\]\s*(.+)", stripped)
            if fail_m:
                current_name = fail_m.group(1).strip()[:200]
            file_m = re.match(r"([\w/._-]+_test\.go):(\d+)", stripped)
            if file_m:
                current_file = f"{file_m.group(1)}:{file_m.group(2)}"
            if current_name:
                key = current_name[:80]
                if key not in seen:
                    seen.add(key)
                    failures.append({
                        "name": current_name,
                        "message": "",
                        "test_file": current_file,
                    })
                current_name = ""
                current_file = ""

    # --- Priority 2: Ginkgo [FAIL] blocks with error details ---
    fail_blocks = re.split(r"(?=\[FAIL(?:ED)?\])", clean_text)
    for block in fail_blocks:
        if not block.startswith("[FAIL"):
            continue

        name_m = re.match(r"\[FAIL(?:ED)?\]\s*(.+?)(?:\n|$)", block)
        if not name_m:
            continue
        name = name_m.group(1).strip()[:200]
        key = name[:80]
        if key in seen:
            existing = next((f for f in failures if f["name"][:80] == key), None)
            if existing and not existing.get("message"):
                msg = _extract_failure_message(block)
                if msg:
                    existing["message"] = msg
                tf = _extract_test_file(block)
                if tf and not existing.get("test_file"):
                    existing["test_file"] = tf
            continue

        seen.add(key)
        failures.append({
            "name": name,
            "message": _extract_failure_message(block),
            "test_file": _extract_test_file(block),
        })

    # --- Priority 3: Go test failures ---
    go_fails = re.findall(r"--- FAIL:\s+(\S+)\s+\(([^)]+)\)", clean_text)
    for test_name, duration in go_fails:
        if test_name not in seen:
            seen.add(test_name)
            failures.append({
                "name": test_name,
                "message": f"Failed in {duration}",
                "test_file": "",
            })

    # --- Priority 4: Generic errors (only if nothing else found) ---
    if not failures:
        for pat in [
            r"(?:FAILED|FAIL!)\s*[—-]*\s*(.+?)(?:\n|$)",
            r"(?:Error:|panic:)\s*(.+?)(?:\n|$)",
        ]:
            for m in re.finditer(pat, clean_text):
                err = m.group(1).strip()[:200]
                if len(err) > 15 and err not in seen:
                    seen.add(err)
                    failures.append({"name": err, "message": "", "test_file": ""})
                    if len(failures) >= 5:
                        break
            if failures:
                break

    return failures[:20]


def _extract_failure_message(block: str) -> str:
    """Extract the actual error/assertion message from a Ginkgo [FAIL] block."""
    patterns = [
        r"(?:Unexpected error|FAILED).*?:\s*\n?\s*(.+?)(?:\n\s*\n|\n\s*In \[|\n\s*occurred)",
        r"(?:Expected|Got|to equal|to match|to be)\s*(.+?)(?:\n\s*\n|\Z)",
        r"the following ports (?:are used but (?:are not documented|don.t have)|are (?:documented but are not used|not used)).*?:\s*\n?\s*(.+?)(?:\n\s*\n|\Z)",
        r"(?:error|Error):\s*(.+?)(?:\n\s*\n|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, block, re.DOTALL)
        if m:
            msg = m.group(1).strip().replace("\n", " ")[:300]
            if len(msg) > 10:
                return msg
    return ""


def _extract_test_file(block: str) -> str:
    """Extract the test file path and line number from a Ginkgo block."""
    m = re.search(r"([\w/._-]+_test\.go):(\d+)", block)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    m = re.search(r"In \[It\] at:\s*([\w/._-]+\.go):(\d+)", block)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return ""


def fetch_step_junit(job: dict, workflow: str, step: str) -> list[dict]:
    """Try to fetch JUnit XML from inside a step's artifacts subdirectory."""
    url = job.get("url", "")
    m = re.search(r"/logs/(.+)/(\d+)$", url)
    if not m:
        return []
    job_path, build_id = m.group(1), m.group(2)

    junit_paths = [
        f"{GCS_BASE}/{job_path}/{build_id}/artifacts/{workflow}/{step}/artifacts/junit/junit.xml",
        f"{GCS_BASE}/{job_path}/{build_id}/artifacts/{workflow}/{step}/artifacts/junit/e2e.xml",
    ]
    for junit_url in junit_paths:
        try:
            resp = requests.get(junit_url, timeout=10)
            if resp.status_code == 200 and ("<?xml" in resp.text[:200] or "<test" in resp.text[:200]):
                results = _parse_junit_xml(resp.text)
                if results:
                    return results
        except Exception:
            pass

    artifacts_url = f"{GCS_BASE}/{job_path}/{build_id}/artifacts/{workflow}/{step}/artifacts/"
    try:
        resp = requests.get(artifacts_url, timeout=10)
        if resp.status_code == 200:
            xml_files = re.findall(r'href="[^"]*?(junit[^"]*\.xml)"', resp.text)
            if not xml_files:
                xml_files = re.findall(r'href="[^"]*?([^/"]+\.xml)"', resp.text)
            for xf in xml_files[:3]:
                xf_url = f"{artifacts_url}{xf}" if not xf.startswith("http") else xf
                try:
                    xresp = requests.get(xf_url, timeout=10)
                    if xresp.status_code == 200:
                        results = _parse_junit_xml(xresp.text)
                        if results:
                            return results
                except Exception:
                    pass
    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# Failure investigation & fix suggestion
# ---------------------------------------------------------------------------

def investigate_failure(job: dict, category: str, reason: str,
                        matrix_diff: dict, junit_failures: list[dict],
                        step_logs: dict[str, str],
                        build_log: str) -> dict:
    """Produce a structured investigation report from the actual logs.

    Fully generic — extracts all information from the log output itself,
    never assumes a specific repo structure or file layout.
    """
    report: dict = {
        "summary": "",
        "root_cause": "",
        "failed_tests": [],
        "source_files": [],
        "error_output": [],
        "suggested_fix": "",
        "severity": "MEDIUM",
        "fix_type": "",
    }

    analysis_text = "\n".join(step_logs.values()) if step_logs else build_log

    # --- Extract actual test failures from step logs ---
    for step_name, step_log in step_logs.items():
        parsed = parse_test_failures_from_log(step_log)
        for tf in parsed:
            report["failed_tests"].append({
                "step": step_name,
                "name": tf["name"],
                "message": tf["message"],
            })

    # --- If no failures found in step logs, try step-level JUnit XMLs ---
    if not report["failed_tests"]:
        for jf in junit_failures:
            jf_name = jf.get("name", "")
            workflow, step = _extract_step_info(jf_name)
            if workflow and step:
                step_junits = fetch_step_junit(job, workflow, step)
                for sj in step_junits:
                    report["failed_tests"].append({
                        "step": step,
                        "name": sj.get("name", "unknown")[:200],
                        "message": sj.get("message", "")[:300],
                    })

    # --- Last resort: parse JUnit failure messages ---
    if not report["failed_tests"]:
        for jf in junit_failures:
            jf_name = jf.get("name", "")
            if "container test" not in jf_name.lower() and "test phase" not in jf_name.lower():
                continue
            _, step = _extract_step_info(jf_name)
            msg = jf.get("message", "")
            parsed = parse_test_failures_from_log(msg)
            if parsed:
                for tf in parsed:
                    report["failed_tests"].append({
                        "step": step or jf_name[:60],
                        "name": tf["name"],
                        "message": tf["message"],
                    })
            else:
                clean_msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
                err_m = re.search(r"(?:Unexpected error|FAILED|Error:)\s*(.+?)(?:\n\n|\Z)", clean_msg, re.DOTALL)
                report["failed_tests"].append({
                    "step": step or jf_name[:60],
                    "name": step or "unknown",
                    "message": (err_m.group(1).strip()[:300] if err_m else clean_msg[:200]).replace("\n", " "),
                })

    # --- Extract source file references from logs ---
    file_refs = re.findall(
        r"([\w/._-]+\.(?:go|py|yaml|yml|json|sh|csv|xml)):(\d+)",
        analysis_text,
    )
    seen_files = set()
    for fpath, line_no in file_refs:
        if fpath.startswith("/tmp/") or fpath.startswith("vendor/"):
            base = fpath.split("/")[-1] if "/" in fpath else fpath
            key = f"{base}:{line_no}"
        else:
            key = f"{fpath}:{line_no}"
        if key not in seen_files:
            seen_files.add(key)
            report["source_files"].append({"file": fpath, "line": line_no})

    # --- Extract error output lines from logs ---
    error_lines = []
    for line in analysis_text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) < 10:
            continue
        is_error = bool(re.search(
            r"\[FAIL\]|Unexpected error|FAILED|panic:|fatal:|"
            r"Error:|level=error|level=warning.*(?:not used|not documented|mismatch|fail)",
            stripped, re.IGNORECASE,
        ))
        if is_error:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", stripped)[:300]
            if clean not in error_lines:
                error_lines.append(clean)
    report["error_output"] = error_lines[-10:]

    # --- Category-specific analysis (all from the logs, nothing hardcoded) ---

    if category == "matrix_mismatch" and matrix_diff.get("is_matrix_mismatch"):
        report["severity"] = "HIGH"
        report["fix_type"] = "data_update"

        undoc = matrix_diff.get("undocumented_ports", [])
        stale = matrix_diff.get("stale_ports", [])
        no_ep = matrix_diff.get("no_endpointslice_ports", [])

        parts = []
        if undoc:
            parts.append(f"{len(undoc)} port(s) used but not documented")
        if stale:
            parts.append(f"{len(stale)} port(s) in matrix but not in use")
        if no_ep:
            parts.append(f"{len(no_ep)} port(s) open but missing EndpointSlice")
        report["summary"] = ". ".join(parts)

        root_parts = []
        if undoc:
            root_parts.append(
                "Ports used but not documented: " + "; ".join(undoc[:5])
            )
        if stale:
            root_parts.append(
                "Ports in matrix but not open on nodes: " + "; ".join(stale[:5])
            )
        if no_ep:
            root_parts.append(
                "Ports open on nodes but have no EndpointSlice: " + "; ".join(no_ep[:5])
            )
        report["root_cause"] = ". ".join(root_parts)

        fix_lines = ["Update the matrix or investigate the port discrepancies:", ""]
        if undoc:
            fix_lines.append("ADD to documented matrix (ports now in use):")
            for p in undoc:
                fix_lines.append(f"  {p}")
            fix_lines.append("")
        if stale:
            fix_lines.append("REMOVE from matrix (ports no longer in use):")
            for p in stale:
                fix_lines.append(f"  {p}")
            fix_lines.append("")
        if no_ep:
            fix_lines.append("INVESTIGATE — ports open on node but no EndpointSlice found:")
            for p in no_ep:
                fix_lines.append(f"  {p}")
            fix_lines.append("These may be new services that need EndpointSlice resources,")
            fix_lines.append("or ephemeral ports (like CRI-O) that should be added to static entries.")
            fix_lines.append("")
        report["suggested_fix"] = "\n".join(fix_lines)

    elif category == "test_failure":
        report["fix_type"] = "test_investigation"

        if report["failed_tests"]:
            test_names = [t.get("name", t.get("step", "?")) for t in report["failed_tests"][:3]]
            report["summary"] = f"{len(report['failed_tests'])} test(s) failed: {'; '.join(test_names)}"

            root_parts = []
            for t in report["failed_tests"][:3]:
                name = t.get("name", "")
                msg = t.get("message", "")
                if msg:
                    root_parts.append(f"{name}: {msg}"[:200])
                elif name:
                    root_parts.append(name[:200])
            report["root_cause"] = "; ".join(root_parts)[:600] if root_parts else reason
        else:
            report["summary"] = reason
            report["root_cause"] = reason

        fix_parts = []
        if report["failed_tests"]:
            fix_parts.append(
                f"{len(report['failed_tests'])} test(s) failed:"
            )
            for t in report["failed_tests"][:5]:
                name = t.get("name", t.get("step", "?"))
                msg = t.get("message", "")
                fix_parts.append(f"  - {name}")
                if msg:
                    fix_parts.append(f"    {msg[:150]}")
            fix_parts.append("")
        if report["source_files"]:
            fix_parts.append(
                "Source references: "
                + ", ".join(f"{s['file']}:{s['line']}" for s in report["source_files"][:5])
            )
        fix_parts.append(
            "Check if this is a flake (retry the job) or a real regression "
            "from a recent commit."
        )
        report["suggested_fix"] = "\n".join(fix_parts)

    elif category == "build_error":
        report["severity"] = "HIGH"
        report["fix_type"] = "build_fix"
        report["summary"] = reason

        compile_errs = re.findall(
            r"(.*(?:cannot find|undefined|syntax error|build.*fail|"
            r"import cycle|cannot load|no required module).*)",
            analysis_text, re.IGNORECASE,
        )
        if compile_errs:
            report["root_cause"] = "; ".join(
                e.strip()[:150] for e in compile_errs[-3:]
            )

        fix_parts = ["Fix the build/compilation errors:"]
        if report["source_files"]:
            fix_parts.append(
                "Files with errors: "
                + ", ".join(f"{s['file']}:{s['line']}" for s in report["source_files"][:5])
            )
        fix_parts.append("Reproduce locally with 'go build ./...' or 'make build'.")
        if "go.mod" in analysis_text.lower() or "go.sum" in analysis_text.lower():
            fix_parts.append("Try 'go mod tidy' if this is a dependency issue.")
        report["suggested_fix"] = "\n".join(fix_parts)

    elif category == "infra":
        report["severity"] = "LOW"
        report["fix_type"] = "infra"
        report["summary"] = reason
        report["root_cause"] = reason
        report["suggested_fix"] = (
            "Infrastructure issue — likely transient. Retry the job.\n"
            "If this recurs across multiple runs, check:\n"
            "  - Cloud quotas and limits\n"
            "  - Cluster provisioning config\n"
            "  - Network connectivity"
        )

    else:
        report["fix_type"] = "unknown"
        report["summary"] = reason
        report["root_cause"] = reason
        report["suggested_fix"] = (
            "Manual investigation required.\n"
            "Check the full Prow logs and error output below for details."
        )

    return report


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
      <h3 style="font-size:14px;color:#8b949e;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px">Daily Status by Version</h3>
      <div style="display:flex;gap:16px;margin-bottom:12px;font-size:11px;color:#8b949e">
        <span><span style="display:inline-block;width:10px;height:10px;background:#3fb950;border-radius:50%;margin-right:4px"></span>All passed</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#f85149;border-radius:50%;margin-right:4px"></span>Has failures</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#21262d;border:1px solid #30363d;border-radius:50%;margin-right:4px"></span>No data</span>
        <span style="color:#484f58">← Each dot = one day (newest on right)</span>
      </div>
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
        r"the following ports are not used:",
        r"ports are used but don.t have an endpointslice",
        r"generated communication matrix should be equal to documented",
        r"communication matrix ports match the node.s open ports",
        r"matrix.*(?:mismatch|not equal|differ)",
        r"unexpected.*port|expected.*port.*missing",
    ]
    for pat in matrix_patterns:
        if re.search(pat, log_text, re.IGNORECASE):
            diff = parse_matrix_diff(log_text)
            if diff["is_matrix_mismatch"]:
                return "matrix_mismatch", f"Matrix mismatch: {diff['summary']}"
            return "matrix_mismatch", "Communication matrix mismatch — ports changed"

    parsed_failures = parse_test_failures_from_log(log_text)
    if parsed_failures:
        first = parsed_failures[0]
        name = first.get("name", "")
        msg = first.get("message", "")
        test_file = first.get("test_file", "")
        detail = name
        if msg:
            detail += f": {msg}"
        if test_file:
            detail += f" ({test_file})"
        return "test_failure", f"Test failed: {detail[:250]}"

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
    if AI_PROVIDER == "huggingface" and HF_API_KEY:
        return "huggingface", HF_API_KEY, AI_MODEL or "meta-llama/Meta-Llama-3.1-70B-Instruct"
    if AI_PROVIDER == "groq" and GROQ_API_KEY:
        return "groq", GROQ_API_KEY, AI_MODEL or "llama-3.3-70b-versatile"
    if AI_PROVIDER == "cerebras" and CEREBRAS_API_KEY:
        return "cerebras", CEREBRAS_API_KEY, AI_MODEL or "llama-3.3-70b"
    if AI_PROVIDER == "deepseek" and DEEPSEEK_API_KEY:
        return "deepseek", DEEPSEEK_API_KEY, AI_MODEL or "deepseek-chat"
    if ANTHROPIC_API_KEY:
        return "claude", ANTHROPIC_API_KEY, AI_MODEL or "claude-sonnet-4-20250514"
    if GROQ_API_KEY:
        return "groq", GROQ_API_KEY, AI_MODEL or "llama-3.3-70b-versatile"
    if CEREBRAS_API_KEY:
        return "cerebras", CEREBRAS_API_KEY, AI_MODEL or "llama-3.3-70b"
    if DEEPSEEK_API_KEY:
        return "deepseek", DEEPSEEK_API_KEY, AI_MODEL or "deepseek-chat"
    if HF_API_KEY:
        return "huggingface", HF_API_KEY, AI_MODEL or "meta-llama/Meta-Llama-3.1-70B-Instruct"
    if OPENAI_API_KEY:
        return "openai", OPENAI_API_KEY, AI_MODEL or "gpt-4o-mini"
    if GEMINI_API_KEY:
        return "gemini", GEMINI_API_KEY, AI_MODEL or "gemini-2.0-flash"
    return "", "", ""


def _fetch_test_source(job: dict, test_files: list[dict]) -> str:
    """Fetch relevant test source code from GitHub for the failed test.

    Looks at the test file references from the logs (e.g., validation_test.go:161)
    and fetches the relevant function from the repo.
    """
    repo_url = _guess_repo_from_job(job.get("name", ""))
    if not repo_url:
        return ""

    repo_slug = repo_url.replace("https://github.com/", "")
    source_parts = []

    for tf in test_files[:2]:
        filepath = tf.get("file", "")
        if not filepath or "_test.go" not in filepath:
            continue

        base = filepath.split("/")[-1] if "/" in filepath else filepath
        search_paths = [
            f"test/e2e/{base}",
            f"test/{base}",
            base,
        ]

        for sp in search_paths:
            try:
                raw_url = f"https://raw.githubusercontent.com/{repo_slug}/main/{sp}"
                resp = requests.get(raw_url, timeout=10)
                if resp.status_code == 200:
                    lines = resp.text.splitlines()
                    line_no = int(tf.get("line", 0))
                    if line_no > 0:
                        start = max(0, line_no - 20)
                        end = min(len(lines), line_no + 30)
                        snippet = "\n".join(lines[start:end])
                    else:
                        snippet = "\n".join(lines[:80])
                    source_parts.append(f"--- {sp} (lines {start+1}-{end}) ---\n{snippet}")
                    break
            except Exception:
                continue

    return "\n\n".join(source_parts)[:2000] if source_parts else ""


def _fetch_artifacts_context(job: dict, category: str,
                             matrix_diff: dict,
                             step_logs: dict[str, str]) -> dict:
    """Fetch targeted artifacts based on failure type for deeper investigation.

    Returns dict with: ss_findings, stale_info, junit_findings, text_summary.
    """
    result: dict = {"ss_findings": [], "stale_info": [], "junit_findings": [], "text_summary": ""}
    url = job.get("url", "")
    m = re.search(r"/logs/(.+)/(\d+)$", url)
    if not m:
        return result
    job_path, build_id = m.group(1), m.group(2)

    parts = []

    if category == "matrix_mismatch":
        no_ep = matrix_diff.get("no_endpointslice_ports", [])
        if no_ep:
            job_name = job.get("name", "")
            name_short = job_name.replace("periodic-ci-openshift-release-main-nightly-", "")
            wf_parts = name_short.split("-")
            ver_idx = next((i for i, p in enumerate(wf_parts) if re.match(r"\d+\.\d+", p)), -1)
            wf_name = "-".join(wf_parts[ver_idx + 1:]) if ver_idx >= 0 else name_short

            ss_paths = [
                f"{GCS_BASE}/{job_path}/{build_id}/artifacts/{wf_name}/network-flow-matrix-tests/artifacts/raw-ss-tcp",
                f"{GCS_BASE}/{job_path}/{build_id}/artifacts/{wf_name}/network-flow-matrix-tests/artifacts/commatrix/raw-ss-tcp",
            ]
            for step in step_logs:
                if "network-flow-matrix" in step:
                    ss_paths.insert(0, f"{GCS_BASE}/{job_path}/{build_id}/artifacts/{wf_name}/{step}/artifacts/raw-ss-tcp")

            for ss_url in ss_paths:
                try:
                    resp = requests.get(ss_url, timeout=10)
                    if resp.status_code == 200:
                        log.info("  Fetched ss output from artifacts")
                        for port_entry in no_ep:
                            port_fields = port_entry.split(",")
                            if len(port_fields) >= 3:
                                port_num = port_fields[2]
                                for line in resp.text.splitlines():
                                    if f":{port_num}" in line:
                                        clean_line = line.strip()
                                        result["ss_findings"].append({
                                            "port": port_num,
                                            "entry": port_entry,
                                            "ss_line": clean_line,
                                        })
                                        parts.append(f"ss output for port {port_num}: {clean_line}")
                        break
                except Exception:
                    pass

        stale = matrix_diff.get("stale_ports", [])
        if stale:
            parts.append(f"Stale ports (in matrix but not on nodes): {len(stale)} entries")
            for p in stale[:3]:
                parts.append(f"  {p}")
                result["stale_info"].append(p)

    result["text_summary"] = "\n".join(parts)[:1500] if parts else ""
    return result


def _extract_failure_context(log_text: str) -> str:
    """Extract just the failure-relevant parts from a CI log.

    Prioritizes Ginkgo summary, [FAIL] blocks, and error lines
    instead of sending the entire log to AI.
    """
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    parts = []

    summary_m = re.search(
        r"(Summarizing \d+ Failure.*?)(?=\nRan \d+ of|\Z)",
        clean, re.DOTALL,
    )
    if summary_m:
        parts.append(summary_m.group(1).strip()[:1000])

    for m in re.finditer(r"(\[FAIL(?:ED)?\].+?)(?=\n-{10,}|\n\[FAIL|\Z)", clean, re.DOTALL):
        block = m.group(1).strip()[:500]
        if block not in "\n".join(parts):
            parts.append(block)

    for m in re.finditer(
        r"((?:ports are (?:documented but are not used|used but are not documented|"
        r"not used)|ports are used but don.t have an endpointslice).*?)(?=\n\s*\[|\n\s*•|\Z)",
        clean, re.DOTALL,
    ):
        parts.append(m.group(1).strip()[:500])

    result_m = re.search(r"((?:FAIL!|Ran \d+ of \d+ Specs).*?$)", clean, re.MULTILINE)
    if result_m:
        parts.append(result_m.group(1).strip()[:200])

    if parts:
        return "\n---\n".join(parts)[:4000]

    return clean[-4000:] if len(clean) > 4000 else clean


def ai_analyze_failure(job: dict, log_text: str,
                       investigation: dict | None = None,
                       category: str = "",
                       matrix_diff: dict | None = None,
                       step_logs: dict[str, str] | None = None) -> str:
    """Use an LLM to analyze the failure log with test source and artifacts."""
    provider, api_key, model = _get_ai_provider()
    if not provider:
        return ""

    log_truncated = _extract_failure_context(log_text)

    extra_context = ""

    if investigation and investigation.get("source_files"):
        source_code = _fetch_test_source(job, investigation["source_files"])
        if source_code:
            log.info("  Fetched test source code for AI context")
            extra_context += f"\n\n**Test Source Code:**\n{source_code}"

    artifacts_data = {}
    if category and matrix_diff and step_logs:
        artifacts_data = _fetch_artifacts_context(job, category, matrix_diff or {}, step_logs or {})
        if artifacts_data.get("text_summary"):
            log.info("  Fetched artifacts context for AI")
            extra_context += f"\n\n**Artifacts:**\n{artifacts_data['text_summary']}"

    source_section = extra_context if extra_context else ""

    prompt = f"""You are a senior CI failure analyst for OpenShift. Analyze this failed CI job and provide a detailed, structured investigation report.

Job: {job['name']}
State: {job['state']}
{source_section}

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
        elif provider in ("groq", "cerebras", "deepseek"):
            api_urls = {
                "groq": "https://api.groq.com/openai/v1/chat/completions",
                "cerebras": "https://api.cerebras.ai/v1/chat/completions",
                "deepseek": "https://api.deepseek.com/chat/completions",
            }
            resp = requests.post(
                api_urls[provider],
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 800,
                    "temperature": 0.2,
                },
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            else:
                log.warning("%s analysis failed: HTTP %d — %s", provider, resp.status_code, resp.text[:200])
                return ""
        elif provider == "huggingface":
            resp = requests.post(
                "https://router.huggingface.co/sambanova/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 800,
                    "temperature": 0.2,
                },
                timeout=120,
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
                return ""
            else:
                log.warning("HuggingFace analysis failed: HTTP %d — %s", resp.status_code, resp.text[:200])
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
# Ollama local AI fallback
# ---------------------------------------------------------------------------

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


def _ollama_analyze(job: dict, log_text: str,
                    investigation: dict | None = None,
                    category: str = "",
                    matrix_diff: dict | None = None,
                    step_logs: dict[str, str] | None = None) -> str:
    """Fallback: use local Ollama for AI analysis when API providers fail."""
    try:
        log_truncated = _extract_failure_context(log_text)
        if len(log_truncated) > 1500:
            log_truncated = log_truncated[:1500]

        extra = ""
        if investigation and investigation.get("source_files"):
            source_code = _fetch_test_source(job, investigation["source_files"])
            if source_code:
                extra += f"\n\nTest source:\n{source_code[:600]}"
        if category and matrix_diff and step_logs:
            artifacts_data = _fetch_artifacts_context(job, category, matrix_diff or {}, step_logs or {})
            if artifacts_data.get("text_summary"):
                extra += f"\n\nArtifacts:\n{artifacts_data['text_summary'][:400]}"
        source_hint = extra

        prompt = (
            f"You are a senior CI failure analyst for OpenShift. Analyze this failure.\n"
            f"Job: {job['name']}\n\n"
            f"Context: This is an OpenShift CI nightly job. Common failures include:\n"
            f"- Matrix mismatch: documented ports don't match actual open ports (from ss command)\n"
            f"- Missing EndpointSlice: a port is open (ss shows it) but no Kubernetes EndpointSlice exists. "
            f"CRI-O uses ephemeral ports that need static entries.\n"
            f"- Cluster setup failure: bare metal provisioning (ofcir) or telco5g cluster setup failed\n"
            f"- Upgrade failures: node not ready after upgrade\n\n"
            f"Respond in this EXACT format:\n\n"
            f"**Failed Tests:**\n- <exact test name from the log>\n\n"
            f"**Failure Messages:**\n- \"<quote the exact error message>\"\n\n"
            f"**Root Cause:**\n- <explain specifically WHY it failed, not just what happened>\n\n"
            f"**Classification:**\n- INFRA / TEST_FAILURE / MATRIX_MISMATCH / BUILD_ERROR\n\n"
            f"**Recommended Action:**\n- <specific steps to fix, e.g. 'add static entry for CRI-O port' or 'retry - infra issue'>\n\n"
            f"**Severity:** CRITICAL / HIGH / MEDIUM / LOW\n\n"
            f"Log:\n{log_truncated}{source_hint}"
        )

        resp = None
        for attempt in range(2):
            try:
                resp = requests.post(
                    "http://localhost:11434/api/chat",
                    json={
                        "model": OLLAMA_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.2, "num_predict": 600},
                    },
                    timeout=90,
                )
                break
            except requests.exceptions.Timeout:
                log.warning("  Ollama timeout (attempt %d), retrying shorter...", attempt + 1)
                log_truncated = log_text[-500:] if len(log_text) > 500 else log_text
                prompt = f"What failed? Be very brief.\n\n{log_truncated}"

        if resp and resp.status_code == 200:
            data = resp.json()
            return data.get("message", {}).get("content", "").strip()
        elif resp:
            log.warning("  Ollama HTTP %d", resp.status_code)
        return ""
    except Exception as e:
        log.warning("  Ollama error: %s", e)
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
    """Generate the HTML dashboard using table layout with category breakdown."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    unique_for_stats: dict[str, dict] = {}
    for j in jobs:
        name = j["name"]
        if name not in unique_for_stats or j["start_time"] > unique_for_stats[name]["start_time"]:
            unique_for_stats[name] = j
    stats_list = list(unique_for_stats.values())

    total = len(stats_list)
    passed = sum(1 for j in stats_list if j["state"] == "success")
    failed = sum(1 for j in stats_list if j["state"] in ("failure", "error"))
    pending = sum(1 for j in stats_list if j["state"] in ("pending", "triggered"))
    pass_rate = int(passed / max(passed + failed, 1) * 100)
    rate_color = "green" if pass_rate >= 80 else "yellow" if pass_rate >= 50 else "red"

    ai_provider, _, ai_model = _get_ai_provider()
    ai_status = f"{ai_provider} ({ai_model})" if ai_provider else "disabled"

    versions = sorted(set(extract_version(j["name"]) for j in jobs if extract_version(j["name"])))

    cat_counts: dict[str, int] = {}
    for j in jobs:
        if j["state"] in ("failure", "error"):
            a = analyses.get(j["name"], {})
            cat = a.get("category", "unknown")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

    cat_labels = {
        "matrix_mismatch": "Matrix Mismatch", "test_failure": "Test Failure",
        "infra": "Infra", "build_error": "Build Error",
        "error": "Error", "unknown": "Unknown",
    }
    cat_css_map = {
        "matrix_mismatch": "matrix", "test_failure": "test",
        "infra": "infra", "build_error": "build",
        "error": "unknown", "unknown": "unknown",
    }

    category_cards = '<div class="categories">'
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        css = cat_css_map.get(cat, "unknown")
        label = cat_labels.get(cat, cat)
        category_cards += (
            f'<div class="cat-card {css}" onclick="filterCategory(\'{cat}\')">'
            f'<div class="cat-count">{count}</div>'
            f'<div class="cat-name">{label}</div></div>'
        )
    if cat_counts:
        category_cards += (
            f'<div class="cat-card" onclick="filterCategory(\'\')" style="border-left:4px solid #58a6ff">'
            f'<div class="cat-count" style="color:#58a6ff">{sum(cat_counts.values())}</div>'
            f'<div class="cat-name">All</div></div>'
        )
    category_cards += '</div>'

    rows = []
    for job in jobs:
        state = job["state"]
        emoji = STATE_EMOJI.get(state, "?")
        version = extract_version(job["name"])
        duration = compute_duration(job)
        name_short = job["name"].replace("periodic-ci-openshift-release-main-nightly-", "")
        url = job["url"] or f'{PROW_URL}/?type=periodic&job={job["name"]}'
        started = job["start_time"][:16] if job.get("start_time") else ""

        analysis = analyses.get(job["name"], {})
        category = analysis.get("category", "")
        inv = analysis.get("investigation", {})
        ai_summary = analysis.get("ai_summary", "")

        row_class = f"row-{state}"
        analysis_html = ""

        if state in ("failure", "error"):
            cat_badge = {
                "infra": '<span class="badge badge-infra">INFRA</span>',
                "test_failure": '<span class="badge badge-test">TEST</span>',
                "build_error": '<span class="badge badge-build">BUILD</span>',
                "matrix_mismatch": '<span class="badge badge-matrix">MATRIX</span>',
                "error": '<span class="badge badge-error">ERROR</span>',
                "unknown": '<span class="badge badge-unknown">???</span>',
            }.get(category, "")

            sev = inv.get("severity", "")
            sev_class = f"sev-{sev.lower()}" if sev else ""
            sev_badge = f'<span class="badge {sev_class}">{sev}</span>' if sev else ""

            analysis_html = f'{cat_badge} {sev_badge}'

            failed_tests = inv.get("failed_tests", [])
            if failed_tests:
                for t in failed_tests[:2]:
                    tname = t.get("name", t.get("step", "?"))
                    tfile = t.get("test_file", "")
                    tmsg = t.get("message", "")
                    analysis_html += f'<div style="margin-top:4px"><span class="test-name">{tname}</span>'
                    if tfile:
                        analysis_html += f' <span class="test-file">({tfile})</span>'
                    if tmsg:
                        analysis_html += f'<div class="test-msg">{tmsg[:200]}</div>'
                    analysis_html += '</div>'
                if len(failed_tests) > 2:
                    analysis_html += f'<div style="color:#8b949e;font-size:11px;margin-top:2px">+{len(failed_tests)-2} more</div>'

            if inv.get("suggested_fix"):
                fix_text = inv["suggested_fix"]
                analysis_html += (
                    f'<div class="fix-box">'
                    f'<div class="fix-box-title">Suggested Fix</div>'
                    f'<div class="fix-box-content">{fix_text}</div>'
                    f'</div>'
                )

            detail_buttons = []

            if inv and (inv.get("root_cause") or inv.get("error_output")):
                inv_html = ''
                if inv.get("root_cause"):
                    inv_html += f'<strong style="color:#f0883e">Root Cause:</strong> {inv["root_cause"]}<br><br>'
                if inv.get("error_output"):
                    inv_html += '<pre style="font-size:11px;color:#f0883e;white-space:pre-wrap">'
                    for err in inv["error_output"][:6]:
                        inv_html += f'{err}\n'
                    inv_html += '</pre>'
                detail_buttons.append(f'<details><summary>Investigation</summary><div>{inv_html}</div></details>')

            mdiff = analysis.get("matrix_diff", {})
            art = analysis.get("artifacts", {})
            ss_findings = art.get("ss_findings", []) if art else []

            if mdiff.get("is_matrix_mismatch"):
                diff_html = ''
                for key, label, color in [
                    ("undocumented_ports", "Add", "#f85149"),
                    ("stale_ports", "Remove", "#d29922"),
                ]:
                    ports = mdiff.get(key, [])
                    if ports:
                        diff_html += f'<strong style="color:{color}">{label}:</strong><br>'
                        for p in ports[:10]:
                            diff_html += f'<code>{p}</code><br>'

                no_ep = mdiff.get("no_endpointslice_ports", [])
                if no_ep:
                    diff_html += '<strong style="color:#da3633">Investigate (no EndpointSlice):</strong><br>'
                    for p in no_ep[:10]:
                        port_fields = p.split(",")
                        port_num = port_fields[2] if len(port_fields) >= 3 else "?"
                        diff_html += f'<code style="color:#f85149">{p}</code><br>'
                        ss_match = next((s for s in ss_findings if s["port"] == port_num), None)
                        if ss_match:
                            diff_html += (
                                f'<div style="margin:4px 0 8px 12px;padding:6px 10px;background:#161b22;'
                                f'border-left:3px solid #da3633;border-radius:4px;font-size:11px">'
                                f'<strong style="color:#58a6ff">ss output:</strong> '
                                f'<code style="color:#c9d1d9">{ss_match["ss_line"]}</code>'
                                f'</div>'
                            )
                        else:
                            diff_html += (
                                f'<div style="margin:2px 0 6px 12px;font-size:11px;color:#8b949e">'
                                f'(ss output not found for port {port_num})</div>'
                            )

                detail_buttons.append(f'<details><summary>Matrix Diff</summary><div>{diff_html}</div></details>')

            if ai_summary:
                detail_buttons.append(
                    f'<details><summary>AI Analysis</summary>'
                    f'<div style="white-space:pre-wrap;line-height:1.4">{ai_summary}</div></details>'
                )

            if detail_buttons:
                analysis_html += '<div style="margin-top:6px;display:flex;gap:4px;flex-wrap:wrap">' + "".join(detail_buttons) + '</div>'

        rows.append(
            f'<tr class="{row_class}" data-state="{state}" data-category="{category}">'
            f'<td>{emoji} {state}</td>'
            f'<td><span class="version-badge">{version}</span></td>'
            f'<td><a href="{url}" target="_blank" title="{job["name"]}">{name_short}</a></td>'
            f'<td>{duration}</td>'
            f'<td>{analysis_html}</td>'
            f'<td>{started}</td>'
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
        "{{CATEGORY_CARDS}}": category_cards,
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

        log.info("  Running investigation...")
        investigation = investigate_failure(
            job, category, reason, matrix_diff,
            junit_failures, step_logs, build_log,
        )
        log.info("  Investigation: severity=%s, fix_type=%s",
                 investigation["severity"], investigation["fix_type"])
        if investigation["suggested_fix"]:
            log.info("  Suggested fix: %s", investigation["suggested_fix"][:120])

        artifacts_data = {}
        if category == "matrix_mismatch" and matrix_diff.get("no_endpointslice_ports"):
            log.info("  Fetching artifacts for port investigation...")
            artifacts_data = _fetch_artifacts_context(job, category, matrix_diff, step_logs)
            if artifacts_data.get("ss_findings"):
                log.info("  Found ss data for %d port(s)", len(artifacts_data["ss_findings"]))
                for sf in artifacts_data["ss_findings"]:
                    log.info("    Port %s: %s", sf["port"], sf["ss_line"][:100])

        ai_log = analysis_log if analysis_log else build_log
        ai_summary = ""
        if ai_log and not ai_log.startswith("("):
            provider, _, _ = _get_ai_provider()
            if provider:
                log.info("  Running AI analysis (%s)...", provider)
                time.sleep(5)
                ai_summary = ai_analyze_failure(
                    job, ai_log, investigation, category, matrix_diff, step_logs,
                )
                if ai_summary:
                    log.info("  AI: %s", ai_summary[:200])
            if not ai_summary:
                fb_context = _extract_failure_context(ai_log)
                fb_prompt = (
                    f"You are a senior CI failure analyst for OpenShift. "
                    f"Analyze this failure for job {job['name']}.\n\n"
                    f"Respond with: **Failed Tests**, **Failure Messages**, "
                    f"**Root Cause**, **Classification**, **Recommended Action**, **Severity**\n\n"
                    f"Log:\n{fb_context}"
                )
                for fallback_name, fallback_key, fallback_model, fallback_url in [
                    ("cerebras", CEREBRAS_API_KEY, "llama-3.3-70b", "https://api.cerebras.ai/v1/chat/completions"),
                    ("deepseek", DEEPSEEK_API_KEY, "deepseek-chat", "https://api.deepseek.com/chat/completions"),
                ]:
                    if not fallback_key or fallback_name == provider:
                        continue
                    log.info("  Trying %s fallback...", fallback_name)
                    try:
                        fb_resp = requests.post(
                            fallback_url,
                            headers={"Authorization": f"Bearer {fallback_key}", "Content-Type": "application/json"},
                            json={"model": fallback_model, "messages": [{"role": "user", "content": fb_prompt}], "max_tokens": 800, "temperature": 0.2},
                            timeout=60,
                        )
                        if fb_resp.status_code == 200:
                            ai_summary = fb_resp.json()["choices"][0]["message"]["content"].strip()
                            if ai_summary:
                                log.info("  %s: %s", fallback_name, ai_summary[:200])
                                break
                        else:
                            log.warning("  %s failed: HTTP %d", fallback_name, fb_resp.status_code)
                    except Exception as e:
                        log.warning("  %s error: %s", fallback_name, e)

            if not ai_summary:
                log.info("  Trying Ollama fallback...")
                ai_summary = _ollama_analyze(
                    job, ai_log, investigation, category, matrix_diff, step_logs,
                )
                if ai_summary:
                    log.info("  Ollama: %s", ai_summary[:200])
                else:
                    log.info("  No AI analysis available")

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
            "investigation": investigation,
            "artifacts": artifacts_data,
            "pr_url": pr_url,
            "log_snippet": analysis_log[-500:] if analysis_log else "",
        }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    unique_latest: dict[str, dict] = {}
    for j in jobs:
        name = j["name"]
        if name not in unique_latest or j["start_time"] > unique_latest[name]["start_time"]:
            unique_latest[name] = j
    stats_jobs = list(unique_latest.values())

    history = load_history()
    history = update_history(history, stats_jobs)
    save_history(history)
    trend_html = generate_trend_html(history)
    log.info("Trend history updated (%d runs)", len(history.get("runs", [])))

    html = generate_html(jobs, analyses, trend_html)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_dir = OUTPUT_DIR / "runs" / today
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "index.html").write_text(html)
    log.info("Run dashboard written to %s", run_dir / "index.html")

    html_path = OUTPUT_DIR / "index.html"
    html_path.write_text(html)
    log.info("Latest dashboard written to %s", html_path)

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
    results_path = OUTPUT_DIR / "results.json"
    results_path.write_text(json.dumps(results, indent=2))
    (run_dir / "results.json").write_text(json.dumps(results, indent=2))
    log.info("Results JSON written to %s", results_path)

    _generate_runs_index(OUTPUT_DIR)


def _generate_runs_index(output_dir: Path) -> None:
    """Generate an index page listing all archived runs."""
    runs_dir = output_dir / "runs"
    if not runs_dir.exists():
        return

    run_dates = sorted(
        [d.name for d in runs_dir.iterdir() if d.is_dir() and d.name[0:2] == "20"],
        reverse=True,
    )

    rows = ""
    for date in run_dates:
        run_results = runs_dir / date / "results.json"
        summary = ""
        if run_results.exists():
            try:
                data = json.loads(run_results.read_text())
                total = data.get("total_jobs", 0)
                passed = data.get("passed", 0)
                failed = data.get("failed", 0)
                summary = (
                    f'<span class="green">{passed} passed</span> / '
                    f'<span class="red">{failed} failed</span> / '
                    f'{total} total'
                )
            except Exception:
                pass
        rows += (
            f'<tr>'
            f'<td><a href="runs/{date}/">{date}</a></td>'
            f'<td>{summary or "—"}</td>'
            f'</tr>\n'
        )

    index_html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prow Monitor — Run History</title>
<style>
  body {{ font-family: 'Inter', -apple-system, sans-serif; background: #0f1117; color: #e1e4e8; min-height: 100vh; }}
  .header {{ background: linear-gradient(135deg, #1a1e2e 0%, #2d1b4e 100%); padding: 24px 32px; border-bottom: 1px solid #30363d; }}
  .header h1 {{ font-size: 22px; color: #f0f6fc; }}
  .header .meta {{ color: #8b949e; font-size: 13px; margin-top: 6px; }}
  .container {{ max-width: 800px; margin: 0 auto; padding: 24px 32px; }}
  table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: #161b22; border-radius: 12px; overflow: hidden; border: 1px solid #30363d; }}
  th {{ background: #1c2128; color: #8b949e; padding: 12px 16px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid #21262d; font-size: 14px; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .green {{ color: #3fb950; }}
  .red {{ color: #f85149; }}
  .nav {{ margin-bottom: 20px; }}
  .nav a {{ background: #21262d; padding: 6px 14px; border-radius: 20px; border: 1px solid #30363d; font-size: 13px; }}
</style>
</head><body>
<div class="header">
  <h1>Prow Nightly Monitor — Run History</h1>
  <div class="meta">{len(run_dates)} archived run(s)</div>
</div>
<div class="container">
  <div class="nav"><a href="./">← Latest Dashboard</a></div>
  <table>
    <thead><tr><th>Date</th><th>Summary</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
</body></html>"""

    (output_dir / "history.html").write_text(index_html)
    log.info("Runs index written to %s", output_dir / "history.html")


if __name__ == "__main__":
    main()
