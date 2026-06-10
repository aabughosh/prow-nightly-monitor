#!/usr/bin/env python3
"""Failure fingerprinting — detect recurring issues and only investigate new ones.

A fingerprint is a hash of the key failure characteristics (error message pattern,
failed tests, category). When a failure matches a known fingerprint, we skip deep
AI investigation and reuse the previous analysis. This saves tokens and time.
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

MAX_AGE_DAYS = 14  # fingerprints older than this are expired


def _normalize_error(text: str) -> str:
    """Strip volatile parts (timestamps, IDs, counts, numbers) to get a stable signature."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", "<TS>", text)
    text = re.sub(r"[0-9a-f]{8,}", "<HEX>", text)
    text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<IP>", text)
    text = re.sub(r"pid=\d+", "pid=<N>", text)
    text = re.sub(r"fd=\d+", "fd=<N>", text)
    text = re.sub(r"\b\d+\s*(retries?|attempts?)\s*left", "<N> retries left", text)
    text = re.sub(r"\b\d+[a-z]{0,2}\b", "<N>", text)  # numbers with optional unit suffix
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def compute_fingerprint(job: dict) -> str:
    """Compute a stable fingerprint for a job failure.

    Uses: category + normalized error message + sorted failed test names.
    """
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


def load_db() -> dict:
    """Load the fingerprint database."""
    if FINGERPRINT_DB.exists():
        try:
            return json.loads(FINGERPRINT_DB.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"fingerprints": {}, "version": 1}


def save_db(db: dict) -> None:
    """Save the fingerprint database, pruning expired entries."""
    now = datetime.now(timezone.utc)
    pruned = {}
    for fp, entry in db.get("fingerprints", {}).items():
        last_seen = entry.get("last_seen", "")
        if last_seen:
            try:
                seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if (now - seen_dt).days <= MAX_AGE_DAYS:
                    pruned[fp] = entry
            except ValueError:
                pruned[fp] = entry
        else:
            pruned[fp] = entry

    db["fingerprints"] = pruned
    FINGERPRINT_DB.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_DB.write_text(json.dumps(db, indent=2))


def is_known(db: dict, fingerprint: str) -> bool:
    """Check if a fingerprint is already in the database."""
    return fingerprint in db.get("fingerprints", {})


def get_previous_analysis(db: dict, fingerprint: str) -> dict:
    """Get the stored analysis for a known fingerprint."""
    entry = db.get("fingerprints", {}).get(fingerprint, {})
    return entry


def record_fingerprint(db: dict, fingerprint: str, job: dict, ai_summary: str) -> None:
    """Store a new fingerprint with its analysis result."""
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
    """Update last_seen and occurrence count for a known fingerprint."""
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


def classify_issue(job: dict) -> str:
    """Classify a failure into a problem class for grouping.

    Returns a class label like 'infra_timeout', 'test_regression', 'flake', etc.
    """
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
