#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安徽现货价差日报 — GitHub Actions 离线推送版
==============================================
通过 Grafana 公网 API 查询数据（无需内网 InfluxDB），
生成 Markdown 摘要推送到钉钉群。

触发：每日 09:00 CST（GitHub Actions cron）
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


# ============================================================
# Grafana API 查询
# ============================================================
def grafana_query(query_str: str) -> dict:
    url = f"{GRAFANA_BASE}/api/ds/query"
    payload = {
        "queries": [{
            "refId": "A",
            "datasource": {"type": "influxdb", "uid": DATASOURCE_UID},
            "query": query_str,
            "rawQuery": True,
        }]
    }
    resp = requests.post(
        url, json=payload,
        headers={"Authorization": f"Bearer {GRAFANA_TOKEN}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_to_rows(result: dict) -> list:
    """解析 Grafana 响应为 [{"time": datetime, "field": value}, ...]"""
    rows = []
    try:
        frames = result["results"]["A"]["frames"]
    except (KeyError, IndexError):
        return rows

    for frame in frames:
        fields = frame["schema"]["fields"]
        values = frame["data"]["values"]
        if not values or len(values) < 2:
            continue

        times = values[0]
        vals = values[1]

        # 获取字段名（优先 displayNameFromDS，其次 frame name）
        field_name = ""
        frame_name = frame.get("schema", {}).get("name", "")
        for f in fields:
            dn = f.get("config", {}).get("displayNameFromDS", "")
            if dn:
                field_name = dn
                break
        if not field_name:
            field_name = frame_name
        # 取最后一段作为短名（如 ah_sccqjg.DaPrice → DaPrice）
        short_name = field_name.split(".")[-1] if "." in field_name else field_name

        for i, ts in enumerate(times):
            t = datetime.fromtimestamp(ts / 1000, tz=CST)
            val = vals[i] if i < len(vals) else None
            rows.append({"time": t, "field": short_name, "value": val})

    return rows


# ============================================================
# 数据查询
# ============================================================
def query_table(measurement, fields, target_date, hours_back=0):
    """查询单表数据，返回按小时聚合的 dict"""
    # 北京时间 target_date 00:00 → UTC target_date-1 16:00
    start_cst = datetime(target_date.year, target_date.month, target_date.day, tzinfo=CST)
    if hours_back:
        start_cst -= timedelta(hours=hours_back)
    end_cst = start_cst + timedelta(days=1) + timedelta(hours=hours_back * 2)

    start_utc = start_cst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_cst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    field_list = ", ".join(f'"{f}"' for f in fields)
    query = f'SELECT {field_list} FROM "{measurement}" WHERE time >= \'{start_utc}\' AND time < \'{end_utc}\''

    try:
        result = grafana_query(query)
        rows = parse_to_rows(result)
    except Exception as e:
        print(f"[WARN] {measurement} 查询失败: {e}")
        return {}

    # 按小时聚合
    hourly = {}
    for r in rows:
        hour = r["time"].replace(minute=0, second=0, microsecond=0)
        if hour not in hourly:
            hourly[hour] = {}
        if r["field"] not in hourly[hour]:
            hourly[hour][r["field"]] = []
        if r["value"] is not None:
            hourly[hour][r["field"]].append(r["value"])

    # 求均值
    result_dict = {}
    for hour, fields_data in hourly.items():
        result_dict[hour] = {}
        for f, vals in fields_data.items():
            if vals:
                result_dict[hour][f] = sum(vals) / len(vals)

    return result_dict


def safe_mean(vals):
    if not vals:
        return None
    valid = [v for v in vals if v is not None]
    return sum(valid) / len(valid) if valid else None


# ============================================================
# 钉钉推送
# ============================================================
def send_dingtalk(payload, max_retries=3):
    for i in range(max_retries):
        resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=15)
        result = resp.json()
        if result.get("errcode") == 0:
            print(f"[钉钉] 发送成功: {payload['msgtype']}")
            return True
        print(f"[钉钉] 失败 (尝试 {i+1}/{max_retries}): {result.get('errmsg', resp.text)}")
    return False


def fmt_val(v, precision=1):
    if v is None:
        return "-"
    return f"{v:.{precision}f}"


# ============================================================
# 主逻辑
# ============================================================
def main():
    now = datetime.now(CST)
    today_str = now.strftime("%Y-%m-%d")
    print(f"[INFO] 安徽现货价差日报 — {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # 防重：同一天只推送一次
    last_push_date = os.environ.get("LAST_REPORT_DATE", "").strip()
    if last_push_date == today_str:
        print(f"[SKIP] 今天 ({today_str}) 已推送过日报，跳过")
        sys.exit(0)

    # 确定报告日期：以 RtPrice 有效数据为准，回退查找
    found_date = None
    for days_back in range(5):
        check_date = (now - timedelta(days=days_back)).date()
        price_data = query_table("ah_sccqjg", ["DaPrice", "RtPrice"], check_date)
        if price_data:
            # 必须有有效的 RtPrice 才认为日期可用
            rt_count = sum(1 for data in price_data.values() if data.get("RtPrice") is not None)
            if rt_count >= 20:
                found_date = check_date
                print(f"[INFO] {check_date} RtPrice 有效记录: {rt_count}")
                break
            else:
                print(f"[INFO] {check_date} RtPrice 全为 null，回退...")

    if found_date is None:
        print("[SKIP] 最近4天无有效价格数据，跳过推送")
        sys.exit(2)

    report_date = found_date
    report_date_str = report_date.strftime("%Y-%m-%d")
    print(f"[INFO] 报告日期: {report_date_str}")

    # 1. 查询价格数据
    price_data = query_table("ah_sccqjg", ["DaPrice", "RtPrice", "DaLoad", "RtLoad"], report_date)
    print(f"[INFO] 价格数据: {len(price_data)} 小时")

    # 2. 查询偏差数据
    fhsj_data = query_table("ah_fhsj", ["Load"], report_date)
    scfhyc_data = query_table("ah_scfhyc", ["Load"], report_date)
    xnysj_data = query_table("ah_xnysj", ["Load"], report_date)
    xnyyc_data = query_table("ah_xnyyc", ["Load"], report_date)

    # 3. 计算核心指标
    da_prices = [data.get("DaPrice") for data in price_data.values() if data.get("DaPrice") is not None]
    rt_prices = [data.get("RtPrice") for data in price_data.values() if data.get("RtPrice") is not None]

    avg_da = safe_mean(da_prices) or 0
    avg_rt = safe_mean(rt_prices) or 0

    # 按时计算价差
    hourly_diff = []
    for hour in sorted(price_data.keys()):
        d = price_data[hour]
        da = d.get("DaPrice")
        rt = d.get("RtPrice")
        if da is not None and rt is not None:
            diff = da - rt  # 日前 - 实时
            hourly_diff.append({"hour": hour, "DaPrice": da, "RtPrice": rt, "diff": diff})

    avg_diff = safe_mean([h["diff"] for h in hourly_diff]) or 0

    if hourly_diff:
        max_item = max(hourly_diff, key=lambda x: x["diff"])
        min_item = min(hourly_diff, key=lambda x: x["diff"])
        max_diff = max_item["diff"]
        min_diff = min_item["diff"]
        max_hour = max_item["hour"].strftime("%H:%M")
        min_hour = min_item["hour"].strftime("%H:%M")
    else:
        max_diff = min_diff = 0
        max_hour = min_hour = "-"

    # 4. 趋势判断
    if avg_diff > 5:
        trend = "🟢 日前价高"
    elif avg_diff < -5:
        trend = "🔴 实时价高"
    else:
        trend = "⚪ 价差较小"

    # 5. 构建24小时分时表（选取关键字段）
    detail_rows = []
    for item in sorted(hourly_diff, key=lambda x: x["hour"]):
        h = item["hour"]
        hour_key = h.replace(minute=0, second=0, microsecond=0)
        load_act = fhsj_data.get(hour_key, {}).get("Load")
        load_fc = scfhyc_data.get(hour_key, {}).get("Load")
        xny_act = xnysj_data.get(hour_key, {}).get("Load")
        xny_fc = xnyyc_data.get(hour_key, {}).get("Load")

        # 负荷偏差
        load_bias = None
        if load_act is not None and load_fc is not None and load_fc != 0:
            load_bias = round((load_act - load_fc) / load_fc * 100, 1)

        # 新能源偏差
        xny_bias = None
        if xny_act is not None and xny_fc is not None and xny_fc != 0:
            xny_bias = round((xny_act - xny_fc) / xny_fc * 100, 1)

        diff_icon = "📈" if item["diff"] > 0 else ("📉" if item["diff"] < 0 else "➖")
        detail_rows.append(
            f"| {h.strftime('%H:%M')} "
            f"| {fmt_val(item['DaPrice'], 1)} "
            f"| {fmt_val(item['RtPrice'], 1)} "
            f"| {diff_icon} {fmt_val(item['diff'], 1)} "
            f"| {fmt_val(load_bias, 1)}% "
            f"| {fmt_val(xny_bias, 1)}% |"
        )

    # 6. 构建钉钉 Markdown
    md_text = f"""**安徽** | 现货价差日报

## ⚡ 现货交易价差分析

**📅 {report_date_str}** | 数据粒度：1小时 | {trend}

---

### 📊 核心指标

| 指标 | 数值 |
|---|---|
| 日前均价 | **{avg_da:.1f}** 元/MWh |
| 实时均价 | **{avg_rt:.1f}** 元/MWh |
| 平均价差 | **{avg_diff:.1f}** 元/MWh |
| 最大价差 | **{max_diff:.1f}** 元/MWh (@ {max_hour}) |
| 最小价差 | **{min_diff:.1f}** 元/MWh (@ {min_hour}) |

---

### 📋 分时明细（{len(detail_rows)}/24 小时）

| 时刻 | 日前价 | 实时价 | 价差 | 负荷偏差 | 新能源偏差 |
|---|---|---|---|---|---|
{chr(10).join(detail_rows)}

---

> 💡 价差 = 日前价格 - 实时价格 | 正值为日前高于实时
> ⚙️ 自动推送 · 润建电子科技 | `{now.strftime('%Y-%m-%d %H:%M')}`"""

    # 7. 推送
    print(f"[INFO] 消息长度: {len(md_text)} 字符")
    success = send_dingtalk({
        "msgtype": "markdown",
        "markdown": {"title": f"安徽现货价差日报 {report_date_str}", "text": md_text}
    })

    if success:
        print(f"[SUCCESS] 日报推送完成 — {report_date_str}")
    else:
        print("[FAIL] 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
