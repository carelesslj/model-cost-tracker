#!/usr/bin/env python3
"""模型费用追踪 - 多渠道余额/额度自动拉取 v2
查询 DeepSeek / 智谱 / Kimi / OpenCode Go / 阿里云百炼(BSS) 的余额或额度，
并行拉取 + 指数退避重试，写入 balances.json + data.js，
并同步回写 00-模型实时费用.md 的 frontmatter，追加 fetch_log.txt 拉取日志。

用法:
  python fetch_balances.py                 # 拉取全部渠道并打印结果
  python fetch_balances.py --quiet         # 静默模式（计划任务用）
  python fetch_balances.py --only Kimi 百炼 # 只拉取指定渠道（名称模糊匹配，其余保留原值）
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

# Windows 控制台 GBK 兼容：强制 UTF-8 输出（计划任务/终端都适用）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(DIR, ".secrets", "model_keys.json")
BALANCES_JSON = os.path.join(DIR, "balances.json")
DATA_JS = os.path.join(DIR, "data.js")
MD_FILE = os.path.join(DIR, "00-模型实时费用.md")
LOG_FILE = os.path.join(DIR, "fetch_log.txt")

CASH_PROVIDERS = ["DeepSeek", "智谱", "Kimi", "百炼", "火山", "MiniMax",
                  "硅基流动", "OpenAI", "Agnes"]
MANUAL_ONLY = {"硅基流动": "官方余额 API 已下线",
               "OpenAI": "未配置 key",
               "Agnes": "免费平台无余额接口"}
PLAN_PROVIDERS = ["OpenCode Go", "Token Plan"]
TIMEOUT = 20
MAX_RETRIES = 3          # 网络/5xx 重试次数（4xx 不重试）
LOW_BALANCE_THRESHOLD = 20.0  # 现金余额低于此值时日志中给出 ⚠ 提醒

_log_lock = threading.Lock()


def load_secrets():
    with open(SECRETS, "r", encoding="utf-8") as f:
        return json.load(f)


def today_key():
    return datetime.now().strftime("%Y-%m-%d")


def log_line(msg):
    """追加一行拉取日志（线程安全）"""
    with _log_lock:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                # 简单截断：只保留最近 500 行
                f.seek(0)
        except OSError:
            pass
        if "-v" in sys.argv or "--verbose" in sys.argv:
            print(line)


def trim_log(max_lines=500):
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
    except OSError:
        pass


def with_retry(fn, name):
    """执行 fn()，网络错误/5xx 指数退避重试；4xx 与业务错误直接抛出"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if 400 <= status < 500:
                raise  # key 失效/参数错，重试无意义
            last_err = e
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)  # 2s, 4s 退避
    raise last_err


# ---------------- 各渠道查询 ----------------

def fetch_deepseek(key):
    def _do():
        r = requests.get("https://api.deepseek.com/user/balance",
                         headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    infos = with_retry(_do, "DeepSeek").get("balance_infos", [])
    total = sum(float(i.get("total_balance", 0)) for i in infos
                if i.get("currency") == "CNY")
    return round(total, 2)


def fetch_zhipu(key):
    def _do():
        r = requests.get("https://www.bigmodel.cn/api/biz/account/query-customer-account-report",
                         headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    data = with_retry(_do, "智谱")
    if not data.get("success"):
        raise RuntimeError(data.get("msg", "zhipu api error"))
    d = data["data"]
    return round(float(d.get("availableBalance") or d.get("balance") or 0), 2)


def fetch_kimi(key):
    def _do():
        r = requests.get("https://api.moonshot.cn/v1/users/me/balance",
                         headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    d = with_retry(_do, "Kimi").get("data", {})
    return round(float(d.get("available_balance", 0)), 2), {
        "cash": round(float(d.get("cash_balance", 0)), 2),
        "voucher": round(float(d.get("voucher_balance", 0)), 2),
    }


def fetch_opencode_go(key):
    def _do():
        r = requests.get("https://opencode.ai/zen/go/v1/usage",
                         headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    u = with_retry(_do, "OpenCodeGo").get("usage", {})

    def w(name):
        x = u.get(name) or {}
        return {"percent": x.get("percent"), "resets_at": x.get("resetsAt"),
                "status": x.get("status")}
    return {"rolling": w("rolling"), "weekly": w("weekly"), "monthly": w("monthly")}


def fetch_token_plan(creds):
    """百炼个人版 Token Plan 用量（控制台内部 API，依赖抓取的会话 cookie）
    返回 (已用百分比, 重置时间ISO)"""
    api = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage"
    url = ("https://bailian-cs.console.aliyun.com/data/api.json"
           "?action=BroadScopeAspnGateway&product=sfm_bailian&api=" + api
           + "&_v=undefined")
    po = {"Api": api, "V": "1.0", "Data": {"cornerstoneParam": {
        "feTraceId": uuid.uuid4().hex,
        "feURL": "https://bailian.console.aliyun.com/cn-beijing?tab=plan",
        "protocol": "V2", "console": "ONE_CONSOLE", "productCode": "p_efm",
        "switchAgent": creds.get("switch_agent", 11526723), "switchUserType": 3,
        "domain": "bailian.console.aliyun.com", "consoleSite": "BAILIAN_ALIYUN",
        "xsp_lang": "zh-CN"}}}
    body = ("params=" + urllib.parse.quote(json.dumps(po, ensure_ascii=False))
            + "&region=cn-beijing&sec_token=" + creds["sec_token"])

    def _do():
        r = requests.post(url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": creds["cookie"],
            "Origin": "https://bailian.console.aliyun.com",
            "Referer": "https://bailian.console.aliyun.com/cn-beijing?tab=plan",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0"},
            timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    j = with_retry(_do, "TokenPlan")
    try:
        inner = j["data"]["DataV2"]["data"]
    except (KeyError, TypeError):
        raise RuntimeError("Token Plan 会话已过期或响应异常，需重新抓取（详见 .secrets aliyun_console.note）")
    if not inner.get("success"):
        raise RuntimeError("Token Plan 接口失败: " + str(inner.get("msg"))[:120])
    d = inner["data"]
    pct = round(float(d["per1WeekPercentage"]) * 100, 1)
    reset_ms = d.get("per1WeekResetTime")
    resets_at = None
    if reset_ms:
        resets_at = datetime.fromtimestamp(reset_ms / 1000, timezone.utc) \
            .astimezone().isoformat(timespec="seconds")
    return pct, resets_at


def _aliyun_sign(params, secret):
    """阿里云 RPC API 签名 V1 (HMAC-SHA1)"""
    sorted_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(params.items()))
    sts = f"GET&%2F&{urllib.parse.quote(sorted_qs, safe='')}"
    sig = base64.b64encode(
        hmac.new((secret + "&").encode(), sts.encode(), hashlib.sha1).digest()
    ).decode()
    return sig


def fetch_aliyun_balance(ak_id, ak_secret):
    def _do():
        params = {
            "Action": "QueryAccountBalance",
            "Version": "2017-12-14",
            "Format": "JSON",
            "AccessKeyId": ak_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        params["Signature"] = _aliyun_sign(params, ak_secret)
        r = requests.get("https://business.aliyuncs.com/", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    d = with_retry(_do, "百炼")
    if str(d.get("Code")) != "200":
        raise RuntimeError(d.get("Message", "bss api error"))
    return round(float(d["Data"]["AvailableAmount"]), 2)


def _volc_sign(ak, sk, host, region, service, params):
    """火山引擎 SignerV4（注意：初始密钥用原始 SK，不加 'AK' 前缀）"""
    t = datetime.now(timezone.utc)
    x_date = t.strftime("%Y%m%dT%H%M%SZ")
    date = t.strftime("%Y%m%d")
    qs = "&".join(f"{urllib.parse.quote(k, safe='~')}="
                  f"{urllib.parse.quote(str(v), safe='~')}"
                  for k, v in sorted(params.items()))
    payload_hash = hashlib.sha256(b"").hexdigest()
    hdict = {"host": host, "x-content-sha256": payload_hash, "x-date": x_date}
    signed = ";".join(sorted(hdict))
    ch = "".join(f"{k}:{hdict[k]}\n" for k in sorted(hdict))
    creq = "\n".join(["GET", "/", qs, ch, signed, payload_hash])
    scope = f"{date}/{region}/{service}/request"
    sts = "\n".join(["HMAC-SHA256", x_date, scope,
                     hashlib.sha256(creq.encode()).hexdigest()])

    def h(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k = h(sk.encode(), date)
    k = h(k, region)
    k = h(k, service)
    k = h(k, "request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Host": host, "X-Date": x_date, "X-Content-Sha256": payload_hash,
        "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, "
                         f"SignedHeaders={signed}, Signature={sig}",
    }
    return f"https://{host}/?{qs}", headers


def fetch_volcano(ak_id, ak_secret):
    def _do():
        url, headers = _volc_sign(ak_id, ak_secret, "billing.volcengineapi.com",
                                  "cn-north-1", "billing",
                                  {"Action": "QueryBalanceAcct",
                                   "Version": "2022-01-01"})
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    d = with_retry(_do, "火山")
    result = d.get("Result", {})
    if not result:
        raise RuntimeError(d.get("ResponseMetadata", {}).get("Error", {}).get("Message",
                                                                              "volc api error"))
    return round(float(result.get("CashBalance", 0)), 2)


def fetch_minimax(creds):
    """MiniMax 可用余额（控制台内部接口，依赖抓取的 _token 会话）
    返回 (available_amount, detail)"""
    def _do():
        r = requests.get("https://www.minimaxi.com/account/query_balance", headers={
            "Cookie": creds["cookie"],
            "x-group-id": creds["x_group_id"],
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://platform.minimaxi.com",
            "Referer": "https://platform.minimaxi.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0"},
            timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    d = with_retry(_do, "MiniMax")
    if d.get("base_resp", {}).get("status_code") != 0:
        msg = d.get("base_resp", {}).get("status_msg", "?")
        if "token" in str(msg).lower() or "login" in str(msg).lower():
            msg += "（会话过期，需重新登录抓取）"
        raise RuntimeError("minimax: " + str(msg)[:120])
    return round(float(d.get("available_amount", 0)), 2), {
        "cash": round(float(d.get("cash_balance", 0)), 2),
        "voucher": round(float(d.get("voucher_balance", 0)), 2),
        "credit": round(float(d.get("credit_balance", 0)), 2),
        "owed": round(float(d.get("owed_amount", 0)), 2),
    }


# ---------------- 并行调度 ----------------

def _make_cash_task(name, secrets):
    def task():
        try:
            if name == "DeepSeek":
                return name, {"value": fetch_deepseek(secrets["deepseek"]["primary"]),
                              "source": "auto"}
            if name == "智谱":
                return name, {"value": fetch_zhipu(secrets["zhipu"]["primary"]),
                              "source": "auto"}
            if name == "Kimi":
                val, detail = fetch_kimi(secrets["kimi"]["primary"])
                return name, {"value": val, "source": "auto", "detail": detail}
            if name == "百炼":
                aliyun = secrets.get("aliyun", {})
                if aliyun.get("access_key_id") and aliyun.get("access_key_secret"):
                    return name, {"value": fetch_aliyun_balance(
                        aliyun["access_key_id"], aliyun["access_key_secret"]),
                        "source": "auto"}
                return name, {"value": None, "source": "no_credentials",
                              "error": "未配置阿里云 AK/SK"}
            if name == "火山":
                volc = secrets.get("volcano", {})
                if volc.get("access_key_id") and volc.get("access_key_secret"):
                    return name, {"value": fetch_volcano(
                        volc["access_key_id"], volc["access_key_secret"]),
                        "source": "auto"}
                return name, {"value": None, "source": "no_credentials",
                              "error": "未配置火山引擎 AK/SK"}
            if name == "MiniMax":
                mm = secrets.get("minimax_console", {})
                if mm.get("cookie") and mm.get("x_group_id"):
                    val, detail = fetch_minimax(mm)
                    return name, {"value": val, "source": "auto", "detail": detail}
                return name, {"value": None, "source": "no_credentials",
                              "error": "未抓取 MiniMax 控制台会话"}
            return name, {"value": None, "source": "error", "error": "未知渠道"}
        except Exception as e:
            return name, {"value": None, "source": "error", "error": str(e)[:200]}
    return task


def _make_plan_task(secrets):
    def task():
        try:
            return dict(fetch_opencode_go(secrets["opencode_go"]["primary"]),
                        source="auto")
        except Exception as e:
            return {"rolling": None, "weekly": None, "monthly": None,
                    "source": "error", "error": str(e)[:200]}
    return task


def _make_token_plan_task(secrets):
    def task():
        creds = secrets.get("aliyun_console", {})
        if not creds.get("cookie") or not creds.get("sec_token"):
            return {"monthly_pct": None, "source": "no_credentials",
                    "error": "未抓取百炼控制台会话"}
        try:
            pct, resets_at = fetch_token_plan(creds)
            return {"monthly_pct": pct, "resets_at": resets_at, "source": "auto"}
        except Exception as e:
            return {"monthly_pct": None, "source": "error", "error": str(e)[:200]}
    return task


def query_all(secrets, only=None):
    """并行拉取。only=None 表示全部；only 为渠道名列表（子匹配，如 'kimi'、'go'）。
    返回 (cash dict, plans dict, fetched_names set)。未拉取的渠道值为 None 占位，
    merge_day 会保留历史中已有值。"""
    def selected(name):
        if only is None:
            return True
        return any(q.lower() in name.lower() for q in only)

    cash, plans = {}, {}
    fetched = set()
    # 无公开 API 的模型：直接标记 manual_only，不发起网络请求
    for p in MANUAL_ONLY:
        cash[p] = {"value": None, "source": "manual_only", "error": MANUAL_ONLY[p]}
    tasks = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for p in CASH_PROVIDERS:
            if p in MANUAL_ONLY:
                continue
            if selected(p):
                tasks[pool.submit(_make_cash_task(p, secrets))] = ("cash", p)
        if selected("OpenCode Go"):
            tasks[pool.submit(_make_plan_task(secrets))] = ("plan", "OpenCode Go")
        if selected("Token Plan"):
            tasks[pool.submit(_make_token_plan_task(secrets))] = ("plan_tp", "Token Plan")
        for fut in as_completed(tasks):
            kind, name = tasks[fut]
            if kind == "cash":
                key, result = fut.result()
                cash[key] = result
                fetched.add(key)
            elif kind == "plan":
                plans["OpenCode Go"] = fut.result()
                fetched.add("OpenCode Go")
            else:
                plans["Token Plan"] = fut.result()
                fetched.add("Token Plan")

    # 未拉取的渠道放占位（merge_day 里 skip）
    for p in CASH_PROVIDERS:
        if p not in cash:
            cash[p] = {"value": None, "source": "skipped"}
    if "OpenCode Go" not in plans:
        plans["OpenCode Go"] = {"source": "skipped"}
    if "Token Plan" not in plans:
        plans["Token Plan"] = {"monthly_pct": None, "source": "skipped"}
    return cash, plans, fetched


# ---------------- 汇总 & 落盘 ----------------

def load_history():
    if os.path.exists(BALANCES_JSON):
        with open(BALANCES_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 2, "generated_at": None, "cash_providers": CASH_PROVIDERS,
            "plan_providers": PLAN_PROVIDERS, "daily": {}}


def merge_day(hist, cash, plans):
    """写入当日快照。规则：
    - source==manual 的值不被自动结果覆盖
    - source==skipped（本次未拉取）不覆盖已有值
    - 当天首次出现时，first_* 从前一天终值继承（而非等于当日当前值）
    """
    day = today_key()
    old = hist["daily"].get(day, {})
    # 回溯最近一个已存在的日期，作为"当天首次出现"时的继承基准
    prev_day = None
    for d in sorted(hist["daily"].keys()):
        if d < day:
            prev_day = d
        else:
            break
    prev_hist = hist["daily"].get(prev_day, {}) if prev_day else {}
    old_cash = old.get("cash", {})
    merged_cash = {}
    for p, v in cash.items():
        prev = old_cash.get(p) or {}
        # 当天首次出现时，用前一天的终值作为 prev（用于继承 first_*）
        est_prev = prev if prev else (prev_hist.get("cash", {}).get(p) or {})
        if v.get("source") == "skipped" and prev:
            merged_cash[p] = prev          # 本次未拉，保留旧值
        elif prev.get("source") == "manual":
            merged_cash[p] = prev          # 手动值优先
        elif v.get("value") is None and prev.get("value") is not None:
            merged_cash[p] = prev          # 本次拉取失败，保留今日已有值
        else:
            if v.get("value") is not None:
                # 记录当日首次值（消费=首次值−当前值）
                fv = prev.get("first_value")
                if fv is None:
                    fv = prev.get("value") if prev.get("value") is not None \
                        else (est_prev.get("value") if est_prev.get("value") is not None else v["value"])
                v = dict(v, first_value=fv)
            merged_cash[p] = v
    old_plans = old.get("plans", {})
    prev_plans = prev_hist.get("plans", {})
    merged_plans = dict(old_plans)
    for k, v in plans.items():
        prevp = old_plans.get(k) or {}
        # 当天首次出现时，用前一天的 plans 作为继承基准（first_*）
        est_prevp = prevp if prevp else (prev_plans.get(k) or {})
        if v.get("source") == "skipped" and k in old_plans:
            continue                        # 本次未拉，保留旧值
        if k == "Token Plan" and prevp.get("source") == "manual":
            continue                        # Token Plan 手动值优先
        if k == "OpenCode Go" and v.get("source") == "auto":
            vv = dict(v)
            for win in ("rolling", "weekly", "monthly"):
                cur = dict(vv.get(win) or {})
                pr = est_prevp.get(win) or {}
                if cur.get("percent") is not None:
                    fp = pr.get("first_percent")
                    if fp is None:
                        fp = pr.get("percent") if pr.get("percent") is not None \
                            else cur["percent"]
                    cur["first_percent"] = fp
                    vv[win] = cur
                elif pr.get("percent") is not None:
                    vv[win] = pr            # 本次失败保留旧值
            merged_plans[k] = vv
        elif k == "Token Plan" and v.get("source") == "auto":
            vv = dict(v)
            if v.get("monthly_pct") is not None:
                fp = est_prevp.get("first_pct")
                if fp is None:
                    fp = est_prevp.get("monthly_pct") if est_prevp.get("monthly_pct") is not None \
                        else v["monthly_pct"]
                vv["first_pct"] = fp
            elif est_prevp.get("monthly_pct") is not None:
                vv = est_prevp            # 会话失败保留旧值
            merged_plans[k] = vv
        else:
            merged_plans[k] = v
    hist["daily"][day] = {
        "cash": merged_cash,
        "plans": merged_plans,
        "fetched_at": datetime.now().strftime("%H:%M:%S"),
    }
    hist["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")


def build_md_yaml(hist):
    """从 daily 生成 balanceData / planData 两段 YAML"""
    dates = sorted(hist["daily"].keys())
    lines = ["balanceData:"]
    for d in dates:
        lines.append(f"  {d}:")
        for p in CASH_PROVIDERS:
            v = hist["daily"][d]["cash"].get(p, {}).get("value")
            lines.append(f"    {p}: {v if v is not None else 'null'}")
    lines.append("planData:")
    for d in dates:
        lines.append(f"  {d}:")
        og = hist["daily"][d]["plans"].get("OpenCode Go", {}) or {}
        for win in ("rolling", "weekly", "monthly"):
            w = og.get(win) or {}
            lines.append(f"    OpenCode Go {win}: "
                         f"{w.get('percent') if isinstance(w, dict) else 'null'}")
        tp = hist["daily"][d]["plans"].get("Token Plan", {}) or {}
        tpv = tp.get("monthly_pct")
        lines.append(f"    Token Plan monthly: {tpv if tpv is not None else 'null'}")
    # 当日首次快照（消费 = 首次值 − 当前值；订阅 = 当前% − 首次%）
    lines.append("dayStartData:")
    for d in dates:
        rows = []
        for p in CASH_PROVIDERS:
            fv = hist["daily"][d]["cash"].get(p, {}).get("first_value")
            if fv is not None:
                rows.append(f"    {p}: {fv}")
        og = hist["daily"][d]["plans"].get("OpenCode Go", {}) or {}
        gfp = (og.get("monthly") or {}).get("first_percent")
        if gfp is not None:
            rows.append(f"    Go monthly: {gfp}")
        tfp = (hist["daily"][d]["plans"].get("Token Plan", {}) or {}).get("first_pct")
        if tfp is not None:
            rows.append(f"    Token Plan monthly: {tfp}")
        if rows:
            lines.append(f"  {d}:")
            lines.extend(rows)
    return "\n".join(lines) + "\n"


def sync_md(hist):
    yaml = build_md_yaml(hist)
    if os.path.exists(MD_FILE):
        with open(MD_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        return
    fm = re.match(r"^---\n([\s\S]*?)\n---", content)
    if fm:
        old = fm.group(1)
        # balanceData/planData 总是 frontmatter 的最后内容，整段替换
        new = re.sub(r"balanceData:[\s\S]*", yaml, old, count=1)
        if "balanceData:" not in old:
            new = (old.rstrip() + "\n\n" if old.strip() else "") + yaml
        content = content.replace(f"---\n{old}\n---", f"---\n{new.strip()}\n---")
    else:
        content = f"---\n{yaml}---\n\n{content.lstrip()}"
    with open(MD_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def write_outputs(hist):
    with open(BALANCES_JSON, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    with open(DATA_JS, "w", encoding="utf-8") as f:
        f.write("window.BALANCE_DATA = ")
        json.dump(hist, f, ensure_ascii=False)
        f.write(";\n")


def log_summary(cash, plans):
    """写本次拉取日志 + 低余额提醒"""
    parts = []
    for p in CASH_PROVIDERS:
        v = cash.get(p, {})
        if v.get("source") == "manual_only":
            continue  # 无 API 模型不进日志，避免噪音
        if v.get("source") == "skipped":
            parts.append(f"{p}=skip")
        elif v.get("value") is not None:
            parts.append(f"{p}={v['value']}")
            if float(v["value"]) < LOW_BALANCE_THRESHOLD:
                parts.append(f"⚠{p}余额低")
        else:
            parts.append(f"{p}=ERR({v.get('error', '')[:60]})")
    og = plans.get("OpenCode Go", {})
    if og.get("monthly"):
        parts.append(f"Go={og['rolling']['percent']}/{og['weekly']['percent']}"
                     f"/{og['monthly']['percent']}%")
    elif og.get("source") == "skipped":
        parts.append("Go=skip")
    else:
        parts.append(f"Go=ERR({og.get('error', '')[:60]})")
    tp = plans.get("Token Plan", {})
    if tp.get("source") == "auto":
        parts.append(f"TP={tp['monthly_pct']}%")
    elif tp.get("source") == "skipped":
        parts.append("TP=skip")
    else:
        parts.append(f"TP={tp.get('source')}({tp.get('error', '')[:50]})")
    log_line(" | ".join(parts))
    trim_log()


def main():
    parser = argparse.ArgumentParser(description="模型余额拉取")
    parser.add_argument("--quiet", action="store_true", help="静默（计划任务）")
    parser.add_argument("--only", nargs="+", metavar="渠道",
                        help="只拉取指定渠道，如: --only Kimi 百炼")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    secrets = load_secrets()
    t0 = time.time()
    cash, plans, fetched = query_all(secrets, only=args.only)
    hist = load_history()
    merge_day(hist, cash, plans)
    write_outputs(hist)
    sync_md(hist)
    log_summary(cash, plans)
    try:  # 价格对比数据：超过 6 小时才刷新，失败不影响主流程
        import fetch_prices
        fetch_prices.maybe_update(max_age_hours=6)
    except Exception:
        pass

    if not args.quiet:
        scope = " ".join(sorted(fetched)) if args.only else "全部"
        print(f"📅 {today_key()} 拉取完成（{scope}）耗时 {time.time()-t0:.1f}s")
        for p in CASH_PROVIDERS:
            v = cash[p]
            if v["source"] == "manual_only":
                print(f"  💰 {p}: （手动补录 · {v.get('error','')}）")
                continue
            if v["source"] == "skipped":
                print(f"  💰 {p}: （本次未拉取，保留旧值）")
                continue
            mark = ""
            if v["value"] is not None and float(v["value"]) < LOW_BALANCE_THRESHOLD:
                mark = " ⚠️ 余额偏低"
            print(f"  💰 {p}: {v['value'] if v['value'] is not None else '—'} "
                  f"[{v['source']}]{mark}{' ' + v.get('error', '') if v.get('error') else ''}")
        og = plans["OpenCode Go"]
        if og.get("monthly"):
            print(f"  📦 OpenCode Go: 5h {og['rolling']['percent']}% / "
                  f"周 {og['weekly']['percent']}% / 月 {og['monthly']['percent']}%")
        elif og.get("source") == "skipped":
            print("  📦 OpenCode Go: （本次未拉取，保留旧值）")
        else:
            print(f"  📦 OpenCode Go: error [{og.get('error')}]")
        tp = plans["Token Plan"]
        if tp.get("source") == "auto":
            print(f"   Token Plan: 已用 {tp['monthly_pct']}%（{tp.get('resets_at','')} 重置）")
        elif tp.get("source") == "skipped":
            print("  🧩 Token Plan: （本次未拉取，保留旧值）")
        else:
            print(f"  🧩 Token Plan: {tp.get('source')} [{tp.get('error')}]")


if __name__ == "__main__":
    main()
