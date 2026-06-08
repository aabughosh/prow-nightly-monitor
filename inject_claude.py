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

CURSOR_CLI = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR = os.path.expanduser("~/Documents/GitHub/prow-nightly-monitor")
TARGET_REPO = os.environ.get("TARGET_REPO", "")
FORK_OWNER = os.environ.get("FORK_OWNER", "aabughosh")
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "openshift-kni/commatrix")
INVESTIGATE_DIR = "/tmp/ci-investigate"
EVIDENCE_DIR = os.path.join(INVESTIGATE_DIR, "ci-evidence")
RESULTS = f"{REPO_DIR}/public/results.json"

MAX_RESULTS_SIZE = 50 * 1024 * 1024
AGENT_TIMEOUT = 300  # 5 minutes per job
OPEN_PRS = os.environ.get("OPEN_PRS", "true").lower() == "true"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://aabughosh.github.io/prow-nightly-monitor/cursor/")


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


def dump_evidence(job: dict) -> list[str]:
    """Write all CI artifacts to ci-evidence/ as files the agent can read.

    Returns a list of (filename, description) for the prompt.
    """
    if os.path.exists(EVIDENCE_DIR):
        shutil.rmtree(EVIDENCE_DIR)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)

    analysis = job.get("analysis", {})
    artifacts = analysis.get("artifacts", {})
    all_artifacts = artifacts.get("all_artifacts", {})
    evidence_files = []

    for key, content in all_artifacts.items():
        safe_name = key.replace("/", "__")
        path = os.path.join(EVIDENCE_DIR, safe_name)
        with open(path, "w") as f:
            f.write(content)
        evidence_files.append(safe_name)

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
    """Build a prompt — uses project config for context, agent reads docs and decides."""
    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    category = analysis.get("category", "")
    reason = analysis.get("reason", "")
    matrix_diff = analysis.get("matrix_diff", {})

    failed_tests = "\n".join(
        f"  - {t.get('name', '?')}: {t.get('message', '')[:300]}"
        for t in inv.get("failed_tests", [])
    )

    evidence_listing = "\n".join(f"  - {f}" for f in evidence_files)

    project = _load_project_config()
    project_context = ""
    if project:
        browse_hint = "- **Browse the ENTIRE repo** — look at all directories, all packages, all tests"

        project_context = f"""
## Project Context (from config)
- **What this project does:** {project.get('description', 'N/A')}
{browse_hint}
- **Config directories:** {', '.join(project.get('config_dirs', [])) or 'N/A'}
"""
        hints = project.get("classification_hints", {})
        if category in hints:
            project_context += f"- **Hint for this failure type:** {hints[category]}\n"

    prompt = f"""You are a senior engineer investigating a CI failure. You have FULL tool access:
- You can READ any file in this repo
- You can RUN shell commands (curl, grep, etc.)
- You can WRITE code fixes directly to files
- You can FETCH full CI logs from Prow URLs
{project_context}
## Step 1: Understand the Project
First, read the repo's documentation to understand what this project does:
- Read README.md, CONTRIBUTING.md, and any docs/ folder
- Look at the project structure (ls the root, key directories)
- Understand the test framework, CI setup, and how failures relate to the code

## Step 2: Investigate the Failure
CI evidence files are in ./ci-evidence/. If they're not enough, use the URLs
in ./ci-evidence/prow-urls.txt to curl full logs from Prow.

Evidence files:
{evidence_listing}

Failure details:
- Job: {job['name']}
- Prow URL: {job.get('url', 'N/A')}
- Category: {category}
- Reason: {reason}

Failed tests:
{failed_tests or '(none extracted)'}

Log snippet:
{analysis.get('log_snippet', '(no log)')[:500]}

## Step 3: Decide What To Do
Based on your understanding of the project and the failure evidence, decide:
1. Is this a real bug that needs a code fix? → Write the fix directly to the files.
2. Is this a flake/transient issue? → Just report it, no code changes.
3. Is this an infra/environment problem? → Report it, no code changes.
4. Is this a test/config that needs updating? → Write the update.

IMPORTANT: Only write fixes for REAL issues. Do NOT fix warnings or transient problems.

## Step 4: Write Fix (if applicable)
If you write a fix, save the patch: git diff > ./ci-evidence/fix.patch

## Step 5: Report
Respond with:
**Root Cause:** what specifically caused this failure
**Is it a flake?** yes/no — and why
**Suggested Fix:** what you did or what should be done
**Fix Written:** yes/no — if yes, see ./ci-evidence/fix.patch
**Severity:** CRITICAL / HIGH / MEDIUM / LOW
"""

    return prompt



def _similar_pr_exists(job: dict) -> bool:
    """Check if an open PR already addresses the same job/issue."""
    job_name = job["name"]
    short = re.sub(r"periodic-ci-openshift-release-main-nightly-", "", job_name)
    short = re.sub(r"[^a-zA-Z0-9-]", "", short)[:40]
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", UPSTREAM_REPO, "--state", "open",
             "--search", f"fix {short} in:title", "--json", "title,url", "--limit", "5"],
            capture_output=True, text=True, timeout=30, cwd=INVESTIGATE_DIR)
        if result.returncode == 0:
            import json as _json
            prs = _json.loads(result.stdout or "[]")
            if prs:
                print(f"    Found existing PR: {prs[0].get('url', '')}")
                return True
    except Exception as e:
        print(f"    Warning: could not check existing PRs: {e}")
    return False


def _open_pr(job: dict, patch: str, ai_summary: str) -> str:
    """Create a branch on fork, push, and open a PR against upstream. Returns PR URL."""
    from datetime import datetime
    job_name = job["name"]
    short = re.sub(r"periodic-ci-openshift-release-main-nightly-", "", job_name)
    short = re.sub(r"[^a-zA-Z0-9-]", "", short)[:60]
    branch = f"fix/{short}-{datetime.now().strftime('%m%d')}"

    def _run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              cwd=INVESTIGATE_DIR, **kw)

    fork_url = f"https://github.com/{FORK_OWNER}/{UPSTREAM_REPO.split('/')[-1]}.git"
    _run(["git", "remote", "remove", "fork"])
    _run(["git", "remote", "add", "fork", fork_url])

    _run(["git", "checkout", "-b", branch])
    _run(["git", "checkout", "."])

    patch_file = os.path.join(EVIDENCE_DIR, "fix.patch")
    apply = _run(["git", "apply", "--check", patch_file])
    if apply.returncode != 0:
        print(f"    Patch doesn't apply cleanly: {apply.stderr[:200]}")
        _run(["git", "checkout", "main"])
        _run(["git", "branch", "-D", branch])
        return ""

    _run(["git", "apply", patch_file])

    _run(["git", "add", "-A", "--", ".", ":!ci-evidence"])
    commit_msg = (f"fix: address CI failure in {short}\n\n"
                  f"Auto-generated by prow-nightly-monitor AI analysis.\n"
                  f"Prow job: {job.get('url', 'N/A')}")
    _run(["git", "commit", "-m", commit_msg])

    push = _run(["git", "push", "-u", "fork", branch])
    if push.returncode != 0:
        print(f"    Push failed: {push.stderr[:300]}")
        _run(["git", "checkout", "main"])
        _run(["git", "branch", "-D", branch])
        return ""

    category = job.get("analysis", {}).get("category", "unknown")
    body_lines = [
        "## Summary",
        f"Automated fix for CI failure in `{job_name}`.",
        f"- **Category:** {category}",
        f"- **Prow URL:** {job.get('url', 'N/A')}",
        "",
        "## AI Analysis",
        ai_summary[:3000] if ai_summary else "(no analysis)",
        "",
        "## Patch",
        "```diff",
        patch[:2000],
        "```",
        "",
        "---",
        "*Auto-generated by [prow-nightly-monitor](https://github.com/aabughosh/prow-nightly-monitor)*",
    ]
    body = "\n".join(body_lines)

    pr = _run(["gh", "pr", "create",
               "--repo", UPSTREAM_REPO,
               "--title", f"fix: {short} CI failure",
               "--body", body,
               "--head", f"{FORK_OWNER}:{branch}"])
    if pr.returncode != 0:
        print(f"    PR creation failed: {pr.stderr[:300]}")
        return ""

    pr_url = pr.stdout.strip()
    print(f"    PR opened: {pr_url}")
    return pr_url


def analyze_job(job: dict, opened_patches: set[str] | None = None) -> str:
    """Deep investigation: dump evidence, build prompt, run agent, capture patch."""
    evidence_files = dump_evidence(job)
    print(f"    Dumped {len(evidence_files)} evidence files")
    prompt = build_prompt(job, evidence_files)
    result = run_cursor_agent(prompt)

    patch_file = os.path.join(EVIDENCE_DIR, "fix.patch")
    patch = ""
    if os.path.exists(patch_file):
        with open(patch_file) as f:
            patch = f.read().strip()
        if patch:
            job.setdefault("analysis", {})["fix_patch"] = patch[:4000]
            print(f"    Fix patch captured ({len(patch)} bytes)")

    if patch and OPEN_PRS:
        category = job.get("analysis", {}).get("category", "")
        severity = job.get("analysis", {}).get("investigation", {}).get("severity", "")
        skip_pr_categories = ("infra", "warning")
        if category in skip_pr_categories:
            print(f"    Skipping PR — category '{category}' is warning-level")
        elif severity and severity.upper() in ("LOW",):
            print(f"    Skipping PR — severity is {severity}")
        elif _similar_pr_exists(job):
            print(f"    Skipping PR — similar PR already open")
        else:
            import hashlib
            patch_hash = hashlib.sha256(patch.encode()).hexdigest()[:16]
            if opened_patches is not None and patch_hash in opened_patches:
                print(f"    Duplicate patch — skipping PR (already opened)")
            else:
                pr_url = _open_pr(job, patch, result)
                if pr_url:
                    job.setdefault("analysis", {})["pr_url"] = pr_url
                    if opened_patches is not None:
                        opened_patches.add(patch_hash)

    subprocess.run(["git", "checkout", "."], cwd=INVESTIGATE_DIR,
                   capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=INVESTIGATE_DIR,
                   capture_output=True)

    return result


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

    if TARGET_REPO and not os.path.exists(INVESTIGATE_DIR):
        print(f"Cloning {TARGET_REPO}...")
        subprocess.run(["git", "clone", "--depth=1", TARGET_REPO,
                       INVESTIGATE_DIR], capture_output=True)
    elif not os.path.exists(INVESTIGATE_DIR):
        os.makedirs(INVESTIGATE_DIR, exist_ok=True)

    with open(RESULTS) as f:
        data = json.load(f)

    failed = [j for j in data.get("jobs", []) if j["state"] in ("failure", "error")]
    print(f"Analyzing {len(failed)} failure(s) with Cursor agent (deep mode)...")
    if OPEN_PRS:
        print("  PRs enabled — will open PRs for unique fixes")

    opened_patches: set[str] = set()
    success_count = 0
    for i, job in enumerate(failed, 1):
        name = job["name"].split("-")[-5:] if len(job["name"]) > 50 else [job["name"]]
        short_name = "-".join(name)
        print(f"  [{i}/{len(failed)}] {short_name}...")
        ai = analyze_job(job, opened_patches)
        if ai:
            job.setdefault("analysis", {})["ai_summary"] = ai[:8000]
            success_count += 1
            print(f"    Done ({len(ai)} chars)")
        else:
            print(f"    No analysis returned")

    with open(RESULTS, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Results updated: {success_count}/{len(failed)} failures analyzed")


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

    for j in failed_jobs:
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
