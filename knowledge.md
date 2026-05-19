# Prow Monitor Knowledge Base

This file is loaded by the AI to provide domain-specific context
about OpenShift CI failures. Edit this file to teach the bot
about new patterns.

## Port Ranges

| Range | Type | Description |
|-------|------|-------------|
| 0-1023 | Well-known | System services (SSH 22, HTTP 80, HTTPS 443) |
| 1024-29999 | Registered | Application-specific (etcd 2379-2381, kube-apiserver 6443) |
| 30000-32767 | NodePort | Kubernetes NodePort range (dynamic, skipped by commatrix) |
| 32768-60999 | Linux ephemeral | OS-assigned ephemeral ports (CRI-O, kubelet health) |
| 61000-65535 | High ephemeral | Additional ephemeral range |

## Known Ephemeral Port Processes

| Process | Description | Action |
|---------|-------------|--------|
| crio | CRI-O container runtime health endpoint | Ephemeral, changes every reboot. Add as static entry with optional=true |
| rpc.statd | NFS status monitor | Ephemeral, platform-specific |
| cluster-kube-ap | Kube API server auxiliary | May be ephemeral |

## Commatrix Dynamic Ranges

The commatrix code defines some dynamic ranges (e.g. NodePort 30000-32767)
but does NOT skip Linux ephemeral ports (32768-60999). This means:

- CRI-O uses a random ephemeral port assigned by the OS
- This port changes on every reboot
- The test ALWAYS catches it as "no EndpointSlice" because CRI-O is not
  a Kubernetes service — it's a system daemon
- CRI-O has no EndpointSlice and never will — it needs a static entry
- The fix: add CRI-O to static entries in the commatrix repo

Other processes in the ephemeral range may also be caught. Check if
they always use random ports or if they have a fixed port.

## Common Failure Patterns

### Matrix Mismatch - Ports documented but not used
Ports were removed from the cluster in a new OCP version. The documented
CSV files need updating. Check which version introduced the removal.

### Matrix Mismatch - Ports used but not documented  
New ports appeared. Check if they come from a new operator or service.
Add to the documented CSV.

### Matrix Mismatch - No EndpointSlice
Port is open (ss shows it) but has no Kubernetes EndpointSlice resource.
- If in ephemeral range + CRI-O: add static entry
- If well-known port: a service is missing its EndpointSlice, investigate

### Cluster Setup Failure (telco5g)
Bare metal cluster provisioning failed. Common causes:
- SSH connection refused: hardware not responding
- No free cluster: all bare metal machines in use
- DNS failure: lab infrastructure issue

### ofcir Failure
The ofcir service provisions bare metal machines. If DNS fails for
ofcir.apps-int.master.ci.devcluster.openshift.com, it's a CI infra issue.

### Upgrade Failures
Node not ready after upgrade. Check:
- ClusterVersion status
- Which operators are degraded
- If the MonitorTest caught a transient issue (flake)
