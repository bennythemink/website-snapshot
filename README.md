# Snapshot – Web Page Cloner

A self-contained tool that clones a single web page (HTML + CSS + JS + images)
and generates a deploy-ready Docker bundle you can run behind Caddy.

## Features

- **Web UI** – paste a URL, click clone, watch live progress.
- **wget-based** – uses `--page-requisites --convert-links` for accurate offline copies.
- **Docker bundle per snapshot** – each download gets its own `Dockerfile` +
  `docker-compose.yml` using `nginx:alpine`, pre-configured to join `caddy_net`.
- **Instant preview** – browse the cloned page right from the app before deploying.

## Prerequisites

| Bare metal                          | Docker                  |
| ----------------------------------- | ----------------------- |
| Python ≥ 3.12                       | Docker + Docker Compose |
| [uv](https://docs.astral.sh/uv/)    | —                       |
| wget (`brew install wget` on macOS) | — (included in image)   |

## Quick start – bare metal

```bash
# 1. Install dependencies
uv sync

# 2. Start the app
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# 3. Open
open http://localhost:8080
```

## Quick start – Docker

```bash
# 1. Create the shared network (if it doesn't exist)
docker network create caddy_net || true

# 2. Build & run
docker compose up -d --build

# 3. Open
open http://localhost:8080
```

## How it works

1. Enter a URL in the web UI and click **Clone & Generate Bundle**.
2. The app runs `wget` with safe flags to download the page and its assets.
3. Progress streams to your browser in real time via Server-Sent Events.
4. On completion, the app writes a deploy-ready bundle under `downloads/<slug>-<timestamp>/`:
   ```
   downloads/my-page-20260213-143000/
   ├── site/              ← downloaded HTML/CSS/JS/images
   ├── Dockerfile         ← nginx:alpine serving /site
   ├── docker-compose.yml ← caddy_net external network
   └── README.md          ← deploy instructions + Caddyfile snippet
   ```
5. Click the **preview** link to browse the snapshot locally.
6. Deploy the bundle:
   ```bash
   cd downloads/my-page-20260213-143000
   docker compose up -d --build
   ```
7. Point your Caddyfile at the new container:
   ```
   my-page.yourdomain.com {
       reverse_proxy snapshot_my-page:80
   }
   ```

## Project structure

```
snapshot/
├── app/
│   ├── __init__.py
│   ├── main.py          ← FastAPI routes (UI, API, SSE, preview)
│   ├── cloner.py        ← wget orchestration, job management
│   ├── bundle.py        ← Dockerfile/compose/README generation
│   └── templates/
│       └── index.html   ← Single-page web UI
├── downloads/           ← Generated snapshots (git-ignored)
├── pyproject.toml       ← Project config + dependencies (uv)
├── uv.lock              ← Deterministic lockfile
├── Dockerfile           ← Container for the cloner app itself
├── docker-compose.yml   ← Compose for the cloner app
├── .dockerignore
└── README.md
```

## Configuration

| Variable | Default | Description                                  |
| -------- | ------- | -------------------------------------------- |
| Port     | `8080`  | Change via `--port` flag or compose `ports:` |
| Timeout  | 120 s   | Max wget runtime per job                     |
| Max size | 200 MB  | Kills wget if download exceeds this          |

## Safety

- `wget` is spawned with `asyncio.create_subprocess_exec` (no `shell=True`).
- Slugs are sanitised to `[a-z0-9-]` only.
- Downloaded files are served via path-checked `FileResponse` (no directory traversal).
- ⚠️ **Only clone pages you have rights to copy.** Cloning may violate the
  source site's Terms of Service.
