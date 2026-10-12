"""外部参考数据：xStocks 资产目录、ETH 中心化交易所价格、xStocks RFQ / K 线参考价。"""
import json
import time
import urllib.error
import urllib.request

from .chain import multicall

UA = {"user-agent": "fluxmon/0.1", "accept": "application/json"}
XSTOCKS_API = "https://api.backed.fi/api/v1/token"
RFQ_URL = "https://fluxion-proxy-api-production.up.railway.app/quote"   # 限速 60 次/分钟
KLINE_URL = "https://fluxion.network/api/xstocks/kline"
USPX_KLINE_URL = "https://fluxion.network/api/xstocks/uspx-kline"


def http(url, body=None, timeout=20):
    """返回 (status, 解析后的 JSON 或 None)。"""
    headers = dict(UA)
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=timeout)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def xstocks_catalog():
    """Mantle 上的 xStocks：{address(lower): {symbol, native, kind: native|wrapper}}"""
    status, payload = http(XSTOCKS_API, timeout=30)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"xStocks catalog unavailable: HTTP {status}")
    out = {}
    for a in payload.get("nodes", []):
        sym = a.get("symbol") or ""
        if not sym.lower().endswith("x"):
            continue
        for d in a.get("deployments") or []:
            if d.get("network") != "Mantle" or not d.get("address"):
                continue
            native = d["address"].lower()
            out[native] = {"symbol": sym, "native": native, "kind": "native"}
            for k in ("wrapperAddress", "wrapperAddressV2"):
                if d.get(k):
                    out[d[k].lower()] = {"symbol": sym, "native": native, "kind": "wrapper"}
    return out


def eth_usd():
    """ETH 参考价：Binance 与 Coinbase 的中位/均值；全部失败返回 None。"""
    px = []
    s, j = http("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT")
    if s == 200 and j:
        px.append(float(j["price"]))
    s, j = http("https://api.coinbase.com/v2/prices/ETH-USD/spot")
    if s == 200 and j:
        px.append(float(j["data"]["amount"]))
    return {"price": sum(px) / len(px), "sources": len(px)} if px else None


def wrapper_ratios(wrappers):
    """ERC-4626 包装币：1 share 对应多少 native xStock（convertToAssets(1e18)/1e18）。"""
    r = multicall([(w, "0x07a2d13a" + format(10 ** 18, "064x")) for w in wrappers])
    return {w: (int(h, 16) / 1e18 if h else None) for w, h in zip(wrappers, r)}


def xstock_reference(symbol, native, usdc, rfq_size_usdc=1000):
    """xStock 参考价：优先 RFQ rawPrice（可成交的市场参考价），否则用 Fluxion 1 分钟 K 线收盘价。"""
    ref = {"symbol": symbol, "rfq_http": None, "rfq_price": None, "kline_price": None, "kline_age_s": None}
    s, j = http(RFQ_URL, {"tokenIn": usdc, "tokenOut": native, "amount": str(rfq_size_usdc * 10 ** 6), "type": "EXACT_INPUT"})
    ref["rfq_http"] = s
    if s == 200 and j and j.get("rawPrice"):
        ref["rfq_price"] = float(j["rawPrice"])
    now = int(time.time() * 1000)
    code = symbol[:-1].replace(".", "").upper()
    url = (f"{USPX_KLINE_URL}?ticker={code}" if symbol == "USPXx" else f"{KLINE_URL}?symbol={code}USDT") \
        + f"&interval=1&from={now - 4 * 86400_000}&to={now}&limit=500"
    s, j = http(url)
    if s == 200 and isinstance(j, list) and j:
        last = max(j, key=lambda k: int(k["startTime"]))
        ref["kline_price"] = float(last["closePrice"])
        ref["kline_age_s"] = (now - int(last["startTime"])) / 1000
    ref["price"] = ref["rfq_price"] or ref["kline_price"]
    ref["source"] = "rfq" if ref["rfq_price"] else ("kline" if ref["kline_price"] else None)
    return ref
