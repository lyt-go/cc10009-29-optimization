"""翻页管理业务规则：状态流转、字段校验与筛选口径都收在这里。

翻页版本复用与无效页终止也收在这里，路由层不做业务判断：

- 规则参数（翻页类型、起始页码、最大页码、页码参数名、偏移量参数、总条数定位器）
  任一变化都必须产生新版本；未受影响作业复用原范围，复用时保留绑定版本，
  新版本只对尚未开始的作业生效。
- 翻页采集范围按幂等键（规则编号#作业编号#绑定版本号）只保留一次，
  并发重复提交不重复建档。
- URL过滤与抓取模块读取翻页采集范围，过滤重复页面后再写入分页结果；
  连续重复页立即结束本次分页。
- 空态、没有数据、无效页、异常、失败、重试中断都会结束无效范围，
  停止依据由翻页规则、页码参数名和总条数定位器共同解释，可追溯。
- 空页与总数定位器失效同时发生构成冲突边界：顺着取数逻辑定位、终止优先，
  冲突使翻页采集范围形成不同版本记录。
- 重复终止信号不重复计数。
- 处理后的业务结果写入页面落地侧清单，翻页采集范围保留在同一记录，
  处理结果同步后其他业务记录一并刷新。
"""
from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any

from app.store import store

MODULE = "pagemarker"
RANGES_MODULE = "pagemarker_ranges"
PAGES_MODULE = "pagemarker_pages"
LANDING_MODULE = "pagemarker_landing"

REQUIRED_FIELDS = ["规则编号", "目标站点", "翻页类型"]
STATUS_ORDER = ["草稿", "规则验证中", "已启用", "已废弃"]
ACTION_RULES = {"提交规则验证": "规则验证中", "启用规则": "已启用", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []

# 规则参数：任一变化都必须产生新版本
RULE_PARAM_FIELDS = ["翻页类型", "起始页码", "最大页码", "页码参数名", "偏移量参数", "总条数定位器"]

# 分页结果状态：空态、没有数据、无效页、异常、失败、重试中断命中即结束本次分页
TERMINAL_PAGE_STATUSES = ["空态", "没有数据", "无效页", "异常", "失败", "重试中断"]
PAGE_RESULT_STATUSES = ["成功", *TERMINAL_PAGE_STATUSES]

# 停止原因码：分页抓取终止时必须说明原因
STOP_REASONS = [*TERMINAL_PAGE_STATUSES, "连续重复页", "总数信号耗尽", "到达最大页码", "外部终止"]

RANGE_STATUSES = ["进行中", "已终止", "已完成"]

# 页结果终止时的原因说明模板
TERMINAL_REASON_DETAIL = {
    "空态": "当前页返回空态结构，空页不再请求剩余页码",
    "没有数据": "当前页没有数据，空页不再请求剩余页码",
    "无效页": "当前页为无效页，页码超出有效范围",
    "异常": "分页抓取抛出异常，本次分页结束",
    "失败": "分页抓取请求失败，本次分页结束",
    "重试中断": "重试队列中断，剩余页码不再请求",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class PagemarkerService:
    # ---------- 规则清单（原有能力） ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in RULE_PARAM_FIELDS:
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 登记即产生首个规则版本，后续参数变化在此基础上递增
        entry["当前版本号"] = 1
        entry["版本历史"] = [self._snapshot(entry, 1, "初始登记")]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"翻页规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于翻页管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"翻页规则已{action}"

    # ---------- 规则版本 ----------
    def _snapshot(self, entry: dict[str, Any], version_no: int, note: str) -> dict[str, Any]:
        return {
            "版本号": version_no,
            "参数快照": {field: entry.get(field) for field in RULE_PARAM_FIELDS},
            "变更说明": note,
            "生成时间": _now(),
            "来源": "翻页管理",
        }

    def _ensure_version_fields(self, entry: dict[str, Any]) -> None:
        """兼容历史记录：缺版本字段时按当前参数补建 v1。"""
        if not _to_int(entry.get("当前版本号"), 0):
            entry["当前版本号"] = 1
        if not isinstance(entry.get("版本历史"), list) or not entry["版本历史"]:
            entry["版本历史"] = [self._snapshot(entry, 1, "初始登记")]

    def _bound_snapshot(self, entry: dict[str, Any], version_no: int) -> dict[str, Any]:
        """按绑定版本号取参数快照；复用原范围时只读绑定版本，不读新版本参数。"""
        self._ensure_version_fields(entry)
        for item in entry["版本历史"]:
            if _to_int(item.get("版本号"), 0) == version_no:
                return item["参数快照"]
        return entry["版本历史"][-1]["参数快照"]

    def list_versions(self, entry_id: int) -> list[dict[str, Any]] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        self._ensure_version_fields(entry)
        return entry["版本历史"]

    def change_rule_params(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """登记规则参数变更：参数变化必须产生新版本，未受影响作业复用原范围。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, False, f"翻页规则 {entry_id} 不存在或已归档"
        self._ensure_version_fields(entry)
        changed = [
            field for field in RULE_PARAM_FIELDS
            if field in values and str(values.get(field) or "") != str(entry.get(field) or "")
        ]
        if not changed:
            return entry, False, "规则参数未变化，不产生新版本，作业继续复用原范围"
        for field in changed:
            entry[field] = values.get(field)
        entry["当前版本号"] = _to_int(entry["当前版本号"], 1) + 1
        entry["版本历史"].append(
            self._snapshot(entry, entry["当前版本号"], f"参数变更：{'、'.join(changed)}")
        )
        return entry, True, (
            f"规则参数变化已产生新版本 v{entry['当前版本号']}，"
            "未受影响作业复用原范围并保留绑定版本，新版本只对尚未开始的作业生效"
        )

    # ---------- 翻页采集范围 ----------
    def list_ranges(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(RANGES_MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("规则编号", "")) or keyword in str(row.get("作业编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_range(self, range_id: int) -> dict[str, Any] | None:
        return store.find(RANGES_MODULE, range_id)

    def get_range_detail(self, range_id: int) -> dict[str, Any] | None:
        """范围明细连同分页结果一起返回，来源与处理结果都可追溯。"""
        rng = self.get_range(range_id)
        if rng is None:
            return None
        pages = [row for row in store.rows(PAGES_MODULE) if row.get("范围编号") == rng.get("范围编号")]
        return {**rng, "分页结果": pages}

    def _find_range_by_key(self, idem_key: str) -> dict[str, Any] | None:
        for row in store.rows(RANGES_MODULE):
            if row.get("幂等键") == idem_key:
                return row
        return None

    def submit_range(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """生成翻页采集范围：提交时绑定当前规则版本，重复提交按幂等只保留一次。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, False, f"翻页规则 {entry_id} 不存在或已归档"
        self._ensure_version_fields(entry)
        job_code = str(values.get("作业编号") or "").strip()
        if not job_code:
            return None, False, "缺少必填字段：作业编号"
        bound_version = _to_int(entry["当前版本号"], 1)
        idem_key = f"{entry['规则编号']}#{job_code}#v{bound_version}"
        existing = self._find_range_by_key(idem_key)
        if existing is not None:
            return existing, True, (
                f"重复提交的翻页采集范围按幂等只保留一次：{existing['范围编号']}"
            )
        snapshot = self._bound_snapshot(entry, bound_version)
        start_page = max(_to_int(snapshot.get("起始页码"), 1), 1)
        end_page = _to_int(snapshot.get("最大页码"), start_page)
        if end_page < start_page:
            end_page = start_page
        rows = store.rows(RANGES_MODULE)
        rng = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "范围编号": f"RANGE-{len(rows) + 1:04d}",
            "规则编号": entry["规则编号"],
            "作业编号": job_code,
            "绑定版本号": bound_version,
            "版本来源": "绑定当前版本",
            "范围版本号": 1,
            "起始页码": start_page,
            "结束页码": end_page,
            "当前页码": start_page - 1,
            "状态": "进行中",
            "停止原因码": None,
            "停止依据": None,
            "终止信号数": 0,
            "已记录终止信号": [],
            "连续重复页数": 0,
            "上一页指纹": None,
            "幂等键": idem_key,
            "冲突标记": False,
            "来源": str(values.get("来源") or "翻页管理"),
            "处理结果": "待处理",
        }
        rows.append(rng)
        return rng, False, (
            f"翻页采集范围已生成：{rng['范围编号']} 绑定规则版本 v{bound_version}，"
            f"页码区间 {start_page}-{end_page}"
        )

    # ---------- 分页结果（URL过滤与抓取模块写入） ----------
    def record_page(self, range_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """写入分页结果：先校验复用边界，过滤重复页面，再按取数逻辑定位停止条件。"""
        rng = store.find(RANGES_MODULE, range_id)
        if rng is None:
            return None, f"翻页采集范围 {range_id} 不存在或已归档"
        if rng.get("状态") != "进行中":
            return None, (
                f"翻页采集范围{rng['范围编号']}状态为{rng['状态']}，"
                "无效范围不再接收分页结果，剩余页码不再请求"
            )
        rule = self._find_rule(rng.get("规则编号"))
        if rule is None:
            return None, f"翻页规则 {rng.get('规则编号')} 不存在或已归档"
        # 复用原范围时保留绑定版本：只读绑定版本参数，并发修改规则不扩大旧作业范围
        snapshot = self._bound_snapshot(rule, _to_int(rng.get("绑定版本号"), 1))

        page_no = _to_int(values.get("页码"), _to_int(rng.get("当前页码"), 0) + 1)
        page_param = str(snapshot.get("页码参数名") or "page")
        page_url = str(values.get("页面URL") or "").strip() or (
            f"{rule.get('目标站点', '')}?{page_param}={page_no}"
        )
        fingerprint = str(values.get("页面指纹") or "").strip() or f"fp-{page_url}"
        result_status = str(values.get("结果状态") or "成功").strip()
        if result_status not in PAGE_RESULT_STATUSES:
            return None, f"分页结果状态「{result_status}」不在允许范围：{'、'.join(PAGE_RESULT_STATUSES)}"
        total_signal = values.get("总数信号")

        # URL过滤与抓取模块：过滤重复页面后再写入分页结果
        pages = [row for row in store.rows(PAGES_MODULE) if row.get("范围编号") == rng["范围编号"]]
        if any(row.get("页面URL") == page_url for row in pages):
            return {
                "范围编号": rng["范围编号"],
                "页码": page_no,
                "页面URL": page_url,
                "重复过滤": True,
                "绑定版本号": rng["绑定版本号"],
                "来源": str(values.get("来源") or "URL过滤与抓取"),
                "处理结果": "重复页面已过滤",
            }, "重复页面已过滤，不再写入分页结果"

        # 连续重复页：指纹与上一页一致，立即结束本次分页
        if rng.get("上一页指纹") and fingerprint == rng["上一页指纹"]:
            rng["连续重复页数"] = _to_int(rng.get("连续重复页数"), 0) + 1
            page = self._append_page(rng, page_no, page_url, fingerprint, result_status,
                                     "与上一页指纹一致，判定连续重复页", values)
            rng["当前页码"] = page_no
            self._finish_range(rng, rule, snapshot, "连续重复页",
                               f"第{page_no}页与上一页指纹一致，连续重复页立即结束本次分页")
            return page, f"连续重复页，本次分页立即结束：{rng['范围编号']}"

        page = self._append_page(rng, page_no, page_url, fingerprint, result_status, "", values)
        rng["当前页码"] = page_no
        rng["上一页指纹"] = fingerprint
        rng["连续重复页数"] = 0

        # 顺着取数逻辑定位：先页结果，再总数信号；冲突时保留优先级（终止优先）
        empty_page = result_status in ("空态", "没有数据")
        locator_broken = bool(snapshot.get("总条数定位器")) and total_signal in (None, "")
        if empty_page and locator_broken:
            # 冲突边界：空页要求终止、定位器失效无法证实结束，两种条件同时成立
            rng["冲突标记"] = True
            rng["范围版本号"] = _to_int(rng.get("范围版本号"), 1) + 1
            rng["版本来源"] = "冲突改版"

        if result_status in TERMINAL_PAGE_STATUSES:
            detail = TERMINAL_REASON_DETAIL[result_status]
            if rng["冲突标记"]:
                detail += "；空页与总数定位器失效同时发生，按终止优先结束"
            self._finish_range(rng, rule, snapshot, result_status, detail)
            return page, f"分页抓取{result_status}，范围{rng['范围编号']}已结束：{rng['停止依据']}"

        if total_signal not in (None, ""):
            total_count = _to_int(total_signal, -1)
            page_size = max(_to_int(values.get("每页条数"), 20), 1)
            if total_count >= 0:
                total_pages = max(ceil(total_count / page_size), 1)
                if total_pages < _to_int(rng["结束页码"], total_pages):
                    # 总数信号只收紧不扩大，旧作业范围不因信号或规则变更而扩大
                    rng["结束页码"] = total_pages
                if page_no >= _to_int(rng["结束页码"], page_no):
                    self._finish_range(rng, rule, snapshot, "总数信号耗尽",
                                       f"总数{total_count}条共{total_pages}页，当前第{page_no}页已覆盖",
                                       status="已完成")
                    return page, f"总数信号耗尽，范围{rng['范围编号']}完成：{rng['停止依据']}"
        elif page_no >= _to_int(rng["结束页码"], page_no):
            self._finish_range(rng, rule, snapshot, "到达最大页码",
                               f"第{page_no}页到达绑定版本最大页码{rng['结束页码']}", status="已完成")
            return page, f"到达最大页码，范围{rng['范围编号']}完成：{rng['停止依据']}"

        return page, f"第{page_no}页结果已写入，范围{rng['范围编号']}继续"

    def _append_page(
        self,
        rng: dict[str, Any],
        page_no: int,
        page_url: str,
        fingerprint: str,
        result_status: str,
        note: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        rows = store.rows(PAGES_MODULE)
        page = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "范围编号": rng["范围编号"],
            "页码": page_no,
            "页面URL": page_url,
            "页面指纹": fingerprint,
            "结果状态": result_status,
            "原因说明": note or ("抓取成功" if result_status == "成功" else TERMINAL_REASON_DETAIL.get(result_status, "")),
            "重复过滤": False,
            "绑定版本号": rng["绑定版本号"],
            "来源": str(values.get("来源") or "URL过滤与抓取"),
            "处理结果": "待处理",
        }
        rows.append(page)
        return page

    def _find_rule(self, rule_code: Any) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("规则编号") == rule_code:
                return row
        return None

    def _finish_range(
        self,
        rng: dict[str, Any],
        rule: dict[str, Any],
        snapshot: dict[str, Any],
        reason: str,
        detail: str,
        *,
        status: str = "已终止",
    ) -> None:
        """结束无效范围：停止依据由翻页规则、页码参数名和总条数定位器共同解释。"""
        rng["状态"] = status
        rng["停止原因码"] = reason
        rng["停止依据"] = (
            f"翻页规则{rule.get('规则编号')}@v{rng.get('绑定版本号')}·"
            f"页码参数[{snapshot.get('页码参数名') or '未配置'}]·"
            f"总条数定位器[{snapshot.get('总条数定位器') or '未配置'}]·{detail}"
        )

    # ---------- 终止信号 ----------
    def terminate_range(self, range_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上报终止信号：重复终止信号不重复计数。"""
        rng = store.find(RANGES_MODULE, range_id)
        if rng is None:
            return None, f"翻页采集范围 {range_id} 不存在或已归档"
        signal_id = str(values.get("信号标识") or "").strip() or f"manual-{_now()}"
        recorded = rng.setdefault("已记录终止信号", [])
        if signal_id in recorded:
            return rng, f"重复终止信号{signal_id}已忽略，不重复计数（当前{rng.get('终止信号数', 0)}次）"
        recorded.append(signal_id)
        rng["终止信号数"] = _to_int(rng.get("终止信号数"), 0) + 1
        if rng.get("状态") == "进行中":
            rule = self._find_rule(rng.get("规则编号"))
            snapshot = self._bound_snapshot(rule, _to_int(rng.get("绑定版本号"), 1)) if rule else {}
            reason = str(values.get("停止原因码") or "外部终止").strip()
            if reason not in STOP_REASONS:
                reason = "外部终止"
            if rule:
                self._finish_range(rng, rule, snapshot, reason,
                                   f"收到终止信号{signal_id}，本次分页结束")
            else:
                rng["状态"] = "已终止"
                rng["停止原因码"] = reason
                rng["停止依据"] = f"收到终止信号{signal_id}，本次分页结束"
        return rng, f"终止信号{signal_id}已记录（第{rng['终止信号数']}次），范围{rng['范围编号']}已结束"

    # ---------- 页面落地侧清单 ----------
    def list_landing(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(LANDING_MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("范围编号", "")) or keyword in str(row.get("规则编号", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def write_landing(self, range_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """处理后的业务结果写入页面落地侧清单，翻页采集范围保留在同一记录。"""
        rng = store.find(RANGES_MODULE, range_id)
        if rng is None:
            return None, f"翻页采集范围 {range_id} 不存在或已归档"
        biz_result = str(values.get("业务结果") or "").strip()
        if not biz_result:
            return None, "缺少必填字段：业务结果"
        process_result = str(values.get("处理结果") or "已处理").strip()
        rows = store.rows(LANDING_MODULE)
        landing = next((row for row in rows if row.get("范围编号") == rng["范围编号"]), None)
        if landing is None:
            landing = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "范围编号": rng["范围编号"],
                "规则编号": rng["规则编号"],
                "绑定版本号": rng["绑定版本号"],
                "业务结果": biz_result,
                "处理结果": process_result,
                "同步时间": _now(),
                "关联刷新": ["翻页采集范围", "分页结果"],
            }
            rows.append(landing)
        else:
            landing["业务结果"] = biz_result
            landing["处理结果"] = process_result
            landing["同步时间"] = _now()
        # 处理结果同步到页面落地侧清单，其他业务记录一并刷新
        rng["处理结果"] = process_result
        for page in store.rows(PAGES_MODULE):
            if page.get("范围编号") == rng["范围编号"]:
                page["处理结果"] = process_result
        return landing, (
            f"处理结果已写入页面落地侧清单，翻页采集范围{rng['范围编号']}保留在同一记录，"
            "关联业务记录一并刷新"
        )
