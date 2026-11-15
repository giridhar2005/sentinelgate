from __future__ import annotations

import re
from typing import List, Optional, Tuple

import sqlglot
from sqlglot import exp


def inspect_sql(
    sql_query: str,
    allowed_tables: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Inspect a SQL query for destructive mutations (DROP, TRUNCATE, unconstrained DELETE/UPDATE)."""
    if not sql_query or not sql_query.strip():
        return True, None, None

    raw_sql = sql_query.strip()

    # 1. AST Parsing via sqlglot
    try:
        expressions = sqlglot.parse(raw_sql)
    except Exception:
        expressions = []

    # Check policy allowlist tables
    if allowed_tables and expressions:
        for expr in expressions:
            if expr is not None:
                for tbl_node in expr.find_all(exp.Table):
                    tbl_name = tbl_node.name.lower()
                    if tbl_name and not any(
                        tbl_name == t.lower() or tbl_name.startswith(t.lower())
                        for t in allowed_tables
                    ):
                        return (
                            False,
                            "DESTRUCTIVE_SQL_TABLE_NOT_ALLOWLISTED",
                            f"Table '{tbl_name}' is not in allowed_sql_tables.",
                        )

    if expressions:
        for expression in expressions:
            if expression is None:
                continue

            # Check DROP statement
            if isinstance(expression, exp.Drop) or expression.key == "drop":
                return (
                    False,
                    "DESTRUCTIVE_SQL_DROP",
                    f"Destructive SQL statement: '{raw_sql[:80]}'",
                )

            # Check TRUNCATE statement
            if isinstance(expression, exp.TruncateTable) or "truncate" in expression.key:
                return (
                    False,
                    "DESTRUCTIVE_SQL_TRUNCATE",
                    f"Destructive SQL statement: '{raw_sql[:80]}'",
                )

            # Check DELETE statement
            if isinstance(expression, exp.Delete) or expression.key == "delete":
                where = expression.find(exp.Where)
                if where is None:
                    return (
                        False,
                        "DESTRUCTIVE_SQL_UNCONSTRAINED_DELETE",
                        f"Unconstrained DELETE statement without WHERE clause: '{raw_sql[:80]}'",
                    )
                where_sql = where.sql().upper().replace(" ", "")
                if where_sql in (
                    "WHERE1=1",
                    "WHERE'A'='A'",
                    "WHERETRUE",
                    "WHERE1",
                    "WHERE(1=1)",
                    "WHERE(TRUE)",
                ):
                    return (
                        False,
                        "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
                        f"Tautology DELETE statement: '{raw_sql[:80]}'",
                    )
                for eq in where.find_all(exp.EQ):
                    if eq.this.sql().strip() == eq.expression.sql().strip():
                        return (
                            False,
                            "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
                            f"Tautology DELETE condition '{eq.sql()}': '{raw_sql[:80]}'",
                        )

            # Check UPDATE statement
            if isinstance(expression, exp.Update) or expression.key == "update":
                where = expression.find(exp.Where)
                if where is None:
                    return (
                        False,
                        "DESTRUCTIVE_SQL_UNCONSTRAINED_UPDATE",
                        f"Unconstrained UPDATE statement without WHERE clause: '{raw_sql[:80]}'",
                    )
                where_sql = where.sql().upper().replace(" ", "")
                if where_sql in (
                    "WHERE1=1",
                    "WHERE'A'='A'",
                    "WHERETRUE",
                    "WHERE1",
                    "WHERE(1=1)",
                    "WHERE(TRUE)",
                ):
                    return (
                        False,
                        "DESTRUCTIVE_SQL_TAUTOLOGY_UPDATE",
                        f"Tautology UPDATE statement: '{raw_sql[:80]}'",
                    )
                for eq in where.find_all(exp.EQ):
                    if eq.this.sql().strip() == eq.expression.sql().strip():
                        return (
                            False,
                            "DESTRUCTIVE_SQL_TAUTOLOGY_UPDATE",
                            f"Tautology UPDATE condition '{eq.sql()}': '{raw_sql[:80]}'",
                        )

            # Check ALTER TABLE ... DROP COLUMN
            if isinstance(expression, exp.Alter):
                if expression.find(exp.Drop):
                    return (
                        False,
                        "DESTRUCTIVE_SQL_DROP_COLUMN",
                        f"Destructive ALTER TABLE DROP: '{raw_sql[:80]}'",
                    )

    # 2. Fallback Regex Parsing (Catches syntax evasions, comments like DROP/**/TABLE)
    clean_sql = re.sub(r"/\*.*?\*/", " ", raw_sql, flags=re.DOTALL)
    clean_sql = re.sub(r"--[^\n]*", " ", clean_sql)
    clean_sql = re.sub(r"\s+", " ", clean_sql).strip()

    if re.search(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW)\b", clean_sql, re.IGNORECASE):
        return (
            False,
            "DESTRUCTIVE_SQL_DROP",
            f"Destructive SQL statement: '{raw_sql[:80]}'",
        )

    if re.search(r"\bTRUNCATE\s+(TABLE\s+)?", clean_sql, re.IGNORECASE):
        return (
            False,
            "DESTRUCTIVE_SQL_TRUNCATE",
            f"Destructive SQL statement: '{raw_sql[:80]}'",
        )

    if re.search(
        r"\bDELETE\s+FROM\s+[`\"'\w\.]+\s+WHERE\s+(?:1\s*=\s*1|'[^']+'\s*=\s*'[^']+'|true)(?:\b|;|--|\s|$)",
        clean_sql,
        re.IGNORECASE,
    ):
        return (
            False,
            "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
            f"Destructive SQL statement: '{raw_sql[:80]}'",
        )

    if re.search(r"\bDELETE\s+FROM\s+[`\"'\w\.]+\s*(;|--|$)", clean_sql, re.IGNORECASE):
        return (
            False,
            "DESTRUCTIVE_SQL_UNCONSTRAINED_DELETE",
            f"Destructive SQL statement: '{raw_sql[:80]}'",
        )

    return True, None, None
