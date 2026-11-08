"""Conformance of the CT parser on real PV texts, when the owner has some locally.

Put redacted real texts in tests/fixtures/ct/real/*.txt (the folder is git-ignored: real
PVs name CT centres, controllers and sometimes owners, so they are never committed).
Without them this test is skipped. Failure messages name files and codes only.
"""
import re

import pytest

from auction_agent.ct_parser import RESULT_CRITICAL, RESULT_MAJOR, fold, parse_controle_technique
from auction_agent.privacy import redact
from tests.conftest import FIXTURES

REAL = sorted((FIXTURES / "ct" / "real").glob("*.txt"))
_PHRASE = {
    RESULT_CRITICAL: re.compile(r"DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?\s+CRITIQUES?"),
    RESULT_MAJOR: re.compile(r"DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?\s+MAJEURES?"),
}
_SEVERITY = {RESULT_CRITICAL: "critical", RESULT_MAJOR: "major"}


def stated_results(text: str) -> set[str]:
    """Results stated by a "défavorable pour défaillances ..." phrase (legend lines excluded)."""
    found: set[str] = set()
    for line in fold(text).splitlines():
        hits = {r for r, rx in _PHRASE.items() if rx.search(line)}
        if len(hits) == 1 and not re.search(r"\bFAVORABLE\b", line):
            found |= hits
    return found


@pytest.mark.skipif(not REAL, reason="no real CT texts in tests/fixtures/ct/real/ (git-ignored)")
def test_real_pvs_parse_and_unfavourable_results_are_backed_by_defects():
    problems = []
    for path in REAL:
        text = path.read_text(encoding="utf-8")
        out = parse_controle_technique(text)  # must not raise
        if any(w.startswith("parse_error") for w in out["warnings"]):
            problems.append(f"{path.name}: {out['warnings']}")
            continue
        # The stored text is redacted and re-parsed later: it must give the same safety reading.
        stored = parse_controle_technique(redact(text))
        if (stored["result"], stored["counts"]) != (out["result"], out["counts"]):
            problems.append(f"{path.name}: the redacted text reads {stored['result']} {stored['counts']}, "
                            f"the original {out['result']} {out['counts']}")
        stated = stated_results(text)
        if not stated:
            continue
        expected = RESULT_CRITICAL if RESULT_CRITICAL in stated else RESULT_MAJOR
        if out["result"] != expected:
            problems.append(f"{path.name}: result {out['result']}, the PV states {expected}")
        if not out["counts"][_SEVERITY[expected]]:
            problems.append(f"{path.name}: no {_SEVERITY[expected]} defect extracted "
                            f"(codes: {[d['code'] for d in out['defects']]})")
    assert not problems, f"{len(problems)} of {len(REAL)} real PVs:\n" + "\n".join(problems)
