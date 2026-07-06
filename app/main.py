import logging
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .cgbi import solid_png
from .scanner import Library, extract_ipa

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
        "writable": os.access(DATA_DIR, os.W_OK),
    }


def _ingest_temp(tmp: Path) -> dict:
    """Validate a just-written temp file as an IPA and move it into the
    library under a canonical name. Deletes the temp file on failure."""
    try:
        info = extract_ipa(tmp)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Not a valid IPA: {exc}")
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
    dest = DATA_DIR / f"{safe(info.bundle_id)}-{safe(info.version)}.ipa"
    replaced = dest.exists()
    os.replace(tmp, dest)
    return {
        "name": info.name,
        "bundleIdentifier": info.bundle_id,
        "version": info.version,
        "filename": dest.name,
        "replaced": replaced,
    }


def _new_temp() -> Path:
    """Partial downloads live next to the library (same filesystem, so the
    final move is atomic) with a suffix the scanner ignores."""
    if not os.access(DATA_DIR, os.W_OK):
        raise HTTPException(
            status_code=403, detail="Data directory is read-only (mounted with :ro?)"
        )
    fd, name = tempfile.mkstemp(dir=DATA_DIR, suffix=".part")
    os.close(fd)
    return Path(name)


@app.post("/api/upload")
def upload(file: UploadFile = File(...)) -> dict:
    if not (file.filename or "").lower().endswith(".ipa"):
        raise HTTPException(status_code=400, detail="Only .ipa files are accepted")
    tmp = _new_temp()
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return _ingest_temp(tmp)


class FetchRequest(BaseModel):
    url: str


@app.post("/api/fetch")
def fetch(body: FetchRequest) -> dict:
    if not re.match(r"^https?://", body.url):
        raise HTTPException(status_code=400, detail="URL must be http(s)")
    tmp = _new_temp()
    try:
        req = urllib.request.Request(body.url, headers={"User-Agent": "altrepo-publish"})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out)
    except HTTPException:
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Download failed: {exc}")
    return _ingest_temp(tmp)


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
