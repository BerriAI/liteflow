import re

_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(value: str) -> str:
    return _CONTROL.sub("", _ESCAPE.sub("", value))
