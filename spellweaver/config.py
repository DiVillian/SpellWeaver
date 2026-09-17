"""Locating the server, its credentials, and the id ranges we care about.

Credentials are asked for once and kept in `data/connection.json`, readable only
by the user who saved them. Nothing here assumes a particular machine.

An AzerothCore `.env` file is read when one happens to be present, but only to
pre-fill the form: it is a convenience for the common case of running this beside
a docker-compose install, never the only way in. A person with a database
somewhere else types the details and the tool works.

Precedence, strongest first: environment variables, then whatever the user saved
through the setup step, then `spellweaver.json`, then the `.env`, then plain
defaults. What somebody typed in has to beat a file they may never have opened.

Three reserved id ranges matter, and none is safe to hardcode:

* The reserved band we write into. 950000 is only free on *this* server; another
  install may have a module sitting there. It is configurable, and checked
  against the database before we write anything.
* The creature and trainer bands, which step 3 writes its trainer NPCs into.
  They are checked the same way and for the same reason.
* The stock ceiling, above which spell data is assumed to belong to a module
  rather than to Blizzard. The vocabulary must be derived from stock 3.3.5a data
  only, or the tool learns one server's quirks and stops being portable. This
  matters more than it looks: once this tool ships a client patch of its own,
  anyone re-extracting DBCs from a patched client would otherwise feed our own
  invented spells back in as if they were evidence.
"""
import json
import os
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_ENV = pathlib.Path.home() / "AzerothCore/azerothcore-wotlk/.env"
DEFAULT_CORE_SRC = pathlib.Path.home() / "AzerothCore/azerothcore-wotlk/src"
# Three files, split by who they belong to: the project, this machine, and
# nobody but the person who typed them.
CONFIG_FILE = ROOT / "spellweaver.json"          # shared defaults, worth keeping
LOCAL_FILE = ROOT / "data/local.json"            # paths on this machine
CONNECTION_FILE = ROOT / "data/connection.json"  # credentials

# Stock 3.3.5a (build 12340) Spell.dbc tops out at id 80864. Anything at or
# above this belongs to a module, a client patch, or to us.
DEFAULT_STOCK_CEILING = 100000
DEFAULT_BAND_START = 950000
DEFAULT_BAND_END = 999999

# Trainer NPCs need two more bands: one for the creature templates and one for
# the trainer lists they point at. Both are small; a server needs few trainers.
DEFAULT_CREATURE_BAND_START = 1950000
DEFAULT_CREATURE_BAND_END = 1950999
DEFAULT_TRAINER_BAND_START = 9500
DEFAULT_TRAINER_BAND_END = 9599


@dataclass(frozen=True)
class Band:
    start: int
    end: int

    def contains(self, spell_id):
        return self.start <= spell_id <= self.end

    def __str__(self):
        return "%d-%d" % (self.start, self.end)


@dataclass(frozen=True)
class DbConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    world_db: str = "acore_world"
    characters_db: str = "acore_characters"


@dataclass(frozen=True)
class Settings:
    db: DbConfig
    band: Band
    stock_ceiling: int
    # Where the credentials came from, so the tool can say so rather than
    # leaving the user to guess which of several places it read.
    db_source: str = "none"
    # Where the player's client lives, for the patch that makes spells visible.
    # Empty means none configured yet; the tool asks rather than guessing.
    client_dir: str = ""
    client_locale: str = ""
    client_patch: str = ""
    client_manifest: str = str(ROOT / "data/client_patch.json")
    # An AzerothCore source checkout, if there is one to read. Optional: it only
    # sharpens the validator.
    core_src: str = ""
    # What to run to restart the worldserver. The user's own command, kept on
    # this machine only, and never taken from a request: the page can ask for
    # it to be run, but cannot say what it is.
    restart_command: str = ""
    icon_cache: str = str(ROOT / "data/icons")
    creature_band: Band = Band(DEFAULT_CREATURE_BAND_START, DEFAULT_CREATURE_BAND_END)
    trainer_band: Band = Band(DEFAULT_TRAINER_BAND_START, DEFAULT_TRAINER_BAND_END)

    def is_stock(self, spell_id):
        """Whether a spell id is stock game data rather than module content."""
        return spell_id < self.stock_ceiling


def _parse_env(path):
    """Read KEY=VALUE lines from a docker-style .env file."""
    values = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Strip one layer of matching quotes, as docker-compose does.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def _local_config():
    """Settings that belong to this machine and travel with nobody."""
    if not LOCAL_FILE.is_file():
        return {}
    try:
        values = json.loads(LOCAL_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def saved_connection():
    """Connection details entered by the user, if they have been."""
    if not CONNECTION_FILE.is_file():
        return None
    try:
        values = json.loads(CONNECTION_FILE.read_text())
    except (OSError, ValueError):
        return None
    return values if isinstance(values, dict) else None


def env_file_connection(env_path=None):
    """What an AzerothCore .env can tell us, when there is one to read."""
    path = pathlib.Path(env_path or os.environ.get("SPELLWEAVER_ENV") or DEFAULT_ENV)
    if not path.is_file():
        return None
    env = _parse_env(path)
    password = env.get("DOCKER_DB_ROOT_PASSWORD")
    if not password:
        return None
    return {
        "host": env.get("SPELLWEAVER_DB_HOST", "127.0.0.1"),
        "port": int(env.get("SPELLWEAVER_DB_PORT", "3306")),
        "user": env.get("SPELLWEAVER_DB_USER", "root"),
        "password": password,
        "world_db": env.get("SPELLWEAVER_WORLD_DB", "acore_world"),
        "characters_db": env.get("SPELLWEAVER_CHARACTERS_DB", "acore_characters"),
        "path": str(path),
    }


def save_connection(values):
    """Keep the details, readable only by whoever saved them."""
    CONNECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: values.get(k) for k in
            ("host", "port", "user", "password", "world_db", "characters_db")}
    keep["port"] = int(keep.get("port") or 3306)
    CONNECTION_FILE.write_text(json.dumps(keep, indent=2))
    try:
        CONNECTION_FILE.chmod(0o600)
    except OSError:
        pass                                   # a filesystem that has no opinion
    return CONNECTION_FILE


def forget_connection():
    if CONNECTION_FILE.is_file():
        CONNECTION_FILE.unlink()


def _file_config():
    if not CONFIG_FILE.is_file():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except ValueError as exc:
        raise SystemExit("%s is not valid JSON: %s" % (CONFIG_FILE, exc))


def _setting(file_cfg, section, key, env_name, default, cast=int):
    """Resolve one setting: environment wins, then the config file, then default."""
    if env_name in os.environ:
        return cast(os.environ[env_name])
    if section:
        value = file_cfg.get(section, {}).get(key)
    else:
        value = file_cfg.get(key)
    return default if value is None else cast(value)


def load_settings(env_path=None):
    """Everything the tool needs to run, from wherever it can be found.

    Never raises for missing credentials. A tool that will not start without
    them cannot ask for them, and asking is the whole point of the setup step.
    """
    saved = saved_connection()
    from_env = env_file_connection(env_path)
    base = saved or from_env or {}
    source = "saved" if saved else ("env" if from_env else "none")

    file_cfg = _file_config()

    def _db_setting(key, env_name, default, cast=str):
        """Environment, then what was saved, then the settings file, then a default."""
        if env_name in os.environ:
            return cast(os.environ[env_name])
        if saved and saved.get(key) not in (None, ""):
            return cast(saved[key])
        from_file = file_cfg.get("database", {}).get(key)
        if from_file not in (None, ""):
            return cast(from_file)
        if from_env and from_env.get(key) not in (None, ""):
            return cast(from_env[key])
        return cast(default)

    db = DbConfig(
        host=_db_setting("host", "SPELLWEAVER_DB_HOST", "127.0.0.1"),
        port=_db_setting("port", "SPELLWEAVER_DB_PORT", 3306, int),
        user=_db_setting("user", "SPELLWEAVER_DB_USER", "root"),
        password=_db_setting("password", "SPELLWEAVER_DB_PASSWORD", ""),
        world_db=_db_setting("world_db", "SPELLWEAVER_WORLD_DB", "acore_world"),
        characters_db=_db_setting("characters_db", "SPELLWEAVER_CHARACTERS_DB",
                                  "acore_characters"),
    )
    if source == "none" and (db.password or "SPELLWEAVER_DB_HOST" in os.environ):
        source = "environment"

    band = Band(
        start=_setting(file_cfg, "band", "start", "SPELLWEAVER_BAND_START", DEFAULT_BAND_START),
        end=_setting(file_cfg, "band", "end", "SPELLWEAVER_BAND_END", DEFAULT_BAND_END),
    )
    if band.start > band.end:
        raise SystemExit("Reserved band %s is empty: start is above end." % band)
    creature_band = Band(
        start=_setting(file_cfg, "creature_band", "start", "SPELLWEAVER_CREATURE_BAND_START",
                       DEFAULT_CREATURE_BAND_START),
        end=_setting(file_cfg, "creature_band", "end", "SPELLWEAVER_CREATURE_BAND_END",
                     DEFAULT_CREATURE_BAND_END),
    )
    trainer_band = Band(
        start=_setting(file_cfg, "trainer_band", "start", "SPELLWEAVER_TRAINER_BAND_START",
                       DEFAULT_TRAINER_BAND_START),
        end=_setting(file_cfg, "trainer_band", "end", "SPELLWEAVER_TRAINER_BAND_END",
                     DEFAULT_TRAINER_BAND_END),
    )
    for label, other in (("Creature band", creature_band), ("Trainer band", trainer_band)):
        if other.start > other.end:
            raise SystemExit("%s %s is empty: start is above end." % (label, other))
    ceiling = _setting(file_cfg, None, "stock_ceiling",
                       "SPELLWEAVER_STOCK_CEILING", DEFAULT_STOCK_CEILING)
    if ceiling > band.start:
        raise SystemExit(
            "stock_ceiling (%d) is above the reserved band start (%d), so the "
            "tool would treat its own spells as stock data." % (ceiling, band.start))

    local_cfg = _local_config()

    def _client_setting(key, env_name):
        """Environment, then this machine, then a shared default."""
        if env_name in os.environ:
            return str(os.environ[env_name])
        here = local_cfg.get("client", {}).get(key)
        if here:
            return str(here)
        return str(file_cfg.get("client", {}).get(key) or "")

    core_src = _client_setting("core_src", "SPELLWEAVER_CORE_SRC") or str(DEFAULT_CORE_SRC)
    client_dir = _client_setting("path", "SPELLWEAVER_CLIENT")
    client_locale = _client_setting("locale", "SPELLWEAVER_CLIENT_LOCALE")
    client_patch = _client_setting("patch", "SPELLWEAVER_CLIENT_PATCH")
    restart_command = str(
        os.environ.get("SPELLWEAVER_RESTART")
        or local_cfg.get("server", {}).get("restart_command")
        or file_cfg.get("server", {}).get("restart_command") or "")
    return Settings(db=db, band=band, stock_ceiling=ceiling, db_source=source,
                    creature_band=creature_band, trainer_band=trainer_band,
                    client_dir=client_dir, client_locale=client_locale,
                    client_patch=client_patch, core_src=core_src,
                    restart_command=restart_command)


def save_setting(section, key, value):
    """Write one value into the local settings, leaving everything else alone.

    Local rather than shared: a path to somebody's game client is true of one
    machine and wrong everywhere else.
    """
    current = _local_config()
    if section:
        current.setdefault(section, {})[key] = value
    else:
        current[key] = value
    LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_FILE.write_text(json.dumps(current, indent=2) + "\n")
    return LOCAL_FILE


def require_connection(settings):
    """Stop with something readable when nothing has been set up yet."""
    if settings.db_source == "none":
        raise SystemExit(
            "No database connection has been set up.\n"
            "Run `python3 run.py`, open the page and fill in the Connection tab,\n"
            "or set SPELLWEAVER_DB_HOST and friends in the environment.")
    return settings


def load_db_config(env_path=None):
    """Backwards-compatible shim for callers that only need the database."""
    return load_settings(env_path).db


def band_conflicts(db, band):
    """Spell ids already occupying the reserved band, and who is nearest to it.

    The band must be checked, not assumed: 950000 happens to be clear here
    because this server's modules stop at 948969, but that is a fact about this
    install and nothing else.
    """
    _, rows = db.query(
        "SELECT COUNT(*), COALESCE(MIN(ID), 0), COALESCE(MAX(ID), 0) "
        "FROM spell_dbc WHERE ID BETWEEN %d AND %d" % (band.start, band.end))
    count, lo, hi = rows[0]
    _, rows = db.query(
        "SELECT COALESCE(MAX(ID), 0) FROM spell_dbc WHERE ID < %d" % band.start)
    below = rows[0][0]
    return {"count": count, "min": lo, "max": hi, "nearest_below": below}
