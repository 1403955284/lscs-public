from lscs.logwatch.logconfig import (
    REQUIRED,
    check_log_config,
    fix_log_config,
    fix_log_config_text,
)

GOOD = "[Power]\nLogLevel=1\nFilePrinting=true\nConsolePrinting=false\nScreenPrinting=false\nVerbose=true\n"


def test_check_accepts_complete_power_section():
    assert check_log_config(GOOD).ok


def test_check_reports_missing_and_wrong_values():
    result = check_log_config("[Power]\nLogLevel=1\nVerbose=false\n")
    assert not result.ok
    assert result.wrong["Verbose"] == "false"
    assert result.wrong["FilePrinting"] is None


def test_check_is_case_insensitive_for_section_and_values():
    assert check_log_config(GOOD.replace("[Power]", "[power]").replace("true", "True")).ok


def test_fix_preserves_other_sections_and_crlf_input():
    text = "[LoadingScreen]\r\nLogLevel=1\r\n\r\n[Power]\r\nLogLevel=0\r\nFoo=bar\r\n"
    fixed = fix_log_config_text(text)
    assert check_log_config(fixed).ok
    assert "[LoadingScreen]\nLogLevel=1" in fixed
    assert "Foo=bar" in fixed  # [Power] 段里的其他键保留
    assert fixed.count("[Power]") == 1


def test_fix_empty_text_creates_power_section():
    fixed = fix_log_config_text("")
    assert fixed.startswith("[Power]\n")
    for key, value in REQUIRED.items():
        assert f"{key}={value}" in fixed


def test_fix_file_writes_backup_and_reports_change(tmp_path):
    path = tmp_path / "log.config"
    path.write_text("[Power]\nLogLevel=0\n", encoding="utf-8")
    assert fix_log_config(path) is True
    assert check_log_config(path.read_text(encoding="utf-8")).ok
    assert (tmp_path / "log.config.bak").read_text(encoding="utf-8") == "[Power]\nLogLevel=0\n"


def test_fix_file_leaves_correct_file_untouched(tmp_path):
    path = tmp_path / "log.config"
    path.write_text(GOOD, encoding="utf-8")
    assert fix_log_config(path) is False
    assert not (tmp_path / "log.config.bak").exists()


def test_fix_file_creates_missing_file_and_parents(tmp_path):
    path = tmp_path / "Blizzard" / "Hearthstone" / "log.config"
    assert fix_log_config(path) is True
    assert check_log_config(path.read_text(encoding="utf-8")).ok


def test_check_and_fix_generic_section():
    from lscs.logwatch.logconfig import CLIENT_CONFIG_REQUIRED, CLIENT_CONFIG_SECTION

    kw = {"section": CLIENT_CONFIG_SECTION, "required": CLIENT_CONFIG_REQUIRED}
    assert not check_log_config("", **kw).ok
    assert check_log_config("[Log]\nFileSizeLimit.Int=-1\n", **kw).ok
    fixed = fix_log_config_text("", **kw)
    assert fixed == "[Log]\nFileSizeLimit.Int=-1\n"
    assert fix_log_config_text(fixed, **kw) == fixed  # 幂等


def test_generic_fix_preserves_other_sections():
    from lscs.logwatch.logconfig import CLIENT_CONFIG_REQUIRED, CLIENT_CONFIG_SECTION

    kw = {"section": CLIENT_CONFIG_SECTION, "required": CLIENT_CONFIG_REQUIRED}
    fixed = fix_log_config_text("[Other]\nA=1\n\n[Log]\nFileSizeLimit.Int=10\nX=y\n", **kw)
    assert "[Other]\nA=1" in fixed
    assert "X=y" in fixed
    assert "FileSizeLimit.Int=-1" in fixed
    assert "FileSizeLimit.Int=10" not in fixed
    assert check_log_config(fixed, **kw).ok


def test_fix_client_config_backup_and_idempotent(tmp_path):
    from lscs.logwatch.logconfig import fix_client_config

    path = tmp_path / "client.config"
    path.write_text("[Log]\nFileSizeLimit.Int=10\n", encoding="utf-8")
    assert fix_client_config(tmp_path) is True
    assert "FileSizeLimit.Int=-1" in path.read_text(encoding="utf-8")
    assert (tmp_path / "client.config.bak").read_text(encoding="utf-8") == (
        "[Log]\nFileSizeLimit.Int=10\n"
    )
    assert fix_client_config(tmp_path) is False


def test_fix_client_config_creates_missing_file(tmp_path):
    from lscs.logwatch.logconfig import fix_client_config

    assert fix_client_config(tmp_path) is True
    assert (tmp_path / "client.config").read_text(encoding="utf-8") == (
        "[Log]\nFileSizeLimit.Int=-1\n"
    )
