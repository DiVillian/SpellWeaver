"""A small pure-Python MySQL client.

There is no MySQL driver installed and no pip to install one with, so this
speaks enough of the protocol to authenticate and run queries. It covers
mysql_native_password and caching_sha2_password (both the cached fast path and
the RSA full-auth path), which is what MySQL 8.4 needs over a plain socket.

This is a read/write query client, not a general driver: no prepared
statements, no streaming. Values come back as Python str/int/float/None.
"""
import hashlib
import os
import socket
import struct

# Capability flags we ask for.
CLIENT_LONG_PASSWORD = 0x00000001
CLIENT_LONG_FLAG = 0x00000004
CLIENT_CONNECT_WITH_DB = 0x00000008
CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_TRANSACTIONS = 0x00002000
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_MULTI_RESULTS = 0x00020000
CLIENT_PLUGIN_AUTH = 0x00080000
CLIENT_PLUGIN_AUTH_LENENC = 0x00200000
CLIENT_DEPRECATE_EOF = 0x01000000

_CAPS = (CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG | CLIENT_CONNECT_WITH_DB
         | CLIENT_PROTOCOL_41 | CLIENT_TRANSACTIONS | CLIENT_SECURE_CONNECTION
         | CLIENT_MULTI_RESULTS | CLIENT_PLUGIN_AUTH | CLIENT_PLUGIN_AUTH_LENENC
         | CLIENT_DEPRECATE_EOF)

# Column types that should come back as numbers.
_INT_TYPES = {1, 2, 3, 8, 9, 13, 16}
_FLOAT_TYPES = {4, 5, 0, 246}


class MySQLError(Exception):
    pass


# --- primitive codecs -------------------------------------------------------

def _lenenc_int(value):
    if value < 251:
        return bytes([value])
    if value < 1 << 16:
        return b"\xfc" + struct.pack("<H", value)
    if value < 1 << 24:
        return b"\xfd" + struct.pack("<I", value)[:3]
    return b"\xfe" + struct.pack("<Q", value)


def _read_lenenc_int(buf, pos):
    first = buf[pos]
    if first < 251:
        return first, pos + 1
    if first == 0xFB:
        return None, pos + 1
    if first == 0xFC:
        return struct.unpack_from("<H", buf, pos + 1)[0], pos + 3
    if first == 0xFD:
        return int.from_bytes(buf[pos + 1:pos + 4], "little"), pos + 4
    return struct.unpack_from("<Q", buf, pos + 1)[0], pos + 9


def _read_lenenc_str(buf, pos):
    n, pos = _read_lenenc_int(buf, pos)
    if n is None:
        return None, pos
    return buf[pos:pos + n], pos + n


def _read_nul_str(buf, pos):
    end = buf.index(b"\0", pos)
    return buf[pos:end], end + 1


# --- authentication ---------------------------------------------------------

def _native_password(password, nonce):
    if not password:
        return b""
    pw = password.encode()
    h1 = hashlib.sha1(pw).digest()
    h2 = hashlib.sha1(h1).digest()
    h3 = hashlib.sha1(nonce + h2).digest()
    return bytes(a ^ b for a, b in zip(h1, h3))


def _caching_sha2(password, nonce):
    if not password:
        return b""
    pw = password.encode()
    d1 = hashlib.sha256(pw).digest()
    d2 = hashlib.sha256(d1).digest()
    d3 = hashlib.sha256(d2 + nonce).digest()
    return bytes(a ^ b for a, b in zip(d1, d3))


def _der_read(buf, pos):
    """Read one DER TLV. Returns (tag, content_bytes, next_pos)."""
    tag = buf[pos]
    pos += 1
    n = buf[pos]
    pos += 1
    if n & 0x80:
        count = n & 0x7F
        n = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    return tag, buf[pos:pos + n], pos + n


def _rsa_pubkey_from_pem(pem):
    """Pull (modulus, exponent) out of a PEM SubjectPublicKeyInfo."""
    import base64
    body = b"".join(l for l in pem.splitlines() if not l.startswith(b"-----"))
    der = base64.b64decode(body)
    _, spki, _ = _der_read(der, 0)                 # outer SEQUENCE
    _, _alg, pos = _der_read(spki, 0)              # AlgorithmIdentifier
    _, bitstring, _ = _der_read(spki, pos)         # BIT STRING
    _, rsakey, _ = _der_read(bitstring[1:], 0)     # skip unused-bits byte
    _, n_bytes, pos = _der_read(rsakey, 0)
    _, e_bytes, _ = _der_read(rsakey, pos)
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def _mgf1(seed, length):
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha1(seed + struct.pack(">I", counter)).digest()
        counter += 1
    return out[:length]


def _rsa_oaep_encrypt(message, n, e):
    """RSAES-OAEP with SHA-1, which is what MySQL's server public key expects."""
    k = (n.bit_length() + 7) // 8
    h_len = 20
    if len(message) > k - 2 * h_len - 2:
        raise MySQLError("password too long to encrypt")
    l_hash = hashlib.sha1(b"").digest()
    padding = b"\0" * (k - len(message) - 2 * h_len - 2)
    data_block = l_hash + padding + b"\x01" + message
    seed = os.urandom(h_len)
    masked_db = bytes(a ^ b for a, b in zip(data_block, _mgf1(seed, k - h_len - 1)))
    masked_seed = bytes(a ^ b for a, b in zip(seed, _mgf1(masked_db, h_len)))
    encoded = b"\x00" + masked_seed + masked_db
    cipher = pow(int.from_bytes(encoded, "big"), e, n)
    return cipher.to_bytes(k, "big")


# --- connection -------------------------------------------------------------

class Connection:
    def __init__(self, host="127.0.0.1", port=3306, user="root", password="", database=None):
        self._seq = 0
        self._sock = socket.create_connection((host, port), timeout=30)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._password = password
        try:
            self._handshake(user, password, database)
        except Exception:
            self._sock.close()
            raise

    # -- framing --
    def _recv_exact(self, n):
        chunks = []
        got = 0
        while got < n:
            block = self._sock.recv(n - got)
            if not block:
                raise MySQLError("server closed the connection")
            chunks.append(block)
            got += len(block)
        return b"".join(chunks)

    def _read_packet(self):
        head = self._recv_exact(4)
        length = int.from_bytes(head[:3], "little")
        self._seq = (head[3] + 1) & 0xFF
        payload = self._recv_exact(length)
        # A payload of exactly 0xFFFFFF continues in the next packet.
        while length == 0xFFFFFF:
            head = self._recv_exact(4)
            length = int.from_bytes(head[:3], "little")
            self._seq = (head[3] + 1) & 0xFF
            payload += self._recv_exact(length)
        if payload[:1] == b"\xff":
            code = struct.unpack_from("<H", payload, 1)[0]
            msg = payload[9:].decode("utf-8", "replace")
            raise MySQLError("MySQL error %d: %s" % (code, msg))
        return payload

    def _send_packet(self, payload):
        self._sock.sendall(len(payload).to_bytes(3, "little")
                           + bytes([self._seq]) + payload)
        self._seq = (self._seq + 1) & 0xFF

    # -- login --
    def _handshake(self, user, password, database):
        packet = self._read_packet()
        pos = 1                                       # protocol version
        _, pos = _read_nul_str(packet, pos)           # server version
        pos += 4                                      # connection id
        nonce = packet[pos:pos + 8]
        pos += 8 + 1                                  # scramble part 1 + filler
        pos += 2 + 1 + 2 + 2                          # caps lo, charset, status, caps hi
        scramble_len = packet[pos]
        pos += 1 + 10                                 # length + reserved
        nonce += packet[pos:pos + max(13, scramble_len - 8) - 1]
        pos += max(13, scramble_len - 8)
        plugin, pos = _read_nul_str(packet, pos)
        plugin = plugin.decode()

        if plugin == "caching_sha2_password":
            auth = _caching_sha2(password, nonce)
        else:
            plugin = "mysql_native_password"
            auth = _native_password(password, nonce)

        caps = _CAPS if database else _CAPS & ~CLIENT_CONNECT_WITH_DB
        body = struct.pack("<IIB", caps, 0xFFFFFF, 45) + b"\0" * 23
        body += user.encode() + b"\0"
        body += _lenenc_int(len(auth)) + auth
        if database:
            body += database.encode() + b"\0"
        body += plugin.encode() + b"\0"
        self._send_packet(body)
        self._finish_auth(password, nonce)

    def _finish_auth(self, password, nonce):
        packet = self._read_packet()
        if packet[:1] == b"\x00":
            return
        if packet[:1] == b"\xfe":
            # Server wants a different plugin than the one we offered.
            name, pos = _read_nul_str(packet, 1)
            new_nonce = packet[pos:pos + 20]
            name = name.decode()
            if name == "caching_sha2_password":
                self._send_packet(_caching_sha2(password, new_nonce))
            else:
                self._send_packet(_native_password(password, new_nonce))
            self._finish_auth(password, new_nonce)
            return
        if packet[:1] != b"\x01":
            raise MySQLError("unexpected auth response %r" % packet[:1])

        status = packet[1]
        if status == 3:                # fast auth succeeded; an OK packet follows
            self._read_packet()
            return
        if status != 4:
            raise MySQLError("unsupported auth continuation %d" % status)

        # Full auth. On a plain socket we must encrypt the password with the
        # server's public key, which we ask for with 0x02.
        self._send_packet(b"\x02")
        key_packet = self._read_packet()
        n, e = _rsa_pubkey_from_pem(key_packet[1:])
        secret = password.encode() + b"\0"
        xored = bytes(c ^ nonce[i % len(nonce)] for i, c in enumerate(secret))
        self._send_packet(_rsa_oaep_encrypt(xored, n, e))
        self._read_packet()

    # -- queries --
    def query(self, sql, args=None):
        """Run a SELECT and return (columns, rows)."""
        if args:
            sql = sql % tuple(self.escape(a) for a in args)
        self._seq = 0
        self._send_packet(b"\x03" + sql.encode("utf-8"))
        first = self._read_packet()
        if first[:1] == b"\x00" or first[:1] == b"\xfe":
            return [], []
        count, _ = _read_lenenc_int(first, 0)
        columns, types = [], []
        for _ in range(count):
            col = self._read_packet()
            pos = 0
            for _ in range(5):                       # catalog, db, table, org_table, name
                value, pos = _read_lenenc_str(col, pos)
            columns.append(value.decode("utf-8", "replace"))
            _, pos = _read_lenenc_str(col, pos)      # org_name
            pos += 1 + 2 + 4                         # filler, charset, length
            types.append(col[pos])
        rows = []
        while True:
            packet = self._read_packet()
            if packet[:1] == b"\xfe" and len(packet) < 0xFFFFFF:
                break
            pos = 0
            row = []
            for i in range(count):
                raw, pos = _read_lenenc_str(packet, pos)
                row.append(self._convert(raw, types[i]))
            rows.append(tuple(row))
        return columns, rows

    def execute(self, sql):
        """Run a statement that returns no rows."""
        self._seq = 0
        self._send_packet(b"\x03" + sql.encode("utf-8"))
        self._read_packet()

    @staticmethod
    def _convert(raw, coltype):
        if raw is None:
            return None
        if coltype in _INT_TYPES:
            return int(raw)
        if coltype in _FLOAT_TYPES:
            return float(raw)
        return raw.decode("utf-8", "replace")

    @staticmethod
    def escape(value):
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return repr(value)
        text = str(value)
        out = []
        for ch in text:
            if ch in "\\'\"":
                out.append("\\" + ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\0":
                out.append("\\0")
            elif ch == "\x1a":
                out.append("\\Z")
            else:
                out.append(ch)
        return "'" + "".join(out) + "'"

    def close(self):
        try:
            self._seq = 0
            self._send_packet(b"\x01")
        except Exception:
            pass
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def quote(value):
    """A value as a SQL literal, for the statements we build and show.

    The same escaping the connection uses, exposed on its own because the SQL
    the UI displays is built before anything is sent.
    """
    return Connection.escape(value)


def connect(cfg, database=None):
    return Connection(cfg.host, cfg.port, cfg.user, cfg.password,
                      database or cfg.world_db)
