"""Trying a database connection, and saying plainly what went wrong.

A connection can fail in a handful of ways and they want different answers from
the user: a wrong port is not a wrong password, and a database that exists but
holds no spells is neither. MySQL's own errors are precise but not friendly, so
the ones that actually happen are translated and the rest are passed through
rather than swallowed.
"""
from . import mysql
from .config import DbConfig

# MySQL server error codes worth recognising.
ACCESS_DENIED = 1045
UNKNOWN_DATABASE = 1049


def config_from(values, fallback=None):
    """A DbConfig from what the form sent, keeping the saved password if blank."""
    fallback = fallback or DbConfig()
    password = values.get("password")
    if password in (None, ""):
        password = fallback.password
    return DbConfig(
        host=(values.get("host") or fallback.host).strip(),
        port=int(values.get("port") or fallback.port),
        user=(values.get("user") or fallback.user).strip(),
        password=password,
        world_db=(values.get("world_db") or fallback.world_db).strip(),
        characters_db=(values.get("characters_db") or fallback.characters_db).strip(),
    )


def test(cfg):
    """Connect and look around. Returns (ok, message, details)."""
    details = []
    try:
        with mysql.connect(cfg) as db:
            _, rows = db.query("SELECT VERSION()")
            details.append("MySQL %s" % rows[0][0])
            _, rows = db.query("SHOW TABLES LIKE 'spell_dbc'")
            if not rows:
                return (False,
                        "Connected to %s, but it has no spell_dbc table. Is that "
                        "the world database?" % cfg.world_db, details)
            _, rows = db.query("SELECT COUNT(*) FROM spell_dbc")
            details.append("%s holds %d spells" % (cfg.world_db, rows[0][0]))
            _, rows = db.query("SHOW TABLES LIKE 'skilllineability_dbc'")
            if not rows:
                details.append(
                    "Note: no skilllineability_dbc table, so abilities cannot be "
                    "tied to a class. That table comes with AzerothCore.")
    except mysql.MySQLError as exc:
        return False, _explain(cfg, exc), details
    except OSError as exc:
        return (False,
                "Could not reach MySQL at %s:%d. Is the server running and the "
                "port right? (%s)" % (cfg.host, cfg.port, exc), details)

    try:
        with mysql.connect(cfg, database=cfg.characters_db) as db:
            _, rows = db.query("SHOW TABLES LIKE 'characters'")
            details.append("%s %s" % (cfg.characters_db,
                                      "looks right" if rows else "has no characters table"))
    except (mysql.MySQLError, OSError) as exc:
        details.append("Could not open %s: %s" % (cfg.characters_db, _short(exc)))

    return True, "Connected to %s on %s:%d." % (cfg.world_db, cfg.host, cfg.port), details


def _explain(cfg, exc):
    text = str(exc)
    if "MySQL error %d" % ACCESS_DENIED in text:
        return ("MySQL refused the username or password for %s@%s."
                % (cfg.user, cfg.host))
    if "MySQL error %d" % UNKNOWN_DATABASE in text:
        return "There is no database named %s on %s." % (cfg.world_db, cfg.host)
    return text


def _short(exc):
    text = str(exc)
    return text if len(text) < 120 else text[:117] + "..."
