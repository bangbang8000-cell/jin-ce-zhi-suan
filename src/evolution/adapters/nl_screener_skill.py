"""自然语言条件筛选 Skill 适配器。

该模块负责将用户自然语言提示词编排为：
1. 结构化筛选条件（screen_conditions）
2. 回测执行规则（execution_rules）
3. 直接可展示的筛选结果（filter_result）

设计目标：
- 最小侵入式接入现有条件筛选器
- 通过适配器模式复用已有 LLM 网关与筛选执行器
- 保持返回结构稳定，方便前端直接渲染
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.evolution.adapters.llm_gateway_adapter import build_unified_llm_client
from src.evolution.adapters.execution_rules_adapter import build_execution_plan
from src.evolution.adapters.portfolio_plan_adapter import build_portfolio_plan
from src.evolution.adapters.screener_strategy_demo_adapter import resolve_screener_demo_sort
from src.utils.config_loader import ConfigLoader
from src.utils.screener_data_provider import apply_filters


# 系统提示词：约束模型输出为可执行 JSON，避免额外解释文本污染解析。
SYSTEM_PROMPT_V1 = """你是A股条件筛选执行器，请把用户自然语言策略转换为严格JSON输出。

必须输出如下结构：
{
  "screen_conditions": [],
  "execution_rules": [],
  "strategy_meta": {"name": "", "description": "", "risk_tags": []},
  "warnings": []
}

screen_conditions 每项字段：
- key, tab, operator, label, value, value2, unit, enabled
- confidence: 0~1 的浮点数，表示你对此条件准确反映用户意图的把握程度
  - 0.8+：清晰明确（如"主板"、"市盈率小于20"）
  - 0.5~0.8：有一定推测（如"低估值"→PE<30 是推测值）
  - <0.5：高度推测（如"强势股"无明确数值定义）
  - 无法映射到已知字段时，enabled=false 并写入 warnings

规则约束：
1) 只输出JSON，不要markdown，不要多余文本。
2) operator 仅允许：gt,gte,lt,lte,between,toggle,has_event,formula
3) 仅可使用系统已支持的key；不确定项写入warnings。
4) 买卖时点、止盈止损、仓位控制写入execution_rules，不放入screen_conditions。
5) 遵守A股约束：T+1、涨停不可买、跌停不可卖，写入execution_rules。
6) 优先产出可执行条件（enabled=true），不可执行条件标记enabled=false。
7) 每个条件必须带 confidence，缺失时后端会填默认值 0.7。
"""

# 统一允许的操作符集合，避免模型输出非常规值导致筛选行为不确定。
ALLOWED_OPERATORS = {"gt", "gte", "lt", "lte", "between", "toggle", "has_event", "formula"}
# tab 别名映射：兼容中文/英文混写，避免条件被意外丢弃。
TAB_ALIAS_MAP = {
    "market": "market",
    "行情": "market",
    "市场": "market",
    "price": "market",
    "technical": "technical",
    "技术": "technical",
    "tech": "technical",
    "financial": "financial",
    "财务": "financial",
    "fundamental": "financial",
    "fundamentals": "financial",
}
# 已知 key 集合：当 tab 缺失或异常时按 key 反推归属，保障条件可执行。
TECHNICAL_KEYS = {
    "ma5", "ma10", "ma20", "ma60", "price_vs_ma20",
    "macd_dif", "macd_dea", "macd_hist", "macd_golden_cross", "macd_death_cross",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_12", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower", "atr_14",
}
FINANCIAL_KEYS = {
    "pe_ttm", "pb", "ps_ttm", "roe", "roa", "gross_margin",
    "net_margin", "revenue_yoy", "profit_yoy",
}


def _fallback_conditions_for_example(example_id: str) -> List[Dict[str, Any]]:
    """按示例ID返回后端兜底条件，避免LLM异常输出导致空筛选。"""
    eid = str(example_id or "").strip()
    if eid == "mainboard_base_pool":
        return [
            {"key": "is_main_board", "tab": "market", "operator": "toggle", "label": "主板股票", "value": True, "value2": None, "unit": "", "enabled": True},
        ]
    if eid == "mainboard_strict_pool":
        return [
            {"key": "is_main_board", "tab": "market", "operator": "toggle", "label": "主板股票", "value": True, "value2": None, "unit": "", "enabled": True},
            {"key": "change_5d", "tab": "market", "operator": "between", "label": "近5日涨幅", "value": 0.0, "value2": 12.0, "unit": "%", "enabled": True},
            {"key": "limit_down", "tab": "market", "operator": "lte", "label": "非跌停", "value": 0, "value2": None, "unit": "", "enabled": True},
        ]
    return []


def _infer_example_id_from_prompt(user_prompt: str) -> str:
    """根据用户提示词推断示例ID（用于未传example_id时的后端兜底）。"""
    text = str(user_prompt or "").strip()
    if not text:
        return ""
    # 统一去空白，兼容用户手改文案时的空格差异。
    compact = re.sub(r"\s+", "", text)
    # 主板候选池语义：优先匹配严格版，否则回退基础版。
    if ("主板" in compact) and (("候选池" in compact) or ("候选股" in compact)):
        if ("近5日" in compact) and (("12%" in compact) or ("12" in compact)):
            return "mainboard_strict_pool"
        return "mainboard_base_pool"
    return ""


@dataclass
class NlScreenerResult:
    """统一返回结构，便于 API 与前端复用。"""

    status: str
    data: Dict[str, Any]
    msg: str = ""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出中提取 JSON，兼容 ```json 包裹。"""
    if not text:
        return None
    raw = str(text).strip()
    block = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if block:
        raw = block.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _normalize_conditions(items: Any) -> List[Dict[str, Any]]:
    """标准化模型输出的筛选条件，避免字段缺失导致执行失败。"""
    # 将字符串布尔值转换为布尔，避免 "false" 被 bool("false") 误判为 True。
    def _to_bool(value: Any, default: bool = True) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "1", "yes", "y", "on"}:
                return True
            if text in {"false", "0", "no", "n", "off"}:
                return False
        if value is None:
            return default
        return bool(value)

    # 归一化 tab：优先按别名映射，其次按 key 推断，最终兜底 market。
    def _normalize_tab(raw_tab: Any, key: str) -> str:
        tab_text = str(raw_tab or "").strip().lower()
        mapped = TAB_ALIAS_MAP.get(tab_text)
        if mapped:
            return mapped
        if key in TECHNICAL_KEYS:
            return "technical"
        if key in FINANCIAL_KEYS:
            return "financial"
        return "market"

    if not isinstance(items, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        # 条件字段别名修复：将"market/board=主板"归一化为 is_main_board 开关。
        key_lower = key.lower()
        label_text = str(item.get("label", "")).strip()
        value_text = str(item.get("value", "")).strip()
        combined_text = f"{label_text} {value_text}"
        if key_lower in {"market", "board", "stock_board", "exchange_board"} and ("主板" in combined_text):
            key = "is_main_board"
            item["operator"] = "toggle"
            item["value"] = True
        # 强兼容：若模型直接输出 market/board 条件，统一映射到主板开关，避免无效字段导致全空。
        if key_lower in {"market", "board", "stock_board", "exchange_board"}:
            key = "is_main_board"
            item["operator"] = "toggle"
            item["value"] = True
        if key_lower in {"is_main_board", "main_board"}:
            key = "is_main_board"
            item["operator"] = "toggle"
            item["value"] = True
        operator = str(item.get("operator", "gte")).strip().lower() or "gte"
        if operator not in ALLOWED_OPERATORS:
            operator = "gte"
        normalized.append(
            {
                "key": key,
                "tab": _normalize_tab(item.get("tab", "market"), key),
                "operator": operator,
                "label": str(item.get("label", key)).strip() or key,
                "value": item.get("value"),
                "value2": item.get("value2"),
                "unit": str(item.get("unit", "")).strip(),
                "enabled": _to_bool(item.get("enabled", True), default=True),
                # confidence：LLM 对自身理解准确度的自评分，0~1。
                # 缺失时兜底 0.7（中等偏上，避免低置信被误判为"完全不确定"）。
                "confidence": _clamp_confidence(item.get("confidence")),
            }
        )
    return normalized


def _clamp_confidence(value: Any) -> float:
    """把 LLM 输出的 confidence 强制归一化到 [0, 1] 区间。

    非法值（None / 字符串 / 越界数字）统一兜底为 0.7，避免前端分档逻辑崩溃。
    """
    if value is None:
        return 0.7
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.7
    if f != f:  # NaN
        return 0.7
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return round(f, 2)


def _build_messages(user_prompt: str) -> List[Dict[str, str]]:
    """构造 LLM 对话消息，统一入口。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT_V1},
        {"role": "user", "content": str(user_prompt or "").strip()},
    ]


def run_nl_screener_skill(
    user_prompt: str,
    page: int = 1,
    page_size: int = 50,
    logic_mode: str = "AND",
    scope: str = "screener",
    example_id: str = "",
) -> NlScreenerResult:
    """一步执行自然语言筛选：提示词 -> 条件解析 -> 筛选结果。"""
    text = str(user_prompt or "").strip()
    if not text:
        return NlScreenerResult(status="error", data={}, msg="用户提示词不能为空")

    # 条件筛选走专用 screener 域（默认非推理模型），缺失时回退到 strategy_manager 再 unified。
    cfg = ConfigLoader()
    client = build_unified_llm_client(cfg, scope=scope)
    if not client.cfg.is_ready():
        client = build_unified_llm_client(cfg, scope="strategy_manager")
    if not client.cfg.is_ready():
        client = build_unified_llm_client(cfg, scope="unified")
    if not client.cfg.is_ready():
        return NlScreenerResult(status="error", data={}, msg="LLM未配置，请检查私有配置")

    # 执行模型调用并解析结构化输出。
    # 保留实际发送给模型的提示词，供前端可视化展示与审计。
    messages = _build_messages(text)
    llm_text = client.complete(
        messages=messages,
        temperature=0.1,
        # 与 parse_strategy 同步：为 reasoning 阶段留出足够预算，避免长 JSON 被截断。
        max_tokens=8192,
        response_format={"type": "json_object"},
    ).get("content", "")
    parsed = _extract_json(llm_text)
    if parsed is None:
        return NlScreenerResult(
            status="error",
            data={"raw_llm": llm_text},
            msg="模型返回格式异常，未解析出JSON",
        )

    screen_conditions = _normalize_conditions(parsed.get("screen_conditions", []))
    # execution_rules 可能是对象（包含 sort/top_n 等）或列表，两种格式都要支持
    raw_execution_rules = parsed.get("execution_rules", [])
    if isinstance(raw_execution_rules, dict):
        execution_rules = raw_execution_rules
    elif isinstance(raw_execution_rules, list):
        execution_rules = raw_execution_rules
    else:
        execution_rules = []
    strategy_meta = (
        parsed.get("strategy_meta", {})
        if isinstance(parsed.get("strategy_meta", {}), dict)
        else {}
    )
    warnings = parsed.get("warnings", []) if isinstance(parsed.get("warnings", []), list) else []
    # 示例ID后端兜底：前端未传 example_id 时按提示词语义推断。
    inferred_example_id = _infer_example_id_from_prompt(text)
    effective_input_example_id = str(example_id or "").strip() or inferred_example_id
    # 对内置示例策略应用后端强制排序，避免依赖LLM理解"按xx排序"的语义。
    forced_sort = resolve_screener_demo_sort(user_prompt=text, example_id=effective_input_example_id)
    sort_by = str(forced_sort.get("sort_by", "")).strip() or None
    sort_order = str(forced_sort.get("sort_order", "desc")).strip().lower() or "desc"
    matched_example_id = str(forced_sort.get("matched_example_id", "")).strip()
    # 排序语义兜底：示例识别失败时，按提示词关键词启用稳定排序字段。
    if not sort_by:
        compact_text = re.sub(r"\s+", "", text)
        if ("成交额" in compact_text) or ("amount" in compact_text.lower()):
            sort_by = "amount"
            sort_order = "desc"
        elif ("涨跌幅" in compact_text) or ("change_pct" in compact_text.lower()):
            sort_by = "change_pct"
            sort_order = "desc"

    # 从 execution_rules 中提取 top_n，用于限制返回数量
    top_n = None
    if isinstance(execution_rules, dict):
        raw_top_n = execution_rules.get("top_n")
        if raw_top_n is not None:
            try:
                top_n = int(raw_top_n)
                if top_n < 1 or top_n > 500:
                    top_n = None
            except (ValueError, TypeError):
                top_n = None

    # 如果 execution_rules 中没有 top_n，从用户提示词中提取数量限制
    if top_n is None:
        # 匹配常见模式："前20只"、"前30支"、"top 20"、"输出前20"等
        top_n_patterns = [
            r'前\s*(\d+)\s*[只支个条]',
            r'top\s*(\d+)',
            r'输出\s*前\s*(\d+)',
            r'取\s*前\s*(\d+)',
        ]
        for pattern in top_n_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    extracted_n = int(match.group(1))
                    if 1 <= extracted_n <= 500:
                        top_n = extracted_n
                        break
                except (ValueError, TypeError):
                    continue

    # 只执行 enabled=true 的条件，避免不可执行规则误入筛选器。
    executable = [cond for cond in screen_conditions if bool(cond.get("enabled", False))]
    # 示例强制条件：命中内置示例时，直接采用后端固定条件，不依赖LLM条件质量。
    # 优先使用用户显式传入的 example_id，避免被提示词匹配结果覆盖。
    effective_example_id = effective_input_example_id or matched_example_id
    forced_conditions = _fallback_conditions_for_example(effective_example_id)
    if forced_conditions:
        executable = forced_conditions
        warnings.append("已按内置示例启用后端固定筛选条件（不依赖LLM条件解析）。")
    # 示例兜底：非示例场景下，当模型未产出可执行条件时再尝试按传入示例ID回退。
    if not executable:
        fallback_conditions = _fallback_conditions_for_example(effective_input_example_id)
        if fallback_conditions:
            executable = fallback_conditions
            warnings.append("模型未产出可执行条件，已按示例模板回退到内置筛选条件。")
    # 当没有任何可执行条件时返回空结果，避免退化成"默认股票池前N只"假筛选。
    if not executable:
        warnings.append("未解析到可执行筛选条件，已返回空结果，请补充更明确的筛选描述。")
        execution_plan = build_execution_plan(execution_rules=execution_rules, user_prompt=text)
        empty_filter_result = {
            "total": 0,
            "page": max(1, int(page or 1)),
            "page_size": max(1, min(int(page_size or 50), 500)),
            "total_pages": 1,
            "data": [],
            "source_stats": {
                "pool": {"total": 0, "counts": {}, "ratio_pct": {}},
                "filtered": {"total": 0, "counts": {}, "ratio_pct": {}},
                "filtered_page": {"total": 0, "counts": {}, "ratio_pct": {}},
            },
        }
        portfolio_plan = build_portfolio_plan(filter_result=empty_filter_result, execution_plan=execution_plan)
        return NlScreenerResult(
            status="success",
            data={
                "parsed": {
                    "screen_conditions": screen_conditions,
                    "execution_rules": execution_rules,
                    "strategy_meta": strategy_meta,
                    "warnings": warnings,
                },
                "execution_plan": execution_plan,
                "portfolio_plan": portfolio_plan,
                "filter_result": empty_filter_result,
                "prompt_trace": {
                    "system_prompt": str(messages[0].get("content", "") if len(messages) > 0 else ""),
                    "user_prompt": str(messages[1].get("content", "") if len(messages) > 1 else ""),
                },
                "applied_sort": {
                    "sort_by": sort_by or "",
                    "sort_order": sort_order,
                    "from_example_id": matched_example_id,
                },
            },
            msg="ok",
        )
    market_conditions = [cond for cond in executable if cond.get("tab") == "market"]
    technical_conditions = [cond for cond in executable if cond.get("tab") == "technical"]
    financial_conditions = [cond for cond in executable if cond.get("tab") == "financial"]

    # 如果 LLM 返回了 top_n，用它来限制返回数量（覆盖前端传入的 page_size）
    effective_page_size = page_size
    if top_n is not None:
        effective_page_size = top_n

    # 复用现有筛选执行器，返回分页结果供前端直接展示。
    filter_result = apply_filters(
        market_conditions=market_conditions,
        technical_conditions=technical_conditions,
        financial_conditions=financial_conditions,
        logic_mode=str(logic_mode or "AND").upper(),
        page=max(1, int(page or 1)),
        page_size=max(1, min(int(effective_page_size or 50), 500)),
        sort_by=sort_by,
        sort_order=sort_order,
    )

    # 当用户通过 top_n 限制返回数量时，同步截断 total，避免前端显示"共3156条"误导用户。
    # 场景：用户说"前30只"，total 应显示30，而不是满足条件的全部股票数。
    if top_n is not None and isinstance(filter_result, dict):
        original_total = int(filter_result.get("total") or 0)
        capped_total = min(original_total, top_n)
        filter_result["total"] = capped_total
        # 同步 total_pages，避免翻页逻辑与截断后的 total 不一致。
        original_total_pages = int(filter_result.get("total_pages") or 1)
        if original_total > 0 and capped_total < original_total:
            capped_pages = max(1, (capped_total + filter_result.get("page_size", top_n) - 1) // max(1, int(filter_result.get("page_size") or top_n)))
            filter_result["total_pages"] = min(original_total_pages, capped_pages)

    # 将 execution_rules 归一化为可执行计划，便于回测/实盘层后续直接消费。
    execution_plan = build_execution_plan(execution_rules=execution_rules, user_prompt=text)
    # 基于筛选结果和执行计划生成组合建议（例如 Top3 + 三成仓）。
    portfolio_plan = build_portfolio_plan(filter_result=filter_result, execution_plan=execution_plan)

    return NlScreenerResult(
        status="success",
        data={
            "parsed": {
                "screen_conditions": screen_conditions,
                "execution_rules": execution_rules,
                "strategy_meta": strategy_meta,
                "warnings": warnings,
            },
            "execution_plan": execution_plan,
            "portfolio_plan": portfolio_plan,
            "filter_result": filter_result,
            "prompt_trace": {
                "system_prompt": str(messages[0].get("content", "") if len(messages) > 0 else ""),
                "user_prompt": str(messages[1].get("content", "") if len(messages) > 1 else ""),
            },
            "applied_sort": {
                "sort_by": sort_by or "",
                "sort_order": sort_order,
                "from_example_id": matched_example_id,
            },
        },
        msg="ok",
    )
