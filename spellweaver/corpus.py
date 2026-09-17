"""The existing spell corpus, loaded, filtered and indexed.

This is the evidence base for everything the tool does: the vocabulary of
behaviours, the defaults for each one, and the validator are all derived from
what the shipped spells actually do.

Only *stock* spell data is admitted. Modules add their own spells in high id
bands (this server's mod-era-talents holds ~3,600 rows above 900000), and those
are one author's recreations of other expansions, not Blizzard's data. Learning
defaults from them would tune the tool to one install. The filter also guards
against a subtler loop: this tool's own step 4 emits a client patch, so DBCs
re-extracted from a patched client would otherwise feed our invented spells back
in as evidence for what a spell should look like.
"""
import pathlib

from . import dbc
from .config import DEFAULT_STOCK_CEILING
from .spell_layout import FIELDS, INDEX, FLOAT_FIELDS

DATA = pathlib.Path(__file__).resolve().parent.parent / "data/dbc"

EFFECT_SLOTS = (1, 2, 3)


class Spell:
    """A single Spell.dbc row, addressed by field name."""

    __slots__ = ("_row", "_corpus")

    def __init__(self, row, corpus):
        self._row = row
        self._corpus = corpus

    def __getitem__(self, field):
        raw = self._row[INDEX[field]]
        if field in FLOAT_FIELDS:
            return dbc.as_float(raw)
        return raw

    @property
    def id(self):
        return self._row[0]

    @property
    def name(self):
        return self._corpus.spell.string_at(self._row[INDEX["Name_Lang_enUS"]])

    def effect(self, slot):
        """The (effect id, aura id) pair in one of the three effect slots."""
        return (self["Effect_%d" % slot], self["EffectAura_%d" % slot])

    def base_points(self, slot):
        """EffectBasePoints is stored one below its real value in the DBC."""
        return self["EffectBasePoints_%d" % slot] + 1

    def duration_ms(self):
        return self._corpus.duration(self["DurationIndex"])

    def radius(self, slot):
        return self._corpus.radius(self["EffectRadiusIndex_%d" % slot])

    def cast_time_ms(self):
        return self._corpus.cast_time(self["CastingTimeIndex"])

    def range_yards(self):
        return self._corpus.range_max(self["RangeIndex"])


class Corpus:
    def __init__(self, data_dir=DATA, db=None, stock_ceiling=DEFAULT_STOCK_CEILING):
        d = pathlib.Path(data_dir)
        self.stock_ceiling = stock_ceiling
        self.spell = dbc.Dbc.read(d / "Spell.dbc")
        self._durations = {r[0]: r[1] for r in dbc.Dbc.read(d / "SpellDuration.dbc")}
        self._radii = {r[0]: dbc.as_float(r[1]) for r in dbc.Dbc.read(d / "SpellRadius.dbc")}
        self._cast_times = {r[0]: r[1] for r in dbc.Dbc.read(d / "SpellCastTimes.dbc")}
        # SpellRange: field 3 is MaxRangeHostile.
        self._ranges = {r[0]: dbc.as_float(r[3]) for r in dbc.Dbc.read(d / "SpellRange.dbc")}
        icons = dbc.Dbc.read(d / "SpellIcon.dbc")
        self.icons = {r[0]: icons.string_at(r[1]) for r in icons}

        # Every source is filtered to stock ids, and what was dropped is kept so
        # the report can state it rather than quietly discarding data.
        self.excluded = {}
        all_rows = self.spell.records
        stock_rows = [r for r in all_rows if r[0] < stock_ceiling]
        self.excluded["spells"] = len(all_rows) - len(stock_rows)
        self.spells = [Spell(r, self) for r in stock_rows]
        self.by_id = {s.id: s for s in self.spells}

        sla = dbc.Dbc.read(d / "SkillLineAbility.dbc")
        all_skill = {r[2] for r in sla}
        self.player_spell_ids = {i for i in all_skill if i < stock_ceiling}
        self.excluded["skill_line_spells"] = len(all_skill) - len(self.player_spell_ids)

        self.bonus = {}
        if db is not None:
            self.load_bonus(db)

    def load_bonus(self, db):
        """Spell power coefficients live in the world DB, not the DBC.

        This is the one source read from the live server, so it is the one most
        likely to carry a module's rows; it gets the same stock filter.
        """
        _, rows = db.query(
            "SELECT entry, direct_bonus, dot_bonus, ap_bonus FROM spell_bonus_data")
        self.bonus = {r[0]: {"direct": r[1], "dot": r[2], "ap": r[3]}
                      for r in rows if r[0] < self.stock_ceiling}
        self.excluded["bonus_rows"] = len(rows) - len(self.bonus)

    def duration(self, index):
        return self._durations.get(index, 0)

    def radius(self, index):
        return self._radii.get(index, 0.0)

    def cast_time(self, index):
        return self._cast_times.get(index, 0)

    def range_max(self, index):
        return self._ranges.get(index, 0.0)

    def is_player_spell(self, spell_id):
        return spell_id in self.player_spell_ids

    def max_stock_id(self):
        return max(s.id for s in self.spells)
