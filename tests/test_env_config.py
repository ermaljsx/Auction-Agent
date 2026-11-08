"""The .env loader, the LLM settings and the CLI status line (no values ever printed)."""
import codecs

import pytest

from auction_agent import cli
from auction_agent.config import DEFAULT_LLM_MODEL, Settings, load_env_file
from auction_agent.scoring.llm import LLMConfigError, make_client
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
    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "ANTHROPIC_API_KEY=" in lines  # the Claude key slot, empty, comes first
    assert "# OPENAI_API_KEY=" in lines and "OPENAI_API_KEY=" not in lines  # the fallback stays commented
    assert lines.index("ANTHROPIC_API_KEY=") < lines.index("# OPENAI_API_KEY=")


# ------------------------------------------------------------------ settings

def test_llm_settings_defaults_and_overrides(monkeypatch, tmp_path):
    s = Settings.from_env(data_dir=tmp_path)
    assert s.llm_provider == "anthropic" and s.llm_model == DEFAULT_LLM_MODEL == "claude-haiku-5-5"
    assert s.llm_reasoning_effort == "low" and s.llm_limit == 80 and s.llm_key_name == "ANTHROPIC_API_KEY"
    assert not s.llm_enabled
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")  # the other provider's key does not enable Claude
    assert not Settings.from_env(data_dir=tmp_path).llm_enabled
    monkeypatch.setenv("ANTHROPIC_API_KEY", '  "sk-ant-pasted"  ')
    monkeypatch.setenv("LLM_REASONING_EFFORT", "MEDIUM")
    s = Settings.from_env(data_dir=tmp_path)
    assert s.anthropic_api_key == "sk-ant-pasted" and s.llm_api_key == "sk-ant-pasted"
    assert s.llm_reasoning_effort == "medium" and s.llm_enabled
    assert "sk-ant-pasted" not in repr(s) and "sk-openai" not in repr(s)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "turbo")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        Settings.from_env(data_dir=tmp_path)


def test_openai_provider_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", " OpenAI ")
    s = Settings.from_env(data_dir=tmp_path)
    assert s.llm_provider == "openai" and s.llm_model == "gpt-6-luna" and s.llm_reasoning_effort == "none"
    assert not s.llm_enabled and s.llm_key_name == "OPENAI_API_KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert not Settings.from_env(data_dir=tmp_path).llm_enabled
    monkeypatch.setenv("OPENAI_API_KEY", '  "sk-pasted"  ')
    monkeypatch.setenv("LLM_MODEL", " gpt-5.4-nano ")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "LOW")
    s = Settings.from_env(data_dir=tmp_path)
    assert s.openai_api_key == "sk-pasted" and s.llm_model == "gpt-5.4-nano" and s.llm_reasoning_effort == "low"
    assert s.llm_enabled and s.llm_api_key == "sk-pasted" and "sk-pasted" not in repr(s)
    monkeypatch.setenv("LLM_MODEL", "")
    assert Settings.from_env(data_dir=tmp_path).llm_model == "gpt-6-luna"
    assert Settings.from_env(data_dir=tmp_path, llm_provider="anthropic").llm_model == "claude-haiku-5-5"


def test_unknown_provider_is_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "foo")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Settings.from_env(data_dir=tmp_path)


@pytest.mark.parametrize("changes,expected", [
    ({"llm_model": "gpt-6-luna"}, "LLM_MODEL=gpt-6-luna is an OpenAI model"),
    ({"llm_model": "o4-mini"}, "is an OpenAI model"),
    ({"llm_reasoning_effort": "off"}, "LLM_REASONING_EFFORT=off is not used with Claude"),
    ({"llm_reasoning_effort": "xhigh"}, "LLM_REASONING_EFFORT=xhigh is not used with Claude"),
    ({"llm_provider": "openai", "llm_model": "claude-haiku-5-5"}, "is a Claude model"),
])
def test_leftover_settings_of_the_other_provider_are_non_fatal_config_errors(tmp_path, changes, expected):
    s = Settings.from_env(data_dir=tmp_path).replace(anthropic_api_key="sk-ant-x", openai_api_key="sk-x", **changes)
    s.validate()  # still a valid configuration: the crawl and the rules run
    with pytest.raises(LLMConfigError, match=expected) as info:
        make_client(s)
    assert "sk-ant-x" not in str(info.value) and "sk-x" not in str(info.value)


def test_gemini_variables_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GEMINI_PAID_TIER", "1")
    assert not Settings.from_env(data_dir=tmp_path).llm_enabled


# ------------------------------------------------------------------ CLI

def test_cli_loads_env_file_and_reports_the_key_source(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "my.env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n", encoding="utf-8")
    monkeypatch.setenv("AUCTION_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUCTION_AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # registered, so the loader's write is undone after the test
    assert cli.main(["score"]) == 0
    out = capsys.readouterr().out
    assert "LLM: anthropic claude-haiku-5-5, reasoning effort low, ANTHROPIC_API_KEY from my.env" in out
    assert "sk-ant-from-file" not in out


def test_environment_key_wins_over_env_file(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n", encoding="utf-8")
    monkeypatch.setenv("AUCTION_AGENT_ENV_FILE", str(env_file))
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")
    assert cli.main(["score", "--llm-limit", "0"]) == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY from the environment" in out and "sk-ant-from" not in out


def test_status_line_without_key(tmp_path):
    s = Settings.from_env(data_dir=tmp_path)
    assert cli.llm_status(s) == ("LLM: anthropic claude-haiku-5-5, reasoning effort low, ANTHROPIC_API_KEY "
                                 "not set (see .env.example)")
    s = Settings.from_env(data_dir=tmp_path, llm_provider="openai")
    assert cli.llm_status(s) == "LLM: openai gpt-6-luna, reasoning effort none, OPENAI_API_KEY not set (see .env.example)"


def test_notes_with_markup_characters_do_not_crash_the_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("auction_agent.scoring.service.rescore_all",
                        lambda *a, **k: {"scored": 0, "green": 0, "yellow": 0, "red": 0, "llm_calls": 0,
                                         "llm_cached": 0, "notes": ["OpenAI said [/bold] [red]oops"]})
    assert cli.main(["score", "--no-llm"]) == 0
    assert "[/bold] [red]oops" in capsys.readouterr().out


def test_inline_comments_and_repeated_names(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=sk-old\nBUYER_PROFILE=pro   # pro buyer\nLLM_MODEL=\"gpt-6-luna\" # default\n"
                    "LLM_REASONING_EFFORT='none # really'\nURL=a#b\nOPENAI_API_KEY=sk-new # rotated\n", encoding="utf-8")
    env: dict = {}
    report = load_env_file(path, env)
    assert env["BUYER_PROFILE"] == "pro" and env["LLM_MODEL"] == "gpt-6-luna" and env["URL"] == "a#b"
    assert env["LLM_REASONING_EFFORT"] == "none # really"  # quoted text is kept as written
    assert env["OPENAI_API_KEY"] == "sk-new"  # last line wins, like python-dotenv
    assert any("line 6" in w and "OPENAI_API_KEY" in w for w in report.warnings)
    assert "sk-old" not in " ".join(report.warnings) and "sk-new" not in " ".join(report.warnings)


def test_effort_off_is_a_valid_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "off")
    assert Settings.from_env(data_dir=tmp_path).llm_reasoning_effort == "off"
