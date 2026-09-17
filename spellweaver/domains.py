"""Value sets for the parameters a behaviour can declare.

When a behaviour says it takes a `school` or a `stat`, the UI needs the list of
choices with names a person recognises. These are small, stable server enums;
the ids are what goes into EffectMiscValue.
"""

SCHOOLS = [
    {"value": 1, "label": "Physical"},
    {"value": 2, "label": "Holy"},
    {"value": 4, "label": "Fire"},
    {"value": 8, "label": "Nature"},
    {"value": 16, "label": "Frost"},
    {"value": 32, "label": "Shadow"},
    {"value": 64, "label": "Arcane"},
]

# EffectMiscValue for school-scoped auras wants the mask, same as SchoolMask.
SCHOOL_MASKS = SCHOOLS

POWER_TYPES = [
    {"value": 0, "label": "Mana"},
    {"value": 1, "label": "Rage"},
    {"value": 2, "label": "Focus"},
    {"value": 3, "label": "Energy"},
    {"value": 6, "label": "Runic Power"},
    {"value": -2, "label": "Health"},
]

STATS = [
    {"value": -1, "label": "All stats"},
    {"value": 0, "label": "Strength"},
    {"value": 1, "label": "Agility"},
    {"value": 2, "label": "Stamina"},
    {"value": 3, "label": "Intellect"},
    {"value": 4, "label": "Spirit"},
]

# Combat ratings, for SPELL_AURA_MOD_RATING. The field is a bitmask of ratings.
RATINGS = [
    {"value": 1 << 5, "label": "Hit (melee)"},
    {"value": 1 << 8, "label": "Hit (spell)"},
    {"value": 1 << 9, "label": "Crit (melee)"},
    {"value": 1 << 11, "label": "Crit (spell)"},
    {"value": 1 << 17, "label": "Haste (melee)"},
    {"value": 1 << 19, "label": "Haste (spell)"},
    {"value": 1 << 20, "label": "Expertise"},
    {"value": 1 << 12, "label": "Defence"},
    {"value": 1 << 13, "label": "Dodge"},
    {"value": 1 << 14, "label": "Parry"},
    {"value": 1 << 15, "label": "Block"},
]

DISPEL_TYPES = [
    {"value": 1, "label": "Magic"},
    {"value": 2, "label": "Curse"},
    {"value": 3, "label": "Disease"},
    {"value": 4, "label": "Poison"},
    {"value": 5, "label": "Stealth"},
    {"value": 6, "label": "Invisibility"},
    {"value": 9, "label": "Enrage"},
]

MECHANICS = [
    {"value": 12, "label": "Stun"},
    {"value": 7, "label": "Root"},
    {"value": 5, "label": "Fear"},
    {"value": 2, "label": "Disorient"},
    {"value": 1, "label": "Charm"},
    {"value": 11, "label": "Slow"},
    {"value": 14, "label": "Knockout"},
    {"value": 15, "label": "Bleed"},
    {"value": 16, "label": "Bandage"},
    {"value": 17, "label": "Polymorph"},
    {"value": 19, "label": "Silence"},
    {"value": 22, "label": "Shield"},
    {"value": 25, "label": "Snare"},
    {"value": 26, "label": "Interrupt"},
]

SKILLS = [
    {"value": 43, "label": "Swords"},
    {"value": 44, "label": "Axes"},
    {"value": 45, "label": "Bows"},
    {"value": 54, "label": "Maces"},
    {"value": 95, "label": "Defence"},
    {"value": 162, "label": "Unarmed"},
]

# Which domain each declared parameter draws from.
PARAM_DOMAINS = {
    "school": SCHOOLS,
    "power": POWER_TYPES,
    "stat": STATS,
    "rating": RATINGS,
    "dispel": DISPEL_TYPES,
    "mechanic": MECHANICS,
    "skill": SKILLS,
}

# Human labels and help for the non-domain parameters.
PARAM_LABELS = {
    "amount": ("Amount", "How much this effect does each time it applies."),
    "period": ("Time between ticks", "Seconds between each tick of the effect."),
    "chain": ("Extra targets", "How many further characters the effect jumps to."),
    "trigger": ("Ability to cast", "The id of another ability to cast. It must already exist."),
    "school": ("School", None),
    "power": ("Resource", None),
    "stat": ("Stat", None),
    "rating": ("Rating", None),
    "dispel": ("Type to remove", None),
    "mechanic": ("Effect", None),
    "skill": ("Skill", None),
    "heal_share": ("Healing returned",
                   "A share of the damage dealt, healed back to the caster. "
                   "50% heals you for half of what you deal."),
    "gain_share": ("Share the caster keeps",
                   "How much of what is drained the caster receives."),
    "burn_ratio": ("Damage per point destroyed",
                   "Each point of the resource destroyed deals this much damage."),
}

# Share parameters the browser shows as a percentage; the game stores a fraction.
PERCENT_PARAMS = ("heal_share", "gain_share")
