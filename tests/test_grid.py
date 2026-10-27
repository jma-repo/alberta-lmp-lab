import numpy as np
import pytest

from lmplab import grid, sced
from lmplab.config import settings


@pytest.fixture(scope="module")
def case():
    return grid.load_case(settings().paths.configs / "alberta7.yaml")


def test_parallel_circuits_form_one_corridor_and_uprate_together(case):
    corridor = grid.corridors(case)
    assert corridor["NW-EDM 1"] == corridor["NW-EDM 2"] == "NW-EDM"
    lines = {line.name: line for line in grid.network(case, {"NW-EDM": 100}).lines}
    assert lines["NW-EDM 1"].limit == lines["NW-EDM 2"].limit == 450
    assert lines["NE-EDM 1"].limit == 1200


def test_monte_carlo_hours_are_reproducible_and_wind_is_correlated(case):
    first, again = grid.sample_hours(case, 42), grid.sample_hours(case, 42)
    assert first == again
    sw = np.array([h["wind_sw"] for h in first])
    se = np.array([h["wind_se"] for h in first])
    assert 0.4 < np.corrcoef(sw, se)[0, 1] < 0.8
    assert all(0 <= h["solar"] <= 0.75 for h in first)


def test_windy_hour_traps_cheap_power_in_the_south(case):
    hour = case["showcase_hours"]["Windy spring afternoon"]
    result = grid.dispatch_hour(case, {}, hour)
    assert result.lmp["SW"] < result.lmp["CEN"] and result.lmp["SE"] < result.lmp["CEN"]
    assert result.binding
    assert len(result.contingencies) == len(case["lines"])


def test_dual_screening_ranks_the_binding_corridor_and_ignores_the_slack_one(case):
    hours = grid.sample_hours(case, 42)[:30]
    wind = {"add_capacity": {"SE wind": 400}}
    before, after = grid.summarize(case, {}, hours), grid.summarize(case, wind, hours)
    rise = {c: v - before["rating_value_kusd_per_mw_year"].get(c, 0.0) for c, v in after["rating_value_kusd_per_mw_year"].items()}
    top = max(rise, key=rise.get)
    assert top in ("SW-SE", "CAL-SW")             # the limits south-east wind runs into
    ranked = grid.summarize(case, {**wind, "uprate": {top: 400}}, hours)
    slack = grid.summarize(case, {**wind, "uprate": {"CEN-SE": 400}}, hours)
    assert ranked["production_cost_per_hour"] < after["production_cost_per_hour"]
    # CEN-SE only appears as an outage, never as a binding limit, so its rating is worth nothing.
    assert slack["production_cost_per_hour"] == pytest.approx(after["production_cost_per_hour"])


def test_radial_outage_is_not_studied():
    network = sced.Network(buses=["A", "B"], reference="A", lines=[sced.Line("AB", "A", "B", 0.1, 100.0)])
    result = sced.solve(network, [sced.Offer("G", "A", 200.0, 20.0)], {"B": 50.0}, contingencies="all")
    assert result.contingencies == []
    assert result.lmp == pytest.approx({"A": 20.0, "B": 20.0})
