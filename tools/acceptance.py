#!/usr/bin/env python3
"""End-to-end test through the running web server.

Drives the same HTTP API the browser uses, so it exercises the real path:
definition -> resolve -> tooltip + SQL -> database row. Start the server first
(./.run/start.sh) and run this against it.
"""
import argparse
import json
import pathlib
import re
import os
import shutil
import struct
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spellweaver.config import load_settings, require_connection
from spellweaver.mysql import connect

failures = []
created = []
trainers_made = []


def check(label, condition, detail=""):
    print("  [%s] %s%s" % ("ok  " if condition else "FAIL", label,
                           (" - " + str(detail)) if detail and not condition else ""))
    if not condition:
        failures.append(label)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read().decode())

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, payload):
        return self._call("POST", path, payload)

    def delete(self, path):
        return self._call("DELETE", path)


# Anything Chromium-based will do. The last two are where a Windows browser
# sits when this runs under WSL, which is a common way to reach one from Linux.
BROWSERS = [
    "google-chrome", "chromium", "chromium-browser", "chrome", "msedge",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
]


def find_browser():
    """Any headless-capable browser, or None. The page check is skipped without one.

    SPELLWEAVER_BROWSER names one directly, for anywhere the guesses do not fit.
    """
    named = os.environ.get("SPELLWEAVER_BROWSER")
    if named:
        return named if os.path.exists(named) else shutil.which(named)
    for candidate in BROWSERS:
        if os.path.sep in candidate:
            if os.path.exists(candidate):
                return candidate
        elif shutil.which(candidate):
            return shutil.which(candidate)
    return None


def rendered_dom(browser, url):
    """The page as the browser built it, which only happens if the script ran.

    No profile directory is passed: the browser here may be a Windows binary
    reached across a mount, and it cannot make sense of a path from this side.
    """
    out = subprocess.run(
        [browser, "--headless", "--disable-gpu", "--virtual-time-budget=10000",
         "--dump-dom", url],
        capture_output=True, timeout=180)
    return out.stdout.decode("utf-8", "replace")


def free_port():
    """A port nothing is on, for the throwaway server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(base, tries):
    """True once the server answers, False if it never does or has gone."""
    for _ in range(tries):
        try:
            with urllib.request.urlopen(base + "/", timeout=1):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.25)
    return False


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode())
    except Exception:
        return None


def header_of(url, name):
    """One response header, or None. Used for the caching rules."""
    try:
        with urllib.request.urlopen(url) as res:
            return res.headers.get(name)
    except urllib.error.HTTPError as exc:
        return exc.headers.get(name)


def fetch_bytes(url):
    """A raw GET, for the endpoints that do not answer in JSON."""
    try:
        with urllib.request.urlopen(url) as res:
            return res.read()
    except urllib.error.HTTPError:
        return None


def spell_row(db, spell_id, columns):
    cols, rows = db.query(
        "SELECT %s FROM spell_dbc WHERE ID = %d"
        % (", ".join("`%s`" % c for c in columns), spell_id))
    return dict(zip(cols, rows[0])) if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8800")
    ap.add_argument("--keep", action="store_true",
                    help="leave the created spells in place")
    args = ap.parse_args()

    api = Client(args.base)
    settings = require_connection(load_settings())
    # Whatever happens in the middle, what this run made comes back out of the
    # reserved band. A run that stopped half way used to leave its spells there
    # for somebody else to wonder about.
    try:
        return run(api, settings, args)
    finally:
        cleanup(api, settings, created, trainers_made, args)


def run(api, settings, args):
    status = api.get("/api/status")
    print("Server")
    check("responds", "band" in status, status)
    check("reserved band is clear of foreign spells",
          not status.get("band_intruders"), status.get("band_intruders"))

    vocab = api.get("/api/vocabulary")
    print("Vocabulary")
    check("behaviours offered", len(vocab["behaviours"]) > 100, len(vocab["behaviours"]))
    check("targets include the reticle",
          any(t["key"] == "ground" for t in vocab["targets"]))
    dot = next(b for b in vocab["behaviours"] if b["key"] == "damage_over_time")
    check("damage over time pre-fills a tick rate",
          dot["params"][1]["default"] == 3.0, dot["params"])
    stun = next(b for b in vocab["behaviours"] if b["key"] == "stun")
    check("stun declares no parameters", stun["params"] == [], stun["params"])

    # ---- the acceptance test spell ----------------------------------
    print("Acceptance: a Holy damage-over-time spell, 500 every 3s for 15s")
    definition = {
        "name": "Searing Light", "description": "", "icon_id": 682,
        "school": 2, "power_type": 0, "power_cost": 120,
        "cast_time_ms": 0, "cooldown_ms": 8000, "range_yards": 30,
        "target": "enemy", "radius_yards": 8, "duration_ms": 15000,
        "behaviours": [{"key": "damage_over_time", "amount": 500, "period_ms": 3000}],
    }
    preview = api.post("/api/preview", {"definition": definition})
    tip = preview["tooltip"]
    check("tooltip names the spell", tip["name"] == "Searing Light")
    check("tooltip reads like a spell",
          tip["description"] == "Deals 500 holy damage every 3 sec for 15 sec.",
          tip["description"])
    check("tooltip shows the cost", tip["cost"] == "120 Mana", tip["cost"])
    check("tooltip shows the range", tip["range"] == "30 yd range", tip["range"])
    check("tooltip shows instant cast", tip["cast_time"] == "Instant", tip["cast_time"])
    check("tooltip shows the cooldown", tip["cooldown"] == "8 sec cooldown", tip["cooldown"])
    check("no warnings", not preview["warnings"], preview["warnings"])

    saved = api.post("/api/spells", {"definition": definition})
    check("saved into the reserved band",
          settings.band.contains(saved.get("spell_id", 0)), saved)
    holy_id = saved["spell_id"]
    created.append(holy_id)

    with connect(settings.db) as db:
        row = spell_row(db, holy_id, [
            "Name_Lang_enUS", "Effect_1", "EffectAura_1", "EffectBasePoints_1",
            "EffectDieSides_1", "EffectAuraPeriod_1", "DurationIndex", "SchoolMask",
            "ManaCost", "RecoveryTime", "ImplicitTargetA_1", "Targets",
            "EffectChainAmplitude_1", "SpellIconID"])
        print("Database row")
        check("row exists", row is not None)
        check("name written", row["Name_Lang_enUS"] == "Searing Light")
        check("periodic damage aura", row["Effect_1"] == 6 and row["EffectAura_1"] == 3)
        check("server will compute 500 per tick",
              row["EffectBasePoints_1"] + row["EffectDieSides_1"] == 500,
              "%d + %d" % (row["EffectBasePoints_1"], row["EffectDieSides_1"]))
        check("ticks every 3000ms", row["EffectAuraPeriod_1"] == 3000)
        check("Holy school", row["SchoolMask"] == 2)
        check("costs 120", row["ManaCost"] == 120)
        check("8s cooldown", row["RecoveryTime"] == 8000)
        check("targets one enemy", row["ImplicitTargetA_1"] == 6 and row["Targets"] == 0)
        check("chain amplitude set", row["EffectChainAmplitude_1"] == 1.0)

    # ---- ground targeting -------------------------------------------
    print("Ground targeting (the reticle)")
    blizz = {
        "name": "Hailstorm", "description": "", "icon_id": 285, "school": 16,
        "power_type": 0, "power_cost": 200, "cast_time_ms": 0, "cooldown_ms": 0,
        "range_yards": 35, "target": "ground", "radius_yards": 10,
        "duration_ms": 8000,
        "behaviours": [{"key": "damage_ground", "amount": 35, "period_ms": 2000}],
    }
    saved = api.post("/api/spells", {"definition": blizz})
    ground_id = saved["spell_id"]
    created.append(ground_id)
    with connect(settings.db) as db:
        row = spell_row(db, ground_id,
                        ["Targets", "ImplicitTargetA_1", "EffectRadiusIndex_1", "Effect_1"])
        check("reticle flag set", row["Targets"] == 0x40, hex(row["Targets"]))
        check("anchored to a dynamic object", row["ImplicitTargetA_1"] == 28)
        check("persistent area aura", row["Effect_1"] == 27)
        check("has a radius", row["EffectRadiusIndex_1"] != 0)

    # ---- a behaviour with no amount ---------------------------------
    print("A behaviour that declares no amount writes none")
    stun_def = {
        "name": "Hammerfall", "description": "", "icon_id": 682, "school": 1,
        "power_type": 1, "power_cost": 10, "cast_time_ms": 0, "cooldown_ms": 30000,
        "range_yards": 5, "target": "enemy", "radius_yards": 8, "duration_ms": 4000,
        "behaviours": [{"key": "stun"}],
    }
    saved = api.post("/api/spells", {"definition": stun_def})
    stun_id = saved["spell_id"]
    created.append(stun_id)
    with connect(settings.db) as db:
        row = spell_row(db, stun_id,
                        ["Effect_1", "EffectAura_1", "EffectBasePoints_1", "DurationIndex"])
        check("is a stun aura", row["Effect_1"] == 6 and row["EffectAura_1"] == 12)
        check("no base points invented", row["EffectBasePoints_1"] == 0,
              row["EffectBasePoints_1"])

    # ---- recall and edit --------------------------------------------
    print("Recall and edit")
    listing = api.get("/api/spells")["spells"]
    check("everything created is listed",
          len({s["spell_id"] for s in listing} & set(created)) == len(created))
    loaded = api.get("/api/spells/%d" % holy_id)["definition"]
    check("recalls the behaviour", loaded["behaviours"][0]["key"] == "damage_over_time")
    check("recalls the amount", loaded["behaviours"][0]["amount"] == 500)
    loaded["behaviours"][0]["amount"] = 75
    api.post("/api/spells", {"definition": loaded})
    with connect(settings.db) as db:
        row = spell_row(db, holy_id, ["EffectBasePoints_1", "EffectDieSides_1"])
        check("edit rewrote the same id",
              row["EffectBasePoints_1"] + row["EffectDieSides_1"] == 75,
              row["EffectBasePoints_1"])

    # ---- how hard it hits -------------------------------------------
    print("Scaling, spread and animation")
    varied = {
        "name": "Wildfire", "description": "", "icon_id": 682, "school": 4,
        "class_id": 0, "power_type": 0, "power_cost": 100, "cast_time_ms": 0,
        "cooldown_ms": 0, "range_yards": 30, "target": "enemy", "radius_yards": 8,
        "duration_ms": 0, "spell_level": 1, "power_scaling": 40, "spread_pct": 20,
        "ignore_mitigation": True, "visual_id": 67,
        "behaviours": [{"key": "damage", "amount": 300}],
    }
    preview = api.post("/api/preview", {"definition": varied})
    check("the tooltip reads as a range",
          "240 to 360" in preview["tooltip"]["description"],
          preview["tooltip"]["description"])
    check("the tooltip states what power adds",
          preview["tooltip"]["scaling"] == "+40% of your spell power",
          preview["tooltip"]["scaling"])
    saved = api.post("/api/spells", {"definition": varied})
    varied_id = saved["spell_id"]
    created.append(varied_id)
    with connect(settings.db) as db:
        row = spell_row(db, varied_id, ["EffectBasePoints_1", "EffectDieSides_1",
                                        "SpellVisualID_1", "AttributesEx4"])
        check("the die carries the spread",
              row["EffectBasePoints_1"] == 239 and row["EffectDieSides_1"] == 121, row)
        check("the animation is the one picked", row["SpellVisualID_1"] == 67, row)
        check("mitigation is ignored", row["AttributesEx4"] & 0x100 == 0x100, row)
        cols, rows = db.query(
            "SELECT entry, direct_bonus, dot_bonus, ap_bonus FROM spell_bonus_data "
            "WHERE entry = %d" % varied_id)
        bonus = dict(zip(cols, rows[0])) if rows else None
        check("scaling was written", bonus is not None)
        check("as a direct spell-power coefficient",
              bonus and bonus["direct_bonus"] == 0.4 and bonus["dot_bonus"] == 0
              and bonus["ap_bonus"] == 0, bonus)
    loaded = api.get("/api/spells/%d" % varied_id)["definition"]
    check("it all comes back",
          loaded["power_scaling"] == 40 and loaded["spread_pct"] == 20
          and loaded["visual_id"] == 67 and loaded["ignore_mitigation"],
          {k: loaded.get(k) for k in ("power_scaling", "spread_pct", "visual_id",
                                      "ignore_mitigation")})

    # ---- each effect aims for itself --------------------------------
    print("A spell that hits two different things")
    siphon = {
        "name": "Siphon Light", "description": "", "icon_id": 682, "school": 32,
        "class_id": 0, "power_type": 0, "power_cost": 90, "cast_time_ms": 0,
        "cooldown_ms": 0, "range_yards": 30, "target": "enemy", "radius_yards": 8,
        "duration_ms": 0, "spell_level": 1, "power_scaling": 0, "spread_pct": 0,
        "behaviours": [{"key": "health_leech", "amount": 400, "share": 0.5},
                       {"key": "heal", "amount": 150, "target": "self"}],
    }
    preview = api.post("/api/preview", {"definition": siphon})
    check("the tooltip says what each effect hits",
          preview["tooltip"]["description"]
          == "Deals 400 shadow damage and heals you for 50% of it. Heals you for 150.",
          preview["tooltip"]["description"])
    # The validator is right to speak here: a leech already heals, so pairing it
    # with a separate heal is a shape nothing shipped uses.
    check("the validator notices an unattested pairing",
          any("nothing in the game does it" in w for w in preview["warnings"]),
          preview["warnings"])
    saved = api.post("/api/spells", {"definition": siphon})
    siphon_id = saved["spell_id"]
    created.append(siphon_id)
    with connect(settings.db) as db:
        row = spell_row(db, siphon_id, ["Effect_1", "ImplicitTargetA_1", "EffectMultipleValue_1",
                                        "Effect_2", "ImplicitTargetA_2", "Targets"])
        check("the leech is one effect, not two", row["Effect_1"] == 9, row)
        check("it takes from the enemy", row["ImplicitTargetA_1"] == 6, row)
        check("and returns half of what it deals",
              abs(row["EffectMultipleValue_1"] - 0.5) < 0.0001, row)
        check("the heal lands on the caster",
              row["Effect_2"] == 10 and row["ImplicitTargetA_2"] == 1, row)
        check("no reticle is asked for", row["Targets"] == 0, row)
    loaded = api.get("/api/spells/%d" % siphon_id)["definition"]
    check("the per-effect targets come back",
          loaded["behaviours"][0].get("target", "") == ""
          and loaded["behaviours"][1]["target"] == "self",
          [b.get("target") for b in loaded["behaviours"]])
    check("so does the share", loaded["behaviours"][0]["share"] == 0.5,
          loaded["behaviours"][0].get("share"))

    # ---- ranks are one ability written as several spells -------------
    print("Ranks")
    ladder = dict(definition)
    ladder["name"] = "Ladder of Light"
    ladder["spell_id"] = None
    ladder["spell_level"] = 10
    ladder["power_cost"] = 50
    ladder["class_id"] = 5                           # so the class row per rank is real
    ladder["skill_line"] = 56
    ladder["behaviours"] = [{"key": "damage", "amount": 100}]
    ladder["extra_ranks"] = [
        {"spell_level": 20, "power_cost": 90, "amounts": [220]},
        {"spell_level": 30, "power_cost": 140, "amounts": [400]},
    ]
    preview = api.post("/api/preview", {"definition": ladder})
    check("the tooltip shows the rank", preview["tooltip"]["rank"] == "Rank 1",
          preview["tooltip"].get("rank"))
    check("every rank is summarised", len(preview["ranks"]) == 3, preview["ranks"])

    saved = api.post("/api/spells", {"definition": ladder, "training": {"enabled": True}})
    ladder_ids = saved["ids"]
    created.extend(ladder_ids)
    check("three ranks take three ids", len(ladder_ids) == 3, ladder_ids)
    # The patch report used to give a bare id for every rank past the first,
    # because only the ability itself has a row in the ability table.
    report = api.get("/api/clientpatch")
    listed = {e["id"]: e["name"] for e in (report.get("band") or [])}
    check("the patch report names each rank",
          [listed.get(i) for i in ladder_ids]
          == ["%s Rank %d" % (ladder["name"], n) for n in (1, 2, 3)],
          [listed.get(i) for i in ladder_ids])
    # A rank row exists even for an ability that has only one, to account for
    # the id, so the report has to tell a real ladder from that bookkeeping.
    check("and an ability with one rank is not called Rank 1",
          bool(listed.get(holy_id)) and " Rank " not in listed[holy_id],
          listed.get(holy_id))
    check("and they are consecutive from the first",
          ladder_ids == list(range(ladder_ids[0], ladder_ids[0] + 3)), ladder_ids)
    with connect(settings.db) as db:
        cols, rows = db.query(
            "SELECT ID, NameSubtext_Lang_enUS, SpellLevel, ManaCost, "
            "EffectBasePoints_1 + EffectDieSides_1 AS amount FROM spell_dbc "
            "WHERE ID IN (%s) ORDER BY ID" % ", ".join(str(i) for i in ladder_ids))
        rows = [dict(zip(cols, r)) for r in rows]
        check("each rank is its own spell", len(rows) == 3, rows)
        check("labelled the way the game labels them",
              [r["NameSubtext_Lang_enUS"] for r in rows] == ["Rank 1", "Rank 2", "Rank 3"],
              [r["NameSubtext_Lang_enUS"] for r in rows])
        check("with its own level", [r["SpellLevel"] for r in rows] == [10, 20, 30], rows)
        check("its own cost", [r["ManaCost"] for r in rows] == [50, 90, 140], rows)
        check("and its own damage", [r["amount"] for r in rows] == [100, 220, 400], rows)
        # The chain is what makes the client show only the highest known rank and
        # a trainer offer only the next one.
        cols, chain = db.query(
            "SELECT spell_id, `rank` FROM spell_ranks WHERE first_spell_id = %d "
            "ORDER BY `rank`" % ladder_ids[0])
        check("they are chained in the core's own table",
              [c[0] for c in chain] == ladder_ids, chain)
        check("numbered 1 to N with no gaps", [c[1] for c in chain] == [1, 2, 3], chain)
        _, rows = db.query(
            "SELECT COUNT(*) FROM skilllineability_dbc WHERE ID IN (%s)"
            % ", ".join(str(i) for i in ladder_ids))
        check("every rank belongs to the class", rows[0][0] == 3, rows[0][0])
        cols, rows = db.query(
            "SELECT SpellId, ReqLevel FROM trainer_spell WHERE SpellId IN (%s) "
            "GROUP BY SpellId, ReqLevel ORDER BY SpellId"
            % ", ".join(str(i) for i in ladder_ids))
        check("and each is trained at its own level",
              [r[1] for r in rows] == [10, 20, 30], rows)
    listed = api.get("/api/spells")["spells"]
    entry = next((s for s in listed if s["spell_id"] == ladder_ids[0]), None)
    check("the library lists it once, not three times", entry is not None
          and entry["ranks"] == 3, entry)
    check("and no rank looks like a foreign spell in the band",
          not api.get("/api/status")["band_intruders"],
          api.get("/api/status")["band_intruders"])

    # Taking a rank away gives its id back rather than leaving an orphan.
    back = api.get("/api/spells/%d" % ladder_ids[0])["definition"]
    back["extra_ranks"] = back["extra_ranks"][:1]
    api.post("/api/spells", {"definition": back, "training": {"enabled": True}})
    with connect(settings.db) as db:
        _, rows = db.query("SELECT COUNT(*) FROM spell_dbc WHERE ID = %d" % ladder_ids[2])
        check("a removed rank is gone from the world", rows[0][0] == 0, rows[0][0])
        _, rows = db.query(
            "SELECT COUNT(*) FROM trainer_spell WHERE SpellId = %d" % ladder_ids[2])
        check("and left nothing pointing at it", rows[0][0] == 0, rows[0][0])
    back["extra_ranks"] = []
    api.post("/api/spells", {"definition": back, "training": {"enabled": True}})
    with connect(settings.db) as db:
        _, rows = db.query(
            "SELECT COUNT(*) FROM spell_ranks WHERE first_spell_id = %d" % ladder_ids[0])
        check("dropping to one rank removes the chain the core would reject",
              rows[0][0] == 0, rows[0][0])
        _, rows = db.query(
            "SELECT NameSubtext_Lang_enUS FROM spell_dbc WHERE ID = %d" % ladder_ids[0])
        check("and the rank label with it", rows[0][0] == "", repr(rows[0][0]))

    # ---- a spell belongs to a class ---------------------------------
    print("A spell belongs to a class")
    priest = {
        "name": "Vigil", "description": "", "icon_id": 682, "school": 2,
        "class_id": 5, "power_type": 0, "power_cost": 60, "cast_time_ms": 1500,
        "cooldown_ms": 0, "range_yards": 30, "target": "friend", "radius_yards": 8,
        "duration_ms": 0, "behaviours": [{"key": "heal", "amount": 300}],
    }
    saved = api.post("/api/spells", {"definition": priest})
    priest_id = saved["spell_id"]
    created.append(priest_id)
    with connect(settings.db) as db:
        cols, rows = db.query(
            "SELECT ID, SkillLine, Spell, ClassMask, MinSkillLineRank, AcquireMethod "
            "FROM skilllineability_dbc WHERE ID = %d" % priest_id)
        row = dict(zip(cols, rows[0])) if rows else None
        check("a class row was written", row is not None)
        check("it is on a Priest skill line", row and row["SkillLine"] == 56, row)
        check("it carries the Priest class mask", row and row["ClassMask"] == 16, row)
        check("it points at the spell", row and row["Spell"] == priest_id, row)
        check("it follows stock conventions",
              row and row["MinSkillLineRank"] == 1 and row["AcquireMethod"] == 0, row)
    loaded = api.get("/api/spells/%d" % priest_id)["definition"]
    check("the class comes back with the spell", loaded["class_id"] == 5, loaded.get("class_id"))

    loaded["class_id"] = 0
    api.post("/api/spells", {"definition": loaded})
    with connect(settings.db) as db:
        _, rows = db.query("SELECT COUNT(*) FROM skilllineability_dbc WHERE ID = %d" % priest_id)
        check("opening it to any class removes the row", rows[0][0] == 0, rows[0][0])

    # ---- being taught is the ordinary path --------------------------
    print("Taught by a trainer")
    taught_def = dict(priest)
    taught_def["name"] = "Vigil of Dawn"
    taught_def["spell_level"] = 20
    taught_def["skill_line"] = 78                    # Shadow Magic, not the default
    saved = api.post("/api/spells", {"definition": taught_def,
                                     "training": {"enabled": True}})
    taught_id = saved["spell_id"]
    created.append(taught_id)
    row = saved.get("training") or {}
    if row.get("trainer_created"):
        trainers_made.append(row["trainer_ids"][0])
    # The game already has Priest trainers, and those are the ones a player
    # visits, so nothing should need spawning for a Priest ability.
    check("it goes on trainers without being asked to", bool(row.get("trainer_ids")), row)
    check("on the ones the game already ships", row.get("ours") is False, row.get("ours"))
    check("reaching real NPCs", row.get("npcs", 0) > 10, row.get("npcs"))
    check("so there is nothing to spawn", not row.get("spawn_command"),
          row.get("spawn_command"))
    check("the price is what the game charges at that level",
          row.get("money_cost") == 2200, row.get("money_cost"))
    with connect(settings.db) as db:
        cols, rows = db.query(
            "SELECT TrainerId, MoneyCost, ReqLevel FROM trainer_spell "
            "WHERE SpellId = %d" % taught_id)
        rows = [dict(zip(cols, r)) for r in rows]
        check("every one of those lists really teaches it",
              len(rows) == len(row["trainer_ids"]), rows)
        check("at the level the ability requires",
              all(r["ReqLevel"] == 20 for r in rows), rows)
        check("we only added rows, never edited a shipped one",
              all(r["TrainerId"] not in (0,) for r in rows), rows)
        _, got = db.query(
            "SELECT SkillLine FROM skilllineability_dbc WHERE ID = %d" % taught_id)
        check("and the chosen tree was used, not the default",
              got and got[0][0] == 78, got)

    # An ability open to any class belongs on every class trainer, for the same
    # reason: whoever the player is, their own trainer is the one they visit.
    anyone = api.post("/api/spells",
                      {"definition": dict(taught_def, spell_id=None, class_id=0,
                                          skill_line=0, name="Open Ward"),
                       "training": {"enabled": True}})
    created.append(anyone["spell_id"])
    row = anyone["training"]
    check("a classless ability reaches every class trainer",
          len(row["trainer_ids"]) > 20, len(row["trainer_ids"]))
    check("and still needs nothing spawned", not row.get("spawn_command"), row)
    check("it never touches a profession or riding trainer",
          row["npcs"] < 500, row["npcs"])

    # A trainer of our own is the fallback, and it is made on demand.
    own = api.post("/api/spells", {"definition": dict(taught_def, spell_id=taught_id),
                                   "training": {"enabled": True, "scope": "own"}})
    row = own["training"]
    if row.get("trainer_created"):
        trainers_made.append(row["trainer_ids"][0])
    check("asking for our own makes one", row.get("ours") is True, row)
    check("and it comes with the command that places it",
          (row.get("spawn_command") or "").startswith(".npc add"), row.get("spawn_command"))
    with connect(settings.db) as db:
        _, rows = db.query(
            "SELECT COUNT(*) FROM trainer_spell WHERE SpellId = %d" % taught_id)
        check("and it is taught in exactly one place now", rows[0][0] == 1, rows[0][0])

    # A trainer that does not exist must not take the ability down with it.
    broken = api.post("/api/spells",
                      {"definition": dict(taught_def, spell_id=taught_id),
                       "training": {"enabled": True, "scope": "one",
                                    "trainer_id": 987654}})
    check("a bad trainer does not fail the save", broken.get("spell_id") == taught_id, broken)
    check("but it is reported rather than passed over",
          any("not on a trainer" in w for w in broken.get("warnings", [])),
          broken.get("warnings"))

    api.post("/api/spells", {"definition": dict(taught_def, spell_id=taught_id),
                             "training": {"enabled": False}})
    with connect(settings.db) as db:
        _, rows = db.query("SELECT COUNT(*) FROM trainer_spell WHERE SpellId = %d" % taught_id)
        check("turning it off takes it off every trainer", rows[0][0] == 0, rows[0][0])

    # ---- the patch panel tells the truth about the client -----------
    print("Client patch status")
    state = api.get("/api/clientpatch")
    if not state.get("client"):
        print("      (no client configured; skipped)")
    else:
        check("it reports on a client", bool(state["client"]), state)
        check("it says whether the patch is installed", "installed" in state, state)
        # Spells this test just created are not in any patch, so a status that
        # claims the client is up to date is lying - the failure that matters.
        check("it does not claim up to date while abilities are missing",
              not state["ok"], state.get("problems"))
        check("and it names which ones",
              any("Not in the patch" in p for p in state["problems"]),
              state["problems"])
        check("it never reports on a staging copy",
              not state.get("patch") or state["client"] in state["patch"],
              state.get("patch"))

    # ---- an unfinished ability does not reach the server ------------
    print("An ability needs a name")
    blank = api.post("/api/spells", {"definition": dict(definition, name="   ",
                                                        spell_id=None)})
    check("a blank name is refused", "error" in blank, blank)
    check("and says why", "needs a name" in blank.get("error", ""), blank.get("error"))
    with connect(settings.db) as db:
        _, rows = db.query(
            "SELECT COUNT(*) FROM spell_dbc WHERE ID BETWEEN %d AND %d "
            "AND Name_Lang_enUS = ''" % (settings.band.start, settings.band.end))
        check("nothing nameless was written", rows[0][0] == 0, rows[0][0])

    # ---- the connection is asked for, not assumed --------------------
    print("Settings")
    state = api.get("/api/settings")
    check("the tool reports how it connected", state.get("source") in
          ("saved", "env", "environment"), state.get("source"))
    check("it says it is ready", state.get("ready") is True, state)
    check("the password is never sent back",
          "password" not in state.get("connection", {}), state.get("connection"))
    check("but it says whether one is set",
          "has_password" in state.get("connection", {}), state.get("connection"))
    check("both database names are offered",
          {"world_db", "characters_db"} <= set(state.get("connection", {})),
          state.get("connection"))
    bad = api.post("/api/settings/connection/test",
                   {"host": state["connection"]["host"],
                    "port": state["connection"]["port"],
                    "user": state["connection"]["user"],
                    "password": "not-the-password",
                    "world_db": state["connection"]["world_db"],
                    "characters_db": state["connection"]["characters_db"]})
    check("a wrong password is reported plainly",
          not bad["ok"] and "refused" in bad["message"], bad)
    check("and a failed test saves nothing", bad.get("saved") is False, bad)
    unreachable = api.post("/api/settings/connection/test",
                           {"host": "127.0.0.1", "port": 3999, "user": "x",
                            "password": "y", "world_db": "w", "characters_db": "c"})
    check("an unreachable server is reported plainly",
          not unreachable["ok"] and "Could not reach" in unreachable["message"],
          unreachable)
    # The client folder is a setting too, and is checked before it is kept.
    check("the client folder is reported on", "client" in state, state.keys())
    nonsense = api.post("/api/settings/client", {"path": "/definitely/not/a/client"})
    check("a folder that is not a client is refused",
          not nonsense["ok"] and "no folder" in nonsense["message"].lower(),
          nonsense)
    check("and nothing was saved", nonsense.get("saved") is False, nonsense)

    # ---- the build route is wired, without touching a real client ----
    # Asking it to build for a folder that is not a client exercises the whole
    # path up to the point it would write. A route whose handler has gone
    # missing answers with an AttributeError instead, which is how one did.
    attempt = api.post("/api/clientpatch", {"client_dir": "/not/a/client"})
    check("building is wired to something that exists",
          "AttributeError" not in (attempt.get("error") or ""), attempt)
    check("and it refuses a folder that is not a client",
          "Data directory" in (attempt.get("error") or ""), attempt)

    # ---- the command line says the same thing as the panel -----------
    print("The status command")
    root = pathlib.Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, str(root / "tools/build_client_patch.py"),
                          "--status"], capture_output=True, timeout=300)
    text = out.stdout.decode("utf-8", "replace")
    check("it runs", out.returncode in (0, 1), out.stderr.decode()[-300:])
    check("it names the client", "client:" in text, text[:200])
    # This is what a list of dicts printed straight out looks like, and it is
    # what happened the last time the shape of that data changed.
    check("it prints abilities, not raw data",
          "{'id'" not in text and "{\"id\"" not in text, text)
    if "patch:     not installed" not in text:
        check("it names them", re.search(r"abilities: .*\(\d{6}\)", text) is not None, text)
    check("it says whether the client is up to date",
          "up to date" in text or "PROBLEM:" in text, text)

    # ---- the browser half actually runs -----------------------------
    print("The page")
    browser = find_browser()
    if not browser:
        print("      (no headless browser available; skipped)")
    else:
        dom = rendered_dom(browser, args.base + "/#spell/%d" % holy_id)
        # Every one of these is built by script at load. If the script throws,
        # they are empty, which is exactly how a broken page looks.
        levels = re.search(r'<select id="level"[^>]*>(.*?)</select>', dom, re.S)
        trees = re.search(r'<select id="skill-line"[^>]*>(.*?)</select>', dom, re.S)
        behaviours = re.search(r'<div id="behaviours">(.*?)</div>\s*</div>', dom, re.S)
        check("the page renders", "ABILITY INFORMATION" in dom.upper(), dom[:200])
        check("the level list is built", levels and levels.group(1).count("<option") == 80,
              levels.group(1).count("<option") if levels else None)
        check("the class trees are built", trees and trees.group(1).count("<option") >= 1,
              trees.group(1)[:120] if trees else None)
        check("the effects are drawn", behaviours and "Damage over time" in behaviours.group(1),
              behaviours.group(1)[:120] if behaviours else None)
        check("icons are asked for from the client", "/api/icon/" in dom)
        # Creating and editing are different places, and the page says which it
        # is on rather than leaving one button to mean both.
        fresh = rendered_dom(browser, args.base + "/#new")
        check("a new ability offers to save one",
              re.search(r'id="save"[^>]*>Save Ability<', fresh) is not None,
              re.findall(r'id="save"[^>]*>[^<]*<', fresh))
        check("an existing one offers to save changes",
              re.search(r'id="save"[^>]*>Save Changes<', dom) is not None,
              re.findall(r'id="save"[^>]*>[^<]*<', dom))
        check("and the empty form really is empty",
              re.search(r'<input type="text" id="name"[^>]*value=', fresh) is None,
              re.findall(r'<input type="text" id="name"[^>]*>', fresh))
        # The library lives in the builder's rail now: one page for making an
        # ability and for picking the one to change.
        library = rendered_dom(browser, args.base + "/#spells")
        css = fetch_bytes(args.base + "/static/style.css").decode("utf-8")
        app = fetch_bytes(args.base + "/static/app.js").decode("utf-8")
        check("the library shows an icon per ability",
              library.count("/api/icon/") >= 1, library.count("/api/icon/"))
        check("and no longer asks about being in world",
              "In world" not in library and "in_world" not in library)
        check("the animation filter says what it does",
              'id="visual-search" placeholder="Filter animations"' in library)
        check("and the list opens as a list when filtering",
              'select[size]:not([size="1"])' in css and "el.size" in app)
        check("the patch action says what it does",
              re.search(r'id="patch-build"[^>]*>\s*Apply Patch\s*<', library)
              is not None)
        check("and re-checking is an icon that still says so to a reader",
              'id="patch-refresh"' in library and 'aria-label="Check the client again"'
              in library)
        check("the library sits in the rail beside the builder",
              'id="library-list"' in library and 'id="view-build"' in library)
        check("an ability is a card, not a table row",
              'class="ability-card"' in library,
              library.count('class="ability-card"'))
        check("the card says what the ability is",
              re.search(r'class="name">[^<]+</div>', library) is not None)
        check("and the builder is still on the same page",
              'id="behaviour-add"' in library)
        check("Create and Ability Library are one nav entry",
              'id="nav-abilities"' in library
              and 'id="nav-list"' not in library
              and 'id="nav-create"' not in library)
        check("the list is grouped by class",
              'class="group-head"' in library, library.count('class="group-head"'))
        check("and creating a new one is offered at its head",
              'class="new-card" id="new-ability"' in library)
        check("the steps are no longer numbered",
              'class="num"' not in library)
        # Four icon sizes had drifted apart across the builder. They are one
        # token now, so a new one cannot be added by hand without noticing.
        check("every icon in the chrome is on the one size token",
              css.count("var(--icon)") >= 5 and "--icon: 34px" in css,
              css.count("var(--icon)"))
        check("and only the tooltip's replica icon opts out",
              css.count("var(--icon-tooltip)") == 2,
              css.count("var(--icon-tooltip)"))
        # 700 animations: every one reachable, in a predictable order, findable
        # by name. The school sorts the list; it no longer hides most of it.
        picker = re.search(r'<select id="visual".*?</select>', library, re.S)
        picker = picker.group(0) if picker else ""
        groups = re.findall(r'<optgroup label="([^"]+)"', picker)
        check("the animation list can be searched",
              'id="visual-search"' in library)
        check("every animation is offered, not just the school's",
              picker.count("<option") > 600, picker.count("<option"))
        check("split into the school's and the rest", len(groups) == 2, groups)
        for group in re.findall(r'<optgroup label="[^"]+">(.*?)</optgroup>',
                                picker, re.S):
            names = re.findall(r'>([^<]+)</option>', group)
            check("and each group is alphabetical",
                  names == sorted(names, key=str.casefold), names[:4])
        # School decides which animations come first, so it is asked first.
        order = re.findall(r'id="(school|visual-search)"', library)
        check("school is asked before the animation it filters",
              order == ["school", "visual-search"], order)
        # A behaviour with no parameters ends on its target, which used to sit
        # flush against the bottom of the card.
        check("the target block keeps its padding when it is last",
              ".behaviour .aim:last-child" in css)
        # Saving is offered only once there is something to save, and the
        # attribute alone is not enough: a display rule can outrank [hidden].
        check("nothing to save on an untouched ability",
              re.search(r'id="edit-actions"[^>]*\shidden', fresh) is not None,
              re.findall(r'<div[^>]*id="edit-actions"[^>]*>', fresh))
        check("and the rule that hides it comes last",
              css.index(".actions.pair {") < css.index(".actions[hidden]"))
        # The real bug was Create New Ability after opening one, which needs a
        # click the dumper cannot make. This asserts the cause instead: the
        # reset repaints the icon rather than only clearing it in state.
        reset = app[app.index("function resetForm()"):app.index("function showBuilder()")]
        check("resetting the form repaints the icon",
              "paintIconPreview()" in reset and "renderIconGrid()" in reset)
        check("a new ability shows the blank icon, not the last one viewed",
              re.search(r'id="icon-preview"><img[^>]*src="/api/icon/'
                        r'INV_Misc_QuestionMark\.png"', fresh) is not None,
              re.findall(r'id="icon-preview">.{0,90}', fresh))
        check("nothing rendered as undefined", "undefined" not in dom.lower(),
              [line for line in dom.splitlines() if "undefined" in line.lower()][:2])

    # ---- restarting the worldserver ----------------------------------
    # Whatever command is really configured is put back afterwards: this runs
    # against a live install, and clobbering someone's restart command, or
    # worse running it, is not a test's business.
    print("Restarting the worldserver")
    theirs = (api.get("/api/settings").get("restart") or {}).get("command", "")
    try:
        api.post("/api/settings/restart-command", {"command": "exit 0"})
        state = api.get("/api/settings")
        check("the command is remembered",
              state["restart"]["command"] == "exit 0", state.get("restart"))
        # The page quotes this back when the server has gone, so it has to name
        # the launcher of the machine running the server, not the browser's.
        check("the page is told how to start this one again",
              (state.get("launcher") or "").endswith((".bat", "run.py")),
              state.get("launcher"))
        ran = api.post("/api/restart", {})
        check("and running it reports success", ran.get("ok") is True, ran)

        api.post("/api/settings/restart-command",
                 {"command": "echo refused >&2; exit 4"})
        bad = api.post("/api/restart", {})
        check("a failure is reported rather than hidden", bad.get("ok") is False, bad)
        check("with the code and the reason",
              bad.get("code") == 4 and "refused" in bad.get("output", ""), bad)

        # The property worth protecting: the page asks, it does not dictate.
        sneaky = api.post("/api/restart", {"command": "exit 0"})
        check("a command in the request is ignored",
              sneaky.get("ok") is False and sneaky.get("code") == 4, sneaky)

        api.post("/api/settings/restart-command", {"command": ""})
        empty = api.post("/api/restart", {})
        check("with none set, it says so instead of running anything",
              "Settings" in (empty.get("error") or ""), empty)
    finally:
        api.post("/api/settings/restart-command", {"command": theirs})
    back = (api.get("/api/settings").get("restart") or {}).get("command", "")
    check("and the real command is put back", back == theirs, back)

    # ---- stopping the server -----------------------------------------
    # On a throwaway server on its own port: the one under test has to survive.
    print("Shutting down")
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:%d" % port
        up = wait_for(base, 40)
        check("a second server starts", up, port)
        if up:
            said = post_json(base + "/api/shutdown", {})
            check("the page can stop it", bool(said and said.get("ok")), said)
            check("and it says so before the socket goes", "stopped" in
                  (said or {}).get("message", "").lower(), said)
            check("the port is released", not wait_for(base, 20), port)
            check("and the process exits", proc.wait(timeout=20) is not None)
    finally:
        if proc.poll() is None:
            proc.kill()

    # ---- what the browser is allowed to keep -------------------------
    # A page with no Cache-Control is cached at the browser's discretion, and
    # they do invent one: an updated tool kept running the previous script.
    print("Caching")
    page_cache = header_of(args.base + "/", "Cache-Control")
    js_cache = header_of(args.base + "/static/app.js", "Cache-Control")
    icon_cache = header_of(args.base + "/api/icon/Spell_Holy_LesserHeal.png",
                           "Cache-Control")
    check("the page is not cached", "no-cache" in (page_cache or ""), page_cache)
    check("nor is its script", "no-cache" in (js_cache or ""), js_cache)
    check("but icons are, since a name always means the same image",
          "max-age" in (icon_cache or ""), icon_cache)

    # ---- icons come from the client ---------------------------------
    print("Icons")
    icon = fetch_bytes(args.base + "/api/icon/Spell_Holy_LesserHeal.png")
    if icon is None:
        print("      (no client configured, so icons cannot be read; skipped)")
    else:
        check("an icon is served as a PNG", icon[:8] == b"\x89PNG\r\n\x1a\n", icon[:8])
        width, height = struct.unpack_from(">II", icon, 16)
        check("at the size the client stores it", (width, height) == (64, 64),
              (width, height))
        missing = fetch_bytes(args.base + "/api/icon/No_Such_Icon_At_All.png")
        check("an icon the client does not have is refused", missing is None)

    # ---- the validator speaks ---------------------------------------
    print("Validator")
    odd = dict(definition)
    odd["behaviours"] = [{"key": "heal", "amount": 100}, {"key": "stun"}]
    odd["spell_id"] = None
    preview = api.post("/api/preview", {"definition": odd})
    check("warns about an unseen combination",
          any("nothing in the game does it" in w for w in preview["warnings"]),
          preview["warnings"])
    bad_ground = dict(definition)
    bad_ground["spell_id"] = None
    bad_ground["target"] = "enemy"
    bad_ground["behaviours"] = [{"key": "damage_ground", "amount": 35, "period_ms": 2000}]
    preview = api.post("/api/preview", {"definition": bad_ground})
    check("warns when a ground patch has no ground target",
          any("spot you choose" in w for w in preview["warnings"]),
          preview["warnings"])

    print()
    if failures:
        print("%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("All checks passed.")
    return 0


def cleanup(api, settings, created, trainers_made, args):
    """Always runs, including after a check has thrown.

    A run that stops half way used to leave its spells in the reserved band, so
    the next person to look wondered whose they were.
    """
    if not args.keep:
        print("Cleanup")
        for spell_id in created:
            api.delete("/api/spells/%d" % spell_id)
        # Only what this run created; spells made outside the test stay put.
        with connect(settings.db) as db:
            ids = ", ".join(str(i) for i in created)
            _, rows = db.query("SELECT COUNT(*) FROM spell_dbc WHERE ID IN (%s)" % ids)
            check("created spells removed", rows[0][0] == 0, rows[0][0])
            _, rows = db.query(
                "SELECT COUNT(*) FROM skilllineability_dbc WHERE ID IN (%s)" % ids)
            check("their class rows went with them", rows[0][0] == 0, rows[0][0])
            _, rows = db.query(
                "SELECT COUNT(*) FROM spell_bonus_data WHERE entry IN (%s)" % ids)
            check("their scaling went too", rows[0][0] == 0, rows[0][0])
        for trainer_id in trainers_made:
            api.delete("/api/trainers/%d" % trainer_id)
        if trainers_made:
            with connect(settings.db) as db:
                _, rows = db.query(
                    "SELECT COUNT(*) FROM trainer WHERE Id IN (%s)"
                    % ", ".join(str(i) for i in trainers_made))
            check("trainers made by the test removed", rows[0][0] == 0, rows[0][0])
    else:
        print("Kept: %s" % ", ".join(str(i) for i in created))


if __name__ == "__main__":
    sys.exit(main())
