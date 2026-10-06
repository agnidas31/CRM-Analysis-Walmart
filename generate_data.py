"""
Generate a sanitized snapshot of Agent Assist report data for the portable
demo (agent-assist-scorecard-demo.html).

Source of shape/structure: the real app's own built-in stub/demo-data
generator (backend/app/routes/Agent_Assist/__init__.py), which the app
itself falls back to whenever BigQuery is unavailable. That stub is
already 100% synthetic -- no production numbers ever touch this script.

On top of that already-fake baseline we apply an extra sanitization pass
(sanitize()) that:
  * picks one multiplicative "family factor" per metric family (AHT,
    percentages, NPS/sentiment, counts) from a fixed seed, and
  * adds small independent per-value jitter,
so the exported numbers are guaranteed to differ from both the app's own
stub literals AND from any real prod figures, while preserving every
qualitative insight (AA beats Non-AA on AHT/CSAT/NPS, endpoint rankings,
trend shapes, etc.) because the same factor is applied consistently
within a metric family.

Run: python3 generate_data.py
Writes: data.json
"""
import json
import random
from datetime import datetime, timedelta

SEED = 71834
rng = random.Random(SEED)

PILOT_START = "2026-01-19"

# ── Sanitization (v2 -- wider spread than the first pass) ─────────────────
# Two strategies, chosen per family so bounded metrics (0-100 percentages,
# NPS/sentiment) don't get clipped into a flat pile-up at 0 or 100:
#
#   "mult"     -- unbounded quantities (AHT seconds, contact/agent counts).
#                 One multiplicative factor per family, drawn once, applied
#                 to every value in that family -- preserves every relative
#                 gap and ranking *exactly*, just at a different absolute
#                 scale. Wide range here is safe since there's no ceiling.
#
#   "additive" -- bounded 0-100 / -100..100 quantities (percentages, NPS,
#                 sentiment). A flat per-family offset shifts the whole
#                 family up/down, plus mild multiplicative spread, then
#                 clamps to the valid range. Additive shift is the safer
#                 primary lever here because a big multiplicative factor on
#                 a percentage that's already near 0 or 100 would clip and
#                 quietly erase the AA-vs-Non-AA gap -- an additive shift
#                 moves both sides together and keeps the gap intact.
FAMILY_PARAMS = {
    "time_s": {"mode": "mult", "factor": rng.uniform(0.62, 1.65)},
    "count":  {"mode": "mult", "factor": rng.uniform(0.55, 1.85)},
    # pct / score_pm params are filled in by finalize_bounded_params() below,
    # once we know the actual min/max values in this dataset -- see there
    # for why a *data-aware* safe range beats a blind fixed range.
}


def safe_additive_params(min_val, max_val, lo, hi, floor, spread_range):
    """Pick an additive-shift family transform that is *guaranteed* not to
    collapse the smallest or largest real value in the dataset onto the
    lo/hi boundary. A blind fixed offset range (e.g. always +/-16) can
    silently zero out a naturally-small metric like Transfer Rate (~5-10%)
    for BOTH AA and Non-AA simultaneously, which erases the comparison
    entirely -- exactly the failure mode we need to avoid. Solving for the
    offset range from the real min/max keeps the shift as large as
    possible while leaving `floor` units of headroom on both ends.
    """
    spread = rng.uniform(*spread_range)
    lo_off = (lo + floor) - min_val * spread
    hi_off = (hi - floor) - max_val * spread
    if lo_off > hi_off:
        # Degenerate case (shouldn't happen for this dataset) -- no safe
        # shift room, so don't shift at all rather than risk a collapse.
        offset = 0.0
    else:
        offset = rng.uniform(lo_off, hi_off)
    return {"mode": "additive", "offset": offset, "spread": spread, "lo": lo, "hi": hi}


def collect_family_extremes(node, parent_key=None, extremes=None):
    """Walk the raw (pre-sanitization) data tree and record the min/max
    numeric value seen for each bounded family, so safe_additive_params()
    can compute an offset range that can't collapse a real value to 0/100."""
    if extremes is None:
        extremes = {}
    if isinstance(node, dict):
        for k, v in node.items():
            collect_family_extremes(v, k, extremes)
    elif isinstance(node, list):
        for v in node:
            collect_family_extremes(v, parent_key, extremes)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        family = _family_for_key(parent_key or "")
        if family in ("pct", "score_pm"):
            lo, hi = extremes.get(family, (node, node))
            extremes[family] = (min(lo, node), max(hi, node))
    return extremes


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sanitize(value, family):
    """Scale + jitter a single numeric leaf. None passes through untouched."""
    if value is None:
        return None
    params = FAMILY_PARAMS[family]
    # Small independent jitter -- kept modest on purpose. The *family-wide*
    # factor/offset above is what makes the absolute numbers look very
    # different; this per-value wobble is just enough to avoid a
    # perfectly-linear "obviously-rescaled" look without being large enough
    # to flip the ordering between closely-spaced values (e.g. two
    # endpoints a few seconds apart in AHT).
    jitter = 1 + rng.uniform(-0.025, 0.025)

    if params["mode"] == "mult":
        v = value * params["factor"] * jitter
        if family == "count":
            return int(round(v))
        return round(v, 1)  # time_s

    # additive families (pct / score_pm): shift + mild multiplicative
    # spread around the shifted value, then clamp.
    v = (value + params["offset"]) * params["spread"] * jitter
    v = _clamp(v, params["lo"], params["hi"])
    return round(v, 1)


def sanitize_list(values, family):
    return [sanitize(v, family) for v in values]


# Map dict keys (or key-suffixes) to a sanitize family, used by the generic
# walker below so every generator can just build "real-shaped" numbers and
# have sanitization applied uniformly at the end.
KEY_FAMILY = {
    "aht": "time_s", "goal_aht": "time_s", "non_aa_aht_overall": "time_s",
    "fcr": "pct", "rcr": "pct", "transfer_rate": "pct", "csat": "pct",
    "acceptance_rate": "pct", "override_rate": "pct",
    "csat_response_rate": "pct", "nps_response_rate": "pct",
    "nps": "score_pm", "sentiment": "score_pm", "sentiment_score": "score_pm",
    "variance": "time_s",
    "contacts": "count", "overall": "count", "alpha_group": "count",
    "ab_test_500pairs": "count",
}


def _family_for_key(key: str):
    base = key
    for suffix in ("__contacts",):
        if base.endswith(suffix):
            return "count"
    if base.endswith("_aht"):
        return "time_s"
    if base.endswith("_fcr") or base == "fcr":
        return "pct"
    if base.endswith("_rcr") or base == "rcr":
        return "pct"
    if base.endswith("_transfer") or "transfer_rate" in base:
        return "pct"
    if base.endswith("_csat") or base == "csat":
        return "pct"
    if base.endswith("_nps") or base == "nps":
        return "score_pm"
    if "sentiment" in base:
        return "score_pm"
    if base.endswith("_contacts") or base in ("contacts", "overall", "alpha_group", "ab_test_500pairs"):
        return "count"
    return KEY_FAMILY.get(base)


def deep_sanitize(node, parent_key=None):
    """Recursively walk dicts/lists, sanitizing numeric leaves whose parent
    key maps to a known metric family. Strings, dates, booleans, and
    unrecognized numeric fields (denominators, _den, _tot counts feeding a
    ratio, etc.) pass through unchanged -- we only touch the metrics that
    actually drive chart values."""
    if isinstance(node, dict):
        return {k: deep_sanitize(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [deep_sanitize(v, parent_key) for v in node]
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        family = _family_for_key(parent_key or "")
        if family:
            return sanitize(node, family)
        return node
    return node


# ── 1. Executive Summary / Core Metrics KPI overview ──────────────────────────
# Shape mirrors the app's own /data and /core-metrics stub responses.
KPI_OVERVIEW = {
    "as_of": f"{PILOT_START} -> demo snapshot",
    "aa": {
        "aht": 189.2, "fcr": 78.5, "rcr": 12.3, "transfer_rate": 5.1,
        "csat": 87.2, "nps": 48.5, "acceptance_rate": 91.3, "contacts": 45280,
    },
    "non_aa": {
        "aht": 245.7, "fcr": 71.2, "rcr": 18.9, "transfer_rate": 8.7,
        "csat": 83.1, "nps": 38.4, "acceptance_rate": None, "contacts": 54720,
    },
    "end_buckets": [
        {"key": "smart",     "name": "Smart Transition", "contacts": 12450, "aht": 165.3},
        {"key": "northstar", "name": "Northstar",         "contacts": 18920, "aht": 195.8},
        {"key": "cca2",      "name": "CCA2 Redirect",     "contacts":  7830, "aht": 201.2},
        {"key": "default",   "name": "Default Handoff",   "contacts":  4920, "aht": 218.5},
        {"key": "genai",     "name": "Legacy GenAI",      "contacts":  1160, "aht": 178.9},
    ],
}

CORE_METRICS_BY_ENDPOINT = {
    "aa": KPI_OVERVIEW["aa"],
    "non_aa": KPI_OVERVIEW["non_aa"],
    "by_endpoint": [
        {"key": "smart", "name": "Smart Transition", "contacts": 12450,
         "aht": 165.3, "fcr": 81.2, "rcr": 10.1, "transfer_rate": 4.2,
         "csat": 89.1, "nps": 52.3, "acceptance_rate": 94.1},
        {"key": "northstar", "name": "Northstar", "contacts": 18920,
         "aht": 195.8, "fcr": 76.4, "rcr": 13.8, "transfer_rate": 5.9,
         "csat": 87.0, "nps": 48.1, "acceptance_rate": 90.5},
        {"key": "cca2", "name": "CCA2 Redirect", "contacts": 7830,
         "aht": 201.2, "fcr": 74.1, "rcr": 15.2, "transfer_rate": 6.8,
         "csat": 85.5, "nps": 44.2, "acceptance_rate": 88.3},
        {"key": "default", "name": "Default Handoff", "contacts": 4920,
         "aht": 218.5, "fcr": 71.8, "rcr": 17.4, "transfer_rate": 8.1,
         "csat": 83.2, "nps": 40.5, "acceptance_rate": 86.2},
        {"key": "genai", "name": "Legacy GenAI", "contacts": 1160,
         "aht": 178.9, "fcr": 78.9, "rcr": 12.7, "transfer_rate": 5.5,
         "csat": 88.1, "nps": 50.0, "acceptance_rate": 92.7},
    ],
}


# ── 2. AHT tab (exec-summary-charts) ──────────────────────────────────────────
def make_exec_chart():
    n = 90
    start = datetime(2026, 3, 1)
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    non_aa_aht = [round(537 + (rng.random() - 0.5) * 40, 1) for _ in range(n)]
    # Total drift held constant (~45s of AA improvement over the full window)
    # regardless of how many days we're generating, so a longer history
    # doesn't accidentally create an unrealistically steep decline.
    aa_aht = [round(585 - (i / (n - 1)) * 45 + (rng.random() - 0.5) * 35, 1) for i in range(n)]
    total_aht = [round(a * 0.38 + b * 0.62, 1) for a, b in zip(aa_aht, non_aa_aht)]
    aa_contacts = [int(2200 + (rng.random() - 0.5) * 1000) for _ in range(n)]
    non_aa_contacts = [int(10500 + (rng.random() - 0.5) * 2500) for _ in range(n)]
    variance = [round(a - b, 1) for a, b in zip(aa_aht, non_aa_aht)]

    ep_diff = {"genai": -102, "northstar": -65, "smart": -38, "non_smart": 131, "default": 101, "cca2": 240}
    ep_ct_base = {"smart": 1180, "northstar": 615, "default": 540, "genai": 460, "non_smart": 250, "cca2": 55}
    ep_trends = {"dates": dates, "non_aa_aht": non_aa_aht}
    for ep, diff in ep_diff.items():
        ep_trends[ep] = [round(na + diff + (rng.random() - 0.5) * 30, 1) for na in non_aa_aht]
        ep_trends[f"{ep}__contacts"] = [
            int(ep_ct_base[ep] + (rng.random() - 0.5) * ep_ct_base[ep] * 0.3) for _ in range(n)
        ]
    ep_trends["northstar"][4] = None
    ep_trends["northstar"][11] = None
    ep_trends["northstar"][47] = None
    ep_trends["cca2"][62] = None

    return {
        "trends": {
            "dates": dates, "aa_aht": aa_aht, "non_aa_aht": non_aa_aht,
            "total_aht": total_aht, "aa_contacts": aa_contacts,
            "non_aa_contacts": non_aa_contacts, "variance": variance,
        },
        "endpoint_trends": ep_trends,
        "endpoint_stats": {
            "smart":     {"contacts": 35200, "aht": 498.3, "pct_aa": 38.2, "diff_non_aa":  -38.4},
            "northstar": {"contacts": 18400, "aht": 471.9, "pct_aa": 20.0, "diff_non_aa":  -64.8},
            "default":   {"contacts": 16100, "aht": 637.2, "pct_aa": 17.5, "diff_non_aa":  100.5},
            "genai":     {"contacts": 13800, "aht": 434.5, "pct_aa": 15.0, "diff_non_aa": -102.2},
            "non_smart": {"contacts":  6900, "aht": 667.5, "pct_aa":  7.5, "diff_non_aa":  130.8},
            "cca2":      {"contacts":  1600, "aht": 777.1, "pct_aa":  1.8, "diff_non_aa":  240.4},
        },
        "aa_tenure": [
            {"tenure": "0-3 months",  "genai":  800, "northstar": 200, "smart":  2800, "non_smart":  1900, "default":  1400, "cca2":  200,
             "genai_aht": 480, "northstar_aht": 510, "smart_aht": 530, "non_smart_aht": 720, "default_aht": 680, "cca2_aht": 820},
            {"tenure": "4-6 months",  "genai": 1200, "northstar": 350, "smart":  4800, "non_smart":  3200, "default":  2400, "cca2":  350,
             "genai_aht": 460, "northstar_aht": 490, "smart_aht": 510, "non_smart_aht": 700, "default_aht": 660, "cca2_aht": 800},
            {"tenure": "7-12 months", "genai": 3200, "northstar": 550, "smart":  9800, "non_smart":  6200, "default":  4900, "cca2":  700,
             "genai_aht": 440, "northstar_aht": 475, "smart_aht": 495, "non_smart_aht": 670, "default_aht": 635, "cca2_aht": 780},
            {"tenure": ">12 months",  "genai": 7100, "northstar": 800, "smart": 17200, "non_smart": 11300, "default":  9100, "cca2": 1050,
             "genai_aht": 420, "northstar_aht": 455, "smart_aht": 480, "non_smart_aht": 650, "default_aht": 620, "cca2_aht": 760},
        ],
        "non_aa_tenure": [
            {"tenure": "0-3 months",  "contacts": 12000, "aht": 650},
            {"tenure": "4-6 months",  "contacts": 20000, "aht": 590},
            {"tenure": "7-12 months", "contacts": 18000, "aht": 540},
            {"tenure": ">12 months",  "contacts": 22000, "aht": 500},
        ],
        "goal_aht": 530,
        "non_aa_aht_overall": 536.7,
    }


# ── 3. Executive Summary trend charts (core-metrics-trends) ─────────────────
def make_core_metrics_trends():
    n = 90
    start = datetime(2026, 3, 1)
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    def s(base, noise):
        return [round(base + (rng.random() - 0.5) * 2 * noise, 1) for _ in range(n)]

    # NOTE: these base literals used to have AA *losing* to Non-AA on AHT/FCR/RCR
    # (561.8s/75.2%/17.8% vs 536.3s/75.6%/17.3%) -- a real bug inherited from the
    # source app's own stub data that silently contradicted every other tab in
    # this report (AHT tab, Core Metrics by Endpoint, Workflow, etc. all show AA
    # winning). Fixed here so the Executive Summary headline KPIs -- the first
    # thing anyone sees -- agree with the rest of the story. "Overall" also used
    # to be a naive 50/50 average of aa/non_aa despite AA being ~1.2% of total
    # contacts (45,280 of 3.7M); switched to a volume-weighted blend so "Overall"
    # actually sits close to Non-AA like it should.
    AA_SHARE = 45280 / (45280 + 3667405)
    def overall_weighted(a, b):
        return [round(x * AA_SHARE + y * (1 - AA_SHARE), 1) for x, y in zip(a, b)]

    aa_aht, non_aa_aht = s(478.5, 28), s(536.3, 18)
    overall_aht = overall_weighted(aa_aht, non_aa_aht)
    aa_fcr, non_aa_fcr = s(79.8, 4.0), s(75.6, 3.0)
    overall_fcr = overall_weighted(aa_fcr, non_aa_fcr)
    aa_rcr, non_aa_rcr = s(13.9, 2.5), s(17.3, 2.0)
    overall_rcr = overall_weighted(aa_rcr, non_aa_rcr)
    aa_tr, non_aa_tr = s(6.3, 1.5), s(7.4, 1.2)
    overall_tr = overall_weighted(aa_tr, non_aa_tr)
    aa_csat, non_aa_csat = s(89.6, 5.0), s(87.2, 4.5)
    overall_csat = overall_weighted(aa_csat, non_aa_csat)
    aa_nps, non_aa_nps = s(63.3, 15.0), s(37.2, 12.0)
    overall_nps = overall_weighted(aa_nps, non_aa_nps)
    aa_accept = s(58.0, 4.0)
    aa_override = [round(100.0 - v, 1) for v in aa_accept]

    for i in (5, 18, 33, 52, 71, 84):
        aa_csat[i] = non_aa_csat[i] = overall_csat[i] = None
        aa_nps[i] = non_aa_nps[i] = overall_nps[i] = None

    return {
        "summary": {
            "overall": {"aht": overall_aht[-1], "fcr": overall_fcr[-1], "rcr": overall_rcr[-1], "transfer_rate": overall_tr[-1], "csat": 87.2, "nps": 37.5},
            "aa":      {"aht": 478.5, "fcr": 79.8, "rcr": 13.9, "transfer_rate": 6.3, "csat": 89.6, "nps": 63.3, "acceptance_rate": 58.0, "override_rate": 42.0, "contacts": 45280},
            "non_aa":  {"aht": 536.3, "fcr": 75.6, "rcr": 17.3, "transfer_rate": 7.4, "csat": 87.2, "nps": 37.2, "contacts": 3667405},
        },
        "trends": {
            "dates": dates,
            "overall_aht": overall_aht, "aa_aht": aa_aht, "non_aa_aht": non_aa_aht,
            "overall_fcr": overall_fcr, "aa_fcr": aa_fcr, "non_aa_fcr": non_aa_fcr,
            "overall_rcr": overall_rcr, "aa_rcr": aa_rcr, "non_aa_rcr": non_aa_rcr,
            "overall_transfer_rate": overall_tr, "aa_transfer_rate": aa_tr, "non_aa_transfer_rate": non_aa_tr,
            "overall_csat": overall_csat, "aa_csat": aa_csat, "non_aa_csat": non_aa_csat,
            "overall_nps": overall_nps, "aa_nps": aa_nps, "non_aa_nps": non_aa_nps,
            "aa_acceptance": aa_accept, "aa_override": aa_override,
        },
    }


# ── 4. Core Metrics by Endpoint — per-endpoint daily trends ──────────────
def make_endpoint_metric_trends():
    n = 90
    start = datetime(2026, 3, 1)
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    def s(base, noise):
        return [round(base + (rng.random() - 0.5) * 2 * noise, 1) for _ in range(n)]

    non_aa = {
        "aht": s(536.3, 18.0), "fcr": s(75.6, 3.0), "rcr": s(17.3, 2.0),
        "transfer_rate": s(7.4, 1.2), "csat": s(87.2, 4.5), "nps": s(37.2, 12.0),
    }
    EP_BASES = {
        "genai":     {"aht": 189.2, "fcr": 81.2, "rcr": 10.1, "transfer_rate": 9.0,  "csat": 88.2, "nps": 65.1},
        "northstar": {"aht": 200.5, "fcr": 76.4, "rcr": 14.2, "transfer_rate": 9.8,  "csat": 86.0, "nps": 60.3},
        "smart":     {"aht": 185.0, "fcr": 78.9, "rcr": 11.8, "transfer_rate": 4.0,  "csat": 88.5, "nps": 66.2},
        "non_smart": {"aht": 192.3, "fcr": 77.1, "rcr": 13.3, "transfer_rate": 6.5,  "csat": 87.1, "nps": 62.8},
        "default":   {"aht": 195.4, "fcr": 78.2, "rcr": 12.8, "transfer_rate": 6.7,  "csat": 87.4, "nps": 62.2},
        "cca2":      {"aht": 210.1, "fcr": 74.3, "rcr": 15.7, "transfer_rate": 10.8, "csat": 85.1, "nps": 58.4},
    }
    EP_NOISE = {"aht": 18.0, "fcr": 3.5, "rcr": 2.0, "transfer_rate": 1.5, "csat": 4.0, "nps": 10.0}
    EP_CONTACTS = {"genai": 12500, "northstar": 8200, "smart": 6800, "non_smart": 9100, "default": 5400, "cca2": 3280}

    endpoints = {ep: {m: s(bases[m], EP_NOISE[m]) for m in EP_NOISE} for ep, bases in EP_BASES.items()}
    for ep in endpoints:
        endpoints[ep]["csat"][7] = None
        endpoints[ep]["nps"][7] = None
        endpoints[ep]["csat"][58] = None
        endpoints[ep]["nps"][58] = None
    non_aa["csat"][5] = None
    non_aa["nps"][5] = None
    non_aa["csat"][73] = None
    non_aa["nps"][73] = None

    endpoint_stats = {ep: {**EP_BASES[ep], "contacts": EP_CONTACTS[ep]} for ep in EP_BASES}
    non_aa_stats = {"aht": 536.3, "fcr": 75.6, "rcr": 17.3, "transfer_rate": 7.4, "csat": 87.2, "nps": 37.2, "contacts": 54720}

    return {
        "dates": dates, "non_aa": non_aa, "non_aa_stats": non_aa_stats,
        "endpoints": endpoints, "endpoint_stats": endpoint_stats,
    }


# ── 5. Metrics by L3 Workflow ─────────────────────────────────────────────────
def wf_row(l1, l2, l3, non_aa_c, non_aa_a, aa_c, aa_a, g_c, g_a, ns_c, ns_a, sm_c, sm_a, nsm_c, nsm_a, df_c, df_a, cc_c, cc_a):
    return {
        "l1": l1, "l2": l2, "l3": l3,
        "non_aa_contacts": non_aa_c, "non_aa_aht": non_aa_a,
        "aa_contacts": aa_c, "aa_aht": aa_a,
        "genai_contacts": g_c, "genai_aht": g_a,
        "northstar_contacts": ns_c, "northstar_aht": ns_a,
        "smart_contacts": sm_c, "smart_aht": sm_a,
        "non_smart_contacts": nsm_c, "non_smart_aht": nsm_a,
        "default_contacts": df_c, "default_aht": df_a,
        "cca2_contacts": cc_c, "cca2_aht": cc_a,
    }


WORKFLOW_ROWS = [
    wf_row("Digital", "Orders",   "Order Status",       8450, 245.2, 7230, 182.1, 1180, 195.3, 2090, 178.5, 1540, 168.9,  800, 210.4,  940, 225.7,  680, 185.2),
    wf_row("Digital", "Orders",   "Order Cancellation", 4210, 310.8, 3650, 241.5,  640, 258.0, 1010, 235.4, 1120, 229.8,  420, 265.1,  280, 275.0,  180, 244.0),
    wf_row("Digital", "Returns",  "Return Request",     6820, 312.5, 5490, 228.4,  870, 241.0, 1650, 220.3, 1280, 218.5,  680, 245.7,  590, 260.2,  420, 232.1),
    wf_row("Digital", "Returns",  "Return Status",      3180, 198.4, 2840, 156.7,  490, 162.0,  840, 150.3,  720, 148.8,  340, 168.4,  250, 175.0,  200, 158.2),
    wf_row("Digital", "Payments", "Payment Issue",      2950, 425.6, 2340, 315.2,  310, 328.0,  710, 305.4,  680, 299.8,  290, 335.1,  210, 345.0,  140, 320.0),
    wf_row("Digital", "Account",  "Account Access",     5620, 185.3, 4820, 142.8,  820, 151.0, 1420, 138.5, 1180, 135.9,  590, 155.4,  480, 162.3,  330, 146.0),
    wf_row("Digital", "Delivery", "Delivery Delay",     7340, 278.9, 6120, 205.6,  980, 216.7, 1780, 198.2, 1420, 190.4,  710, 228.9,  650, 240.1,  560, 202.8),
    wf_row("Digital", "Subscription", "Billing Dispute", 3960, 355.7, 3210, 268.3,  520, 281.4,  890, 258.9,  760, 250.2,  380, 295.6,  340, 305.3,  320, 262.7),
    wf_row("Voice",   "Membership", "Card Issue",        4580, 402.1, 3740, 298.5,  610, 312.8, 1030, 288.4,  890, 279.6,  440, 328.2,  400, 340.9,  370, 291.3),
    wf_row("Chat",     "Technical", "App Crash / Error",  2640, 268.4, 2280, 198.2,  380, 208.9,  660, 190.7,  560, 183.1,  280, 220.5,  240, 231.6,  160, 195.4),
]


# ── 6. Agent Utilization — extended to 26 weeks (half a year) for a richer trend ─
def make_agent_utilization():
    n = 26
    start = datetime(2025, 11, 3)
    dates, labels = [], []
    for i in range(n):
        d = start + timedelta(weeks=i)
        dates.append(d.strftime("%Y-%m-%d"))
        labels.append(d.strftime("%Y") + "W" + str(d.isocalendar()[1]))

    def grow(target, start_frac=0.68, noise=0.02):
        out = []
        for i in range(n):
            frac = start_frac + (1 - start_frac) * (i / (n - 1))
            v = target * frac * (1 + rng.uniform(-noise, noise))
            out.append(int(round(v)))
        return out

    overall = grow(1247)
    alpha = grow(982)
    ab_test = grow(265)
    return {
        "cards": {"overall": overall[-1], "alpha_group": alpha[-1], "ab_test_500pairs": ab_test[-1]},
        "dates": dates, "date_labels": labels,
        "trends": {"overall": overall, "alpha_group": alpha, "ab_test_500pairs": ab_test},
    }


# ── 7. Sentiment Score tab ──────────────────────────────────
def make_sentiment():
    n = 90
    start = datetime(2026, 3, 1)
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    def s(base, noise):
        return [round(_clamp(base + (rng.random() - 0.5) * 2 * noise, 0, 100), 1) for _ in range(n)]

    aa_trend = s(52.5, 4.5)
    non_aa_trend = s(59.0, 3.5)

    EP_BASES = {"genai": 55.2, "northstar": 51.8, "smart": 57.4, "non_smart": 50.1, "default": 53.0, "cca2": 47.6}
    endpoint_trends = {"dates": dates, "non_aa": non_aa_trend}
    endpoint_stats = {}
    EP_CONTACTS = {"genai": 12500, "northstar": 8200, "smart": 6800, "non_smart": 9100, "default": 5400, "cca2": 3280}
    for ep, base in EP_BASES.items():
        endpoint_trends[ep] = s(base, 4.0)
        endpoint_stats[ep] = {"sentiment_score": base, "contacts": EP_CONTACTS[ep]}

    return {
        "aa": {"sentiment_score": 52.5, "contacts": 160000},
        "non_aa": {"sentiment_score": 59.0, "contacts": 820000},
        "trends": {"dates": dates, "aa": aa_trend, "non_aa": non_aa_trend},
        "endpoint_trends": endpoint_trends,
        "endpoint_stats": endpoint_stats,
        "aa_tenure": [
            {"tenure": "0-3 months",  "sentiment": 48.2, "contacts":  4700},
            {"tenure": "4-6 months",  "sentiment": 50.6, "contacts":  8500},
            {"tenure": "7-12 months", "sentiment": 52.9, "contacts": 15100},
            {"tenure": ">12 months",  "sentiment": 54.8, "contacts": 41750},
        ],
        "non_aa_tenure": [
            {"tenure": "0-3 months",  "sentiment": 55.1, "contacts": 12000},
            {"tenure": "4-6 months",  "sentiment": 57.3, "contacts": 20000},
            {"tenure": "7-12 months", "sentiment": 59.6, "contacts": 18000},
            {"tenure": ">12 months",  "sentiment": 61.2, "contacts": 22000},
        ],
    }


# ── 8. Channel Matrix widget (Voice vs Chat vs Overall) ──────────────────────
def make_channel_matrix():
    VOICE_SHARE = 0.62
    buckets = {b["key"]: b for b in KPI_OVERVIEW["end_buckets"]}

    def split(entry_contacts, entry_aht, share):
        c = int(round(entry_contacts * share))
        a = round(entry_aht * (1 + rng.uniform(-0.05, 0.05)), 1)
        return c, a

    def build_channel(share):
        aa_c, aa_a = split(KPI_OVERVIEW["aa"]["contacts"], KPI_OVERVIEW["aa"]["aht"], share)
        naa_c, naa_a = split(KPI_OVERVIEW["non_aa"]["contacts"], KPI_OVERVIEW["non_aa"]["aht"], share)
        bucket_out = {}
        for key, b in buckets.items():
            bc, ba = split(b["contacts"], b["aht"], share)
            bucket_out[key] = {"contacts": bc, "aht_s": ba}
        total_c = aa_c + naa_c
        total_a = round((aa_a * aa_c + naa_a * naa_c) / total_c, 1) if total_c else None
        return {
            "aa_contacts": aa_c, "aa_aht_s": aa_a,
            "naa_contacts": naa_c, "naa_aht_s": naa_a,
            "total_contacts": total_c, "total_aht_s": total_a,
            "buckets": bucket_out,
        }

    return {
        "Overall": build_channel(1.0),
        "Voice": build_channel(VOICE_SHARE),
        "Chat": build_channel(1 - VOICE_SHARE),
    }


# ── Assemble + sanitize ───────────────────────────────────────────────────────
raw = {
    "meta": {
        "report_title": "Agent Assist",
        "report_subtitle": "AA vs Non-AA performance metrics -- AHT, FCR, RCR, CSAT, NPS, Acceptance Rate",
        "pilot_start": PILOT_START,
        "note": "Portable demo snapshot with sanitized sample data. Not connected to any live system.",
    },
    "kpi_overview": KPI_OVERVIEW,
    "core_metrics_by_endpoint": CORE_METRICS_BY_ENDPOINT,
    "exec_chart": make_exec_chart(),
    "core_metrics_trends": make_core_metrics_trends(),
    "endpoint_metric_trends": make_endpoint_metric_trends(),
    "workflow_rows": WORKFLOW_ROWS,
    "agent_utilization": make_agent_utilization(),
    "sentiment": make_sentiment(),
    "channel_matrix": make_channel_matrix(),
}

# Compute data-aware safe additive params for the two bounded families
# *before* sanitizing, so the offset can never collapse the smallest or
# largest real value in this dataset onto the 0/100 boundary (see
# safe_additive_params() docstring -- this is what the earlier Transfer
# Rate-collapsed-to-0.0-for-both-sides bug taught us).
extremes = collect_family_extremes(raw)
pct_lo, pct_hi = extremes["pct"]
score_lo, score_hi = extremes["score_pm"]
FAMILY_PARAMS["pct"] = safe_additive_params(pct_lo, pct_hi, 0.0, 100.0, floor=2.0, spread_range=(0.92, 1.08))
FAMILY_PARAMS["score_pm"] = safe_additive_params(
    score_lo, score_hi, lo=0.0, hi=100.0, floor=5.0, spread_range=(0.90, 1.10)
)
# NOTE: every NPS/sentiment value in this dataset is naturally positive
# (net-promoter, not net-detractor), so we use lo=0 here rather than the
# theoretical -100 floor. That's deliberate: sign flips (positive NPS ->
# negative NPS) change the *story* being told -- "customers are promoters"
# vs "customers are detractors" -- which is a bigger narrative change than
# "push the numbers further" should cause, even though the AA-vs-Non-AA
# relative comparison would still technically hold either way.

sanitized = deep_sanitize(raw)

with open("data.json", "w") as f:
    json.dump(sanitized, f, indent=2)

# Sanity check: confirm the core insight direction survives sanitization.
aa_aht = sanitized["kpi_overview"]["aa"]["aht"]
naa_aht = sanitized["kpi_overview"]["non_aa"]["aht"]
print(f"Sanitized AA AHT: {aa_aht}s vs Non-AA AHT: {naa_aht}s "
      f"(AA {'faster' if aa_aht < naa_aht else 'SLOWER -- check factors!'} "
      f"by {round(naa_aht - aa_aht, 1)}s)")
print(f"Family params used this run: {FAMILY_PARAMS}")
print("Wrote data.json")
