"""Rendering a resolved spell the way the game would show it.

Everything here reads `ResolvedSpell.fields` - the values headed for the
database - and never the form. A tooltip that disagrees with the spell is then
a bug you can see while building it, instead of one you find in game.

Descriptions use the game's own substitution tokens, so text written here reads
the same way Blizzard's does:

  $s1 $s2 $s3   the amount of the first, second or third effect, written as a
                range when the spell varies
  $t1 $t2 $t3   that effect's time between ticks, in seconds
  $o1 $o2 $o3   the total that effect does over the whole duration
  $d            the spell's duration
"""
import re

from .scaling import stat_name
from .spell import POWER_NAMES, SCHOOL_NAMES

# Phrasing for the behaviours a person is most likely to reach for. Anything
# without an entry falls back to a generic sentence built from its shape.
# What each effect lands on, in words. Targeting is per effect, so a spell that
# damages an enemy and heals its caster has to read that way rather than saying
# "the target" twice and meaning two different things.
OBJECTS = {
    "self": "you",
    "pet": "your pet",
    "enemies_around_self": "enemies around you",
    "allies_around_self": "allies around you",
    "enemies_around_target": "your target and enemies near it",
    "cone": "enemies in front of you",
    "ground": "everything at the spot",
}

PHRASES = {
    "damage": "Deals $s{n} {school} damage.",
    "damage_over_time": "Deals $s{n} {school} damage every $t{n} sec for $d.",
    "damage_ground": "Calls down a storm, dealing $s{n} {school} damage every $t{n} sec for $d.",
    "heal": "Heals the target for $s{n}.",
    "heal_over_time": "Heals the target for $s{n} every $t{n} sec for $d.",
    "absorb": "Absorbs $s{n} damage for $d.",
    "drain_life": "Drains $s{n} health every $t{n} sec for $d, healing you for {share} of it.",
    "health_leech": "Deals $s{n} {school} damage and heals you for {share} of it.",
    "drain_mana": "Drains $s{n} {power} every $t{n} sec for $d, giving you {share} of it.",
    "power_drain": "Drains $s{n} {power} from the target, giving you {share} of it.",
    "power_burn": "Destroys $s{n} {power} and deals damage for each point destroyed.",
    "stun": "Stuns the target for $d.",
    "root": "Roots the target in place for $d.",
    "fear": "Causes the target to flee in terror for $d.",
    "silence": "Silences the target for $d.",
    "disorient": "Disorients the target for $d.",
    "slow": "Slows the target's movement by $s{n}% for $d.",
    "speed_up": "Increases movement speed by $s{n}% for $d.",
    "restore_power": "Restores $s{n} {power}.",
    "weapon_damage_pct": "Deals $s{n}% weapon damage.",
    "weapon_damage_plus": "Deals weapon damage plus $s{n}.",
    "knock_back": "Knocks the target back.",
    "instakill": "Kills the target outright.",
    "summon": "Summons a creature to fight for you.",
    "trigger_spell": "Casts another spell on the target.",
    "mod_stat": "Increases the target's stats by $s{n} for $d.",
    "mod_attack_power": "Increases attack power by $s{n} for $d.",
    "mod_resistance": "Increases resistance by $s{n} for $d.",
    "mod_damage_done_pct": "Increases damage dealt by $s{n}% for $d.",
    "mod_damage_taken_pct": "Changes damage taken by $s{n}% for $d.",
    "taunt": "Taunts the target, forcing it to attack you.",
    "dispel": "Removes $s{n} harmful effect from the target.",
}


# What the game shows on the icon while the effect is on someone, which is a
# different field from the spell's own description and a different voice: the
# state the target is in, per tick rather than per cast, and no duration,
# because the icon already counts that down. Taken from how stock spells word
# it - Hammer of Justice reads "Stunned.", Blessing of Might "Increases attack
# power by $s1." Anything not named here falls back to the spell's own sentence
# with the duration clause removed, which lands on the same wording for most of
# the "Increases X by Y for $d" behaviours.
AURA_PHRASES = {
    "stun": "Stunned.",
    "root": "Rooted in place.",
    "fear": "Feared.",
    "silence": "Silenced.",
    "disorient": "Disoriented.",
    "pacify": "Cannot attack.",
    "disarm": "Disarmed.",
    "pacify_silence": "Cannot attack or cast spells.",
    "taunt": "Forced to attack the caster.",
    "slow": "Movement slowed by $s{n}%.",
    "slow_ground": "Movement slowed by $s{n}%.",
    "speed_up": "Movement speed increased by $s{n}%.",
    "damage_over_time": "$s{n} {school} damage every $t{n} sec.",
    "damage_ground": "$s{n} {school} damage every $t{n} sec.",
    "damage_pct_health": "$s{n}% of health every $t{n} sec.",
    "heal_over_time": "Healing $s{n} every $t{n} sec.",
    "restore_power_over_time": "Restoring $s{n} {power} every $t{n} sec.",
    "drain_life": "Draining $s{n} health every $t{n} sec.",
    "drain_mana": "Draining $s{n} {power} every $t{n} sec.",
    "absorb": "Absorbs damage.",
    "mana_shield": "Absorbs damage, draining {power} instead.",
    "stealth": "Stealthed.",
    "invisibility": "Invisible.",
    "feign_death": "Feigning death.",
    "water_breathing": "Can breathe underwater.",
}

# Shapes that put something on the target for a while. Anything else is over
# the moment it lands and never shows an icon to hover.
LASTING_SHAPES = ("aura", "periodic", "ground")

_DURATION_CLAUSE = re.compile(r"\s+for \$d(?=[.!]|$)")


def _seconds(ms):
    """Format milliseconds the way the game does."""
    if ms <= 0:
        return "0 sec"
    if ms % 60000 == 0 and ms >= 60000:
        minutes = ms // 60000
        return "%d min" % minutes
    value = ms / 1000.0
    return ("%g sec" % round(value, 2))


def suggest_description(resolved):
    """A description written from the spell's own behaviours.

    The user can rewrite it, but a spell that is never edited still reads like
    a spell rather than being blank.
    """
    school = SCHOOL_NAMES.get(resolved.fields["SchoolMask"], "")
    power = POWER_NAMES.get(resolved.fields["PowerType"], "mana")
    parts = []
    for slot in resolved.active_slots():
        use = resolved.definition.behaviours[slot - 1]
        behaviour = use.behaviour
        template = PHRASES.get(behaviour.key)
        if template is None:
            template = _generic_phrase(behaviour)
        sentence = template.format(n=slot, school=school.lower(), power=power.lower(),
                                   share=_share_words(use))
        parts.append(_aim_words(sentence, resolved.aim_for(slot)))
    return " ".join(parts)


def suggest_aura_description(resolved):
    """What the icon on the target says, or "" when nothing lingers.

    Written from the spell rather than asked for: it is the same information in
    a different voice, and a second description box would be one more thing to
    fill in for a field most people do not know exists.
    """
    school = SCHOOL_NAMES.get(resolved.fields["SchoolMask"], "")
    power = POWER_NAMES.get(resolved.fields["PowerType"], "mana")
    parts = []
    for slot in resolved.active_slots():
        use = resolved.definition.behaviours[slot - 1]
        behaviour = use.behaviour
        if behaviour.shape not in LASTING_SHAPES:
            continue
        template = AURA_PHRASES.get(behaviour.key)
        if template is None:
            spell_phrase = PHRASES.get(behaviour.key) or _generic_phrase(behaviour)
            template = _DURATION_CLAUSE.sub("", spell_phrase)
        sentence = template.format(n=slot, school=school.lower(),
                                   power=power.lower(), share=_share_words(use))
        parts.append(_aim_words(sentence, resolved.aim_for(slot)))
    return " ".join(parts)


def _share_words(use):
    """A leech's share, as a percentage of what it did."""
    share = getattr(use, "share", 0) or 0
    return "%g%%" % round(share * 100, 1)


def _aim_words(sentence, aim):
    """Say what an effect landed on, when it is not simply the target."""
    replacement = OBJECTS.get(aim)
    if not replacement:
        return sentence
    if sentence.startswith("The target"):
        sentence = replacement[0].upper() + replacement[1:] + sentence[len("The target"):]
    return sentence.replace("the target", replacement)


def _generic_phrase(behaviour):
    lasting = behaviour.shape in ("aura", "periodic", "ground")
    has_amount = "amount" in behaviour.params
    label = behaviour.label[0].lower() + behaviour.label[1:]
    if behaviour.shape == "periodic" and has_amount:
        return "Applies %s of $s{n} every $t{n} sec for $d." % label
    if lasting and has_amount:
        return "Applies %s of $s{n} for $d." % label
    if lasting:
        return "Applies %s for $d." % label
    if has_amount:
        return "Applies %s of $s{n}." % label
    return "Applies %s." % label


def substitute(text, resolved):
    """Replace the game's tokens with this spell's real numbers."""
    duration = resolved.duration_ms()

    def replace(match):
        token, index = match.group(1), match.group(2)
        slot = int(index) if index else 1
        if token == "d":
            return _seconds(duration)
        if slot not in resolved.active_slots():
            return match.group(0)
        if token == "s":
            # A spell that varies reads as a range, the way the game writes it.
            low, high = resolved.effect_range(slot)
            low, high = abs(low), abs(high)
            if low == high:
                return str(high)
            return "%d to %d" % (min(low, high), max(low, high))
        if token == "t":
            period = resolved.fields["EffectAuraPeriod_%d" % slot]
            return "%g" % (period / 1000.0) if period else "0"
        if token == "o":
            period = resolved.fields["EffectAuraPeriod_%d" % slot]
            # A total over time is the average per tick, not the best case.
            average = abs(resolved.effect_average(slot))
            if not period or not duration:
                return str(average)
            ticks = max(1, duration // period)
            return str(average * ticks)
        return match.group(0)

    return re.sub(r"\$([sdto])(\d)?", replace, text)


def _scaling_line(resolved):
    """What the caster's own power adds, in the words the game uses for stats."""
    percent = getattr(resolved.definition, "power_scaling", 0) or 0
    if percent <= 0:
        return None
    return "+%g%% of your %s" % (round(float(percent), 2),
                                 stat_name(resolved.fields["SchoolMask"]))


def build(resolved):
    """The tooltip, as data. The browser draws it; the numbers come from here."""
    f = resolved.fields
    description = resolved.definition.description.strip() or suggest_description(resolved)
    cost = f["ManaCost"]
    power = POWER_NAMES.get(f["PowerType"], "Mana")
    cast = resolved.cast_time_ms()
    cooldown = f["RecoveryTime"]
    range_yards = resolved.range_yards()

    return {
        "name": resolved.definition.name or "Unnamed Ability",
        "icon_id": f["SpellIconID"],
        "cost": ("%d %s" % (cost, power)) if cost else None,
        "range": ("%g yd range" % range_yards) if range_yards else None,
        "cast_time": "Instant" if cast <= 0 else "%g sec cast" % (cast / 1000.0),
        "cooldown": ("%s cooldown" % _seconds(cooldown)) if cooldown else None,
        "description": substitute(description, resolved),
        "scaling": _scaling_line(resolved),
        "school": SCHOOL_NAMES.get(f["SchoolMask"], ""),
        # The game writes the rank beside the name; an ability of one rank has
        # nothing to say here.
        "rank": resolved.rank_text or None,
        "level": f["SpellLevel"] if f["SpellLevel"] > 1 else None,
        "spell_id": resolved.spell_id,
    }
