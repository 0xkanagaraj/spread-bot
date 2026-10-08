from client import HyperliquidClient

hl = HyperliquidClient(use_testnet=True)

print(f"Spot USDC: {hl.get_spot_balance('USDC')}")
print(f"Perp: {hl.get_perp_balance()}")

mid = hl.get_mid_price("BTC")
print(f"BTC mid: {mid}")

safe_price = hl.round_price("BTC", mid * 0.80)
print(f"Safe price: {safe_price}")

size = hl.round_size("BTC", 10 / safe_price)
print(f"Size: {size}")

order_result = hl.place_limit_order("BTC", True, size, safe_price)
print(f"Order result: {order_result}")

if order_result.get("status") == "ok":
    status = order_result["response"]["data"]["statuses"][0]
    if "resting" in status:
        oid = status["resting"]["oid"]
        print(f"Order ID: {oid}")

        cancel_result = hl.cancel_order("BTC", oid)
        print(f"Cancel result: {cancel_result}")

        if cancel_result.get("status") == "ok":
            print("\n✅ Place/cancel lifecycle verified")
    else:
        print(f"Order did not rest: {status}")