#!/usr/bin/env python3
"""Run Claude CLI (with full tool access) on each failed job.

Claude can READ files, RUN commands, and WRITE fixes.
Uses --output-format json which returns Claude's full response.
Runs from the cloned commatrix repo so Claude can read the test code.
"""
import json
import os
import subprocess
import sys

CURSOR_CLI = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR = os.path.expanduser("~/Documents/GitHub/prow-nightly-monitor")
COMMATRIX_DIR = "/tmp/commatrix-investigate"
RESULTS = f"{REPO_DIR}/public/results.json"


def run_claude(prompt: str, cwd: str = COMMATRIX_DIR, timeout: int = 180) -> str:
    """Run Claude CLI with full tool access. Returns the response text."""
    try:
        result = subprocess.run(
            [CURSOR_CLI, "agent", "--trust", "--yolo", "--print",
             "--output-format", "json", prompt],
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data.get("result", "")
    except subprocess.TimeoutExpired:
        print("  Claude timeout")
    except json.JSONDecodeError:
        if result.stdout:
            return result.stdout.strip()[:4000]
    except Exception as e:
        print(f"  Error: {e}")
    return ""


def analyze_job(job: dict) -> str:
    """Deep investigation of a failed job using Claude with full tool access."""
    analysis = job.get("analysis", {})
    inv = analysis.get("investigation", {})
    category = analysis.get("category", "")
    reason = analysis.get("reason", "")

    failed_tests = "\n".join(
        f"- {t.get('name', '?')}: {t.get('message', '')[:150]}"
        for t in inv.get("failed_tests", [])[:5]
    )

    matrix_diff = analysis.get("matrix_diff", {})
    port_info = ""
    if matrix_diff:
        for key, label in [("no_endpointslice_ports", "No EndpointSlice"),
                           ("stale_ports", "Stale"), ("undocumented_ports", "Undocumented")]:
            ports = matrix_diff.get(key, [])
            if ports:
                port_info += f"\n{label}:\n" + "\n".join(f"  {p}" for p in ports[:5])

    ss_info = ""
    for sf in analysis.get("artifacts", {}).get("ss_findings", []):
        ss_info += f"\nss port {sf['port']}: {sf['ss_line']}"

    prompt = (
        f"Investigate this CI failure. You have the commatrix repo checked out — "
        f"READ the actual code to understand the test.\n\n"
        f"Job: {job['name']}\n"
        f"Category: {category}\n"
        f"Reason: {reason}\n"
        f"Failed tests:\n{failed_tests}\n"
        f"Port data:{port_info}\n"
        f"ss output:{ss_info}\n\n"
        f"Steps:\n"
        f"1. Read test/e2e/validation_test.go — understand what the test checks\n"
        f"2. Read the filterOutPortsOfKnownServices and filterOutPortsInDynamicRanges functions\n"
        f"3. Read samples/custom-entries/ for existing static entries\n"
        f"4. For each failing port: determine what process it is, can it have EndpointSlice, "
        f"is it already filtered, should it be added to known services or dynamic ranges\n"
        f"5. Suggest a specific fix\n\n"
        f"Keep your response clean and simple:\n"
        f"**What failed:** test name\n"
        f"**Error:** exact message\n"
        f"**Why:** based on code you read\n"
        f"**What to do:** specific fix with file and code change"
    )

    return run_claude(prompt)


def main():
    if not os.path.exists(RESULTS):
        print(f"No results at {RESULTS}")
        sys.exit(1)

    if not os.path.exists(COMMATRIX_DIR):
        print("Cloning commatrix...")
        subprocess.run(["git", "clone", "--depth=1",
                       "https://github.com/openshift-kni/commatrix.git",
                       COMMATRIX_DIR], capture_output=True)

    data = json.load(open(RESULTS))
    failed = [j for j in data.get("jobs", []) if j["state"] in ("failure", "error")]
    print(f"Analyzing {len(failed)} failures with Claude (full tool access)...")

    for job in failed:
        name = job["name"][-50:]
        print(f"  {name}...")
        ai = analyze_job(job)
        if ai:
            job.setdefault("analysis", {})["ai_summary"] = ai[:4000]
            print(f"    Done ({len(ai)} chars)")
        else:
            print(f"    No analysis")

    json.dump(data, open(RESULTS, "w"), indent=2)
    print("Results updated with Claude analysis")


if __name__ == "__main__":
    main()
