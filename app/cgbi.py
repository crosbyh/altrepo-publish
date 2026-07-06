"""Normalize Apple CgBI-optimized PNGs into standard PNGs.

Icons inside IPAs are usually run through Apple's pngcrush variant, which
inserts a CgBI chunk, strips the zlib header from IDAT, and stores pixels
as BGRA. Browsers (and Feather's image views) can't decode that, so we
undo it. Alpha stays premultiplied — a visible difference only on
translucent edges, which app icons don't have.
"""

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Apple-private chunks that should not be carried into the rebuilt file.
_DROP_CHUNKS = {b"CgBI", b"iDOT"}


def _read_chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        yield ctype, cdata
        pos += 12 + length
        if ctype == b"IEND":
            break


def _write_chunk(out: bytearray, ctype: bytes, cdata: bytes) -> None:
    out.extend(struct.pack(">I", len(cdata)))
    out.extend(ctype)
    out.extend(cdata)
    out.extend(struct.pack(">I", zlib.crc32(ctype + cdata) & 0xFFFFFFFF))


def normalize_png(data: bytes) -> bytes:
    """Return standard PNG bytes; input is returned unchanged if it is not
    a CgBI file (or not a PNG at all)."""
    if not data.startswith(PNG_SIGNATURE):
        return data

    chunks = list(_read_chunks(data))
    if not any(t == b"CgBI" for t, _ in chunks):
        return data

    ihdr = next((c for t, c in chunks if t == b"IHDR"), None)
    if ihdr is None:
        return data
    width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )

    idat = b"".join(c for t, c in chunks if t == b"IDAT")
    decomp = zlib.decompressobj(-15)  # CgBI IDAT is raw deflate, no zlib header
    raw = bytearray(decomp.decompress(idat) + decomp.flush())

    if bit_depth == 8 and color_type == 6 and interlace == 0:
        # Swap BGRA -> RGBA. PNG row filters operate bytewise at fixed pixel
        # offsets, so a consistent per-pixel channel swap commutes with the
        # filtering and can be applied to the filtered scanlines directly.
        stride = width * 4 + 1
        for y in range(height):
            row = y * stride + 1
            for i in range(row, row + width * 4, 4):
                raw[i], raw[i + 2] = raw[i + 2], raw[i]

    out = bytearray(PNG_SIGNATURE)
    for ctype, cdata in chunks:
        if ctype in _DROP_CHUNKS or ctype == b"IDAT":
            continue
        if ctype == b"IEND":
            _write_chunk(out, b"IDAT", zlib.compress(bytes(raw), 9))
            _write_chunk(out, b"IEND", b"")
            break
        _write_chunk(out, ctype, cdata)
    return bytes(out)


def solid_png(size: int = 256, rgba: tuple = (99, 91, 255, 255)) -> bytes:
    """Generate a solid-color PNG, used as the fallback icon."""
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    row = b"\x00" + bytes(rgba) * size
    out = bytearray(PNG_SIGNATURE)
    _write_chunk(out, b"IHDR", ihdr)
    _write_chunk(out, b"IDAT", zlib.compress(row * size, 9))
    _write_chunk(out, b"IEND", b"")
    return bytes(out)
