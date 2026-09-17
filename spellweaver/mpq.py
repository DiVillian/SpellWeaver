"""Reading and writing MPQ archives, in pure Python.

The client keeps its data in MPQ archives, and a custom spell only becomes
visible to a player when a patch archive supplies a Spell.dbc that mentions it.
Writing one is the last step of the pipeline.

This is written from the format rather than bound to a library, for the same
reason mysql.py is: spellweaver has to run wherever the server runs, and a tool
that needs a compiler before it will start is a tool the user cannot run. The
standard library already has the only hard part - zlib, which is what the client
compresses these files with.

The format, briefly. A 32-byte header points at two tables. The hash table maps
a file name, hashed three separate ways, to a slot; the block table says where
that file's data is and how it is stored. Both tables are encrypted with a key
derived from their own names. File data is cut into fixed-size sectors, each
compressed on its own and preceded by a table of offsets, so the client can read
the middle of a large file without inflating the whole thing.

Only what a 3.3.5a client actually uses is implemented: format 0 and 1, zlib and
bzip2 and stored sectors, and encrypted files. Anything else raises rather than
returning plausible nonsense.
"""
import bz2
import os
import struct
import zlib

MAGIC = b"MPQ\x1a"
HEADER = struct.Struct("<4sIIHHIIII")        # 32 bytes: the two table
                                             # positions come before their sizes
HASH_ENTRY = struct.Struct("<IIHHI")         # 16 bytes
BLOCK_ENTRY = struct.Struct("<IIII")         # 16 bytes

EMPTY = 0xFFFFFFFF
DELETED = 0xFFFFFFFE

FLAG_IMPLODE = 0x00000100
FLAG_COMPRESS = 0x00000200
FLAG_ENCRYPTED = 0x00010000
FLAG_FIX_KEY = 0x00020000
FLAG_SINGLE_UNIT = 0x01000000
FLAG_SECTOR_CRC = 0x04000000
FLAG_EXISTS = 0x80000000

COMPRESS_ZLIB = 0x02
COMPRESS_BZIP2 = 0x10

LISTFILE = "(listfile)"

# 512 << 3 = 4096, what Blizzard's own archives use.
DEFAULT_SECTOR_SHIFT = 3


class MpqError(Exception):
    pass


def _crypt_table():
    """The table every hash and every encrypted block in the format derives from."""
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        index = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            high = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            table[index] = high | (seed & 0xFFFF)
            index += 0x100
    return table


_CRYPT = _crypt_table()

HASH_OFFSET = 0
HASH_NAME_A = 1
HASH_NAME_B = 2
HASH_FILE_KEY = 3


def _normalise(name):
    """Internal paths are case-insensitive and use backslashes."""
    return name.replace("/", "\\").upper()


def hash_string(text, kind):
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for char in _normalise(text):
        value = ord(char)
        seed1 = _CRYPT[(kind << 8) + value] ^ ((seed1 + seed2) & 0xFFFFFFFF)
        seed2 = (value + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1 & 0xFFFFFFFF


def _decrypt(words, key):
    out = []
    seed = 0xEEEEEEEE
    for word in words:
        seed = (seed + _CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        plain = word ^ ((key + seed) & 0xFFFFFFFF)
        out.append(plain)
        key = ((((~key) << 0x15) & 0xFFFFFFFF) + 0x11111111 | (key >> 0x0B)) & 0xFFFFFFFF
        seed = (plain + seed + (seed << 5) + 3) & 0xFFFFFFFF
    return out


def _encrypt(words, key):
    out = []
    seed = 0xEEEEEEEE
    for plain in words:
        seed = (seed + _CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        out.append((plain ^ ((key + seed) & 0xFFFFFFFF)) & 0xFFFFFFFF)
        key = ((((~key) << 0x15) & 0xFFFFFFFF) + 0x11111111 | (key >> 0x0B)) & 0xFFFFFFFF
        seed = (plain + seed + (seed << 5) + 3) & 0xFFFFFFFF
    return out


def _words(blob):
    return list(struct.unpack("<%dI" % (len(blob) // 4), blob[:len(blob) // 4 * 4]))


def _bytes(words):
    return struct.pack("<%dI" % len(words), *words)


def _decompress(blob, expected):
    """One sector. The first byte says which methods were used on it."""
    if len(blob) >= expected:
        return blob[:expected]                      # stored, not compressed
    mask, body = blob[0], blob[1:]
    if mask == COMPRESS_ZLIB:
        return zlib.decompress(body)
    if mask == COMPRESS_BZIP2:
        return bz2.decompress(body)
    raise MpqError("unsupported sector compression 0x%02x" % mask)


class Archive:
    """An MPQ open for reading."""

    def __init__(self, path):
        self.path = str(path)
        self._fh = open(self.path, "rb")
        try:
            self._read_header()
            self._read_tables()
        except Exception:
            self._fh.close()
            raise

    # -- opening -------------------------------------------------------
    def _read_header(self):
        # The header is not always at zero: an archive can be appended to
        # another file, so the client looks every 512 bytes for the magic.
        size = os.path.getsize(self.path)
        offset = 0
        while offset < size:
            self._fh.seek(offset)
            head = self._fh.read(HEADER.size)
            if len(head) < HEADER.size:
                break
            if head[:4] == MAGIC:
                (_, header_size, _archive_size, version, shift,
                 hash_pos, block_pos, hash_count, block_count) = HEADER.unpack(head)
                if version > 1:
                    raise MpqError("%s: MPQ format %d is newer than 3.3.5a uses"
                                   % (self.path, version))
                self.base = offset
                self.header_size = header_size
                self.version = version
                self.sector_size = 512 << shift
                self.hash_pos = hash_pos
                self.hash_count = hash_count
                self.block_pos = block_pos
                self.block_count = block_count
                return
            offset += 512
        raise MpqError("%s: no MPQ header found" % self.path)

    def _read_tables(self):
        # Both tables are four 32-bit words per entry, so they are read straight
        # out of the decrypted word list. Rebuilding the blob per entry would
        # make opening a 100,000-file archive quadratic.
        self._fh.seek(self.base + self.hash_pos)
        words = _decrypt(_words(self._fh.read(self.hash_count * HASH_ENTRY.size)),
                         hash_string("(hash table)", HASH_FILE_KEY))
        self.hash_table = [(words[i], words[i + 1],
                            words[i + 2] & 0xFFFF, words[i + 2] >> 16, words[i + 3])
                           for i in range(0, self.hash_count * 4, 4)]

        self._fh.seek(self.base + self.block_pos)
        words = _decrypt(_words(self._fh.read(self.block_count * BLOCK_ENTRY.size)),
                         hash_string("(block table)", HASH_FILE_KEY))
        self.block_table = [tuple(words[i:i + 4])
                            for i in range(0, self.block_count * 4, 4)]

    # -- lookup --------------------------------------------------------
    def _find(self, name):
        if not self.hash_count:
            return None
        start = hash_string(name, HASH_OFFSET) % self.hash_count
        want_a = hash_string(name, HASH_NAME_A)
        want_b = hash_string(name, HASH_NAME_B)
        for step in range(self.hash_count):
            entry = self.hash_table[(start + step) % self.hash_count]
            name_a, name_b, _locale, _platform, block = entry
            if block == EMPTY:
                return None                          # never used: the chain ends
            if block != DELETED and name_a == want_a and name_b == want_b:
                return block
        return None

    def has(self, name):
        return self._find(name) is not None

    def read(self, name):
        """The file's contents, decompressed and decrypted."""
        index = self._find(name)
        if index is None:
            raise KeyError(name)
        if index >= len(self.block_table):
            raise MpqError("%s: block index %d out of range" % (self.path, index))
        position, packed_size, size, flags = self.block_table[index]
        if not flags & FLAG_EXISTS:
            raise KeyError(name)
        if flags & FLAG_IMPLODE:
            raise MpqError("%s: %s uses PKWARE implode, which is not supported"
                           % (self.path, name))

        self._fh.seek(self.base + position)
        raw = self._fh.read(packed_size)

        key = None
        if flags & FLAG_ENCRYPTED:
            key = hash_string(name.replace("/", "\\").rsplit("\\", 1)[-1], HASH_FILE_KEY)
            if flags & FLAG_FIX_KEY:
                key = ((key + position) ^ size) & 0xFFFFFFFF

        compressed = bool(flags & (FLAG_COMPRESS | FLAG_IMPLODE))

        if flags & FLAG_SINGLE_UNIT:
            if key is not None:
                raw = _bytes(_decrypt(_words(raw), key))[:packed_size]
            return _decompress(raw, size) if compressed else raw[:size]

        # The table of sector offsets only exists for a file that was
        # compressed. A stored file is its own bytes, one sector after another,
        # and looking for a table in front of it reads file data as offsets.
        if not compressed:
            if key is None:
                return raw[:size]
            out = bytearray()
            for i in range(0, (size + self.sector_size - 1) // self.sector_size):
                start = i * self.sector_size
                chunk = raw[start:start + min(self.sector_size, size - start)]
                out.extend(_bytes(_decrypt(_words(chunk), (key + i) & 0xFFFFFFFF))[:len(chunk)])
            return bytes(out[:size])

        count = (size + self.sector_size - 1) // self.sector_size
        table_len = count + 1
        if flags & FLAG_SECTOR_CRC:
            table_len += 1
        offsets = _words(raw[:table_len * 4])
        if key is not None:
            offsets = _decrypt(offsets, (key - 1) & 0xFFFFFFFF)

        out = bytearray()
        for i in range(count):
            chunk = raw[offsets[i]:offsets[i + 1]]
            if key is not None:
                chunk = _bytes(_decrypt(_words(chunk), (key + i) & 0xFFFFFFFF))[:len(chunk)]
            expected = min(self.sector_size, size - len(out))
            out.extend(_decompress(chunk, expected))
        return bytes(out[:size])

    def names(self):
        """What the archive says it holds. Only files it lists can be named."""
        try:
            listing = self.read(LISTFILE)
        except (KeyError, MpqError):
            return []
        text = listing.decode("utf-8", "replace")
        return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _table_size(file_count):
    """Hash tables are a power of two, and want slack so probing stays short."""
    size = 16
    while size < file_count * 2:
        size *= 2
    return size


def write(path, files, sector_shift=DEFAULT_SECTOR_SHIFT):
    """Write a new archive holding `files`, a mapping of internal path to bytes.

    Everything is zlib-compressed and left unencrypted, which is what the client
    reads fastest and what makes the result easy to inspect afterwards. A
    (listfile) is generated so the archive can describe itself.
    """
    if not files:
        raise MpqError("refusing to write an archive with no files")
    sector_size = 512 << sector_shift

    contents = dict(files)
    contents[LISTFILE] = "\r\n".join(contents).encode("utf-8")

    packed = []
    for name, blob in contents.items():
        body = bytearray()
        sectors = [blob[i:i + sector_size] for i in range(0, len(blob), sector_size)] or [b""]
        offsets = [(len(sectors) + 1) * 4]
        for sector in sectors:
            squeezed = zlib.compress(sector, 9)
            # A sector is only worth compressing if it actually got smaller;
            # otherwise it is stored raw and the reader tells by its length.
            if len(squeezed) + 1 < len(sector):
                body.extend(bytes([COMPRESS_ZLIB]) + squeezed)
            else:
                body.extend(sector)
            # Each entry is where the next sector starts: the table, then every
            # sector written so far.
            offsets.append(offsets[0] + len(body))
        table = _bytes(offsets)
        packed.append((name, bytes(table) + bytes(body), len(blob)))

    hash_count = _table_size(len(packed))
    header_size = HEADER.size
    position = header_size
    blocks, placed = [], []
    for name, body, size in packed:
        blocks.append((position, len(body), size, FLAG_EXISTS | FLAG_COMPRESS))
        placed.append(body)
        position += len(body)

    hash_pos = position
    block_pos = hash_pos + hash_count * HASH_ENTRY.size
    archive_size = block_pos + len(blocks) * BLOCK_ENTRY.size

    table = [[EMPTY, EMPTY, 0xFFFF, 0xFFFF, EMPTY] for _ in range(hash_count)]
    for index, (name, _body, _size) in enumerate(packed):
        slot = hash_string(name, HASH_OFFSET) % hash_count
        while table[slot][4] != EMPTY:
            slot = (slot + 1) % hash_count
        table[slot] = [hash_string(name, HASH_NAME_A), hash_string(name, HASH_NAME_B),
                       0, 0, index]

    hash_blob = b"".join(HASH_ENTRY.pack(*row) for row in table)
    hash_blob = _bytes(_encrypt(_words(hash_blob), hash_string("(hash table)", HASH_FILE_KEY)))
    block_blob = b"".join(BLOCK_ENTRY.pack(*row) for row in blocks)
    block_blob = _bytes(_encrypt(_words(block_blob), hash_string("(block table)", HASH_FILE_KEY)))

    with open(path, "wb") as fh:
        fh.write(HEADER.pack(MAGIC, header_size, archive_size, 0, sector_shift,
                             hash_pos, block_pos, hash_count, len(blocks)))
        for body in placed:
            fh.write(body)
        fh.write(hash_blob)
        fh.write(block_blob)
    return archive_size
