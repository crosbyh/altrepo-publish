# altrepo-publish

Self-hosted [AltStore source](https://faq.altstore.io/developers/make-a-source)
generator and server. Drop `.ipa` files in a folder; get a browsable web UI
and an `apps.json` source you can add to [Feather](https://github.com/khcrysalis/Feather),
AltStore, or SideStore.

- **Zero per-app config** — name, bundle ID, version, minimum iOS version,
  and icon are read from each IPA's embedded `Info.plist`.
- **Icons that actually render** — Apple's CgBI-optimized PNGs are converted
  to standard PNGs on extraction.
- **Multiple versions per app** — IPAs sharing a bundle ID are grouped into
  the source's `versions` array, newest first.
- **Lazy rescans** — the folder is re-fingerprinted on each request; adding
  or replacing an IPA is picked up immediately, no restarts or watchers.
- **Add IPAs from the web UI** — upload a file (button or drag-and-drop
  anywhere on the page) or paste a direct download URL; the server validates
  the archive and files it as `<bundle-id>-<version>.ipa`. Mount `/data`
  with `:ro` to disable all mutating features.
- **GitHub release tracking** — register `owner/repo` entries in the UI and
  the server polls their latest releases, auto-downloading new `.ipa`
  assets. Optional per-tracker regex narrows which assets match.
- **Manage from the UI** — QR code for adding the source to your phone,
  per-version download/delete, delete-whole-app, expandable version history.
- **Auto-prune** — set `KEEP_VERSIONS=N` to keep only the newest N versions
  per app.
- **Metadata overrides** — optional `overrides.json` polishes names,
  descriptions, and tint colors without touching the IPAs.
- Designed for private networks (Tailscale, LAN). No auth is built in — put
  it behind your reverse proxy and keep it off the public internet.

## Quick start

```yaml
services:
  altrepo:
    image: ghcr.io/crosbyh/altrepo-publish:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./ipas:/data
      - altrepo-cache:/cache
    environment:
      SOURCE_NAME: "My Apps"
      SOURCE_IDENTIFIER: "com.example.myapps"
      DEVELOPER_NAME: "Me"

volumes:
  altrepo-cache:
```

Put `.ipa` files in `./ipas`, then open `http://host:8080/` to confirm the
apps look right. Add `http://host:8080/apps.json` as a source in Feather.

> **TLS matters:** iOS refuses downloads from endpoints with untrusted
> certificates. Serve this behind a reverse proxy with a real certificate
> (e.g. Traefik + Let's Encrypt) and add the source via that HTTPS URL.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SOURCE_NAME` | `My IPA Library` | Source name shown in Feather |
| `SOURCE_IDENTIFIER` | `local.altrepo.source` | Stable reverse-DNS ID for the source — don't change it after adding the source to devices |
| `DEVELOPER_NAME` | `Self-hosted` | Shown as each app's developer |
| `PUBLIC_URL` | *(derived from request)* | Force the base URL used in `apps.json`. Normally unnecessary: the server honors `X-Forwarded-Proto`/`X-Forwarded-Host` from your proxy |
| `DATA_DIR` | `/data` | Folder scanned for `.ipa` files. With a host bind mount, the folder must be writable by uid 1000 for web-UI adds/trackers to work (or add `:ro` to disable them) |
| `CACHE_DIR` | `/cache` | Extracted-icon cache (needs write access) |
| `KEEP_VERSIONS` | `0` | Keep only the newest N versions per app, deleting older IPAs on scan. `0` keeps everything |
| `TRACKER_INTERVAL_HOURS` | `6` | How often to poll tracked GitHub repos for new releases. `0` disables background polling ("Check now" still works) |
| `GITHUB_TOKEN` | *(unset)* | Optional token for tracker API calls — higher rate limits and private repos |

## Endpoints

| Path | What |
| --- | --- |
| `/` | Web UI for visual confirmation |
| `/apps.json` | The AltStore source (legacy + 2.0 fields) |
| `/ipas/<file>` | IPA downloads |
| `/icons/<file>` | Extracted app icons |
| `/qr.svg` | QR code of the source URL |
| `/api/upload` | `POST` multipart `file` — add an IPA |
| `/api/fetch` | `POST` `{"url": "https://…/app.ipa"}` — server-side download |
| `/api/ipas/<file>` | `DELETE` — remove an IPA |
| `/api/trackers` | `GET` list / `POST` `{"repo": "owner/name", "pattern": null}` / `DELETE /api/trackers/<owner>/<name>` |
| `/api/trackers/check` | `POST` — poll all tracked repos now |
| `/api/status` | JSON status incl. parse errors for bad IPAs |
| `/health` | Liveness probe |

## Metadata overrides

Create `overrides.json` next to your IPAs to polish how apps appear in
Feather. Keys are bundle identifiers; the special `_source` key overrides
source-level fields. Changes are picked up on the next request — no restart.

```json
{
  "_source": { "name": "Crosby's Apps", "subtitle": "Personal sideload library" },
  "com.utmapp.UTM": {
    "name": "UTM",
    "subtitle": "Virtual machines on iOS",
    "developerName": "UTM Team",
    "localizedDescription": "Run VMs on your iPhone or iPad.",
    "tintColor": "#4a90d9"
  }
}
```

Supported per-app keys: `name`, `subtitle`, `developerName`,
`localizedDescription`, `iconURL`, `tintColor`. Source keys: `name`,
`subtitle`, `iconURL`, `website`.

## GitHub release tracking

Add `owner/repo` in the UI (state lives in `trackers.json` next to your
IPAs). On each poll the server fetches the latest non-prerelease release
and ingests any `.ipa` assets it hasn't seen. The optional regex narrows
asset filenames when a release ships several IPAs. Unauthenticated GitHub
API limits (60 req/h) are plenty at the default 6-hour interval; set
`GITHUB_TOKEN` if you track many repos or private ones.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
DATA_DIR=./ipas CACHE_DIR=/tmp/altrepo-cache uvicorn app.main:app --reload
```

## Releases

Every push to `main` runs the tests and publishes
`ghcr.io/crosbyh/altrepo-publish:latest` (linux/amd64 + linux/arm64).
Pushing a `v*` tag additionally publishes a semver tag.
