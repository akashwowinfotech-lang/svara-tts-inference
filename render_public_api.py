"""Render the shareable developer guide from PUBLIC_API.md."""

from __future__ import annotations

import html
import re
from pathlib import Path

from markdown_it import MarkdownIt


root = Path(__file__).resolve().parent
source = (root / "PUBLIC_API.md").read_text(encoding="utf-8")
markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
tokens = markdown.parse(source)
links = []
for index, token in enumerate(tokens):
    if token.type == "heading_open":
        title = tokens[index + 1].content
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        token.attrSet("id", slug)
        if token.tag == "h2":
            links.append((slug, title))

navigation = "\n".join(
    f'<a href="#{html.escape(slug)}">{html.escape(title)}</a>' for slug, title in links
)
body = markdown.renderer.render(tokens, markdown.options, {})
page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Speech API integration guide for HTTPS and WebSocket streaming.">
  <title>Speech API integration guide</title>
  <style>
    :root {{ font: 16px/1.55 system-ui,sans-serif; color:#e9f1fb; background:#0d1422; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; }}
    a {{ color:#83dfc5; }}
    .layout {{ display:grid; grid-template-columns:230px minmax(0,900px); gap:2.5rem; max-width:1230px; margin:auto; padding:2rem 1.2rem 5rem; }}
    nav {{ position:sticky; top:1.5rem; align-self:start; display:grid; gap:.15rem; max-height:90vh; overflow:auto; }}
    nav strong {{ color:#fff; margin-bottom:.6rem; }}
    nav a {{ color:#a9bbd2; padding:.35rem .5rem; text-decoration:none; border-radius:6px; }}
    nav a:hover,nav a:focus {{ color:#fff; background:#213149; }}
    article {{ min-width:0; }}
    h1 {{ font-size:2.25rem; line-height:1.2; margin:0 0 1rem; }}
    h2 {{ font-size:1.45rem; margin:2.4rem 0 .6rem; padding-top:.3rem; border-top:1px solid #32445d; }}
    h3 {{ font-size:1.15rem; margin:1.6rem 0 .5rem; }}
    p,li {{ color:#c4d0df; }}
    strong {{ color:#f5f8fc; }}
    code {{ font: .9em ui-monospace,SFMono-Regular,Consolas,monospace; background:#1b2a3e; padding:.1rem .25rem; border-radius:4px; overflow-wrap:anywhere; }}
    pre {{ overflow:auto; padding:1rem; background:#111e30; border:1px solid #334963; border-radius:10px; line-height:1.5; }}
    pre code {{ background:none; padding:0; overflow-wrap:normal; }}
    table {{ display:block; overflow-x:auto; border-collapse:collapse; width:100%; margin:1rem 0; }}
    th,td {{ padding:.6rem .7rem; text-align:left; border:1px solid #30425b; vertical-align:top; min-width:110px; }}
    th {{ background:#1b2b42; color:#eaf5ff; }}
    td {{ color:#c4d0df; }}
    blockquote {{ border-left:3px solid #78dfbd; margin:1rem 0; padding:.1rem 1rem; background:#172538; }}
    .download {{ display:inline-block; margin-top:1.5rem; font-size:.92rem; }}
    @media(max-width:850px) {{ .layout {{ display:block; }} nav {{ position:static; display:flex; flex-wrap:wrap; margin-bottom:1.8rem; }} nav strong {{ width:100%; }} nav a {{ padding:.25rem .4rem; }} }}
  </style>
</head>
<body>
  <div class="layout">
    <nav aria-label="Guide sections"><strong>On this page</strong>{navigation}<a class="download" href="/integration.md" download>Download Markdown</a></nav>
    <article>{body}</article>
  </div>
</body>
</html>
"""
(root / "developer-guide.html").write_text(page, encoding="utf-8")
print("Rendered developer-guide.html")
