"""翻页管理接口：维护翻页规则，覆盖提交规则验证、启用规则、废弃规则等动作。

翻页版本复用与无效页终止接口也在这里：
- 规则参数变更产生新版本，版本历史可追溯；
- 翻页采集范围幂等提交，URL过滤与抓取模块过滤重复页面后写入分页结果；
- 终止信号幂等计数，处理结果同步页面落地侧清单。

注意：/ranges、/landing、/export 等字面路径必须声明在 /{entry_id} 之前，
否则会被路径参数路由吞掉。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.pagemarker import PagemarkerService

router = APIRouter(prefix="/api/pagemarker", tags=["翻页管理"])

service = PagemarkerService()

LIST_FIELDS = ["规则编号", "目标站点", "翻页类型", "起始页码", "最大页码", "页码参数名", "偏移量参数", "总条数定位器"]
STATUSES = ["草稿", "规则验证中", "已启用", "已废弃"]
RANGE_STATUSES = ["进行中", "已终止", "已完成"]


# ---------- 翻页采集范围（字面路径，先于 /{entry_id} 声明） ----------
@router.get("/ranges", response_model=PageResult[dict])
def list_ranges(
    keyword: str | None = Query(default=None, description="按规则编号或作业编号检索"),
    status: str | None = Query(default=None, description="进行中、已终止、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """翻页采集范围列表：绑定版本、停止依据与处理结果都可追溯；没有数据时返回空页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_ranges(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/ranges/{range_id}", response_model=dict)
def get_range(range_id: int) -> dict:
    """范围明细连同分页结果一起返回；不存在时给出可读的错误说明。"""
    detail = service.get_range_detail(range_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"翻页采集范围 {range_id} 不存在或已归档")
    return detail


@router.post("/ranges/{range_id}/pages", response_model=ActionResult)
def record_page(range_id: int, payload: EntryPayload) -> ActionResult:
    """URL过滤与抓取模块写入分页结果：先过滤重复页面，再按取数逻辑定位停止条件。"""
    page, message = service.record_page(range_id, payload.values)
    if page is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=page)


@router.post("/ranges/{range_id}/terminate", response_model=ActionResult)
def terminate_range(range_id: int, payload: EntryPayload) -> ActionResult:
    """上报终止信号：重复终止信号不重复计数。"""
    rng, message = service.terminate_range(range_id, payload.values)
    if rng is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=rng)


@router.post("/ranges/{range_id}/landing", response_model=ActionResult)
def write_landing(range_id: int, payload: EntryPayload) -> ActionResult:
    """处理后的业务结果写入页面落地侧清单，翻页采集范围保留在同一记录。"""
    landing, message = service.write_landing(range_id, payload.values)
    if landing is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=landing)


@router.get("/landing", response_model=PageResult[dict])
def list_landing(
    keyword: str | None = Query(default=None, description="按范围编号或规则编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """页面落地侧清单：处理结果与翻页采集范围在同一记录里。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_landing(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出翻页管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "pagemarker", "total": total, "items": items}


# ---------- 翻页规则清单 ----------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则验证中、已启用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤翻页管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条翻页规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="翻页规则已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条翻页规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"翻页规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条翻页规则执行提交规则验证、启用规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/versions", response_model=dict)
def list_versions(entry_id: int) -> dict:
    """规则版本历史：参数快照、变更说明与生成时间都可追溯。"""
    versions = service.list_versions(entry_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=f"翻页规则 {entry_id} 不存在或已归档")
    return {"items": versions, "total": len(versions)}


@router.post("/{entry_id}/versions", response_model=ActionResult)
def change_rule_params(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记规则参数变更：参数变化必须产生新版本，未受影响作业复用原范围。"""
    entry, changed, message = service.change_rule_params(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=changed, message=message, entry=entry)


@router.post("/{entry_id}/ranges", response_model=ActionResult)
def submit_range(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交翻页采集范围：绑定当前规则版本，重复提交按幂等只保留一次。"""
    rng, duplicated, message = service.submit_range(entry_id, payload.values)
    if rng is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=not duplicated, message=message, entry=rng)
