"""A spell as the user describes it, and as the database needs it.

`SpellDef` is what the form collects: a name, an icon, some behaviours and their
parameters. `resolve()` turns that into a `ResolvedSpell`, which holds all 234
Spell.dbc fields with their real values.

Targeting is per effect, not per spell. Each of the three slots has its own
ImplicitTarget pair in the DBC, which is what lets one spell damage an enemy in
the first slot and heal its caster in the second. A behaviour with no target of
its own uses the spell's primary one, so a spell that never touches this stays
simple.

Everything downstream reads the resolved fields, never the form. The SQL is
generated from them and so is the tooltip, which means the tooltip is a
rendering of what will actually be written. If the two ever disagree, the
tooltip shows it on screen rather than the player discovering it in game.
"""
import copy
import json
from dataclasses import dataclass, field, asdict

from .behaviours import BY_KEY as BEHAVIOUR_BY_KEY
from .domains import PARAM_DOMAINS
from .indices import snap, tables
from .spell_layout import FIELDS, FLOAT_FIELDS
from .targeting import BY_KEY as TARGET_BY_KEY, DEFAULT as DEFAULT_TARGET, implicit_for

MAX_EFFECTS = 3

# AttributesEx4. The core's own title for this bit is "Deals fixed damage": it
# skips armour and the target's damage-taken modifiers.
ATTR4_IGNORE_MITIGATION = 0x00000100

# The global cooldown category and length every active spell uses.
GCD_CATEGORY = 133
GCD_MS = 1500

SCHOOL_NAMES = {1: "Physical", 2: "Holy", 4: "Fire", 8: "Nature",
                16: "Frost", 32: "Shadow", 64: "Arcane"}
POWER_NAMES = {0: "Mana", 1: "Rage", 2: "Focus", 3: "Energy",
               6: "Runic Power", -2: "Health"}


@dataclass
class BehaviourUse:
    """One behaviour placed in a spell, with the values the user chose."""
    key: str
    amount: int = 0
    period_ms: int = 0
    chain: int = 0
    misc: int = 0
    trigger: int = 0
    # The share of this effect that comes back to the caster, for the
    # behaviours that leech. Zero means nothing comes back.
    share: float = 0.0
    # Each effect slot carries its own target, so one spell can damage an enemy
    # and heal the caster. Empty means the spell's primary target.
    target: str = ""

    @property
    def behaviour(self):
        return BEHAVIOUR_BY_KEY[self.key]


@dataclass
class RankValues:
    """What one rank of an ability changes.

    Everything else - school, targeting, animation, who teaches it - belongs to
    the ability, not to a rank of it. A rank is the same spell stepped up.
    """
    spell_level: int = 1
    power_cost: int = 0
    amounts: list = field(default_factory=list)     # one per effect, in order
    price_copper: int = -1                          # -1 takes the going rate


@dataclass
class SpellDef:
    name: str = "New Spell"
    description: str = ""
    icon_id: int = 1
    # A spell belongs to a class from the moment it exists, not from the moment
    # a trainer is assigned. 0 means any class may learn it.
    class_id: int = 0
    # Which of the class's trees it belongs to, and so which tab of the
    # spellbook it appears under. 0 takes the class's busiest.
    skill_line: int = 0
    school: int = 2
    power_type: int = 0
    power_cost: int = 0
    cast_time_ms: int = 0
    cooldown_ms: int = 0
    range_yards: float = 30.0
    target: str = DEFAULT_TARGET
    radius_yards: float = 8.0
    duration_ms: int = 0
    spell_level: int = 1
    # How much of the caster's own power is added, as a percentage. The stat is
    # the game's choice, not ours: spell power for magic, attack power for
    # physical.
    power_scaling: float = 0.0
    # How far either side of the amount a hit can land, as a percentage.
    spread_pct: int = 0
    ignore_mitigation: bool = False
    visual_id: int = 0
    spell_id: int = None
    behaviours: list = field(default_factory=list)
    # Ranks 2 and up. Rank 1 is the ability itself, so an ability with no extra
    # ranks is exactly what it was before ranks existed.
    extra_ranks: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        uses = [BehaviourUse(**{k: v for k, v in b.items()
                                if k in BehaviourUse.__dataclass_fields__})
                for b in data.get("behaviours", [])]
        extra = [RankValues(**{k: v for k, v in r.items()
                               if k in RankValues.__dataclass_fields__})
                 for r in data.get("extra_ranks", [])]
        known = {k: v for k, v in data.items()
                 if k in cls.__dataclass_fields__
                 and k not in ("behaviours", "extra_ranks")}
        return cls(behaviours=uses, extra_ranks=extra, **known)

    def rank_count(self):
        return 1 + len(self.extra_ranks)

    def to_dict(self):
        return asdict(self)


class ResolvedSpell:
    """A SpellDef expanded into real Spell.dbc field values."""

    def __init__(self, definition, spell_id, fields_, notes, warnings,
                 rank=1, ranks_total=1):
        self.definition = definition
        self.spell_id = spell_id
        self.fields = fields_
        self.notes = notes
        self.warnings = warnings
        self.rank = rank
        self.ranks_total = ranks_total

    @property
    def rank_text(self):
        """The line under the name. Blank unless the ability really has ranks."""
        return "Rank %d" % self.rank if self.ranks_total > 1 else ""

    # -- the values actually written, read back for display ---------------
    def value(self, name):
        return self.fields[name]

    def cast_time_ms(self):
        return tables()["cast_time"].value(self.fields["CastingTimeIndex"])

    def duration_ms(self):
        return tables()["duration"].value(self.fields["DurationIndex"])

    def range_yards(self):
        return tables()["range"].value(self.fields["RangeIndex"])

    def radius_yards(self, slot=1):
        return tables()["radius"].value(self.fields["EffectRadiusIndex_%d" % slot])

    def effect_amount(self, slot):
        """The most this effect can do: BasePoints + DieSides."""
        return self.fields["EffectBasePoints_%d" % slot] + self.fields["EffectDieSides_%d" % slot]

    def effect_range(self, slot):
        """(least, most) this effect can do. They are equal when it never varies."""
        base = self.fields["EffectBasePoints_%d" % slot]
        die = self.fields["EffectDieSides_%d" % slot]
        return base + 1, base + die

    def effect_average(self, slot):
        low, high = self.effect_range(slot)
        return (low + high) // 2

    def has_direct(self):
        """Whether anything here lands at once, rather than over time."""
        return any(not self.fields["EffectAuraPeriod_%d" % s] for s in self.active_slots())

    def has_periodic(self):
        return any(self.fields["EffectAuraPeriod_%d" % s] for s in self.active_slots())

    def active_slots(self):
        return [s for s in (1, 2, 3) if self.fields["Effect_%d" % s]]

    def aim_for(self, slot):
        """The target key this effect slot landed on."""
        aims = getattr(self, "aims", [])
        return aims[slot - 1] if slot - 1 < len(aims) else None


def resolve(definition, vocabulary=None, spell_id=None, rank=1, ranks_total=1):
    """Expand a definition into every Spell.dbc field, with notes and warnings."""
    notes, warnings = [], []
    f = {name: 0 for name in FIELDS}
    for name in FLOAT_FIELDS:
        f[name] = 0.0

    spell_id = spell_id if spell_id is not None else definition.spell_id
    f["ID"] = spell_id or 0

    uses = list(definition.behaviours)[:MAX_EFFECTS]
    if len(definition.behaviours) > MAX_EFFECTS:
        warnings.append("A spell has only three effect slots; the extra ones were dropped.")
    primary = TARGET_BY_KEY.get(definition.target) or TARGET_BY_KEY[DEFAULT_TARGET]
    # One aim per effect slot, falling back to the spell's primary target.
    aims = [TARGET_BY_KEY.get(use.target) or primary for use in uses]
    every_aim = aims or [primary]

    needs_duration = any(u.behaviour.shape in ("aura", "periodic", "ground") for u in uses)
    is_passive_only = bool(uses) and all(
        u.behaviour.shape in ("aura", "periodic") for u in uses) and not definition.cast_time_ms

    # --- framing ------------------------------------------------------
    f["SchoolMask"] = definition.school
    f["DispelType"] = 1 if definition.school != 1 else 0   # magic schools are dispellable
    f["DefenseType"] = 1 if definition.school != 1 else 2  # magic vs melee
    f["PreventionType"] = 1 if definition.school != 1 else 2
    f["EquippedItemClass"] = -1
    # 49,456 of the 49,839 stock spells carry 1.0 here. Left at zero, an effect
    # that chains to further targets would scale its value away to nothing.
    for slot in (1, 2, 3):
        f["EffectChainAmplitude_%d" % slot] = 1.0
    f["SpellClassSet"] = 0        # generic: no class talent modifiers apply
    f["SpellLevel"] = definition.spell_level
    f["BaseLevel"] = definition.spell_level
    f["MaxLevel"] = 0
    f["ProcChance"] = 101
    f["SpellIconID"] = definition.icon_id
    f["ActiveIconID"] = definition.icon_id
    f["Attributes"] = 0x00010000   # not usable while shapeshifted, as most spells set
    if definition.ignore_mitigation:
        f["AttributesEx4"] |= ATTR4_IGNORE_MITIGATION
    f["SpellVisualID_1"] = max(0, int(definition.visual_id or 0))
    f["InterruptFlags"] = 15 if definition.cast_time_ms else 8
    # The reticle flag belongs to the spell, not an effect, so one ground-aimed
    # effect is enough to need it.
    f["Targets"] = primary.flags if not aims else 0
    for aim in every_aim:
        f["Targets"] |= aim.flags

    # --- cost and timing ----------------------------------------------
    f["PowerType"] = definition.power_type
    f["ManaCost"] = max(0, definition.power_cost)
    f["RecoveryTime"] = max(0, definition.cooldown_ms)
    f["StartRecoveryCategory"] = GCD_CATEGORY
    f["StartRecoveryTime"] = GCD_MS

    index, value, moved = snap("cast_time", max(0, definition.cast_time_ms))
    f["CastingTimeIndex"] = index
    if moved:
        notes.append("Cast time snapped to %.2gs; the client only knows fixed cast times."
                     % (value / 1000.0))

    wanted_range = definition.range_yards if any(_reaches(a) for a in every_aim) else 0.0
    index, value, moved = snap("range", max(0.0, wanted_range))
    f["RangeIndex"] = index
    if moved and wanted_range:
        notes.append("Range snapped to %gyd." % value)

    if needs_duration:
        index, value, moved = snap("duration", max(1, definition.duration_ms))
        f["DurationIndex"] = index
        if moved:
            notes.append("Duration snapped to %.3gs." % (value / 1000.0))
    elif definition.duration_ms:
        notes.append("Duration ignored: none of these behaviours last.")

    radius_index = 0
    if any(a.area for a in every_aim):
        radius_index, value, moved = snap("radius", max(0.5, definition.radius_yards))
        if moved:
            notes.append("Radius snapped to %gyd." % value)

    # --- effects --------------------------------------------------------
    for i, use in enumerate(uses, start=1):
        b = use.behaviour
        effect = b.effect
        f["Effect_%d" % i] = effect
        f["EffectAura_%d" % i] = b.aura

        aim = aims[i - 1]
        a, bb = implicit_for(aim, b)
        f["ImplicitTargetA_%d" % i] = a
        f["ImplicitTargetB_%d" % i] = bb

        if "amount" in b.params:
            # value = BasePoints + irand(1, DieSides). A one-sided die is a flat
            # amount; widening it is what gives a spell its range.
            low, high = _spread(int(use.amount), definition.spread_pct)
            f["EffectBasePoints_%d" % i] = low - 1
            f["EffectDieSides_%d" % i] = high - low + 1
        if "period" in b.params:
            f["EffectAuraPeriod_%d" % i] = max(0, int(use.period_ms))
        if "chain" in b.params:
            f["EffectChainTargets_%d" % i] = max(0, int(use.chain))
        if "trigger" in b.params:
            f["EffectTriggerSpell_%d" % i] = max(0, int(use.trigger))
        for name in b.params:
            if name in PARAM_DOMAINS:
                f["EffectMiscValue_%d" % i] = int(use.misc)
        if "heal_share" in b.params or "gain_share" in b.params or "burn_ratio" in b.params:
            # The share is the whole point of a leech; left at zero the effect
            # damages and returns nothing.
            f["EffectMultipleValue_%d" % i] = float(use.share or 0.0)
        if aim.area or b.shape == "ground":
            f["EffectRadiusIndex_%d" % i] = radius_index or snap("radius", definition.radius_yards)[0]

        if ("heal_share" in b.params or "gain_share" in b.params) and not use.share:
            warnings.append("%s returns nothing to the caster while its share is zero."
                            % b.label)
        if "amount" in b.params and int(use.amount) == 0:
            warnings.append("%s has an amount of zero, so it will do nothing." % b.label)
        if "trigger" in b.params and not int(use.trigger):
            warnings.append("%s needs the id of a spell to cast." % b.label)

    if not uses:
        warnings.append("The spell has no behaviours, so it does nothing.")
    if needs_duration and not definition.duration_ms:
        warnings.append("These behaviours last for a while, but no duration is set.")
    for use in uses:
        b = use.behaviour
        if b.shape == "periodic" and "period" in b.params and not use.period_ms:
            warnings.append("%s ticks, but no time between ticks is set." % b.label)

    if vocabulary is not None:
        warnings.extend(vocabulary.check_combination([u.key for u in uses]))

    resolved = ResolvedSpell(definition, spell_id, f, notes, warnings,
                             rank=rank, ranks_total=ranks_total)
    resolved.aims = [a.key for a in aims]
    resolved.warnings.extend(_target_warnings(aims, uses))
    return resolved


def rank_definitions(definition):
    """One definition per rank, rank 1 first.

    Rank 1 is the ability as it stands; each later rank is a copy with its own
    level, cost and amounts. Everything else is shared, because a rank is the
    same ability stepped up rather than a different one.
    """
    out = [(1, definition)]
    for rank, values in enumerate(definition.extra_ranks, start=2):
        clone = copy.deepcopy(definition)
        clone.extra_ranks = []
        clone.spell_id = None
        clone.spell_level = int(values.spell_level or definition.spell_level)
        clone.power_cost = int(values.power_cost or 0)
        for index, amount in enumerate(values.amounts or []):
            if index < len(clone.behaviours):
                clone.behaviours[index].amount = int(amount)
        out.append((rank, clone))
    return out


def next_rank_values(definition):
    """A new rank starts where the last one left off, on the way up."""
    if definition.extra_ranks:
        last = definition.extra_ranks[-1]
        return RankValues(spell_level=last.spell_level, power_cost=last.power_cost,
                          amounts=list(last.amounts), price_copper=-1)
    return RankValues(spell_level=definition.spell_level,
                      power_cost=definition.power_cost,
                      amounts=[int(u.amount) for u in definition.behaviours],
                      price_copper=-1)


def _spread(amount, percent):
    """The least and most one application can do, from an amount and a spread."""
    if amount <= 0:
        return amount, amount
    percent = max(0, min(100, int(percent or 0)))
    half = int(round(amount * percent / 100.0))
    return max(1, amount - half), amount + half


def _reaches(targeting):
    """Whether this aim needs the spell to have a range at all."""
    return targeting.family == "ground" or (targeting.family == "unit"
                                            and targeting.key != "self")


def _target_warnings(aims, uses):
    out = []
    for aim, use in zip(aims, uses):
        b = use.behaviour
        if b.shape == "ground" and aim.family != "ground":
            out.append(
                "%s leaves a patch on the ground, so it needs a target the player "
                "clicks. Choose 'A spot you choose'." % b.label)
    return out
