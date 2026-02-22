"""
cloner.py – wget-based web page cloner with async streaming.

Responsibilities:
 • Sanitise slugs and build safe output paths.
 • Build the wget argument list (no shell=True anywhere).
 • Run wget as an asyncio subprocess, stream stdout/stderr lines
   into an asyncio.Queue that the SSE endpoint reads from.
 • Enforce a 2-minute timeout and a ~200 MB size cap.
 • After wget finishes, locate the main HTML file for preview.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"
MAX_TIMEOUT_SECONDS = 120  # 2 minutes
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB
SLUG_MAX_LENGTH = 48
SLUG_PATTERN = re.compile(r"[^a-z0-9-]")
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    job_id: str
    url: str
    slug: str
    extra_domains: list[str]
    remove_links: bool = False
    strip_dead_links: bool = False
    llm_export: bool = False
    max_pages: int = 1
    crawl_depth: int = 0
    status: JobStatus = JobStatus.PENDING
    output_dir: Path = Path(".")
    site_dir: Path = Path(".")
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    error: Optional[str] = None
    main_html: Optional[str] = None
    llm_export_file: Optional[str] = None
    created_at: float = field(default_factory=time.time)


# In-memory job store  (single-process / single-user is fine)
jobs: dict[str, Job] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitise_slug(raw: str | None, url: str) -> str:
    """Return a filesystem-safe slug derived from user input or the URL host."""
    if raw and raw.strip():
        base = raw.strip().lower()
    else:
        parsed = urlparse(url)
        base = parsed.hostname or "site"
    base = SLUG_PATTERN.sub("-", base).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    return base[:SLUG_MAX_LENGTH] or "site"


def _dir_size(path: Path) -> int:
    """Total bytes of all files under *path*."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _has_html_files(site_dir: Path) -> bool:
    """Return True if site_dir contains at least one HTML file."""
    for root, _dirs, files in os.walk(site_dir):
        for f in files:
            if f.endswith((".html", ".htm")):
                return True
    return False


def _count_html_files(site_dir: Path) -> int:
    """Count .html/.htm files under *site_dir*."""
    count = 0
    for _root, _dirs, files in os.walk(site_dir):
        for f in files:
            if f.endswith((".html", ".htm")):
                count += 1
    return count


def _find_main_html(site_dir: Path, url: str) -> str | None:
    """Heuristically locate the main HTML file wget saved."""
    # wget saves into site_dir/<host>/path/…
    # First try: look for index.html anywhere
    for root, _dirs, files in os.walk(site_dir):
        for f in files:
            if f == "index.html":
                return os.path.relpath(os.path.join(root, f), site_dir)

    # Second try: first .html file
    for root, _dirs, files in os.walk(site_dir):
        for f in sorted(files):
            if f.endswith((".html", ".htm")):
                return os.path.relpath(os.path.join(root, f), site_dir)

    return None


def _strip_links(site_dir: Path) -> int:
    """Remove href attribute from all <a> tags in HTML files under site_dir.

    Links remain visible but become inert (href removed, not emptied).
    Returns the total number of links modified.
    """
    link_pattern = re.compile(
        r"""(<a\b[^>]*?)\s+href\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
        re.IGNORECASE,
    )
    total = 0
    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            if not fname.endswith((".html", ".htm")):
                continue
            fpath = Path(root) / fname
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new_content, count = link_pattern.subn(r'\1', content)
            if count > 0:
                fpath.write_text(new_content, encoding="utf-8")
                total += count
    return total


def _fix_css_fragment_references(site_dir: Path) -> int:
    """Restore CSS ``url(#id)`` references corrupted by wget ``--convert-links``.

    wget's ``--convert-links`` misinterprets same-document SVG fragment
    references such as ``url(#home-clipping-path)`` inside CSS files and
    rewrites them as relative paths that point back at the CSS file itself
    (e.g. ``url(main.css)``).  This breaks ``clip-path``, ``filter``,
    ``mask``, and ``fill`` references to inline SVG definitions.

    Strategy:
     1. Collect every ``id`` from ``<clipPath>``, ``<filter>``, ``<mask>``,
        and ``<linearGradient>``/``<radialGradient>`` elements found in the
        downloaded HTML files.
     2. For each CSS file, find ``url(...)`` values where the reference
        matches the CSS file's own name (the telltale sign of corruption).
     3. Replace those self-references with the matching ``url(#id)`` using
        the collected SVG IDs.

    Returns the total number of ``url()`` values restored.
    """
    # ------------------------------------------------------------------
    # Step 1 – harvest SVG-definition IDs from all HTML files
    # ------------------------------------------------------------------
    svg_id_pattern = re.compile(
        r"<(?:clipPath|filter|mask|linearGradient|radialGradient)"
        r'[^>]+\bid\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    svg_ids: set[str] = set()

    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            if not fname.endswith((".html", ".htm")):
                continue
            fpath = Path(root) / fname
            try:
                html = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            svg_ids.update(svg_id_pattern.findall(html))

    if not svg_ids:
        return 0  # nothing to fix

    # ------------------------------------------------------------------
    # Step 2 & 3 – scan CSS files and restore corrupted url() values
    # ------------------------------------------------------------------
    total_fixed = 0

    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            if not fname.endswith(".css"):
                continue
            fpath = Path(root) / fname

            try:
                css = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Build a pattern that matches url(<this-css-filename>)
            # wget may or may not add a path prefix, but the filename
            # (possibly with query-string mangling) will always appear.
            # Escape the filename for regex and allow optional leading path.
            escaped_name = re.escape(fname)
            self_ref = re.compile(
                r"url\(\s*" + r"(?:[^)]*/)?" + escaped_name + r"\s*\)",
                re.IGNORECASE,
            )

            if not self_ref.search(css):
                continue

            # If there is exactly one SVG ID we can replace directly.
            # If there are multiple, we try to narrow down by looking at
            # CSS property context (clip-path → clipPath, filter → filter, etc.)
            # For safety, if we cannot disambiguate we still fix single-ID cases.
            def _replace(match: re.Match) -> str:  # noqa: ANN001
                """Pick the right SVG fragment ID for this url() occurrence."""
                # Walk backwards from the match to find the CSS property name
                start = max(0, match.start() - 200)
                context = css[start:match.start()]
                # Find the last property name before this url()
                prop_match = re.findall(
                    r"([\w-]+)\s*:\s*[^;]*$", context, re.DOTALL,
                )
                prop_name = prop_match[-1].lower() if prop_match else ""

                # Try to pick the best SVG ID by matching property to tag type
                prop_tag_map = {
                    "clip-path": "clip",
                    "-webkit-clip-path": "clip",
                    "filter": "filter",
                    "mask": "mask",
                    "fill": "gradient",
                    "background": "gradient",
                }
                hint = prop_tag_map.get(prop_name, "")

                # Score each SVG ID by how well it matches the context
                best: str | None = None
                for sid in svg_ids:
                    sid_lower = sid.lower()
                    if hint and hint in sid_lower:
                        best = sid
                        break  # good match
                    if best is None:
                        best = sid  # fallback to first

                if best:
                    return f"url(#{best})"
                return match.group(0)  # leave unchanged if no IDs

            new_css, count = self_ref.subn(_replace, css)
            if count:
                fpath.write_text(new_css, encoding="utf-8")
                total_fixed += count

    return total_fixed


def _rewrite_root_relative_urls(site_dir: Path) -> int:
    """Convert root-relative URLs (/path/…) to relative URLs in HTML and CSS.

    wget's ``--convert-links`` only runs when wget exits normally.  If wget
    is terminated (SIGTERM for page / size limits) the conversion is skipped
    and references stay root-relative (e.g. ``/Themes/style.css``).  These
    break in the preview which serves files under ``/preview/{job_id}/…``
    because the browser resolves them against the server root.

    When ``--convert-links`` *has* already run, all references are already
    relative so the regexes below match nothing and this is effectively a
    no-op.

    Returns the total number of URLs rewritten.
    """
    total = 0

    for root, _dirs, files in os.walk(site_dir):
        rel_dir = os.path.relpath(root, site_dir)
        if rel_dir == ".":
            prefix = ""
        else:
            depth = len(Path(rel_dir).parts)
            prefix = "../" * depth

        for fname in files:
            fpath = Path(root) / fname
            is_html = fname.endswith((".html", ".htm"))
            is_css = fname.endswith(".css")
            if not (is_html or is_css):
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            original = content
            file_count = 0

            # HTML: rewrite href="/…", src="/…", action="/…"
            if is_html:
                # First: handle bare "/" → "index.html" (e.g. href="/")
                # Uses a lookahead so we only match "/" immediately before the
                # closing quote, not "/cables" etc.
                def _repl_root(m, _pfx=prefix):
                    nonlocal file_count
                    file_count += 1
                    return m.group(1) + _pfx + "index.html"

                content = re.sub(
                    r'((?:href|src|action)\s*=\s*["\'])/(?=["\'])',
                    _repl_root,
                    content,
                    flags=re.IGNORECASE,
                )

                # Then: general root-relative rewrite  /foo → foo
                def _repl_attr(m, _pfx=prefix):
                    nonlocal file_count
                    file_count += 1
                    return m.group(1) + _pfx

                content = re.sub(
                    r'((?:href|src|action)\s*=\s*["\'])/(?!/)',
                    _repl_attr,
                    content,
                    flags=re.IGNORECASE,
                )

            # CSS and inline styles: rewrite url(/…)
            def _repl_url(m, _pfx=prefix):
                nonlocal file_count
                file_count += 1
                return "url(" + m.group(1) + _pfx

            content = re.sub(
                r'url\(\s*(["\']?)/(?!/)',
                _repl_url,
                content,
                flags=re.IGNORECASE,
            )

            if content != original:
                fpath.write_text(content, encoding="utf-8")
                total += file_count

    return total


def _fix_wget_filenames(site_dir: Path) -> int:
    """Rewrite HTML/CSS references to match wget's on-disk filenames.

    When wget is terminated before ``--convert-links`` runs, references in
    HTML files keep their original form (e.g. ``styles.css?t=123``) while
    the file on disk was saved with wget's filename mangling:

    * ``--restrict-file-names=windows`` replaces ``?`` → ``@``
    * ``--adjust-extension`` may append an extra ``.css`` / ``.html``

    This function builds an index of every file on disk, then scans each
    HTML/CSS file and rewrites ``href``, ``src``, and ``url()`` values to
    point at the actual filenames that exist.

    Returns the number of references fixed.
    """
    # Build lookup: relative-path → True for every file under site_dir
    on_disk: set[str] = set()
    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), site_dir)
            on_disk.add(rel.replace(os.sep, "/"))

    total = 0

    def _mangle(ref: str) -> str | None:
        """Try to find the mangled filename that matches *ref* on disk."""
        # Strip fragment
        base = ref.split("#")[0]
        if not base:
            return None

        # Already exists as-is?
        if base in on_disk:
            return None

        # Try ?→@ replacement (restrict-file-names=windows)
        mangled = base.replace("?", "@")
        if mangled in on_disk:
            return mangled

        # Try with extra extension appended (--adjust-extension)
        for ext in (".css", ".html", ".htm", ".js"):
            if (mangled + ext) in on_disk:
                return mangled + ext
            if (base + ext) in on_disk:
                return base + ext

        return None

    def _resolve_and_fix(ref: str, file_rel_dir: str) -> str | None:
        """Given a reference from an HTML/CSS file, return the fixed version or None."""
        # Skip absolute URLs, fragments, data URIs, etc.
        if not ref or ref.startswith(("http://", "https://", "//", "#", "data:", "javascript:", "mailto:", "tel:")):
            return None

        # Resolve relative to the file's directory
        if file_rel_dir and file_rel_dir != ".":
            full_ref = os.path.normpath(file_rel_dir + "/" + ref).replace(os.sep, "/")
        else:
            full_ref = os.path.normpath(ref).replace(os.sep, "/")

        replacement = _mangle(full_ref)
        if replacement is None:
            return None

        # Convert back to relative from this file's dir
        if file_rel_dir and file_rel_dir != ".":
            return os.path.relpath(replacement, file_rel_dir).replace(os.sep, "/")
        return replacement

    # Pattern for href="...", src="...", action="..."  (double or single quotes)
    attr_re = re.compile(
        r'((?:href|src|action)\s*=\s*["\'])([^"\']*?)(["\'])',
        re.IGNORECASE,
    )

    for root, _dirs, files in os.walk(site_dir):
        rel_dir = os.path.relpath(root, site_dir).replace(os.sep, "/")

        for fname in files:
            fpath = Path(root) / fname
            is_html = fname.endswith((".html", ".htm"))
            is_css = fname.endswith(".css")
            if not (is_html or is_css):
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            original = content

            if is_html:
                def _fix_html(m, _dir=rel_dir):
                    nonlocal total
                    fixed = _resolve_and_fix(m.group(2), _dir)
                    if fixed is None:
                        return m.group(0)
                    total += 1
                    return m.group(1) + fixed + m.group(3)

                content = attr_re.sub(_fix_html, content)

            # TODO: also fix url() in CSS files if needed in future

            if content != original:
                fpath.write_text(content, encoding="utf-8")

    return total


def _clean_empty_hrefs(site_dir: Path) -> int:
    """Remove pre-existing ``href=""`` and ``href=''`` attributes from anchors.

    Many sites include ``<a href="">`` placeholders (JS templates, etc.)
    which cause the browser to reload the current page when clicked.
    Converting them to plain ``<a>`` (no href) makes them inert.

    Returns the number of links cleaned.
    """
    empty_href_re = re.compile(
        r"""(<a\b[^>]*?)\s+href\s*=\s*(?:""|'')""",
        re.IGNORECASE,
    )
    total = 0
    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            if not fname.endswith((".html", ".htm")):
                continue
            fpath = Path(root) / fname
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new_content, count = empty_href_re.subn(r'\1', content)
            if count > 0:
                fpath.write_text(new_content, encoding="utf-8")
                total += count
    return total


def _strip_dead_links(site_dir: Path) -> int:
    """Remove href from <a> tags that point to pages not present in site_dir.

    Links between downloaded pages are preserved.  Fragment-only links (#...)
    and mailto:/tel: links are left untouched.

    Returns the number of links stripped.
    """
    from urllib.parse import unquote, urlparse as _urlparse

    # Build a set of all downloaded file paths (relative to site_dir)
    downloaded: set[str] = set()
    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), site_dir)
            # Normalise separators
            downloaded.add(rel.replace(os.sep, "/"))
            # Also register directory index shorthand
            if fname in ("index.html", "index.htm"):
                parent_rel = os.path.relpath(root, site_dir).replace(os.sep, "/")
                if parent_rel == ".":
                    downloaded.add("")
                else:
                    downloaded.add(parent_rel)
                    downloaded.add(parent_rel + "/")

    href_re = re.compile(
        r"""(<a\b[^>]*?)\s+href\s*=\s*(?:"([^"]*)"|'([^']*)')""",
        re.IGNORECASE,
    )
    total = 0

    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            if not fname.endswith((".html", ".htm")):
                continue
            fpath = Path(root) / fname
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            file_dir_rel = os.path.relpath(root, site_dir).replace(os.sep, "/")

            def _check(match: re.Match) -> str:
                nonlocal total
                href = match.group(2) if match.group(2) is not None else match.group(3)
                if href is None:
                    return match.group(0)

                # Skip fragment-only, empty, mailto, tel, javascript links
                stripped = href.strip()
                if not stripped or stripped.startswith(("#", "mailto:", "tel:", "javascript:")):
                    return match.group(0)

                # Parse and check if it's an external URL
                parsed = _urlparse(stripped)
                if parsed.scheme in ("http", "https") or stripped.startswith("//"):
                    # Try to resolve the URL path to a downloaded file.
                    # Same-site absolute links (e.g. https://example.com/cables)
                    # should be converted to relative paths, not stripped.
                    url_path = unquote(parsed.path).strip("/")
                    if not url_path:
                        url_path = "index.html"

                    abs_candidates = [url_path]
                    if not url_path.endswith((".html", ".htm")):
                        abs_candidates.extend([
                            url_path + ".html",
                            url_path + "/index.html",
                            url_path + "/index.htm",
                        ])

                    found = None
                    for c in abs_candidates:
                        if c in downloaded:
                            found = c
                            break

                    if found is not None:
                        # Convert to relative path from this file's directory
                        if file_dir_rel and file_dir_rel != ".":
                            rel = os.path.relpath(found, file_dir_rel).replace(os.sep, "/")
                        else:
                            rel = found
                        return f'{match.group(1)} href="{rel}"'

                    # Truly external / not downloaded — remove href
                    total += 1
                    return match.group(1)

                # Resolve relative path
                path_part = unquote(parsed.path)
                if file_dir_rel and file_dir_rel != ".":
                    resolved = os.path.normpath(file_dir_rel + "/" + path_part)
                else:
                    resolved = os.path.normpath(path_part)
                resolved = resolved.replace(os.sep, "/")

                # Check with and without .html extension
                candidates = [resolved]
                if not resolved.endswith((".html", ".htm")):
                    candidates.append(resolved + "/index.html")
                    candidates.append(resolved + "/index.htm")
                    candidates.append(resolved + ".html")

                for candidate in candidates:
                    if candidate in downloaded:
                        return match.group(0)  # Link target exists — keep it

                # Target not downloaded — remove href
                total += 1
                return match.group(1)

            new_content = href_re.sub(_check, content)
            if new_content != content:
                fpath.write_text(new_content, encoding="utf-8")

    return total


def _write_robots_txt(site_dir: Path) -> None:
    """Write a robots.txt at the site root that blocks all crawlers."""
    content = "User-agent: *\nDisallow: /\n"
    (site_dir / "robots.txt").write_text(content)


def _inject_noindex(site_dir: Path) -> int:
    """Inject a noindex/nofollow meta tag into every HTML file's <head>.

    Also injects a CSS rule so that ``<a>`` elements without an ``href``
    attribute (dead-link-stripped anchors) look like plain text instead
    of interactive links.

    Returns the number of files modified.
    """
    meta_tag = '<meta name="robots" content="noindex, nofollow">'
    dead_link_css = (
        '<style>a:not([href]){color:inherit;text-decoration:none;'
        'pointer-events:none;cursor:default}</style>'
    )
    head_pattern = re.compile(r"(<head[^>]*>)", re.IGNORECASE)
    count = 0
    for root, _dirs, files in os.walk(site_dir):
        for fname in files:
            if not fname.endswith((".html", ".htm")):
                continue
            fpath = Path(root) / fname
            try:
                html = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if meta_tag in html:
                continue  # already present
            new_html, replacements = head_pattern.subn(
                rf"\1\n    {meta_tag}\n    {dead_link_css}", html, count=1,
            )
            if replacements:
                fpath.write_text(new_html, encoding="utf-8")
                count += 1
    return count


def _validate_url(url: str) -> str:
    """Basic URL validation.  Returns the normalised URL or raises."""
    url = url.strip()
    if not url:
        raise ValueError("URL must not be empty.")
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("Could not parse a hostname from the URL.")
    return url


# ---------------------------------------------------------------------------
# wget command builder
# ---------------------------------------------------------------------------

def _build_wget_args(
    url: str,
    site_dir: Path,
    extra_domains: list[str],
    user_agent: str | None = None,
    crawl_depth: int = 0,
) -> list[str]:
    """Build the wget argument list.  Never use shell=True."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    args: list[str] = [
        "wget",
        "--page-requisites",
        "--convert-links",
        "--adjust-extension",
        "--no-parent",
        "--restrict-file-names=windows",
        "--timeout=30",
        "--tries=2",
        "--wait=0.2",
        "--no-verbose",              # progress but not debug
        "-nH",                       # no host-name directories
        *(["--user-agent=" + user_agent] if user_agent else []),
        "-e", "robots=off",
        "-P", str(site_dir),
    ]

    # Recursive crawling
    if crawl_depth > 0:
        args += ["-r", f"--level={crawl_depth}"]

    all_domains = [host] + [d.strip() for d in extra_domains if d.strip()]
    if len(all_domains) > 1:
        args += [
            "--span-hosts",
            f"--domains={','.join(all_domains)}",
        ]

    args.append(url)
    return args


# ---------------------------------------------------------------------------
# Core async runner
# ---------------------------------------------------------------------------

async def _fetch_missing_assets(job: Job, user_agent: str | None = None) -> None:
    """Run a supplementary non-recursive wget to fetch page requisites.

    After the main recursive wget is killed at the page limit, some CSS/JS/
    image files referenced by downloaded pages may not have been fetched yet.
    This function runs wget **without** ``-r`` but **with** ``--page-requisites``
    and ``--convert-links`` to fetch missing assets and fix references.
    """
    site_dir = job.site_dir
    parsed = urlparse(job.url)
    host = parsed.hostname or ""

    args: list[str] = [
        "wget",
        "--page-requisites",
        "--convert-links",
        "--adjust-extension",
        "--no-parent",
        "--restrict-file-names=windows",
        "--timeout=30",
        "--tries=2",
        "--no-verbose",
        "-nH",
        *((["--user-agent=" + user_agent] if user_agent else
           ["--user-agent=" + BROWSER_USER_AGENT])),
        "-e", "robots=off",
        "-P", str(site_dir),
    ]

    all_domains = [host] + [d.strip() for d in job.extra_domains if d.strip()]
    if len(all_domains) > 1:
        args += ["--span-hosts", f"--domains={','.join(all_domains)}"]

    args.append(job.url)

    await job.queue.put("[info] Fetching missing page assets (supplementary pass)…")
    await job.queue.put(f"[cmd] {' '.join(args)}")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _drain(stream: asyncio.StreamReader, prefix: str):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                await job.queue.put(f"[{prefix}] {text}")

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _drain(proc.stdout, "wget-assets"),  # type: ignore[arg-type]
                _drain(proc.stderr, "wget-assets"),  # type: ignore[arg-type]
                proc.wait(),
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
        await job.queue.put("[warn] Asset fetch timed out.")
        return

    new_files = sum(len(fs) for _, _, fs in os.walk(site_dir))
    await job.queue.put(f"[info] Asset fetch complete (total files now: {new_files})")


async def _run_wget(job: Job) -> None:
    """Spawn wget, stream output lines into job.queue, enforce limits.

    If the first attempt produces no HTML files, retry once with a
    browser User-Agent header (many sites block wget's default UA).
    """
    job.status = JobStatus.RUNNING
    site_dir = job.site_dir

    max_pages = job.max_pages
    enforce_page_limit = job.crawl_depth > 0  # only kill for page count in recursive mode
    pages_limit_hit = False  # set when we intentionally kill wget for page limit

    async def _attempt(user_agent: str | None = None) -> int | None:
        """Run one wget attempt.  Returns the process return code, or None on timeout."""
        nonlocal pages_limit_hit
        pages_limit_hit = False
        site_dir.mkdir(parents=True, exist_ok=True)

        args = _build_wget_args(
            job.url, site_dir, job.extra_domains, user_agent, job.crawl_depth,
        )
        cmd_display = " ".join(args)
        await job.queue.put(f"[cmd] {cmd_display}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _stream(stream: asyncio.StreamReader, prefix: str):
            nonlocal pages_limit_hit
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    await job.queue.put(f"[{prefix}] {text}")
                # Combined size + page-count check in one walk
                total_bytes = 0
                html_count = 0
                for dirpath, _dirnames, filenames in os.walk(site_dir):
                    for fname in filenames:
                        fp = os.path.join(dirpath, fname)
                        try:
                            total_bytes += os.path.getsize(fp)
                        except OSError:
                            pass
                        if fname.endswith((".html", ".htm")):
                            html_count += 1
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    await job.queue.put("[warn] Download size limit exceeded – stopping wget.")
                    proc.terminate()
                    return
                if enforce_page_limit and html_count > max_pages:
                    pages_limit_hit = True
                    await job.queue.put(
                        f"[info] Reached max pages limit ({max_pages}) – stopping wget."
                    )
                    proc.terminate()
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _stream(proc.stdout, "wget"),  # type: ignore[arg-type]
                    _stream(proc.stderr, "wget"),  # type: ignore[arg-type]
                    proc.wait(),
                ),
                timeout=MAX_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.terminate()
            # Give wget a grace period to finish --convert-links
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            return None

        # If we sent SIGTERM (page/size limit), allow a grace period
        # for wget to finish its --convert-links post-processing.
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                proc.kill()

        return proc.returncode

    # ---- First attempt (default UA) ----
    rc = await _attempt()

    if rc is None:
        await job.queue.put("[error] wget timed out after 2 minutes.")
        job.status = JobStatus.FAILED
        job.error = "Timeout"
        await job.queue.put("[done]")
        return

    got_html = _has_html_files(site_dir)

    if (rc in (0, 8) and got_html) or (pages_limit_hit and got_html):
        # First attempt succeeded (or we intentionally stopped at the page limit)
        job.status = JobStatus.COMPLETED
        if pages_limit_hit:
            await job.queue.put("[info] wget stopped at page limit — download complete.")
            await _fetch_missing_assets(job)
        else:
            await job.queue.put("[info] wget finished successfully.")
    elif not got_html:
        # No HTML downloaded — retry with browser User-Agent
        await job.queue.put("[warn] No HTML files downloaded. Retrying with browser User-Agent…")
        shutil.rmtree(site_dir, ignore_errors=True)

        rc = await _attempt(user_agent=BROWSER_USER_AGENT)

        if rc is None:
            await job.queue.put("[error] wget timed out on retry.")
            job.status = JobStatus.FAILED
            job.error = "Timeout (retry)"
            await job.queue.put("[done]")
            return

        if (rc in (0, 8) or pages_limit_hit) and _has_html_files(site_dir):
            job.status = JobStatus.COMPLETED
            await job.queue.put("[info] Retry succeeded with browser User-Agent.")
            if pages_limit_hit:
                await _fetch_missing_assets(job, user_agent=BROWSER_USER_AGENT)
        else:
            await job.queue.put(f"[error] Retry also failed (exit code {rc}).")
            job.status = JobStatus.FAILED
            job.error = f"wget exit code {rc} (after retry)"
    else:
        await job.queue.put(f"[error] wget exited with code {rc}")
        job.status = JobStatus.FAILED
        job.error = f"wget exit code {rc}"

    # Locate the main HTML
    main = _find_main_html(site_dir, job.url)
    job.main_html = main
    await job.queue.put(f"[info] Main HTML file: {main or '(not found)'}")

    size_mb = _dir_size(site_dir) / (1024 * 1024)
    file_count = sum(len(fs) for _, _, fs in os.walk(site_dir))
    await job.queue.put(f"[info] Downloaded {file_count} files ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_job(
    url: str,
    slug_raw: str | None,
    extra_domains_raw: str | None,
    remove_links: bool = False,
    strip_dead_links: bool = False,
    llm_export: bool = False,
    max_pages: int = 1,
    crawl_depth: int = 0,
) -> Job:
    """Validate inputs, set up the output directory, return a Job."""
    url = _validate_url(url)
    slug = sanitise_slug(slug_raw, url)
    ts = time.strftime("%Y%m%d-%H%M%S")
    folder_name = f"{slug}-{ts}"
    output_dir = DOWNLOADS_DIR / folder_name
    site_dir = output_dir / "site"

    extra_domains: list[str] = []
    if extra_domains_raw:
        extra_domains = [
            d.strip() for d in re.split(r"[,\s]+", extra_domains_raw) if d.strip()
        ]

    # Ensure sane minimums
    max_pages = max(1, max_pages)
    crawl_depth = max(0, crawl_depth)

    job = Job(
        job_id=uuid.uuid4().hex[:12],
        url=url,
        slug=slug,
        extra_domains=extra_domains,
        remove_links=remove_links,
        strip_dead_links=strip_dead_links,
        llm_export=llm_export,
        max_pages=max_pages,
        crawl_depth=crawl_depth,
        output_dir=output_dir,
        site_dir=site_dir,
    )
    jobs[job.job_id] = job
    return job


async def run_clone(job: Job) -> None:
    """Run the full clone pipeline: wget → bundle generation."""
    from app.bundle import generate_bundle  # avoid circular import

    parsed = urlparse(job.url)
    await job.queue.put(f"[info] Starting clone of {job.url}")
    await job.queue.put(f"[info] Host: {parsed.hostname}")
    await job.queue.put(f"[info] Slug: {job.slug}")
    await job.queue.put(f"[info] Output: {job.output_dir}")
    await job.queue.put(f"[info] Max pages: {job.max_pages}")
    await job.queue.put(f"[info] Crawl depth: {job.crawl_depth}")
    if job.extra_domains:
        await job.queue.put(f"[info] Extra domains: {', '.join(job.extra_domains)}")

    await _run_wget(job)

    # Post-processing: block search-engine indexing
    if job.status == JobStatus.COMPLETED:
        await job.queue.put("[info] Fixing CSS url(#…) fragment references corrupted by wget…")
        frag_count = _fix_css_fragment_references(job.site_dir)
        await job.queue.put(f"[info] Restored {frag_count} CSS fragment reference(s)")

        await job.queue.put("[info] Converting root-relative URLs to relative paths…")
        rewrite_count = _rewrite_root_relative_urls(job.site_dir)
        await job.queue.put(f"[info] Rewrote {rewrite_count} root-relative URL(s)")

        await job.queue.put("[info] Fixing wget filename mangling (query strings, extensions)…")
        fname_count = _fix_wget_filenames(job.site_dir)
        await job.queue.put(f"[info] Fixed {fname_count} mangled filename reference(s)")

        await job.queue.put("[info] Adding robots.txt (disallow all crawlers)")
        _write_robots_txt(job.site_dir)
        await job.queue.put("[info] Injecting noindex meta tags into HTML files…")
        noindex_count = _inject_noindex(job.site_dir)
        await job.queue.put(f"[info] Added noindex meta tag to {noindex_count} HTML file(s)")

    # Post-processing: strip dead links (multi-page mode)
    if job.strip_dead_links and not job.remove_links and job.status == JobStatus.COMPLETED:
        await job.queue.put("[info] Stripping links to non-downloaded pages…")
        dead_count = _strip_dead_links(job.site_dir)
        await job.queue.put(f"[info] Stripped {dead_count} dead link(s)")

    # Post-processing: strip ALL links if requested (single-page mode)
    if job.remove_links and job.status == JobStatus.COMPLETED:
        await job.queue.put("[info] Removing all links from HTML files…")
        count = _strip_links(job.site_dir)
        await job.queue.put(f"[info] Stripped href from {count} link(s)")

    # Post-processing: clean up any remaining empty href="" attributes
    # (from original site HTML or our stripping) so clicks don't reload the page.
    if job.status == JobStatus.COMPLETED:
        await job.queue.put("[info] Cleaning up empty href attributes…")
        empty_count = _clean_empty_hrefs(job.site_dir)
        await job.queue.put(f"[info] Cleaned {empty_count} empty href(s)")

    # LLM Markdown export (if requested)
    if job.llm_export and job.status == JobStatus.COMPLETED:
        from app.llm_export import generate_llm_export

        await job.queue.put("[info] Generating LLM Markdown export…")
        md_path = job.output_dir / f"{job.slug}-llm.md"
        try:
            generate_llm_export(job.site_dir, md_path, job.url)
            job.llm_export_file = md_path.name
            await job.queue.put(f"[info] LLM export saved: {md_path.name}")
            await job.queue.put("[llm_link]")
        except Exception as exc:
            await job.queue.put(f"[warn] LLM export failed: {exc}")

    if job.status == JobStatus.COMPLETED:
        generate_bundle(job)
        await job.queue.put(f"[info] Bundle generated in {job.output_dir}")
        await job.queue.put(f"[info] To deploy:  cd {job.output_dir} && docker compose up -d --build")

    await job.queue.put("[done]")
