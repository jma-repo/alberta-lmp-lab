import os

import pytest

from lmplab import pdup
from lmplab.config import settings

os.environ.setdefault("LMPLAB_NO_LLM", "1")
PATHS = settings().paths
RULES = pdup.load_rules(PATHS.pdup / "rules.yaml")


def submission(name: str) -> dict:
    return pdup.load_submission(PATHS.pdup / "submissions" / f"{name}.yaml")


@pytest.fixture(scope="module")
def wind():
    return pdup.review(submission("prairie-ridge-wind"), RULES, seed=42)


@pytest.fixture(scope="module")
def solar():
    return pdup.review(submission("coulee-solar-storage"), RULES, seed=42)


def rules_with(result: dict, severity: str) -> set[str]:
    return {f["rule"] for f in result["findings"] if f["severity"] == severity}


def test_clean_submission_is_accepted(solar):
    assert solar["outcome"] == "Accepted"
    assert solar["findings"] == []
    assert solar["power_flow"]["export_margin_mvar"] > 0 and solar["short_circuit"]["scr"] > 3


def test_every_injected_error_is_found(wind):
    assert rules_with(wind, "error") == {"TOP-01", "TOP-02", "TOP-03", "LINE-01", "LINE-05", "XF-01",
                                         "DYN-04", "DYN-05", "PF-01", "PF-02"}
    assert rules_with(wind, "warning") == {"GEN-03", "DYN-03", "PF-03", "SC-01", "ML-01"}
    assert wind["outcome"] == "Resubmission required"


def test_probable_causes_name_the_data_entry_slip(wind):
    causes = {f["rule"]: f for f in wind["findings"]}
    assert causes["LINE-01"]["fix"]["value"] == pytest.approx(1.08 / (34.5**2 / 100), rel=1e-3)   # ohms -> p.u.
    assert causes["XF-01"]["fix"]["value"] == pytest.approx(10.0, abs=0.05)                        # system -> own base
    assert causes["TOP-02"]["fix"]["value"] == pytest.approx(0.69)                                 # volts -> kV
    assert causes["LINE-05"]["fix"]["value"] == {"summer": 400, "winter": 450}


def test_non_convergence_is_traced_to_one_correction(wind):
    restoring = [t["fix"]["name"] for t in wind["power_flow"]["fixes_tested"] if t["converged"]]
    assert restoring == ["GSU 2"]


def test_anomaly_model_catches_what_the_ranges_miss(wind):
    (flag,) = [f for f in wind["findings"] if f["rule"] == "ML-01"]
    assert flag["element"] == "Collector 1"
    assert not any(f["element"] == "Collector 1" for f in wind["findings"] if f["rule"].startswith("LINE"))


def test_checklist_covers_every_rule(wind):
    statuses = {row["rule"]: row["status"] for row in wind["checklist"]}
    assert set(statuses) == set(pdup.RULES)
    assert statuses["PF-01"] == "fail" and statuses["ML-01"] == "review" and statuses["GEN-01"] == "pass"


def test_vector_group_parsing():
    assert pdup._lv_delta("YNd1") and pdup._lv_delta("Dd0")
    assert not pdup._lv_delta("YNyn0") and not pdup._lv_delta("Dyn11")


def test_line_requirements_follow_the_topology():
    needed = pdup.line_requirements(submission("prairie-ridge-wind"))
    assert needed == {"Gen-tie": 224.0, "Collector 1": 112.0, "Collector 2": 112.0}


def test_letter_lists_deficiencies_most_material_first(wind):
    text = pdup.letter(wind, "Summary.", "template")
    assert "Resubmission required" in text
    assert "| 1 | PF-01 |" in text and "| 2 | PF-02 |" in text and "| 10 | TOP-01 |" in text
    assert "provisional" in text
