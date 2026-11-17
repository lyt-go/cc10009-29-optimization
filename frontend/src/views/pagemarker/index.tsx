import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/pagemarker'
const columns = [
  "规则编号",
  "目标站点",
  "翻页类型",
  "起始页码",
  "最大页码",
  "页码参数名",
  "偏移量参数",
  "总条数定位器",
  "当前版本号"
]
const actions = [
  "提交规则验证",
  "启用规则",
  "废弃规则"
]
const statuses = [
  "草稿",
  "规则验证中",
  "已启用",
  "已废弃"
]
const paramFields = [
  "翻页类型",
  "起始页码",
  "最大页码",
  "页码参数名",
  "偏移量参数",
  "总条数定位器"
]
const rangeColumns = [
  "范围编号",
  "规则编号",
  "作业编号",
  "绑定版本号",
  "版本来源",
  "页码区间",
  "当前页码",
  "状态",
  "停止原因码",
  "终止信号数",
  "冲突标记",
  "停止依据"
]
const landingColumns = [
  "范围编号",
  "规则编号",
  "绑定版本号",
  "业务结果",
  "处理结果",
  "同步时间"
]

type Row = Record<string, string | number | boolean | null>

export default function PagemarkerView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [ranges, setRanges] = useState<Row[]>([])
  const [rangeTotal, setRangeTotal] = useState(0)
  const [landing, setLanding] = useState<Row[]>([])
  const [landingTotal, setLandingTotal] = useState(0)
  const [versions, setVersions] = useState<Row[]>([])
  const [versionRule, setVersionRule] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [ruleResp, rangeResp, landingResp] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/ranges`),
        request(`${ENDPOINT}/landing`),
      ])
      if (!ruleResp.ok || !rangeResp.ok || !landingResp.ok) {
        throw new Error('翻页管理数据读取失败')
      }
      const rulePayload = await ruleResp.json()
      const rangePayload = await rangeResp.json()
      const landingPayload = await landingResp.json()
      setRows(rulePayload.items ?? [])
      setTotal(rulePayload.total ?? 0)
      setRanges(rangePayload.items ?? [])
      setRangeTotal(rangePayload.total ?? 0)
      setLanding(landingPayload.items ?? [])
      setLandingTotal(landingPayload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '翻页管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function post(path: string, values: Record<string, unknown>, fallback: string) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json().catch(() => ({}))
      setNoticeMessage(typeof payload.message === 'string' ? payload.message : fallback)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : fallback)
    }
  }

  async function runAction(action: string, row: Row) {
    await post(`${ENDPOINT}/${row.id}/actions`, { action }, '翻页管理动作未生效，请稍后重试')
  }

  async function showVersions(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/versions`)
      if (!response.ok) {
        throw new Error('规则版本历史读取失败')
      }
      const payload = await response.json()
      setVersions(payload.items ?? [])
      setVersionRule(String(row['规则编号'] ?? ''))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '规则版本历史读取失败')
    }
  }

  async function changeParams(row: Row) {
    const field = window.prompt(`要变更的规则参数（${paramFields.join('、')}）`, '最大页码')
    if (!field || !paramFields.includes(field)) {
      if (field) setErrorMessage(`参数「${field}」不在可变范围：${paramFields.join('、')}`)
      return
    }
    const value = window.prompt(`规则${row['规则编号']} 的新${field}`, String(row[field] ?? ''))
    if (value === null) return
    await post(`${ENDPOINT}/${row.id}/versions`, { [field]: value }, '规则参数变更失败')
  }

  async function submitRange(row: Row) {
    const job = window.prompt(`为规则${row['规则编号']} 提交翻页采集范围，请输入作业编号`, 'JOB-')
    if (!job) return
    await post(`${ENDPOINT}/${row.id}/ranges`, { 作业编号: job }, '翻页采集范围提交失败')
  }

  async function recordPage(row: Row, resultStatus: string) {
    const nextPage = Number(row['当前页码'] ?? 0) + 1
    await post(`${ENDPOINT}/ranges/${row.id}/pages`, {
      页码: nextPage,
      结果状态: resultStatus,
      来源: 'URL过滤与抓取',
    }, '分页结果写入失败')
  }

  async function terminateRange(row: Row) {
    // 信号标识按范围固定：重复点击可验证重复终止信号不重复计数
    await post(`${ENDPOINT}/ranges/${row.id}/terminate`, {
      信号标识: `sig-${row['范围编号']}`,
    }, '终止信号上报失败')
  }

  async function writeLanding(row: Row) {
    const biz = window.prompt(`范围${row['范围编号']} 的业务结果`, `${row['作业编号']} 落地数据`)
    if (!biz) return
    await post(`${ENDPOINT}/ranges/${row.id}/landing`, {
      业务结果: biz,
      处理结果: '已处理',
    }, '页面落地侧清单写入失败')
  }

  const stats = [
    { label: '翻页规则总数', value: total },
    { label: '已启用规则', value: rows.filter((row) => row.status === '已启用').length },
    { label: '进行中范围', value: ranges.filter((row) => row['状态'] === '进行中').length },
    { label: '已终止范围', value: ranges.filter((row) => row['状态'] === '已终止').length },
  ]

  return (
    <section className="page" data-module="pagemarker">
      <header className="page-head">
        <div>
          <h2>翻页管理管理</h2>
          <p className="page-desc">管理列表页的分页抓取规则，规则参数变化产生新版本，未受影响作业复用原范围。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('翻页规则登记入口尚未接入审批流')}>
            登记翻页规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出翻页管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void changeParams(row)}>参数变更</button>
                <button className="link" type="button" onClick={() => void showVersions(row)}>版本历史</button>
                <button className="link" type="button" onClick={() => void submitRange(row)}>提交采集范围</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无翻页管理数据，可先登记翻页规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条翻页管理记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
        {noticeMessage && <span>{noticeMessage}</span>}
      </footer>

      {versions.length > 0 && (
        <section className="page" data-module="pagemarker-versions">
          <header className="page-head">
            <div>
              <h2>规则版本历史：{versionRule}</h2>
              <p className="page-desc">参数快照、变更说明与生成时间都可追溯；复用原范围时保留绑定版本。</p>
            </div>
            <div className="page-actions">
              <button className="btn ghost" type="button" onClick={() => setVersions([])}>收起</button>
            </div>
          </header>
          <table className="data-table">
            <thead>
              <tr>
                <th>版本号</th>
                <th>变更说明</th>
                <th>生成时间</th>
                <th>来源</th>
                <th>参数快照</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((item) => (
                <tr key={String(item['版本号'])}>
                  <td>v{String(item['版本号'])}</td>
                  <td>{String(item['变更说明'] ?? '—')}</td>
                  <td>{String(item['生成时间'] ?? '—')}</td>
                  <td>{String(item['来源'] ?? '—')}</td>
                  <td>{JSON.stringify(item['参数快照'] ?? {})}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="page" data-module="pagemarker-ranges">
        <header className="page-head">
          <div>
            <h2>翻页采集范围</h2>
            <p className="page-desc">重复提交按幂等只保留一次；空态、没有数据、无效页、异常、失败、重试中断都会结束无效范围并说明停止依据。</p>
          </div>
        </header>
        <table className="data-table">
          <thead>
            <tr>
              {rangeColumns.map((column) => <th key={column}>{column}</th>)}
              <th>可执行动作</th>
            </tr>
          </thead>
          <tbody>
            {ranges.map((row) => (
              <tr key={String(row.id)}>
                <td>{row['范围编号'] ?? '—'}</td>
                <td>{row['规则编号'] ?? '—'}</td>
                <td>{row['作业编号'] ?? '—'}</td>
                <td>v{String(row['绑定版本号'] ?? '—')}</td>
                <td>{row['版本来源'] ?? '—'}</td>
                <td>{`${row['起始页码'] ?? '—'} ~ ${row['结束页码'] ?? '—'}`}</td>
                <td>{row['当前页码'] ?? '—'}</td>
                <td>{row['状态'] ?? '—'}</td>
                <td>{row['停止原因码'] ?? '—'}</td>
                <td>{row['终止信号数'] ?? 0}</td>
                <td>{row['冲突标记'] ? '是' : '否'}</td>
                <td>{row['停止依据'] ?? '—'}</td>
                <td className="row-actions">
                  {row['状态'] === '进行中' && (
                    <>
                      <button className="link" type="button" onClick={() => void recordPage(row, '成功')}>上报成功页</button>
                      <button className="link" type="button" onClick={() => void recordPage(row, '空态')}>上报空态页</button>
                      <button className="link" type="button" onClick={() => void terminateRange(row)}>终止信号</button>
                    </>
                  )}
                  <button className="link" type="button" onClick={() => void writeLanding(row)}>写入落地清单</button>
                </td>
              </tr>
            ))}
            {ranges.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={rangeColumns.length + 1}>暂无翻页采集范围，可在规则行上提交采集范围</td>
              </tr>
            )}
          </tbody>
        </table>
        <footer className="page-foot">
          <span>共 {rangeTotal} 条翻页采集范围</span>
        </footer>
      </section>

      <section className="page" data-module="pagemarker-landing">
        <header className="page-head">
          <div>
            <h2>页面落地侧清单</h2>
            <p className="page-desc">处理后的业务结果写入落地清单，翻页采集范围保留在同一记录，处理结果同步后关联记录一并刷新。</p>
          </div>
        </header>
        <table className="data-table">
          <thead>
            <tr>
              {landingColumns.map((column) => <th key={column}>{column}</th>)}
            </tr>
          </thead>
          <tbody>
            {landing.map((row) => (
              <tr key={String(row.id)}>
                {landingColumns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              </tr>
            ))}
            {landing.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={landingColumns.length}>暂无页面落地记录，可在范围行上写入落地清单</td>
              </tr>
            )}
          </tbody>
        </table>
        <footer className="page-foot">
          <span>共 {landingTotal} 条页面落地记录</span>
        </footer>
      </section>
    </section>
  )
}
