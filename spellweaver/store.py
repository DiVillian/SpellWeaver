"""Writing spells to the world database, and remembering what we wrote.

Two tables are involved:

* `spell_dbc`, a core AzerothCore table. The worldserver merges it over the
  client's Spell.dbc at startup, so a row here is a real spell. This is the only
  core table spellweaver writes to, and only ever inside the reserved band.
* `skilllineability_dbc`, another core override table, which is what ties the
  spell to a class. See classes.py.
* `spell_bonus_data`, which is how much the caster's own power adds. See
  scaling.py.
* `spellweaver_spell`, ours. It keeps the original definition - the behaviours
  and the numbers the user chose - so a spell can be listed and reopened for
  editing later. spell_dbc alone cannot be edited back into a form, because
  turning 234 integers back into "Damage over time" loses the user's intent.

Nothing outside the reserved band is ever touched; `_guard` enforces that on
every write rather than trusting callers.
"""
import json
import time

from . import classes, ranks as ranks_module, scaling, trainers
from .mysql import quote as _quote
from .spell import SpellDef, rank_definitions, resolve
from .spell_layout import FIELDS
from .tooltip import suggest_aura_description, suggest_description

TABLE = "spellweaver_spell"

SCHEMA = """
CREATE TABLE IF NOT EXISTS %s (
  spell_id    INT UNSIGNED NOT NULL PRIMARY KEY,
  name        VARCHAR(255) NOT NULL,
  definition  MEDIUMTEXT   NOT NULL,
  created_at  INT UNSIGNED NOT NULL,
  updated_at  INT UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""" % TABLE


class BandError(Exception):
    """Raised when a write would land outside the reserved band."""


def ensure_schema(db):
    db.execute(SCHEMA)


def _guard(band, spell_id):
    if not band.contains(spell_id):
        raise BandError(
            "Refusing to touch spell %d: outside the reserved band %s."
            % (spell_id, band))


def check_band_available(db, band):
    """Rows in the band that we did not put there.

    A rank past the first has no row of its own in the library - one ability is
    one entry - so the rank table has to be consulted too, or every ability with
    ranks would look like somebody else's spell sitting in our band.
    """
    ensure_schema(db)
    ranks_module.ensure_schema(db)
    _, rows = db.query(
        "SELECT d.ID FROM spell_dbc d "
        "LEFT JOIN %s s ON s.spell_id = d.ID "
        "LEFT JOIN %s r ON r.rank_spell_id = d.ID "
        "WHERE d.ID BETWEEN %d AND %d AND s.spell_id IS NULL "
        "AND r.rank_spell_id IS NULL LIMIT 20"
        % (TABLE, ranks_module.TABLE, band.start, band.end))
    return [r[0] for r in rows]


def next_spell_id(db, band, avoid=()):
    """The lowest free id in the band, reusing gaps left by deletions."""
    _, rows = db.query(
        "SELECT ID FROM spell_dbc WHERE ID BETWEEN %d AND %d ORDER BY ID"
        % (band.start, band.end))
    used = {r[0] for r in rows}
    _, rows = db.query("SELECT spell_id FROM %s" % TABLE)
    used |= {r[0] for r in rows}
    used |= ranks_module.taken_ids(db)
    used |= set(avoid)
    candidate = band.start
    while candidate in used:
        candidate += 1
    if candidate > band.end:
        raise BandError("The reserved band %s is full." % band)
    return candidate


def spell_dbc_sql(resolved, name, description, rank_text="", aura_description=None):
    """The INSERT that makes this spell real, as text.

    Returned rather than executed so the UI can show exactly what will run.
    """
    values = []
    for column in FIELDS:
        if column == "Name_Lang_enUS":
            values.append(_quote(name))
        elif column == "NameSubtext_Lang_enUS":
            # What the game shows under the name: "Rank 3", or nothing at all.
            values.append(_quote(rank_text))
        elif column == "Description_Lang_enUS":
            values.append(_quote(description))
        elif column == "AuraDescription_Lang_enUS":
            # What the game shows when the effect is on someone. Left empty the
            # icon has a name and nothing else, which is how it read in game.
            values.append(_quote(
                suggest_aura_description(resolved)
                if aura_description is None else aura_description))
        elif column.endswith("_Lang_Mask"):
            values.append("16712190")     # the mask stock spells use for enUS
        elif "_Lang_" in column:
            values.append("''")
        else:
            value = resolved.fields[column]
            values.append(repr(value) if isinstance(value, float) else str(int(value)))
    return (
        "DELETE FROM `spell_dbc` WHERE `ID` = %d;\n"
        "INSERT INTO `spell_dbc` (%s)\nVALUES (%s);"
        % (resolved.spell_id,
           ", ".join("`%s`" % c for c in FIELDS),
           ", ".join(values)))


def write_spell(db, band, definition, resolved, description):
    """Write one spell: its row, its class, and how it scales.

    One rank of an ability is one spell, so this is called once per rank.
    """
    _guard(band, resolved.spell_id)
    ensure_schema(db)
    sql = spell_dbc_sql(resolved, definition.name, description, resolved.rank_text)
    for statement in sql.split(";\n"):
        statement = statement.strip().rstrip(";")
        if statement:
            db.execute(statement)
    # The class row goes in with the spell, so a spell is never briefly in the
    # world belonging to nobody.
    classes.set_ability(db, band, resolved.spell_id, definition.class_id,
                        definition.skill_line)
    scaling.set_bonus(db, band, resolved.spell_id, definition.name,
                      resolved.fields["SchoolMask"], resolved.has_direct(),
                      resolved.has_periodic(), definition.power_scaling)
    return resolved.spell_id


def remember(db, spell_id, definition):
    """Keep the definition, under the first rank's id. One ability, one row."""
    ensure_schema(db)
    now = int(time.time())
    payload = json.dumps(definition.to_dict())
    db.execute(
        "INSERT INTO %s (spell_id, name, definition, created_at, updated_at) "
        "VALUES (%d, %s, %s, %d, %d) "
        "ON DUPLICATE KEY UPDATE name = VALUES(name), "
        "definition = VALUES(definition), updated_at = VALUES(updated_at)"
        % (TABLE, int(spell_id), _quote(definition.name), _quote(payload), now, now))


def save(db, band, definition, resolved, description):
    """Backwards-compatible single-spell save."""
    write_spell(db, band, definition, resolved, description)
    remember(db, resolved.spell_id, definition)
    return resolved.spell_id


def save_ability(db, settings, definition, vocabulary=None):
    """Write every rank of an ability and chain them together.

    An ability of one rank takes one id and writes no chain, which is what it
    did before ranks existed. An ability of five takes five, and says so.
    """
    ensure_schema(db)
    ranks_module.ensure_schema(db)
    pairs = rank_definitions(definition)
    total = len(pairs)

    first_id = definition.spell_id or next_spell_id(db, settings.band)
    held = ranks_module.ids_for(db, first_id) if definition.spell_id else [first_id]
    ids, taken = [], set(held)
    for rank, _defn in pairs:
        if rank - 1 < len(held):
            ids.append(held[rank - 1])
        else:
            fresh = next_spell_id(db, settings.band, avoid=set(ids) | taken)
            ids.append(fresh)
            taken.add(fresh)

    written = []
    for (rank, defn), spell_id in zip(pairs, ids):
        defn.spell_id = spell_id
        resolved = resolve(defn, vocabulary=vocabulary, spell_id=spell_id,
                           rank=rank, ranks_total=total)
        description = defn.description.strip() or suggest_description(resolved)
        write_spell(db, settings.band, defn, resolved, description)
        written.append({"rank": rank, "spell_id": spell_id,
                        "spell_level": defn.spell_level,
                        "power_cost": defn.power_cost,
                        "warnings": resolved.warnings, "notes": resolved.notes})

    # Ranks the user removed give their ids back rather than lingering as
    # spells nothing points at.
    for spare in held[total:]:
        _remove_spell(db, settings.band, spare)

    ranks_module.set_chain(db, settings.band, ids)
    definition.spell_id = first_id
    remember(db, first_id, definition)
    return {"spell_id": first_id, "ids": ids, "ranks": written}


def list_spells(db):
    ensure_schema(db)
    ranks_module.ensure_schema(db)
    _, rows = db.query(
        "SELECT s.spell_id, s.name, s.updated_at, s.definition, "
        "       (SELECT COUNT(*) FROM %s r WHERE r.spell_id = s.spell_id) AS ranks "
        "FROM %s s ORDER BY s.name" % (ranks_module.TABLE, TABLE))
    out = []
    for spell_id, name, updated, payload, rank_count in rows:
        # The library shows what an ability is, not how it is stored, so the
        # icon and class come out of the definition it was saved with.
        try:
            saved = json.loads(payload)
        except ValueError:
            saved = {}
        out.append({"spell_id": spell_id, "name": name, "updated_at": updated,
                    "ranks": max(1, rank_count),
                    "icon_id": saved.get("icon_id") or 0,
                    "class_id": saved.get("class_id") or 0,
                    "skill_line": saved.get("skill_line") or 0})
    return out


def load(db, spell_id):
    ensure_schema(db)
    _, rows = db.query(
        "SELECT definition FROM %s WHERE spell_id = %d" % (TABLE, int(spell_id)))
    if not rows:
        return None
    definition = SpellDef.from_dict(json.loads(rows[0][0]))
    definition.spell_id = int(spell_id)
    return definition


def _remove_spell(db, band, spell_id):
    """Take one spell out of the world, and everything that pointed at it.

    A trainer_spell row pointing at a spell that no longer exists is an error
    the worldserver logs on every startup, so the two cannot be separated.
    """
    _guard(band, int(spell_id))
    trainers.clear_training(db, int(spell_id))
    classes.clear_ability(db, int(spell_id))
    scaling.clear_bonus(db, int(spell_id))
    db.execute("DELETE FROM spell_dbc WHERE ID = %d" % int(spell_id))


def delete(db, band, spell_id):
    """Remove an ability: every rank of it, and everything pointing at them."""
    _guard(band, int(spell_id))
    ensure_schema(db)
    ranks_module.ensure_schema(db)
    for rank_id in ranks_module.ids_for(db, int(spell_id)):
        _remove_spell(db, band, rank_id)
    ranks_module.clear_chain(db, int(spell_id))
    db.execute("DELETE FROM %s WHERE spell_id = %d" % (TABLE, int(spell_id)))
