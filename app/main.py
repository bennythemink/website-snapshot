"""
main.py – FastAPI application for the Snapshot web-page cloner.

Routes
------
GET  /                           → Single-page UI
POST /api/clone                  → Start a clone job → returns job_id + SSE URL
GET  /api/clone/{job_id}/events  → SSE stream of progress messages
GET  /preview/{job_id}/{path}    → Serve the downloaded static site for preview
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.cloner import Job, JobStatus, create_job, jobs, run_clone

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(
    title="Snapshot – Web Page Cloner",
    description="Clone a single web page and generate a Docker bundle.",
    version="1.0.0",
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CloneRequest(BaseModel):
    url: str
    slug: str | None = None
    extra_domains: str | None = None


class CloneResponse(BaseModel):
    job_id: str
    sse_url: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the single-page UI."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/clone", response_model=CloneResponse)
async def start_clone(body: CloneRequest):
    """Validate inputs, create a job, kick off the background clone."""
    try:
        job: Job = create_job(body.url, body.slug, body.extra_domains)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Fire-and-forget the clone task
    asyncio.create_task(run_clone(job))

    return CloneResponse(
        job_id=job.job_id,
        sse_url=f"/api/clone/{job.job_id}/events",
    )


@app.get("/api/clone/{job_id}/events")
async def clone_events(job_id: str):
    """Stream clone progress via Server-Sent Events."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_generator() -> AsyncGenerator[dict, None]:
        while True:
            try:
                msg = await asyncio.wait_for(job.queue.get(), timeout=300)
            except asyncio.TimeoutError:
                # Keep-alive / timeout
                yield {"event": "ping", "data": "keep-alive"}
                continue

            if msg == "[done]":
                # Send final summary
                payload = {
                    "status": job.status.value,
                    "output_dir": str(job.output_dir),
                    "slug": job.slug,
                }
                if job.main_html:
                    payload["preview_url"] = f"/preview/{job.job_id}/{job.main_html}"
                if job.error:
                    payload["error"] = job.error

                yield {"event": "done", "data": _json_str(payload)}
                return
            else:
                yield {"event": "log", "data": msg}

    return EventSourceResponse(event_generator())


@app.get("/preview/{job_id}/{path:path}")
async def preview(job_id: str, path: str):
    """Serve a file from the downloaded site folder for in-browser preview."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    file_path = job.site_dir / path
    if not file_path.exists() or not file_path.is_file():
        # Try with index.html appended
        index_path = file_path / "index.html"
        if index_path.exists():
            file_path = index_path
        else:
            raise HTTPException(status_code=404, detail="File not found.")

    # Security: ensure we stay within the site dir
    try:
        file_path.resolve().relative_to(job.site_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden.")

    return FileResponse(file_path)


@app.get("/api/jobs")
async def list_jobs():
    """List recent jobs (lightweight admin helper)."""
    result = []
    for j in jobs.values():
        entry = {
            "job_id": j.job_id,
            "url": j.url,
            "slug": j.slug,
            "status": j.status.value,
            "output_dir": str(j.output_dir),
        }
        if j.main_html:
            entry["preview_url"] = f"/preview/{j.job_id}/{j.main_html}"
        result.append(entry)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import json


def _json_str(obj: dict) -> str:
    return json.dumps(obj)
