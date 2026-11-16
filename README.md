# 跨市场设备服务质量数据平台

该 FastAPI 服务在离线环境下分析不同市场的设备运维事件。演示数据均为本地合成数据，SQLite 是唯一持久化服务。

## 启动

依赖精确版本见 requirements.txt。在预装依赖的环境中运行 uvicorn brand_signal.api:app --host 0.0.0.0 --port 8000。通过 BRAND_SIGNAL_DB 指定数据库文件，默认是 /tmp/brand-signal.sqlite3。运行 python -m brand_signal.demo 可执行一次性端到端演示。

## 数据流程

先登记市场及 IANA 时区、产品和设备安装记录。可选配置来源字段契约、外部设备号映射和服务响应目标。工单及计量事件使用请求键批量提交；每行分别返回接收或拒绝结果，相同请求键和内容重放原结果。工单变更需要预期修订号。按市场本地日期查询分析指标、跨来源对账、告警及服务复核。

发布候选固定某一源修订及预期发布版本。只有源修订和发布版本未变化时才能发布。旧版本、CSV 和 JSON 导出不可变。迟到数据须进入新候选和新发布版本。市场汇总、比较、抽样与溯源均按明确版本读取。

## 安装日期修订

设备生命周期以安装时间（含）和退役时间（不含）为边界。安装日期录错时，分析人员用设备当前修订号做并发校验（POST /devices/{id}/lifecycle）：

- POST /devices/{id}/lifecycle/impact 事前评估：列出受影响的已接收工单、计量事件和工单处置，按天给出指标变化，并指出哪些发布候选会失效、哪些已发布版本会被新版本取代。该调用只读，不改变任何状态。
- POST /devices/{id}/lifecycle 确认修订：写入新的设备修订号、源修订号、备注和逐条事件处置（excluded/restored）。原始工单与计量事件不会被删除或改写。
- GET /devices/{id}/lifecycle 查看每次生命周期变更；GET /devices/{id}/lifecycle/dispositions 追溯某次修订的逐条处置；GET /lifecycle/exclusions/{market} 按日期窗口列出被生命周期排除的已接收事件（可用 source_revision 还原历史口径）。
- 修订后落在生命周期外的原始事件按 excluded_tickets、excluded_telemetry、excluded_ticket_actions 计数并随指标行发布，不再静默计入或消失。对账会把它们列为 event_outside_lifecycle（low 严重度）。
- 已发布版本保持原样，可用 digest/digest_verified 复验；修订后的确认值进入新的发布候选和新版本。GET /publications/{market}/diff/{old}/{new} 返回逐字段变化和两端摘要校验结果。
- 并发修订失败返回 409 stale_device，并在审计日志记录 device_revision_conflict（预期修订号、实际修订号、被拒绝的安装时间）。

## 验证

python -m unittest discover -s tests -v

python -m compileall -q brand_signal

python -m brand_signal.demo

API 错误返回稳定的 error 代码和 HTTP 4xx 状态。空分母表示为 JSON null。输入时间戳必须包含时区偏移，本地日期格式为 YYYY-MM-DD。
