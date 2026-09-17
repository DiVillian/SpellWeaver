"""Animations, named after spells people recognise.

A spell's animation is `SpellVisualID`, an integer with no meaning on its own:
67 is not "fireball" in any readable sense, it is just the row that happens to
hold that missile, its impact and its cast animation.

But every stock spell points at one, so the corpus can name them. Group the
spells by the visual they use and the id becomes "Fireball" or "Shadow Bolt" -
something the user has already seen in the game and can pick deliberately.

Two things make the list usable rather than exhaustive:

* One entry per visual. Thousands of spells share a few hundred animations, and
  ten entries that all look identical is a worse list than one.
* The label is the most recognisable spell using it. A spell with many ranks is
  one the game leaned on, so the name repeated most often across ranks wins;
  ties go to the oldest id. That puts Fireball ahead of an obscure one-off that
  happens to share the animation.

The list is filtered by school, because a fire animation on a holy spell is
rarely what anyone meant.
"""
import collections
import statistics

# Names that mark a spell as leftover development content rather than something
# a player ever saw.
_JUNK = ("OLD", "TEST", "DND", "UNUSED", "Unused", "zz", "QA ", "[PH]", "PH ",
         "Bonus", "Racial", "NPC ")

SCHOOL_NAMES = {1: "Physical", 2: "Holy", 4: "Fire", 8: "Nature",
                16: "Frost", 32: "Shadow", 64: "Arcane"}


def _is_clean(name):
    return bool(name) and not any(j in name for j in _JUNK)


class Visual:
    """One animation, and the spells that wear it."""

    __slots__ = ("visual_id", "label", "schools", "cast_ms", "spells", "ranks")

    def __init__(self, visual_id, label, schools, cast_ms, spells, ranks):
        self.visual_id = visual_id
        self.label = label
        self.schools = schools
        self.cast_ms = cast_ms
        self.spells = spells
        self.ranks = ranks

    def to_dict(self):
        return {"value": self.visual_id, "label": self.label,
                "schools": sorted(self.schools), "cast_ms": self.cast_ms,
                "spells": self.spells, "ranks": self.ranks}


def extract(corpus):
    """Every usable animation in the corpus, best label first."""
    by_visual = collections.defaultdict(list)
    for spell in corpus.spells:
        visual = spell["SpellVisualID_1"]
        # Only spells a player could have seen: on a skill line, sensibly named.
        if visual and corpus.is_player_spell(spell.id) and _is_clean(spell.name):
            by_visual[visual].append(spell)

    out = []
    for visual, spells in by_visual.items():
        names = collections.Counter(s.name for s in spells)
        top = max(names.items(), key=lambda kv: (kv[1], -min(
            s.id for s in spells if s.name == kv[0])))
        label, ranks = top
        schools = {s["SchoolMask"] for s in spells if s["SchoolMask"]}
        casts = [s.cast_time_ms() for s in spells]
        out.append(Visual(visual, label, schools,
                          int(statistics.median(casts)) if casts else 0,
                          len(spells), ranks))
    # Most-ranked first: the animations people have actually seen, at the top.
    out.sort(key=lambda v: (-v.ranks, -v.spells, v.label))
    return out


def payload(corpus):
    return [v.to_dict() for v in extract(corpus)]


def for_school(visuals, school):
    """The animations that belong to a school, as the UI filters them."""
    return [v for v in visuals if not v.schools or school in v.schools]


def cast_time_note(visual, cast_time_ms):
    """Whether the animation and the cast time contradict each other.

    The cast animation comes from the cast time, not from the visual, so an
    instant spell wearing a long-cast animation simply loses the wind-up. Worth
    saying, not worth refusing.
    """
    if visual is None:
        return None
    theirs, ours = visual.cast_ms, max(0, int(cast_time_ms))
    if theirs and not ours:
        return ("%s is a %.3gs cast in the game, so its wind-up animation will "
                "not play on an instant spell." % (visual.label, theirs / 1000.0))
    if ours and not theirs:
        return ("%s is an instant in the game, so it has no wind-up animation "
                "to play while your spell is being cast." % visual.label)
    return None
