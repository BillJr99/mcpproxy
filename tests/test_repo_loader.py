"""Unit tests for repo_loader.py."""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from repo_loader import (
    clone_or_pull,
    get_repo_path,
    install_packages,
    install_requirements,
    setup_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(url="https://github.com/user/repo", **overrides) -> dict:
    repo = {"url": url}
    repo.update(overrides)
    return {"repo": repo, "code": "", "tools": []}


# ---------------------------------------------------------------------------
# clone_or_pull
# ---------------------------------------------------------------------------

class TestCloneOrPull:
    def test_clones_when_no_git_dir(self, tmp_path):
        dest = tmp_path / "myrepo"
        with patch("repo_loader._run") as mock_run:
            clone_or_pull("https://github.com/x/y", dest, "main")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "clone" in cmd
        assert "https://github.com/x/y" in cmd
        assert str(dest) in cmd

    def test_pulls_when_git_dir_exists(self, tmp_path):
        dest = tmp_path / "myrepo"
        dest.mkdir()
        (dest / ".git").mkdir()
        with patch("repo_loader._run") as mock_run:
            clone_or_pull("https://github.com/x/y", dest, "main")
        cmd = mock_run.call_args[0][0]
        assert "pull" in cmd
        assert str(dest) in cmd

    def test_clone_uses_correct_branch(self, tmp_path):
        dest = tmp_path / "repo"
        with patch("repo_loader._run") as mock_run:
            clone_or_pull("https://github.com/x/y", dest, "develop")
        cmd = mock_run.call_args[0][0]
        assert "develop" in cmd

    def test_clone_creates_parent_dir(self, tmp_path):
        dest = tmp_path / "nested" / "path" / "repo"
        with patch("repo_loader._run"):
            clone_or_pull("https://github.com/x/y", dest, "main")
        assert dest.parent.exists()

    def test_subprocess_error_propagates(self, tmp_path):
        dest = tmp_path / "repo"
        with patch("repo_loader._run", side_effect=subprocess.CalledProcessError(128, "git")):
            with pytest.raises(subprocess.CalledProcessError):
                clone_or_pull("https://github.com/x/y", dest, "main")


# ---------------------------------------------------------------------------
# install_requirements
# ---------------------------------------------------------------------------

class TestInstallRequirements:
    def test_installs_when_file_exists(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with patch("repo_loader._run") as mock_run:
            install_requirements(tmp_path, "requirements.txt")
        cmd = mock_run.call_args[0][0]
        assert "-r" in cmd
        assert str(req) in cmd

    def test_skips_when_file_missing(self, tmp_path):
        with patch("repo_loader._run") as mock_run:
            install_requirements(tmp_path, "requirements.txt")
        mock_run.assert_not_called()

    def test_custom_requirements_filename(self, tmp_path):
        req = tmp_path / "reqs.txt"
        req.write_text("flask\n")
        with patch("repo_loader._run") as mock_run:
            install_requirements(tmp_path, "reqs.txt")
        cmd = mock_run.call_args[0][0]
        assert "reqs.txt" in " ".join(cmd)


# ---------------------------------------------------------------------------
# install_packages
# ---------------------------------------------------------------------------

class TestInstallPackages:
    def test_installs_packages(self):
        with patch("repo_loader._run") as mock_run:
            install_packages(["requests", "httpx"])
        cmd = mock_run.call_args[0][0]
        assert "requests" in cmd
        assert "httpx" in cmd

    def test_skips_empty_list(self):
        with patch("repo_loader._run") as mock_run:
            install_packages([])
        mock_run.assert_not_called()

    def test_skips_none(self):
        with patch("repo_loader._run") as mock_run:
            install_packages(None)  # type: ignore
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# setup_repo
# ---------------------------------------------------------------------------

class TestSetupRepo:
    def _patch_all(self):
        """Return a context manager that patches clone, req, pkg."""
        from unittest.mock import patch as _patch
        return (
            _patch("repo_loader.clone_or_pull"),
            _patch("repo_loader.install_requirements"),
            _patch("repo_loader.install_packages"),
        )

    def test_missing_url_raises(self, tmp_path):
        spec = {"repo": {}, "code": "", "tools": []}
        with pytest.raises(ValueError, match="url"):
            setup_repo(spec, "myprovider")

    def test_adds_to_sys_path(self, tmp_path, repos_dir):
        spec = _make_spec()
        with (
            patch("repo_loader.clone_or_pull"),
            patch("repo_loader.install_requirements"),
            patch("repo_loader.install_packages"),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            path = setup_repo(spec, "myprovider")

        assert str(path) in sys.path

    def test_returns_correct_path(self, tmp_path, repos_dir):
        spec = _make_spec(url="https://github.com/x/y")
        with (
            patch("repo_loader.clone_or_pull"),
            patch("repo_loader.install_requirements"),
            patch("repo_loader.install_packages"),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            path = setup_repo(spec, "myprovider")
        assert path == (repos_dir / "myprovider").resolve()

    def test_custom_path_used(self, tmp_path, repos_dir):
        custom = tmp_path / "custom_dest"
        spec = _make_spec(path=str(custom))
        with (
            patch("repo_loader.clone_or_pull"),
            patch("repo_loader.install_requirements"),
            patch("repo_loader.install_packages"),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            path = setup_repo(spec, "myprovider")
        assert path == custom.resolve()

    def test_subfolder_appended_to_sys_path(self, tmp_path, repos_dir):
        spec = _make_spec(subfolder="src")
        with (
            patch("repo_loader.clone_or_pull"),
            patch("repo_loader.install_requirements"),
            patch("repo_loader.install_packages"),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            path = setup_repo(spec, "myprovider")
        assert "src" in str(path)

    def test_clone_called_with_correct_args(self, repos_dir):
        spec = _make_spec(url="https://github.com/a/b", branch="develop")
        with (
            patch("repo_loader.clone_or_pull") as mock_clone,
            patch("repo_loader.install_requirements"),
            patch("repo_loader.install_packages"),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            setup_repo(spec, "myprovider")
        mock_clone.assert_called_once()
        args = mock_clone.call_args[0]
        assert args[0] == "https://github.com/a/b"
        assert args[2] == "develop"

    def test_git_failure_raises_runtime_error(self, repos_dir):
        spec = _make_spec()
        err = subprocess.CalledProcessError(128, "git", stderr=b"not found")
        with (
            patch("repo_loader.clone_or_pull", side_effect=err),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            with pytest.raises(RuntimeError, match="repo setup failed"):
                setup_repo(spec, "myprovider")

    def test_does_not_duplicate_sys_path_entry(self, repos_dir):
        spec = _make_spec()
        with (
            patch("repo_loader.clone_or_pull"),
            patch("repo_loader.install_requirements"),
            patch("repo_loader.install_packages"),
            patch("repo_loader.REPOS_DIR", repos_dir),
        ):
            setup_repo(spec, "no_dupe")
            before = sys.path.count(str((repos_dir / "no_dupe").resolve()))
            setup_repo(spec, "no_dupe")
            after = sys.path.count(str((repos_dir / "no_dupe").resolve()))
        assert after == before  # no duplicate added


# ---------------------------------------------------------------------------
# get_repo_path
# ---------------------------------------------------------------------------

class TestGetRepoPath:
    def test_default_path_under_repos_dir(self, repos_dir):
        spec = _make_spec()
        with patch("repo_loader.REPOS_DIR", repos_dir):
            path = get_repo_path(spec, "myprovider")
        assert path == repos_dir / "myprovider"

    def test_custom_path_respected(self, tmp_path, repos_dir):
        custom = tmp_path / "custom"
        spec = _make_spec(path=str(custom))
        with patch("repo_loader.REPOS_DIR", repos_dir):
            path = get_repo_path(spec, "myprovider")
        assert path == custom
