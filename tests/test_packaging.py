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

# The sdist deliberately ships no .github/. These two checks are about the
# repository, not the package, so they skip rather than fail when the suite
# is run from an unpacked sdist -- which is how downstream packagers run it.
repo_only = pytest.mark.skipif(not (ROOT / ".github").is_dir(), reason="not a source checkout")


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


@repo_only
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
    # And the other way. A classifier is a claim someone installing acts on, so
    # it may not run ahead of the matrix either: 3.14 worked for a year while
    # PyPI said otherwise, and nothing here would have noticed the reverse.
    assert classified <= tested, (
        f"PyPI advertises {sorted(classified - tested)}, CI does not test it"
    )


@repo_only
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


@repo_only
def test_ci_floor_job_pins_the_versions_pyproject_declares():
    """The dependency lower bounds are a claim; CI's floor job is the check.

    They are written in two places, so this asserts they agree. A bound raised
    in pyproject without updating the job would leave the job testing a version
    the package no longer claims to support.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = {}
    for spec in pyproject["project"]["dependencies"]:
        name, _, bound = spec.partition(">=")
        assert bound, f"{spec!r} has no lower bound to check"
        declared[name.strip()] = tuple(int(p) for p in bound.strip().split("."))

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    pinned = {
        name: tuple(int(p) for p in ver.split("."))
        for name, ver in re.findall(r'"([a-z]+)==([0-9.]+)"', ci)
    }

    for name, bound in declared.items():
        assert name in pinned, f"the floor job does not pin {name}"
        got = pinned[name]
        padded = bound + (0,) * (len(got) - len(bound))
        assert got == padded, f"{name}: pyproject says >={bound}, floor job pins =={got}"


def _jobs(workflow: Path) -> dict[str, str]:
    """Every job in a workflow, as name -> its own block of text.

    Parsed with a regex rather than a YAML library on purpose: the suite's only
    declared test dependency is pytest, and the CI floor job installs nothing
    else. A checker that cannot run everywhere the suite runs is not a checker.
    """
    text = workflow.read_text()
    body = text.split("\njobs:\n", 1)[1]
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^  ([A-Za-z_][\w-]*):$", body, re.M)]
    assert starts, f"no jobs found in {workflow.name}"
    bounds = [*[s for s, _ in starts], len(body)]
    return {name: body[bounds[i] : bounds[i + 1]] for i, (_, name) in enumerate(starts)}


@repo_only
@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_every_job_declares_a_timeout(workflow):
    """GitHub's default is six hours.

    A job that hangs -- a deadlock in the tracer, a nightly dependency that
    never returns, a runner that loses its network -- otherwise burns the whole
    budget and blocks everything queued behind it.
    """
    for name, block in _jobs(ROOT / ".github" / "workflows" / workflow).items():
        assert "timeout-minutes:" in block, f"{workflow}: job {name!r} has no timeout"


@repo_only
def test_the_scheduled_run_is_guarded_against_forks():
    """A cron is inherited by every fork of the repository.

    Without a guard each fork runs this workflow weekly, spending its owner's
    Actions minutes on a schedule they never set up. The guard has to be on
    every job: a job added without it silently reintroduces the problem for
    everyone who has forked.
    """
    path = ROOT / ".github" / "workflows" / "ci.yml"
    assert "schedule:" in path.read_text(), "no cron here any more; drop this test"
    for name, block in _jobs(path).items():
        assert "github.event_name != 'schedule'" in block, (
            f"job {name!r} runs on a fork's weekly cron"
        )


@repo_only
def test_only_pull_request_builds_are_cancelled():
    """Superseding a push to main throws away the record of that commit's build.

    Collapsing repeated pushes to one pull request is the point of the setting;
    doing it to main is not.
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    line = next(ln for ln in ci.splitlines() if "cancel-in-progress:" in ln)
    assert "github.event_name == 'pull_request'" in line, f"unconditional cancel: {line.strip()}"
