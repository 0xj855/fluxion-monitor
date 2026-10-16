"""邮件通知（Resend HTTP API）：critical 告警即时邮件（按 key 去重）+ 每日日报。

配置全部走环境变量（不进仓库）：
  FLUXMON_RESEND_API_KEY / FLUXMON_EMAIL_TO（逗号分隔）
  FLUXMON_EMAIL_FROM（可选，默认 onboarding@resend.dev；未验证域名时只能发到 Resend 注册邮箱）
  FLUXMON_DASHBOARD_URL（可选，邮件里附看板链接）

去重：kv 表 email_critical 记录每个告警 key 的 {sent, seen}。同一 key 只在「新一轮」触发时发邮件——
即从未发过，或上次出现距今已超过 critical_quiet_s（中间消失够久才算新一轮）。持续存在或短时间内反复
恢复/触发的告警都不会重复发；恢复不发邮件，统一在日报里体现。
"""
import html
import json
import os
import time
import urllib.error
import urllib.request

SUBJECT = "Mantle DEX 流动性监控"


def configured():
    return bool(os.environ.get("FLUXMON_RESEND_API_KEY") and os.environ.get("FLUXMON_EMAIL_TO"))


def send(subject, html_body, text_body):
    env = os.environ
    body = {"from": env.get("FLUXMON_EMAIL_FROM") or "Fluxion Monitor <onboarding@resend.dev>",
            "to": [x.strip() for x in env["FLUXMON_EMAIL_TO"].split(",") if x.strip()],
            "subject": subject, "html": html_body, "text": text_body}
    req = urllib.request.Request("https://api.resend.com/emails", json.dumps(body).encode(), {
        "authorization": f"Bearer {env['FLUXMON_RESEND_API_KEY']}", "content-type": "application/json",
        "user-agent": "fluxmon"})   # 默认 Python-urllib UA 会被 Resend 的 Cloudflare 拦截
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"resend {e.code}: {e.read().decode(errors='replace')[:300]}") from None


def _dashboard():
    url = os.environ.get("FLUXMON_DASHBOARD_URL")
    return f'<p><a href="{html.escape(url)}">打开看板</a></p>' if url else ""


def _page(title, body):
    return (f'<html><body style="font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;color:#222">'
            f'<h2 style="margin:0 0 12px">{html.escape(title)}</h2>{body}{_dashboard()}</body></html>')


def _table(head, rows):
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:1px solid #ccc">{html.escape(h)}</th>' for h in head)
    tr = "".join("<tr>" + "".join(f'<td style="padding:4px 8px;border-bottom:1px solid #eee">{html.escape(str(c))}</td>' for c in r) + "</tr>"
                 for r in rows)
    return f'<table style="border-collapse:collapse;margin:8px 0 16px">{"<tr>" + th + "</tr>"}{tr}</table>'


def critical_alerts(store, fired, now, cfg):
    """fired: {key: (sev, msg, code, params)}。有新一轮 critical 告警时合并成一封邮件发送。"""
    quiet = cfg.get("critical_quiet_s", 86400)
    state = store.get("email_critical", {})
    new = [(k, msg) for k, (sev, msg, _, _) in fired.items()
           if sev == "critical" and (k not in state or now - state[k]["seen"] > quiet)]
    if new:
        ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
        subject = f"[CRITICAL] {SUBJECT}：{len(new)} 条新告警"
        body = f"<p>{ts}</p><ul>" + "".join(f"<li>{html.escape(m)}</li>" for _, m in new) + "</ul>"
        try:
            send(subject, _page(subject, body), f"{ts}\n" + "\n".join(f"- {m}" for _, m in new))
            print(f"critical email sent: {len(new)}")
        except Exception as e:
            print("critical email failed:", e)
            new = []   # 发送失败不记为已发，下轮重试
    for k, _ in new:
        state[k] = {"sent": now, "seen": now}
    for k, (sev, *_rest) in fired.items():
        if sev == "critical" and k in state:
            state[k]["seen"] = now
    store.put("email_critical", {k: v for k, v in state.items() if now - v["seen"] <= 30 * 86400})


def daily_due(store, now, cfg):
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    return store.get("email_daily_last") != day and time.gmtime(now).tm_hour >= cfg.get("daily_hour_utc", 1)


def daily_report(store, rep, now, cfg):
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    fmt = lambda v, f="${:,.0f}": "-" if v is None else f.format(v)
    sev_rank = {"critical": 0, "warn": 1, "info": 2}

    active = sorted(rep["alerts"], key=lambda a: sev_rank.get(a["severity"], 9))
    n = {s: sum(1 for a in active if a["severity"] == s) for s in sev_rank}
    recent = store.alerts_since(now - 86400)
    resolved = [r for r in recent if not r["active"]]
    crit_24h = [r for r in recent if r["severity"] == "critical"]

    parts = [f"<p>{day}，区块 {rep['block']}。当前告警：critical {n['critical']} / warn {n['warn']} / info {n['info']}；"
             f"过去 24h 出现 critical {len(crit_24h)} 条，已恢复 {len(resolved)} 条。</p>"]
    text = [f"{day} 当前告警 critical {n['critical']} / warn {n['warn']} / info {n['info']}；24h critical {len(crit_24h)}，已恢复 {len(resolved)}"]
    if active:
        parts.append("<h3>当前告警</h3>" + _table(["级别", "内容"], [(a["severity"], a["message"]) for a in active]))
        text += [f"[{a['severity']}] {a['message']}" for a in active]
    if resolved:
        parts.append("<h3>过去 24h 已恢复</h3>" + _table(["级别", "内容"], [(r["severity"], r["message"]) for r in resolved]))

    pools = sorted(rep["pools"], key=lambda r: -(r.get("tvl_usd") or 0))[:cfg.get("daily_max_pools", 20)]
    parts.append("<h3>池子（按 TVL）</h3>" + _table(
        ["DEX", "池子", "TVL", "±2% 上", "±2% 下", "24h 成交", "偏离参考价"],
        [(r.get("dex_name", ""), f"{r['pair']} ({r['fee_pct']}%)", fmt(r.get("tvl_usd")),
          fmt((r["depth"].get("2") or {}).get("up")), fmt((r["depth"].get("2") or {}).get("down")),
          fmt((r.get("events_24h") or {}).get("swap_usd", 0)), fmt(r.get("price_dev_pct"), "{:+.2f}%")) for r in pools]))

    rfq = rep.get("rfq")
    if rfq:
        sm = rfq["summary"]
        parts.append(f"<h3>Fluxion RFQ</h3><p>在线 {sm['active']}/{sm['total']}，SLA 达标 {sm.get('sla_pass_count', 0)}/{sm['total']}，"
                     f"平均买卖价差 {fmt(sm.get('avg_spread_bps'), '{:.1f} bps')}</p>")
    fl = rep.get("flows")
    if fl:
        t, w = fl["totals"], fl["windows"].get("24h") or {}
        parts.append(f"<h3>xStocks 流通与 Bybit 充提</h3><p>流通 {fmt(t['circulating_usd'])}（交易所 {fmt(t['exchange_usd'])}）；"
                     f"24h 提现 {fmt(w.get('wd_usd'))} / 充值 {fmt(w.get('dp_usd'))} / 净流入链上 {fmt(w.get('net_usd'), '${:+,.0f}')}</p>")

    subject = f"{SUBJECT} 日报 {day}" + (f"（{n['critical']} 条 critical）" if n["critical"] else "")
    try:
        send(subject, _page(subject, "".join(parts)), "\n".join(text))
        store.put("email_daily_last", day)
        print("daily email sent")
    except Exception as e:
        print("daily email failed:", e)
