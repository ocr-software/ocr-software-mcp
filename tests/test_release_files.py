"""The files that go to PyPI and to the MCP registry agree with each other."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RULE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")  # schema of the registry: name
PINNED = re.compile(r'^dependencies = \[("[A-Za-z0-9_.-]+==[0-9.]+"(, )?)+\]$', re.M)


def test_server_json_matches_pyproject_and_registry_rules():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package = server["packages"][0]
    assert NAME_RULE.match(server["name"]) and 1 <= len(server["description"]) <= 100 and len(server["title"]) <= 100
    assert re.search(r'^name = "%s"$' % re.escape(package["identifier"]), pyproject, re.M)
    assert re.search(r'^version = "%s"$' % re.escape(server["version"]), pyproject, re.M) and package["version"] == server["version"]
    assert package["registryType"] == "pypi" and package["registryBaseUrl"] == "https://pypi.org"


def test_registry_checks_the_name_in_the_readme():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    readme = (ROOT / "INSTALL.txt").read_text(encoding="utf-8")
    assert re.search(r"^mcp-name: %s$" % re.escape(server["name"]), readme, re.M)


def test_dependencies_are_pinned_exactly():
    assert PINNED.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_version_is_the_same_as_in_pyproject():
    from ocr_software_mcp import __version__
    assert re.search(r'^version = "%s"$' % re.escape(__version__), (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)


def test_publish_workflow_is_trusted_publishing_with_tests_and_tag_check_before_upload():
    """Review 13.11.2026: no token, OIDC, environment pypi, release trigger, tag = version, tests before the upload step."""
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    for needed in ("types: [published]", "id-token: write", "environment: pypi", "GITHUB_REF_NAME"):
        assert needed in wf
    assert "password" not in wf and "secrets." not in wf
    assert wf.index("GITHUB_REF_NAME") < wf.index("pytest") < wf.index("twine check") < wf.index("gh-action-pypi-publish")
