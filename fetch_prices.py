#!/usr/bin/env python3
"""模型价格对比拉取：OpenCode Go 价格表（文档页抓取）+ OpenRouter（公开 API）
+ DeepSeek 官方定价页，输出 prices.json / prices.js 供仪表盘底部渲染。

用法:
  python fetch_prices.py            # 拉取并打印对比
  python fetch_prices.py --quiet    # 静默（由 fetch_balances.py 定期调用）
"""
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIR = os.path.dirname(os.path.abspath(__file__))
PRICES_JSON = os.path.join(DIR, "prices.json")
PRICES_JS = os.path.join(DIR, "prices.js")
TIMEOUT = 25
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0"}

# Go 名称 → OpenRouter 名称的别名映射（id 后缀匹配失败时兜底）
ALIASES = {
    "muse spark 1.2 contributor": "muse spark 1.2",
}
# 模型家族 → OpenRouter 厂商 id 前缀（用于消歧）
VENDOR = {"grok": "x-ai", "gpt": "openai", "glm": "z-ai", "kimi": "moonshot",
          "longcat": "meituan", "mimo": "xiaomi", "minimax": "minimax",
          "muse": "meta", "qwen": "qwen", "deepseek": "deepseek",
          "hy": "tencent", "hunyuan": "tencent"}


def norm(s):
    s = re.sub(r"\(.*?\)|（.*?）", "", s or "")
    s = re.sub(r"[^a-z0-9.]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def parse_money(s):
    m = re.search(r"\$?([\d,.]+)", (s or "").replace(",", ""))
    return float(m.group(1)) if m else None


def tier_of(go_name):
    """识别价格档位后缀"""
    n = go_name.lower()
    if ">" in n:
        return "长上下文档"
    if "off-peak" in n:
        return "低峰价"
    if "peak" in n:
        return "高峰价"
    return None


def fetch_go_pricing():
    """抓取 OpenCode Go 文档页价格表（模型/输入/输出/缓存读取/缓存写入/使用额度）"""
    r = requests.get("https://opencode.ai/docs/zh-cn/go/", headers=UA, timeout=TIMEOUT)
    soup = BeautifulSoup(r.content, "html.parser")
    table = None
    for tb in soup.find_all("table"):
        heads = [th.get_text(strip=True) for th in tb.find_all("th")]
        if any("输入" in h for h in heads):
            table = tb
            break
    if table is None:
        raise RuntimeError("未找到 Go 价格表（页面结构可能已变更）")
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 6:
            continue
        rows.append({
            "model": tds[0],
            "input": parse_money(tds[1]), "output": parse_money(tds[2]),
            "cache_read": parse_money(tds[3]), "cache_write": parse_money(tds[4]),
            "quota": tds[5],
        })
    m = re.search(r"最近更新[：:]\s*([^<]+)", str(soup.get_text()))
    page_date = None
    m2 = re.search(r"最近更新[：:]\s*([^\n\r]+)", soup.get_text())
    if m2:
        page_date = m2.group(1).strip()
    return rows, page_date


def fetch_go_requests():
    """抓取 OpenCode Go 文档页「预估请求数」表（Model / 每5小时 / 每周 / 每月）。
    与价格表同名模型归并；请求数表模型名无档位后缀（≤200K/>200K 等）。
    返回 [{model, h5, weekly, monthly}]，数字为千分位整数请求数。"""
    r = requests.get("https://opencode.ai/docs/zh-cn/go/", headers=UA, timeout=TIMEOUT)
    soup = BeautifulSoup(r.content, "html.parser")
    table = None
    for tb in soup.find_all("table"):
        heads = [th.get_text(strip=True) for th in tb.find_all("th")]
        if any("每 5 小时" in h or "每5小时" in h for h in heads):
            table = tb
            break
    if table is None:
        raise RuntimeError("未找到 Go 预估请求数表（页面结构可能已变更）")
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 4:
            continue
        def reqnum(s):
            m = re.search(r"[\d,]+", (s or "").replace(",", ""))
            return int(m.group(0)) if m else None
        rows.append({
            "model": tds[0],
            "h5": reqnum(tds[1]),
            "weekly": reqnum(tds[2]),
            "monthly": reqnum(tds[3]),
        })
    return rows


def fetch_openrouter():
    """OpenRouter 公开模型列表（USD / 1M tokens），过滤免费/批量/零价变体"""
    r = requests.get("https://openrouter.ai/api/v1/models", timeout=TIMEOUT)
    out = []
    for m in r.json().get("data", []):
        mid = m.get("id", "")
        if ":" in mid:            # :free / :batch / :nitro 等变体一律不要
            continue
        p = m.get("pricing", {})
        try:
            inp = round(float(p.get("prompt") or 0) * 1e6, 4)
            outp = round(float(p.get("completion") or 0) * 1e6, 4)
        except (TypeError, ValueError):
            continue
        if inp == 0 and outp == 0:
            continue
        cr = p.get("input_cache_read")
        out.append({
            "id": mid, "name": m.get("name", ""),
            "base": norm(m.get("name", "").split(": ", 1)[-1]),
            "input": inp, "output": outp,
            "cache_read": round(float(cr) * 1e6, 4) if cr else None,
        })
    return out


def match_or(go_name, or_list):
    n = norm(go_name)
    n = ALIASES.get(n, n)
    if not n:
        return None
    slug = n.replace(" ", "-")
    vendor = next((v for k, v in VENDOR.items()
                   if n == k or n.startswith(k + " ")), None)
    # 1) 模型 id 后缀精确匹配（最可靠：deepseek v4 pro → deepseek/deepseek-v4-pro）
    for e in or_list:
        if e["id"].split("/", 1)[-1] == slug:
            return e
    # 2) 去品牌前缀后的名称精确匹配
    exact = [e for e in or_list if e["base"] == n]
    if exact:
        if vendor:
            for e in exact:
                if e["id"].startswith(vendor + "/"):
                    return e
        return exact[0]
    # 3) 包含匹配（厂商前缀优先，短名优先）
    subs = [e for e in or_list if n in e["base"] or e["base"] in n]
    if vendor:
        vs = [e for e in subs if e["id"].startswith(vendor + "/")]
        if vs:
            subs = vs
    if not subs:
        return None
    subs.sort(key=lambda e: len(e["base"]))
    return subs[0]


def build():
    go_rows, page_date = fetch_go_pricing()
    or_list = fetch_openrouter()
    try:
        req_rows = fetch_go_requests()
    except Exception:
        req_rows = []
    rows = []
    for g in go_rows:
        o = match_or(g["model"], or_list)
        row = dict(g)
        row["tier"] = tier_of(g["model"])
        row["or"] = o
        if o and g["input"] and o["input"]:
            row["in_diff_pct"] = round((g["input"] - o["input"]) / o["input"] * 100, 1)
        else:
            row["in_diff_pct"] = None
        rows.append(row)
    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "go_page_date": page_date,
        "sources": {
            "go": "https://opencode.ai/docs/zh-cn/go/",
            "openrouter": "https://openrouter.ai/api/v1/models",
        },
        "rows": rows,
        "requests": req_rows,
    }


def write_outputs(data):
    with open(PRICES_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(PRICES_JS, "w", encoding="utf-8") as f:
        f.write("window.PRICE_DATA = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";\n")


def maybe_update(max_age_hours=12):
    """供 fetch_balances 调用：价格数据超过 max_age_hours 才重新拉取（失败静默）"""
    try:
        if os.path.exists(PRICES_JSON):
            age = time.time() - os.path.getmtime(PRICES_JSON)
            if age < max_age_hours * 3600:
                return False
        data = build()
        write_outputs(data)
        return True
    except Exception as e:
        try:
            with open(os.path.join(DIR, "fetch_log.txt"), "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 价格拉取失败: {str(e)[:150]}\n")
        except OSError:
            pass
        return False


def main():
    quiet = "--quiet" in sys.argv
    data = build()
    write_outputs(data)
    if not quiet:
        print(f"💲 价格拉取完成：{len(data['rows'])} 个 Go 模型 · "
              f"文档页更新于 {data['go_page_date'] or '?'}")
        matched = sum(1 for r in data["rows"] if r["or"])
        print(f"   OpenRouter 匹配 {matched}/{len(data['rows'])}")
        for r in data["rows"]:
            o = r["or"]
            flag = ""
            if r["in_diff_pct"] is not None:
                flag = "✅一致" if abs(r["in_diff_pct"]) <= 5 else f"⚠️差{r['in_diff_pct']:+.0f}%"
            if r["tier"]:
                flag += f" [{r['tier']}]"
            ors = f"OR ${o['input']}/${o['output']} ({o['id']})" if o else "OR 未匹配"
            print(f"  {r['model']:<38} Go ${r['input']}/${r['output']} | {ors} {flag}")


if __name__ == "__main__":
    main()
