"""Track upstream GitHub repos and auto-download IPA release assets.

Config and state live together in DATA_DIR/trackers.json:
  {"trackers": [{"repo": "owner/name", "pattern": null, "prerelease": false,
                 "lastRelease": "v1.2"}]}

`pattern` optionally narrows asset filenames; by default every `.ipa`
asset of the newest non-draft, non-prerelease release is ingested.
`prerelease` opts a tracker into pre-releases, for repos (e.g.
OatmealDome/dolphin-ios) that never publish a full release.
"""

import json
import logging
import os
import re
import shutil
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from .scanner import ingest_temp, new_temp

log = logging.getLogger("altrepo")


class TrackerStore:
    def __init__(
        self,
        data_dir: Path,
        api_base: str = "https://api.github.com",
        token: Optional[str] = None,
    ):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "trackers.json"
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def load(self) -> list:
        try:
            return json.loads(self.path.read_text()).get("trackers", [])
        except FileNotFoundError:
            return []
        except Exception as exc:
            log.warning("trackers.json unreadable: %s", exc)
            self.errors["trackers.json"] = str(exc)
            return []

    def _save(self, trackers: list) -> None:
        if not os.access(self.data_dir, os.W_OK):
            raise PermissionError("data directory is read-only")
        self.path.write_text(json.dumps({"trackers": trackers}, indent=2) + "\n")

    def add(
        self, repo: str, pattern: Optional[str] = None, prerelease: bool = False
    ) -> None:
        with self._lock:
            trackers = self.load()
            if any(t["repo"].lower() == repo.lower() for t in trackers):
                raise ValueError(f"{repo} is already tracked")
            trackers.append(
                {
                    "repo": repo,
                    "pattern": pattern,
                    "prerelease": prerelease,
                    "lastRelease": None,
                }
            )
            self._save(trackers)

    def remove(self, repo: str) -> bool:
        with self._lock:
            trackers = self.load()
            kept = [t for t in trackers if t["repo"].lower() != repo.lower()]
            if len(kept) == len(trackers):
                return False
            self._save(kept)
            self.errors.pop(repo, None)
            return True

    def _request(self, url: str) -> urllib.request.Request:
        headers = {
            "User-Agent": "altrepo-publish",
            "Accept": "application/vnd.github+json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def _download_asset(self, url: str) -> dict:
        tmp = new_temp(self.data_dir)
        try:
            with urllib.request.urlopen(self._request(url), timeout=600) as resp, \
                    tmp.open("wb") as out:
                shutil.copyfileobj(resp, out)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return ingest_temp(tmp, self.data_dir)

    def _check_one(self, tracker: dict) -> dict:
        repo = tracker["repo"]
        with urllib.request.urlopen(
            self._request(f"{self.api_base}/repos/{repo}/releases?per_page=30"),
            timeout=30,
        ) as resp:
            releases = json.loads(resp.read())
        release = next(
            (
                r
                for r in releases
                if not r.get("draft")
                and (tracker.get("prerelease") or not r.get("prerelease"))
            ),
            None,
        )
        if release is None:
            return {"repo": repo, "release": None, "status": "no-release", "added": []}
        tag = release.get("tag_name") or ""
        if tag == tracker.get("lastRelease"):
            return {"repo": repo, "release": tag, "status": "up-to-date", "added": []}

        assets = [
            a
            for a in release.get("assets", [])
            if a.get("name", "").lower().endswith(".ipa")
        ]
        if tracker.get("pattern"):
            rx = re.compile(tracker["pattern"])
            assets = [a for a in assets if rx.search(a["name"])]
        added = [self._download_asset(a["browser_download_url"]) for a in assets]
        tracker["lastRelease"] = tag
        log.info("tracker %s: release %s, %d IPA(s) added", repo, tag, len(added))
        return {
            "repo": repo,
            "release": tag,
            "status": "updated" if added else "no-ipa-assets",
            "added": added,
        }

    def check_all(self) -> list:
        with self._lock:
            trackers = self.load()
            results = []
            for tracker in trackers:
                try:
                    result = self._check_one(tracker)
                    self.errors.pop(tracker["repo"], None)
                except Exception as exc:
                    log.warning("tracker %s failed: %s", tracker["repo"], exc)
                    self.errors[tracker["repo"]] = str(exc)
                    result = {
                        "repo": tracker["repo"],
                        "status": "error",
                        "error": str(exc),
                    }
                results.append(result)
            try:
                self._save(trackers)
            except PermissionError:
                pass  # read-only mount: lastRelease state just doesn't persist
            return results
