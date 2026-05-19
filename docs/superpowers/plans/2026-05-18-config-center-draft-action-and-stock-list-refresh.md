# Config Center Draft Action And Stock List Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a stock-list refresh capability with AkShare-first fallback and add a config-center action model where execution/test buttons use unsaved draft values directly, including a new manual `更新股票池` button in the history sync section.

**Architecture:** Keep stock-list refresh isolated from `history_sync` core execution by implementing it as a standalone script plus a thin server endpoint. In the frontend, introduce a shared draft-action path so config-center execution buttons consistently read `collectConfigFromForm()` results, validate required fields in strict mode, and only then call the backend.

**Tech Stack:** Python, FastAPI, pandas, AkShare, TuShare, vanilla JavaScript, pytest

---

## File Map

- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`
  - Add the new `更新股票池` button in the `工部增量同步` operation area.
  - Add shared strict draft-action helpers for config-center execution/test buttons.
  - Route `runHistorySync()` and the new stock-list refresh action through the shared draft-action pipeline.

- Modify: `d:\04.量化\jin-ce-zhi-suan\server.py`
  - Add a thin API route for manual stock-list refresh.
  - Add request model and payload parsing for stock-list refresh.
  - Reuse existing config compatibility behavior while allowing config-center actions to pass full draft payloads.

- Create: `d:\04.量化\jin-ce-zhi-suan\scripts\update_history_sync_stock_list.py`
  - Standalone CLI entry for stock-list refresh.

- Create: `d:\04.量化\jin-ce-zhi-suan\src\utils\stock_list_refresh.py`
  - Core stock-list refresh logic, provider selection, normalization, safe CSV replace.

- Modify: `d:\04.量化\jin-ce-zhi-suan\docs\API_DOCS.md`
  - Document the new manual stock-list refresh API and usage.

- Test: `d:\04.量化\jin-ce-zhi-suan\tests\test_history_sync_config.py`
  - Extend server-side payload parsing tests and stock-list refresh request tests.

- Test: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_stock_list_refresh.py`
  - New unit tests for AkShare-first, TuShare-fallback, normalization, and safe overwrite behavior.

- Test: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_server_consistency_routes_regression.py`
  - Add route-level regression coverage for the new refresh endpoint if that file already covers thin API contracts.

### Task 1: Build Stock List Refresh Core

**Files:**
- Create: `d:\04.量化\jin-ce-zhi-suan\src\utils\stock_list_refresh.py`
- Test: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_stock_list_refresh.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

import pandas as pd

from src.utils.stock_list_refresh import (
    normalize_stock_list_df,
    refresh_stock_list,
)


class _FakeAkshareClient:
    def __init__(self, df=None, err=None):
        self._df = df
        self._err = err

    def fetch_stock_list(self):
        if self._err:
            raise self._err
        return self._df.copy()


class _FakeTushareClient:
    def __init__(self, df=None, err=None):
        self._df = df
        self._err = err

    def fetch_stock_list(self):
        if self._err:
            raise self._err
        return self._df.copy()


def test_normalize_stock_list_df_keeps_a_share_codes_only():
    raw = pd.DataFrame(
        [
            {"code": "600000", "name": "浦发银行"},
            {"code": "000001", "name": "平安银行"},
            {"code": "430001", "name": "北交样本"},
            {"code": "159001", "name": "ETF样本"},
            {"code": "", "name": "空值"},
        ]
    )

    out = normalize_stock_list_df(raw, source="akshare")

    assert list(out["code"]) == ["000001.SZ", "430001.BJ", "600000.SH"]
    assert list(out["market"]) == ["SZ", "BJ", "SH"]
    assert set(out["source"]) == {"akshare"}


def test_refresh_stock_list_falls_back_to_tushare(tmp_path):
    output = tmp_path / "stock_list.csv"
    ak = _FakeAkshareClient(err=RuntimeError("akshare down"))
    ts = _FakeTushareClient(df=pd.DataFrame([{"code": "600000", "name": "浦发银行"}]))

    result = refresh_stock_list(output_path=output, provider="auto", akshare_client=ak, tushare_client=ts)

    assert result["status"] == "success"
    assert result["source"] == "tushare"
    assert result["fallback_used"] is True
    assert output.exists()
    assert "600000.SH" in output.read_text(encoding="utf-8")


def test_refresh_stock_list_keeps_old_file_when_all_sources_fail(tmp_path):
    output = tmp_path / "stock_list.csv"
    output.write_text("code,name\n000001.SZ,old\n", encoding="utf-8")
    ak = _FakeAkshareClient(err=RuntimeError("akshare down"))
    ts = _FakeTushareClient(err=RuntimeError("tushare down"))

    result = refresh_stock_list(output_path=output, provider="auto", akshare_client=ak, tushare_client=ts)

    assert result["status"] == "error"
    assert result["preserved_existing_file"] is True
    assert output.read_text(encoding="utf-8") == "code,name\n000001.SZ,old\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/unit/test_stock_list_refresh.py -v
```

Expected:

- FAIL with `ModuleNotFoundError` or import errors for `src.utils.stock_list_refresh`

- [ ] **Step 3: Write minimal implementation**

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import pandas as pd


def _infer_market(code: str) -> str:
    text = str(code or "").strip()
    if len(text) != 6 or not text.isdigit():
        return ""
    if text.startswith(("60", "68")):
        return "SH"
    if text.startswith(("00", "30")):
        return "SZ"
    if text.startswith(("43", "83", "87", "88", "92")) or text.startswith(("4", "8")):
        return "BJ"
    return ""


def normalize_stock_list_df(df: pd.DataFrame, source: str) -> pd.DataFrame:
    work = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if work.empty:
        return pd.DataFrame(columns=["code", "name", "market", "source", "updated_at"])
    code_col = "code" if "code" in work.columns else ("symbol" if "symbol" in work.columns else "")
    name_col = "name" if "name" in work.columns else ("名称" if "名称" in work.columns else "")
    if not code_col:
        return pd.DataFrame(columns=["code", "name", "market", "source", "updated_at"])
    work["raw_code"] = work[code_col].astype(str).str.strip()
    work["name"] = work[name_col].astype(str).str.strip() if name_col else ""
    work["market"] = work["raw_code"].map(_infer_market)
    work = work[(work["market"] != "") & (work["raw_code"].str.len() == 6)]
    if work.empty:
        return pd.DataFrame(columns=["code", "name", "market", "source", "updated_at"])
    work["code"] = work["raw_code"] + "." + work["market"]
    work["source"] = str(source or "").strip().lower() or "unknown"
    work["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    work = work[["code", "name", "market", "source", "updated_at"]].drop_duplicates(subset=["code"]).sort_values("code")
    return work.reset_index(drop=True)


def _safe_write_csv(df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", dir=str(output_path.parent), encoding="utf-8", newline="") as tmp:
        temp_path = Path(tmp.name)
    df.to_csv(temp_path, index=False, encoding="utf-8")
    temp_path.replace(output_path)


def refresh_stock_list(output_path, provider="auto", akshare_client=None, tushare_client=None):
    path = Path(output_path)
    provider_norm = str(provider or "auto").strip().lower() or "auto"
    attempts = []
    if provider_norm == "akshare":
        attempts = [("akshare", akshare_client)]
    elif provider_norm == "tushare":
        attempts = [("tushare", tushare_client)]
    else:
        attempts = [("akshare", akshare_client), ("tushare", tushare_client)]
    errors = []
    fallback_used = False
    for idx, (name, client) in enumerate(attempts):
        if client is None:
            errors.append(f"{name}: client unavailable")
            continue
        try:
            raw = client.fetch_stock_list()
            normalized = normalize_stock_list_df(raw, source=name)
            if normalized.empty:
                raise RuntimeError(f"{name} returned empty normalized stock list")
            _safe_write_csv(normalized, path)
            return {
                "status": "success",
                "source": name,
                "fallback_used": idx > 0,
                "codes": int(len(normalized)),
                "output_path": str(path),
                "preserved_existing_file": False,
            }
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            fallback_used = fallback_used or idx > 0
    return {
        "status": "error",
        "source": "",
        "fallback_used": fallback_used,
        "codes": 0,
        "output_path": str(path),
        "preserved_existing_file": path.exists(),
        "error": " | ".join(errors),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m pytest tests/unit/test_stock_list_refresh.py -v
```

Expected:

- PASS for the new stock-list refresh tests

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_stock_list_refresh.py src/utils/stock_list_refresh.py
git commit -m "feat: add stock list refresh core"
```

### Task 2: Add Real Provider Adapters And CLI Script

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\stock_list_refresh.py`
- Create: `d:\04.量化\jin-ce-zhi-suan\scripts\update_history_sync_stock_list.py`
- Test: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_stock_list_refresh.py`

- [ ] **Step 1: Write the failing test**

```python
from src.utils.stock_list_refresh import build_refresh_clients


def test_build_refresh_clients_returns_supported_providers():
    clients = build_refresh_clients()

    assert "akshare" in clients
    assert "tushare" in clients
    assert hasattr(clients["akshare"], "fetch_stock_list")
    assert hasattr(clients["tushare"], "fetch_stock_list")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/unit/test_stock_list_refresh.py::test_build_refresh_clients_returns_supported_providers -v
```

Expected:

- FAIL with `ImportError` or `AttributeError` for `build_refresh_clients`

- [ ] **Step 3: Write minimal implementation**

```python
import akshare as ak
from src.utils.config_loader import ConfigLoader
from src.utils.tushare_provider import TushareProvider


class AkshareStockListClient:
    def fetch_stock_list(self):
        df = ak.stock_info_a_code_name()
        if "code" not in df.columns and "证券代码" in df.columns:
            df = df.rename(columns={"证券代码": "code"})
        if "name" not in df.columns and "证券简称" in df.columns:
            df = df.rename(columns={"证券简称": "name"})
        return df


class TushareStockListClient:
    def __init__(self):
        cfg = ConfigLoader.reload()
        token = str(cfg.get("private.tushare_token", "") or cfg.get("tushare.token", "") or "").strip()
        self._provider = TushareProvider(token=token)

    def fetch_stock_list(self):
        if getattr(self._provider, "pro", None) is None:
            raise RuntimeError("tushare token not configured")
        df = self._provider.pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
        if "ts_code" not in df.columns:
            return pd.DataFrame()
        out = pd.DataFrame()
        out["code"] = df["ts_code"].astype(str).str.split(".").str[0]
        out["name"] = df["name"].astype(str).str.strip()
        return out


def build_refresh_clients():
    return {
        "akshare": AkshareStockListClient(),
        "tushare": TushareStockListClient(),
    }
```

And create the CLI entry:

```python
import argparse
from pathlib import Path

from src.utils.stock_list_refresh import build_refresh_clients, refresh_stock_list


def main():
    parser = argparse.ArgumentParser(description="Refresh history sync stock list.")
    parser.add_argument("--provider", default="auto", choices=["auto", "akshare", "tushare"])
    parser.add_argument("--output", default="data/stock_list.csv")
    args = parser.parse_args()

    clients = build_refresh_clients()
    result = refresh_stock_list(
        output_path=Path(args.output),
        provider=args.provider,
        akshare_client=clients.get("akshare"),
        tushare_client=clients.get("tushare"),
    )
    if result.get("status") != "success":
        raise SystemExit(result.get("error") or "stock list refresh failed")
    print(f"股票池更新完成 source={result['source']} codes={result['codes']} output={result['output_path']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m pytest tests/unit/test_stock_list_refresh.py -v
```

Expected:

- PASS including provider builder test

- [ ] **Step 5: Commit**

```bash
git add src/utils/stock_list_refresh.py scripts/update_history_sync_stock_list.py tests/unit/test_stock_list_refresh.py
git commit -m "feat: add stock list refresh providers and cli"
```

### Task 3: Add Server Endpoint For Manual Stock List Refresh

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\server.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\test_history_sync_config.py`
- Test: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_server_consistency_routes_regression.py`

- [ ] **Step 1: Write the failing test**

```python
from server import HistorySyncStockListRefreshRequest, _stock_list_refresh_payload_from_request


def test_stock_list_refresh_payload_uses_request_values():
    req = HistorySyncStockListRefreshRequest(
        provider="auto",
        output_path="data/stock_list.csv",
    )

    payload = _stock_list_refresh_payload_from_request(req)

    assert payload["provider"] == "auto"
    assert payload["output_path"].endswith("data/stock_list.csv")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_history_sync_config.py::test_stock_list_refresh_payload_uses_request_values -v
```

Expected:

- FAIL because the request model/helper do not exist yet

- [ ] **Step 3: Write minimal implementation**

```python
from pydantic import BaseModel
from pathlib import Path
from src.utils.stock_list_refresh import build_refresh_clients, refresh_stock_list


class HistorySyncStockListRefreshRequest(BaseModel):
    provider: Optional[str] = None
    output_path: Optional[str] = None


def _stock_list_refresh_payload_from_request(req: HistorySyncStockListRefreshRequest):
    output_path = str(req.output_path or "data/stock_list.csv").strip() or "data/stock_list.csv"
    return {
        "provider": str(req.provider or "auto").strip().lower() or "auto",
        "output_path": output_path,
    }


@app.post("/api/history_sync/stock_list/refresh")
async def api_history_sync_stock_list_refresh(req: HistorySyncStockListRefreshRequest):
    try:
        payload = _stock_list_refresh_payload_from_request(req)
        clients = build_refresh_clients()
        result = await asyncio.to_thread(
            refresh_stock_list,
            Path(payload["output_path"]),
            payload["provider"],
            clients.get("akshare"),
            clients.get("tushare"),
        )
        if result.get("status") != "success":
            return {"status": "error", "msg": result.get("error") or "refresh failed", "result": result}
        return {"status": "success", "msg": "stock list refreshed", "result": result}
    except Exception as e:
        logger.error("history sync stock list refresh failed: %s", e, exc_info=True)
        return {"status": "error", "msg": str(e)}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m pytest tests/test_history_sync_config.py::test_stock_list_refresh_payload_uses_request_values -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_history_sync_config.py
git commit -m "feat: add history sync stock list refresh api"
```

### Task 4: Add Config-Center Draft Action Helpers And History Sync Refresh Button

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`

- [ ] **Step 1: Write the failing static check**

```python
from pathlib import Path


def test_dashboard_contains_history_sync_stock_list_refresh_button():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert 'history-sync-refresh-stock-list-btn' in html
    assert 'runHistorySyncStockListRefresh()' in html
    assert '使用当前草稿执行' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -c "from pathlib import Path; html = Path('dashboard.html').read_text(encoding='utf-8'); assert 'history-sync-refresh-stock-list-btn' in html; assert 'runHistorySyncStockListRefresh()' in html; assert '使用当前草稿执行' in html"
```

Expected:

- FAIL because the button/helper text do not exist yet

- [ ] **Step 3: Write minimal implementation**

Add a shared helper pattern in `dashboard.html`:

```javascript
function getConfigDraftStrict() {
    const draft = collectConfigFromForm();
    if (!draft || typeof draft !== 'object') {
        throw new Error('无法读取当前草稿配置');
    }
    return draft;
}

function getMissingDraftPaths(draft, requiredPaths) {
    const missing = [];
    for (const path of (Array.isArray(requiredPaths) ? requiredPaths : [])) {
        const value = getByPath(draft, path, undefined);
        const empty = value === undefined || value === null || String(value).trim?.() === '';
        if (empty) missing.push(String(path));
    }
    return missing;
}

function ensureDraftActionRequirements(actionLabel, draft, requiredPaths) {
    const missing = getMissingDraftPaths(draft, requiredPaths);
    if (missing.length) {
        throw new Error(`无法执行${actionLabel}：缺少 ${missing.join('、')}`);
    }
}
```

Update the history sync area button block:

```javascript
<button id="history-sync-refresh-stock-list-btn" onclick="runHistorySyncStockListRefresh()" class="bg-cyan-700 hover:bg-cyan-600 text-white px-3 py-1.5 rounded text-xs border border-cyan-500">
    <i id="history-sync-refresh-stock-list-icon" class="fa-solid fa-list-check"></i>
    <span id="history-sync-refresh-stock-list-label">更新股票池</span>
</button>
```

And add the new action:

```javascript
async function runHistorySyncStockListRefresh() {
    const draft = getConfigDraftStrict();
    logMessage('SYSTEM', '使用当前草稿执行股票池更新', 'info');
    const provider = String(getByPath(draft, 'history_sync.stock_list_provider', 'auto') || 'auto');
    const outputPath = String(getByPath(draft, 'history_sync.stock_list_output_path', 'data/stock_list.csv') || 'data/stock_list.csv').trim();
    const payload = {
        provider,
        output_path: outputPath,
        config: draft,
    };
    const res = await fetch('/api/history_sync/stock_list/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok || data.status !== 'success') {
        throw new Error(data.msg || '股票池更新失败');
    }
    const result = data.result || {};
    logMessage('SYSTEM', `股票池更新完成：source=${result.source || '--'} codes=${Number(result.codes || 0)}`, 'success');
}
```

- [ ] **Step 4: Run check to verify it passes**

Run:

```bash
python -c "from pathlib import Path; html = Path('dashboard.html').read_text(encoding='utf-8'); assert 'history-sync-refresh-stock-list-btn' in html; assert 'runHistorySyncStockListRefresh()' in html; assert '使用当前草稿执行股票池更新' in html; print('checks passed')"
```

Expected:

- PASS with `checks passed`

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: add config center stock list refresh action"
```

### Task 5: Route History Sync Actions Through Strict Draft Validation

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\dashboard.html`

- [ ] **Step 1: Write the failing static check**

```python
from pathlib import Path


def test_history_sync_run_uses_strict_draft_helpers():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert 'getConfigDraftStrict()' in html
    assert 'ensureDraftActionRequirements(' in html
    assert '使用当前草稿执行增量同步' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -c "from pathlib import Path; html = Path('dashboard.html').read_text(encoding='utf-8'); assert '使用当前草稿执行增量同步' in html"
```

Expected:

- FAIL because the log text and strict helper usage are not complete yet

- [ ] **Step 3: Write minimal implementation**

Update `runHistorySync()` to use strict draft mode:

```javascript
async function runHistorySync() {
    if (historySyncRunning) {
        logMessage('SYSTEM', '增量同步任务已在运行中，请点击“停止同步”按钮中断', 'warning');
        return;
    }
    historySyncRunning = true;
    historySyncStopRequested = false;
    renderHistorySyncButtons();
    try {
        const cfg = getConfigDraftStrict();
        const timeMode = String(getByPath(cfg, 'history_sync.time_mode', 'lookback') || 'lookback');
        const requiredPaths = [
            'data_provider.source',
            'history_sync.write_mode',
            'history_sync.direct_db_source',
        ];
        if (timeMode === 'custom') {
            requiredPaths.push('history_sync.custom_start_time');
            requiredPaths.push('history_sync.custom_end_time');
        } else {
            requiredPaths.push('history_sync.lookback_days');
        }
        ensureDraftActionRequirements('增量同步', cfg, requiredPaths);
        logMessage('SYSTEM', '使用当前草稿执行增量同步', 'info');
        // keep existing payload build logic below
    } catch (e) {
        logMessage('SYSTEM', `增量同步失败: ${String(e?.message || e || '未知错误')}`, 'danger');
    } finally {
        await refreshHistorySyncStatus();
    }
}
```

Also update any other config-center execution/test button functions that still rely on saved config reload rather than `collectConfigFromForm()`, using the same helper path.

- [ ] **Step 4: Run check to verify it passes**

Run:

```bash
python -c "from pathlib import Path; html = Path('dashboard.html').read_text(encoding='utf-8'); assert '使用当前草稿执行增量同步' in html; assert 'ensureDraftActionRequirements(' in html; print('checks passed')"
```

Expected:

- PASS with `checks passed`

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: enforce strict draft execution in config center"
```

### Task 6: Add Docs And Final Regression

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\docs\API_DOCS.md`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\test_history_sync_config.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_server_consistency_routes_regression.py`

- [ ] **Step 1: Write the failing checks**

```python
from pathlib import Path


def test_api_docs_include_stock_list_refresh_endpoint():
    text = Path("docs/API_DOCS.md").read_text(encoding="utf-8")

    assert "/api/history_sync/stock_list/refresh" in text
    assert "AkShare" in text
    assert "TuShare" in text
```

- [ ] **Step 2: Run check to verify it fails**

Run:

```bash
python -c "from pathlib import Path; text = Path('docs/API_DOCS.md').read_text(encoding='utf-8'); assert '/api/history_sync/stock_list/refresh' in text"
```

Expected:

- FAIL because docs are not updated yet

- [ ] **Step 3: Write minimal implementation**

Document:

```markdown
### 历史同步股票池刷新

- 路径：`POST /api/history_sync/stock_list/refresh`
- 作用：刷新 `data/stock_list.csv`
- 策略：默认 `AkShare` 优先，失败后回退 `TuShare`
- 说明：双源都失败时不会覆盖旧文件

请求示例：

```json
{
  "provider": "auto",
  "output_path": "data/stock_list.csv"
}
```
```

Then run the regression suite:

```bash
python -m pytest tests/unit/test_stock_list_refresh.py tests/test_history_sync_config.py tests/unit/test_server_consistency_routes_regression.py -v
```

And perform static checks:

```bash
python -c "from pathlib import Path; html = Path('dashboard.html').read_text(encoding='utf-8'); docs = Path('docs/API_DOCS.md').read_text(encoding='utf-8'); assert 'history-sync-refresh-stock-list-btn' in html; assert '/api/history_sync/stock_list/refresh' in docs; print('checks passed')"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/unit/test_stock_list_refresh.py tests/test_history_sync_config.py tests/unit/test_server_consistency_routes_regression.py -v
```

Expected:

- PASS for all targeted tests

- [ ] **Step 5: Commit**

```bash
git add docs/API_DOCS.md tests/test_history_sync_config.py tests/unit/test_server_consistency_routes_regression.py
git commit -m "docs: add stock list refresh api and config center action coverage"
```
