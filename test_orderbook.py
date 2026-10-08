import asyncio
from client import HyperliquidClient

async def print_book():
    hl = HyperliquidClient(use_testnet=True)
    async for book in hl.stream_orderbook("BTC"):
        best_bid = float(book["levels"][0][0]["px"])
        best_ask = float(book["levels"][1][0]["px"])
        print(f"Bid: {best_bid} | Ask: {best_ask} | Spread: {best_ask - best_bid}")
        break  # just see one message

asyncio.run(print_book())