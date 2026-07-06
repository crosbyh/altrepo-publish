import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .cgbi import solid_png
from .scanner import Library

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
SOURCE_NAME = os.environ.get("SOURCE_NAME", "My IPA Library")
SOURCE_IDENTIFIER = os.environ.get("SOURCE_IDENTIFIER", "local.altrepo.source")
DEVELOPER_NAME = os.environ.get("DEVELOPER_NAME", "Self-hosted")
# Optional override; by default URLs are derived from the incoming request
# (uvicorn runs with --proxy-headers, so X-Forwarded-Proto/Host are honored).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="altrepo-publish", docs_url=None, redoc_url=None)
library = Library(DATA_DIR, CACHE_DIR)
_default_icon = solid_png()


def _base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/apps.json")
def apps_json(request: Request) -> JSONResponse:
    library.refresh()
    return JSONResponse(
        library.source_json(
            _base_url(request), SOURCE_NAME, SOURCE_IDENTIFIER, DEVELOPER_NAME
        )
    )


@app.get("/api/status")
def status(request: Request) -> dict:
    library.refresh()
    groups = library.grouped()
    return {
        "sourceName": SOURCE_NAME,
        "sourceIdentifier": SOURCE_IDENTIFIER,
        "sourceURL": f"{_base_url(request)}/apps.json",
        "appCount": len(groups),
        "ipaCount": sum(len(v) for v in groups.values()),
        "lastScan": library.last_scan.isoformat() if library.last_scan else None,
        "errors": library.errors,
    }


@app.get("/icons/default.png")
def default_icon() -> Response:
    return Response(_default_icon, media_type="image/png")


def _safe_child(directory: Path, name: str) -> Path:
    path = (directory / name).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        raise HTTPException(status_code=404)
    return path


@app.get("/icons/{name}")
def icon(name: str) -> FileResponse:
    return FileResponse(_safe_child(CACHE_DIR, name), media_type="image/png")


@app.get("/ipas/{name}")
def ipa(name: str) -> FileResponse:
    return FileResponse(
        _safe_child(DATA_DIR, name),
        media_type="application/octet-stream",
        filename=name,
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True}
