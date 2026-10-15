import json

import pytest

from mcpclerk import catalog


def test_catalog_union_preserves_risk_precedence():
    entry = catalog.merge({"x": {"read": ["op"], "targets": ["a"]}},
                          {"x": {"write": ["op"], "targets": ["b"]}})["x"]
    assert entry["read"] == ["op"] and entry["write"] == ["op"]
    assert catalog.validate_layer({"x": entry})["x"]["targets"] == ["b"]


@pytest.mark.parametrize("value", ["{", "[]", '{"unknown":"maybe"}', '{"x": {"read": "op"}}'])
def test_bad_user_catalog_fails_closed(tmp_path, value):
    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp_catalog.json").write_text(value)
    with pytest.raises(ValueError):
        catalog.load(environ={"MCPCLERK_HOME": str(home)})


def test_project_config_shape_and_unknown_mode(tmp_path):
    (tmp_path / "mcpclerk.json").write_text(json.dumps({"mcp": {"custom": {"read": ["lookup"]}}, "unknown": "block"}))
    entries, mode = catalog.load(str(tmp_path), environ={"MCPCLERK_HOME": str(tmp_path / "none")})
    assert mode == "block" and entries["custom"]["read"] == ["lookup"]
    (tmp_path / "mcpclerk.json").write_text('{"mcp": {}, "unknown":"maybe"}')
    with pytest.raises(ValueError):
        catalog.load(str(tmp_path), environ={"MCPCLERK_HOME": str(tmp_path / "none")})


def test_packaged_catalog_cannot_disappear_or_be_empty(monkeypatch):
    monkeypatch.setattr(catalog.Path, "is_file", lambda _path: False)
    with pytest.raises(ValueError, match="missing"):
        catalog._plugin()
    monkeypatch.setattr(catalog.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(catalog, "_read", lambda _path: {"_comment": "no services"})
    with pytest.raises(ValueError, match="no service entries"):
        catalog._plugin()
