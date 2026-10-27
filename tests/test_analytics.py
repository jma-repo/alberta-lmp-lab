from collections import Counter

import numpy as np
import pandas as pd
import pytest

from lmplab import analytics
from lmplab.areas import AREAS, adjacency


@pytest.fixture(scope="module")
def prices():
    """Synthetic hourly prices with a cheap south-east and a dear north-west, like the backcast."""
    rng = np.random.default_rng(7)
    hours = 24 * 60
    system = 45 + 10 * rng.standard_normal(hours)
    frame = {}
    for area in AREAS:
        local = 0.0
        if area in (4, 43, 47, 48, 52):
            local = -12 * (rng.random(hours) < 0.3)
        elif area in (17, 18, 19, 20):
            local = 15 * (rng.random(hours) < 0.2)
        frame[area] = system + local + rng.standard_normal(hours)
    return pd.DataFrame(frame)


def connected(members: list[int]) -> bool:
    links = {a: set() for a in members}
    for a, b in adjacency():
        if a in links and b in links:
            links[a].add(b)
            links[b].add(a)
    seen, todo = set(), [members[0]]
    while todo:
        area = todo.pop()
        if area not in seen:
            seen.add(area)
            todo.extend(links[area] - seen)
    return seen == set(members)


@pytest.mark.parametrize("k", [2, 4, 6])
def test_clustered_hubs_are_k_liquid_connected_regions(prices, k):
    assignment = analytics.cluster_hubs(prices, k)
    sizes = Counter(assignment.values())
    assert set(assignment) == set(AREAS)
    assert len(sizes) == k
    assert min(sizes.values()) >= analytics.MIN_HUB_AREAS
    for hub in sizes:
        assert connected([a for a, h in assignment.items() if h == hub]), hub


def test_hub_metrics_reward_grouping_similar_areas(prices):
    by_price = {a: ("low" if a in (4, 43, 47, 48, 52) else "high" if a in (17, 18, 19, 20) else "mid") for a in AREAS}
    scrambled = {a: ["low", "mid", "high"][i % 3] for i, a in enumerate(AREAS)}
    good, bad = analytics.hub_metrics(prices, by_price), analytics.hub_metrics(prices, scrambled)
    assert good["basis_deviation"] < bad["basis_deviation"]
    assert good["min_separation"] > bad["min_separation"]


def test_settlement_surplus_is_zero_without_separation():
    hourly = pd.DataFrame({"ts": [0, 0], "lmp": [40.0, 40.0], "alp": [40.0, 40.0],
                           "sysgen_mw": [100.0, 0.0], "load_mw": [0.0, 100.0]})
    assert analytics.settlement_surplus(hourly)["surplus"].iloc[0] == pytest.approx(0.0)


def test_settlement_surplus_counts_the_discount_trapped_generation_takes():
    hourly = pd.DataFrame({"ts": [0, 0], "lmp": [10.0, 50.0], "alp": [50.0, 50.0],
                           "sysgen_mw": [100.0, 0.0], "load_mw": [0.0, 100.0]})
    assert analytics.settlement_surplus(hourly)["surplus"].iloc[0] == pytest.approx(100 * (50 - 10))
