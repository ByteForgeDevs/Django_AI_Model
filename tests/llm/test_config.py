"""Configuration, and the secret it refuses to hold.

The default matters more than anything else here: a fresh install, a checked
out repository and a CI run must all be offline until somebody asks otherwise.
"""

from __future__ import annotations

import textwrap

import pytest

from djaudit.llm.config import (
    ConfigError,
    Credential,
    LLMConfig,
    from_pyproject,
    resolve,
)


def pyproject(tmp_path, body: str):
    path = tmp_path / "pyproject.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestTheDefaultIsOffline:
    """Not "offline unless credentials exist" -- offline until asked."""

    def test_a_bare_config_is_offline(self):
        assert LLMConfig().offline is True

    def test_and_says_so_when_asked_why(self):
        usable, why = LLMConfig().usable
        assert usable is False
        assert "offline" in why

    def test_a_missing_pyproject_is_offline(self, tmp_path):
        assert from_pyproject(tmp_path / "nope.toml").offline is True

    def test_a_pyproject_with_no_table_is_offline(self, tmp_path):
        path = pyproject(tmp_path, '[project]\nname = "x"\n')
        assert from_pyproject(path).offline is True

    def test_a_configured_provider_alone_does_not_enable_one(self, tmp_path):
        """Describing a provider is not asking for it.

        Otherwise checking out a repository that names a model would start
        making calls on the next run, which nobody asked for.
        """
        path = pyproject(
            tmp_path,
            """
            [tool.djaudit.llm]
            provider = "openai"
            model = "gpt-4o-mini"
            api_key_env = "OPENAI_API_KEY"
            """,
        )
        config = from_pyproject(path)
        assert config.provider == "openai"
        assert config.offline is True

    def test_enabled_true_turns_it_on(self, tmp_path):
        path = pyproject(
            tmp_path,
            """
            [tool.djaudit.llm]
            enabled = true
            provider = "openai"
            api_key_env = "OPENAI_API_KEY"
            """,
        )
        assert from_pyproject(path).offline is False


class TestItRefusesToHoldASecret:
    """djaudit reports `DJS-002` for a secret in source. Not doing the same
    thing in its own config file is the least it can do.
    """

    def test_an_inline_api_key_is_refused(self, tmp_path):
        path = pyproject(
            tmp_path,
            """
            [tool.djaudit.llm]
            api_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
            """,
        )
        with pytest.raises(ConfigError, match="DJS-002"):
            from_pyproject(path)

    @pytest.mark.parametrize("name", ["key", "token", "secret", "password"])
    def test_the_other_names_people_use(self, tmp_path, name):
        path = pyproject(tmp_path, f'[tool.djaudit.llm]\n{name} = "whatever"\n')
        with pytest.raises(ConfigError, match="must not contain"):
            from_pyproject(path)

    def test_a_key_shaped_value_under_an_innocent_name(self, tmp_path):
        """Renaming the field must not be the way around the check."""
        path = pyproject(
            tmp_path,
            """
            [tool.djaudit.llm]
            provider = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
            """,
        )
        with pytest.raises(ConfigError, match="looks like a credential"):
            from_pyproject(path)

    def test_an_ordinary_value_is_not_mistaken_for_one(self, tmp_path):
        """The contrast. A checker that refuses everything teaches nothing."""
        path = pyproject(
            tmp_path,
            """
            [tool.djaudit.llm]
            provider = "openai"
            model = "gpt-4o-mini"
            api_key_env = "OPENAI_API_KEY"
            """,
        )
        assert from_pyproject(path).model == "gpt-4o-mini"

    def test_a_credential_pasted_where_a_variable_name_goes(self):
        with pytest.raises(ConfigError, match="looks like a key"):
            Credential("sk-abcdefghijklmnopqrstuvwxyz0123456789")

    def test_the_error_does_not_echo_the_whole_key(self):
        """An error message is a place secrets leak into logs."""
        with pytest.raises(ConfigError) as caught:
            Credential("sk-abcdefghijklmnopqrstuvwxyz0123456789")
        assert "vwxyz0123456789" not in str(caught.value)


class TestTheCredentialIsNeverStored:
    def test_it_holds_only_the_variable_name(self, monkeypatch):
        monkeypatch.setenv("DJAUDIT_TEST_KEY", "sk-secret-value-here")
        credential = Credential("DJAUDIT_TEST_KEY")
        assert "sk-secret-value-here" not in repr(credential)

    def test_it_reads_the_environment_at_use(self, monkeypatch):
        credential = Credential("DJAUDIT_TEST_KEY")
        assert credential.resolve() is None
        monkeypatch.setenv("DJAUDIT_TEST_KEY", "value")
        assert credential.resolve() == "value"

    def test_an_empty_variable_counts_as_absent(self, monkeypatch):
        """`export KEY=` is how people turn one off, and it must mean off."""
        monkeypatch.setenv("DJAUDIT_TEST_KEY", "")
        assert Credential("DJAUDIT_TEST_KEY").present is False

    def test_a_config_repr_cannot_leak_a_key(self, monkeypatch):
        monkeypatch.setenv("DJAUDIT_TEST_KEY", "sk-secret-value-here")
        config = LLMConfig(
            offline=False, provider="openai", credential=Credential("DJAUDIT_TEST_KEY")
        )
        assert "sk-secret-value-here" not in repr(config)


class TestWhichAbsenceThisIs:
    """Three different problems, three different messages. A user told only
    that nothing happened will debug the wrong one.
    """

    def test_no_provider(self):
        _, why = LLMConfig(offline=False).usable
        assert "no provider" in why

    def test_no_credential_configured(self):
        _, why = LLMConfig(offline=False, provider="openai").usable
        assert "needs a credential" in why

    def test_credential_configured_but_unset(self, monkeypatch):
        monkeypatch.delenv("DJAUDIT_TEST_KEY", raising=False)
        config = LLMConfig(
            offline=False, provider="openai", credential=Credential("DJAUDIT_TEST_KEY")
        )
        _, why = config.usable
        assert "DJAUDIT_TEST_KEY is not set" in why

    def test_everything_present(self, monkeypatch):
        monkeypatch.setenv("DJAUDIT_TEST_KEY", "value")
        config = LLMConfig(
            offline=False, provider="openai", credential=Credential("DJAUDIT_TEST_KEY")
        )
        usable, why = config.usable
        assert usable is True
        assert why == ""

    def test_degrading_records_why(self):
        degraded = LLMConfig(offline=False, provider="openai").going_offline("budget spent")
        assert degraded.offline is True
        assert degraded.extras["degraded"] == "budget spent"


class TestFlagsBeatTheFile:
    def test_no_llm_overrides_an_enabling_file(self, tmp_path):
        """The person typing it is at the machine and the file is not."""
        path = pyproject(
            tmp_path,
            """
            [tool.djaudit.llm]
            enabled = true
            provider = "openai"
            api_key_env = "OPENAI_API_KEY"
            """,
        )
        assert resolve(path, enable=False).offline is True

    def test_llm_overrides_a_silent_file(self, tmp_path):
        path = pyproject(tmp_path, '[tool.djaudit.llm]\nprovider = "openai"\n')
        assert resolve(path, enable=True).offline is False

    def test_a_flag_can_replace_the_model(self, tmp_path):
        path = pyproject(tmp_path, '[tool.djaudit.llm]\nmodel = "a"\n')
        assert resolve(path, model="b").model == "b"

    def test_no_path_means_the_offline_default(self):
        assert resolve(None).offline is True


class TestMalformedConfig:
    def test_broken_toml_is_an_error_not_a_shrug(self, tmp_path):
        """A typo in a config file is a mistake, and silence would hide it."""
        path = pyproject(tmp_path, "[tool.djaudit.llm\nprovider =\n")
        with pytest.raises(ConfigError):
            from_pyproject(path)
