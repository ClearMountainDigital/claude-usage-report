#!/usr/bin/env python3
"""claude-usage-report — a local token & cost dashboard for Claude Code.

Reads the session transcripts Claude Code already keeps on your machine
(~/.claude/projects/**/*.jsonl), aggregates token usage over a trailing
window, applies published per-model list prices, and writes a single
self-contained static HTML dashboard. No dependencies, no network calls,
nothing leaves your machine.

Usage:
    python3 claude_usage.py                 # last 30 days -> claude-usage-report.html, opens browser
    python3 claude_usage.py --days 7
    python3 claude_usage.py --out ~/Desktop/usage.html --no-open
    python3 claude_usage.py --claude-dir /path/to/.claude/projects

The numbers are ESTIMATES computed from local logs at list prices — not
your official Anthropic bill. For that, see console.anthropic.com.
"""

import argparse
import glob
import html as H
import json
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Pricing — $ per 1M tokens: (input, output). Edit here as prices change.
# Cache writes bill at 1.25x input (5-min TTL); cache reads at 0.1x input.
# Source: platform.claude.com/docs pricing (checked 2026-07).
# ---------------------------------------------------------------------------
RATES = {
    "claude-fable-5":            (10.0, 50.0),
    "claude-mythos-5":           (10.0, 50.0),
    "claude-opus-4-8":           (5.0, 25.0),
    "claude-opus-4-7":           (5.0, 25.0),
    "claude-opus-4-6":           (5.0, 25.0),
    "claude-opus-4-5":           (5.0, 25.0),
    "claude-sonnet-5":           (2.0, 10.0),   # intro pricing through 2026-08-31 (list: 3/15)
    "claude-sonnet-4-6":         (3.0, 15.0),
    "claude-sonnet-4-5":         (3.0, 15.0),
    "claude-haiku-4-5":          (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
DEFAULT_RATE = (5.0, 25.0)  # unknown models fall back to Opus-tier pricing
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10

DISPLAY_NAMES = {
    "claude-fable-5": "Fable 5",
    "claude-mythos-5": "Mythos 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-5": "Opus 4.5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-haiku-4-5": "Haiku 4.5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
}

# (light, dark) color pairs assigned to models in cost order.
# MountainLabs.ai brand hues: forest/sage, slate/sky, rust, amber, red, stone.
PALETTE = [
    ("#3f5147", "#96c8a5"),  # forest / sage
    ("#495b6c", "#96b4cd"),  # slate / sky
    ("#a4561f", "#e28a4a"),  # rust
    ("#8c6a25", "#d0aa68"),  # amber
    ("#9c423c", "#ee6c64"),  # red
    ("#716a56", "#a89e88"),  # stone
]

TOKEN_TYPES = [("output", 1.0), ("input", 0.75), ("cache_write", 0.5), ("cache_read", 0.28)]


def rate(model):
    inp, out = RATES.get(model, DEFAULT_RATE)
    return {
        "input": inp,
        "output": out,
        "cache_write": inp * CACHE_WRITE_MULT,
        "cache_read": inp * CACHE_READ_MULT,
    }


def display_name(model):
    return DISPLAY_NAMES.get(model, model)


def strip_home(encoded, home_prefix):
    if encoded.startswith(home_prefix + "-"):
        return encoded[len(home_prefix) + 1:]
    if encoded.startswith(home_prefix):
        return encoded[len(home_prefix):] or "~"
    return encoded


def prettify_projects(names):
    """Strip the path prefix common to ALL projects, keeping >=1 segment each."""
    tokenized = {n: n.split("-") for n in names}
    fallback = None
    if len(names) > 1:
        shortest = min(len(t) for t in tokenized.values())
        common = 0
        while common < shortest:
            segs = {t[common] for t in tokenized.values()}
            if len(segs) != 1:
                break
            common += 1
        if common:
            fallback = tokenized[names[0]][common - 1]  # last shared segment
            tokenized = {n: t[common:] for n, t in tokenized.items()}
    return {n: ("-".join(t) or fallback or n).replace("--claude-worktrees-", " » ")
            for n, t in tokenized.items()}


def fmt_cost(v):
    return "${:,.2f}".format(v)


def fmt_tokens(v):
    v = float(v)
    if v >= 1e9:
        return "{:.2f}B".format(v / 1e9)
    if v >= 1e6:
        return "{:.1f}M".format(v / 1e6)
    if v >= 1e3:
        return "{:.0f}K".format(v / 1e3)
    return str(int(v))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def collect(claude_dir, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    files = glob.glob(os.path.join(claude_dir, "**", "*.jsonl"), recursive=True)
    if not files:
        return None

    home_prefix = str(Path.home()).replace("/", "-").replace("\\", "-")

    proj_model = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    day_cost = defaultdict(float)
    seen = set()
    responses = 0

    for f in files:
        try:
            rel = f.split(os.sep + "projects" + os.sep, 1)[1]
            proj = rel.split(os.sep, 1)[0]
        except IndexError:
            proj = os.path.basename(os.path.dirname(f))
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                ts = obj.get("timestamp")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                mid = msg.get("id")
                key = (mid, usage.get("output_tokens"))
                if mid and key in seen:
                    continue
                if mid:
                    seen.add(key)
                model = msg.get("model", "unknown")
                if model.startswith("<"):  # skip synthetic placeholder entries
                    continue
                toks = {
                    "input": usage.get("input_tokens", 0) or 0,
                    "output": usage.get("output_tokens", 0) or 0,
                    "cache_write": usage.get("cache_creation_input_tokens", 0) or 0,
                    "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                }
                if not any(toks.values()):
                    continue
                for k, v in toks.items():
                    proj_model[proj][model][k] += v
                r = rate(model)
                day = dt.astimezone().strftime("%Y-%m-%d")
                day_cost[day] += sum(toks[k] * r[k] / 1e6 for k in toks)
                responses += 1

    if not proj_model:
        return None

    projects = []
    model_totals = defaultdict(lambda: {"tokens": 0, "cost": 0.0})
    grand_cost = 0.0
    grand_tok = 0
    for proj, models in proj_model.items():
        p_tok, p_cost, p_models = 0, 0.0, {}
        for model, toks in models.items():
            r = rate(model)
            c = sum(toks[k] * r[k] / 1e6 for k in toks)
            t = sum(toks.values())
            p_models[model] = {"tokens": dict(toks), "total_tokens": t, "cost": c}
            p_tok += t
            p_cost += c
            model_totals[model]["tokens"] += t
            model_totals[model]["cost"] += c
        if p_cost <= 0:
            continue
        projects.append({
            "project": strip_home(proj, home_prefix),
            "total_tokens": p_tok,
            "cost": p_cost,
            "models": p_models,
        })
        grand_cost += p_cost
        grand_tok += p_tok

    if projects:
        pretty = prettify_projects([p["project"] for p in projects])
        for p in projects:
            p["project"] = pretty[p["project"]]
    projects.sort(key=lambda p: -p["cost"])
    return {
        "projects": projects,
        "model_totals": {m: v for m, v in model_totals.items() if v["cost"] > 0},
        "by_day": dict(day_cost),
        "grand_cost": grand_cost,
        "grand_tokens": grand_tok,
        "responses": responses,
    }


# ---------------------------------------------------------------------------
# HTML generation (fully static — no JavaScript, so it renders anywhere)
# ---------------------------------------------------------------------------

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Code — Token &amp; Cost Report</title>
<style>
/* MountainLabs.ai palette — forest/stone/slate identity hues, warm #26140A base,
   amber spend accent; sage->rust->red reserved for semantic use. */
:root{
  --paper:#f2eee4;--panel:#fbf9f4;--ink:#26140a;--muted:#5d564a;--faint:#8a8172;
  --line:#e2dbc9;--line2:#ece6d7;--accent:#8c6a25;--accent-soft:#f0e4c8;
  --shadow:0 1px 2px rgba(38,20,10,.06),0 1px 3px rgba(38,20,10,.05);
__CSSVARS_LIGHT__
}
@media (prefers-color-scheme:dark){:root{
  --paper:#26140a;--panel:#301d0e;--ink:#f2f2f2;--muted:#c2b49b;--faint:#8f8168;
  --line:#43301c;--line2:#382817;--accent:#d0aa68;--accent-soft:#3a2a12;
  --shadow:0 1px 3px rgba(0,0,0,.45);
__CSSVARS_DARK__
}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:32px 24px 64px}
.eyebrow{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
header.top{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:8px}
h1{font-size:23px;margin:6px 0 0;letter-spacing:-.01em;text-wrap:balance}
.rangebox{text-align:right;font-size:12.5px;color:var(--muted)}
.rangebox .r{font-family:ui-monospace,Menlo,monospace;color:var(--ink);font-size:13px;font-variant-numeric:tabular-nums}
.note{font-size:12px;color:var(--muted);background:var(--accent-soft);border:1px solid var(--line);
  border-radius:8px;padding:9px 13px;margin:18px 0 26px;display:flex;gap:8px;align-items:baseline}
.note b{color:var(--ink)}
.note code{font-family:ui-monospace,Menlo,monospace;font-size:11px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:15px 16px;box-shadow:var(--shadow)}
.kpi .lbl{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}
.kpi .val{font-family:ui-monospace,Menlo,monospace;font-size:27px;font-weight:600;letter-spacing:-.02em;margin-top:7px;font-variant-numeric:tabular-nums}
.kpi .val.sm{font-size:15px;line-height:1.35}
.kpi .sub{font-size:11.5px;color:var(--muted);margin-top:3px}
.kpi.accent .val{color:var(--accent)}
.grid2{display:grid;grid-template-columns:1.15fr 1fr;gap:14px;margin:14px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:17px 18px;box-shadow:var(--shadow)}
.card h2{font-size:12px;font-family:ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin:0 0 16px;font-weight:600}
.chart{display:flex;align-items:flex-end;gap:6px;height:150px;padding-top:6px}
.bar{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%;justify-content:flex-end;min-width:0}
.bar .col{width:100%;max-width:34px;background:linear-gradient(var(--accent),color-mix(in srgb,var(--accent) 55%,transparent));
  border-radius:4px 4px 0 0;min-height:2px}
.bar .cost{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.bar .day{font-family:ui-monospace,Menlo,monospace;font-size:9.5px;color:var(--faint);font-variant-numeric:tabular-nums;white-space:nowrap}
.mgroup{margin-bottom:13px}
.mrow{display:grid;grid-template-columns:14px 1fr auto;gap:10px;align-items:center}
.dot{width:11px;height:11px;border-radius:3px;flex:none}
.mname{font-size:13px}
.mname .t{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--faint)}
.mbar{height:6px;border-radius:3px;background:var(--line2);margin-top:5px;overflow:hidden}
.mbar span{display:block;height:100%;border-radius:3px}
.mcost{font-family:ui-monospace,Menlo,monospace;font-size:13px;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.mcost .p{font-size:10.5px;color:var(--faint);font-weight:400}
.projhead{display:flex;justify-content:space-between;align-items:baseline;margin:26px 0 12px}
.projhead h2{font-size:12px;font-family:ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:0}
.ptable{display:flex;flex-direction:column;gap:9px}
.prow{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 16px;box-shadow:var(--shadow)}
.pmain{display:grid;grid-template-columns:1fr auto auto;gap:16px;align-items:center;cursor:pointer;list-style:none}
.pmain::-webkit-details-marker{display:none}
.pname{font-size:14px;font-weight:600;letter-spacing:-.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pname .arw{color:var(--faint);font-size:10px;margin-right:7px;display:inline-block;transition:transform .18s}
.prow[open] .arw{transform:rotate(90deg)}
.ptok{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.pcost{font-family:ui-monospace,Menlo,monospace;font-size:16px;font-weight:600;text-align:right;min-width:82px;font-variant-numeric:tabular-nums}
.compbar{display:flex;height:7px;border-radius:4px;overflow:hidden;margin-top:11px;background:var(--line2)}
.compbar span{height:100%}
.detail-in{padding-top:14px;margin-top:13px;border-top:1px solid var(--line2)}
.mline{display:grid;grid-template-columns:16px 150px 1fr 74px;gap:11px;align-items:center;font-size:12px;margin-bottom:9px}
.mline .nm{font-family:ui-monospace,Menlo,monospace;font-size:11px}
.mline .tk{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:var(--faint)}
.mline .c{font-family:ui-monospace,Menlo,monospace;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.stack{display:flex;height:9px;border-radius:3px;overflow:hidden;background:var(--line2)}
.stack span{height:100%}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:6px;font-size:11px;color:var(--muted)}
.legend .li{display:flex;align-items:center;gap:6px}
.legend .sw{width:10px;height:10px;border-radius:2px;background:var(--faint)}
footer{margin-top:34px;padding-top:20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
footer h3{font-size:11px;font-family:ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin:0 0 10px}
.rates{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px 20px;margin:10px 0}
.rates div{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.rates .rn{color:var(--ink)}
.disc{margin-top:14px;line-height:1.6}
@media(max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}
  .mline{grid-template-columns:16px 1fr 60px}.mline .stack{display:none}.chart{gap:3px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <div class="eyebrow">MountainLabs.ai · Claude Code Telemetry</div>
      <h1>Token &amp; Cost Report</h1>
    </div>
    <div class="rangebox">
      <div class="r">__RANGE__</div>
      <div>trailing __DAYSN__ days · __NRESP__ model responses</div>
    </div>
  </header>

  <div class="note"><span>&#9888;&#65038;</span><span><b>Estimate from local session logs</b>, not official Anthropic billing. Costs apply published per-model rates to the token counts in <code>~/.claude/projects</code>. Cache reads billed at 0.1&times; input, cache writes at 1.25&times; (5-min TTL). Official figures: console.anthropic.com.</span></div>

  <div class="kpis">
    <div class="kpi accent"><div class="lbl">Est. Spend</div><div class="val">__KCOST__</div><div class="sub">__DAYSN__-day total</div></div>
    <div class="kpi"><div class="lbl">Total Tokens</div><div class="val">__KTOK__</div><div class="sub">__KTOKSUB__</div></div>
    <div class="kpi"><div class="lbl">Active Days</div><div class="val">__KDAYS__</div><div class="sub">__KDAYSSUB__</div></div>
    <div class="kpi"><div class="lbl">Top Project</div><div class="val sm">__KTOP__</div><div class="sub">__KTOPSUB__</div></div>
  </div>

  <div class="grid2">
    <div class="card"><h2>Estimated cost by day</h2><div class="chart">__CHART__</div></div>
    <div class="card"><h2>Cost by model</h2><div>__MODELS__</div></div>
  </div>

  <div class="projhead"><h2>Cost by project</h2><div class="eyebrow">click a row for the model breakdown</div></div>
  <div class="ptable">__PROJECTS__</div>

  <footer>
    <h3>Methodology &amp; rates ($ / 1M tokens)</h3>
    <div class="rates">__RATES__</div>
    <div class="disc">Token counts are de-duplicated by message ID across all local session transcripts. Cache-read tokens (context re-fed cheaply each turn) usually dominate raw token volume but are the least expensive component. Models not in the rate table fall back to Opus-tier pricing. Generated by claude-usage-report · <a href="https://mountainlabs.ai" style="color:var(--accent);text-decoration:none">MountainLabs.ai</a>.</div>
  </footer>
</div>
</body>
</html>"""


def build_html(data, days):
    day_keys = sorted(data["by_day"])
    grand_cost, grand_tok = data["grand_cost"], data["grand_tokens"]
    peak = max(day_keys, key=lambda x: data["by_day"][x])
    top = data["projects"][0]

    # model -> css var, in cost order
    models_sorted = sorted(data["model_totals"], key=lambda m: -data["model_totals"][m]["cost"])
    css_light, css_dark, mvar = [], [], {}
    for i, m in enumerate(models_sorted):
        light, dark = PALETTE[i % len(PALETTE)]
        var = "--mc{}".format(i)
        mvar[m] = var
        css_light.append("  {}:{};".format(var, light))
        css_dark.append("  {}:{};".format(var, dark))

    # daily chart — thin out labels when the window is wide
    maxd = max(data["by_day"][x] for x in day_keys)
    show_cost = len(day_keys) <= 14
    label_step = max(1, (len(day_keys) + 13) // 14)
    bars = []
    for i, x in enumerate(day_keys):
        c = data["by_day"][x]
        h = max(2, round(c / maxd * 118))
        cost_html = '<div class="cost">${:,.0f}</div>'.format(c) if show_cost else ""
        day_html = '<div class="day">{}</div>'.format(x[5:]) if i % label_step == 0 else '<div class="day">&nbsp;</div>'
        bars.append('<div class="bar" title="{}: {}">{}<div class="col" style="height:{}px"></div>{}</div>'
                    .format(x, fmt_cost(c), cost_html, h, day_html))

    # model breakdown
    maxm = data["model_totals"][models_sorted[0]]["cost"]
    mrows = []
    for m in models_sorted:
        v = data["model_totals"][m]
        pct = round(v["cost"] / grand_cost * 100)
        mrows.append(
            '<div class="mgroup"><div class="mrow"><div class="dot" style="background:var({c})"></div>'
            '<div class="mname">{name} <span class="t">{tok} tok</span></div>'
            '<div class="mcost">{cost} <span class="p">{pct}%</span></div></div>'
            '<div class="mbar"><span style="width:{w:.1f}%;background:var({c})"></span></div></div>'
            .format(c=mvar[m], name=H.escape(display_name(m)), tok=fmt_tokens(v["tokens"]),
                    cost=fmt_cost(v["cost"]), pct=pct, w=v["cost"] / maxm * 100))

    # project rows
    prows = []
    for p in data["projects"]:
        ms = sorted(p["models"].items(), key=lambda kv: -kv[1]["cost"])
        comp = "".join('<span style="width:{:.2f}%;background:var({})" title="{} {}"></span>'
                       .format(v["cost"] / p["cost"] * 100 if p["cost"] else 0, mvar.get(m, "--accent"),
                               H.escape(display_name(m)), fmt_cost(v["cost"])) for m, v in ms)
        det = []
        for m, v in ms:
            tot = v["total_tokens"] or 1
            stack = ""
            for t, op in TOKEN_TYPES:
                w = v["tokens"].get(t, 0) / tot * 100
                if w > 0:
                    stack += ('<span style="width:{:.2f}%;background:var({});opacity:{}" title="{}: {:,}"></span>'
                              .format(w, mvar.get(m, "--accent"), op, t.replace("_", " "), v["tokens"].get(t, 0)))
            det.append(
                '<div class="mline"><div class="dot" style="background:var({c})"></div>'
                '<div class="nm">{name}<div class="tk">{tok} tok</div></div>'
                '<div class="stack">{stack}</div><div class="c">{cost}</div></div>'
                .format(c=mvar.get(m, "--accent"), name=H.escape(display_name(m)),
                        tok=fmt_tokens(tot), stack=stack, cost=fmt_cost(v["cost"])))
        legend = ('<div class="legend">'
                  '<span class="li"><span class="sw" style="opacity:1"></span>output</span>'
                  '<span class="li"><span class="sw" style="opacity:.75"></span>input</span>'
                  '<span class="li"><span class="sw" style="opacity:.5"></span>cache write</span>'
                  '<span class="li"><span class="sw" style="opacity:.28"></span>cache read</span></div>')
        prows.append(
            '<details class="prow">'
            '<summary class="pmain"><div class="pname"><span class="arw">&#9654;</span>{name}</div>'
            '<div class="ptok">{tok} tok</div><div class="pcost">{cost}</div></summary>'
            '<div class="compbar">{comp}</div>'
            '<div class="detail-in">{det}{legend}</div></details>'
            .format(name=H.escape(p["project"]), tok=fmt_tokens(p["total_tokens"]),
                    cost=fmt_cost(p["cost"]), comp=comp, det="".join(det), legend=legend))

    # rates footer, only for models that appeared
    rate_rows = []
    for m in models_sorted:
        inp, out = RATES.get(m, DEFAULT_RATE)
        star = "" if m in RATES else ' <span style="color:var(--faint)">(default)</span>'
        rate_rows.append('<div><span class="rn">{}</span> · in ${:g} · out ${:g}{}</div>'
                         .format(H.escape(display_name(m)), inp, out, star))

    top_name = top["project"] if len(top["project"]) <= 22 else top["project"][:21] + "…"

    return (TEMPLATE
            .replace("__CSSVARS_LIGHT__", "\n".join(css_light))
            .replace("__CSSVARS_DARK__", "\n".join(css_dark))
            .replace("__RANGE__", day_keys[0] + " &rarr; " + day_keys[-1])
            .replace("__DAYSN__", str(days))
            .replace("__NRESP__", "{:,}".format(data["responses"]))
            .replace("__KCOST__", fmt_cost(grand_cost))
            .replace("__KTOK__", fmt_tokens(grand_tok))
            .replace("__KTOKSUB__", "{:,} total".format(grand_tok))
            .replace("__KDAYS__", str(len(day_keys)))
            .replace("__KDAYSSUB__", "peak {} · {}".format(peak[5:], fmt_cost(data["by_day"][peak])))
            .replace("__KTOP__", H.escape(top_name))
            .replace("__KTOPSUB__", "{} · {}% of spend".format(fmt_cost(top["cost"]),
                                                              round(top["cost"] / grand_cost * 100)))
            .replace("__CHART__", "\n".join(bars))
            .replace("__MODELS__", "\n".join(mrows))
            .replace("__PROJECTS__", "\n".join(prows))
            .replace("__RATES__", "\n".join(rate_rows)))


def main():
    ap = argparse.ArgumentParser(description="Generate a local Claude Code token & cost dashboard.")
    ap.add_argument("--days", type=int, default=30, help="trailing window in days (default 30)")
    ap.add_argument("--claude-dir", default=os.path.join(str(Path.home()), ".claude", "projects"),
                    help="path to Claude Code's projects log directory")
    ap.add_argument("--out", default="claude-usage-report.html", help="output HTML path")
    ap.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    args = ap.parse_args()

    claude_dir = os.path.expanduser(args.claude_dir)
    if not os.path.isdir(claude_dir):
        sys.exit("No Claude Code logs found at {}\n"
                 "If Claude Code stores data elsewhere, pass --claude-dir.".format(claude_dir))

    print("Scanning {} (last {} days)...".format(claude_dir, args.days))
    data = collect(claude_dir, args.days)
    if data is None or not data["projects"]:
        sys.exit("No usage found in the last {} days.".format(args.days))

    out_path = os.path.abspath(os.path.expanduser(args.out))
    html = build_html(data, args.days)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("  {:,} model responses · {} tokens · est. {}".format(
        data["responses"], fmt_tokens(data["grand_tokens"]), fmt_cost(data["grand_cost"])))
    print("Report written to {}".format(out_path))
    if not args.no_open:
        webbrowser.open("file://" + out_path)


if __name__ == "__main__":
    main()
