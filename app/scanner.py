"""Scan a directory of .ipa files and build an AltStore source from them.

Metadata comes straight from each IPA's embedded Info.plist; icons are
extracted, CgBI-normalized, and cached on disk. Scans are lazy: callers
invoke refresh(), which fingerprints the directory listing and only
re-extracts files that appeared or changed.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import zipfile
import plistlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cgbi import normalize_png

log = logging.getLogger("altrepo")

_INFO_PLIST_RE = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


@dataclass
class IPAInfo:
    filename: str
    size: int
    mtime: float
    name: str
    bundle_id: str
    version: str
    build_version: str
    min_os_version: str
    icon_name: Optional[str]  # filename inside the icon cache dir, or None

    @property
    def date_iso(self) -> str:
        return datetime.fromtimestamp(self.mtime, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )


def _version_sort_key(info: IPAInfo):
    parts = [int(p) for p in re.findall(r"\d+", info.version)[:6]]
    return (parts + [0] * 6)[:6], info.mtime


def _pick_info_plist(zf: zipfile.ZipFile) -> Optional[str]:
    candidates = [n for n in zf.namelist() if _INFO_PLIST_RE.match(n)]
    # The main app bundle has the shortest path (nested Watch/PlugIn
    # bundles live deeper and are excluded by the regex anyway).
    return min(candidates, key=len) if candidates else None


def _icon_candidates(plist: dict) -> list:
    names = []
    for icons_key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        primary = plist.get(icons_key, {}).get("CFBundlePrimaryIcon", {})
        if isinstance(primary, dict):
            names.extend(primary.get("CFBundleIconFiles", []))
        elif isinstance(primary, str):
            names.append(primary)
    names.extend(plist.get("CFBundleIconFiles", []))
    if isinstance(plist.get("CFBundleIconFile"), str):
        names.append(plist["CFBundleIconFile"])
    return names


def _extract_icon(zf: zipfile.ZipFile, app_dir: str, plist: dict) -> Optional[bytes]:
    entries = {
        i.filename: i
        for i in zf.infolist()
        if i.filename.startswith(app_dir)
        and i.filename.count("/") == app_dir.count("/")
        and i.filename.lower().endswith(".png")
    }
    matches = []
    for base in _icon_candidates(plist):
        base = app_dir + base
        matches.extend(i for n, i in entries.items() if n.startswith(base))
    if not matches:
        matches = [
            i
            for n, i in entries.items()
            if "appicon" in n.rsplit("/", 1)[-1].lower()
            or n.rsplit("/", 1)[-1].lower().startswith("icon")
        ]
    if not matches:
        return None
    best = max(matches, key=lambda i: i.file_size)
    return zf.read(best.filename)


def extract_ipa(path: Path) -> IPAInfo:
    """Read one IPA's metadata and raw icon bytes. Raises on unreadable
    or plist-less archives."""
    stat = path.stat()
    with zipfile.ZipFile(path) as zf:
        plist_name = _pick_info_plist(zf)
        if plist_name is None:
            raise ValueError("no Payload/*.app/Info.plist found")
        plist = plistlib.loads(zf.read(plist_name))
        app_dir = plist_name.rsplit("/", 1)[0] + "/"
        icon_bytes = _extract_icon(zf, app_dir, plist)

    info = IPAInfo(
        filename=path.name,
        size=stat.st_size,
        mtime=stat.st_mtime,
        name=plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or path.stem,
        bundle_id=plist.get("CFBundleIdentifier", f"unknown.{path.stem}"),
        version=plist.get("CFBundleShortVersionString")
        or plist.get("CFBundleVersion")
        or "0",
        build_version=plist.get("CFBundleVersion", ""),
        min_os_version=plist.get("MinimumOSVersion", ""),
        icon_name=None,
    )
    info._raw_icon = icon_bytes  # consumed by Library, not part of the dataclass
    return info


def new_temp(data_dir: Path) -> Path:
    """Temp file inside the library dir (same filesystem, so the final move
    is atomic) with a suffix the scanner ignores. Raises PermissionError
    when the library is mounted read-only."""
    if not os.access(data_dir, os.W_OK):
        raise PermissionError("data directory is read-only")
    fd, name = tempfile.mkstemp(dir=data_dir, suffix=".part")
    os.close(fd)
    return Path(name)


def ingest_temp(tmp: Path, data_dir: Path) -> dict:
    """Validate a just-written temp file as an IPA and move it into the
    library under a canonical name. Deletes the temp file on failure and
    raises ValueError."""
    try:
        info = extract_ipa(tmp)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ValueError(str(exc)) from exc
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
    dest = Path(data_dir) / f"{safe(info.bundle_id)}-{safe(info.version)}.ipa"
    replaced = dest.exists()
    os.replace(tmp, dest)
    return {
        "name": info.name,
        "bundleIdentifier": info.bundle_id,
        "version": info.version,
        "filename": dest.name,
        "replaced": replaced,
    }


class Library:
    def __init__(self, data_dir: Path, cache_dir: Path, keep_versions: int = 0):
        self.data_dir = Path(data_dir)
        self.cache_dir = Path(cache_dir)
        self.keep_versions = keep_versions
        self._lock = threading.Lock()
        self._cache: dict[tuple, IPAInfo] = {}
        self._errors: dict[str, str] = {}
        self.overrides: dict = {}
        self._overrides_fp = None
        self.last_scan: Optional[datetime] = None

    def _fingerprints(self) -> dict[tuple, Path]:
        out = {}
        if not self.data_dir.is_dir():
            return out
        for p in sorted(self.data_dir.iterdir()):
            if p.suffix.lower() == ".ipa" and p.is_file():
                s = p.stat()
                out[(p.name, s.st_size, s.st_mtime)] = p
        return out

    def _store_icon(self, info: IPAInfo, key: tuple) -> None:
        raw = getattr(info, "_raw_icon", None)
        if not raw:
            return
        try:
            png = normalize_png(raw)
        except Exception:
            log.warning("icon normalization failed for %s, using as-is", info.filename)
            png = raw
        digest = hashlib.sha256(repr(key).encode()).hexdigest()[:16]
        icon_name = f"{digest}.png"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / icon_name).write_bytes(png)
        info.icon_name = icon_name

    def refresh(self) -> None:
        with self._lock:
            current = self._fingerprints()
            for key in list(self._cache):
                if key not in current:
                    del self._cache[key]
            self._errors = {
                name: err
                for name, err in self._errors.items()
                if not name.endswith(".ipa") or any(k[0] == name for k in current)
            }
            for key, path in current.items():
                if key in self._cache:
                    continue
                try:
                    info = extract_ipa(path)
                    self._store_icon(info, key)
                    self._cache[key] = info
                    self._errors.pop(path.name, None)
                except Exception as exc:
                    log.warning("skipping %s: %s", path.name, exc)
                    self._errors[path.name] = str(exc)
            self._load_overrides()
            self._prune()
            self._gc_icons()
            self.last_scan = datetime.now(tz=timezone.utc)

    def _load_overrides(self) -> None:
        path = self.data_dir / "overrides.json"
        try:
            s = path.stat()
            fp = (s.st_size, s.st_mtime)
        except FileNotFoundError:
            fp = None
        if fp == self._overrides_fp:
            return
        self._overrides_fp = fp
        self.overrides = {}
        self._errors.pop("overrides.json", None)
        if fp is None:
            return
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
            self.overrides = data
        except Exception as exc:
            log.warning("overrides.json ignored: %s", exc)
            self._errors["overrides.json"] = str(exc)

    def _prune(self) -> None:
        if self.keep_versions <= 0 or not os.access(self.data_dir, os.W_OK):
            return
        groups: dict[str, list[tuple]] = {}
        for key, info in self._cache.items():
            groups.setdefault(info.bundle_id, []).append((key, info))
        for entries in groups.values():
            entries.sort(key=lambda kv: _version_sort_key(kv[1]), reverse=True)
            for key, info in entries[self.keep_versions :]:
                try:
                    (self.data_dir / info.filename).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("prune failed for %s: %s", info.filename, exc)
                    continue
                del self._cache[key]
                log.info(
                    "pruned %s (keeping newest %d)", info.filename, self.keep_versions
                )

    def _gc_icons(self) -> None:
        if not self.cache_dir.is_dir():
            return
        referenced = {i.icon_name for i in self._cache.values() if i.icon_name}
        for png in self.cache_dir.glob("*.png"):
            if png.name not in referenced:
                png.unlink(missing_ok=True)

    @property
    def errors(self) -> dict:
        return dict(self._errors)

    def grouped(self) -> dict[str, list[IPAInfo]]:
        """bundle_id -> versions, newest first."""
        groups: dict[str, list[IPAInfo]] = {}
        for info in self._cache.values():
            groups.setdefault(info.bundle_id, []).append(info)
        for versions in groups.values():
            versions.sort(key=_version_sort_key, reverse=True)
        return dict(
            sorted(groups.items(), key=lambda kv: kv[1][0].name.lower())
        )

    _APP_OVERRIDE_KEYS = (
        "name",
        "subtitle",
        "developerName",
        "localizedDescription",
        "iconURL",
        "tintColor",
    )
    _SOURCE_OVERRIDE_KEYS = ("name", "subtitle", "iconURL", "website")

    def source_json(
        self,
        base_url: str,
        source_name: str,
        source_identifier: str,
        developer_name: str,
    ) -> dict:
        base = base_url.rstrip("/")
        apps = []
        for bundle_id, versions in self.grouped().items():
            latest = versions[0]
            icon_url = (
                f"{base}/icons/{latest.icon_name}"
                if latest.icon_name
                else f"{base}/icons/default.png"
            )
            version_entries = []
            for v in versions:
                entry = {
                    "version": v.version,
                    "date": v.date_iso,
                    "size": v.size,
                    "downloadURL": f"{base}/ipas/{v.filename}",
                    "localizedDescription": f"Version {v.version}",
                }
                if v.build_version:
                    entry["buildVersion"] = v.build_version
                if v.min_os_version:
                    entry["minOSVersion"] = v.min_os_version
                version_entries.append(entry)
            entry = {
                "name": latest.name,
                "bundleIdentifier": bundle_id,
                "developerName": developer_name,
                "localizedDescription": f"{latest.name} ({bundle_id})",
                "iconURL": icon_url,
                "versions": version_entries,
                # Legacy single-version fields for older source parsers.
                "version": latest.version,
                "versionDate": latest.date_iso,
                "size": latest.size,
                "downloadURL": f"{base}/ipas/{latest.filename}",
                "appPermissions": {"entitlements": [], "privacy": {}},
            }
            override = self.overrides.get(bundle_id)
            if isinstance(override, dict):
                for k in self._APP_OVERRIDE_KEYS:
                    if k in override:
                        entry[k] = override[k]
                entry["_overridden"] = True
            apps.append(entry)
        source = {
            "name": source_name,
            "identifier": source_identifier,
            "apps": apps,
            "news": [],
        }
        source_override = self.overrides.get("_source")
        if isinstance(source_override, dict):
            for k in self._SOURCE_OVERRIDE_KEYS:
                if k in source_override:
                    source[k] = source_override[k]
        return source
