"""Icons, taken from the client the player will actually use.

SpellIcon.dbc names an icon as a path - `Interface\\Icons\\Spell_Holy_LesserHeal`
- and the artwork itself is a BLP inside one of the client's archives. Since the
tool can already read those archives and decode that format, it can render the
real icon rather than fetching a lookalike from a website.

That matters beyond tidiness: the picker then shows exactly what the player will
see, it works with no network, and a client carrying custom artwork shows its
own. The load order is respected, so an icon replaced by a patch resolves to the
replacement, the same way Spell.dbc does.

Decoding is a few milliseconds per icon, and the results are kept - in memory for
the session and on disk between them - because the grid asks for sixty at once.
"""
import pathlib
import re
import threading

from . import blp, client, mpq

ICON_DIR = "Interface\\Icons\\"
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,96}$")


class IconError(Exception):
    pass


def is_safe(name):
    """Icon names come from a URL, so they are checked before becoming a path."""
    return bool(SAFE_NAME.match(name or "")) and ".." not in name


class IconLibrary:
    """The client's icons, decoded on demand and remembered afterwards."""

    def __init__(self, client_dir, locale=None, cache_dir=None):
        self.client_dir = str(client_dir)
        self.locale = locale
        self.cache_dir = pathlib.Path(cache_dir) if cache_dir else None
        self._archives = None
        self._memory = {}
        self._lock = threading.Lock()

    def _open_archives(self):
        """Every archive, strongest first, so the first hit is the right one."""
        ordered, _locale = client.chain(self.client_dir, self.locale)
        opened = []
        for path, _tier, _rank in reversed(ordered):
            try:
                opened.append(mpq.Archive(path))
            except (mpq.MpqError, OSError):
                continue
        if not opened:
            raise IconError("No readable archives in %s" % self.client_dir)
        return opened

    def _archive_list(self):
        if self._archives is None:
            self._archives = self._open_archives()
        return self._archives

    def raw(self, name):
        """The BLP for an icon, from the archive that wins the load order."""
        internal = ICON_DIR + name + ".blp"
        for archive in self._archive_list():
            try:
                if archive.has(internal):
                    return archive.read(internal)
            except (mpq.MpqError, OSError):
                continue
        raise KeyError(name)

    def png(self, name):
        if not is_safe(name):
            raise IconError("unusable icon name")
        if name in self._memory:
            return self._memory[name]
        cached = self.cache_dir / (name + ".png") if self.cache_dir else None
        if cached and cached.is_file():
            data = cached.read_bytes()
            self._memory[name] = data
            return data
        with self._lock:
            # Archives are read with one file handle each, so only one thread
            # may be seeking in them at a time.
            data = blp.to_png(self.raw(name))
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        self._memory[name] = data
        return data

    def close(self):
        for archive in self._archives or []:
            archive.close()
        self._archives = None
