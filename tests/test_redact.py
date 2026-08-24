import json

from mcpclerk.redact import Redactor, redact


def test_key_rules_replace_whole_value():
    out = redact({"api_key": "abc", "Password": "p", "Authorization": "x", "nested": {"token": 1}, "ok": "fine"})
    assert out["api_key"].startswith("[REDACTED:key-")
    assert out["Password"].startswith("[REDACTED:key-")
    assert out["Authorization"].startswith("[REDACTED:key-")
    assert out["nested"]["token"].startswith("[REDACTED:key-")
    assert out["ok"] == "fine"


def test_value_rules_in_strings_and_lists():
    text = ("Bearer abc.def-ghi sk-abcdefghijklmnopqrstuvwxyz AKIAABCDEFGHIJKLMNOP "
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789 xoxb-1234567890-abc "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
            "postgres://user:hunter2@host/db password=hunter2 done")
    out = redact({"content": text, "list": [text]})
    for rule in ("bearer", "openai-key", "aws-access-key", "github-token", "slack-token", "jwt", "url-userinfo", "kv-secret"):
        assert f"[REDACTED:{rule}]" in out["content"], rule
    assert "hunter2" not in out["content"] and "hunter2" not in out["list"][0]
    assert out["content"].endswith("done")


def test_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
    assert redact({"c": f"x {pem} y"})["c"] == "x [REDACTED:private-key-block] y"


def test_input_is_not_mutated_and_non_strings_pass():
    src = {"token": "t", "n": 3, "b": True, "none": None, "l": [1, "sk-abcdefghijklmnopqrstuvwxyz"]}
    snapshot = json.dumps(src, sort_keys=True)
    out = redact(src)
    assert json.dumps(src, sort_keys=True) == snapshot
    assert out["n"] == 3 and out["b"] is True and out["none"] is None and out["l"][0] == 1
    assert out["l"][1] == "[REDACTED:openai-key]"


def test_user_extensions_and_replace_builtin():
    r = Redactor(extend=[r"questrade[-_ ]?refresh[-_ ]?token\s*[:=]\s*\S+"], extend_keys=["refresh_id"])
    out = r.redact({"refresh_id": "x", "client_secret": "y", "note": "questrade_refresh_token=abc"})
    assert out["refresh_id"] == "[REDACTED:key-user-0]" and out["note"] == "[REDACTED:user-0]"
    assert out["client_secret"] == "[REDACTED:key-secret]"  # builtin key rule still applies
    only = Redactor(extend=[r"zzz"], replace_builtin=True)
    assert only.redact({"api_key": "sk-abcdefghijklmnopqrstuvwxyz zzz"})["api_key"] == "sk-abcdefghijklmnopqrstuvwxyz [REDACTED:user-0]"


def test_plain_words_are_not_over_redacted():
    out = redact({"path": "notes.md", "content": "the token economy and my password manager are topics"})
    assert out["content"] == "the token economy and my password manager are topics"
