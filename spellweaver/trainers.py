"""Trainer NPCs, and which spells they teach.

A spell that exists is not a spell anyone can get. Step 3 is the other half:
someone in the world who will teach it, and a price.

The core reads three tables for this - `trainer` (a list), `trainer_spell` (what
is on it) and `creature_default_trainer` (which NPC shows it). The older
`npc_trainer` table is still in the schema but nothing loads it; writing there
would look like it worked and do nothing.

Two ways to put a spell in front of a player:

* A trainer of our own. The tool writes a `creature_template`, gives it the
  trainer flag and its own trainer list, all inside reserved bands, and the
  admin spawns it with `.npc add`. Nothing shipped is touched. Unlike a spell,
  an NPC is entirely server-side, so a custom trainer looks and behaves
  correctly in game with no client patch at all.
* An existing trainer. Adding a row to a stock trainer's list is additive - no
  shipped row is edited - and every row added is recorded in
  `spellweaver_training`, so the tool can take back exactly what it put there.

Every write is guarded against the reserved bands rather than trusting callers,
the same way spells are.
"""
import time

from . import classes
from .mysql import quote

TRAINER_TABLE = "spellweaver_trainer"
TRAINING_TABLE = "spellweaver_training"

SCHEMA = ("""
CREATE TABLE IF NOT EXISTS %s (
  trainer_id     INT UNSIGNED NOT NULL PRIMARY KEY,
  creature_entry INT UNSIGNED NOT NULL,
  name           VARCHAR(100) NOT NULL,
  subname        VARCHAR(100) NOT NULL DEFAULT '',
  class_id       TINYINT UNSIGNED NOT NULL DEFAULT 0,
  display_id     INT UNSIGNED NOT NULL,
  faction        SMALLINT UNSIGNED NOT NULL,
  created_at     INT UNSIGNED NOT NULL,
  updated_at     INT UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""" % TRAINER_TABLE, """
CREATE TABLE IF NOT EXISTS %s (
  spell_id    INT UNSIGNED NOT NULL,
  trainer_id  INT UNSIGNED NOT NULL,
  money_cost  INT UNSIGNED NOT NULL DEFAULT 0,
  req_level   TINYINT UNSIGNED NOT NULL DEFAULT 1,
  updated_at  INT UNSIGNED NOT NULL,
  PRIMARY KEY (spell_id, trainer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""" % TRAINING_TABLE)

# trainer.Type. Class is the only one with a class requirement; Tradeskill with
# no requirement is the core's own way of saying "anyone may train here".
TYPE_CLASS = 0
TYPE_TRADESKILL = 2

# npcflag. GOSSIP puts the NPC's menu up, TRAINER puts "Train me!" on it, and
# TRAINER_CLASS is what a class trainer carries.
NPC_FLAG_GOSSIP = 0x001
NPC_FLAG_TRAINER = 0x010
NPC_FLAG_TRAINER_CLASS = 0x020

# The NPC never fights - it is immune to players and to other creatures - so its
# level is only what shows on the nameplate. 80 keeps it from reading as prey.
TRAINER_LEVEL = 80

# Classes come from classes.py, which derives them once. Two lists of the same
# thing is how "Anyone Trainer" and "Any class" end up in the same interface.
CLASSES = classes.CLASSES
CLASS_LABELS = classes.LABELS

FACTIONS = [
    {"value": 35, "label": "Friendly to everyone"},
    {"value": 12, "label": "Alliance"},
    {"value": 29, "label": "Horde"},
]
DEFAULT_FACTION = 35

# Columns whose value we choose rather than inherit from the donor row below.
_OWN_COLUMNS = (
    "entry", "name", "subname", "IconName", "gossip_menu_id", "minlevel",
    "maxlevel", "faction", "npcflag", "difficulty_entry_1", "difficulty_entry_2",
    "difficulty_entry_3", "KillCredit1", "KillCredit2", "lootid", "pickpocketloot",
    "skinloot", "PetSpellDataId", "VehicleId", "mingold", "maxgold", "movementId",
    "family", "CreatureImmunitiesId", "RacialLeader", "dynamicflags", "AIName",
    "MovementType", "ScriptName", "VerifiedBuild",
)


# Names that mark a creature as leftover development content rather than an NPC
# a player could walk up to.
_JUNK = ("UNUSED", "[PH]", "TEST", "(DND)", "OLD ")
_NOT_JUNK = " ".join("AND ct.name NOT LIKE %s" % quote("%" + j + "%") for j in _JUNK)


class TrainerError(Exception):
    """Raised when a write would land outside a reserved band, or cannot be made."""


def ensure_schema(db):
    for statement in SCHEMA:
        db.execute(statement)


def _guard(band, value, what):
    if not band.contains(value):
        raise TrainerError(
            "Refusing to touch %s %d: outside the reserved band %s." % (what, value, band))


# -- what is already there -------------------------------------------------

def band_conflicts(db, settings):
    """Rows sitting in our creature and trainer bands that we did not write."""
    ensure_schema(db)
    creature, trainer = settings.creature_band, settings.trainer_band
    _, rows = db.query(
        "SELECT ct.entry FROM creature_template ct LEFT JOIN %s s "
        "ON s.creature_entry = ct.entry "
        "WHERE ct.entry BETWEEN %d AND %d AND s.creature_entry IS NULL LIMIT 20"
        % (TRAINER_TABLE, creature.start, creature.end))
    creatures = [r[0] for r in rows]
    _, rows = db.query(
        "SELECT t.Id FROM trainer t LEFT JOIN %s s ON s.trainer_id = t.Id "
        "WHERE t.Id BETWEEN %d AND %d AND s.trainer_id IS NULL LIMIT 20"
        % (TRAINER_TABLE, trainer.start, trainer.end))
    return {"creatures": creatures, "trainers": [r[0] for r in rows]}


def _next_id(db, band, sql, what):
    _, rows = db.query(sql % (band.start, band.end))
    used = {r[0] for r in rows}
    candidate = band.start
    while candidate in used:
        candidate += 1
    if candidate > band.end:
        raise TrainerError("The reserved %s band %s is full." % (what, band))
    return candidate


def next_creature_entry(db, band):
    return _next_id(
        db, band,
        "SELECT entry FROM creature_template WHERE entry BETWEEN %d AND %d",
        "creature")


def next_trainer_id(db, band):
    return _next_id(
        db, band, "SELECT Id FROM trainer WHERE Id BETWEEN %d AND %d", "trainer")


def _donor_row(db):
    """A stock class trainer, copied for the fields we have no opinion about.

    Fifty-five columns describe a creature and only a handful of them are about
    being a trainer. The rest - speeds, attack times, regeneration, the flags
    that keep an NPC out of combat - are copied from a trainer the game already
    ships, so a spellweaver trainer stands there behaving like one.
    """
    cols, rows = db.query(
        "SELECT ct.* FROM creature_template ct "
        "JOIN creature_default_trainer cdt ON cdt.CreatureId = ct.entry "
        "JOIN trainer t ON t.Id = cdt.TrainerId AND t.Type = %d "
        "WHERE ct.AIName = '' AND ct.ScriptName = '' AND ct.rank = 0 "
        "ORDER BY ct.entry LIMIT 1" % TYPE_CLASS)
    if not rows:
        raise TrainerError(
            "No stock class trainer to copy a creature template from; is this "
            "world database complete?")
    return list(cols), dict(zip(cols, rows[0]))


def model_options(db, limit=40):
    """Looks to choose from: the display ids stock class trainers already use.

    Model ids live in the client's own files. Rather than ask for a number, the
    tool offers the ones the game's trainers wear, named by who wears them.
    """
    _, rows = db.query(
        "SELECT ctm.CreatureDisplayID, MIN(ct.name), MIN(ct.subname) "
        "FROM creature_template_model ctm "
        "JOIN creature_default_trainer cdt ON cdt.CreatureId = ctm.CreatureID "
        "JOIN trainer t ON t.Id = cdt.TrainerId AND t.Type = %d "
        "JOIN creature_template ct ON ct.entry = ctm.CreatureID "
        "WHERE ctm.Idx = 0 AND ctm.CreatureDisplayID > 0 "
        "GROUP BY ctm.CreatureDisplayID ORDER BY MIN(ct.name) LIMIT %d"
        % (TYPE_CLASS, int(limit)))
    return [{"value": r[0],
             "label": "%s%s" % (r[1], (" - %s" % r[2]) if r[2] else "")}
            for r in rows]


def list_trainers(db, settings):
    """The trainers this tool made, with what each one teaches."""
    ensure_schema(db)
    _, rows = db.query(
        "SELECT s.trainer_id, s.creature_entry, s.name, s.subname, s.class_id, "
        "       s.display_id, s.faction, "
        "       (SELECT COUNT(*) FROM trainer_spell ts WHERE ts.TrainerId = s.trainer_id), "
        "       (SELECT COUNT(*) FROM creature c WHERE c.id = s.creature_entry), "
        "       (t.Id IS NOT NULL) "
        "FROM %s s LEFT JOIN trainer t ON t.Id = s.trainer_id "
        "ORDER BY s.trainer_id" % TRAINER_TABLE)
    return [{"trainer_id": r[0], "creature_entry": r[1], "name": r[2],
             "subname": r[3], "class_id": r[4],
             "class_label": CLASS_LABELS.get(r[4], "Any class"),
             "display_id": r[5], "faction": r[6], "spells": r[7], "spawns": r[8],
             "in_world": bool(r[9]), "ours": True,
             "spawn_command": ".npc add %d" % r[1]}
            for r in rows]


def list_core_trainers(db, settings):
    """Trainers the game shipped, named by an NPC that uses them.

    Offered so a custom spell can be taught by the trainer a player already
    visits. Only ever added to, never edited.
    """
    band = settings.trainer_band
    _, rows = db.query(
        "SELECT t.Id, t.Type, t.Requirement, MIN(ct.name), MIN(ct.subname), "
        "       COUNT(DISTINCT cdt.CreatureId) "
        "FROM trainer t "
        "JOIN creature_default_trainer cdt ON cdt.TrainerId = t.Id "
        "JOIN creature_template ct ON ct.entry = cdt.CreatureId "
        "WHERE t.Id NOT BETWEEN %d AND %d %s "
        "GROUP BY t.Id, t.Type, t.Requirement ORDER BY MIN(ct.name)"
        % (band.start, band.end, _NOT_JUNK))
    out = []
    for trainer_id, type_, requirement, name, subname, creatures in rows:
        label = name or ("Trainer %d" % trainer_id)
        if subname:
            label += " - %s" % subname
        if creatures > 1:
            label += " (+%d more)" % (creatures - 1)
        out.append({"trainer_id": trainer_id, "name": label, "type": type_,
                    "requirement": requirement, "creatures": creatures,
                    "class_id": requirement if type_ == TYPE_CLASS else 0,
                    "ours": False})
    return out


# -- making one ------------------------------------------------------------

def _npcflag(class_id):
    flags = NPC_FLAG_GOSSIP | NPC_FLAG_TRAINER
    return flags | NPC_FLAG_TRAINER_CLASS if class_id else flags


def creature_statements(donor_cols, donor, entry, name, subname, class_id, faction):
    """The INSERT that makes the NPC, built from a stock trainer plus our own fields."""
    chosen = {
        "entry": entry,
        "name": name,
        "subname": subname or None,
        "IconName": None,
        "gossip_menu_id": 0,          # menu 0 carries the default "Train me!"
        "minlevel": TRAINER_LEVEL,
        "maxlevel": TRAINER_LEVEL,
        "faction": faction,
        "npcflag": _npcflag(class_id),
        "AIName": "",
        "ScriptName": "",
        "MovementType": 0,
        "VerifiedBuild": 0,
    }
    for column in _OWN_COLUMNS:
        chosen.setdefault(column, 0)
    values = [chosen[c] if c in chosen else donor[c] for c in donor_cols]
    return [
        "DELETE FROM `creature_template` WHERE `entry` = %d" % entry,
        "INSERT INTO `creature_template` (%s)\nVALUES (%s)"
        % (", ".join("`%s`" % c for c in donor_cols),
           ", ".join(quote(v) for v in values)),
    ]


def trainer_statements(trainer_id, entry, class_id, greeting, display_id):
    type_ = TYPE_CLASS if class_id else TYPE_TRADESKILL
    return [
        "DELETE FROM `creature_template_model` WHERE `CreatureID` = %d" % entry,
        "INSERT INTO `creature_template_model` "
        "(`CreatureID`, `Idx`, `CreatureDisplayID`, `DisplayScale`, `Probability`)\n"
        "VALUES (%d, 0, %d, 1, 1)" % (entry, display_id),
        "DELETE FROM `trainer` WHERE `Id` = %d" % trainer_id,
        "INSERT INTO `trainer` (`Id`, `Type`, `Requirement`, `Greeting`)\n"
        "VALUES (%d, %d, %d, %s)"
        % (trainer_id, type_, class_id, quote(greeting or "")),
        "DELETE FROM `creature_default_trainer` WHERE `CreatureId` = %d" % entry,
        "INSERT INTO `creature_default_trainer` (`CreatureId`, `TrainerId`)\n"
        "VALUES (%d, %d)" % (entry, trainer_id),
    ]


def as_text(statements):
    return ";\n".join(statements) + ";"


def class_trainers(db, settings, class_id):
    """The trainer lists the game already ships that suit an ability.

    A Priest ability belongs on the trainers Priests already visit: two lists
    between 42 NPCs, and adding a row to each puts it in front of all of them.

    An ability with no class restriction belongs on *every* class trainer, for
    the same reason - whoever the player is, their own trainer is the one they
    already visit. Only class trainers, though: the game's other 118 lists teach
    cooking, riding and pets, and none of those should be handing out abilities.

    Nothing shipped is edited. A row is added beside theirs, and recorded so it
    can be taken back.
    """
    band = settings.trainer_band
    restriction = ("AND t.Requirement = %d" % int(class_id)) if class_id else ""
    _, rows = db.query(
        "SELECT t.Id, COUNT(DISTINCT cdt.CreatureId), MIN(ct.name) "
        "FROM trainer t "
        "JOIN creature_default_trainer cdt ON cdt.TrainerId = t.Id "
        "JOIN creature_template ct ON ct.entry = cdt.CreatureId "
        "WHERE t.Type = %d %s AND t.Id NOT BETWEEN %d AND %d "
        "GROUP BY t.Id ORDER BY COUNT(DISTINCT cdt.CreatureId) DESC"
        % (TYPE_CLASS, restriction, band.start, band.end))
    return [{"trainer_id": r[0], "npcs": r[1], "name": r[2], "ours": False}
            for r in rows if r[1]]


def ensure_trainer_for(db, settings, class_id):
    """A trainer of our own that can teach this class, making one if there is none.

    The fallback for an ability no shipped trainer covers - one open to any
    class, or a class the game has no trainer for. Restricted to the same class
    as the ability so the two cannot disagree.
    """
    ensure_schema(db)
    class_id = int(class_id or 0)
    for existing in list_trainers(db, settings):
        if existing["class_id"] == class_id:
            return existing, False
    label = CLASS_LABELS.get(class_id) if class_id else None
    made = create_trainer(db, settings, name="%s Trainer" % (label or "Ability"),
                          subname="Spellweaver", class_id=class_id)
    return made, True


def create_trainer(db, settings, name, subname="", class_id=0, display_id=None,
                   faction=DEFAULT_FACTION, greeting=""):
    """Write a trainer NPC of our own, and remember that we wrote it."""
    ensure_schema(db)
    name = (name or "").strip()
    if not name:
        raise TrainerError("A trainer needs a name.")
    class_id = int(class_id or 0)
    if class_id and class_id not in CLASS_LABELS:
        raise TrainerError("There is no class %d." % class_id)
    models = model_options(db)
    if display_id is None:
        display_id = models[0]["value"] if models else 0
    if not int(display_id):
        raise TrainerError("A trainer needs a look; no display id was given.")

    intruders = band_conflicts(db, settings)
    if intruders["creatures"] or intruders["trainers"]:
        raise TrainerError(
            "The reserved bands already hold rows this tool did not create "
            "(creatures %s, trainers %s). Change creature_band or trainer_band "
            "in spellweaver.json."
            % (intruders["creatures"][:3] or "-", intruders["trainers"][:3] or "-"))

    entry = next_creature_entry(db, settings.creature_band)
    trainer_id = next_trainer_id(db, settings.trainer_band)
    _guard(settings.creature_band, entry, "creature")
    _guard(settings.trainer_band, trainer_id, "trainer")

    donor_cols, donor = _donor_row(db)
    statements = creature_statements(donor_cols, donor, entry, name, subname,
                                     class_id, int(faction))
    statements += trainer_statements(trainer_id, entry, class_id, greeting,
                                     int(display_id))
    for statement in statements:
        db.execute(statement)

    now = int(time.time())
    db.execute(
        "INSERT INTO %s (trainer_id, creature_entry, name, subname, class_id, "
        "display_id, faction, created_at, updated_at) "
        "VALUES (%d, %d, %s, %s, %d, %d, %d, %d, %d)"
        % (TRAINER_TABLE, trainer_id, entry, quote(name), quote(subname or ""),
           class_id, int(display_id), int(faction), now, now))
    return {"trainer_id": trainer_id, "creature_entry": entry, "name": name,
            "subname": subname or "", "class_id": class_id,
            "class_label": CLASS_LABELS.get(class_id, "Anyone"),
            "display_id": int(display_id), "faction": int(faction),
            "spawn_command": ".npc add %d" % entry, "sql": as_text(statements)}


def delete_trainer(db, settings, trainer_id):
    """Take back everything we wrote for one trainer, spawns included."""
    ensure_schema(db)
    trainer_id = int(trainer_id)
    _guard(settings.trainer_band, trainer_id, "trainer")
    _, rows = db.query(
        "SELECT creature_entry FROM %s WHERE trainer_id = %d" % (TRAINER_TABLE, trainer_id))
    if not rows:
        raise TrainerError("Trainer %d was not made by spellweaver." % trainer_id)
    entry = rows[0][0]
    _guard(settings.creature_band, entry, "creature")
    _, rows = db.query("SELECT COUNT(*) FROM creature WHERE id = %d" % entry)
    spawns = rows[0][0]
    for statement in (
            "DELETE FROM trainer_spell WHERE TrainerId = %d" % trainer_id,
            "DELETE FROM %s WHERE trainer_id = %d" % (TRAINING_TABLE, trainer_id),
            "DELETE FROM creature_default_trainer WHERE CreatureId = %d" % entry,
            "DELETE FROM trainer WHERE Id = %d" % trainer_id,
            "DELETE FROM creature WHERE id = %d" % entry,
            "DELETE FROM creature_template_model WHERE CreatureID = %d" % entry,
            "DELETE FROM creature_template WHERE entry = %d" % entry,
            "DELETE FROM %s WHERE trainer_id = %d" % (TRAINER_TABLE, trainer_id)):
        db.execute(statement)
    return {"trainer_id": trainer_id, "creature_entry": entry, "spawns": spawns}


# -- teaching a spell ------------------------------------------------------

def training_statements(spell_id, trainer_id, money_cost, req_level):
    return [
        "DELETE FROM `trainer_spell` WHERE `TrainerId` = %d AND `SpellId` = %d"
        % (trainer_id, spell_id),
        "INSERT INTO `trainer_spell` "
        "(`TrainerId`, `SpellId`, `MoneyCost`, `ReqSkillLine`, `ReqSkillRank`, "
        "`ReqAbility1`, `ReqAbility2`, `ReqAbility3`, `ReqLevel`)\n"
        "VALUES (%d, %d, %d, 0, 0, 0, 0, 0, %d)"
        % (trainer_id, spell_id, max(0, int(money_cost)), max(0, min(80, int(req_level)))),
    ]


def set_training(db, settings, spell_id, trainer_ids, money_cost, req_level):
    """Put one ability on every given trainer's list, replacing what was there.

    Several trainers is the ordinary case rather than the exception: a class has
    more than one shipped list and the ability belongs on all of them.
    """
    ensure_schema(db)
    spell_id = int(spell_id)
    _guard(settings.band, spell_id, "spell")
    if isinstance(trainer_ids, int):
        trainer_ids = [trainer_ids]
    wanted = [int(t) for t in trainer_ids if int(t)]
    if not wanted:
        raise TrainerError("No trainer to teach %d." % spell_id)
    for trainer_id in wanted:
        if not _trainer_exists(db, trainer_id):
            raise TrainerError("There is no trainer %d." % trainer_id)

    clear_training(db, spell_id)
    cost = max(0, int(money_cost))
    level = max(0, min(80, int(req_level)))
    statements = []
    now = int(time.time())
    for trainer_id in wanted:
        statements.extend(training_statements(spell_id, trainer_id, cost, level))
        db.execute(
            "INSERT INTO %s (spell_id, trainer_id, money_cost, req_level, updated_at) "
            "VALUES (%d, %d, %d, %d, %d) ON DUPLICATE KEY UPDATE "
            "money_cost = VALUES(money_cost), req_level = VALUES(req_level), "
            "updated_at = VALUES(updated_at)"
            % (TRAINING_TABLE, spell_id, trainer_id, cost, level, now))
    for statement in statements:
        db.execute(statement)
    return {"spell_id": spell_id, "trainer_ids": wanted, "money_cost": cost,
            "req_level": level, "sql": as_text(statements)}


def _trainer_exists(db, trainer_id):
    _, rows = db.query("SELECT 1 FROM trainer WHERE Id = %d" % int(trainer_id))
    return bool(rows)


def clear_training(db, spell_id):
    """Remove this spell from every list we put it on, and nothing else."""
    ensure_schema(db)
    spell_id = int(spell_id)
    _, rows = db.query(
        "SELECT trainer_id FROM %s WHERE spell_id = %d" % (TRAINING_TABLE, spell_id))
    for (trainer_id,) in rows:
        db.execute("DELETE FROM trainer_spell WHERE TrainerId = %d AND SpellId = %d"
                   % (trainer_id, spell_id))
    db.execute("DELETE FROM %s WHERE spell_id = %d" % (TRAINING_TABLE, spell_id))
    return [r[0] for r in rows]


def training_for(db, spell_id):
    """Every trainer this ability is on, or None."""
    ensure_schema(db)
    _, rows = db.query(
        "SELECT w.trainer_id, w.money_cost, w.req_level, "
        "       COALESCE(s.name, MIN(ct.name), CONCAT('Trainer ', w.trainer_id)), "
        "       (s.trainer_id IS NOT NULL), COUNT(DISTINCT cdt.CreatureId) "
        "FROM %s w "
        "LEFT JOIN %s s ON s.trainer_id = w.trainer_id "
        "LEFT JOIN creature_default_trainer cdt ON cdt.TrainerId = w.trainer_id "
        "LEFT JOIN creature_template ct ON ct.entry = cdt.CreatureId "
        "WHERE w.spell_id = %d "
        "GROUP BY w.trainer_id, w.money_cost, w.req_level, s.name, s.trainer_id"
        % (TRAINING_TABLE, TRAINER_TABLE, int(spell_id)))
    if not rows:
        return None
    trainers_on = [{"trainer_id": r[0], "name": r[3], "ours": bool(r[4]), "npcs": r[5]}
                   for r in rows]
    return {"trainer_ids": [t["trainer_id"] for t in trainers_on],
            "money_cost": rows[0][1], "req_level": rows[0][2],
            "trainers": trainers_on,
            "npcs": sum(t["npcs"] for t in trainers_on),
            "ours": all(t["ours"] for t in trainers_on)}


def taught_by(db, trainer_id):
    """The spellweaver spells on one trainer's list."""
    ensure_schema(db)
    _, rows = db.query(
        "SELECT w.spell_id, w.money_cost, w.req_level, s.name "
        "FROM %s w LEFT JOIN spellweaver_spell s ON s.spell_id = w.spell_id "
        "WHERE w.trainer_id = %d ORDER BY w.spell_id" % (TRAINING_TABLE, int(trainer_id)))
    return [{"spell_id": r[0], "money_cost": r[1], "req_level": r[2],
             "name": r[3] or "Spell %d" % r[0]} for r in rows]
