#!/usr/bin/env python3
"""Map a Prow CI job name to its test branch and production code branch.

Usage:
    python scripts/identify_release_branch.py <job_name>

Example:
    python scripts/identify_release_branch.py \
        "periodic-ci-openshift-release-main-nightly-4.20-e2e-telco5g-ptp"

Output (JSON):
    {"version": "4.20", "test_branch": "main", "code_branch": "release-4.20",
     "project": "ptp-operator", "note": "Tests from main, production code from release-4.20"}
"""
import json
import re
import sys

PROJECT_FILTERS = {
    "e2e-telco5g-ptp": "ptp-operator",
    "network-flow-matrix": "commatrix",
    "cnf-features": "cnf-features-deploy",
    "sriov": "sriov-network-operator",
}


def identify(job_name: str) -> dict:
    version_match = re.search(r"nightly-(\d+\.\d+)", job_name)
    version = version_match.group(1) if version_match else "unknown"

    # CI config branch: always main (openshift/release)
    test_branch = "main"

    # Production code branch: release-X.Y for the extracted version
    # Exception: version 5.0+ may use main directly if release branch doesn't exist yet
    if version != "unknown":
        major, minor = version.split(".")
        if int(major) >= 5 and int(minor) == 0:
            code_branch = "release-5.0"
            note = "Tests from main. Production code likely from release-5.0 (or main if branch not yet cut)."
        else:
            code_branch = f"release-{version}"
            note = f"Tests from main, production code from release-{version}"
    else:
        code_branch = "unknown"
        note = "Could not determine version from job name"

    # Identify project
    project = "unknown"
    for filter_key, proj_name in PROJECT_FILTERS.items():
        if filter_key in job_name:
            project = proj_name
            break

    # Upstream variant: uses upstream (k8snetworkplumbingwg) test code
    is_upstream = "upstream" in job_name
    if is_upstream:
        test_branch = "main (upstream k8snetworkplumbingwg/ptp-operator)"
        note += ". UPSTREAM variant: test code from upstream repo main branch."

    return {
        "version": version,
        "test_branch": test_branch,
        "code_branch": code_branch,
        "project": project,
        "is_upstream": is_upstream,
        "note": note,
    }


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <job_name>")
        sys.exit(1)

    job_name = sys.argv[1]
    result = identify(job_name)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
