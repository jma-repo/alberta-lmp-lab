"""What-if studies on a stylized seven-zone Alberta network, priced by the SCED engine.

For each scenario the same Monte Carlo hours (load level, correlated south-west and south-east
wind, solar) are dispatched with N-1 security and losses, so differences between scenarios are
the scenario's effect and not sampling noise. Outputs: price distributions by zone, how often
each limit binds, congestion rent, renewable curtailment, and production cost. The last two
turn a congestion pattern into the benefit side of a transmission business case.

Upgrade screening uses the duals: a corridor's shadow prices, summed over the hours it binds,
are what one more MW of rating on each of its circuits would save. Corridors, not circuits,
because under N-1 the loss of one parallel circuit loads the other: uprating a single circuit
only moves the binding limit to its twin. Ranking corridors by that value picks the upgrade,
and re-dispatching with the upgrade in place checks the estimate beyond the first MW.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import yaml
from scipy.stats import beta, norm

from lmplab import sced
from lmplab.config import Settings

RENEWABLE = ("wind_sw", "wind_se", "solar")


def load_case(path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def corridors(case: dict[str, Any]) -> dict[str, str]:
    """Line name -> corridor name: circuits between the same two zones form one corridor."""
    return {name: f"{spec[0]}-{spec[1]}" for name, spec in case["lines"].items()}


def network(case: dict[str, Any], uprate: dict[str, float] | None = None) -> sced.Network:
    """The case's network, with `uprate` adding MW to every circuit of the named corridors."""
    uprate = uprate or {}
    corridor = corridors(case)
    lines = []
    for name, (start, end, x, rating, emergency, r) in case["lines"].items():
        extra = float(uprate.get(corridor[name], 0.0))
        lines.append(sced.Line(name, start, end, float(x), float(rating) + extra, float(emergency) + extra, r=float(r)))
    return sced.Network(buses=list(case["zones"]), lines=lines, reference=case["reference"])


def offers(case: dict[str, Any], hour: dict[str, float], add_capacity: dict[str, float] | None = None) -> list[sced.Offer]:
    add_capacity = add_capacity or {}
    out = []
    for name, (zone, mw, price, profile) in case["units"].items():
        capacity = float(mw) + float(add_capacity.get(name, 0.0))
        available = capacity if profile == "firm" else capacity * float(hour[profile])
        out.append(sced.Offer(name, zone, available, float(price)))
    return out


def loads(case: dict[str, Any], hour: dict[str, float]) -> dict[str, float]:
    noise = hour.get("zone_factors", {})
    return {zone: spec["load"] * hour["load"] * noise.get(zone, 1.0) for zone, spec in case["zones"].items()}


def sample_hours(case: dict[str, Any], seed: int) -> list[dict[str, Any]]:
    mc = case["monte_carlo"]
    rng = np.random.default_rng(seed)
    n = int(mc["samples"])
    low, high = mc["load_range"]
    level = np.clip(rng.normal(1.0, mc["load_sigma"], n), low, high)
    rho = float(mc["wind_correlation"])
    z = rng.multivariate_normal([0.0, 0.0], [[1.0, rho], [rho, 1.0]], n)
    a, b = mc["wind_beta"]
    wind = beta.ppf(norm.cdf(z), a, b)
    daylight = rng.random(n) < mc["daylight_share"]
    solar = np.where(daylight, rng.uniform(*mc["solar_range"], n), 0.0)
    zones = list(case["zones"])
    hours = []
    for i in range(n):
        factors = dict(zip(zones, rng.normal(1.0, mc["zone_noise"], len(zones)).round(4), strict=True))
        hours.append({"load": float(level[i]), "wind_sw": float(wind[i, 0]), "wind_se": float(wind[i, 1]),
                      "solar": float(solar[i]), "zone_factors": factors})
    return hours


def dispatch_hour(case: dict[str, Any], scenario: dict[str, Any], hour: dict[str, Any]) -> sced.Dispatch:
    return sced.solve(network(case, scenario.get("uprate")), offers(case, hour, scenario.get("add_capacity")),
                      loads(case, hour), contingencies="all", losses=True)


def summarize(case: dict[str, Any], scenario: dict[str, Any], hours: list[dict[str, Any]]) -> dict[str, Any]:
    zones = list(case["zones"])
    renewable_units = [name for name, spec in case["units"].items() if spec[3] in RENEWABLE]
    threshold = float(case["monte_carlo"]["separation_threshold"])
    prices, alps, rents, costs, curtailed, losses, binding = [], [], [], [], [], [], {}
    corridor = corridors(case)
    value = dict.fromkeys(corridor.values(), 0.0)
    for hour in hours:
        result = dispatch_hour(case, scenario, hour)
        demand = loads(case, hour)
        lmp = np.array([result.lmp[z] for z in zones])
        weights = np.array([demand[z] for z in zones])
        prices.append(lmp)
        alps.append(float(lmp @ weights / weights.sum()))
        rents.append(result.surplus)
        costs.append(result.cost)
        losses.append(result.losses_mw)
        available = {o.unit: o.mw for o in offers(case, hour, scenario.get("add_capacity"))}
        curtailed.append(sum(available[u] - result.dispatch.get(u, 0.0) for u in renewable_units))
        for limit in result.binding:
            label = limit["line"] + (f" | loss of {limit['contingency']}" if limit["contingency"] else "")
            binding[label] = binding.get(label, 0) + 1
            value[corridor[limit["line"]]] += limit["shadow_price"]
    prices_arr = np.array(prices)
    alp = np.array(alps)
    n = len(hours)
    return {
        "zones": {z: {"mean": round(float(prices_arr[:, i].mean()), 2),
                      "p10": round(float(np.percentile(prices_arr[:, i], 10)), 2),
                      "p90": round(float(np.percentile(prices_arr[:, i], 90)), 2),
                      "separated_share": round(float((np.abs(prices_arr[:, i] - alp) > threshold).mean()), 3),
                      "histogram": np.histogram(prices_arr[:, i], bins=np.arange(-5, 105, 5))[0].tolist()}
                  for i, z in enumerate(zones)},
        "alp_mean": round(float(alp.mean()), 2),
        "binding_share": {k: round(v / n, 3) for k, v in sorted(binding.items(), key=lambda kv: -kv[1])},
        # $k per MW of extra rating (on every circuit of the corridor) per year: mean hourly shadow price x 8,760 h
        "rating_value_kusd_per_mw_year": {k: round(v / n * 8760 / 1000, 1)
                                          for k, v in sorted(value.items(), key=lambda kv: -kv[1]) if v > 0},
        "congestion_rent_per_hour": round(float(np.mean(rents)), 0),
        "production_cost_per_hour": round(float(np.mean(costs)), 0),
        "curtailment_mw": round(float(np.mean(curtailed)), 1),
        "losses_mw": round(float(np.mean(losses)), 1),
        "samples": n,
    }


def showcase(case: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, hour in case["showcase_hours"].items():
        result = dispatch_hour(case, {}, hour)
        out.append({"name": name, "hour": hour, "prices": result.table(), "binding": result.binding,
                    "flows": {k: round(v, 1) for k, v in result.flows.items()},
                    "dispatch": {k: round(v, 1) for k, v in result.dispatch.items()},
                    "losses_mw": round(result.losses_mw, 1), "contingencies": len(result.contingencies)})
    return out


def run(settings: Settings) -> dict[str, Any]:
    case = load_case(settings.paths.configs / "alberta7.yaml")
    hours = sample_hours(case, settings.seed)
    scenarios: dict[str, dict[str, Any]] = {}
    for name, spec in case["scenarios"].items():
        spec = dict(spec or {})
        uprate = dict(spec.get("uprate", {}))
        if "auto" in uprate:
            # The corridor whose rating value rose most from the first scenario to the previous one.
            first, last = (scenarios[key]["rating_value_kusd_per_mw_year"] for key in (next(iter(scenarios)), list(scenarios)[-1]))
            top = max(last, key=lambda c: last[c] - first.get(c, 0.0))
            uprate = {top: uprate.pop("auto")}
            spec["uprate"] = uprate
        scenarios[name] = {**summarize(case, spec, hours), "spec": spec}
    base, wind, ranked, other = scenarios.values()

    def savings(before: dict[str, Any], after: dict[str, Any]) -> float:
        return round((before["production_cost_per_hour"] - after["production_cost_per_hour"]) * 8760 / 1e6, 1)

    top_line, uprate_mw = next(iter(ranked["spec"]["uprate"].items()))
    deltas = {
        "se_price_drop_with_wind": round(base["zones"]["SE"]["mean"] - wind["zones"]["SE"]["mean"], 2),
        "curtailment_added_gwh_per_year": round((wind["curtailment_mw"] - base["curtailment_mw"]) * 8.76, 1),
        "wind_production_cost_savings_musd_per_year": savings(base, wind),
        "top_ranked_line": top_line, "uprate_mw": uprate_mw,
        "dual_estimate_musd_per_year": round(wind["rating_value_kusd_per_mw_year"][top_line] * uprate_mw / 1000, 1),
        "upgrade_savings_musd_per_year": savings(wind, ranked),
        "upgrade_curtailment_avoided_gwh_per_year": round((wind["curtailment_mw"] - ranked["curtailment_mw"]) * 8.76, 1),
        "other_upgrade_line": next(iter(other["spec"]["uprate"])),
        "other_upgrade_savings_musd_per_year": savings(wind, other),
    }
    lines = [{"name": name, "from": spec[0], "to": spec[1], "rating": spec[3], "emergency": spec[4]}
             for name, spec in case["lines"].items()]
    report = {
        "aeso_examples": sced.aeso_examples(),
        "case": {"reference": case["reference"], "zones": case["zones"], "lines": lines,
                 "units": {k: {"zone": v[0], "mw": v[1], "offer": v[2], "profile": v[3]} for k, v in case["units"].items()}},
        "showcase": showcase(case),
        "scenarios": scenarios,
        "deltas": deltas,
    }
    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    (settings.paths.reports / "sced.json").write_text(json.dumps(report, indent=1), encoding="utf-8", newline="\n")
    return report
