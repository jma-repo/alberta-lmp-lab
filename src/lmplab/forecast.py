"""AI layer: a day-ahead forecaster of each area's basis (LMP - ALP), with calibrated
uncertainty, a separation-risk probability, and explained drivers.

Question it answers: for every planning area and every hour of tomorrow, how far will the local
price sit from the Alberta Load Price, how sure are we, and why? That is the basis risk a
generator carries under LMP, and the congestion signal AESO monitors.

Inputs, all known when a day-ahead forecast is made
  conditions   system load, intertie imports, the system price (ALP), net generation by region
               and in the area, hour. These are 2025 actuals standing in for the load, renewable
               and pool-price forecasts a forecaster would have, so the scores are an upper bound
               for a production forecaster. The system price matters because a trapped area's
               basis is roughly minus the system price when it spikes.
  history      the area's basis 24 h and 168 h earlier and over the trailing week. Congestion
               persists while an outage lasts, and recent prices are the only outage signal in
               public data.
The model learns from AESO's 2025 backcast and is scored on months it never saw
(train Jan-Aug, calibrate Sep, test Oct-Dec, the fall outage season).

Models (LightGBM, one model pooled across the 42 areas)
  P50 basis        against uniform-price, climatology, persistence and per-area linear baselines
  P10 / P90 basis  quantile models, conformalized per area (CQR): once on a fixed calibration
                   month, and on a rolling 30-day window that keeps up with drift
  separation risk  probability that |basis| exceeds the separation threshold (AUC, Brier score)
Drivers are LightGBM's exact tree SHAP values (pred_contrib), summarised per AESO region.
"""

from __future__ import annotations

import json
import os
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import brier_score_loss, roc_auc_score

from lmplab import analytics
from lmplab.areas import AREAS, HUB_REGION, HUB_REGIONS, NAMES
from lmplab.config import Settings

REGIONAL = [f"net_gen_{region.lower()}" for region in HUB_REGIONS]
SYSTEM = ["system_load_mw", "net_import_mw", "alp"]
LOCAL = ["area_load_mw", "area_net_gen_mw"]
HISTORY = ["basis_lag24", "basis_lag168", "basis_mean_7d", "separated_7d"]
FEATURES = ["area_id", "hour", "weekend", *SYSTEM, *REGIONAL, *LOCAL, *HISTORY]
LINEAR_FEATURES = [*SYSTEM, *REGIONAL, *LOCAL, *HISTORY]
LABELS = {
    "area_id": "Area", "hour": "Hour of day", "weekend": "Weekend",
    "system_load_mw": "System load", "net_import_mw": "Net imports", "alp": "System price (ALP)",
    **{f"net_gen_{r.lower()}": f"{r} net generation" for r in HUB_REGIONS},
    "area_load_mw": "Area load", "area_net_gen_mw": "Area net generation",
    "basis_lag24": "Basis 24 h earlier", "basis_lag168": "Basis a week earlier",
    "basis_mean_7d": "Mean basis, trailing week", "separated_7d": "Share of hours separated, trailing week",
}
PARAMS = {"n_estimators": 500, "learning_rate": 0.05, "num_leaves": 63, "min_child_samples": 40,
          "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.9, "verbose": -1}
SHOWCASE_AREAS = [4, 18, 6]  # Medicine Hat (trapped south-east supply), High Level (remote north-west), Calgary
ROLLING_DAYS = 30


def build_frame(hourly: pd.DataFrame, system: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """One row per area-hour: the basis to predict, the conditions, and the price history known a day ahead."""
    regional = (hourly.pivot_table(index="ts", columns="hub_region", values="net_gen_mw", aggfunc="sum")
                .reindex(columns=HUB_REGIONS, fill_value=0.0)
                .rename(columns=lambda region: f"net_gen_{region.lower()}"))
    conditions = (system.set_index("ts")[["load_mw", "net_import_mw"]]
                  .rename(columns={"load_mw": "system_load_mw"}).join(regional))
    frame = (hourly[["ts", "area_id", "load_mw", "net_gen_mw", "lmp", "alp", "basis"]]
             .rename(columns={"load_mw": "area_load_mw", "net_gen_mw": "area_net_gen_mw"})
             .merge(conditions, left_on="ts", right_index=True)
             .sort_values(["area_id", "ts"]).reset_index(drop=True))
    # Hours are contiguous per area (checked at ingest), so row shifts are hour shifts.
    basis = frame.groupby("area_id")["basis"]
    frame["basis_lag24"] = basis.shift(24)
    frame["basis_lag168"] = basis.shift(168)
    frame["basis_mean_7d"] = basis.transform(lambda s: s.shift(24).rolling(168, min_periods=24).mean())
    frame["separated_7d"] = basis.transform(
        lambda s: (s.abs() > threshold).astype(float).shift(24).rolling(168, min_periods=24).mean())
    frame["hour"] = frame["ts"].dt.hour
    frame["weekend"] = (frame["ts"].dt.dayofweek >= 5).astype(int)
    frame["area_id"] = pd.Categorical(frame["area_id"], categories=AREAS)
    return frame.sort_values(["ts", "area_id"]).reset_index(drop=True)


def split(frame: pd.DataFrame, train_until: str, calibrate_until: str) -> dict[str, pd.DataFrame]:
    first_calibration = pd.Timestamp(train_until) + pd.Timedelta(days=1)
    first_test = pd.Timestamp(calibrate_until) + pd.Timedelta(days=1)
    return {"train": frame[frame["ts"] < first_calibration],
            "calibrate": frame[(frame["ts"] >= first_calibration) & (frame["ts"] < first_test)],
            "test": frame[frame["ts"] >= first_test]}


# --- baselines -------------------------------------------------------------------------------

def climatology(train: pd.DataFrame, rows: pd.DataFrame) -> np.ndarray:
    """Median training basis for the same area and hour of day."""
    table = train.groupby(["area_id", "hour"], observed=True)["basis"].median()
    keys = pd.MultiIndex.from_arrays([rows["area_id"].astype(int), rows["hour"]])
    table.index = pd.MultiIndex.from_arrays([table.index.get_level_values(0).astype(int), table.index.get_level_values(1)])
    return table.reindex(keys).fillna(0.0).to_numpy()


def linear_per_area(train: pd.DataFrame, rows: pd.DataFrame) -> np.ndarray:
    """Least squares per area on the same conditions: a fixed shift-factor view of congestion,
    which cannot represent a constraint switching between binding and slack."""
    predictions = np.zeros(len(rows))
    for area, history in train.groupby("area_id", observed=True):
        mask = (rows["area_id"] == area).to_numpy()
        if not mask.any():
            continue
        inputs = history[LINEAR_FEATURES].fillna(0.0)
        mean, scale = inputs.mean(), inputs.std().replace(0, 1)
        design = np.column_stack([np.ones(len(inputs)), ((inputs - mean) / scale).to_numpy()])
        coef = np.linalg.lstsq(design, history["basis"].to_numpy(), rcond=None)[0]
        target = rows.loc[mask, LINEAR_FEATURES].fillna(0.0)
        predictions[mask] = np.column_stack([np.ones(mask.sum()), ((target - mean) / scale).to_numpy()]) @ coef
    return predictions


# --- conformal calibration -------------------------------------------------------------------

def conformal_offsets(low: np.ndarray, high: np.ndarray, actual: np.ndarray, groups: np.ndarray,
                      coverage: float) -> dict[Any, float]:
    """Conformalized quantile regression, one offset per group (Mondrian): widen (or narrow)
    [low, high] by the finite-sample (1 - alpha) quantile of the calibration nonconformity scores."""
    offsets = {}
    for group in np.unique(groups):
        mask = groups == group
        scores = np.maximum(low[mask] - actual[mask], actual[mask] - high[mask])
        n = int(mask.sum())
        level = min(1.0, np.ceil((n + 1) * coverage) / n)
        offsets[group] = float(np.quantile(scores, level, method="higher"))
    return offsets


def rolling_offsets(scores: pd.DataFrame, days: pd.DatetimeIndex, coverage: float, window: int) -> pd.DataFrame:
    """Online CQR: each day's per-area offset uses only the scores of the previous `window` days,
    all of which are settled by the time a day-ahead forecast is made."""
    rows = []
    for day in days:
        recent = scores[(scores["ts"] >= day - pd.Timedelta(days=window)) & (scores["ts"] < day)]
        for area, group in recent.groupby("area_id"):
            n = len(group)
            level = min(1.0, np.ceil((n + 1) * coverage) / n)
            rows.append({"day": day, "area_id": int(area), "offset": float(np.quantile(group["score"], level, method="higher"))})
    return pd.DataFrame(rows)


def interval_stats(low: np.ndarray, high: np.ndarray, actual: np.ndarray) -> dict[str, float]:
    inside = (actual >= low) & (actual <= high)
    return {"coverage": round(float(inside.mean()), 4), "mean_width": round(float(np.mean(high - low)), 2)}


# --- drivers ---------------------------------------------------------------------------------

def drivers(model: lgb.LGBMRegressor, rows: pd.DataFrame, seed: int, sample: int = 20000) -> dict[str, Any]:
    """Tree SHAP attributions for the P50 model: global importance, importance by region, and the
    average $/MWh change in basis per extra 100 MW of each region's net generation."""
    picked = rows.sample(n=min(sample, len(rows)), random_state=seed)
    contributions = model.predict(picked[FEATURES], pred_contrib=True)[:, :len(FEATURES)]
    shap = pd.DataFrame(contributions, columns=FEATURES, index=picked.index)
    regions = picked["area_id"].astype(int).map(HUB_REGION)
    overall = shap.abs().mean().sort_values(ascending=False)
    by_region = {region: shap[regions == region].abs().mean().round(3).to_dict() for region in HUB_REGIONS}
    sensitivity: dict[str, dict[str, float]] = {}
    for region in HUB_REGIONS:
        mask = (regions == region).to_numpy()
        sensitivity[region] = {}
        for feature in REGIONAL:
            x, effect = picked.loc[mask, feature].to_numpy(), shap.loc[mask, feature].to_numpy()
            spread = np.var(x)
            sensitivity[region][feature] = round(float(np.cov(x, effect, bias=True)[0, 1] / spread * 100), 3) if spread else 0.0
    # Dependence curves: how a region's basis responds to its own net generation. A flat line that
    # turns down is an export constraint starting to bind; the model learns it from prices alone.
    dependence = {}
    for region in ("Southeast", "Southwest", "Northwest"):
        feature = f"net_gen_{region.lower()}"
        mask = (regions == region).to_numpy()
        values = picked.loc[mask, feature]
        bins = pd.qcut(values, 12, duplicates="drop")
        table = pd.DataFrame({"x": values, "shap": shap.loc[mask, feature], "basis": picked.loc[mask, "basis"]}).groupby(bins, observed=True)
        curve = table.agg(x=("x", "mean"), shap=("shap", "mean"), basis=("basis", "mean"))
        dependence[region] = {"feature": feature, **{column: curve[column].round(2).tolist() for column in curve.columns}}
    return {"labels": LABELS, "overall": overall.round(3).to_dict(), "by_region": by_region,
            "per_100mw": sensitivity, "dependence": dependence, "sample_rows": int(len(picked))}


# --- pipeline step ---------------------------------------------------------------------------

def _fit(objective: str, seed: int, **extra: Any) -> lgb.LGBMModel:
    if objective == "binary":
        return lgb.LGBMClassifier(objective="binary", random_state=seed, **PARAMS, **extra)
    return lgb.LGBMRegressor(objective=objective, random_state=seed, **PARAMS, **extra)


def _errors(actual: np.ndarray, predicted: np.ndarray, separated: np.ndarray) -> dict[str, float]:
    error = np.abs(actual - predicted)
    return {"mae": round(float(error.mean()), 3), "mae_separated": round(float(error[separated].mean()), 3)}


def showcase(test: pd.DataFrame, low: np.ndarray, mid: np.ndarray, high: np.ndarray, days: int = 10) -> dict[str, Any]:
    """The test window where the showcase areas' basis moved most, for the dashboard."""
    picked = test.assign(p10=low, p50=mid, p90=high)
    picked = picked[picked["area_id"].astype(int).isin(SHOWCASE_AREAS)]
    activity = picked.groupby("ts")["basis"].apply(lambda s: s.abs().mean()).rolling(days * 24).mean()
    end = activity.idxmax()
    window = picked[(picked["ts"] > end - pd.Timedelta(hours=days * 24)) & (picked["ts"] <= end)]
    series = {}
    for area in SHOWCASE_AREAS:
        rows = window[window["area_id"].astype(int) == area]
        series[str(area)] = {"name": NAMES[area], **{column: rows[column].round(2).tolist()
                                                    for column in ("basis", "p10", "p50", "p90")}}
    hours = sorted(window["ts"].unique())
    return {"ts": [pd.Timestamp(t).strftime("%Y-%m-%d %H:00") for t in hours], "areas": series}


def run(settings: Settings) -> dict[str, Any]:
    cfg = settings.config["forecast"]
    threshold = float(settings.config["analytics"]["separation_threshold"])
    seed = settings.seed
    hourly, system = analytics.load_panel(settings)
    parts = split(build_frame(hourly, system, threshold), cfg["train_until"], cfg["valid_until"])
    train, calibrate, test = parts["train"], parts["calibrate"], parts["test"]

    quantiles = [float(q) for q in cfg["quantiles"]]
    models = {q: _fit("quantile", seed, alpha=q).fit(train[FEATURES], train["basis"]) for q in quantiles}
    separated_train = (train["basis"].abs() > threshold).astype(int)
    classifier = _fit("binary", seed).fit(train[FEATURES], separated_train)

    def predict(rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        low, mid, high = (models[q].predict(rows[FEATURES]) for q in quantiles)
        return np.minimum(low, high), mid, np.maximum(low, high)

    low_c, _, high_c = predict(calibrate)
    coverage = float(cfg["coverage_target"])
    offsets = conformal_offsets(low_c, high_c, calibrate["basis"].to_numpy(), calibrate["area_id"].astype(int).to_numpy(), coverage)

    actual = test["basis"].to_numpy()
    separated = np.abs(actual) > threshold
    areas = test["area_id"].astype(int).to_numpy()
    low, mid, high = predict(test)
    widen = np.array([offsets[a] for a in areas])
    low_fixed, high_fixed = low - widen, high + widen

    # Rolling recalibration: scores from the frozen models on every settled hour before each test day.
    settled = pd.concat([calibrate.assign(low=low_c, high=high_c), test.assign(low=low, high=high)])
    scores = pd.DataFrame({"ts": settled["ts"], "area_id": settled["area_id"].astype(int),
                           "score": np.maximum(settled["low"] - settled["basis"], settled["basis"] - settled["high"])})
    days = pd.DatetimeIndex(sorted(test["ts"].dt.normalize().unique()))
    daily = rolling_offsets(scores, days, coverage, ROLLING_DAYS).set_index(["day", "area_id"])["offset"]
    keys = pd.MultiIndex.from_arrays([test["ts"].dt.normalize(), areas])
    widen_rolling = daily.reindex(keys).to_numpy()
    low_conf, high_conf = low - widen_rolling, high + widen_rolling

    candidates = {
        "Uniform price (basis = 0)": np.zeros(len(test)),
        "Climatology (area x hour median)": climatology(train, test),
        "Persistence (basis 24 h earlier)": test["basis_lag24"].fillna(0.0).to_numpy(),
        "Linear, per area": linear_per_area(train, test),
        "LightGBM (P50)": mid,
    }
    point = {name: _errors(actual, predicted, separated) for name, predicted in candidates.items()}
    uniform_mae = point["Uniform price (basis = 0)"]["mae"]
    for name in point:
        point[name]["skill_vs_uniform"] = round(1 - point[name]["mae"] / uniform_mae, 3)

    risk = classifier.predict_proba(test[FEATURES])[:, 1]
    true_frac, mean_pred = calibration_curve(separated, risk, n_bins=10, strategy="quantile")
    per_area = []
    for area in AREAS:
        mask = areas == area
        per_area.append({
            "area_id": area, "name": NAMES[area], "hub_region": HUB_REGION[area],
            "mae": round(float(np.abs(actual[mask] - mid[mask]).mean()), 3),
            "mae_uniform": round(float(np.abs(actual[mask]).mean()), 3),
            "coverage": round(float(((actual[mask] >= low_conf[mask]) & (actual[mask] <= high_conf[mask])).mean()), 4),
            "offset": round(offsets[area], 3),
            "separated_share": round(float(separated[mask].mean()), 4),
            "auc": round(float(roc_auc_score(separated[mask], risk[mask])), 3) if 0 < separated[mask].sum() < mask.sum() else None,
        })

    report = {
        "split": {name: {"rows": int(len(rows)), "from": str(rows["ts"].min()), "to": str(rows["ts"].max())}
                  for name, rows in parts.items()},
        "features": FEATURES, "labels": LABELS, "params": PARAMS, "threshold": threshold,
        "point": point,
        "intervals": {"target": coverage, "rolling_days": ROLLING_DAYS,
                      "raw": interval_stats(low, high, actual),
                      "fixed": interval_stats(low_fixed, high_fixed, actual),
                      "conformal": interval_stats(low_conf, high_conf, actual),
                      "calibration_month_raw": interval_stats(low_c, high_c, calibrate["basis"].to_numpy()),
                      "monthly": {month: {"raw": interval_stats(low[m], high[m], actual[m])["coverage"],
                                          "fixed": interval_stats(low_fixed[m], high_fixed[m], actual[m])["coverage"],
                                          "rolling": interval_stats(low_conf[m], high_conf[m], actual[m])["coverage"]}
                                  for month in sorted(test["ts"].dt.strftime("%Y-%m").unique())
                                  for m in [(test["ts"].dt.strftime("%Y-%m") == month).to_numpy()]}},
        "separation": {"auc": round(float(roc_auc_score(separated, risk)), 4),
                       "brier": round(float(brier_score_loss(separated, risk)), 4),
                       "base_rate": round(float(separated.mean()), 4),
                       "reliability": {"predicted": mean_pred.round(4).tolist(), "observed": true_frac.round(4).tolist()}},
        "per_area": per_area,
        "drivers": drivers(models[0.5], test, seed),
        "showcase": showcase(test, low_conf, mid, high_conf),
    }
    paths = settings.paths
    paths.models.mkdir(parents=True, exist_ok=True)
    for q, model in models.items():
        model.booster_.save_model(str(paths.models / f"basis_p{round(q * 100):02d}.txt"))
    classifier.booster_.save_model(str(paths.models / "separation_risk.txt"))
    (paths.models / "conformal_offsets.json").write_text(json.dumps({str(k): v for k, v in offsets.items()}, indent=1),
                                                         encoding="utf-8")
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "forecast.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8", newline="\n")
    report["mlflow_run"] = _track(settings, report)
    return report


def _track(settings: Settings, report: dict[str, Any]) -> str | None:
    """Log the run to the local MLflow store (skipped when MLflow is not installed)."""
    if os.environ.get("LMPLAB_NO_MLFLOW"):
        return None
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    try:
        import mlflow
    except ImportError:
        return None
    paths = settings.paths
    mlflow.set_tracking_uri(paths.mlflow_uri)
    experiment = "alberta-basis-surrogate"
    if mlflow.get_experiment_by_name(experiment) is None:
        mlflow.create_experiment(experiment, artifact_location=paths.mlflow_artifacts.resolve().as_uri())
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name="lightgbm-cqr") as run:
        mlflow.log_params({**PARAMS, "features": len(FEATURES), "coverage_target": report["intervals"]["target"],
                           "train_rows": report["split"]["train"]["rows"]})
        surrogate = report["point"]["LightGBM (P50)"]
        mlflow.log_metrics({"test_mae": surrogate["mae"], "test_mae_separated": surrogate["mae_separated"],
                            "skill_vs_uniform": surrogate["skill_vs_uniform"],
                            "coverage_raw": report["intervals"]["raw"]["coverage"],
                            "coverage_conformal": report["intervals"]["conformal"]["coverage"],
                            "interval_width": report["intervals"]["conformal"]["mean_width"],
                            "separation_auc": report["separation"]["auc"],
                            "separation_brier": report["separation"]["brier"]})
        for path in sorted(paths.models.glob("*")):
            mlflow.log_artifact(str(path), artifact_path="model")
        mlflow.log_artifact(str(paths.reports / "forecast.json"))
        return run.info.run_id
