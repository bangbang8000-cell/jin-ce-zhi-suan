"""条件筛选器数据访问层。

从 DuckDB/stock_manager 等来源获取标的列表、行情指标、技术指标，
为 /api/screener/* 接口提供数据。
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

try:
    # AkShare 作为 DuckDB 缺失时的行情兜底来源；若环境未安装则安全降级。
    import akshare as ak
except Exception:
    ak = None

from src.utils.stock_manager import stock_manager
from src.utils.screener_progress import progress_log

CACHE_DIR = os.path.join("data", "screener_cache")
CACHE_TTL_SEC = 300  # 5 分钟


def _get_latest_trading_date() -> str:
    """获取最近一个交易日的日期（YYYY-MM-DD 格式）。

    简单规则：
    - 如果当前是交易时间（周一到周五 9:30-15:00），返回今天
    - 如果当前是交易日的非交易时间（如 15:00 之后），返回今天
    - 如果当前是周六，返回周五
    - 如果当前是周日，返回周五
    注意：不考虑节假日，如需精确判断需要交易日历。
    """
    now = datetime.now()
    weekday = now.weekday()  # 0=周一, 6=周日

    # 周六 -> 周五
    if weekday == 5:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # 周日 -> 周五
    elif weekday == 6:
        return (now - timedelta(days=2)).strftime("%Y-%m-%d")
    # 周一到周五 -> 今天
    else:
        return now.strftime("%Y-%m-%d")


def _in_trading_hours(now: Optional[datetime] = None) -> bool:
    """判断是否在 A 股连续竞价时段（9:25-11:30，13:00-15:05），且非周末。

    不考虑节假日；节假日误判为交易日的后果只是多发一次网络请求，可接受。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 25 <= t <= 11 * 60 + 30
    afternoon = 13 * 60 <= t <= 15 * 60 + 5
    return morning or afternoon


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _read_cache(key: str, ttl: int = CACHE_TTL_SEC) -> Optional[Any]:
    try:
        path = os.path.join(CACHE_DIR, f"{key}.json")
        if not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        if time.time() - mtime > ttl:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(key: str, data: Any):
    _ensure_cache_dir()
    path = os.path.join(CACHE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)


def _to_float_or_none(value: Any) -> Optional[float]:
    """将输入尽量转为 float；失败返回 None，避免中断筛选主流程。"""
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if value == "":
                return None
        f = float(value)
        if f != f:  # noqa: E711
            return None
        return f
    except Exception:
        return None


def _first_value(row: Dict[str, Any], candidates: List[str]) -> Any:
    """按候选列名顺序获取第一条可用值，兼容不同 AkShare 版本列名。"""
    for col in candidates:
        if col in row:
            val = row.get(col)
            if val is not None and str(val).strip() != "":
                return val
    return None


def _fetch_tdx_spot_map() -> Dict[str, Dict[str, Any]]:
    """使用 TdxProvider 获取全市场实时行情快照。

    优先级：pytdx > DuckDB > AkShare
    使用缓存减少重复请求，缓存有效期 3 分钟。
    """
    cached = _read_cache("tdx_spot_map_v1", ttl=180)
    if isinstance(cached, dict) and cached:
        return cached

    try:
        from src.utils.tdx_provider import TdxProvider
        tdx = TdxProvider()

        progress_log.push("正在连接 TDX 行情服务器...", "info")
        progress_log.push("（首次连接可能需要 60~90 秒探测最优节点）", "info")

        # 检查 TdxProvider 是否可用（使用招商银行作为测试股票）
        # 这一步内部会触发服务器预热重测，可能耗时较长
        ok, msg = tdx.check_connectivity("600036.SH")
        if not ok:
            logger.warning(f"TdxProvider 不可用: {msg}")
            progress_log.push(f"TDX 连接失败: {msg}", "error")
            return {}

        progress_log.push("TDX 行情服务器连接成功", "success")

        stock_manager.ensure_loaded()
        stocks = stock_manager.stocks

        out: Dict[str, Dict[str, Any]] = {}
        # 限制获取数量，避免超时（最多获取 500 只股票作为示例）
        # 实际使用时可以移除限制或改为分批获取
        max_stocks = min(len(stocks), 5000)
        progress_log.push(f"开始获取 {max_stocks} 只股票行情数据...", "info")

        # 预收集 symbol 列表，用于批量获取 quotes（含昨收 last_close）
        # 逐只 fetch_kline_data 无法拿到昨收，且该接口签名不接受 count 参数，
        # 之前因此永远抛 TypeError，导致 change_pct 全部退化为 0。
        symbol_list: list = []
        code_by_symbol: Dict[str, str] = {}
        for s in stocks[:max_stocks]:
            code = _normalize_stock_code(s.get("code", ""))
            if not code:
                continue
            try:
                sym = tdx._normalize_symbol(code)
                symbol_list.append(sym)
                code_by_symbol[sym] = code
            except Exception:
                continue

        # 批量拉 quotes（每批 80 只，避免单次请求过大）
        quotes_by_sym: Dict[str, Dict[str, Any]] = {}
        try:
            q = tdx._ensure_quotes()
            if q is not None:
                batch_size = 80
                for j in range(0, len(symbol_list), batch_size):
                    batch = symbol_list[j:j + batch_size]
                    try:
                        df_q = q.quotes(batch)
                        if df_q is None or df_q.empty:
                            continue
                        for _, r in df_q.iterrows():
                            sym_raw = str(r.get("code", "")).strip()
                            # 匹配回 code_by_symbol：兼容返回原始 6 位或带后缀形式
                            matched_sym = None
                            if sym_raw in code_by_symbol:
                                matched_sym = sym_raw
                            else:
                                for sym_key in code_by_symbol:
                                    if sym_key.endswith(sym_raw) or sym_key.startswith(sym_raw):
                                        matched_sym = sym_key
                                        break
                            if matched_sym:
                                quotes_by_sym[matched_sym] = r.to_dict()
                    except Exception as e:
                        logger.debug(f"TDX quotes 批次失败: {e}")
        except Exception as e:
            logger.debug(f"TDX quotes 批量初始化失败: {e}")

        for i, s in enumerate(stocks[:max_stocks]):
            code = _normalize_stock_code(s.get("code", ""))
            if not code:
                continue

            try:
                bar = tdx.get_latest_bar(code)
                if not bar:
                    continue

                # 标准化字段
                price = float(bar.get("close", 0) or 0)
                open_val = float(bar.get("open", 0) or 0)
                high_val = float(bar.get("high", 0) or 0)
                low_val = float(bar.get("low", 0) or 0)
                volume = float(bar.get("vol", 0) or 0)
                amount = float(bar.get("amount", 0) or 0)

                # 计算涨跌幅：优先使用 quotes 返回的 last_close（昨收价）
                sym_key = tdx._normalize_symbol(code)
                q_row = quotes_by_sym.get(sym_key, {}) if quotes_by_sym else {}
                pre_close_raw = q_row.get("last_close") if q_row else None
                try:
                    pre_close = float(pre_close_raw) if pre_close_raw not in (None, "", 0) else 0.0
                except Exception:
                    pre_close = 0.0

                change_pct = 0.0
                if pre_close > 0:
                    change_pct = round((price - pre_close) / pre_close * 100, 2)
                else:
                    # 兜底：用 bar 自身无法得到昨收，保持 0；DuckDB/AkShare 会在后续补齐
                    pre_close = price

                # 开高低涨幅
                open_pct = round((open_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                high_pct = round((high_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                low_pct = round((low_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0

                out[code] = {
                    "price": round(price, 2) if price > 0 else None,
                    "change_pct": round(change_pct, 2),
                    "open_pct": open_pct,
                    "high_pct": high_pct,
                    "low_pct": low_pct,
                    "volume": int(volume) if volume > 0 else None,
                    "amount": round(amount / 10000.0, 2) if amount > 0 else None,  # 转为万元
                    "turnover": None,  # TdxProvider 不提供换手率
                    "amplitude": round((high_val - low_val) / pre_close * 100, 2) if pre_close > 0 else 0,
                    "total_mv": None,  # TdxProvider 不提供市值
                    "float_mv": None,
                    "_source": "pytdx",
                }

                # 每 100 只股票输出一次进度
                if (i + 1) % 100 == 0:
                    logger.info(f"TdxProvider 已获取 {i + 1}/{max_stocks} 只股票行情")
                    progress_log.push(f"已获取 {i + 1}/{max_stocks} 只股票行情", "info")

            except Exception as e:
                logger.debug(f"TdxProvider 获取 {code} 失败: {e}")
                continue

        if out:
            _write_cache("tdx_spot_map_v1", out)
            logger.info(f"TdxProvider 成功获取 {len(out)} 只股票行情")
            progress_log.push(f"TDX 行情获取完成，共 {len(out)} 只股票", "success")

        return out
    except Exception as e:
        logger.warning(f"TdxProvider 获取全市场行情失败: {e}")
        progress_log.push(f"TDX 行情获取失败: {e}", "error")
        return {}


def _fetch_akshare_spot_map() -> Dict[str, Dict[str, Any]]:
    """拉取 AkShare 全市场快照并标准化为 code->metrics 映射。"""
    # 使用独立缓存键，避免每次筛选都打网络请求。
    cached = _read_cache("akshare_spot_map_v1", ttl=180)
    if isinstance(cached, dict) and cached:
        return cached
    if ak is None:
        return {}
    try:
        progress_log.push("正在通过 AkShare 获取行情快照...", "info")
        # 采用一次全量快照，再按代码映射，减少逐票请求开销。
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return {}
        rows = df.to_dict("records")
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            # 兼容不同列名版本（代码/名称/现价等）。
            code_raw = _first_value(row, ["代码", "code", "symbol", "证券代码"])
            code = _normalize_stock_code(code_raw)
            if not code:
                continue
            price = _to_float_or_none(_first_value(row, ["最新价", "close", "最新", "price"]))
            pre_close = _to_float_or_none(_first_value(row, ["昨收", "pre_close", "昨收价"]))
            open_val = _to_float_or_none(_first_value(row, ["今开", "open", "开盘"]))
            high_val = _to_float_or_none(_first_value(row, ["最高", "high"]))
            low_val = _to_float_or_none(_first_value(row, ["最低", "low"]))
            change_pct = _to_float_or_none(_first_value(row, ["涨跌幅", "pct_chg", "change_pct"]))
            turnover = _to_float_or_none(_first_value(row, ["换手率", "turnover_rate", "turnover"]))
            amount_raw = _to_float_or_none(_first_value(row, ["成交额", "amount", "成交金额"]))
            volume_raw = _to_float_or_none(_first_value(row, ["成交量", "volume", "vol"]))
            amp = _to_float_or_none(_first_value(row, ["振幅", "amplitude"]))
            total_mv_raw = _to_float_or_none(_first_value(row, ["总市值", "total_mv"]))
            float_mv_raw = _to_float_or_none(_first_value(row, ["流通市值", "float_mv", "circ_mv"]))

            # 行情金额口径统一：筛选器内部 amount 使用"万元"。
            amount_wan = round(amount_raw / 10000.0, 2) if amount_raw is not None else None
            # 市值统一：筛选器目录是"亿元"。
            total_mv_yi = round(total_mv_raw / 100000000.0, 2) if total_mv_raw is not None else None
            float_mv_yi = round(float_mv_raw / 100000000.0, 2) if float_mv_raw is not None else None
            # 开高低涨幅优先使用昨收推导，缺失时返回 None。
            open_pct = None
            high_pct = None
            low_pct = None
            if pre_close is not None and pre_close > 0:
                if open_val is not None:
                    open_pct = round((open_val - pre_close) / pre_close * 100, 2)
                if high_val is not None:
                    high_pct = round((high_val - pre_close) / pre_close * 100, 2)
                if low_val is not None:
                    low_pct = round((low_val - pre_close) / pre_close * 100, 2)
            out[code] = {
                "price": round(price, 2) if price is not None else None,
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
                "open_pct": open_pct,
                "high_pct": high_pct,
                "low_pct": low_pct,
                "volume": int(volume_raw) if volume_raw is not None else None,
                "amount": amount_wan,
                "turnover": round(turnover, 2) if turnover is not None else None,
                "amplitude": round(amp, 2) if amp is not None else None,
                "total_mv": total_mv_yi,
                "float_mv": float_mv_yi,
            }
        _write_cache("akshare_spot_map_v1", out)
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 交易所派生（基于代码前缀，无需外部依赖）
# ---------------------------------------------------------------------------
def _infer_exchange(code: str) -> str:
    """根据股票代码前缀推断交易所。"""
    c = str(code).strip().upper()
    if c.startswith("6"):
        return "上交所"
    if c.startswith("0") or c.startswith("3"):
        return "深交所"
    if c.startswith("8") or c.startswith("4"):
        return "北交所"
    return "未知"


def _is_main_board_code(code: str) -> bool:
    """判断是否为A股主板代码（用于筛选层主板限定，不影响交易引擎）。"""
    c = str(code or "").strip().upper()
    # 兼容带后缀代码写法。
    if "." in c:
        c = c.split(".", 1)[0]
    # 上交所主板常见前缀：600/601/603/605。
    if c.startswith(("600", "601", "603", "605")):
        return True
    # 深交所主板常见前缀：000/001/002（并板后仍视作主板）。
    if c.startswith(("000", "001", "002")):
        return True
    # 明确排除：创业板300、科创板688、北交所8/4。
    return False


def _normalize_stock_code(raw_code: Any) -> str:
    """标准化股票代码为6位数字（无后缀），用于筛选展示与查询对齐。"""
    text = str(raw_code or "").strip().upper()
    if not text:
        return ""
    # 兼容 SH600000 / SZ000001 / BJ430001 形态。
    m_prefixed = re.match(r"^(SH|SZ|BJ)(\d{1,6})$", text)
    if m_prefixed:
        return m_prefixed.group(2).zfill(6)
    # 兼容 600000.SH / 000001.SZ / 430001.BJ 形态。
    m_suffixed = re.match(r"^(\d{1,6})\.(SH|SZ|BJ|XSHG|XSHE|XBJ)$", text)
    if m_suffixed:
        return m_suffixed.group(1).zfill(6)
    # 纯数字场景：补齐到6位，修复"1/2/6"这类被去零代码。
    if text.isdigit():
        return text.zfill(6)
    # 兜底：提取数字部分，最多取后6位，避免异常字符导致空值。
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return digits[-6:].zfill(6)
    return ""


# ---------------------------------------------------------------------------
# 前置过滤选项
# ---------------------------------------------------------------------------
def get_filter_options() -> Dict[str, List[str]]:
    """返回4个前置下拉框的可选项。"""
    stock_manager.ensure_loaded()
    stocks = stock_manager.stocks
    exchanges = sorted({_infer_exchange(s.get("code", "")) for s in stocks})
    # Phase 1: 地域/企业性质/两融 返回占位，Phase 2 接 Tushare
    return {
        "exchange": ["全部"] + [e for e in exchanges if e != "未知"],
        "region": [],       # TODO: Tushare stock_basic.area
        "enterprise_type": [],  # TODO: Tushare stock_basic.list_date / 性质
        "margin_trading": ["全部", "融资", "融券", "两融"],  # TODO: Tushare margin trading
    }


# ---------------------------------------------------------------------------
# 筛选条件目录树（8 个 tab 下的分类与字段）
# ---------------------------------------------------------------------------
def get_catalog() -> Dict[str, List[Dict[str, Any]]]:
    """返回全部筛选条件的目录结构。

    每个 tab 下是 category 列表，每个 category 包含：
      - category: 分类名
      - fields: 字段列表，每个字段有 {key, label, type, unit, options, ...}
    """
    return {
        "market": [
            {
                "category": "股票价格",
                "fields": [
                    {"key": "price", "label": "最新价", "type": "range", "unit": "元", "min_val": 0, "max_val": 9999},
                ],
            },
            {
                "category": "股价涨幅",
                "fields": [
                    {"key": "change_pct", "label": "涨跌幅", "type": "range", "unit": "%", "min_val": -20, "max_val": 20, "step": 0.1},
                    {"key": "change_5d", "label": "近5日涨幅", "type": "range", "unit": "%", "min_val": -50, "max_val": 50},
                    {"key": "change_20d", "label": "近20日涨幅", "type": "range", "unit": "%", "min_val": -50, "max_val": 50},
                    {"key": "change_60d", "label": "近60日涨幅", "type": "range", "unit": "%", "min_val": -50, "max_val": 50},
                ],
            },
            {
                "category": "涨跌停标记",
                "fields": [
                    {"key": "limit_up", "label": "涨停", "type": "toggle"},
                    {"key": "limit_down", "label": "跌停", "type": "toggle"},
                    {"key": "is_main_board", "label": "主板股票", "type": "toggle"},
                ],
            },
            {
                "category": "融资融券",
                "fields": [
                    {"key": "is_margin", "label": "融资融券标的", "type": "toggle"},
                ],
            },
            {
                "category": "股价振幅",
                "fields": [
                    {"key": "amplitude", "label": "振幅", "type": "range", "unit": "%", "min_val": 0, "max_val": 20},
                ],
            },
            {
                "category": "成交额",
                "fields": [
                    {"key": "amount", "label": "成交额", "type": "range", "unit": "万元", "min_val": 0, "max_val": 99999999},
                ],
            },
            {
                "category": "成交量",
                "fields": [
                    {"key": "volume", "label": "成交量", "type": "range", "unit": "手", "min_val": 0, "max_val": 99999999},
                ],
            },
            {
                "category": "换手率",
                "fields": [
                    {"key": "turnover", "label": "换手率", "type": "range", "unit": "%", "min_val": 0, "max_val": 50},
                ],
            },
            {
                "category": "股本和市值",
                "fields": [
                    {"key": "total_mv", "label": "总市值", "type": "range", "unit": "亿元", "min_val": 0, "max_val": 999999},
                    {"key": "float_mv", "label": "流通市值", "type": "range", "unit": "亿元", "min_val": 0, "max_val": 999999},
                    {"key": "total_shares", "label": "总股本", "type": "range", "unit": "亿股", "min_val": 0, "max_val": 99999},
                    {"key": "float_shares", "label": "流通股本", "type": "range", "unit": "亿股", "min_val": 0, "max_val": 99999},
                ],
            },
            {
                "category": "资金净流入",
                "fields": [
                    {"key": "net_inflow", "label": "主力净流入", "type": "range", "unit": "万元", "min_val": -999999, "max_val": 999999},
                ],
            },
            {
                "category": "港资持股",
                "fields": [
                    {"key": "hk_hold_ratio", "label": "港资持股比例", "type": "range", "unit": "%", "min_val": 0, "max_val": 50},
                ],
            },
            {
                "category": "日内行情",
                "fields": [
                    {"key": "open_pct", "label": "开盘涨幅", "type": "range", "unit": "%", "min_val": -20, "max_val": 20},
                    {"key": "high_pct", "label": "最高涨幅", "type": "range", "unit": "%", "min_val": -20, "max_val": 20},
                    {"key": "low_pct", "label": "最低涨幅", "type": "range", "unit": "%", "min_val": -20, "max_val": 20},
                ],
            },
            {
                "category": "新股指标",
                "fields": [
                    {"key": "is_new", "label": "上市新股(近60日)", "type": "toggle"},
                    {"key": "listing_days", "label": "上市天数", "type": "range", "min_val": 1, "max_val": 10000},
                ],
            },
            {
                "category": "AH股溢价率",
                "fields": [
                    {"key": "ah_premium", "label": "AH溢价率", "type": "range", "unit": "%", "min_val": -100, "max_val": 500},
                ],
            },
            {
                "category": "上市天数",
                "fields": [
                    {"key": "listing_days2", "label": "上市天数", "type": "range", "min_val": 1, "max_val": 10000},
                ],
            },
            {
                "category": "交易天数",
                "fields": [
                    {"key": "trading_days", "label": "近N日交易天数", "type": "range", "min_val": 1, "max_val": 250},
                ],
            },
        ],
        "technical": [
            {
                "category": "均线系统",
                "fields": [
                    {"key": "ma5", "label": "MA5", "type": "range", "unit": "元"},
                    {"key": "ma10", "label": "MA10", "type": "range", "unit": "元"},
                    {"key": "ma20", "label": "MA20", "type": "range", "unit": "元"},
                    {"key": "ma60", "label": "MA60", "type": "range", "unit": "元"},
                    {"key": "price_vs_ma20", "label": "股价相对MA20", "type": "range", "unit": "%"},
                ],
            },
            {
                "category": "MACD",
                "fields": [
                    {"key": "macd_dif", "label": "DIF", "type": "range"},
                    {"key": "macd_dea", "label": "DEA", "type": "range"},
                    {"key": "macd_hist", "label": "MACD柱", "type": "range"},
                    {"key": "macd_golden_cross", "label": "金叉", "type": "toggle"},
                    {"key": "macd_death_cross", "label": "死叉", "type": "toggle"},
                ],
            },
            {
                "category": "KDJ",
                "fields": [
                    {"key": "kdj_k", "label": "K值", "type": "range", "min_val": 0, "max_val": 100},
                    {"key": "kdj_d", "label": "D值", "type": "range", "min_val": 0, "max_val": 100},
                    {"key": "kdj_j", "label": "J值", "type": "range", "min_val": 0, "max_val": 100},
                ],
            },
            {
                "category": "RSI",
                "fields": [
                    {"key": "rsi_6", "label": "RSI6", "type": "range", "min_val": 0, "max_val": 100},
                    {"key": "rsi_12", "label": "RSI12", "type": "range", "min_val": 0, "max_val": 100},
                    {"key": "rsi_24", "label": "RSI24", "type": "range", "min_val": 0, "max_val": 100},
                ],
            },
            {
                "category": "BOLL",
                "fields": [
                    {"key": "boll_upper", "label": "上轨", "type": "range", "unit": "元"},
                    {"key": "boll_mid", "label": "中轨", "type": "range", "unit": "元"},
                    {"key": "boll_lower", "label": "下轨", "type": "range", "unit": "元"},
                ],
            },
            {
                "category": "ATR",
                "fields": [
                    {"key": "atr_14", "label": "ATR14", "type": "range", "unit": "元"},
                ],
            },
        ],
        "financial": [
            {
                "category": "估值指标",
                "fields": [
                    {"key": "pe_ttm", "label": "PE(TTM)", "type": "range"},
                    {"key": "pb", "label": "PB", "type": "range"},
                    {"key": "ps_ttm", "label": "PS(TTM)", "type": "range"},
                ],
            },
            {
                "category": "盈利指标",
                "fields": [
                    {"key": "roe", "label": "ROE", "type": "range", "unit": "%"},
                    {"key": "roa", "label": "ROA", "type": "range", "unit": "%"},
                    {"key": "gross_margin", "label": "毛利率", "type": "range", "unit": "%"},
                    {"key": "net_margin", "label": "净利率", "type": "range", "unit": "%"},
                ],
            },
            {
                "category": "成长性",
                "fields": [
                    {"key": "revenue_yoy", "label": "营收同比增长", "type": "range", "unit": "%"},
                    {"key": "profit_yoy", "label": "净利润同比增长", "type": "range", "unit": "%"},
                ],
            },
        ],
        "report": [
            {"category": "财报条目", "fields": [
                {"key": "report_note", "label": "说明", "type": "text", "placeholder": "Phase 2 支持"},
            ]},
        ],
        "company": [
            {"category": "公司信息", "fields": [
                {"key": "company_note", "label": "说明", "type": "text", "placeholder": "Phase 2 支持"},
            ]},
        ],
        "analyst": [
            {"category": "分析师评级", "fields": [
                {"key": "analyst_note", "label": "说明", "type": "text", "placeholder": "Phase 2 支持"},
            ]},
        ],
        "index": [
            {"category": "大盘指标", "fields": [
                {"key": "index_note", "label": "说明", "type": "text", "placeholder": "Phase 2 支持"},
            ]},
        ],
        "custom": [
            {"category": "自定义条件", "fields": [
                {"key": "custom_expr", "label": "表达式", "type": "text", "placeholder": "例如: price < 10 AND turnover > 3"},
            ]},
        ],
    }


# ---------------------------------------------------------------------------
# 核心筛选逻辑
# ---------------------------------------------------------------------------
def get_duckdb_conn():
    """获取 DuckDB 连接（复用已有 provider）。

    注意：DuckDbProvider 暴露的是 `_connect(read_only=...)`，之前误用 `get_connection()`
    导致这里永远返回 None，DuckDB 路径在筛选器里实际上是死的。
    写路径请用 `screener_auto_refresh._get_conn()`。
    """
    try:
        from src.utils.duckdb_provider import DuckDbProvider
        provider = DuckDbProvider()
        return provider._connect(read_only=False)
    except Exception:
        return None


def _compute_technical_indicators(df: pd.DataFrame) -> Dict[str, float]:
    """对日K线 DataFrame 计算常用技术指标，返回 dict。"""
    result = {}
    if df is None or df.empty or len(df) < 20:
        return result

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # MA
    for n in [5, 10, 20, 60]:
        ma = close.rolling(window=n).mean()
        result[f"ma{n}"] = float(ma.iloc[-1]) if not ma.iloc[-1] != ma.iloc[-1] else None  # noqa: E711

    # MACD (12, 26, 9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = 2 * (dif - dea)
    result["macd_dif"] = float(dif.iloc[-1]) if not dif.iloc[-1] != dif.iloc[-1] else None
    result["macd_dea"] = float(dea.iloc[-1]) if not dea.iloc[-1] != dea.iloc[-1] else None
    result["macd_hist"] = float(hist.iloc[-1]) if not hist.iloc[-1] != hist.iloc[-1] else None
    if len(dif) >= 2:
        result["macd_golden_cross"] = bool(dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2])
        result["macd_death_cross"] = bool(dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2])

    # KDJ (9, 3, 3)
    low_n = low.rolling(window=9).min()
    high_n = high.rolling(window=9).max()
    rsv = (close - low_n) / (high_n - low_n) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    result["kdj_k"] = float(k.iloc[-1]) if not k.iloc[-1] != k.iloc[-1] else None
    result["kdj_d"] = float(d.iloc[-1]) if not d.iloc[-1] != d.iloc[-1] else None
    result["kdj_j"] = float(j.iloc[-1]) if not j.iloc[-1] != j.iloc[-1] else None

    # RSI (6, 12, 24)
    for period in [6, 12, 24]:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        result[f"rsi_{period}"] = float(rsi.iloc[-1]) if not rsi.iloc[-1] != rsi.iloc[-1] else None

    # BOLL (20, 2)
    ma20 = close.rolling(window=20).mean()
    std20 = close.rolling(window=20).std()
    result["boll_upper"] = float(ma20.iloc[-1] + 2 * std20.iloc[-1]) if not std20.iloc[-1] != std20.iloc[-1] else None
    result["boll_mid"] = float(ma20.iloc[-1]) if not ma20.iloc[-1] != ma20.iloc[-1] else None
    result["boll_lower"] = float(ma20.iloc[-1] - 2 * std20.iloc[-1]) if not std20.iloc[-1] != std20.iloc[-1] else None

    # ATR (14)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()
    result["atr_14"] = float(atr.iloc[-1]) if not atr.iloc[-1] != atr.iloc[-1] else None

    return result


def fetch_latest_metrics() -> List[Dict[str, Any]]:
    """获取所有标的最新行情指标，带文件缓存。

    数据源优先级：
      1) DuckDB `dat_screener_metrics`（后台自动刷新落库的当日快照）
      2) 文件缓存 `latest_metrics_v2.json`（TTL 5 分钟）
      3) pytdx > DuckDB dat_day > AkShare > 本地股票清单（完整重算链路）
    """
    trade_date = _get_latest_trading_date()

    # 优先级 1：从 DuckDB 直读当日快照（后台 refresh_loop 已落库）
    try:
        from src.utils.screener_auto_refresh import (
            _read_from_duckdb,
            _db_has_date,
            _in_trading_hours,
        )
        # 盘内：必须 5 分钟内的新数据；盘外：当日有数据即视为有效
        if _in_trading_hours() or _db_has_date(trade_date):
            rows = _read_from_duckdb(trade_date)
            if rows and len(rows) > 1000:
                return rows
    except Exception as e:
        logger.debug(f"fetch_latest_metrics: DuckDB 读取失败，回退到原链路: {e}")

    # 优先级 2：文件缓存（兜底，避免后台任务未启动时每次都重算）
    cached = _read_cache("latest_metrics_v2")
    if cached is not None:
        return cached

    stock_manager.ensure_loaded()
    stocks = stock_manager.stocks
    conn = get_duckdb_conn()
    # 先准备 pytdx 快照映射（最高优先级）
    tdx_spot_map = _fetch_tdx_spot_map()
    # 再准备 AkShare 快照映射，作为 DuckDB 缺失字段兜底。
    ak_spot_map = _fetch_akshare_spot_map()
    # 使用最近交易日作为默认 screener_time，而不是当前时间（避免周末显示今天）
    latest_trading_date = trade_date
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    results = []
    for s in stocks:
        code = _normalize_stock_code(s.get("code", ""))
        name = str(s.get("name", "")).strip()
        if not code:
            continue
        exchange = _infer_exchange(code)

        # 补齐 .SH/.SZ 后缀用于 DuckDB 查询
        db_code = code
        if "." not in db_code:
            if db_code.startswith("6"):
                db_code = f"{db_code}.SH"
            elif db_code.startswith("0") or db_code.startswith("3"):
                db_code = f"{db_code}.SZ"
            elif db_code.startswith("8") or db_code.startswith("4"):
                db_code = f"{db_code}.BJ"

        row = {"code": code, "name": name, "exchange": exchange, "screener_time": f"{latest_trading_date} 15:00:00", "price_date": latest_trading_date}
        # 标记是否主板，供"主板限定"策略直接执行筛选。
        row["is_main_board"] = _is_main_board_code(code)
        # 记录数据来源主标签，便于前端展示"来源占比"。
        row["_source_main"] = "basic"
        duckdb_hit = False
        pytdx_hit = False

        # 优先级1：pytdx 实时行情（最高优先级）
        tdx_row = tdx_spot_map.get(code, {})
        if isinstance(tdx_row, dict) and tdx_row and tdx_row.get("price") is not None:
            pytdx_hit = True
            # 使用 pytdx 数据填充核心行情字段
            for k in ["price", "change_pct", "open_pct", "high_pct", "low_pct", "volume", "amount", "amplitude"]:
                if tdx_row.get(k) is not None:
                    row[k] = tdx_row.get(k)
            # 设置涨跌停标记
            if row.get("change_pct") is not None:
                cp = float(row.get("change_pct") or 0.0)
                row["limit_up"] = abs(cp - 10.0) < 0.1 or abs(cp - 20.0) < 0.1
                row["limit_down"] = abs(cp + 10.0) < 0.1 or abs(cp + 20.0) < 0.1

        # 优先级2：DuckDB 历史数据（用于技术指标和时序条件）
        if conn:
            try:
                df = conn.execute(
                    "SELECT * FROM dat_day WHERE code = ? ORDER BY trade_date DESC LIMIT 1",
                    [db_code]
                ).fetchdf()
                if df is not None and not df.empty and len(df) > 0:
                    duckdb_hit = True
                    latest = df.iloc[0]
                    # 使用数据实际的交易日期作为 screener_time，而不是当前时间
                    trade_date = latest.get("trade_date")
                    if trade_date is not None:
                        try:
                            # 兼容多种日期格式：datetime/date/字符串
                            if hasattr(trade_date, "strftime"):
                                row["screener_time"] = trade_date.strftime("%Y-%m-%d") + " 15:00:00"  # 收盘时间
                                row["price_date"] = trade_date.strftime("%Y-%m-%d")
                            else:
                                trade_date_str = str(trade_date)[:10]
                                row["screener_time"] = trade_date_str + " 15:00:00"
                                row["price_date"] = trade_date_str
                        except Exception:
                            row["screener_time"] = f"{latest_trading_date} 15:00:00"
                    close_val = float(latest.get("close", 0) or 0)
                    open_val = float(latest.get("open", 0) or 0)
                    high_val = float(latest.get("high", 0) or 0)
                    low_val = float(latest.get("low", 0) or 0)
                    volume_val = float(latest.get("volume", 0) or 0)
                    amount_val = float(latest.get("amount", 0) or 0)
                    turnover_val = float(latest.get("turnover", 0) or 0)
                    pre_close = float(latest.get("pre_close", 0) or 0)
                    if pre_close <= 0:
                        pre_close = close_val

                    row["price"] = round(close_val, 2)
                    row["open_pct"] = round((open_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                    row["high_pct"] = round((high_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                    row["low_pct"] = round((low_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                    row["change_pct"] = round((close_val - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                    row["volume"] = int(volume_val)
                    row["amount"] = round(amount_val / 10000, 2)  # 转为万元
                    row["turnover"] = round(turnover_val, 2)
                    row["amplitude"] = round((high_val - low_val) / pre_close * 100, 2) if pre_close > 0 else 0
                    row["limit_up"] = abs(row["change_pct"] - 10.0) < 0.1 or abs(row["change_pct"] - 20.0) < 0.1
                    row["limit_down"] = abs(row["change_pct"] + 10.0) < 0.1 or abs(row["change_pct"] + 20.0) < 0.1

                    # 计算技术指标
                    tech = _compute_technical_indicators(df)
                    row.update(tech)

                    # 近5/20/60日涨幅
                    for days, key in [(5, "change_5d"), (20, "change_20d"), (60, "change_60d")]:
                        df_hist = conn.execute(
                            "SELECT close FROM dat_day WHERE code = ? ORDER BY trade_date DESC LIMIT ?",
                            [db_code, days + 1]
                        ).fetchdf()
                        if df_hist is not None and len(df_hist) > 1:
                            old_close = float(df_hist.iloc[-1]["close"])
                            if old_close > 0:
                                row[key] = round((close_val - old_close) / old_close * 100, 2)
                            else:
                                row[key] = 0
                        else:
                            row[key] = 0
            except Exception:
                pass

        # AkShare 兜底：当 DuckDB 不可用或字段缺失时补齐核心行情字段。
        # 注：技术指标与复杂时序逻辑仍优先依赖本地历史库保障稳定复现。
        ak_row = ak_spot_map.get(code, {})
        ak_fill_count = 0
        if isinstance(ak_row, dict) and ak_row:
            for k in [
                "price", "change_pct", "open_pct", "high_pct", "low_pct",
                "volume", "amount", "turnover", "amplitude", "total_mv", "float_mv",
            ]:
                if row.get(k) is None:
                    v = ak_row.get(k)
                    if v is not None:
                        row[k] = v
                        ak_fill_count += 1
            # 涨跌停标记兜底：若 DuckDB 未产出，使用 AkShare 涨跌幅估算。
            if row.get("limit_up") is None and row.get("change_pct") is not None:
                cp = float(row.get("change_pct") or 0.0)
                row["limit_up"] = abs(cp - 10.0) < 0.1 or abs(cp - 20.0) < 0.1
            if row.get("limit_down") is None and row.get("change_pct") is not None:
                cp = float(row.get("change_pct") or 0.0)
                row["limit_down"] = abs(cp + 10.0) < 0.1 or abs(cp + 20.0) < 0.1

        # 统一标记主来源：pytdx / duckdb / mixed / akshare / basic。
        if pytdx_hit and duckdb_hit:
            row["_source_main"] = "pytdx"  # pytdx 优先，DuckDB 用于技术指标
        elif pytdx_hit:
            row["_source_main"] = "pytdx"
        elif duckdb_hit and ak_fill_count > 0:
            row["_source_main"] = "mixed"
        elif duckdb_hit:
            row["_source_main"] = "duckdb"
        elif ak_fill_count > 0:
            row["_source_main"] = "akshare"
        else:
            row["_source_main"] = "basic"

        results.append(row)

    _write_cache("latest_metrics_v2", results)
    return results


def get_data_source_documentation() -> Dict[str, Any]:
    """返回条件筛选的数据来源、落地路径与执行逻辑说明（用于前端展示）。"""
    # 说明文案由后端统一输出，避免前后端多处复制导致口径不一致。
    return {
        "title": "条件筛选数据说明",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_priority": [
            "1) pytdx(TdxProvider)实时行情优先：通过通达信接口获取最新价/涨跌幅/成交量等核心行情字段",
            "2) DuckDB(日线dat_day)：提供历史可复现数据、技术指标、时序事件条件",
            "3) AkShare实时快照兜底：当pytdx/DuckDB缺字段时，补齐换手率/市值等字段",
            "4) 本地股票清单兜底：code/name 基础标的来自 data/stock_list.csv（无文件时尝试 AkShare 拉取）",
        ],
        "data_paths": [
            "本地股票列表: data/stock_list.csv",
            "筛选缓存目录: data/screener_cache/*.json",
            "DuckDB日线表: dat_day（路径由 data_provider.duckdb_path 配置）",
            "pytdx实时行情缓存: data/screener_cache/tdx_spot_map_v1.json",
            "AkShare兜底快照缓存: data/screener_cache/akshare_spot_map_v1.json",
        ],
        "usage_logic": [
            "步骤1：先加载股票池(code/name/交易所)并标准化代码",
            "步骤2：优先从pytdx获取实时行情快照（最新价/涨跌幅/成交量等）",
            "步骤3：从DuckDB读取历史日线，计算技术指标、近N日涨幅、时序事件条件",
            "步骤4：若pytdx/DuckDB缺字段，自动使用AkShare快照补齐换手率/市值等字段",
            "步骤5：先执行简单条件，再按需执行时序条件（limit_up_5d、volume_shrink等）",
            "步骤6：输出分页结果并写入5分钟缓存，减少重复查询开销",
        ],
        "limitations": [
            "pytdx 为逐只获取，全市场获取可能较慢（已加缓存机制）",
            "技术指标/复杂时序条件仍建议依赖DuckDB历史数据以保证复现一致性",
            "region/enterprise_type/margin_trading当前为占位字段，尚未接入稳定数据源",
        ],
    }


def apply_filters(
    exchange: Optional[str] = None,
    region: Optional[str] = None,
    enterprise_type: Optional[str] = None,
    margin_trading: Optional[str] = None,
    market_conditions: Optional[List[Dict[str, Any]]] = None,
    technical_conditions: Optional[List[Dict[str, Any]]] = None,
    financial_conditions: Optional[List[Dict[str, Any]]] = None,
    logic_mode: str = "AND",
    page: int = 1,
    page_size: int = 50,
    sort_by: Optional[str] = None,
    sort_order: str = "desc",
) -> Dict[str, Any]:
    """应用筛选条件，返回分页结果。"""
    metrics = fetch_latest_metrics()

    def _summarize_sources(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """统计来源占比，便于前端展示数据透明度。"""
        total_rows = len(rows)
        raw_counts = {
            "pytdx": 0,
            "duckdb": 0,
            "akshare": 0,
            "mixed": 0,
            "basic": 0,
            "unknown": 0,
        }
        for r in rows:
            s = str(r.get("_source_main", "unknown")).strip().lower()
            if s not in raw_counts:
                s = "unknown"
            raw_counts[s] += 1
        ratio = {}
        for k, v in raw_counts.items():
            ratio[k] = round((v / total_rows) * 100, 2) if total_rows > 0 else 0.0
        return {"total": total_rows, "counts": raw_counts, "ratio_pct": ratio}

    # 前置过滤
    filtered = metrics
    if exchange and exchange != "全部":
        filtered = [r for r in filtered if r.get("exchange") == exchange]

    # Phase 1: region/enterprise/margin 当前为占位字段。
    # 两融数据尚未接入稳定来源时，必须忽略该筛选项，避免"误清空结果"。
    if margin_trading and margin_trading != "全部":
        # no-op: 仅记录占位语义，不参与实际过滤。
        pass

    # 合并所有条件
    all_conditions = []
    if market_conditions:
        all_conditions.extend(market_conditions)
    if technical_conditions:
        all_conditions.extend(technical_conditions)
    if financial_conditions:
        all_conditions.extend(financial_conditions)

    # 分离需要 DuckDB 时序查询的条件
    time_series_keys = {
        "limit_up_5d", "limit_up_10d", "limit_up_20d",
        "volume_shrink", "volume_expand",
        "consecutive_up", "consecutive_down",
        "high_5d", "low_5d", "high_20d", "low_20d",
        "avg_volume_5d", "avg_volume_20d",
        "multi_day_change",
    }

    simple_conditions = [c for c in all_conditions if c.get("key") not in time_series_keys and c.get("operator") not in ("has_event", "formula")]
    ts_conditions = [c for c in all_conditions if c.get("key") in time_series_keys or c.get("operator") in ("has_event", "formula")]

    # 条件去噪：当某字段当前全量为 None 时，像 "volume >= 0" 这类基线条件不应把结果误清空。
    # 典型场景：上游字段暂未接入（全 None），AI 又给出">=0"类宽松条件。
    ignored_conditions: List[Dict[str, Any]] = []

    def _safe_float(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _is_non_restrictive_baseline(cond: Dict[str, Any]) -> bool:
        key = str(cond.get("key", "")).strip()
        op = str(cond.get("operator", "")).strip().lower()
        val = _safe_float(cond.get("value"))
        val2 = _safe_float(cond.get("value2"))
        # 仅对常见"非负指标"做兜底，避免误伤本应允许负值的字段。
        non_negative_keys = {
            "price", "volume", "amount", "turnover", "amplitude",
            "float_mv", "total_mv", "listing_days",
        }
        if key not in non_negative_keys:
            return False
        if op == "gte" and val is not None and val <= 0:
            return True
        if op == "gt" and val is not None and val < 0:
            return True
        if op == "between":
            # between [<=0, +inf) 也等价于非限制条件。
            if val is not None and val2 is None and val <= 0:
                return True
            if val is not None and val2 is not None and val <= 0 and val2 >= 1e12:
                return True
        return False

    refined_simple_conditions: List[Dict[str, Any]] = []
    for cond in simple_conditions:
        key = str(cond.get("key", "")).strip()
        if not key:
            continue
        # 仅在"字段全缺失 + 条件本身是基线约束"时忽略。
        has_any_value = any(r.get(key) is not None for r in filtered)
        if (not has_any_value) and _is_non_restrictive_baseline(cond):
            ignored_conditions.append(cond)
            continue
        refined_simple_conditions.append(cond)
    simple_conditions = refined_simple_conditions

    # 先过滤简单条件
    def _match_field(row: Dict, cond: Dict) -> bool:
        key = cond.get("key", "")
        op = cond.get("operator", "")
        val = cond.get("value")
        val2 = cond.get("value2")
        actual = row.get(key)

        if actual is None:
            return False

        try:
            actual = float(actual)
        except (ValueError, TypeError):
            return False

        if op == "gt":
            return actual > float(val) if val is not None else False
        if op == "gte":
            return actual >= float(val) if val is not None else False
        if op == "lt":
            return actual < float(val) if val is not None else False
        if op == "lte":
            return actual <= float(val) if val is not None else False
        if op == "eq":
            return actual == float(val) if val is not None else False
        if op == "between":
            if val is not None and val2 is not None:
                return float(val) <= actual <= float(val2)
            if val is not None:
                return actual >= float(val)
            if val2 is not None:
                return actual <= float(val2)
            return False
        if op == "toggle":
            return bool(actual)
        return True

    def _match_simple(row: Dict) -> bool:
        if not simple_conditions:
            return True
        if logic_mode == "OR":
            return any(_match_field(row, c) for c in simple_conditions)
        return all(_match_field(row, c) for c in simple_conditions)

    filtered = [r for r in filtered if _match_simple(r)]

    # 对过滤后的候选标的执行时序条件查询
    if ts_conditions:
        conn = get_duckdb_conn()
        if conn:
            filtered = _apply_time_series_conditions(filtered, ts_conditions, conn, logic_mode)
        else:
            # 无 DuckDB，时序条件全部不通过
            filtered = []

    # 排序
    if sort_by:
        reverse = sort_order == "desc"
        filtered.sort(key=lambda r: r.get(sort_by, 0) or 0, reverse=reverse)

    # 分页
    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_data = filtered[start:end]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": page_data,
        # 返回被自动忽略的条件，便于前端排障与提示。
        "ignored_conditions": ignored_conditions,
        # 同时返回"筛选前"和"筛选后"的来源占比，便于看板统计与可视化解释。
        "source_stats": {
            "pool": _summarize_sources(metrics),
            "filtered": _summarize_sources(filtered),
            # 新增"当前页"口径，方便前端分页场景展示更直观的来源占比。
            "filtered_page": _summarize_sources(page_data),
        },
    }


# ---------------------------------------------------------------------------
# 时序查询引擎
# ---------------------------------------------------------------------------
def _is_limit_up(change_pct: float) -> bool:
    """判断是否涨停：主板 ±10%，科创/创业 ±20%。"""
    return abs(change_pct - 10.0) < 0.1 or abs(change_pct - 20.0) < 0.1


def _apply_time_series_conditions(
    rows: List[Dict],
    conditions: List[Dict],
    conn,
    logic_mode: str,
) -> List[Dict]:
    """对候选标的执行时序条件（基于 DuckDB dat_day 历史数据）。"""
    results = []
    for row in rows:
        code = _normalize_stock_code(row.get("code", ""))
        if not code:
            continue
        db_code = code
        if "." not in db_code:
            if db_code.startswith("6"):
                db_code = f"{db_code}.SH"
            elif db_code.startswith("0") or db_code.startswith("3"):
                db_code = f"{db_code}.SZ"
            elif db_code.startswith("8") or db_code.startswith("4"):
                db_code = f"{db_code}.BJ"

        # 按需查询历史数据
        history_cache = {}
        condition_results = []

        for cond in conditions:
            key = cond.get("key", "")
            op = cond.get("operator", "")
            val = cond.get("value")

            try:
                passed = _evaluate_time_series(code, db_code, key, op, val, conn, history_cache)
            except Exception:
                passed = False

            condition_results.append(passed)

        if logic_mode == "OR":
            if any(condition_results):
                results.append(row)
        else:
            if all(condition_results):
                results.append(row)

    return results


def _evaluate_time_series(
    code: str,
    db_code: str,
    key: str,
    op: str,
    value: Any,
    conn,
    history_cache: Dict[str, Any],
) -> bool:
    """评估单个时序条件。"""
    # 获取历史数据（带缓存）
    if "history" not in history_cache:
        df = conn.execute(
            "SELECT trade_date, open, high, low, close, pre_close, volume, amount, turnover "
            "FROM dat_day WHERE code = ? ORDER BY trade_date DESC LIMIT 100",
            [db_code]
        ).fetchdf()
        history_cache["history"] = df if df is not None and not df.empty else None

    df = history_cache.get("history")
    if df is None or df.empty:
        return False

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    pre_close = df["pre_close"].astype(float)
    change_pct = ((close - pre_close) / pre_close * 100).fillna(0)

    # --- has_event: 近N日发生过某事件 ---
    if op == "has_event":
        n_days = int(value or 5)
        if key in ("limit_up_5d", "limit_up_10d", "limit_up_20d"):
            n_days_map = {"limit_up_5d": 5, "limit_up_10d": 10, "limit_up_20d": 20}
            n_days = n_days_map.get(key, int(value or 5))
        recent_change = change_pct.iloc[:n_days]
        return any(_is_limit_up(c) for c in recent_change)

    # --- limit_up_Nd: 近N日有涨停（has_event 的别名） ---
    if key in ("limit_up_5d", "limit_up_10d", "limit_up_20d"):
        n_days_map = {"limit_up_5d": 5, "limit_up_10d": 10, "limit_up_20d": 20}
        n_days = n_days_map.get(key, 5)
        recent_change = change_pct.iloc[:n_days]
        return any(_is_limit_up(c) for c in recent_change)

    # --- volume_shrink: 涨停后N日内成交量 < 涨停日成交量 * 比例 ---
    if key == "volume_shrink":
        # 统一参数语义：
        # - value 为 0~1 浮点时，视为缩量阈值（默认 0.5）
        # - value 为整数>1 时，视为回看天数（阈值仍 0.5）
        # - value 为 dict 时支持 {"days": 5, "ratio": 0.5}
        lookback_days = 5
        threshold = 0.5
        if isinstance(value, dict):
            try:
                lookback_days = max(1, int(value.get("days", 5) or 5))
            except Exception:
                lookback_days = 5
            try:
                threshold = float(value.get("ratio", 0.5) or 0.5)
            except Exception:
                threshold = 0.5
        elif isinstance(value, (int, float)):
            v = float(value)
            if 0 < v <= 1:
                threshold = v
            elif v > 1:
                lookback_days = int(v)
        elif isinstance(value, str) and value.strip():
            try:
                v = float(value.strip())
                if 0 < v <= 1:
                    threshold = v
                elif v > 1:
                    lookback_days = int(v)
            except ValueError:
                pass

        # dat_day 按 trade_date DESC 查询：iloc[0] 为"今天"，iloc[1] 为"昨天"。
        # 这里的业务语义是"最近一次涨停后的N日内，当前日缩量到阈值以下"：
        # - 在最近 lookback_days 天内寻找最近涨停日（不含今天）
        # - 判断今天成交量 < 涨停日成交量 * threshold
        current_volume = float(volume.iloc[0]) if len(volume) > 0 else 0.0
        if current_volume <= 0:
            return False

        limit_up_idx = None
        for i in range(1, min(lookback_days + 1, len(change_pct))):
            if _is_limit_up(float(change_pct.iloc[i])):
                limit_up_idx = i
                break
        if limit_up_idx is None:
            return False

        lu_volume = float(volume.iloc[limit_up_idx])
        if lu_volume <= 0:
            return False
        return current_volume < lu_volume * threshold

    # --- volume_expand: 近N日成交量放大 ---
    if key == "volume_expand":
        n_days = int(value or 5)
        if len(volume) < n_days + 1:
            return False
        avg_recent = volume.iloc[:n_days].mean()
        avg_prev = volume.iloc[n_days: n_days * 2].mean()
        return avg_recent > avg_prev * 1.5  # 放量50%

    # --- consecutive_up: 近N日连涨 ---
    if key == "consecutive_up":
        n_days = int(value or 5)
        recent_change = change_pct.iloc[:n_days]
        return all(c > 0 for c in recent_change)

    # --- consecutive_down: 近N日连跌 ---
    if key == "consecutive_down":
        n_days = int(value or 5)
        recent_change = change_pct.iloc[:n_days]
        return all(c < 0 for c in recent_change)

    # --- high_5d / low_5d / high_20d / low_20d: 当前价创N日新高/新低 ---
    if key in ("high_5d", "low_5d", "high_20d", "low_20d"):
        n_days_map = {"high_5d": 5, "low_5d": 5, "high_20d": 20, "low_20d": 20}
        n_days = n_days_map.get(key, 5)
        recent_high = df["high"].astype(float).iloc[:n_days].max()
        recent_low = df["low"].astype(float).iloc[:n_days].min()
        current_close = close.iloc[0]
        if "high" in key:
            return abs(current_close - recent_high) / recent_high < 0.01  # 接近新高
        return abs(current_close - recent_low) / recent_low < 0.01  # 接近新低

    # --- avg_volume_5d / avg_volume_20d ---
    if key in ("avg_volume_5d", "avg_volume_20d"):
        n_days_map = {"avg_volume_5d": 5, "avg_volume_20d": 20}
        n_days = n_days_map.get(key, 5)
        avg_vol = volume.iloc[:n_days].mean()
        actual_val = float(value or 0)
        if actual_val <= 0:
            return True
        return avg_vol > actual_val

    # --- multi_day_change: 近N日累计涨幅 ---
    if key == "multi_day_change":
        n_days = int(value or 5)
        if len(close) < n_days + 1:
            return False
        old_close = close.iloc[n_days]
        current_close = close.iloc[0]
        pct = (current_close - old_close) / old_close * 100
        actual_val = float(value or 0) if isinstance(value, (int, float)) else 0
        return pct > actual_val

    # --- formula: 自定义公式 ---
    if op == "formula":
        formula = str(value or "")
        if not formula:
            return True
        # 支持的变量：price, volume, change_pct, pre_close, high, low, close
        # 涨停缩量公式示例：volume[i] < volume[limit_up_day] * 0.5
        # 简化版：仅判断最新一根K线的变量关系
        try:
            local_vars = {
                "price": close.iloc[0],
                "volume": volume.iloc[0],
                "change_pct": change_pct.iloc[0],
                "pre_close": pre_close.iloc[0],
                "high": float(df["high"].astype(float).iloc[0]),
                "low": float(df["low"].astype(float).iloc[0]),
                "close": close.iloc[0],
            }
            return bool(eval(formula, {"__builtins__": {}}, local_vars))
        except Exception:
            return False

    return True
