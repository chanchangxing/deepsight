import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run(command, *, cwd, env=None, stdout=None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=stdout is None,
        check=False,
    )


def test_candidate_git_archive_builds_from_clean_checkout(tmp_path):
    """A clean archive must not depend on ignored local build-time source."""
    if not (REPOSITORY_ROOT / "node_modules").is_dir():
        pytest.skip("run npm ci before exercising the clean-checkout build")

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    listed = run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY_ROOT,
    )
    assert listed.returncode == 0, listed.stdout
    for relative_name in listed.stdout.split("\0"):
        if not relative_name:
            continue
        source = REPOSITORY_ROOT / relative_name
        destination = candidate / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for command in (
        ["git", "init", "--quiet"],
        ["git", "add", "--all"],
        [
            "git",
            "-c",
            "user.name=Clean Checkout Test",
            "-c",
            "user.email=clean-checkout@example.test",
            "commit",
            "--quiet",
            "-m",
            "candidate source",
        ],
    ):
        result = run(command, cwd=candidate)
        assert result.returncode == 0, result.stdout

    archive = tmp_path / "candidate.tar"
    with archive.open("wb") as output:
        archived = run(["git", "archive", "HEAD"], cwd=candidate, stdout=output)
    assert archived.returncode == 0

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with tarfile.open(archive) as source_archive:
        # The archive is created immediately above from this controlled fixture.
        source_archive.extractall(checkout)
    (checkout / "node_modules").symlink_to(
        REPOSITORY_ROOT / "node_modules", target_is_directory=True
    )

    environment = os.environ.copy()
    if environment.get("NVM_BIN"):
        environment["PATH"] = os.pathsep.join(
            [environment["NVM_BIN"], environment["PATH"]]
        )
    environment["WRANGLER_LOG_PATH"] = str(tmp_path / "wrangler.log")
    built = run(["npm", "run", "build"], cwd=checkout, env=environment)

    assert built.returncode == 0, built.stdout
