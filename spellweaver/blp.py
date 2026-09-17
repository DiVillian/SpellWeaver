"""Reading the client's own texture format, and writing one a browser can show.

Icons are BLP2 files inside the client's archives - the same files the game
draws from. Rendering them here rather than fetching lookalikes from a website
means the picker shows exactly what a player will see, works with no network at
all, and needs nothing installed.

A BLP2 is a 1,172-byte header, an optional 256-colour palette, and then the
image, usually as DXT blocks. DXT is a fixed-ratio scheme: each 4x4 block of
pixels is stored as two endpoint colours plus two bits per pixel saying where
between them that pixel sits. It is lossy, it is what the game itself shows, and
it decodes in a few lines.

Three storage modes appear in practice and all three are handled: DXT (the
common case), a palette with separate alpha, and plain uncompressed BGRA.
"""
import struct
import zlib

MAGIC = b"BLP2"
HEADER = struct.Struct("<4sIBBBBII")      # magic, type, compression, alpha size,
                                          # alpha type, mips, width, height
PALETTE_AT = 148
MIP_OFFSETS_AT = 20
MIP_SIZES_AT = 84

COMPRESSION_PALETTE = 1
COMPRESSION_DXT = 2
COMPRESSION_BGRA = 3

# Which DXT variant, as the alpha type records it.
DXT1, DXT3, DXT5 = 0, 1, 7


class BlpError(Exception):
    pass


def _rgb565(value):
    r = (value >> 11) & 0x1F
    g = (value >> 5) & 0x3F
    b = value & 0x1F
    return ((r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31)


def _colour_table(c0, c1, opaque_only):
    """The four colours a DXT block interpolates between.

    When the first endpoint is not greater than the second, DXT1 spends its
    fourth slot on transparency instead of a third shade. DXT3 and DXT5 carry
    alpha separately and always use four shades.
    """
    a, b = _rgb565(c0), _rgb565(c1)
    if c0 > c1 or opaque_only:
        two = tuple((2 * a[i] + b[i]) // 3 for i in range(3)) + (255,)
        three = tuple((a[i] + 2 * b[i]) // 3 for i in range(3)) + (255,)
    else:
        two = tuple((a[i] + b[i]) // 2 for i in range(3)) + (255,)
        three = (0, 0, 0, 0)
    return (a + (255,), b + (255,), two, three)


def _blocks(width, height):
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            yield bx, by


def _decode_dxt(data, width, height, variant):
    """DXT1, DXT3 and DXT5 into straight RGBA."""
    stride = 8 if variant == DXT1 else 16
    out = bytearray(width * height * 4)
    pos = 0
    for bx, by in _blocks(width, height):
        if pos + stride > len(data):
            break
        block = data[pos:pos + stride]
        pos += stride

        alpha = None
        if variant == DXT3:
            # Four bits of alpha per pixel, straight through.
            alpha = [((block[i // 2] >> (4 * (i % 2))) & 0xF) * 17 for i in range(16)]
            colour = block[8:]
        elif variant == DXT5:
            a0, a1 = block[0], block[1]
            bits = int.from_bytes(block[2:8], "little")
            if a0 > a1:
                ramp = [a0, a1] + [((7 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)]
            else:
                ramp = [a0, a1] + [((5 - i) * a0 + (i + 1) * a1) // 5 for i in range(4)] + [0, 255]
            alpha = [ramp[(bits >> (3 * i)) & 0x7] for i in range(16)]
            colour = block[8:]
        else:
            colour = block

        c0, c1 = struct.unpack_from("<HH", colour, 0)
        table = _colour_table(c0, c1, variant != DXT1)
        indices = int.from_bytes(colour[4:8], "little")
        for i in range(16):
            x, y = bx + (i % 4), by + (i // 4)
            if x >= width or y >= height:
                continue
            r, g, b, a = table[(indices >> (2 * i)) & 0x3]
            if alpha is not None:
                a = alpha[i]
            at = (y * width + x) * 4
            out[at:at + 4] = bytes((r, g, b, a))
    return bytes(out)


def _decode_palette(data, palette, width, height, alpha_size):
    out = bytearray(width * height * 4)
    count = width * height
    for i in range(count):
        b, g, r, _ = palette[data[i]]
        alpha = 255
        if alpha_size == 8:
            alpha = data[count + i]
        elif alpha_size == 4:
            byte = data[count + i // 2]
            alpha = ((byte >> (4 * (i % 2))) & 0xF) * 17
        elif alpha_size == 1:
            byte = data[count + i // 8]
            alpha = 255 if (byte >> (i % 8)) & 1 else 0
        out[i * 4:i * 4 + 4] = bytes((r, g, b, alpha))
    return bytes(out)


def decode(blob):
    """A BLP2 file as (width, height, RGBA bytes), at full size."""
    if len(blob) < PALETTE_AT or blob[:4] != MAGIC:
        raise BlpError("not a BLP2 file")
    _, _type, compression, alpha_size, alpha_type, _mips, width, height = \
        HEADER.unpack_from(blob, 0)
    if not width or not height:
        raise BlpError("BLP has no size")

    offset = struct.unpack_from("<I", blob, MIP_OFFSETS_AT)[0]
    size = struct.unpack_from("<I", blob, MIP_SIZES_AT)[0]
    data = blob[offset:offset + size]

    if compression == COMPRESSION_DXT:
        if alpha_type not in (DXT1, DXT3, DXT5):
            raise BlpError("unknown DXT variant %d" % alpha_type)
        return width, height, _decode_dxt(data, width, height, alpha_type)
    if compression == COMPRESSION_PALETTE:
        palette = [tuple(blob[PALETTE_AT + i * 4:PALETTE_AT + i * 4 + 4]) for i in range(256)]
        return width, height, _decode_palette(data, palette, width, height, alpha_size)
    if compression == COMPRESSION_BGRA:
        out = bytearray(width * height * 4)
        for i in range(width * height):
            b, g, r, a = data[i * 4:i * 4 + 4]
            out[i * 4:i * 4 + 4] = bytes((r, g, b, a))
        return width, height, bytes(out)
    raise BlpError("unknown BLP compression %d" % compression)


def png(width, height, rgba):
    """RGBA bytes as a PNG, which is a header, one zlib stream and a checksum."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # no per-row filtering
        raw.extend(rgba[y * width * 4:(y + 1) * width * 4])

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def to_png(blob):
    width, height, rgba = decode(blob)
    return png(width, height, rgba)
