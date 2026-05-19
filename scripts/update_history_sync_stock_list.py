import argparse
from pathlib import Path

from src.utils.stock_list_refresh import build_refresh_clients, refresh_stock_list


def main():
    # CLI 仅负责解析参数并触发统一的刷新核心逻辑。
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
    print(
        f"股票池更新完成 source={result['source']} "
        f"codes={result['codes']} output={result['output_path']}"
    )


if __name__ == "__main__":
    main()
