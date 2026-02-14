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
    status: JobStatus = JobStatus.PENDING
    output_dir: Path = Path(".")
    site_dir: Path = Path(".")
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    error: Optional[str] = None
    main_html: Optional[str] = None
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
    """Remove href values from all <a> tags in HTML files under site_dir.

    Links remain visible but point nowhere (href="").
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
            new_content, count = link_pattern.subn(r'\1 href=""', content)
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


def _write_robots_txt(site_dir: Path) -> None:
    """Write a robots.txt at the site root that blocks all crawlers."""
    content = "User-agent: *\nDisallow: /\n"
    (site_dir / "robots.txt").write_text(content)


def _inject_noindex(site_dir: Path) -> int:
    """Inject a noindex/nofollow meta tag into every HTML file's <head>.

    Returns the number of files modified.
    """
    meta_tag = '<meta name="robots" content="noindex, nofollow">'
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
                rf"\1\n    {meta_tag}", html, count=1,
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

def _build_wget_args(url: str, site_dir: Path, extra_domains: list[str]) -> list[str]:
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
        "-e", "robots=off",
        "-P", str(site_dir),
    ]

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

async def _run_wget(job: Job) -> None:
    """Spawn wget, stream output lines into job.queue, enforce limits."""
    job.status = JobStatus.RUNNING

    site_dir = job.site_dir
    site_dir.mkdir(parents=True, exist_ok=True)

    args = _build_wget_args(job.url, site_dir, job.extra_domains)
    cmd_display = " ".join(args)
    await job.queue.put(f"[cmd] {cmd_display}")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _stream(stream: asyncio.StreamReader, prefix: str):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                await job.queue.put(f"[{prefix}] {text}")
            # Check size limit
            if _dir_size(site_dir) > MAX_DOWNLOAD_BYTES:
                await job.queue.put("[warn] Download size limit exceeded – killing wget.")
                proc.kill()
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
        proc.kill()
        await job.queue.put("[error] wget timed out after 2 minutes.")
        job.status = JobStatus.FAILED
        job.error = "Timeout"
        await job.queue.put("[done]")
        return

    rc = proc.returncode
    if rc not in (0, 8):
        # rc 8 = "Server issued an error response" but files may still be saved
        await job.queue.put(f"[error] wget exited with code {rc}")
        job.status = JobStatus.FAILED
        job.error = f"wget exit code {rc}"
    else:
        job.status = JobStatus.COMPLETED
        await job.queue.put("[info] wget finished successfully.")

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

    job = Job(
        job_id=uuid.uuid4().hex[:12],
        url=url,
        slug=slug,
        extra_domains=extra_domains,
        remove_links=remove_links,
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
    if job.extra_domains:
        await job.queue.put(f"[info] Extra domains: {', '.join(job.extra_domains)}")

    await _run_wget(job)

    # Post-processing: block search-engine indexing
    if job.status == JobStatus.COMPLETED:
        await job.queue.put("[info] Fixing CSS url(#…) fragment references corrupted by wget…")
        frag_count = _fix_css_fragment_references(job.site_dir)
        await job.queue.put(f"[info] Restored {frag_count} CSS fragment reference(s)")

        await job.queue.put("[info] Adding robots.txt (disallow all crawlers)")
        _write_robots_txt(job.site_dir)
        await job.queue.put("[info] Injecting noindex meta tags into HTML files…")
        noindex_count = _inject_noindex(job.site_dir)
        await job.queue.put(f"[info] Added noindex meta tag to {noindex_count} HTML file(s)")

    # Post-processing: strip links if requested
    if job.remove_links and job.status == JobStatus.COMPLETED:
        await job.queue.put("[info] Removing links from HTML files…")
        count = _strip_links(job.site_dir)
        await job.queue.put(f"[info] Stripped href from {count} link(s)")

    if job.status == JobStatus.COMPLETED:
        generate_bundle(job)
        await job.queue.put(f"[info] Bundle generated in {job.output_dir}")
        await job.queue.put(f"[info] To deploy:  cd {job.output_dir} && docker compose up -d --build")

    await job.queue.put("[done]")
