# History Sync Performance Profiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为现有增量同步链路补充最小侵入式性能剖析能力，把阶段耗时输出到日志、运行中状态接口与最终报告，同时不改变同步业务行为。

**Architecture:** 基于现有 `src/utils/history_sync_service.py` 继续增量扩展，不新增新模块。通过在 `writer flush`、`table`、`code`、`chunk` 四层加轻量计时与聚合字段，实现“只加观测、不改行为”的第一阶段性能定位方案。测试继续复用已有 `tests/utils/` 与 `tests/unit/` 下的 history sync 相关测试文件，避免空降新的测试结构。

**Tech Stack:** Python 3、pytest、pandas、ThreadPoolExecutor、time.perf_counter、现有 `HistoryDiffSyncService` / `DuckDbSerialWriter`

---

## File Map

- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\history_sync_service.py`
  - 为 `DuckDbSerialWriter` 增加 flush 耗时与 flush 行数统计
  - 为 `_submit_duckdb_write_task()` 增加等待耗时和回传写入执行耗时
  - 为 `_process_code_sync()` 增加 source/table/code 三级耗时字段
  - 为 `_run_sync_impl()` 增加 chunk 聚合耗时与 summary 聚合字段
  - 新增维护 `slow_codes_topn` 的辅助方法
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_duckdb_serial_writer.py`
  - 覆盖 writer flush 性能字段
  - 覆盖 `_submit_duckdb_write_task()` 回传结构
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_history_sync_duckdb_integration.py`
  - 覆盖 `_process_code_sync()` 的 code/table 性能字段
  - 覆盖 `_run_sync_impl()` 的 summary 与 chunk 聚合字段
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_history_sync_service_duckdb.py`
  - 覆盖 `slow_codes_topn` 维护与 summary 聚合辅助逻辑

## Task 1: 给 DuckDB Writer 加 flush 性能统计

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_duckdb_serial_writer.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\history_sync_service.py`

- [ ] **Step 1: 在 writer 测试里先写失败用例，锁定 flush 性能字段**

```python
def test_writer_records_flush_elapsed_and_rows():
    # writer 刷盘后应暴露 flush 行数和耗时，供 summary 聚合与实时状态读取。
    provider = SlowDuckDbProvider(sleep_sec=0.01)
    writer = DuckDbSerialWriter(
        provider=provider,
        batch_size=100,
        max_batch_rows=1,
        max_batch_codes=1,
        max_wait_ms=5,
        queue_maxsize=4,
    )
    writer.start()
    task = _build_task("000001.SZ", "2026-03-02 09:30:00")

    writer.submit(task)
    writer.close_and_wait()

    result = task.result_future.result(timeout=1)

    assert writer.flush_batches == 1
    assert writer.flushed_codes == 1
    assert writer.total_flush_rows == 1
    assert writer.last_flush_elapsed_sec > 0
    assert writer.max_flush_elapsed_sec >= writer.last_flush_elapsed_sec
    assert result["written_rows"] == 1
    assert result["write_exec_elapsed_sec"] > 0
```

- [ ] **Step 2: 运行测试，确认当前字段尚未实现**

Run:

```bash
pytest tests/utils/test_duckdb_serial_writer.py::test_writer_records_flush_elapsed_and_rows -v
```

Expected:

```text
FAILED tests/utils/test_duckdb_serial_writer.py::test_writer_records_flush_elapsed_and_rows
E   AttributeError: 'DuckDbSerialWriter' object has no attribute 'total_flush_rows'
```

- [ ] **Step 3: 在 `DuckDbSerialWriter` 中实现最小性能统计**

```python
class DuckDbSerialWriter:
    # 通过单独写线程串行刷 DuckDB，规避单文件并发写入冲突。
    def __init__(
        self,
        provider: DuckDbProvider,
        batch_size: int,
        max_batch_rows: int,
        max_batch_codes: int,
        max_wait_ms: int,
        queue_maxsize: int,
    ):
        self.provider = provider
        self.batch_size = max(1, int(batch_size or 1))
        self.max_batch_rows = max(1, int(max_batch_rows or 1))
        self.max_batch_codes = max(1, int(max_batch_codes or 1))
        self.max_wait_ms = max(1, int(max_wait_ms or 1))
        self.queue = Queue(maxsize=max(1, int(queue_maxsize or 1)))
        self.fatal_error: Optional[Exception] = None
        self.flush_batches = 0
        self.flushed_codes = 0
        self.queue_peak_size = 0
        self.total_flush_rows = 0
        self.last_flush_elapsed_sec = 0.0
        self.max_flush_elapsed_sec = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn = None
        self._buckets: dict[tuple[str, str], list[DuckDbWriteTask]] = {}

    def _flush_bucket(self, key: tuple[str, str]) -> None:
        tasks = self._buckets.get(key, [])
        if not tasks:
            return
        _, interval = key
        frames = [item.df for item in tasks if item.df is not None and not item.df.empty]
        merged_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        flush_started = time.perf_counter()
        written_rows = int(
            self.provider.upsert_kline_data_with_conn(
                self._conn,
                merged_df,
                interval=interval,
                batch_size=self.batch_size,
            )
            or 0
        )
        flush_elapsed_sec = max(0.0, time.perf_counter() - flush_started)
        if written_rows <= 0 and str(getattr(self.provider, "last_error", "")).strip():
            raise RuntimeError(self.provider.last_error)
        self.flush_batches += 1
        self.flushed_codes += len(tasks)
        self.total_flush_rows += int(written_rows)
        self.last_flush_elapsed_sec = float(flush_elapsed_sec)
        self.max_flush_elapsed_sec = max(float(self.max_flush_elapsed_sec), float(flush_elapsed_sec))
        for item in tasks:
            if not item.result_future.done():
                item.result_future.set_result(
                    {
                        "code": item.code,
                        "table": item.table,
                        "written_rows": int(item.missing_rows or 0),
                        "write_exec_elapsed_sec": float(flush_elapsed_sec),
                    }
                )
        self._buckets[key] = []
```

- [ ] **Step 4: 运行测试，确认 writer 性能字段生效**

Run:

```bash
pytest tests/utils/test_duckdb_serial_writer.py::test_writer_records_flush_elapsed_and_rows -v
```

Expected:

```text
PASSED tests/utils/test_duckdb_serial_writer.py::test_writer_records_flush_elapsed_and_rows
```

- [ ] **Step 5: 提交 writer 性能统计改动**

```bash
git add tests/utils/test_duckdb_serial_writer.py src/utils/history_sync_service.py
git commit -m "feat: add duckdb writer profiling metrics"
```

## Task 2: 给写任务等待和表级耗时补回传字段

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_duckdb_serial_writer.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_history_sync_duckdb_integration.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\history_sync_service.py`

- [ ] **Step 1: 先写 `_submit_duckdb_write_task()` 的等待耗时失败测试**

```python
def test_submit_duckdb_write_task_returns_wait_and_exec_elapsed(monkeypatch):
    # 写任务返回结果时应包含等待耗时与执行耗时，供表级 report 使用。
    service = HistoryDiffSyncService()

    class FixedWriter:
        def __init__(self):
            self.submitted = []

        def submit(self, task):
            self.submitted.append(task)
            task.result_future.set_result(
                {
                    "code": task.code,
                    "table": task.table,
                    "written_rows": 3,
                    "write_exec_elapsed_sec": 0.25,
                }
            )

    service._duckdb_writer = FixedWriter()
    result = service._submit_duckdb_write_task(
        code="000001.SZ",
        table="dat_1mins",
        df=pd.DataFrame([{"code": "000001.SZ", "trade_time": "2026-03-02 09:30:00"}]),
        source_rows=10,
        existing_rows=7,
        missing_rows=3,
    )

    assert result["written_rows"] == 3
    assert result["write_exec_elapsed_sec"] == 0.25
    assert result["write_wait_elapsed_sec"] >= 0.0
```

- [ ] **Step 2: 写 `_process_code_sync()` 的表级耗时失败测试**

```python
def test_process_code_sync_exposes_code_and_table_profiling(monkeypatch):
    # code/table 两层都应暴露阶段耗时，便于判断慢在 source、dedup 还是写入等待。
    service = HistoryDiffSyncService()
    service._duckdb_checkpoint_store = None
    service._duckdb_writer = RecordingWriter()

    monkeypatch.setattr(service, "_ensure_target_db_ready", lambda **kwargs: None)
    monkeypatch.setattr(
        service,
        "_build_worker_runtime",
        lambda **kwargs: {
            "source_provider": object(),
            "target_db_provider": DummyProvider(),
            "session": None,
            "headers": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_build_source_frames",
        lambda provider, code, start_time, end_time, tables, session_only=True: {
            "dat_1mins": pd.DataFrame(
                [
                    {
                        "code": code,
                        "trade_time": "2026-03-02 09:30:00",
                        "open": 1.0,
                        "high": 1.0,
                        "low": 1.0,
                        "close": 1.0,
                        "vol": 1.0,
                        "amount": 1.0,
                        "date": "2026-03-02",
                        "pre_close": 1.0,
                        "change": 0.0,
                        "pct_chg": 0.0,
                    }
                ]
            )
        },
    )

    result = service._process_code_sync(
        code="000001.SZ",
        cfg={},
        provider_source="duckdb",
        start_time=datetime(2026, 3, 2, 9, 30, 0),
        end_time=datetime(2026, 3, 2, 15, 0, 0),
        tables=["dat_1mins"],
        session_only=True,
        write_mode="direct_db",
        direct_db_source="duckdb",
        dry_run=False,
        batch_size=500,
        on_duplicate="ignore",
        history_base_url="",
        history_api_key="",
        existing_keys_by_table={"dat_1mins": {"000001.SZ": set()}},
        runtime_token="t1",
    )

    table_report = result["code_report"]["tables"][0]

    assert result["code_report"]["code_elapsed_sec"] >= 0.0
    assert result["code_report"]["source_build_elapsed_sec"] >= 0.0
    assert result["code_report"]["tables_elapsed_sec"] >= 0.0
    assert table_report["existing_keys_count"] == 0
    assert table_report["dedup_elapsed_sec"] >= 0.0
    assert table_report["write_wait_elapsed_sec"] >= 0.0
    assert table_report["write_exec_elapsed_sec"] >= 0.0
```

- [ ] **Step 3: 运行两条测试，确认当前返回结构不满足要求**

Run:

```bash
pytest tests/utils/test_duckdb_serial_writer.py::test_submit_duckdb_write_task_returns_wait_and_exec_elapsed tests/utils/test_history_sync_duckdb_integration.py::test_process_code_sync_exposes_code_and_table_profiling -v
```

Expected:

```text
FAILED tests/utils/test_duckdb_serial_writer.py::test_submit_duckdb_write_task_returns_wait_and_exec_elapsed
FAILED tests/utils/test_history_sync_duckdb_integration.py::test_process_code_sync_exposes_code_and_table_profiling
```

- [ ] **Step 4: 给 `_submit_duckdb_write_task()` 和 `_process_code_sync()` 加最小实现**

```python
def _submit_duckdb_write_task(
    self,
    code: str,
    table: str,
    df: pd.DataFrame,
    source_rows: int,
    existing_rows: int,
    missing_rows: int,
) -> dict[str, Any]:
    # 工作线程只负责提交缺失数据并等待写线程确认结果。
    if self._duckdb_writer is None:
        raise RuntimeError("duckdb serial writer not initialized")
    wait_timeout_sec = getattr(self, "_duckdb_writer_result_timeout_sec", 1800)
    try:
        normalized_timeout = float(wait_timeout_sec)
    except Exception:
        normalized_timeout = 1800.0
    if normalized_timeout <= 0:
        normalized_timeout = None
    task = DuckDbWriteTask(
        code=code,
        table=table,
        interval=TABLE_INTERVAL_MAP.get(table, "1min"),
        df=df,
        source_rows=source_rows,
        existing_rows=existing_rows,
        missing_rows=missing_rows,
    )
    self._duckdb_writer.submit(task)
    wait_started = time.perf_counter()
    try:
        raw_result = task.result_future.result(timeout=normalized_timeout)
    except FutureTimeoutError as e:
        raise RuntimeError(
            f"duckdb serial writer timeout: code={code} table={table} "
            f"wait_timeout_sec={wait_timeout_sec} missing_rows={missing_rows}"
        ) from e
    wait_elapsed_sec = max(0.0, time.perf_counter() - wait_started)
    result = raw_result if isinstance(raw_result, dict) else {}
    return {
        "code": code,
        "table": table,
        "written_rows": int(result.get("written_rows", 0) or 0),
        "write_exec_elapsed_sec": float(result.get("write_exec_elapsed_sec", 0.0) or 0.0),
        "write_wait_elapsed_sec": float(wait_elapsed_sec),
    }
```

```python
def _process_code_sync(
    self,
    code: str,
    cfg: dict[str, Any],
    provider_source: str,
    start_time: datetime,
    end_time: datetime,
    tables: list[str],
    session_only: bool,
    write_mode: str,
    direct_db_source: str,
    dry_run: bool,
    batch_size: int,
    on_duplicate: str,
    history_base_url: str,
    history_api_key: str,
    existing_keys_by_table: dict[str, dict[str, set[str]]],
    runtime_token: str,
) -> dict[str, Any]:
    self._check_stop_requested(context=f"before code {code}")
    code_started = time.perf_counter()
    runtime = self._build_worker_runtime(
        cfg=cfg,
        provider_source=provider_source,
        write_mode=write_mode,
        direct_db_source=direct_db_source,
        history_api_key=history_api_key,
        runtime_token=runtime_token,
    )
    provider = runtime.get("source_provider")
    session = runtime.get("session")
    headers = runtime.get("headers", {})
    target_db_provider = runtime.get("target_db_provider")
    serial_duckdb = self._is_duckdb_serial_writer_enabled(write_mode, direct_db_source, cfg)
    if write_mode == "direct_db":
        self._ensure_target_db_ready(
            write_mode=write_mode,
            provider=target_db_provider,
            sample_code=code,
        )
    source_build_started = time.perf_counter()
    source_frames = self._build_source_frames(provider, code, start_time, end_time, tables, session_only=session_only)
    source_build_elapsed_sec = max(0.0, time.perf_counter() - source_build_started)
    tables_started = time.perf_counter()
    code_report = {
        "code": code,
        "source_build_elapsed_sec": float(source_build_elapsed_sec),
        "tables_elapsed_sec": 0.0,
        "code_elapsed_sec": 0.0,
        "tables": [],
    }
    for table in tables:
        self._check_stop_requested(context=f"before table {table} code {code}")
        source_df = source_frames.get(table)
        if source_df is None or source_df.empty:
            code_report["tables"].append(
                {
                    "table": table,
                    "source_rows": 0,
                    "existing_rows": 0,
                    "existing_keys_count": 0,
                    "missing_rows": 0,
                    "written_rows": 0,
                    "dedup_elapsed_sec": 0.0,
                    "write_wait_elapsed_sec": 0.0,
                    "write_exec_elapsed_sec": 0.0,
                }
            )
            continue
        key_col = "trade_time" if not self._is_day_table(table) else "date"
        if write_mode == "api":
            existing_keys = self._fetch_existing_keys(
                session=session,
                base_url=history_base_url,
                headers=headers,
                table=table,
                code=code,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            existing_keys = existing_keys_by_table.get(table, {}).get(code, set())
        dedup_started = time.perf_counter()
        source_keys = source_df[key_col].map(lambda x: self._normalize_time_key(x, is_day=self._is_day_table(table)))
        missing_mask = ~source_keys.isin(existing_keys)
        missing_df = source_df.loc[missing_mask].copy()
        dedup_elapsed_sec = max(0.0, time.perf_counter() - dedup_started)
        written_rows = 0
        write_wait_elapsed_sec = 0.0
        write_exec_elapsed_sec = 0.0
        if not dry_run and not missing_df.empty:
            if write_mode == "api":
                rows = missing_df.to_dict("records")
                write_started = time.perf_counter()
                written_rows = self._push_rows(
                    session=session,
                    base_url=history_base_url,
                    headers=headers,
                    table=table,
                    rows=rows,
                    batch_size=batch_size,
                    on_duplicate=on_duplicate,
                )
                write_exec_elapsed_sec = max(0.0, time.perf_counter() - write_started)
                write_wait_elapsed_sec = float(write_exec_elapsed_sec)
            else:
                upsert_df = self._build_direct_db_upsert_df(table=table, df=missing_df)
                if serial_duckdb:
                    write_result = self._submit_duckdb_write_task(
                        code=code,
                        table=table,
                        df=upsert_df,
                        source_rows=int(len(source_df)),
                        existing_rows=int(len(existing_keys)),
                        missing_rows=int(len(missing_df)),
                    )
                    written_rows = int(write_result.get("written_rows", 0) or 0)
                    write_wait_elapsed_sec = float(write_result.get("write_wait_elapsed_sec", 0.0) or 0.0)
                    write_exec_elapsed_sec = float(write_result.get("write_exec_elapsed_sec", 0.0) or 0.0)
                else:
                    interval = TABLE_INTERVAL_MAP.get(table, "1min")
                    write_started = time.perf_counter()
                    written_rows = int(
                        target_db_provider.upsert_kline_data(upsert_df, interval=interval, batch_size=batch_size) or 0
                    )
                    write_exec_elapsed_sec = max(0.0, time.perf_counter() - write_started)
                    write_wait_elapsed_sec = float(write_exec_elapsed_sec)
                    if written_rows <= 0 and str(getattr(target_db_provider, "last_error", "")).strip():
                        raise RuntimeError(f"direct_db upsert failed table={table} code={code}: {target_db_provider.last_error}")
        code_report["tables"].append(
            {
                "table": table,
                "source_rows": int(len(source_df)),
                "existing_rows": int(len(existing_keys)),
                "existing_keys_count": int(len(existing_keys)),
                "missing_rows": int(len(missing_df)),
                "written_rows": int(written_rows),
                "dedup_elapsed_sec": float(dedup_elapsed_sec),
                "write_wait_elapsed_sec": float(write_wait_elapsed_sec),
                "write_exec_elapsed_sec": float(write_exec_elapsed_sec),
            }
        )
    code_report["tables_elapsed_sec"] = max(0.0, time.perf_counter() - tables_started)
    code_report["code_elapsed_sec"] = max(0.0, time.perf_counter() - code_started)
    return {
        "code": code,
        "code_report": code_report,
        "code_elapsed": float(code_report["code_elapsed_sec"]),
    }
```

- [ ] **Step 5: 运行测试，确认表级与 code 级耗时字段通过**

Run:

```bash
pytest tests/utils/test_duckdb_serial_writer.py::test_submit_duckdb_write_task_returns_wait_and_exec_elapsed tests/utils/test_history_sync_duckdb_integration.py::test_process_code_sync_exposes_code_and_table_profiling -v
```

Expected:

```text
2 passed
```

- [ ] **Step 6: 提交表级和写任务等待耗时改动**

```bash
git add tests/utils/test_duckdb_serial_writer.py tests/utils/test_history_sync_duckdb_integration.py src/utils/history_sync_service.py
git commit -m "feat: add history sync table profiling metrics"
```

## Task 3: 给 summary 聚合和 slow_codes_topn 增加辅助逻辑

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_history_sync_service_duckdb.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\history_sync_service.py`

- [ ] **Step 1: 先写 slow codes 与 summary 聚合失败测试**

```python
def test_history_sync_append_code_report_aggregates_profiling_metrics():
    service = HistoryDiffSyncService()
    summary = {
        "total_source_rows": 0,
        "total_existing_rows": 0,
        "total_missing_rows": 0,
        "total_written_rows": 0,
        "total_source_build_elapsed_sec": 0.0,
        "total_dedup_elapsed_sec": 0.0,
        "total_write_wait_elapsed_sec": 0.0,
        "total_write_exec_elapsed_sec": 0.0,
        "max_code_elapsed_sec": 0.0,
        "slow_codes_topn": [],
    }
    code_report = {
        "code": "000001.SZ",
        "source_build_elapsed_sec": 0.4,
        "tables_elapsed_sec": 0.5,
        "code_elapsed_sec": 1.2,
        "tables": [
            {
                "table": "dat_1mins",
                "source_rows": 10,
                "existing_rows": 4,
                "existing_keys_count": 4,
                "missing_rows": 6,
                "written_rows": 6,
                "dedup_elapsed_sec": 0.1,
                "write_wait_elapsed_sec": 0.2,
                "write_exec_elapsed_sec": 0.3,
            }
        ],
    }

    service._append_code_report_to_summary(summary, code_report)

    assert summary["total_source_rows"] == 10
    assert summary["total_existing_rows"] == 4
    assert summary["total_missing_rows"] == 6
    assert summary["total_written_rows"] == 6
    assert summary["total_source_build_elapsed_sec"] == 0.4
    assert summary["total_dedup_elapsed_sec"] == 0.1
    assert summary["total_write_wait_elapsed_sec"] == 0.2
    assert summary["total_write_exec_elapsed_sec"] == 0.3
    assert summary["max_code_elapsed_sec"] == 1.2
    assert summary["slow_codes_topn"][0]["code"] == "000001.SZ"
```

```python
def test_history_sync_slow_codes_topn_keeps_only_slowest_ten():
    service = HistoryDiffSyncService()
    summary = {
        "total_source_rows": 0,
        "total_existing_rows": 0,
        "total_missing_rows": 0,
        "total_written_rows": 0,
        "total_source_build_elapsed_sec": 0.0,
        "total_dedup_elapsed_sec": 0.0,
        "total_write_wait_elapsed_sec": 0.0,
        "total_write_exec_elapsed_sec": 0.0,
        "max_code_elapsed_sec": 0.0,
        "slow_codes_topn": [],
    }

    for idx in range(12):
        service._append_code_report_to_summary(
            summary,
            {
                "code": f"{idx:06d}.SZ",
                "source_build_elapsed_sec": 0.1,
                "tables_elapsed_sec": 0.2,
                "code_elapsed_sec": float(idx),
                "tables": [
                    {
                        "table": "dat_1mins",
                        "source_rows": idx + 1,
                        "existing_rows": 0,
                        "existing_keys_count": 0,
                        "missing_rows": idx + 1,
                        "written_rows": idx + 1,
                        "dedup_elapsed_sec": 0.01,
                        "write_wait_elapsed_sec": 0.02,
                        "write_exec_elapsed_sec": 0.03,
                    }
                ],
            },
        )

    assert len(summary["slow_codes_topn"]) == 10
    assert summary["slow_codes_topn"][0]["code_elapsed_sec"] == 11.0
    assert summary["slow_codes_topn"][-1]["code_elapsed_sec"] == 2.0
```

- [ ] **Step 2: 运行测试，确认当前 summary 聚合字段不足**

Run:

```bash
pytest tests/unit/test_history_sync_service_duckdb.py::test_history_sync_append_code_report_aggregates_profiling_metrics tests/unit/test_history_sync_service_duckdb.py::test_history_sync_slow_codes_topn_keeps_only_slowest_ten -v
```

Expected:

```text
FAILED tests/unit/test_history_sync_service_duckdb.py::test_history_sync_append_code_report_aggregates_profiling_metrics
FAILED tests/unit/test_history_sync_service_duckdb.py::test_history_sync_slow_codes_topn_keeps_only_slowest_ten
```

- [ ] **Step 3: 实现 summary 聚合和 slow codes 辅助方法**

```python
def _update_slow_codes_topn(self, summary: dict[str, Any], code_report: dict[str, Any], limit: int = 10) -> None:
    # 维护轻量慢股票 TopN，避免把整份 code_report 复制到 summary 里。
    if not isinstance(summary, dict) or not isinstance(code_report, dict):
        return
    tables = code_report.get("tables", [])
    source_rows = 0
    missing_rows = 0
    for table_report in tables if isinstance(tables, list) else []:
        if not isinstance(table_report, dict):
            continue
        source_rows += int(table_report.get("source_rows", 0) or 0)
        missing_rows += int(table_report.get("missing_rows", 0) or 0)
    item = {
        "code": str(code_report.get("code", "") or ""),
        "code_elapsed_sec": float(code_report.get("code_elapsed_sec", 0.0) or 0.0),
        "source_rows": int(source_rows),
        "missing_rows": int(missing_rows),
    }
    items = list(summary.get("slow_codes_topn", []) or [])
    items.append(item)
    items = [row for row in items if str((row or {}).get("code", "") or "").strip()]
    items.sort(key=lambda row: float(row.get("code_elapsed_sec", 0.0) or 0.0), reverse=True)
    summary["slow_codes_topn"] = items[: max(1, int(limit or 10))]

def _append_code_report_to_summary(self, summary: dict[str, Any], code_report: dict[str, Any]) -> None:
    # 汇总逻辑单独收敛，保证串行/并发两条执行路径统计口径完全一致。
    tables = code_report.get("tables", []) if isinstance(code_report, dict) else []
    summary["total_source_build_elapsed_sec"] += float(code_report.get("source_build_elapsed_sec", 0.0) or 0.0)
    summary["max_code_elapsed_sec"] = max(
        float(summary.get("max_code_elapsed_sec", 0.0) or 0.0),
        float(code_report.get("code_elapsed_sec", 0.0) or 0.0),
    )
    for table_report in tables:
        if not isinstance(table_report, dict):
            continue
        summary["total_source_rows"] += int(table_report.get("source_rows", 0) or 0)
        summary["total_existing_rows"] += int(table_report.get("existing_rows", 0) or 0)
        summary["total_missing_rows"] += int(table_report.get("missing_rows", 0) or 0)
        summary["total_written_rows"] += int(table_report.get("written_rows", 0) or 0)
        summary["total_dedup_elapsed_sec"] += float(table_report.get("dedup_elapsed_sec", 0.0) or 0.0)
        summary["total_write_wait_elapsed_sec"] += float(table_report.get("write_wait_elapsed_sec", 0.0) or 0.0)
        summary["total_write_exec_elapsed_sec"] += float(table_report.get("write_exec_elapsed_sec", 0.0) or 0.0)
    self._update_slow_codes_topn(summary, code_report)
```

- [ ] **Step 4: 运行测试，确认 summary 聚合通过**

Run:

```bash
pytest tests/unit/test_history_sync_service_duckdb.py::test_history_sync_append_code_report_aggregates_profiling_metrics tests/unit/test_history_sync_service_duckdb.py::test_history_sync_slow_codes_topn_keeps_only_slowest_ten -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: 提交 summary 聚合改动**

```bash
git add tests/unit/test_history_sync_service_duckdb.py src/utils/history_sync_service.py
git commit -m "feat: add history sync summary profiling aggregation"
```

## Task 4: 给运行时 summary 和 chunk 聚合接入性能字段

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_history_sync_duckdb_integration.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\history_sync_service.py`

- [ ] **Step 1: 先写 `_run_sync_impl()` 的 chunk 和 summary 失败测试**

```python
def test_run_sync_impl_exposes_chunk_and_summary_profiling(monkeypatch, tmp_path):
    # 运行 summary 应累计 chunk、writer 和 code 级性能字段，并可进入最终 report。
    service = HistoryDiffSyncService()
    service._records_dir = str(tmp_path)
    cfg = {
        "history_sync": {
            "duckdb_writer_enabled": True,
            "resume_from_checkpoint": True,
            "duckdb_writer_batch_rows": 100,
            "duckdb_writer_batch_codes": 2,
            "duckdb_writer_wait_ms": 10,
            "duckdb_writer_queue_maxsize": 10,
        }
    }

    class FakeRunWriter:
        def __init__(self, *args, **kwargs):
            self.fatal_error = None
            self.flush_batches = 2
            self.flushed_codes = 1
            self.queue_peak_size = 3
            self.total_flush_rows = 8
            self.last_flush_elapsed_sec = 0.4
            self.max_flush_elapsed_sec = 0.6

        def start(self):
            return None

        def close_and_wait(self):
            return None

    monkeypatch.setattr("src.utils.history_sync_service._build_runtime_sync_config", lambda incoming=None: cfg)
    monkeypatch.setattr(service, "_resolve_time_range", lambda **kwargs: (datetime(2026, 3, 2, 9, 30, 0), datetime(2026, 3, 2, 15, 0, 0)))
    monkeypatch.setattr(service, "_resolve_codes", lambda payload_codes, max_codes, cfg=None: ["000001.SZ"])
    monkeypatch.setattr(service, "_build_target_db_provider", lambda **kwargs: DuckDbProvider(db_path=":memory:"))
    monkeypatch.setattr(service, "_ensure_target_db_ready", lambda **kwargs: None)
    monkeypatch.setattr(service, "_prefetch_existing_keys_for_chunk", lambda *args, **kwargs: {"dat_1mins": {"000001.SZ": set()}})
    monkeypatch.setattr("src.utils.history_sync_service.DuckDbSerialWriter", FakeRunWriter)

    def _fake_iter_code_chunk_results(**kwargs):
        yield {
            "code": "000001.SZ",
            "code_report": {
                "code": "000001.SZ",
                "source_build_elapsed_sec": 0.5,
                "tables_elapsed_sec": 0.8,
                "code_elapsed_sec": 1.3,
                "tables": [
                    {
                        "table": "dat_1mins",
                        "source_rows": 8,
                        "existing_rows": 0,
                        "existing_keys_count": 0,
                        "missing_rows": 8,
                        "written_rows": 8,
                        "dedup_elapsed_sec": 0.1,
                        "write_wait_elapsed_sec": 0.2,
                        "write_exec_elapsed_sec": 0.3,
                    }
                ],
            },
            "code_elapsed": 1.3,
        }

    monkeypatch.setattr(service, "_iter_code_chunk_results", _fake_iter_code_chunk_results)

    report = service._run_sync_impl(
        {
            "write_mode": "direct_db",
            "direct_db_source": "duckdb",
            "provider_source": "duckdb",
            "tables": ["dat_1mins"],
            "start_time": "2026-03-02T09:30:00",
            "end_time": "2026-03-02T15:00:00",
            "codes": ["000001.SZ"],
            "concurrency": 4,
            "batch_size": 100,
            "session_only": True,
        }
    )

    assert report["total_source_build_elapsed_sec"] == 0.5
    assert report["total_dedup_elapsed_sec"] == 0.1
    assert report["total_write_wait_elapsed_sec"] == 0.2
    assert report["total_write_exec_elapsed_sec"] == 0.3
    assert report["total_existing_keys_prefetch_elapsed_sec"] >= 0.0
    assert report["max_code_elapsed_sec"] == 1.3
    assert report["max_chunk_elapsed_sec"] >= 0.0
    assert report["writer_total_flush_rows"] == 8
    assert report["writer_last_flush_elapsed_sec"] == 0.4
    assert report["writer_max_flush_elapsed_sec"] == 0.6
    assert report["slow_codes_topn"][0]["code"] == "000001.SZ"
```

- [ ] **Step 2: 运行测试，确认 `_run_sync_impl()` 还未汇总这些字段**

Run:

```bash
pytest tests/utils/test_history_sync_duckdb_integration.py::test_run_sync_impl_exposes_chunk_and_summary_profiling -v
```

Expected:

```text
FAILED tests/utils/test_history_sync_duckdb_integration.py::test_run_sync_impl_exposes_chunk_and_summary_profiling
```

- [ ] **Step 3: 在 `_run_sync_impl()` 里接入 chunk 和 summary 聚合**

```python
summary = {
    "codes_total": len(codes),
    "tables": tables,
    "dry_run": dry_run,
    "provider_source": provider_source,
    "write_mode": write_mode,
    "direct_db_source": direct_db_source if write_mode == "direct_db" else "",
    "time_mode": time_mode,
    "session_only": session_only,
    "requested_concurrency": requested_concurrency,
    "effective_concurrency": effective_concurrency,
    "start_time": start_time.isoformat(timespec="seconds"),
    "end_time": end_time.isoformat(timespec="seconds"),
    "total_source_rows": 0,
    "total_existing_rows": 0,
    "total_missing_rows": 0,
    "total_written_rows": 0,
    "total_source_build_elapsed_sec": 0.0,
    "total_existing_keys_prefetch_elapsed_sec": 0.0,
    "total_dedup_elapsed_sec": 0.0,
    "total_write_wait_elapsed_sec": 0.0,
    "total_write_exec_elapsed_sec": 0.0,
    "max_code_elapsed_sec": 0.0,
    "max_chunk_elapsed_sec": 0.0,
    "slow_codes_topn": [],
    "checkpoint_task_signature": task_signature,
    "checkpoint_completed_codes": 0,
    "checkpoint_skipped_codes": int(checkpoint_skipped_codes or 0),
    "writer_flush_batches": 0,
    "writer_flushed_codes": 0,
    "writer_queue_peak_size": 0,
    "writer_total_flush_rows": 0,
    "writer_last_flush_elapsed_sec": 0.0,
    "writer_max_flush_elapsed_sec": 0.0,
    "code_reports": [],
}
```

```python
for chunk_index, code_chunk in enumerate(code_chunks, start=1):
    self._check_stop_requested(context=f"before chunk {chunk_index}")
    if not code_chunk:
        continue
    chunk_started = time.perf_counter()
    prefetch_started = time.perf_counter()
    self._ensure_target_db_ready(
        write_mode=write_mode,
        provider=target_db_provider,
        sample_code=code_chunk[0],
    )
    existing_keys_by_table: dict[str, dict[str, set[str]]] = {}
    if write_mode == "direct_db":
        if current_existing_future is not None:
            existing_keys_by_table = current_existing_future.result()
        else:
            existing_keys_by_table = self._prefetch_existing_keys_for_chunk(
                target_db_provider,
                tables,
                code_chunk,
                start_time,
                end_time,
                chunk_index,
                len(code_chunks),
            )
        summary["total_existing_keys_prefetch_elapsed_sec"] += max(0.0, time.perf_counter() - prefetch_started)
        if existing_keys_executor is not None and chunk_index < len(code_chunks):
            next_code_chunk = code_chunks[chunk_index]
            next_existing_future = existing_keys_executor.submit(
                self._prefetch_existing_keys_for_chunk,
                target_db_provider,
                tables,
                next_code_chunk,
                start_time,
                end_time,
                chunk_index + 1,
                len(code_chunks),
            )
    for code_result in self._iter_code_chunk_results(
        code_chunk=code_chunk,
        cfg=cfg,
        provider_source=provider_source,
        start_time=start_time,
        end_time=end_time,
        tables=tables,
        session_only=session_only,
        write_mode=write_mode,
        direct_db_source=direct_db_source,
        dry_run=dry_run,
        batch_size=batch_size,
        on_duplicate=on_duplicate,
        history_base_url=history_base_url,
        history_api_key=history_api_key,
        existing_keys_by_table=existing_keys_by_table,
        concurrency=effective_concurrency,
        runtime_token=runtime_token,
    ):
        processed_codes += 1
        code = str(code_result.get("code", "") or "")
        code_report = code_result.get("code_report", {})
        self._append_code_report_to_summary(summary, code_report if isinstance(code_report, dict) else {})
        code_elapsed = float(code_result.get("code_elapsed", 0.0) or 0.0)
        summary["code_reports"].append(code_report)
        if use_serial_writer and task_signature and self._duckdb_checkpoint_store is not None:
            checkpoint = self._duckdb_checkpoint_store.mark_code_completed(task_signature, code)
            summary["checkpoint_completed_codes"] = int(
                ((checkpoint or {}).get("summary", {}) or {}).get("codes_completed", 0) or 0
            )
        if self._duckdb_writer is not None:
            summary["writer_flush_batches"] = int(getattr(self._duckdb_writer, "flush_batches", 0) or 0)
            summary["writer_flushed_codes"] = int(getattr(self._duckdb_writer, "flushed_codes", 0) or 0)
            summary["writer_queue_peak_size"] = int(getattr(self._duckdb_writer, "queue_peak_size", 0) or 0)
            summary["writer_total_flush_rows"] = int(getattr(self._duckdb_writer, "total_flush_rows", 0) or 0)
            summary["writer_last_flush_elapsed_sec"] = float(getattr(self._duckdb_writer, "last_flush_elapsed_sec", 0.0) or 0.0)
            summary["writer_max_flush_elapsed_sec"] = float(getattr(self._duckdb_writer, "max_flush_elapsed_sec", 0.0) or 0.0)
        self._set_current_report(summary, status="running")
        chunk_done = processed_codes - ((chunk_index - 1) * existing_keys_chunk_size)
        percent = (processed_codes / total_codes * 100.0) if total_codes > 0 else 0.0
        logger.info(
            f"增量同步进度：已完成股票={processed_codes}/{total_codes} ({percent:.2f}%) "
            f"当前批次={chunk_index}/{len(code_chunks)} 批次内完成={chunk_done}/{len(code_chunk)} "
            f"当前股票={code} 股票耗时={code_elapsed:.2f}s "
            f"源构建耗时={float(code_report.get('source_build_elapsed_sec', 0.0) or 0.0):.2f}s "
            f"表处理耗时={float(code_report.get('tables_elapsed_sec', 0.0) or 0.0):.2f}s "
            f"本股票写入行数={sum(int(item.get('written_rows', 0) or 0) for item in code_report['tables'])}"
        )
    summary["max_chunk_elapsed_sec"] = max(
        float(summary.get("max_chunk_elapsed_sec", 0.0) or 0.0),
        max(0.0, time.perf_counter() - chunk_started),
    )
    current_existing_future = next_existing_future
    next_existing_future = None
```

- [ ] **Step 4: 在 writer 收尾阶段同步新增 summary 字段**

```python
if self._duckdb_writer is not None:
    self._duckdb_writer.close_and_wait()
    summary["writer_flush_batches"] = int(getattr(self._duckdb_writer, "flush_batches", 0) or 0)
    summary["writer_flushed_codes"] = int(getattr(self._duckdb_writer, "flushed_codes", 0) or 0)
    summary["writer_queue_peak_size"] = int(getattr(self._duckdb_writer, "queue_peak_size", 0) or 0)
    summary["writer_total_flush_rows"] = int(getattr(self._duckdb_writer, "total_flush_rows", 0) or 0)
    summary["writer_last_flush_elapsed_sec"] = float(getattr(self._duckdb_writer, "last_flush_elapsed_sec", 0.0) or 0.0)
    summary["writer_max_flush_elapsed_sec"] = float(getattr(self._duckdb_writer, "max_flush_elapsed_sec", 0.0) or 0.0)
    if self._duckdb_writer.fatal_error is not None:
        raise RuntimeError(f"duckdb serial writer failed: {self._duckdb_writer.fatal_error}")
    self._duckdb_writer = None
```

- [ ] **Step 5: 运行测试，确认 summary 与 chunk 聚合通过**

Run:

```bash
pytest tests/utils/test_history_sync_duckdb_integration.py::test_run_sync_impl_exposes_chunk_and_summary_profiling -v
```

Expected:

```text
PASSED tests/utils/test_history_sync_duckdb_integration.py::test_run_sync_impl_exposes_chunk_and_summary_profiling
```

- [ ] **Step 6: 提交运行时性能剖析改动**

```bash
git add tests/utils/test_history_sync_duckdb_integration.py src/utils/history_sync_service.py
git commit -m "feat: add history sync runtime profiling summary"
```

## Task 5: 跑回归、查诊断、做最小收尾

**Files:**
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_duckdb_serial_writer.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\utils\test_history_sync_duckdb_integration.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\tests\unit\test_history_sync_service_duckdb.py`
- Modify: `d:\04.量化\jin-ce-zhi-suan\src\utils\history_sync_service.py`

- [ ] **Step 1: 跑本次新增和受影响的测试集合**

Run:

```bash
pytest tests/utils/test_duckdb_serial_writer.py tests/utils/test_history_sync_duckdb_integration.py tests/unit/test_history_sync_service_duckdb.py -v
```

Expected:

```text
all selected tests passed
```

- [ ] **Step 2: 再跑一次更贴近目标链路的回归测试**

Run:

```bash
pytest tests/utils/test_history_sync_checkpoint.py tests/utils/test_duckdb_provider_reuse_conn.py tests/utils/test_duckdb_serial_writer.py tests/utils/test_history_sync_duckdb_integration.py tests/unit/test_history_sync_service_duckdb.py -v
```

Expected:

```text
all selected tests passed
```

- [ ] **Step 3: 获取语言诊断并修复显而易见的问题**

Run:

```bash
python -m pytest tests/utils/test_duckdb_serial_writer.py tests/utils/test_history_sync_duckdb_integration.py tests/unit/test_history_sync_service_duckdb.py -q
```

Expected:

```text
.......                                                                  [100%]
```

- [ ] **Step 4: 提交最终收尾**

```bash
git add src/utils/history_sync_service.py tests/utils/test_duckdb_serial_writer.py tests/utils/test_history_sync_duckdb_integration.py tests/unit/test_history_sync_service_duckdb.py
git commit -m "feat: add history sync performance profiling metrics"
```

## Self-Review

- **Spec coverage:** 已覆盖 spec 中要求的 writer、table、code、chunk 四层埋点，覆盖 summary 聚合、状态接口可读字段、慢股票 TopN 与日志增强边界。
- **Placeholder scan:** 计划中未使用 `TODO`、`TBD`、`implement later`、`similar to task` 等占位写法。
- **Type consistency:** 计划内统一使用 `write_wait_elapsed_sec`、`write_exec_elapsed_sec`、`source_build_elapsed_sec`、`total_existing_keys_prefetch_elapsed_sec`、`slow_codes_topn` 这些与 spec 一致的字段名。
