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
