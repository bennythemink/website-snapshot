"""
llm_export.py – Convert downloaded HTML pages into a single LLM-friendly Markdown file.

Features
--------
* Extracts **main content only** (prefers <main>, <article>, [role="main"])
* Strips navigation, footer, script, style, and hidden elements
* Deduplicates repeated sections (e.g. shared headers/footers across pages)
* Produces a single consolidated .md with YAML-style frontmatter
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, Tag
from markdownify import markdownify as md


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tags / selectors that never carry useful page content
_STRIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "canvas"}
_STRIP_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
_STRIP_SELECTORS = [
    "nav",
    "header",
    "footer",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    "[aria-hidden='true']",
    ".cookie-banner",
    ".cookie-consent",
    "#cookie-banner",
    "#cookie-consent",
]

# Containers that are likely to hold the page's primary content
_MAIN_SELECTORS = [
    "main",
    "[role='main']",
    "article",
    "#main-content",
    "#content",
    ".main-content",
    ".content",
    "#main",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _remove_noise(soup: BeautifulSoup) -> None:
    """Remove non-content elements in-place."""
    # Comments
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Unwanted tags
    for tag_name in _STRIP_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()

    # Unwanted roles
    for el in soup.find_all(attrs={"role": True}):
        if isinstance(el, Tag) and el.get("role") in _STRIP_ROLES:
            el.decompose()

    # Unwanted CSS selectors
    for sel in _STRIP_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    # Hidden elements
    for el in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        el.decompose()


def _extract_main_content(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """Return the most likely main-content container, or the whole <body>."""
    for sel in _MAIN_SELECTORS:
        container = soup.select_one(sel)
        if container and container.get_text(strip=True):
            return container
    # Fallback: use <body> if present
    body = soup.find("body")
    return body if body else soup


def _get_title(soup: BeautifulSoup) -> str:
    """Extract page title from <title> or first <h1>."""
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        return title_tag.get_text(strip=True)
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return "Untitled"


def _content_hash(text: str) -> str:
    """Return a short hash for deduplication."""
    # Normalise whitespace before hashing so trivial differences don't matter
    normalised = re.sub(r"\s+", " ", text.strip())
    return hashlib.md5(normalised.encode()).hexdigest()


def _html_to_markdown(html_tag: Tag | BeautifulSoup) -> str:
    """Convert an HTML element to clean Markdown."""
    raw = md(
        str(html_tag),
        heading_style="ATX",
        strip=["img"],  # strip images – they're not useful for LLM text
    )
    # Collapse excessive blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", raw)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_llm_export(
    site_dir: Path,
    output_path: Path,
    source_url: str,
) -> Path:
    """Walk *site_dir* for HTML files and produce a consolidated Markdown file.

    Parameters
    ----------
    site_dir:
        Directory containing the downloaded site (HTML + assets).
    output_path:
        Full path to write the resulting ``.md`` file.
    source_url:
        Original URL that was cloned (for frontmatter).

    Returns
    -------
    Path to the written Markdown file.
    """
    html_files = sorted(site_dir.rglob("*.html"))
    if not html_files:
        # Nothing to export
        output_path.write_text(
            f"---\nsource: {source_url}\npages: 0\n---\n\nNo HTML pages found.\n",
            encoding="utf-8",
        )
        return output_path

    sections: list[dict] = []  # {title, rel_path, markdown}
    seen_hashes: set[str] = set()

    for html_file in html_files:
        rel = html_file.relative_to(site_dir)
        html = html_file.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        title = _get_title(soup)
        _remove_noise(soup)
        main_el = _extract_main_content(soup)
        markdown = _html_to_markdown(main_el)

        if not markdown.strip():
            continue

        # Deduplicate: skip if this page's content is identical to one already seen
        h = _content_hash(markdown)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        sections.append({
            "title": title,
            "rel_path": str(rel),
            "markdown": markdown,
        })

    # Build the final document
    parsed = urlparse(source_url)
    domain = parsed.hostname or source_url
    lines: list[str] = []

    # YAML-style frontmatter
    lines.append("---")
    lines.append(f"source: {source_url}")
    lines.append(f"domain: {domain}")
    lines.append(f"pages: {len(sections)}")
    lines.append("---")
    lines.append("")

    for i, sec in enumerate(sections):
        lines.append(f"# {sec['title']}")
        lines.append(f"*Source: {sec['rel_path']}*")
        lines.append("")
        lines.append(sec["markdown"])
        lines.append("")
        if i < len(sections) - 1:
            lines.append("---")
            lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
