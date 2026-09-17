"""Who or what a spell lands on.

Targeting is orthogonal to behaviour: "damage" is one thing, "damage everything
where I click" is that thing aimed somewhere. Keeping them apart means the
target picker is written once and applies to every behaviour, rather than the
vocabulary growing an area variant of each entry.

Three families, which the game treats quite differently:

* unit      the spell needs something selected, or is cast on the caster
* around    an area centred on a unit, with no reticle
* ground    the reticle: the player clicks a spot and the spell lands there

Ground targeting is not just a different implicit target id. The client only
draws a reticle when TARGET_FLAG_DEST_LOCATION (0x40) is set in the spell's
`Targets` bitmask, which is how Blizzard and Rain of Fire do it. Miss that flag
and the spell silently becomes self-cast.

The implicit target also depends on what the behaviour *is*. Under a ground
target, a one-shot effect hits UNIT_DEST_AREA_ENEMY (everything at the spot)
while a persistent patch anchors to DEST_DYNOBJ_ENEMY. Flamestrike carries both
at once, one per effect slot. `implicit_for` resolves that per behaviour.
"""
from collections import namedtuple

# Bits in the Spell.dbc `Targets` field.
#
# Only destination targeting sets a bit here. It is tempting to also set the
# unit flag (0x02) for unit-targeted spells, but stock data says otherwise: of
# the 7,345 spells whose first effect targets UNIT_TARGET_ENEMY, 7,343 leave
# `Targets` at zero and let the implicit target do the work.
TARGET_FLAG_NONE = 0x0000
TARGET_FLAG_DEST_LOCATION = 0x0040

Targeting = namedtuple(
    "Targeting",
    "key label family flags implicit ground_implicit area needs_target hostility summary")


def _t(key, label, family, flags, implicit, ground_implicit, area, needs_target,
       hostility, summary):
    return Targeting(key, label, family, flags, implicit, ground_implicit, area,
                     needs_target, hostility, summary)


TARGETS = [
    _t("self", "Yourself", "unit", TARGET_FLAG_NONE, 1, None, False, False, "none",
       "Affects the caster."),
    _t("enemy", "One enemy", "unit", TARGET_FLAG_NONE, 6, None, False, True, "enemy",
       "Affects the hostile character the caster has selected."),
    _t("ally", "One friendly target", "unit", TARGET_FLAG_NONE, 21, None, False, True, "friendly",
       "Affects the friendly character the caster has selected."),
    _t("any", "Any one target", "unit", TARGET_FLAG_NONE, 25, None, False, True, "any",
       "Affects whichever character the caster has selected, friendly or hostile."),
    _t("pet", "Your pet", "unit", TARGET_FLAG_NONE, 5, None, False, False, "friendly",
       "Affects the caster's pet."),
    _t("enemies_around_self", "Enemies around you", "around", TARGET_FLAG_NONE, 15, None,
       True, False, "enemy",
       "Affects every hostile character within range of the caster."),
    _t("allies_around_self", "Allies around you", "around", TARGET_FLAG_NONE, 30, None,
       True, False, "friendly",
       "Affects the caster and every friendly character within range."),
    _t("enemies_around_target", "Enemies around your target", "around", TARGET_FLAG_NONE, 16, None,
       True, True, "enemy",
       "Affects the caster's target and hostile characters near it."),
    _t("cone", "A cone in front of you", "around", TARGET_FLAG_NONE, 104, None,
       True, False, "enemy",
       "Affects hostile characters in a cone in front of the caster."),
    _t("ground", "A spot you choose", "ground", TARGET_FLAG_DEST_LOCATION, 16, 28,
       True, False, "enemy",
       "The caster picks a spot on the ground and the ability lands there."),
]

BY_KEY = {t.key: t for t in TARGETS}
DEFAULT = "enemy"

# Implicit target used for the second half of an area pair, keyed by the first.
# TARGET_SRC_CASTER pairs with the "around the caster" targets.
_SRC_CASTER = 22
_PAIRED_WITH_SRC_CASTER = {15, 30}


def implicit_for(targeting, behaviour):
    """The (ImplicitTargetA, ImplicitTargetB) pair for one behaviour.

    A ground-targeted persistent patch anchors to a dynamic object, while a
    one-shot effect under the same targeting hits everything at the spot. That
    is why Flamestrike has two different implicit targets in its two slots.
    """
    if targeting.family == "ground" and behaviour.shape == "ground":
        return (targeting.ground_implicit, 0)
    if targeting.implicit in _PAIRED_WITH_SRC_CASTER:
        return (_SRC_CASTER, targeting.implicit)
    return (targeting.implicit, 0)


def allowed_for(behaviour):
    """Which targets make sense for a behaviour.

    A persistent ground patch has to be anchored somewhere; it cannot be cast on
    a selected unit, so those pairings are not offered.
    """
    if behaviour.shape == "ground":
        return [t for t in TARGETS if t.family in ("ground", "around")]
    return list(TARGETS)


def requires_radius(targeting):
    return targeting.area
