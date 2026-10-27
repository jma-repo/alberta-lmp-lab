"""Load the AESO files into a DuckDB warehouse, with data-quality checks that stop bad data.

Tables
  area       one row per planning area: name, AESO region, hub region, map position
  area_hour  hourly metered load and generation per area (MW), hour-beginning MST
  lmp_hour   AESO's 2025 backcast LMP per area ($/MWh), aligned to the same hours
Views
  v_hour     area_hour joined to lmp_hour, with net generation
  v_system   one row per hour: system load, generation, and the Alberta Load Price (ALP)
"""

from __future__ import annotations

import json
import re
from typing import Any

import duckdb
import pandas as pd

from lmplab.areas import AREAS, HUB_REGION, NAMES, map_position
from lmplab.config import Settings

HOUR_LABEL = re.compile(r"^(\d{4})_(\d{2})_(\d{2}) Hr (\d{2})$")
AREA_LABEL = re.compile(r"^\s*(\d+)\s*\[")


class DataQualityError(ValueError):
    """Raised when the source files break an assumption the analysis depends on."""


def read_backcast(path) -> pd.DataFrame:
    """Wide AESO workbook (one column per area, hour-ending labels) -> long frame."""
    wide = pd.read_excel(path, sheet_name=0)
    labels = wide.pop(wide.columns[0]).astype(str)
    parsed = labels.str.extract(HOUR_LABEL)
    if parsed.isna().any().any():
        raise DataQualityError(f"unparseable hour labels, e.g. {labels[parsed.isna().any(axis=1)].head(3).tolist()}")
    # "Hr 01" is hour ending 01:00, i.e. the hour beginning at 00:00.
    ts = pd.to_datetime(parsed[0] + "-" + parsed[1] + "-" + parsed[2]) + pd.to_timedelta(parsed[3].astype(int) - 1, unit="h")
    columns = {}
    for column in wide.columns:
        match = AREA_LABEL.match(str(column))
        if not match:
            raise DataQualityError(f"unexpected area column {column!r}")
        columns[column] = int(match.group(1))
    wide = wide.rename(columns=columns)
    wide.insert(0, "ts", ts.to_numpy())
    long = wide.melt(id_vars="ts", var_name="area_id", value_name="lmp")
    long["area_id"] = long["area_id"].astype(int)
    return long


def read_planning_area(path) -> pd.DataFrame:
    frame = pd.read_csv(path, compression="zip")
    frame = frame.rename(columns={"DT_MST": "ts", "REGION": "region", "PLANNING_AREA": "area_id", "Load": "load_mw",
                                  "SysGen": "sysgen_mw", "CSDGen": "csdgen_mw", "BTFGen": "btfgen_mw",
                                  "ActLoad": "actload_mw"})
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame["area_id"] = frame["area_id"].astype(int)
    return frame[["ts", "area_id", "region", "load_mw", "sysgen_mw", "csdgen_mw", "btfgen_mw", "actload_mw"]]


def quality_report(backcast: pd.DataFrame, planning: pd.DataFrame) -> dict[str, Any]:
    """Checks that must hold before any analysis; raises on a hard failure, records soft findings."""
    problems, findings = [], []
    hours = backcast["ts"].nunique()
    if hours != 8760:
        problems.append(f"backcast has {hours} hours, expected 8760")
    if set(backcast["area_id"]) != set(AREAS):
        problems.append(f"backcast areas differ from the planning map: {sorted(set(backcast['area_id']) ^ set(AREAS))}")
    if backcast.duplicated(["ts", "area_id"]).any():
        problems.append("duplicate backcast rows")
    if backcast["lmp"].isna().any():
        problems.append("missing backcast prices")
    per_area = planning.groupby("area_id")["ts"].nunique()
    if set(per_area.index) != set(AREAS):
        problems.append(f"planning-area file areas differ: {sorted(set(per_area.index) ^ set(AREAS))}")
    duplicates = int(planning.duplicated(["ts", "area_id"]).sum())
    if duplicates:
        findings.append(f"{duplicates} duplicate area-hours in the planning-area file (kept the first)")
    for column in ["load_mw", "sysgen_mw", "actload_mw"]:
        negative = int((planning[column] < -1).sum())
        if negative:
            findings.append(f"{negative} rows with negative {column} (kept; small negatives are metering noise)")
    missing_csd = int(planning["csdgen_mw"].isna().sum())
    if missing_csd:
        findings.append(f"{missing_csd} rows with missing CSD generation (not used by the analysis)")
    for column in ["load_mw", "sysgen_mw", "actload_mw"]:
        missing = int(planning[column].isna().sum())
        if missing:
            areas = sorted(planning.loc[planning[column].isna(), "area_id"].unique().tolist())
            findings.append(f"{missing} rows with missing {column} in areas {areas} (treated as 0 MW: no metered asset)")
    joined = backcast.merge(planning[["ts", "area_id"]].drop_duplicates(), on=["ts", "area_id"], how="left", indicator=True)
    coverage = float((joined["_merge"] == "both").mean())
    if coverage < 0.99:
        problems.append(f"only {coverage:.1%} of backcast area-hours have metered load and generation")
    capped = int((backcast["lmp"] > 1000).sum())
    findings.append(f"{capped} area-hours above $1,000/MWh in the backcast")
    if problems:
        raise DataQualityError("; ".join(problems))
    return {
        "hours": hours,
        "areas": int(backcast["area_id"].nunique()),
        "backcast_rows": int(len(backcast)),
        "planning_rows": int(len(planning)),
        "join_coverage": round(coverage, 4),
        "lmp_min": round(float(backcast["lmp"].min()), 2),
        "lmp_max": round(float(backcast["lmp"].max()), 2),
        "findings": findings,
    }


def run(settings: Settings) -> dict[str, Any]:
    paths = settings.paths
    backcast = read_backcast(settings.source_path("backcast"))
    planning = read_planning_area(settings.source_path("planning_area"))
    report = quality_report(backcast, planning)
    planning = planning.drop_duplicates(["ts", "area_id"])
    planning[["load_mw", "sysgen_mw", "actload_mw"]] = planning[["load_mw", "sysgen_mw", "actload_mw"]].fillna(0.0)

    region_of = planning.groupby("area_id")["region"].agg(lambda s: s.mode().iloc[0]).to_dict()
    area = pd.DataFrame([{"area_id": a, "name": NAMES[a], "region": region_of.get(a), "hub_region": HUB_REGION[a],
                          "x": map_position(a)[0], "y": map_position(a)[1]} for a in AREAS])

    paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
    if paths.warehouse.exists():
        paths.warehouse.unlink()
    con = duckdb.connect(str(paths.warehouse))
    try:
        for name, frame in (("area", area), ("area_hour", planning.drop(columns="region")), ("lmp_hour", backcast)):
            con.register("frame", frame)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame")
            con.unregister("frame")
        con.execute("""
            CREATE VIEW v_hour AS
            SELECT h.ts, h.area_id, a.name, a.region, a.hub_region, h.load_mw, h.sysgen_mw, h.actload_mw,
                   h.sysgen_mw - h.load_mw AS net_gen_mw, p.lmp
            FROM area_hour h
            JOIN lmp_hour p USING (ts, area_id)
            JOIN area a USING (area_id)
        """)
        # Alberta Load Price: load-weighted average of area LMPs, the price most consumers will pay.
        con.execute("""
            CREATE VIEW v_system AS
            SELECT ts,
                   sum(load_mw) AS load_mw,
                   sum(actload_mw) AS ail_mw,
                   sum(sysgen_mw) AS sysgen_mw,
                   sum(load_mw) - sum(sysgen_mw) AS net_import_mw,
                   sum(lmp * greatest(load_mw, 0)) / nullif(sum(greatest(load_mw, 0)), 0) AS alp,
                   min(lmp) AS lmp_min,
                   max(lmp) AS lmp_max
            FROM v_hour
            GROUP BY ts
        """)
        report["warehouse_rows"] = int(con.execute("SELECT count(*) FROM v_hour").fetchone()[0])
    finally:
        con.close()
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "data_quality.json").write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    return report
