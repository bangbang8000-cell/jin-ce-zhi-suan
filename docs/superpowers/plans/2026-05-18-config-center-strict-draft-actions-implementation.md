# Config Center Strict Draft Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all config-center execute/test actions run against the current unsaved draft, while preserving the existing semantics of save/reload/stop actions.

**Architecture:** Keep the change minimal and incremental. First add backend runtime-config compatibility for the endpoints that still read saved config (`/api/llm/ping` and `/api/webhook/test`), then unify the config-center frontend around a lightweight strict-draft action registry that reuses the existing `getConfigDraftStrict()` and `ensureDraftActionRequirements()` helpers.

**Tech Stack:** Python, FastAPI, Pydantic, plain JavaScript in `dashboard.html`, pytest

---

## File Map

- Modify: `d:\04.量化\jin-ce-zhi-suan\server.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\webhook_notifier.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_dashboard_history_sync_stock_list_ui_regression.py`
- Add: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_config_center_runtime_action_server_regression.py`

### Task 1: Lock The Backend Runtime-Config Contracts With Failing Tests

**Files:**
- Add: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_config_center_runtime_action_server_regression.py`
- Modify later: `d:\04.量化\jin-ce-zhi-suan\server.py`
- Modify later: `d:\04.量化\jin-ce-zhi-suan\src\utils\webhook_notifier.py`

- [ ] **Step 1: Write the failing tests for `LLM ping` and `Webhook test` runtime draft support**

```python
import asyncio

import server


def test_api_llm_ping_uses_runtime_config_scope(monkeypatch):
    # 配置中心未保存草稿时，LLM 连通性测试也必须按本次草稿执行。
    captured = {}

    monkeypatch.setattr(
        server,
        "_probe_llm_connectivity",
        lambda prompt="", scenario="", scope="unified", update_cache=True, runtime_config=None: {
            "status": "success",
            "ok": True,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "scope": scope,
            "active_sources": {"model": "draft-model"},
            "runtime_config_seen": runtime_config,
        },
    )

    req = server.LlmConnectivityTestRequest(
        scenario="config_center_data_provider",
        scope="data_provider",
        config={
            "data_provider": {
                "llm_provider": "openai",
                "llm_model": "draft-model",
                "llm_api_key": "draft-key",
                "llm_api_url": "http://draft-llm",
            }
        },
    )

    result = asyncio.run(server.api_llm_ping(req))

    assert result["status"] == "success"
    assert result["ok"] is True
    assert result["runtime_config_seen"]["data_provider"]["llm_model"] == "draft-model"


def test_api_webhook_test_uses_runtime_config(monkeypatch):
    # Webhook 测试必须支持读取当前草稿中的 webhook_notification 配置。
    seen = {}

    async def _fake_test_delivery(stock_code="000001.SZ", event_type="system", data=None, config_section=None):
        seen["config_section"] = config_section
        return {
            "ok": True,
            "summary": {"total": 1, "success": 1, "failed": 0},
            "details": [{"channel": "generic", "ok": True}],
        }

    monkeypatch.setattr(server.webhook_notifier, "test_delivery", _fake_test_delivery)

    req = server.WebhookTestRequest(
        event_type="system",
        stock_code="600000.SH",
        msg="draft webhook test",
        config={
            "webhook_notification": {
                "enabled": True,
                "webhook_urls": ["http://draft-webhook.test/hook"],
            }
        },
    )

    result = asyncio.run(server.api_webhook_test(req))

    assert result["status"] == "success"
    assert seen["config_section"]["enabled"] is True
    assert seen["config_section"]["webhook_urls"] == ["http://draft-webhook.test/hook"]
```

- [ ] **Step 2: Run the new tests to verify they fail for the right reason**

Run:

```bash
python -m pytest tests/unit/test_config_center_runtime_action_server_regression.py -v
```

Expected:

- `LlmConnectivityTestRequest` does not accept `config`, or
- `_probe_llm_connectivity()` does not accept `runtime_config`, or
- `WebhookTestRequest` does not accept `config`, or
- `WebhookNotifier.test_delivery()` does not accept `config_section`

- [ ] **Step 3: Write the minimal backend implementation to pass the tests**

`server.py`

```python
class WebhookTestRequest(BaseModel):
    # 可选事件类型，默认使用 system 以保证大多数通道可接收。
    event_type: Optional[str] = "system"
    # 可选股票代码，仅用于消息展示与上下文定位。
    stock_code: Optional[str] = "000001.SZ"
    # 可选测试消息，便于人工区分不同测试批次。
    msg: Optional[str] = None
    # 配置中心未保存草稿时，允许按本次 webhook_notification 配置执行测试。
    config: Optional[dict] = None


class LlmConnectivityTestRequest(BaseModel):
    # 可选场景标记，仅用于日志与提示，不影响模型调用主流程。
    scenario: Optional[str] = "strategy_codegen"
    # 可选测试提示词，默认使用轻量化探活提示词。
    prompt: Optional[str] = None
    # 可选配置域：unified / evolution / strategy_manager / data_provider
    scope: Optional[str] = "unified"
    # 配置中心测试模型时允许使用当前草稿，而不是仅依赖已保存配置。
    config: Optional[dict] = None
```

`server.py`

```python
@app.post("/api/llm/ping")
async def api_llm_ping(req: Optional[LlmConnectivityTestRequest] = None):
    """统一模型层连通性测试接口，用于配置中心快速探活。"""
    provider = ""
    model = ""
    try:
        req_prompt = str(getattr(req, "prompt", "") or "").strip() if req is not None else ""
        req_scenario = str(getattr(req, "scenario", "strategy_codegen") or "strategy_codegen") if req is not None else "strategy_codegen"
        req_scope = str(getattr(req, "scope", "unified") or "unified") if req is not None else "unified"
        runtime_config = _build_runtime_test_config(getattr(req, "config", None)) if req is not None else {}
        payload = _probe_llm_connectivity(
            prompt=req_prompt,
            scenario=req_scenario,
            scope=req_scope,
            update_cache=True,
            runtime_config=runtime_config,
        )
        provider = str(payload.get("provider", "") or "")
        model = str(payload.get("model", "") or "")
        return payload
    except Exception as e:
        logger.error(f"/api/llm/ping failed: {e}", exc_info=True)
        return {"status": "error", "ok": False, "msg": str(e), "provider": provider, "model": model}


def _probe_llm_connectivity(
    prompt: str = "",
    scenario: str = "strategy_codegen",
    scope: str = "unified",
    update_cache: bool = True,
    runtime_config: Optional[dict] = None,
) -> Dict[str, Any]:
    """执行一次真实 LLM 探活，并可选择回写缓存。"""
    cfg_view = RuntimeConfigView(runtime_config) if isinstance(runtime_config, dict) and runtime_config else ConfigLoader.reload()
    llm_client = build_unified_llm_client(cfg_view, scope=scope)
```

`server.py`

```python
@app.post("/api/webhook/test")
async def api_webhook_test(req: WebhookTestRequest):
    try:
        event_type = str(req.event_type or "system").strip() or "system"
        stock_code = str(req.stock_code or "000001.SZ").strip().upper() or "000001.SZ"
        msg_text = str(req.msg or "").strip() or "webhook test message"
        runtime_cfg = _build_runtime_test_config(req.config)
        config_section = runtime_cfg.get("webhook_notification", {}) if isinstance(runtime_cfg, dict) else {}
        result = await webhook_notifier.test_delivery(
            stock_code=stock_code,
            event_type=event_type,
            data={
                "msg": msg_text,
                "source": "api_webhook_test",
                "trigger_at": datetime.now().isoformat(timespec="seconds")
            },
            config_section=config_section if isinstance(config_section, dict) else None,
        )
```

`src/utils/webhook_notifier.py`

```python
class WebhookNotifier:
    def __init__(self):
        self._last_sent = {}
        self._cfg_cache = {}
        self._cfg_cache_at = 0.0
        self._failed_lock = threading.Lock()
        self._retry_lock = None
        self._last_retry_ts = 0.0
        self._strategy_name_map_cache = {}
        self._strategy_name_map_at = 0.0

    async def test_delivery(self, stock_code="000001.SZ", event_type="system", data=None, config_section=None):
        # 配置中心测试允许显式传入当前草稿，否则继续读取已生效配置。
        cfg = config_section if isinstance(config_section, dict) and config_section else self._load_cfg()
        if not bool(cfg.get("enabled", False)):
            return {
                "ok": False,
                "summary": {"total": 0, "success": 0, "failed": 0},
                "details": [],
                "msg": "webhook_notification.enabled=false"
            }
```

- [ ] **Step 4: Run the tests again to verify they pass**

Run:

```bash
python -m pytest tests/unit/test_config_center_runtime_action_server_regression.py -v
```

Expected:

- `2 passed`

- [ ] **Step 5: Commit the backend compatibility slice**

```bash
git add tests/unit/test_config_center_runtime_action_server_regression.py server.py src/utils/webhook_notifier.py
git commit -m "feat: support runtime config for config center test actions"
```

### Task 2: Lock The Config-Center UI Contract With Failing Regression Tests

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_dashboard_history_sync_stock_list_ui_regression.py`
- Modify later: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`

- [ ] **Step 1: Extend the dashboard regression test with the full strict-draft scope**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = ROOT / "dashboard.html"


def _read_dashboard_html() -> str:
    # 统一读取 dashboard 源码，避免每个断言重复打开文件。
    return DASHBOARD_PATH.read_text(encoding="utf-8")


def test_dashboard_config_center_actions_use_strict_draft_runtime():
    text = _read_dashboard_html()

    assert 'function getConfigDraftStrict()' in text
    assert 'function ensureDraftActionRequirements(' in text
    assert 'function getConfigCenterDraftActionRegistry()' in text
    assert 'function runConfigCenterDraftAction(' in text
    assert '使用当前草稿执行数据源连通性测试' in text
    assert '使用当前草稿执行模型连通性测试' in text
    assert '使用当前草稿执行 Webhook 测试' in text
    assert '使用当前草稿执行增量同步' in text
    assert '使用当前草稿执行股票池更新' in text
```

- [ ] **Step 2: Run the single dashboard regression test and verify it fails**

Run:

```bash
python -m pytest tests/unit/test_dashboard_history_sync_stock_list_ui_regression.py::test_dashboard_config_center_actions_use_strict_draft_runtime -v
```

Expected:

- FAIL because the registry functions or log strings do not exist yet

- [ ] **Step 3: Commit the failing UI guard before editing `dashboard.html`**

```bash
git add tests/unit/test_dashboard_history_sync_stock_list_ui_regression.py
git commit -m "test: lock config center strict draft ui contract"
```

### Task 3: Implement The Lightweight Strict-Draft Action Registry In `dashboard.html`

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
- Test: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_dashboard_history_sync_stock_list_ui_regression.py`

- [ ] **Step 1: Add a lightweight registry and a shared draft-action runner**

```javascript
function getConfigCenterDraftActionRegistry() {
    // 配置中心执行型按钮统一注册在这里，避免后续再次散落草稿读取逻辑。
    return {
        data_source_connectivity: {
            actionLabel: '数据源连通性测试',
            beforeLog: '使用当前草稿执行数据源连通性测试',
            requiredPaths: ['data_provider.source'],
        },
        llm_connectivity: {
            actionLabel: '模型连通性测试',
            beforeLog: '使用当前草稿执行模型连通性测试',
            requiredPathsByScope: {
                evolution: ['evolution.llm.provider', 'evolution.llm.model'],
                strategy_manager: ['data_provider.strategy_llm_provider', 'data_provider.strategy_llm_model'],
                data_provider: ['data_provider.llm_provider', 'data_provider.llm_model'],
            },
        },
        webhook_test: {
            actionLabel: 'Webhook 测试',
            beforeLog: '使用当前草稿执行 Webhook 测试',
            requiredPaths: ['webhook_notification.enabled'],
        },
        history_sync_run: {
            actionLabel: '增量同步',
            beforeLog: '使用当前草稿执行增量同步',
        },
        history_sync_stock_list_refresh: {
            actionLabel: '股票池更新',
            beforeLog: '使用当前草稿执行股票池更新',
        },
        history_sync_scheduler_start: {
            actionLabel: '启动定时同步',
            beforeLog: '使用当前草稿执行定时同步启动',
        },
    };
}


function runConfigCenterDraftAction(actionLabel, requiredPaths) {
    // 配置中心统一严格草稿入口：只读当前表单，不回退已保存配置。
    const cfg = getConfigDraftStrict();
    ensureDraftActionRequirements(actionLabel, cfg, requiredPaths);
    return cfg;
}
```

- [ ] **Step 2: Refactor data-source connectivity to use the shared runner and pass `config`**

```javascript
async function testSelectedDataSourceConnectivity(sourceOverride = '') {
    const btn = document.getElementById('config-data-source-test-btn');
    const icon = document.getElementById('config-data-source-test-icon');
    const label = document.getElementById('config-data-source-test-label');
    const status = document.getElementById('config-data-source-test-status');
    const registry = getConfigCenterDraftActionRegistry();
    const setStatus = (text, level = 'info') => {
        if (!status) return;
        status.innerText = String(text || '');
        status.className = `text-[10px] ${level === 'ok' ? 'text-emerald-300' : (level === 'error' ? 'text-rose-300' : 'text-slate-400')}`;
    };
    const parsed = collectConfigFromForm();
    const meta = getSelectedDataSourceConnectivityMeta(parsed, sourceOverride);
    const originIconClass = String(meta.iconClass || 'fa-solid fa-plug-circle-check');
    const originLabel = String(meta.label || '测试数据源连通性');
    try {
        if (btn) btn.disabled = true;
        if (icon) icon.className = 'fa-solid fa-spinner fa-spin';
        if (label) label.innerText = String(meta.busyLabel || '连通性校验中...');
        setStatus(String(meta.pendingHint || '正在测试连接，请稍候...'), 'info');
        const cfg = runConfigCenterDraftAction(
            registry.data_source_connectivity.actionLabel,
            registry.data_source_connectivity.requiredPaths,
        );
        logMessage('SYSTEM', registry.data_source_connectivity.beforeLog, 'info');
        const targets = Array.isArray(getByPath(cfg, 'targets', [])) ? getByPath(cfg, 'targets', []) : [];
        const stockCode = String((targets[0] || '000001.SZ')).trim().toUpperCase();
        const payload = {
            source: String(meta.source || 'default'),
            stock_code: stockCode || '000001.SZ',
            auto_detect: true,
            config: cfg,
        };
```

- [ ] **Step 3: Refactor LLM tests and webhook test to use the shared runner and pass `config`**

```javascript
async function testLlmConnectivityByScope(scope, domIdPrefix, scenario) {
    const btn = document.getElementById(`${domIdPrefix}-btn`);
    const icon = document.getElementById(`${domIdPrefix}-icon`);
    const label = document.getElementById(`${domIdPrefix}-label`);
    const status = document.getElementById(`${domIdPrefix}-status`);
    const registry = getConfigCenterDraftActionRegistry();
    const originLabel = label ? String(label.innerText || '') : '';
    const requiredPaths = registry.llm_connectivity.requiredPathsByScope[String(scope || 'unified')] || [];
    const setStatus = (text, level = 'info') => {
        if (!status) return;
        status.innerText = String(text || '');
        status.className = `text-[10px] ${level === 'ok' ? 'text-emerald-300' : (level === 'error' ? 'text-rose-300' : 'text-slate-400')}`;
    };
    try {
        if (btn) btn.disabled = true;
        if (icon) icon.className = 'fa-solid fa-spinner fa-spin';
        if (label) label.innerText = '测试中...';
        setStatus('正在请求统一模型层探活...', 'info');
        const cfg = runConfigCenterDraftAction(registry.llm_connectivity.actionLabel, requiredPaths);
        logMessage('SYSTEM', registry.llm_connectivity.beforeLog, 'info');
        const res = await fetch('/api/llm/ping', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                scenario: String(scenario || 'config_center'),
                scope: String(scope || 'unified'),
                config: cfg,
            })
        });
```

```javascript
async function testWebhookDelivery() {
    const btn = document.getElementById('config-webhook-test-btn');
    const icon = document.getElementById('config-webhook-test-icon');
    const label = document.getElementById('config-webhook-test-label');
    const status = document.getElementById('config-webhook-test-status');
    const registry = getConfigCenterDraftActionRegistry();
    const setStatus = (text, level = 'info') => {
        if (!status) return;
        status.innerText = String(text || '');
        status.className = `text-[10px] ${level === 'ok' ? 'text-emerald-300' : (level === 'error' ? 'text-rose-300' : 'text-slate-400')}`;
    };
    try {
        if (btn) btn.disabled = true;
        if (icon) icon.className = 'fa-solid fa-spinner fa-spin';
        if (label) label.innerText = '发送中...';
        setStatus('正在发送测试消息，请稍候...', 'info');
        const cfg = runConfigCenterDraftAction(
            registry.webhook_test.actionLabel,
            registry.webhook_test.requiredPaths,
        );
        logMessage('SYSTEM', registry.webhook_test.beforeLog, 'info');
        const targets = Array.isArray(getByPath(cfg, 'targets', [])) ? getByPath(cfg, 'targets', []) : [];
        const stockCode = String((targets[0] || '000001.SZ')).trim().toUpperCase() || '000001.SZ';
        const payload = {
            event_type: 'system',
            stock_code: stockCode,
            msg: `配置中心测试消息 ${new Date().toLocaleString()}`,
            config: cfg,
        };
```

- [ ] **Step 4: Refactor scheduler start to use strict draft mode; leave stop path unchanged**

```javascript
async function toggleHistorySyncScheduler() {
    const nextOn = !historySyncSchedulerRunning;
    try {
        if (nextOn) {
            const cfg = getConfigDraftStrict();
            const timeMode = String(getByPath(cfg, 'history_sync.time_mode', 'lookback') || 'lookback');
            const requiredPaths = [
                'data_provider.source',
                'history_sync.write_mode',
                'history_sync.direct_db_source',
                'history_sync.interval_minutes',
                'history_sync.scheduler_start_time',
            ];
            if (timeMode === 'custom') {
                requiredPaths.push('history_sync.custom_start_time');
                requiredPaths.push('history_sync.custom_end_time');
            } else {
                requiredPaths.push('history_sync.lookback_days');
            }
            ensureDraftActionRequirements('定时同步启动', cfg, requiredPaths);
            logMessage('SYSTEM', '使用当前草稿执行定时同步启动', 'info');
            const providerSource = String(getByPath(cfg, 'data_provider.source', 'default') || 'default');
            const writeMode = String(getByPath(cfg, 'history_sync.write_mode', 'api') || 'api');
            const directDbSource = String(getByPath(cfg, 'history_sync.direct_db_source', 'mysql') || 'mysql');
            const schedulerStartTime = String(getByPath(cfg, 'history_sync.scheduler_start_time', '09:30') || '09:30').trim();
            const intervalMinutes = Number(getByPath(cfg, 'history_sync.interval_minutes', 60) || 60);
            const tablesCfg = getByPath(cfg, 'history_sync.tables', DEFAULT_HISTORY_SYNC_TABLES);
            const tables = normalizeHistorySyncTables(tablesCfg);
            const res = await fetch('/api/history_sync/scheduler/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    interval_minutes: Math.max(1, Math.floor(intervalMinutes)),
                    scheduler_start_time: schedulerStartTime,
                    provider_source: providerSource,
                    write_mode: writeMode,
                    direct_db_source: directDbSource,
                    tables,
                })
            });
        } else {
            const res = await fetch('/api/history_sync/scheduler/stop', { method: 'POST' });
            const data = await res.json();
            if (!res.ok || data.status !== 'success') throw new Error(data.msg || '停止失败');
            logMessage('SYSTEM', '定时增量同步已关闭', 'warning');
        }
    } catch (e) {
        logMessage('SYSTEM', `定时同步切换失败: ${e}`, 'danger');
    } finally {
        await refreshHistorySyncStatus();
    }
}
```

- [ ] **Step 5: Run the dashboard regression tests and verify they pass**

Run:

```bash
python -m pytest tests/unit/test_dashboard_history_sync_stock_list_ui_regression.py -v
```

Expected:

- All tests in the file pass

- [ ] **Step 6: Commit the frontend strict-draft registry**

```bash
git add dashboard.html tests/unit/test_dashboard_history_sync_stock_list_ui_regression.py
git commit -m "feat: unify config center draft actions"
```

### Task 4: Run The Focused End-To-End Regression Set

**Files:**
- Modify if needed: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
- Modify if needed: `d:\04.量化\jin-ce-zhi-suan\server.py`
- Modify if needed: `d:\04.量化\jin-ce-zhi-suan\src\utils\webhook_notifier.py`

- [ ] **Step 1: Run the focused regression set**

Run:

```bash
python -m pytest tests/unit/test_config_center_runtime_action_server_regression.py tests/unit/test_dashboard_history_sync_stock_list_ui_regression.py tests/test_history_sync_config.py tests/unit/test_server_consistency_routes_regression.py -v
```

Expected:

- All selected tests pass

- [ ] **Step 2: Check diagnostics for edited files**

Run diagnostics for:

- `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
- `d:\04.量化\jin-ce-zhi-suan\server.py`
- `d:\04.量化\jin-ce-zhi-suan\src\utils\webhook_notifier.py`

Expected:

- No newly introduced diagnostics

- [ ] **Step 3: Commit the verified slice**

```bash
git add dashboard.html server.py src/utils/webhook_notifier.py tests/unit/test_config_center_runtime_action_server_regression.py tests/unit/test_dashboard_history_sync_stock_list_ui_regression.py tests/test_history_sync_config.py
git commit -m "feat: apply strict draft execution across config center"
```

## Self-Review Checklist

- `Scope` coverage:
  - Data source connectivity: Task 3
  - Three LLM connectivity buttons: Task 1 + Task 3
  - Webhook test: Task 1 + Task 3
  - History sync run / stock list refresh: existing behavior preserved and folded into Task 3 registry
  - Scheduler start vs stop boundary: Task 3
- No placeholder tasks remain; each task includes exact files, commands, and code snippets.
- Types stay consistent across tasks:
  - `LlmConnectivityTestRequest.config`
  - `WebhookTestRequest.config`
  - `_probe_llm_connectivity(..., runtime_config=...)`
  - `WebhookNotifier.test_delivery(..., config_section=...)`
