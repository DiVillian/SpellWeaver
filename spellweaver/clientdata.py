"""Getting the game's own data files out of a client.

The vocabulary is derived from Blizzard's spell data, so the tool needs a handful
of DBCs to work from. They are Blizzard's files and are not distributed with this
project: they are taken from the client the tool is pointed at, which is the same
client the patch is later written into.

The custom patch slots are skipped, so what comes out is what Blizzard shipped
rather than whatever a module has layered on top. That is a different question
from the one the client patch asks: the patch merges into the copy the client
actually *reads*, while the vocabulary has to come from the copy Blizzard *sent*.
Reading a module's spells in as evidence would tune the tool to one install, and
reading our own back in would tune it to itself.
"""
import pathlib

from . import client as client_module
from . import mpq

# What the corpus, the index tables and the class trees are built from.
REQUIRED = [
    "Spell.dbc",
    "SpellIcon.dbc",
    "SpellCastTimes.dbc",
    "SpellDuration.dbc",
    "SpellRadius.dbc",
    "SpellRange.dbc",
    "SkillLine.dbc",
    "SkillLineAbility.dbc",
]

DATA = pathlib.Path(__file__).resolve().parent.parent / "data/dbc"

HOW = ("Set the game client on the Settings tab, or run "
       "`python3 tools/extract_dbc.py --client /path/to/WoW`.")


class DataError(Exception):
    pass


def missing(into=DATA):
    """Which of the files we need are not here yet."""
    into = pathlib.Path(into)
    return [name for name in REQUIRED if not (into / name).is_file()]


def extract(client_dir, into=DATA, locale=None):
    """Take each file from the strongest archive Blizzard shipped it in."""
    into = pathlib.Path(into)
    into.mkdir(parents=True, exist_ok=True)
    ordered, locale = client_module.chain(client_dir, locale)
    stock = [path for path, tier, _rank in ordered if not tier.startswith("custom")]

    opened = []
    for path in reversed(stock):                        # strongest stock first
        try:
            opened.append(mpq.Archive(path))
        except (mpq.MpqError, OSError):
            continue
    if not opened:
        raise DataError("No readable archives in %s." % client_dir)

    written = []
    try:
        for name in REQUIRED:
            internal = "DBFilesClient\\" + name
            for archive in opened:
                if not archive.has(internal):
                    continue
                blob = archive.read(internal)
                (into / name).write_bytes(blob)
                written.append({"name": name,
                                "archive": pathlib.Path(archive.path).name,
                                "bytes": len(blob)})
                break
            else:
                raise DataError(
                    "No archive in %s holds %s. Is that a 3.3.5a client?"
                    % (client_dir, internal))
    finally:
        for archive in opened:
            archive.close()
    return written, locale
