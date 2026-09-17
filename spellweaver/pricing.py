"""What a trainer should charge, learned from what the game's trainers charge.

A price is not a number the tool can invent. A spell that costs 5 gold at level
12 is wrong in a way the user cannot see until a player meets it, so the price
comes from the same place the behaviours do: the shipped data.

`trainer_spell` holds every price the game charges, and the median cost at each
required level rises smoothly from 10 copper at level 1 to about 18 gold at
level 80. That curve is the suggestion.

Only *class* trainers are read. Profession trainers price recipes and riding
trainers price mounts; neither is what it costs a player to learn a spell.
Rows belonging to trainers this tool created are excluded, for the same reason
the spell corpus stops at the stock ceiling: our own inventions must never
become the evidence for what the next spell should cost.
"""
import statistics

MIN_LEVEL = 1
MAX_LEVEL = 80

CLASS_TRAINER = 0          # trainer.Type

GOLD = 10000               # copper
SILVER = 100


def format_money(copper):
    """Copper as the game writes it: 18g 40s 5c, dropping the empty parts."""
    copper = int(copper)
    if copper <= 0:
        return "free"
    parts = []
    if copper >= GOLD:
        parts.append("%dg" % (copper // GOLD))
    if copper % GOLD >= SILVER:
        parts.append("%ds" % (copper % GOLD // SILVER))
    if copper % SILVER:
        parts.append("%dc" % (copper % SILVER))
    return " ".join(parts)


class Pricing:
    """The going rate for a trained spell, by required level."""

    def __init__(self, db, settings):
        self.samples = self._load(db, settings)
        self.curve = self._build(self.samples)

    @staticmethod
    def _load(db, settings):
        band = settings.trainer_band
        _, rows = db.query(
            "SELECT ts.ReqLevel, ts.MoneyCost FROM trainer_spell ts "
            "JOIN trainer t ON t.Id = ts.TrainerId "
            "WHERE t.Type = %d AND ts.MoneyCost > 0 "
            "AND ts.ReqLevel BETWEEN %d AND %d "
            "AND ts.SpellId < %d "
            "AND ts.TrainerId NOT BETWEEN %d AND %d"
            % (CLASS_TRAINER, MIN_LEVEL, MAX_LEVEL, settings.stock_ceiling,
               band.start, band.end))
        samples = {}
        for level, cost in rows:
            samples.setdefault(level, []).append(cost)
        return samples

    @staticmethod
    def _build(samples):
        """A cost for every level, interpolating the ones nobody trains at.

        Stock trainers teach on even levels, so half the curve has no evidence
        of its own and is read off the line between its neighbours.
        """
        known = sorted((level, int(statistics.median(costs)))
                       for level, costs in samples.items())
        curve = {}
        if not known:
            return curve
        for level in range(MIN_LEVEL, MAX_LEVEL + 1):
            below = [k for k in known if k[0] <= level]
            above = [k for k in known if k[0] >= level]
            if not below:
                curve[level] = above[0][1]
            elif not above:
                curve[level] = below[-1][1]
            else:
                (lo_level, lo_cost), (hi_level, hi_cost) = below[-1], above[0]
                if lo_level == hi_level:
                    curve[level] = lo_cost
                else:
                    span = (level - lo_level) / (hi_level - lo_level)
                    curve[level] = int(round(lo_cost + span * (hi_cost - lo_cost)))
        return curve

    def suggest(self, level):
        """The going rate at this level, in copper."""
        level = max(MIN_LEVEL, min(MAX_LEVEL, int(level or MIN_LEVEL)))
        return self.curve.get(level, 0)

    def evidence(self, level):
        """The suggestion, and what it was drawn from, so the UI can say so."""
        level = max(MIN_LEVEL, min(MAX_LEVEL, int(level or MIN_LEVEL)))
        count = len(self.samples.get(level, []))
        return {
            "level": level,
            "cost": self.suggest(level),
            "money": format_money(self.suggest(level)),
            "samples": count,
            "interpolated": count == 0,
        }

    def payload(self):
        """The whole curve, so the browser can price a level without asking."""
        return [self.evidence(level)
                for level in range(MIN_LEVEL, MAX_LEVEL + 1)]

    def total_samples(self):
        return sum(len(v) for v in self.samples.values())
