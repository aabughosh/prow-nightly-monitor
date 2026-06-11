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
AGENT_TIMEOUT = 600  # 10 minutes per issue
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
                return stdout.strip()[:8000]
            return ""

        return stdout.strip()[:8000] if stdout.strip() else ""
    except Exception as e:
        print(f"    Agent error: {e}")
    return ""


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
    "junit.xml", "test_results", "build-log",
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
        m = _re.search(r"/logs/(.+)/(\d+)$", prow_url)
        if m:
            job_path, build_id = m.group(1), m.group(2)
            gcs_base = "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs"
            urls = [
                f"Prow UI: {prow_url}",
                f"Artifacts: {gcs_base}/{job_path}/{build_id}/artifacts/",
                f"Build log: {gcs_base}/{job_path}/{build_id}/build-log.txt",
            ]
            with open(os.path.join(EVIDENCE_DIR, "prow-urls.txt"), "w") as f:
                f.write("\n".join(urls))
            evidence_files.append("prow-urls.txt")

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

    failed_tests = "\n".join(
        f"  - {t.get('name', '?')}: {t.get('message', '')[:500]}"
        for t in inv.get("failed_tests", [])
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

    prompt = f"""Analyze this CI failure for OCP version {version or 'unknown'}. Evidence files are in ./ci-evidence/ — read them directly.
DO NOT mix analysis with other OCP versions. Focus ONLY on version {version or 'this job'}.

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

Read the evidence files (especially JUnit XML and test_results files). Find the EXACT test names, error messages, and line numbers from the source code.
Then search the source repo for the relevant test code and recent PRs/commits that may have caused the regression.

IMPORTANT: Investigate EACH failed test INDIVIDUALLY. Do NOT lump them together.

Respond with EXACTLY this format (all sections required):

---
FOR EACH FAILED TEST, write a separate section:

### Test 1: `[exact.test.suite] exact test name`
- **Duration:** how long it ran before failing
- **Error:** exact error message or assertion failure
- **Root Cause:** what specifically broke for THIS test. Reference the specific function, file, and line in source code. (3-5 sentences)
- **Breaking PR/Commit:** link to the PR or commit that caused THIS failure (or "Unknown — needs git bisect")
- **Source File:** https://github.com/{UPSTREAM_REPO}/blob/main/path/to/test_file.go#L123 — the test code
- **Is it a flake?** yes/no — with evidence
- **Suggested Fix:** specific actionable fix for THIS test (2-3 sentences)

### Test 2: `[exact.test.suite] exact test name`
(same structure as above)

(repeat for ALL failed tests)

---
AFTER all individual tests, add:

**Relation Between Failures:** Are these tests failing for the same reason? Is there a common root cause, or are they independent issues? (2-4 sentences explaining the connection or lack thereof)

**Affected Images:**
- list the container images involved (e.g. openshift-ptp/linuxptp-daemon:{version})

**Overall Issue Class:** one of: infra_timeout, infra_quota, infra_other, test_regression, test_flake, test_failure, matrix_mismatch, build_error, unknown

**Overall Severity:** CRITICAL / HIGH / MEDIUM / LOW — with justification
"""

    return prompt






def analyze_job(job: dict) -> str:
    """Deep investigation: dump evidence, build prompt, run agent."""
    evidence_files = dump_evidence(job)
    print(f"    Dumped {len(evidence_files)} evidence files")
    prompt = build_prompt(job, evidence_files)
    result = run_cursor_agent(prompt)

    subprocess.run(["git", "checkout", "."], cwd=INVESTIGATE_DIR,
                   capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=INVESTIGATE_DIR,
                   capture_output=True)

    return result


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
            job.setdefault("analysis", {})["ai_summary"] = ai[:8000]
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

    # Deduplicate: group issues by fingerprint (version-specific)
    unique_issues: dict[str, dict] = {}  # fp -> first issue dict + list of jobs
    for issue in all_issues:
        # Extract OCP version from job name for version-specific fingerprinting
        _ver = ""
        _ver_m = re.search(r"nightly-(\d+\.\d+)", issue.get("job_name", ""))
        if _ver_m:
            _ver = _ver_m.group(1)
        fp = compute_issue_fingerprint(
            issue["test_name"], issue["error_msg"], issue["category"], version=_ver
        )
        if fp not in unique_issues:
            unique_issues[fp] = {
                "test_name": issue["test_name"],
                "error_msg": issue["error_msg"],
                "category": issue["category"],
                "version": _ver,
                "jobs": [],
            }
        unique_issues[fp]["jobs"].append({
            "name": issue["job_name"],
            "url": issue["job_url"],
        })

    print(f"  {len(unique_issues)} unique issues (deduplicated across jobs)")

    # Check which are known vs new
    new_issues: dict[str, dict] = {}
    reused_count = 0
    for fp, issue_data in unique_issues.items():
        if is_known_issue(fp_db, fp) and not FORCE_REANALYZE:
            prev = get_issue(fp_db, fp)
            # Update affected_jobs for each job this issue appears in
            for j in issue_data["jobs"]:
                mark_issue_seen(fp_db, fp, j["name"], j["url"])
            reused_count += 1
            short = issue_data["test_name"][:60]
            print(f"  SKIP (recurring #{prev.get('occurrences',1)+1}): {short}")

            # Tag the affected jobs with recurring info (including saved ai_summary)
            saved_ai = prev.get("ai_summary_short", "") or prev.get("ai_summary", "")
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

    # Investigate new issues — one AI call per unique issue
    success_count = 0
    for i, (fp, issue_data) in enumerate(new_issues.items(), 1):
        test_name = issue_data["test_name"]
        affected_jobs = issue_data["jobs"]
        job_names = ", ".join(j["name"].split("nightly-")[-1][:30] for j in affected_jobs[:5])
        print(f"  [{i}/{len(new_issues)}] {test_name[:60]} (in: {job_names})...")

        # Pick the first affected job for evidence gathering
        target_job = None
        for j in affected_jobs:
            for job in failed:
                if job["name"] == j["name"]:
                    target_job = job
                    break
            if target_job:
                break

        if not target_job:
            print(f"    No job data found — skipping")
            continue

        ai = analyze_job(target_job)
        if ai:
            root_cause = _extract_root_cause(ai)
            classification = _extract_classification(ai)
            is_flake = _extract_is_flake(ai)

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
                            "ai_summary": ai[:4000],
                            "is_flake": is_flake,
                        })

            success_count += 1
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
