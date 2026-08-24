"""Redaction of secret-looking values, for the log and the human prompt only.

Pure functions. The upstream never sees the output of this module; the proxy forwards the
original arguments untouched. Replacement text names the rule so the log shows that something
was hidden and which rule hid it.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

KEY_RULES: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in [
        ("key-api-key", r"api[_-]?key"),
        ("key-secret", r"secret"),
        ("key-token", r"token"),
        ("key-password", r"passw(or)?d"),
        ("key-authorization", r"^authorization$"),
        ("key-bearer", r"bearer"),
        ("key-cookie", r"cookie"),
        ("key-private-key", r"private[_-]?key"),
        ("key-credential", r"credential"),
    ]
]

VALUE_RULES: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pat))
    for name, pat in [
        ("bearer", r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
        ("openai-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
        ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"),
        ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        ("slack-token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        ("url-userinfo", r"://[^/\s:@]+:[^@\s]+@"),
        ("kv-secret", r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',;]+"),
    ]
]


def _compile_extra(patterns: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(f"user-{i}", re.compile(p)) for i, p in enumerate(patterns)]


def _compile_extra_keys(names: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(f"key-user-{i}", re.compile(re.escape(n), re.IGNORECASE)) for i, n in enumerate(names)]


class Redactor:
    def __init__(self, extend: Iterable[str] = (), extend_keys: Iterable[str] = (), replace_builtin: bool = False):
        base_values = [] if replace_builtin else list(VALUE_RULES)
        base_keys = [] if replace_builtin else list(KEY_RULES)
        self.value_rules = base_values + _compile_extra(extend)
        self.key_rules = base_keys + _compile_extra_keys(extend_keys)

    def _key_hit(self, key: str) -> str | None:
        for name, pat in self.key_rules:
            if pat.search(key):
                return name
        return None

    def redact_string(self, s: str) -> str:
        for name, pat in self.value_rules:
            s = pat.sub(f"[REDACTED:{name}]", s)
        return s

    def redact(self, obj: Any) -> Any:
        """Return a redacted deep copy. Never mutates the input."""
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                hit = self._key_hit(str(k))
                out[k] = f"[REDACTED:{hit}]" if hit else self.redact(v)
            return out
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        if isinstance(obj, tuple):
            return [self.redact(v) for v in obj]
        if isinstance(obj, str):
            return self.redact_string(obj)
        return obj


DEFAULT = Redactor()


def redact(obj: Any) -> Any:
    return DEFAULT.redact(obj)
