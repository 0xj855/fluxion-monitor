"""Merchant Moe Liquidity Book（LB v2.1/2.2）：池子发现、状态读取、深度与滑点计算、LP 持仓。

接口与 v3.py 保持同样的语义（token0 = tokenX，token1 = tokenY，价格 = token1/token0 原始单位），
analyze.py 可以不区分池子类型直接调用 band_reserves / swap_exact_in。
"""
import math

from .chain import addr_word, get_logs, multicall, rpc, retry, word, words

SEL_N_PAIRS, SEL_PAIR_AT = "0x4e937c3a", "0x7daf5d66"
SEL_X, SEL_Y, SEL_STEP, SEL_FEE = "0x05e8746d", "0xda10610c", "0x17f11ecc", "0x7ca0de30"
SEL_RESERVES, SEL_ACTIVE, SEL_BIN, SEL_SUPPLY, SEL_BAL = "0x0902f1ac", "0xdbe65edc", "0x0abe9688", "0xbd85b039", "0x00fdd58e"
T_SWAP = "0xad7d6f97abf51ce18e17a38f4d70e975be9c0708474987bb3e26ad21bd93ca70"
T_DEPOSIT = "0x87f1f9dcf5e8089a3e00811b6a008d8f30293a3da878cb1fe8c90ca376402f8a"
T_WITHDRAW = "0xa32e146844d6144a22e94c586715a1317d58a8aa3581ec33d040113ddcb24350"
T_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
ID_ONE = 2 ** 23
BIN_RANGE = 200   # 读取 active ±200 个 bin（bin 步长 0.5% 时约覆盖 ±60%~+170%）


def unpack(h):
    """LB 的 bytes32 打包数量：低 128 位为 X，高 128 位为 Y。"""
    v = int(h, 16)
    return v & (2 ** 128 - 1), v >> 128


def bin_price(bin_id, step):
    return (1 + step / 1e4) ** (bin_id - ID_ONE)


def sync_pairs(store, dex):
    """增量枚举 LBFactory 的全部交易对（数量少，直接按索引读）。"""
    f = dex["factory"]
    done = store.get(f"lb_pairs_count:{dex['id']}", 0)
    n = int(multicall([(f, SEL_N_PAIRS)])[0], 16)
    if n <= done:
        return []
    addrs = ["0x" + h[-40:] for h in multicall([(f, SEL_PAIR_AT + word(i)) for i in range(done, n)])]
    r = multicall([(a, s) for s in (SEL_X, SEL_Y, SEL_STEP, SEL_FEE) for a in addrs])
    k = len(addrs)
    pools = []
    for i, a in enumerate(addrs):
        step = int(r[2 * k + i], 16)
        base_factor = int(words(r[3 * k + i])[0], 16) if r[3 * k + i] else 0
        pools.append({"pool": a, "token0": "0x" + r[i][-40:], "token1": "0x" + r[k + i][-40:],
                      "fee": round(base_factor * step * 1e10 / 1e18 * 1e6),   # 基础费率，单位 1e-6（与 v3 fee 同口径）
                      "tickSpacing": step, "block": 0, "dex": dex["id"], "kind": "lb"})
    store.add_pools(pools)
    store.put(f"lb_pairs_count:{dex['id']}", n)
    return pools


def read_pair(pool, block="latest"):
    pa, step = pool["pool"], pool["tickSpacing"]
    head = multicall([(pa, SEL_RESERVES), (pa, SEL_ACTIVE)], block)
    rw = words(head[0])
    active = int(head[1], 16)
    ids = list(range(active - BIN_RANGE, active + BIN_RANGE + 1))
    br = multicall([(pa, SEL_BIN + word(i)) for i in ids], block)
    bins = {}
    for i, h in zip(ids, br):
        if h:
            w = words(h)
            x, y = int(w[0], 16), int(w[1], 16)
            if x or y:
                bins[i] = (x, y)
    p = bin_price(active, step)
    return {"kind": "lb", "active": active, "step": step, "bins": bins, "tick": active,
            "bal0": int(rw[0], 16), "bal1": int(rw[1], 16), "sqrtP": math.sqrt(p),
            "L": 1 if (int(rw[0], 16) or int(rw[1], 16)) else 0, "ticks": bins}


def band_reserves(st, factor):
    """价格移动到 factor 倍途中被吃掉的储备：factor>1 返回 X，factor<1 返回 Y（原始单位）。"""
    p0 = bin_price(st["active"], st["step"])
    acc = 0.0
    for i, (x, y) in st["bins"].items():
        pr = bin_price(i, st["step"])
        if factor > 1 and i >= st["active"] and pr <= p0 * factor * (1 + 1e-12):
            acc += x
        elif factor < 1 and i <= st["active"] and pr >= p0 * factor * (1 - 1e-12):
            acc += y
    return acc


def swap_exact_in(st, amount_in, zero_for_one, fee):
    """逐 bin 模拟 exact-input 成交（只计基础费率，不含可变费率）。返回 (amount_out, None, 是否流动性不足)。"""
    rem, out = amount_in * (1 - fee), 0.0
    ids = sorted((i for i in st["bins"] if (i <= st["active"] if zero_for_one else i >= st["active"])), reverse=zero_for_one)
    for i in ids:
        x, y = st["bins"][i]
        pr = bin_price(i, st["step"])
        if zero_for_one:          # X in → Y out
            if rem * pr <= y:
                return out + rem * pr, None, False
            out += y
            rem -= y / pr
        else:                     # Y in → X out
            if rem / pr <= x:
                return out + rem / pr, None, False
            out += x
            rem -= x * pr
    return out, None, True


def holders(store, pool, st, head, created_hint):
    """LP 持仓：候选地址来自 DepositedToBins 的接收方与 LB token 的 TransferBatch 接收方（增量缓存），
    再按每个非空 bin 的 balanceOf / totalSupply 折算储备。返回 {owner: (x_raw, y_raw)}。"""
    pa = pool["pool"]
    key = f"lb_holders:{pa}"
    cache = store.get(key, {"block": created_hint, "cands": []})
    logs = get_logs(pa, [[T_DEPOSIT, T_TRANSFER_BATCH]], cache["block"] + 1, head)
    cands = set(cache["cands"])
    for l in logs:
        cands.add("0x" + (l["topics"][2] if l["topics"][0] == T_DEPOSIT else l["topics"][3])[-40:])
    cands.discard("0x" + "0" * 40)
    store.put(key, {"block": head, "cands": sorted(cands)})
    ids = sorted(st["bins"])
    if not ids or not cands:
        return {}
    supply = dict(zip(ids, (int(h, 16) if h else 0 for h in multicall([(pa, SEL_SUPPLY + word(i)) for i in ids]))))
    cl = sorted(cands)
    res = multicall([(pa, SEL_BAL + addr_word(a) + word(i)) for a in cl for i in ids])
    out, k = {}, 0
    for a in cl:
        x = y = 0.0
        for i in ids:
            bal = int(res[k], 16) if res[k] else 0
            k += 1
            if bal and supply[i]:
                f = bal / supply[i]
                x += st["bins"][i][0] * f
                y += st["bins"][i][1] * f
        if x or y:
            out[a] = (x, y)
    return out


def first_block(addr, head, lo=60_000_000):
    """二分查找合约部署区块（用于 LP 候选地址的首次回溯起点）。"""
    hi = head
    while lo < hi:
        m = (lo + hi) // 2
        if len(retry(lambda: rpc("eth_getCode", [addr, hex(m)]))) > 2:
            hi = m
        else:
            lo = m + 1
    return lo
