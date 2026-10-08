import os
from dotenv import load_dotenv
import asyncio
import json
import websockets

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()

class HyperliquidClient:
    def __init__(self, use_testnet=True):
        self.main_private_key = os.getenv("MAIN_PRIVATE_KEY")
        self.main_address = os.getenv("MAIN_ADDRESS")
        self.agent_private_key = os.getenv("AGENT_PRIVATE_KEY")
        self.agent_address = os.getenv("AGENT_ADDRESS")

        if not all([self.main_private_key, self.main_address,
                    self.agent_private_key, self.agent_address]):
            raise ValueError("Missing required environment variables")

        self.base_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL

        self.main_account = eth_account.Account.from_key(self.main_private_key)
        self.agent_account = eth_account.Account.from_key(self.agent_private_key)

        self.info = Info(self.base_url, skip_ws=True)
        self.main_exchange = Exchange(self.main_account, self.base_url)
        self.agent_exchange = Exchange(
            self.agent_account,
            self.base_url,
            account_address=self.main_address
        )

    def get_spot_balance(self, coin="USDC"):
        state = self.info.spot_user_state(self.main_address)
        for balance in state.get("balances", []):
            if balance["coin"] == coin:
                return float(balance["total"])
        return 0.0

    def get_all_spot_balances(self):
        state = self.info.spot_user_state(self.main_address)
        return {
            b["coin"]: {
                "total": float(b["total"]),
                "hold": float(b["hold"]),
                "entryNtl": float(b["entryNtl"])
            }
            for b in state.get("balances", [])
        }

    def get_perp_balance(self):
        state = self.info.user_state(self.main_address)
        return float(state["marginSummary"]["accountValue"])

    def get_perp_state(self):
        return self.info.user_state(self.main_address)

    def get_positions(self):
        state = self.info.user_state(self.main_address)
        positions = []
        for pos in state.get("assetPositions", []):
            p = pos["position"]
            positions.append({
                "coin": p["coin"],
                "size": float(p["szi"]),
                "entry_price": float(p["entryPx"]),
                "unrealized_pnl": float(p["unrealizedPnl"]),
                "leverage": p["leverage"],
                "liquidation_price": float(p.get("liquidationPx") or 0)
            })
        return positions

    # def transfer_spot_to_perp(self, amount):
    #     return self.main_exchange.usd_class_transfer(amount, to_perp=True)

    def transfer_spot_to_perp(self, amount):
        try:
            result = self.main_exchange.usd_class_transfer(amount, to_perp=True)
            print(f"Transfer raw result: {result}")
            return result
        except Exception as e:
            print(f"Transfer exception: {type(e).__name__}: {e}")
            raise

    def transfer_perp_to_spot(self, amount):
        return self.main_exchange.usd_class_transfer(amount, to_perp=False)

    def get_mid_price(self, coin):
        book = self.info.l2_snapshot(coin)
        best_bid = float(book["levels"][0][0]["px"])
        best_ask = float(book["levels"][1][0]["px"])
        return (best_bid + best_ask) / 2

    def get_orderbook(self, coin, depth=5):
        book = self.info.l2_snapshot(coin)
        return {
            "bids": [{"px": float(l["px"]), "sz": float(l["sz"])} for l in book["levels"][0][:depth]],
            "asks": [{"px": float(l["px"]), "sz": float(l["sz"])} for l in book["levels"][1][:depth]]
        }

    def get_asset_index(self, coin):
        meta = self.info.meta()
        for i, asset in enumerate(meta["universe"]):
            if asset["name"] == coin:
                return i
        raise ValueError(f"Asset {coin} not found")

    def get_sz_decimals(self, coin):
        meta = self.info.meta()
        for asset in meta["universe"]:
            if asset["name"] == coin:
                return asset["szDecimals"]
        raise ValueError(f"Asset {coin} not found")

    def round_size(self, coin, size):
        decimals = self.get_sz_decimals(coin)
        return round(size, decimals)

    def place_limit_order(self, coin, is_buy, size, price, tif="Alo", reduce_only=False):
        size = self.round_size(coin, size)
        price = self.round_price(coin, price)
        return self.agent_exchange.order(
            coin,
            is_buy,
            size,
            price,
            {"limit": {"tif": tif}},
            reduce_only=reduce_only
        )

    def place_market_order(self, coin, is_buy, size, reduce_only=False):
        size = self.round_size(coin, size)
        return self.agent_exchange.market_open(coin, is_buy, size) if not reduce_only \
            else self.agent_exchange.market_close(coin, size)

    def cancel_order(self, coin, oid):
        return self.agent_exchange.cancel(coin, oid)

    def cancel_all_orders(self, coin=None):
        open_orders = self.get_open_orders()
        if coin:
            open_orders = [o for o in open_orders if o["coin"] == coin]
        results = []
        for order in open_orders:
            results.append(self.cancel_order(order["coin"], order["oid"]))
        return results

    def get_open_orders(self, coin=None):
        if coin:
            return self.info.open_orders(self.main_address, coin)
        return self.info.open_orders(self.main_address)

    def modify_order(self, coin, oid, is_buy, size, price, tif="Gtc", reduce_only=False):
        size = self.round_size(coin, size)
        return self.agent_exchange.modify_order(
            oid,
            coin,
            is_buy,
            size,
            price,
            {"limit": {"tif": tif}},
            reduce_only=reduce_only
        )

    def set_leverage(self, coin, leverage, is_cross=True):
        return self.agent_exchange.update_leverage(leverage, coin, is_cross)

    def get_user_fills(self):
        return self.info.user_fills(self.main_address)

    def get_funding_history(self, start_time, end_time=None):
        return self.info.funding_history(self.main_address, start_time, end_time)

    def close_position(self, coin):
        positions = self.get_positions()
        for pos in positions:
            if pos["coin"] == coin and pos["size"] != 0:
                return self.agent_exchange.market_close(coin, abs(pos["size"]))
        return None

    def close_all_positions(self):
        results = []
        for pos in self.get_positions():
            if pos["size"] != 0:
                results.append(self.agent_exchange.market_close(pos["coin"], abs(pos["size"])))
        return results

    def get_tick_size(self, coin):
        sz_decimals = self.get_sz_decimals(coin)
        if sz_decimals >= 2:
            return 10 ** -(sz_decimals - 1)
        return 1.0

    def round_price(self, coin, price):
        sz_decimals = self.get_sz_decimals(coin)
        max_decimals = 6 - sz_decimals
        rounded = round(price, max_decimals)
        return rounded

    async def stream_orderbook(self, coin="BTC", callback=None):
        """Subscribe to L2 order book updates via WebSocket."""
        ws_url = "wss://api.hyperliquid-testnet.xyz/ws"
        
        async with websockets.connect(ws_url) as ws:
            subscribe_msg = {
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": coin}
            }
            await ws.send(json.dumps(subscribe_msg))
            
            async for raw_msg in ws:
                msg = json.loads(raw_msg)
                if msg.get("channel") == "l2Book":
                    if callback:
                        callback(msg["data"])
                    else:
                        yield msg["data"]

if __name__=="__main__":
    client = HyperliquidClient()
    print("Perp:", client.get_perp_balance())
    print("Spot:", client.get_spot_balance())
    client.transfer_spot_to_perp(10)
    print("Perp:", client.get_perp_balance())
    print("Spot:", client.get_spot_balance())