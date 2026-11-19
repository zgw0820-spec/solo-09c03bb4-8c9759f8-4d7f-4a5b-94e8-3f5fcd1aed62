"""FastAPI surface for the C build cache service."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response

from .builder import (
    CFLAGS,
    BuildError,
    ObjectCache,
    build_project,
    validate_request,
)
from .models import BuildRequest, ErrorResponse

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "./data/cache"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "./data/work"))

cache = ObjectCache(CACHE_DIR)

app = FastAPI(
    title="C Build Cache Service",
    version="1.0.0",
    description=(
        "Accepts a small C project as JSON (sources and headers by relative "
        "path), compiles it with fixed GCC flags "
        f"(`{' '.join(CFLAGS)}`, project root on the include path), reuses "
        "object files whose source and transitively included project headers "
        "are unchanged, links only this submission's objects, and returns "
        "the executable plus reuse counters."
    ),
)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok"}


@app.get("/stats", tags=["meta"])
def stats():
    """Global cache counters (useful to observe singleflight behaviour)."""
    s = cache.stats
    with s.lock:
        return {
            "cached_objects": cache.cached_objects(),
            "requests_total": s.requests_total,
            "failed_builds_total": s.failed_builds_total,
            "compiled_total": s.compiled_total,
            "reused_total": s.reused_total,
            "singleflight_waits_total": s.singleflight_waits_total,
        }


@app.post(
    "/build",
    tags=["build"],
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
            "description": (
                "The linked executable. Reuse counters are returned in the "
                "`X-Compiled-Count`, `X-Reused-Count` and `X-Unit-Count` headers."
            ),
            "headers": {
                "X-Compiled-Count": {"schema": {"type": "integer"}, "description": "Objects compiled by this request"},
                "X-Reused-Count": {"schema": {"type": "integer"}, "description": "Objects reused from the cache (including singleflight waits)"},
                "X-Unit-Count": {"schema": {"type": "integer"}, "description": "Total translation units in this submission"},
            },
        },
        400: {"model": ErrorResponse, "description": "Validation, compile or link failure; GCC diagnostics included"},
        422: {"description": "Request body does not match the schema"},
    },
)
def build(req: BuildRequest) -> Response:
    with cache.stats.lock:
        cache.stats.requests_total += 1
    try:
        files = validate_request(req.files)
        result = build_project(files, cache, WORK_DIR)
    except BuildError as exc:
        with cache.stats.lock:
            cache.stats.failed_builds_total += 1
        raise HTTPException(
            status_code=400,
            detail={
                "stage": exc.stage,
                "message": str(exc),
                "unit": exc.unit,
                "diagnostics": exc.diagnostics,
            },
        )
    return Response(
        content=result.executable,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{req.output}"',
            "X-Compiled-Count": str(result.compiled),
            "X-Reused-Count": str(result.reused),
            "X-Unit-Count": str(result.units),
        },
    )
