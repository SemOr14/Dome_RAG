"""Redact before SQLite, including raw frames, invalid JSON and embedded JSON strings."""

import re

from .codec import dumps, loads

MASK = "[REDACTED]"
SENSITIVE = re.compile(
    r"^(?:key|keys|token|tokens|secret|secrets|password|passwd|pwd|authorization|"
    r"proxyauthorization|cookie|setcookie|apikey|apisecret|accesskey|privatekey|"
    r"clientsecret|accesstoken|refreshtoken|idtoken|authtoken|servicetoken|"
    r"xapikey|xauthtoken|xservicetoken|xcsrftoken|csrftoken|sessioncookie)$",
    re.I,
)
ASSIGNMENT = re.compile(
    r"""(?ix)(["']?(?:api[_-]?key|api[_-]?secret|access[_-]?token|refresh[_-]?token|
    client[_-]?secret|auth[_-]?token|service[_-]?token|password|passwd|secret|token|key)
    ["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s&,;}\]]+)"""
)
AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
PROVIDER_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]+)\b")
URL_AUTH = re.compile(r"(\w+://)[^\s/@:]+:[^\s/@]+@")


def sensitive(key):
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return bool(SENSITIVE.fullmatch(normalized)) or normalized.endswith(
        ("apikey", "password", "accesstoken", "refreshtoken", "clientsecret", "privatekey")
    )


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def text(self, text, depth=0):
        # Preserve the original text unless a secret must be removed.
        if depth < 8:
            try:
                obj = loads(text)
            except (ValueError, TypeError, RecursionError):
                pass
            else:
                if isinstance(obj, (dict, list)):
                    cleaned = self.value(obj, depth + 1)
                    if cleaned != obj:
                        return dumps(cleaned)
        for secret in self.secrets:
            text = text.replace(secret, MASK)
        text = AUTH.sub(lambda m: m[1] + " " + MASK, text)
        text = ASSIGNMENT.sub(lambda m: m[1] + '"' + MASK + '"', text)
        text = URL_AUTH.sub(lambda m: m[1] + MASK + "@", text)
        return PROVIDER_KEY.sub(MASK, text)

    def value(self, value, depth=0):
        if isinstance(value, dict):
            return {
                self.text(k, depth + 1): MASK if sensitive(k) else self.value(v, depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.value(v, depth + 1) for v in value]
        if isinstance(value, str):
            return self.text(value, depth)
        return value

    def body(self, body):
        # Invalid UTF-8 is represented explicitly, not silently discarded. This API accepts JSON.
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="backslashreplace")
        return self.text(text)
