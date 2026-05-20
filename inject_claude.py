#!/usr/bin/env python3
"""Inject Claude AI analysis into results.json for each failed job.

Runs cursor agent --print for each failure, stores the result in
the ai_summary field, so the dashboard HTML shows Claude's analysis.
"""
import json
import os
import subprocess

CURSOR_CLI = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR = os.path.expanduser("~/Documents/GitHub/prow-nightly-monitor")
COMMATRIX_DIR = "/tmp/commatrix-investigate"
RESULTS = f"{REPO_DIR}/public/results.json"


def analyze_job(job: dict) -> str:
    """Run Claude on a single failed job."""
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

    artifacts = analysis.get("artifacts", {})
    ss_info = ""
    for sf in artifacts.get("ss_findings", []):
        ss_info += f"\nss port {sf['port']}: {sf['ss_line']}"

    prompt = (
        f"CI job failed: {job['name']}\n"
        f"Category: {category}\n"
        f"Reason: {reason}\n\n"
        f"Failed tests:\n{failed_tests}\n\n"
        f"Port data:{port_info}\n"
        f"ss output:{ss_info}\n\n"
        f"Investigate. Read test/e2e/validation_test.go and samples/custom-entries/ "
        f"in this repo to understand the test.\n\n"
        f"Simple format: What failed, Error, Warnings (separate), Why, What to do."
    )

    try:
        result = subprocess.run(
            [CURSOR_CLI, "agent", "--trust", "--print", "--output-format", "text", prompt],
            capture_output=True, text=True, timeout=120,
            cwd=COMMATRIX_DIR,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:4000]
    except Exception as e:
        print(f"  Error: {e}")
    return ""


def main():
    data = json.load(open(RESULTS))
    failed = [j for j in data.get("jobs", []) if j["state"] in ("failure", "error")]
    print(f"Analyzing {len(failed)} failures with Claude...")

    for job in failed:
        name = job["name"][-50:]
        print(f"  {name}...")
        ai = analyze_job(job)
        if ai:
            job.setdefault("analysis", {})["ai_summary"] = ai
            print(f"    Done ({len(ai)} chars)")
        else:
            print(f"    No analysis")

    json.dump(data, open(RESULTS, "w"), indent=2)
    print("Results updated")


if __name__ == "__main__":
    main()
