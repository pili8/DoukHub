# Table Browse Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace table browsing text filters with per-field selectable facets, show selected filters and record counts, and polish the account-works local-directory button.

**Architecture:** Move filter validation and SQL construction into a small backend module, add a facet-count API, and let the existing table page render one multi-select menu per visible field. The page persists search, sort, and filters per table in `localStorage`. Facet counts are computed after the global search and all other field filters, while ignoring the currently opened field so users can adjust its own selection.

**Tech Stack:** FastAPI, SQLite, vanilla JavaScript, AG Grid, Pytest, localStorage.

## Global Constraints

- All visible table fields use selection filters; remove the existing general text filter controls.
- One field is OR semantics: selecting several values keeps records matching any selected value.
- Different fields are AND semantics.
- Primary count means records matching global search plus every other field filter; the opened field's own selection is ignored for its option counts.
- Secondary count means the same value in the current global search before any field filters. Display it as muted `(n)` only when it differs from the primary count.
- Candidate search narrows option values first; counts then use that searched population.
- Sort candidates by primary count, descending, then by label. Return at most 100 options and report the total matched option count.
- Numeric and date fields use automatic distribution-based buckets, not fixed buckets. Numeric data with more than 20 distinct values targets up to 10 fine range buckets. Dates prefer days, then months, and only fall back to years for long spans.
- Global search, sort, and filters are remembered separately for each table after refresh.
- Show selected filters as removable chips above the table and mark filtered column headers.
- Only the account-works modal's "打开目录" icon changes in this plan.
- Do not implement other unrelated pending features in this plan.

---

### Task 1: Backend multi-field filter model

**Files:**
- Create: `app/core/table_filters.py`
- Modify: `app/core/database.py:1228-1286`
- Modify: `app/main.py:2031-2061`
- Modify: `app/main.py:2114-2143`
- Test: `tests/test_database_generic.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: existing `Database.query_table`, `Database.VALID_TABLES`, and SQLite schema dictionaries.
- Produces: `normalize_filters(raw, schema)`, `filter_kind(field, schema)`, `build_where(filters, columns)`, and the filter shape `{ "field": str, "kind": "text" | "tag" | "number" | "date", "values"?: list[str], "buckets"?: list[dict] }`.

- [ ] **Step 1: Write failing filter-model tests**

Add these tests to the end of `tests/test_database_generic.py`:

```python
# ========== selectable table filters ==========

def test_normalize_filters_accepts_multi_select():
    schema = db().get_table_schema("cookie_cache")
    raw = json.dumps([
        {"field": "备注", "kind": "text", "values": ["测试", "测试", ""]},
        {"field": "Cookie", "kind": "text", "values": ["abc"]},
    ])
    filters = normalize_filters(raw, schema)
    assert filters[0] == {"field": "备注", "kind": "text", "values": ["测试", ""]}
    assert filters[1] == {"field": "Cookie", "kind": "text", "values": ["abc"]}


def test_normalize_filters_rejects_unknown_field():
    schema = db().get_table_schema("cookie_cache")
    raw = json.dumps([{"field": "不存在", "kind": "text", "values": ["x"]}])
    with pytest.raises(ValueError):
        normalize_filters(raw, schema)


def test_query_table_multi_value_same_field_is_or():
    db_obj = db()
    db_obj.insert_cookie({"record_id": "c1", "Cookie": "aaa", "备注": "测试"})
    db_obj.insert_cookie({"record_id": "c2", "Cookie": "bbb", "备注": "其他"})
    db_obj.insert_cookie({"record_id": "c3", "Cookie": "ccc", "备注": "第三"})
    filters = [{"field": "备注", "kind": "text", "values": ["测试", "第三"]}]
    result = db_obj.query_table("cookie_cache", filters=filters)
    assert result["total"] == 2
    assert {r["record_id"] for r in result["records"]} == {"c1", "c3"}


def test_query_table_different_fields_are_and():
    db_obj = db()
    db_obj.insert_cookie({"record_id": "c1", "Cookie": "aaa", "备注": "测试"})
    db_obj.insert_cookie({"record_id": "c2", "Cookie": "bbb", "备注": "测试"})
    filters = [
        {"field": "Cookie", "kind": "text", "values": ["aaa"]},
        {"field": "备注", "kind": "text", "values": ["测试"]},
    ]
    result = db_obj.query_table("cookie_cache", filters=filters)
    assert [r["record_id"] for r in result["records"]] == ["c1"]


def test_query_table_empty_bucket_selects_null_and_blank():
    db_obj = db()
    db_obj.insert_cookie({"record_id": "c1", "Cookie": "aaa", "备注": "x"})
    db_obj.insert_cookie({"record_id": "c2", "Cookie": "bbb", "备注": ""})
    filters = [{"field": "备注", "kind": "text", "values": [""]}]
    result = db_obj.query_table("cookie_cache", filters=filters)
    assert {r["record_id"] for r in result["records"]} == {"c2"}
```

Add `json` to the imports at the top of the file:

```python
import json
```

Add this import after the existing `Database` import:

```python
from app.core.table_filters import normalize_filters
```

Run:

```bash
venv\Scripts\python.exe -m pytest tests/test_database_generic.py -q
```

Expected: the five new tests fail because `app.core.table_filters` and `query_table(filters=...)` do not exist.

- [ ] **Step 2: Create the filter module**

Create `app/core/table_filters.py`:

```python
"""Validation and SQL construction for table-browser selections."""
from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_FILTER_FIELDS = 10
MAX_FILTER_VALUES = 100


def filter_kind(field: str, schema: list[dict]) -> str:
    if field == "标签":
        return "tag"
    definition = next((item for item in schema if item["name"] == field), None)
    db_type = ((definition or {}).get("type") or "").upper()
    if any(marker in db_type for marker in ("INT", "REAL", "NUM", "FLOAT", "DOUBLE")):
        return "number"
    if "DATE" in db_type or "TIME" in db_type or ("时间" in field or "日期" in field):
        return "date"
    return "text"


def _clean_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("values must be a list")
    values: list[str] = []
    for value in raw[:MAX_FILTER_VALUES]:
        value = "" if value is None else str(value)
        if value not in values:
            values.append(value)
    return values


def _clean_buckets(raw: Any, kind: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("buckets must be a list")
    result: list[dict] = []
    for item in raw[:MAX_FILTER_VALUES]:
        if not isinstance(item, dict):
            raise ValueError("bucket must be an object")
        if kind == "number":
            low = item.get("min")
            high = item.get("max")
            if low is None and high is None:
                continue
            bucket: dict[str, Any] = {"kind": "number"}
            if low is not None:
                bucket["min"] = float(low)
            if high is not None:
                bucket["max"] = float(high)
            if "min" in bucket and "max" in bucket and bucket["min"] > bucket["max"]:
                raise ValueError("invalid numeric bucket")
            if item.get("label") is not None:
                bucket["label"] = str(item["label"])
            result.append(bucket)
        else:
            unit = str(item.get("unit", ""))
            value = str(item.get("value", ""))
            if unit not in ("day", "month", "year") or not re.match(r"^\d{4}(-\d{2})?(-\d{2})?$", value):
                continue
            if unit == "day" and len(value) != 10:
                continue
            if unit == "month" and len(value) != 7:
                continue
            if unit == "year" and len(value) != 4:
                continue
            bucket = {"kind": "date", "unit": unit, "value": value}
            if item.get("label") is not None:
                bucket["label"] = str(item["label"])
            result.append(bucket)
    return result


def normalize_filters(raw: str | bytes | list[Any] | None, schema: list[dict]) -> list[dict]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("filters is not valid JSON") from exc
    if not isinstance(raw, list):
        raise ValueError("filters must be a JSON array")
    names = {item["name"] for item in schema}
    result: list[dict] = []
    seen: set[str] = set()
    for item in raw[:MAX_FILTER_FIELDS]:
        if not isinstance(item, dict):
            raise ValueError("filter must be an object")
        field = str(item.get("field", "")).strip()
        if field not in names:
            raise ValueError(f"unknown filter field: {field}")
        if field in seen:
            continue
        seen.add(field)
        kind = str(item.get("kind") or filter_kind(field, schema))
        if kind not in ("text", "tag", "number", "date"):
            raise ValueError(f"unknown filter kind: {kind}")
        normalized: dict[str, Any] = {"field": field, "kind": kind}
        if kind in ("text", "tag"):
            values = _clean_values(item.get("values"))
            if not values:
                continue
            normalized["values"] = values
        else:
            buckets = _clean_buckets(item.get("buckets"), kind)
            if not buckets:
                continue
            normalized["buckets"] = buckets
        result.append(normalized)
    return result


def _empty_condition(column: str) -> tuple[str, list[Any]]:
    return f'("{column}" IS NULL OR TRIM(CAST("{column}" AS TEXT)) = "")', []


def _tag_condition(column: str, value: str) -> tuple[str, list[Any]]:
    sql = (
        f'(TRIM(CAST("{column}" AS TEXT)) = ? '
        f'OR EXISTS (SELECT 1 FROM json_each(CASE '
        f'WHEN json_valid(CAST("{column}" AS TEXT)) THEN CAST("{column}" AS TEXT) '
        f'ELSE json_array(CAST("{column}" AS TEXT)) END) je '
        f'WHERE TRIM(je.value) = ?) '
        f"OR instr(',' || replace(CAST(\"{column}\" AS TEXT), ' ', '') || ',', ',' || ? || ',') > 0)"
    )
    return sql, [value, value, value]


def _condition_for_filter(filter_item: dict) -> tuple[str, list[Any]]:
    column = filter_item["field"]
    kind = filter_item["kind"]
    or_parts: list[str] = []
    params: list[Any] = []

    if kind in ("text", "tag"):
        for value in filter_item.get("values", []):
            if value == "":
                sql, sql_params = _empty_condition(column)
            elif kind == "tag":
                sql, sql_params = _tag_condition(column, value)
            else:
                sql, sql_params = f'(TRIM(CAST("{column}" AS TEXT)) = ?)', [value]
            or_parts.append(sql)
            params.extend(sql_params)
    elif kind == "number":
        for bucket in filter_item.get("buckets", []):
            low = bucket.get("min")
            high = bucket.get("max")
            if low is not None and high is not None:
                sql = f'(CAST("{column}" AS REAL) >= ? AND CAST("{column}" AS REAL) <= ?)'
                sql_params: list[Any] = [low, high]
            elif low is not None:
                sql = f'CAST("{column}" AS REAL) >= ?'
                sql_params = [low]
            else:
                sql = f'CAST("{column}" AS REAL) <= ?'
                sql_params = [high]
            or_parts.append(sql)
            params.extend(sql_params)
    else:
        for bucket in filter_item.get("buckets", []):
            unit = bucket["unit"]
            value = bucket["value"]
            if unit == "day":
                sql = f'CAST("{column}" AS TEXT) = ?'
            else:
                sql = f'CAST("{column}" AS TEXT) LIKE ?'
                value = value + "%"
            or_parts.append(sql)
            params.append(value)

    if not or_parts:
        return "", []
    return "(" + " OR ".join(or_parts) + ")", params


def build_where(filters: list[dict], columns: list[str]) -> tuple[str, list[Any]]:
    valid = [item for item in filters if item.get("field") in columns]
    if not valid:
        return "", []
    parts: list[str] = []
    params: list[Any] = []
    for item in valid:
        sql, sql_params = _condition_for_filter(item)
        if sql:
            parts.append(sql)
            params.extend(sql_params)
    if not parts:
        return "", []
    return "(" + " AND ".join(parts) + ")", params
```

- [ ] **Step 3: Wire filters into `query_table`**

In `app/core/database.py`, add this import near the top:

```python
from app.core.table_filters import build_where, normalize_filters
```

Replace the `query_table` signature and WHERE block with:

```python
    def query_table(
        self,
        table: str,
        limit: int = 100,
        offset: int = 0,
        search: str = "",
        sort_field: Optional[str] = None,
        sort_order: str = "desc",
        filter_field: Optional[str] = None,
        filter_value: Optional[str] = None,
        filter_op: Optional[str] = None,
        filters: str | bytes | list[Any] | None = None,
    ) -> dict:
        """通用表查询，支持搜索、排序、多字段选择筛选、分页。"""
        with self._connect() as conn:
            schema = self.get_table_schema(table)
            cols = [item["name"] for item in schema]

            params: list[Any] = []
            where_parts: list[str] = []
            if search:
                where_parts.append("(" + " OR ".join([f'CAST("{c}" AS TEXT) LIKE ?' for c in cols]) + ")")
                params += [f"%{search}%"] * len(cols)

            if filters is not None:
                normalized_filters = normalize_filters(filters, schema)
            elif filter_field and filter_field in cols and filter_value is not None:
                if filter_op == "contains":
                    normalized_filters = [{"field": filter_field, "kind": "tag" if filter_field == "标签" else "text", "values": [str(filter_value)]}]
                elif filter_op == "equals":
                    normalized_filters = [{"field": filter_field, "kind": "text", "values": [str(filter_value)]}]
                else:
                    normalized_filters = []
            else:
                normalized_filters = []

            filter_where, filter_params = build_where(normalized_filters, cols)
            if filter_where:
                where_parts.append(filter_where)
                params.extend(filter_params)

            where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

            order = " ORDER BY rowid DESC"
            if sort_field and sort_field in cols:
                direction = "ASC" if sort_order.lower() == "asc" else "DESC"
                order = f' ORDER BY "{sort_field}" {direction}'

            total = conn.execute(
                f"SELECT COUNT(*) FROM {table}{where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {table}{where}{order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {
                "records": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
```

Keep the rest of the original method body out; this replacement covers the whole method through its `return`.

- [ ] **Step 4: Add API transport and export compatibility**

Replace `api_database_table` with:

```python
@app.get("/api/database/table/{table_name}")
async def api_database_table(
    table_name: str,
    limit: int = 100,
    offset: int = 0,
    search: str = "",
    sort_field: str = "",
    sort_order: str = "desc",
    filter_field: str = "",
    filter_value: str = "",
    filter_op: str = "",
    filters: str = "",
):
    """获取表数据，支持分页、搜索、排序、多字段选择筛选。"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    try:
        return db.query_table(
            table_name,
            limit=limit,
            offset=offset,
            search=search,
            sort_field=sort_field or None,
            sort_order=sort_order,
            filter_field=filter_field or None,
            filter_value=filter_value or None,
            filter_op=filter_op or None,
            filters=filters or None,
        )
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
```

In `api_database_export_csv`, add `filters: str = ""` after `search: str = ""`, and replace the query call with:

```python
    try:
        result = db.query_table(
            table_name,
            limit=10**9,
            offset=0,
            search=search,
            filters=filters or None,
        )
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
    records = result["records"]
```

The frontend export URL will later send the same persisted filters.

- [ ] **Step 5: Add API transport tests**

Add to `tests/test_api.py` inside `TestAPIEndpoints`:

```python
    def test_api_table_multi_select_filters(self, app_env, tmp_path):
        client, *_ = app_env
        import app.main as app_main
        from app.core.database import Database

        orig_db = app_main.database
        try:
            app_main.database = Database(tmp_path / "test.db")
            db = app_main.database
            db.insert_cookie({"record_id": "c1", "Cookie": "aaa", "备注": "测试"})
            db.insert_cookie({"record_id": "c2", "Cookie": "bbb", "备注": "其他"})
            filters = [{"field": "备注", "kind": "text", "values": ["测试", "其他"]}]
            r = client.get(
                "/api/database/table/cookie_cache",
                params={"filters": json.dumps(filters)},
            )
            assert r.status_code == 200
            assert r.json()["total"] == 2
        finally:
            app_main.database = orig_db

    def test_api_table_invalid_filters(self, app_env, tmp_path):
        client, *_ = app_env
        import app.main as app_main
        from app.core.database import Database

        orig_db = app_main.database
        try:
            app_main.database = Database(tmp_path / "test.db")
            r = client.get(
                "/api/database/table/cookie_cache",
                params={"filters": "[{\"field\":\"missing\",\"kind\":\"text\",\"values\":[\"x\"]}]"},
            )
            assert r.status_code == 400
        finally:
            app_main.database = orig_db
```

- [ ] **Step 6: Run backend filter tests**

Run:

```bash
venv\Scripts\python.exe -m pytest tests/test_database_generic.py tests/test_api.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/core/table_filters.py app/core/database.py app/main.py tests/test_database_generic.py tests/test_api.py
git commit -m "feat: add multi-field table filters"
```

---

### Task 2: Automatic facet buckets and counts

**Files:**
- Modify: `app/core/table_filters.py`
- Modify: `app/core/database.py`
- Modify: `app/main.py:2064-2070`
- Test: `tests/test_database_generic.py`

**Interfaces:**
- Consumes: Task 1 filter shapes and `filter_kind`.
- Produces: `Database.get_table_facets(table, field, search, filters, option_search)` returning `{kind, options, matched, filtered_total}`. Each option is `{key, kind, label, count, all_count}` plus `min/max` for numeric buckets or `unit/value` for date buckets.

- [ ] **Step 1: Write failing facet tests**

Append to `tests/test_database_generic.py`:

```python
def test_get_table_facets_text_counts_ignore_own_filter():
    db_obj = db()
    db_obj.insert_cookie({"record_id": "c1", "Cookie": "abc", "备注": "测试"})
    db_obj.insert_cookie({"record_id": "c2", "Cookie": "abd", "备注": "测试"})
    db_obj.insert_cookie({"record_id": "c3", "Cookie": "abd", "备注": "其他"})
    filters = [{"field": "备注", "kind": "text", "values": ["测试"]}]
    result = db_obj.get_table_facets(
        "cookie_cache", "备注", search="", filters=filters
    )
    assert result["kind"] == "text"
    by_key = {item["key"]: item for item in result["options"]}
    assert by_key["测试"]["count"] == 2
    assert by_key["测试"]["all_count"] == 2
    assert by_key["其他"]["count"] == 1
    assert by_key["其他"]["all_count"] == 1


def test_get_table_facets_option_search_narrows_values():
    db_obj = db()
    db_obj.insert_cookie({"record_id": "c1", "Cookie": "a", "备注": "apple"})
    db_obj.insert_cookie({"record_id": "c2", "Cookie": "b", "备注": "banana"})
    result = db_obj.get_table_facets(
        "cookie_cache", "备注", search="", filters=[], option_search="app"
    )
    assert [item["label"] for item in result["options"]] == ["apple"]


def test_get_table_facets_numeric_exact_values():
    db_obj = db()
    for index, fans in enumerate([1, 5, 10]):
        db_obj.insert_account({"record_id": f"a{index}", "sec_user_id": f"s{index}", "粉丝数": fans})
    result = db_obj.get_table_facets("account_cache", "粉丝数", search="", filters=[])
    assert result["kind"] == "number"
    counts = {item["label"]: item["count"] for item in result["options"]}
    assert counts == {"1": 1, "5": 1, "10": 1}


def test_get_table_facets_numeric_auto_buckets_are_fine():
    db_obj = db()
    for index in range(40):
        db_obj.insert_account({
            "record_id": f"fine{index}",
            "sec_user_id": f"s{index}",
            "粉丝数": (index + 1) * 10,
        })
    result = db_obj.get_table_facets("account_cache", "粉丝数", search="", filters=[])
    ranges = [item for item in result["options"] if item["kind"] == "number"]
    assert 2 <= len(ranges) <= 10
    assert ranges[0]["min"] == 10
    assert ranges[-1]["max"] == 400
```

Run:

```bash
venv\Scripts\python.exe -m pytest tests/test_database_generic.py -q
```

Expected: the three new tests fail because `get_table_facets` does not exist.

- [ ] **Step 2: Add facet helpers**

Append this code to `app/core/table_filters.py`:

```python
FACET_LIMIT = 100
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def _base_where(search: str, columns: list[str]) -> tuple[str, list[Any]]:
    if not search:
        return "", []
    sql = "(" + " OR ".join([f'CAST("{c}" AS TEXT) LIKE ?' for c in columns]) + ")"
    return sql, [f"%{search}%"] * len(columns)


def _combine_where(parts: list[tuple[str, list[Any]]]) -> tuple[str, list[Any]]:
    sql_parts: list[str] = []
    params: list[Any] = []
    for sql, sql_params in parts:
        if sql:
            sql_parts.append(sql)
            params.extend(sql_params)
    if not sql_parts:
        return "", []
    return " WHERE " + " AND ".join(sql_parts), params


def _raw_values(conn: Any, table: str, field: str, where: str, params: list[Any]) -> list[Any]:
    rows = conn.execute(f'SELECT "{field}" FROM {table}{where}', params).fetchall()
    return [row[0] for row in rows]


def _expand_raw_value(kind: str, raw: Any) -> list[str]:
    if raw is None or str(raw) == "":
        return [""]
    if kind != "tag":
        return [str(raw)]
    value = str(raw)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        parsed = value
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item) != ""]
    if "," in value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def _parse_date(raw: Any):
    value = str(raw or "").strip()
    if re.fullmatch(r"\d{13}", value):
        return datetime.fromtimestamp(int(value) / 1000).date()
    if re.fullmatch(r"\d{10}", value):
        return datetime.fromtimestamp(int(value)).date()
    match = _DATE_RE.match(value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _quantile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] * (high - position) + sorted_values[high] * (position - low)


def _number_label(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _number_bucket_options(
    primary: Counter, secondary: Counter
) -> tuple[list[dict], dict[Any, dict]]:
    numeric = {float(value): count for value, count in primary.items() if value != "" and value is not None}
    distinct = sorted(numeric)
    if not distinct:
        return [], {}
    if len(distinct) <= 20:
        ranges = [{"min": value, "max": value} for value in distinct]
    else:
        boundaries = [
            _quantile(distinct, p)
            for p in [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        ]
        ranges = []
        for index in range(len(boundaries) - 1):
            low = math.floor(boundaries[index]) if float(boundaries[index]).is_integer() is False else boundaries[index]
            high = math.ceil(boundaries[index + 1]) if float(boundaries[index + 1]).is_integer() is False else boundaries[index + 1]
            if index == 0:
                low = min(low, distinct[0])
            if index == len(boundaries) - 2:
                high = max(high, distinct[-1])
            if high < low:
                high = low
            current = {"min": low, "max": high}
            if not ranges or ranges[-1]["min"] != low or ranges[-1]["max"] != high:
                ranges.append(current)

    buckets: list[dict] = []
    mapping: dict[Any, dict] = {}
    for raw_value, count in primary.items():
        if raw_value == "" or raw_value is None:
            continue
        value = float(raw_value)
        selected = ranges[-1]
        for bucket in ranges:
            low = bucket["min"]
            high = bucket["max"]
            if value >= low and (value < high or bucket is ranges[-1]):
                selected = bucket
                break
        primary_count = numeric[value]
        all_count = sum(count2 for raw2, count2 in secondary.items() if raw2 not in ("", None) and float(raw2) == value)
        key = (selected["min"], selected["max"])
        found = next((item for item in buckets if item["min"] == selected["min"] and item["max"] == selected["max"]), None)
        if found is None:
            label = selected.get("label")
            if not label:
                low_label = _number_label(selected["min"])
                high_label = _number_label(selected["max"])
                label = low_label if low_label == high_label else f"{low_label} - {high_label}"
            found = {
                "key": f"{selected['min']}:{selected['max']}",
                "kind": "number",
                "min": selected["min"],
                "max": selected["max"],
                "label": label,
                "count": 0,
                "all_count": 0,
            }
            buckets.append(found)
        found["count"] += count
        found["all_count"] += all_count
        mapping[raw_value] = found
    buckets.sort(key=lambda item: (item["min"], item["max"]))
    return buckets, mapping


def _date_bucket_options(
    primary: Counter, secondary: Counter
) -> tuple[list[dict], dict[Any, dict]]:
    parsed_primary = {value: _parse_date(value) for value in primary if value not in ("", None)}
    parsed_values = sorted({value for value in parsed_primary.values() if value is not None})
    if not parsed_values:
        return [], {}
    distinct_days = len(parsed_values)
    span = (parsed_values[-1] - parsed_values[0]).days
    if distinct_days <= 60 or span <= 180:
        unit = "day"
    elif len({value.strftime("%Y-%m") for value in parsed_values}) <= 60:
        unit = "month"
    else:
        unit = "year"

    def date_key(value):
        parsed = _parse_date(value)
        if parsed is None:
            return None
        if unit == "day":
            return parsed.isoformat()
        if unit == "month":
            return parsed.strftime("%Y-%m")
        return parsed.strftime("%Y")

    buckets: list[dict] = []
    mapping: dict[Any, dict] = {}
    all_keys = {date_key(value) for value in secondary if value not in ("", None)}
    for key in sorted(item for item in all_keys if item is not None):
        buckets.append({
            "key": f"{unit}:{key}",
            "kind": "date",
            "unit": unit,
            "value": key,
            "label": key,
            "count": 0,
            "all_count": 0,
        })
    by_key = {item["value"]: item for item in buckets}
    for raw_value, count in primary.items():
        key = date_key(raw_value)
        if key is None:
            continue
        bucket = by_key[key]
        bucket["count"] += count
        bucket["all_count"] += sum(count2 for raw2, count2 in secondary.items() if date_key(raw2) == key)
        mapping[raw_value] = bucket
    return buckets, mapping


def build_table_facets(
    conn: Any,
    table: str,
    field: str,
    schema: list[dict],
    columns: list[str],
    search: str,
    filters: list[dict],
    option_search: str,
) -> dict:
    if field not in columns:
        raise ValueError(f"unknown facet field: {field}")
    kind = filter_kind(field, schema)
    search_sql, search_params = _base_where(search, columns)
    search_only = _combine_where([(search_sql, search_params)])
    other_filters = [item for item in filters if item.get("field") != field]
    other_sql, other_params = build_where(other_filters, columns)
    base = _combine_where([(search_sql, search_params), (other_sql, other_params)])

    primary_rows = _raw_values(conn, table, field, base[0], base[1])
    secondary_rows = _raw_values(conn, table, field, search_only[0], search_only[1])
    primary: Counter = Counter()
    secondary: Counter = Counter()
    for raw in primary_rows:
        for value in _expand_raw_value(kind, raw):
            primary[value] += 1
    for raw in secondary_rows:
        for value in _expand_raw_value(kind, raw):
            secondary[value] += 1

    options: list[dict] = []
    raw_mapping: dict[Any, dict] = {}
    if kind in ("text", "tag"):
        for value in sorted(set(primary) | set(secondary)):
            option_kind = "empty" if value == "" else kind
            options.append({
                "key": "__empty__" if value == "" else value,
                "kind": option_kind,
                "label": "（空）" if value == "" else value,
                "count": primary.get(value, 0),
                "all_count": secondary.get(value, 0),
            })
    elif kind == "number":
        if primary.get("", 0) or primary.get(None, 0):
            options.append({
                "key": "__empty__", "kind": "empty", "label": "（空）",
                "count": primary.get("", 0) + primary.get(None, 0),
                "all_count": secondary.get("", 0) + secondary.get(None, 0),
            })
        buckets, raw_mapping = _number_bucket_options(primary, secondary)
        options.extend(buckets)
    else:
        if primary.get("", 0) or primary.get(None, 0):
            options.append({
                "key": "__empty__", "kind": "empty", "label": "（空）",
                "count": primary.get("", 0) + primary.get(None, 0),
                "all_count": secondary.get("", 0) + secondary.get(None, 0),
            })
        buckets, raw_mapping = _date_bucket_options(primary, secondary)
        options.extend(buckets)

    query = (option_search or "").strip().casefold()
    if query:
        options = [item for item in options if query in item["label"].casefold()]
    matched = len(options)
    options.sort(key=lambda item: (-item["count"], item["label"].casefold()))
    filtered_total = conn.execute(f"SELECT COUNT(*) FROM {table}{base[0]}", base[1]).fetchone()[0]
    return {
        "kind": kind,
        "options": options[:FACET_LIMIT],
        "matched": matched,
        "filtered_total": filtered_total,
    }
```

Add `Counter` to the module imports:

```python
from collections import Counter
```

- [ ] **Step 3: Expose the database method and API**

Add this method directly after `query_table` in `app/core/database.py`:

```python
    def get_table_facets(
        self,
        table: str,
        field: str,
        search: str = "",
        filters: str | bytes | list[Any] | None = None,
        option_search: str = "",
    ) -> dict:
        """获取某个字段的可选值/分档和条目数。"""
        with self._connect() as conn:
            schema = self.get_table_schema(table)
            columns = [item["name"] for item in schema]
            normalized_filters = normalize_filters(filters, schema)
            return build_table_facets(
                conn, table, field, schema, columns, search,
                normalized_filters, option_search,
            )
```

Update the import to:

```python
from app.core.table_filters import build_table_facets, build_where, normalize_filters
```

Add this route after `api_database_table_schema`:

```python
@app.get("/api/database/table/{table_name}/facets")
async def api_database_table_facets(
    table_name: str,
    field: str,
    search: str = "",
    filters: str = "",
    option_search: str = "",
):
    """获取字段筛选候选值和条目数。"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    try:
        return db.get_table_facets(
            table_name,
            field=field,
            search=search,
            filters=filters or None,
            option_search=option_search,
        )
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)
```

- [ ] **Step 4: Run facet tests**

Run:

```bash
venv\Scripts\python.exe -m pytest tests/test_database_generic.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/core/table_filters.py app/core/database.py tests/test_database_generic.py
git commit -m "feat: add table facet buckets and counts"
```

---

### Task 3: Selection menu, chips, and persistence

**Files:**
- Modify: `app/templates/table.html:9-82`
- Modify: `app/templates/table.html:991-1008`
- Modify: `app/templates/table.html:1191-1259`
- Modify: `app/templates/table.html:1269-1417`
- Modify: `app/templates/table.html:1787-1929`
- Modify: `app/templates/table.html:2029-2048`

**Interfaces:**
- Consumes: `/api/database/table/{table}/facets`, multi-filter `filters` JSON, and facet option shapes from Tasks 1-2.
- Produces: frontend state `currentFilters`; persistence key `doukhub-table-browse-settings-v1`; global functions `applyFieldSelection(field, option, checked)`, `clearFieldFilter(field)`, `clearAllFilters()`, and `onFieldOptionSearchInput(field, value)`.

- [ ] **Step 1: Add chips container and filter CSS**

Insert this immediately before `<div id="table-main" ...>`:

```html
<div id="tbl-filter-chips" class="tbl-filter-chips" aria-live="polite"></div>
```

Add this CSS inside the existing table style block after the `.tbl-action-btn` rules:

```css
.tbl-filter-chips {
    display: none;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
    padding: 0 20px 8px;
}
.tbl-filter-chips.has-filters { display: flex; }
.tbl-filter-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    max-width: 260px;
    height: 26px;
    padding: 0 8px;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-full);
    background: var(--surface-overlay);
    color: var(--text-secondary);
    font-size: 11px;
}
.tbl-filter-chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tbl-filter-chip button {
    display: inline-flex; border: none; background: transparent;
    color: var(--text-muted); cursor: pointer; padding: 1px;
}
.tbl-filter-chip button:hover { color: var(--danger); }
.tbl-filter-clear {
    height: 26px; border: none; background: transparent;
    color: var(--accent); font-size: 11px; cursor: pointer;
}
.tbl-filter-menu { width: 280px; max-height: min(70vh, 520px); overflow: hidden; }
.tbl-filter-search { position: sticky; top: 0; background: var(--surface-overlay); padding-bottom: 6px; }
.tbl-filter-options { max-height: 340px; overflow-y: auto; }
.tbl-filter-option {
    display: grid; grid-template-columns: 16px minmax(0, 1fr) auto;
    gap: 7px; align-items: center; padding: 6px 8px; border-radius: var(--radius-xs);
}
.tbl-filter-option:hover { background: var(--menu-hover); }
.tbl-filter-option input { width: 16px; height: 16px; accent-color: var(--accent); }
.tbl-filter-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tbl-filter-count { color: var(--text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
.tbl-filter-total { color: var(--text-muted); font-size: 10px; margin-left: 3px; }
.tbl-filter-empty { color: var(--text-muted); font-size: 12px; padding: 14px 8px; text-align: center; }
```

- [ ] **Step 2: Replace single-filter state**

Replace these three declarations:

```javascript
var currentFilterField = '';
var currentFilterOp = '';
var currentFilterValue = '';
```

with:

```javascript
var currentFilters = [];
var facetRequestToken = 0;
const TABLE_SETTINGS_KEY = 'doukhub-table-browse-settings-v1';
```

Update `DoukHeaderComponent._render` from:

```javascript
const hasFilter = currentFilterField === this.field;
```

to:

```javascript
const hasFilter = currentFilters.some(item => item.field === this.field);
```

Change the header button title from `菜单：排序/筛选` to `菜单：排序/选择筛选`.

- [ ] **Step 3: Add persistence helpers**

Add these functions immediately after the state declarations:

```javascript
function loadTableSettings() {
    try { return JSON.parse(localStorage.getItem(TABLE_SETTINGS_KEY) || '{}') || {}; }
    catch (e) { return {}; }
}

function saveTableSettings() {
    try {
        const all = loadTableSettings();
        all[currentTable] = {
            keyword: currentKeyword || '',
            sort: { field: currentSortField || '', order: currentSortOrder || '' },
            filters: currentFilters || []
        };
        localStorage.setItem(TABLE_SETTINGS_KEY, JSON.stringify(all));
    } catch (e) {}
}

function restoreTableSettings(tableName, explicitKeyword) {
    const saved = loadTableSettings()[tableName] || {};
    currentKeyword = explicitKeyword !== undefined && explicitKeyword !== ''
        ? explicitKeyword
        : (saved.keyword || '');
    currentSortField = saved.sort && saved.sort.field ? saved.sort.field : '';
    currentSortOrder = saved.sort && saved.sort.order ? saved.sort.order : '';
    currentFilters = Array.isArray(saved.filters) ? saved.filters : [];
    const searchInput = document.getElementById('search-keyword');
    if (searchInput) searchInput.value = currentKeyword;
    const clearButton = document.getElementById('search-clear-btn');
    if (clearButton) clearButton.style.display = currentKeyword ? 'flex' : 'none';
}
```

- [ ] **Step 4: Replace the old filter menu implementation**

Delete `collectDistinctValues`, the filter controls inside `openColMenu`, `applyColFilter`, and old references to `currentFilterField/currentFilterOp/currentFilterValue`.

Keep sort items and add these functions in their place:

```javascript
function selectedFilter(field) {
    return currentFilters.find(item => item.field === field);
}

function optionMatchesFilter(option, filter) {
    if (!filter) return false;
    if (option.kind === 'empty') {
        return filter.kind === 'text' && (filter.values || []).includes('');
    }
    if (option.kind === 'text' || option.kind === 'tag') {
        return filter.kind === option.kind && (filter.values || []).includes(option.key);
    }
    if (option.kind === 'number') {
        return filter.kind === 'number' && (filter.buckets || []).some(bucket =>
            Number(bucket.min) === Number(option.min) && Number(bucket.max) === Number(option.max));
    }
    if (option.kind === 'date') {
        return filter.kind === 'date' && (filter.buckets || []).some(bucket =>
            bucket.unit === option.unit && bucket.value === option.value);
    }
    return false;
}

function optionToFilterValue(option) {
    if (option.kind === 'empty') return '';
    if (option.kind === 'number') return { min: option.min, max: option.max, label: option.label };
    if (option.kind === 'date') return { unit: option.unit, value: option.value, label: option.label };
    return option.key;
}

function filterValueLabel(filter, value) {
    if (filter.kind === 'number') return value.label || (value.min + ' - ' + value.max);
    if (filter.kind === 'date') return value.label || value.value;
    return value === '' ? '（空）' : String(value);
}

function updateFilterChips() {
    const box = document.getElementById('tbl-filter-chips');
    if (!box) return;
    box.innerHTML = '';
    box.classList.toggle('has-filters', currentFilters.length > 0);
    currentFilters.forEach(filter => {
        const values = filter.kind === 'text' || filter.kind === 'tag' ? filter.values : filter.buckets;
        const labels = values.map(value => filterValueLabel(filter, value));
        const chip = document.createElement('span');
        chip.className = 'tbl-filter-chip';
        chip.innerHTML = '<span title="' + htmlEscape(filter.field + ': ' + labels.join(' / ')) + '">'
            + htmlEscape(filter.field + ': ' + labels.join(' / ')) + '</span>'
            + '<button type="button" title="清除该字段筛选" aria-label="清除该字段筛选"><i class="ph ph-x"></i></button>';
        chip.querySelector('button').addEventListener('click', () => clearFieldFilter(filter.field));
        box.appendChild(chip);
    });
    if (currentFilters.length > 1) {
        const clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'tbl-filter-clear';
        clear.textContent = '清除全部';
        clear.addEventListener('click', clearAllFilters);
        box.appendChild(clear);
    }
}

function setFieldFilter(field, kind, values, buckets) {
    currentFilters = currentFilters.filter(item => item.field !== field);
    const hasValues = kind === 'text' || kind === 'tag' ? values.length > 0 : buckets.length > 0;
    if (hasValues) {
        currentFilters.push(hasValues ? (kind === 'text' || kind === 'tag' ? { field, kind, values } : { field, kind, buckets }) : {});
    }
    saveTableSettings();
    updateFilterChips();
    if (gridApi) gridApi.refreshHeader();
    showTable(currentTable, 1, currentKeyword);
}

window.applyFieldSelection = function(field, encodedOption, checked) {
    const option = JSON.parse(decodeURIComponent(encodedOption));
    const old = selectedFilter(field) || { field, kind: option.kind === 'tag' ? 'tag' : option.kind, values: [], buckets: [] };
    let values = [...(old.values || [])];
    let buckets = [...(old.buckets || [])];
    if (option.kind === 'text' || option.kind === 'tag' || option.kind === 'empty') {
        const value = option.kind === 'empty' ? '' : option.key;
        values = checked ? [...new Set([...values, value])] : values.filter(item => item !== value);
    } else if (option.kind === 'number') {
        const match = item => Number(item.min) === Number(option.min) && Number(item.max) === Number(option.max);
        buckets = checked ? [...buckets, { min: option.min, max: option.max, label: option.label }] : buckets.filter(item => !match(item));
    } else {
        const match = item => item.unit === option.unit && item.value === option.value;
        buckets = checked ? [...buckets, { unit: option.unit, value: option.value, label: option.label }] : buckets.filter(item => !match(item));
    }
    const kind = option.kind === 'tag' ? 'tag' : (old.kind || option.kind);
    setFieldFilter(field, old.kind || kind, values, buckets);
};

window.clearFieldFilter = function(field) {
    setFieldFilter(field, 'text', [], []);
};

window.clearAllFilters = function() {
    currentFilters = [];
    saveTableSettings();
    updateFilterChips();
    if (gridApi) gridApi.refreshHeader();
    showTable(currentTable, 1, currentKeyword);
};
```

Inside `openColMenu`, after the sort items and separator, build this filter area instead of the old controls:

```javascript
        const filterRow = document.createElement('div');
        filterRow.className = 'tbl-filter-menu';
        const searchBox = document.createElement('input');
        searchBox.type = 'text';
        searchBox.placeholder = '搜索候选值…';
        searchBox.className = 'tbl-filter-search';
        searchBox.addEventListener('input', () => loadFieldOptions(field, searchBox.value, optionsBox, statusBox));
        const optionsBox = document.createElement('div');
        optionsBox.className = 'tbl-filter-options';
        const statusBox = document.createElement('div');
        statusBox.className = 'tbl-filter-empty';
        filterRow.appendChild(searchBox);
        filterRow.appendChild(optionsBox);
        filterRow.appendChild(statusBox);
        menu.appendChild(filterRow);
        loadFieldOptions(field, '', optionsBox, statusBox);
```

Also add the facet loader before `openColMenu`:

```javascript
async function loadFieldOptions(field, optionSearch, optionsBox, statusBox) {
    const token = ++facetRequestToken;
    optionsBox.innerHTML = '<div class="tbl-filter-empty">加载中…</div>';
    statusBox.textContent = '';
    try {
        const qs = new URLSearchParams({
            field, search: currentKeyword || '', filters: JSON.stringify(currentFilters),
            option_search: optionSearch || ''
        });
        const data = await apiCall('/api/database/table/' + currentTable + '/facets?' + qs, 'GET');
        if (token !== facetRequestToken) return;
        renderFieldOptions(field, data, optionsBox, statusBox);
    } catch (e) {
        if (token === facetRequestToken) optionsBox.innerHTML = '<div class="tbl-filter-empty">候选值加载失败</div>';
    }
}

function renderFieldOptions(field, data, optionsBox, statusBox) {
    const selected = selectedFilter(field);
    const selectedValues = selected ? (selected.values || selected.buckets || []) : [];
    const selectedOptions = selectedValues.map(value => ({
        key: value === '' ? '__empty__' : (value.min !== undefined ? value.min + ':' + value.max : (value.unit ? value.unit + ':' + value.value : value)),
        kind: selected.kind,
        label: filterValueLabel(selected, value),
        count: -1,
        all_count: -1
    })).filter(option => option.kind !== 'text' && option.kind !== 'tag' ? true : true);
    const options = data.options || [];
    const seen = new Set(options.map(item => item.key));
    const extras = selectedOptions.filter(item => !seen.has(item.key));
    const rows = [...extras, ...options];
    optionsBox.innerHTML = '';
    if (!rows.length) {
        optionsBox.innerHTML = '<div class="tbl-filter-empty">没有候选值</div>';
    } else {
        rows.forEach(option => {
            const checked = option.count === -1 || optionMatchesFilter(option, selected);
            const encoded = encodeURIComponent(JSON.stringify(option));
            const label = document.createElement('label');
            label.className = 'tbl-filter-option';
            const countHtml = option.count < 0 ? '' :
                '<span class="tbl-filter-count">' + option.count +
                (option.all_count >= 0 && option.all_count !== option.count
                    ? '<span class="tbl-filter-total">(' + option.all_count + ')</span>' : '') + '</span>';
            label.innerHTML = '<input type="checkbox" ' + (checked ? 'checked' : '') + '>'
                + '<span class="tbl-filter-label" title="' + htmlEscape(option.label) + '">' + htmlEscape(option.label) + '</span>'
                + countHtml;
            label.querySelector('input').addEventListener('change', event => {
                applyFieldSelection(field, encoded, event.target.checked);
            });
            optionsBox.appendChild(label);
        });
    }
    const hidden = Math.max(0, (data.matched || 0) - options.length);
    statusBox.textContent = hidden > 0 ? '还有 ' + hidden + ' 个候选值，请继续搜索' : '';
}

window.onFieldOptionSearchInput = function(field, value) {
    const menu = colMenuEl;
    if (!menu) return;
    loadFieldOptions(field, value, menu.querySelector('.tbl-filter-options'), menu.querySelector('.tbl-filter-empty:last-child'));
};
```

The inline `searchBox.addEventListener` already calls `loadFieldOptions`, so `onFieldOptionSearchInput` exists only as a stable global fallback for debugging.

- [ ] **Step 5: Send filters, persist search, and restore per table**

In `showTable`, replace the old reset block:

```javascript
        if (typeof currentSortField !== 'undefined' && document.getElementById('table-tabs') &&
            document.getElementById('table-tabs').dataset.lastTable !== tableName) {
            currentSortField = '';
            currentSortOrder = '';
            currentFilterField = '';
            currentFilterOp = '';
            currentFilterValue = '';
        }
        if (document.getElementById('table-tabs')) {
            document.getElementById('table-tabs').dataset.lastTable = tableName;
        }
```

with:

```javascript
        restoreTableSettings(tableName, keyword);
        if (document.getElementById('table-tabs')) {
            document.getElementById('table-tabs').dataset.lastTable = tableName;
        }
```

After `updateBatchToolbar();` at the end of `showTable`, add:

```javascript
        updateFilterChips();
        saveTableSettings();
```

Replace the old single-filter query line with:

```javascript
        if (currentFilters.length) url += '&filters=' + encodeURIComponent(JSON.stringify(currentFilters));
```

In `onSearchInput` and `doSearch`, call `saveTableSettings()` after assigning `currentKeyword` or before data reload. In `clearSearch`, set `currentKeyword = ''`, call `saveTableSettings()`, then reload.

Replace `_presetCondition` with:

```javascript
    function _presetCondition() {
        const rating = currentFilters.find(item => item.field === '等级');
        if (rating && rating.buckets && rating.buckets.length === 1) {
            const min = parseInt(rating.buckets[0].min);
            if (min >= 1 && min <= 5) return { label: '等级 ≥ ' + min + ' 星', data: { rating_min: min } };
        }
        const tags = currentFilters.find(item => item.field === '标签');
        if (tags && tags.values && tags.values.length === 1) {
            return { label: '标签包含「' + tags.values[0] + '」', data: { tags: tags.values[0] } };
        }
        const platform = currentFilters.find(item => item.field === '平台');
        if (platform && platform.values && platform.values.length === 1) {
            const pmap = { '抖音': 'douyin', 'TikTok': 'tiktok', '小红书': 'xhs' };
            const pf = pmap[platform.values[0]];
            if (pf) return { label: '平台 = ' + platform.values[0], data: { platform: pf } };
        }
        return null;
    }
```

- [ ] **Step 6: Update export action**

Find `exportCsv()` and ensure its URL includes the current filters:

```javascript
    let url = '/api/database/table/' + currentTable + '/export?search=' + encodeURIComponent(currentKeyword || '');
    if (currentFilters.length) url += '&filters=' + encodeURIComponent(JSON.stringify(currentFilters));
```

- [ ] **Step 7: Add template smoke checks**

Append to `tests/test_workflow_ui.py` inside the table-page test class:

```python
    def test_table_page_contains_selectable_filter_surface(self, client):
        response = client.get("/database/tables")
        assert response.status_code == 200
        assert 'id="tbl-filter-chips"' in response.text
        assert "loadFieldOptions" in response.text
        assert "currentFilterField" not in response.text
```

If the existing fixture path is `/table`, use that exact path instead of `/database/tables`; do not change the route.

- [ ] **Step 8: Run tests**

Run:

```bash
venv\Scripts\python.exe -m pytest tests/test_database_generic.py tests/test_api.py tests/test_workflow_ui.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add app/templates/table.html tests/test_workflow_ui.py
git commit -m "feat: render selectable table facets"
```

---

### Task 4: Account-works directory button polish

**Files:**
- Modify: `app/static/js/account-works.js:119`

**Interfaces:**
- Consumes: existing Lucide runtime and `AW_open(id, "dir")`.
- Produces: no API or behavior change.

- [ ] **Step 1: Replace the icon**

Replace:

```javascript
+ '<button class="aw-btn" onclick="AW_open(' + w.id + ',\'dir\')"><i data-lucide="folder-open"></i>打开目录</button>'
```

with:

```javascript
+ '<button class="aw-btn" title="打开本地目录" aria-label="打开本地目录" onclick="AW_open(' + w.id + ',\'dir\')"><i data-lucide="folder-symlink" style="width:14px;height:14px;stroke-width:1.75;"></i>打开目录</button>'
```

- [ ] **Step 2: Verify icon availability**

Run:

```bash
rg -o "FolderSymlink" app/static/js/lucide.min.js
```

Expected: one or more matches.

- [ ] **Step 3: Run focused tests**

Run:

```bash
venv\Scripts\python.exe -m pytest tests/test_workflow_ui.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add app/static/js/account-works.js
git commit -m "style: polish account works directory button"
```

---

### Task 5: End-to-end verification

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: verified user-facing behavior.

- [ ] **Step 1: Run full relevant test suite**

Run:

```bash
venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run application**

If no server is already running:

```bash
venv\Scripts\python.exe main.py
```

Open `http://127.0.0.1:2999/table`.

- [ ] **Step 3: Verify filter interactions**

Check all of these:

1. Every visible column menu has a selectable candidate list; no contains/equals text input remains.
2. Selecting multiple options in one field widens results; selecting conditions in two fields narrows results.
3. Numeric data with more than 20 distinct values shows up to 10 automatic range buckets; dates use the finest practical day/month/year level.
4. Primary counts change with search and other filters; the opened field's own selection does not suppress its zero-count selected option.
5. Muted secondary counts appear only when they differ from primary counts.
6. Candidate search limits the list to 100 and shows how many are hidden.
7. Selected filters appear as chips, are individually removable, and column headers stay marked.
8. Refreshing keeps search, sort, and filters for the same table; switching tables restores each table's own state.
9. Export respects the active filters.
10. The account-works modal shows the new folder icon and still opens the correct directory.

- [ ] **Step 4: Commit verification-only fixes, if any**

If verification requires fixes, add focused tests first, then run the relevant tests again and commit:

```bash
git add .
git commit -m "fix: verify table facet behavior"
```

Do not commit unrelated working-tree changes.
