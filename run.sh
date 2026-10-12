#!/usr/bin/env bash
# 定时任务入口：cd 到项目目录后运行一次监控。飞书推送需设置 FLUXMON_LARK_WEBHOOK。
cd "$(dirname "$0")" && exec python3 -m fluxmon.main "$@"
