"""Minimal WDBC (World of Warcraft 3.3.5a client database) reader and writer.

A WDBC file is a 20-byte header, a block of fixed-size records, then a string
block. Every field is 4 bytes; whether it is an int, a float or an offset into
the string block is not recorded in the file, so the caller supplies a layout.
"""
import struct

HEADER = struct.Struct("<4sIIII")
MAGIC = b"WDBC"


class DbcError(Exception):
    pass


class Dbc:
    """A parsed WDBC file.

    Records are kept as raw tuples of ints; use `field()` to pull a typed value
    out of one. This keeps a 49k-row, 234-field table cheap to hold in memory.
    """

    def __init__(self, records, string_block, field_count):
        self.records = records
        self.string_block = string_block
        self.field_count = field_count

    @classmethod
    def read(cls, path):
        with open(path, "rb") as fh:
            return cls.parse(fh.read(), path)

    @classmethod
    def parse(cls, blob, path="<bytes>"):
        """Parse a DBC already in memory, as one read out of an archive is."""
        if len(blob) < HEADER.size:
            raise DbcError("%s is too short to be a DBC file" % path)
        magic, count, fields, size, sblock = HEADER.unpack_from(blob, 0)
        if magic != MAGIC:
            raise DbcError("%s is not a WDBC file (magic %r)" % (path, magic))
        if fields * 4 != size:
            raise DbcError("%s: record size %d does not match %d fields" % (path, size, fields))
        expected = HEADER.size + count * size + sblock
        if len(blob) != expected:
            raise DbcError("%s: expected %d bytes, found %d" % (path, expected, len(blob)))
        row = struct.Struct("<%di" % fields)
        base = HEADER.size
        records = [row.unpack_from(blob, base + i * size) for i in range(count)]
        strings = blob[base + count * size:]
        return cls(records, strings, fields)

    def string_at(self, offset):
        """Decode the NUL-terminated string at `offset` in the string block."""
        if offset <= 0 or offset >= len(self.string_block):
            return ""
        end = self.string_block.find(b"\0", offset)
        if end < 0:
            end = len(self.string_block)
        return self.string_block[offset:end].decode("utf-8", "replace")

    def __len__(self):
        return len(self.records)

    def __iter__(self):
        return iter(self.records)


def as_float(raw):
    """Reinterpret a field's raw int bits as the float they actually encode."""
    return struct.unpack("<f", struct.pack("<i", raw))[0]


def write(path, rows, string_block):
    """Write a WDBC file. `rows` is a list of equal-length int tuples."""
    if not rows:
        raise DbcError("refusing to write a DBC with no records")
    fields = len(rows[0])
    row = struct.Struct("<%di" % fields)
    with open(path, "wb") as fh:
        fh.write(HEADER.pack(MAGIC, len(rows), fields, fields * 4, len(string_block)))
        for r in rows:
            if len(r) != fields:
                raise DbcError("row has %d fields, expected %d" % (len(r), fields))
            fh.write(row.pack(*r))
        fh.write(string_block)
