"""Regression: every provider example in the repo still loads.

Two sources:

* ``tests/fixtures/providers/*.yaml`` — complete provider files, legacy and
  managed-auth, loaded with the server's own loader;
* every fenced ``yaml`` block in README.md that is a complete, single-kind
  provider file (a ``tools:`` list plus exactly one of ``code:``, ``rest:`` or
  ``package:``).  Fragments, compose snippets and the all-keys reference block
  are not providers and are skipped.

Each one must build its tool handlers, pass the UI validator (when its
external files are not needed), and survive the UI's structured round trip.
"""
import re
from pathlib import Path

import pytest
import yaml

import server
from frontend.app import _provider_to_structured, _structured_to_yaml, _validate_provider

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "providers"
KINDS = ("code", "rest", "package")


def _readme_providers() -> list[tuple[str, str]]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    out = []
    for i, block in enumerate(re.findall(r"```ya?ml\n(.*?)```", text, re.S)):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
            continue
        if sum(1 for k in KINDS if k in data) != 1 or "repository" in data:
            continue
        out.append((f"README block {i}", block))
    return out


FIXTURE_FILES = sorted(FIXTURES.glob("*.yaml"))
README_BLOCKS = _readme_providers()


def test_fixtures_and_readme_examples_exist():
    assert len(FIXTURE_FILES) >= 5
    # The README documents managed sign-in with complete examples.
    assert any("inject_as" in block for _name, block in README_BLOCKS)


def _check(name: str, spec_text: str, tmp_path: Path) -> None:
    path = tmp_path / f"{name}.yaml"
    path.write_text(spec_text, encoding="utf-8")
    (spec,) = server.load_provider_specs(tmp_path)
    handlers = server.build_tool_handlers(spec)
    enabled = [t for t in spec["tools"] if t.get("enabled", True) is not False]
    assert len(handlers) == len(enabled)

    structured = _provider_to_structured(name, spec)
    if spec["tools"]:
        # Package examples with ``tools: []`` rely on runtime introspection;
        # the UI validator (which wants declared tools) does not apply.
        result = _validate_provider(structured)
        assert result["ok"], result["errors"]
    again = yaml.safe_load(_structured_to_yaml(structured))
    assert [t["name"] for t in again["tools"]] == [t["name"] for t in spec["tools"]]
    if isinstance(spec.get("auth"), dict) and "code" in spec:
        assert again["auth"] == spec["auth"]


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_provider_loads(path: Path, tmp_path: Path):
    _check(path.stem, path.read_text(encoding="utf-8"), tmp_path)


@pytest.mark.parametrize("name,block", README_BLOCKS, ids=[n for n, _ in README_BLOCKS])
def test_readme_provider_loads(name: str, block: str, tmp_path: Path):
    _check("example", block, tmp_path)
