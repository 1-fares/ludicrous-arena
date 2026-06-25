#!/usr/bin/env python3
"""Render the prose docs (GAMES / API / ARCHITECTURE) into a small static site for
docs.ludicrous-arena.com. Output: backend/build/docs-site/. Run via build-docs.sh
(which provides the `markdown` dependency through uv).

Cross-links between the doc files are rewritten to the rendered .html pages; other
repo-relative links (the repo is private) are flattened to plain text; http links
are left alone. A prominent link points at the live interactive API.
"""
import pathlib
import re

import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "backend" / "build" / "docs-site"
API_DOCS_URL = "https://api.ludicrous-arena.com/docs"

PAGES = [
    ("games", "Game guide", ROOT / "docs" / "GAMES.md"),
    ("api", "API reference", ROOT / "docs" / "API.md"),
    ("architecture", "Architecture", ROOT / "docs" / "ARCHITECTURE.md"),
]
NAV = [("index.html", "Home"), ("games.html", "Games"), ("api.html", "API"),
       ("architecture.html", "Architecture"), (API_DOCS_URL, "Live API ↗")]
DOC_MAP = {"games.md": "games.html", "api.md": "api.html",
           "architecture.md": "architecture.html", "readme.md": "index.html"}

CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0a0e16;color:#cdd6e6;font:16px/1.65 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:rgba(10,14,22,.92);backdrop-filter:blur(6px);border-bottom:1px solid #1c2433}
nav{max-width:860px;margin:0 auto;padding:14px 20px;display:flex;gap:18px;flex-wrap:wrap;align-items:center}
nav .brand{font-weight:700;color:#7cc4ff;margin-right:8px;letter-spacing:.02em}
nav a{color:#9fb0cc;text-decoration:none;font-size:14px}
nav a:hover{color:#fff}
main{max-width:860px;margin:0 auto;padding:32px 20px 80px}
h1,h2,h3{color:#eaf0fb;line-height:1.25}
h1{font-size:2rem;margin-top:0}
h2{margin-top:2.2rem;border-bottom:1px solid #1c2433;padding-bottom:.3rem}
a{color:#7cc4ff}
code{background:#141b29;padding:.15em .4em;border-radius:4px;font-size:.92em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:#0f1521;border:1px solid #1c2433;border-radius:8px;padding:14px 16px;overflow:auto}
pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;margin:1rem 0}
th,td{border:1px solid #1c2433;padding:8px 10px;text-align:left}
th{background:#10182a}
blockquote{border-left:3px solid #2a3b52;margin:1rem 0;padding:.2rem 1rem;color:#9fb0cc}
.hero{padding:14px 0 8px}
.hero p{color:#9fb0cc;font-size:1.05rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:24px}
.card{display:block;border:1px solid #1c2433;border-radius:10px;padding:18px;text-decoration:none;color:inherit;background:#0f1521}
.card:hover{border-color:#33507a}
.card b{color:#eaf0fb;display:block;margin-bottom:4px}
.card span{color:#9fb0cc;font-size:.9rem}
footer{max-width:860px;margin:0 auto;padding:24px 20px 60px;color:#5a6a8c;font-size:.85rem;border-top:1px solid #1c2433}
"""


def shell(title: str, body: str) -> str:
    nav = "".join(f'<a href="{href}">{label}</a>' for href, label in NAV)
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title} · Ludicrous Arena</title><style>{CSS}</style></head><body>"
            f"<header><nav><span class=\"brand\">LUDICROUS ARENA</span>{nav}</nav></header>"
            f"<main>{body}</main>"
            f"<footer>Ludicrous Arena · API-controlled multiplayer game server. "
            f"Hosted in Switzerland (eu-central-2).</footer></body></html>")


def rewrite_links(text: str) -> str:
    def repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        base, _, anchor = target.partition("#")
        if target.startswith("http"):
            return m.group(0)
        if base == "" and anchor:           # in-page anchor
            return m.group(0)
        leaf = base.lower().rstrip("/").split("/")[-1]
        if leaf in DOC_MAP:
            tail = f"#{anchor}" if anchor else ""
            return f"[{label}]({DOC_MAP[leaf]}{tail})"
        return label                        # private-repo relative link: keep text only
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl, text)


def index_body() -> str:
    cards = "".join(
        f'<a class="card" href="{slug}.html"><b>{title}</b>'
        f'<span>{desc}</span></a>'
        for slug, title, desc in [
            ("games", "Game guide", "The games, their rules, actions, and observations."),
            ("api", "API reference", "Endpoints, auth, and the request/response shapes."),
            ("architecture", "Architecture", "The serverless, on-demand design."),
        ])
    cards += (f'<a class="card" href="{API_DOCS_URL}"><b>Live API ↗</b>'
              f'<span>Interactive Swagger against the running server.</span></a>')
    return (
        '<div class="hero"><h1>Ludicrous Arena</h1>'
        '<p>A multiplayer game server you play through an API, not a controller. '
        'Point your agent at the arena with a token and it drives your character; '
        'the browser viewer is just a window onto the action.</p></div>'
        f'<div class="cards">{cards}</div>')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    md = markdown.Markdown(extensions=["fenced_code", "tables", "toc", "sane_lists"])
    (OUT / "index.html").write_text(shell("Home", index_body()), encoding="utf-8")
    for slug, title, path in PAGES:
        md.reset()
        body = md.convert(rewrite_links(path.read_text(encoding="utf-8")))
        (OUT / f"{slug}.html").write_text(shell(title, body), encoding="utf-8")
    print(f"built {1 + len(PAGES)} pages -> {OUT}")


if __name__ == "__main__":
    main()
