"""Build the CashAsh public site into `_site/`.

Two sources, one output tree:

- `marketing/site/` — the hand-written landing page and its assets.
- `docs/*.md` — the repo's own docs, rendered to HTML so the published site and
  the repo can never drift apart. There is no second copy to maintain.

Run it with the `site` dependency group:

    uv run --group site python marketing/build.py
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = REPO_ROOT / "marketing" / "site"
DOCS_SRC = REPO_ROOT / "docs"
DEFAULT_OUT = REPO_ROOT / "_site"

GITHUB_REPO = "https://github.com/Alchemication/cash-ash"
GITHUB_BLOB = f"{GITHUB_REPO}/blob/main"

# The landing page quotes the guardrail defaults, and those are owned by
# src/config.py. Importing them here means the page cannot drift from the code:
# change a default and the next build prints the new one.
sys.path.insert(0, str(REPO_ROOT / "src"))
import config  # noqa: E402

# Docs are listed in this order when one is present; anything not named here is
# appended alphabetically. Ordering is editorial — reference before plans —
# rather than derived from the filesystem.
DOCS_ORDER = [
    "commands",
    "roadmap",
]

# One-line blurbs for the docs index. A doc with no entry falls back to its
# first paragraph, which is usually serviceable but rarely as tight.
DOCS_BLURBS = {
    "commands": "Every CLI subcommand, the weekly cycle, and the evidence format.",
    "roadmap": "What is not built yet, in priority order, and why.",
}


@dataclass(frozen=True)
class Doc:
    """A rendered documentation page.

    Attributes:
        slug: Output filename stem, matching the source Markdown stem.
        title: Page title taken from the first H1, or a prettified slug.
        body: Rendered HTML body.
        blurb: One-line summary for the docs index.
    """

    slug: str
    title: str
    body: str
    blurb: str


def read_base_css() -> str:
    """Read the shared palette and site chrome.

    Every page inlines this rather than linking it, so each output file stays
    self-contained while the palette has exactly one definition.

    Returns:
        The stylesheet text.

    Raises:
        SystemExit: If the stylesheet is missing.
    """
    path = SITE_SRC / "assets" / "base.css"
    if not path.exists():
        print(
            f"error: {path.relative_to(REPO_ROOT)} is missing.\n"
            "Every page of the site inlines it for the palette and chrome. "
            "Restore it, or update read_base_css() if it moved.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return path.read_text(encoding="utf-8")


def rewrite_links(rendered: str, *, in_docs: bool) -> str:
    """Point Markdown-relative links at their built equivalents.

    Args:
        rendered: Rendered HTML fragment.
        in_docs: True when the fragment will live under `/docs/`, which makes
            sibling `foo.md` links resolve to `foo.html` in the same directory.

    Returns:
        The fragment with `.md` hrefs rewritten and repo-relative source paths
        pointed at GitHub.
    """

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        target, _, anchor = href.partition("#")
        suffix = f"#{anchor}" if anchor else ""

        if target.endswith(".md"):
            stem = Path(target).stem
            if stem == "README":
                # The landing page is the README's public face. Section anchors
                # from the README have no counterpart there, so they are dropped
                # rather than left pointing at an id that does not exist.
                return f'href="{"../" if in_docs else "./"}"'
            relative = (
                f"{stem}.html{suffix}" if in_docs else f"docs/{stem}.html{suffix}"
            )
            return f'href="{relative}"'

        # Repo-relative source paths (src/config.py, ../events.example.toml)
        # only exist on GitHub, never in the built tree. A docs page's `../`
        # points at the repo root, which is where GITHUB_BLOB already points.
        if target and not target.startswith("/"):
            clean = target.removeprefix("../") if in_docs else target
            return f'href="{GITHUB_BLOB}/{clean}{suffix}"'
        return match.group(0)

    return re.sub(r'href="([^"]+)"', replace, rendered)


def render_markdown(md: MarkdownIt, source: str, *, in_docs: bool) -> tuple[str, str]:
    """Render Markdown to an HTML body and extract its title.

    Args:
        md: Configured markdown-it renderer.
        source: Markdown text.
        in_docs: Passed through to link rewriting.

    Returns:
        A `(title, body_html)` pair. Title is the first H1 if present, else "".
    """
    body = rewrite_links(md.render(source), in_docs=in_docs)
    heading = re.search(r"^#\s+(.+)$", source, re.MULTILINE)
    title = heading.group(1).strip() if heading else ""
    # The H1 is reprinted by the page shell, so drop the duplicate from the body.
    body = re.sub(r"<h1>.*?</h1>\s*", "", body, count=1, flags=re.DOTALL)
    return title, body


def add_heading_anchors(body: str) -> str:
    """Give every H2 an id so the landing page can deep-link into a doc.

    Args:
        body: Rendered HTML.

    Returns:
        HTML with `<h2 id="slug">` in place of bare `<h2>`.
    """

    def replace(match: re.Match[str]) -> str:
        text = re.sub(r"<[^>]+>", "", match.group(1))
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return f'<h2 id="{slug}">{match.group(1)}</h2>'

    return re.sub(r"<h2>(.*?)</h2>", replace, body, flags=re.DOTALL)


def first_paragraph(source: str) -> str:
    """Return the first non-heading, non-quote paragraph as plain-ish text.

    Args:
        source: Markdown text.

    Returns:
        A trimmed one-line summary, or "" when nothing suitable is found.
    """
    for block in source.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(("#", ">", "-", "*", "|", "```")):
            continue
        flat = " ".join(block.split())
        flat = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", flat)
        flat = flat.replace("`", "")
        return flat if len(flat) <= 160 else flat[:157].rsplit(" ", 1)[0] + "…"
    return ""


def css_for_depth(base_css: str, depth: int) -> str:
    """Point the stylesheet's font URLs at the site root from a nested page.

    `base.css` names its font files relative to the site root. A page under
    `docs/` inlines the same text, so the prefix is rewritten rather than
    asking every page to know where the fonts live.

    Args:
        base_css: The shared stylesheet as written.
        depth: Directory depth below the site root.

    Returns:
        The stylesheet with `url("assets/` rewritten for that depth.
    """
    up = "../" * depth
    return base_css.replace('url("assets/', f'url("{up}assets/')


def heading_index(body: str) -> list[tuple[str, str]]:
    """List the `(id, text)` of every anchored H2, for a page's contents list.

    Args:
        body: HTML that has been through `add_heading_anchors`.

    Returns:
        Heading ids and their plain text, in document order.
    """
    found = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body, flags=re.DOTALL)
    return [(slug, re.sub(r"<[^>]+>", "", text)) for slug, text in found]


WORDMARK_SVG = """<svg viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="4" fill="var(--ledger-2)"/>
      <path d="M9.5 3v26" stroke="var(--margin)" stroke-width="2"/>
      <path d="M4 11h24M4 17h24M4 23h24" stroke="var(--rule)" stroke-width="1.5"/>
      <path d="M13 9h9M13 15h12M13 21h6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
    </svg>"""


def page_shell(
    *,
    title: str,
    description: str,
    base_css: str,
    content: str,
    depth: int,
    contents: list[tuple[str, str]] | None = None,
) -> str:
    """Wrap rendered content in the shared docs chrome.

    Args:
        title: Page title, used for `<title>` and the visible H1.
        description: Meta description text.
        base_css: The shared stylesheet, inlined into the page.
        content: HTML body content.
        depth: Directory depth below the site root, for relative asset paths.
        contents: `(id, text)` pairs for the page's own H2s, listed in the
            margin beside the article. Omit for pages with no sections.

    Returns:
        A complete HTML document.
    """
    up = "../" * depth
    base_css = css_for_depth(base_css, depth)
    links = "".join(
        f'\n    <a href="#{slug}">{html.escape(text)}</a>'
        for slug, text in contents or []
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — CashAsh</title>
<meta name="description" content="{html.escape(description)}">
<link rel="icon" href="{up}assets/favicon.svg" type="image/svg+xml">
<style>
{base_css}
/* Docs-specific: a prose column with the ledger margin down its left edge. */
body {{ line-height: 1.6; }}
.shell {{ width: min(960px, calc(100% - 40px)); }}
main {{ padding: 40px 0 88px; }}
.doc {{
  display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 0 48px;
}}
.doc > aside {{
  align-self: start; position: sticky; top: 24px;
  padding-right: 20px; border-right: 2px solid var(--margin);
  font-family: var(--display); font-size: 14px;
}}
.doc > aside a {{ display: block; padding: 3px 0; color: var(--ink-2); text-decoration: none; line-height: 1.3; }}
.doc > aside a:hover {{ color: var(--ink); }}
.doc > aside .up {{ margin-bottom: 14px; color: var(--ink); font-weight: 600; }}
.doc > article {{ min-width: 0; max-width: 680px; }}
h1 {{
  margin: 0 0 22px; font-family: var(--display);
  font-size: clamp(36px, 5vw, 56px); font-weight: 800; font-stretch: 80%; line-height: .98; letter-spacing: -.02em;
}}
h2 {{
  margin: 46px 0 12px; padding-top: 14px; border-top: 1px solid var(--line-strong);
  font-family: var(--display); font-size: 27px; font-weight: 700; font-stretch: 88%; line-height: 1.1; letter-spacing: -.015em;
}}
h3 {{ margin: 28px 0 8px; font-family: var(--display); font-size: 19px; font-weight: 700; font-stretch: 92%; }}
p, li {{ max-width: 70ch; }}
article a {{ color: var(--sourced); }}
pre {{
  margin: 18px 0; overflow-x: auto; padding: 16px 18px; border-radius: 4px;
  background: var(--ink); color: var(--ledger); line-height: 1.6; font-size: 13.5px;
}}
pre code {{ padding: 0; background: none; color: inherit; font-size: inherit; }}
blockquote {{
  margin: 20px 0; padding: 2px 0 2px 18px;
  border-left: 2px solid var(--margin); color: var(--ink-2); font-style: italic;
}}
table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-family: var(--display); font-size: 14.5px; line-height: 1.4; }}
th, td {{ padding: 8px 10px; border-top: 1px solid var(--rule); text-align: left; vertical-align: top; }}
tr:last-child td {{ border-bottom: 1px solid var(--rule); }}
th {{ border-top-color: var(--line-strong); font-weight: 650; }}
.table-scroll {{ overflow-x: auto; }}
.lede {{ margin: 0 0 30px; font-size: 20px; color: var(--ink-2); }}
.doc-list {{ list-style: none; margin: 0; padding: 0; border-bottom: 1px solid var(--rule); }}
.doc-list li {{ padding: 16px 0; border-top: 1px solid var(--rule); }}
.doc-list a {{ font-family: var(--display); font-size: 22px; font-weight: 700; font-stretch: 90%; color: var(--ink); text-decoration: none; }}
.doc-list a:hover {{ color: var(--sourced); }}
.doc-list span {{ display: block; color: var(--ink-2); }}
@media (max-width: 800px) {{
  .doc {{ grid-template-columns: 1fr; gap: 24px 0; }}
  .doc > aside {{ position: static; border-right: 0; border-left: 2px solid var(--margin); padding: 0 0 0 16px; }}
}}
</style>
</head>
<body>
<header class="shell topbar">
  <a class="wordmark" href="{up}">
    {WORDMARK_SVG}
    CashAsh
  </a>
  <nav>
    <a href="{up}docs/">Docs</a>
    <a href="{up}docs/roadmap.html">Roadmap</a>
    <a href="{GITHUB_REPO}">GitHub</a>
  </nav>
</header>
<main class="shell doc">
  <aside>
    <a class="up" href="{up}docs/">All docs</a>{links}
  </aside>
  <article>
<h1>{html.escape(title)}</h1>
{content}
  </article>
</main>
<footer class="shell">
  <span>CashAsh is not advice, not a broker and not a forecast.</span>
  <span><a href="{GITHUB_BLOB}/docs">Edit these docs on GitHub</a></span>
</footer>
</body>
</html>
"""


def wrap_tables(body: str) -> str:
    """Wrap tables so wide ones scroll instead of widening the page.

    Args:
        body: Rendered HTML.

    Returns:
        HTML with each `<table>` inside a horizontally scrollable container.
    """
    return body.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )


def build_docs(out: Path, base_css: str) -> list[Doc]:
    """Render every `docs/*.md` into `_site/docs/`.

    Args:
        out: Site output root.
        base_css: Shared stylesheet, inlined into each page.

    Returns:
        The rendered docs, in display order.
    """
    md = MarkdownIt("commonmark", {"html": False, "linkify": True})
    md.enable(["table", "strikethrough"])

    sources = sorted(DOCS_SRC.glob("*.md"))
    order = {slug: i for i, slug in enumerate(DOCS_ORDER)}
    sources.sort(key=lambda p: (order.get(p.stem, len(order)), p.stem))

    docs_out = out / "docs"
    docs_out.mkdir(parents=True, exist_ok=True)

    docs: list[Doc] = []
    for path in sources:
        source = path.read_text(encoding="utf-8")
        title, body = render_markdown(md, source, in_docs=True)
        title = title or path.stem.replace("-", " ").title()
        blurb = DOCS_BLURBS.get(path.stem) or first_paragraph(source)
        body = add_heading_anchors(wrap_tables(body))
        doc = Doc(slug=path.stem, title=title, body=body, blurb=blurb)
        docs.append(doc)
        (docs_out / f"{doc.slug}.html").write_text(
            page_shell(
                title=doc.title,
                description=blurb or f"CashAsh documentation: {doc.title}",
                base_css=base_css,
                content=doc.body,
                depth=1,
                contents=heading_index(doc.body),
            ),
            encoding="utf-8",
        )

    items = "\n".join(
        f'  <li><a href="{d.slug}.html">{html.escape(d.title)}</a>'
        f"<span>{html.escape(d.blurb)}</span></li>"
        for d in docs
    )
    index = (
        '<p class="lede">Rendered from the repository\'s own <code>docs/</code> '
        "directory on every push, so these pages and the code never drift apart.</p>\n"
        f'<ul class="doc-list">\n{items}\n</ul>'
    )
    (docs_out / "index.html").write_text(
        page_shell(
            title="Documentation",
            description="Commands, the weekly cycle, evidence format and roadmap for CashAsh.",
            base_css=base_css,
            content=index,
            depth=1,
        ),
        encoding="utf-8",
    )
    return docs


def _figure(value: float) -> str:
    """Format a config number the way a person would write it on a page."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def landing_placeholders() -> dict[str, str]:
    """Resolve `{{...}}` tokens in the landing page from the code that owns them.

    The landing page quotes guardrail defaults. Hard-coding them guarantees the
    page eventually lies, so they are read from `src/config.py` at build time.
    Environment overrides are honoured too — the build runs where the defaults
    apply, and CI has none set — so the published figures are the shipped ones.

    Returns:
        A mapping of placeholder name to replacement text.
    """
    return {
        "MAX_POSITION_WEIGHT_PCT": _figure(config.MAX_POSITION_WEIGHT_PCT),
        "MAX_NEW_TRADE_EUR": _figure(config.MAX_NEW_TRADE_EUR),
        "MAX_WEEKLY_ALLOCATION_EUR": _figure(config.MAX_WEEKLY_ALLOCATION_EUR),
        "RECOMMENDATION_EXPIRY_DAYS": str(config.RECOMMENDATION_EXPIRY_DAYS),
        "PRICE_STALE_AFTER_DAYS": str(config.PRICE_STALE_AFTER_DAYS),
        "BENCHMARK_NAME": html.escape(config.BENCHMARK_NAME),
    }


def render_landing(out: Path, base_css: str) -> None:
    """Copy the landing page, substituting its build-time placeholders.

    Args:
        out: Site output root.
        base_css: Shared stylesheet, inlined in place of `{{BASE_CSS}}`.

    Raises:
        SystemExit: If any `{{PLACEHOLDER}}` survives substitution, which would
            otherwise publish literal braces to visitors.
    """
    page = (SITE_SRC / "index.html").read_text(encoding="utf-8")
    substitutions = {
        "BASE_CSS": base_css,
        **landing_placeholders(),
    }
    for name, value in substitutions.items():
        page = page.replace(f"{{{{{name}}}}}", value)

    leftover = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", page)))
    if leftover:
        print(
            f"error: unresolved placeholder(s) in the landing page: {', '.join(leftover)}.\n"
            "Add them to landing_placeholders() or remove them from "
            "marketing/site/index.html.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    (out / "index.html").write_text(page, encoding="utf-8")


def build(out: Path) -> None:
    """Build the whole site.

    Args:
        out: Site output root; removed and recreated.
    """
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    base_css = read_base_css()

    render_landing(out, base_css)
    shutil.copytree(SITE_SRC / "assets", out / "assets")

    docs = build_docs(out, base_css)

    # Pages would otherwise run Jekyll, which skips files beginning with "_".
    (out / ".nojekyll").write_text("", encoding="utf-8")

    # --out may point outside the repository (CI, tests), so do not assume the
    # path is relative to it.
    shown = out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out
    print(f"Built {shown}: landing + {len(docs)} doc page(s)")


def main() -> None:
    """CLI entry point for the site build."""
    parser = argparse.ArgumentParser(description="Build the CashAsh public site.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    build(args.out.resolve())


if __name__ == "__main__":
    main()
