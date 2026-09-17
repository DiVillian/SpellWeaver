"""How much the caster's own power adds to a spell.

A flat 500 is not how the game behaves. A real spell is a base number plus a
share of whatever the caster has built up, which is why two players casting the
same spell do different amounts.

3.3.5a decides *which* stat that is on its own - spell power for magic, attack
power for physical - so the only question is how strongly it scales. That is one
number, and the corpus already knows the usual answer for each behaviour: 13%
for a damage-over-time, 81% for a direct heal.

The number lives in `spell_bonus_data`, a core table the server reads at
startup, keyed on the spell. Which column it goes in follows from the spell
itself: periodic effects take the over-time column, direct effects the direct
one, and a physical spell takes the attack-power pair instead.
"""
import time

from .mysql import quote

TABLE = "spell_bonus_data"
PHYSICAL = 1                      # SchoolMask for a physical spell


class ScalingError(Exception):
    pass


def columns_for(school_mask, has_direct, has_periodic):
    """Which columns this spell's scaling belongs in, and why.

    Returns a list of column names. A spell with both a direct hit and a
    lingering effect scales in both places, as the game's own do.
    """
    physical = int(school_mask) == PHYSICAL
    out = []
    if has_direct:
        out.append("ap_bonus" if physical else "direct_bonus")
    if has_periodic:
        out.append("ap_dot_bonus" if physical else "dot_bonus")
    return out


def stat_name(school_mask):
    return "attack power" if int(school_mask) == PHYSICAL else "spell power"


def bonus_values(school_mask, has_direct, has_periodic, percent):
    """The four coefficients, as the table stores them."""
    fraction = max(0.0, float(percent or 0)) / 100.0
    values = {"direct_bonus": 0.0, "dot_bonus": 0.0,
              "ap_bonus": 0.0, "ap_dot_bonus": 0.0}
    if not fraction:
        return values
    for column in columns_for(school_mask, has_direct, has_periodic):
        values[column] = round(fraction, 4)
    return values


def bonus_sql(spell_id, name, school_mask, has_direct, has_periodic, percent):
    """The row that makes a spell scale, shown before it runs."""
    statements = ["DELETE FROM `%s` WHERE `entry` = %d" % (TABLE, int(spell_id))]
    values = bonus_values(school_mask, has_direct, has_periodic, percent)
    if any(values.values()):
        statements.append(
            "INSERT INTO `%s` (`entry`, `direct_bonus`, `dot_bonus`, `ap_bonus`, "
            "`ap_dot_bonus`, `comments`)\nVALUES (%d, %s, %s, %s, %s, %s)"
            % (TABLE, int(spell_id), values["direct_bonus"], values["dot_bonus"],
               values["ap_bonus"], values["ap_dot_bonus"],
               quote("spellweaver - %s" % name)))
    return ";\n".join(statements) + ";"


def set_bonus(db, band, spell_id, name, school_mask, has_direct, has_periodic, percent):
    """Write the scaling, or take it away when it is set to nothing."""
    spell_id = int(spell_id)
    if not band.contains(spell_id):
        raise ScalingError(
            "Refusing to touch scaling for %d: outside the reserved band %s."
            % (spell_id, band))
    db.execute("DELETE FROM %s WHERE entry = %d" % (TABLE, spell_id))
    values = bonus_values(school_mask, has_direct, has_periodic, percent)
    if not any(values.values()):
        return None
    db.execute(
        "INSERT INTO %s (entry, direct_bonus, dot_bonus, ap_bonus, ap_dot_bonus, comments) "
        "VALUES (%d, %s, %s, %s, %s, %s)"
        % (TABLE, spell_id, values["direct_bonus"], values["dot_bonus"],
           values["ap_bonus"], values["ap_dot_bonus"],
           quote("spellweaver - %s" % name)))
    return values


def clear_bonus(db, spell_id):
    db.execute("DELETE FROM %s WHERE entry = %d" % (TABLE, int(spell_id)))


def bonus_for(db, spell_id):
    _, rows = db.query(
        "SELECT entry, direct_bonus, dot_bonus, ap_bonus, ap_dot_bonus "
        "FROM %s WHERE entry = %d" % (TABLE, int(spell_id)))
    if not rows:
        return None
    entry, direct, dot, ap, ap_dot = rows[0]
    return {"entry": entry, "direct_bonus": direct, "dot_bonus": dot,
            "ap_bonus": ap, "ap_dot_bonus": ap_dot}
