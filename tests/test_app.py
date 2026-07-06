import plistlib
import struct
import zipfile
import zlib

import pytest

from app.cgbi import PNG_SIGNATURE, normalize_png, solid_png
from app.scanner import Library, extract_ipa


def make_cgbi_png(width=4, height=4, bgra=(0, 0, 255, 255)) -> bytes:
    """Build a minimal Apple-optimized PNG: CgBI chunk, headerless IDAT,
    BGRA pixel order. bgra defaults to red stored as BGRA."""

    def chunk(ctype, data):
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = (b"\x00" + bytes(bgra) * width) * height
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw deflate
    idat = compressor.compress(raw) + compressor.flush()
    return (
        PNG_SIGNATURE
        + chunk(b"CgBI", b"\x50\x00\x20\x02")
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def make_ipa(path, bundle_id="com.example.demo", version="1.0", name="Demo",
             icon: bytes | None = None):
    plist = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": "42",
        "CFBundleDisplayName": name,
        "MinimumOSVersion": "15.0",
        "CFBundleIcons": {
            "CFBundlePrimaryIcon": {"CFBundleIconFiles": ["AppIcon60x60"]}
        },
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Demo.app/Info.plist", plistlib.dumps(plist))
        if icon is not None:
            zf.writestr("Payload/Demo.app/AppIcon60x60@2x.png", icon)
        zf.writestr("Payload/Demo.app/Watch/W.app/Info.plist", b"not-the-one")
    return path


def test_normalize_cgbi_roundtrip():
    normalized = normalize_png(make_cgbi_png())
    # No CgBI chunk left, and pixels came back as RGBA red.
    assert b"CgBI" not in normalized
    assert normalized.startswith(PNG_SIGNATURE)
    idat_pos = normalized.index(b"IDAT") + 4
    length = struct.unpack(">I", normalized[idat_pos - 8 : idat_pos - 4])[0]
    raw = zlib.decompress(normalized[idat_pos : idat_pos + length])
    assert raw[1:5] == bytes((255, 0, 0, 255))


def test_normalize_passthrough():
    plain = solid_png(8)
    assert normalize_png(plain) == plain
    assert normalize_png(b"not a png") == b"not a png"


def test_extract_ipa(tmp_path):
    ipa = make_ipa(tmp_path / "demo.ipa", icon=make_cgbi_png())
    info = extract_ipa(ipa)
    assert info.bundle_id == "com.example.demo"
    assert info.version == "1.0"
    assert info.build_version == "42"
    assert info.name == "Demo"
    assert info.min_os_version == "15.0"
    assert info._raw_icon is not None


def test_extract_rejects_plistless(tmp_path):
    bad = tmp_path / "bad.ipa"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "nope")
    with pytest.raises(ValueError):
        extract_ipa(bad)


def test_library_source_json(tmp_path):
    data, cache = tmp_path / "data", tmp_path / "cache"
    data.mkdir()
    make_ipa(data / "demo-1.0.ipa", version="1.0", icon=make_cgbi_png())
    make_ipa(data / "demo-2.0.ipa", version="2.0", icon=make_cgbi_png())
    make_ipa(data / "other.ipa", bundle_id="com.example.other", name="Other")

    lib = Library(data, cache)
    lib.refresh()
    source = lib.source_json(
        "https://apps.example.net/", "Test Source", "test.source", "Crosby"
    )

    assert source["name"] == "Test Source"
    assert len(source["apps"]) == 2
    demo = next(a for a in source["apps"] if a["bundleIdentifier"] == "com.example.demo")
    assert demo["version"] == "2.0"  # newest wins the legacy fields
    assert [v["version"] for v in demo["versions"]] == ["2.0", "1.0"]
    assert demo["downloadURL"] == "https://apps.example.net/ipas/demo-2.0.ipa"
    assert demo["iconURL"].startswith("https://apps.example.net/icons/")
    # icon was extracted, normalized, and cached
    icon_file = cache / demo["iconURL"].rsplit("/", 1)[-1]
    assert icon_file.is_file()
    assert b"CgBI" not in icon_file.read_bytes()
    # app without an icon falls back to the default
    other = next(a for a in source["apps"] if a["bundleIdentifier"] == "com.example.other")
    assert other["iconURL"].endswith("/icons/default.png")


def test_library_tracks_changes(tmp_path):
    data, cache = tmp_path / "data", tmp_path / "cache"
    data.mkdir()
    lib = Library(data, cache)
    lib.refresh()
    assert lib.grouped() == {}

    make_ipa(data / "demo.ipa")
    lib.refresh()
    assert "com.example.demo" in lib.grouped()

    (data / "demo.ipa").unlink()
    lib.refresh()
    assert lib.grouped() == {}


def test_http_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    data, cache = tmp_path / "data", tmp_path / "cache"
    data.mkdir()
    make_ipa(data / "demo.ipa", icon=make_cgbi_png())
    monkeypatch.setattr(main, "DATA_DIR", data)
    monkeypatch.setattr(main, "CACHE_DIR", cache)
    monkeypatch.setattr(main, "library", Library(data, cache))

    client = TestClient(main.app)
    source = client.get("/apps.json").json()
    assert source["apps"][0]["bundleIdentifier"] == "com.example.demo"
    assert source["apps"][0]["downloadURL"].startswith("http://testserver/ipas/")

    status = client.get("/api/status").json()
    assert status["appCount"] == 1 and status["ipaCount"] == 1

    assert client.get("/ipas/demo.ipa").status_code == 200
    assert client.get("/ipas/../secrets").status_code in (404, 400)
    assert client.get("/icons/default.png").headers["content-type"] == "image/png"
    assert "<html" in client.get("/").text


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    data, cache = tmp_path / "data", tmp_path / "cache"
    data.mkdir()
    monkeypatch.setattr(main, "DATA_DIR", data)
    monkeypatch.setattr(main, "CACHE_DIR", cache)
    monkeypatch.setattr(main, "library", Library(data, cache))
    return TestClient(main.app), data


def test_upload(tmp_path, monkeypatch):
    client, data = _client(tmp_path, monkeypatch)
    ipa_bytes = make_ipa(tmp_path / "src.ipa", version="1.5").read_bytes()

    resp = client.post(
        "/api/upload", files={"file": ("whatever.ipa", ipa_bytes, "application/octet-stream")}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bundleIdentifier"] == "com.example.demo"
    assert body["filename"] == "com.example.demo-1.5.ipa"
    assert not body["replaced"]
    assert (data / "com.example.demo-1.5.ipa").is_file()
    # re-upload of the same version reports replacement
    resp = client.post("/api/upload", files={"file": ("w.ipa", ipa_bytes, "application/octet-stream")})
    assert resp.json()["replaced"]
    # and it shows up in the source
    assert client.get("/apps.json").json()["apps"][0]["version"] == "1.5"


def test_upload_rejects_garbage(tmp_path, monkeypatch):
    client, data = _client(tmp_path, monkeypatch)
    assert client.post(
        "/api/upload", files={"file": ("x.txt", b"hi", "text/plain")}
    ).status_code == 400
    resp = client.post(
        "/api/upload", files={"file": ("x.ipa", b"not a zip", "application/octet-stream")}
    )
    assert resp.status_code == 400
    assert "Not a valid IPA" in resp.json()["detail"]
    # no stray partials or ipas left behind
    assert list(data.iterdir()) == []


def test_fetch_url(tmp_path, monkeypatch):
    import functools
    import http.server
    import threading

    client, data = _client(tmp_path, monkeypatch)
    srv_dir = tmp_path / "www"
    srv_dir.mkdir()
    make_ipa(srv_dir / "remote.ipa", bundle_id="com.example.fetched", version="2.1")

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(srv_dir)
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/remote.ipa"
        resp = client.post("/api/fetch", json={"url": url})
        assert resp.status_code == 200, resp.text
        assert resp.json()["filename"] == "com.example.fetched-2.1.ipa"
        assert (data / "com.example.fetched-2.1.ipa").is_file()

        assert client.post("/api/fetch", json={"url": "ftp://nope"}).status_code == 400
        bad = client.post("/api/fetch", json={"url": url + ".missing"})
        assert bad.status_code == 400
        assert "Download failed" in bad.json()["detail"]
    finally:
        srv.shutdown()


def test_readonly_data_dir(tmp_path, monkeypatch):
    client, data = _client(tmp_path, monkeypatch)
    data.chmod(0o555)
    try:
        assert client.get("/api/status").json()["writable"] is False
        resp = client.post(
            "/api/upload", files={"file": ("x.ipa", b"zz", "application/octet-stream")}
        )
        assert resp.status_code == 403
    finally:
        data.chmod(0o755)
