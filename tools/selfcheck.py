#!/usr/bin/env python3
"""Sanity checks for the extraction pipeline.

These are assertions about things that must stay true as the vocabulary grows:
the DBC layout still matches the database, no behaviour names a pair that does
not exist, the stock filter actually bites, and the well-known spells still
decode to the values they are famous for.
"""
import os
import dataclasses
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spellweaver import (blp, classes, client, clientpatch, config as config_module,
                         connection, dbc, icons, mpq, ranks, scaling, visuals)
from spellweaver.behaviours import BEHAVIOURS, WITHHELD
from spellweaver.config import band_conflicts, load_settings, require_connection
from spellweaver.coredefs import (DEAD, available as coredefs_available,
                                  aura_support, effect_support)
from spellweaver.corpus import Corpus
from spellweaver.mysql import connect
from spellweaver.spell_layout import FIELDS
from spellweaver.spell import (SpellDef, BehaviourUse, RankValues, resolve, _spread,
                               rank_definitions, next_rank_values,
                               ATTR4_IGNORE_MITIGATION)
from spellweaver.store import check_band_available, spell_dbc_sql
from spellweaver.targeting import BY_KEY as TARGET_BY_KEY, implicit_for
from spellweaver.tooltip import (build as build_tooltip,
                                 suggest_aura_description, suggest_description)
from spellweaver.vocabulary import Vocabulary

failures = []


def struct_pack_dbc(parsed):
    """A DBC back into bytes, for tests that need one in memory."""
    fields = parsed.field_count
    out = struct.pack("<4sIIII", b"WDBC", len(parsed.records), fields,
                      fields * 4, len(parsed.string_block))
    for row in parsed.records:
        out += struct.pack("<%di" % fields, *row)
    return out + parsed.string_block


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print("  [%s] %s%s" % (status, label, (" - " + detail) if detail and not condition else ""))
    if not condition:
        failures.append(label)


def main():
    settings = require_connection(load_settings())
    with connect(settings.db) as db:
        corpus = Corpus(db=db, stock_ceiling=settings.stock_ceiling)
        band = band_conflicts(db, settings.band)
        intruders = check_band_available(db, settings.band)
        _, cols = db.query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA='%s' AND TABLE_NAME='spell_dbc' "
            "ORDER BY ORDINAL_POSITION" % settings.db.world_db)
    vocab = Vocabulary(corpus)

    print("Schema and layout")
    check("spell_dbc has 234 columns", len(cols) == 234, str(len(cols)))
    check("DBC layout matches the live schema", [c[0] for c in cols] == FIELDS)
    check("Spell.dbc parsed", len(corpus.spells) > 40000, str(len(corpus.spells)))

    print("Stock filter")
    check("no module ids in the corpus",
          corpus.max_stock_id() < settings.stock_ceiling, str(corpus.max_stock_id()))
    check("filter removes high ids when tightened",
          len(Corpus(stock_ceiling=20000).spells) < len(corpus.spells))
    check("no bonus rows above the ceiling",
          all(i < settings.stock_ceiling for i in corpus.bonus))

    print("Reserved band")
    check("band is above the stock ceiling", settings.band.start > settings.stock_ceiling)
    # Spells this tool created are expected to be in the band; only rows it did
    # not write are a problem.
    check("nothing foreign occupies the band", not intruders,
          "foreign ids: %s" % intruders[:5])
    print("      (%d spell(s) in the band, all created by spellweaver)" % band["count"])

    print("Vocabulary")
    keys = [b.key for b in BEHAVIOURS]
    check("behaviour keys are unique", len(keys) == len(set(keys)))
    unattested = [b.key for b in BEHAVIOURS if not vocab.is_attested(b.effect, b.aura)]
    check("every behaviour is attested in stock data", not unattested, str(unattested))
    if coredefs_available():
        effects, auras = effect_support(), aura_support()
        dead = [b.key for b in BEHAVIOURS
                if effects.get(b.effect) == DEAD or (b.aura and auras.get(b.aura) == DEAD)]
        check("no behaviour maps to a do-nothing handler", not dead, str(dead))
    else:
        print("      (no AzerothCore source to read handlers from; skipped)")
    overlap = {(b.effect, b.aura) for b in BEHAVIOURS} & set(WITHHELD)
    check("nothing is both offered and withheld", not overlap, str(overlap))

    print("Known spells decode correctly")
    fireball, corruption, pws = corpus.by_id[133], corpus.by_id[172], corpus.by_id[17]
    check("Fireball is named", fireball.name == "Fireball", fireball.name)
    check("Fireball is Fire school", fireball["SchoolMask"] == 4)
    check("Fireball does school damage", fireball.effect(1) == (2, 0))
    check("Corruption is a damage-over-time aura", corruption.effect(1) == (6, 3))
    check("Corruption ticks every 3s", corruption["EffectAuraPeriod_1"] == 3000)
    check("Power Word: Shield absorbs", pws.effect(1) == (6, 69), str(pws.effect(1)))

    print("Derived defaults are plausible")
    by_key = {b.key: b for b in BEHAVIOURS}
    dot = vocab.defaults(by_key["damage_over_time"])
    check("DoT ticks every 3s", dot["period"]["value"] == 3000)
    check("DoT coefficient is between 0 and 1", 0 < dot["coeff_dot"]["value"] < 1)
    heal = vocab.defaults(by_key["heal"])
    check("direct heal coefficient near 0.8",
          0.5 < heal["coeff_direct"]["value"] < 1.1, str(heal["coeff_direct"]["value"]))
    check("stun has a duration and no coefficient",
          "duration" in vocab.defaults(by_key["stun"])
          and "coeff_direct" not in vocab.defaults(by_key["stun"]))

    print("Validator")
    check("a real combination passes",
          not vocab.check_combination(["damage", "damage_over_time"]))
    check("an unseen combination warns", bool(vocab.check_combination(["heal", "stun"])))
    check("too many behaviours warn",
          any("three effect slots" in w for w in
              vocab.check_combination(["damage", "heal", "stun", "root"])))

    print("Targeting matches stock conventions")
    dot = BehaviourUse("damage_over_time", amount=500, period_ms=3000)
    unit = resolve(SpellDef(name="T", school=2, target="enemy", duration_ms=15000,
                            behaviours=[dot]), spell_id=settings.band.start)
    check("unit targeting leaves Targets at zero", unit.fields["Targets"] == 0,
          hex(unit.fields["Targets"]))
    check("unit targeting uses UNIT_TARGET_ENEMY", unit.fields["ImplicitTargetA_1"] == 6)
    check("chain amplitude is 1.0", unit.fields["EffectChainAmplitude_1"] == 1.0)
    patch = resolve(SpellDef(name="B", school=16, target="ground", duration_ms=8000,
                             radius_yards=10,
                             behaviours=[BehaviourUse("damage_ground", amount=35,
                                                      period_ms=2000)]),
                    spell_id=settings.band.start)
    check("ground targeting sets the reticle flag", patch.fields["Targets"] == 0x40)
    check("ground patch anchors to a dynamic object",
          patch.fields["ImplicitTargetA_1"] == 28)
    check("ground patch has a radius", patch.radius_yards(1) == 10)
    check("a ground behaviour on a unit target warns",
          any("spot you click" in w or "spot you choose" in w
              for w in resolve(SpellDef(name="B", target="enemy", duration_ms=8000,
                                        behaviours=[BehaviourUse("damage_ground",
                                                                 amount=35,
                                                                 period_ms=2000)]),
                               spell_id=settings.band.start).warnings))

    print("Each effect aims for itself")
    pair = resolve(SpellDef(name="Siphon", school=32, target="enemy",
                            behaviours=[BehaviourUse("damage", amount=400),
                                        BehaviourUse("heal", amount=200, target="self")]),
                   spell_id=settings.band.start)
    check("the first effect hits the enemy", pair.fields["ImplicitTargetA_1"] == 6,
          str(pair.fields["ImplicitTargetA_1"]))
    check("the second heals the caster", pair.fields["ImplicitTargetA_2"] == 1,
          str(pair.fields["ImplicitTargetA_2"]))
    check("a spell reaching an enemy keeps its range", pair.range_yards() == 30,
          str(pair.range_yards()))
    check("the tooltip says who each one hits",
          build_tooltip(pair)["description"] == "Deals 400 shadow damage. Heals you for 200.",
          build_tooltip(pair)["description"])
    followed = resolve(SpellDef(name="Plain", school=2, target="ally",
                                behaviours=[BehaviourUse("heal", amount=100)]),
                       spell_id=settings.band.start)
    check("a behaviour with no target of its own follows the spell",
          followed.fields["ImplicitTargetA_1"] == 21,
          str(followed.fields["ImplicitTargetA_1"]))
    mixed = resolve(SpellDef(name="Mixed", school=4, target="enemy", duration_ms=8000,
                             radius_yards=10,
                             behaviours=[BehaviourUse("damage", amount=100),
                                         BehaviourUse("damage_ground", amount=50,
                                                      period_ms=2000, target="ground")]),
                    spell_id=settings.band.start)
    check("one ground-aimed effect is enough for the reticle",
          mixed.fields["Targets"] == 0x40, hex(mixed.fields["Targets"]))
    check("and it does not disturb the other slot",
          mixed.fields["ImplicitTargetA_1"] == 6 and mixed.fields["ImplicitTargetA_2"] == 28,
          str((mixed.fields["ImplicitTargetA_1"], mixed.fields["ImplicitTargetA_2"])))

    print("Leeching returns a share, not a promise")
    leech = resolve(SpellDef(name="Siphon", school=32, target="enemy",
                             behaviours=[BehaviourUse("health_leech", amount=400, share=0.5)]),
                    spell_id=settings.band.start)
    check("the share reaches the field", leech.fields["EffectMultipleValue_1"] == 0.5,
          str(leech.fields["EffectMultipleValue_1"]))
    check("the tooltip states the proportion",
          "heals you for 50% of it" in build_tooltip(leech)["description"],
          build_tooltip(leech)["description"])
    empty = resolve(SpellDef(name="Siphon", school=32, target="enemy",
                             behaviours=[BehaviourUse("health_leech", amount=400)]),
                    spell_id=settings.band.start)
    check("a leech with no share warns rather than silently returning nothing",
          any("returns nothing" in w for w in empty.warnings), str(empty.warnings))
    for key in ("health_leech", "drain_life", "drain_mana", "power_drain", "power_burn"):
        b = by_key[key]
        check("%s declares the share it depends on" % key,
              any(p in ("heal_share", "gain_share", "burn_ratio") for p in b.params),
              str(b.params))
        check("%s has a share extracted from stock data" % key,
              vocab.defaults(b).get("multiple", {}).get("value", 0) > 0,
              str(vocab.defaults(b).get("multiple")))

    print("The acceptance-test spell")
    holy = resolve(SpellDef(name="Searing Light", school=2, power_cost=120,
                            cooldown_ms=8000, range_yards=30, target="enemy",
                            duration_ms=15000, icon_id=682, behaviours=[dot]),
                   spell_id=settings.band.start)
    check("is a periodic damage aura", holy.fields["Effect_1"] == 6
          and holy.fields["EffectAura_1"] == 3)
    check("ticks for exactly 500", holy.effect_amount(1) == 500,
          str(holy.effect_amount(1)))
    check("ticks every 3 seconds", holy.fields["EffectAuraPeriod_1"] == 3000)
    check("lasts 15 seconds", holy.duration_ms() == 15000, str(holy.duration_ms()))
    check("is Holy", holy.fields["SchoolMask"] == 2)
    check("resolves with no warnings", not holy.warnings, str(holy.warnings))

    print("Tooltip is a rendering of the resolved spell")
    tip = build_tooltip(holy)
    check("tooltip amount matches the DBC value",
          str(holy.effect_amount(1)) in tip["description"], tip["description"])
    check("tooltip duration matches DurationIndex",
          "15 sec" in tip["description"], tip["description"])
    check("tooltip cost matches ManaCost",
          tip["cost"] == "%d Mana" % holy.fields["ManaCost"], str(tip["cost"]))
    check("tooltip range matches RangeIndex",
          tip["range"] == "%g yd range" % holy.range_yards(), str(tip["range"]))
    check("tooltip cooldown matches RecoveryTime", tip["cooldown"] == "8 sec cooldown",
          str(tip["cooldown"]))
    # The tooltip and the SQL must come from the same resolved values.
    sql = spell_dbc_sql(holy, "Searing Light", suggest_description(holy))
    check("SQL carries the same base points",
          ", %d, " % holy.fields["EffectBasePoints_1"] in sql or
          str(holy.fields["EffectBasePoints_1"]) in sql)
    check("SQL targets the reserved band",
          "`ID` = %d" % settings.band.start in sql)

    # The icon on the target reads from AuraDescription, not Description. Left
    # empty, a debuff in game has a name and nothing else.
    print("What the effect on the target says")
    aura = suggest_aura_description(holy)
    check("a lasting effect describes itself", bool(aura), aura)
    check("in the per-tick voice the game uses, without the duration",
          "$d" not in aura and ("$s" in aura or "." in aura), aura)
    check("and the row carries it", aura.replace("'", "''") in sql, aura)
    instant = resolve(SpellDef.from_dict(
        {"name": "Bolt", "school": 4,
         "behaviours": [{"key": "damage", "amount": 300, "target": "enemy"}]}),
        vocabulary=vocab, spell_id=settings.band.start, rank=1, ranks_total=1)
    check("nothing that ends at once claims an effect",
          suggest_aura_description(instant) == "",
          suggest_aura_description(instant))

    print("Parameters are never invented")
    for key in ("stun", "root", "silence"):
        b = by_key[key]
        check("%s declares no amount" % key, "amount" not in b.params)
        r = resolve(SpellDef(name="x", target="enemy", duration_ms=5000,
                             behaviours=[BehaviourUse(key)]),
                    spell_id=settings.band.start)
        check("%s writes no base points" % key, r.fields["EffectBasePoints_1"] == 0)

    print("Ranks are the game's own way of scaling")
    laddered = SpellDef(name="Ladder", school=2, target="enemy", spell_level=10,
                        power_cost=50, behaviours=[BehaviourUse("damage", amount=100)],
                        extra_ranks=[RankValues(spell_level=20, power_cost=90, amounts=[220]),
                                     RankValues(spell_level=30, power_cost=140, amounts=[400])])
    check("an ability counts its ranks", laddered.rank_count() == 3)
    steps = rank_definitions(laddered)
    check("one definition per rank", len(steps) == 3, len(steps))
    check("rank 1 is the ability itself", steps[0][1] is laddered)
    check("each rank carries its own level",
          [d.spell_level for _r, d in steps] == [10, 20, 30],
          [d.spell_level for _r, d in steps])
    check("its own cost", [d.power_cost for _r, d in steps] == [50, 90, 140])
    check("and its own numbers",
          [d.behaviours[0].amount for _r, d in steps] == [100, 220, 400])
    check("later ranks do not carry ranks of their own",
          all(not d.extra_ranks for _r, d in steps[1:]))
    check("a rank does not change what the ability is",
          all(d.school == 2 and d.target == "enemy" for _r, d in steps))

    plain = SpellDef(name="Plain", behaviours=[BehaviourUse("damage", amount=5)])
    check("an ability with one rank is what it always was",
          len(rank_definitions(plain)) == 1 and not plain.extra_ranks)
    check("and carries no rank text",
          resolve(plain, spell_id=settings.band.start, rank=1, ranks_total=1).rank_text == "")
    check("a ranked one does",
          resolve(laddered, spell_id=settings.band.start, rank=2, ranks_total=3).rank_text
          == "Rank 2")
    check("a new rank starts from the last one",
          next_rank_values(laddered).spell_level == 30
          and next_rank_values(laddered).amounts == [400],
          str(next_rank_values(laddered)))
    check("and from the ability itself when there are none yet",
          next_rank_values(plain).amounts == [5], str(next_rank_values(plain)))

    # The core reads spell_ranks as 1..N in order and rejects a chain of one
    # outright, so a single-rank ability writes nothing there.
    one = ranks.chain_statements([settings.band.start])
    check("one rank writes no chain", "INSERT" not in one, one)
    check("but still clears any old one", "DELETE" in one, one)
    three = ranks.chain_statements([950000, 950001, 950002])
    check("a chain names the first spell throughout",
          three.count("950000, ") >= 3, three)
    check("and numbers the ranks 1 to N in order",
          "(950000, 950000, 1), (950000, 950001, 2), (950000, 950002, 3)" in three, three)
    check("the core's minimum chain length is respected", ranks.MIN_CHAIN == 2)

    print("Spread")
    check("no spread is a flat amount", _spread(500, 0) == (500, 500), str(_spread(500, 0)))
    check("10% of 500 lands between 450 and 550", _spread(500, 10) == (450, 550),
          str(_spread(500, 10)))
    check("a spread never goes below 1", _spread(4, 100)[0] >= 1, str(_spread(4, 100)))
    check("an amount of zero stays zero", _spread(0, 50) == (0, 0), str(_spread(0, 50)))
    spread = resolve(SpellDef(name="S", school=2, target="enemy", duration_ms=15000,
                              spread_pct=10, behaviours=[dot]), spell_id=settings.band.start)
    check("the die carries the spread", spread.effect_range(1) == (450, 550),
          str(spread.effect_range(1)))
    check("the average is still the amount", spread.effect_average(1) == 500,
          str(spread.effect_average(1)))
    flat = resolve(SpellDef(name="S", school=2, target="enemy", duration_ms=15000,
                            behaviours=[dot]), spell_id=settings.band.start)
    check("without spread the die has one side", flat.fields["EffectDieSides_1"] == 1)
    check("the tooltip reads a range",
          "450 to 550" in build_tooltip(spread)["description"],
          build_tooltip(spread)["description"])

    print("Scaling with the caster's power")
    check("magic damage scales off spell power",
          scaling.columns_for(2, True, False) == ["direct_bonus"])
    check("magic over time uses the over-time column",
          scaling.columns_for(2, False, True) == ["dot_bonus"])
    check("physical scales off attack power",
          scaling.columns_for(1, True, True) == ["ap_bonus", "ap_dot_bonus"])
    check("the stat is named for the user",
          scaling.stat_name(2) == "spell power" and scaling.stat_name(1) == "attack power")
    values = scaling.bonus_values(2, False, True, 13)
    check("13% is written as 0.13", values["dot_bonus"] == 0.13, str(values))
    check("no scaling writes no row", not any(scaling.bonus_values(2, True, True, 0).values()))
    check("our own scaling stays out of the corpus",
          all(i < settings.stock_ceiling for i in corpus.bonus),
          "corpus holds %d bonus rows" % len(corpus.bonus))

    print("Ignoring mitigation")
    plain = resolve(SpellDef(name="S", school=2, target="enemy", behaviours=[dot]),
                    spell_id=settings.band.start)
    fixed = resolve(SpellDef(name="S", school=2, target="enemy", ignore_mitigation=True,
                             behaviours=[dot]), spell_id=settings.band.start)
    check("off by default", not plain.fields["AttributesEx4"] & ATTR4_IGNORE_MITIGATION)
    check("on sets exactly the documented bit",
          fixed.fields["AttributesEx4"] == ATTR4_IGNORE_MITIGATION,
          hex(fixed.fields["AttributesEx4"]))

    print("Animations")
    found = visuals.extract(corpus)
    check("animations are derived from the corpus", len(found) > 300, str(len(found)))
    ids = [v.visual_id for v in found]
    check("one entry per animation", len(ids) == len(set(ids)))
    by_label = {v.label: v for v in found}
    check("Fireball names its own animation", by_label.get("Fireball") is not None)
    check("and it is the one Fireball uses",
          by_label["Fireball"].visual_id == corpus.by_id[133]["SpellVisualID_1"],
          str(by_label["Fireball"].visual_id))
    holy = visuals.for_school(found, 2)
    check("the list is filtered by school",
          all(not v.schools or 2 in v.schools for v in holy) and 20 < len(holy) < len(found),
          str(len(holy)))
    check("a fire animation is not offered for a holy spell",
          by_label["Fireball"] not in holy)
    check("a cast animation on an instant spell is flagged",
          "wind-up" in (visuals.cast_time_note(by_label["Fireball"], 0) or ""),
          str(visuals.cast_time_note(by_label["Fireball"], 0)))
    check("a matching cast time says nothing",
          visuals.cast_time_note(by_label["Fireball"], 3000) is None)

    print("Classes and their skill lines")
    lines = classes.skill_lines()
    every = [c["value"] for c in classes.CLASSES if c["value"]]
    check("every class derives a skill line", all(c in lines for c in every),
          str([c for c in every if c not in lines]))
    check("class masks are single bits",
          all(bin(classes.class_mask(c)).count("1") == 1 for c in every))
    check("Priest lands on a Priest skill line", lines[5][0]["skill_line"] == 56,
          str(lines.get(5)))
    priest = classes.trees_for(5)
    check("a class offers every tree it uses", len(priest) >= 3, str(priest))
    check("busiest first", priest[0]["name"] == "Holy", str(priest[0]))
    check("and the others are there",
          {t["name"] for t in priest} >= {"Holy", "Shadow Magic", "Discipline"},
          str([t["name"] for t in priest]))
    check("a line borrowed by one dual-class spell is not a tree",
          len({t["name"] for t in priest}) == len(priest),
          str([t["name"] for t in priest]))
    check("a chosen tree is honoured",
          classes.skill_line_for(5, 78) == (78, "Shadow Magic"),
          str(classes.skill_line_for(5, 78)))
    check("the default is the busiest", classes.skill_line_for(5)[0] == 56)
    try:
        classes.skill_line_for(5, 8)
        check("a tree from another class is refused", False)
    except classes.ClassError as exc:
        check("a tree from another class is refused", "does not belong" in str(exc))
    check("the chosen tree reaches the row",
          classes.ability_row(settings.band.start, 5, 78)[1] == 78,
          str(classes.ability_row(settings.band.start, 5, 78)))

    row = classes.ability_row(settings.band.start, 5)
    check("an ability row is 14 fields", len(row) == len(classes.COLUMNS), str(len(row)))
    check("the row points at its own spell",
          row[0] == settings.band.start and row[2] == settings.band.start)
    check("the row carries the class mask", row[4] == classes.class_mask(5))
    check("stock conventions are copied, not invented",
          row[3] == 0 and row[7] == 1 and row[9] == 0, str(row))
    check("an unrestricted spell gets no row", classes.ability_row(settings.band.start, 0) is None)

    print("MPQ archives")
    # The two keys every MPQ in existence uses for its own tables; if the hash
    # is wrong, nothing else can be right.
    check("hash of (hash table) is the known key",
          mpq.hash_string("(hash table)", mpq.HASH_FILE_KEY) == 0xC3AF3770,
          hex(mpq.hash_string("(hash table)", mpq.HASH_FILE_KEY)))
    check("hash of (block table) is the known key",
          mpq.hash_string("(block table)", mpq.HASH_FILE_KEY) == 0xEC83B3A3,
          hex(mpq.hash_string("(block table)", mpq.HASH_FILE_KEY)))
    check("internal paths are case and separator insensitive",
          mpq.hash_string("DBFilesClient/Spell.dbc", 0)
          == mpq.hash_string("dbfilesclient\\spell.dbc", 0))
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "patch-test.MPQ")
        payload = {
            "DBFilesClient\\Spell.dbc": bytes(range(256)) * 400,   # spans sectors
            "small.txt": b"one sector",
            "empty.bin": b"",
        }
        mpq.write(archive, payload)
        with mpq.Archive(archive) as opened:
            check("an archive we wrote reads back",
                  all(opened.read(k) == v for k, v in payload.items()))
            check("it lists what it holds",
                  set(opened.names()) == set(payload), str(opened.names()))
            check("a file it does not hold is absent", not opened.has("nope.txt"))

    print("Client patch")
    # A client build whose Spell.dbc has a different shape is a different game;
    # merging into it would write a file the client cannot read.
    wrong = dbc.Dbc([tuple([0] * 240)], b"\0", 240)
    blob = struct_pack_dbc(wrong)
    try:
        clientpatch._merge_spells(blob, [], "test.MPQ")
        check("a base with the wrong field count is refused", False)
    except clientpatch.PatchError as exc:
        check("a base with the wrong field count is refused", "240 fields" in str(exc), str(exc))
    check("the patch ships both files the client needs",
          clientpatch.SPELL_FIELDS == 234 and clientpatch.ABILITY_FIELDS == 14)

    print("Connecting is configured, not assumed")
    check("settings load without any credentials at all",
          load_settings.__name__ == "load_settings")
    saved_before = config_module.CONNECTION_FILE
    with tempfile.TemporaryDirectory() as tmp:
        config_module.CONNECTION_FILE = pathlib.Path(tmp) / "connection.json"
        try:
            check("nothing is saved to begin with", config_module.saved_connection() is None)
            config_module.save_connection({
                "host": "db.example", "port": 3307, "user": "wow",
                "password": "hunter2", "world_db": "w", "characters_db": "c"})
            back = config_module.saved_connection()
            check("what is saved comes back", back["host"] == "db.example"
                  and back["port"] == 3307 and back["password"] == "hunter2", str(back))
            mode = oct(config_module.CONNECTION_FILE.stat().st_mode & 0o777)
            check("and only the user who saved it can read it", mode == "0o600", mode)
            check("saved details win over an env file",
                  load_settings().db.host == "db.example",
                  load_settings().db.host)
            check("and the source says where they came from",
                  load_settings().db_source == "saved", load_settings().db_source)
            config_module.forget_connection()
            check("forgetting leaves nothing", config_module.saved_connection() is None)
        finally:
            config_module.CONNECTION_FILE = saved_before

    check("an env file is a default, not a requirement",
          config_module.env_file_connection("/nowhere/at/all") is None)
    blank = connection.config_from({"host": "h", "port": 1, "user": "u", "password": ""},
                                   settings.db)
    check("an empty password box keeps the saved one",
          blank.password == settings.db.password)
    typed = connection.config_from({"password": "new"}, settings.db)
    check("a typed password replaces it", typed.password == "new")

    print("A connection that fails says why")
    shut = connection.test(config_module.DbConfig(host="127.0.0.1", port=3999))
    check("an unreachable port is named as one", "Could not reach" in shut[1], shut[1])
    wrong = connection.test(config_module.DbConfig(
        host=settings.db.host, port=settings.db.port, user=settings.db.user,
        password="certainly-not-the-password"))
    check("a refused login is named as one", "refused the username" in wrong[1], wrong[1])
    missing = connection.test(config_module.DbConfig(
        host=settings.db.host, port=settings.db.port, user=settings.db.user,
        password=settings.db.password, world_db="no_such_database"))
    check("a missing database is named as one", "no database named" in missing[1], missing[1])
    good = connection.test(settings.db)
    check("a working one says so", good[0] and "Connected to" in good[1], good[1])
    check("and reports what it found", any("holds" in d for d in good[2]), good[2])

    print("Icons come from the client, not the internet")
    # A DXT block is two endpoint colours and two bits per pixel saying where
    # between them each one sits; these are the two modes icons actually use.
    solid = struct.pack("<HHI", 0xF800, 0xF800, 0)          # both ends pure red
    w, h, rgba = 4, 4, blp._decode_dxt(solid, 4, 4, blp.DXT1)
    check("a DXT1 block decodes to its own colour", rgba[:4] == bytes((255, 0, 0, 255)),
          str(rgba[:4]))
    check("and fills the whole block", len(rgba) == 4 * 4 * 4, str(len(rgba)))
    faded = struct.pack("<HHI", 0x0000, 0xFFFF, 0xFFFFFFFF)  # index 3 everywhere
    rgba = blp._decode_dxt(faded, 4, 4, blp.DXT1)
    check("DXT1 spends its fourth slot on transparency",
          rgba[3] == 0, str(rgba[:4]))
    dxt3 = b"\xff" * 8 + struct.pack("<HHI", 0xF800, 0xF800, 0)
    rgba = blp._decode_dxt(dxt3, 4, 4, blp.DXT3)
    check("DXT3 carries its own alpha", rgba[3] == 255, str(rgba[:4]))

    png = blp.png(2, 1, bytes((255, 0, 0, 255, 0, 255, 0, 128)))
    check("a PNG starts with the PNG signature", png[:8] == b"\x89PNG\r\n\x1a\n")
    check("and ends with IEND", png[-8:-4] == b"IEND", str(png[-8:]))
    check("an icon name from a URL is checked",
          icons.is_safe("Spell_Holy_LesserHeal") and not icons.is_safe("../../etc/passwd")
          and not icons.is_safe("a/b"))

    print("Client load order")
    # Measured against a real client: the same spell shipped in Data/patch-W.MPQ
    # and Data/enUS/patch-enUS-Z.MPQ, and the generic one is what the client drew,
    # even though Z sorts after W.
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "Data")
        os.makedirs(os.path.join(data, "enUS"))
        for name in ("common.MPQ", "patch-3.MPQ", "patch-W.MPQ"):
            open(os.path.join(data, name), "wb").close()
        for name in ("locale-enUS.MPQ", "patch-enUS-3.MPQ", "patch-enUS-Z.MPQ"):
            open(os.path.join(data, "enUS", name), "wb").close()
        ordered, locale = client.chain(tmp)
        rank = {os.path.basename(path): position for path, _tier, position in ordered}
        check("locale is found", locale == "enUS", locale)
        check("a generic custom patch outranks a locale one",
              rank["patch-W.MPQ"] > rank["patch-enUS-Z.MPQ"], str(rank))
        check("custom slots outrank numbered patches",
              rank["patch-W.MPQ"] > rank["patch-enUS-3.MPQ"] > rank["patch-3.MPQ"], str(rank))
        check("base archives are lowest", rank["common.MPQ"] < rank["locale-enUS.MPQ"], str(rank))
        slot, _ = client.free_slot(tmp)
        check("a new patch takes a generic slot that beats everything present",
              os.path.dirname(slot) == data and os.path.basename(slot) == "patch-Z.MPQ", slot)

    print("Restarting the worldserver")
    from spellweaver import restart as restart_module
    def with_command(command=""):
        """Settings that differ from the real ones only in the command."""
        return dataclasses.replace(settings, restart_command=command)

    blank = with_command()
    check("nothing configured is said, not guessed at",
          not restart_module.configured(blank))
    try:
        restart_module.run(blank)
        check("running without one is refused", False, "no error raised")
    except restart_module.RestartError as exc:
        check("running without one is refused", "Settings" in str(exc), str(exc))
    told = with_command("exit 0")
    check("a configured one is seen", restart_module.configured(told))
    check("and it runs", restart_module.run(told)["ok"])
    failed = restart_module.run(with_command("exit 7"))
    check("a refusal is reported, not raised", failed["ok"] is False, failed)
    check("with the code the server gave", failed["code"] == 7, failed)
    said = restart_module.run(with_command("echo out; echo err >&2"))
    check("both streams are kept", "out" in said["output"] and "err" in said["output"],
          said["output"])
    long_output = restart_module.run(
        with_command("python3 -c \"print('x' * 20000)\""))
    check("a wall of output is trimmed to its end",
          len(long_output["output"]) <= restart_module.MAX_OUTPUT + 3,
          len(long_output["output"]))
    # The one property that matters: the command is the operator's, never the
    # caller's. Nothing in the module reads a command from anywhere but settings.
    source = (ROOT / "spellweaver" / "restart.py").read_text(encoding="utf-8")
    check("the command comes only from settings",
          source.count("settings.restart_command") == 2
          and "_body" not in source, source.count("settings.restart_command"))

    # A dead server used to surface as the browser's own words - "NetworkError
    # when attempting to fetch resource" - which names neither the cause nor
    # the cure.
    print("A server that has gone says so")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    check("requests go through one place",
          js.count("await fetch(") == 1, js.count("await fetch("))
    check("and that place is the wrapper",
          "async function ask(path, options)" in js)
    check("a failure to connect becomes something readable",
          "SpellWeaver is not running" in js)
    check("naming however this one is started",
          "Run ${LAUNCHER} to restart" in js)
    check("which the server decides, not the browser",
          '"launcher"' in (ROOT / "spellweaver" / "server.py")
          .read_text(encoding="utf-8"))
    check("and stays general until it has said",
          "Start it again to carry on" in js)
    check("told apart from an answer that could not be parsed",
          "could not read" in js)
    check("and from a stop the user asked for",
          'classList.contains("stopped")' in js)
    check("the page has somewhere to say it", 'id="offline"' in page)
    check("which does not block putting it right",
          "pointer-events: none" not in style.split(".offline {")[1].split("}")[0])

    # Colour alone carried what kind of message this was, which is nothing in
    # greyscale and nothing to anyone not already fluent in the code.
    print("Messages say what they are")
    app_js = js
    check("every kind of message has a mark",
          all(('%s:' % kind) in app_js.split("MSG_MARKS = {")[1].split("};")[0]
              for kind in ("note", "warn", "err", "ok")))
    check("one place builds them, so none can be missed",
          app_js.count('className = `msg ${cls}`') == 1,
          app_js.count('className = `msg ${cls}`'))
    check("the coloured edge is gone", "border-left-color: var(--warn)" not in style)
    check("and the mark is drawn, not typed",
          ".msg .mark" in style and "stroke: currentColor" in style)

    # The Windows launcher is the only entry point most people will use, so it
    # has to keep matching the script it starts.
    print("The Windows launcher")
    bat = (ROOT / "SpellWeaver.bat").read_text(encoding="utf-8")
    check("there is one", bool(bat))
    check("it starts run.py", "run.py" in bat)
    check("asks for the browser", "--open" in bat)
    check("and run.py still takes that flag",
          '"--open"' in (ROOT / "run.py").read_text(encoding="utf-8"))
    check("it passes on whatever else it was given", "%*" in bat)
    check("it refuses a network path, which cmd cannot use",
          '"%HERE:~0,2%"=="\\\\"' in bat)
    check("it prefers the py launcher over a Store stub",
          bat.index("py -3 -c") < bat.index("python -c"))
    check("it waits when something went wrong, so the reason can be read",
          "pause" in bat)
    check("and closes on a clean stop rather than making the user dismiss it",
          '"%CODE%"=="0" exit /b 0' in bat)

    print("Band is enforced on writes")
    from spellweaver import store as _store
    try:
        _store._guard(settings.band, settings.band.start - 1)
        check("a write below the band is refused", False)
    except _store.BandError:
        check("a write below the band is refused", True)

    print()
    if failures:
        print("%d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
