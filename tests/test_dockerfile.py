"""Guard: every local module imported by runtime code must be COPYd into the image.

The Dockerfile copies Python modules by explicit name (not `COPY . .`), so adding
a new top-level module that runtime code imports — without adding it to the COPY
line — produces an image that imports a missing module at request time.  This
happened once with `rest_provider.py`; this test keeps it from recurring.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _local_module_names() -> set[str]:
    """Top-level .py files in the repo root are importable local modules."""
    return {p.stem for p in ROOT.glob("*.py")}


def _copied_modules() -> set[str]:
    """Module stems COPYd into the image by the Dockerfile (root-level .py files)."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied: set[str] = set()
    for line in text.splitlines():
        if line.startswith("COPY"):
            for tok in re.findall(r"(\w[\w./-]*\.py)\b", line):
                copied.add(Path(tok).stem)
    return copied


def _imported_local_modules(py_files, local: set[str]) -> set[str]:
    used: set[str] = set()
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in local:
                    used.add(root)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in local:
                        used.add(root)
    return used


def test_runtime_imports_are_copied_into_image():
    local = _local_module_names()
    # Runtime entrypoints + the packages baked into the image.
    runtime_files = [ROOT / "server.py", ROOT / "frontend" / "app.py"]
    runtime_files += sorted((ROOT / "frontend").glob("*.py"))
    runtime_files += sorted((ROOT / "handlers").glob("*.py"))
    # Follow one hop: modules those entrypoints import are themselves copied and
    # may import further local modules.
    copied = _copied_modules()
    runtime_files += [ROOT / f"{m}.py" for m in copied if (ROOT / f"{m}.py").exists()]

    used = _imported_local_modules(set(runtime_files), local)
    missing = used - copied
    assert not missing, (
        f"Local modules imported by runtime code but not COPYd in the Dockerfile: "
        f"{sorted(missing)}. Add them to the COPY line."
    )


def test_rest_provider_is_in_the_image():
    # Explicit guard for the specific regression this test was written for.
    assert "rest_provider" in _copied_modules()
