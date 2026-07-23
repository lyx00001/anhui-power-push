#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安徽 D+1 日前价差预测 — 钉钉推送
===================================
通过 Grafana 公网 API 查询 ah_price_diff_forecast 表，
获取明日（D+1）四个预测指标，格式化后推送到钉钉群。

特性：
- 只推送 24 时刻完整分时明细（无日均/极值统计）
- 支持 LAST_PUSH_DATE 防重：同一天只推送一次
- 数据不足 24 条时静默退出，等待下次轮询

运行环境：GitHub Actions (Python 3.x)
轮询策略：每 30 分钟检查一次（09:00~14:00），数据满 24 条即推送
"""

import json
import os
import sys
import requests
from datetime import datetime, timedelta, timezone

# ============================================================
# 配置
# ============================================================
GRAFANA_BASE = "https://data-view.rundopower.com"
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")
DATASOURCE_UID = "dfg5l3aa9i60wa"

DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")

CST = timezone(timedelta(hours=8))
MIN_RECORDS = 24  # 必须满 24 条才推送

FIELD_LABELS = {
    "ah_price_diff_forecast.xishu": ("推荐系数", ".2f"),
    "ah_price_diff_forecast.pricediff": ("预测价差", ".1f"),
    "ah_price_diff_forecast.confidence": ("置信度", ".1f"),
    "ah_price_diff_forecast.accuracy_hour": ("分时准确率", ".3f"),
}


# ============================================================
# 数据提取
# ============================================================


def grafana_query(query_str: str) -> dict:
    """通过 Grafana 代理 API 查询 InfluxDB"""
    url = f"{GRAFANA_BASE}/api/ds/query"
    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "influxdb", "uid": DATASOURCE_UID},
                "query": query_str,
                "rawQuery": True,
            }
        ]
    }
    resp = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {GRAFANA_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_multi_frame(result: dict) -> dict:
    """
    解析 Grafana 多帧响应（每 SELECT 字段一个帧）。
    返回: { timestamp_ms: {"xishu": ..., "pricediff": ..., ...} }
    """
    data_by_ts = {}
    try:
        frames = result["results"]["A"]["frames"]
    except (KeyError, IndexError):
        print("[ERROR] 响应中无 frames")
        return data_by_ts

    for frame in frames:
        schema = frame["schema"]
        fields = schema["fields"]
        values = frame["data"]["values"]

        if not values:
            continue

        time_col = values[0]
        val_col = values[1] if len(values) > 1 else []

        display_name = ""
        for f in fields:
            cfg = f.get("config", {})
            dn = cfg.get("displayNameFromDS", "")
            if dn:
                display_name = dn
                break

        short_name = display_name.split(".")[-1] if "." in display_name else display_name

        for i, ts in enumerate(time_col):
            if ts not in data_by_ts:
                data_by_ts[ts] = {}
            val = val_col[i] if i < len(val_col) else None
            data_by_ts[ts][short_name] = val

    return data_by_ts


# ============================================================
# 钉钉推送
# ============================================================


def build_dingtalk_markdown(title: str, text: str) -> dict:
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }


def send_dingtalk(payload: dict, max_retries=3):
    for i in range(max_retries):
        resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=15)
        result = resp.json()
        if result.get("errcode") == 0:
            print(f"[钉钉] 发送成功: {payload['msgtype']}")
            return True
        err = result.get("errmsg", resp.text)
        print(f"[钉钉] 发送失败 (尝试 {i+1}/{max_retries}): {err}")
    return False


# ============================================================
# 格式化工具
# ============================================================


def fm(val, fmt_str):
    if val is None:
        return "-"
    return format(val, fmt_str)


def safe_mean(vals):
    if not vals:
        return None
    return sum(vals) / len(vals)


# ============================================================
# 主逻辑
# ============================================================


def main():
    now = datetime.now(CST)
    today_str = now.strftime("%Y-%m-%d")
    print(f"[INFO] 安徽 D+1 价差预测推送 — {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 防重检查：同一天只推送一次
    last_push_date = os.environ.get("LAST_PUSH_DATE", "").strip()
    if last_push_date == today_str:
        print(f"[SKIP] 今天 ({today_str}) 已推送过，跳过")
        sys.exit(0)  # exit 0 = 正常跳过，允许"今天已推送"场景（非错误）

    # 2. 确定目标日期 (D+1 = 明天)
    #    数据库时间映射: 北京时间 D+1 日 00:00 = UTC D 日 16:00
    #    查询范围: D 日 16:00Z ~ D+1 日 16:00Z，覆盖 D+1 全天 24 小时预测
    tomorrow = (now + timedelta(days=1)).date()
    date_str = tomorrow.strftime("%Y-%m-%d")

    # 北京时间 D+1 日 00:00 → UTC = D+1 日 00:00 CST → 减去 8 小时 → D 日 16:00 UTC
    query_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=CST)
    query_start_utc = query_start - timedelta(hours=8)
    query_end_utc = query_start_utc + timedelta(days=1)

    start_str = query_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = query_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[INFO] 目标日期 D+1: {date_str}")
    print(f"[INFO] 查询范围 UTC: {start_str} ~ {end_str}")

    # 3. 查询数据
    query = (
        f"SELECT xishu, pricediff, confidence, accuracy_hour "
        f"FROM ah_price_diff_forecast "
        f"WHERE type::tag='lgb' "
        f"AND time >= '{start_str}' "
        f"AND time < '{end_str}'"
    )

    print(f"[INFO] 查询中...")
    try:
        result = grafana_query(query)
        data_by_ts = parse_multi_frame(result)
    except requests.RequestException as e:
        print(f"[ERROR] Grafana API 失败: {e}")
        sys.exit(1)

    # 4. 无数据时回退到 D 日（同样的时间映射逻辑）
    if not data_by_ts:
        print(f"[WARN] {date_str} 无 D+1 数据，尝试回退到 D 日...")
        today = now.date()
        date_str = today.strftime("%Y-%m-%d")
        qs = datetime(today.year, today.month, today.day, tzinfo=CST).astimezone(timezone.utc)
        qe = qs + timedelta(days=1)
        qs_str = qs.strftime("%Y-%m-%dT%H:%M:%SZ")
        qe_str = qe.strftime("%Y-%m-%dT%H:%M:%SZ")
        query = (
            f"SELECT xishu, pricediff, confidence, accuracy_hour "
            f"FROM ah_price_diff_forecast "
            f"WHERE type::tag='lgb' "
            f"AND time >= '{qs_str}' "
            f"AND time < '{qe_str}'"
        )
        try:
            result = grafana_query(query)
            data_by_ts = parse_multi_frame(result)
        except Exception as e:
            print(f"[ERROR] 回退查询也失败: {e}")

    if not data_by_ts:
        print(f"[SKIP] {date_str} 无可用数据，等待下次轮询")
        sys.exit(2)  # exit 2 = 未推送，阻止 GitHub Actions 误存缓存

    # 5. 按时间排序
    sorted_ts = sorted(data_by_ts.keys())
    row_count = len(sorted_ts)
    print(f"[INFO] 获取到 {row_count} 条记录")

    # 6. 数据完整性检查：不满 24 条不推送
    if row_count < MIN_RECORDS:
        print(f"[SKIP] 数据不完整 ({row_count}/{MIN_RECORDS})，等待下次轮询")
        sys.exit(2)  # exit 2 = 未推送，阻止 GitHub Actions 误存缓存

    # 7. 趋势判断（用于标题提示）
    pricediff_list = [data_by_ts[ts].get("pricediff") for ts in sorted_ts if data_by_ts[ts].get("pricediff") is not None]
    avg_pricediff = safe_mean(pricediff_list)
    if avg_pricediff is not None:
        trend_emoji = "🟢" if avg_pricediff > 0 else "🔴"
        trend_text = "日前 > 实时" if avg_pricediff > 0 else "实时 > 日前"
    else:
        trend_emoji = "⚪"
        trend_text = "无数据"

    # 8. 构建 24 时刻分时明细表
    detail_rows = []
    for ts in sorted_ts:
        d = data_by_ts[ts]
        t = datetime.fromtimestamp(ts / 1000, tz=CST)
        hour_str = t.strftime("%H:%M")
        detail_rows.append(
            f"| {hour_str} "
            f"| {fm(d.get('xishu'), '.2f')} "
            f"| {fm(d.get('pricediff'), '.1f')} "
            f"| {fm(d.get('confidence'), '.1f')}% "
            f"| {fm(d.get('accuracy_hour'), '.3f')} |"
        )

    # 9. 构建钉钉 Markdown（纯分时明细，无日均/极值）
    md_text = f"""**安徽** | 价差预测日报

## ⚡ D+1 日前价差预测

**📅 {date_str}** | 数据粒度：1小时 | 模型：LGB | {trend_emoji} {trend_text}

---

### 📋 分时明细（{row_count}/24 小时）

| 时刻 | 推荐系数 | 预测价差 | 置信度 | 准确率 |
|---|---|---|---|---|
{chr(10).join(detail_rows)}

---

> ⚙️ 自动推送 · 润建电子科技 | `{now.strftime('%Y-%m-%d %H:%M')}`"""

    # 10. 发送
    print(f"[INFO] 消息长度: {len(md_text)} 字符")
    success = send_dingtalk(
        build_dingtalk_markdown(f"安徽价差预测 {date_str}", md_text)
    )

    if success:
        print(f"[SUCCESS] 推送完成 — {date_str} ({row_count} 条)")
        # 输出今天日期，供 GitHub Actions cache 记录
        print(f"::set-output name=push_date::{today_str}")
    else:
        print("[FAIL] 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
