"""LMP analytics on the 2025 backcast: price separation, settlement surplus, and pricing hubs.

Definitions
  ALP          Alberta Load Price: load-weighted average of area LMPs (what most consumers pay)
  basis        area LMP - ALP; negative where generation is trapped behind a constraint
  separated    |basis| above the configured threshold ($5/MWh by default)
  surplus      ALP x load - sum(LMP x generation): what a uniform load price collects beyond what
               generators are paid at their own LMP. It is the money that funds financial
               transmission rights, and the area-level analogue of AESO's congestion rent.
"""

from __future__ import annotations

import json
from itertools import combinations
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering

from lmplab.areas import AREAS, HUB_REGION, NAMES, adjacency
from lmplab.config import Settings


def load_panel(settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    con = duckdb.connect(str(settings.paths.warehouse), read_only=True)
    try:
        hourly = con.execute("SELECT * FROM v_hour ORDER BY ts, area_id").df()
        system = con.execute("SELECT * FROM v_system ORDER BY ts").df()
    finally:
        con.close()
    hourly = hourly.merge(system[["ts", "alp"]], on="ts")
    hourly["basis"] = hourly["lmp"] - hourly["alp"]
    return hourly, system


def price_matrix(hourly: pd.DataFrame) -> pd.DataFrame:
    """Hours x areas LMP matrix."""
    return hourly.pivot(index="ts", columns="area_id", values="lmp")[AREAS]


# --- area and system statistics -------------------------------------------------------------

def area_stats(hourly: pd.DataFrame, threshold: float) -> list[dict[str, Any]]:
    rows = []
    for area_id, group in hourly.groupby("area_id"):
        basis = group["basis"]
        rows.append({
            "area_id": int(area_id), "name": NAMES[area_id], "hub_region": HUB_REGION[area_id],
            "region": group["region"].iloc[0],
            "mean_lmp": round(float(group["lmp"].mean()), 2),
            "mean_basis": round(float(basis.mean()), 2),
            "p05_basis": round(float(basis.quantile(0.05)), 2),
            "p95_basis": round(float(basis.quantile(0.95)), 2),
            "separated_share": round(float((basis.abs() > threshold).mean()), 4),
            "below_share": round(float((basis < -threshold).mean()), 4),
            "above_share": round(float((basis > threshold).mean()), 4),
            "mean_load_mw": round(float(group["load_mw"].mean()), 1),
            "mean_gen_mw": round(float(group["sysgen_mw"].mean()), 1),
            "mean_net_gen_mw": round(float(group["net_gen_mw"].mean()), 1),
            "gen_weighted_lmp": round(float((group["lmp"] * group["sysgen_mw"].clip(lower=0)).sum()
                                            / max(group["sysgen_mw"].clip(lower=0).sum(), 1e-9)), 2),
        })
    return rows


def settlement_surplus(hourly: pd.DataFrame) -> pd.DataFrame:
    """Hourly surplus of a uniform load price over nodal generator payments ($)."""
    gen = hourly["sysgen_mw"].clip(lower=0)
    load = hourly["load_mw"].clip(lower=0)
    frame = hourly.assign(load_pay=hourly["alp"] * load, gen_pay=hourly["lmp"] * gen,
                          gen_at_alp=hourly["alp"] * gen)
    per_hour = frame.groupby("ts")[["load_pay", "gen_pay", "gen_at_alp"]].sum()
    # Generation priced at ALP minus at its own LMP: the congestion (and loss) discount generators bear.
    per_hour["surplus"] = per_hour["gen_at_alp"] - per_hour["gen_pay"]
    return per_hour


def separation_events(hourly: pd.DataFrame, system: pd.DataFrame, top: int = 12) -> list[dict[str, Any]]:
    spread = system.assign(spread=system["lmp_max"] - system["lmp_min"]).nlargest(top, "spread")
    events = []
    for row in spread.itertuples():
        hour = hourly[hourly["ts"] == row.ts]
        low, high = hour.nsmallest(1, "lmp").iloc[0], hour.nlargest(1, "lmp").iloc[0]
        regional = hour.groupby("hub_region")["net_gen_mw"].sum()
        events.append({
            "ts": row.ts.strftime("%Y-%m-%d %H:00"), "spread": round(float(row.spread), 1), "alp": round(float(row.alp), 1),
            "low_area": f"{int(low.area_id)} {NAMES[int(low.area_id)]}", "low_lmp": round(float(low.lmp), 1),
            "high_area": f"{int(high.area_id)} {NAMES[int(high.area_id)]}", "high_lmp": round(float(high.lmp), 1),
            "system_load_mw": round(float(row.load_mw), 0),
            "regional_net_gen_mw": {k: round(float(v), 0) for k, v in regional.items()},
        })
    return events


# --- pricing hubs --------------------------------------------------------------------------

def hub_prices(prices: pd.DataFrame, assignment: dict[int, str]) -> pd.DataFrame:
    """Hourly hub price = simple average of member-area LMPs (AESO averages representative buses)."""
    groups: dict[str, list[int]] = {}
    for area, hub in assignment.items():
        groups.setdefault(hub, []).append(area)
    return pd.DataFrame({hub: prices[members].mean(axis=1) for hub, members in groups.items()})


def hub_metrics(prices: pd.DataFrame, assignment: dict[int, str]) -> dict[str, Any]:
    """AESO's two hub criteria: basis deviation (lower = better fit) and minimum separation
    between hubs (higher = clearer signal), both on hourly prices in $/MWh."""
    hubs = hub_prices(prices, assignment)
    deviation = np.mean([np.abs(prices[area] - hubs[hub]).mean() for area, hub in assignment.items()])
    pairs = [float(np.abs(hubs[a] - hubs[b]).mean()) for a, b in combinations(hubs.columns, 2)]
    return {
        "hubs": int(hubs.shape[1]),
        "basis_deviation": round(float(deviation), 3),
        "min_separation": round(min(pairs), 3) if pairs else 0.0,
        "hub_mean_price": {hub: round(float(hubs[hub].mean()), 2) for hub in hubs.columns},
    }


MIN_HUB_AREAS = 3  # a one-area "hub" has no liquidity; small clusters merge into their closest neighbour


def area_distance(prices: pd.DataFrame) -> np.ndarray:
    """Mean absolute hourly LMP difference between every pair of areas ($/MWh)."""
    values = prices[AREAS].to_numpy()
    return np.mean(np.abs(values[:, :, None] - values[:, None, :]), axis=0)


def cluster_hubs(prices: pd.DataFrame, k: int, distance: np.ndarray | None = None,
                 loads: dict[int, float] | None = None) -> dict[int, str]:
    """Contiguous hubs from Ward clustering of the areas' hourly price series, restricted to
    neighbouring areas so each hub is one connected region. Clusters smaller than
    MIN_HUB_AREAS are folded into their closest neighbour, starting from more clusters
    when needed so that k liquid hubs remain."""
    distance = area_distance(prices) if distance is None else distance
    index = {area: position for position, area in enumerate(AREAS)}
    connectivity = np.zeros((len(AREAS), len(AREAS)))
    for a, b in adjacency():
        connectivity[index[a], index[b]] = connectivity[index[b], index[a]] = 1
    series = prices[AREAS].to_numpy().T

    def gap(first: list[int], second: list[int]) -> float:
        return float(distance[np.ix_([index[a] for a in first], [index[b] for b in second])].mean())

    def neighbours(groups: dict[int, list[int]], label: int) -> list[int]:
        return [other for other in groups if other != label
                and connectivity[np.ix_([index[a] for a in groups[label]], [index[b] for b in groups[other]])].any()]

    for start in range(k, len(AREAS) + 1):
        labels = AgglomerativeClustering(n_clusters=start, linkage="ward", connectivity=connectivity).fit_predict(series)
        groups = {int(label): [AREAS[i] for i in np.flatnonzero(labels == label)] for label in set(labels)}
        while len(groups) > 1:
            small = [label for label, members in groups.items() if len(members) < MIN_HUB_AREAS]
            if not small:
                break
            label = min(small, key=lambda item: len(groups[item]))
            target = min(neighbours(groups, label), key=lambda other: gap(groups[label], groups[other]))
            groups[target] += groups.pop(label)
        if len(groups) >= k:
            break
    while len(groups) > k:  # fold the closest pair of neighbouring hubs together
        first, second = min(((a, b) for a in groups for b in neighbours(groups, a) if a < b),
                            key=lambda pair: gap(groups[pair[0]], groups[pair[1]]))
        groups[first] += groups.pop(second)
    return name_hubs(list(groups.values()), loads or {})


def name_hubs(groups: list[list[int]], loads: dict[int, float]) -> dict[int, str]:
    """Name each hub after its two largest load centres (areas belong to one hub, so names are unique)."""
    assignment = {}
    for members in groups:
        anchors = sorted(members, key=lambda a: (-loads.get(a, 0.0), a))[:2]
        name = " / ".join(NAMES[a] for a in anchors)
        for area in members:
            assignment[area] = name
    return assignment


def hub_study(prices: pd.DataFrame, ks: list[int], regions_by_area: dict[int, str],
              loads: dict[int, float]) -> dict[str, Any]:
    scenarios = []
    seven = {a: HUB_REGION[a] for a in AREAS}
    six = {a: regions_by_area[a] for a in AREAS}
    two = {a: "South" if HUB_REGION[a] in ("Southwest", "Southeast") else "North" for a in AREAS}
    for name, assignment, kind in (("7 regional hubs (South split)", seven, "predefined"),
                                   ("6 planning-region hubs", six, "predefined"),
                                   ("2 predefined hubs (North / South)", two, "predefined")):
        scenarios.append({"name": name, "kind": kind, **hub_metrics(prices, assignment), "assignment": assignment})
    distance = area_distance(prices)
    for k in ks:
        assignment = cluster_hubs(prices, k, distance, loads)
        scenarios.append({"name": f"{k} clustered hubs", "kind": "clustered", "k": k,
                          **hub_metrics(prices, assignment), "assignment": assignment})
    return {"scenarios": scenarios, "min_hub_areas": MIN_HUB_AREAS,
            "aeso_reference": {"2 predefined hubs": {"min_separation": 4.5, "basis_deviation": 1.59},
                               "2 optimized hubs": {"min_separation": 3.5, "basis_deviation": 1.38},
                               "6 predefined hubs": {"min_separation": 0.96, "basis_deviation": 1.19}}}


# --- pipeline step ---------------------------------------------------------------------------

def run(settings: Settings) -> dict[str, Any]:
    cfg = settings.config["analytics"]
    hourly, system = load_panel(settings)
    prices = price_matrix(hourly)
    threshold = float(cfg["separation_threshold"])

    stats = area_stats(hourly, threshold)
    surplus = settlement_surplus(hourly)
    monthly_surplus = surplus["surplus"].groupby(surplus.index.to_period("M")).sum()
    system = system.assign(month=system["ts"].dt.strftime("%Y-%m"), spread=system["lmp_max"] - system["lmp_min"])
    hourly["month"] = hourly["ts"].dt.strftime("%Y-%m")
    monthly_area = hourly.groupby(["area_id", "month"])["lmp"].mean().unstack()
    separated = (hourly.assign(sep=hourly["basis"].abs() > threshold).groupby("ts")["sep"].any())
    regions_by_area = hourly.groupby("area_id")["region"].first().to_dict()
    loads = hourly.groupby("area_id")["load_mw"].mean().to_dict()
    hubs = hub_study(prices, list(cfg["hub_k"]), regions_by_area, loads)

    percentiles = np.linspace(0, 100, 51)
    duration = {int(a): np.percentile(prices[a], 100 - percentiles).round(2).tolist() for a in AREAS}
    basis = hourly.pivot(index="ts", columns="area_id", values="basis")
    basis_duration = {int(a): np.percentile(basis[a], 100 - percentiles).round(2).tolist() for a in AREAS}
    daily = system.set_index("ts").resample("D").agg({"alp": "mean", "lmp_min": "min", "lmp_max": "max", "load_mw": "mean"})

    summary = {
        "hours": int(len(system)),
        "annual_alp": round(float((system["alp"] * system["load_mw"]).sum() / system["load_mw"].sum()), 2),
        "mean_alp": round(float(system["alp"].mean()), 2),
        "hours_any_separation": int(separated.sum()),
        "share_any_separation": round(float(separated.mean()), 4),
        "mean_spread": round(float(system["spread"].mean()), 2),
        "annual_surplus_musd": round(float(surplus["surplus"].sum() / 1e6), 1),
        "aeso_reported_congestion_rent_musd": 91.0,
        "cheapest_area": min(stats, key=lambda r: r["mean_lmp"]),
        "dearest_area": max(stats, key=lambda r: r["mean_lmp"]),
        "threshold": threshold,
    }
    report = {
        "summary": summary,
        "areas": stats,
        "monthly_alp": system.groupby("month").apply(lambda g: (g["alp"] * g["load_mw"]).sum() / g["load_mw"].sum(),
                                                       include_groups=False).round(2).to_dict(),
        "monthly_area_lmp": {int(a): monthly_area.loc[a].round(2).to_dict() for a in AREAS},
        "monthly_surplus_musd": {str(k): round(float(v) / 1e6, 2) for k, v in monthly_surplus.items()},
        "duration": {"percentiles": percentiles.round(1).tolist(), "areas": duration},
        "basis_duration": {"percentiles": percentiles.round(1).tolist(), "areas": basis_duration},
        "daily": {"date": [d.strftime("%Y-%m-%d") for d in daily.index], "alp": daily["alp"].round(2).tolist(),
                  "min": daily["lmp_min"].round(2).tolist(), "max": daily["lmp_max"].round(2).tolist()},
        "events": separation_events(hourly, system),
        "hubs": hubs,
    }
    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    (settings.paths.reports / "analytics.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8", newline="\n")
    return summary
