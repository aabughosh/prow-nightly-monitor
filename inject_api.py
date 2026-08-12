#!/usr/bin/env python3
"""Run Claude via Vertex AI on each failed job with full CI evidence.

Drop-in replacement for inject_claude.py — uses direct API calls instead
of the Cursor CLI, so it runs anywhere (CI pipelines, servers, containers).

Auth: set GOOGLE_APPLICATION_CREDENTIALS to a Vertex AI service account JSON,
or run `gcloud auth application-default login` for local dev.

Environment variables:
    VERTEX_PROJECT      - GCP project ID (e.g. itpc-ca-48013775ee)
    VERTEX_REGION       - Vertex AI region (default: us-east5)
    VERTEX_MODEL        - Model ID (default: claude-sonnet-4-6)
    GOOGLE_APPLICATION_CREDENTIALS - Path to service account JSON
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fingerprint import (
    compute_fingerprint, load_db, save_db, is_known,
    get_previous_analysis, record_fingerprint, mark_seen,
    group_by_class,
    compute_issue_fingerprint, extract_issues_from_job,
    is_known_issue, get_issue, record_issue, mark_issue_seen,
    _extract_root_cause, _extract_classification, _extract_is_flake,
)

REPO_DIR = os.environ.get("REPO_DIR", os.path.dirname(os.path.abspath(__file__)))
TARGET_REPO = os.environ.get("TARGET_REPO", "")
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "openshift-kni/commatrix")
INVESTIGATE_DIR = os.environ.get("INVESTIGATE_DIR", "/tmp/ci-investigate")
EVIDENCE_DIR = os.path.join(INVESTIGATE_DIR, "ci-evidence")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", f"{REPO_DIR}/public")
RESULTS = os.path.join(OUTPUT_DIR, "results.json")

VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "itpc-ca-48013775ee")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "us-east5")
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "claude-sonnet-4-6")

MAX_RESULTS_SIZE = 50 * 1024 * 1024
API_TIMEOUT = 120
MAX_RETRIES = 2
MIN_VERSION = os.environ.get("MIN_VERSION", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "https://aabughosh.github.io/prow-nightly-monitor/cursor/"
)
FORCE_REANALYZE = os.environ.get("FORCE_REANALYZE", "false").lower() == "true"

_client = None


def _get_client():
    """Lazy-init the Vertex AI client."""
    global _client
    if _client is None:
        from anthropic import AnthropicVertex
        _client = AnthropicVertex(
            project_id=VERTEX_PROJECT,
            region=VERTEX_REGION,
        )
    return _client


def check_auth() -> bool:
    """Verify Vertex AI credentials are valid."""
    try:
        client = _get_client()
        msg = client.messages.create(
            model=VERTEX_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply OK"}],
        )
        print(f"  Auth OK: Vertex AI ({VERTEX_PROJECT}/{VERTEX_REGION}, model={VERTEX_MODEL})")
        return True
    except Exception as e:
        print(f"  Auth failed: {e}")
        return False


def call_claude(prompt: str, max_tokens: int = 16000) -> str:
    """Call Claude via Vertex AI. Returns the response text."""
    client = _get_client()

    for attempt in range(MAX_RETRIES + 1):
        try:
            msg = client.messages.create(
                model=VERTEX_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = ""
            for block in msg.content:
                if hasattr(block, "text"):
                    text += block.text
            return text.strip()
        except Exception as e:
            print(f"    API error (attempt {attempt + 1}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
    return ""


# ── Evidence handling (same as inject_claude.py) ──

MAX_EVIDENCE_FILES = 12

_SKIP_PATTERNS = (
    "gather-extra/", "gather-extra__",
    "baremetalds-", "aws-deprovision", "ofcir-",
    "ipi-conf", "cloud-init",
    "cluster-setup/", "cluster-setup__",
    ".html",
    "/artifacts/",
    "__artifacts__",
)
_KEEP_PATTERNS = (
    "junit.xml", "test_results", "build-log", "finished.json",
    "pod-logs/", "pod-logs__", "pod_logs",
    "commatrix-e2e/", "network-flow-matrix",
    "matrix-diff", "doc-diff", "raw-ss", "communication-matrix",
    "nftables", "mc-master", "mc-worker", "ss-generated",
)


def _is_essential_artifact(key: str) -> bool:
    key_lower = key.lower()
    for pat in _SKIP_PATTERNS:
        if pat in key_lower:
            return False
    for pat in _KEEP_PATTERNS:
        if pat in key_lower:
            return True
    return False


def dump_evidence(job: dict) -> list[str]:
    """Write essential CI artifacts to ci-evidence/ and return filenames."""
    if os.path.exists(EVIDENCE_DIR):
        shutil.rmtree(EVIDENCE_DIR)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)

    analysis = job.get("analysis", {})
    artifacts = analysis.get("artifacts", {})
    all_artifacts = artifacts.get("all_artifacts", {})
    evidence_files = []

    for key, content in all_artifacts.items():
        if not _is_essential_artifact(key):
            continue
        safe_name = key.replace("/", "__")
        path = os.path.join(EVIDENCE_DIR, safe_name)
        with open(path, "w") as f:
            f.write(content)
        evidence_files.append(safe_name)
        if len(evidence_files) >= MAX_EVIDENCE_FILES:
            break

    log_snippet = analysis.get("log_snippet", "")
    if log_snippet:
        with open(os.path.join(EVIDENCE_DIR, "failure-log.txt"), "w") as f:
            f.write(log_snippet)
        evidence_files.append("failure-log.txt")

    prow_url = job.get("url", "")
    if prow_url:
        m = re.search(r"/logs/(.+)/(\d+)$", prow_url)
        if m:
            job_path, build_id = m.group(1), m.group(2)
            gcs_raw = "https://storage.googleapis.com/test-platform-results/logs"

            try:
                import requests as _req
                build_log_url = f"{gcs_raw}/{job_path}/{build_id}/build-log.txt"
                resp = _req.get(build_log_url, timeout=30)
                if resp.status_code == 200:
                    text = resp.text
                    if len(text) > 16000:
                        head = text[:8000]
                        tail = text[-8000:]
                        text = head + "\n\n... (middle truncated) ...\n\n" + tail
                    with open(os.path.join(EVIDENCE_DIR, "ci-operator-build-log.txt"), "w") as f:
                        f.write(text)
                    evidence_files.append("ci-operator-build-log.txt")
            except Exception:
                pass

    inv = analysis.get("investigation", {})
    if inv:
        lines = [
            f"Severity: {inv.get('severity', '?')}",
            f"Fix type: {inv.get('fix_type', '?')}",
            f"Suggested fix: {inv.get('suggested_fix', 'N/A')}",
        ]
        for t in inv.get("failed_tests", []):
            lines.append(f"\nFailed test: {t.get('name', '?')}")
            lines.append(f"  Message: {t.get('message', '')}")
            if t.get("file"):
                lines.append(f"  File: {t['file']}")
        with open(os.path.join(EVIDENCE_DIR, "investigation-summary.txt"), "w") as f:
            f.write("\n".join(lines))
        evidence_files.append("investigation-summary.txt")

    junit = analysis.get("junit_failures", [])
    if junit:
        lines = []
        for jf in junit:
            lines.append(f"Test: {jf.get('name', '?')}")
            lines.append(f"  Message: {jf.get('message', '')[:500]}")
            lines.append("")
        with open(os.path.join(EVIDENCE_DIR, "junit-failures.txt"), "w") as f:
            f.write("\n".join(lines))
        evidence_files.append("junit-failures.txt")

    matrix_diff = analysis.get("matrix_diff", {})
    if matrix_diff:
        lines = []
        for key_name, label in [
            ("no_endpointslice_ports", "Ports open but missing EndpointSlice"),
            ("stale_ports", "Ports in matrix but not in use on node"),
            ("undocumented_ports", "Ports in use but not in matrix"),
        ]:
            ports = matrix_diff.get(key_name, [])
            if ports:
                lines.append(f"\n{label}:")
                for p in ports:
                    lines.append(f"  {p}")
        if lines:
            with open(os.path.join(EVIDENCE_DIR, "matrix-diff-summary.txt"), "w") as f:
                f.write("\n".join(lines))
            evidence_files.append("matrix-diff-summary.txt")

    ss_findings = artifacts.get("ss_findings", [])
    if ss_findings:
        lines = [
            f"Port {sf['port']}: {sf['ss_line']}\n  Matrix entry: {sf.get('entry', '')}"
            for sf in ss_findings
        ]
        with open(os.path.join(EVIDENCE_DIR, "ss-port-analysis.txt"), "w") as f:
            f.write("\n".join(lines))
        evidence_files.append("ss-port-analysis.txt")

    return evidence_files


def _read_evidence_files(evidence_files: list[str]) -> str:
    """Read all evidence files and bundle them into a single text block.

    This replaces the Cursor CLI's ability to read files interactively.
    """
    parts = []
    total_chars = 0
    max_total = 80000  # keep the total context manageable

    for fname in evidence_files:
        fpath = os.path.join(EVIDENCE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            content = open(fpath).read()
            if total_chars + len(content) > max_total:
                remaining = max_total - total_chars
                if remaining > 500:
                    content = content[:remaining] + "\n... (truncated)"
                else:
                    parts.append(f"\n--- {fname} ---\n(skipped — context limit reached)")
                    continue
            parts.append(f"\n--- {fname} ---\n{content}")
            total_chars += len(content)
        except Exception:
            continue

    return "\n".join(parts)


def _fetch_source_context(job: dict) -> str:
    """Fetch relevant source code from the cloned repo to include in the prompt."""
    if not os.path.exists(INVESTIGATE_DIR):
        return ""

    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    tests = inv.get("failed_tests", []) or analysis.get("junit_failures", [])

    source_parts = []
    seen_files = set()

    for t in tests[:5]:
        filepath = t.get("file", "")
        if not filepath or filepath in seen_files:
            continue
        seen_files.add(filepath)

        # Try to find the file in the cloned repo
        candidates = []
        if filepath.startswith("/"):
            filepath = filepath.lstrip("/")
        for root, dirs, files in os.walk(INVESTIGATE_DIR):
            if ".git" in root:
                continue
            for f in files:
                full = os.path.join(root, f)
                if full.endswith(filepath) or f == os.path.basename(filepath):
                    candidates.append(full)
            if len(candidates) >= 2:
                break

        for cand in candidates[:1]:
            try:
                content = open(cand).read()
                rel = os.path.relpath(cand, INVESTIGATE_DIR)
                if len(content) > 5000:
                    content = content[:5000] + "\n... (truncated)"
                source_parts.append(f"\n--- Source: {rel} ---\n{content}")
            except Exception:
                continue

    # Also include recent git log
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=INVESTIGATE_DIR, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            source_parts.append(f"\n--- Recent commits (git log --oneline -20) ---\n{result.stdout}")
    except Exception:
        pass

    return "\n".join(source_parts) if source_parts else ""


# ── Prompt building ──

def _load_project_config() -> dict:
    config_path = Path(__file__).parent / "projects.json"
    if not config_path.exists():
        return {}
    all_projects = json.loads(config_path.read_text())
    job_filter = os.environ.get("JOB_FILTER", "")
    for pconf in all_projects.values():
        if pconf.get("job_filter", "") and pconf["job_filter"] in job_filter:
            return pconf
    for pconf in all_projects.values():
        if job_filter in pconf.get("job_filter", ""):
            return pconf
    return {}


def build_prompt(job: dict, evidence_files: list[str]) -> str:
    """Build a focused prompt for CI failure analysis with all evidence inline."""
    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    category = analysis.get("category", "")
    reason = analysis.get("reason", "")

    tests_list = inv.get("failed_tests", []) or analysis.get("junit_failures", [])
    failed_tests = "\n".join(
        f"  - {t.get('name', '?')}: {t.get('message', '')[:500]}"
        for t in tests_list
    )

    project = _load_project_config()
    project_desc = project.get("description", "N/A") if project else "N/A"
    hint = ""
    topology_info = ""
    if project:
        hints = project.get("classification_hints", {})
        if category in hints:
            hint = f"\nHint: {hints[category]}"
        lab_topo = project.get("lab_topology", {})
        if lab_topo:
            topology_info = (
                f"\n**Lab Topology:** {lab_topo.get('description', '')}"
                f"\n{lab_topo.get('note', '')}\n"
            )

    version = ""
    _ver_match = re.search(r"nightly-(\d+\.\d+)", job["name"])
    if _ver_match:
        version = _ver_match.group(1)

    branch_context = ""
    if version:
        branch_context = (
            f"\n\nCRITICAL — BRANCH AWARENESS:"
            f"\nThis job runs test scripts from `main` against `release-{version}` production images."
            f"\nA PR merged ONLY on `main` CANNOT be the root cause unless cherry-picked to `release-{version}`.\n"
        )

    versions_list = job.get("_affected_versions", [])
    if versions_list and len(versions_list) > 1:
        ver_str = ", ".join(sorted(versions_list))
        version_instruction = (
            f"This test fails on OCP versions: {ver_str}. "
            f"Evidence is from version {version}."
        )
    else:
        version_instruction = f"Focus on OCP version {version or 'unknown'}."

    evidence_content = _read_evidence_files(evidence_files)
    source_context = _fetch_source_context(job)

    return f"""Analyze this CI failure based on the evidence provided below.
{version_instruction}
{branch_context}
**Project:** {project_desc}{hint}
**Source repo:** https://github.com/{UPSTREAM_REPO}
{topology_info}

**Job:** {job['name']}
**Prow URL:** {job.get('url', 'N/A')}
**Category:** {category}
**Reason:** {reason}

**Failed tests:**
{failed_tests or '(none extracted)'}

**Log snippet:**
{analysis.get('log_snippet', '(no log)')[:800]}

== EVIDENCE FILES ==
{evidence_content}

{f"== SOURCE CODE CONTEXT =={source_context}" if source_context else ""}

INVESTIGATION INSTRUCTIONS:
1. Read ci-operator-build-log.txt FIRST — it shows ALL job steps and which failed.
2. CHECK FOR IMAGE PULL FAILURES in the first half of the build log.
3. Check finished.json — if "passed":true, the project's tests passed and the failure is from the CI framework.
4. For CI framework failures: classify as infra_other.
5. Trace back to the UPSTREAM cause — find the FIRST domino, not the last.
6. Quote verbatim log evidence for every conclusion. Do NOT guess.

CRITICAL OUTPUT RULES:
1. Write your analysis ONCE. Do NOT restart or repeat.
2. Group tests by root cause. If N tests share the same error, write ONE group.
3. Keep total output under 80 lines.

Respond with EXACTLY this format:

---

**TL;DR:** One sentence (max 20 words).

**Overall Issue Class:** [infra_timeout | infra_other | test_regression | test_flake | build_error | unknown]
**Overall Severity:** [CRITICAL / HIGH / MEDIUM / LOW]

## Root Cause Groups

### Group A — [Label] (N tests)
- **Tests:** [comma-separated list]
- **Classification:** [operator_bug | test_regression | infra_issue | test_flake] | **Confidence:** [high/medium/low]
- **Evidence:** `[1-3 key log lines]`
- **Root Cause:** 2-3 sentences. Reference file:line.
- **Breaking PR/Commit:** [link or "Unknown"]
- **Flake?** [yes/no — with evidence]
- **Fix:** 1-2 sentences.

## Summary Table

| # | Test | Group | Classification | Confidence | Root Cause (one line) | Fix |
|---|------|-------|---------------|------------|----------------------|-----|

## Action Items
1. [most impactful fix]
2. [second fix]
"""


def build_suite_prompt(
    job: dict, evidence_files: list[str], suite_name: str, suite_tests: list[dict]
) -> str:
    """Build a prompt for analyzing one test suite's failures."""
    analysis = job.get("analysis", {})
    category = analysis.get("category", "")

    failed_tests = "\n".join(
        f"  - {t.get('name', '?')}: {t.get('message', '')[:400]}"
        for t in suite_tests
    )

    project = _load_project_config()
    project_desc = project.get("description", "N/A") if project else "N/A"
    topology_info = ""
    if project:
        lab_topo = project.get("lab_topology", {})
        if lab_topo:
            topology_info = (
                f"\n**Lab Topology:** {lab_topo.get('description', '')}"
                f"\n{lab_topo.get('note', '')}\n"
            )

    version = ""
    _ver_match = re.search(r"nightly-(\d+\.\d+)", job["name"])
    if _ver_match:
        version = _ver_match.group(1)

    branch_context = ""
    if version:
        branch_context = (
            f"\nCRITICAL — BRANCH AWARENESS:"
            f"\nThis job runs test scripts from `main` against `release-{version}` production images."
            f"\nA PR merged ONLY on `main` CANNOT be the root cause unless cherry-picked to `release-{version}`.\n"
        )

    evidence_content = _read_evidence_files(evidence_files)
    source_context = _fetch_source_context(job)

    return f"""Analyze the **{suite_name}** test suite failures from this CI job.
Focus on OCP version {version or 'unknown'}.
{branch_context}

**Project:** {project_desc}
**Source repo:** https://github.com/{UPSTREAM_REPO}
{topology_info}

**Job:** {job['name']}
**Prow URL:** {job.get('url', 'N/A')}
**Suite:** {suite_name} ({len(suite_tests)} failed tests)

**Failed tests in this suite:**
{failed_tests}

== EVIDENCE FILES ==
{evidence_content}

{f"== SOURCE CODE CONTEXT =={source_context}" if source_context else ""}

INSTRUCTIONS:
1. Read ci-operator-build-log.txt FIRST to understand job-level context.
2. Check for image pull failures.
3. Trace back to the UPSTREAM cause. If tests say "timeout" or "not found", find WHY.
4. Quote verbatim log evidence. Do NOT guess.
5. If you claim cascade failures, provide evidence proving the dependency chain.

CRITICAL OUTPUT RULES:
1. Write your analysis ONCE. Do NOT repeat.
2. Group tests by root cause.
3. Keep total output under 60 lines.

Respond with EXACTLY this format:

---

### Suite: {suite_name} — {len(suite_tests)} failures

**TL;DR:** One sentence.
**Suite Issue Class:** [infra_timeout | infra_other | test_regression | test_flake | test_failure | build_error | unknown]
**Suite Severity:** [CRITICAL / HIGH / MEDIUM / LOW]

**Root Cause Groups:**

**Group 1: [name]** ({len(suite_tests)} tests)
- Tests: [list names, comma-separated]
- Evidence: `[1-3 key log lines]`
- Root Cause: 2-3 sentences. Reference file:line.
- Breaking PR/Commit: [link or "Unknown"]
- Flake? [yes/no — with evidence]
- Fix: 1 sentence.
"""


def build_summary_prompt(
    job: dict, suite_analyses: dict[str, str], total_failures: int
) -> str:
    """Build a prompt for the cross-suite summary."""
    version = ""
    _ver_match = re.search(r"nightly-(\d+\.\d+)", job["name"])
    if _ver_match:
        version = _ver_match.group(1)

    suite_sections = ""
    for suite_name, analysis in suite_analyses.items():
        suite_sections += f"\n\n--- {suite_name} ---\n{analysis[:3000]}"

    return f"""You are given per-suite AI analyses of a CI job with {total_failures} total test failures.
Produce a CROSS-SUITE SUMMARY identifying common patterns.

**Job:** {job['name']}
**Version:** {version}
**Total failures:** {total_failures}
**Suites analyzed:** {', '.join(suite_analyses.keys())}

INDIVIDUAL SUITE ANALYSES:
{suite_sections}

RULES:
1. RESPECT PER-SUITE CLASSIFICATIONS. Do not silently override them.
2. CASCADE CLAIMS REQUIRE PROOF from BOTH suites.
3. FLAG CONTRADICTIONS between suites.
4. Keep total output under 80 lines.

Respond with EXACTLY this format:

---

**TL;DR:** One sentence (max 20 words).

**Overall Issue Class:** [infra_timeout | infra_other | test_regression | test_flake | build_error | unknown]
**Overall Severity:** [CRITICAL / HIGH / MEDIUM / LOW]

## Root Causes ({total_failures} failures across {len(suite_analyses)} suites)

### N/M failures — [root cause name]
- **Suites:** [list] | **Evidence:** [PROVEN or HYPOTHESIS]
- **What:** 1-2 sentences.
- **Breaking change:** [PR link or "Unknown"]
- **Flake?** yes/no
- **Fix:** 1 sentence.

## Summary Table

| # | Test | Suite | Group | Classification | Confidence | Root Cause (one line) | Fix |
|---|------|-------|-------|---------------|------------|----------------------|-----|

## Action Items
1. [most impactful fix]
2. [second fix]
"""


# ── Job analysis orchestration ──

def _group_tests_by_suite(job: dict) -> dict[str, list[dict]]:
    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    tests_list = inv.get("failed_tests", []) or analysis.get("junit_failures", [])

    suites: dict[str, list[dict]] = {}
    for t in tests_list:
        name = t.get("name", t.get("step", ""))
        m = re.search(r"\[(\w+?)[-_](serial|parallel)\]", name)
        suite = m.group(1) if m else "all"
        suites.setdefault(suite, []).append(t)

    return suites


MAX_SUITE_TESTS = 12


def analyze_job(job: dict) -> str:
    """Deep investigation with per-suite splitting for large jobs."""
    evidence_files = dump_evidence(job)
    print(f"    Dumped {len(evidence_files)} evidence files")

    suites = _group_tests_by_suite(job)
    total_tests = sum(len(tests) for tests in suites.values())

    if len(suites) <= 1 and total_tests <= MAX_SUITE_TESTS:
        prompt = build_prompt(job, evidence_files)
        return call_claude(prompt)

    SMALL_SUITE_THRESHOLD = 3
    batched_suites: dict[str, list[dict]] = {}
    small_batch: dict[str, list[dict]] = {}
    small_batch_tests = 0
    for sname, stests in suites.items():
        if len(stests) <= SMALL_SUITE_THRESHOLD:
            small_batch[sname] = stests
            small_batch_tests += len(stests)
        else:
            batched_suites[sname] = stests

    if small_batch and small_batch_tests <= MAX_SUITE_TESTS:
        combined_name = "+".join(small_batch.keys())
        combined_tests = []
        for stests in small_batch.values():
            combined_tests.extend(stests)
        batched_suites[combined_name] = combined_tests
    elif small_batch:
        batched_suites.update(small_batch)

    print(
        f"    Split into {len(batched_suites)} suite groups: "
        f"{', '.join(f'{s}({len(t)})' for s, t in batched_suites.items())}"
    )

    suite_analyses: dict[str, str] = {}
    for suite_name, suite_tests in batched_suites.items():
        print(f"    Suite '{suite_name}' ({len(suite_tests)} tests)...")
        prompt = build_suite_prompt(job, evidence_files, suite_name, suite_tests)
        result = call_claude(prompt)
        if result:
            suite_analyses[suite_name] = result
            print(f"      Done ({len(result)} chars)")
        else:
            print(f"      No analysis returned")

    if not suite_analyses:
        return ""

    print(f"    Generating cross-suite summary...")
    summary = call_claude(build_summary_prompt(job, suite_analyses, total_tests))

    parts = []
    if summary:
        parts.append(summary)

    suite_detail_parts = []
    for suite_name, analysis in suite_analyses.items():
        trimmed = analysis[:2000]
        if len(analysis) > 2000:
            trimmed += "\n\n_(truncated)_"
        suite_detail_parts.append(f"\n### {suite_name}\n\n{trimmed}")

    if suite_detail_parts:
        parts.append("\n\n---\n\n<!-- SUITE_DETAILS_START -->\n## Per-Suite Details\n")
        parts.append("\n".join(suite_detail_parts))
        parts.append("\n<!-- SUITE_DETAILS_END -->")

    return "\n".join(parts)


def _checkout_source_repos() -> None:
    """Clone target repo + related repos for source context."""
    if not TARGET_REPO:
        os.makedirs(INVESTIGATE_DIR, exist_ok=True)
        os.makedirs(os.path.join(INVESTIGATE_DIR, "ci-evidence"), exist_ok=True)
        return

    if not os.path.exists(INVESTIGATE_DIR):
        print(f"  Cloning {TARGET_REPO} ...")
        subprocess.run(
            ["git", "clone", "--depth=1", TARGET_REPO, INVESTIGATE_DIR],
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "pull", "--ff-only"], cwd=INVESTIGATE_DIR, capture_output=True
        )

    os.makedirs(os.path.join(INVESTIGATE_DIR, "ci-evidence"), exist_ok=True)

    project = _load_project_config()
    if project:
        related = project.get("related_repos", [])
        for repo_url in related:
            repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
            repo_path = os.path.join(INVESTIGATE_DIR, repo_name)
            if not os.path.exists(repo_path):
                print(f"  Cloning related: {repo_name} ...")
                subprocess.run(
                    ["git", "clone", "--depth=1", repo_url, repo_path],
                    capture_output=True,
                )


# ── Per-issue analysis (same logic as inject_claude.py) ──

def _analyze_per_issue(data: dict, failed: list[dict], fp_db: dict) -> None:
    for job in failed:
        job.setdefault("analysis", {})["issues"] = []

    all_issues: list[dict] = []
    for job in failed:
        issues = extract_issues_from_job(job)
        all_issues.extend(issues)

    print(f"  Extracted {len(all_issues)} individual issues from {len(failed)} failed jobs")

    unique_issues: dict[str, dict] = {}
    for issue in all_issues:
        _ver = ""
        _ver_m = re.search(r"nightly-(\d+\.\d+)", issue.get("job_name", ""))
        if _ver_m:
            _ver = _ver_m.group(1)

        fp = compute_issue_fingerprint(
            issue["test_name"],
            issue["error_msg"],
            issue["category"],
            version=_ver,
            job_filter=os.environ.get("JOB_FILTER", ""),
        )
        if fp not in unique_issues:
            unique_issues[fp] = {
                "test_name": issue["test_name"],
                "error_msg": issue["error_msg"],
                "category": issue["category"],
                "versions": [],
                "jobs": [],
            }
        if _ver and _ver not in unique_issues[fp]["versions"]:
            unique_issues[fp]["versions"].append(_ver)
        unique_issues[fp]["jobs"].append(
            {"name": issue["job_name"], "url": issue["job_url"], "version": _ver}
        )

    print(f"  {len(unique_issues)} unique issues (deduplicated across jobs)")

    new_issues: dict[str, dict] = {}
    reused_count = 0
    for fp, issue_data in unique_issues.items():
        if is_known_issue(fp_db, fp) and not FORCE_REANALYZE:
            prev = get_issue(fp_db, fp)
            saved_ai = prev.get("ai_summary_short", "") or prev.get("ai_summary", "")

            if not saved_ai:
                print(f"  RE-ANALYZE (empty analysis): {issue_data['test_name'][:60]}")
                new_issues[fp] = issue_data
                continue

            for j in issue_data["jobs"]:
                mark_issue_seen(fp_db, fp, j["name"], j["url"])
            reused_count += 1
            short = issue_data["test_name"][:60]
            print(f"  SKIP (recurring #{prev.get('occurrences', 1) + 1}): {short}")
            for j in issue_data["jobs"]:
                for job in failed:
                    if job["name"] == j["name"]:
                        job.setdefault("analysis", {}).setdefault("issues", []).append(
                            {
                                "fingerprint": fp,
                                "test_name": issue_data["test_name"],
                                "is_recurring": True,
                                "first_seen": prev.get("first_seen", ""),
                                "occurrences": prev.get("occurrences", 1) + 1,
                                "classification": prev.get("classification", "unknown"),
                                "root_cause": prev.get("root_cause", ""),
                                "ai_summary": saved_ai,
                            }
                        )
        else:
            new_issues[fp] = issue_data

    print(f"  {reused_count} recurring (skipped), {len(new_issues)} NEW to investigate")

    job_groups: dict[str, list[tuple[str, dict]]] = {}
    for fp, issue_data in new_issues.items():
        target_name = ""
        for j in issue_data["jobs"]:
            for job in failed:
                if job["name"] == j["name"]:
                    target_name = j["name"]
                    break
            if target_name:
                break
        if not target_name:
            target_name = f"_no_job_{fp[:8]}"
        job_groups.setdefault(target_name, []).append((fp, issue_data))

    MAX_AI_CALLS_PER_RUN = int(os.environ.get("MAX_AI_CALLS", "8"))

    if len(job_groups) > MAX_AI_CALLS_PER_RUN:
        sorted_groups = sorted(job_groups.items(), key=lambda x: -len(x[1]))
        deferred = sorted_groups[MAX_AI_CALLS_PER_RUN:]
        job_groups = dict(sorted_groups[:MAX_AI_CALLS_PER_RUN])
        deferred_issues = sum(len(fps) for _, fps in deferred)
        print(
            f"  CAP: analyzing {MAX_AI_CALLS_PER_RUN}/{MAX_AI_CALLS_PER_RUN + len(deferred)} "
            f"jobs this run ({deferred_issues} issues deferred to next run)"
        )

    print(f"  Grouped into {len(job_groups)} AI calls (by target job)")

    success_count = 0
    call_num = 0
    for target_name, fps_in_group in job_groups.items():
        call_num += 1
        test_names = [d["test_name"][:50] for _, d in fps_in_group]
        print(
            f"  [{call_num}/{len(job_groups)}] {target_name.split('nightly-')[-1][:35]} "
            f"({len(fps_in_group)} issues: {', '.join(test_names[:3])}{'...' if len(test_names) > 3 else ''})"
        )

        target_job = None
        for job in failed:
            if job["name"] == target_name:
                target_job = job
                break
        if not target_job:
            print(f"    No job data found — skipping")
            continue

        all_versions = []
        for _, issue_data in fps_in_group:
            for v in issue_data.get("versions", []):
                if v not in all_versions:
                    all_versions.append(v)
        target_job["_affected_versions"] = all_versions

        ai = analyze_job(target_job)
        if ai:
            root_cause = _extract_root_cause(ai)
            classification = _extract_classification(ai)
            is_flake = _extract_is_flake(ai)

            for fp, issue_data in fps_in_group:
                test_name = issue_data["test_name"]
                affected_jobs = issue_data["jobs"]

                for j in affected_jobs:
                    record_issue(
                        fp_db,
                        fp,
                        test_name=test_name,
                        job_name=j["name"],
                        job_url=j["url"],
                        classification=classification,
                        root_cause=root_cause,
                        ai_summary=ai,
                        is_flake=is_flake,
                    )

                for j in affected_jobs:
                    for job in failed:
                        if job["name"] == j["name"]:
                            job.setdefault("analysis", {}).setdefault(
                                "issues", []
                            ).append(
                                {
                                    "fingerprint": fp,
                                    "test_name": test_name,
                                    "is_recurring": False,
                                    "classification": classification,
                                    "root_cause": root_cause,
                                    "ai_summary": ai[:50000],
                                    "is_flake": is_flake,
                                }
                            )

            success_count += len(fps_in_group)
            print(
                f"    Done ({len(ai)} chars) → class={classification}, flake={is_flake}"
            )

            save_db(fp_db)
            with open(RESULTS, "w") as _f:
                json.dump(data, _f, indent=2)
        else:
            print(f"    No analysis returned")

    print(
        f"Results updated: {success_count}/{len(new_issues)} new issues analyzed, "
        f"{reused_count} recurring reused"
    )


def main():
    if not os.path.exists(RESULTS):
        print(f"No results at {RESULTS}")
        sys.exit(1)

    file_size = os.path.getsize(RESULTS)
    if file_size > MAX_RESULTS_SIZE:
        print(
            f"results.json is {file_size / 1024 / 1024:.0f} MB — too large "
            f"(limit {MAX_RESULTS_SIZE // 1024 // 1024} MB)"
        )
        sys.exit(1)

    if not check_auth():
        print("Vertex AI auth failed. Set GOOGLE_APPLICATION_CREDENTIALS or run:")
        print("  gcloud auth application-default login")
        sys.exit(1)

    _checkout_source_repos()

    with open(RESULTS) as f:
        data = json.load(f)

    failed = [j for j in data.get("jobs", []) if j["state"] in ("failure", "error")]

    if MIN_VERSION:
        min_parts = [int(x) for x in MIN_VERSION.split(".")]

        def _version_ok(job_name):
            m = re.search(r"(\d+)\.(\d+)", job_name)
            if not m:
                return True
            return [int(m.group(1)), int(m.group(2))] >= min_parts

        before = len(failed)
        failed = [j for j in failed if _version_ok(j["name"])]
        if len(failed) < before:
            print(f"Filtered {before - len(failed)} job(s) below MIN_VERSION {MIN_VERSION}")

    print(f"Found {len(failed)} failure(s) to analyze")

    fp_db = load_db()
    _analyze_per_issue(data, failed, fp_db)

    save_db(fp_db)

    with open(RESULTS, "w") as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
