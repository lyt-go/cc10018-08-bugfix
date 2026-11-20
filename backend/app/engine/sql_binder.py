"""SQL模板参数绑定引擎：支持占位符解析、类型校验、默认值回填、动态SQL生成。

支持的占位符语法：
- {{param_name}}        → 文本替换（自动转义）
- {{param_name:int}}    → 整数类型校验
- {{param_name:date}}   → 日期格式校验
- {{param_name:list}}   → IN 列表展开
- {{#if cond}}...{{/if}} → 条件分支
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.store import store


class SqlTemplateBinder:
    """SQL模板参数绑定引擎。"""

    TYPE_PATTERNS = {
        "int": r"^-?\d+$",
        "float": r"^-?\d+(\.\d+)?$",
        "date": r"^\d{4}-\d{2}-\d{2}$",
        "datetime": r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$",
        "str": r".*",
    }

    def __init__(self) -> None:
        pass

    def parse_params(self, sql_text: str) -> list[dict[str, str]]:
        """从SQL模板中提取所有参数标记。"""
        pattern = r"\{\{(\w+)(?::(\w+))?\}\}"
        found = []
        seen = set()
        for m in re.finditer(pattern, sql_text):
            name, ptype = m.group(1), m.group(2) or "str"
            if name not in seen:
                seen.add(name)
                found.append({"name": name, "type": ptype, "pos": m.start()})
        return found

    def validate_params(self, sql_text: str, values: dict[str, Any]) -> list[str]:
        """校验参数值是否符合模板声明的类型。"""
        errors = []
        params = self.parse_params(sql_text)
        for p in params:
            name = p["name"]
            ptype = p["type"]
            val = values.get(name)
            if val is None:
                # 尝试从模板默认配置中补全
                tmpl = self._find_template_by_sql(sql_text)
                if tmpl:
                    defaults = self._parse_defaults(tmpl.get("默认值", ""))
                    val = defaults.get(name)
                if val is None:
                    errors.append(f"参数 {name} 未提供")
                    continue
            val_str = str(val)
            pat = self.TYPE_PATTERNS.get(ptype, self.TYPE_PATTERNS["str"])
            if not re.match(pat, val_str):
                errors.append(f"参数 {name} 值 '{val_str}' 不符合类型 {ptype}")
        return errors

    def bind(self, sql_text: str, values: dict[str, Any]) -> dict[str, Any]:
        """执行参数绑定，返回绑定后的SQL和参数列表。"""
        errors = self.validate_params(sql_text, values)
        if errors:
            return {"ok": False, "errors": errors, "sql": None, "params": []}

        bound_sql = sql_text
        bound_params: list[Any] = []

        # 处理列表类型展开
        def replace_list(m: re.Match) -> str:
            name = m.group(1)
            val = values.get(name, [])
            if isinstance(val, str):
                val = [v.strip() for v in val.split(",")]
            placeholders = ", ".join(f"${len(bound_params) + j + 1}" for j in range(len(val)))
            bound_params.extend(val)
            return placeholders

        # 先处理 :list 类型
        bound_sql = re.sub(r"\{\{(\w+):list\}\}", replace_list, bound_sql)

        # 再处理普通类型
        def replace_param(m: re.Match) -> str:
            name = m.group(1)
            ptype = m.group(2) or "str"
            val = values.get(name)
            if val is None:
                tmpl = self._find_template_by_sql(sql_text)
                if tmpl:
                    defaults = self._parse_defaults(tmpl.get("默认值", ""))
                    val = defaults.get(name)
            if ptype == "int":
                val = int(val)  # type: ignore[arg-type]
            elif ptype == "float":
                val = float(val)  # type: ignore[arg-type]
            bound_params.append(val)
            return f"${len(bound_params)}"

        bound_sql = re.sub(r"\{\{(\w+)(?::(\w+))?\}\}", replace_param, bound_sql)

        return {"ok": True, "sql": bound_sql, "params": bound_params}

    def expand_template(self, template_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按模板编号加载并展开。"""
        tmpl = store.find("sql_template", template_id)
        if not tmpl:
            return {"ok": False, "errors": [f"模板 {template_id} 不存在"], "sql": None}
        sql_text = tmpl.get("SQL片段", "")
        return self.bind(sql_text, values)

    def expand_api_query(self, api_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按API接口编号生成绑定SQL（API服务→SQL模板→查询执行链）。

        绑定前必须消费接口绑定校验结果：
        - 仅已发布且带有有效校验结果的接口可取数；
        - 模板指纹与校验时不一致（校验期间模板被替换）直接拒绝，
          不让本次查询用上未经核对的新模板；
        - 缺少默认值的参数必须显式给值，在取数前拦住；
        - 接口未声明的实参一律拒绝，保证查询字段与契约一致；
        - 绑定一律使用发布时核对过的SQL快照，而不是模板当前内容。
        """
        from app.engine.contract_validator import ContractValidator

        api = store.find("api_service", api_id)
        if not api:
            return {"ok": False, "errors": [f"接口 {api_id} 不存在"], "sql": None, "params": []}

        validator = ContractValidator()
        validation = api.get("binding_validation")

        # 兼容早于契约校验机制发布的存量接口：首次取数时按当前模板补做一次核对，
        # 之后即持久化校验结果并受指纹保护。
        if not validation:
            if api.get("status") != "已发布":
                return {
                    "ok": False,
                    "errors": [f"接口 {api.get('接口编号', api_id)} 尚未发布，当前状态为 {api.get('status')}，不能取数"],
                    "sql": None,
                    "params": [],
                }
            live_template = validator.resolve_template(api)
            validation = validator.validate_binding(api, live_template)
            if not validation["ok"]:
                return {
                    "ok": False,
                    "errors": validation["errors"],
                    "conflicts": validation["conflicts"],
                    "sql": None,
                    "params": [],
                }
            api["binding_validation"] = validation

        if not validation.get("ok"):
            return {
                "ok": False,
                "errors": validation.get("errors", ["接口绑定校验未通过"]),
                "conflicts": validation.get("conflicts", []),
                "sql": None,
                "params": [],
            }

        live_template = validator.resolve_template(api)
        if live_template is None:
            return {
                "ok": False,
                "errors": [f"接口所选模板 {validation.get('template_id', '')} 不存在，无法取数"],
                "sql": None,
                "params": [],
            }
        live_fingerprint = validator.template_fingerprint(live_template)
        if live_fingerprint != validation.get("template_fingerprint"):
            return {
                "ok": False,
                "errors": [
                    "所选模板在校验通过后发生变更（参数或SQL已替换），"
                    "旧校验结果已失效，请重新提交校验并发布后再取数"
                ],
                "template_changed": True,
                "sql": None,
                "params": [],
            }

        declared_names = {item["name"] for item in validation.get("template_params", [])}
        defaults = validation.get("defaults", {})

        # 接口未声明的实参不得进入查询，避免按契约外条件取数。
        undeclared = [name for name in values if name not in declared_names]
        if undeclared:
            return {
                "ok": False,
                "errors": [f"实参 {name} 不在接口声明参数内，已拒绝绑定" for name in undeclared],
                "sql": None,
                "params": [],
            }

        # 缺少默认值的参数必须在取数前拦住（空字符串、空列表都视为未提供）。
        def _is_blank(value: Any) -> bool:
            return value is None or value == "" or (isinstance(value, (list, tuple, dict)) and len(value) == 0)

        missing = [
            name for name in validation.get("required_params", [])
            if _is_blank(values.get(name))
        ]
        if missing:
            return {
                "ok": False,
                "errors": [f"必填参数 {name} 未提供且模板无默认值" for name in missing],
                "sql": None,
                "params": [],
            }

        # 先用模板默认值兜底，再用调用方实参覆盖，保证绑定条件与契约一致。
        merged_values: dict[str, Any] = dict(defaults)
        merged_values.update(values)

        # list 类型参数统一转成列表：模板默认值是 "a,b" 形式的字符串，
        # 不转换会被当成单元素列表绑定。
        list_params = {item["name"] for item in validation.get("template_params", []) if item["type"] == "list"}
        for name in list_params:
            val = merged_values.get(name)
            if isinstance(val, str):
                merged_values[name] = [part.strip() for part in val.split(",") if part.strip()]

        # 绑定使用发布时核对过的SQL快照，不直接读取模板当前SQL。
        snapshot_sql = validation.get("sql_text", "")
        result = self.bind(snapshot_sql, merged_values)
        if result.get("ok"):
            result["binding_validation"] = self._public_validation(validation)
        return result

    def _public_validation(self, validation: dict[str, Any]) -> dict[str, Any]:
        """摘取可对外暴露的校验结果摘要，去掉SQL全文等冗余内容。"""
        return {
            "ok": validation.get("ok"),
            "template_id": validation.get("template_id"),
            "template_version": validation.get("template_version"),
            "template_fingerprint": validation.get("template_fingerprint"),
            "declared_params": validation.get("declared_params"),
            "template_params": validation.get("template_params"),
            "required_params": validation.get("required_params"),
            "optional_params": validation.get("optional_params"),
            "declared_columns": validation.get("declared_columns"),
            "template_columns": validation.get("template_columns"),
            "conflict_fields": validation.get("conflict_fields"),
        }

    def _find_template_by_sql(self, sql_text: str) -> dict[str, Any] | None:
        for r in store.rows("sql_template"):
            if r.get("SQL片段", "") == sql_text:
                return r
        return None

    def _parse_defaults(self, defaults_raw: str) -> dict[str, str]:
        """解析默认值为字典，与契约校验器共用同一套口径。"""
        from app.engine.contract_validator import ContractValidator

        return ContractValidator().parse_defaults(defaults_raw)

    def suggest_optimization(self, sql_text: str) -> list[dict[str, str]]:
        """基于SQL文本给出优化建议（与查询优化模块联动）。"""
        suggestions = []
        sql_upper = sql_text.upper()

        if "SELECT *" in sql_upper:
            suggestions.append({
                "type": "column_prune",
                "severity": "warning",
                "message": "避免使用 SELECT *，应显式列出所需字段以减少IO",
            })

        if re.search(r"\bLIKE\s+['\"]%[^'\"]+%['\"]", sql_text, re.IGNORECASE):
            suggestions.append({
                "type": "index_unfriendly",
                "severity": "warning",
                "message": "前后通配LIKE会导致索引失效，建议考虑全文检索或前缀索引",
            })

        if sql_upper.count("JOIN") > 3:
            suggestions.append({
                "type": "join_complexity",
                "severity": "info",
                "message": f"检测到 {sql_upper.count('JOIN')} 个JOIN，建议检查是否可拆分为子查询或物化视图",
            })

        if "ORDER BY" in sql_upper and "LIMIT" not in sql_upper:
            suggestions.append({
                "type": "sort_cost",
                "severity": "info",
                "message": "无LIMIT的ORDER BY可能导致全表排序，建议添加分页限制",
            })

        if re.search(r"\bWHERE\b", sql_upper) and not re.search(r"\bINDEX\b", sql_upper):
            suggestions.append({
                "type": "index_suggest",
                "severity": "info",
                "message": "WHERE条件字段建议创建索引以加速查询",
            })

        return suggestions
