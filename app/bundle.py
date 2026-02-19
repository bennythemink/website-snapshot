"""
bundle.py – Generate a deploy-ready Docker bundle for each cloned snapshot.

Writes three files into the job's output_dir:
  • Dockerfile        – nginx:alpine serving the static site
  • docker-compose.yml – container on caddy_net
  • README.md         – quick-start instructions + Caddyfile example
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.cloner import Job


def generate_bundle(job: "Job") -> None:
    """Write Dockerfile, docker-compose.yml, and README.md into job.output_dir."""
    out = job.output_dir
    out.mkdir(parents=True, exist_ok=True)

    _write_dockerfile(out)
    _write_compose(out, job.slug)
    _write_readme(out, job.slug, job.url)


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

def _write_dockerfile(out: Path) -> None:
    content = textwrap.dedent("""\
        FROM nginx:alpine

        # Remove default nginx static content
        RUN rm -rf /usr/share/nginx/html/*

        # Copy the downloaded site into the nginx html root
        COPY ./site/ /usr/share/nginx/html/

        EXPOSE 80
    """)
    (out / "Dockerfile").write_text(content)


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------

def _write_compose(out: Path, slug: str) -> None:
    service_name = slug
    content = textwrap.dedent(f"""\
        services:
          {service_name}:
            build: .
            container_name: {service_name}
            restart: unless-stopped
            expose:
              - "80"
            # Uncomment the next two lines to also publish to the host for local testing:
            # ports:
            #   - "8090:80"
            networks:
              - caddy-net

        networks:
          caddy-net:
            external: true
    """)
    (out / "docker-compose.yml").write_text(content)


# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------

def _write_readme(out: Path, slug: str, url: str) -> None:
    service_name = slug
    content = textwrap.dedent(f"""\
        # Snapshot: {slug}

        Static-site snapshot of **{url}**

        > ⚠️  Only clone pages you have permission to copy.
        > Cloning may violate the source site's Terms of Service.

        ## Quick start

        ```bash
        # 1. Ensure the shared Docker network exists
        docker network create caddy-net || true

        # 2. Build & run
        docker compose up -d --build

        # 3. Verify
        docker ps | grep {service_name}
        ```

        ## Caddy reverse-proxy snippet

        Add a block like this to your **Caddyfile** (on the host running your
        central Caddy container):

        ```
        {slug}.yourdomain.com {{
            reverse_proxy {service_name}:80
        }}
        ```

        Then reload Caddy:

        ```bash
        docker exec -w /etc/caddy caddy caddy reload
        ```

        ## Files

        | Path | Description |
        |------|-------------|
        | `site/` | Downloaded HTML, CSS, JS, and assets |
        | `Dockerfile` | Builds an nginx:alpine image with the site |
        | `docker-compose.yml` | Runs the container on `caddy-net` |

        ## Notes

        - The container exposes port **80** internally (no host port published
          by default).  Caddy reaches it via the `caddy-net` Docker network.
        - To test locally without Caddy, uncomment the `ports:` section in
          `docker-compose.yml`.
    """)
    (out / "README.md").write_text(content)
