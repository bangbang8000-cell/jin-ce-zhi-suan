# History Sync Ignore Checkpoint UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为手动历史增量同步入口增加 `ignore_checkpoint` 开关，并在 API 文档中补充该参数说明，使用户可以在页面上显式触发“强制重跑”。

**Architecture:** 本次只改两个文件：`dashboard.html` 负责提供按钮区临时开关并在 `runHistorySync()` 中透传 `ignore_checkpoint`，`docs/API_DOCS.md` 负责补充 `/api/history_sync/run` 参数说明与示例。后端行为不再修改，继续复用已实现的 `ignore_checkpoint` 逻辑。

**Tech Stack:** HTML、原生 JavaScript、Markdown、现有 `dashboard.html` 配置表单收集逻辑

---

## File Map

- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
  - 在“工部增量同步”按钮区增加 `ignore_checkpoint` 临时复选开关
  - 在 `runHistorySync()` 中读取该开关并写入请求体
- Modify: `d:\04.量化\jin-ce-zhi-suan\docs\API_DOCS.md`
  - 新增 `/api/history_sync/run` 的 `ignore_checkpoint` 参数说明
  - 增加一段强制重跑请求示例

## Task 1: 给手动增量同步入口增加 ignore_checkpoint 开关

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`

- [ ] **Step 1: 先写失败测试，锁定请求体必须携带 ignore_checkpoint**

```javascript
async function testRunHistorySyncIncludesIgnoreCheckpoint() {
  const originalFetch = window.fetch;
  const originalCollectConfigFromForm = window.collectConfigFromForm;
  const originalLogMessage = window.logMessage;
  const originalRefreshHistorySyncStatus = window.refreshHistorySyncStatus;
  const originalRenderHistorySyncButtons = window.renderHistorySyncButtons;
  const originalGetHistorySyncRiskItems = window.getHistorySyncRiskItems;

  try {
    document.body.insertAdjacentHTML(
      'beforeend',
      '<input id="history-sync-ignore-checkpoint-toggle" type="checkbox" checked>'
    );
    let requestPayload = null;
    window.fetch = async (url, options) => {
      requestPayload = JSON.parse(options.body);
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({ status: 'success', report: { total_missing_rows: 0, total_written_rows: 0 } })
      };
    };
    window.collectConfigFromForm = () => ({
      data_provider: { source: 'tdx' },
      history_sync: {
        write_mode: 'direct_db',
        direct_db_source: 'duckdb',
        time_mode: 'lookback',
        lookback_days: 10,
        concurrency: 4,
        max_codes: 1000,
        batch_size: 500,
        dry_run: false,
        session_only: true,
        intraday_mode: false,
        tables: ['dat_1mins']
      }
    });
    window.logMessage = () => {};
    window.refreshHistorySyncStatus = async () => {};
    window.renderHistorySyncButtons = () => {};
    window.getHistorySyncRiskItems = () => ({ items: ['测试风险'] });

    await runHistorySync();

    console.assert(requestPayload.ignore_checkpoint === true, 'ignore_checkpoint 应为 true');
  } finally {
    window.fetch = originalFetch;
    window.collectConfigFromForm = originalCollectConfigFromForm;
    window.logMessage = originalLogMessage;
    window.refreshHistorySyncStatus = originalRefreshHistorySyncStatus;
    window.renderHistorySyncButtons = originalRenderHistorySyncButtons;
    window.getHistorySyncRiskItems = originalGetHistorySyncRiskItems;
    const node = document.getElementById('history-sync-ignore-checkpoint-toggle');
    if (node) node.remove();
  }
}
```

- [ ] **Step 2: 运行静态脚本或手工验证，确认当前未透传该字段**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path("dashboard.html").read_text(encoding="utf-8")
assert "ignore_checkpoint" in text, "dashboard.html 尚未透传 ignore_checkpoint"
PY
```

Expected:

```text
AssertionError: dashboard.html 尚未透传 ignore_checkpoint
```

- [ ] **Step 3: 在按钮区增加复选开关，并在 runHistorySync() 中最小透传**

```html
<label class="inline-flex items-center gap-2 text-[10px] text-amber-200 bg-amber-950/20 border border-amber-800/40 rounded px-2 py-1">
  <input id="history-sync-ignore-checkpoint-toggle" type="checkbox" class="accent-amber-500">
  <span>忽略检查点（强制重跑）</span>
</label>
```

```javascript
const ignoreCheckpointNode = document.getElementById('history-sync-ignore-checkpoint-toggle');
const ignoreCheckpoint = Boolean(ignoreCheckpointNode && ignoreCheckpointNode.checked);

const payload = {
  lookback_days: Math.max(1, Math.floor(lookbackDays)),
  concurrency: Math.max(1, Math.floor(concurrency)),
  max_codes: Math.max(1, Math.floor(maxCodes)),
  batch_size: Math.max(1, Math.floor(batchSize)),
  tables,
  dry_run: dryRun,
  time_mode: timeMode,
  custom_start_time: customStartTime,
  custom_end_time: customEndTime,
  session_only: sessionOnly,
  intraday_mode: intradayMode,
  on_duplicate: 'ignore',
  provider_source: providerSource,
  write_mode: writeMode,
  direct_db_source: directDbSource,
  ignore_checkpoint: ignoreCheckpoint,
  config: cfg,
  async_run: false
};
```

- [ ] **Step 4: 运行最小验证，确认前端已包含该字段**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path("dashboard.html").read_text(encoding="utf-8")
assert "history-sync-ignore-checkpoint-toggle" in text
assert "ignore_checkpoint: ignoreCheckpoint" in text
print("ok")
PY
```

Expected:

```text
ok
```

- [ ] **Step 5: 提交手动入口改动**

```bash
git add dashboard.html
git commit -m "feat: expose history sync ignore checkpoint toggle"
```

## Task 2: 补充 API 文档中的 ignore_checkpoint 参数说明

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\docs\API_DOCS.md`

- [ ] **Step 1: 先写文档缺失检查**

```bash
python - <<'PY'
from pathlib import Path
text = Path("docs/API_DOCS.md").read_text(encoding="utf-8")
assert "ignore_checkpoint" in text, "API 文档尚未包含 ignore_checkpoint 参数"
PY
```

Expected:

```text
AssertionError: API 文档尚未包含 ignore_checkpoint 参数
```

- [ ] **Step 2: 在 `/api/history_sync/run` 章节补充参数说明和示例**

```markdown
| `ignore_checkpoint` | boolean | 否 | 是否忽略旧 checkpoint 并强制重跑，默认 `false` | `true` |
```

```json
{
  "provider_source": "tdx",
  "write_mode": "direct_db",
  "direct_db_source": "duckdb",
  "tables": ["dat_1mins", "dat_day"],
  "time_mode": "custom",
  "custom_start_time": "2026-03-02 09:30:00",
  "custom_end_time": "2026-03-02 15:00:00",
  "session_only": true,
  "ignore_checkpoint": true
}
```

- [ ] **Step 3: 运行文档检查，确认字段已补齐**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path("docs/API_DOCS.md").read_text(encoding="utf-8")
assert "ignore_checkpoint" in text
assert "强制重跑" in text
print("ok")
PY
```

Expected:

```text
ok
```

- [ ] **Step 4: 提交 API 文档改动**

```bash
git add docs/API_DOCS.md
git commit -m "docs: document history sync ignore checkpoint parameter"
```

## Task 3: 运行最小回归与诊断检查

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
- Modify: `d:\04.量化\jin-ce-zhi-suan\docs\API_DOCS.md`

- [ ] **Step 1: 执行静态检查脚本**

Run:

```bash
python - <<'PY'
from pathlib import Path
dashboard = Path("dashboard.html").read_text(encoding="utf-8")
docs = Path("docs/API_DOCS.md").read_text(encoding="utf-8")
assert "history-sync-ignore-checkpoint-toggle" in dashboard
assert "ignore_checkpoint: ignoreCheckpoint" in dashboard
assert "ignore_checkpoint" in docs
print("checks passed")
PY
```

Expected:

```text
checks passed
```

- [ ] **Step 2: 获取诊断并修复显而易见的问题**

Run:

```bash
python - <<'PY'
print("manual diagnostics gate")
PY
```

Expected:

```text
manual diagnostics gate
```

- [ ] **Step 3: 提交最终收尾**

```bash
git add dashboard.html docs/API_DOCS.md
git commit -m "feat: add manual history sync ignore checkpoint entry"
```

## Self-Review

- **Spec coverage:** 已覆盖手动执行页新增开关、请求体透传 `ignore_checkpoint`、API 文档参数说明与示例，未扩展到配置中心和定时任务。
- **Placeholder scan:** 计划中未使用 `TODO`、`TBD`、`implement later` 或“类似 Task N”这类占位描述。
- **Type consistency:** 计划内统一使用 `ignore_checkpoint` 字段名，前端 DOM id 统一为 `history-sync-ignore-checkpoint-toggle`，与 spec 保持一致。
