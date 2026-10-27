"""Assemble every report into one dashboard page.

The page embeds its data, so it opens from disk or GitHub Pages with no server.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lmplab.areas import AREAS, HUB_REGION, NAMES, adjacency, map_position
from lmplab.config import Settings

STATIC = Path(__file__).parent / "static"
ZONE_XY = {"NW": (1.0, 6.4), "NE": (4.3, 6.6), "EDM": (2.9, 5.1), "CEN": (3.1, 3.6),
           "CAL": (2.5, 2.4), "SW": (2.1, 1.0), "SE": (4.4, 1.5)}   # schematic positions, north up


def _read(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _reference_library(settings: Settings) -> dict[str, Any]:
    """Points of the synthetic accepted-model library behind the anomaly check, for the scatter."""
    from lmplab import pdup

    library = pdup.reference_library(settings.seed)
    frame = library[("cable", "34.5")]
    return {"group": "34.5 kV cable", "log_r": frame["log_r"].round(4).tolist(),
            "log_x": frame["log_x"].round(4).tolist(), "log_b": frame["log_b"].round(4).tolist()}


def collect(settings: Settings) -> dict[str, Any]:
    reports = settings.paths.reports
    analytics = _read(reports / "analytics.json", {})
    pdup = _read(reports / "pdup.json", {"reviews": []})
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "areas": [{"area_id": a, "name": NAMES[a], "hub_region": HUB_REGION[a], "x": map_position(a)[0],
                   "y": map_position(a)[1]} for a in AREAS],
        "links": sorted([list(pair) for pair in adjacency()]),
        "quality": _read(reports / "data_quality.json", {}),
        "analytics": analytics,
        "forecast": _read(reports / "forecast.json", {}),
        "sced": _read(reports / "sced.json", {}),
        "zone_xy": ZONE_XY,
        "pdup": pdup,
        "reference_library": _reference_library(settings) if pdup["reviews"] else None,
        "agent": _read(reports / "agent.json", {"transcripts": []}),
        "sources": settings.config["sources"],
        "config": {"analytics": settings.config["analytics"], "forecast": settings.config["forecast"]},
    }


def run(settings: Settings) -> Path:
    from plotly.offline import get_plotlyjs

    site = settings.paths.site
    assets = site / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("app.js", "styles.css", "favicon.svg"):
        shutil.copyfile(STATIC / name, assets / name)
    plotly_js = assets / "plotly.min.js"
    bundled = get_plotlyjs().encode("utf-8")
    if not plotly_js.exists() or plotly_js.read_bytes() != bundled:
        plotly_js.write_bytes(bundled)  # bytes: text mode on Windows would add CRLF to a vendored file

    data = collect(settings)
    environment = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape(["html"]))
    page = environment.get_template("template.html").render(
        # "</" cannot appear inside the inline JSON block, so a string in the data cannot close it.
        data_json=json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/"),
        generated_at=data["generated_at"],
        # Content hashes in asset URLs: a redeploy reaches visitors at once instead of after the CDN cache expires.
        v={name: hashlib.sha256((assets / name).read_bytes()).hexdigest()[:10]
           for name in ("app.js", "styles.css", "plotly.min.js")},
    )
    (site / "index.html").write_text(page, encoding="utf-8", newline="\n")
    (site / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: serve assets as-is
    return site / "index.html"
