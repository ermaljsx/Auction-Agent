"""Regression tests for blocker B2 of the v2 brief: the CT line of a car under 4 years old.

The line used to say « CT pas encore exigé (moins de 4 ans) » whatever the published CT said,
so lot 847 (a voluntary CT with 6 major defects) read as if it had no CT. When a CT was parsed,
the line now says what it found, and « CT volontaire » when it was a voluntary inspection.
"""
import json

import pytest

from auction_agent.report import ct_line
from tests.test_mvp import _publish_seeded
from tests.test_regressions_site_data import _failed_ct
from tests.test_scoring import FAV

YOUNG_CODES = [{"code": "ct_not_required"}]
YOUNG_FAILED_CT_LINE = "CT volontaire défavorable, 6 défaillances majeures (pas encore exigé)"


def _ct(result, nature="volontaire", **counts):
    return {"result": result, "nature": nature, "counts": {"minor": 0, "major": 0, "critical": 0, **counts}}


@pytest.mark.parametrize("ct, codes, expected", [
    (_ct("FAVORABLE", minor=2), YOUNG_CODES, "CT volontaire favorable, 2 défaillances mineures (pas encore exigé)"),
    (_ct("FAVORABLE"), YOUNG_CODES, "CT volontaire favorable, sans défaillance (pas encore exigé)"),
    (_ct("UNKNOWN"), YOUNG_CODES, "CT volontaire : résultat non lu (pas encore exigé)"),
    (_ct(None), YOUNG_CODES, "CT volontaire : résultat non lu (pas encore exigé)"),
    (_ct("FAVORABLE", nature="periodique", minor=1), YOUNG_CODES,
     "CT favorable, 1 défaillance mineure (pas encore exigé)"),
    (_ct("DEFAVORABLE_MAJEURE", major=6, minor=5), [{"code": "ct_young_failed"}],
     "CT volontaire défavorable, 6 défaillances majeures (pas encore exigé)"),
    (None, YOUNG_CODES, "CT pas encore exigé (moins de 4 ans)"),  # no CT published: unchanged
    (_ct("FAVORABLE", nature="periodique"), [], "CT favorable, sans défaillance"),  # older car: unchanged
    (_ct("DEFAVORABLE_MAJEURE", nature="periodique", major=2), [], "CT défavorable, 2 défaillances majeures"),
])
def test_ct_line_follows_the_parsed_ct(ct, codes, expected):
    assert ct_line(ct, codes) == expected


def test_a_parsed_ct_is_never_hidden_behind_pas_encore_exige():
    for result in ("FAVORABLE", "DEFAVORABLE_MAJEURE", "DEFAVORABLE_CRITIQUE", "UNKNOWN"):
        line = ct_line(_ct(result, major=1, critical=1), YOUNG_CODES + [{"code": "ct_young_failed"}])
        assert line != "CT pas encore exigé (moins de 4 ans)" and line.startswith("CT volontaire")


@pytest.mark.parametrize("ct, expected", [
    (_failed_ct(), YOUNG_FAILED_CT_LINE),  # lot 847
    (dict(FAV, nature="volontaire", parse_confidence=0.6), None),  # passed: starts "CT volontaire favorable"
    (dict(_failed_ct(), result="UNKNOWN"), "CT volontaire : résultat non lu (pas encore exigé)"),  # lots 508, 519
])
def test_young_car_ct_line_through_publish(settings, tmp_path, ct, expected):
    def young(conn):
        conn.execute("UPDATE vehicles SET year = 2024, first_registration = '2024-02-01' WHERE id = 1")
        conn.execute("UPDATE documents SET parsed_json = ? WHERE vehicle_id = 1 AND doc_type = 'ct'", (json.dumps(ct),))

    snap = _publish_seeded(settings, tmp_path, prepare=young)
    car = next(c for c in snap["top"] + snap["cards"] if c["mileage"] == 60_000)
    if expected is None:
        assert car["ct_line"].startswith("CT volontaire favorable") and car["ct_line"].endswith("(pas encore exigé)")
    else:
        assert car["ct_line"] == expected
