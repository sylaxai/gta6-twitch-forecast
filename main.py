"""Forecast the launch-day Twitch concurrent viewers of Grand Theft Auto VI,
hour by hour, from the first hour's count.

Entry for make.fun gta6-twitch-ccu-forecast-01.  Reads one case from the input
directory (/input/<id>.in, or $MAKEFUN_INPUT_DIR) and writes one forecast to
the output directory (/output/<id>.out, or $MAKEFUN_OUTPUT_DIR): for each of
hours 2 to 24 of the release day (UTC), five quantiles (5%, 25%, 50%, 75%,
95%) of that hour's concurrent viewers in the game's Twitch category.

Model: an ensemble of two analog forecasters, both fit at run time on the
case's own comparables (past big game launches' release days, each with its
hourly Twitch counts).  Nothing is hard-coded from the practice cases; the
program re-fits on whatever comparables the case it is given holds, so the
same code forecasts the final case with its 42 comparables.

  A. Shape regression.  For each hour h = 2..24, regress log viewers on log
     hour-1 viewers across the comparables,
         ln v_h = a_h + b_h * ln v_1 + eps_h,
     and take the fitted median with a normal spread from the residual
     standard deviation (scaled by 0.85).  The slope b_h is near 1 in the
     small hours and near 0 in the evening: the evening audience is nearly
     independent of how the day opened, while the size of the day's ramp is
     what hour 1 predicts (the ramp's log correlates -0.94 with log hour 1).

  B. Nearest-analog quantiles.  Take the 8 comparables whose hour-1 print is
     closest in log space to this case's, and use the empirical quantiles of
     their log viewers at each hour.

  The forecast is the geometric mean (log-space average, equal weights) of
  the two components' quantiles.  Averaging a global regression with a local
  analog hedges the two failure modes: the regression is smooth but can be
  dragged by extreme launches, the analog is faithful but noisy.

If either component cannot be fit for an hour (too few comparables), the other
carries it; if neither can, the hour falls back to persistence (the house
baseline's shape: every hour at hour 1's count, spread 0.15 * sqrt(h - 1)).
Everything is deterministic: no randomness, no clock, no network, standard
library only.  One case runs in well under a second.
"""

import json
import math
import os
import sys
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The quantile levels the contest scores, in the order each row lists them.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# The standard normal's quantile at each level.
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]

# How many nearest comparables the analog component uses.
K_NEIGHBORS = 8
# The regression component's spread is the residual standard deviation times
# this: the practice data prefers a slightly narrower spread than the raw fit.
SPREAD_SCALE = 0.85
# The ensemble weight of the regression component (the analog gets 1 - this).
WEIGHT_REGRESSION = 0.5
# The fallback (persistence) spread, in log units per square-root hour.
BASE_SIGMA = 0.15
# A comparable counts as an analog when its release day holds hour 1 and at
# least this many of hours 2..24.
MIN_COMPARABLE_HOURS = 12
# A regression for one hour needs at least this many comparables with it.
MIN_REGRESSION_POINTS = 5


def round2(x):
    """x rounded to 2 decimals, half up, as JavaScript rounds it.

    A whole number comes back as an int, so json.dumps writes 1240 where
    JavaScript writes 1240 (not 1240.0).
    """
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def release_day_viewers(comp):
    """A comparable's release-day hourly viewers, {hour: viewers}.

    The comparable's `hourly` points run from 14 days before its release day
    to 3 days after it; the release day is the UTC day of `day_start`, hour h
    the point whose `from` is that day at h - 1 o'clock.  A point SullyGnome
    did not sample is simply absent; a zero or missing count is skipped.
    """
    day = comp.get("day_start", "")[:10]
    if not day:
        return {}
    out = {}
    for point in comp.get("hourly") or []:
        start = point.get("from", "")
        if start[:10] != day:
            continue
        viewers = point.get("viewers")
        if viewers is None:
            continue
        hour = int(start[11:13]) + 1
        if 1 <= hour <= 24 and viewers >= 1:
            out[hour] = viewers
    return out


def usable_comparables(case):
    """The case's comparables that can serve as analogs: (hour-1, day dict)."""
    pool = []
    for comp in case.get("comparables") or []:
        day = release_day_viewers(comp)
        if 1 not in day:
            continue
        present = sum(1 for h in range(2, 25) if h in day)
        if present < MIN_COMPARABLE_HOURS:
            continue
        pool.append((day[1], day))
    return pool


def ols(points):
    """Least squares y ~ a + b*x over (x, y) points; (a, b, resid_sd) or None."""
    n = len(points)
    if n < MIN_REGRESSION_POINTS:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    b = sxy / sxx
    a = my - b * mx
    resid = [p[1] - (a + b * p[0]) for p in points]
    sd = math.sqrt(sum(r * r for r in resid) / max(1, n - 2))
    return a, b, sd


def regression_quantiles(log_v1, pool):
    """Component A: per-hour log-linear fit on hour 1, {hour: [5 quantiles]}."""
    out = {}
    for h in range(2, 25):
        points = [
            (math.log(v1), math.log(day[h]))
            for v1, day in pool
            if h in day
        ]
        fit = ols(points)
        if fit is None:
            continue
        a, b, sd = fit
        median = a + b * log_v1
        spread = sd * SPREAD_SCALE
        out[h] = [math.exp(median + z * spread) for z in Z]
    return out


def empirical_quantile(sorted_values, tau):
    """A tau-quantile of sorted log values, linearly interpolated."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = tau * (len(sorted_values) - 1)
    low = int(math.floor(pos))
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def analog_quantiles(log_v1, pool):
    """Component B: empirical quantiles of the K nearest comparables."""
    ranked = sorted(
        pool, key=lambda item: (abs(math.log(item[0]) - log_v1), item[1].get(1, 0))
    )
    neighbors = ranked[:K_NEIGHBORS]
    out = {}
    for h in range(2, 25):
        values = sorted(math.log(day[h]) for _, day in neighbors if h in day)
        if not values:
            continue
        out[h] = [math.exp(empirical_quantile(values, tau)) for tau in LEVELS]
    return out


def persistence_quantiles(p1, h):
    """The fallback: every hour at hour 1's count, widening spread."""
    return [p1 * math.exp(z * BASE_SIGMA * math.sqrt(h - 1)) for z in Z]


def clean_quantiles(values):
    """Make five quantiles valid: finite, above 0, at most 1e9, non-decreasing."""
    cleaned = []
    previous = 0.0
    for value in values:
        if not math.isfinite(value):
            value = previous if previous > 0 else 1.0
        value = min(max(value, 0.01), 1e9)
        value = max(value, previous)
        cleaned.append(round2(value))
        previous = cleaned[-1]
    return cleaned


def forecast_case(case):
    """The forecast for one case: {"hours": [{"hour": h, "q": [...]}]}."""
    known = next((k for k in case.get("known") or [] if k.get("hour") == 1), None)
    p1 = max(1, known["viewers"]) if known else 1
    log_v1 = math.log(p1)
    pool = usable_comparables(case)
    reg = regression_quantiles(log_v1, pool)
    ana = analog_quantiles(log_v1, pool)
    hours = []
    for row in case.get("forecast") or []:
        h = row["hour"]
        in_reg = h in reg
        in_ana = h in ana
        if in_reg and in_ana:
            quantiles = [
                math.exp(
                    WEIGHT_REGRESSION * math.log(reg[h][i])
                    + (1 - WEIGHT_REGRESSION) * math.log(ana[h][i])
                )
                for i in range(5)
            ]
        elif in_reg:
            quantiles = reg[h]
        elif in_ana:
            quantiles = ana[h]
        else:
            quantiles = persistence_quantiles(p1, h)
        hours.append({"hour": h, "q": clean_quantiles(quantiles)})
    hours.sort(key=lambda row: row["hour"])
    return {"hours": hours}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
