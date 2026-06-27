"""条件筛选模块：后台自动刷新 + DuckDB 落库。

- 服务启动后常驻后台 `refresh_loop`，每分钟判定数据是否最新；不最新则调 `fetch_latest_metrics` 拉一次
- 结果优先写 DuckDB 表 `dat_screener_metrics`（按交易日分区），无 DuckDB 时回落 JSON 文件缓存
- 进度通过 `progress_log.push` 推送到 SSE，前端复用现有 EventSource 渲染，支持"续上"语义
- 非交易时间（盘后/周末）只读不写：DB 有当日数据直接跳过，缺数据时从 JSON 缓存回填
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from src.utils.screener_progress import progress_log

# ---------------------------------------------------------------------------
# 状态单例（供 API 查询）
# ---------------------------------------------------------------------------

@dataclass
class _RefreshState:
    running: bool = False
    last_updated_at: Optional[str] = None  # ISO 时间串
    last_trade_date: Optional[str] = None  # YYYY-MM-DD
    last_status: str = "idle"              # idle / running / failed / backfill
    last_error: Optional[str] = None
    last_row_count: int = 0
    last_elapsed_sec: float = 0.0
    fail_streak: int = 0
    storage_mode: str = "unknown"          # duckdb / json / unknown
    loop_started: bool = False


state = _RefreshState()
_lock = threading.Lock()
_backfill_lock = threading.Lock()  # 与 refresh_lock 分离，避免回填阻塞刷新


# ---------------------------------------------------------------------------
# 时间 / 交易日辅助
# ---------------------------------------------------------------------------

def _in_trading_hours(now: Optional[datetime] = None) -> bool:
    """判断是否在 A 股连续竞价时段（9:25-11:30，13:00-15:05），且非周末。

    不考虑节假日，节假日误判为交易日的后果只是多发一次网络请求，可接受。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 25 <= t <= 11 * 60 + 30
    afternoon = 13 * 60 <= t <= 15 * 60 + 5
    return morning or afternoon


def _get_latest_trading_date() -> str:
    # 复用 screener_data_provider 的实现，避免重复
    from src.utils.screener_data_provider import _get_latest_trading_date as _impl
    return _impl()


# ---------------------------------------------------------------------------
# DuckDB 读写（dat_screener_metrics 表）
# ---------------------------------------------------------------------------

_TABLE_NAME = "dat_screener_metrics"

_COLUMNS = [
    "trade_date", "code", "name", "exchange",
    "price", "change_pct", "open_pct", "high_pct", "low_pct",
    "volume", "amount", "turnover", "amplitude",
    "total_mv", "float_mv",
    "limit_up", "limit_down", "is_main_board",
    "screener_time", "source_main", "updated_at",
]

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
    trade_date DATE NOT NULL,
    code VARCHAR(16) NOT NULL,
    name VARCHAR(32),
    exchange VARCHAR(4),
    price DOUBLE,
    change_pct DOUBLE,
    open_pct DOUBLE,
    high_pct DOUBLE,
    low_pct DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    turnover DOUBLE,
    amplitude DOUBLE,
    total_mv DOUBLE,
    float_mv DOUBLE,
    limit_up BOOLEAN,
    limit_down BOOLEAN,
    is_main_board BOOLEAN,
    screener_time VARCHAR(24),
    source_main VARCHAR(16) DEFAULT 'duckdb',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, code)
)
"""


def _get_conn():
    """获取 DuckDB 写连接（失败返回 None）。"""
    try:
        from src.utils.duckdb_provider import DuckDbProvider
        provider = DuckDbProvider()
        # DuckDbProvider 暴露 _connect；用 read_only=False 拿到写连接
        return provider._connect(read_only=False)
    except Exception as e:
        logger.debug(f"screener_auto_refresh: 获取 DuckDB 连接失败: {e}")
        return None


def ensure_table() -> bool:
    """建表；并兜底补齐旧表可能缺失的 source_main 列。返回是否可用。"""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        conn.execute(_CREATE_SQL)
        # 旧表升级：若缺 source_main 列则补上，避免 INSERT 报列数不匹配
        try:
            cols = [row[0] for row in conn.execute(f"DESCRIBE {_TABLE_NAME}").fetchall()]
            if "source_main" not in cols:
                conn.execute(
                    f"ALTER TABLE {_TABLE_NAME} ADD COLUMN source_main VARCHAR(16) DEFAULT 'duckdb'"
                )
                logger.info(f"screener_auto_refresh: 补齐 {_TABLE_NAME}.source_main 列")
        except Exception as e:
            logger.debug(f"screener_auto_refresh: DESCRIBE/ALTER 跳过: {e}")
        return True
    except Exception as e:
        logger.warning(f"screener_auto_refresh: 建表失败: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _save_to_duckdb(rows: List[Dict[str, Any]], trade_date: str) -> bool:
    """整日替换写入 dat_screener_metrics（DELETE + INSERT）。

    把 `_source_main` 写到 `source_main` 列；前端通过 `_source_main` 读取，落库时去掉前导下划线。
    """
    if not rows:
        return False
    conn = _get_conn()
    if conn is None:
        return False
    try:
        import pandas as pd
        # 仅保留目标列，缺失字段补 None；updated_at 让 DB 默认
        clean_rows = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            # _source_main 缺省时按 storage_mode 兜底成 'duckdb'，前端统计不再被归到 unknown
            src = r.get("_source_main") or r.get("source_main") or "duckdb"
            clean_rows.append({
                "trade_date": trade_date,
                "code": r.get("code"),
                "name": str(r.get("name", "") or "")[:32],
                "exchange": r.get("exchange"),
                "price": r.get("price"),
                "change_pct": r.get("change_pct"),
                "open_pct": r.get("open_pct"),
                "high_pct": r.get("high_pct"),
                "low_pct": r.get("low_pct"),
                "volume": float(r["volume"]) if r.get("volume") is not None else None,
                "amount": r.get("amount"),
                "turnover": r.get("turnover"),
                "amplitude": r.get("amplitude"),
                "total_mv": r.get("total_mv"),
                "float_mv": r.get("float_mv"),
                "limit_up": bool(r["limit_up"]) if r.get("limit_up") is not None else None,
                "limit_down": bool(r["limit_down"]) if r.get("limit_down") is not None else None,
                "is_main_board": bool(r["is_main_board"]) if r.get("is_main_board") is not None else None,
                "screener_time": r.get("screener_time"),
                "source_main": str(src)[:16],
                "updated_at": now_str,
            })
        df = pd.DataFrame(clean_rows)
        conn.execute(f"DELETE FROM {_TABLE_NAME} WHERE trade_date = ?", [trade_date])
        # 显式列名 INSERT：避免 DataFrame 列顺序与表 schema 不一致时被按位置错位写入
        cols = ", ".join(_COLUMNS)
        conn.execute(f"INSERT INTO {_TABLE_NAME} ({cols}) SELECT {cols} FROM df")
        return True
    except Exception as e:
        logger.warning(f"screener_auto_refresh: 写 DB 失败: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_from_duckdb(trade_date: str) -> List[Dict[str, Any]]:
    """读当日快照，返回 records（空列表表示缺失）。

    输出字段全部为 JSON 友好类型（日期/时间转字符串，None 保留），
    与 fetch_latest_metrics 的 dict 形态保持一致。
    DB 列 `source_main` 在这里被还原为前端/统计侧约定的 `_source_main`。
    """
    conn = _get_conn()
    if conn is None:
        return []
    try:
        df = conn.execute(
            f"SELECT * FROM {_TABLE_NAME} WHERE trade_date = ? ORDER BY code",
            [trade_date],
        ).fetchdf()
        if df is None or df.empty:
            return []
        # 把 Timestamp / date 列统一成字符串，避免 API 序列化踩雷
        for col in ("trade_date", "updated_at"):
            if col in df.columns:
                df[col] = df[col].astype(str).where(df[col].notna(), None)
        # source_main → _source_main：保持与 fetch_latest_metrics 原始字段名一致
        if "source_main" in df.columns:
            df = df.rename(columns={"source_main": "_source_main"})
        else:
            # 旧数据缺列时兜底成 duckdb，避免前端统计被归到 unknown
            df["_source_main"] = "duckdb"
        records = df.to_dict("records")
        # 过滤掉 NaN，避免 JSON 序列化出现 null 之外的非法值
        for r in records:
            for k, v in list(r.items()):
                if isinstance(v, float) and v != v:  # noqa: E711
                    r[k] = None
        return records
    except Exception as e:
        # 表不存在也归为"缺失"
        logger.debug(f"screener_auto_refresh: 读 DB 失败: {e}")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _db_has_date(trade_date: str) -> bool:
    conn = _get_conn()
    if conn is None:
        return False
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {_TABLE_NAME} WHERE trade_date = ?",
            [trade_date],
        ).fetchone()
        return bool(row and row[0] and row[0] > 0)
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 文件缓存兜底
# ---------------------------------------------------------------------------

def _cache_path() -> str:
    from src.utils.screener_data_provider import CACHE_DIR
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "latest_metrics_v2.json")


def _read_cache_file() -> Optional[List[Dict[str, Any]]]:
    try:
        p = _cache_path()
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        return None
    return None


def _write_cache_file_atomic(rows: List[Dict[str, Any]]) -> bool:
    """原子写入 JSON 缓存（临时文件 + rename），避免并发读到半截内容。"""
    try:
        p = _cache_path()
        d = os.path.dirname(p)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".latest_metrics_", dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, default=str)
            # Windows rename 需要目标不存在；先删再替换
            if os.name == "nt" and os.path.exists(p):
                os.remove(p)
            os.replace(tmp, p)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.warning(f"screener_auto_refresh: 写缓存失败: {e}")
        return False


def _backfill_from_cache() -> bool:
    """盘外且 DB 缺数据时，从文件缓存回填到 DB（不触网）。"""
    with _backfill_lock:
        td = _get_latest_trading_date()
        if _db_has_date(td):
            return True
        rows = _read_cache_file()
        if not rows:
            # 再尝试昨日
            from datetime import timedelta
            for delta in range(1, 5):
                prev = (datetime.now() - timedelta(days=delta)).strftime("%Y-%m-%d")
                if _db_has_date(prev):
                    state.last_trade_date = prev
                    state.last_status = "backfill"
                    return True
            return False
        if not ensure_table():
            return False
        ok = _save_to_duckdb(rows, td)
        if ok:
            state.last_trade_date = td
            state.last_status = "backfill"
            state.last_row_count = len(rows)
            progress_log.push(f"[auto] 盘外从文件缓存回填 DB: {len(rows)} 条 ({td})", "info")
        return ok


# ---------------------------------------------------------------------------
# 新旧判定
# ---------------------------------------------------------------------------

def is_data_fresh() -> bool:
    """按"交易日 + 时间窗"判定数据是否最新。"""
    td = _get_latest_trading_date()
    if state.last_trade_date != td:
        return False
    if not state.last_updated_at:
        return False
    if _in_trading_hours():
        try:
            last = datetime.fromisoformat(state.last_updated_at)
            return (datetime.now() - last).total_seconds() < 300  # 5 分钟
        except Exception:
            return False
    # 盘外：当日已拉过即视为新
    return True


# ---------------------------------------------------------------------------
# 主入口：refresh_once
# ---------------------------------------------------------------------------

def refresh_once(force: bool = False) -> bool:
    """拉一次最新数据，写 DB + 缓存。force=True 忽略新旧判定。"""
    with _lock:
        if state.running:
            return False
        state.running = True
        state.last_status = "running"

    begin = time.time()
    try:
        from src.utils.screener_data_provider import fetch_latest_metrics

        progress_log.push("[auto] 后台刷新开始：拉取全市场最新行情...", "info")

        # 用线程跑阻塞的 IO（fetch_latest_metrics 内部逐只调 pytdx）
        rows = fetch_latest_metrics()
        if not rows:
            raise RuntimeError("fetch_latest_metrics 返回空")

        trade_date = _get_latest_trading_date()
        db_ok = False
        if ensure_table():
            db_ok = _save_to_duckdb(rows, trade_date)
        cache_ok = _write_cache_file_atomic(rows)

        elapsed = time.time() - begin
        with _lock:
            state.last_updated_at = datetime.now().isoformat(timespec="seconds")
            state.last_trade_date = trade_date
            state.last_row_count = len(rows)
            state.last_elapsed_sec = round(elapsed, 2)
            state.fail_streak = 0
            state.last_status = "idle"
            state.storage_mode = "duckdb" if db_ok else ("json" if cache_ok else "unknown")

        progress_log.push(
            f"[auto] 后台刷新完成：{len(rows)} 条，"
            f"DB={'✓' if db_ok else '×'} 缓存={'✓' if cache_ok else '×'}，耗时 {elapsed:.1f}s",
            "success",
        )
        return True

    except Exception as e:
        elapsed = time.time() - begin
        logger.error(f"screener_auto_refresh: refresh_once 失败: {e}", exc_info=True)
        with _lock:
            state.fail_streak += 1
            state.last_status = "failed"
            state.last_error = str(e)
            state.last_elapsed_sec = round(elapsed, 2)
        progress_log.push(f"[auto] 后台刷新失败: {e}", "error")
        return False
    finally:
        with _lock:
            state.running = False


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def refresh_loop():
    """常驻后台任务：每分钟判定一次。

    状态机：
    - 盘内 + 数据新：sleep 60s
    - 盘内 + 数据旧：refresh_once()
    - 盘外 + DB有当日：sleep 60s
    - 盘外 + DB缺数据：_backfill_from_cache()（不触网）
    - 连续失败 ≥3 次：sleep 300s 再重置
    """
    state.loop_started = True
    logger.info("[startup] screener auto refresh loop started")
    progress_log.push("[auto] 后台数据自检循环已启动（每分钟一次）", "info")

    # 启动后先跑一次建表探测，避免后续写表时才发现 DB 不可用
    try:
        await asyncio.to_thread(ensure_table)
    except Exception as e:
        logger.debug(f"screener_auto_refresh: 启动建表探测失败: {e}")

    while True:
        try:
            now = datetime.now()
            td = _get_latest_trading_date()

            if _in_trading_hours(now):
                if not is_data_fresh():
                    await asyncio.to_thread(refresh_once)
            else:
                # 盘外：DB 缺当日数据时从文件回填，不触网
                has_date = await asyncio.to_thread(_db_has_date, td)
                if not has_date:
                    await asyncio.to_thread(_backfill_from_cache)

            # 失败退避
            sleep_sec = 60
            if state.fail_streak >= 3:
                sleep_sec = 300
                progress_log.push(
                    f"[auto] 连续失败 {state.fail_streak} 次，退避 5 分钟",
                    "warning",
                )
            await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            logger.info("[shutdown] screener auto refresh loop cancelled")
            break
        except Exception as e:
            logger.error(f"screener_auto_refresh: loop 异常: {e}", exc_info=True)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------

def get_status() -> Dict[str, Any]:
    return {
        "running": state.running,
        "last_updated_at": state.last_updated_at,
        "last_trade_date": state.last_trade_date,
        "last_status": state.last_status,
        "last_error": state.last_error,
        "last_row_count": state.last_row_count,
        "last_elapsed_sec": state.last_elapsed_sec,
        "fail_streak": state.fail_streak,
        "storage_mode": state.storage_mode,
        "is_fresh": is_data_fresh(),
        "in_trading_hours": _in_trading_hours(),
        "loop_started": state.loop_started,
    }
