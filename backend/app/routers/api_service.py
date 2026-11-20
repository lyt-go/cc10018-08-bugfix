"""API服务接口：维护数据接口，覆盖提交校验、发布接口、下线接口等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.api_service import ApiServiceService

router = APIRouter(prefix="/api/api_service", tags=["API服务"])

service = ApiServiceService()

LIST_FIELDS = ["接口编号", "接口名称", "查询SQL", "参数列表", "返回字段", "缓存时长", "限流阈值", "接口版本"]
STATUSES = ["草稿", "待校验", "已发布", "已下线"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按接口编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已发布、已下线"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、接口编号关键字与状态过滤API服务列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="api_service.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据接口明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据接口 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条数据接口，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据接口已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条数据接口执行提交校验、发布接口、下线接口。

    提交校验与发布接口都会核对接口声明字段与所选模板（参数名、类型、可绑定返回列）；
    校验失败时拦截动作、保留原状态，并在 message 中指出冲突字段。
    """
    entry, message, validation = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message, entry=None)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/binding-validation")
def get_binding_validation(entry_id: int) -> dict:
    """返回接口绑定校验结果；尚未校验时现场按当前模板核对一次但不落状态。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据接口 {entry_id} 不存在或已归档")
    stored = service.check_binding(entry_id)
    if stored is not None:
        return stored
    _, validation = service.validate_contract(entry_id)
    return validation or {"ok": False, "errors": ["校验结果不可用"], "conflicts": []}


@router.post("/{entry_id}/bind-sql")
def bind_sql(entry_id: int, payload: dict[str, object]) -> dict:
    """对API接口执行参数绑定（API服务→SQL模板链）。

    绑定消费发布时固化的接口绑定校验结果：模板指纹漂移、必填参数缺失、
    契约外实参都会在取数前被拒绝，绑定SQL取自发布时核对过的快照。
    """
    from app.engine.sql_binder import SqlTemplateBinder
    binder = SqlTemplateBinder()
    entry = service.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"接口 {entry_id} 不存在")
    return binder.expand_api_query(entry_id, payload)


@router.get("/{entry_id}/optimize")
def optimize_api(entry_id: int) -> dict:
    """分析API接口的SQL并返回优化建议（API服务→查询优化链）。"""
    from app.engine.query_optimizer import QueryOptimizer
    optimizer = QueryOptimizer()
    report = optimizer.analyze_slow_query(entry_id)
    return report
