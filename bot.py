import asyncio
import json, time
import signal
import websockets
from client import HyperliquidClient

COIN = "SOL"
REQUOTE_THRESHOLD_PCT = 0.0005
MAX_POSITION_USD = 60.0
MAX_DRAWDOWN_PCT = 0.02
ORDER_SIZE_USD = 20

class SpreadBot:
    def __init__(self, hl):
        self.hl = hl
        self.active_oids = []
        self.last_quote_mid = None
        self.starting_balance = hl.get_spot_balance("USDC")
        self.running = True
        self.inventory = 0.0
        self.last_requote_time = 0
        self.min_requote_interval = 2.0
        self.total_fees = 0.0
        self.last_mid = None

    async def stream_book(self):
        ws_url = "wss://api.hyperliquid-testnet.xyz/ws"
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": COIN}
            }))
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "orderUpdates", "user": self.hl.main_address}
            }))
            while self.running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    msg = json.loads(raw)
                    if msg.get("channel") == "l2Book":
                        yield ("book", msg["data"])
                    elif msg.get("channel") == "orderUpdates":
                        yield ("orders", msg["data"])
                except asyncio.TimeoutError:
                    continue

    async def stream_book_with_reconnect(self):
        while self.running:
            try:
                async for book in self.stream_book():
                    yield book
            except websockets.exceptions.ConnectionClosed:
                print("WebSocket disconnected, reconnecting in 5s...")
                await asyncio.sleep(5)
            except (GeneratorExit, RuntimeError):
                return
            except Exception as e:
                print(f"Stream error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    def get_mid(self, book):
        best_bid = float(book["levels"][0][0]["px"])
        best_ask = float(book["levels"][1][0]["px"])
        return (best_bid + best_ask) / 2

    def check_drawdown(self):
        current = self.hl.get_spot_balance("USDC")
        if current < self.starting_balance * (1 - MAX_DRAWDOWN_PCT):
            print(f"🛑 DRAWDOWN HIT: {current} < {self.starting_balance}")
            return True
        return False


    def can_requote(self, mid):
        now = time.time()
        if now - self.last_requote_time < self.min_requote_interval:
            return False
        return (
            not self.active_oids
            or self.last_quote_mid is None
            or abs(mid - self.last_quote_mid) / self.last_quote_mid > REQUOTE_THRESHOLD_PCT
        )

    def cancel_all(self):
        if not self.active_oids:
            return
        for oid in self.active_oids:
            try:
                res = self.hl.cancel_order(COIN, oid)
                print(f"Cancelled {oid}: {res.get('status')}")
            except Exception as e:
                print(f"Cancel error {oid}: {e}")
        self.active_oids = []

    def place_quotes(self, mid, book):
        size = self.hl.round_size(COIN, ORDER_SIZE_USD / mid)
        if size == 0:
            print("Size rounds to 0, skipping quote")
            return

        best_bid = float(book["levels"][0][0]["px"])
        best_ask = float(book["levels"][1][0]["px"])
        tick = self.hl.get_tick_size(COIN)

        max_units = self.get_max_position_units(mid)
        skew_factor = self.inventory / max_units if max_units > 0 else 0
        skew_ticks = int(round(skew_factor * 5))

        buy_price = self.hl.round_price(COIN, best_bid - skew_ticks * tick)
        sell_price = self.hl.round_price(COIN, best_ask - skew_ticks * tick)

        if buy_price >= sell_price:
            print("Spread inverted, skipping quote")
            return

        buy_res = self.hl.place_limit_order(COIN, True, size, buy_price)
        sell_res = self.hl.place_limit_order(COIN, False, size, sell_price)

        for res, label in ((buy_res, "bid"), (sell_res, "ask")):
            if res.get("status") == "ok":
                s = res["response"]["data"]["statuses"][0]
                if "resting" in s:
                    self.active_oids.append(s["resting"]["oid"])
                else:
                    print(f"{label} rejected: {s}")

        print(f"Quoted: bid={buy_price} ask={sell_price} | Inv={self.inventory} skew={skew_ticks}")

    def handle_order_update(self, updates):
        for update in updates:
            order = update.get("order", {})
            status = update.get("status")
            oid = order.get("oid")

            if status == "filled":
                if oid in self.active_oids:
                    self.active_oids.remove(oid)
                self.sync_inventory()
                fills = self.hl.get_user_fills()
                if fills:
                    latest = fills[0]
                    fee = float(latest.get("fee", 0))
                    self.total_fees += fee
                    print(f"✅ FILL | Inv: {self.inventory} | Fee: {fee:.6f} | Total: {self.total_fees:.6f}")
            elif status == "canceled":
                if oid in self.active_oids:
                    self.active_oids.remove(oid)
            elif status == "open":
                if oid not in self.active_oids:
                    self.active_oids.append(oid)

    def reconcile_on_startup(self):
        positions = self.hl.get_positions()
        for pos in positions:
            if pos["coin"] == COIN and pos["size"] != 0:
                print(f"⚠️ ORPHAN: {pos['size']} {COIN} @ {pos['entry_price']}")
                self.hl.close_position(COIN)
                self.inventory = 0.0
                print("   Closed orphan")
                return
        self.inventory = 0.0
        print("✓ Startup reconciliation complete")

    def sync_inventory(self):
        positions = self.hl.get_positions()
        for pos in positions:
            if pos["coin"] == COIN:
                self.inventory = pos["size"]
                return
        self.inventory = 0.0

    def check_inventory_limit(self):
        if self.last_mid is None:
            return False
        max_units = self.get_max_position_units(self.last_mid)
        if abs(self.inventory) > max_units:
            print(f"⚠️ Inventory limit: {self.inventory} > {max_units:.4f}")
            self.hl.close_position(COIN)
            self.sync_inventory()
            return True
        return False

    def get_max_position_units(self, mid):
        if mid <= 0:
            return 0
        return MAX_POSITION_USD / mid

    async def run(self):
        self.reconcile_on_startup()
        async for kind, data in self.stream_book_with_reconnect():
            if not self.running:
                break

            if kind == "orders":
                self.handle_order_update(data)
                continue

            mid = self.get_mid(data)
            self.last_mid = mid

            if self.check_drawdown() or self.check_inventory_limit():
                self.cancel_all()
                self.running = False
                break

            if self.can_requote(mid):
                self.cancel_all()
                self.place_quotes(mid, data)
                self.last_quote_mid = mid
                self.last_requote_time = time.time()

        self.cancel_all()

        if abs(self.inventory) > 0:
            print(f"Closing residual position: {self.inventory}")
            self.hl.close_position(COIN)
            self.sync_inventory()

        ending = self.hl.get_spot_balance("USDC")
        net = ending - self.starting_balance
        print(f"Starting: {self.starting_balance:.4f} | Ending: {ending:.4f} | Net: {net:+.4f}")
        print(f"Total fees paid: {self.total_fees:.6f}")
        print("Bot stopped, all orders cancelled")

def handle_signal(sig, frame):
    print("\nShutdown signal received")
    bot.running = False

bot = None
if __name__ == "__main__":
    hl = HyperliquidClient(use_testnet=True)
    print("SOL szDecimals:", hl.get_sz_decimals("SOL"))
    print("SOL tick size:", hl.get_tick_size("SOL"))
    print("SOL mid:", hl.get_mid_price("SOL"))
    bot = SpreadBot(hl)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass