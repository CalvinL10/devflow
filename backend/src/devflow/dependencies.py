"""Static inputs and wheel-only helpers; never execute project build backends.

Downloads use --no-deps: validate metadata BEFORE transitive network requests.
Offline pip resolves the bounded wheel set; unsupported graphs fail closed.
"""
from __future__ import annotations

import email.parser
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

MAX_REQUIREMENTS = 200
MAX_SOURCE_BYTES = 64 * 1024
MAX_WHEEL_BYTES = 256 * 1024 * 1024
TOOLS = ["pytest>=8,<10", "ruff>=0.9,<1"]
DEPENDENCY_ENV = "/deps/venv"


def normalize_requirements(requirements: Sequence[str]) -> list[str]:
    if not isinstance(requirements, (list, tuple)) or len(requirements) > MAX_REQUIREMENTS:
        raise ValueError("requirements must be a bounded list of strings")
    result = set()
    for value in requirements:
        if (not isinstance(value, str) or not value.strip() or len(value) > 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("invalid requirement text")
        try:
            requirement = Requirement(value)
        except InvalidRequirement as error:
            raise ValueError("only named public PyPI requirements are supported") from error
        if requirement.url is not None:
            raise ValueError("URL, VCS and local dependencies are not supported")
        name = canonicalize_name(requirement.name)
        extras = sorted(canonicalize_name(extra) for extra in requirement.extras)
        normalized = name + (f"[{','.join(extras)}]" if extras else "")
        normalized += str(requirement.specifier)
        if requirement.marker:
            normalized += f"; {requirement.marker}"
        result.add(normalized)
    normalized = sorted(result)
    if len(json.dumps(normalized).encode()) > MAX_SOURCE_BYTES:
        raise ValueError("requirements exceed the input limit")
    return normalized


def parse_dependencies(
    files: Mapping[str, str], source: str | None, extras: Sequence[str] = (),
) -> list[str]:
    """Read exactly the selected relative requirements*.txt or static pyproject.

    None selects no dependencies. Extras select PEP 621 optional-dependencies.
    Build-system declarations are ignored, never installed; dynamic dependency
    declarations and attempts to select build sources are rejected.
    """
    if (not isinstance(extras, (list, tuple)) or len(extras) > 32
            or any(not isinstance(extra, str) or not re.fullmatch(
                r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", extra
            ) for extra in extras)):
        raise ValueError("invalid extras selection")
    if source is None:
        if extras:
            raise ValueError("extras require a pyproject source")
        return []
    if (not isinstance(source, str) or "\\" in source or ":" in source
            or source.startswith("/") or any(p in ("", ".", "..") for p in source.split("/"))):
        raise ValueError("dependency source must be a safe relative file name")
    path = PurePosixPath(source)
    text = files.get(source)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("selected dependency source is missing or too large")
    if path.name == "pyproject.toml":
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ValueError("invalid pyproject.toml") from error
        project = document.get("project")
        if not isinstance(project, dict):
            raise ValueError("pyproject requires a static [project] table")
        dynamic = project.get("dynamic", [])
        if (not isinstance(dynamic, list) or any(not isinstance(v, str) for v in dynamic)
                or {"dependencies", "optional-dependencies"}.intersection(dynamic)):
            raise ValueError("dynamic dependencies are not supported")
        requirements = project.get("dependencies", [])
        if not isinstance(requirements, list):
            raise ValueError("project.dependencies must be a list")
        requirements = list(requirements)
        optional = project.get("optional-dependencies", {})
        if not isinstance(optional, dict):
            raise ValueError("project.optional-dependencies must be a table")
        selected = {}
        for key, values in optional.items():
            normalized_key = canonicalize_name(key)
            if normalized_key in selected:
                raise ValueError("ambiguous normalized extra name")
            selected[normalized_key] = values
        for extra in sorted({canonicalize_name(e) for e in extras}):
            if extra not in selected:
                raise ValueError(f"unknown project extra: {extra}")
            values = selected.get(extra)
            if not isinstance(values, list):
                raise TypeError(f"invalid project extra: {extra}")
            requirements.extend(values)
        return normalize_requirements(requirements)
    if path.suffix != ".txt" or not path.name.startswith("requirements"):
        raise ValueError("select requirements*.txt or pyproject.toml, not build dependencies")
    if extras:
        raise ValueError("extras selection requires pyproject.toml")
    requirements = []
    for line in text.splitlines():
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or "\\" in line:
            raise ValueError("requirements options and continuations are not supported")
        requirements.append(line)
    return normalize_requirements(requirements)


def wheel_requirements(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as wheel:
        if (len(wheel.infolist()) > 20_000
                or sum(entry.file_size for entry in wheel.infolist()) > MAX_WHEEL_BYTES):
            raise ValueError("wheel exceeds unpacked size limits")
        entries = [entry for entry in wheel.infolist() if entry.filename.endswith(
            ".dist-info/METADATA"
        )]
        if len(entries) != 1 or entries[0].file_size > MAX_SOURCE_BYTES:
            raise ValueError("invalid or oversized wheel metadata")
        with wheel.open(entries[0]) as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("oversized wheel metadata")
        metadata = email.parser.BytesParser().parsebytes(data)
        if metadata.defects or metadata.get("Name") is None or metadata.get("Version") is None:
            raise ValueError("invalid wheel metadata headers")
    return normalize_requirements(metadata.get_all("Requires-Dist", []))


def _pip(arguments: list[str], deadline: float) -> None:
    from devflow.candidate_runner import _run_bounded, captured_text

    remaining = min(45, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("dependency preparation deadline exceeded")
    code, out, err, timed_out, limited = _run_bounded(
        ["/usr/local/bin/python", "-I", "-m", "pip", "--isolated",
         "--disable-pip-version-check", *arguments], cwd=Path("/tmp"),
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
             "LANG": "C.UTF-8", "PIP_CONFIG_FILE": os.devnull}, timeout_seconds=remaining,
    )
    if code or timed_out or limited:
        raise RuntimeError(f"wheel operation failed: {captured_text(err or out)}")


def download(requirements: list[str]) -> None:
    requirements = normalize_requirements([*requirements, *TOOLS])
    destination = Path("/tmp/download")
    destination.mkdir()
    deadline = time.monotonic() + 150
    pending = list(requirements)
    seen = set()
    requested_extras: dict[str, set[str]] = {}
    while pending:
        value = pending.pop(0)
        if value in seen:
            continue
        if len(seen) >= MAX_REQUIREMENTS:
            raise ValueError("dependency graph exceeds the requirement limit")
        seen.add(value)
        requirement = Requirement(value)
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        requested_extras.setdefault(canonicalize_name(requirement.name), set()).update(
            requirement.extras
        )
        _pip(["download", "--no-deps", "--only-binary=:all:", "--no-cache-dir",
              "--index-url=https://pypi.org/simple", "--timeout=10", "--retries=0",
              "--dest", str(destination), value], deadline)
        wheels = list(destination.iterdir())
        if (len(wheels) > MAX_REQUIREMENTS
                or sum(p.stat().st_size for p in wheels) > MAX_WHEEL_BYTES):
            raise ValueError("wheel download exceeds storage limits")
        for wheel in wheels:
            if wheel.suffix != ".whl":
                raise ValueError("source/build distributions are not supported")
            wheel_name = str(parse_wheel_filename(wheel.name)[0])
            contexts = {"", *requested_extras.get(wheel_name, set())}
            for dependency in wheel_requirements(wheel):
                item = Requirement(dependency)
                if item.marker and not any(item.marker.evaluate({"extra": e}) for e in contexts):
                    continue
                item.marker = None
                normalized = normalize_requirements([str(item)])[0]
                if normalized not in seen and normalized not in pending:
                    pending.append(normalized)
    for wheel in destination.iterdir():
        shutil.copyfile(wheel, Path("/wheels") / wheel.name)


def install(requirements: list[str]) -> dict[str, str]:
    requirements = normalize_requirements([*requirements, *TOOLS])
    unpacked = 0
    for wheel in Path("/wheels").iterdir():
        if wheel.suffix != ".whl":
            raise ValueError("non-wheel artifact in wheelhouse")
        wheel_requirements(wheel)
        with zipfile.ZipFile(wheel) as archive:
            unpacked += sum(entry.file_size for entry in archive.infolist())
        if unpacked > MAX_WHEEL_BYTES:
            raise ValueError("dependency environment exceeds unpacked size limit")
    subprocess.run(["/usr/local/bin/python", "-I", "-m", "venv", "--without-pip",
                    DEPENDENCY_ENV], check=True, timeout=15)
    _pip(["--python", f"{DEPENDENCY_ENV}/bin/python", "install", "--no-index",
          "--find-links=/wheels", "--only-binary=:all:", "--no-cache-dir", "--no-compile",
          *requirements], time.monotonic() + 90)
    import importlib.metadata
    versions = {}
    for directory in Path(DEPENDENCY_ENV).glob("lib/python*/site-packages"):
        for distribution in importlib.metadata.distributions(path=[str(directory)]):
            versions[canonicalize_name(distribution.metadata["Name"])] = distribution.version
    return versions


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"download", "install"}:
        raise SystemExit("usage: dependencies <download|install> <normalized-json>")
    requirements = normalize_requirements(json.loads(sys.argv[2]))
    if sys.argv[1] == "download":
        download(requirements)
    else:
        print(json.dumps(install(requirements), sort_keys=True))


if __name__ == "__main__":
    main()
