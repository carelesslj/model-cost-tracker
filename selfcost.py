# -*- coding: utf-8 -*-
"""selfcost.py — 统计本仪表盘 agent 自身（Hermes profile）的真实 LLM 开销

数据源: Hermes state.db 的 session_model_usage（token 级真实计量）
计价口径（本项目 2026-09 核实）:
  - Credits: 每 1M tokens 消耗 = 100 × 该类型按量牌价(元/百万)；输出计费含 reasoning
  - yuan_tp : Credits × 139/45000（Token Plan Standard 折算实付）
  - yuan_api: Credits × 0.01（按量忙时牌价当量）
产物: selfcost.json + selfcost.js —— 均 gitignore（含个人用量，绝不推云端）
"""
import datetime
import json
import os
import sqlite3
import sys

D = os.path.dirname(os.path.abspath(__file__))
PROF = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "profiles",
                    "u6a21-u578b-u8d39-u7528-u4eea-u8868-u76d8", "state.db")
TZ = datetime.timezone(datetime.timedelta(hours=8))
# 百炼按量忙时价（元/百万 tokens，2026-09 与仪表盘价目核对一致）
PRICE = {
    "qwen3.8-flash":       {"in": 0.8, "out": 2.7, "cache": 0.1},
    "deepseek-v4.1-flash": {"in": 2.0, "out": 8.0, "cache": 0.2},
}
CREDIT_VALUE = 139 / 45000.0   # ¥/Credit（Standard 档 45,000 Credits = ¥139）


def month_of(ts):
    return datetime.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m")


def main():
    if not os.path.exists(PROF):
        print("selfcost: state.db 不存在，跳过", file=sys.stderr)
        return
    con = sqlite3.connect(f"file:{PROF}?mode=ro", uri=True)
    months = {}
    for model, prov, calls, tin, tout, tc, rw, fs in con.execute(
            "select model,billing_provider,api_call_count,input_tokens,"
            "output_tokens,cache_read_tokens,reasoning_tokens,first_seen"
            " from session_model_usage"):
        m = month_of(fs) if fs else datetime.datetime.now(TZ).strftime("%Y-%m")
        mo = months.setdefault(m, {"models": {}, "unpriced": {},
                                   "total_credits": 0.0,
                                   "total_yuan_tp": 0.0,
                                   "total_yuan_api": 0.0})
        p = PRICE.get(model)
        if p is None or prov != "alibaba-token-plan-cn":
            key = f"{model} @ {prov or '?'}"
            e = mo["unpriced"].setdefault(key, {"calls": 0, "input": 0,
                                                "output": 0, "cache_read": 0})
            e["calls"] += calls or 0; e["input"] += tin or 0
            e["output"] += (tout or 0) + (rw or 0); e["cache_read"] += tc or 0
            continue
        credits = (tin / 1e6 * p["in"] + (tout + rw) / 1e6 * p["out"]
                   + tc / 1e6 * p["cache"]) * 100
        y_tp, y_api = credits * CREDIT_VALUE, credits * 0.01
        e = mo["models"].setdefault(model, {
            "calls": 0, "input": 0, "output": 0, "cache_read": 0,
            "credits": 0.0, "yuan_tp": 0.0, "yuan_api": 0.0})
        e["calls"] += calls or 0; e["input"] += tin or 0
        e["output"] += (tout or 0) + (rw or 0); e["cache_read"] += tc or 0
        e["credits"] += credits
        e["yuan_tp"] += y_tp
        e["yuan_api"] += y_api
        mo["total_credits"] += credits
        mo["total_yuan_tp"] += y_tp
        mo["total_yuan_api"] += y_api
    for mo in months.values():
        for store in (mo["models"],):
            for e in store.values():
                for f in ("credits", "yuan_tp", "yuan_api"):
                    e[f] = round(e[f], 3)
        for f in ("total_credits", "total_yuan_tp", "total_yuan_api"):
            mo[f] = round(mo[f], 3)
    out = {
        "updated_at": datetime.datetime.now(TZ).isoformat(timespec="seconds"),
        "scope": "本仪表盘 agent 自身（Hermes profile：模型费用仪表盘）",
        "note": ("Credits=100×按量牌价(元/M)×tokens(百万)；¥tp=Credits×139/45000"
                 "(Standard档)；¥api=忙时按量当量；夜间22-8点 qwen×0.4/deepseek×0.5"),
        "price_cny_per_mtok": PRICE,
        "months": months,
    }
    with open(os.path.join(D, "selfcost.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(D, "selfcost.js"), "w", encoding="utf-8") as f:
        f.write("window.SELFCOST_DATA = "
                + json.dumps(out, ensure_ascii=False) + ";\n")
    cur = datetime.datetime.now(TZ).strftime("%Y-%m")
    mo = months.get(cur, {})
    print(f"selfcost ok | {cur}: {mo.get('total_credits', 0):.0f} Credits"
          f" ≈ ¥{mo.get('total_yuan_tp', 0):.2f}(TP) / ¥{mo.get('total_yuan_api', 0):.2f}(API当量)")


if __name__ == "__main__":
    main()
