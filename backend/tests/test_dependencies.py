from __future__ import annotations

import zipfile

import pytest
from packaging.requirements import Requirement

from devflow.dependencies import (
    MAX_REQUIREMENTS,
    MAX_SOURCE_BYTES,
    normalize_requirements,
    parse_dependencies,
    wheel_requirements,
)


def test_requirements_are_normalized_deduplicated_and_selected_only():
    files = {
        "requirements.txt": '# comment\nRequests[SOCKS]>=2,<3 # note\nrequests[socks]<3,>=2\n'
                            'colorama; sys_platform == "win32"\n',
        "pyproject.toml": 'invalid unselected content',
    }
    result = parse_dependencies(files, "requirements.txt", [])
    assert result == ['colorama; sys_platform == "win32"', 'requests[socks]<3,>=2']
    assert all(Requirement(value).url is None for value in result)


def test_pyproject_static_dependencies_and_only_selected_extras():
    files = {"pyproject.toml": '''
[build-system]
requires = ["malicious-build @ https://example.invalid/build.whl"]
build-backend = "never_import_this"
[project]
dependencies = ["Requests>=2"]
[project.optional-dependencies]
test = ["pytest>=8"]
Other = ["private @ https://example.invalid/secret.whl"]
'''}
    assert parse_dependencies(files, "pyproject.toml", ["TEST"]) == ["pytest>=8", "requests>=2"]
    assert parse_dependencies(files, "pyproject.toml", []) == ["requests>=2"]
    with pytest.raises(ValueError, match="URL"):
        parse_dependencies(files, "pyproject.toml", ["other"])


@pytest.mark.parametrize("value", [
    "https://example.org/a.whl", "foo @ https://example.org/a.whl",
    "foo @ git+https://example.org/repo", "git+https://example.org/repo",
    "foo @ file:///private/a.whl", "./local", "../local", "/tmp/local", "C:\\local",
    "-e .", "-r nested.txt", "-c constraints.txt", "--index-url https://evil.invalid",
    "--extra-index-url https://evil.invalid", "foo --hash=sha256:123",
    "foo \\\n >=1", "foo\x00", "foo\t>=1",
])
def test_rejects_non_named_or_option_requirements(value):
    with pytest.raises(ValueError):
        parse_dependencies({"requirements.txt": value}, "requirements.txt", [])


@pytest.mark.parametrize("source", [
    "../requirements.txt", "/requirements.txt", "a/../requirements.txt",
    "a\\requirements.txt", "C:/requirements.txt", "a//requirements.txt", "setup.py",
    "build-system", "poetry.lock", "requirements.in",
])
def test_invalid_source(source):
    with pytest.raises(ValueError):
        parse_dependencies({source: "requests"}, source, [])


@pytest.mark.parametrize("text", [
    '[project]\ndynamic=["dependencies"]',
    '[project]\ndynamic=["optional-dependencies"]',
    '[project]\ndynamic="dependencies"',
    '[project]\ndependencies="requests"',
    '[project]\ndependencies=[7]',
    '[tool.poetry.dependencies]\npython="^3.11"',
    '[project',
])
def test_dynamic_or_nonstatic_pyproject_rejected(text):
    with pytest.raises(ValueError):
        parse_dependencies({"pyproject.toml": text}, "pyproject.toml", [])


def test_missing_extra_source_and_bounded_inputs():
    assert parse_dependencies({}, None, []) == []
    with pytest.raises(ValueError):
        parse_dependencies({}, None, ["test"])
    with pytest.raises(ValueError):
        parse_dependencies({}, "requirements.txt", [])
    with pytest.raises(ValueError):
        parse_dependencies({"pyproject.toml": "[project]"}, "pyproject.toml", ["missing"])
    with pytest.raises(ValueError):
        parse_dependencies({"requirements.txt": "x" * (MAX_SOURCE_BYTES + 1)},
                           "requirements.txt", [])
    with pytest.raises(ValueError):
        normalize_requirements(["foo"] * (MAX_REQUIREMENTS + 1))
    with pytest.raises(ValueError):
        normalize_requirements("requests")


def test_normalized_extra_collision_is_rejected():
    text = '[project.optional-dependencies]\nfoo_bar=[]\nfoo-bar=[]'
    with pytest.raises(ValueError, match="ambiguous"):
        parse_dependencies({"pyproject.toml": text}, "pyproject.toml", [])


def test_transitive_wheel_urls_rejected_even_under_inactive_marker(tmp_path):
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\n"
                       'Requires-Dist: other @ https://evil.invalid/other.whl ; extra == "unused"\n')
    with pytest.raises(ValueError, match="URL"):
        wheel_requirements(path)


def test_wheel_metadata_is_read_without_extracting_or_importing(tmp_path):
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\n"
                       'Requires-Dist: Requests>=2\n')
        wheel.writestr("demo/__init__.py", "raise AssertionError('never execute')")
    assert wheel_requirements(path) == ["requests>=2"]
    assert not (tmp_path / "demo").exists()


def test_oversized_wheel_metadata_rejected(tmp_path):
    path = tmp_path / "demo.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("demo.dist-info/METADATA", "x" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="metadata"):
        wheel_requirements(path)
