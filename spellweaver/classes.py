"""Which class a spell belongs to, and the row that says so.

A spell belongs to a class from the moment it exists, not from the moment a
trainer is assigned. The trainer gate is downstream of this: when a Priest
trainer offers a spell, the server asks whether the spell fits the player's
class, and it answers out of the SkillLineAbility store. A spell with no row
there fits everyone.

That store is fed from two places and both matter:

* `skilllineability_dbc`, a world-database table the core merges over the DBC at
  startup, exactly as `spell_dbc` works. This is what the *server* enforces.
* `DBFilesClient\\SkillLineAbility.dbc` inside the client patch, which is what
  puts the spell in the right tab of the spellbook.

One class choice writes both, so the two can never disagree.

The skill lines for each class are derived rather than hardcoded: every
class-category line its own spells already sit on, busiest first. A Priest has
Holy, Shadow Magic and Discipline, and which one an ability belongs to is the
user's choice - it decides the tab it appears under in the spellbook. The
busiest is only the default, not the assumption.
"""
import collections
import pathlib
import time

from . import dbc
from .mysql import quote

TABLE = "skilllineability_dbc"
DATA = pathlib.Path(__file__).resolve().parent.parent / "data/dbc"

# SkillLine.dbc: the category that holds class skills rather than professions,
# weapon skills or languages.
CLASS_SKILL_CATEGORY = 7

# A class-category line shows up for a class as soon as one spell carries both
# class masks, which is how a Paladin line appears under Priest. A line has to
# hold a real share of the class's spells to be one of its trees.
MIN_TREE_SPELLS = 5
MIN_TREE_SHARE = 0.05

# SkillLineAbility.dbc column order, which is also this table's column order.
COLUMNS = ("ID", "SkillLine", "Spell", "RaceMask", "ClassMask", "ExcludeRace",
           "ExcludeClass", "MinSkillLineRank", "SupercededBySpell", "AcquireMethod",
           "TrivialSkillLineRankHigh", "TrivialSkillLineRankLow",
           "CharacterPoints_1", "CharacterPoints_2")

# What all 3,187 stock class-spell rows carry: every race, no exclusions, rank 1,
# learned rather than granted. Copied rather than invented.
ALL_RACES = 0
MIN_RANK = 1
ACQUIRE_LEARNED = 0

CLASSES = [
    {"value": 0, "label": "Any class"},
    {"value": 1, "label": "Warrior"},
    {"value": 2, "label": "Paladin"},
    {"value": 3, "label": "Hunter"},
    {"value": 4, "label": "Rogue"},
    {"value": 5, "label": "Priest"},
    {"value": 6, "label": "Death Knight"},
    {"value": 7, "label": "Shaman"},
    {"value": 8, "label": "Mage"},
    {"value": 9, "label": "Warlock"},
    {"value": 11, "label": "Druid"},
]
LABELS = {c["value"]: c["label"] for c in CLASSES}


class ClassError(Exception):
    pass


def class_mask(class_id):
    """The bit a class occupies in a ClassMask field."""
    return 0 if not class_id else 1 << (int(class_id) - 1)


_SKILL_LINES = None


def _derive(data_dir=DATA):
    """Each class's home skill line, taken from where its own spells live."""
    d = pathlib.Path(data_dir)
    skill_line = dbc.Dbc.read(d / "SkillLine.dbc")
    ability = dbc.Dbc.read(d / "SkillLineAbility.dbc")
    NAME = 3                                     # DisplayName_Lang_enUS
    names = {r[0]: skill_line.string_at(r[NAME]) for r in skill_line.records}
    category = {r[0]: r[1] for r in skill_line.records}

    out = {}
    for entry in CLASSES:
        class_id = entry["value"]
        if not class_id:
            continue
        mask = class_mask(class_id)
        counts = collections.Counter()
        for row in ability.records:
            if row[4] & mask and category.get(row[1]) == CLASS_SKILL_CATEGORY:
                counts[row[1]] += 1
        if not counts:
            continue
        # Busiest first: the one most of the class's own spells use leads, and
        # the rest are offered beside it.
        total = sum(counts.values())
        out[class_id] = [{"skill_line": line, "name": names.get(line, "?"), "spells": n}
                         for line, n in counts.most_common()
                         if n >= MIN_TREE_SPELLS and n >= total * MIN_TREE_SHARE]
    return out


def skill_lines():
    global _SKILL_LINES
    if _SKILL_LINES is None:
        _SKILL_LINES = _derive()
    return _SKILL_LINES


def trees_for(class_id):
    """Every skill line a class's own spells use, busiest first."""
    return skill_lines().get(int(class_id or 0), [])


def skill_line_for(class_id, skill_line=0):
    """(skill line id, its name), honouring a chosen tree when one is given."""
    if not class_id:
        return 0, ""
    trees = trees_for(class_id)
    if not trees:
        raise ClassError("No class skill line found for class %s" % class_id)
    if skill_line:
        for tree in trees:
            if tree["skill_line"] == int(skill_line):
                return tree["skill_line"], tree["name"]
        raise ClassError(
            "Skill line %s does not belong to %s."
            % (skill_line, LABELS.get(int(class_id), class_id)))
    return trees[0]["skill_line"], trees[0]["name"]


def ability_row(spell_id, class_id, skill_line=0):
    """The SkillLineAbility record for one spell, as plain ints.

    The row's id is the spell's own id. Stock SkillLineAbility stops at 21,980,
    so a reserved spell band can never collide with it, and a row is trivially
    traceable back to the spell it belongs to.
    """
    line, _name = skill_line_for(class_id, skill_line)
    if not line:
        return None
    return (int(spell_id), int(line), int(spell_id), ALL_RACES, class_mask(class_id),
            0, 0, MIN_RANK, 0, ACQUIRE_LEARNED, 0, 0, 0, 0)


def ability_sql(spell_id, class_id, skill_line=0):
    """The statements that put the row in front of the server, shown before they run."""
    row = ability_row(spell_id, class_id, skill_line)
    statements = ["DELETE FROM `%s` WHERE `ID` = %d" % (TABLE, int(spell_id))]
    if row:
        statements.append(
            "INSERT INTO `%s` (%s)\nVALUES (%s)"
            % (TABLE, ", ".join("`%s`" % c for c in COLUMNS),
               ", ".join(str(v) for v in row)))
    return ";\n".join(statements) + ";"


def ensure_schema(db):
    """The table ships with AzerothCore; a missing one means a core too old."""
    _, rows = db.query("SHOW TABLES LIKE %s" % quote(TABLE))
    if not rows:
        raise ClassError(
            "This world database has no `%s` table, so a spell cannot be tied to "
            "a class server-side. That table is part of AzerothCore's DBC "
            "override support." % TABLE)


def set_ability(db, band, spell_id, class_id, skill_line=0):
    """Tie a spell to a class, or untie it when the class is 'any'."""
    spell_id = int(spell_id)
    if not band.contains(spell_id):
        raise ClassError(
            "Refusing to touch skill line ability %d: outside the reserved band %s."
            % (spell_id, band))
    ensure_schema(db)
    db.execute("DELETE FROM %s WHERE ID = %d" % (TABLE, spell_id))
    row = ability_row(spell_id, class_id, skill_line)
    if row:
        db.execute("INSERT INTO %s (%s) VALUES (%s)"
                   % (TABLE, ", ".join(COLUMNS), ", ".join(str(v) for v in row)))
    return row


def clear_ability(db, spell_id):
    db.execute("DELETE FROM %s WHERE ID = %d" % (TABLE, int(spell_id)))


def ability_for(db, spell_id):
    """The row this tool wrote for a spell, if any."""
    _, rows = db.query(
        "SELECT %s FROM %s WHERE ID = %d"
        % (", ".join(COLUMNS), TABLE, int(spell_id)))
    return dict(zip(COLUMNS, rows[0])) if rows else None


def rows_in_band(db, band):
    """Every ability row we own, for the client patch to carry."""
    _, rows = db.query(
        "SELECT %s FROM %s WHERE ID BETWEEN %d AND %d ORDER BY ID"
        % (", ".join(COLUMNS), TABLE, band.start, band.end))
    return [tuple(int(v) for v in r) for r in rows]


def payload():
    """Classes for the UI, each with the spellbook tabs it can use."""
    out = []
    for entry in CLASSES:
        item = dict(entry)
        trees = trees_for(entry["value"])
        item["trees"] = [{"value": t["skill_line"], "label": t["name"],
                          "spells": t["spells"]} for t in trees]
        item["skill_line"] = trees[0]["skill_line"] if trees else 0
        item["skill_line_name"] = trees[0]["name"] if trees else ""
        out.append(item)
    return out
