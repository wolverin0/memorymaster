"""Application-owned semantic fingerprints for PostgreSQL RLS predicates."""
from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Mapping


_TEXT_CAST_RE = re.compile(
    r"::\s*(?:pg_catalog\.)?(?:text|character\s+varying)\b",
    re.IGNORECASE,
)
_ANY_ARRAY_RE = re.compile(
    r"(?P<left>[a-z_][a-z0-9_.]*)\s*=\s*any\s*"
    r"\(\s*array\[(?P<values>[^\]]+)]\s*\)",
    re.IGNORECASE,
)
_FROM_AS_RE = re.compile(
    r"\b(from|join)\s+([a-z_][a-z0-9_.]*)\s+as\s+",
    re.IGNORECASE,
)
_SAFE_QUOTED_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*")


def _protect_sql_quotes(value: str) -> tuple[str, tuple[str, ...]]:
    output: list[str] = []
    protected: list[str] = []
    index = 0
    while index < len(value):
        quote = value[index]
        if quote not in {"'", '"'}:
            output.append(quote)
            index += 1
            continue
        end = index + 1
        while end < len(value):
            if value[end] != quote:
                end += 1
                continue
            if end + 1 < len(value) and value[end + 1] == quote:
                end += 2
                continue
            end += 1
            break
        token = value[index:end]
        inner = token[1:-1]
        if quote == '"' and _SAFE_QUOTED_IDENTIFIER_RE.fullmatch(inner):
            output.append(inner)
        else:
            marker = f"__mm_sql_quoted_{len(protected)}__"
            protected.append(token)
            output.append(marker)
        index = end
    return "".join(output), tuple(protected)


def _restore_sql_quotes(value: str, protected: tuple[str, ...]) -> str:
    for index, token in enumerate(protected):
        value = value.replace(f"__mm_sql_quoted_{index}__", token)
    return value


def canonicalize_sql_tokens(value: object, *, drop_parentheses: bool = False) -> str:
    """Normalize SQL syntax without changing quoted literal/identifier bytes."""
    text, protected = _protect_sql_quotes(str(value or ""))
    text = text.lower()
    if drop_parentheses:
        text = text.replace("(", " ").replace(")", " ")
    return _restore_sql_quotes(" ".join(text.split()), protected)


def _normalize_deparse_variants(value: object) -> str:
    text, protected = _protect_sql_quotes(str(value or ""))
    text = text.lower()
    text = _TEXT_CAST_RE.sub("", text)
    text = _ANY_ARRAY_RE.sub(
        lambda match: f"{match.group('left')} in ({match.group('values')})",
        text,
    )
    text = _FROM_AS_RE.sub(r"\1 \2 ", text)
    return _restore_sql_quotes(" ".join(text.split()), protected)


def _flat_token_signature(value: object) -> str:
    text = _normalize_deparse_variants(value)
    return canonicalize_sql_tokens(text, drop_parentheses=True)


def _or_offsets(value: str) -> list[int]:
    offsets: list[int] = []
    quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and value[index:index + 2] == "or":
            before = value[index - 1] if index else " "
            after = value[index + 2] if index + 2 < len(value) else " "
            if not (before.isalnum() or before == "_") and not (
                after.isalnum() or after == "_"
            ):
                offsets.append(index)
        index += 1
    return offsets


def _smallest_parenthesized_group(value: str, offset: int) -> str | None:
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and char == "(":
            stack.append(index)
        elif not quoted and char == ")" and stack:
            pairs.append((stack.pop(), index))
        index += 1
    enclosing = [pair for pair in pairs if pair[0] < offset < pair[1]]
    if not enclosing:
        return None
    start, end = min(enclosing, key=lambda pair: pair[1] - pair[0])
    return value[start + 1:end]


def _or_group_signatures(value: object) -> tuple[str, ...]:
    normalized = _normalize_deparse_variants(value)
    groups: list[str] = []
    for offset in _or_offsets(normalized):
        group = _smallest_parenthesized_group(normalized, offset)
        if group is None:
            groups.append("<ungrouped-or>")
        else:
            groups.append(_flat_token_signature(group))
    return tuple(groups)


def expressions_match(actual: object, expected: object) -> bool:
    """Compare exact tokens plus the security-relevant grouping of every OR."""
    if actual is None or expected is None:
        return actual is expected
    return (
        _flat_token_signature(actual) == _flat_token_signature(expected)
        and _or_group_signatures(actual) == _or_group_signatures(expected)
    )


def expected_policy_expressions(
    tenant_tables: Iterable[str],
    deny_tables: Iterable[str],
    command_policies: Mapping[str, str],
    permit_policies: Mapping[str, str],
) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )
    expected: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for table in tenant_tables:
        for command, restrictive_name in command_policies.items():
            predicate = (
                migration._READ_PREDICATES[table]
                if command == "SELECT"
                else migration._WRITE_PREDICATES[table]
            )
            expressions = (
                None if command == "INSERT" else predicate,
                predicate if command in {"INSERT", "UPDATE"} else None,
            )
            expected[(table, restrictive_name)] = expressions
            expected[(table, permit_policies[command])] = expressions
    for table in deny_tables:
        expected[(table, "memorymaster_team_deny")] = ("FALSE", "FALSE")
    return expected
