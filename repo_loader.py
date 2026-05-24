"""
Manages external Git repositories referenced by provider YAML files.

When a provider spec contains a ``repo:`` block, the server calls
``setup_repo()`` before executing the provider's ``code`` block.  The
repo is cloned once (or pulled if already present) into REPOS_DIR,
any declared dependencies are installed, and the relevant path is
prepended to ``sys.path`` so the code block can import from it.

YAML ``repo:`` block schema
----------------------------
repo:
  url: https://github.com/user/repo    # required
  branch: main                         # optional (default: main)
  subfolder: src                       # optional sub-path added to sys.path
  path: repos/my-repo                  # optional override for clone destination
  requirements: requirements.txt       # optional pip requirements file to install
  install_packages:                    # optional explicit pip packages
    - some-package
    - another-package
"""

import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from config import REPOS_DIR


# ---------------------------------------------------------------------------
# Low-level git / pip helpers (thin wrappers so they're easy to mock in tests)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a subprocess command; raises CalledProcessError on failure."""
    return subprocess.run(cmd, check=True, capture_output=True, **kwargs)


def clone_or_pull(url: str, dest: Path, branch: str = "main") -> None:
    """Clone *url* to *dest*, or pull if *dest* is already a git checkout."""
    if (dest / ".git").exists():
        print(f"repo_loader: pulling {dest}")
        _run(["git", "-C", str(dest), "pull", "--ff-only"])
    else:
        print(f"repo_loader: cloning {url} (branch={branch}) → {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--depth=1", "--branch", branch, url, str(dest)])


def install_requirements(dest: Path, requirements_file: str = "requirements.txt") -> None:
    """pip-install from *requirements_file* inside *dest* if the file exists."""
    req_path = dest / requirements_file
    if req_path.exists():
        print(f"repo_loader: installing {req_path}")
        _run([sys.executable, "-m", "pip", "install", "-r", str(req_path), "-q"])


def install_packages(packages: list) -> None:
    """pip-install an explicit list of package names (skips if empty)."""
    if packages:
        print(f"repo_loader: installing packages {packages}")
        _run([sys.executable, "-m", "pip", "install"] + list(packages) + ["-q"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_repo(spec: dict[str, Any], provider_name: str) -> Path:
    """
    Set up the external repo described by *spec['repo']* for *provider_name*.

    Steps:
      1. Clone or pull the repo.
      2. Install any declared requirements / packages.
      3. Prepend the appropriate directory to ``sys.path``.

    Returns the path that was added to ``sys.path``.
    Raises ``ValueError`` if ``repo.url`` is missing.
    Raises ``RuntimeError`` wrapping a ``CalledProcessError`` on git/pip failure.
    """
    repo_spec = spec.get("repo", {})
    url = repo_spec.get("url", "").strip()
    if not url:
        raise ValueError(f"Provider '{provider_name}': repo.url is required")

    branch = repo_spec.get("branch", "main") or "main"
    subfolder = repo_spec.get("subfolder", "") or ""
    requirements_file = repo_spec.get("requirements", "requirements.txt") or "requirements.txt"
    install_pkgs = list(repo_spec.get("install_packages") or [])

    # Determine clone destination
    custom_path = repo_spec.get("path", "")
    dest = Path(custom_path) if custom_path else (REPOS_DIR / provider_name)

    try:
        clone_or_pull(url, dest, branch)
        install_requirements(dest, requirements_file)
        install_packages(install_pkgs)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Provider '{provider_name}': repo setup failed for {url}: {exc.stderr.decode(errors='replace') if exc.stderr else exc}"
        ) from exc
    except Exception as exc:
        print(f"repo_loader setup_repo error: {exc}")
        traceback.print_exc()
        raise

    # Add the right directory to sys.path
    code_path = (dest / subfolder).resolve() if subfolder else dest.resolve()
    path_str = str(code_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
        print(f"repo_loader: added to sys.path: {path_str}")

    return code_path


def get_repo_path(spec: dict[str, Any], provider_name: str) -> Path:
    """Return the expected clone destination without actually cloning."""
    repo_spec = spec.get("repo", {})
    custom_path = repo_spec.get("path", "")
    return Path(custom_path) if custom_path else (REPOS_DIR / provider_name)
