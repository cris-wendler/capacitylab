# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choosing the model from the web UI: what it stores, what it refuses, and what it never touches."""

import json

import pytest
from fastapi.testclient import TestClient

from capacitylab import settings_store
from capacitylab.settings import Settings
from capacitylab.web.app import create_app

FAKE_KEY = "sk-" + "test-not-a-real-key-000"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(runs_dir=tmp_path), inline_jobs=True)) as c:
        yield c


def stored(tmp_path):
    return json.loads(settings_store.path_for(tmp_path).read_text())


def test_the_page_shows_what_is_in_use_and_whether_the_key_is_there(client, monkeypatch):
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Model settings" in page.text and "claude-opus-5" in page.text
    assert "Ollama, on this machine" in page.text and "capacitylab-llama3.2" in page.text
    assert "DeepSeek" in page.text and "Groq" in page.text  # the free and cheap routes are offered, not buried
    # Key presence is reported; the value never is.
    assert "missing" in page.text or "set</span>" in page.text


def test_saving_a_choice_changes_what_the_next_review_uses(client, tmp_path):
    response = client.post("/settings", data={
        "llm_provider": "openai", "model": "llama3.1:8b", "llm_base_url": "http://127.0.0.1:11434/v1",
        "llm_api_key_env": "OLLAMA_API_KEY", "effort": "low", "reasoning_effort": "",
        "input_usd_per_mtok": "0", "output_usd_per_mtok": "0", "cached_input_multiplier": "1",
        "max_usd_per_run": "0", "max_usd_total": "0", "max_output_tokens": "5000", "max_rounds": "2"})
    assert response.status_code == 200 and "Saved to" in response.text

    saved = stored(tmp_path)
    assert saved["model"] == "llama3.1:8b" and saved["llm_provider"] == "openai"
    assert saved["llm_base_url"] == "http://127.0.0.1:11434/v1" and saved["max_rounds"] == 2

    # The page now reports the new configuration, and a fresh app picks it up from the file.
    assert "llama3.1:8b" in client.get("/settings").text
    settings = settings_store.apply(Settings(runs_dir=tmp_path), settings_store.load(tmp_path))
    assert settings.model == "llama3.1:8b" and settings.llm_base_url.endswith(":11434/v1")
    assert settings.max_rounds == 2 and settings.effort == "low"


def test_a_preset_fills_everything_in_one_click(client, tmp_path):
    assert "Saved to" in client.post("/settings", data={"preset": "ollama"}).text
    saved = stored(tmp_path)
    assert saved["model"] == "capacitylab-llama3.2" and saved["input_usd_per_mtok"] == 0.0
    assert client.post("/settings", data={"preset": "anthropic-sonnet"}).status_code == 200
    assert stored(tmp_path)["model"] == "claude-sonnet-5"
    # Back to the environment: the file is emptied rather than left with stale values.
    assert "Saved to" in client.post("/settings", data={"reset": "1"}).text
    assert stored(tmp_path) == {}
    assert "claude-opus-5" in client.get("/settings").text


def test_nonsense_is_refused_with_a_reason_and_nothing_is_saved(client, tmp_path):
    response = client.post("/settings", data={
        "llm_provider": "telepathy", "model": "", "effort": "extreme", "llm_base_url": "ftp://nope",
        "llm_api_key_env": "bad name!", "input_usd_per_mtok": "-3", "max_rounds": "99",
        "max_output_tokens": "10", "output_usd_per_mtok": "not a number"})
    assert response.status_code == 200 and "Saved to" not in response.text
    for message in ("Provider must be", "Model cannot be empty", "Effort must be", "must start with http",
                    "letters, digits and underscores", "cannot be negative", "must be a number",
                    "Rounds must be between 1 and 6", "below 1000"):
        assert message in response.text, message
    assert not settings_store.path_for(tmp_path).exists()


def test_an_api_key_can_never_be_stored_through_this_page(client, tmp_path, monkeypatch):
    """The page chooses which variable holds the key. The key itself stays in the environment."""
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    client.post("/settings", data={"llm_provider": "openai", "model": "gpt-5", "effort": "auto",
                                   "llm_api_key_env": "OPENAI_API_KEY", "api_key": FAKE_KEY,
                                   "OPENAI_API_KEY": FAKE_KEY, "max_rounds": "3", "max_output_tokens": "5000"})
    text = settings_store.path_for(tmp_path).read_text()
    assert FAKE_KEY not in text  # the value never lands in the file
    saved = json.loads(text)
    assert saved["llm_api_key_env"] == "OPENAI_API_KEY"  # only the variable's name is kept
    assert not any(k.lower().endswith("key") for k in saved)
    # The page reports presence only.
    page = client.get("/settings").text
    assert FAKE_KEY not in page and "OPENAI_API_KEY" in page


def test_only_the_model_fields_can_be_changed_from_the_web(client, tmp_path):
    """Where the tool is allowed to reach stays with the environment, not with a form post."""
    client.post("/settings", data={"llm_provider": "openai", "model": "gpt-5", "effort": "auto",
                                   "runs_dir": "/etc", "aws_live": "true", "aws_endpoint": "https://rds.example.invalid",
                                   "web_password": "x", "session_secret": "y", "max_rounds": "3",
                                   "max_output_tokens": "5000"})
    saved = stored(tmp_path)
    assert set(saved) <= set(settings_store.TEXT_FIELDS + settings_store.NUMBER_FIELDS)
    for forbidden in ("runs_dir", "aws_live", "aws_endpoint", "web_password", "session_secret"):
        assert forbidden not in saved


def test_a_local_endpoint_needs_no_key_and_a_remote_one_does(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    local = Settings(llm_provider="openai", llm_base_url="http://127.0.0.1:11434/v1")
    remote = Settings(llm_provider="openai", llm_base_url=None)
    assert settings_store.key_state(local) == {"name": "OPENAI_API_KEY", "present": False, "needed": False}
    assert settings_store.key_state(remote)["needed"] is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert settings_store.key_state(Settings(llm_provider="anthropic")) == {
        "name": "ANTHROPIC_API_KEY", "present": True, "needed": True}


def test_listing_local_models_never_reaches_a_remote_host():
    assert settings_store.local_models("https://api.openai.com/v1") == []
    assert settings_store.local_models(None) == []
    assert settings_store.local_models("http://127.0.0.1:59999/v1", timeout_s=0.2) == []  # nothing there, no error


def test_a_damaged_settings_file_is_ignored_rather_than_crashing(tmp_path):
    settings_store.path_for(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    settings_store.path_for(tmp_path).write_text("{not json")
    assert settings_store.load(tmp_path) == {}
    with TestClient(create_app(Settings(runs_dir=tmp_path), inline_jobs=True)) as client:
        assert client.get("/settings").status_code == 200
