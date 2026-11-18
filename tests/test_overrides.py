"""The user's changes to the shipped files, in one file of their own (chaperone/overrides.py)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chaperone import load_project, yaml_lines
from chaperone.__main__ import main
from chaperone.connect.llm import load_llm
from chaperone.overrides import read_layer

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A copy of the examples and the model files, and the command run from it, as from the folder project-chaperone."""
    shutil.copytree(REPO / "examples", tmp_path / "examples", ignore=shutil.ignore_patterns("__pycache__", "corpus"))
    shutil.copytree(REPO / "configs", tmp_path / "configs")
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    yaml_lines.LAYER = None


def layer(repo: Path, text: str):
    (repo / "private").mkdir(exist_ok=True)
    (repo / "private" / "local.yaml").write_text(text)
    yaml_lines.LAYER = read_layer("private/local.yaml")
    return yaml_lines.LAYER


def test_only_the_keys_written_change_and_each_change_is_said(repo: Path) -> None:
    changes = layer(repo, "configs/llm/local.yaml:\n  api_base: http://localhost:8080/v1\n  max_tokens: 8192\n"
                          "examples/deliveries/setups/real.yaml:\n  tools: {MAILBOX: {client: {days: 7}}}\n")
    settings = load_llm("configs/llm/local.yaml")
    assert settings.api_base == "http://localhost:8080/v1" and settings.max_tokens == 8192
    assert settings.timeout == 300 and settings.model == "Qwen3.6-35B-A3B-MTP-UD-Q6_K_XL"        # the rest as shipped
    client = load_project("examples/deliveries", "real").tools["MAILBOX"]["client"]
    assert client["days"] == 7 and client["max_messages"] == 300 and client["timezone"] == "America/New_York"
    assert changes.report() == [
        "Configuration: 3 settings changed by private/local.yaml:",
        "    configs/llm/local.yaml: api_base (private/local.yaml:2)",
        "    configs/llm/local.yaml: max_tokens (private/local.yaml:3)",
        "    examples/deliveries/setups/real.yaml: tools.MAILBOX.client.days (private/local.yaml:5)"]
    assert changes.report() == []                                                    # each change is said once


def test_a_key_the_shipped_file_lacks_changes_nothing_and_is_said(repo: Path) -> None:
    changes = layer(repo, "configs/llm/local.yaml:\n  max_token: 8192\n")
    assert load_llm("configs/llm/local.yaml").max_tokens == 1024
    assert changes.report() == ["WARNING private/local.yaml:2: configs/llm/local.yaml has no key `max_token`; nothing changed."]


def test_a_list_is_replaced_whole_and_that_is_said(repo: Path) -> None:
    changes = layer(repo, "examples/deliveries/setups/real.yaml:\n  tools: {MAILBOX: {facts: {adds: [UNTRUSTED]}}}\n")
    assert load_project("examples/deliveries", "real").tools["MAILBOX"]["facts"]["adds"] == ["UNTRUSTED"]   # was [UNTRUSTED, PERSONAL]
    assert changes.report() == ["Configuration: 1 setting changed by private/local.yaml:",
                                "    examples/deliveries/setups/real.yaml: tools.MAILBOX.facts.adds (private/local.yaml:2, a list, replaced whole)"]


def test_a_changed_fact_changes_the_result_of_the_check(repo: Path, capsys) -> None:
    assert main(["check", "examples/deliveries", "--setup", "demo_llm", "--state", str(repo / "state")]) == 0
    capsys.readouterr()
    (repo / "private").mkdir()
    (repo / "private" / "mine.yaml").write_text(
        "examples/deliveries/setups/demo_llm.yaml:\n  tools: {LOCAL_LLM: {facts: {LEAVES_NETWORK: true}}}\n")
    assert main(["check", "examples/deliveries", "--setup", "demo_llm", "--config", "private/mine.yaml",
                 "--state", str(repo / "state")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("Configuration: 1 setting changed by private/mine.yaml:\n"
                          "    examples/deliveries/setups/demo_llm.yaml: tools.LOCAL_LLM.facts.LEAVES_NETWORK (private/mine.yaml:2)")
    assert "NOTHING_LEAVES_NETWORK" in out                                        # the LLM's calls now leave the network
    assert main(["check", "examples/deliveries", "--setup", "demo_llm", "--config", "private/mine.yaml", "--defaults-only",
                 "--state", str(repo / "state")]) == 0                           # the shipped files only
    assert "Configuration:" not in capsys.readouterr().out


def test_rules_and_exceptions_are_not_changed_and_a_missing_file_is_said(repo: Path) -> None:
    changes = layer(repo, "examples/deliveries/exceptions.yaml:\n  exceptions: []\n"
                          "examples/rules/default.yaml:\n  rules: []\n"
                          "configs/llm/locl.yaml:\n  api_base: x\n")
    project = load_project("examples/deliveries", "demo")
    assert len(project.exceptions) == 12 and len(project.rules) == 5
    report = changes.report()
    assert "WARNING private/local.yaml:5: no file configs/llm/locl.yaml; nothing changed." in report
    assert ("WARNING private/local.yaml:1: examples/deliveries/exceptions.yaml is a rules, exceptions or state file, "
            "which private/local.yaml does not change; nothing changed.") in report
    assert any("examples/rules/default.yaml is a rules, exceptions or state file" in line for line in report)


def test_a_file_the_run_does_not_read_is_never_opened_and_is_said(repo: Path) -> None:
    changes = layer(repo, "examples/safe_rag/setups/local.yaml:\n  note: changed\n")
    load_project("examples/deliveries", "demo")
    assert changes.report() == []
    assert changes.unused() == ["NOTE private/local.yaml: examples/safe_rag/setups/local.yaml was not read by this run; "
                                "nothing in it changed."]
