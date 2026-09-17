"""Deriving the vocabulary, its defaults and its validator from the corpus.

Three things come out of here:

1. The behaviour list - curated names in behaviours.py, joined to evidence of
   how often each one is actually used and by which spells.
2. Defaults per behaviour - the typical tick rate, radius, duration, amount and
   spell power coefficient, taken as medians over the spells that use it, so a
   new "Damage over time" starts out shaped like the game's other DoTs.
3. A validator - every (effect, aura) pair and every behaviour pairing that
   appears anywhere in the corpus. A combination attested nowhere in ~50k
   spells is not necessarily wrong, but it is worth a warning.

Defaults are drawn from player-facing spells (those on a skill line) where
there are enough of them, because NPC and internal spells are tuned very
differently and would drag the numbers somewhere unhelpful.
"""
import collections
import statistics

from .behaviours import AREA_AURA_EFFECTS, BEHAVIOURS, BY_KEY, WITHHELD
from .coredefs import effect_support, aura_support, WORKS, SCRIPT, DEAD
from .corpus import EFFECT_SLOTS
from .enums import SPELL_EFFECTS, SPELL_AURAS, SPELL_TARGETS

MIN_PLAYER_SAMPLES = 5

# Parameters that are all the same DBC field - the share of an effect that comes
# back to the caster - worded differently depending on what is being shared.
SHARE_PARAMS = ("heal_share", "gain_share", "burn_ratio")

# Names that mark a spell as not shipped content.
_JUNK = ("OLD", "TEST", "DND", "UNUSED", "Unused", "zz", "QA ", "[PH]", "PH ")


def _is_clean(name):
    return bool(name) and not any(j in name for j in _JUNK)


def _median(values):
    return statistics.median(values) if values else None


class Observation:
    """Everything the corpus has to say about one (effect, aura) pair."""

    def __init__(self, effect, aura):
        self.effect = effect
        self.aura = aura
        self.count_all = 0
        self.count_player = 0
        self.examples = []
        # Samples are kept twice: all spells, and player-facing spells only.
        self.samples = {"all": collections.defaultdict(list),
                        "player": collections.defaultdict(list)}
        self.targets = collections.Counter()
        self.schools = collections.Counter()

    def record(self, spell, slot, corpus, is_player):
        self.count_all += 1
        buckets = [self.samples["all"]]
        if is_player:
            self.count_player += 1
            buckets.append(self.samples["player"])
            if len(self.examples) < 6 and _is_clean(spell.name):
                self.examples.append((spell.id, spell.name))

        amount = spell.base_points(slot)
        period = spell["EffectAuraPeriod_%d" % slot]
        radius = spell.radius(slot)
        duration = spell.duration_ms()
        chain = spell["EffectChainTargets_%d" % slot]
        # EffectMultipleValue: what share of an effect comes back to the caster,
        # and the only thing that makes a leech leech anything.
        multiple = spell["EffectMultipleValue_%d" % slot]
        bonus = corpus.bonus.get(spell.id)
        for bucket in buckets:
            if amount:
                bucket["amount"].append(amount)
            if period:
                bucket["period"].append(period)
            if radius:
                bucket["radius"].append(radius)
            if duration > 0:
                bucket["duration"].append(duration)
            if chain:
                bucket["chain"].append(chain)
            if multiple:
                bucket["multiple"].append(multiple)
            if bonus:
                if bonus["direct"]:
                    bucket["coeff_direct"].append(bonus["direct"])
                if bonus["dot"]:
                    bucket["coeff_dot"].append(bonus["dot"])

        self.targets[(spell["ImplicitTargetA_%d" % slot],
                      spell["ImplicitTargetB_%d" % slot])] += 1
        self.schools[spell["SchoolMask"]] += 1

    def typical(self, name):
        """Median of `name`, preferring player spells when there are enough."""
        player = self.samples["player"][name]
        if len(player) >= MIN_PLAYER_SAMPLES:
            return _median(player), "player", len(player)
        every = self.samples["all"][name]
        return _median(every), "all", len(every)


class Vocabulary:
    def __init__(self, corpus):
        self.corpus = corpus
        self.effect_support = effect_support()
        self.aura_support = aura_support()
        self.observations = {}
        self.pair_counts = collections.Counter()
        self.cooccurrence = collections.Counter()
        self._scan()

    def _scan(self):
        for spell in self.corpus.spells:
            is_player = self.corpus.is_player_spell(spell.id)
            present = []
            for slot in EFFECT_SLOTS:
                effect, aura = spell.effect(slot)
                if effect == 0:
                    continue
                key = (effect, aura)
                self.pair_counts[key] += 1
                present.append(key)
                obs = self.observations.get(key)
                if obs is None:
                    obs = self.observations[key] = Observation(effect, aura)
                obs.record(spell, slot, self.corpus, is_player)
            for i, a in enumerate(present):
                for b in present[i + 1:]:
                    self.cooccurrence[tuple(sorted((a, b)))] += 1

    # -- lookups --------------------------------------------------------
    def observation(self, behaviour):
        return self.observations.get((behaviour.effect, behaviour.aura))

    def support(self, effect, aura):
        """Worst-case support level across the effect and its aura."""
        # An unclassified id means the core was not there to ask, which is not
        # evidence against it.
        levels = [self.effect_support.get(effect, WORKS)]
        if aura:
            levels.append(self.aura_support.get(aura, WORKS))
        for level in (DEAD, SCRIPT, WORKS):
            if level in levels:
                return level
        return DEAD

    def wanted_stats(self, behaviour):
        """Which statistics are meaningful for this behaviour.

        Only the parameters a behaviour actually declares are reported. Without
        this the numbers lie: a Stun shares its spell with a damage effect often
        enough that it would otherwise be handed that effect's spell power
        coefficient, and a median "amount" for a behaviour that has no amount.
        """
        wanted = [p for p in behaviour.params if p in ("amount", "period", "chain")]
        # A behaviour that returns a share of itself to the caster needs to know
        # the usual share, or it silently returns nothing.
        if any(p in SHARE_PARAMS for p in behaviour.params):
            wanted.append("multiple")
        # Radius is never a behaviour parameter (the target decides how wide a
        # spell reaches) but the corpus median still seeds the radius field, so
        # a ground-targeted spell starts at a sane size for what it does.
        wanted.append("radius")
        if behaviour.shape in ("aura", "periodic", "ground"):
            wanted.append("duration")
        if behaviour.category in ("Damage", "Healing"):
            # Direct effects scale off the direct coefficient, ticking ones off
            # the damage-over-time coefficient.
            wanted.append("coeff_dot" if behaviour.shape in ("periodic", "ground")
                          else "coeff_direct")
        return wanted

    def defaults(self, behaviour):
        """The starting values a new spell using this behaviour should get."""
        obs = self.observation(behaviour)
        if obs is None:
            return {}
        out = {}
        for name in self.wanted_stats(behaviour):
            value, source, n = obs.typical(name)
            if value is None:
                continue
            if name.startswith("coeff"):
                value = round(value, 4)
            elif name == "multiple":
                # A share is a fraction: 0.15 must not round to nothing.
                value = round(value, 3)
            elif name == "radius":
                value = round(value, 1)
            else:
                value = int(round(value))
            out[name] = {"value": value, "from": source, "samples": n}
        return out

    def target_shapes(self, behaviour, limit=5):
        obs = self.observation(behaviour)
        if obs is None:
            return []
        out = []
        for (a, b), n in obs.targets.most_common(limit):
            out.append({
                "a": a, "b": b, "count": n,
                "label": _target_label(a, b),
            })
        return out

    # -- validation -----------------------------------------------------
    def is_attested(self, effect, aura):
        return (effect, aura) in self.pair_counts

    def check_combination(self, keys):
        """Warn about behaviour combinations the corpus has never seen."""
        warnings = []
        pairs = []
        for key in keys:
            behaviour = BY_KEY.get(key)
            if behaviour is None:
                warnings.append("Unknown behaviour %r." % key)
                continue
            pairs.append((behaviour, (behaviour.effect, behaviour.aura)))
            level = self.support(behaviour.effect, behaviour.aura)
            if level == SCRIPT:
                warnings.append(
                    "%s needs a C++ script to do anything." % behaviour.label)
            elif level == DEAD:
                warnings.append(
                    "%s is not implemented by the server." % behaviour.label)
        if len(pairs) > 3:
            warnings.append(
                "A spell has only three effect slots; %d behaviours were given."
                % len(pairs))
        for i, (ba, pa) in enumerate(pairs):
            for bb, pb in pairs[i + 1:]:
                if pa == pb:
                    continue
                if not self.cooccurrence[tuple(sorted((pa, pb)))]:
                    warnings.append(
                        "No shipped spell combines %s with %s. This may still "
                        "work, but nothing in the game does it."
                        % (ba.label, bb.label))
        return warnings

    # -- reporting ------------------------------------------------------
    def covered_pairs(self):
        """Every (effect, aura) pair the vocabulary can express.

        An aura behaviour covers more than its own pair: the same aura applied
        through any of the APPLY_AREA_AURA_* effects is the same behaviour with
        a different application mode, so those pairs are covered too.
        """
        covered = set()
        for behaviour in BEHAVIOURS:
            covered.add((behaviour.effect, behaviour.aura))
            if behaviour.effect == 6 and behaviour.aura:
                for effect in AREA_AURA_EFFECTS:
                    covered.add((effect, behaviour.aura))
        return covered

    def coverage(self, corpus):
        """How much of the player-facing corpus the vocabulary accounts for."""
        covered = self.covered_pairs()
        total = named = withheld = 0
        for spell in corpus.spells:
            if not corpus.is_player_spell(spell.id):
                continue
            for slot in EFFECT_SLOTS:
                effect, aura = spell.effect(slot)
                if effect == 0:
                    continue
                total += 1
                if (effect, aura) in covered:
                    named += 1
                elif (effect, aura) in WITHHELD:
                    withheld += 1
        return {"total": total, "named": named, "withheld": withheld,
                "accounted": named + withheld}

    def unnamed_common_pairs(self, threshold=40):
        """Frequent, working pairs that the curated vocabulary does not cover.

        This is the honest measure of how much of the game the vocabulary can
        express. Anything listed here is a gap, or a deliberate omission.
        """
        covered = self.covered_pairs()
        out = []
        for key, count in self.pair_counts.most_common():
            if key in covered or count < threshold:
                continue
            if self.support(*key) != WORKS:
                continue
            out.append((key, count, WITHHELD.get(key)))
        return out


def _target_label(a, b):
    a_name = SPELL_TARGETS.get(a, str(a)) if a else "-"
    b_name = SPELL_TARGETS.get(b, str(b)) if b else None
    return a_name if not b_name else "%s + %s" % (a_name, b_name)


def pair_label(effect, aura):
    if aura:
        return "%s / %s" % (SPELL_EFFECTS.get(effect, effect), SPELL_AURAS.get(aura, aura))
    return SPELL_EFFECTS.get(effect, str(effect))
