#!/usr/bin/env python3
"""Check whether a commit (or PR merge commit) exists on a given release branch.

Usage:
    python scripts/check_backport.py <repo_path> <commit_sha_or_pr_number> <release_branch>

Examples:
    python scripts/check_backport.py /tmp/ci-investigate abc123def release-4.20
    python scripts/check_backport.py /tmp/ci-investigate 613 release-4.20

Exit codes:
    0 - commit found on the branch (PRESENT)
    1 - commit NOT found on the branch (NOT_PRESENT)
    2 - error (repo not found, branch doesn't exist, etc.)
"""
import subprocess
import sys
import os
import re


def run_git(repo_path: str, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", repo_path] + list(args),
        capture_output=True, text=True, timeout=30
    )
    return result.returncode, result.stdout.strip()


def fetch_branch_if_needed(repo_path: str, branch: str) -> bool:
    """Ensure the branch is available locally (fetch if shallow clone)."""
    rc, _ = run_git(repo_path, "rev-parse", "--verify", branch)
    if rc == 0:
        return True
    rc, _ = run_git(repo_path, "fetch", "origin", branch, "--depth=100")
    if rc == 0:
        run_git(repo_path, "branch", branch, f"origin/{branch}")
        return True
    return False


def check_sha_on_branch(repo_path: str, sha: str, branch: str) -> bool:
    """Check if a commit SHA is an ancestor of the given branch."""
    rc, _ = run_git(repo_path, "merge-base", "--is-ancestor", sha, branch)
    return rc == 0


def find_pr_commits(repo_path: str, pr_number: str) -> list[str]:
    """Try to find merge commits for a PR number via commit message grep."""
    rc, output = run_git(
        repo_path, "log", "--all", "--oneline", "--grep",
        f"#{pr_number}", "--format=%H"
    )
    if rc == 0 and output:
        return output.splitlines()
    return []


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <repo_path> <commit_or_pr> <release_branch>")
        sys.exit(2)

    repo_path = sys.argv[1]
    identifier = sys.argv[2]
    branch = sys.argv[3]

    if not os.path.isdir(repo_path):
        print(f"ERROR: repo path does not exist: {repo_path}")
        sys.exit(2)

    if not os.path.isdir(os.path.join(repo_path, ".git")):
        print(f"ERROR: not a git repository: {repo_path}")
        sys.exit(2)

    if not fetch_branch_if_needed(repo_path, branch):
        print(f"ERROR: branch '{branch}' not found in {repo_path}")
        sys.exit(2)

    # Determine if identifier is a SHA or PR number
    is_sha = re.match(r"^[0-9a-f]{7,40}$", identifier)

    if is_sha:
        shas_to_check = [identifier]
    else:
        shas_to_check = find_pr_commits(repo_path, identifier)
        if not shas_to_check:
            print(f"NOT_PRESENT: could not find any commits for PR #{identifier} in {repo_path}")
            sys.exit(1)

    for sha in shas_to_check:
        if check_sha_on_branch(repo_path, sha, branch):
            short_sha = sha[:12]
            print(f"PRESENT: {short_sha} exists on {branch}")
            sys.exit(0)

    short_id = identifier[:12] if is_sha else f"PR #{identifier}"
    print(f"NOT_PRESENT: {short_id} is NOT on {branch} — cannot be root cause for this version")
    sys.exit(1)


if __name__ == "__main__":
    main()
