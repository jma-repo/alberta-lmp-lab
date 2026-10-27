import numpy as np
import pandas as pd
import pytest

from lmplab import forecast


def test_conformal_offsets_reach_the_target_coverage():
    rng = np.random.default_rng(3)
    n = 4000
    actual = rng.standard_normal(n) * np.where(np.arange(n) % 2, 1.0, 3.0)
    groups = np.where(np.arange(n) % 2, "calm", "volatile")
    low, high = np.full(n, -0.5), np.full(n, 0.5)          # far too narrow on purpose
    offsets = forecast.conformal_offsets(low[: n // 2], high[: n // 2], actual[: n // 2], groups[: n // 2], 0.8)
    widen = np.array([offsets[g] for g in groups[n // 2:]])
    inside = (actual[n // 2:] >= low[n // 2:] - widen) & (actual[n // 2:] <= high[n // 2:] + widen)
    assert offsets["volatile"] > offsets["calm"] > 0
    for g in ("calm", "volatile"):
        assert inside[groups[n // 2:] == g].mean() == pytest.approx(0.8, abs=0.03)


def test_rolling_offsets_only_use_settled_days_and_adapt_within_a_week():
    days = pd.date_range("2025-10-01", periods=50, freq="D")
    ts = pd.date_range("2025-10-01", periods=50 * 24, freq="h")
    score = np.where(ts >= pd.Timestamp("2025-11-05"), 100.0, 1.0)   # a regime change on 5 Nov
    scores = pd.DataFrame({"ts": ts, "area_id": 4, "score": score})
    offsets = forecast.rolling_offsets(scores, days[10:], coverage=0.8, window=30).set_index("day")["offset"]
    assert offsets[pd.Timestamp("2025-11-05")] == pytest.approx(1.0)    # 5 Nov's own scores are not known yet
    assert offsets[pd.Timestamp("2025-11-08")] == pytest.approx(1.0)    # 3 of 30 days: still under the 80% quantile
    assert offsets[pd.Timestamp("2025-11-13")] == pytest.approx(100.0)  # 8 of 30 days: the window has caught up


def test_history_features_are_lagged_a_full_day():
    ts = pd.date_range("2025-01-01", periods=24 * 10, freq="h")
    hourly = pd.DataFrame({"ts": ts, "area_id": 4, "hub_region": "Southeast", "load_mw": 10.0, "net_gen_mw": 5.0,
                           "lmp": np.arange(len(ts), dtype=float), "alp": 0.0})
    hourly["basis"] = hourly["lmp"]
    system = pd.DataFrame({"ts": ts, "load_mw": 100.0, "net_import_mw": 0.0})
    frame = forecast.build_frame(hourly, system, threshold=5.0)
    row = frame.iloc[100]
    assert row["basis_lag24"] == pytest.approx(row["basis"] - 24)
    assert row["basis_mean_7d"] <= row["basis"] - 24       # the trailing week ends a day before
