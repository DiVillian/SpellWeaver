"""Snapping plain numbers onto DBC index tables.

Cast time, duration, range and radius are not stored on a spell as numbers.
Each is an index into a small companion DBC (SpellCastTimes, SpellDuration,
SpellRange, SpellRadius) which the *server* loads from its own data directory.
We cannot invent a row in those tables without shipping a client patch, so a
value the user types has to snap to the nearest one that already exists.

The tables are dense enough that this is rarely felt: 88 durations, 58 cast
times. But when a value does move, the UI says so rather than quietly writing
something the user did not ask for.
"""
import pathlib

from . import dbc

DATA = pathlib.Path(__file__).resolve().parent.parent / "data/dbc"


class IndexTable:
    """One DBC that maps an index to a value, with nearest-value lookup."""

    def __init__(self, pairs, unit, name):
        # pairs: [(index, value)], already filtered to usable rows
        self.by_index = dict(pairs)
        self.sorted = sorted(pairs, key=lambda p: p[1])
        self.unit = unit
        self.name = name

    def value(self, index):
        return self.by_index.get(index, 0)

    def snap(self, wanted):
        """Nearest available (index, value) to `wanted`."""
        best = min(self.sorted, key=lambda p: abs(p[1] - wanted))
        return best

    def options(self):
        return list(self.sorted)


def _load(data_dir=DATA):
    d = pathlib.Path(data_dir)

    cast = dbc.Dbc.read(d / "SpellCastTimes.dbc")
    # Negative base times are sentinels used by a handful of internal spells.
    cast_pairs = [(r[0], r[1]) for r in cast if r[1] >= 0]

    dur = dbc.Dbc.read(d / "SpellDuration.dbc")
    # Index 21 is the "infinite" row (-1); it is offered separately, not snapped to.
    dur_pairs = [(r[0], r[1]) for r in dur if 0 <= r[1] <= 3600000]

    rng = dbc.Dbc.read(d / "SpellRange.dbc")
    # Field 3 is MaxRangeHostile. Skip the 50000y "anywhere" rows.
    rng_pairs = [(r[0], dbc.as_float(r[3])) for r in rng
                 if 0 <= dbc.as_float(r[3]) <= 1000]

    rad = dbc.Dbc.read(d / "SpellRadius.dbc")
    rad_pairs = [(r[0], dbc.as_float(r[1])) for r in rad
                 if 0 < dbc.as_float(r[1]) <= 200]

    return {
        "cast_time": IndexTable(cast_pairs, "ms", "cast time"),
        "duration": IndexTable(dur_pairs, "ms", "duration"),
        "range": IndexTable(rng_pairs, "yd", "range"),
        "radius": IndexTable(rad_pairs, "yd", "radius"),
    }


_TABLES = None


def tables():
    global _TABLES
    if _TABLES is None:
        _TABLES = _load()
    return _TABLES


def snap(kind, wanted):
    """Snap `wanted` to the nearest available value for `kind`.

    Returns (index, actual_value, moved) where `moved` is True if the value had
    to change, so the caller can tell the user.
    """
    table = tables()[kind]
    index, value = table.snap(wanted)
    moved = abs(value - wanted) > (0.01 if isinstance(value, float) else 0)
    return index, value, moved
