import asyncio
import io
import logging
import os
import re
import shutil
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import segno
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .cgbi import solid_png
from .scanner import Library, ingest_temp, new_temp
from .tracker import TrackerStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("altrepo")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
SOURCE_NAME = os.environ.get("SOURCE_NAME", "My IPA Library")
SOURCE_IDENTIFIER = os.environ.get("SOURCE_IDENTIFIER", "local.altrepo.source")
DEVELOPER_NAME = os.environ.get("DEVELOPER_NAME", "Self-hosted")
# Optional override; by default URLs are derived from the incoming request
# (uvicorn runs with --proxy-headers, so X-Forwarded-Proto/Host are honored).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
KEEP_VERSIONS = int(os.environ.get("KEEP_VERSIONS", "0"))
TRACKER_INTERVAL_HOURS = float(os.environ.get("TRACKER_INTERVAL_HOURS", "6"))

STATIC_DIR = Path(__file__).parent / "static"

library = Library(DATA_DIR, CACHE_DIR, keep_versions=KEEP_VERSIONS)
trackers = TrackerStore(
    DATA_DIR,
    api_base=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    token=os.environ.get("GITHUB_TOKEN"),
)
_default_icon = solid_png()


async def _tracker_poll_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(trackers.check_all)
        except Exception:
            log.exception("tracker poll failed")
        await asyncio.sleep(TRACKER_INTERVAL_HOURS * 3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = (
        asyncio.create_task(_tracker_poll_loop())
        if TRACKER_INTERVAL_HOURS > 0
        else None
    )
    yield
    if task:
        task.cancel()


app = FastAPI(title="altrepo-publish", docs_url=None, redoc_url=None, lifespan=lifespan)


def _base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def _require_writable() -> None:
    if not os.access(DATA_DIR, os.W_OK):
        raise HTTPException(
            status_code=403, detail="Data directory is read-only (mounted with :ro?)"
        )


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
        "keepVersions": KEEP_VERSIONS,
    }


@app.get("/qr.svg")
def qr_svg(request: Request) -> Response:
    buf = io.BytesIO()
    segno.make(f"{_base_url(request)}/apps.json", error="m").save(
        buf, kind="svg", scale=5, border=2, dark="#000000", light="#ffffff"
    )
    return Response(buf.getvalue(), media_type="image/svg+xml")


def _ingest(tmp: Path) -> dict:
    try:
        return ingest_temp(tmp, DATA_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Not a valid IPA: {exc}")


def _new_temp() -> Path:
    try:
        return new_temp(DATA_DIR)
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="Data directory is read-only (mounted with :ro?)"
        )


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
    return _ingest(tmp)


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
    return _ingest(tmp)


def _safe_child(directory: Path, name: str) -> Path:
    path = (directory / name).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        raise HTTPException(status_code=404)
    return path


@app.get("/icons/default.png")
def default_icon() -> Response:
    return Response(_default_icon, media_type="image/png")


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


@app.delete("/api/ipas/{name}")
def delete_ipa(name: str) -> dict:
    _require_writable()
    if not name.lower().endswith(".ipa"):
        raise HTTPException(status_code=404)
    _safe_child(DATA_DIR, name).unlink()
    return {"ok": True, "deleted": name}


class TrackerRequest(BaseModel):
    repo: str
    pattern: Optional[str] = None


@app.get("/api/trackers")
def list_trackers() -> dict:
    return {"trackers": trackers.load(), "errors": trackers.errors}


@app.post("/api/trackers")
def add_tracker(body: TrackerRequest) -> dict:
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", body.repo):
        raise HTTPException(status_code=400, detail="Repo must look like owner/name")
    if body.pattern:
        try:
            re.compile(body.pattern)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Bad pattern: {exc}")
    _require_writable()
    try:
        trackers.add(body.repo, body.pattern)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.delete("/api/trackers/{owner}/{name}")
def remove_tracker(owner: str, name: str) -> dict:
    _require_writable()
    if not trackers.remove(f"{owner}/{name}"):
        raise HTTPException(status_code=404)
    return {"ok": True}


@app.post("/api/trackers/check")
def check_trackers() -> dict:
    _require_writable()
    return {"results": trackers.check_all()}


@app.get("/health")
def health() -> dict:
    return {"ok": True}
