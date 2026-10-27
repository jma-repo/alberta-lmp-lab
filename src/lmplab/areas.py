"""AESO transmission planning areas: names, hub regions, schematic map positions and adjacency.

Names and numbers follow AESO's Planning Regions map. Map positions are read off that map
(pixel coordinates, y pointing down) for a schematic bubble map; adjacency is approximate
geographic contiguity, used to keep pricing hubs connected, as AESO requires.
"""

from __future__ import annotations

NAMES = {
    4: "Medicine Hat", 6: "Calgary", 13: "Lloydminster", 17: "Rainbow Lake", 18: "High Level",
    19: "Peace River", 20: "Grande Prairie", 21: "High Prairie", 22: "Grande Cache", 23: "Valleyview",
    24: "Fox Creek", 25: "Fort McMurray", 26: "Swan Hills", 27: "Athabasca/Lac La Biche", 28: "Cold Lake",
    29: "Hinton/Edson", 30: "Drayton Valley", 31: "Wetaskiwin", 32: "Wainwright", 33: "Fort Saskatchewan",
    34: "Abraham Lake", 35: "Red Deer", 36: "Alliance/Battle River", 37: "Provost", 38: "Caroline",
    39: "Didsbury", 40: "Wabamun", 42: "Hanna", 43: "Sheerness", 44: "Seebe", 45: "Strathmore/Blackie",
    46: "High River", 47: "Brooks", 48: "Empress", 49: "Stavely", 52: "Vauxhall", 53: "Fort Macleod",
    54: "Lethbridge", 55: "Glenwood", 56: "Vegreville", 57: "Airdrie", 60: "Edmonton",
}

# AESO's six planning regions, with South split the way AESO's hub scenarios split it.
HUB_REGION = {
    **{a: "Northwest" for a in (17, 18, 19, 20, 21, 22, 23, 24, 26)},
    **{a: "Northeast" for a in (25, 27, 33)},
    **{a: "Edmonton" for a in (31, 40, 60)},
    **{a: "Calgary" for a in (6, 57)},
    **{a: "Central" for a in (13, 28, 29, 30, 32, 34, 35, 36, 37, 38, 39, 42, 56)},
    **{a: "Southwest" for a in (44, 46, 49, 53, 54, 55)},
    **{a: "Southeast" for a in (4, 43, 45, 47, 48, 52)},
}

MAP_XY = {
    17: (168, 348), 18: (281, 370), 25: (441, 441), 19: (213, 525), 20: (142, 616), 23: (221, 650),
    21: (294, 638), 26: (294, 700), 24: (229, 717), 22: (134, 730), 27: (424, 685), 40: (340, 758),
    60: (385, 765), 33: (424, 760), 28: (523, 693), 56: (478, 770), 13: (537, 782), 29: (217, 795),
    30: (305, 830), 31: (403, 830), 32: (507, 830), 37: (545, 858), 35: (380, 862), 36: (455, 865),
    34: (262, 903), 38: (325, 903), 39: (370, 922), 42: (469, 912), 57: (370, 958), 6: (370, 978),
    44: (314, 982), 43: (481, 967), 45: (408, 995), 47: (476, 1008), 48: (546, 1002), 46: (364, 1032),
    49: (408, 1052), 52: (486, 1070), 4: (546, 1080), 53: (376, 1105), 54: (440, 1083), 55: (440, 1130),
}

_NEIGHBOURS = {
    17: (18, 19), 18: (17, 19, 25), 19: (17, 18, 20, 21, 23, 25), 20: (19, 22, 23), 21: (19, 23, 26, 27),
    22: (20, 23, 24, 29), 23: (19, 20, 21, 22, 24, 26), 24: (22, 23, 26, 29), 26: (21, 23, 24, 27, 29, 40),
    25: (18, 19, 27, 28), 27: (21, 25, 26, 28, 33, 40, 56, 60), 28: (13, 25, 27, 56), 33: (27, 31, 56, 60),
    40: (26, 27, 29, 30, 31, 60), 60: (27, 31, 33, 40), 31: (30, 33, 35, 36, 40, 56, 60),
    29: (22, 24, 26, 30, 34, 40), 30: (29, 31, 34, 35, 38, 40), 56: (13, 27, 28, 31, 32, 33, 36),
    13: (28, 32, 56), 32: (13, 36, 37, 42, 56), 37: (32, 36, 42), 36: (31, 32, 35, 37, 42, 56),
    35: (30, 31, 36, 38, 39, 42), 34: (29, 30, 38, 44), 38: (30, 34, 35, 39, 44), 39: (35, 38, 42, 44, 57),
    42: (32, 35, 36, 37, 39, 43, 45, 48, 57), 57: (6, 39, 42, 44, 45), 6: (44, 45, 46, 57),
    44: (6, 34, 38, 39, 46, 53, 57), 43: (42, 45, 47, 48), 45: (6, 42, 43, 46, 47, 49, 57),
    46: (6, 44, 45, 49, 53), 47: (43, 45, 48, 49, 52), 48: (4, 42, 43, 47), 49: (45, 46, 47, 52, 53, 54),
    52: (4, 47, 49, 54, 55), 4: (48, 52, 55), 53: (44, 46, 49, 54, 55), 54: (49, 52, 53, 55),
    55: (4, 52, 53, 54),
}

AREAS = sorted(NAMES)
HUB_REGIONS = ["Northwest", "Northeast", "Edmonton", "Calgary", "Central", "Southwest", "Southeast"]


def adjacency() -> set[tuple[int, int]]:
    """Undirected neighbour pairs (a, b) with a < b."""
    pairs = set()
    for area, neighbours in _NEIGHBOURS.items():
        for other in neighbours:
            pairs.add((min(area, other), max(area, other)))
    return pairs


def map_position(area: int) -> tuple[float, float]:
    x, y = MAP_XY[area]
    return float(x), float(1235 - y)  # flip so north is up in a chart
