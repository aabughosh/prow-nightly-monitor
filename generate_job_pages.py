#!/usr/bin/env python3
"""Generate individual per-job analysis HTML pages for GitLab Pages integration.

Reads results.json for each project and creates a standalone HTML page per failed job at:
  public/projects/<project>/jobs/<job-name>.html

These pages have predictable URLs that prow-status can link to directly.

Usage:
    python3 generate_job_pages.py                    # all projects
    python3 generate_job_pages.py ptp-operator       # single project
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_DIR = Path(__file__).parent
PUBLIC_DIR = REPO_DIR / "public"
GITLAB_PAGES_BASE = os.environ.get(
    "GITLAB_PAGES_BASE",
    "https://prow-ai-analysis-a0411b.pages.redhat.com",
)


def _inline_md(text: str) -> str:
    text = re.sub(
        r"`([^`]+)`",
        r'<code style="background:#161b22;padding:1px 4px;border-radius:3px;'
        r'font-size:12px">\1</code>',
        text,
    )
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text


def _md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    in_code = False
    in_table = False
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append(
                    '<pre style="background:#161b22;padding:10px;border-radius:6px;'
                    'overflow-x:auto;font-size:12px"><code>'
                )
                in_code = True
            i += 1
            continue

        if in_code:
            out.append(html_mod.escape(line))
            i += 1
            continue

        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i + 1]):
                if not in_table:
                    out.append(
                        '<table style="border-collapse:collapse;width:100%;'
                        'font-size:12px;margin:8px 0">'
                    )
                    in_table = True
                out.append(
                    "<tr>"
                    + "".join(
                        f'<th style="border:1px solid #30363d;padding:6px 8px;'
                        f'background:#161b22;text-align:left">{_inline_md(c)}</th>'
                        for c in cells
                    )
                    + "</tr>"
                )
                i += 2
                continue
            elif in_table:
                out.append(
                    "<tr>"
                    + "".join(
                        f'<td style="border:1px solid #30363d;padding:6px 8px">'
                        f"{_inline_md(c)}</td>"
                        for c in cells
                    )
                    + "</tr>"
                )
                i += 1
                continue

        if in_table:
            out.append("</table>")
            in_table = False

        stripped = line.strip()

        if not stripped:
            out.append("<br>")
            i += 1
            continue

        if stripped.startswith("### "):
            out.append(
                f'<h4 style="color:#58a6ff;margin:12px 0 4px;font-size:14px">'
                f"{_inline_md(stripped[4:])}</h4>"
            )
        elif stripped.startswith("## "):
            out.append(
                f'<h3 style="color:#58a6ff;margin:14px 0 6px;font-size:15px;'
                f'border-bottom:1px solid #21262d;padding-bottom:4px">'
                f"{_inline_md(stripped[3:])}</h3>"
            )
        elif stripped.startswith("# "):
            out.append(
                f'<h2 style="color:#58a6ff;margin:16px 0 8px;font-size:17px">'
                f"{_inline_md(stripped[2:])}</h2>"
            )
        elif stripped.startswith("- "):
            out.append(
                f'<div style="padding-left:16px;margin:2px 0">'
                f"&bull; {_inline_md(stripped[2:])}</div>"
            )
        elif stripped.startswith("---"):
            out.append(
                '<hr style="border:none;border-top:1px solid #21262d;margin:12px 0">'
            )
        elif re.match(r"^\d+\.\s", stripped):
            m = re.match(r"^(\d+)\.\s(.+)", stripped)
            if m:
                out.append(
                    f'<div style="padding-left:16px;margin:2px 0">'
                    f"{m.group(1)}. {_inline_md(m.group(2))}</div>"
                )
        else:
            out.append(f'<p style="margin:4px 0">{_inline_md(stripped)}</p>')

        i += 1

    if in_table:
        out.append("</table>")
    if in_code:
        out.append("</code></pre>")

    return "\n".join(out)


def _extract_version(job_name: str) -> str:
    m = re.search(r"nightly-(\d+\.\d+)", job_name)
    return m.group(1) if m else ""


def _compute_duration(job: dict) -> str:
    if job.get("duration"):
        return job["duration"]
    start = job.get("start_time", "")
    end = job.get("completion_time", "")
    if not start or not end:
        return ""
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        mins = int((e - s).total_seconds() / 60)
        return f"{mins}m"
    except (ValueError, TypeError):
        return ""


def _extract_build_id(job: dict) -> str:
    """Extract Prow build ID from job URL."""
    m = re.search(r"/(\d{15,})$", job.get("url", ""))
    return m.group(1) if m else ""


def _job_filename(job_name: str, build_id: str = "") -> str:
    """Convert job name (+ optional build ID) to a safe filename."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", job_name)
    if build_id:
        return f"{safe}-{build_id}.html"
    return f"{safe}.html"


BADGE_COLORS = {
    "infra": ("#d29922", "INFRA"),
    "test_failure": ("#f85149", "TEST"),
    "build_error": ("#da3633", "BUILD"),
    "matrix_mismatch": ("#f0883e", "MATRIX"),
    "error": ("#8b949e", "ERROR"),
    "unknown": ("#484f58", "UNKNOWN"),
}

SEV_COLORS = {
    "critical": "#f85149",
    "high": "#f0883e",
    "medium": "#d29922",
    "low": "#8b949e",
}


def generate_job_page(job: dict, project_name: str, generated_at: str) -> str:
    """Generate a standalone HTML page for a single failed job's AI analysis."""
    name = job["name"]
    version = _extract_version(name)
    duration = _compute_duration(job)
    state = job.get("state", "failure")
    url = job.get("url", "")
    started = (job.get("start_time", "") or "")[:16]
    analysis = job.get("analysis", {})
    category = analysis.get("category", "unknown")
    inv = analysis.get("investigation", {})
    severity = inv.get("severity", "")

    cat_color, cat_label = BADGE_COLORS.get(category, ("#484f58", "UNKNOWN"))
    sev_color = SEV_COLORS.get(severity.lower(), "#484f58") if severity else ""

    ai_summary = analysis.get("ai_summary", "")
    if not ai_summary and analysis.get("issues"):
        parts = []
        for iss in analysis["issues"]:
            iss_ai = iss.get("ai_summary", "")
            if iss_ai:
                parts.append(iss_ai)
            elif iss.get("root_cause"):
                parts.append(
                    f"**Issue Class:** {iss.get('classification', 'unknown')}\n"
                    f"**Root Cause:** {iss['root_cause']}\n"
                )
        if parts:
            ai_summary = "\n\n---\n\n".join(parts)

    if state == "success":
        analysis_rendered = (
            '<div style="text-align:center;padding:40px 20px">'
            '<div style="font-size:48px;margin-bottom:12px">&#10003;</div>'
            '<div style="font-size:18px;font-weight:600;color:#3fb950">Job Passed</div>'
            '<div style="color:#8b949e;margin-top:8px">All tests passed — no AI analysis needed.</div>'
            '</div>'
        )
    elif ai_summary:
        analysis_rendered = _md_to_html(ai_summary)
    else:
        analysis_rendered = (
            '<div style="text-align:center;padding:40px 20px">'
            '<div style="font-size:48px;margin-bottom:12px">&#8987;</div>'
            '<div style="font-size:18px;font-weight:600;color:#d29922">Not Yet Analyzed</div>'
            '<div style="color:#8b949e;margin-top:8px">This failure has not been analyzed by AI yet. '
            'Analysis runs daily and will be available after the next scheduled run.</div>'
            '</div>'
        )

    failed_tests = inv.get("failed_tests", [])
    tests_html = ""
    if failed_tests:
        tests_items = []
        for t in failed_tests:
            tname = html_mod.escape(t.get("name", t.get("step", "?")))
            tmsg = html_mod.escape((t.get("message", "") or "")[:300])
            tests_items.append(
                f'<div style="margin:6px 0;padding:8px 12px;background:#161b22;'
                f'border-radius:6px;border-left:3px solid #f85149">'
                f'<div style="font-family:monospace;font-size:12px;color:#f0883e">{tname}</div>'
                f'<div style="font-size:11px;color:#8b949e;margin-top:2px">{tmsg}</div>'
                f"</div>"
            )
        tests_html = (
            '<div style="margin:16px 0">'
            '<h3 style="color:#8b949e;font-size:13px;text-transform:uppercase;'
            'letter-spacing:1px;margin-bottom:8px">Failed Tests</h3>'
            + "\n".join(tests_items)
            + "</div>"
        )

    name_short = name.replace("periodic-ci-openshift-release-main-nightly-", "")
    dashboard_link = f"../index.html#{name}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Analysis — {html_mod.escape(name_short)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e1e4e8; min-height: 100vh; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: #21262d; padding: 1px 5px; border-radius: 3px; font-size: 11px;
         color: #f0883e; font-family: 'SF Mono', 'Fira Code', monospace; }}
  .header {{ background: linear-gradient(135deg, #161b22 0%, #1a1e2e 50%, #2d1b4e 100%);
            padding: 20px 32px; border-bottom: 1px solid #30363d; }}
  .header h1 {{ font-size: 18px; font-weight: 600; color: #f0f6fc; }}
  .header .meta {{ color: #8b949e; font-size: 12px; margin-top: 4px; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 32px; }}
  .info-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
               gap: 12px; margin-bottom: 24px; }}
  .info-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
              padding: 12px 16px; }}
  .info-label {{ font-size: 10px; color: #8b949e; text-transform: uppercase;
                letter-spacing: 1px; margin-bottom: 4px; }}
  .info-value {{ font-size: 14px; font-weight: 600; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
           font-size: 10px; font-weight: 600; letter-spacing: 0.5px; color: white; }}
  .analysis-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                  padding: 20px 24px; margin-top: 20px; font-size: 13px; line-height: 1.6; }}
  .nav {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }}
  .nav a {{ font-size: 13px; }}
  .footer {{ margin-top: 32px; padding: 16px; text-align: center; color: #484f58;
            font-size: 11px; border-top: 1px solid #21262d; }}
</style>
</head>
<body>

<div class="header">
  <h1>AI Analysis — <code>{html_mod.escape(name_short)}</code></h1>
  <div class="meta">Generated: {html_mod.escape(generated_at)} | Project: {html_mod.escape(project_name)}</div>
</div>

<div class="container">

<div class="nav">
  <a href="{dashboard_link}">Back to Dashboard</a>
  <span style="color:#30363d">|</span>
  <a href="{html_mod.escape(url)}" target="_blank">View in Prow</a>
  <span style="color:#30363d">|</span>
  <a href="../issues.html">Known Issues</a>
</div>

<div class="info-grid">
  <div class="info-card">
    <div class="info-label">Status</div>
    <div class="info-value" style="color:{'#3fb950' if state == 'success' else '#f85149'}">{html_mod.escape(state)}</div>
  </div>
  <div class="info-card">
    <div class="info-label">Version</div>
    <div class="info-value">{html_mod.escape(version or 'N/A')}</div>
  </div>
  <div class="info-card">
    <div class="info-label">Duration</div>
    <div class="info-value">{html_mod.escape(duration or 'N/A')}</div>
  </div>
  <div class="info-card">
    <div class="info-label">Category</div>
    <div class="info-value"><span class="badge" style="background:{cat_color}">{cat_label}</span></div>
  </div>
  {"" if not severity else f'''<div class="info-card">
    <div class="info-label">Severity</div>
    <div class="info-value"><span class="badge" style="background:{sev_color}">{html_mod.escape(severity)}</span></div>
  </div>'''}
  <div class="info-card">
    <div class="info-label">Started</div>
    <div class="info-value" style="font-size:12px">{html_mod.escape(started)}</div>
  </div>
</div>

{tests_html}

<h2 style="color:#58a6ff;font-size:16px;margin:20px 0 12px;border-bottom:1px solid #21262d;padding-bottom:8px">
  AI Analysis
</h2>
<div class="analysis-box">
  {analysis_rendered}
</div>

<div class="footer">
  <a href="https://github.com/aabughosh/prow-nightly-monitor">prow-nightly-monitor</a> |
  AI-powered CI failure analysis
</div>

</div>
</body>
</html>"""


def generate_index_page(
    project_name: str, job_pages: list[dict], generated_at: str
) -> str:
    """Generate an index page listing all per-job analysis pages."""
    rows = []
    for jp in job_pages:
        name_short = jp["name"].replace(
            "periodic-ci-openshift-release-main-nightly-", ""
        )
        version = _extract_version(jp["name"])
        cat_color, cat_label = BADGE_COLORS.get(
            jp.get("category", "unknown"), ("#484f58", "UNKNOWN")
        )
        has_ai = "Yes" if jp.get("has_ai") else "No"
        ai_color = "#3fb950" if jp.get("has_ai") else "#8b949e"
        rows.append(
            f'<tr>'
            f'<td><a href="{jp["filename"]}">{html_mod.escape(name_short)}</a></td>'
            f'<td><span style="background:#21262d;padding:2px 8px;border-radius:5px;'
            f'font-weight:700;border:1px solid #30363d">{html_mod.escape(version)}</span></td>'
            f'<td><span class="badge" style="background:{cat_color}">{cat_label}</span></td>'
            f'<td style="color:{ai_color}">{has_ai}</td>'
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Analysis Index — {html_mod.escape(project_name)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e1e4e8; min-height: 100vh; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .header {{ background: linear-gradient(135deg, #161b22 0%, #1a1e2e 50%, #2d1b4e 100%);
            padding: 20px 32px; border-bottom: 1px solid #30363d; }}
  .header h1 {{ font-size: 20px; font-weight: 600; color: #f0f6fc; }}
  .header .meta {{ color: #8b949e; font-size: 12px; margin-top: 4px; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 32px; }}
  table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: #161b22;
          border-radius: 10px; overflow: hidden; border: 1px solid #30363d; }}
  th {{ background: #1c2128; color: #8b949e; padding: 10px 14px; text-align: left;
      font-size: 11px; text-transform: uppercase; letter-spacing: 1px; font-weight: 600;
      border-bottom: 1px solid #30363d; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid #21262d; font-size: 13px; }}
  tr:hover {{ background: #1c2128; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
           font-size: 10px; font-weight: 600; color: white; }}
  .nav {{ margin-bottom: 16px; }}
  .nav a {{ font-size: 13px; }}
  .footer {{ margin-top: 32px; padding: 16px; text-align: center; color: #484f58;
            font-size: 11px; border-top: 1px solid #21262d; }}
</style>
</head>
<body>
<div class="header">
  <h1>AI Analysis — <code>{html_mod.escape(project_name)}</code></h1>
  <div class="meta">Generated: {html_mod.escape(generated_at)} | {len(job_pages)} failed job(s)</div>
</div>
<div class="container">
  <div class="nav">
    <a href="../index.html">Back to Dashboard</a>
  </div>
  <table>
    <thead><tr><th>Job</th><th>Version</th><th>Category</th><th>AI Analysis</th></tr></thead>
    <tbody>
      {"".join(rows) if rows else '<tr><td colspan="4" style="text-align:center;color:#8b949e">No failed jobs</td></tr>'}
    </tbody>
  </table>
  <div class="footer">
    <a href="https://github.com/aabughosh/prow-nightly-monitor">prow-nightly-monitor</a> |
    AI-powered CI failure analysis
  </div>
</div>
</body>
</html>"""


def _generate_job_files(
    jobs: list[dict], project_name: str, generated_at: str, jobs_dir: Path
) -> list[dict]:
    """Generate HTML pages for a list of jobs. Returns page metadata."""
    job_pages = []
    latest_by_name: dict[str, dict] = {}

    for job in jobs:
        build_id = _extract_build_id(job)

        # Per-build page (unique per run)
        if build_id:
            filename = _job_filename(job["name"], build_id)
            page_html = generate_job_page(job, project_name, generated_at)
            (jobs_dir / filename).write_text(page_html)

        # Track latest build per job name for the default page
        start = job.get("start_time", "")
        prev = latest_by_name.get(job["name"])
        if not prev or start > prev.get("start_time", ""):
            latest_by_name[job["name"]] = job

        analysis = job.get("analysis", {})
        has_ai = bool(analysis.get("ai_summary") or analysis.get("issues"))

        job_pages.append({
            "name": job["name"],
            "filename": _job_filename(job["name"], build_id) if build_id else _job_filename(job["name"]),
            "category": analysis.get("category", "unknown"),
            "has_ai": has_ai,
            "build_id": build_id,
        })

    # Generate default (no build_id) pages pointing to the latest build
    for name, job in latest_by_name.items():
        filename = _job_filename(name)
        page_html = generate_job_page(job, project_name, generated_at)
        (jobs_dir / filename).write_text(page_html)

    return job_pages


def process_project(project_name: str) -> int:
    """Generate per-job pages for a single project. Returns count of pages generated."""
    results_path = PUBLIC_DIR / "projects" / project_name / "results.json"
    if not results_path.exists():
        print(f"  No results.json for {project_name}")
        return 0

    data = json.loads(results_path.read_text())
    generated_at = data.get("generated_at", datetime.now(timezone.utc).isoformat())

    jobs_dir = PUBLIC_DIR / "projects" / project_name / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    all_jobs = list(data.get("jobs", []))

    # Also pull in jobs from historical runs (past 7 days)
    runs_dir = PUBLIC_DIR / "projects" / project_name / "runs"
    if runs_dir.exists():
        seen_builds = {_extract_build_id(j) for j in all_jobs if _extract_build_id(j)}
        for run_results in sorted(runs_dir.glob("*/results.json")):
            try:
                run_data = json.loads(run_results.read_text())
                for job in run_data.get("jobs", []):
                    bid = _extract_build_id(job)
                    if bid and bid not in seen_builds:
                        all_jobs.append(job)
                        seen_builds.add(bid)
            except (json.JSONDecodeError, OSError):
                pass

    job_pages = _generate_job_files(all_jobs, project_name, generated_at, jobs_dir)

    index_html = generate_index_page(project_name, job_pages, generated_at)
    (jobs_dir / "index.html").write_text(index_html)

    print(f"  {project_name}: generated {len(job_pages)} job pages ({len(set(jp['build_id'] for jp in job_pages if jp['build_id']))} unique builds)")
    return len(job_pages)


def main():
    projects_json = REPO_DIR / "projects.json"
    if not projects_json.exists():
        print("projects.json not found")
        sys.exit(1)

    projects = json.loads(projects_json.read_text())

    filter_project = sys.argv[1] if len(sys.argv) > 1 else None
    if filter_project and filter_project not in projects:
        print(f"Unknown project: {filter_project}")
        print(f"Available: {', '.join(projects.keys())}")
        sys.exit(1)

    total = 0
    for name in projects:
        if filter_project and name != filter_project:
            continue
        print(f"Processing {name}...")
        total += process_project(name)

    print(f"Done: {total} total job pages generated")


if __name__ == "__main__":
    main()
