"""接口绑定契约校验：发布与绑定共用同一份契约口径。

接口契约由两部分组成，必须互相对得上：
- 接口自身声明：``api_service.查询SQL`` 中的占位符（参数名+类型）与 ``返回字段``；
- 所选模板：``sql_template`` 行的 ``SQL片段`` 占位符、SELECT 可绑定列与 ``默认值``。

发布（提交校验/发布接口）与取数绑定（SQL绑定/查询服务链）都调用本模块，
并通过模板指纹（fingerprint）识别“校验后模板被替换”的场景，避免旧校验结果
被用在新版本模板上。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.store import store

PARAM_PATTERN = re.compile(r"\{\{(\w+)(?::(\w+))?\}")
SELECT_PATTERN = re.compile(r"SELECT\s+(.*?)\s+FROM\s+", re.IGNORECASE | re.DOTALL)
ALIAS_PATTERN = re.compile(r"\bAS\s+[`\"\[]?(\w+)[`\"\]]?\s*$", re.IGNORECASE)
IDENT_PATTERN = re.compile(r"[`\"\[]?(\w+)[`\"\]]?\s*$")

# 冲突类型常量，路由层与测试都按这些 kind 取冲突字段。
TEMPLATE_MISSING = "template_missing"
PARAM_MISSING = "param_missing"        # 模板要求、接口未声明
PARAM_UNDECLARED = "param_undeclared"  # 接口声明、模板不存在
TYPE_CONFLICT = "type_conflict"        # 同名参数类型不一致
COLUMN_UNBINDABLE = "column_unbindable"  # 接口返回字段在模板 SELECT 中不可绑定


class ContractValidator:
    """接口声明与所选SQL模板之间的绑定契约校验器。"""

    # ── 模板定位与指纹 ──────────────────────────────────────

    def resolve_template(self, api_entry: dict[str, Any]) -> dict[str, Any] | None:
        """按接口 ``参数列表`` 中登记的模板编号定位所选模板。"""
        template_code = str(api_entry.get("参数列表", "") or "").strip()
        if not template_code:
            return None
        return next(
            (row for row in store.rows("sql_template") if row.get("模板编号") == template_code),
            None,
        )

    def template_fingerprint(self, template: dict[str, Any]) -> str:
        """模板参与契约的内容指纹；任一字段变化都会产生新指纹。"""
        material = "\n".join([
            str(template.get("模板编号", "")),
            str(template.get("SQL片段", "")),
            str(template.get("参数标记", "")),
            str(template.get("参数类型", "")),
            str(template.get("默认值", "")),
            str(template.get("模板版本", "")),
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # ── 契约要素解析 ────────────────────────────────────────

    def parse_placeholders(self, sql_text: str) -> list[dict[str, str]]:
        """解析SQL中的参数占位符，按出现顺序去重，返回参数名与类型。"""
        found: list[dict[str, str]] = []
        seen: set[str] = set()
        for match in PARAM_PATTERN.finditer(sql_text or ""):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                found.append({"name": name, "type": match.group(2) or "str"})
        return found

    def parse_select_columns(self, sql_text: str) -> list[str]:
        """提取 SELECT 子句可绑定的输出列名（取 AS 别名或末段标识符）。

        ``SELECT *`` 时返回 ``["*"]``，通配符视为任意列均可绑定。
        """
        match = SELECT_PATTERN.search(sql_text or "")
        if not match:
            return []
        select_body = match.group(1)
        columns: list[str] = []
        for raw_part in select_body.split(","):
            expr = raw_part.strip()
            if not expr:
                continue
            if expr == "*" or expr.endswith(".*"):
                return ["*"]
            alias_match = ALIAS_PATTERN.search(expr)
            if alias_match:
                columns.append(alias_match.group(1))
                continue
            ident_match = IDENT_PATTERN.search(expr)
            if ident_match:
                columns.append(ident_match.group(1))
        return columns

    def parse_declared_columns(self, raw_fields: str) -> list[str]:
        """解析接口 ``返回字段``（逗号分隔），保留声明顺序并去重。"""
        result: list[str] = []
        for part in str(raw_fields or "").split(","):
            name = part.strip()
            if name and name not in result:
                result.append(name)
        return result

    def parse_defaults(self, defaults_raw: str) -> dict[str, str]:
        """解析模板默认值，如 'start_date=2026-01-01,end_date=2026-12-31'。

        list 参数的多值默认可写为 'shop_list=101,102'：只有当逗号后面是
        ``新键=`` 形式时才视为键值对分隔符，值内部的逗号保留；
        也支持引号写法 'shop_list="101,102"'。
        """
        result: dict[str, str] = {}
        segments: list[str] = []
        buffer = ""
        in_quote = False
        text = str(defaults_raw or "")
        for index, char in enumerate(text):
            if char in ('"', "'"):
                in_quote = not in_quote
                continue
            if char == "," and not in_quote:
                rest = text[index + 1:]
                # 逗号后形如 word= 才是下一个键值对，否则属于当前值（list 多值）。
                if re.match(r"\s*[\w一-鿿]+\s*=", rest):
                    segments.append(buffer)
                    buffer = ""
                    continue
            buffer += char
        if buffer:
            segments.append(buffer)
        for part in segments:
            if "=" in part:
                key, value = part.split("=", 1)
                result[key.strip()] = value.strip()
        return result

    # ── 核心校验 ────────────────────────────────────────────

    def validate_binding(
        self,
        api_entry: dict[str, Any],
        template: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """核对接口声明字段与所选模板，返回完整的接口绑定校验结果。

        同时核对三类契约要素：参数名、参数类型、可绑定返回列。
        失败时 ``conflicts`` 逐项指出冲突字段与冲突类型，绝不抛异常。
        """
        if template is None:
            template = self.resolve_template(api_entry)

        conflicts: list[dict[str, str]] = []
        template_code = str(api_entry.get("参数列表", "") or "").strip()

        if template is None:
            conflicts.append({
                "field": template_code or "参数列表",
                "kind": TEMPLATE_MISSING,
                "message": f"接口所选模板 {template_code or '（空）'} 不存在，无法核对绑定契约",
            })
            template_params: list[dict[str, str]] = []
            template_columns: list[str] = []
            defaults: dict[str, str] = {}
            sql_text = str(api_entry.get("查询SQL", "") or "")
            fingerprint = ""
            template_version = ""
        else:
            sql_text = str(template.get("SQL片段", "") or "")
            template_params = self.parse_placeholders(sql_text)
            template_columns = self.parse_select_columns(sql_text)
            defaults = self.parse_defaults(template.get("默认值", ""))
            fingerprint = self.template_fingerprint(template)
            template_version = str(template.get("模板版本", "") or "")

        declared_params = self.parse_placeholders(str(api_entry.get("查询SQL", "") or ""))
        declared_columns = self.parse_declared_columns(str(api_entry.get("返回字段", "") or ""))

        declared_map = {item["name"]: item["type"] for item in declared_params}
        template_map = {item["name"]: item["type"] for item in template_params}

        # 1) 参数名核对：模板要求但接口未声明
        for name in template_map:
            if name not in declared_map:
                conflicts.append({
                    "field": name,
                    "kind": PARAM_MISSING,
                    "message": f"模板要求参数 {name} 未在接口声明中出现",
                })

        # 2) 参数名核对：接口声明但模板不存在
        for name in declared_map:
            if name not in template_map:
                conflicts.append({
                    "field": name,
                    "kind": PARAM_UNDECLARED,
                    "message": f"接口声明参数 {name} 在所选模板中不存在，实参将无法绑定",
                })

        # 3) 同名参数类型核对
        for name in sorted(set(declared_map) & set(template_map)):
            if declared_map[name] != template_map[name]:
                conflicts.append({
                    "field": name,
                    "kind": TYPE_CONFLICT,
                    "message": (
                        f"参数 {name} 类型冲突：接口声明为 {declared_map[name]}，"
                        f"模板要求为 {template_map[name]}"
                    ),
                })

        # 4) 可绑定返回列核对（通配符模板跳过）
        if template_columns and template_columns != ["*"]:
            for column in declared_columns:
                if column not in template_columns:
                    conflicts.append({
                        "field": column,
                        "kind": COLUMN_UNBINDABLE,
                        "message": f"返回字段 {column} 在模板 SELECT 输出列中不可绑定",
                    })

        required_params = [item["name"] for item in template_params if item["name"] not in defaults]
        optional_params = [item["name"] for item in template_params if item["name"] in defaults]

        return {
            "ok": not conflicts,
            "template_id": template_code,
            "template_version": template_version,
            "template_fingerprint": fingerprint,
            "sql_text": sql_text,
            "declared_params": declared_params,
            "template_params": template_params,
            "declared_columns": declared_columns,
            "template_columns": template_columns,
            "defaults": defaults,
            "required_params": required_params,
            "optional_params": optional_params,
            "conflict_fields": [item["field"] for item in conflicts],
            "conflicts": conflicts,
            "errors": [item["message"] for item in conflicts],
        }
