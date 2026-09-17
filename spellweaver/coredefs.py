"""What the server core actually implements.

Spell.dbc will happily hold any effect id, but a good number of them dispatch to
a do-nothing handler, and others (the DUMMY family) only mean something when a
C++ SpellScript is attached. A spell built out of those looks fine in the
database and does nothing in game, which is the worst failure mode for a tool
aimed at someone who cannot read the schema.

So when a source checkout is available we parse the core's own dispatch tables
and classify every effect and aura:

  "works"  - a real handler; safe to offer
  "script" - a dummy/script hook; does nothing without C++ support
  "dead"   - HandleNULL / EffectUnused; does nothing at all

Without a checkout the tool still runs; it simply stops warning about handlers
it can no longer see, since a missing answer is not the same as a bad one.

Note that the aura table's `HandleNoImmediateEffect` is *not* a sign of an
unimplemented aura. It covers 123 auras, including PERIODIC_DAMAGE, and only
means the aura has no work to do at apply time because it is consumed later,
during damage calculation or on a periodic tick.
"""
import pathlib
import re

from .config import load_settings


def _tables():
    """Where the core's dispatch tables are, if this machine has them.

    A source checkout is not needed to run the tool, only to sharpen it, so the
    path is a setting and its absence is an answer rather than an error.
    """
    src = pathlib.Path(load_settings().core_src)
    return (src / "server/game/Spells/SpellEffects.cpp",
            src / "server/game/Spells/Auras/SpellAuraEffects.cpp")


def available():
    return all(path.is_file() for path in _tables())

WORKS, SCRIPT, DEAD = "works", "script", "dead"

# Handlers that exist but deliberately do nothing.
_DEAD = {"EffectNULL", "EffectUnused", "HandleNULL", "HandleUnused"}
# Handlers that are only a hook for a C++ script to attach to.
_SCRIPT = {"EffectDummy", "EffectScriptEffect", "EffectSendEvent", "HandleAuraDummy"}
# PERIODIC_DUMMY shares the generic handler but is a script hook by convention.
_SCRIPT_AURA_IDS = {226}


def _handlers(path, pattern):
    text = path.read_text(encoding="utf-8", errors="replace")
    return [m.group(1) for m in re.finditer(pattern, text)]


def _classify(handlers, script_ids=frozenset()):
    out = {}
    for idx, name in enumerate(handlers):
        if name in _DEAD:
            out[idx] = DEAD
        elif name in _SCRIPT or idx in script_ids:
            out[idx] = SCRIPT
        else:
            out[idx] = WORKS
    return out


def effect_support():
    """Effect id to handler quality, or nothing at all if the core is not here."""
    effects, _auras = _tables()
    if not effects.is_file():
        return {}
    return _classify(_handlers(effects, r"&Spell::(\w+),"))


def aura_support():
    _effects, auras = _tables()
    if not auras.is_file():
        return {}
    return _classify(_handlers(auras, r"&AuraEffect::(\w+),"), _SCRIPT_AURA_IDS)
