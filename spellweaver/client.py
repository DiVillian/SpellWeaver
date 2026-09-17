"""Finding the file the client actually reads.

A WoW client does not have one Spell.dbc. This client has five, spread across
its archives, and they are not variations on a theme - they have 222, 239, 240
and 234 fields, because the record grew over the expansions. The client reads
whichever copy sits highest in its load order and ignores the rest.

So a patch cannot be built against "the" Spell.dbc. It has to be built against
the copy that wins, which depends entirely on what is installed: on a server
running mod-individual-progression that is patch-V.mpq, and on a clean client it
is patch-enUS-3.MPQ. The tool works it out rather than being told, and reports
the chain it resolved so a wrong answer is visible instead of silent.

The order below is the client's: base archives, then the locale ones, then the
numbered patches, and last the custom slots that private servers use.

Within the custom slots, a patch in `Data/` beats one in `Data/<locale>/`. That
is measured, not assumed: the same spell was shipped in `Data/patch-W.MPQ` and
`Data/enUS/patch-enUS-Z.MPQ` under different names, and the client showed the
one from `Data/` - even though Z sorts after W, so it is the folder that decides
and not the letter. `resolve()` returns every archive that holds the file, so the
winner can always be checked against the runners-up.
"""
import os
import pathlib
import re

from . import mpq

SPELL_DBC = "DBFilesClient\\Spell.dbc"
SKILL_LINE_ABILITY_DBC = "DBFilesClient\\SkillLineAbility.dbc"

# Lowest priority first. %s is the locale, e.g. enUS.
GENERIC_BASE = ["common.MPQ", "common-2.MPQ", "expansion.MPQ", "lichking.MPQ"]
LOCALE_BASE = ["locale-%s.MPQ", "expansion-locale-%s.MPQ", "lichking-locale-%s.MPQ",
               "base-%s.MPQ", "backup-%s.MPQ", "speech-%s.MPQ",
               "expansion-speech-%s.MPQ", "lichking-speech-%s.MPQ"]
GENERIC_NUMBERED = ["patch.MPQ", "patch-2.MPQ", "patch-3.MPQ"]
LOCALE_NUMBERED = ["patch-%s.MPQ", "patch-%s-2.MPQ", "patch-%s-3.MPQ"]

# The custom slots, in the order the client takes them. Numbers before letters.
CUSTOM_SUFFIXES = [str(n) for n in range(4, 10)] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]


class ClientError(Exception):
    pass


class Candidate:
    """One archive that holds the file we asked about."""

    def __init__(self, path, tier, rank, records=None, fields=None):
        self.path = path
        self.tier = tier
        self.rank = rank
        self.records = records
        self.fields = fields

    @property
    def name(self):
        return os.path.basename(self.path)

    def __repr__(self):
        return "<%s %s rank=%d>" % (self.name, self.tier, self.rank)


def locales(client_dir):
    """The locale folders a client has, e.g. ['enUS']."""
    data = pathlib.Path(client_dir) / "Data"
    if not data.is_dir():
        raise ClientError("%s has no Data directory; is that a WoW client?" % client_dir)
    found = []
    for entry in sorted(data.iterdir()):
        if entry.is_dir() and re.fullmatch(r"[a-z]{2}[A-Z]{2}", entry.name):
            if any(p.suffix.lower() == ".mpq" for p in entry.iterdir()):
                found.append(entry.name)
    return found


def _exists(folder, name):
    """Case-insensitive lookup; the client is on Windows, we may not be."""
    target = name.lower()
    try:
        for entry in os.listdir(folder):
            if entry.lower() == target:
                return os.path.join(folder, entry)
    except OSError:
        pass
    return None


def chain(client_dir, locale=None):
    """Every archive the client loads, lowest priority first."""
    data = pathlib.Path(client_dir) / "Data"
    if not data.is_dir():
        raise ClientError("%s has no Data directory; is that a WoW client?" % client_dir)
    if locale is None:
        found = locales(client_dir)
        if not found:
            raise ClientError("%s has no locale folder (enUS and friends)" % client_dir)
        locale = found[0]
    loc_dir = data / locale

    ordered = []

    def add(folder, name, tier):
        path = _exists(str(folder), name)
        if path:
            ordered.append((path, tier))

    for name in GENERIC_BASE:
        add(data, name, "base")
    for name in LOCALE_BASE:
        add(loc_dir, name % locale, "base (locale)")
    for name in GENERIC_NUMBERED:
        add(data, name, "patch")
    for name in LOCALE_NUMBERED:
        add(loc_dir, name % locale, "patch (locale)")
    # The custom slots load last. A locale custom patch is outranked by a generic
    # one, so the generic list is walked second and wins.
    for suffix in CUSTOM_SUFFIXES:
        add(loc_dir, "patch-%s-%s.MPQ" % (locale, suffix), "custom (locale)")
    for suffix in CUSTOM_SUFFIXES:
        add(data, "patch-%s.MPQ" % suffix, "custom")

    return [(path, tier, rank) for rank, (path, tier) in enumerate(ordered)], locale


def resolve(client_dir, internal_path=SPELL_DBC, locale=None, exclude=()):
    """Which archive supplies `internal_path`, and what every other copy is.

    Returns (winner, contents, candidates). `candidates` is in load order, so
    the last one is the winner and the rest are what it overrides.
    """
    ordered, locale = chain(client_dir, locale)
    # A patch we wrote ourselves sits at the top of the chain. Rebuilding has to
    # ignore it, or the next patch would be built on top of the last one.
    skip = {os.path.normcase(os.path.abspath(p)) for p in exclude}
    candidates = []
    for path, tier, rank in ordered:
        if os.path.normcase(os.path.abspath(path)) in skip:
            continue
        try:
            with mpq.Archive(path) as archive:
                if not archive.has(internal_path):
                    continue
                blob = archive.read(internal_path)
        except mpq.MpqError:
            continue
        found = Candidate(path, tier, rank)
        if len(blob) >= 12 and blob[:4] == b"WDBC":
            found.records = int.from_bytes(blob[4:8], "little")
            found.fields = int.from_bytes(blob[8:12], "little")
        found.blob = blob
        candidates.append(found)
    if not candidates:
        raise ClientError("No archive in %s holds %s" % (client_dir, internal_path))
    winner = candidates[-1]
    return winner, winner.blob, candidates


def check(client_dir):
    """Whether a folder is a client we can work with. Returns (ok, message, details)."""
    if not (client_dir or "").strip():
        return False, "No client folder set.", []
    where = pathlib.Path(client_dir)
    if not where.is_dir():
        return False, "There is no folder at %s." % where, []
    if not (where / "Data").is_dir():
        return (False, "%s has no Data folder, so it is not a WoW client." % where, [])
    try:
        found = locales(client_dir)
    except ClientError as exc:
        return False, str(exc), []
    if not found:
        return (False, "%s/Data has no locale folder such as enUS." % where, [])
    ordered, locale = chain(client_dir, None)
    details = ["%d archives, locale %s" % (len(ordered), locale)]
    try:
        winner, _blob, candidates = resolve(client_dir, SPELL_DBC, locale)
        details.append("Spell.dbc comes from %s (%s records)"
                       % (winner.name, winner.records))
        if len(candidates) > 1:
            details.append("%d older copies are overridden" % (len(candidates) - 1))
    except ClientError as exc:
        return False, "No archive in %s holds Spell.dbc. %s" % (where, exc), details
    return True, "%s looks like a 3.3.5a client." % where, details


def free_slot(client_dir, locale=None):
    """The free patch slot that outranks every archive already loaded.

    Taking the highest free slot is what makes the patch win: a slot below an
    archive that holds the same file would simply be overridden. The candidates
    are walked in reverse load order, so the first free one is the best one.
    """
    data = pathlib.Path(client_dir) / "Data"
    ordered, locale = chain(client_dir, locale)
    taken = {os.path.basename(p).lower() for p, _, _ in ordered}
    # In load order, so walking it backwards gives the strongest slot first.
    candidates = ([str(data / locale / ("patch-%s-%s.MPQ" % (locale, suffix)))
                   for suffix in CUSTOM_SUFFIXES]
                  + [str(data / ("patch-%s.MPQ" % suffix)) for suffix in CUSTOM_SUFFIXES])
    for path in reversed(candidates):
        if os.path.basename(path).lower() not in taken:
            return path, locale
    raise ClientError("Every custom patch slot in %s is taken" % client_dir)
