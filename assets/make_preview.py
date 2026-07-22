#!/usr/bin/env python3
"""Regenerate the README preview images from sample data.

Renders the dashboard with fabricated-but-realistic numbers (so no real
usage data ends up in the repo), then screenshots it with headless Chrome:

    python3 assets/make_preview.py
    # -> assets/demo.html (gitignored), assets/preview-light.png, assets/preview-dark.png
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from claude_usage import build_html, rate  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# project -> model -> raw token counts (input, output, cache_write, cache_read)
SAMPLE = {
    "acme-website": {
        "claude-opus-4-8": (2_100_000, 940_000, 7_600_000, 96_000_000),
        "claude-sonnet-5": (410_000, 130_000, 1_100_000, 14_500_000),
    },
    "data-pipeline": {
        "claude-opus-4-8": (1_300_000, 610_000, 4_900_000, 61_000_000),
        "claude-haiku-4-5": (95_000, 41_000, 380_000, 5_200_000),
    },
    "mobile-app": {
        "claude-sonnet-5": (880_000, 320_000, 2_700_000, 33_000_000),
        "claude-opus-4-8": (240_000, 88_000, 900_000, 9_800_000),
    },
    "internal-tools": {
        "claude-opus-4-8": (410_000, 170_000, 1_500_000, 17_000_000),
    },
    "docs": {
        "claude-haiku-4-5": (140_000, 52_000, 430_000, 4_100_000),
    },
}

# relative daily weights over a two-week window (weekend dips)
DAY_WEIGHTS = [4, 9, 11, 7, 10, 2, 1, 6, 12, 9, 8, 11, 3, 5]


def main():
    keys = ("input", "output", "cache_write", "cache_read")
    projects, model_totals = [], {}
    grand_cost = grand_tok = 0
    for proj, models in SAMPLE.items():
        p_models, p_tok, p_cost = {}, 0, 0.0
        for model, raw in models.items():
            toks = dict(zip(keys, raw))
            r = rate(model)
            c = sum(toks[k] * r[k] / 1e6 for k in keys)
            t = sum(toks.values())
            p_models[model] = {"tokens": toks, "total_tokens": t, "cost": c}
            p_tok += t
            p_cost += c
            mt = model_totals.setdefault(model, {"tokens": 0, "cost": 0.0})
            mt["tokens"] += t
            mt["cost"] += c
        projects.append({"project": proj, "total_tokens": p_tok, "cost": p_cost, "models": p_models})
        grand_cost += p_cost
        grand_tok += p_tok
    projects.sort(key=lambda p: -p["cost"])

    scale = grand_cost / sum(DAY_WEIGHTS)
    by_day = {"2026-07-{:02d}".format(7 + i): w * scale for i, w in enumerate(DAY_WEIGHTS)}

    data = {
        "projects": projects,
        "model_totals": model_totals,
        "by_day": by_day,
        "grand_cost": grand_cost,
        "grand_tokens": grand_tok,
        "responses": 3421,
    }

    html = build_html(data, 14)
    # Headless Chrome inherits the OS appearance, so pin each variant explicitly:
    # light strips the dark media block; dark promotes its tokens to :root.
    import re
    pat = r"@media \(prefers-color-scheme:dark\)\{:root\{(.*?)\}\}"
    m = re.search(pat, html, re.S)
    dark_html = html.replace("</head>", "<style>:root{" + m.group(1) + "}</style></head>", 1)
    light_html = re.sub(pat, "", html, count=1, flags=re.S)

    shots = []
    for name, doc in (("demo.html", light_html), ("demo-dark.html", dark_html)):
        path = os.path.join(HERE, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print("wrote", path)
        shots.append(path)

    if not os.path.exists(CHROME):
        sys.exit("Chrome not found — open assets/demo.html and screenshot manually.")
    for demo, name in zip(shots, ("preview-light.png", "preview-dark.png")):
        out = os.path.join(HERE, name)
        subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--force-device-scale-factor=2",
             "--window-size=1120,1210", "--screenshot=" + out, "file://" + demo],
            check=True, capture_output=True)
        print("wrote", out)


if __name__ == "__main__":
    main()
