"""Checks on what gets published, not on what the code does.

The README is the PyPI project page as well as the GitHub landing page, and
PyPI does not rewrite relative paths the way GitHub does. A relative link here
is a 404 for everyone arriving from `pip install`.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # tomllib landed in 3.11; the package itself still supports 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

MD_IMAGE = re.compile(r"!\[[^\]]*\]\((?!https?://)([^)]+)\)")
MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\((?!https?://|#)([^)]+)\)")


def test_readme_has_no_relative_paths():
    text = README.read_text()
    assert not MD_IMAGE.findall(text), "relative image: broken on the PyPI page"
    assert not MD_LINK.findall(text), "relative link: broken on the PyPI page"


def test_readme_urls_point_at_files_that_exist():
    """An absolute GitHub URL still has to name a real file in this repo."""
    text = README.read_text()
    prefixes = (
        "https://github.com/SEPURI-SAI-KRISHNA/blame/blob/main/",
        "https://raw.githubusercontent.com/SEPURI-SAI-KRISHNA/blame/main/",
    )
    checked = 0
    for url in re.findall(r"\]\((https?://[^)]+)\)", text):
        for prefix in prefixes:
            if url.startswith(prefix):
                path = ROOT / url[len(prefix) :]
                assert path.exists(), f"{url} names a file that does not exist"
                checked += 1
    assert checked >= 5, "expected the docs links and the demo GIF to be checked"


def test_version_is_consistent():
    import blame

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert blame.__version__ == declared


def test_changelog_mentions_the_current_version():
    import blame

    assert blame.__version__ in (ROOT / "CHANGELOG.md").read_text()


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_no_data_directories_are_tracked():
    """`.blame/` holds the user's own data. It must never be committed."""
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    bad = [f for f in tracked if f.startswith((".blame/", "dist/")) or f.endswith(".pyc")]
    assert not bad, f"these should not be tracked: {bad}"


def test_py_typed_marker_ships():
    """PEP 561: without this file every downstream type checker silently
    ignores the annotations in this package, however many there are."""
    assert (ROOT / "src" / "blame" / "py.typed").exists()


def test_classifiers_cover_every_python_ci_tests():
    """The matrix is the truth about what is supported; the classifiers are
    what PyPI shows. They drifted once -- CI proved 3.13 while PyPI said 3.12.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    classified = {
        c.rsplit(" :: ", 1)[1]
        for c in pyproject["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    }
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    tested = set(re.findall(r'python:\s*"(3\.\d+)"', ci))
    assert tested, "could not read the python versions out of the CI matrix"
    assert tested <= classified, (
        f"CI tests {sorted(tested - classified)}, PyPI does not advertise it"
    )


def test_publishing_action_is_pinned_to_an_exact_version():
    """That step carries the OIDC identity allowed to publish to PyPI, so it
    must not follow a moving tag like `release/v1`.

    It must not be pinned to a commit SHA either: the action runs a container
    from ghcr.io/pypa/gh-action-pypi-publish tagged with this exact ref, and
    only release tags exist there. A SHA fails with "manifest unknown".
    """
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    for line in release.splitlines():
        if "gh-action-pypi-publish@" in line:
            ref = line.split("@", 1)[1].split()[0]
            assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), f"not an exact release tag: {ref}"
            break
    else:
        raise AssertionError("release.yml no longer publishes to PyPI")
