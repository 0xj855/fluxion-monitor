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

SUBJECT = "Mantle 流动性监控"


def configured():
    return bool(os.environ.get("FLUXMON_RESEND_API_KEY") and os.environ.get("FLUXMON_EMAIL_TO"))


def send(subject, html_body, text_body):
    env = os.environ
    body = {"from": env.get("FLUXMON_EMAIL_FROM") or "Mantle Liquidity Monitor <onboarding@resend.dev>",
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
    sent = False
    if new:
        ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
        subject = f"[CRITICAL] {SUBJECT}：{len(new)} 条新告警"
        body = f"<p>{ts}</p><ul>" + "".join(f"<li>{html.escape(m)}</li>" for _, m in new) + "</ul>"
        try:
            link = f"\n看板：{os.environ['FLUXMON_DASHBOARD_URL']}" if os.environ.get("FLUXMON_DASHBOARD_URL") else ""
            send(subject, _page(subject, body), f"{ts}\n" + "\n".join(f"- {m}" for _, m in new) + link)
            sent = True
            print(f"critical email sent: {len(new)}")
        except Exception as e:
            print("critical email failed:", e)
    pending = {k for k, _ in new}
    for k, (sev, *_rest) in fired.items():
        if sev != "critical":
            continue
        if k in pending:
            if sent:
                state[k] = {"sent": now, "seen": now}
            # 发送失败：保持原状态（含旧的 seen），下轮仍算新一轮重试
        elif k in state:
            state[k]["seen"] = now
    store.put("email_critical", {k: v for k, v in state.items() if now - v["seen"] <= 30 * 86400})


def daily_due(store, now, cfg):
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    return store.get("email_daily_last") != day and time.gmtime(now).tm_hour >= cfg.get("daily_hour_utc", 1)


def daily_report(store, rep, now, cfg):
    """日报只含 critical：当前仍在的 + 过去 24h 已恢复的；其余指标看看板。"""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    active = [a for a in rep["alerts"] if a["severity"] == "critical"]
    resolved = [r for r in store.alerts_since(now - 86400) if not r["active"] and r["severity"] == "critical"]

    parts = [f"<p>{day}：当前 critical {len(active)} 条，过去 24h 已恢复 {len(resolved)} 条。</p>"]
    text = [f"{day} 当前 critical {len(active)} 条，过去 24h 已恢复 {len(resolved)} 条"]
    if active:
        parts.append("<h3>当前 critical</h3><ul>" + "".join(f"<li>{html.escape(a['message'])}</li>" for a in active) + "</ul>")
        text += [f"- {a['message']}" for a in active]
    if resolved:
        parts.append("<h3>过去 24h 已恢复</h3><ul>" + "".join(f"<li>{html.escape(r['message'])}</li>" for r in resolved) + "</ul>")
        text += [f"- [已恢复] {r['message']}" for r in resolved]
    if os.environ.get("FLUXMON_DASHBOARD_URL"):
        text.append(f"看板：{os.environ['FLUXMON_DASHBOARD_URL']}")

    subject = f"{SUBJECT} 日报 {day}" + (f"（{len(active)} 条 critical）" if active else "（无 critical）")
    try:
        send(subject, _page(subject, "".join(parts)), "\n".join(text))
        store.put("email_daily_last", day)
        print("daily email sent")
    except Exception as e:
        print("daily email failed:", e)


def monitor_failed(store, now, detail, cooldown_s=6 * 3600):
    """监控本身运行失败（workflow 失败步骤调用）。同一问题 6 小时内只发一封，恢复后重新计时。"""
    last = store.get("email_failure_last", 0)
    if now - last < cooldown_s:
        print("failure email suppressed (cooldown)")
        return
    subject = f"[FAILED] {SUBJECT} 运行失败"
    try:
        send(subject, _page(subject, f"<p>{html.escape(detail)}</p>"), detail)
        store.put("email_failure_last", now)
        print("failure email sent")
    except Exception as e:
        print("failure email failed:", e)


if __name__ == "__main__":   # python -m fluxmon.mailer failed "<详情>"
    import sys
    from pathlib import Path
    from .store import Store
    if len(sys.argv) >= 2 and sys.argv[1] == "failed" and configured():
        monitor_failed(Store(Path(__file__).resolve().parent.parent / "data" / "fluxmon.db"), int(time.time()),
                       sys.argv[2] if len(sys.argv) > 2 else "monitor run failed")
