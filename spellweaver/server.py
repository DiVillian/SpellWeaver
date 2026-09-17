"""A local web server for the spell builder.

Standard library only: there is no package manager available here, and a tool
that needs one before it will start is a tool the user cannot run.
"""
import json
import pathlib
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (api, classes, clientdata, clientpatch, icons as icons_module,
               ranks, store, trainers, visuals as visuals_module)
from . import config as config_module
from . import connection
from . import restart as restart_module
from . import client as client_module
from .client import ClientError
from .config import load_settings
from .corpus import Corpus
from .mysql import connect
from .pricing import Pricing, format_money
from .spell import SpellDef, resolve
from .tooltip import suggest_description
from .vocabulary import Vocabulary

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".json": "application/json",
                 ".svg": "image/svg+xml"}


class Context:
    """Shared, read-mostly state. The corpus takes a moment to load; do it once.

    Everything here needs a database, and on a fresh install there is not one
    yet. So loading is a step that can fail and be retried rather than something
    that happens on the way up: a tool that will not start without credentials
    cannot ask for them.
    """

    def __init__(self):
        self.settings = load_settings()
        self.ready = False
        self.problem = ""
        self.corpus = None
        self.vocab = None
        self.visuals = []
        self.pricing = None
        self._payload = {}
        self._lock = threading.Lock()
        self._icons = None
        self._icon_lock = threading.Lock()
        self.start()

    def start(self):
        """Load what needs the database. Returns whether it worked."""
        if self.settings.db_source == "none":
            self.ready, self.problem = False, "No database connection set up yet."
            return False
        absent = clientdata.missing()
        if absent:
            # Blizzard's files are not shipped with this project; they come out
            # of the client the tool is pointed at.
            self.ready = False
            self.problem = ("The game's data files are not here yet (%s). %s"
                            % (", ".join(absent[:3])
                               + ("..." if len(absent) > 3 else ""), clientdata.HOW))
            return False
        try:
            with self._db() as db:
                self.corpus = Corpus(db=db, stock_ceiling=self.settings.stock_ceiling)
                self.pricing = Pricing(db, self.settings)
                store.ensure_schema(db)
                trainers.ensure_schema(db)
            self.vocab = Vocabulary(self.corpus)
            self.visuals = visuals_module.extract(self.corpus)
            self._payload = api.vocabulary_payload(self.vocab, self.corpus, self.settings,
                                                   self.pricing, self.visuals)
            self.ready, self.problem = True, ""
        except Exception as exc:                # any failure leaves it askable
            self.ready = False
            self.problem = "%s: %s" % (type(exc).__name__, exc)
        return self.ready

    def reload(self):
        """Pick up credentials that have just been saved."""
        self.settings = load_settings()
        self._icons = None
        return self.start()

    def icons(self):
        """The icon library, opened the first time an icon is asked for.

        Opening a client's archives takes a moment, and a session may never look
        at an icon, so it is not done at startup.
        """
        with self._icon_lock:
            if self._icons is None:
                where = self.settings.client_dir or self._client_from_manifest()
                if not where:
                    raise icons_module.IconError(
                        "No client configured, so icons cannot be read. Set "
                        "\"client\": {\"path\": \"...\"} in spellweaver.json, or "
                        "build a client patch first.")
                self._icons = icons_module.IconLibrary(
                    where, self.settings.client_locale or None,
                    cache_dir=self.settings.icon_cache)
            return self._icons

    def _client_from_manifest(self):
        try:
            record = json.loads(pathlib.Path(self.settings.client_manifest).read_text())
        except (OSError, ValueError):
            return ""
        return record.get("client", "")

    def _db(self):
        return connect(self.settings.db)

    def db(self):
        return self._db()

    def vocabulary(self):
        return self._payload


class Handler(BaseHTTPRequestHandler):
    context = None
    server_version = "spellweaver"

    def log_message(self, fmt, *args):
        pass                                   # the console is for our own output

    # -- helpers --
    def _send(self, code, body, content_type="application/json", cache=None):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # Without a Cache-Control header a browser is free to invent one, and
        # they do: after updating the tool the page kept running the previous
        # version's script until the cache was cleared by hand. Only the icons
        # below are worth caching, and they say so for themselves.
        self.send_header("Cache-Control", cache or "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data))

    def _error(self, message, code=400):
        self._json({"error": message}, code)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _unready(self):
        """Refuse work that needs a database there is not one for yet."""
        self._json({"error": self.context.problem or "Not set up yet.",
                    "setup": True}, 503)
        return True

    # -- routing --
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/settings":
                return self._settings_state()
            if (path.startswith("/api/") and not path.startswith("/api/icon/")
                    and not self.context.ready):
                return self._unready()
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                # Served from web/static/, so the URL path maps straight through.
                return self._static(path.lstrip("/"))
            if path == "/api/vocabulary":
                return self._json(self.context.vocabulary())
            if path == "/api/spells":
                with self.context.db() as db:
                    return self._json({"spells": store.list_spells(db)})
            if path == "/api/trainers":
                with self.context.db() as db:
                    return self._json(api.trainers_payload(db, self.context.settings))
            if path.startswith("/api/trainers/"):
                trainer_id = int(path.rsplit("/", 1)[-1])
                with self.context.db() as db:
                    return self._json({"spells": trainers.taught_by(db, trainer_id)})
            if path.startswith("/api/spells/"):
                spell_id = int(path.rsplit("/", 1)[-1])
                with self.context.db() as db:
                    definition = store.load(db, spell_id)
                    training = trainers.training_for(db, spell_id)
                if definition is None:
                    return self._error("No spell %d" % spell_id, 404)
                return self._json({"definition": definition.to_dict(),
                                   "training": training})
            if path == "/api/clientpatch":
                asked = parse_qs(urlparse(self.path).query).get("client", [""])[0]
                with self.context.db() as db:
                    return self._json(clientpatch.status(db, self.context.settings,
                                                         asked.strip() or None))
            if path.startswith("/api/icon/"):
                return self._icon(path.rsplit("/", 1)[-1])
            if path == "/api/status":
                return self._status()
            return self._error("Not found", 404)
        except Exception as exc:                # a broken request must not kill the server
            return self._error("%s: %s" % (type(exc).__name__, exc), 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/settings/connection/test":
                return self._test_connection(save=False)
            if path == "/api/settings/connection":
                return self._test_connection(save=True)
            if path == "/api/settings/client":
                return self._save_client()
            if path == "/api/shutdown":
                return self._shutdown()
            if path == "/api/settings/restart-command":
                return self._save_restart_command()
            if path == "/api/restart":
                return self._restart()
            if path.startswith("/api/") and not self.context.ready:
                return self._unready()
            if path == "/api/preview":
                data = self._body()
                return self._json(api.preview_payload(data.get("definition", {}),
                                                      self.context.vocab,
                                                      training=data.get("training"),
                                                      visuals=self.context.visuals))
            if path == "/api/spells":
                return self._save()
            if path == "/api/trainers":
                return self._make_trainer()
            if path == "/api/clientpatch":
                return self._build_patch()
            return self._error("Not found", 404)
        except (trainers.TrainerError, clientpatch.PatchError, ClientError) as exc:
            # These are written for the user to read, so they are passed through
            # rather than dressed in an exception class name.
            return self._error(str(exc), 400)
        except Exception as exc:
            return self._error("%s: %s" % (type(exc).__name__, exc), 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/") and not self.context.ready:
                return self._unready()
            if path.startswith("/api/spells/"):
                spell_id = int(path.rsplit("/", 1)[-1])
                with self.context.db() as db:
                    store.delete(db, self.context.settings.band, spell_id)
                return self._json({"deleted": spell_id})
            if path.startswith("/api/trainers/"):
                trainer_id = int(path.rsplit("/", 1)[-1])
                with self.context.db() as db:
                    removed = trainers.delete_trainer(db, self.context.settings, trainer_id)
                return self._json(removed)
            return self._error("Not found", 404)
        except (store.BandError, trainers.TrainerError) as exc:
            return self._error(str(exc), 403)
        except Exception as exc:
            return self._error("%s: %s" % (type(exc).__name__, exc), 500)

    # -- actions --
    def _save_client(self):
        """Point the tool at a client, once, rather than on every action."""
        where = (self._body().get("path") or "").strip()
        ok, message, details = client_module.check(where)
        if ok:
            config_module.save_setting("client", "path", where)
            # A fresh clone has no game data at all, and this is the moment we
            # first know where to get it from.
            if clientdata.missing():
                try:
                    written, _locale = clientdata.extract(where)
                    details.append("Took %d data files from it, starting with %s."
                                   % (len(written),
                                      ", ".join(r["name"] for r in written[:3])))
                except clientdata.DataError as exc:
                    return self._json({"ok": False, "message": str(exc),
                                       "details": details, "saved": True})
            self.context.reload()
        return self._json({"ok": ok, "message": message, "details": details,
                           "saved": ok})

    def _save_restart_command(self):
        """Remember how this server is restarted. Blank forgets it."""
        command = (self._body().get("command") or "").strip()
        config_module.save_setting("server", "restart_command", command)
        self.context.reload()
        return self._json({
            "ok": True,
            "command": command,
            "message": ("Saved. Restart Server is on the Client patch page."
                        if command else
                        "Cleared. Restart Server will ask for a command again."),
        })

    def _restart(self):
        """Run the stored command. The request never says what to run."""
        try:
            return self._json(restart_module.run(self.context.settings))
        except restart_module.RestartError as exc:
            return self._error(str(exc), 400)

    def _settings_state(self):
        """What the settings page needs: what is set, and what could pre-fill it."""
        settings = self.context.settings
        found = config_module.env_file_connection()
        where = settings.client_dir or self._client_from_manifest_path()
        client_ok, client_message, client_details = client_module.check(where)
        return self._json({
            "client": {"path": settings.client_dir, "found": where,
                       "ok": client_ok, "message": client_message,
                       "details": client_details,
                       "remembered": bool(where and not settings.client_dir)},
            "restart": {"command": settings.restart_command,
                        "set": restart_module.configured(settings)},
            # How to start this again, for the page to quote when the server has
            # gone. It is the server's platform that decides, not the browser's:
            # a Windows browser is quite often pointed at a server on Linux, and
            # sending that person to a .bat would be sending them nowhere.
            "launcher": ("SpellWeaver.bat" if sys.platform == "win32"
                         else "python3 run.py"),
            "ready": self.context.ready,
            "problem": self.context.problem,
            "source": settings.db_source,
            "connection": {
                "host": settings.db.host, "port": settings.db.port,
                "user": settings.db.user, "world_db": settings.db.world_db,
                "characters_db": settings.db.characters_db,
                # The password is never sent back; an empty box means "keep it".
                "has_password": bool(settings.db.password),
            },
            "env": ({"path": found["path"], "host": found["host"],
                     "port": found["port"], "user": found["user"],
                     "world_db": found["world_db"],
                     "characters_db": found["characters_db"]} if found else None),
        })

    def _client_from_manifest_path(self):
        return self.context._client_from_manifest()

    def _test_connection(self, save):
        values = self._body()
        cfg = connection.config_from(values, self.context.settings.db)
        ok, message, details = connection.test(cfg)
        saved = False
        if ok and save:
            # Only keep details that work: persisting a broken connection just
            # moves the failure to the next thing the user tries.
            config_module.save_connection({
                "host": cfg.host, "port": cfg.port, "user": cfg.user,
                "password": cfg.password, "world_db": cfg.world_db,
                "characters_db": cfg.characters_db})
            saved = True
            self.context.reload()
        return self._json({"ok": ok, "message": message, "details": details,
                           "saved": saved, "ready": self.context.ready,
                           "problem": self.context.problem})

    def _save(self):
        body = self._body()
        data = body.get("definition", {})
        training = body.get("training")
        definition = SpellDef.from_dict(data)
        if not definition.name.strip():
            return self._error("An ability needs a name before it can be saved.", 400)
        settings = self.context.settings
        with self.context.db() as db:
            intruders = store.check_band_available(db, settings.band)
            if intruders and definition.spell_id not in intruders:
                return self._error(
                    "The reserved band %s already holds spells this tool did not "
                    "create (%s...). Change the band in spellweaver.json."
                    % (settings.band, ", ".join(str(i) for i in intruders[:3])), 409)
            try:
                outcome = store.save_ability(db, settings, definition, self.context.vocab)
            except (store.BandError, ranks.RankError) as exc:
                return self._error(str(exc), 403)
            # The ability is saved by this point. If putting it on a trainer
            # fails, say so rather than failing the whole call and leaving a
            # saved ability behind that the caller was told nothing about.
            taught, trouble = None, None
            try:
                taught = self._teach(db, outcome, definition, training)
            except (trainers.TrainerError, classes.ClassError) as exc:
                trouble = str(exc)
            except Exception as exc:                # noqa: BLE001 - reported, not hidden
                trouble = "%s: %s" % (type(exc).__name__, exc)

        warnings, notes = [], []
        for row in outcome["ranks"]:
            for warning in row["warnings"]:
                label = ("Rank %d: " % row["rank"]) if len(outcome["ranks"]) > 1 else ""
                if label + warning not in warnings:
                    warnings.append(label + warning)
            for note in row["notes"]:
                if note not in notes:
                    notes.append(note)
        if trouble:
            warnings.append("Saved, but it is not on a trainer: %s" % trouble)
        return self._json({"spell_id": outcome["spell_id"], "warnings": warnings,
                           "notes": notes, "training": taught,
                           "ids": outcome["ids"], "ranks": outcome["ranks"]})

    def _teach(self, db, outcome, definition, training):
        """Put every rank on trainers, which is the ordinary way to get one.

        Each rank is its own spell with its own level and price, exactly as the
        game's own ranks are. The chain does the rest: a trainer will not offer
        rank 4 to somebody who has not learned rank 3.
        """
        settings = self.context.settings
        rows = outcome["ranks"]
        if not training or not training.get("enabled", True):
            for row in rows:
                trainers.clear_training(db, row["spell_id"])
            return None

        scope = training.get("scope") or "auto"
        chosen = int(training.get("trainer_id") or 0)
        created = False
        made = None

        if scope == "one" and chosen:
            here = {t["trainer_id"]: t for t in trainers.list_trainers(db, settings)}
            made = here.get(chosen)
            on = [made] if made else [{"trainer_id": chosen,
                                       "name": "Trainer %d" % chosen, "npcs": 0,
                                       "ours": False}]
        else:
            on = [] if scope == "own" else trainers.class_trainers(
                db, settings, definition.class_id)
            if not on:
                made, created = trainers.ensure_trainer_for(db, settings,
                                                            definition.class_id)
                on = [dict(made, npcs=made.get("spawns", 0), ours=True)]

        trainer_ids = [t["trainer_id"] for t in on]
        taught_ranks = []
        for row in rows:
            cost = self._rank_price(definition, row, training)
            trainers.set_training(db, settings, row["spell_id"], trainer_ids, cost,
                                  row["spell_level"])
            taught_ranks.append({"rank": row["rank"], "spell_id": row["spell_id"],
                                 "money_cost": cost, "money": format_money(cost),
                                 "req_level": row["spell_level"]})

        first = taught_ranks[0]
        return {"spell_id": first["spell_id"], "trainer_ids": trainer_ids,
                "money_cost": first["money_cost"], "money": first["money"],
                "req_level": first["req_level"], "ranks": taught_ranks,
                "trainers": [{"trainer_id": t["trainer_id"], "name": t.get("name", ""),
                              "npcs": t.get("npcs", 0), "ours": bool(t.get("ours"))}
                             for t in on],
                "npcs": sum(t.get("npcs", 0) for t in on),
                "ours": all(t.get("ours") for t in on),
                "trainer_created": created,
                "spawn_command": (made or {}).get("spawn_command", "") if created or
                (made or {}).get("ours") else ""}

    def _rank_price(self, definition, row, training):
        """What this rank costs: what was typed for it, else the going rate."""
        if row["rank"] == 1:
            cost = training.get("money_cost")
        else:
            values = definition.extra_ranks[row["rank"] - 2]
            cost = values.price_copper if values.price_copper is not None else -1
            if cost is not None and cost < 0:
                cost = None
        if cost is None:
            cost = self.context.pricing.suggest(row["spell_level"])
        return max(0, int(cost))

    def _build_patch(self):
        """Build the patch and write it into the client, in one step."""
        data = self._body()
        with self.context.db() as db:
            record = clientpatch.build(
                db, self.context.settings,
                client_dir=(data.get("client_dir") or "").strip() or None,
                locale=(data.get("locale") or "").strip() or None)
        return self._json(record)

    def _make_trainer(self):
        data = self._body()
        with self.context.db() as db:
            made = trainers.create_trainer(
                db, self.context.settings,
                name=data.get("name", ""), subname=data.get("subname", ""),
                class_id=data.get("class_id") or 0,
                display_id=data.get("display_id"),
                faction=data.get("faction") or trainers.DEFAULT_FACTION,
                greeting=data.get("greeting", ""))
        return self._json(made)

    def _icon(self, filename):
        """One icon as a PNG, decoded from the client's own archives."""
        name = filename[:-4] if filename.lower().endswith(".png") else filename
        try:
            data = self.context.icons().png(name)
        except KeyError:
            return self._error("No icon named %s in the client" % name, 404)
        except (icons_module.IconError, ClientError) as exc:
            return self._error(str(exc), 404)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        # An icon never changes under the same name, so the browser may keep it.
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _shutdown(self):
        """Stop the server at the user's asking.

        The reply goes out first, because once the loop is down there is nothing
        left to answer with. shutdown() has to run on another thread: it waits
        for the serving loop to finish, and this handler is that loop.
        """
        self._json({"ok": True, "message": "SpellWeaver has stopped. "
                                           "Run it again when you need it."})
        self.wfile.flush()
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _status(self):
        settings = self.context.settings
        with self.context.db() as db:
            intruders = store.check_band_available(db, settings.band)
            count = len(store.list_spells(db))
            conflicts = trainers.band_conflicts(db, settings)
            trainer_count = len(trainers.list_trainers(db, settings))
            _, rows = db.query(
                "SELECT COUNT(*) FROM spell_dbc WHERE ID BETWEEN %d AND %d"
                % (settings.band.start, settings.band.end))
            used = rows[0][0]
        return self._json({
            "band": {"start": settings.band.start, "end": settings.band.end},
            "band_intruders": intruders,
            "creature_band": {"start": settings.creature_band.start,
                              "end": settings.creature_band.end},
            "trainer_band": {"start": settings.trainer_band.start,
                             "end": settings.trainer_band.end},
            "trainer_band_intruders": conflicts,
            "spells": count,
            # Ids, not abilities: a five-rank ability takes five of them.
            "band_used": used,
            "band_free": settings.band.end - settings.band.start + 1 - used,
            "trainers": trainer_count,
            "client_dir": settings.client_dir,
            "world_db": settings.db.world_db,
        })

    def _static(self, name):
        target = (WEB / name).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            return self._error("Not found", 404)
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def serve(host="127.0.0.1", port=8800, open_browser=False):
    print("spellweaver: loading spell data...")
    Handler.context = Context()
    settings = Handler.context.settings
    if not Handler.context.ready:
        print("spellweaver: %s" % Handler.context.problem)
        print("spellweaver: open the page and fill in the database details.")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("spellweaver: reserved band %s, trainers %s, creatures %s, world db %s"
          % (settings.band, settings.trainer_band, settings.creature_band,
             settings.db.world_db))
    url = "http://%s:%d/" % (host, port)
    print("spellweaver: ready at %s" % url)
    if open_browser:
        # After the loop is accepting, not before: a browser that arrives first
        # gets a refused connection and shows its own error page instead.
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
        # serve_forever returns when the page asks the server to stop, which is
        # an ordinary exit rather than the interrupt below.
        print("spellweaver: stopped from the page")
    except KeyboardInterrupt:
        print("\nspellweaver: stopped")
