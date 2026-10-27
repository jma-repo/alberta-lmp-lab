"""PDUP model validation: a structured, repeatable first-pass review of an interconnection model.

The checks a reviewer runs by hand, written as rules with IDs (the checklist), followed by the
studies that need a solver:

  data and topology   required fields, unique names, every bus reaches the POI, branch voltages
                      agree with bus voltages, the collector system is grounded
  lines               R, X, B per km for the voltage class and construction, surge impedance,
                      X/R, seasonal ratings, rating against the power the line must carry
  transformers        impedance on its own base, X/R, tap changer, vector group
  generators          MBASE, Pmax, reactive capability
  dynamic models      required WECC models present, parameters in range, time constants safe for
                      the integration step, current limit covers P and Q, droop and deadband,
                      static and dynamic reactive limits agree
  power flow          AC convergence with the data as submitted and, when it fails, which suspected
                      error restores it; reactive capability at the POI; voltages and loading
  short circuit       short-circuit ratio at the POI (the EMT-study trigger), collector fault level
  sensitivity         how the POI reactive margin moves with each uncertain parameter
  anomaly detection   an isolation forest trained on a library of accepted models flags parameter
                      combinations that no single range check catches

Every finding carries its evidence, a probable cause when one can be inferred (ohms typed into a
p.u. field, an impedance on the wrong MVA base, swapped seasonal ratings), the required action
and the study impact, so the letter to the market participant follows directly from the findings.
The submissions and the reference library in this repository are synthetic.
"""

from __future__ import annotations

import copy
import json
import math
import warnings
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from sklearn.ensemble import IsolationForest

from lmplab.config import Settings

RULES = {
    "DATA-01": ("Required fields are present", "Data"),
    "DATA-02": ("Element names are unique", "Data"),
    "TOP-01": ("Every bus connects to the POI", "Topology"),
    "TOP-02": ("Branch voltages match bus voltages", "Topology"),
    "TOP-03": ("Collector system is grounded", "Topology"),
    "LINE-01": ("R and X per km fit the voltage class and construction", "Lines"),
    "LINE-02": ("B per km fits the voltage class and construction", "Lines"),
    "LINE-03": ("Surge impedance is plausible", "Lines"),
    "LINE-04": ("X/R is plausible (overhead lines)", "Lines"),
    "LINE-05": ("Seasonal ratings are present and ordered", "Lines"),
    "LINE-06": ("Rating covers the power the line carries", "Lines"),
    "XF-01": ("Impedance is plausible on the transformer's own base", "Transformers"),
    "XF-02": ("X/R is plausible", "Transformers"),
    "XF-03": ("Tap changer data are consistent", "Transformers"),
    "XF-04": ("Vector group is declared", "Transformers"),
    "GEN-01": ("MBASE and Pmax are consistent", "Generators"),
    "GEN-02": ("Total Pmax matches the plant capacity", "Generators"),
    "GEN-03": ("Reactive capability at the terminals", "Generators"),
    "DYN-01": ("Required dynamic models are present", "Dynamic models"),
    "DYN-02": ("Parameters are within screening ranges", "Dynamic models"),
    "DYN-03": ("Time constants are numerically safe", "Dynamic models"),
    "DYN-04": ("Current limit covers rated P with required Q", "Dynamic models"),
    "DYN-05": ("Frequency droop and deadband", "Dynamic models"),
    "DYN-06": ("Dynamic and static reactive limits agree", "Dynamic models"),
    "PF-01": ("AC power flow converges as submitted", "Power flow"),
    "PF-02": ("Reactive capability at the POI", "Power flow"),
    "PF-03": ("Voltages and loading within limits", "Power flow"),
    "SC-01": ("Short-circuit ratio at the POI", "Short circuit"),
    "SC-02": ("Collector fault level within switchgear rating", "Short circuit"),
    "ML-01": ("Parameters look like accepted models", "Anomaly detection"),
}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
# Which deficiencies to name first in a summary: those that block studies, then those that change results.
MATERIALITY = ["PF-01", "PF-02", "TOP-02", "XF-01", "LINE-01", "DYN-04", "DYN-05", "TOP-03", "SC-02", "LINE-05", "TOP-01"]
FAILURE = {   # how a failed rule reads in a summary sentence
    "PF-01": "the case does not solve as submitted", "PF-02": "a reactive capability shortfall at the POI",
    "TOP-02": "a winding voltage that does not match its bus", "XF-01": "a transformer impedance on the wrong MVA base",
    "LINE-01": "a line impedance outside the plausible range", "LINE-02": "line charging outside the plausible range",
    "DYN-04": "a converter current limit too low for rated output", "DYN-05": "frequency response settings outside requirements",
    "TOP-03": "an ungrounded collector system", "SC-02": "a fault level above the switchgear rating",
    "LINE-05": "seasonal ratings out of order", "TOP-01": "an islanded bus", "DATA-01": "missing data",
    "DATA-02": "duplicate element names", "XF-03": "inconsistent tap changer data", "GEN-01": "inconsistent MBASE or Pmax",
    "DYN-01": "missing dynamic models",
}


@dataclass
class Finding:
    rule: str
    severity: str                      # error = deficiency, warning = clarification, info
    element: str
    message: str
    action: str
    impact: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    probable_cause: str | None = None
    fix: dict[str, Any] | None = None  # {"kind": "line" | "transformer", "name", "field", "value"}


def load_rules(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_submission(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def z_base(kv: float, base_mva: float) -> float:
    return kv * kv / base_mva


def voltage_class(table: dict[str, Any], kv: float) -> str | None:
    """The screening class nearest to kv, if one is within 15%."""
    best = min(table, key=lambda key: abs(float(key) - kv), default=None)
    return best if best is not None and abs(float(best) - kv) <= 0.15 * kv else None


def per_km(line: dict[str, Any], base_mva: float) -> dict[str, float]:
    """Per-km values of one circuit (impedances are given per circuit; `parallel` identical circuits)."""
    zb, length = z_base(float(line["kv"]), base_mva), float(line["length_km"])
    r, x = line["r_pu"] * zb / length, line["x_pu"] * zb / length
    b = line["b_pu"] / zb / length * 1e6
    return {"r": r, "x": x, "b": b, "xr": x / r if r else math.inf, "zc": math.sqrt(x / (b * 1e-6)) if b > 0 else math.inf}


def circuits(line: dict[str, Any]) -> int:
    return int(line.get("parallel", 1))


def _outside(value: float, bounds: list[float]) -> bool:
    return value < bounds[0] or value > bounds[1]


def _fmt(bounds: list[float]) -> str:
    return f"{bounds[0]:g}-{bounds[1]:g}"


# --- static checks ---------------------------------------------------------------------------

REQUIRED = {
    "buses": ["name", "kv"],
    "lines": ["name", "from", "to", "kv", "construction", "length_km", "r_pu", "x_pu", "b_pu", "rating_mva"],
    "transformers": ["name", "role", "hv", "lv", "kv_hv", "kv_lv", "mva", "z_pct", "x_over_r"],
    "generators": ["name", "bus", "units", "unit_mva", "mbase_mva", "pmax_mw", "qmax_mvar", "qmin_mvar", "dynamics"],
}


def check_data(sub: dict[str, Any]) -> list[Finding]:
    findings = []
    for table, columns in REQUIRED.items():
        for row in sub.get(table, []):
            missing = [c for c in columns if row.get(c) is None]
            if missing:
                findings.append(Finding("DATA-01", "error", row.get("name", f"{table} row"),
                                        f"Missing {', '.join(missing)}.", "Provide the missing fields.",
                                        "The element cannot be modelled."))
    for group in (("buses",), ("lines", "transformers", "generators")):
        names = [row.get("name") for table in group for row in sub.get(table, [])]
        for name in sorted({n for n in names if names.count(n) > 1}, key=str):
            findings.append(Finding("DATA-02", "error", str(name), "Name used by more than one element.",
                                    "Give every element a unique name.", "Results cannot be traced to equipment."))
    return findings


def merge_duplicates(findings: list[Finding]) -> list[Finding]:
    """One finding per (rule, message): generator groups that share a parameter set report once."""
    merged: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        key = (finding.rule, finding.severity, finding.message)
        if key in merged:
            merged[key].element += f", {finding.element}"
        else:
            merged[key] = copy.deepcopy(finding)
    return list(merged.values())


def branches(sub: dict[str, Any]) -> list[tuple[str, str, str]]:
    return ([(line["name"], line["from"], line["to"]) for line in sub["lines"]]
            + [(xf["name"], xf["hv"], xf["lv"]) for xf in sub["transformers"]])


def reachable(sub: dict[str, Any], skip: str | None = None) -> set[str]:
    """Buses connected to the POI, optionally with one branch out of service."""
    links: dict[str, set[str]] = {bus["name"]: set() for bus in sub["buses"]}
    for name, a, b in branches(sub):
        if name != skip and a in links and b in links:
            links[a].add(b)
            links[b].add(a)
    start = sub["system_equivalent"]["bus"]
    seen, todo = {start}, deque([start])
    while todo:
        for nxt in links[todo.popleft()] - seen:
            seen.add(nxt)
            todo.append(nxt)
    return seen


def check_topology(sub: dict[str, Any]) -> list[Finding]:
    findings = []
    kv = {bus["name"]: float(bus["kv"]) for bus in sub["buses"]}
    connected = reachable(sub)
    for bus in sub["buses"]:
        if bus["name"] not in connected:
            findings.append(Finding("TOP-01", "error", bus["name"], f"Bus {bus['name']} ({bus['kv']} kV) is not connected to the POI.",
                                    "Connect the bus or remove it from the case.",
                                    "An islanded bus is dropped from the study and hides missing equipment."))
    for line in sub["lines"]:
        for end in ("from", "to"):
            bus = line[end]
            if bus not in kv:
                findings.append(Finding("TOP-02", "error", line["name"], f"Connects to undefined bus {bus}.",
                                        "Define the bus or correct the connection.", "The line cannot be placed."))
            elif abs(kv[bus] - float(line["kv"])) > 0.05 * float(line["kv"]):
                findings.append(Finding("TOP-02", "error", line["name"],
                                        f"Line is {line['kv']} kV but bus {bus} is {kv[bus]} kV.",
                                        "Correct the line or bus voltage.", "Per-unit impedances would be on the wrong base."))
    for xf in sub["transformers"]:
        for side in ("hv", "lv"):
            bus, rated = xf[side], float(xf[f"kv_{side}"])
            if bus in kv and abs(kv[bus] - rated) > 0.05 * rated:
                cause = fix = None
                for factor, unit in ((1000.0, "volts"), (0.001, "megavolts")):
                    if abs(rated / factor - kv[bus]) <= 0.05 * kv[bus]:
                        cause = f"The winding voltage looks like it was entered in {unit}: {rated:g} -> {rated / factor:g} kV."
                        fix = {"kind": "transformer", "name": xf["name"], "field": f"kv_{side}", "value": rated / factor}
                findings.append(Finding("TOP-02", "error", xf["name"],
                                        f"{side.upper()} winding is {rated:g} kV but bus {bus} is {kv[bus]:g} kV.",
                                        "Correct the winding or bus voltage.",
                                        "An off-nominal ratio of this size distorts every voltage in the plant.",
                                        evidence={"winding_kv": rated, "bus_kv": kv[bus]}, probable_cause=cause, fix=fix))
    main = next((xf for xf in sub["transformers"] if xf["role"] == "main"), None)
    if main and _lv_delta(main.get("vector_group", "")):
        grounded = any(g["bus"] == main["lv"] for g in sub.get("grounding_transformers") or [])
        if not grounded:
            findings.append(Finding(
                "TOP-03", "error", main["name"],
                f"The {main['vector_group']} main transformer has a delta low-voltage winding and no grounding "
                f"transformer is modelled on {main['lv']}.",
                "Add the collector grounding transformer (impedance and connection) or confirm the grounding method.",
                "Without a ground source the collector is ungrounded: ground-fault currents, overvoltages and "
                "protection studies are wrong.",
                evidence={"vector_group": main["vector_group"], "bus": main["lv"]}))
    return findings


def _lv_delta(vector_group: str) -> bool:
    """True when the second winding of a vector group such as YNd1 is delta."""
    letters = "".join(c for c in vector_group if c.isalpha())
    lower = [c for c in letters if c.islower() and c != "n"]
    return bool(lower) and lower[0] == "d"


def line_requirements(sub: dict[str, Any]) -> dict[str, float]:
    """MVA of generation that depends on each line: generators cut off from the POI without it."""
    out = {}
    for line in sub["lines"]:
        alive = reachable(sub, skip=line["name"])
        out[line["name"]] = sum(g["mbase_mva"] for g in sub["generators"] if g["bus"] not in alive)
    return out


def check_lines(sub: dict[str, Any], rules: dict[str, Any]) -> list[Finding]:
    base = rules["study"]["base_mva"]
    findings = []
    needed = line_requirements(sub)
    for line in sub["lines"]:
        table = rules["lines"].get(line["construction"], {})
        cls = voltage_class(table, float(line["kv"]))
        values = per_km(line, base)
        flagged = False
        if cls is None:
            findings.append(Finding("LINE-01", "info", line["name"],
                                    f"No screening range for {line['construction']} at {line['kv']} kV.",
                                    "Review the impedances manually."))
        else:
            limits = table[cls]
            zb, length = z_base(float(line["kv"]), base), float(line["length_km"])
            for key, label in (("r", "R"), ("x", "X")):
                if _outside(values[key], limits[key]):
                    flagged = True
                    cause, fix = _unit_diagnosis(line, key, limits[key], zb, length)
                    findings.append(Finding(
                        "LINE-01", "error", line["name"],
                        f"{label} = {values[key]:.3f} ohm/km, outside {_fmt(limits[key])} ohm/km for {cls} kV {line['construction']}.",
                        f"Confirm {label} for the conductor and length, in p.u. on {base:g} MVA.",
                        "Power flow, short-circuit and stability results depend directly on this impedance.",
                        evidence={"value": round(values[key], 4), "range": limits[key], f"{key}_pu": line[f"{key}_pu"]},
                        probable_cause=cause, fix=fix))
            if _outside(values["b"], limits["b"]):
                flagged = True
                findings.append(Finding(
                    "LINE-02", "error", line["name"],
                    f"B = {values['b']:.1f} uS/km, outside {_fmt(limits['b'])} uS/km for {cls} kV {line['construction']}.",
                    "Confirm the line charging (total B in p.u.).",
                    "Charging sets the plant's reactive balance and light-load voltages.",
                    evidence={"value": round(values["b"], 2), "range": limits["b"]}))
            if not flagged and _outside(values["zc"], limits["zc"]):
                findings.append(Finding(
                    "LINE-03", "warning", line["name"],
                    f"Surge impedance sqrt(X/B) = {values['zc']:.0f} ohm, outside {_fmt(limits['zc'])} ohm, although X and B "
                    "are each in range.", "Check that X and B come from the same conductor and construction data.",
                    "Inconsistent series and shunt data misstate both losses and charging.",
                    evidence={"zc": round(values["zc"], 1), "range": limits["zc"]}))
            if not flagged and line["construction"] == "overhead" and "xr" in limits and _outside(values["xr"], limits["xr"]):
                findings.append(Finding("LINE-04", "warning", line["name"],
                                        f"X/R = {values['xr']:.1f}, outside {_fmt(limits['xr'])} for {cls} kV overhead.",
                                        "Confirm the conductor resistance.", "Affects losses and fault current asymmetry.",
                                        evidence={"xr": round(values["xr"], 2), "range": limits["xr"]}))
        rating = line.get("rating_mva") or {}
        summer, winter = rating.get("summer"), rating.get("winter")
        if summer is None or winter is None:
            findings.append(Finding("LINE-05", "error", line["name"], "Summer and winter ratings are both required.",
                                    "Provide seasonal ratings.", "Seasonal studies cannot apply the right limit."))
        elif winter < summer:
            findings.append(Finding(
                "LINE-05", "error", line["name"],
                f"Winter rating {winter} MVA is below the summer rating {summer} MVA; cooler winter air should allow more.",
                "Confirm the seasonal ratings.", "Summer studies would use an optimistic limit.",
                evidence={"summer": summer, "winter": winter},
                probable_cause="The summer and winter values appear to be swapped.",
                fix={"kind": "line", "name": line["name"], "field": "rating_mva", "value": {"summer": winter, "winter": summer}}))
        carried = needed.get(line["name"], 0.0)
        if summer is not None and winter is not None and carried > min(summer, winter) * circuits(line):
            capacity = min(summer, winter) * circuits(line)
            findings.append(Finding("LINE-06", "warning", line["name"],
                                    f"Lowest seasonal rating {capacity:g} MVA ({circuits(line)} circuit(s)) is below the "
                                    f"{carried:.0f} MVA of generation behind it.",
                                    "Confirm the rating or explain how output is limited.", "The line may overload at full output.",
                                    evidence={"rating": capacity, "generation_mva": carried}))
    return findings


def _unit_diagnosis(line: dict[str, Any], key: str, bounds: list[float], zb: float, length: float):
    """Explain an out-of-range impedance by a common data-entry slip, if one fits."""
    value = float(line[f"{key}_pu"])
    candidates = [
        ("ohms typed into the p.u. field", value / length, value / zb),
        ("a per-km p.u. value entered for the whole line", value * zb, value * length),
    ]
    for label, implied_per_km, corrected in candidates:
        if bounds[0] <= implied_per_km <= bounds[1]:
            return (f"Likely {label}: read that way it is {implied_per_km:.3f} ohm/km, a normal value "
                    f"({corrected:.5f} p.u. on the study base)."), {
                "kind": "line", "name": line["name"], "field": f"{key}_pu", "value": round(corrected, 6)}
    return None, None


def check_transformers(sub: dict[str, Any], rules: dict[str, Any]) -> list[Finding]:
    base = rules["study"]["base_mva"]
    bus_names = {bus["name"] for bus in sub["buses"]}
    findings = []
    for xf in sub["transformers"]:
        limits = rules["transformers"]["main" if xf["role"] == "main" else "generator"]
        z = float(xf["z_pct"])
        if _outside(z, limits["z_pct"]):
            cause = fix = None
            for converted, label in ((z * xf["mva"] / base, f"on the {base:g} MVA system base"),
                                     (z * base / xf["mva"], "on the system base the other way round")):
                if limits["z_pct"][0] <= converted <= limits["z_pct"][1]:
                    cause = (f"The value looks like it is {label}: {z:g}% x {xf['mva']:g}/{base:g} = {converted:.1f}% "
                             "on the transformer's own base, which is typical.")
                    fix = {"kind": "transformer", "name": xf["name"], "field": "z_pct", "value": round(converted, 3)}
                    break
            findings.append(Finding(
                "XF-01", "error", xf["name"],
                f"Impedance {z:g}% on {xf['mva']:g} MVA is outside {_fmt(limits['z_pct'])}% for a {xf['role']} transformer.",
                "Confirm the test-report impedance and the MVA base it is stated on.",
                "Transformer impedance drives voltage regulation, reactive losses and fault levels.",
                evidence={"z_pct": z, "mva": xf["mva"], "range": limits["z_pct"]}, probable_cause=cause, fix=fix))
        if _outside(float(xf["x_over_r"]), limits["xr"]):
            findings.append(Finding("XF-02", "warning", xf["name"],
                                    f"X/R = {xf['x_over_r']:g}, outside {_fmt(limits['xr'])}.",
                                    "Confirm load losses from the test report.", "Affects losses and fault current asymmetry."))
        tap = xf.get("tap")
        if tap:
            problems = []
            if not tap["min_pct"] < 0 < tap["max_pct"]:
                problems.append("the range should straddle the neutral tap")
            if "tap_range_pct" in limits and _outside(max(abs(tap["min_pct"]), abs(tap["max_pct"])), limits["tap_range_pct"]):
                problems.append(f"the range +/-{max(abs(tap['min_pct']), abs(tap['max_pct'])):g}% is outside {_fmt(limits['tap_range_pct'])}%")
            if tap["steps"] < 3 or tap["steps"] % 2 == 0:
                problems.append(f"{tap['steps']} positions: expected an odd number with a neutral position")
            if tap.get("controlled_bus") and tap["controlled_bus"] not in bus_names:
                problems.append(f"controlled bus {tap['controlled_bus']} does not exist")
            if problems:
                findings.append(Finding("XF-03", "error", xf["name"], "Tap changer: " + "; ".join(problems) + ".",
                                        "Correct the tap changer data.", "Voltage control studies would be wrong."))
        if not xf.get("vector_group"):
            findings.append(Finding("XF-04", "warning", xf["name"], "Vector group is not declared.",
                                    "State the winding connections and grounding.",
                                    "Zero-sequence and ground-fault studies need the connection."))
    return findings


def check_generators(sub: dict[str, Any], rules: dict[str, Any]) -> list[Finding]:
    findings = []
    ratio = rules["generators"]["q_ratio_min"]
    for gen in sub["generators"]:
        expected = gen["units"] * gen["unit_mva"]
        if abs(expected - gen["mbase_mva"]) > 0.01 * expected or gen["pmax_mw"] > gen["mbase_mva"]:
            findings.append(Finding("GEN-01", "error", gen["name"],
                                    f"MBASE {gen['mbase_mva']} MVA vs {gen['units']} x {gen['unit_mva']} MVA = {expected:g} MVA; "
                                    f"Pmax {gen['pmax_mw']} MW.", "Make MBASE the sum of unit ratings and Pmax at most MBASE.",
                                    "Dynamic model per-unit values are on MBASE."))
        q_needed = ratio * gen["pmax_mw"]
        if gen["qmax_mvar"] < q_needed or -gen["qmin_mvar"] < q_needed:
            findings.append(Finding(
                "GEN-03", "warning", gen["name"],
                f"Reactive range {gen['qmin_mvar']:g} to {gen['qmax_mvar']:g} Mvar is below +/-{q_needed:.0f} Mvar "
                f"({ratio:g} x Pmax) at the terminals.", "Provide the capability curve at full output.",
                "The plant may not meet the power-factor requirement at the POI (see PF-02).",
                evidence={"qmax": gen["qmax_mvar"], "qmin": gen["qmin_mvar"], "needed": round(q_needed, 1)}))
    total = sum(g["pmax_mw"] for g in sub["generators"])
    capacity = sub["project"]["capacity_mw"]
    if abs(total - capacity) > rules["study"]["capacity_tolerance"] * capacity:
        findings.append(Finding("GEN-02", "warning", sub["project"]["name"],
                                f"Generator Pmax totals {total:g} MW against a declared {capacity:g} MW.",
                                "Reconcile the modelled and declared capacity.", "Studies may test the wrong output."))
    return findings


def check_dynamics(sub: dict[str, Any], rules: dict[str, Any]) -> list[Finding]:
    spec = rules["dynamics"]
    step = rules["study"]["integration_step_s"] * rules["study"]["min_time_constant_steps"]
    ratio = rules["generators"]["q_ratio_min"]
    tolerance = rules["study"]["dynamic_q_tolerance"]
    required = spec["required"].get(sub["project"]["technology"], [])
    findings, flagged = [], set()
    for gen in sub["generators"]:
        models = (sub.get("dynamics") or {}).get(gen["dynamics"], {})
        missing = [m for m in required if m not in models]
        if missing:
            findings.append(Finding("DYN-01", "error", gen["name"], f"Missing dynamic models: {', '.join(missing)}.",
                                    "Provide the full model set with parameters.", "Stability studies cannot run."))
        for model, names in spec["time_constants"].items():
            for name in names:
                value = models.get(model, {}).get(name)
                if value is not None and 0 < value < step:
                    flagged.add((gen["dynamics"], model, name))
                    findings.append(Finding(
                        "DYN-03", "warning", f"{gen['name']} {model}",
                        f"{name} = {value:g} s is shorter than {rules['study']['min_time_constant_steps']} integration steps "
                        f"({step:.4f} s).", f"Set {name} to 0 to bypass it, or to at least {step:.3f} s.",
                        "Time constants below a few steps make the simulation numerically unstable or step-size dependent.",
                        evidence={"value": value, "minimum": round(step, 4)}))
        reec = models.get("REECA1", {})
        if reec:
            p = reec.get("PMax", gen["pmax_mw"] / gen["mbase_mva"])
            q = ratio * gen["pmax_mw"] / gen["mbase_mva"]
            needed = math.hypot(p, q)
            if reec.get("Imax", math.inf) < needed:
                flagged.add((gen["dynamics"], "REECA1", "Imax"))
                findings.append(Finding(
                    "DYN-04", "error", f"{gen['name']} REECA1",
                    f"Imax = {reec['Imax']:g} p.u. cannot carry rated P ({p:.3f}) with the required Q ({q:.3f}): "
                    f"needs {needed:.3f}.", "Raise Imax to the converter's current rating or justify the limit.",
                    "The model would cut active or reactive current at full output, misstating fault ride-through.",
                    evidence={"Imax": reec["Imax"], "needed": round(needed, 3)}))
            for key, static in (("QMax", gen["qmax_mvar"]), ("QMin", gen["qmin_mvar"])):
                if key in reec:
                    dynamic = reec[key] * gen["mbase_mva"]
                    if abs(dynamic - static) > tolerance * max(abs(static), 1.0):
                        findings.append(Finding(
                            "DYN-06", "warning", f"{gen['name']} REECA1",
                            f"{key} x MBASE = {dynamic:.1f} Mvar but the power-flow limit is {static:g} Mvar.",
                            "Align the dynamic and static reactive limits.",
                            "The case will not initialise cleanly (flat-run drift).",
                            evidence={"dynamic": round(dynamic, 1), "static": static}))
        repc = models.get("REPCA1", {})
        if repc:
            frequency = spec["frequency_response"]
            ddn = repc.get("Ddn", 0)
            droop = 100.0 / ddn if ddn else math.inf
            if _outside(droop, frequency["droop_pct"]):
                flagged.add((gen["dynamics"], "REPCA1", "Ddn"))
                findings.append(Finding(
                    "DYN-05", "error", f"{gen['name']} REPCA1",
                    f"Ddn = {ddn:g} gives a {droop:.0f}% droop, outside {_fmt(frequency['droop_pct'])}%.",
                    "Set the droop to the plant's frequency-response setting (for 5%, Ddn = 20).",
                    "Over-frequency response would be too weak in frequency studies.",
                    evidence={"Ddn": ddn, "droop_pct": round(droop, 1)}))
            band = max(abs(repc.get("fdbd1", 0)), abs(repc.get("fdbd2", 0)))
            if band > frequency["max_deadband_pu"]:
                flagged.add((gen["dynamics"], "REPCA1", "fdbd"))
                findings.append(Finding(
                    "DYN-05", "error", f"{gen['name']} REPCA1",
                    f"Frequency deadband {band * 60_000:.0f} mHz exceeds {frequency['max_deadband_pu'] * 60_000:.0f} mHz.",
                    "Narrow the deadband to the required setting.", "Frequency response would start too late.",
                    evidence={"deadband_pu": band}))
    seen = set()
    for gen in sub["generators"]:
        if gen["dynamics"] in seen:
            continue
        seen.add(gen["dynamics"])
        models = (sub.get("dynamics") or {}).get(gen["dynamics"], {})
        for model, ranges in spec["ranges"].items():
            for name, bounds in ranges.items():
                value = models.get(model, {}).get(name)
                if value is None or (gen["dynamics"], model, name) in flagged:
                    continue
                if _outside(float(value), bounds):
                    findings.append(Finding("DYN-02", "warning", f"{gen['dynamics']} {model}",
                                            f"{name} = {value:g}, outside the screening range {_fmt(bounds)}.",
                                            f"Confirm {name} against the manufacturer's model.",
                                            "Unusual parameters need a justification before stability studies.",
                                            evidence={"value": value, "range": bounds}))
    return findings


# --- power flow, short circuit and sensitivity (pandapower) --------------------------------

def apply_fixes(sub: dict[str, Any], fixes: list[dict[str, Any]]) -> dict[str, Any]:
    fixed = copy.deepcopy(sub)
    for fix in fixes:
        table = "lines" if fix["kind"] == "line" else "transformers"
        for row in fixed[table]:
            if row["name"] == fix["name"]:
                row[fix["field"]] = fix["value"]
    return fixed


def build_net(sub: dict[str, Any], rules: dict[str, Any], q_mode: str = "unity", scale: dict[str, float] | None = None):
    import pandapower as pp

    scale = scale or {}
    base, f_hz = rules["study"]["base_mva"], rules["study"]["frequency_hz"]
    net = pp.create_empty_network(sn_mva=base, f_hz=f_hz)
    index = {bus["name"]: pp.create_bus(net, vn_kv=float(bus["kv"]), name=bus["name"]) for bus in sub["buses"]}
    eq = sub["system_equivalent"]
    pp.create_ext_grid(net, index[eq["bus"]], vm_pu=scale.get("poi_vm", 1.0), s_sc_max_mva=eq["short_circuit_mva"],
                       s_sc_min_mva=eq["short_circuit_mva"], rx_max=1.0 / eq["x_over_r"], rx_min=1.0 / eq["x_over_r"])
    for line in sub["lines"]:
        zb, length = z_base(float(line["kv"]), base), float(line["length_km"])
        x_scale = scale.get("collector_x", 1.0) if float(line["kv"]) < 69 else 1.0
        rating = min(line["rating_mva"].values())
        pp.create_line_from_parameters(
            net, index[line["from"]], index[line["to"]], length_km=length,
            r_ohm_per_km=line["r_pu"] * zb / length, x_ohm_per_km=line["x_pu"] * zb / length * x_scale,
            c_nf_per_km=line["b_pu"] / zb / length / (2 * math.pi * f_hz) * 1e9,
            max_i_ka=rating / (math.sqrt(3) * float(line["kv"])), parallel=circuits(line), name=line["name"])
    for xf in sub["transformers"]:
        z = float(xf["z_pct"]) * scale.get("main_z" if xf["role"] == "main" else "gsu_z", 1.0)
        tap = xf.get("tap")
        taps: dict[str, Any] = {}
        if tap:
            half = (int(tap["steps"]) - 1) // 2
            taps = {"tap_side": tap.get("side", "hv"), "tap_neutral": 0, "tap_min": -half, "tap_max": half,
                    "tap_step_percent": (tap["max_pct"] - tap["min_pct"]) / (int(tap["steps"]) - 1), "tap_pos": 0,
                    "tap_changer_type": "Ratio"}
        pp.create_transformer_from_parameters(
            net, index[xf["hv"]], index[xf["lv"]], sn_mva=float(xf["mva"]), vn_hv_kv=float(xf["kv_hv"]),
            vn_lv_kv=float(xf["kv_lv"]), vk_percent=z, vkr_percent=z / math.sqrt(1 + float(xf["x_over_r"]) ** 2),
            pfe_kw=0.0, i0_percent=0.0, shift_degree=0.0, name=xf["name"], **taps)
    q_scale = scale.get("q_limit", 1.0)
    for gen in sub["generators"]:
        q = {"unity": 0.0, "export": gen["qmax_mvar"] * q_scale, "absorb": gen["qmin_mvar"] * q_scale}[q_mode]
        pp.create_sgen(net, index[gen["bus"]], p_mw=float(gen["pmax_mw"]), q_mvar=float(q), sn_mva=float(gen["mbase_mva"]),
                       name=gen["name"], k=1.2, generator_type="current_source")
    return net, index


def solve(net, sub: dict[str, Any], rules: dict[str, Any], index: dict[str, int]) -> dict[str, Any]:
    """AC power flow with the main transformer's tap changer holding its controlled bus in band."""
    import pandapower as pp

    band = rules["study"]["ltc_band"]
    main = next((xf for xf in sub["transformers"] if xf["role"] == "main" and xf.get("tap")), None)
    trafo = int(net.trafo.index[net.trafo.name == main["name"]][0]) if main else None
    controlled = index.get(main["tap"].get("controlled_bus")) if main else None
    attempts = []
    for _ in range(40):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pp.runpp(net, algorithm="nr", max_iteration=40, init="auto", numba=False)
        except Exception as first:  # noqa: BLE001 - pandapower raises LoadflowNotConverged and numerical errors
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    pp.runpp(net, algorithm="nr", max_iteration=60, init="dc", numba=False)
            except Exception as second:  # noqa: BLE001
                attempts += [type(first).__name__, type(second).__name__]
                return {"converged": False, "errors": attempts}
        solved = net.res_bus.vm_pu.dropna()
        if ((solved < 0.5) | (solved > 1.5)).any():   # converged numerically, but not to a real operating point
            worst = net.bus.name.at[(solved - 1.0).abs().idxmax()]
            return {"converged": False, "errors": [f"implausible solution: {worst} at {float(solved[(solved - 1.0).abs().idxmax()]):.3g} p.u."]}
        if trafo is None or controlled is None:
            break
        v = float(net.res_bus.vm_pu.at[controlled])
        position = int(net.trafo.tap_pos.at[trafo])
        if v > band[1] and position < net.trafo.tap_max.at[trafo]:
            net.trafo.at[trafo, "tap_pos"] = position + 1
        elif v < band[0] and position > net.trafo.tap_min.at[trafo]:
            net.trafo.at[trafo, "tap_pos"] = position - 1
        else:
            break
    names = net.bus.name
    return {
        "converged": True,
        "p_poi_mw": round(-float(net.res_ext_grid.p_mw.sum()), 2),
        "q_poi_mvar": round(-float(net.res_ext_grid.q_mvar.sum()), 2),
        "losses_mw": round(float(net.res_sgen.p_mw.sum() + net.res_ext_grid.p_mw.sum()), 2),
        "voltages": {names.at[i]: round(float(v), 4) for i, v in net.res_bus.vm_pu.items() if not np.isnan(v)},
        "line_loading": {net.line.name.at[i]: round(float(v), 1) for i, v in net.res_line.loading_percent.items()},
        "trafo_loading": {net.trafo.name.at[i]: round(float(v), 1) for i, v in net.res_trafo.loading_percent.items()},
        "tap_position": int(net.trafo.tap_pos.at[trafo]) if trafo is not None else None,
    }


def power_flow_study(sub: dict[str, Any], rules: dict[str, Any], fixes: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Finding]]:
    findings = []
    net, index = build_net(sub, rules)
    submitted = solve(net, sub, rules, index)
    study: dict[str, Any] = {"as_submitted": submitted, "fixes_tested": [], "case": "as submitted"}
    case = sub
    if not submitted["converged"]:
        restoring = []
        for fix in fixes:
            trial_sub = apply_fixes(sub, [fix])
            trial_net, trial_index = build_net(trial_sub, rules)
            result = solve(trial_net, trial_sub, rules, trial_index)
            study["fixes_tested"].append({"fix": fix, "converged": result["converged"]})
            if result["converged"]:
                restoring.append(fix)
        cause = (" It solves to a valid operating point once " + " or once ".join(
            f"{f['name']} {f['field']} is set to {f['value']}" for f in restoring) + ".") if restoring else ""
        findings.append(Finding(
            "PF-01", "error", sub["project"]["name"],
            "The AC power flow does not reach a valid solution at full output with the data as submitted." + cause,
            "Correct the model so the case solves from a flat start.",
            "No steady-state, short-circuit or dynamic study can be run on this case.",
            evidence={"solver_errors": submitted.get("errors", []), "restoring_fixes": restoring},
            probable_cause=("Root cause isolated by re-solving with each suspected correction on its own."
                            if restoring else None)))
        case = apply_fixes(sub, fixes)
        study["case"] = "with the probable corrections applied (provisional)"
    requirement = math.tan(math.acos(rules["study"]["poi_power_factor"]))
    results = {}
    for mode in ("unity", "export", "absorb"):
        net, index = build_net(case, rules, q_mode=mode)
        results[mode] = solve(net, case, rules, index)
    study.update(results)
    if all(r["converged"] for r in results.values()):
        p = results["unity"]["p_poi_mw"]
        need = requirement * p
        export, absorb = results["export"]["q_poi_mvar"], -results["absorb"]["q_poi_mvar"]
        study["requirement_mvar"] = round(need, 1)
        study["export_margin_mvar"] = round(export - need, 1)
        study["absorb_margin_mvar"] = round(absorb - need, 1)
        if export < need or absorb < need:
            findings.append(Finding(
                "PF-02", "error", sub["project"]["name"],
                f"At {p:.0f} MW the plant delivers {export:+.0f} / {-absorb:+.0f} Mvar at the POI; "
                f"{rules['study']['poi_power_factor']:g} power factor needs +/-{need:.0f} Mvar.",
                "Add reactive capability (inverter MVA, capacitor or STATCOM) or justify the shortfall.",
                "The facility would not meet the reactive power requirement at the POI.",
                evidence={"p_mw": p, "export_mvar": export, "absorb_mvar": absorb, "required_mvar": round(need, 1)}))
        # Limits are judged where the plant just meets the requirement, not at its reactive extremes.
        points = {"unity": results["unity"], **{mode: required_point(case, rules, mode, need) for mode in ("export", "absorb")}}
        study["required_points"] = points
        limits = rules["study"]["plant_voltage_limits"]
        issues = []
        for mode, result in points.items():
            for bus, v in result["voltages"].items():
                if bus != sub["system_equivalent"]["bus"] and (v < limits[0] or v > limits[1]):
                    issues.append(f"{bus} at {v:.3f} p.u. ({mode})")
            for name, loading in {**result["line_loading"], **result["trafo_loading"]}.items():
                if loading > 100:
                    issues.append(f"{name} loaded {loading:.0f}% ({mode})")
        if issues:
            findings.append(Finding("PF-03", "warning", sub["project"]["name"],
                                    "At the required operating points: " + ", ".join(sorted(set(issues))[:8]) + ".",
                                    "Review voltage control settings and equipment ratings.",
                                    "Equipment would operate outside its ratings while meeting the requirement."))
    return study, findings


def required_point(case: dict[str, Any], rules: dict[str, Any], mode: str, need: float) -> dict[str, Any]:
    """Bisect the share of inverter reactive capability that just meets the POI requirement."""
    def reach(share: float) -> dict[str, Any]:
        net, index = build_net(case, rules, q_mode=mode, scale={"q_limit": share})
        result = solve(net, case, rules, index)
        result["share_of_capability"] = round(share, 3)
        return result

    def delivered(result: dict[str, Any]) -> float:
        return result["q_poi_mvar"] if mode == "export" else -result["q_poi_mvar"]

    best = reach(1.0)
    if not best["converged"] or delivered(best) <= need:
        return best
    low, high = 0.0, 1.0
    for _ in range(8):
        middle = (low + high) / 2
        trial = reach(middle)
        if trial["converged"] and delivered(trial) >= need:
            high, best = middle, trial
        else:
            low = middle
    return best


def short_circuit_study(sub: dict[str, Any], rules: dict[str, Any]) -> tuple[dict[str, Any], list[Finding]]:
    import pandapower.shortcircuit as sc

    findings = []
    eq = sub["system_equivalent"]
    scr = eq["short_circuit_mva"] / sub["project"]["capacity_mw"]
    study: dict[str, Any] = {"scr": round(scr, 2), "system_mva": eq["short_circuit_mva"]}
    if scr < rules["study"]["min_scr"]:
        findings.append(Finding(
            "SC-01", "warning", sub["project"]["name"],
            f"Short-circuit ratio {scr:.1f} ({eq['short_circuit_mva']} MVA / {sub['project']['capacity_mw']} MW) is below "
            f"{rules['study']['min_scr']:g}: a weak connection point.",
            "Provide a validated EMT (PSCAD) model; an EMT study will be required.",
            "Phasor-domain (PSS/E) models can miss control instability at low system strength.",
            evidence={"scr": round(scr, 2)}))
    main = next((xf for xf in sub["transformers"] if xf["role"] == "main"), None)
    if main:
        net, index = build_net(sub, rules)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sc.calc_sc(net, case="max", bus=[index[main["lv"]]], ip=False, ith=False)
        fault_ka = float(net.res_bus_sc.ikss_ka.at[index[main["lv"]]])
        study["collector_fault_ka"] = round(fault_ka, 2)
        rating = eq.get("mv_switchgear_ka")
        study["switchgear_ka"] = rating
        if rating and fault_ka > rating:
            findings.append(Finding("SC-02", "error", main["lv"],
                                    f"Three-phase fault level {fault_ka:.1f} kA exceeds the {rating} kA switchgear rating.",
                                    "Confirm switchgear ratings or limit fault current.",
                                    "Breakers could fail to interrupt a fault."))
    return study, findings


SENSITIVITY = [
    ("Main transformer impedance", "main_z", 0.8, 1.2, "-20% / +20%"),
    ("Generator transformer impedance", "gsu_z", 0.8, 1.2, "-20% / +20%"),
    ("Collector reactance", "collector_x", 0.8, 1.2, "-20% / +20%"),
    ("Inverter reactive limit", "q_limit", 0.9, 1.1, "-10% / +10%"),
    ("POI voltage", "poi_vm", 0.95, 1.05, "0.95 / 1.05 p.u."),
]


def sensitivity_study(case: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """POI reactive export margin (Mvar above the requirement) as each uncertain input moves."""
    requirement = math.tan(math.acos(rules["study"]["poi_power_factor"]))

    def margin(scale: dict[str, float]) -> float | None:
        net, index = build_net(case, rules, q_mode="export", scale=scale)
        result = solve(net, case, rules, index)
        return round(result["q_poi_mvar"] - requirement * result["p_poi_mw"], 1) if result["converged"] else None

    base = margin({})
    rows = [{"parameter": label, "range": text, "low": margin({key: low}), "high": margin({key: high})}
            for label, key, low, high, text in SENSITIVITY]
    rows.sort(key=lambda r: -abs((r["high"] or 0) - (r["low"] or 0)))
    return {"metric": "POI reactive export margin (Mvar)", "base": base, "rows": rows}


# --- anomaly detection against accepted models -------------------------------------------

LINE_FEATURES = ["log_r", "log_x", "log_b"]
REFERENCE_GROUPS = {   # typical per-km values for accepted models: (r at small conductor, r ratio to large, x, x drop, b, b rise)
    ("cable", "34.5"): (0.30, 0.20, 0.17, 0.06, 80.0, 90.0),
    ("cable", "138"): (0.08, 0.35, 0.20, 0.06, 55.0, 50.0),
    ("overhead", "240"): (0.09, 0.40, 0.47, 0.07, 3.0, 0.7),
    ("overhead", "138"): (0.20, 0.40, 0.50, 0.06, 2.8, 0.5),
    ("overhead", "34.5"): (0.50, 0.35, 0.42, 0.05, 2.8, 0.4),
}


def reference_library(seed: int, n: int = 600) -> dict[tuple[str, str], pd.DataFrame]:
    """Synthetic stand-in for an archive of accepted models: per-km parameters that move together
    with conductor size, plus manufacturing scatter."""
    rng = np.random.default_rng(seed)
    library = {}
    for key, (r0, r_ratio, x0, x_drop, b0, b_rise) in REFERENCE_GROUPS.items():
        size = rng.random(n)
        noise = rng.normal(0.0, 0.07, (n, 3))
        library[key] = pd.DataFrame({"log_r": np.log(r0 * r_ratio**size) + noise[:, 0],
                                     "log_x": np.log(x0 - x_drop * size) + noise[:, 1],
                                     "log_b": np.log(b0 + b_rise * size) + noise[:, 2]})
    return library


def anomaly_study(sub: dict[str, Any], rules: dict[str, Any], seed: int,
                  already: set[str]) -> tuple[dict[str, Any], list[Finding]]:
    base = rules["study"]["base_mva"]
    library = reference_library(seed)
    findings, scored = [], []
    for line in sub["lines"]:
        key = (line["construction"], voltage_class({k[1]: 0 for k in library if k[0] == line["construction"]}, float(line["kv"])))
        if key not in library:
            continue
        reference = library[key]
        forest = IsolationForest(n_estimators=300, random_state=seed).fit(reference[LINE_FEATURES])
        threshold = float(np.percentile(forest.score_samples(reference[LINE_FEATURES]), 1))
        values = per_km(line, base)
        point = pd.DataFrame([{"log_r": math.log(values["r"]), "log_x": math.log(values["x"]), "log_b": math.log(values["b"])}])
        score = float(forest.score_samples(point)[0])
        median = reference.median()
        mad = (reference - median).abs().median() * 1.4826
        z = ((point.iloc[0] - median) / mad).round(1).to_dict()
        rank = float((forest.score_samples(reference[LINE_FEATURES]) < score).mean())
        scored.append({"element": line["name"], "group": f"{key[1]} kV {key[0]}", "score": round(score, 3),
                       "threshold": round(threshold, 3), "share_of_accepted_more_unusual": round(rank, 3),
                       "robust_z": {k.replace("log_", ""): v for k, v in z.items()},
                       "per_km": {k: round(values[k], 4) for k in ("r", "x", "b")}, "flagged": score < threshold})
        if score < threshold and line["name"] not in already:
            drivers = sorted(z.items(), key=lambda kv: -abs(kv[1]))
            text = ", ".join(f"{name.replace('log_', '').upper()} {'high' if v > 0 else 'low'} ({v:+.1f} robust sd)"
                             for name, v in drivers if abs(v) >= 2) or "the combination, not any single value"
            share = (f"every one of the {len(reference)}" if rank == 0 else f"{1 - rank:.1%} of the {len(reference)}")
            findings.append(Finding(
                "ML-01", "warning", line["name"],
                f"Each value passes its range check, but the combination is more unusual than {share} accepted "
                f"{key[1]} kV {key[0]} models: {text}.",
                "Confirm the conductor and cable data sheet values together.",
                "A plausible-looking but wrong impedance passes every single-value check and skews every study.",
                evidence={"score": round(score, 3), "threshold": round(threshold, 3), "robust_z": z}))
    return {"lines": scored, "library": "synthetic, 600 accepted models per group"}, findings


# --- review --------------------------------------------------------------------------------

def review(sub: dict[str, Any], rules: dict[str, Any], seed: int) -> dict[str, Any]:
    findings = merge_duplicates(check_data(sub) + check_topology(sub) + check_lines(sub, rules)
                                + check_transformers(sub, rules) + check_generators(sub, rules) + check_dynamics(sub, rules))
    fixes = [f.fix for f in findings if f.fix and f.fix["field"] != "rating_mva"]
    clean = apply_fixes(sub, [])
    clean["buses"] = [b for b in clean["buses"] if b["name"] in reachable(clean)]   # islanded buses are dropped
    pf, pf_findings = power_flow_study(clean, rules, fixes)
    case = apply_fixes(clean, fixes) if not pf["as_submitted"]["converged"] else clean
    sc_result, sc_findings = short_circuit_study(case, rules)
    sensitivity = sensitivity_study(case, rules) if pf["unity"]["converged"] else None
    flagged = {f.element for f in findings if f.rule in ("LINE-01", "LINE-02", "LINE-03")}
    anomalies, ml_findings = anomaly_study(sub, rules, seed, flagged)
    findings += pf_findings + sc_findings + ml_findings
    # Most material first: what blocks studies, then what changes their results, then rule order.
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity],
                                 MATERIALITY.index(f.rule) if f.rule in MATERIALITY else len(MATERIALITY),
                                 list(RULES).index(f.rule)))
    errors = sum(f.severity == "error" for f in findings)
    warnings_ = sum(f.severity == "warning" for f in findings)
    outcome = ("Resubmission required" if errors else "Accepted with clarifications" if warnings_ else "Accepted")
    failed = {f.rule for f in findings if f.severity in ("error", "warning")}
    checklist = [{"rule": rule, "title": title, "category": category,
                  "status": ("fail" if any(f.rule == rule and f.severity == "error" for f in findings)
                             else "review" if rule in failed else "pass")}
                 for rule, (title, category) in RULES.items()]
    return {"project": sub["project"], "outcome": outcome, "errors": errors, "warnings": warnings_,
            "findings": [asdict(f) for f in findings], "checklist": checklist, "power_flow": pf,
            "short_circuit": sc_result, "sensitivity": sensitivity, "anomaly": anomalies}


def summary_text(result: dict[str, Any], use_llm: bool = True) -> tuple[str, str]:
    """A short cover summary for the letter: drafted by the local LLM when available, else a template.
    The LLM sees only the findings, and the findings table in the letter is never generated by it."""
    project = result["project"]
    errors = sorted((f for f in result["findings"] if f["severity"] == "error"),
                    key=lambda f: MATERIALITY.index(f["rule"]) if f["rule"] in MATERIALITY else len(MATERIALITY))
    warnings_ = [f for f in result["findings"] if f["severity"] == "warning"]
    template = (f"This automated first-pass review of {project['name']} found {len(errors)} deficiencies and "
                f"{len(warnings_)} items needing clarification. ")
    if errors:
        def named(f: dict[str, Any]) -> str:
            where = "" if f["element"] == project["name"] else f", {f['element']}"
            return f"{FAILURE.get(f['rule'], RULES[f['rule']][0].lower())} ({f['rule']}{where})"

        template += ("The most material are " + "; ".join(named(f) for f in errors[:3]) + ". "
                     "The model cannot be accepted for interconnection studies until the deficiencies are corrected.")
    else:
        template += "The model is fit for interconnection studies subject to the clarifications below."
    if not use_llm:
        return template, "template"
    from lmplab.llm import LLMUnavailable, LocalLLM

    facts = "\n".join(f"- [{f['severity']}] {f['rule']} {f['element']}: {f['message']}" for f in result["findings"][:14])
    try:
        text = LocalLLM().chat(
            "You are a senior power-system model validation engineer writing to a market participant. Write a "
            "4-sentence cover summary of the review. Use only the findings provided; do not add findings, numbers "
            "or requirements. Be direct and professional.",
            f"Project: {project['name']} ({project['capacity_mw']} MW {project['technology']}). Outcome: {result['outcome']}.\n"
            f"Findings:\n{facts or '- none: every rule passed'}\n/no_think", max_tokens=1500)   # room for a reasoning trace
        return text, "local LLM"
    except LLMUnavailable:
        return template, "template"


def letter(result: dict[str, Any], summary: str, source: str) -> str:
    project = result["project"]
    lines = [f"# PDUP model review: {project['name']}", "",
             f"{project['id']} · Stage {project['stage']} · {project['capacity_mw']} MW {project['technology']} · "
             f"POI {project['poi']}", "",
             f"**Outcome: {result['outcome']}** ({result['errors']} deficiencies, {result['warnings']} clarifications)", "",
             "## Summary", "", summary, "", f"_Summary drafted by: {source}; findings below are rule-based._", ""]
    for severity, title in (("error", "Deficiencies (must be corrected)"), ("warning", "Clarifications requested")):
        rows = [f for f in result["findings"] if f["severity"] == severity]
        if not rows:
            continue
        lines += [f"## {title}", "", "| # | Rule | Element | Finding | Required action |", "|---|---|---|---|---|"]
        for i, f in enumerate(rows, 1):
            finding = f["message"] + (f" *{f['probable_cause']}*" if f.get("probable_cause") else "")
            lines.append(f"| {i} | {f['rule']} | {f['element']} | {finding} | {f['action']} |")
        lines.append("")
    pf = result["power_flow"]
    lines += ["## Study results", ""]
    if not pf["as_submitted"]["converged"]:
        lines.append("- AC power flow: **no valid solution as submitted**; results below use the probable corrections "
                     "and are provisional until the market participant confirms them.")
    if pf.get("requirement_mvar") is not None:
        lines.append(f"- Reactive capability at the POI: {pf['export']['q_poi_mvar']:+.1f} / {pf['absorb']['q_poi_mvar']:+.1f} Mvar "
                     f"at {pf['unity']['p_poi_mw']:.1f} MW against +/-{pf['requirement_mvar']:.1f} Mvar required.")
    scr = result["short_circuit"]
    lines.append(f"- Short-circuit ratio at the POI: {scr['scr']:.1f}; collector fault level "
                 f"{scr.get('collector_fault_ka', float('nan')):.1f} kA (switchgear {scr.get('switchgear_ka')} kA).")
    if result["sensitivity"]:
        top = result["sensitivity"]["rows"][0]
        lines.append(f"- Most sensitive input for the POI reactive margin: {top['parameter']} "
                     f"({top['range']}: {top['low']:+.1f} to {top['high']:+.1f} Mvar).")
    lines += ["", "_Synthetic project and data, produced by an automated first-pass review; engineering judgement "
              "and AESO's PDUP checklist govern._", ""]
    return "\n".join(lines)


def run(settings: Settings) -> dict[str, Any]:
    paths = settings.paths
    rules = load_rules(paths.pdup / "rules.yaml")
    use_llm = True
    out_dir = paths.artifacts / "pdup"
    out_dir.mkdir(parents=True, exist_ok=True)
    reviews = []
    for path in sorted((paths.pdup / "submissions").glob("*.yaml")):
        result = review(load_submission(path), rules, settings.seed)
        summary, source = summary_text(result, use_llm)
        use_llm = use_llm and source == "local LLM"
        result["summary"], result["summary_source"] = summary, source
        result["letter"] = letter(result, summary, source)
        (out_dir / f"{path.stem}-review.md").write_text(result["letter"], encoding="utf-8", newline="\n")
        reviews.append(result)
    report = {"rules": {k: {"title": v[0], "category": v[1]} for k, v in RULES.items()}, "reviews": reviews}
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "pdup.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8", newline="\n")
    return report
