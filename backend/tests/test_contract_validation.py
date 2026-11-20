"""接口绑定契约校验测试：覆盖发布、绑定与查询链对同一份契约的核对。

三条重点路径：
1. 核对不匹配发布：接口声明与所选模板参数名/类型/返回列不一致时，发布被拦截，
   已发布版本不被改变，冲突字段必须可读。
2. 修正后有效发布：修正接口声明后重新校验并发布，绑定按契约字段与实参条件生成。
3. 校验期间替换模板：校验通过后模板被换成另一套条件（日期→地区列表），
   旧校验结果用在新模板上必须被指纹识别并拒绝，不能按错误条件取数。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import store
from app.seed import SEED_ROWS
from app.services.api_service import ApiServiceService
from app.engine.contract_validator import (
    ContractValidator,
    PARAM_MISSING,
    PARAM_UNDECLARED,
    TYPE_CONFLICT,
    COLUMN_UNBINDABLE,
    TEMPLATE_MISSING,
)
from app.engine.sql_binder import SqlTemplateBinder
from app.engine.chain_orchestrator import ChainOrchestrator


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


def make_api(api_id: str, sql: str, template_code: str, returns: str) -> dict:
    rows = store.rows("api_service")
    entry = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "status": "草稿",
        "pending": True,
        "abnormal": False,
        "接口编号": api_id,
        "接口名称": f"测试接口-{api_id}",
        "查询SQL": sql,
        "参数列表": template_code,
        "返回字段": returns,
        "缓存时长": "60",
        "限流阈值": "100",
        "接口版本": "v1",
    }
    rows.append(entry)
    return entry


# ── 路径1：核对不匹配发布 ─────────────────────────────────


def test_publish_rejected_on_param_name_conflict():
    reset_store()
    service = ApiServiceService()
    # 模板 SQL-TRADE-001 要求 start_date/end_date/shop_list；接口只声明两个日期参数。
    entry = make_api(
        "API-TEST-BAD-1",
        "SELECT dt, gmv FROM t WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}}",
        "SQL-TRADE-001",
        "dt,gmv",
    )
    original_status = entry["status"]

    updated, message, validation = service.run_action(entry["id"], "发布接口")

    assert updated is None
    assert validation is not None and validation["ok"] is False
    assert "shop_list" in validation["conflict_fields"]
    kinds = {c["kind"] for c in validation["conflicts"]}
    assert PARAM_MISSING in kinds  # 模板要求的 shop_list 接口未声明
    # 失败不得改变此前状态/版本
    assert entry["status"] == original_status
    assert entry["接口版本"] == "v1"
    assert "shop_list" in message
    print("  [PASS] 路径1a: 参数名不匹配时发布被拦截且不改变原版本")


def test_publish_rejected_on_type_conflict():
    reset_store()
    service = ApiServiceService()
    # 同名字段但类型冲突：start_date 在模板里是 date，接口声明成 int。
    entry = make_api(
        "API-TEST-BAD-2",
        "SELECT dt, dau FROM t WHERE dt = {{start_date:int}}",
        "SQL-TRADE-001",
        "dt,dau",
    )

    updated, _message, validation = service.run_action(entry["id"], "提交校验")

    assert updated is None
    conflict = next(c for c in validation["conflicts"] if c["field"] == "start_date")
    assert conflict["kind"] == TYPE_CONFLICT
    assert "int" in conflict["message"] and "date" in conflict["message"]
    assert entry["status"] == "草稿"  # 校验失败保持草稿，未流转到待校验
    print("  [PASS] 路径1b: 参数类型冲突被指出且状态不流转")


def test_publish_rejected_on_unbindable_return_column():
    reset_store()
    service = ApiServiceService()
    # 模板输出列为 dt/shop_id/gmv；接口声明了模板 SELECT 中不存在的返回列。
    entry = make_api(
        "API-TEST-BAD-3",
        "SELECT dt, shop_id, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} GROUP BY dt, shop_id ORDER BY dt",
        "SQL-TRADE-001",
        "dt,shop_id,gmv,profit",  # profit 不可绑定
    )

    _updated, _message, validation = service.run_action(entry["id"], "发布接口")

    assert validation["ok"] is False
    conflict = next(c for c in validation["conflicts"] if c["field"] == "profit")
    assert conflict["kind"] == COLUMN_UNBINDABLE
    print("  [PASS] 路径1c: 不可绑定返回列被拦截并指出冲突字段")


def test_publish_rejected_when_template_missing():
    reset_store()
    service = ApiServiceService()
    entry = make_api(
        "API-TEST-BAD-4",
        "SELECT dt FROM t WHERE dt = {{query_date:date}}",
        "SQL-NOT-EXIST",
        "dt",
    )
    _updated, _message, validation = service.run_action(entry["id"], "发布接口")
    assert validation["ok"] is False
    assert validation["conflicts"][0]["kind"] == TEMPLATE_MISSING
    assert entry["status"] == "草稿"
    print("  [PASS] 路径1d: 所选模板不存在时拒绝发布")


def test_undeclared_param_rejected_on_publish():
    reset_store()
    service = ApiServiceService()
    # 接口多出一个模板里不存在的参数 region。
    entry = make_api(
        "API-TEST-BAD-5",
        "SELECT dt, shop_id, gmv FROM t WHERE dt BETWEEN {{start_date:date}} "
        "AND {{end_date:date}} AND shop_id IN {{shop_list:list}} AND region = {{region:str}}",
        "SQL-TRADE-001",
        "dt,shop_id,gmv",
    )
    _updated, _message, validation = service.run_action(entry["id"], "发布接口")
    conflict = next(c for c in validation["conflicts"] if c["field"] == "region")
    assert conflict["kind"] == PARAM_UNDECLARED
    print("  [PASS] 路径1e: 接口声明的模板外参数被拒绝")


# ── 路径2：修正后有效发布与绑定 ───────────────────────────


def test_fixed_api_publishes_and_binds_with_contract():
    reset_store()
    service = ApiServiceService()
    binder = SqlTemplateBinder()

    entry = make_api(
        "API-TEST-OK-1",
        "SELECT dt, shop_id, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} GROUP BY dt, shop_id ORDER BY dt",
        "SQL-TRADE-001",
        "dt,shop_id,gmv",
    )

    updated, message, validation = service.run_action(entry["id"], "提交校验")
    assert updated is not None and validation["ok"] is True
    assert entry["status"] == "待校验"
    assert validation["required_params"] == []  # 三个参数模板都给了默认值
    assert set(validation["conflict_fields"]) == set()

    updated, _message, validation = service.run_action(entry["id"], "发布接口")
    assert updated is not None and entry["status"] == "已发布"
    assert validation["template_fingerprint"]

    # 绑定按契约字段与实参条件生成（shop_list 展开成两个占位符）。
    result = binder.expand_api_query(entry["id"], {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": ["101", "102"],
    })
    assert result["ok"], result
    assert "$1" in result["sql"] and "$4" in result["sql"]
    # 列表占位符先展开，标量占位符后绑定，参数顺序与占位符处理顺序一致。
    assert sorted(result["params"]) == sorted(["2026-11-01", "2026-11-15", "101", "102"])
    assert result["binding_validation"]["template_id"] == "SQL-TRADE-001"

    # 默认值参数允许只传部分实参，由模板默认值兜底。
    partial = binder.expand_api_query(entry["id"], {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
    })
    assert partial["ok"], partial
    assert sorted(partial["params"]) == sorted(["2026-11-01", "2026-11-15", "101", "102"])
    print("  [PASS] 路径2: 修正后有效发布，绑定使用一致字段与默认值")


def test_required_param_without_default_blocked_before_fetch():
    reset_store()
    service = ApiServiceService()
    binder = SqlTemplateBinder()
    # SQL-USER-001 给 query_date 配了默认值；构造一个无默认值的新模板。
    store.rows("sql_template").append({
        "id": 99,
        "status": "已生效",
        "pending": False,
        "abnormal": False,
        "模板编号": "SQL-NODEFAULT-001",
        "模板名称": "无默认值模板",
        "SQL片段": "SELECT region, gmv FROM t WHERE region IN {{region_list:list}}",
        "参数标记": "region_list",
        "参数类型": "list",
        "默认值": "",
        "使用次数": "0",
        "模板版本": "v1",
    })
    entry = make_api(
        "API-TEST-OK-2",
        "SELECT region, gmv FROM t WHERE region IN {{region_list:list}}",
        "SQL-NODEFAULT-001",
        "region,gmv",
    )
    _updated, _message, validation = service.run_action(entry["id"], "发布接口")
    assert validation["ok"] is True
    assert validation["required_params"] == ["region_list"]

    blocked = binder.expand_api_query(entry["id"], {})
    assert blocked["ok"] is False
    assert any("region_list" in err for err in blocked["errors"])
    assert blocked["sql"] is None  # 取数前被拦住，没有生成SQL

    ok_result = binder.expand_api_query(entry["id"], {"region_list": ["east", "west"]})
    assert ok_result["ok"], ok_result
    print("  [PASS] 路径2b: 缺少默认值的必填参数在取数前拦截，补齐后可绑定")


def test_contract_outside_argument_rejected():
    reset_store()
    service = ApiServiceService()
    binder = SqlTemplateBinder()
    entry = make_api(
        "API-TEST-OK-3",
        "SELECT dt, shop_id, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} GROUP BY dt, shop_id ORDER BY dt",
        "SQL-TRADE-001",
        "dt,shop_id,gmv",
    )
    service.run_action(entry["id"], "发布接口")
    result = binder.expand_api_query(entry["id"], {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": ["101"],
        "region": "east",  # 契约外实参
    })
    assert result["ok"] is False
    assert any("region" in err for err in result["errors"])
    print("  [PASS] 路径2c: 契约外实参被拒绝，不进入查询条件")


# ── 路径3：校验期间替换模板（TOCTOU） ─────────────────────


def test_template_swap_after_validation_blocks_binding():
    reset_store()
    service = ApiServiceService()
    binder = SqlTemplateBinder()
    validator = ContractValidator()

    entry = make_api(
        "API-TEST-SWAP-1",
        "SELECT dt, shop_id, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} GROUP BY dt, shop_id ORDER BY dt",
        "SQL-TRADE-001",
        "dt,shop_id,gmv",
    )
    service.run_action(entry["id"], "提交校验")
    _updated, _message, validation = service.run_action(entry["id"], "发布接口")
    assert validation["ok"] is True
    old_fingerprint = entry["binding_validation"]["template_fingerprint"]

    # 校验通过后，模板被替换：日期条件改成地区列表（同一模板编号、新SQL片段）。
    template = next(r for r in store.rows("sql_template") if r.get("模板编号") == "SQL-TRADE-001")
    template["SQL片段"] = (
        "SELECT region, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE region IN {{region_list:list}} GROUP BY region"
    )
    template["参数标记"] = "region_list"
    template["参数类型"] = "list"
    template["默认值"] = "region_list=east,west"
    template["模板版本"] = "v3"
    new_fingerprint = validator.template_fingerprint(template)
    assert new_fingerprint != old_fingerprint

    # 旧校验结果用在新模板上：必须被指纹拦下，不能按 region 条件取数。
    result = binder.expand_api_query(entry["id"], {"region_list": ["east", "west"]})
    assert result["ok"] is False
    assert result.get("template_changed") is True
    assert result["sql"] is None
    # 已发布版本本身不被破坏，仍保留旧快照与旧校验结果。
    assert entry["status"] == "已发布"
    assert entry["binding_validation"]["template_fingerprint"] == old_fingerprint
    print("  [PASS] 路径3a: 校验后替换模板，指纹漂移被拦截且已发布版本不变")


def test_revalidation_picks_up_swapped_template_and_republish():
    reset_store()
    service = ApiServiceService()
    binder = SqlTemplateBinder()

    entry = make_api(
        "API-TEST-SWAP-2",
        "SELECT dt, shop_id, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} GROUP BY dt, shop_id ORDER BY dt",
        "SQL-TRADE-001",
        "dt,shop_id,gmv",
    )
    service.run_action(entry["id"], "发布接口")

    # 模板被换成地区列表版本。
    template = next(r for r in store.rows("sql_template") if r.get("模板编号") == "SQL-TRADE-001")
    template["SQL片段"] = (
        "SELECT region, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE region IN {{region_list:list}} GROUP BY region"
    )
    template["参数标记"] = "region_list"
    template["参数类型"] = "list"
    template["默认值"] = "region_list=east,west"

    # 接口契约还是旧的日期参数：重新校验应当失败，并指出冲突字段。
    updated, _message, validation = service.run_action(entry["id"], "发布接口")
    assert updated is None and validation["ok"] is False
    assert "region_list" in validation["conflict_fields"]  # 新模板要求、接口未声明
    assert "start_date" in validation["conflict_fields"]  # 接口旧参数、新模板不存在
    assert entry["status"] == "已发布"  # 失败不改变已发布版本

    # 按新模板修正接口声明后再发布，绑定应使用新的地区条件。
    entry["查询SQL"] = (
        "SELECT region, SUM(order_amt) AS gmv FROM dwd_trade_order_di "
        "WHERE region IN {{region_list:list}} GROUP BY region"
    )
    entry["返回字段"] = "region,gmv"
    updated, _message, validation = service.run_action(entry["id"], "发布接口")
    assert updated is not None and validation["ok"] is True

    result = binder.expand_api_query(entry["id"], {"region_list": ["east", "west", "north"]})
    assert result["ok"], result
    assert "region IN" in result["sql"]
    assert "start_date" not in result["sql"]
    assert result["params"] == ["east", "west", "north"]
    print("  [PASS] 路径3b: 重新校验反映新模板，修正后发布按新条件取数")


# ── 查询服务链消费校验结果 ────────────────────────────────


def test_query_service_chain_consumes_validation():
    reset_store()
    orch = ChainOrchestrator()
    # 种子接口 1 已发布但无历史校验：首次取数按当前模板补做核对并持久化。
    api = store.find("api_service", 1)
    assert api.get("binding_validation") is None

    result = orch.run_query_service_chain(1, 1, {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": ["101", "102"],
    })
    assert result["ok"], result
    assert api["binding_validation"] is not None  # 补做结果已固化
    assert result["binding_validation"]["template_fingerprint"]
    assert "$1" in result["bound_sql"]

    # 必填参数缺失（用无默认值模板的接口）在链路内被拦住。
    store.rows("sql_template").append({
        "id": 99, "status": "已生效", "pending": False, "abnormal": False,
        "模板编号": "SQL-CHAIN-001", "模板名称": "链路无默认值模板",
        "SQL片段": "SELECT region FROM t WHERE region IN {{region_list:list}}",
        "参数标记": "region_list", "参数类型": "list", "默认值": "",
        "使用次数": "0", "模板版本": "v1",
    })
    entry = make_api(
        "API-TEST-CHAIN-1",
        "SELECT region FROM t WHERE region IN {{region_list:list}}",
        "SQL-CHAIN-001",
        "region",
    )
    service = ApiServiceService()
    service.run_action(entry["id"], "发布接口")
    blocked = orch.run_query_service_chain(entry["id"], 99, {})
    assert blocked["ok"] is False
    assert blocked["steps"][1]["module"] == "sql_template"
    assert any("region_list" in s for s in [blocked["steps"][1]["detail"]])
    assert "bound_sql" not in blocked
    print("  [PASS] 路径4: 查询服务链消费校验结果并在取数前拦截缺参")


def test_query_service_chain_rejects_mismatched_template_id():
    reset_store()
    orch = ChainOrchestrator()
    # 接口1绑定 SQL-TRADE-001，却用 SQL-USER-001 的 id=2 发起链路。
    result = orch.run_query_service_chain(1, 2, {
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "shop_list": ["101"],
    })
    assert result["ok"] is False
    detail = result["steps"][1]["detail"]
    assert "SQL-USER-001" in detail and "SQL-TRADE-001" in detail
    print("  [PASS] 路径4b: 链路拒绝与发布模板不一致的 template_id")


def test_http_endpoints_expose_validation():
    reset_store()
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # 构造一个契约不匹配的接口并尝试发布。
    created = client.post("/api/api_service", json={
        "接口编号": "API-HTTP-BAD-1",
        "接口名称": "HTTP冲突接口",
        "查询SQL": "SELECT dt FROM t WHERE dt = {{query_date:date}}",
        "参数列表": "SQL-TRADE-001",
        "返回字段": "dt",
    })
    assert created.json()["ok"] is True
    new_id = created.json()["entry"]["id"]

    action = client.post(f"/api/api_service/{new_id}/actions", json={"action": "发布接口"})
    body = action.json()
    assert body["ok"] is False
    assert "shop_list" in body["message"]  # 冲突字段出现在消息里

    # 校验结果查询端点可读到结构化冲突。
    validation = client.get(f"/api/api_service/{new_id}/binding-validation")
    assert validation.status_code == 200
    assert validation.json()["ok"] is False
    assert "shop_list" in validation.json()["conflict_fields"]

    # 绑定端点对未发布接口拒绝取数。
    bind = client.post(f"/api/api_service/{new_id}/bind-sql", json={"query_date": "2026-11-15"})
    assert bind.json()["ok"] is False
    print("  [PASS] 路径5: HTTP 层暴露校验结果、冲突字段与绑定拦截")


def run_all() -> bool:
    tests = [
        test_publish_rejected_on_param_name_conflict,
        test_publish_rejected_on_type_conflict,
        test_publish_rejected_on_unbindable_return_column,
        test_publish_rejected_when_template_missing,
        test_undeclared_param_rejected_on_publish,
        test_fixed_api_publishes_and_binds_with_contract,
        test_required_param_without_default_blocked_before_fetch,
        test_contract_outside_argument_rejected,
        test_template_swap_after_validation_blocks_binding,
        test_revalidation_picks_up_swapped_template_and_republish,
        test_query_service_chain_consumes_validation,
        test_query_service_chain_rejects_mismatched_template_id,
        test_http_endpoints_expose_validation,
    ]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {test.__name__}: {exc}")
            failed += 1
    print(f"\n=== 接口绑定契约测试: {passed} 通过, {failed} 失败, 共 {len(tests)} 项 ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
