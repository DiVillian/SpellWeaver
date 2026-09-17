"""The JSON payloads the browser needs.

Kept apart from the HTTP plumbing so the same calls can be exercised from a
test without going through a socket.
"""
from . import classes, scaling, store, trainers, visuals as visuals_module
from .behaviours import BEHAVIOURS
from .domains import PARAM_DOMAINS, PARAM_LABELS, PERCENT_PARAMS
from .pricing import MAX_LEVEL, format_money
from .spell import (SpellDef, rank_definitions, resolve, POWER_NAMES,
                    SCHOOL_NAMES)
from .targeting import TARGETS, allowed_for
from .vocabulary import SHARE_PARAMS
from .tooltip import build as build_tooltip, suggest_description


def vocabulary_payload(vocab, corpus, settings, pricing=None, visuals=None):
    """Everything the form needs to render itself.

    Each behaviour carries its own defaults, so a field can be pre-filled the
    moment it appears rather than after a round trip.
    """
    behaviours = []
    for b in BEHAVIOURS:
        obs = vocab.observation(b)
        defaults = vocab.defaults(b)
        params = []
        for name in b.params:
            label, help_text = PARAM_LABELS.get(name, (name.title(), None))
            entry = {"name": name, "label": label, "help": help_text}
            if name in PARAM_DOMAINS:
                entry["options"] = PARAM_DOMAINS[name]
                entry["default"] = PARAM_DOMAINS[name][0]["value"]
            elif name == "period":
                entry["default"] = round(defaults.get("period", {}).get("value", 3000) / 1000.0, 2)
            elif name in SHARE_PARAMS:
                # All three are the same field, worded for what is being shared.
                # The two that read as percentages are shown as percentages.
                share = defaults.get("multiple", {}).get("value", 1.0)
                entry["share"] = True
                entry["percent"] = name in PERCENT_PARAMS
                entry["default"] = round(share * 100, 1) if name in PERCENT_PARAMS else share
            else:
                entry["default"] = defaults.get(name, {}).get("value", 0)
            params.append(entry)
        behaviours.append({
            "key": b.key,
            "label": b.label,
            "category": b.category,
            "shape": b.shape,
            "summary": b.summary,
            "params": params,
            "uses": obs.count_all if obs else 0,
            "player_uses": obs.count_player if obs else 0,
            "duration_default": defaults.get("duration", {}).get("value", 0),
            "radius_default": defaults.get("radius", {}).get("value", 8),
            "needs_duration": b.shape in ("aura", "periodic", "ground"),
            "targets": [t.key for t in allowed_for(b)],
            "examples": [n for _, n in (obs.examples[:3] if obs else [])],
            # What the game's own spells of this shape scale at, as a percentage.
            "scaling_default": round(100 * (
                defaults.get("coeff_dot", defaults.get("coeff_direct", {}))
                .get("value", 0) or 0), 1),
            "has_amount": "amount" in b.params,
        })
    return {
        "behaviours": behaviours,
        "categories": ["Damage", "Healing", "Control", "Movement", "Attributes", "Utility"],
        "classes": classes.payload(),
        "visuals": [v.to_dict() for v in (visuals or [])],
        "targets": [{"key": t.key, "label": t.label, "family": t.family,
                     "area": t.area, "summary": t.summary} for t in TARGETS],
        "schools": [{"value": v, "label": l} for v, l in sorted(SCHOOL_NAMES.items())],
        "powers": [{"value": v, "label": l} for v, l in POWER_NAMES.items()],
        "band": {"start": settings.band.start, "end": settings.band.end},
        "icons": [{"id": i, "name": _icon_name(p)} for i, p in sorted(corpus.icons.items())],
        # The whole price curve travels with the vocabulary, so changing the
        # required level re-prices the spell without asking the server.
        "price_curve": pricing.payload() if pricing else [],
        "max_level": MAX_LEVEL,
    }


def _icon_name(path):
    return path.rsplit("\\", 1)[-1] if path else ""


def trainers_payload(db, settings):
    """Who can teach a spell: the trainers we made, and the ones the game ships."""
    # What the game already ships for each class, so the browser can offer
    # "every Priest trainer in the game" without asking again.
    by_class = {}
    for entry in classes.CLASSES:
        if not entry["value"]:
            continue
        found = trainers.class_trainers(db, settings, entry["value"])
        if found:
            by_class[str(entry["value"])] = {
                "lists": len(found), "npcs": sum(t["npcs"] for t in found),
                "names": [t["name"] for t in found[:3]]}
    return {
        "trainers": trainers.list_trainers(db, settings),
        "by_class": by_class,
        "core": trainers.list_core_trainers(db, settings),
        "models": trainers.model_options(db),
        "classes": trainers.CLASSES,
        "factions": trainers.FACTIONS,
        "creature_band": {"start": settings.creature_band.start,
                          "end": settings.creature_band.end},
        "trainer_band": {"start": settings.trainer_band.start,
                         "end": settings.trainer_band.end},
    }


def preview_payload(data, vocab, spell_id=None, training=None, visuals=None):
    """Resolve a definition and return everything derived from it.

    The tooltip and the SQL come out of the same ResolvedSpell, so they cannot
    drift apart.
    """
    definition = SpellDef.from_dict(data)
    pairs = rank_definitions(definition)
    total = len(pairs)
    # The preview is of rank 1; the others are summarised beside it.
    resolved = resolve(pairs[0][1], vocabulary=vocab,
                       spell_id=spell_id or definition.spell_id or 0,
                       rank=1, ranks_total=total)
    description = definition.description.strip() or suggest_description(resolved)
    note = _visual_note(definition, visuals)
    if note:
        resolved.notes.append(note)
    return {
        "ranks": _rank_summary(pairs, vocab),
        "class_sql": classes.ability_sql(resolved.spell_id, definition.class_id,
                                        definition.skill_line),
        "scaling_sql": scaling.bonus_sql(
            resolved.spell_id, definition.name, resolved.fields["SchoolMask"],
            resolved.has_direct(), resolved.has_periodic(), definition.power_scaling),
        "training_sql": _training_sql(resolved, training),
        "training_summary": _training_summary(resolved, training),
        "tooltip": build_tooltip(resolved),
        "suggested_description": suggest_description(resolved),
        "sql": store.spell_dbc_sql(resolved, definition.name, description),
        "warnings": resolved.warnings,
        "notes": resolved.notes,
        "resolved": {
            "duration_ms": resolved.duration_ms(),
            "cast_time_ms": resolved.cast_time_ms(),
            "range_yards": resolved.range_yards(),
            "effects": [
                {"slot": s,
                 "effect": resolved.fields["Effect_%d" % s],
                 "aura": resolved.fields["EffectAura_%d" % s],
                 "amount": resolved.effect_amount(s),
                 "period_ms": resolved.fields["EffectAuraPeriod_%d" % s],
                 "target_a": resolved.fields["ImplicitTargetA_%d" % s],
                 "target_b": resolved.fields["ImplicitTargetB_%d" % s],
                 "radius": resolved.radius_yards(s)}
                for s in resolved.active_slots()],
        },
    }


def _training_sql(resolved, training):
    """The row that puts this spell on a trainer's list, shown before it runs."""
    if not training or not training.get("trainer_id"):
        return ""
    return trainers.as_text(trainers.training_statements(
        resolved.spell_id, int(training["trainer_id"]),
        int(training.get("money_cost") or 0), resolved.fields["SpellLevel"]))


def _training_summary(resolved, training):
    if not training or not training.get("trainer_id"):
        return None
    cost = int(training.get("money_cost") or 0)
    return {"trainer_id": int(training["trainer_id"]),
            "trainer_name": training.get("trainer_name") or "",
            "money_cost": cost,
            "money": format_money(cost),
            "req_level": resolved.fields["SpellLevel"]}


def _visual_note(definition, visuals):
    """Say so when the animation and the cast time do not agree."""
    if not definition.visual_id or not visuals:
        return None
    chosen = next((v for v in visuals if v.visual_id == definition.visual_id), None)
    return visuals_module.cast_time_note(chosen, definition.cast_time_ms)


def _rank_summary(pairs, vocab):
    """Each rank as it will be written, so the form can show the whole line."""
    total = len(pairs)
    out = []
    for rank, defn in pairs:
        resolved = resolve(defn, vocabulary=vocab, spell_id=0, rank=rank,
                           ranks_total=total)
        out.append({
            "rank": rank,
            "spell_level": defn.spell_level,
            "power_cost": defn.power_cost,
            "amounts": [resolved.effect_amount(slot)
                        for slot in resolved.active_slots()],
            "label": resolved.rank_text,
        })
    return out
