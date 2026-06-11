#!/usr/bin/env python3
"""Failure fingerprinting — detect recurring issues at the individual test/error level.

Each unique failure (specific test name + error pattern) gets its own fingerprint.
The same issue appearing across multiple jobs/versions is tracked as one fingerprint
with a list of affected jobs. This lets the tool:
- Skip re-analyzing known issues (saves AI tokens)
- Show users a unified "Known Issues" view
- Track when an issue first appeared and how many times it recurs
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

FINGERPRINT_DB = Path(os.environ.get(
    "FINGERPRINT_DB",
    os.path.expanduser("~/Documents/GitHub/prow-nightly-monitor/public/fingerprints.json")
))

MAX_AGE_DAYS = 30  # fingerprints older than this are expired
MAX_AFFECTED_JOBS = 50  # cap stored job references per issue


def _normalize_error(text: str) -> str:
    """Strip volatile parts (timestamps, IDs, counts, numbers) to get a stable signature."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", "<TS>", text)
    text = re.sub(r"[0-9a-f]{8,}", "<HEX>", text)
    text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<IP>", text)
    text = re.sub(r"pid=\d+", "pid=<N>", text)
    text = re.sub(r"fd=\d+", "fd=<N>", text)
    text = re.sub(r"\b\d+\s*(retries?|attempts?)\s*left", "<N> retries left", text)
    text = re.sub(r"\b\d+[a-z]{0,2}\b", "<N>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _normalize_test_name(name: str) -> str:
    """Normalize a test name for stable fingerprinting (strip version-specific parts)."""
    name = re.sub(r"\d+\.\d+", "<VER>", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


# ---------------------------------------------------------------------------
# Per-issue fingerprinting (new model)
# ---------------------------------------------------------------------------

def compute_issue_fingerprint(test_name: str, error_msg: str = "", category: str = "") -> str:
    """Compute a fingerprint for a single test/error issue.

    Uses: normalized test name + normalized error snippet + category.
    """
    norm_name = _normalize_test_name(test_name)
    norm_error = _normalize_error(error_msg)[:200]
    raw = f"{category}::{norm_name}::{norm_error}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def extract_issues_from_job(job: dict) -> list[dict]:
    """Extract individual issues (test failures / errors) from a job.

    Returns a list of issue dicts, each with:
      - test_name: the failing test name or error title
      - error_msg: the error message snippet
      - category: issue category
      - job_name: the parent job name
      - job_url: link to the prow job
    """
    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    failed_tests = inv.get("failed_tests", [])
    category = analysis.get("category", "unknown")
    job_name = job.get("name", "")
    job_url = job.get("url", "")

    issues = []

    if failed_tests:
        for t in failed_tests:
            name = t.get("name", t.get("step", ""))
            if not name or "Data:{" in name or "Result:0x" in name or "ResultType:vector" in name:
                continue
            issues.append({
                "test_name": name,
                "error_msg": t.get("message", "")[:300],
                "category": category,
                "job_name": job_name,
                "job_url": job_url,
            })

    # If no test-level failures, create one issue for the whole job error
    if not issues:
        reason = analysis.get("reason", "")
        # Skip garbled Prometheus data in reason
        if reason and ("Data:{" in reason or "Result:0x" in reason or "ResultType:vector" in reason):
            reason = ""
        title = reason[:120] if reason else f"Job failure: {job_name.split('nightly-')[-1]}"
        issues.append({
            "test_name": title,
            "error_msg": reason[:300],
            "category": category,
            "job_name": job_name,
            "job_url": job_url,
        })

    return issues


# ---------------------------------------------------------------------------
# Legacy per-job fingerprinting (kept for PTP backward compat)
# ---------------------------------------------------------------------------

def compute_fingerprint(job: dict) -> str:
    """Compute a stable fingerprint for a job failure (legacy: whole-job level)."""
    analysis = job.get("analysis", {})
    category = analysis.get("category", "unknown")
    reason = _normalize_error(analysis.get("reason", ""))

    inv = analysis.get("investigation", {})
    test_names = sorted(
        t.get("name", "") for t in inv.get("failed_tests", [])
    )
    tests_key = "|".join(test_names[:10])

    raw = f"{category}::{reason}::{tests_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def load_db() -> dict:
    """Load the fingerprint database."""
    if FINGERPRINT_DB.exists():
        try:
            return json.loads(FINGERPRINT_DB.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"issues": {}, "fingerprints": {}, "version": 2}


def save_db(db: dict) -> None:
    """Save the fingerprint database, pruning expired entries."""
    now = datetime.now(timezone.utc)

    # Prune legacy fingerprints
    pruned_fp = {}
    for fp, entry in db.get("fingerprints", {}).items():
        last_seen = entry.get("last_seen", "")
        if last_seen:
            try:
                seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if (now - seen_dt).days <= MAX_AGE_DAYS:
                    pruned_fp[fp] = entry
            except ValueError:
                pruned_fp[fp] = entry
        else:
            pruned_fp[fp] = entry
    db["fingerprints"] = pruned_fp

    # Prune issue-level fingerprints
    pruned_issues = {}
    for fp, entry in db.get("issues", {}).items():
        last_seen = entry.get("last_seen", "")
        if last_seen:
            try:
                seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if (now - seen_dt).days <= MAX_AGE_DAYS:
                    pruned_issues[fp] = entry
            except ValueError:
                pruned_issues[fp] = entry
        else:
            pruned_issues[fp] = entry
    db["issues"] = pruned_issues

    FINGERPRINT_DB.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_DB.write_text(json.dumps(db, indent=2))


def is_known(db: dict, fingerprint: str) -> bool:
    """Check if a fingerprint is already in the database (legacy or issue-level)."""
    return (fingerprint in db.get("fingerprints", {}) or
            fingerprint in db.get("issues", {}))


def is_known_issue(db: dict, fingerprint: str) -> bool:
    """Check if an issue-level fingerprint exists."""
    return fingerprint in db.get("issues", {})


def get_previous_analysis(db: dict, fingerprint: str) -> dict:
    """Get the stored analysis for a known fingerprint (legacy)."""
    return db.get("fingerprints", {}).get(fingerprint, {})


def get_issue(db: dict, fingerprint: str) -> dict:
    """Get a stored issue by fingerprint."""
    return db.get("issues", {}).get(fingerprint, {})


def record_issue(db: dict, fingerprint: str, test_name: str, job_name: str,
                 job_url: str, classification: str = "", root_cause: str = "",
                 ai_summary: str = "", is_flake: bool = False) -> None:
    """Record or update an issue-level fingerprint."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today = now.strftime("%Y-%m-%d")

    entry = db.setdefault("issues", {}).get(fingerprint, {})

    # Build affected_jobs list
    affected = entry.get("affected_jobs", [])
    short_name = re.sub(r"periodic-ci-openshift-release-main-nightly-", "", job_name)
    # Don't add duplicate job entries for the same day
    existing_keys = {(j["name"], j["date"]) for j in affected}
    if (short_name, today) not in existing_keys:
        affected.append({
            "name": short_name,
            "url": job_url,
            "date": today,
        })
    # Cap the list
    if len(affected) > MAX_AFFECTED_JOBS:
        affected = affected[-MAX_AFFECTED_JOBS:]

    db.setdefault("issues", {})[fingerprint] = {
        "title": test_name,
        "first_seen": entry.get("first_seen", now_iso),
        "last_seen": now_iso,
        "occurrences": entry.get("occurrences", 0) + 1,
        "classification": classification or entry.get("classification", "unknown"),
        "root_cause": root_cause[:300] if root_cause else entry.get("root_cause", ""),
        "ai_summary_short": ai_summary[:2000] if ai_summary else entry.get("ai_summary_short", ""),
        "is_flake": is_flake,
        "affected_jobs": affected,
        "status": "active",
    }


def mark_issue_seen(db: dict, fingerprint: str, job_name: str, job_url: str) -> None:
    """Update last_seen and add the job to the affected list."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    entry = db.get("issues", {}).get(fingerprint)
    if not entry:
        return
    entry["last_seen"] = now.isoformat()
    entry["occurrences"] = entry.get("occurrences", 0) + 1

    short_name = re.sub(r"periodic-ci-openshift-release-main-nightly-", "", job_name)
    affected = entry.get("affected_jobs", [])
    existing_keys = {(j["name"], j["date"]) for j in affected}
    if (short_name, today) not in existing_keys:
        affected.append({"name": short_name, "url": job_url, "date": today})
    if len(affected) > MAX_AFFECTED_JOBS:
        entry["affected_jobs"] = affected[-MAX_AFFECTED_JOBS:]


# ---------------------------------------------------------------------------
# Legacy record (for PTP backward compat)
# ---------------------------------------------------------------------------

def record_fingerprint(db: dict, fingerprint: str, job: dict, ai_summary: str) -> None:
    """Store a new fingerprint with its analysis result (legacy per-job)."""
    now = datetime.now(timezone.utc).isoformat()
    analysis = job.get("analysis", {})

    entry = db.get("fingerprints", {}).get(fingerprint, {})
    occurrence_count = entry.get("occurrences", 0) + 1

    db.setdefault("fingerprints", {})[fingerprint] = {
        "first_seen": entry.get("first_seen", now),
        "last_seen": now,
        "occurrences": occurrence_count,
        "job_name_pattern": re.sub(
            r"periodic-ci-openshift-release-main-nightly-", "", job["name"]
        ),
        "category": analysis.get("category", "unknown"),
        "severity": analysis.get("investigation", {}).get("severity", ""),
        "root_cause": _extract_root_cause(ai_summary),
        "ai_summary_short": ai_summary[:2000] if ai_summary else "",
        "is_flake": "flake" in ai_summary.lower() if ai_summary else False,
    }


def mark_seen(db: dict, fingerprint: str) -> None:
    """Update last_seen and occurrence count for a known fingerprint (legacy)."""
    now = datetime.now(timezone.utc).isoformat()
    entry = db.get("fingerprints", {}).get(fingerprint)
    if entry:
        entry["last_seen"] = now
        entry["occurrences"] = entry.get("occurrences", 0) + 1


def _extract_root_cause(ai_summary: str) -> str:
    """Extract root cause from AI summary text."""
    if not ai_summary:
        return ""
    for line in ai_summary.split("\n"):
        stripped = line.strip()
        if stripped.startswith("**Root Cause"):
            return re.sub(r"\*\*Root Cause[^*]*\*\*:?\s*", "", stripped)[:300]
        if stripped.startswith("## Root Cause"):
            return stripped.replace("## Root Cause", "").strip()[:300]
    return ""


def _extract_classification(ai_summary: str) -> str:
    """Extract issue class from AI summary text."""
    if not ai_summary:
        return "unknown"
    for line in ai_summary.split("\n"):
        stripped = line.strip()
        if stripped.startswith("**Issue Class"):
            cls = re.sub(r"\*\*Issue Class[^*]*\*\*:?\s*", "", stripped)
            cls = cls.strip("`*() ").split("(")[0].split(" ")[0].strip()
            return cls if cls else "unknown"
    return "unknown"


def _extract_is_flake(ai_summary: str) -> bool:
    """Extract flake status from AI summary."""
    if not ai_summary:
        return False
    for line in ai_summary.split("\n"):
        stripped = line.strip()
        if stripped.startswith("**Is it a flake?**"):
            answer = stripped.replace("**Is it a flake?**", "").strip().lower()
            return answer.startswith("yes") or "partially yes" in answer
    return False


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_issue(job: dict) -> str:
    """Classify a failure into a problem class for grouping."""
    analysis = job.get("analysis", {})
    category = analysis.get("category", "unknown")
    reason = analysis.get("reason", "").lower()
    inv = analysis.get("investigation", {})
    severity = inv.get("severity", "").upper()

    if category == "infra":
        if "timeout" in reason or "deadline" in reason:
            return "infra_timeout"
        if "quota" in reason or "capacity" in reason:
            return "infra_quota"
        return "infra_other"

    if category == "test_failure":
        if any("flak" in t.get("message", "").lower() for t in inv.get("failed_tests", [])):
            return "test_flake"
        if severity in ("HIGH", "CRITICAL"):
            return "test_regression"
        return "test_failure"

    if category == "matrix_mismatch":
        return "matrix_mismatch"

    if category == "build_error":
        return "build_error"

    return "unknown"


def group_by_class(jobs: list[dict]) -> dict[str, list[dict]]:
    """Group failed jobs by their issue class."""
    groups: dict[str, list[dict]] = {}
    for job in jobs:
        if job.get("state") in ("failure", "error"):
            cls = classify_issue(job)
            groups.setdefault(cls, []).append(job)
    return groups
