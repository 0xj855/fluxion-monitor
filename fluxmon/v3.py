"""Uniswap V3 兼容池（Fluxion V3）的状态读取、深度与滑点计算。

所有价格/数量在这里都是原始单位（未除 decimals），sqrtP = sqrt(token1/token0)。
"""
import math

from .chain import addr_word, multicall, sint, word, words

MAX_TICK = 887272
SEL_SLOT0, SEL_LIQ, SEL_BITMAP, SEL_TICKS, SEL_BAL = "0x3850c7bd", "0x1a686502", "0x5339c296", "0xf30dba93", "0x70a08231"


def tick_sqrt(t):
    return 1.0001 ** (t / 2)


def read_pool(pool, block="latest"):
    """pool: {pool, token0, token1, fee, tickSpacing} -> 链上状态（含全部已初始化 tick 的 liquidityNet）"""
    pa, sp = pool["pool"], pool["tickSpacing"]
    wps = list(range(((-MAX_TICK // sp) >> 8) - 1, ((MAX_TICK // sp) >> 8) + 2))
    r = multicall([(pa, SEL_SLOT0), (pa, SEL_LIQ),
                   (pool["token0"], SEL_BAL + addr_word(pa)), (pool["token1"], SEL_BAL + addr_word(pa))]
                  + [(pa, SEL_BITMAP + word(w)) for w in wps], block)
    s = words(r[0])
    inited = []
    for wp, h in zip(wps, r[4:]):
        bits = int(h, 16)
        while bits:
            b = (bits & -bits).bit_length() - 1
            inited.append(((wp << 8) + b) * sp)
            bits &= bits - 1
    tr = multicall([(pa, SEL_TICKS + word(t)) for t in inited], block) if inited else []
    return {
        "sqrtP": int(s[0], 16) / 2 ** 96,
        "tick": sint(s[1]),
        "L": int(r[1], 16),
        "bal0": int(r[2], 16),
        "bal1": int(r[3], 16),
        "ticks": {t: sint(words(h)[1]) for t, h in zip(inited, tr)},
    }


def _segments(st, up):
    """从当前价格向上/向下依次产出 (下一个边界 sqrtP, 穿过后 L 的变化量)。"""
    sq = st["sqrtP"]
    ts = sorted(st["ticks"])
    if up:
        for t in ts:
            if tick_sqrt(t) > sq:
                yield tick_sqrt(t), st["ticks"][t]
        yield tick_sqrt(MAX_TICK), 0
    else:
        for t in reversed(ts):
            if tick_sqrt(t) <= sq:
                yield tick_sqrt(t), -st["ticks"][t]
        yield tick_sqrt(-MAX_TICK), 0


def band_reserves(st, factor):
    """价格（token1/token0）移动到 factor 倍过程中被吃掉的储备（原始单位）。

    factor>1 返回 token0 数量，factor<1 返回 token1 数量。即订单簿口径的 ±x% 深度。
    """
    target = st["sqrtP"] * math.sqrt(factor)
    sq, L, acc = st["sqrtP"], st["L"], 0.0
    up = factor > 1
    for nxt, dL in _segments(st, up):
        nxt = min(nxt, target) if up else max(nxt, target)
        acc += L * (1 / sq - 1 / nxt) if up else L * (sq - nxt)
        sq = nxt
        if sq == target:
            break
        L += dL
    return acc


def swap_exact_in(st, amount_in, zero_for_one, fee):
    """模拟 exact-input swap，返回 (amount_out, 成交后 sqrtP, 是否流动性不足)。"""
    rem = amount_in * (1 - fee)
    sq, L, out = st["sqrtP"], st["L"], 0.0
    for nxt, dL in _segments(st, not zero_for_one):
        if L > 0:
            if zero_for_one:   # token0 in，价格下跌
                cap = L * (1 / nxt - 1 / sq)
                if rem <= cap:
                    new = 1 / (1 / sq + rem / L)
                    return out + L * (sq - new), new, False
                out += L * (sq - nxt)
            else:              # token1 in，价格上涨
                cap = L * (nxt - sq)
                if rem <= cap:
                    new = sq + rem / L
                    return out + L * (1 / sq - 1 / new), new, False
                out += L * (1 / sq - 1 / nxt)
            rem -= cap
        sq = nxt
        L += dL
    return out, sq, True
