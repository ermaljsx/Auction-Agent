"""Conformance of the battery-certificate parser on real texts, when the owner has some locally.

Put stored (redacted) certificate texts in tests/fixtures/battery/real/*.txt. The folder is
git-ignored: a real certificate names the car's model, the diagnostic place and, before
bat-1.0, a link to an unredacted copy. Without them this test is skipped. Failure messages
name files only.
"""
import pytest

from auction_agent.battery_parser import parse_battery_certificate
from auction_agent.privacy import redact_battery_text
from tests.conftest import FIXTURES

REAL = sorted((FIXTURES / "battery" / "real").glob("*.txt"))


@pytest.mark.skipif(not REAL, reason="no real battery texts in tests/fixtures/battery/real/ (git-ignored)")
def test_real_certificates_give_a_plausible_soh():
    problems = []
    for path in REAL:
        text = path.read_text(encoding="utf-8")
        out = parse_battery_certificate(text)
        soh, ratio = out["soh_pct"], out["capacity_ratio_pct"]
        if out["soh_source"] != "certificate" or soh is None or not 50 <= soh <= 100:
            problems.append(f"{path.name}: SoH not read ({out['warnings']})")
        elif ratio is not None and abs(soh - ratio) > 1:
            problems.append(f"{path.name}: SoH far from the capacity ratio")
        if parse_battery_certificate(redact_battery_text(text)) != out:
            problems.append(f"{path.name}: different reading from the stored text")
    assert not problems, "\n".join(problems)
