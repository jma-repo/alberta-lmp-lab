"""Security-constrained economic dispatch (SCED) with locational marginal prices.

A DC optimal power flow solved as a linear program (HiGHS through SciPy). Prices are read
straight from the LP duals and split the way AESO publishes them:

    LMP_k = MCE + MCL_k + MCC_k
    MCE   = lambda, the energy price at the reference bus
    MCL_k = -lambda * MLF_k, with MLF_k the marginal loss factor of bus k (0 when lossless)
    MCC_k = sum over binding limits of -mu * shift factor (N-0) and -gamma * post-contingency
            shift factor (N-1)

Sensitivities come from line reactances: PTDF (injection shift factors relative to the reference
bus) and LODF (line outage distribution factors), which give the post-contingency shift factors
for N-1 security constraints. Losses use the standard iterative loss-factor formulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linprog

VOLL = 3000.0      # $/MWh, AESO's overall price cap: unserved load is priced here
BASE_MVA = 100.0
TOL = 1e-6


@dataclass(frozen=True)
class Line:
    name: str
    start: str
    end: str
    x: float                         # reactance, p.u. on BASE_MVA
    limit: float                     # normal rating, MW, either direction
    emergency: float | None = None   # post-contingency rating, MW (defaults to the normal rating)
    r: float = 0.0                   # resistance, p.u. (losses)
    contingency: bool = True         # whether its outage is studied under N-1


@dataclass(frozen=True)
class Offer:
    unit: str
    bus: str
    mw: float
    price: float


@dataclass(frozen=True)
class Network:
    buses: list[str]
    lines: list[Line]
    reference: str

    def index(self) -> dict[str, int]:
        return {bus: i for i, bus in enumerate(self.buses)}


@dataclass
class Dispatch:
    dispatch: dict[str, float]
    flows: dict[str, float]
    lmp: dict[str, float]
    mce: float
    mcl: dict[str, float]
    mcc: dict[str, float]
    binding: list[dict[str, Any]]
    cost: float
    losses_mw: float
    shed: dict[str, float]
    surplus: float                   # merchandising surplus: load payments minus generator payments
    contingencies: list[str] = field(default_factory=list)
    iterations: int = 1

    def table(self) -> list[dict[str, Any]]:
        return [{"bus": bus, "lmp": round(self.lmp[bus], 2), "mce": round(self.mce, 2),
                 "mcl": round(self.mcl[bus], 2), "mcc": round(self.mcc[bus], 2)} for bus in self.lmp]


# --- network sensitivities -------------------------------------------------------------------

def incidence(network: Network) -> np.ndarray:
    index = network.index()
    matrix = np.zeros((len(network.lines), len(network.buses)))
    for row, line in enumerate(network.lines):
        matrix[row, index[line.start]], matrix[row, index[line.end]] = 1.0, -1.0
    return matrix


def ptdf(network: Network) -> np.ndarray:
    """Lines x buses: MW of flow on each line per MW injected at each bus and withdrawn at the reference bus."""
    a = incidence(network)
    susceptance = np.diag([1.0 / line.x for line in network.lines])
    b_bus = a.T @ susceptance @ a
    keep = [i for i, bus in enumerate(network.buses) if bus != network.reference]
    reactance = np.zeros_like(b_bus)
    reactance[np.ix_(keep, keep)] = np.linalg.inv(b_bus[np.ix_(keep, keep)])
    return susceptance @ a @ reactance


def lodf(network: Network, shift: np.ndarray | None = None) -> np.ndarray:
    """Lines x outaged lines: share of the outaged line's pre-contingency flow picked up by each line.
    Columns are NaN for lines whose outage would island part of the network."""
    shift = ptdf(network) if shift is None else shift
    transfer = shift @ incidence(network).T          # flow on l for a 1 MW transfer across line k
    denominator = 1.0 - np.diag(transfer)
    factors = np.full_like(transfer, np.nan)
    for k in range(len(network.lines)):
        if abs(denominator[k]) > 1e-9:
            factors[:, k] = transfer[:, k] / denominator[k]
            factors[k, k] = -1.0
    return factors


def contingency_list(network: Network, contingencies: str | list[str] | None) -> list[str]:
    if not contingencies:
        return []
    if contingencies == "all":
        return [line.name for line in network.lines if line.contingency]
    return list(contingencies)


# --- dispatch --------------------------------------------------------------------------------

def solve(network: Network, offers: list[Offer], loads: dict[str, float],
          contingencies: str | list[str] | None = None, losses: bool = False, iterations: int = 8) -> Dispatch:
    """Least-cost dispatch that respects every line limit before (N-0) and after (N-1) each studied outage."""
    index = network.index()
    names = [line.name for line in network.lines]
    shift = ptdf(network)
    rows: list[tuple[str, str | None]] = [(name, None) for name in names]
    sensitivity = [shift[row] for row in range(len(names))]
    limits = [line.limit for line in network.lines]
    studied = []
    outages = contingency_list(network, contingencies)
    if outages:
        factors = lodf(network, shift)
        for outage in outages:
            k = names.index(outage)
            if np.isnan(factors[:, k]).any():
                continue                           # the outage islands load or generation: not a secure-able case
            studied.append(outage)
            post = shift + np.outer(factors[:, k], shift[k])
            for row, line in enumerate(network.lines):
                if row != k:
                    rows.append((line.name, outage))
                    sensitivity.append(post[row])
                    limits.append(line.emergency if line.emergency is not None else line.limit)
    s = np.array(sensitivity)
    limit = np.array(limits)
    demand = np.array([float(loads.get(bus, 0.0)) for bus in network.buses])

    n_offers, n_buses = len(offers), len(network.buses)
    placement = np.zeros((n_buses, n_offers + n_buses))   # bus injection from each variable
    for j, offer in enumerate(offers):
        placement[index[offer.bus], j] = 1.0
    placement[:, n_offers:] = np.eye(n_buses)              # unserved load acts as an injection at VOLL
    cost = np.concatenate([[offer.price for offer in offers], np.full(n_buses, VOLL)])
    bounds = [(0.0, offer.mw) for offer in offers] + [(0.0, max(d, 0.0)) for d in demand]
    resistance = np.array([line.r for line in network.lines])

    mlf, offset, used = np.zeros(n_buses), 0.0, 0
    while True:
        used += 1
        flow_rows = s @ placement
        result = linprog(cost, A_ub=np.vstack([flow_rows, -flow_rows]),
                         b_ub=np.concatenate([limit + s @ demand, limit - s @ demand]),
                         A_eq=((1.0 - mlf) @ placement)[None, :], b_eq=[(1.0 - mlf) @ demand + offset],
                         bounds=bounds, method="highs")
        if result.status != 0:
            raise RuntimeError(f"SCED did not solve: {result.message}")
        injection = placement @ result.x - demand
        flows = shift @ injection
        if not losses or used >= iterations:
            break
        loss_mw = float(resistance @ flows**2 / BASE_MVA)
        new_mlf = (2.0 * resistance * flows / BASE_MVA) @ shift
        new_offset = loss_mw - new_mlf @ injection
        if np.abs(new_mlf - mlf).max() < 1e-6 and abs(new_offset - offset) < 1e-3:
            break
        mlf, offset = new_mlf, new_offset

    lam = float(result.eqlin.marginals[0])
    marginal = result.ineqlin.marginals
    upper, lower = marginal[:len(rows)], marginal[len(rows):]
    congestion = (upper - lower) @ s
    lmp = lam * (1.0 - mlf) + congestion
    binding = []
    for row, (line, outage) in enumerate(rows):
        for value, direction in ((upper[row], "forward"), (lower[row], "reverse")):
            if abs(value) > TOL:
                binding.append({"line": line, "contingency": outage, "direction": direction,
                                "shadow_price": round(-float(value), 4),
                                "flow": round(float(s[row] @ injection), 2), "limit": float(limit[row])})
    dispatch: dict[str, float] = {}
    for j, offer in enumerate(offers):
        dispatch[offer.unit] = dispatch.get(offer.unit, 0.0) + float(result.x[j])
    return Dispatch(
        dispatch=dispatch,
        flows={name: float(flows[row]) for row, name in enumerate(names)},
        lmp={bus: float(lmp[i]) for i, bus in enumerate(network.buses)},
        mce=lam,
        mcl={bus: float(-lam * mlf[i]) for i, bus in enumerate(network.buses)},
        mcc={bus: float(congestion[i]) for i, bus in enumerate(network.buses)},
        binding=binding,
        cost=float(cost @ result.x),
        losses_mw=float(resistance @ flows**2 / BASE_MVA) if losses else 0.0,
        shed={bus: float(result.x[n_offers + i]) for i, bus in enumerate(network.buses) if result.x[n_offers + i] > TOL},
        surplus=float(-(lmp @ injection)),
        contingencies=studied,
        iterations=used,
    )


# --- AESO's worked examples ------------------------------------------------------------------

AESO_PUBLISHED = {   # LMPs ($/MWh) in AESO's 14 Sep 2026 LMP stakeholder session, slides 34-45
    "Example 1: no binding constraint": {"A": 40.0, "B": 40.0, "C": 40.0},
    "Example 2: N-0 limit on line Y": {"A": 10.0, "B": 40.0, "C": 40.0},
    "Example 3: N-1 limit on line X for loss of line Y": {"A": 10.0, "B": 40.0, "C": 40.0},
}


def aeso_three_bus(line_y_limit: float = 200.0) -> tuple[Network, list[Offer], dict[str, float]]:
    """AESO's three-bus system: G1 at A ($10, 200 MW), G2 at B ($40, 300 MW), 250 MW of load at C,
    lines X (A-B), Y (A-C), Z (B-C) of equal reactance, reference bus A, lossless."""
    network = Network(buses=["A", "B", "C"], reference="A", lines=[
        Line("X", "A", "B", 1.0, 100.0), Line("Y", "A", "C", 1.0, line_y_limit), Line("Z", "B", "C", 1.0, 300.0)])
    return network, [Offer("G1", "A", 200.0, 10.0), Offer("G2", "B", 300.0, 40.0)], {"C": 250.0}


def aeso_examples() -> list[dict[str, Any]]:
    """Solve the three examples and compare with the published prices, checking each LMP by
    re-dispatching with one more MW of load at the bus (the definition of an LMP)."""
    cases = [("Example 1: no binding constraint", 200.0, None),
             ("Example 2: N-0 limit on line Y", 100.0, None),
             ("Example 3: N-1 limit on line X for loss of line Y", 200.0, ["Y"])]
    out = []
    for name, y_limit, contingencies in cases:
        network, offers, loads = aeso_three_bus(y_limit)
        base = solve(network, offers, loads, contingencies)
        incremental = {}
        for bus in network.buses:
            bumped = dict(loads)
            bumped[bus] = bumped.get(bus, 0.0) + 1.0
            incremental[bus] = round(solve(network, offers, bumped, contingencies).cost - base.cost, 4)
        out.append({
            "name": name, "line_y_limit": y_limit, "contingencies": contingencies or [],
            "dispatch": {unit: round(mw, 2) for unit, mw in base.dispatch.items()},
            "flows": {line: round(mw, 2) for line, mw in base.flows.items()},
            "prices": base.table(), "binding": base.binding,
            "incremental_cost": incremental, "published": AESO_PUBLISHED[name],
            "matches_published": all(abs(base.lmp[bus] - AESO_PUBLISHED[name][bus]) < 0.01 for bus in network.buses),
        })
    return out
