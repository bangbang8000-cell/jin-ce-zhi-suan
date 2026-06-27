# src/utils/jqdata_provider.py
import pandas as pd
from datetime import datetime, timedelta
import os
import time
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators


class JqdataProvider:
    """
    JQData (聚宽) Data Provider
    官网：joinquant.com
    需要注册获取用户名/密码（手机号/邮箱），研究版免费
    """

    def __init__(self, username=None, password=None):
        cfg = ConfigLoader.reload()
        self.username = username or cfg.get("data_provider.jqdata_username", "")
        self.password = password or cfg.get("data_provider.jqdata_password", "")
        self.last_error = ""
        self._client = None
        self._cache_enabled = bool(cfg.get("data_provider.local_cache_enabled", True))
        cache_dir = str(cfg.get("data_provider.local_cache_dir", "data/history/cache") or "data/history/cache")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        self._cache_dir = cache_dir if os.path.isabs(cache_dir) else os.path.join(project_root, cache_dir)
        os.makedirs(self._cache_dir, exist_ok=True)

        # 实时行情缓存
        self._latest_bar_cache_ttl_sec = max(5.0, float(cfg.get("data_provider.jqdata_latest_bar_cache_ttl_sec", 30) or 30))
        self._latest_bar_cache = {}

        if self.username and self.password:
            self._login()

    # ------------------------------------------------------------------ #
    #  登录 / 登出
    # ------------------------------------------------------------------ #
    def _login(self):
        """建立 JQData 连接"""
        try:
            from jqdatasdk import auth
            auth(self.username, self.password)
            self._client = True  # 标记已认证
            self.last_error = ""
        except ImportError:
            self.last_error = "jqdatasdk 未安装，请运行: pip install jqdatasdk"
        except Exception as e:
            self.last_error = f"jqdata_auth_failed err={e}"
            self._client = None

    def _ensure_connected(self):
        """确保已连接到 JQData"""
        if self._client:
            return True
        self._login()
        return self._client is not None

    # ------------------------------------------------------------------ #
    #  代码转换
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_jqdata_code(code: str) -> str:
        """600036.SH -> 600036.XSHG, 000001.SZ -> 000001.XSZ"""
        code = str(code).upper().strip()
        if "." not in code:
            # 纯 6 位数字
            if code.startswith(("6", "9")):
                return f"{code}.XSHG"
            return f"{code}.XSZ"
        parts = code.split(".")
        suffix = parts[1].upper()
        if suffix == "SH":
            return f"{parts[0]}.XSHG"
        return f"{parts[0]}.XSZ"

    # ------------------------------------------------------------------ #
    #  缓存（与其他 Provider 保持一致的模式）
    # ------------------------------------------------------------------ #
    def _cache_file_path(self, code, interval="1min"):
        safe_code = str(code).upper().replace(".", "_")
        return os.path.join(self._cache_dir, f"jqdata_{safe_code}_{interval}.csv")

    def _normalize_minutes_df(self, df):
        if df is None or df.empty:
            return pd.DataFrame()
        work = df.copy()
        if "time" in work.columns and "dt" not in work.columns:
            work = work.rename(columns={"time": "dt"})
        required_cols = ["code", "open", "high", "low", "close", "vol", "amount", "dt"]
        for c in required_cols:
            if c not in work.columns:
                return pd.DataFrame()
        work["dt"] = pd.to_datetime(work["dt"])
        for c in ["open", "high", "low", "close", "vol", "amount"]:
            work[c] = pd.to_numeric(work[c], errors="coerce")
        work = work.dropna(subset=["dt", "open", "high", "low", "close"])
        work = work.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        return work[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]

    def _normalize_daily_df(self, df):
        if df is None or df.empty:
            return pd.DataFrame()
        work = df.copy()
        if "date" in work.columns and "dt" not in work.columns:
            work = work.rename(columns={"date": "dt"})
        required_cols = ["code", "open", "high", "low", "close", "vol", "amount", "dt"]
        for c in required_cols:
            if c not in work.columns:
                return pd.DataFrame()
        work["dt"] = pd.to_datetime(work["dt"])
        for c in ["open", "high", "low", "close", "vol", "amount"]:
            work[c] = pd.to_numeric(work[c], errors="coerce")
        work = work.dropna(subset=["dt", "open", "high", "low", "close"])
        work = work.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        return work[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]

    def _load_cached_minute_data(self, code, start_time, end_time):
        if not self._cache_enabled:
            return pd.DataFrame(), False
        path = self._cache_file_path(code, "1min")
        if not os.path.exists(path):
            return pd.DataFrame(), False
        try:
            df = pd.read_csv(path)
            if "dt" in df.columns:
                df["dt"] = pd.to_datetime(df["dt"])
            df = self._normalize_minutes_df(df)
            if df.empty:
                return pd.DataFrame(), False
            full_coverage = df["dt"].min() <= start_time and df["dt"].max() >= end_time
            df_range = df[(df["dt"] >= start_time) & (df["dt"] <= end_time)].copy()
            return df_range, bool(full_coverage and not df_range.empty)
        except Exception:
            return pd.DataFrame(), False

    def _save_minute_cache(self, code, df):
        if not self._cache_enabled or df is None or df.empty:
            return
        path = self._cache_file_path(code, "1min")
        try:
            df_save = self._normalize_minutes_df(df)
            if df_save.empty:
                return
            if os.path.exists(path):
                old_df = pd.read_csv(path)
                if "dt" in old_df.columns:
                    old_df["dt"] = pd.to_datetime(old_df["dt"])
                old_df = self._normalize_minutes_df(old_df)
                if not old_df.empty:
                    df_save = pd.concat([old_df, df_save], ignore_index=True)
                    df_save = self._normalize_minutes_df(df_save)
            df_save.to_csv(path, index=False, encoding="utf-8")
        except Exception:
            return

    # ------------------------------------------------------------------ #
    #  实时行情缓存
    # ------------------------------------------------------------------ #
    def _get_latest_bar_cached(self, code):
        code_u = str(code).upper()
        state = self._latest_bar_cache.get(code_u)
        if not isinstance(state, dict):
            return None
        ts_mono = float(state.get("ts", 0.0) or 0.0)
        payload = state.get("payload")
        if not isinstance(payload, dict):
            return None
        if (time.monotonic() - ts_mono) > float(self._latest_bar_cache_ttl_sec):
            return None
        return dict(payload)

    def _set_latest_bar_cached(self, code, payload):
        code_u = str(code).upper()
        if not isinstance(payload, dict):
            return
        self._latest_bar_cache[code_u] = {
            "ts": time.monotonic(),
            "payload": payload
        }

    # ------------------------------------------------------------------ #
    #  核心接口
    # ------------------------------------------------------------------ #
    def fetch_minute_data(self, code, start_time, end_time):
        """
        获取历史 1 分钟 K 线数据
        """
        if not self._ensure_connected():
            return pd.DataFrame()

        cached_df, cache_hit = self._load_cached_minute_data(code, start_time, end_time)
        if cache_hit:
            return cached_df

        fetch_start = start_time
        if not cached_df.empty:
            fetch_start = cached_df["dt"].max() + timedelta(minutes=1)
            if fetch_start > end_time:
                return cached_df

        jq_code = self._to_jqdata_code(code)
        start_str = fetch_start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            from jqdatasdk import get_price
            df = get_price(
                jq_code,
                start_date=start_str,
                end_date=end_str,
                frequency="1m",
                fields=["open", "high", "low", "close", "volume", "money"],
                skip_paused=True
            )
            if df is None or df.empty:
                self.last_error = f"no_data code={jq_code} range={start_str}->{end_str}"
                return cached_df if not cached_df.empty else pd.DataFrame()

            # JQData get_price 返回 DataFrame，index 为 datetime
            df = df.reset_index()
            if "time" not in df.columns and "index" in df.columns:
                df = df.rename(columns={"index": "time"})
            if "time" not in df.columns:
                # 尝试第一个时间类型的列
                for col in df.columns:
                    if "time" in col.lower() or "date" in col.lower():
                        df = df.rename(columns={col: "time"})
                        break

            df = df.rename(columns={
                "time": "dt",
                "volume": "vol",
                "money": "amount"
            })
            df["code"] = code
            df = df[(df["dt"] >= start_time) & (df["dt"] <= end_time)]

            df = self._normalize_minutes_df(df[["code", "dt", "open", "high", "low", "close", "vol", "amount"]])
            if not cached_df.empty:
                df = pd.concat([cached_df, df], ignore_index=True)
                df = self._normalize_minutes_df(df)

            self._save_minute_cache(code, df)
            self.last_error = ""
            return df

        except Exception as e:
            self.last_error = f"fetch_minute_data_failed code={jq_code} range={start_str}->{end_str} err={e}"
            print(f"Error fetching JQData history: {e}")
            return cached_df if not cached_df.empty else pd.DataFrame()

    def fetch_daily_data(self, code, start_time, end_time):
        """
        获取日 K 线数据
        """
        if not self._ensure_connected():
            return pd.DataFrame()

        jq_code = self._to_jqdata_code(code)
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            from jqdatasdk import get_price
            df = get_price(
                jq_code,
                start_date=start_str,
                end_date=end_str,
                frequency="daily",
                fields=["open", "high", "low", "close", "volume", "money"],
                skip_paused=True
            )
            if df is None or df.empty:
                return pd.DataFrame()

            df = df.reset_index()
            if "time" not in df.columns and "index" in df.columns:
                df = df.rename(columns={"index": "time"})
            if "time" not in df.columns:
                for col in df.columns:
                    if "time" in col.lower() or "date" in col.lower():
                        df = df.rename(columns={col: "time"})
                        break

            df = df.rename(columns={
                "time": "dt",
                "volume": "vol",
                "money": "amount"
            })
            df["code"] = code
            return self._normalize_daily_df(df[["code", "dt", "open", "high", "low", "close", "vol", "amount"]])

        except Exception as e:
            self.last_error = f"fetch_daily_data_failed code={jq_code} err={e}"
            return pd.DataFrame()

    def fetch_kline_data(self, code, start_time, end_time, interval="1min"):
        """
        获取各周期 K 线数据，支持 1/5/15/30/60min 及日线
        """
        if not self._ensure_connected():
            return pd.DataFrame()

        tf = str(interval or "1min")
        if tf == "1min":
            return self.fetch_minute_data(code, start_time, end_time)
        if tf == "D":
            df_d = self.fetch_daily_data(code, start_time, end_time)
            if not df_d.empty:
                return df_d
            df_1m = self.fetch_minute_data(code, start_time, end_time)
            return Indicators.resample(df_1m, "D") if not df_1m.empty else pd.DataFrame()

        # 分钟级周期
        freq_map = {
            "5min": "5m",
            "15min": "15m",
            "30min": "30m",
            "60min": "60m",
        }
        if tf in freq_map:
            jq_code = self._to_jqdata_code(code)
            start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
            end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                from jqdatasdk import get_price
                df = get_price(
                    jq_code,
                    start_date=start_str,
                    end_date=end_str,
                    frequency=freq_map[tf],
                    fields=["open", "high", "low", "close", "volume", "money"],
                    skip_paused=True
                )
                if df is not None and not df.empty:
                    df = df.reset_index()
                    if "time" not in df.columns and "index" in df.columns:
                        df = df.rename(columns={"index": "time"})
                    if "time" not in df.columns:
                        for col in df.columns:
                            if "time" in col.lower() or "date" in col.lower():
                                df = df.rename(columns={col: "time"})
                                break
                    df = df.rename(columns={
                        "time": "dt",
                        "volume": "vol",
                        "money": "amount"
                    })
                    df["code"] = code
                    return self._normalize_minutes_df(df[["code", "dt", "open", "high", "low", "close", "vol", "amount"]])
            except Exception as e:
                self.last_error = f"fetch_kline_data_failed code={jq_code} interval={tf} err={e}"
                print(f"Error fetching JQData kline: {e}")

            # 回退：用 1min 重采样
            df_1m = self.fetch_minute_data(code, start_time, end_time)
            if df_1m.empty:
                return pd.DataFrame()
            return Indicators.resample(df_1m, tf)

        # 未知周期，用 1min 重采样
        df_1m = self.fetch_minute_data(code, start_time, end_time)
        if df_1m.empty:
            return pd.DataFrame()
        return Indicators.resample(df_1m, tf)

    def get_latest_bar(self, code):
        """
        获取最新实时行情
        JQData 没有直接的实时报价 API，用当前时刻的 1m K 线代替
        """
        cached = self._get_latest_bar_cached(code)
        if cached:
            return cached

        if not self._ensure_connected():
            return None

        jq_code = self._to_jqdata_code(code)
        now = datetime.now()
        # 取最近 5 分钟的数据
        start_str = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        end_str = now.strftime("%Y-%m-%d %H:%M:%S")

        try:
            from jqdatasdk import get_price
            df = get_price(
                jq_code,
                start_date=start_str,
                end_date=end_str,
                frequency="1m",
                fields=["open", "high", "low", "close", "volume", "money"],
                skip_paused=True
            )
            if df is None or df.empty:
                # 非交易时段或刚收盘，取最近一根日 K
                try:
                    df_daily = get_price(
                        jq_code,
                        count=1,
                        frequency="daily",
                        fields=["open", "high", "low", "close", "volume", "money"],
                        skip_paused=True
                    )
                    if df_daily is not None and not df_daily.empty:
                        row = df_daily.iloc[-1]
                        dt = pd.to_datetime(row.name if hasattr(row, "name") else row.get("time", row.get("date", now)))
                        payload = {
                            "code": code,
                            "dt": dt,
                            "open": float(row.get("open", 0.0) or 0.0),
                            "high": float(row.get("high", 0.0) or 0.0),
                            "low": float(row.get("low", 0.0) or 0.0),
                            "close": float(row.get("close", 0.0) or 0.0),
                            "vol": float(row.get("volume", 0.0) or 0.0),
                            "amount": float(row.get("money", 0.0) or 0.0),
                        }
                        self._set_latest_bar_cached(code, payload)
                        self.last_error = ""
                        return payload
                except Exception:
                    pass
                self.last_error = f"no_data code={jq_code}"
                return None

            df = df.reset_index()
            if "time" not in df.columns and "index" in df.columns:
                df = df.rename(columns={"index": "time"})
            if "time" not in df.columns:
                for col in df.columns:
                    if "time" in col.lower() or "date" in col.lower():
                        df = df.rename(columns={col: "time"})
                        break

            row = df.iloc[-1]
            dt_col = "time" if "time" in df.columns else df.columns[0]
            dt = pd.to_datetime(row[dt_col])

            payload = {
                "code": code,
                "dt": dt,
                "open": float(row.get("open", 0.0) or 0.0),
                "high": float(row.get("high", 0.0) or 0.0),
                "low": float(row.get("low", 0.0) or 0.0),
                "close": float(row.get("close", 0.0) or 0.0),
                "vol": float(row.get("volume", 0.0) or 0.0),
                "amount": float(row.get("money", 0.0) or 0.0),
            }
            self._set_latest_bar_cached(code, payload)
            self.last_error = ""
            return payload

        except Exception as e:
            self.last_error = f"get_latest_bar_failed code={jq_code} err={e}"
            print(f"Error fetching JQData latest bar: {e}")
            return None

    def check_connectivity(self, code="600000.XSHG"):
        """连通性检查"""
        if not self._ensure_connected():
            return False, self.last_error
        try:
            from jqdatasdk import get_security_info
            info = get_security_info(code)
            if info:
                self.last_error = ""
                return True, "ok"
            return False, "get_security_info 返回空"
        except Exception as e:
            return False, str(e)

    def set_credentials(self, username, password):
        """运行时设置凭据"""
        self.username = username
        self.password = password
        self._login()

    def fetch_financial_data(self, codes: list, date: str) -> dict:
        """
        批量获取财务数据

        Args:
            codes: 股票代码列表（如 ["600000.SH", "000001.SZ"]）
            date: 查询日期（如 "2026-06-23"）

        Returns:
            {
                "600000.SH": {
                    "pe_ttm": 8.5,
                    "pb": 0.6,
                    "ps_ttm": 2.3,
                    "roe": 12.5,
                    "roa": 1.2,
                    "gross_margin": 45.2,
                    "net_margin": 28.3,
                    "revenue_yoy": 15.6,
                    "profit_yoy": 18.9
                },
                ...
            }
        """
        if not self._ensure_connected():
            self.last_error = "JQData 未连接"
            return {}

        if not codes:
            return {}

        result = {}

        try:
            from jqdatasdk import get_fundamentals, valuation, indicator, stat

            # 转换代码格式：600000.SH -> 600000.XSHG
            jq_codes = [self._to_jqdata_code(code) for code in codes]

            # 1. 获取估值指标（PE/PB/PS）- 从 valuation 表
            q_valuation = get_fundamentals(
                query(
                    valuation.code,
                    valuation.pe_ratio,
                    valuation.pb_ratio,
                    valuation.ps_ratio
                ).filter(
                    valuation.code.in_(jq_codes)
                ),
                date=date
            )

            if q_valuation is not None and not q_valuation.empty:
                for _, row in q_valuation.iterrows():
                    # 转换回标准代码格式：600000.XSHG -> 600000.SH
                    jq_code = row['code']
                    code = self._from_jqdata_code(jq_code)

                    if code not in result:
                        result[code] = {}

                    # 估值指标
                    if pd.notna(row.get('pe_ratio')):
                        result[code]['pe_ttm'] = float(row['pe_ratio'])
                    if pd.notna(row.get('pb_ratio')):
                        result[code]['pb'] = float(row['pb_ratio'])
                    if pd.notna(row.get('ps_ratio')):
                        result[code]['ps_ttm'] = float(row['ps_ratio'])

            # 2. 获取财务指标（ROE/ROA/毛利率/净利率）- 从 indicator 表
            q_indicator = get_fundamentals(
                query(
                    indicator.code,
                    indicator.roe,
                    indicator.roa,
                    indicator.gross_profit_margin,
                    indicator.net_profit_margin
                ).filter(
                    indicator.code.in_(jq_codes)
                ),
                date=date
            )

            if q_indicator is not None and not q_indicator.empty:
                for _, row in q_indicator.iterrows():
                    jq_code = row['code']
                    code = self._from_jqdata_code(jq_code)

                    if code not in result:
                        result[code] = {}

                    # 盈利指标
                    if pd.notna(row.get('roe')):
                        result[code]['roe'] = float(row['roe'])
                    if pd.notna(row.get('roa')):
                        result[code]['roa'] = float(row['roa'])
                    if pd.notna(row.get('gross_profit_margin')):
                        result[code]['gross_margin'] = float(row['gross_profit_margin'])
                    if pd.notna(row.get('net_profit_margin')):
                        result[code]['net_margin'] = float(row['net_profit_margin'])

            # 3. 获取成长性指标（营收同比/利润同比）- 从 stat 表
            q_stat = get_fundamentals(
                query(
                    stat.code,
                    stat.inc_revenue_year_on_year,
                    stat.inc_net_profit_year_on_year
                ).filter(
                    stat.code.in_(jq_codes)
                ),
                date=date
            )

            if q_stat is not None and not q_stat.empty:
                for _, row in q_stat.iterrows():
                    jq_code = row['code']
                    code = self._from_jqdata_code(jq_code)

                    if code not in result:
                        result[code] = {}

                    # 成长性指标
                    if pd.notna(row.get('inc_revenue_year_on_year')):
                        result[code]['revenue_yoy'] = float(row['inc_revenue_year_on_year'])
                    if pd.notna(row.get('inc_net_profit_year_on_year')):
                        result[code]['profit_yoy'] = float(row['inc_net_profit_year_on_year'])

            self.last_error = ""
            return result

        except Exception as e:
            err_text = str(e)
            self.last_error = f"fetch_financial_data_failed date={date} err={err_text}"
            return result  # 返回已获取的部分数据

    def _from_jqdata_code(self, jq_code: str) -> str:
        """
        转换 JQData 代码格式为标准格式
        600000.XSHG -> 600000.SH
        000001.XSHE -> 000001.SZ
        """
        if not jq_code or '.' not in jq_code:
            return jq_code

        parts = jq_code.split('.')
        code = parts[0]
        exchange = parts[1].upper()

        if exchange == 'XSHG':
            return f"{code}.SH"
        elif exchange == 'XSHE':
            return f"{code}.SZ"
        else:
            return jq_code
