# Prow Nightly Monitor — Demo Presentation
## 10 min | Team audience

---

## Slide 1: Title
**Prow Nightly Monitor**
*AI-powered CI failure detection, investigation, and auto-fix*

- Your name
- Date
- Link: https://aabughosh.github.io/prow-nightly-monitor/

---

## Slide 2: The Problem
**We waste hours investigating nightly CI failures**

- Every morning: check Prow → find failures → dig through logs → figure out root cause
- Multiple projects (commatrix, PTP, SR-IOV, CNF) = multiple dashboards to check
- Same failure patterns repeat — we re-investigate things we've already seen
- Context switching: need to remember what each test checks, what artifacts to look at

**Speaker notes:** "How many times have you opened Prow, scrolled through logs, and spent 30 minutes figuring out it was just an infra flake? That's what this tool solves."

---

## Slide 3: The Solution
**Automated daily monitoring with weekly AI deep analysis**

```
Daily (Mon-Fri noon):
  Prow API → Fetch jobs → Classify failures → Dashboard → Slack alert

Weekly (Monday):
  + Clone repo → AI agent reads ALL code → Investigates each failure
  → Writes fix patches → Opens PRs automatically
```

**Key point:** The AI agent reads the repo's README and browses the entire codebase before analyzing — it understands the project like an engineer would.

---

## Slide 4: How It Works — Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────┐
│  Prow API   │────▸│  monitor.py  │────▸│ inject_claude.py │────▸│ Dashboard│
│ + GCS logs  │     │  fetch/class │     │  Cursor CLI agent│     │ + PRs    │
└─────────────┘     └──────────────┘     └─────────────────┘     └──────────┘
```

1. **Fetches** all nightly jobs matching filter from Prow
2. **Downloads** relevant artifacts (JUnit, logs, test output)
3. **Classifies** failures (test failure, infra, build error)
4. **AI investigates** — reads repo docs, browses code, checks evidence
5. **Generates fix** and opens PR if it's a real bug

---

## Slide 5: Live Demo — Dashboard
**Show: https://aabughosh.github.io/prow-nightly-monitor/cursor/**

Point out:
- Pass/fail summary at top
- Trend chart (last 14 days)
- Each failure card shows:
  - Job name + Prow link
  - Category badge (Test Failure / Infra / Matrix Mismatch)
  - Severity (CRITICAL / HIGH / MEDIUM / LOW)
  - AI analysis expandable section
  - Fix patch (if generated)
  - PR link (if opened)

**Speaker notes:** "Let me show you what this looks like. Here's today's dashboard — you can see 3 failures. Let me expand one to show the AI analysis..."

---

## Slide 6: Live Demo — AI Analysis Deep Dive
**Expand one failure card to show:**

- Root cause explanation (written by AI after reading the repo)
- Whether it's a flake or real bug
- Specific code path that failed
- Suggested fix with exact file/line
- Fix patch diff (if written)

**Speaker notes:** "The AI doesn't just say 'test failed' — it actually read the test code, understood what it checks, looked at the CI evidence, and explained WHY it failed. And for this one, it even wrote a fix."

---

## Slide 7: Live Demo — PTP Example
**Show: the PTP dashboard (file:///...ptp-operator/index.html)**

- Same tool, different project — just change the config
- AI read the PTP operator README, browsed the test packages
- Identified clock sync issues vs real test bugs
- Generated a patch for one of the failures

**Speaker notes:** "This isn't just for commatrix. I ran it on PTP and it worked immediately. The AI read the ptp-operator README, understood what PTP does, and investigated the failures in that context."

---

## Slide 8: Multi-Project Support
**One tool for all our projects**

| Project | Job Filter | What it monitors |
|---------|-----------|-----------------|
| commatrix | `network-flow-matrix` | Port matrix validation |
| PTP | `e2e-telco5g-ptp` | Precision Time Protocol |
| CNF features | `cnf-features` | DPDK, SR-IOV, SCTP |
| SR-IOV | `sriov` | Network operator VFs |

Each project gets:
- Its own dashboard page
- Project-specific artifact downloading
- Context-aware AI analysis (reads that repo's docs)

**To add a new project:** just add an entry to `projects.json`

---

## Slide 9: Slack Integration
**Daily alerts in your channel**

Show the Slack message format:
```
Prow Nightly Monitor — Jun 9, 2026
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 7 passed  ❌ 3 failed

🟠 4.23-telco5g-network-flow-matrix-bm
   TEST_FAILURE | Severity: MEDIUM
   The job failed because...

📊 Dashboard
```

No need to check Prow manually — the alert comes to you with AI analysis included.

---

## Slide 10: Auto PR Creation
**AI fixes submitted automatically**

- AI writes fix → generates patch → opens PR on upstream
- Smart duplicate detection (won't open PR if similar one exists)
- Skips infra/flake issues (only real bugs get PRs)
- Excludes CI evidence files from the PR (clean patches only)

Show an example PR if available.

**Speaker notes:** "On Mondays, if the AI finds a real bug and writes a fix, it opens a PR automatically. It checks first if there's already a similar PR open so we don't get duplicates."

---

## Slide 11: Why Local Cron (launchd) — Not GitHub Actions?

**GitHub Actions can't do this:**
- No access to Cursor CLI agent (needs local install + auth)
- AI agent needs to clone repos, run commands, browse code — needs a real machine
- GitHub Actions would need expensive self-hosted runners for AI
- Rate limits on GitHub Actions for long-running AI tasks (~20 min per project)

**Local cron (macOS launchd) gives us:**
- Runs on your laptop every weekday at noon — fully autonomous
- Direct access to Cursor CLI (already authenticated)
- No CI minutes consumed, no runner costs
- Survives reboots (launchd restarts it)
- Can run even when you're in meetings — fires and forgets

**The daily run (no AI) COULD run in GitHub Actions** — but we keep it local for simplicity and to have everything in one place.

**Speaker notes:** "Why not GitHub Actions? Because the AI agent needs Cursor CLI which requires a local install and login. It also needs to clone repos, run shell commands, and write fixes — things that would need expensive self-hosted runners. With launchd, it just runs on my machine every day while I'm in meetings."

---

## Slide 12: Cost Breakdown

| | Daily (Tue-Fri) | Weekly AI (Monday) |
|---|---|---|
| **What runs** | Fetch + classify + Slack | + AI deep analysis + PRs |
| **Duration** | ~5 minutes | ~20-30 min per project |
| **Cost** | Free (just API calls) | ~$0.50-2.00 per project |
| **Output** | Dashboard + Slack alert | + AI summaries + fix patches + PRs |

**Monthly cost estimate (4 projects):**
- Daily runs (20 days): $0
- Weekly AI (4 Mondays × 4 projects): ~$8-32/month
- **Total: ~$8-32/month** for automated investigation of ALL nightly failures

**Compare to engineer time:**
- 30 min/day × 20 days = 10 hours/month investigating failures manually
- At $80/hr engineer cost = **$800/month of saved time**

**ROI: 25-100x return on AI cost**

**Speaker notes:** "The AI runs once a week per project. That's about $0.50-2 per project per week. For 4 projects that's maybe $30/month. Compare that to 10 hours a month of engineer time spent digging through Prow logs — the ROI is massive."

---

## Slide 12: How to Use It for Your Project

1. Add your project to `projects.json`:
```json
{
  "my-project": {
    "job_filter": "my-prow-job-name",
    "target_repo": "https://github.com/org/repo.git",
    "upstream_repo": "org/repo",
    "description": "What this project does",
    "artifact_patterns": ["junit*.xml", "*.log"],
    "artifact_dirs": ["e2e", "test"]
  }
}
```

2. That's it — next run picks it up automatically

---

## Slide 13: Summary

**Before:** Manual log digging every morning, 30-60 min per failure
**After:** AI does it overnight, you get a Slack message with root cause + fix

- Works for any Prow-monitored project
- AI reads the repo and decides what to do (generic)
- Catches real bugs AND identifies flakes
- Opens PRs for real fixes
- All in one dashboard

**Repo:** https://github.com/aabughosh/prow-nightly-monitor

---

## Slide 14: Q&A

Questions?

---

# Design Tips for Google Slides

- **Theme:** Dark background (like the dashboard) — use dark blue/black with white text
- **Fonts:** Use monospace for code/commands, sans-serif for headings
- **Colors:**
  - Blue (#58a6ff) for links and highlights
  - Green for pass/success
  - Red/orange for failures
  - Gray for secondary text
- **Screenshots:** Take screenshots of the actual dashboard for slides 5-7
- **Animations:** Use slide transitions, not bullet animations (keep it clean)
