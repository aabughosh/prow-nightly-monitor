#!/usr/bin/env python3
"""Convert a markdown report to a styled HTML page matching the dashboard theme."""
import re
import sys
from pathlib import Path
from datetime import datetime, timezone


def md_to_html(md_text: str) -> str:
    """Simple markdown to HTML converter for investigation reports."""
    html_lines = []
    in_code = False
    in_table = False
    in_list = False

    for line in md_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                html_lines.append("</pre>")
                in_code = False
            else:
                html_lines.append('<pre style="background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;font-size:12px;color:#f0883e;overflow-x:auto;white-space:pre-wrap">')
                in_code = True
            continue

        if in_code:
            html_lines.append(line)
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= set("- :") for c in cells):
                continue
            if not in_table:
                html_lines.append('<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:13px">')
                tag = "th"
                in_table = True
            else:
                tag = "td"
            row = "".join(
                f'<{tag} style="padding:8px 12px;border:1px solid #30363d;text-align:left">{_inline(c)}</{tag}>'
                for c in cells
            )
            html_lines.append(f"<tr>{row}</tr>")
            continue
        elif in_table:
            html_lines.append("</table>")
            in_table = False

        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul style='margin:8px 0;padding-left:20px'>")
                in_list = True
            html_lines.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        elif in_list and stripped:
            html_lines.append("</ul>")
            in_list = False

        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br>")
            continue

        if stripped.startswith("# "):
            html_lines.append(f'<h1 style="font-size:20px;color:#f0f6fc;margin:20px 0 10px;border-bottom:1px solid #30363d;padding-bottom:8px">{_inline(stripped[2:])}</h1>')
        elif stripped.startswith("## "):
            html_lines.append(f'<h2 style="font-size:16px;color:#f0f6fc;margin:20px 0 8px;border-bottom:1px solid #21262d;padding-bottom:6px">{_inline(stripped[3:])}</h2>')
        elif stripped.startswith("### "):
            html_lines.append(f'<h3 style="font-size:14px;color:#c9d1d9;margin:16px 0 6px">{_inline(stripped[4:])}</h3>')
        elif stripped.startswith("> "):
            html_lines.append(f'<blockquote style="border-left:3px solid #30363d;padding:4px 12px;color:#8b949e;margin:8px 0;font-size:13px">{_inline(stripped[2:])}</blockquote>')
        elif stripped.startswith("---"):
            html_lines.append('<hr style="border:none;border-top:1px solid #21262d;margin:20px 0">')
        else:
            html_lines.append(f"<p style='margin:6px 0;line-height:1.5'>{_inline(stripped)}</p>")

    if in_table:
        html_lines.append("</table>")
    if in_list:
        html_lines.append("</ul>")
    if in_code:
        html_lines.append("</pre>")

    return "\n".join(html_lines)


def _inline(text: str) -> str:
    """Convert inline markdown (bold, code, links)."""
    text = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color:#f0f6fc">\1</strong>', text)
    text = re.sub(r"`([^`]+)`", r'<code style="background:#21262d;padding:1px 5px;border-radius:3px;font-size:12px;color:#f0883e">\1</code>', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" style="color:#58a6ff">\1</a>', text)
    return text


def convert(md_path: str, html_path: str) -> None:
    md_text = Path(md_path).read_text()
    body = md_to_html(md_text)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Investigation Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', -apple-system, sans-serif; background: #0d1117; color: #e1e4e8; min-height: 100vh; }}
  .header {{ background: linear-gradient(135deg, #161b22 0%, #1a1e2e 50%, #2d1b4e 100%); padding: 20px 32px; border-bottom: 1px solid #30363d; }}
  .header h1 {{ font-size: 20px; color: #f0f6fc; }}
  .header .meta {{ color: #8b949e; font-size: 12px; margin-top: 4px; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 32px; font-size: 14px; color: #c9d1d9; line-height: 1.6; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .footer {{ margin-top: 32px; padding: 16px; text-align: center; color: #484f58; font-size: 11px; border-top: 1px solid #21262d; }}
</style>
</head>
<body>
<div class="header">
  <h1>Claude Investigation Report</h1>
  <div class="meta">Generated: {now} | Analyzed by Claude (Cursor CLI) | <a href="./" style="color:#8b949e">Back to Dashboard</a></div>
</div>
<div class="container">
{body}
</div>
<div class="footer">
  <a href="./">Dashboard</a> | <a href="history.html">Run History</a> | <a href="https://github.com/aabughosh/prow-nightly-monitor">Source</a>
</div>
</body>
</html>"""

    Path(html_path).write_text(html)
    print(f"Converted {md_path} -> {html_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} input.md output.html")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
