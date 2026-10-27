import json
import shutil

import pytest

from lmplab.config import REPO_ROOT, Paths, Settings
from lmplab.report import build

REPORTS = REPO_ROOT / "artifacts" / "reports"
NEEDED = ["analytics.json", "forecast.json", "sced.json", "pdup.json", "agent.json", "data_quality.json"]


@pytest.mark.skipif(not all((REPORTS / name).exists() for name in NEEDED), reason="needs artifacts/reports")
def test_dashboard_builds_from_the_committed_reports(tmp_path):
    shutil.copytree(REPORTS, tmp_path / "artifacts" / "reports")
    page = build.run(Settings(Paths(repo=REPO_ROOT, work=tmp_path)))
    html = page.read_text(encoding="utf-8")
    start = html.index('<script id="dashboard-data" type="application/json">') + len('<script id="dashboard-data" type="application/json">')
    payload = html[start:html.index("</script>", start)]
    data = json.loads(payload.replace("<\\/", "</"))
    assert len(data["areas"]) == 42 and data["sced"]["aeso_examples"]
    assert "</" not in payload   # data can never close the script block
    for asset in ("app.js", "styles.css", "plotly.min.js", "favicon.svg"):
        assert (tmp_path / "site" / "assets" / asset).exists()
