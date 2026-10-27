import numpy as np
import pytest

from lmplab import sced


def test_shift_factors_split_flow_by_reactance():
    network, _, _ = sced.aeso_three_bus()
    shift = sced.ptdf(network)
    # 1 MW from B to the reference bus A: 2/3 directly on X (B->A is negative), 1/3 round B-C-A.
    assert shift[:, 1] == pytest.approx([-2 / 3, -1 / 3, 1 / 3])
    assert shift[:, 0] == pytest.approx([0.0, 0.0, 0.0])


def test_outage_factors_move_the_whole_flow_onto_the_parallel_path():
    network, _, _ = sced.aeso_three_bus()
    factors = sced.lodf(network)
    # Losing Y (A-C) sends all of its flow round A-B-C: +1 on X and Z.
    assert factors[:, 1] == pytest.approx([1.0, -1.0, 1.0])


def test_example_1_unconstrained():
    network, offers, loads = sced.aeso_three_bus(200.0)
    result = sced.solve(network, offers, loads)
    assert result.dispatch == pytest.approx({"G1": 200.0, "G2": 50.0})
    assert result.flows == pytest.approx({"X": 50.0, "Y": 150.0, "Z": 100.0})
    assert result.lmp == pytest.approx({"A": 40.0, "B": 40.0, "C": 40.0})
    assert result.binding == []


def test_example_2_n0_constraint_dispatch_matches_aeso():
    network, offers, loads = sced.aeso_three_bus(100.0)
    result = sced.solve(network, offers, loads)
    assert result.dispatch == pytest.approx({"G1": 50.0, "G2": 200.0})
    assert result.flows == pytest.approx({"X": -50.0, "Y": 100.0, "Z": 150.0})
    assert result.mce == pytest.approx(10.0)
    assert [b["line"] for b in result.binding] == ["Y"]
    assert result.binding[0]["shadow_price"] == pytest.approx(90.0)


def test_example_2_price_at_c_is_70_not_40():
    """Serving 1 MW more at C with Y at its limit needs G2 +2 MW and G1 -1 MW: 2 x 40 - 10 = $70."""
    network, offers, loads = sced.aeso_three_bus(100.0)
    result = sced.solve(network, offers, loads)
    assert result.lmp == pytest.approx({"A": 10.0, "B": 40.0, "C": 70.0})
    assert result.mcc == pytest.approx({"A": 0.0, "B": 30.0, "C": 60.0})
    bumped = sced.solve(network, offers, {"C": 251.0})
    assert bumped.cost - result.cost == pytest.approx(70.0)
    assert bumped.dispatch == pytest.approx({"G1": 49.0, "G2": 202.0})


def test_example_3_n1_constraint_matches_aeso():
    network, offers, loads = sced.aeso_three_bus(200.0)
    result = sced.solve(network, offers, loads, contingencies=["Y"])
    assert result.dispatch == pytest.approx({"G1": 100.0, "G2": 150.0})
    assert result.flows == pytest.approx({"X": -50 / 3, "Y": 350 / 3, "Z": 400 / 3})
    assert result.lmp == pytest.approx({"A": 10.0, "B": 40.0, "C": 40.0})
    (limit,) = result.binding
    assert (limit["line"], limit["contingency"], limit["flow"]) == ("X", "Y", pytest.approx(100.0))


def test_published_comparison_flags_only_example_2():
    results = sced.aeso_examples()
    assert [r["matches_published"] for r in results] == [True, False, True]
    for r in results:   # every dual price equals the brute-force cost of one more MW
        for row in r["prices"]:
            assert row["lmp"] == pytest.approx(r["incremental_cost"][row["bus"]], abs=1e-6)


def test_load_is_shed_at_the_price_cap_when_the_network_cannot_deliver():
    # Line Y caps what reaches C at 450 MW (G1 150 + G2 300), below the 500 MW of offers.
    network, offers, _ = sced.aeso_three_bus(200.0)
    result = sced.solve(network, offers, {"C": 470.0})
    assert result.shed == pytest.approx({"C": 20.0})
    assert result.dispatch == pytest.approx({"G1": 150.0, "G2": 300.0})
    assert result.lmp["C"] == pytest.approx(sced.VOLL)


def test_losses_raise_prices_away_from_the_reference_bus():
    network, offers, loads = sced.aeso_three_bus(200.0)
    lossy = sced.Network(network.buses, [sced.Line(line.name, line.start, line.end, line.x, line.limit, r=0.01)
                                         for line in network.lines], network.reference)
    result = sced.solve(lossy, offers, loads, losses=True)
    assert result.losses_mw > 0
    assert result.mcl["A"] == pytest.approx(0.0)
    assert result.mcl["C"] > 0
    assert result.lmp["C"] == pytest.approx(result.mce + result.mcl["C"] + result.mcc["C"])
    assert sum(result.dispatch.values()) == pytest.approx(250.0 + result.losses_mw, rel=1e-3)


def test_merchandising_surplus_equals_congestion_rent():
    network, offers, loads = sced.aeso_three_bus(100.0)
    result = sced.solve(network, offers, loads)
    rent = sum(b["shadow_price"] * b["limit"] for b in result.binding)
    assert result.surplus == pytest.approx(rent)
    assert np.isclose(result.surplus, 250 * 70 - 50 * 10 - 200 * 40)
