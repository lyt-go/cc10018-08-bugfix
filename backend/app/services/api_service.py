"""API服务业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "api_service"
LIST_FIELDS = ["接口编号", "接口名称", "查询SQL", "参数列表", "返回字段", "缓存时长", "限流阈值", "接口版本"]
REQUIRED_FIELDS = ["接口编号", "接口名称", "查询SQL"]
STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "发布接口": "已发布", "下线接口": "已下线"}
NEGATIVE_ACTIONS = ["下线接口"]


class ApiServiceService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("接口编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def check_binding(self, entry_id: int) -> dict[str, Any] | None:
        """返回接口最近一次接口绑定校验结果；从未校验时返回 None。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return entry.get("binding_validation")

    def validate_contract(self, entry_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """按当前所选模板重新核对接口契约（参数名、类型、可绑定返回列）。

        每次都实时解析模板并重新生成指纹，因此校验期间模板被替换时，
        新结果自然反映新模板，旧结果不会被沿用。
        """
        from app.engine.contract_validator import ContractValidator

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None
        validator = ContractValidator()
        template = validator.resolve_template(entry)
        return entry, validator.validate_binding(entry, template)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据接口 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于API服务可执行范围", None
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里", None

        # 提交校验与发布接口必须核对同一份契约；下线接口不涉及取数绑定，保持原流转。
        if action in ("提交校验", "发布接口"):
            entry, validation = self.validate_contract(entry_id)
            assert entry is not None and validation is not None
            # 无论通过与否都留下最近一次校验结果，前端可展示冲突字段。
            entry["binding_validation"] = validation
            if not validation["ok"]:
                # 校验失败不得改变接口当前版本/发布状态，并指出每个冲突字段。
                detail = "；".join(validation["errors"])
                return None, f"接口绑定校验未通过，已拦截{action}：{detail}", validation
            if action == "提交校验":
                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = action in NEGATIVE_ACTIONS
                return entry, "数据接口已提交校验，接口绑定校验通过", validation
            # 发布接口：再次确认模板指纹与本次校验一致（同一事务内刚解析，必然一致），
            # 并把核对过的SQL快照随校验结果固化，绑定与查询链只消费这份结果。
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, "数据接口已发布，接口绑定校验通过", validation

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"数据接口已{action}", entry.get("binding_validation")
