"""Metric information registry for the dashboard side panel.

Each entry maps a `metric_id` (as referenced by `data-info="..."` on a card)
to:

- `title`: the panel header.
- `what_html`: static markup describing what the metric is, how it's
  computed, and the scale.
- `sources`: list of `{title, url}` to scientific articles or standards.
- `build_insight(ctx)`: callable that receives the per-render context and
  returns a short, data-driven recommendation.

Insights are produced server-side so they can reference the same numbers
the cards are rendering. The rendered payload is serialized into a single
`<script type="application/json">` block on the page; the JS panel reads
it on click.

Whenever you add a new card to `dashboard.py`, give it a `data-info`
attribute and add a corresponding entry here.
"""
from __future__ import annotations

from typing import Any, Callable

# ──────────────────────────────────────────────────────────────────────────────
# Helpers used by build_insight callables
# ──────────────────────────────────────────────────────────────────────────────


def _fmt_pct(v: float | None, digits: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%"


def _fmt(v: float | int | None, digits: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def _wrap_p(*parts: str) -> str:
    return "".join(f"<p>{p}</p>" for p in parts if p)


def _none_msg(metric: str) -> str:
    return _wrap_p(
        f"Not enough data yet to compute {metric}. Keep wearing your watch and "
        f"logging meals — most checks need ~7 days of history."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Insight builders — one per metric
# ──────────────────────────────────────────────────────────────────────────────


def _readiness_insight(ctx: dict) -> str:
    r = ctx.get("readiness") or {}
    score = r.get("score")
    if score is None:
        return _none_msg("a readiness score")
    band = r.get("band")
    comp = r.get("components") or {}
    advice = {
        "high":   "Body's primed. Push intensity today — high-quality intervals or a long effort.",
        "medium": "Solid recovery state. Train as planned, but listen for signs of fatigue.",
        "low":    "Recovery is suppressed. Prioritize sleep, light movement, and extra calories.",
    }.get(band or "", "Track another day or two so the baseline stabilises.")
    parts = [
        f"<strong>Score: {score} / 100</strong> ({band or '—'}). {advice}",
        f"Component deltas: HRV {_fmt_pct(comp.get('hrv'))} · "
        f"RHR {_fmt_pct(comp.get('rhr'))} · "
        f"Sleep {_fmt_pct(comp.get('sleep'))} · "
        f"Body Battery {_fmt_pct(comp.get('bb'))}.",
    ]
    return _wrap_p(*parts)


def _rhr_insight(ctx: dict) -> str:
    summary = ctx.get("summary") or {}
    rhr = summary.get("resting_hr")
    if rhr is None:
        return _none_msg("a resting HR")
    if rhr < 50:
        bucket = "athletic range — typical of trained endurance individuals."
    elif rhr < 60:
        bucket = "above-average fitness range."
    elif rhr < 70:
        bucket = "average for healthy adults."
    elif rhr < 80:
        bucket = "on the higher side of healthy."
    else:
        bucket = "elevated. Persistent values >80 warrant a check-in."
    return _wrap_p(
        f"Today's resting HR is <strong>{rhr} bpm</strong> — {bucket}",
        "Watch the trend, not single days. A 5-day rising streak triggers a Telegram alert "
        "even if no value crosses the absolute threshold.",
    )


def _hrv_insight(ctx: dict) -> str:
    summary = ctx.get("summary") or {}
    hrv = summary.get("hrv")
    state = summary.get("hrv_state") or "—"
    if hrv is None:
        return _none_msg("an HRV reading")
    state_msg = {
        "HIGH":     "Above your 7-day baseline — well-recovered, ready for intensity.",
        "BALANCED": "Within ±5% of your baseline — typical recovery state.",
        "LOW":      "Below baseline — autonomic nervous system is loaded. Reduce intensity.",
    }.get(state, "Building a baseline; values will stabilise after ~7 days.")
    return _wrap_p(
        f"Last night's overnight HRV (RMSSD) was <strong>{hrv} ms</strong> · {state_msg}",
        "HRV is highly individual. Compare yourself to your own trend, not population norms.",
    )


def _steps_insight(ctx: dict) -> str:
    summary = ctx.get("summary") or {}
    steps = summary.get("steps") or 0
    if steps == 0:
        return _wrap_p("No steps recorded yet today.")
    if steps < 5000:
        bucket = "sedentary range. Try to add a 10-minute walk or two before evening."
    elif steps < 7500:
        bucket = "moderately active. Most adults benefit from pushing toward 7,500."
    elif steps < 10000:
        bucket = "active range — meaningful daily-mortality benefits start showing here."
    else:
        bucket = "highly active day. Past ~10,000 the marginal benefits taper."
    return _wrap_p(
        f"{steps:,} steps today — {bucket}",
        "The 10,000-step number is a marketing artefact, not a clinical threshold. "
        "Saint-Maurice et al. (2020) found mortality benefits plateauing around 7,500.",
    )


def _sleep_insight(ctx: dict) -> str:
    summary = ctx.get("summary") or {}
    secs = summary.get("sleep_seconds")
    target_h = (ctx.get("cfg") and ctx["cfg"].sleep_target_hours) or 8
    if not secs:
        return _none_msg("a sleep total")
    h = secs / 3600
    delta = h - target_h
    if h >= target_h:
        msg = f"You hit your {target_h}h target with {abs(delta):.1f}h to spare."
    elif h >= target_h - 1:
        msg = f"Within an hour of the {target_h}h target — acceptable."
    else:
        msg = f"{abs(delta):.1f}h short of your {target_h}h target. Sleep debt accumulates fast."
    return _wrap_p(
        f"Last night: <strong>{h:.1f}h</strong>. {msg}",
        "NSF (Hirshkowitz 2015) recommends 7–9h for adults 18–64. Chronic sleep below 6h "
        "is associated with cognitive deficits and elevated mortality risk.",
    )


def _bb_insight(ctx: dict) -> str:
    summary = ctx.get("summary") or {}
    bb = summary.get("body_battery")
    if bb is None:
        return _none_msg("a body-battery value")
    if bb >= 75:
        msg = "high — you've got energy reserves to spend on hard work."
    elif bb >= 50:
        msg = "moderate — balanced state."
    elif bb >= 25:
        msg = "drained — start protecting recovery."
    else:
        msg = "very low — prioritize rest and sleep tonight."
    return _wrap_p(
        f"Current body battery: <strong>{bb} / 100</strong> — {msg}",
        "Body Battery is Garmin's proprietary blend of HRV, stress, sleep, and activity. "
        "Useful as a holistic check, but the raw HRV/RHR signals are more reliable for training decisions.",
    )


def _stress_insight(ctx: dict) -> str:
    summary = ctx.get("summary") or {}
    stress = summary.get("stress_avg")
    if stress is None:
        return _none_msg("a stress reading")
    if stress < 25:
        msg = "rest level — autonomic state is calm."
    elif stress < 50:
        msg = "low-medium — typical workday range."
    elif stress < 75:
        msg = "medium-high — sustained activation, watch recovery."
    else:
        msg = "high — chronic at this level affects sleep and HRV."
    return _wrap_p(
        f"Today's average stress: <strong>{stress} / 100</strong> — {msg}",
        "Garmin estimates stress from short-term HRV. Single high readings are normal; "
        "the trend matters.",
    )


def _acwr_insight(ctx: dict) -> str:
    a = ctx.get("acwr") or {}
    ratio = a.get("ratio")
    if ratio is None:
        return _none_msg("an ACWR")
    band = a.get("band") or "—"
    advice = {
        "undertrained": "You're well below your chronic load. A planned ramp-up is fine; "
                        "ratios <0.8 are also linked to detraining.",
        "optimal":     "You're in the protective sweet-spot (Gabbett 2016). "
                       "Keep load increases gradual.",
        "caution":     "Acute load is starting to outrun your fitness base. "
                       "Pull back intensity for a few days.",
        "high_risk":   "Acute load is sharply higher than your 28-day average — "
                       "Gabbett's data shows injury risk roughly doubling above 1.5.",
    }.get(band, "Need a longer window for a meaningful ratio.")
    return _wrap_p(
        f"<strong>ACWR = {ratio:.2f}</strong> ({band}). {advice}",
        f"Acute (7d): {a.get('acute_avg', 0):.0f} kcal · "
        f"Chronic (28d): {a.get('chronic_avg', 0):.0f} kcal.",
    )


def _monotony_insight(ctx: dict) -> str:
    m = ctx.get("monotony") or {}
    val = m.get("monotony")
    band = m.get("band") or "—"
    if band == "rest":
        return _wrap_p(
            "No training load this week. A planned rest week is healthy — "
            "structured recovery underpins adaptation.",
        )
    if val is None:
        return _none_msg("a monotony score")
    advice = {
        "varied":     "Healthy load distribution — high and low days alternating.",
        "elevated":   "Load is starting to flatten. Add at least one easy/rest day.",
        "monotonous": "Foster (1998) flagged values >2.0 as predictive of overuse injury "
                      "and immune suppression. Insert a clear rest day.",
    }.get(band, "Rolling 7-day window — interpret with that in mind.")
    return _wrap_p(
        f"<strong>Monotony = {val:.2f}</strong> ({band}). {advice}",
        "Foster index = mean(daily load) ÷ std(daily load). High mean ÷ low std = same "
        "thing every day, which masks fatigue accumulation.",
    )


def _z2_insight(ctx: dict) -> str:
    z = ctx.get("z2") or {}
    minutes = z.get("minutes", 0)
    goal = z.get("goal_minutes", 150)
    pct = (minutes / goal * 100) if goal else 0
    if minutes >= goal:
        msg = f"You've cleared the {goal}-minute weekly goal. This is the "\
              f"foundation of aerobic adaptation — keep it up."
    elif minutes >= goal * 0.5:
        msg = f"At {minutes} min ({pct:.0f}% of {goal}). Slot in another easy session "\
              f"or extend an existing one to close the gap."
    else:
        msg = f"Only {minutes} min so far this week. Most endurance practitioners aim for "\
              f"≥80% of total volume in Z2 (Seiler's polarized model)."
    return _wrap_p(
        msg,
        f"Zone 2 here = activities with avg HR in {z.get('lower_bpm','—')}–{z.get('upper_bpm','—')} bpm "
        f"(60–70% of HRmax {z.get('hrmax','—')}). Set USER_HRMAX in .env if 220−age is wrong for you.",
    )


def _sleep_debt_insight(ctx: dict) -> str:
    sd = ctx.get("sleep_debt") or {}
    total = sd.get("total_h", 0)
    target = sd.get("target_h", 8)
    if total > 5:
        msg = f"You're {total:.1f}h in the red over the last 7 days vs {target}h/night target. "\
              f"That's a meaningful deficit — performance and mood will degrade."
    elif total > 0:
        msg = f"Mild deficit ({total:.1f}h). Catch up with one or two longer nights."
    elif total < 0:
        msg = f"You've slept {abs(total):.1f}h above target this week — surplus, "\
              f"which usually means catching up after a deficit."
    else:
        msg = "On target — sleep balance is right where it should be."
    return _wrap_p(
        msg,
        "Cumulative deficit is computed as Σ(target − actual) over the last 7 days. "
        "Walker (2017) — sleep debt cannot be 'paid off' on weekends in full; chronic "
        "shortfall is the main predictor of next-day performance.",
    )


def _calorie_balance_insight(ctx: dict) -> str:
    bal = ctx.get("balance") or {}
    eaten = bal.get("eaten_kcal") or 0
    bal_kcal = bal.get("balance_kcal") or 0
    meal_count = bal.get("meal_count", 0)
    if meal_count == 0:
        return _wrap_p(
            "No meals logged today. Use <code>log-meal</code> or <code>log-barcode</code> "
            "from the CLI to start tracking."
        )
    if bal_kcal >= 500:
        msg = f"Surplus of {bal_kcal:+.0f} kcal — supports muscle gain or hard training. "\
              f"Sustained surpluses above ~500/day add ~0.5kg/week of mass."
    elif bal_kcal >= 0:
        msg = f"Small surplus of {bal_kcal:+.0f} kcal — maintenance range."
    elif bal_kcal >= -500:
        msg = f"Mild deficit of {bal_kcal:+.0f} kcal — sustainable for cutting, "\
              f"~0.5kg/week of fat loss if held."
    else:
        msg = f"Aggressive deficit of {bal_kcal:+.0f} kcal — risks lean-mass loss "\
              f"and recovery debt if held more than a week."
    return _wrap_p(
        f"Eaten today: {eaten:.0f} kcal across {meal_count} meal(s). {msg}",
        "Burned = BMR (Mifflin-St Jeor) + step calories (~0.048 kcal/step scaled to weight) "
        "+ activity calories. Set USER_HEIGHT_CM / WEIGHT_KG / AGE / SEX for accuracy.",
    )


def _macros_insight(ctx: dict) -> str:
    bal = ctx.get("balance") or {}
    p_target = ctx.get("protein_target_g") or 0
    f_target = ctx.get("fiber_target_g") or 0
    p = bal.get("protein_g") or 0
    f = sum((m.get("fiber_g") or 0) for m in (ctx.get("meals") or []))
    parts = []
    if p_target > 0:
        p_pct = p / p_target * 100
        if p < p_target * 0.7:
            parts.append(
                f"Protein at <strong>{p:.0f}g</strong> ({p_pct:.0f}% of {p_target:.0f}g target). "
                f"Below 1.2 g/kg compromises recovery, especially around training days."
            )
        else:
            parts.append(
                f"Protein at <strong>{p:.0f}g</strong> ({p_pct:.0f}% of {p_target:.0f}g target) — on track."
            )
    if f_target > 0:
        if f < f_target * 0.5:
            parts.append(
                f"Fiber low at {f:.0f}g vs {f_target:.0f}g target. Vegetables, fruit, "
                f"whole grains, legumes — easy lever."
            )
        elif f < f_target * 0.9:
            parts.append(f"Fiber tracking at {f:.0f}g — close to your {f_target:.0f}g target.")
        else:
            parts.append(f"Fiber on point at {f:.0f}g.")
    if not parts:
        parts.append("Log meals to compute protein and fiber adequacy.")
    parts.append(
        "ISSN guidance: 1.4–2.0 g/kg/day protein for active individuals (Jäger 2017). "
        "IOM: ~14g fiber per 1000 kcal."
    )
    return _wrap_p(*parts)


def _ea_insight(ctx: dict) -> str:
    ea = ctx.get("energy_availability") or {}
    status = ea.get("status")
    if status in (None, "unknown"):
        return _wrap_p(
            "Need at least one logged meal today to compute Energy Availability."
        )
    val = ea.get("ea_kcal_per_kg")
    advice = {
        "optimal": f"<strong>{val:.1f} kcal/kg LBM</strong> — supportive of adaptation, "
                   f"endocrine function, and bone health.",
        "low":     f"<strong>{val:.1f} kcal/kg LBM</strong> — between 30 and 45. "
                   f"Adaptation may suffer if held; investigate if persistent.",
        "red_s":   f"<strong>{val:.1f} kcal/kg LBM</strong> — below the IOC 30 kcal/kg "
                   f"threshold. RED-S risk: hormonal disruption, immune suppression, bone "
                   f"density loss. Eat more or train less.",
    }.get(status, "")
    return _wrap_p(
        advice,
        f"Computed as (eaten kcal − activity kcal) ÷ lean body mass "
        f"(estimated as weight × 0.85 ≈ {ea.get('lbm_kg', 0):.1f} kg).",
    )


def _meal_timing_insight(ctx: dict) -> str:
    timing = ctx.get("meal_timing")
    if not timing:
        return _wrap_p("Log a meal to start tracking your eating window.")
    window = timing.get("eating_window_h", 0)
    last_h = timing.get("hours_since_last_meal", 0)
    if window <= 8:
        window_msg = f"Eating window {window:.1f}h — qualifies as Time-Restricted Eating (TRE)."
    elif window <= 12:
        window_msg = f"Eating window {window:.1f}h — moderate, common circadian-friendly pattern."
    else:
        window_msg = f"Eating window {window:.1f}h. Most TRE protocols aim for ≤12h."
    return _wrap_p(
        f"First meal {timing['first_meal_local']}, last meal {timing['last_meal_local']}. "
        f"{window_msg} Currently <strong>{last_h:.1f}h</strong> since last eat.",
        "Patterson et al. (2015) found that confining eating to the active circadian phase "
        "(≤12h window) improved metabolic markers independent of total calories.",
    )


def _balance_history_insight(ctx: dict) -> str:
    history = ctx.get("history_7d") or []
    if not history:
        return _wrap_p("Log meals across multiple days to populate this trend.")
    vals = [h["balance_kcal"] for h in history]
    avg = sum(vals) / len(vals)
    if avg > 200:
        trend = f"7-day average is {avg:+.0f} kcal/day — sustained surplus."
    elif avg > -200:
        trend = f"7-day average is {avg:+.0f} kcal/day — maintenance range."
    else:
        trend = f"7-day average is {avg:+.0f} kcal/day — sustained deficit."
    return _wrap_p(
        trend,
        "The dashed line is a 7-day moving average. Single-day spikes are noise; "
        "the line is the signal.",
    )


def _heatmap_insight(ctx: dict) -> str:
    history = ctx.get("heatmap") or []
    scored = [h for h in history if h.get("score") is not None]
    if not scored:
        return _wrap_p("Need ≥7 days of summary data before readiness scores are computed.")
    high = sum(1 for h in scored if (h.get("band") == "high"))
    medium = sum(1 for h in scored if (h.get("band") == "medium"))
    low = sum(1 for h in scored if (h.get("band") == "low"))
    return _wrap_p(
        f"Last {len(scored)} scored days: <strong>{high}</strong> high · "
        f"<strong>{medium}</strong> medium · <strong>{low}</strong> low.",
        "Brighter cells = higher readiness. Streaks of low (magenta) cells often "
        "correspond to illness, jet lag, or accumulated training stress.",
    )


def _hrv_trend_insight(ctx: dict) -> str:
    return _wrap_p(
        "Day-by-day overnight HRV plus a 7-day moving average. The average is the "
        "primary signal — single-night dips are normal and typically mean nothing.",
        "Watch for the moving average drifting down across weeks (training stress, "
        "illness, sleep debt) or sudden ≥20% drops vs baseline (the smart-alert threshold).",
    )


def _rhr_trend_insight(ctx: dict) -> str:
    return _wrap_p(
        "Resting HR over the last 30 days. Trends matter more than absolutes.",
        "A 5-day strictly-rising streak fires a Telegram alert. The 7-day moving "
        "average is the cleaner signal for medium-term changes.",
    )


def _sleep_stages_insight(ctx: dict) -> str:
    return _wrap_p(
        "Garmin estimates four stages from HR, HRV, and accelerometer movement. "
        "Healthy adults typically see 13–23% deep, 50–60% light, 20–25% REM, and ≤10% awake.",
        "Stage estimates from wrist devices are noisy compared to PSG (lab) — use the "
        "totals as rough guidance, not diagnostic.",
    )


def _sleep_duration_insight(ctx: dict) -> str:
    return _wrap_p(
        "Total sleep per night for the last 14 days. Look for the average rather "
        "than individual nights.",
        "Most adults need 7–9h. Sub-6h sustained nights are linked to elevated "
        "cortisol, insulin resistance, and accidents.",
    )


def _activities_insight(ctx: dict) -> str:
    activities = ctx.get("activities") or []
    if not activities:
        return _wrap_p("No activities synced in the last 14 days.")
    types = {}
    for a in activities:
        types[a.get("activity_type") or "other"] = types.get(a.get("activity_type") or "other", 0) + 1
    breakdown = ", ".join(f"{k.replace('_', ' ')} × {v}" for k, v in sorted(types.items(), key=lambda x: -x[1])[:5])
    return _wrap_p(
        f"{len(activities)} activities in the last 14 days — {breakdown}.",
        "TE column = aerobic Training Effect (Garmin). 1.0–2.0 maintaining · "
        "2.0–3.0 improving · 3.0–4.0 highly improving · 4.0–5.0 overreaching.",
    )


def _training_load_insight(ctx: dict) -> str:
    return _wrap_p(
        "Daily activity calories over 30 days. The bars feed the ACWR and "
        "Training Monotony calculations.",
        "Sustained zero-bar runs build chronic-load deficit; sustained equal-height "
        "runs flag monotony — both increase injury risk.",
    )


def _alerts_insight(ctx: dict) -> str:
    return _wrap_p(
        "Recent Telegram alerts. Each alert kind has a separate cooldown stored in "
        "SQLite, so cooldowns survive restarts.",
        "Kinds: <code>resting_hr_high</code> (single-day threshold), "
        "<code>resting_hr_trend</code> (5-day climb), <code>hrv_drop</code> "
        "(≥20% below 7-day baseline), <code>illness_risk</code> (RHR↑ + HRV↓ "
        "for 2+ days), <code>training_ready</code>, <code>recovery_day</code>.",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Source links — most metrics share a few canonical references
# ──────────────────────────────────────────────────────────────────────────────

_SRC_PLEWS_HRV = {
    "title": "Plews et al. (2013) — Training adaptation and HRV in elite endurance athletes",
    "url": "https://link.springer.com/article/10.1007/s40279-013-0071-8",
}
_SRC_BUCHHEIT = {
    "title": "Buchheit (2014) — Monitoring training status with HR measures",
    "url": "https://www.frontiersin.org/articles/10.3389/fphys.2014.00073/full",
}
_SRC_TASK_FORCE_HRV = {
    "title": "Task Force ESC/NASPE (1996) — Heart rate variability standards",
    "url": "https://www.ahajournals.org/doi/10.1161/01.CIR.93.5.1043",
}
_SRC_GABBETT = {
    "title": "Gabbett (2016) — The training-injury prevention paradox: ACWR",
    "url": "https://bjsm.bmj.com/content/50/5/273",
}
_SRC_HULIN = {
    "title": "Hulin et al. (2014) — ACWR and injury in cricket fast bowlers",
    "url": "https://bjsm.bmj.com/content/48/8/708",
}
_SRC_FOSTER = {
    "title": "Foster (1998) — Monitoring training in athletes with reference to overtraining syndrome",
    "url": "https://journals.lww.com/acsm-msse/Fulltext/1998/07000/Monitoring_training_in_athletes_with_reference_to.23.aspx",
}
_SRC_SEILER = {
    "title": "Seiler (2010) — What is best practice for training intensity distribution?",
    "url": "https://journals.humankinetics.com/view/journals/ijspp/5/3/article-p276.xml",
}
_SRC_NSF_SLEEP = {
    "title": "Hirshkowitz et al. (2015) — National Sleep Foundation duration recommendations",
    "url": "https://www.sleephealthjournal.org/article/S2352-7218(15)00015-7/fulltext",
}
_SRC_WALKER = {
    "title": "Walker (2017) — Why We Sleep (book / lectures)",
    "url": "https://www.sleepdiplomat.com/",
}
_SRC_MIFFLIN = {
    "title": "Mifflin–St Jeor (1990) — Predictive equation for resting energy expenditure",
    "url": "https://academic.oup.com/ajcn/article-abstract/51/2/241/4695104",
}
_SRC_ISSN_PROTEIN = {
    "title": "Jäger et al. (2017) — ISSN position stand: protein and exercise",
    "url": "https://jissn.biomedcentral.com/articles/10.1186/s12970-017-0177-8",
}
_SRC_IOM_FIBER = {
    "title": "IOM (2005) — Dietary Reference Intakes: fiber 14g per 1000 kcal",
    "url": "https://www.ncbi.nlm.nih.gov/books/NBK56068/",
}
_SRC_LOUCKS = {
    "title": "Loucks (2007) — Energy availability, not body fatness, regulates reproductive function",
    "url": "https://www.tandfonline.com/doi/abs/10.1080/02640410601038551",
}
_SRC_REDS_IOC = {
    "title": "Mountjoy et al. (2018) — IOC consensus statement on Relative Energy Deficiency in Sport (RED-S)",
    "url": "https://bjsm.bmj.com/content/52/11/687",
}
_SRC_PATTERSON_TRE = {
    "title": "Patterson & Sears (2017) — Metabolic effects of intermittent fasting",
    "url": "https://www.annualreviews.org/doi/10.1146/annurev-nutr-071816-064634",
}
_SRC_SAINT_MAURICE_STEPS = {
    "title": "Saint-Maurice et al. (2020) — Steps per day and all-cause mortality",
    "url": "https://jamanetwork.com/journals/jama/fullarticle/2763292",
}
_SRC_KIVINIEMI = {
    "title": "Kiviniemi (2007) — Endurance training guided by daily HRV",
    "url": "https://link.springer.com/article/10.1007/s00421-007-0497-5",
}


# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────


INFO: dict[str, dict[str, Any]] = {
    "readiness": {
        "title": "Composite Readiness Score",
        "what_html": (
            "<p>A single 0–100 score blending four signals against your own 7-day baseline:</p>"
            "<ul class='list-disc ml-5'>"
            "<li>HRV vs 7d baseline (50% weight) — autonomic recovery</li>"
            "<li>RHR vs 7d baseline (20%) — inverted; lower is better</li>"
            "<li>Sleep vs target hours (20%)</li>"
            "<li>Body Battery deviation from 50 (10%)</li>"
            "</ul>"
            "<p>Each component is clamped to ±100% deviation, weighted, and mapped to 0–100. "
            "Bands: 80+ high · 60–80 medium · &lt;60 low.</p>"
        ),
        "build_insight": _readiness_insight,
        "sources": [_SRC_PLEWS_HRV, _SRC_BUCHHEIT, _SRC_TASK_FORCE_HRV],
    },
    "rhr": {
        "title": "Resting Heart Rate",
        "what_html": (
            "<p>Lowest HR sample of the day Garmin marks as 'resting' — typically captured "
            "during overnight sleep.</p>"
            "<p>Population norms: trained athletes 40–60 bpm · average adults 60–80 bpm. "
            "Persistent &gt;80 bpm warrants follow-up.</p>"
        ),
        "build_insight": _rhr_insight,
        "sources": [_SRC_BUCHHEIT, _SRC_TASK_FORCE_HRV],
    },
    "hrv": {
        "title": "Overnight HRV (RMSSD)",
        "what_html": (
            "<p>Garmin's overnight HRV is approximately RMSSD — root-mean-square of successive "
            "differences in R-R intervals, captured during sleep. It indexes parasympathetic "
            "(recovery) tone.</p>"
            "<p>Higher = more recovered. Compare to your own baseline, not population norms — "
            "absolute HRV varies enormously between individuals.</p>"
        ),
        "build_insight": _hrv_insight,
        "sources": [_SRC_PLEWS_HRV, _SRC_TASK_FORCE_HRV, _SRC_KIVINIEMI],
    },
    "steps": {
        "title": "Daily Step Count",
        "what_html": (
            "<p>Garmin's accelerometer-derived step count for the day.</p>"
            "<p>The 10,000 number originated from a 1960s Japanese pedometer marketing campaign, "
            "not clinical research. Modern data (Saint-Maurice 2020) finds mortality benefits "
            "plateau around 7,500 daily steps.</p>"
        ),
        "build_insight": _steps_insight,
        "sources": [_SRC_SAINT_MAURICE_STEPS],
    },
    "sleep": {
        "title": "Sleep Duration",
        "what_html": (
            "<p>Total time asleep last night (light + deep + REM, excluding awake periods).</p>"
            "<p>National Sleep Foundation recommends 7–9 hours for adults aged 18–64. "
            "Set <code>SLEEP_TARGET_HOURS</code> in <code>.env</code> to customise the target.</p>"
        ),
        "build_insight": _sleep_insight,
        "sources": [_SRC_NSF_SLEEP, _SRC_WALKER],
    },
    "body_battery": {
        "title": "Body Battery",
        "what_html": (
            "<p>Garmin's proprietary 0–100 'energy reserve' score combining HRV, stress, "
            "sleep, and activity. Drops while you spend energy, charges while you rest.</p>"
            "<p>Useful as a holistic at-a-glance metric, but for training decisions the raw "
            "HRV and RHR signals are more reliable.</p>"
        ),
        "build_insight": _bb_insight,
        "sources": [_SRC_BUCHHEIT],
    },
    "stress": {
        "title": "Stress Score",
        "what_html": (
            "<p>Garmin's 0–100 stress estimate based on short-term HRV. 0–25 is rest, "
            "25–50 low, 50–75 medium, 75–100 high.</p>"
            "<p>Single high readings during workouts or caffeine spikes are normal; the "
            "daily and weekly averages are the meaningful signal.</p>"
        ),
        "build_insight": _stress_insight,
        "sources": [_SRC_TASK_FORCE_HRV],
    },
    "acwr": {
        "title": "Acute:Chronic Workload Ratio (ACWR)",
        "what_html": (
            "<p>Mean(7-day load) ÷ Mean(28-day load), where 'load' is daily activity calories.</p>"
            "<p>Bands: &lt;0.8 undertrained · 0.8–1.3 optimal · 1.3–1.5 caution · &gt;1.5 high "
            "injury risk. Gabbett's data: athletes operating &gt;1.5 had ~2× the injury rate "
            "of those in the sweet-spot.</p>"
            "<p>Garmin doesn't expose this number raw — most athletes only see the proxy "
            "<em>Training Status</em>.</p>"
        ),
        "build_insight": _acwr_insight,
        "sources": [_SRC_GABBETT, _SRC_HULIN],
    },
    "monotony": {
        "title": "Training Monotony (Foster Index)",
        "what_html": (
            "<p>Mean(daily load) ÷ Standard deviation(daily load) over a rolling 7-day window.</p>"
            "<p>High monotony = same load every day = no recovery contrast. Foster (1998) "
            "associated values &gt;2.0 with overuse injury, immune suppression, and overreaching "
            "in athletes.</p>"
            "<p>The cure is one or two genuinely-easy days per week.</p>"
        ),
        "build_insight": _monotony_insight,
        "sources": [_SRC_FOSTER],
    },
    "z2": {
        "title": "Zone 2 Aerobic Minutes",
        "what_html": (
            "<p>Weekly minutes spent in Zone 2 — defined here as activities with avg HR in "
            "60–70% of HRmax. HRmax defaults to 220 − age unless <code>USER_HRMAX</code> is set.</p>"
            "<p>Z2 builds mitochondrial density, fat oxidation, and the aerobic base that "
            "underpins all higher-intensity training. Polarized training research (Seiler) "
            "argues most volume should sit at this intensity.</p>"
            "<p>Caveat: this dashboard computes Z2 from <em>average</em> HR per activity, "
            "not per-second. Steady-state runs/rides are accurate; intervals less so.</p>"
        ),
        "build_insight": _z2_insight,
        "sources": [_SRC_SEILER],
    },
    "sleep_debt": {
        "title": "7-Day Sleep Debt",
        "what_html": (
            "<p>Σ(target − actual) sleep hours over the last 7 days. Positive = net deficit. "
            "Target from <code>SLEEP_TARGET_HOURS</code> (default 8h).</p>"
            "<p>Walker (2017) — sleep debt cannot be 'paid off' on weekends; chronic shortfall "
            "is the primary predictor of next-day cognitive and physical performance.</p>"
        ),
        "build_insight": _sleep_debt_insight,
        "sources": [_SRC_NSF_SLEEP, _SRC_WALKER],
    },
    "calorie_balance": {
        "title": "Calorie Balance",
        "what_html": (
            "<p>Eaten kcal − Total burned kcal, where burned = BMR (Mifflin-St Jeor) "
            "+ step calories (~0.048 kcal/step scaled to your weight) + activity calories.</p>"
            "<p>For accuracy, set <code>USER_AGE</code>, <code>USER_HEIGHT_CM</code>, "
            "<code>USER_WEIGHT_KG</code>, <code>USER_SEX</code> in <code>.env</code>. "
            "Sustained ±500 kcal/day ≈ ±0.5 kg/week mass change.</p>"
        ),
        "build_insight": _calorie_balance_insight,
        "sources": [_SRC_MIFFLIN],
    },
    "macros": {
        "title": "Macronutrient Targets",
        "what_html": (
            "<p>Three rings: protein (P), carbs (C), fat (F).</p>"
            "<ul class='list-disc ml-5'>"
            "<li>Protein target: <code>weight_kg × PROTEIN_TARGET_G_PER_KG</code> "
            "(default 1.6, raise to 2.0 for hard training)</li>"
            "<li>Carbs target: 50% of <code>KCAL_TARGET</code> ÷ 4 kcal/g</li>"
            "<li>Fat target: 25% of <code>KCAL_TARGET</code> ÷ 9 kcal/g</li>"
            "<li>Fiber target: 14g per 1000 kcal (IOM)</li>"
            "<li>Sodium ceiling: 2300 mg/day (WHO upper limit)</li>"
            "</ul>"
        ),
        "build_insight": _macros_insight,
        "sources": [_SRC_ISSN_PROTEIN, _SRC_IOM_FIBER],
    },
    "energy_availability": {
        "title": "Energy Availability (RED-S)",
        "what_html": (
            "<p>(Eaten kcal − Activity kcal) ÷ Lean Body Mass (kg). LBM is estimated as "
            "weight × 0.85.</p>"
            "<ul class='list-disc ml-5'>"
            "<li>≥45 kcal/kg LBM — optimal</li>"
            "<li>30–45 — low (adaptation may suffer)</li>"
            "<li>&lt;30 — RED-S threshold (IOC). Hormonal disruption, immune suppression, "
            "bone density loss.</li>"
            "</ul>"
            "<p>Most relevant if you train hard and eat little — or unintentionally underfuel.</p>"
        ),
        "build_insight": _ea_insight,
        "sources": [_SRC_REDS_IOC, _SRC_LOUCKS],
    },
    "meal_timing": {
        "title": "Meal Timing & Eating Window",
        "what_html": (
            "<p>Each glowing dot is a logged meal placed on a 24-hour timeline. The eating "
            "window = first meal → last meal.</p>"
            "<p>Time-Restricted Eating literature (Patterson 2017) suggests confining eating "
            "to a ≤12-hour daily window — and ideally the active circadian phase — improves "
            "glycemic control and lipid markers independently of total calories.</p>"
        ),
        "build_insight": _meal_timing_insight,
        "sources": [_SRC_PATTERSON_TRE],
    },
    "balance_history": {
        "title": "7-Day Calorie Balance",
        "what_html": (
            "<p>Daily surplus (+) or deficit (−) over the last 7 days. The dashed line is a "
            "7-day moving average — your true cutting/maintaining/bulking signal.</p>"
            "<p>Single-day spikes are noise. Direction of the moving average is what matters "
            "for body composition.</p>"
        ),
        "build_insight": _balance_history_insight,
        "sources": [_SRC_MIFFLIN],
    },
    "heatmap": {
        "title": "Annual Readiness Heatmap",
        "what_html": (
            "<p>One cell per day, coloured by composite readiness score. Brighter = better recovered.</p>"
            "<p>Look for streaks: consecutive low (magenta) days often correspond to illness, "
            "jet lag, life stress, or accumulated training load — and the streak ends with a "
            "rest week.</p>"
        ),
        "build_insight": _heatmap_insight,
        "sources": [_SRC_PLEWS_HRV],
    },
    "hrv_trend": {
        "title": "HRV — 30-Day Trend",
        "what_html": (
            "<p>Day-by-day overnight HRV plus a 7-day moving average (dashed).</p>"
            "<p>The moving average is the primary signal. A sudden ≥20% drop vs the 7-day "
            "baseline triggers a Telegram <code>hrv_drop</code> alert.</p>"
        ),
        "build_insight": _hrv_trend_insight,
        "sources": [_SRC_PLEWS_HRV, _SRC_KIVINIEMI],
    },
    "rhr_trend": {
        "title": "Resting HR — 30-Day Trend",
        "what_html": (
            "<p>Daily resting HR with a 7-day moving average overlay.</p>"
            "<p>A 5-day strictly-rising streak fires a Telegram <code>resting_hr_trend</code> "
            "alert. The moving average is the cleaner signal for medium-term changes "
            "(training adaptation, illness onset).</p>"
        ),
        "build_insight": _rhr_trend_insight,
        "sources": [_SRC_BUCHHEIT, _SRC_TASK_FORCE_HRV],
    },
    "sleep_stages": {
        "title": "Sleep Stages",
        "what_html": (
            "<p>Garmin estimates four stages from HR, HRV, and accelerometer movement: "
            "deep, light, REM, awake.</p>"
            "<p>Healthy adult norms: 13–23% deep · 50–60% light · 20–25% REM · ≤10% awake. "
            "Wrist-based stage estimates are noisy compared to lab PSG — use the totals "
            "as rough guidance, not diagnosis.</p>"
        ),
        "build_insight": _sleep_stages_insight,
        "sources": [_SRC_NSF_SLEEP, _SRC_WALKER],
    },
    "sleep_duration": {
        "title": "Sleep Duration — 14 Days",
        "what_html": (
            "<p>Total sleep per night for the last 14 days. Compare individual nights to "
            "your average and your target.</p>"
        ),
        "build_insight": _sleep_duration_insight,
        "sources": [_SRC_NSF_SLEEP, _SRC_WALKER],
    },
    "activities": {
        "title": "Activities (14d)",
        "what_html": (
            "<p>Recent activities pulled from Garmin Connect. Each row: type · name · "
            "duration · average HR · calories · aerobic Training Effect.</p>"
            "<p>Training Effect bands: 1.0–2.0 maintaining · 2.0–3.0 improving · "
            "3.0–4.0 highly improving · 4.0–5.0 overreaching.</p>"
        ),
        "build_insight": _activities_insight,
        "sources": [_SRC_FOSTER],
    },
    "training_load": {
        "title": "Training Load — 30 Days",
        "what_html": (
            "<p>Daily activity calories. This series feeds the ACWR and Training Monotony "
            "calculations.</p>"
            "<p>Empty-bar streaks build chronic-load deficit; flat equal-height streaks "
            "flag monotony.</p>"
        ),
        "build_insight": _training_load_insight,
        "sources": [_SRC_GABBETT, _SRC_FOSTER],
    },
    "alerts": {
        "title": "Recent Alerts",
        "what_html": (
            "<p>The last 10 alert events stored locally — whether they were sent to Telegram "
            "successfully or not. Cooldowns are persisted per alert kind so they survive "
            "restarts.</p>"
            "<p>Daily-readiness alerts (illness_risk, training_ready, recovery_day) fire at "
            "most once per day; threshold alerts respect <code>ALERT_COOLDOWN_SECONDS</code>.</p>"
        ),
        "build_insight": _alerts_insight,
        "sources": [_SRC_PLEWS_HRV, _SRC_BUCHHEIT, _SRC_KIVINIEMI],
    },
}


def build_payload(ctx: dict) -> dict[str, dict[str, Any]]:
    """Return a JSON-serializable map of metric_id → {title, what, insight, sources}.

    `ctx` carries the per-render data needed by `build_insight` callables. The
    output is consumed by the dashboard's side-panel JS via a `<script type=
    "application/json">` block.
    """
    out: dict[str, dict[str, Any]] = {}
    for mid, entry in INFO.items():
        try:
            insight_html = entry["build_insight"](ctx)
        except Exception:
            insight_html = "<p>Insight unavailable for this metric right now.</p>"
        out[mid] = {
            "title": entry["title"],
            "what": entry["what_html"],
            "insight": insight_html,
            "sources": entry["sources"],
        }
    return out
