# PTP / Commatrix CI Analysis Rules

This rule applies when analyzing CI job failures for PTP-operator or commatrix projects.

## CI Job Architecture — CRITICAL

OpenShift nightly CI jobs follow this model:

- **Test scripts** come from the `main` branch of `openshift/release` (CI config repo)
- **Production code** (operator, daemon, images) comes from `release-X.Y` branch of the project repo
- The OCP version in the job name (e.g. `nightly-4.20-e2e-telco5g-ptp`) indicates the **release branch** the production code is built from

### What this means for root cause analysis:

1. A PR/commit merged **only on `main`** of ptp-operator or linuxptp-daemon **CANNOT** be the root cause of a failure on a `release-4.20` job — unless it was cherry-picked/backported to `release-4.20`.
2. The test suite code (Ginkgo tests in `test/`) may come from `main` while the code under test comes from `release-X.Y`. New tests added on `main` that exercise features not present on `release-X.Y` will fail — this is a **test/code version mismatch**, not a regression.
3. Always check whether cited commits exist on the target release branch before blaming them.

### Branch mapping:

| Job name contains | Test scripts branch | Production code branch |
|---|---|---|
| `nightly-4.17-e2e-telco5g-ptp` | `main` | `release-4.17` |
| `nightly-4.18-e2e-telco5g-ptp` | `main` | `release-4.18` |
| `nightly-4.20-e2e-telco5g-ptp` | `main` | `release-4.20` |
| `nightly-4.21-e2e-telco5g-ptp` | `main` | `release-4.21` |
| `nightly-4.22-e2e-telco5g-ptp` | `main` | `release-4.22` |
| `nightly-4.23-e2e-telco5g-ptp` | `main` | `release-4.23` |
| `nightly-5.0-e2e-telco5g-ptp` | `main` | `release-5.0` or `main` |
| `*-e2e-telco5g-ptp-upstream` | `main` (upstream k8snetworkplumbingwg) | same as above |

### Verification procedure:

Before citing a PR or commit as root cause, run:
```bash
python scripts/check_backport.py /tmp/ci-investigate <commit_sha> release-X.Y
```

If the script returns "NOT_PRESENT", that commit is NOT on the release branch and cannot be the root cause.

## Common False Positives — AVOID THESE

1. **Main-only PR blamed for release-X.Y failure**: A PR merged on `main` is cited as root cause for an older release job. Always verify backport status.

2. **Hardware issues blamed without deployment evidence**: Don't claim "no PTP-capable NICs" unless you've confirmed the operator actually deployed and the DaemonSet ran. If the operator image failed to pull, the DaemonSet never existed — that's an image/build problem, not hardware.

3. **MonitorTest failures attributed to project code**: `openshift-tests` MonitorTests (apiserver-availability, static-pod-lifecycle, etc.) detect cluster-level disruption. If the project's own `finished.json` reports SUCCESS, the project tests passed — the failure is CI framework infrastructure.

4. **Test/code version mismatch misidentified as regression**: When `main` adds a test that requires features only in newer releases, older release jobs will fail that test. This is expected skew, not a regression in the production code.

## Project-Specific Context

### PTP Operator (`e2e-telco5g-ptp`)
- **Upstream repo:** https://github.com/k8snetworkplumbingwg/ptp-operator (mirrored to `openshift/ptp-operator`)
- Deploys PTP Operator + linuxptp-daemon DaemonSet on bare-metal telco lab nodes
- Tests require physical PTP-capable NICs and GNSS receivers
- **Multi-image build** (see `ptp-tools/` directory): The operator is composed of multiple container images built together:
  - `ptp-operator` (ptpop) — main operator managing PTP configurations
  - `linuxptp-daemon` (lptpd) — daemon running ptp4l/phc2sys/ts2phc on nodes (https://github.com/openshift/linuxptp-daemon)
  - `cloud-event-proxy` (cep) — handles PTP events and cloud event publishing (https://github.com/redhat-cne/cloud-event-proxy)
  - `kube-rbac-proxy` (krp) — RBAC proxy for secure metrics access
- The telco CI script builds the operator image in-cluster; if the builder image is unreachable, the operator never deploys
- **CI step registry:** Test execution is defined in `openshift/release` at `ci-operator/step-registry/telco5g/ptp/` — the `telco5g-ptp-tests` step clones the test repo, builds locally, and runs Ginkgo suites
- Related repos: `openshift/linuxptp-daemon`, `redhat-cne/cloud-event-proxy`

### Commatrix (`network-flow-matrix`)
- Validates node open ports match documented communication matrix
- Runs on AWS and bare-metal (OFCIR-acquired hosts)
- nftables test intentionally reboots SNO nodes — this triggers MonitorTest failures (expected, not a bug)
- Related repo: `openshift-kni/commatrix`

## Corrections Database

Before finalizing your analysis, check `corrections.yaml` in the repo root for similar past mistakes. If your conclusion matches a known false-positive pattern, reconsider.

## Output Quality Rules

1. Never start analysis with filler like "I now have all the data needed" or "Here is the full analysis" — jump directly into the findings.
2. Strip markdown bold (`**`) from root cause summaries stored in fingerprint DB.
3. The `root_cause` field should be a concise technical explanation, not a narrative.
