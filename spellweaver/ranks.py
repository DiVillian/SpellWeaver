"""Ranks: one ability, several spells, chained so the game treats them as one.

3.3.5a has no level scaling for player abilities. The way the game makes a spell
grow with a character is ranks: Fireball is six separate spells, each with its
own id, level, cost and damage, linked so that learning a higher one replaces the
lower one in the spellbook and a trainer only offers the next.

The link is `spell_ranks`, a core table of (first_spell_id, spell_id, rank). The
server builds its chain from it, and everything else follows: `GetPrevSpellInChain`
is what makes a trainer withhold rank 4 until rank 3 is known, and the chain is
what makes learning rank 4 deactivate rank 3 and send the client a
SMSG_SUPERCEDED_SPELL so only the highest shows.

Two rules the core enforces, so we do too:

* ranks must be 1..N with no gaps, in order
* a chain of one is rejected outright - "entry is not needed" - so a
  single-rank ability writes no rows here at all, and behaves exactly as an
  ability did before ranks existed

`SkillLineAbility.SupercededBySpell` is *not* how this works, whatever its name
suggests: stock Fireball leaves it at zero for every rank.
"""
import time

# `rank` became a reserved word in MySQL 8, so it is quoted everywhere it is
# named - in our table and in the core's.
CHAIN_TABLE = "spell_ranks"
TABLE = "spellweaver_rank"

SCHEMA = """
CREATE TABLE IF NOT EXISTS %s (
  spell_id       INT UNSIGNED NOT NULL,
  `rank`         TINYINT UNSIGNED NOT NULL,
  rank_spell_id  INT UNSIGNED NOT NULL,
  updated_at     INT UNSIGNED NOT NULL,
  PRIMARY KEY (spell_id, `rank`),
  UNIQUE KEY (rank_spell_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""" % TABLE

# The core stops reading a chain that is not 1..N in order, so a rank line is
# only ever written whole.
MIN_CHAIN = 2


class RankError(Exception):
    pass


def ensure_schema(db):
    db.execute(SCHEMA)


def rank_text(rank, total):
    """What the game writes under the name. Nothing, for an ability of one rank."""
    return "Rank %d" % rank if total > 1 else ""


def chain_statements(ids):
    """The rows that link a rank line, shown before they run."""
    first = ids[0]
    statements = ["DELETE FROM `%s` WHERE `first_spell_id` = %d" % (CHAIN_TABLE, first)]
    if len(ids) >= MIN_CHAIN:
        values = ", ".join("(%d, %d, %d)" % (first, spell_id, rank)
                           for rank, spell_id in enumerate(ids, start=1))
        statements.append(
            "INSERT INTO `%s` (`first_spell_id`, `spell_id`, `rank`)\nVALUES %s"
            % (CHAIN_TABLE, values))
    return ";\n".join(statements) + ";"


def set_chain(db, band, ids):
    """Link the ranks, and remember which ids belong to this ability."""
    ensure_schema(db)
    ids = [int(i) for i in ids]
    for spell_id in ids:
        if not band.contains(spell_id):
            raise RankError(
                "Refusing to chain %d: outside the reserved band %s." % (spell_id, band))
    first = ids[0]
    db.execute("DELETE FROM %s WHERE first_spell_id = %d" % (CHAIN_TABLE, first))
    if len(ids) >= MIN_CHAIN:
        db.execute(
            "INSERT INTO %s (first_spell_id, spell_id, `rank`) VALUES %s"
            % (CHAIN_TABLE, ", ".join("(%d, %d, %d)" % (first, spell_id, rank)
                                      for rank, spell_id in enumerate(ids, start=1))))
    now = int(time.time())
    db.execute("DELETE FROM %s WHERE spell_id = %d" % (TABLE, first))
    db.execute(
        "INSERT INTO %s (spell_id, `rank`, rank_spell_id, updated_at) VALUES %s"
        % (TABLE, ", ".join("(%d, %d, %d, %d)" % (first, rank, spell_id, now)
                            for rank, spell_id in enumerate(ids, start=1))))
    return ids


def clear_chain(db, first_id):
    """Forget a rank line, leaving nothing of it in the core table."""
    ensure_schema(db)
    first_id = int(first_id)
    db.execute("DELETE FROM %s WHERE first_spell_id = %d" % (CHAIN_TABLE, first_id))
    db.execute("DELETE FROM %s WHERE spell_id = %d" % (TABLE, first_id))


def ids_for(db, first_id):
    """Every spell id this ability occupies, rank 1 first."""
    ensure_schema(db)
    _, rows = db.query(
        "SELECT `rank`, rank_spell_id FROM %s WHERE spell_id = %d ORDER BY `rank`"
        % (TABLE, int(first_id)))
    return [r[1] for r in rows] or [int(first_id)]


def owner_of(db, rank_spell_id):
    """Which ability a rank id belongs to, and which rank it is."""
    ensure_schema(db)
    _, rows = db.query(
        "SELECT spell_id, `rank` FROM %s WHERE rank_spell_id = %d" % (TABLE, int(rank_spell_id)))
    return (rows[0][0], rows[0][1]) if rows else (None, None)


def taken_ids(db):
    """Every id held by a rank, so a new ability does not land on one."""
    ensure_schema(db)
    _, rows = db.query("SELECT rank_spell_id FROM %s" % TABLE)
    return {r[0] for r in rows}
