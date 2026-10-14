"""The .env loader, the LLM settings and the CLI status line (no values ever printed)."""
import codecs

import pytest

from auction_agent import cli
from auction_agent.config import DEFAULT_LLM_MODEL, Settings, load_env_file
from tests.conftest import PROJECT_ROOT

CONTENT = """# comment
OPENAI_API_KEY = "sk-from-file"
export LLM_MODEL=gpt-6-luna
LLM_LIMIT='12'
EMPTY=
not a valid line
BUYER_PROFILE=pro
"""


def test_env_file_rules(tmp_path):
    path = tmp_path / ".env"
    path.write_text(CONTENT, encoding="utf-8")
    env = {"BUYER_PROFILE": "particulier"}
    report = load_env_file(path, env)
    assert env["OPENAI_API_KEY"] == "sk-from-file" and env["LLM_MODEL"] == "gpt-6-luna" and env["LLM_LIMIT"] == "12"
    assert "EMPTY" not in env
    assert env["BUYER_PROFILE"] == "particulier" and report.kept_existing == ["BUYER_PROFILE"]
    assert report.loaded == ["OPENAI_API_KEY", "LLM_MODEL", "LLM_LIMIT"]
    assert len(report.warnings) == 1 and "line 6" in report.warnings[0]
    assert "sk-from-file" not in repr(report)


@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
])
def test_notepad_and_powershell_encodings(tmp_path, encoding, bom):
    path = tmp_path / ".env"
    path.write_bytes(bom + "OPENAI_API_KEY=sk-x\r\n".encode(encoding))
    env: dict = {}
    assert load_env_file(path, env).loaded == ["OPENAI_API_KEY"] and env["OPENAI_API_KEY"] == "sk-x"


def test_unreadable_or_missing_file_never_crashes(tmp_path):
    bad = tmp_path / ".env"
    bad.write_bytes(b"OPENAI_API_KEY=\xff\xfe\xfa\n\xc3")
    env: dict = {}
    report = load_env_file(bad, env)
    assert env == {} and report.warnings and "UTF-8" in report.warnings[0]
    assert load_env_file(tmp_path / "missing.env", env).warnings == []
    folder = tmp_path / "folder.env"
    folder.mkdir()
    assert load_env_file(folder, env).loaded == []


def test_windows_txt_extension_is_flagged(tmp_path):
    (tmp_path / ".env.txt").write_text("OPENAI_API_KEY=sk-x\n", encoding="utf-8")
    report = load_env_file(tmp_path / ".env", {})
    assert report.loaded == [] and ".env.txt" in report.warnings[0]


def test_committed_template_holds_no_secret():
    env: dict = {}
    report = load_env_file(PROJECT_ROOT / ".env.example", env)
    assert env == {} and report.loaded == [] and report.warnings == []
    assert "OPENAI_API_KEY=" in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")


# ------------------------------------------------------------------ settings

def test_llm_settings_defaults_and_overrides(monkeypatch, tmp_path):
    s = Settings.from_env(data_dir=tmp_path)
    assert s.llm_model == DEFAULT_LLM_MODEL == "gpt-6-luna" and s.llm_reasoning_effort == "none"
    assert not s.llm_enabled
    monkeypatch.setenv("OPENAI_API_KEY", '  "sk-pasted"  ')
    monkeypatch.setenv("LLM_MODEL", " gpt-5.4-nano ")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "LOW")
    s = Settings.from_env(data_dir=tmp_path)
    assert s.openai_api_key == "sk-pasted" and s.llm_model == "gpt-5.4-nano" and s.llm_reasoning_effort == "low"
    assert s.llm_enabled and "sk-pasted" not in repr(s)
    monkeypatch.setenv("LLM_MODEL", "")
    assert Settings.from_env(data_dir=tmp_path).llm_model == "gpt-6-luna"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "turbo")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        Settings.from_env(data_dir=tmp_path)


def test_gemini_variables_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GEMINI_PAID_TIER", "1")
    assert not Settings.from_env(data_dir=tmp_path).llm_enabled


# ------------------------------------------------------------------ CLI

def test_cli_loads_env_file_and_reports_the_key_source(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "my.env"
    env_file.write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("AUCTION_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUCTION_AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("OPENAI_API_KEY", "")  # registered, so the loader's write is undone after the test
    assert cli.main(["score"]) == 0
    out = capsys.readouterr().out
    assert "LLM: gpt-6-luna, reasoning effort none, key from my.env" in out
    assert "sk-from-file" not in out


def test_environment_key_wins_over_env_file(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("AUCTION_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")
    assert cli.main(["score", "--llm-limit", "0"]) == 0
    out = capsys.readouterr().out
    assert "key from the environment" in out and "sk-from" not in out


def test_status_line_without_key(tmp_path):
    s = Settings.from_env(data_dir=tmp_path)
    assert cli.llm_status(s) == "LLM: gpt-6-luna, reasoning effort none, key not set (see .env.example)"


def test_notes_with_markup_characters_do_not_crash_the_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("auction_agent.scoring.service.rescore_all",
                        lambda *a, **k: {"scored": 0, "green": 0, "yellow": 0, "red": 0, "llm_calls": 0,
                                         "llm_cached": 0, "notes": ["OpenAI said [/bold] [red]oops"]})
    assert cli.main(["score", "--no-llm"]) == 0
    assert "[/bold] [red]oops" in capsys.readouterr().out
