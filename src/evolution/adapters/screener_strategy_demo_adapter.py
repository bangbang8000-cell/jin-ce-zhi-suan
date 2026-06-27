"""选股策略示例适配器。

用于在"策略管理器"与"AI解析界面"之间提供统一的示例资产：
1) 示例提示词（用于 AI 解析输入预填）；
2) 示例策略代码（用于策略管理器一键导入演示策略）。

该模块只做数据组织与入库编排，不改动回测引擎。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from textwrap import dedent
from typing import Any, Dict, List

from src.strategies import strategy_manager_repo as strategy_repo
from src.utils.screener_data_provider import apply_filters


logger = logging.getLogger(__name__)


# 固定示例策略ID，便于重复演示时"更新而非堆积"。
SCREENER_DEMO_STRATEGY_ID = "SC001"
# 固定示例策略名称，便于在策略管理器检索。
SCREENER_DEMO_STRATEGY_NAME = "选股策略示例-主板强势回撤"
# 统一分类名称，用于策略管理器"新增一类"展示。
SCREENER_DEMO_CATEGORY = "选股策略示例"


# _strict_demo_available 探测结果缓存：避免每次拉取示例列表都重跑 apply_filters。
# 默认 TTL 5 分钟；可通过环境变量 SCREENER_DEMO_PROBE_TTL_SEC 覆盖（便于调试）。
_STRICT_DEMO_PROBE_TTL_SEC = 5 * 60
_STRICT_DEMO_PROBE_LOCK = threading.Lock()
_STRICT_DEMO_PROBE_CACHE: Dict[str, Any] = {
    "value": None,
    "ts": 0.0,
    "in_flight": False,
}


def _strict_demo_available() -> bool:
    """判断严格版示例在当前数据口径下是否能筛出股票。

    设计要点：
    1) 结果按 TTL 缓存（默认 5 分钟，可由 SCREENER_DEMO_PROBE_TTL_SEC 覆盖）；
    2) 不阻塞：若缓存未命中且当前无探测在跑，触发一次异步探测并立刻返回 False，
       前端下一次请求（或点"刷新示例"）再读取新缓存；
    3) 同步入口 `warm_strict_demo_probe()` 可用于启动阶段主动预热。
    """
    import os

    try:
        ttl = float(os.environ.get("SCREENER_DEMO_PROBE_TTL_SEC", _STRICT_DEMO_PROBE_TTL_SEC))
    except (TypeError, ValueError):
        ttl = _STRICT_DEMO_PROBE_TTL_SEC
    now = time.time()
    with _STRICT_DEMO_PROBE_LOCK:
        cached = _STRICT_DEMO_PROBE_CACHE
        if cached["value"] is not None and (now - cached["ts"]) < ttl:
            return bool(cached["value"])
        if cached["in_flight"]:
            return False
        cached["in_flight"] = True

    def _worker() -> None:
        try:
            result = apply_filters(
                market_conditions=[
                    {"key": "is_main_board", "operator": "toggle", "value": True},
                    {"key": "change_5d", "operator": "between", "value": 0.0, "value2": 12.0},
                    {"key": "limit_down", "operator": "lte", "value": 0},
                ],
                technical_conditions=[],
                financial_conditions=[],
                logic_mode="AND",
                page=1,
                page_size=1,
            )
            ok = int((result or {}).get("total", 0) or 0) > 0
        except Exception as e:
            logger.warning("_strict_demo_available probe failed: %s", e)
            ok = False
        finally:
            with _STRICT_DEMO_PROBE_LOCK:
                _STRICT_DEMO_PROBE_CACHE["value"] = ok
                _STRICT_DEMO_PROBE_CACHE["ts"] = time.time()
                _STRICT_DEMO_PROBE_CACHE["in_flight"] = False

    threading.Thread(target=_worker, name="screener-demo-probe", daemon=True).start()
    return False


def warm_strict_demo_probe() -> bool:
    """同步预热严格示例探测缓存，供启动阶段调用。

    返回是否可展示严格示例。若探测失败/超时返回 False，不会抛异常。
    """
    try:
        result = apply_filters(
            market_conditions=[
                {"key": "is_main_board", "operator": "toggle", "value": True},
                {"key": "change_5d", "operator": "between", "value": 0.0, "value2": 12.0},
                {"key": "limit_down", "operator": "lte", "value": 0},
            ],
            technical_conditions=[],
            financial_conditions=[],
            logic_mode="AND",
            page=1,
            page_size=1,
        )
        ok = int((result or {}).get("total", 0) or 0) > 0
    except Exception as e:
        logger.warning("warm_strict_demo_probe failed: %s", e)
        ok = False
    with _STRICT_DEMO_PROBE_LOCK:
        _STRICT_DEMO_PROBE_CACHE["value"] = ok
        _STRICT_DEMO_PROBE_CACHE["ts"] = time.time()
        _STRICT_DEMO_PROBE_CACHE["in_flight"] = False
    return ok


def list_screener_prompt_examples() -> List[Dict[str, Any]]:
    """返回 AI 解析界面可直接使用的示例提示词列表。"""
    # 每个示例都可直接填充到"screener-ai-input"，用户仍可继续修改。
    # 默认保留"高命中示例"，并补充稳定排序口径，提升结果可读性。
    examples = [
        {
            "example_id": "mainboard_base_pool",
            "title": "主板基础候选池",
            "category": SCREENER_DEMO_CATEGORY,
            # 默认排序改为结构化字段，由后端强制执行，不依赖LLM语义理解。
            "default_sort_by": "amount",
            "default_sort_order": "desc",
            "prompt": (
                "筛选主板股票，作为今日候选池输出。"
                "先不过滤复杂时序条件，优先保证可稳定选出股票。"
                "按成交额从高到低排序，输出前30只候选股。"
            ),
            "description": "内置高命中示例：用于验证筛选链路可正常产出结果。"
        },
    ]
    # 严格版仅在"当前可筛出股票"时展示，避免用户看到空结果。
    if _strict_demo_available():
        examples.append(
            {
                "example_id": "mainboard_strict_pool",
                "title": "主板严格候选池",
                "category": SCREENER_DEMO_CATEGORY,
                # 严格版默认按涨跌幅降序，帮助快速聚焦当日更强势标的。
                "default_sort_by": "change_pct",
                "default_sort_order": "desc",
                "prompt": (
                    "筛选主板股票，近5日涨幅在0%到12%之间，且当日非跌停。"
                    "按涨跌幅从高到低排序，输出前20只候选股。"
                ),
                "description": "严格版示例：仅在当前数据可命中时自动显示。"
            }
        )
    return examples


# 静态示例配置表：供 resolve_screener_demo_sort 使用，不依赖探测状态。
# 即使 _strict_demo_available() 返回 False，按 eid 查找仍能命中正确配置。
_EXAMPLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "mainboard_base_pool": {
        "default_sort_by": "amount",
        "default_sort_order": "desc",
    },
    "mainboard_strict_pool": {
        "default_sort_by": "change_pct",
        "default_sort_order": "desc",
    },
}


def resolve_screener_demo_sort(user_prompt: str, example_id: str = "") -> Dict[str, str]:
    """根据示例ID（或提示词）解析后端强制排序参数。"""
    text = str(user_prompt or "").strip()
    eid = str(example_id or "").strip()
    text_norm = text.replace(" ", "")
    # 优先按 eid 从静态配置表查找，不依赖探测状态。
    target_config = _EXAMPLE_CONFIGS.get(eid) if eid else None
    matched_eid = eid if target_config else ""
    if not target_config:
        # 兜底：按提示词关键词推断示例ID
        inferred_eid = ""
        if ("主板" in text_norm) and (("候选池" in text_norm) or ("候选股" in text_norm)):
            if ("近5日" in text_norm) and (("0%" in text_norm) or ("12%" in text_norm) or ("12" in text_norm)):
                inferred_eid = "mainboard_strict_pool"
            else:
                inferred_eid = "mainboard_base_pool"
        if inferred_eid:
            target_config = _EXAMPLE_CONFIGS.get(inferred_eid)
            matched_eid = inferred_eid
    sort_by = str((target_config or {}).get("default_sort_by", "")).strip()
    sort_order = str((target_config or {}).get("default_sort_order", "")).strip().lower()
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"
    return {
        "sort_by": sort_by,
        "sort_order": sort_order,
        "matched_example_id": matched_eid,
    }


def _build_screener_demo_strategy_code(strategy_id: str, strategy_name: str) -> str:
    """构建"选股策略示例"可运行代码。"""
    # 将输入标准化为字符串，避免外部传入异常类型。
    sid = str(strategy_id or SCREENER_DEMO_STRATEGY_ID).strip() or SCREENER_DEMO_STRATEGY_ID
    sname = str(strategy_name or SCREENER_DEMO_STRATEGY_NAME).strip() or SCREENER_DEMO_STRATEGY_NAME
    # 代码模板聚焦"可跑通与可解释"，不是收益最优。
    code_text = f"""
from src.strategies.implemented_strategies import BaseImplementedStrategy
from src.utils.indicators import Indicators
import pandas as pd


class ScreenerDemoMainboardStrategy(BaseImplementedStrategy):
    \"\"\"选股策略示例：主板强势回撤（日线）。\"\"\"

    def __init__(self):
        # 使用日线触发，方便与条件筛选口径保持一致。
        super().__init__("{sid}", "{sname}", trigger_timeframe="D")
        # 每个标的独立维护K线历史与买入日。
        self.history = {{}}
        self.last_buy_day = {{}}
        self.entry_price_local = {{}}

    def _is_main_board(self, code):
        # 主板限定：600/601/603/605/000/001/002。
        c = str(code or "").split(".", 1)[0].strip().upper()
        return c.startswith(("600", "601", "603", "605", "000", "001", "002"))

    def _limit_pct(self, kline):
        # 计算涨跌幅，用于涨跌停约束判断。
        pre_close = float(kline.get("pre_close", 0.0) or 0.0)
        close = float(kline.get("close", 0.0) or 0.0)
        if pre_close <= 0:
            return 0.0
        return (close - pre_close) / pre_close * 100.0

    def _is_limit_up(self, kline):
        # 涨停不可买（近似判定）。
        pct = self._limit_pct(kline)
        return abs(pct - 10.0) < 0.1 or abs(pct - 20.0) < 0.1

    def _is_limit_down(self, kline):
        # 跌停不可卖（近似判定）。
        pct = self._limit_pct(kline)
        return abs(pct + 10.0) < 0.1 or abs(pct + 20.0) < 0.1

    def _same_day(self, code, dt_value):
        # 将日期归一化，供 T+1 判断。
        day_text = str(pd.to_datetime(dt_value, errors="coerce").strftime("%Y-%m-%d"))
        return self.last_buy_day.get(code) == day_text, day_text

    def on_bar(self, kline):
        # 基础字段读取。
        code = str(kline.get("code", "") or "").strip()
        if not code:
            return None
        if not self._is_main_board(code):
            return None

        # 维护历史数据，限制窗口保证性能。
        if code not in self.history:
            self.history[code] = pd.DataFrame()
        self.history[code] = pd.concat([self.history[code], pd.DataFrame([kline])], ignore_index=True).tail(900)
        df = self.history[code]
        if len(df) < 30:
            return None

        # 计算核心指标：MA5 / MA20 / 近5日涨幅。
        close = df["close"].astype(float)
        ma5 = Indicators.MA(close, 5)
        ma20 = Indicators.MA(close, 20)
        if len(ma5) < 2 or len(ma20) < 2:
            return None
        curr_close = float(kline.get("close", 0.0) or 0.0)
        if curr_close <= 0:
            return None
        old_close = float(close.iloc[-6]) if len(close) >= 6 else curr_close
        change_5d = ((curr_close - old_close) / old_close * 100.0) if old_close > 0 else 0.0
        qty = int(self.positions.get(code, 0) or 0)

        # 参数可通过 runtime_params 覆盖。
        stop_loss_pct = float(self._cfg("stop_loss_pct", 0.03))
        take_profit_pct = float(self._cfg("take_profit_pct", 0.08))
        max_hold_bars = int(self._cfg("max_hold_bars", 15))

        # 入场：空仓 + MA5>MA20 + 近5日涨幅在区间内 + 非涨停。
        if qty <= 0:
            trend_ok = float(ma5.iloc[-1]) > float(ma20.iloc[-1]) and curr_close > float(ma20.iloc[-1])
            momentum_ok = 2.0 <= change_5d <= 25.0
            if trend_ok and momentum_ok and (not self._is_limit_up(kline)):
                buy_qty = int(self._qty())
                if buy_qty <= 0:
                    buy_qty = 100
                _same, day_text = self._same_day(code, kline.get("dt"))
                self.last_buy_day[code] = day_text
                self.entry_price_local[code] = curr_close
                return {{
                    "strategy_id": self.id,
                    "code": code,
                    "dt": kline["dt"],
                    "direction": "BUY",
                    "price": curr_close,
                    "qty": buy_qty,
                    "stop_loss": curr_close * (1 - stop_loss_pct),
                    "take_profit": curr_close * (1 + take_profit_pct),
                }}
            return None

        # T+1：买入当日不卖出。
        same_day, _day_text = self._same_day(code, kline.get("dt"))
        if same_day:
            return None
        # 跌停不可卖。
        if self._is_limit_down(kline):
            return None

        # 出场：MA5下穿MA20 或 止盈止损 或 持仓超时。
        death_cross = float(ma5.iloc[-2]) >= float(ma20.iloc[-2]) and float(ma5.iloc[-1]) < float(ma20.iloc[-1])
        entry_price = float(self.entry_price_local.get(code, curr_close) or curr_close)
        stop_loss_hit = curr_close <= entry_price * (1 - stop_loss_pct)
        take_profit_hit = curr_close >= entry_price * (1 + take_profit_pct)
        self.update_holding_time(code)
        timeout_exit = self.check_max_holding_time(code, max_hold_bars)
        if death_cross or stop_loss_hit or take_profit_hit or timeout_exit:
            reason = []
            if death_cross:
                reason.append("MA Death Cross")
            if stop_loss_hit:
                reason.append("Stop Loss")
            if take_profit_hit:
                reason.append("Take Profit")
            if timeout_exit:
                reason.append("Time Exit")
            return self.create_exit_signal(kline, qty, " | ".join(reason) if reason else "Rule Exit")
        return None
"""
    return dedent(code_text).strip() + "\n"


def build_screener_demo_strategy_payload(
    strategy_id: str = SCREENER_DEMO_STRATEGY_ID,
    strategy_name: str = SCREENER_DEMO_STRATEGY_NAME,
) -> Dict[str, Any]:
    """构建示例策略入库 payload。"""
    sid = str(strategy_id or SCREENER_DEMO_STRATEGY_ID).strip() or SCREENER_DEMO_STRATEGY_ID
    sname = str(strategy_name or SCREENER_DEMO_STRATEGY_NAME).strip() or SCREENER_DEMO_STRATEGY_NAME
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    code_text = _build_screener_demo_strategy_code(strategy_id=sid, strategy_name=sname)
    # 保持 strategy_intent 结构完整，兼容策略管理器已有解析链路。
    strategy_intent = {
        "source": "human",
        "strategy_type": "stock_selection",
        "logic": "主板趋势+动量入场，T+1与涨跌停约束，死叉/风控退出",
        "indicators": ["MA5", "MA20", "change_5d"],
        "entry": "MA5>MA20 且近5日涨幅在2%-25%且非涨停",
        "exit": "MA死叉或止盈止损或持仓超时，且遵守T+1与跌停不卖",
        "risk_profile": "balanced",
        "confidence": 0.72,
    }
    return {
        "id": sid,
        "name": sname,
        "class_name": "ScreenerDemoMainboardStrategy",
        "code": code_text,
        "kline_type": "1day",
        "template_text": "选股策略示例模板：主板强势回撤 + A股约束",
        "analysis_text": f"选股策略示例，更新时间 {now_text}，用于全流程演示。",
        "source": "human",
        "protect_level": "custom",
        "immutable": False,
        "depends_on": [],
        "raw_requirement_title": SCREENER_DEMO_CATEGORY,
        "raw_requirement": (
            "用于策略管理器「选股策略示例」分类展示，并可在AI解析界面直接填充提示词。"
        ),
        "strategy_intent": strategy_intent,
    }


def upsert_screener_demo_strategy(
    strategy_id: str = SCREENER_DEMO_STRATEGY_ID,
    strategy_name: str = SCREENER_DEMO_STRATEGY_NAME,
) -> Dict[str, Any]:
    """将示例策略写入策略库（存在即更新，不存在即新增）。"""
    payload = build_screener_demo_strategy_payload(strategy_id=strategy_id, strategy_name=strategy_name)
    sid = str(payload.get("id", "")).strip()
    rows = strategy_repo.list_all_strategy_meta()
    exists = any(str(r.get("id", "")).strip() == sid for r in rows if isinstance(r, dict))
    if exists:
        # update 时保留同一ID，覆盖其余字段，便于演示重复执行。
        strategy_repo.update_custom_strategy(
            {
                "id": sid,
                "name": payload.get("name", ""),
                "class_name": payload.get("class_name", ""),
                "code": payload.get("code", ""),
                "kline_type": payload.get("kline_type", ""),
                "analysis_text": payload.get("analysis_text", ""),
                "source": payload.get("source", "human"),
                "raw_requirement_title": payload.get("raw_requirement_title", ""),
                "raw_requirement": payload.get("raw_requirement", ""),
                "depends_on": payload.get("depends_on", []),
                "protect_level": payload.get("protect_level", "custom"),
                "immutable": bool(payload.get("immutable", False)),
            }
        )
        return {
            "status": "success",
            "action": "updated",
            "strategy_id": sid,
            "strategy_name": str(payload.get("name", "")),
            "category": SCREENER_DEMO_CATEGORY,
        }
    strategy_repo.add_custom_strategy(payload)
    return {
        "status": "success",
        "action": "created",
        "strategy_id": sid,
        "strategy_name": str(payload.get("name", "")),
        "category": SCREENER_DEMO_CATEGORY,
    }
