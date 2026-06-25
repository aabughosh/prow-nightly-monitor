#!/usr/bin/env python3
"""Run Cursor CLI agent on each failed job with full CI evidence.

For each failure, dumps all downloaded artifacts into ci-evidence/ inside the
target repo checkout so the agent can read them alongside the source code.
The agent has full tool access: it can read files, run shell commands (curl, grep),
search code, fetch full logs from Prow, and write code fixes directly.

Set TARGET_REPO to the repo under test (e.g. https://github.com/openshift-kni/commatrix.git).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from fingerprint import (
    compute_fingerprint, load_db, save_db, is_known,
    get_previous_analysis, record_fingerprint, mark_seen,
    group_by_class,
    # Per-issue fingerprinting (for commatrix)
    compute_issue_fingerprint, extract_issues_from_job,
    is_known_issue, get_issue, record_issue, mark_issue_seen,
    _extract_root_cause, _extract_classification, _extract_is_flake,
)

CURSOR_CLI = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR = os.path.expanduser("~/Documents/GitHub/prow-nightly-monitor")
TARGET_REPO = os.environ.get("TARGET_REPO", "")
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "openshift-kni/commatrix")
INVESTIGATE_DIR = "/tmp/ci-investigate"
EVIDENCE_DIR = os.path.join(INVESTIGATE_DIR, "ci-evidence")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", f"{REPO_DIR}/public")
RESULTS = os.path.join(OUTPUT_DIR, "results.json")

MAX_RESULTS_SIZE = 50 * 1024 * 1024
AGENT_TIMEOUT = 1800  # 30 minutes per issue
MIN_VERSION = os.environ.get("MIN_VERSION", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://aabughosh.github.io/prow-nightly-monitor/cursor/")
FORCE_REANALYZE = os.environ.get("FORCE_REANALYZE", "false").lower() == "true"


def check_auth() -> bool:
    """Verify the Cursor CLI is authenticated before running agents."""
    try:
        result = subprocess.run(
            [CURSOR_CLI, "agent", "status"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and "Logged in" in result.stdout:
            print(f"  Auth OK: {result.stdout.strip()}")
            return True
        print(f"  Auth failed: {result.stdout.strip()} {result.stderr.strip()}")
    except Exception as e:
        print(f"  Auth check error: {e}")
    return False


def run_cursor_agent(prompt: str, cwd: str = INVESTIGATE_DIR) -> str:
    """Run Cursor CLI agent with full tool access. Returns the response text.

    Writes the prompt to ci-evidence/prompt.txt and tells the agent to read it,
    avoiding shell argument length limits on long prompts.
    """
    import signal

    prompt_file = os.path.join(cwd, "ci-evidence", "prompt.txt")
    os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
    with open(prompt_file, "w") as f:
        f.write(prompt)

    short_prompt = (
        "Read the file ./ci-evidence/prompt.txt for your full instructions. "
        "Follow them exactly. Write your final analysis to stdout."
    )

    try:
        proc = subprocess.Popen(
            [CURSOR_CLI, "agent", "--trust", "--yolo", "--print", short_prompt],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=cwd, preexec_fn=os.setsid,
        )
        try:
            stdout, stderr = proc.communicate(timeout=AGENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            print(f"    Agent timed out after {AGENT_TIMEOUT}s")
            return ""

        if proc.returncode != 0:
            print(f"    Agent exit code {proc.returncode}")
            if stderr:
                print(f"    stderr: {stderr[:500]}")
            if stdout:
                return _dedup_output(stdout.strip())
            return ""

        return _dedup_output(stdout.strip()) if stdout.strip() else ""
    except Exception as e:
        print(f"    Agent error: {e}")
    return ""


def _dedup_output(text: str) -> str:
    """Remove repetition loops from AI agent output.

    The Cursor CLI sometimes gets stuck repeating the same analysis block.
    Detects repetition by finding repeated structural anchors and keeps the
    longest complete block.
    """
    import re as _re

    # All possible repetition boundaries -- preambles AND structural markers
    boundary_re = _re.compile(
        r"(?:(?:Now )?I (?:now )?have (?:all the |sufficient |enough )?(?:evidence|information)[^\n]*"
        r"|Let me (?:write|compile) (?:the |all )?(?:final |full )?analysis[^\n]*"
        r"|Here is the (?:final |full )?analysis[^\n]*)",
        _re.IGNORECASE,
    )

    # Structural anchors that should appear only ONCE in a clean output.
    # Ordered by specificity -- prefer splitting on unique top-level markers first.
    anchors = [
        "Cross-Suite Summary",
        "Per-Suite Details",
        "Actionable Recommendations",
        "Cross-Suite Patterns",
        "Overall Severity:",
    ]

    # Find the best anchor to split on (the one that repeats most)
    best_anchor = None
    best_count = 1
    for anchor in anchors:
        count = text.count(anchor)
        if count > best_count:
            best_anchor = anchor
            best_count = count

    if best_anchor and best_count > 1:
        # Split on the repeating structural anchor
        parts = text.split(best_anchor)
        # Reconstruct blocks: each block starts with the anchor
        blocks = []
        for i in range(1, len(parts)):
            # Find the end of this block (next occurrence or end)
            end_idx = i + 1
            block = best_anchor + parts[i]
            # If next part starts with content before the anchor, include it
            if i == 1 and parts[0].strip():
                block = parts[0].rstrip() + "\n" + block
            blocks.append(block)

        if blocks:
            # Pick the longest block (most complete)
            best = max(blocks, key=len)
            print(f"    Dedup: '{best_anchor}' repeated {best_count}x, "
                  f"keeping best ({len(best)} chars out of {len(text)} total)")
            text = best

    # Also strip preamble markers
    preamble_positions = [m.start() for m in boundary_re.finditer(text)]

    if len(preamble_positions) > 1:
        # Multiple preambles remain -- extract blocks, keep longest
        blocks = []
        for i, pos in enumerate(preamble_positions):
            end = preamble_positions[i + 1] if i + 1 < len(preamble_positions) else len(text)
            block = text[pos:end]
            first_nl = block.find("\n")
            content = block[first_nl:].strip() if first_nl != -1 else block.strip()
            blocks.append(content)
        text = max(blocks, key=len)
        print(f"    Dedup: stripped {len(preamble_positions)} preamble markers")

    # Strip remaining preamble fragments
    text = boundary_re.sub("", text).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")

    return text[:16000]


MAX_EVIDENCE_FILES = 12

_SKIP_PATTERNS = (
    "gather-extra/", "gather-extra__",
    "baremetalds-", "aws-deprovision", "ofcir-",
    "ipi-conf", "cloud-init",
    "cluster-setup/", "cluster-setup__",
    ".html",
    "/artifacts/",  # skip duplicate nested artifacts/ copies
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
    """Return True if this artifact is worth including as evidence."""
    key_lower = key.lower()
    for pat in _SKIP_PATTERNS:
        if pat in key_lower:
            return False
    for pat in _KEEP_PATTERNS:
        if pat in key_lower:
            return True
    return False


def dump_evidence(job: dict) -> list[str]:
    """Write essential CI artifacts to ci-evidence/ for the agent.

    Filters out irrelevant cluster metadata to keep evidence lean and fast.
    Returns a list of filenames for the prompt.
    """
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
        import re as _re
        import requests as _req
        m = _re.search(r"/logs/(.+)/(\d+)$", prow_url)
        if m:
            job_path, build_id = m.group(1), m.group(2)
            gcs_base = "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs"
            gcs_raw = f"https://storage.googleapis.com/test-platform-results/logs"
            urls = [
                f"Prow UI: {prow_url}",
                f"Artifacts (browse): {gcs_base}/{job_path}/{build_id}/artifacts/",
                f"Artifacts (raw curl): {gcs_raw}/{job_path}/{build_id}/artifacts/",
                f"Build log: {gcs_raw}/{job_path}/{build_id}/build-log.txt",
                f"Tip: to curl a file, use: curl -s {gcs_raw}/{job_path}/{build_id}/artifacts/<workflow>/<step>/artifacts/<file>",
            ]
            with open(os.path.join(EVIDENCE_DIR, "prow-urls.txt"), "w") as f:
                f.write("\n".join(urls))
            evidence_files.append("prow-urls.txt")

            # Fetch the top-level build log — beginning (image builds/pulls) + end (step results)
            try:
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
        lines = []
        lines.append(f"Severity: {inv.get('severity', '?')}")
        lines.append(f"Fix type: {inv.get('fix_type', '?')}")
        lines.append(f"Suggested fix: {inv.get('suggested_fix', 'N/A')}")
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
        for key_name, label in [("no_endpointslice_ports", "Ports open but missing EndpointSlice"),
                                ("stale_ports", "Ports in matrix but not in use on node"),
                                ("undocumented_ports", "Ports in use but not in matrix")]:
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
        lines = []
        for sf in ss_findings:
            lines.append(f"Port {sf['port']}: {sf['ss_line']}")
            lines.append(f"  Matrix entry: {sf.get('entry', '')}")
        with open(os.path.join(EVIDENCE_DIR, "ss-port-analysis.txt"), "w") as f:
            f.write("\n".join(lines))
        evidence_files.append("ss-port-analysis.txt")

    all_ports = _build_port_map(matrix_diff, ss_findings)
    if all_ports:
        with open(os.path.join(EVIDENCE_DIR, "port-map.txt"), "w") as f:
            f.write(all_ports)
        evidence_files.append("port-map.txt")

    return evidence_files


def _build_port_map(matrix_diff: dict, ss_findings: list[dict]) -> str:
    """Cross-reference all port data into a single view per port."""
    if not matrix_diff and not ss_findings:
        return ""

    ports: dict[str, dict] = {}

    def _extract_port(entry: str) -> str:
        parts = entry.split(",")
        return parts[2].strip() if len(parts) >= 3 else ""

    for p in matrix_diff.get("no_endpointslice_ports", []):
        pn = _extract_port(p)
        if pn:
            ports.setdefault(pn, {"number": pn, "issues": [], "ss": "", "entries": []})
            ports[pn]["issues"].append("NO_ENDPOINTSLICE")
            ports[pn]["entries"].append(p)

    for p in matrix_diff.get("stale_ports", []):
        pn = _extract_port(p)
        if pn:
            ports.setdefault(pn, {"number": pn, "issues": [], "ss": "", "entries": []})
            ports[pn]["issues"].append("STALE_IN_MATRIX")
            ports[pn]["entries"].append(p)

    for p in matrix_diff.get("undocumented_ports", []):
        pn = _extract_port(p)
        if pn:
            ports.setdefault(pn, {"number": pn, "issues": [], "ss": "", "entries": []})
            ports[pn]["issues"].append("UNDOCUMENTED")
            ports[pn]["entries"].append(p)

    for sf in ss_findings:
        pn = sf.get("port", "")
        if pn:
            ports.setdefault(pn, {"number": pn, "issues": [], "ss": "", "entries": []})
            ports[pn]["ss"] = sf.get("ss_line", "")

    if not ports:
        return ""

    lines = ["UNIFIED PORT MAP — each port with all its data in one place", ""]
    for pn in sorted(ports, key=lambda x: int(x) if x.isdigit() else 0):
        info = ports[pn]
        ephemeral = "YES" if pn.isdigit() and 32768 <= int(pn) <= 60999 else "no"
        lines.append(f"PORT {pn}:")
        lines.append(f"  Issues: {', '.join(info['issues'])}")
        lines.append(f"  Ephemeral range: {ephemeral}")
        if info["ss"]:
            lines.append(f"  Socket state: {info['ss']}")
        for e in info["entries"]:
            lines.append(f"  Matrix entry: {e}")
        lines.append(f"  --> Needs decision: ADD / REMOVE / SKIP / INVESTIGATE")
        lines.append("")

    return "\n".join(lines)


def _load_project_config() -> dict:
    """Load the project config for the current JOB_FILTER."""
    config_path = Path(__file__).parent / "projects.json"
    if not config_path.exists():
        return {}
    import json as _j
    all_projects = _j.loads(config_path.read_text())
    job_filter = os.environ.get("JOB_FILTER", "")
    for pconf in all_projects.values():
        if pconf.get("job_filter", "") and pconf["job_filter"] in job_filter:
            return pconf
    for pconf in all_projects.values():
        if job_filter in pconf.get("job_filter", ""):
            return pconf
    return {}


def build_prompt(job: dict, evidence_files: list[str]) -> str:
    """Build a focused prompt for fast CI failure analysis."""
    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    category = analysis.get("category", "")
    reason = analysis.get("reason", "")

    tests_list = inv.get("failed_tests", []) or analysis.get("junit_failures", [])
    failed_tests = "\n".join(
        f"  - {t.get('name', '?')}: {t.get('message', '')[:500]}"
        for t in tests_list
    )

    evidence_listing = "\n".join(f"  - {f}" for f in evidence_files)

    project = _load_project_config()
    project_desc = project.get("description", "N/A") if project else "N/A"
    hint = ""
    related_repos_info = ""
    if project:
        hints = project.get("classification_hints", {})
        if category in hints:
            hint = f"\nHint: {hints[category]}"
        related = project.get("related_repos", [])
        if related:
            repo_names = [r.rstrip("/").split("/")[-1].replace(".git", "") for r in related]
            related_repos_info = (
                "\n**Related source repos (cloned locally for you to search):**\n"
                + "\n".join(f"  - ./{name}/" for name in repo_names)
            )

    version = ""
    import re as _re_ver
    _ver_match = _re_ver.search(r"nightly-(\d+\.\d+)", job['name'])
    if _ver_match:
        version = _ver_match.group(1)

    # If multiple versions affected, tell the AI to compare them
    versions_list = job.get("_affected_versions", [])
    if versions_list and len(versions_list) > 1:
        ver_str = ", ".join(sorted(versions_list))
        version_instruction = (
            f"This test fails on OCP versions: {ver_str}. "
            f"Evidence is from version {version}. "
            f"In your analysis, note which versions are affected and whether the root cause is the same across versions."
        )
    else:
        version_instruction = f"Focus on OCP version {version or 'unknown'}."

    prompt = f"""Analyze this CI failure. Evidence files are in ./ci-evidence/ — read them directly.
{version_instruction}

**Project:** {project_desc}{hint}
**Source repo:** https://github.com/{UPSTREAM_REPO}
{related_repos_info}
You have the source code cloned locally. Search it with grep/find to find the test code, recent commits, and relevant functions.

**Job:** {job['name']}
**Prow URL:** {job.get('url', 'N/A')}
**Category:** {category}
**Reason:** {reason}

**Failed tests:**
{failed_tests or '(none extracted)'}

**Log snippet:**
{analysis.get('log_snippet', '(no log)')[:800]}

**Evidence files available in ./ci-evidence/:**
{evidence_listing}

CRITICAL FIRST STEP — determine the REAL failure source:
1. Read ci-operator-build-log.txt FIRST — it shows ALL job steps, which passed, which failed, and the final error. Pay special attention to the BEGINNING of the log which shows image builds and pulls.
2. CHECK FOR IMAGE PULL FAILURES: Look for "error", "failed", "not found", "unauthorized", "ImagePull" in the first half of ci-operator-build-log.txt. If images from registry.ci failed to build or pull, that is the root cause — it means the operator/daemon never deployed and all downstream failures (DaemonSet not found, timeouts, gather failures) are cascades.
3. Check finished.json in the test step. If it says "passed":true / "result":"SUCCESS", the PROJECT's tests passed!
4. If the project's tests passed but the job still failed, the failure is from the CI FRAMEWORK (MonitorTest, operator-state-analyzer, lease-checker, openshift-e2e, etc.) — NOT from {UPSTREAM_REPO.split('/')[-1]} code.
5. For CI framework failures: classify as infra_other, explain that the project tests passed, and describe what CI component actually failed (e.g. "MonitorTest detected node-not-ready during intentional reboot").
6. Only investigate source code regressions if the PROJECT's OWN tests actually failed (check test_results.json, junit.xml in the test step).

You can use `curl` to fetch more details from the Prow job: {job.get('url', '')}
The ci-operator-build-log.txt contains both the BEGINNING (first 8KB — image builds/pulls) and END (last 8KB — step results/failures) of the top-level build log.

Read the evidence files (especially JUnit XML, test_results.json, build-log.txt, and finished.json). Find the EXACT test names, error messages, and line numbers from the source code.
Then search the source repo for the relevant test code and recent PRs/commits that may have caused the regression.

DEEP INVESTIGATION RULE — DO NOT just report the test error message. You MUST trace back to the UPSTREAM cause:
- If tests say "pod not found" or "DaemonSet not found" → WHY wasn't it created? Check: image pull errors, operator crash logs, deployment failures in ci-operator-build-log.txt and pod-logs.
- If tests say "timeout" or "not ready" → WHY didn't it become ready? Check: pod status, events, operator logs.
- Check pod-logs/ files in ci-evidence/ — they contain operator/daemon container logs showing crashes, image pulls, or startup failures.
- Use `curl` to fetch more files from GCS if needed. See prow-urls.txt for the raw URL pattern. Example: `curl -s <raw_artifacts_url>/<workflow>/<step>/artifacts/pod-logs/<pod-name>.log`
- NEVER conclude "cluster error state" or "systematic failure" without explaining WHAT caused it. The user needs the FIRST domino, not the last.

EVIDENCE RULE: You MUST present verbatim log quotes as proof for every conclusion. Do NOT guess or assume root causes — find the actual error in the logs.

CRITICAL IMAGE-PULL CHECK: If a DaemonSet or Pod was "not found" or never created, the #1 cause is an IMAGE PULL FAILURE from registry.ci (e.g. "image not found", "manifest unknown", "unauthorized", "ErrImagePull"). Look at the BEGINNING of ci-operator-build-log.txt for lines containing "error", "failed to pull", "registry.ci", "ImagePullBackOff", or "tag not found". Do NOT conclude "no PTP-capable NICs" or "hardware issue" unless you have explicit log evidence that the operator started successfully and detected no hardware — an image pull failure prevents the operator from ever running.

If the failure is from the CI framework (not the project's tests), state "PROJECT TESTS PASSED — failure is from CI framework".

CRITICAL OUTPUT RULES:
1. Write your analysis ONCE. Do NOT restart or repeat. Output each section exactly once.
2. GROUPING: First scan ALL failed tests. Group them by root cause. If N tests share the same error (e.g. all hit "i/o timeout"), write ONE deep analysis for the group, not N separate analyses.
3. Keep it concise — max 6 lines per field.

Respond with EXACTLY this format (write it ONCE, do not repeat):

---

**TL;DR:** One sentence (max 15 words) summarizing all failures

**Root Cause Groups:**
For each distinct root cause, list:
- **Group N: [root cause name]** (affects Tests: list them)
  - **Evidence:** verbatim log lines proving this root cause
  - **Root Cause:** 2-3 sentences. Reference function/file/line. Explain the FIRST domino.
  - **Breaking PR/Commit:** link or "Unknown"
  - **Is it a flake?** yes/no — one sentence
  - **Suggested Fix:** 1-2 sentences
  - **Affected tests:** list each test name in this group

If ALL tests share ONE root cause, write ONE group. Do NOT write separate sections per test.
If tests have DIFFERENT root causes, write one group per distinct cause.

---

**Relation Between Groups:** How do the root cause groups relate? Common upstream cause?
**Per-Version Notes:** one line per version
**Affected Images:** container images list
**Overall Issue Class:** infra_timeout | infra_quota | infra_other | test_regression | test_flake | test_failure | matrix_mismatch | build_error | unknown
**Overall Severity:** CRITICAL / HIGH / MEDIUM / LOW
"""

    return prompt






def _group_tests_by_suite(job: dict) -> dict[str, list[dict]]:
    """Group failed tests by suite prefix (e.g. dualfollower, dualnicbc, tgm).

    Extracts the suite name from test names like:
      [It] [dualnicbc-serial] PTP e2e tests ...  → suite "dualnicbc"
      [dualfollower-parallel] Event based tests   → suite "dualfollower"
    Falls back to a single "all" group if no suite prefixes found.
    """
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


def build_suite_prompt(
    job: dict, evidence_files: list[str], suite_name: str, suite_tests: list[dict]
) -> str:
    """Build a prompt for analyzing one test suite's failures."""
    analysis = job.get("analysis", {})
    category = analysis.get("category", "")
    reason = analysis.get("reason", "")

    failed_tests = "\n".join(
        f"  - {t.get('name', '?')}: {t.get('message', '')[:400]}"
        for t in suite_tests
    )

    evidence_listing = "\n".join(f"  - {f}" for f in evidence_files)

    project = _load_project_config()
    project_desc = project.get("description", "N/A") if project else "N/A"
    related_repos_info = ""
    if project:
        related = project.get("related_repos", [])
        if related:
            repo_names = [r.rstrip("/").split("/")[-1].replace(".git", "") for r in related]
            related_repos_info = (
                "\n**Related source repos (cloned locally for you to search):**\n"
                + "\n".join(f"  - ./{name}/" for name in repo_names)
            )

    version = ""
    _ver_match = re.search(r"nightly-(\d+\.\d+)", job['name'])
    if _ver_match:
        version = _ver_match.group(1)

    return f"""Analyze the **{suite_name}** test suite failures from this CI job.
Evidence files are in ./ci-evidence/ — read them directly.
Focus on OCP version {version or 'unknown'}.

**Project:** {project_desc}
**Source repo:** https://github.com/{UPSTREAM_REPO}
{related_repos_info}
You have the source code cloned locally. Search it with grep/find.

**Job:** {job['name']}
**Prow URL:** {job.get('url', 'N/A')}
**Suite:** {suite_name} ({len(suite_tests)} failed tests)

**Failed tests in this suite:**
{failed_tests}

**Evidence files in ./ci-evidence/:**
{evidence_listing}

INSTRUCTIONS:
1. Read ci-operator-build-log.txt FIRST to understand what happened at the job level.
2. Check for image pull failures in the first half of the build log.
3. Read the JUnit XML for this suite (test_results_{suite_name}.xml if it exists).
4. Search the source repo for test code and recent commits.

DEEP INVESTIGATION: Trace back to the UPSTREAM cause. If tests say "timeout" or "not found", find WHY.
EVIDENCE RULE: Quote verbatim log lines as proof. Do NOT guess.

CRITICAL OUTPUT RULES:
1. Write your analysis ONCE. Do NOT restart or repeat.
2. GROUPING: First scan ALL {len(suite_tests)} tests. Group by root cause. If N tests share the same error, write ONE deep analysis for the group — not N copies.

Respond with EXACTLY this format (write it ONCE, do not repeat):

---

### Suite: {suite_name} ({len(suite_tests)} failures)

**Suite TL;DR:** One sentence summarizing this suite's failures

**Root Cause Groups:**
For each distinct root cause:
- **Group N: [root cause name]** (affects: list test names)
  - **Evidence:** verbatim log lines
  - **Root Cause:** 2-3 sentences. Reference function/file/line.
  - **Breaking PR/Commit:** link or "Unknown"
  - **Is it a flake?** yes/no
  - **Suggested Fix:** 1-2 sentences

**Suite Issue Class:** infra_timeout | infra_other | test_regression | test_flake | test_failure | build_error | unknown
**Suite Severity:** CRITICAL / HIGH / MEDIUM / LOW
"""


def build_summary_prompt(
    job: dict, suite_analyses: dict[str, str], total_failures: int
) -> str:
    """Build a prompt for the cross-suite summary."""
    version = ""
    _ver_match = re.search(r"nightly-(\d+\.\d+)", job['name'])
    if _ver_match:
        version = _ver_match.group(1)

    suite_sections = ""
    for suite_name, analysis in suite_analyses.items():
        suite_sections += f"\n\n--- {suite_name} ---\n{analysis[:3000]}"

    return f"""You are given per-suite AI analyses of a CI job with {total_failures} total test failures.
Your task is to produce a CROSS-SUITE SUMMARY that identifies common patterns.

**Job:** {job['name']}
**Version:** {version}
**Total failures:** {total_failures}
**Suites analyzed:** {', '.join(suite_analyses.keys())}

INDIVIDUAL SUITE ANALYSES:
{suite_sections}

Based on the above, produce this summary:

**TL;DR:** One sentence (max 20 words) covering the whole job
**Root Cause Groups:** Group the {total_failures} failures by root cause. Example:
  - "18/25 failures: GM in FREERUN (clock class 248), cascading to all BC/slave sync tests"
  - "7/25 failures: pmc argument bug in commit 3b93c9f4"
**Cross-Suite Patterns:** Which suites share root causes? Are any suite-specific?
**Actionable Recommendations:** 1-3 concrete steps to fix the most failures
**Overall Issue Class:** infra_timeout | infra_other | test_regression | test_flake | test_failure | build_error | unknown
**Overall Severity:** CRITICAL / HIGH / MEDIUM / LOW
"""


def analyze_job(job: dict) -> str:
    """Deep investigation with per-suite splitting for large jobs.

    For jobs with many failures across multiple test suites, runs one AI call
    per suite (keeping each call small enough for clean output), then produces
    a cross-suite summary.
    """
    evidence_files = dump_evidence(job)
    print(f"    Dumped {len(evidence_files)} evidence files")

    suites = _group_tests_by_suite(job)

    # If only one suite or few total tests, use the original single-call approach
    total_tests = sum(len(tests) for tests in suites.values())
    if len(suites) <= 1 and total_tests <= MAX_SUITE_TESTS:
        prompt = build_prompt(job, evidence_files)
        result = run_cursor_agent(prompt)
        _reset_repo()
        return result

    print(f"    Split into {len(suites)} suites: "
          f"{', '.join(f'{s}({len(t)})' for s, t in suites.items())}")

    # Per-suite analysis
    suite_analyses: dict[str, str] = {}
    for suite_name, suite_tests in suites.items():
        print(f"    Suite '{suite_name}' ({len(suite_tests)} tests)...")
        prompt = build_suite_prompt(job, evidence_files, suite_name, suite_tests)
        result = run_cursor_agent(prompt)
        if result:
            suite_analyses[suite_name] = result
            print(f"      Done ({len(result)} chars)")
        else:
            print(f"      No analysis returned")
        _reset_repo()

    if not suite_analyses:
        return ""

    # Cross-suite summary
    print(f"    Generating cross-suite summary...")
    summary_prompt = build_summary_prompt(job, suite_analyses, total_tests)
    summary = run_cursor_agent(summary_prompt)
    _reset_repo()

    # Combine: summary first, then each suite's detail
    parts = []
    if summary:
        parts.append(summary)
    parts.append("\n\n---\n\n# Per-Suite Details\n")
    for suite_name, analysis in suite_analyses.items():
        parts.append(f"\n## {suite_name}\n\n{analysis}")

    return "\n".join(parts)


def _reset_repo():
    """Reset the investigation repo to clean state between agent calls."""
    subprocess.run(["git", "checkout", "."], cwd=INVESTIGATE_DIR,
                   capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=INVESTIGATE_DIR,
                   capture_output=True)


def _checkout_source_repos() -> None:
    """Clone target repo + related repos so the agent can search source code."""
    if not TARGET_REPO:
        os.makedirs(INVESTIGATE_DIR, exist_ok=True)
        os.makedirs(os.path.join(INVESTIGATE_DIR, "ci-evidence"), exist_ok=True)
        return

    if not os.path.exists(INVESTIGATE_DIR):
        print(f"  Cloning {TARGET_REPO} ...")
        subprocess.run(["git", "clone", "--depth=1", TARGET_REPO,
                       INVESTIGATE_DIR], capture_output=True)
    else:
        subprocess.run(["git", "pull", "--ff-only"], cwd=INVESTIGATE_DIR,
                       capture_output=True)

    os.makedirs(os.path.join(INVESTIGATE_DIR, "ci-evidence"), exist_ok=True)

    # Clone related repos (e.g. linuxptp-daemon, cloud-event-proxy) for source search
    project = _load_project_config()
    if project:
        related = project.get("related_repos", [])
        for repo_url in related:
            repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
            repo_path = os.path.join(INVESTIGATE_DIR, repo_name)
            if not os.path.exists(repo_path):
                print(f"  Cloning related: {repo_name} ...")
                subprocess.run(["git", "clone", "--depth=1", repo_url, repo_path],
                               capture_output=True)


def main():
    if not os.path.exists(RESULTS):
        print(f"No results at {RESULTS}")
        sys.exit(1)

    file_size = os.path.getsize(RESULTS)
    if file_size > MAX_RESULTS_SIZE:
        print(f"results.json is {file_size / 1024 / 1024:.0f} MB — too large (limit {MAX_RESULTS_SIZE // 1024 // 1024} MB)")
        print("Delete it and re-run monitor.py first.")
        sys.exit(1)

    if not check_auth():
        print("Cursor CLI not authenticated. Run:")
        print(f"  {CURSOR_CLI} agent login")
        sys.exit(1)

    _checkout_source_repos()

    with open(RESULTS) as f:
        data = json.load(f)

    failed = [j for j in data.get("jobs", []) if j["state"] in ("failure", "error")]

    # Filter by min version — don't waste AI tokens on old versions
    if MIN_VERSION:
        min_parts = [int(x) for x in MIN_VERSION.split(".")]
        def _version_ok(job_name):
            import re as _re
            m = _re.search(r"(\d+)\.(\d+)", job_name)
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


def _analyze_per_job(data: dict, failed: list[dict], fp_db: dict) -> None:
    """Legacy per-job analysis (for PTP and similar projects)."""
    new_failures = []
    reused_count = 0

    for job in failed:
        fp = compute_fingerprint(job)
        job.setdefault("analysis", {})["fingerprint"] = fp

        if is_known(fp_db, fp):
            prev = get_previous_analysis(fp_db, fp)
            mark_seen(fp_db, fp)
            job["analysis"]["ai_summary"] = (
                f"[Recurring issue — seen {prev.get('occurrences', 1)+1}x] "
                f"{prev.get('root_cause', prev.get('ai_summary_short', '')[:200])}"
            )
            job["analysis"]["is_recurring"] = True
            job["analysis"]["first_seen"] = prev.get("first_seen", "")
            job["analysis"]["occurrences"] = prev.get("occurrences", 1) + 1
            reused_count += 1
            short = re.sub(r"periodic-ci-openshift-release-main-nightly-", "", job["name"])
            print(f"  SKIP (recurring #{prev.get('occurrences',1)+1}): {short[:60]}")
        else:
            new_failures.append(job)

    print(f"  {reused_count} recurring (skipped), {len(new_failures)} NEW to investigate")

    groups = group_by_class(new_failures)
    if groups:
        print("  Issue classes:")
        for cls, jobs_in_cls in sorted(groups.items(), key=lambda x: -len(x[1])):
            print(f"    {cls}: {len(jobs_in_cls)} failure(s)")

    success_count = 0
    for i, job in enumerate(new_failures, 1):
        name = job["name"].split("-")[-5:] if len(job["name"]) > 50 else [job["name"]]
        short_name = "-".join(name)
        print(f"  [{i}/{len(new_failures)}] {short_name}...")
        ai = analyze_job(job)
        if ai:
            job.setdefault("analysis", {})["ai_summary"] = ai[:16000]
            fp = job["analysis"].get("fingerprint", compute_fingerprint(job))
            record_fingerprint(fp_db, fp, job, ai)
            success_count += 1
            print(f"    Done ({len(ai)} chars)")
        else:
            print(f"    No analysis returned")

    print(f"Results updated: {success_count}/{len(new_failures)} new failures analyzed, "
          f"{reused_count} recurring reused")


def _analyze_per_issue(data: dict, failed: list[dict], fp_db: dict) -> None:
    """Per-issue analysis for commatrix: fingerprint each test/error individually."""
    # Clear stale issues from previous runs to prevent accumulation
    for job in failed:
        job.setdefault("analysis", {})["issues"] = []

    # Extract all individual issues from all failed jobs
    all_issues: list[dict] = []
    for job in failed:
        issues = extract_issues_from_job(job)
        all_issues.extend(issues)

    print(f"  Extracted {len(all_issues)} individual issues from {len(failed)} failed jobs")

    # Deduplicate: group issues by fingerprint
    # Strategy:
    # - Specific test failures (named tests): version-agnostic so we analyze once
    #   and note per-version differences
    # - Generic "Job failure:" fallbacks (no test names extracted): version-SPECIFIC
    #   because different versions likely have different actual failures that we can
    #   only discover through evidence analysis
    unique_issues: dict[str, dict] = {}  # fp -> first issue dict + list of jobs
    for issue in all_issues:
        _ver = ""
        _ver_m = re.search(r"nightly-(\d+\.\d+)", issue.get("job_name", ""))
        if _ver_m:
            _ver = _ver_m.group(1)

        # If test_name is a generic fallback (no real test identified), use
        # version-specific fingerprint so each version gets its own AI analysis
        is_generic = issue["test_name"].startswith("Job failure:") or \
                     issue["test_name"].startswith("Last log lines:")
        fp_version = _ver if is_generic else ""
        fp = compute_issue_fingerprint(
            issue["test_name"], issue["error_msg"], issue["category"], version=fp_version
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
        unique_issues[fp]["jobs"].append({
            "name": issue["job_name"],
            "url": issue["job_url"],
            "version": _ver,
        })

    print(f"  {len(unique_issues)} unique issues (deduplicated across jobs)")

    # Check which are known vs new
    new_issues: dict[str, dict] = {}
    reused_count = 0
    for fp, issue_data in unique_issues.items():
        if is_known_issue(fp_db, fp) and not FORCE_REANALYZE:
            prev = get_issue(fp_db, fp)
            saved_ai = prev.get("ai_summary_short", "") or prev.get("ai_summary", "")

            # If the issue exists but has no analysis, treat as new (re-analyze)
            if not saved_ai:
                print(f"  RE-ANALYZE (empty analysis): {issue_data['test_name'][:60]}")
                new_issues[fp] = issue_data
                continue

            # Update affected_jobs for each job this issue appears in
            for j in issue_data["jobs"]:
                mark_issue_seen(fp_db, fp, j["name"], j["url"])
            reused_count += 1
            short = issue_data["test_name"][:60]
            print(f"  SKIP (recurring #{prev.get('occurrences',1)+1}): {short}")
            for j in issue_data["jobs"]:
                for job in failed:
                    if job["name"] == j["name"]:
                        job.setdefault("analysis", {}).setdefault("issues", []).append({
                            "fingerprint": fp,
                            "test_name": issue_data["test_name"],
                            "is_recurring": True,
                            "first_seen": prev.get("first_seen", ""),
                            "occurrences": prev.get("occurrences", 1) + 1,
                            "classification": prev.get("classification", "unknown"),
                            "root_cause": prev.get("root_cause", ""),
                            "ai_summary": saved_ai,
                        })
        else:
            new_issues[fp] = issue_data

    print(f"  {reused_count} recurring (skipped), {len(new_issues)} NEW to investigate")

    # Group new issues by target job to avoid duplicate AI calls.
    # Multiple fingerprints from the same job share one AI analysis.
    job_groups: dict[str, list[tuple[str, dict]]] = {}  # job_name -> [(fp, issue_data)]
    for fp, issue_data in new_issues.items():
        # Find the target job name for this issue
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

    print(f"  Grouped into {len(job_groups)} AI calls (by target job)")

    success_count = 0
    call_num = 0
    for target_name, fps_in_group in job_groups.items():
        call_num += 1
        test_names = [d["test_name"][:50] for _, d in fps_in_group]
        print(f"  [{call_num}/{len(job_groups)}] {target_name.split('nightly-')[-1][:35]} "
              f"({len(fps_in_group)} issues: {', '.join(test_names[:3])}{'...' if len(test_names)>3 else ''})")

        # Find the actual job object
        target_job = None
        for job in failed:
            if job["name"] == target_name:
                target_job = job
                break
        if not target_job:
            print(f"    No job data found — skipping")
            continue

        # Merge affected versions from all issues in this group
        all_versions = []
        for _, issue_data in fps_in_group:
            for v in issue_data.get("versions", []):
                if v not in all_versions:
                    all_versions.append(v)
        target_job["_affected_versions"] = all_versions

        # One AI call for this job
        ai = analyze_job(target_job)
        if ai:
            root_cause = _extract_root_cause(ai)
            classification = _extract_classification(ai)
            is_flake = _extract_is_flake(ai)

            # Apply the SAME analysis to all fingerprints in this group
            for fp, issue_data in fps_in_group:
                test_name = issue_data["test_name"]
                affected_jobs = issue_data["jobs"]

                # Record issue in the database
                for j in affected_jobs:
                    record_issue(
                        fp_db, fp,
                        test_name=test_name,
                        job_name=j["name"],
                        job_url=j["url"],
                        classification=classification,
                        root_cause=root_cause,
                        ai_summary=ai,
                        is_flake=is_flake,
                    )

                # Tag all affected jobs with this issue's analysis
                for j in affected_jobs:
                    for job in failed:
                        if job["name"] == j["name"]:
                            job.setdefault("analysis", {}).setdefault("issues", []).append({
                                "fingerprint": fp,
                                "test_name": test_name,
                                "is_recurring": False,
                                "classification": classification,
                                "root_cause": root_cause,
                                "ai_summary": ai[:16000],
                                "is_flake": is_flake,
                            })

            success_count += len(fps_in_group)
            print(f"    Done ({len(ai)} chars) → class={classification}, flake={is_flake}")
        else:
            print(f"    No analysis returned")

    print(f"Results updated: {success_count}/{len(new_issues)} new issues analyzed, "
          f"{reused_count} recurring reused")


def send_slack_summary():
    """Send a daily summary to Slack via Incoming Webhook using Block Kit."""
    from datetime import datetime
    import urllib.request

    if not SLACK_WEBHOOK_URL:
        print("No SLACK_WEBHOOK_URL set — skipping Slack notification")
        return

    if not os.path.exists(RESULTS):
        print(f"No results at {RESULTS}")
        return

    data = json.load(open(RESULTS))
    jobs = data.get("jobs", [])
    passed = sum(1 for j in jobs if j["state"] == "success")
    failed_jobs = [j for j in jobs if j["state"] in ("failure", "error")]
    failed = len(failed_jobs)

    today = datetime.now().strftime("%B %d, %Y")

    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Prow Nightly Monitor — {today}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f":white_check_mark: *{passed}* passed"},
                {"type": "mrkdwn", "text": f":x: *{failed}* failed"},
            ],
        },
        {"type": "divider"},
    ]

    new_failures = [j for j in failed_jobs if not j.get("analysis", {}).get("is_recurring")]
    recurring_failures = [j for j in failed_jobs if j.get("analysis", {}).get("is_recurring")]

    if new_failures:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":new: *{len(new_failures)} new issue(s):*"},
        })

    for j in new_failures:
        analysis = j.get("analysis", {})
        category = analysis.get("category", "unknown").upper()
        severity = analysis.get("investigation", {}).get("severity", "?")
        ai = analysis.get("ai_summary", "")
        pr_url = analysis.get("pr_url", "")

        short_name = re.sub(
            r"periodic-ci-openshift-release-main-nightly-", "", j["name"]
        )

        root_cause = ""
        if ai:
            for al in ai.split("\n"):
                stripped = al.strip()
                if stripped.startswith("**Root Cause"):
                    root_cause = re.sub(r"\*\*Root Cause[^*]*\*\*:?\s*", "", stripped)
                    break
                if stripped.startswith("## Root Cause"):
                    root_cause = stripped.replace("## Root Cause", "").strip()
                    break
            if not root_cause:
                for al in ai.split("\n"):
                    stripped = al.strip()
                    if stripped and not stripped.startswith(("#", "---", "```")):
                        root_cause = stripped[:150]
                        break

        status_emoji = ":red_circle:" if severity in ("HIGH", "CRITICAL") else ":large_orange_circle:" if severity == "MEDIUM" else ":white_circle:"

        job_text = f"{status_emoji} *`{short_name}`*\n>{category} | Severity: *{severity}*"
        if root_cause:
            job_text += f"\n>_{root_cause[:200]}_"

        block: dict = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": job_text},
        }
        if pr_url:
            block["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": ":wrench: View PR"},
                "url": pr_url,
            }
        blocks.append(block)

    if recurring_failures:
        recurring_summary = ", ".join(
            f"`{re.sub(r'periodic-ci-openshift-release-main-nightly-', '', j['name'])[:40]}`"
            for j in recurring_failures[:5]
        )
        if len(recurring_failures) > 5:
            recurring_summary += f" +{len(recurring_failures) - 5} more"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text":
                     f":repeat: *{len(recurring_failures)} recurring* (known issues): {recurring_summary}"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":bar_chart: Open Dashboard"},
                "url": DASHBOARD_URL,
                "style": "primary",
            }
        ],
    })

    payload = json.dumps({"blocks": blocks}).encode("utf-8")

    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Slack notification sent ({resp.status})")
    except Exception as e:
        print(f"Slack notification failed: {e}")


if __name__ == "__main__":
    main()
