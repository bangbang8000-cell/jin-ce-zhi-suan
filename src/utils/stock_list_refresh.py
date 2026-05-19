from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import akshare as ak
import pandas as pd

from src.utils.config_loader import ConfigLoader
from src.utils.tushare_provider import TushareProvider


def _empty_stock_list_df() -> pd.DataFrame:
    # 统一返回空表结构，避免上层每次手工补列。
    return pd.DataFrame(columns=["code", "name", "market", "source", "updated_at"])


def _infer_market(raw_code: str) -> str:
    # 按 A 股常见前缀推断交易所，无法识别时直接过滤。
    code = str(raw_code or "").strip()
    if len(code) != 6 or not code.isdigit():
        return ""
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("00", "30")):
        return "SZ"
    if code.startswith(("4", "8")):
        return "BJ"
    return ""


def normalize_stock_list_df(df: pd.DataFrame, source: str) -> pd.DataFrame:
    # 将不同来源的股票列表统一成 history_sync 可直接复用的 CSV 结构。
    work = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if work.empty:
        return _empty_stock_list_df()
    code_col = "code" if "code" in work.columns else ("symbol" if "symbol" in work.columns else "")
    name_col = "name" if "name" in work.columns else ("名称" if "名称" in work.columns else "")
    if not code_col:
        return _empty_stock_list_df()
    work["raw_code"] = work[code_col].astype(str).str.strip()
    if name_col:
        work["name"] = work[name_col].astype(str).str.strip()
    else:
        work["name"] = ""
    work["market"] = work["raw_code"].map(_infer_market)
    work = work[(work["market"] != "") & (work["raw_code"].str.len() == 6)]
    if work.empty:
        return _empty_stock_list_df()
    work["code"] = work["raw_code"] + "." + work["market"]
    work["source"] = str(source or "").strip().lower() or "unknown"
    work["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    work = work[["code", "name", "market", "source", "updated_at"]]
    work = work.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)
    return work


def _safe_write_csv(df: pd.DataFrame, output_path: Path) -> None:
    # 先写临时文件再替换，避免中途中断时破坏旧股票池。
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        suffix=".csv",
        dir=str(output_path.parent),
        encoding="utf-8",
        newline="",
    ) as temp_file:
        temp_path = Path(temp_file.name)
    df.to_csv(temp_path, index=False, encoding="utf-8")
    temp_path.replace(output_path)


def refresh_stock_list(output_path, provider="auto", akshare_client=None, tushare_client=None):
    # 按 AkShare 优先、TuShare 兜底的顺序刷新股票池文件。
    path = Path(output_path)
    provider_name = str(provider or "auto").strip().lower() or "auto"
    if provider_name == "akshare":
        attempts = [("akshare", akshare_client)]
    elif provider_name == "tushare":
        attempts = [("tushare", tushare_client)]
    else:
        attempts = [("akshare", akshare_client), ("tushare", tushare_client)]
    errors = []
    for index, (source, client) in enumerate(attempts):
        if client is None:
            errors.append(f"{source}: client unavailable")
            continue
        try:
            raw_df = client.fetch_stock_list()
            normalized = normalize_stock_list_df(raw_df, source=source)
            if normalized.empty:
                raise RuntimeError(f"{source} returned empty normalized stock list")
            _safe_write_csv(normalized, path)
            return {
                "status": "success",
                "source": source,
                "fallback_used": index > 0,
                "codes": int(len(normalized)),
                "output_path": str(path),
                "preserved_existing_file": False,
            }
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    return {
        "status": "error",
        "source": "",
        "fallback_used": provider_name == "auto",
        "codes": 0,
        "output_path": str(path),
        "preserved_existing_file": path.exists(),
        "error": " | ".join(errors),
    }


class AkshareStockListClient:
    # 通过 AkShare 免费接口拉取全 A 股票基础清单。
    def fetch_stock_list(self):
        df = ak.stock_info_a_code_name()
        if not isinstance(df, pd.DataFrame):
            return pd.DataFrame()
        if "code" not in df.columns and "证券代码" in df.columns:
            df = df.rename(columns={"证券代码": "code"})
        if "name" not in df.columns:
            if "证券简称" in df.columns:
                df = df.rename(columns={"证券简称": "name"})
            elif "名称" in df.columns:
                df = df.rename(columns={"名称": "name"})
        return df


class TushareStockListClient:
    # 当免费源不可用时，使用 TuShare 作为股票列表兜底来源。
    def __init__(self, cfg=None):
        cfg = cfg if cfg is not None else ConfigLoader.reload()
        token = str(cfg.get("data_provider.tushare_token", "") or "").strip()
        self._provider = TushareProvider(token=token)

    def fetch_stock_list(self):
        if getattr(self._provider, "pro", None) is None:
            raise RuntimeError("tushare token not configured")
        df = self._provider.pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
        if not isinstance(df, pd.DataFrame) or ("ts_code" not in df.columns):
            return pd.DataFrame()
        out = pd.DataFrame()
        out["code"] = df["ts_code"].astype(str).str.split(".").str[0]
        out["name"] = df["name"].astype(str).str.strip() if "name" in df.columns else ""
        return out


class RuntimeConfigView:
    # 用轻量包装器复用统一的 get(path, default) 读取协议。
    def __init__(self, data=None):
        self._data = data if isinstance(data, dict) else {}

    def get(self, path, default=None):
        cur = self._data
        for key in str(path or "").split("."):
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur.get(key)
        return cur


def build_refresh_clients(config_data=None):
    # 统一构建刷新股票池所需的 provider 适配器。
    cfg = RuntimeConfigView(config_data) if isinstance(config_data, dict) else None
    return {
        "akshare": AkshareStockListClient(),
        "tushare": TushareStockListClient(cfg=cfg),
    }
