"""The live smoke workflow's shell logic (no YAML library needed, no network)."""
import re
import shutil
import subprocess

import pytest

from tests.conftest import PROJECT_ROOT

SMOKE = (PROJECT_ROOT / ".github" / "workflows" / "smoke.yml").read_text(encoding="utf-8")


def _recon_line() -> str:
    m = re.search(r"run: (python -m auction_agent recon[^\n]*)", SMOKE)
    assert m, "recon step missing"
    return m.group(1).replace("python -m auction_agent recon --room beauvais", "(exit $RC)")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("rc,sale_url,ok", [
    ("0", "", True),
    ("3", "", False),                       # nothing resolved and no URL given: stop
    ("3", "/salle-de-vente-encheres/beauvais/10231", True),  # a given URL still gets crawled
    ("2", "/salle-de-vente-encheres/beauvais/10231", False),  # a block always stops
])
def test_recon_step_tolerates_only_unresolved_sale_with_a_given_url(rc, sale_url, ok):
    result = subprocess.run(["bash", "-c", _recon_line()], env={"RC": rc, "SALE_URL": sale_url, "PATH": "/usr/bin:/bin"})
    assert (result.returncode == 0) is ok


def test_smoke_runs_only_by_hand_or_with_the_tag_and_logs_counts_only():
    assert "contains(github.event.head_commit.message, '[smoke]')" in SMOKE
    assert "--quiet" in SMOKE and "--llm-limit 5" in SMOKE and "--pages 1 --limit 3" in SMOKE
    assert "permissions:\n  contents: read" in SMOKE
