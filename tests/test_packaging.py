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
    ignores the annotations in this package, however many there are.

    Checked on the *installed* package rather than on `src/`. A marker sitting
    in the repository proves nothing about what a user receives -- and the CI
    job that runs this suite against the built wheel deletes `src/` first, so
    there would be nothing there to look at.
    """
    import blame

    assert (Path(blame.__file__).parent / "py.typed").exists()


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


_WORKFLOW_DIR = ROOT / ".github" / "workflows"
# Discovered rather than listed, so a workflow added later is covered by these
# checks without anyone remembering to add it here.
WORKFLOWS = (
    sorted(p.name for p in _WORKFLOW_DIR.glob("*.yml")) if _WORKFLOW_DIR.is_dir() else ["ci.yml"]
)


@repo_only
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_job_declares_a_timeout(workflow):
    """GitHub's default is six hours.

    A job that hangs -- a deadlock in the tracer, a nightly dependency that
    never returns, a runner that loses its network -- otherwise burns the whole
    budget and blocks everything queued behind it.
    """
    for name, block in _jobs(ROOT / ".github" / "workflows" / workflow).items():
        assert "timeout-minutes:" in block, f"{workflow}: job {name!r} has no timeout"


@repo_only
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_scheduled_run_is_guarded_against_forks(workflow):
    """A cron is inherited by every fork of the repository.

    Without a guard each fork runs that workflow on schedule, spending its
    owner's Actions minutes on something they never set up. The guard has to be
    on every job of every workflow that has a cron: one job added without it
    silently reintroduces the problem for everyone who has forked.
    """
    path = ROOT / ".github" / "workflows" / workflow
    if "schedule:" not in path.read_text():
        pytest.skip(f"{workflow} has no cron")
    for name, block in _jobs(path).items():
        assert "github.event_name != 'schedule'" in block, (
            f"{workflow}: job {name!r} runs on a fork's cron"
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


@repo_only
def test_dependabot_watches_the_action_pins():
    """Every workflow pins its actions to an exact release tag, which is what
    makes the supply chain reviewable. Pins rot: without something watching
    them, "pinned" quietly becomes "stuck on a version with a known problem".
    """
    config = (ROOT / ".github" / "dependabot.yml").read_text()
    assert "github-actions" in config, "nothing watches the action pins"
    # Dependabot's default subject line is "Bump x from a to b", which does not
    # parse as a conventional commit -- and the PR title check would reject it.
    assert "chore(deps)" in config, "dependabot's PR titles would not follow the convention"


@repo_only
def test_codeql_writes_its_findings_where_they_can_be_read():
    """A security workflow that cannot upload results is decoration.

    It needs security-events: write, and it must be scoped to that job rather
    than granted to the whole workflow.
    """
    path = ROOT / ".github" / "workflows" / "codeql.yml"
    text = path.read_text()
    assert "permissions: {}" in text, "the workflow default should grant nothing"
    for name, block in _jobs(path).items():
        assert "security-events: write" in block, f"job {name!r} cannot upload its findings"


@repo_only
def test_the_release_is_tested_before_it_is_uploaded():
    """Publishing is gated on a human approving a deployment, and that dialog
    shows a deploy request rather than a test result.

    Without a suite in the build job, approving means trusting from memory that
    the tagged commit was green -- and a tag is easy to put on the wrong
    commit. The order matters as much as the presence: a suite that runs after
    the upload cannot stop the artifact reaching the publish job.
    """
    jobs = _jobs(ROOT / ".github" / "workflows" / "release.yml")
    build = jobs["build"]
    # The invocation, not the word: `pytest` appears in a comment in that job,
    # and an earlier version of this test passed with the step itself deleted.
    assert "-m pytest" in build, "the build job uploads an artifact it never tested"
    assert "upload-artifact" in build, "the build job no longer hands anything to publish"
    assert build.index("-m pytest") < build.index("upload-artifact"), (
        "the artifact is uploaded before the suite runs, so a failure cannot stop it"
    )
    # Against the built wheel, not the checkout: the point is to test the file
    # that is about to be published, installed the way a user installs it.
    assert "dist/*.whl" in build, "the suite runs against the source tree, not the wheel"
    assert "needs: build" in jobs["publish"], "publish no longer waits for build"


@repo_only
def test_the_publish_step_still_signs_what_it_uploads():
    """PEP 740 provenance is what lets someone check that a file on PyPI was
    built by this repository's release workflow and not uploaded by whoever
    obtained a token. `gh-action-pypi-publish` emits it by default under
    trusted publishing, so there are only two ways to lose it: turn it off, or
    drop the OIDC permission that signs it.
    """
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "attestations: false" not in release, "provenance is switched off"
    assert "id-token: write" in release, "nothing can be signed without the OIDC identity"


USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(?:\s+#\s*(\S+))?", re.M)

# Pinned to a release tag on purpose, and the only action that is. It runs a
# Docker image from ghcr.io tagged with whatever ref appears here, and only
# release tags are published there -- a commit SHA gives "manifest unknown".
# The reasoning is in .github/zizmor.yml and in release.yml itself.
_TAG_PINNED = {"pypa/gh-action-pypi-publish"}


@repo_only
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_action_is_pinned_to_a_commit(workflow):
    """A tag can be moved to point at different code; a commit cannot.

    Everything here runs with a token on every push, so a tag pin means
    trusting whoever can move that tag, forever. The version goes in a trailing
    comment: a bare SHA is unreviewable, and Dependabot writes the comment back
    when it bumps the pin.
    """
    text = (ROOT / ".github" / "workflows" / workflow).read_text()
    refs = USES.findall(text)
    assert refs, f"{workflow} runs no actions at all -- has the format changed?"
    for ref, comment in refs:
        repo, _, pin = ref.partition("@")
        if repo in _TAG_PINNED:
            continue
        assert re.fullmatch(r"[0-9a-f]{40}", pin), f"{ref} is not pinned to a commit"
        assert re.fullmatch(r"v\d+(\.\d+)*", comment or ""), (
            f"{ref} has no version comment, so nobody can tell what it is pinned to"
        )


@repo_only
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_checkout_leaves_no_credential_behind(workflow):
    """`actions/checkout` writes a credential into .git/config and leaves it
    there for the rest of the job unless told not to.

    Everything that job runs afterwards can read it -- every test, and every
    dependency of every test. This repository installs pandas, matplotlib and
    their transitive dependencies before running anything.
    """
    lines = (ROOT / ".github" / "workflows" / workflow).read_text().splitlines()
    checkouts = 0
    for i, line in enumerate(lines):
        if "uses:" in line and "actions/checkout@" in line:
            checkouts += 1
            block = "\n".join(lines[i : i + 6])
            assert "persist-credentials: false" in block, (
                f"{workflow}:{i + 1} leaves a credential in .git/config"
            )
    assert checkouts, f"{workflow} no longer checks the repository out"


@repo_only
def test_the_workflows_themselves_are_audited():
    """They are the one part of this repository that executes third-party code
    on every push, and they were the one part nothing read."""
    lint = _jobs(ROOT / ".github" / "workflows" / "ci.yml")["lint"]
    config = (ROOT / ".pre-commit-config.yaml").read_text()
    assert "zizmor" in config, "nothing audits the workflows"
    assert "actionlint" in lint, "nothing checks the shell inside run: blocks"
    # The linter is downloaded from a release. Fetching a binary and running it
    # unverified inside the job that guards the supply chain would be its own
    # answer to this test.
    assert "sha256sum -c" in lint, "the downloaded binary is not checksummed"
    assert (ROOT / ".github" / "zizmor.yml").exists(), "the audit exceptions are gone"


@repo_only
def test_every_document_in_the_root_ships_in_the_sdist():
    """The sdist include list is maintained by hand and goes stale in one
    direction only: a new document does not ship, downstream packagers never
    receive it, and nothing fails -- tests do not read docs.

    `check-sdist` in CI compares the whole sdist against what git tracks. This
    is the cheap offline half of that, covering the case that actually
    happened: SUPPORT.md was written, committed, and shipped nowhere.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    missing = [p.name for p in sorted(ROOT.glob("*.md")) if p.name not in include]
    assert not missing, f"tracked in git, absent from the sdist: {missing}"


@repo_only
def test_the_suite_measures_what_it_reaches():
    """Coverage went unmeasured until `cli.py` was found at 0% -- every
    subcommand and every error path unexecuted, with the suite green.

    A floor that does not fail the build is a number in a log nobody reads.
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "--cov=blame" in ci, "nothing measures coverage"
    floor = re.search(r"--fail-under=(\d+)", ci)
    assert floor, "coverage is measured but nothing fails when it drops"
    assert int(floor.group(1)) >= 85, "the floor is too low to notice a module going untested"


@repo_only
def test_the_linters_have_one_source_of_truth():
    """CI runs the contributor's hooks rather than a second list of its own.

    With two lists they drift, and the drift is silent in both directions: a
    hook reformats what CI then rejects, or CI runs an unpinned linter whose
    next release turns an unrelated pull request red.
    """
    lint = _jobs(ROOT / ".github" / "workflows" / "ci.yml")["lint"]
    config = (ROOT / ".pre-commit-config.yaml").read_text()

    assert "pre-commit run --all-files" in lint, "CI does not run the hooks"
    assert "uvx ruff" not in lint, "ruff is invoked directly as well as through the hooks"
    for hook in ("ruff-check", "ruff-format", "zizmor"):
        assert f"id: {hook}" in config, f"{hook} is not among the hooks"

    # Nothing watches these: Dependabot has no pre-commit ecosystem, so the
    # revisions only move when somebody runs `pre-commit autoupdate`. A branch
    # or a bare SHA would make that impossible to review.
    revs = re.findall(r"^\s+rev: (\S+)", config, re.M)
    assert revs, "no pinned hook revisions"
    assert all(re.fullmatch(r"v\d+(\.\d+)*", rev) for rev in revs), revs
