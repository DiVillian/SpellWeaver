#!/usr/bin/env python3
"""Step 1: derive the behaviour vocabulary from the shipped spell data.

Reads Spell.dbc and its companions, joins them to the world database, and
prints the vocabulary for review. With --json it also writes the machine
readable form the rest of the tool builds on.

  python3 tools/extract_vocabulary.py                 # the report
  python3 tools/extract_vocabulary.py --json out.json # and save it
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from spellweaver.behaviours import APPLICATION_MODES, BEHAVIOURS, WITHHELD
from spellweaver.config import band_conflicts, load_settings, require_connection
from spellweaver.corpus import Corpus
from spellweaver.enums import SPELL_AURAS, SPELL_EFFECTS
from spellweaver.mysql import connect
from spellweaver.vocabulary import Vocabulary, pair_label

RULE = "=" * 78


def heading(text):
    print()
    print(RULE)
    print(text)
    print(RULE)


def fmt_defaults(defaults):
    """Render defaults compactly, with units."""
    units = {"period": "ms", "duration": "ms", "radius": "yd"}
    bits = []
    for name, d in defaults.items():
        label = {"coeff_direct": "coeff", "coeff_dot": "coeff"}.get(name, name)
        bits.append("%s=%s%s" % (label, d["value"], units.get(name, "")))
    return " ".join(bits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="write the vocabulary as JSON")
    ap.add_argument("--min-uses", type=int, default=20,
                    help="threshold for reporting uncovered pairs (default 20)")
    args = ap.parse_args()

    settings = require_connection(load_settings())
    with connect(settings.db) as db:
        corpus = Corpus(db=db, stock_ceiling=settings.stock_ceiling)
        band = band_conflicts(db, settings.band)
    vocab = Vocabulary(corpus)

    heading("CORPUS")
    print("  Stock spells only: ids below %d are treated as Blizzard data," % settings.stock_ceiling)
    print("  anything at or above it as a module's. Highest stock id seen: %d."
          % corpus.max_stock_id())
    print()
    print("  Spell.dbc                %6d spells" % len(corpus.spells))
    print("  on a skill line          %6d (player facing; used to weight defaults)"
          % len(corpus.player_spell_ids))
    print("  spell icons              %6d" % len(corpus.icons))
    print("  spell_bonus_data rows    %6d (spell power coefficients)" % len(corpus.bonus))
    print("  distinct effect/aura pairs in use %d" % len(vocab.pair_counts))
    print()
    print("  Excluded as module content:")
    for source, n in sorted(corpus.excluded.items()):
        print("    %-20s %6d" % (source, n))
    if not any(corpus.excluded.values()):
        print("    (none: every source on this server is already stock-only,")
        print("     so the vocabulary below is unaffected by mod-era-talents)")

    heading("RESERVED ID BAND")
    print("  spellweaver writes to    %s  (set in spellweaver.json)" % settings.band)
    print("  rows already in that band in %s.spell_dbc: %d"
          % (settings.db.world_db, band["count"]))
    print("  nearest existing spell id below the band: %d" % band["nearest_below"])
    if band["count"]:
        print()
        print("  WARNING: %d rows already occupy %d-%d. Pick a different band in"
              % (band["count"], band["min"], band["max"]))
        print("  spellweaver.json before creating anything.")
    else:
        print("  Clear on this server, with %d ids of headroom below the floor."
              % (settings.band.start - band["nearest_below"]))
        print("  That headroom is a fact about this install, not a guarantee;")
        print("  the band is re-checked before every write.")

    heading("VOCABULARY")
    print("Each behaviour is one (effect id / aura id) pair. `uses` counts effect")
    print("slots across all %d spells; `player` counts those on a skill line." % len(corpus.spells))
    print("Defaults are medians over the spells that use the behaviour.")
    by_category = {}
    for b in BEHAVIOURS:
        by_category.setdefault(b.category, []).append(b)

    for category in ("Damage", "Healing", "Control", "Movement", "Attributes", "Utility"):
        print()
        print("-- %s %s" % (category, "-" * (74 - len(category))))
        for b in by_category.get(category, []):
            obs = vocab.observation(b)
            uses = obs.count_all if obs else 0
            player = obs.count_player if obs else 0
            print()
            print("  %-36s %-9s uses=%-5d player=%-4d" %
                  (b.label, "%d/%d" % (b.effect, b.aura), uses, player))
            print("      %s" % b.summary)
            defaults = vocab.defaults(b)
            if defaults:
                print("      defaults: %s" % fmt_defaults(defaults))
            if b.params:
                print("      user sets: %s" % ", ".join(b.params))
            shapes = vocab.target_shapes(b, 2)
            if shapes:
                print("      usual targets: %s" % "; ".join(s["label"] for s in shapes))
            if obs and obs.examples:
                print("      seen in: %s" % ", ".join(n for _, n in obs.examples[:3]))
            if not uses:
                print("      NOTE: this pair appears nowhere in the corpus.")

    heading("APPLYING AN AURA TO A GROUP")
    print("The aura behaviours above use APPLY_AURA, which lands on one target.")
    print("The same aura can be radiated from the caster instead, by swapping the")
    print("effect id. This is an axis in the UI, not %d more behaviours." % len(BEHAVIOURS))
    aura_behaviours = [b for b in BEHAVIOURS if b.effect == 6]
    print()
    for effect, key, label in APPLICATION_MODES:
        attested = sum(1 for b in aura_behaviours
                       if vocab.is_attested(effect, b.aura))
        print("  %-8s effect %-4d %-44s %d/%d attested"
              % (key, effect, label, attested, len(aura_behaviours)))

    heading("DELIBERATELY NOT OFFERED")
    print("Common effects left out of the vocabulary, and why. Anything needing a")
    print("C++ script or a row in another table cannot be built from this UI alone.")
    print()
    withheld_rows = sorted(
        ((vocab.pair_counts.get(k, 0), k, why) for k, why in WITHHELD.items()),
        reverse=True)
    for uses, key, why in withheld_rows:
        print("  %-9s %-44s uses=%-5d" % ("%d/%d" % key, pair_label(*key), uses))
        print("      %s" % why)

    heading("COVERAGE")
    cov = vocab.coverage(corpus)
    total = cov["total"]
    print("  Effect slots across player-facing stock spells: %d" % total)
    print("    named by the vocabulary   %5d  %5.1f%%"
          % (cov["named"], 100.0 * cov["named"] / total))
    print("    explicitly withheld       %5d  %5.1f%%"
          % (cov["withheld"], 100.0 * cov["withheld"] / total))
    print("    accounted for             %5d  %5.1f%%"
          % (cov["accounted"], 100.0 * cov["accounted"] / total))
    print()
    gaps = [(k, n) for k, n, why in vocab.unnamed_common_pairs(args.min_uses) if not why]
    if gaps:
        print("  Working pairs with >=%d uses that are neither named nor withheld:" % args.min_uses)
        for key, n in gaps:
            print("    %-9s %-46s %d" % ("%d/%d" % key, pair_label(*key), n))
    else:
        print("  No working pair with >=%d uses is unaccounted for." % args.min_uses)

    heading("VALIDATOR")
    print("  Known-good effect/aura pairs      %d" % len(vocab.pair_counts))
    print("  Known-good pairs of behaviours    %d" % len(vocab.cooccurrence))
    print()
    print("  A combination absent from both is not necessarily broken, but nothing")
    print("  in %d shipped spells does it, which is usually a mistake." % len(corpus.spells))
    print()
    for keys in (["damage", "damage_over_time"], ["heal", "stun"], ["damage", "fly"]):
        warnings = vocab.check_combination(keys)
        print("  %-34s %s" % (" + ".join(keys),
                              "ok" if not warnings else warnings[0]))

    if args.json:
        write_json(args.json, vocab, corpus, settings)
        print()
        print("Wrote %s" % args.json)


def write_json(path, vocab, corpus, settings):
    out = {
        "corpus": {
            "spells": len(corpus.spells),
            "player_spells": len(corpus.player_spell_ids),
            "distinct_pairs": len(vocab.pair_counts),
            "stock_ceiling": settings.stock_ceiling,
            "max_stock_id": corpus.max_stock_id(),
            "excluded": corpus.excluded,
        },
        "band": {"start": settings.band.start, "end": settings.band.end},
        "application_modes": [
            {"effect": e, "key": k, "label": l} for e, k, l in APPLICATION_MODES],
        "behaviours": [],
        "withheld": [{"effect": e, "aura": a, "name": pair_label(e, a), "reason": why,
                      "uses": vocab.pair_counts.get((e, a), 0)}
                     for (e, a), why in WITHHELD.items()],
        "attested_pairs": ["%d/%d" % k for k in sorted(vocab.pair_counts)],
        "attested_combinations": ["%d/%d+%d/%d" % (a[0], a[1], b[0], b[1])
                                  for a, b in sorted(vocab.cooccurrence)],
    }
    for b in BEHAVIOURS:
        obs = vocab.observation(b)
        out["behaviours"].append({
            "key": b.key,
            "label": b.label,
            "category": b.category,
            "effect": b.effect,
            "aura": b.aura,
            "effect_name": SPELL_EFFECTS.get(b.effect),
            "aura_name": SPELL_AURAS.get(b.aura) if b.aura else None,
            "shape": b.shape,
            "params": list(b.params),
            "summary": b.summary,
            "uses": obs.count_all if obs else 0,
            "player_uses": obs.count_player if obs else 0,
            "defaults": vocab.defaults(b),
            "target_shapes": vocab.target_shapes(b),
            "examples": [{"id": i, "name": n} for i, n in (obs.examples if obs else [])],
            "area_modes": [k for e, k, _ in APPLICATION_MODES
                           if b.effect == 6 and vocab.is_attested(e, b.aura)],
        })
    pathlib.Path(path).write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
