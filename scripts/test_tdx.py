"""测试 pytdx/TdxProvider 的可用性"""
import time
import sys

def test_tdx_provider():
    print("=" * 60)
    print("测试 TdxProvider (pytdx) 可用性")
    print("=" * 60)

    # 1. 测试导入
    print("\n[1/5] 测试导入 TdxProvider...")
    try:
        from src.utils.tdx_provider import TdxProvider
        print("[OK] TdxProvider 导入成功")
    except Exception as e:
        print(f"[FAIL] TdxProvider 导入失败: {e}")
        return False

    # 2. 测试初始化
    print("\n[2/5] 测试初始化 TdxProvider...")
    try:
        tdx = TdxProvider()
        print(f"[OK] TdxProvider 初始化成功")
        print(f"  - 主机: {tdx.host}:{tdx.port}")
        print(f"  - 超时: {tdx.quote_timeout_sec}秒")
        print(f"  - 模式: {tdx.provider_mode}")
    except Exception as e:
        print(f"[FAIL] TdxProvider 初始化失败: {e}")
        return False

    # 3. 测试连通性
    print("\n[3/5] 测试连通性...")
    try:
        ok, msg = tdx.check_connectivity("600036.SH")
        if ok:
            print(f"[OK] 连通性检查通过: {msg}")
        else:
            print(f"[FAIL] 连通性检查失败: {msg}")
            return False
    except Exception as e:
        print(f"[FAIL] 连通性检查异常: {e}")
        return False

    # 4. 测试获取单只股票数据
    print("\n[4/5] 测试获取单只股票数据 (600036.SH 招商银行)...")
    try:
        start_time = time.time()
        bar = tdx.get_latest_bar("600036.SH")
        elapsed = time.time() - start_time

        if bar:
            print(f"[OK] 获取数据成功 (耗时: {elapsed:.2f}秒)")
            print(f"  - 代码: {bar.get('code')}")
            print(f"  - 时间: {bar.get('dt')}")
            print(f"  - 开盘: {bar.get('open')}")
            print(f"  - 最高: {bar.get('high')}")
            print(f"  - 最低: {bar.get('low')}")
            print(f"  - 收盘: {bar.get('close')}")
            print(f"  - 成交量: {bar.get('vol')}")
            print(f"  - 成交额: {bar.get('amount')}")
        else:
            print(f"[FAIL] 获取数据失败: 返回空数据")
            return False
    except Exception as e:
        print(f"[FAIL] 获取数据异常: {e}")
        return False

    # 5. 测试批量获取（小样本）
    print("\n[5/5] 测试批量获取（10只股票）...")
    test_codes = [
        "600036.SH",  # 招商银行
        "000001.SZ",  # 平安银行
        "600519.SH",  # 贵州茅台
        "000858.SZ",  # 五粮液
        "601318.SH",  # 中国平安
        "000333.SZ",  # 美的集团
        "600276.SH",  # 恒瑞医药
        "000651.SZ",  # 格力电器
        "601888.SH",  # 中国中免
        "300750.SZ",  # 宁德时代
    ]

    try:
        start_time = time.time()
        success_count = 0
        fail_count = 0

        for code in test_codes:
            try:
                bar = tdx.get_latest_bar(code)
                if bar:
                    success_count += 1
                    print(f"  [OK] {code}: 收盘 {bar.get('close')}")
                else:
                    fail_count += 1
                    print(f"  [FAIL] {code}: 无数据")
            except Exception as e:
                fail_count += 1
                print(f"  [FAIL] {code}: {e}")

        elapsed = time.time() - start_time
        print(f"\n批量获取完成 (耗时: {elapsed:.2f}秒)")
        print(f"  - 成功: {success_count}/{len(test_codes)}")
        print(f"  - 失败: {fail_count}/{len(test_codes)}")

        if success_count > 0:
            avg_time = elapsed / success_count
            print(f"  - 平均耗时: {avg_time:.2f}秒/只")

            # 估算全市场耗时
            estimated_total = avg_time * 5000  # 假设5000只股票
            print(f"\n估算全市场(5000只)耗时: {estimated_total:.0f}秒 ({estimated_total/60:.1f}分钟)")

            if estimated_total > 300:  # 5分钟
                print("[WARN] 警告: 全市场获取耗时过长，建议使用缓存机制")
            else:
                print("[OK] 全市场获取耗时可接受")

        return success_count > 0

    except Exception as e:
        print(f"[FAIL] 批量获取异常: {e}")
        return False

if __name__ == "__main__":
    success = test_tdx_provider()

    print("\n" + "=" * 60)
    if success:
        print("[OK] pytdx 测试通过，可以正常使用")
        sys.exit(0)
    else:
        print("[FAIL] pytdx 测试失败，请检查网络连接或配置")
        sys.exit(1)
