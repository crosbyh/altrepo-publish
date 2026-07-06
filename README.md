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
      - ./ipas:/data:ro
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
| `DATA_DIR` | `/data` | Folder scanned for `.ipa` files (mount read-only) |
| `CACHE_DIR` | `/cache` | Extracted-icon cache (needs write access) |

## Endpoints

| Path | What |
| --- | --- |
| `/` | Web UI for visual confirmation |
| `/apps.json` | The AltStore source (legacy + 2.0 fields) |
| `/ipas/<file>` | IPA downloads |
| `/icons/<file>` | Extracted app icons |
| `/api/status` | JSON status incl. parse errors for bad IPAs |
| `/health` | Liveness probe |

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
