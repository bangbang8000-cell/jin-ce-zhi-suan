# History Sync Ignore Checkpoint UI Design

## 1. Background

当前 `history_sync` 已经支持后端参数 `ignore_checkpoint`，用于在同一组同步参数下强制忽略旧 checkpoint，重新执行本轮同步。

现状问题有两个：

- 手动执行入口 `dashboard.html` 还没有把这个参数透出给用户
- `docs/API_DOCS.md` 还没有说明这个参数的用途和取值语义

这会导致用户虽然可以通过直接构造 HTTP 请求使用该能力，但在日常操作中无法方便触发“强制重跑”，也不容易理解为什么同参执行时会被旧 checkpoint 全部跳过。

## 2. Goals And Non-Goals

### 2.1 Goals

- 在手动执行历史增量同步时，为用户提供显式的 `ignore_checkpoint` 控件
- 在 API 文档中补充 `ignore_checkpoint` 参数说明
- 保持默认行为不变，即不勾选时仍按现有 checkpoint 恢复逻辑执行
- 不破坏现有手动执行、停止同步、状态轮询和日志展示链路

### 2.2 Non-Goals

- 不把 `ignore_checkpoint` 加入全局配置中心
- 不把 `ignore_checkpoint` 接入定时调度任务
- 不修改 checkpoint 签名算法
- 不新增新的后端接口
- 不改变现有 `resume_from_checkpoint` 的默认语义

## 3. Scope

本次改动只涉及以下两个位置：

- `dashboard.html`
- `docs/API_DOCS.md`

本次不涉及以下位置：

- `config.json`
- `server.py` 的调度 payload 构建
- 定时同步页面交互
- checkpoint 文件结构

## 4. Approaches

### 4.1 Recommended Approach

在手动“增量同步”操作区增加一个临时 UI 开关：

- 标签：`忽略检查点（强制重跑）`
- 默认值：关闭
- 仅在点击“增量同步”按钮发起本次请求时读取并透传到 `/api/history_sync/run`

同时在 `docs/API_DOCS.md` 中补充该字段说明。

优点：

- 改动最小
- 用户可立即使用
- 不引入新的全局默认配置风险

缺点：

- 该开关属于“本次执行行为”，不会被长期保存

### 4.2 Rejected Approach: Add To Global Config Form

把 `ignore_checkpoint` 作为 `history_sync` 全局配置项加入配置中心。

不采用原因：

- 容易被误设置为长期默认开启
- 与“本次是否强制重跑”的临时操作语义不匹配
- 本次需求只要求手动入口可用，不需要扩展到全局配置

### 4.3 Rejected Approach: Documentation Only

只更新 API 文档，不改 UI。

不采用原因：

- 用户仍然需要手工构造请求
- 不能解决日常操作入口缺失的问题

## 5. Detailed Design

### 5.1 Dashboard UI

在 `dashboard.html` 的历史增量同步操作区域增加一个布尔开关，显示规则如下：

- 文案：`忽略检查点（强制重跑）`
- 默认未选中
- 视觉层级低于主按钮，高于说明文字
- 说明文案简短提示：勾选后将忽略旧 checkpoint，重新执行当前同步范围

交互规则：

- 用户未勾选时，请求不变
- 用户勾选时，请求体增加 `ignore_checkpoint: true`
- 执行完成或失败后，不自动改写全局配置

### 5.2 Request Payload

`runHistorySync()` 组装请求体时新增字段：

- `ignore_checkpoint`

取值规则：

- 从新 UI 开关读取
- 若控件不存在或读取失败，回退为 `false`

### 5.3 API Documentation

在 `docs/API_DOCS.md` 的 `/api/history_sync/run` 章节中补充：

- 参数名：`ignore_checkpoint`
- 类型：`boolean`
- 必填：否
- 默认值：`false`
- 说明：
  - `false`：继续复用同签名 checkpoint，跳过已完成股票
  - `true`：忽略旧 checkpoint，按当前参数重新执行全部待同步股票

同时补充一个请求示例，展示如何发起强制重跑。

## 6. Data Flow

手动执行路径如下：

1. 用户在 `dashboard.html` 勾选 `忽略检查点（强制重跑）`
2. 点击“增量同步”
3. 前端 `runHistorySync()` 读取该值并写入 `/api/history_sync/run` 请求体
4. 后端沿用现有 `ignore_checkpoint` 逻辑执行
5. 日志与 report 中继续显示 `ignore_checkpoint=True/False`

## 7. Error Handling

- 若 UI 开关值读取失败，前端按 `false` 处理，避免阻断同步
- 若后端返回错误，沿用现有前端错误提示和日志输出，不新增特殊分支
- 若用户未勾选但旧 checkpoint 已完成，仍按现有逻辑显示“本轮无待执行股票”

## 8. Testing Strategy

本次优先做最小有效验证：

- 验证手动执行请求体包含 `ignore_checkpoint`
- 验证默认未勾选时不会错误透传为 `true`
- 验证 API 文档新增字段说明

如果现有前端没有自动化测试基础，则以最小代码级测试或静态检查为主，不为本次小范围功能额外引入新的前端测试框架。

## 9. Risks

- 用户可能误把“强制重跑”理解为永久配置，因此 UI 文案必须强调它只作用于本次手动执行
- 如果按钮区视觉过重，可能误导用户频繁重跑，因此控件应保持辅助级别，不抢主按钮注意力
- 如果后续需要把该参数扩展到定时任务，再单独做设计，不在本次范围内混入

## 10. Acceptance Criteria

满足以下条件则视为完成：

- `dashboard.html` 手动执行入口可勾选 `忽略检查点（强制重跑）`
- 点击“增量同步”后，请求体可正确携带 `ignore_checkpoint`
- 默认情况下不影响原有手动执行行为
- `docs/API_DOCS.md` 已包含 `ignore_checkpoint` 参数说明和示例
- 不改动配置中心和定时任务逻辑
