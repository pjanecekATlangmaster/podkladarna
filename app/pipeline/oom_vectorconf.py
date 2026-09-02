from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.settings import CONFIG_DIR


@dataclass(frozen=True)
class VectorconfRule:
    symbol_name: str
    kp_code: str
    filter_expr: str


def load_vectorconf(name: str) -> list[VectorconfRule]:
    path = CONFIG_DIR / name
    rules: list[VectorconfRule] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        rules.append(VectorconfRule(parts[0], parts[1], parts[2]))
    return rules


def _norm(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def match_filter(props: dict, expr: str) -> bool:
    expr = expr.strip()
    if "&" in expr:
        return all(match_filter(props, part) for part in expr.split("&"))
    if expr.endswith("!="):
        key = expr[:-2]
        return _norm(props.get(key)) != ""
    if "=" in expr:
        key, expected = expr.split("=", 1)
        return _norm(props.get(key)) == expected
    return False


def match_feature(props: dict, rules: list[VectorconfRule]) -> VectorconfRule | None:
    for rule in rules:
        if match_filter(props, rule.filter_expr):
            return rule
    return None
