#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百炼控制台会话一键重抓（macOS，2026-09-07 流程脚本化）

背景：Token Plan 用量走百炼控制台内部 API，依赖浏览器会话 cookie，
百炼会话 3~6 天必过期（09-01/09-04/09-07 三连过期）。过期后拉取会失败，
但 fetch_balances.py 已改为「失败回填旧值」，不会污染 data.js；
本脚本负责低成本恢复会话。

用法（本机 Terminal 或在 Obsidian 里让 Claudian 执行）：
    python3 bailian_reauth.py

前提：本机日常使用的 Chrome 已登录过百炼控制台（复用其登录态）。
架构（持久 profile = 根治会话过期的关键）：
  · 专用持久 profile 存于 .secrets/bailian-profile（不走网盘/git，0700）
  · 每次运行：启动它 → CDP 提取 sec_token+cookies → 验证 → 写回 .secrets
  · 访问控制台本身会给登录态"续命"，配合同步任务每日运行 ⇒ 会话永不过期
  · 首次运行用 .secrets 现有会话做种子（免登录）；种子也失效时只需在
    弹出窗口登录一次，登录态永久留存于该 profile，之后全自动
  · 日常 Chrome 全程只读引导（仅冷启动无种子时复制一次会话文件）

注意：macOS Keychain 可能弹窗请求 "Chrome Safe Storage" 解密授权，点「始终允许」。
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(DIR, ".secrets", "model_keys.json")
IS_WIN = sys.platform == "win32"
# 跨平台：主设备(Windows)与 Mac 同一套命令
if IS_WIN:
    _LD = os.environ.get("LOCALAPPDATA", "")
    CHROME = next((p for p in [os.path.join(_LD, "Google/Chrome/Application/chrome.exe"),
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
                 if p and os.path.exists(p)), "chrome.exe")
    PROFILE = os.path.join(_LD, "Google", "Chrome", "User Data")
else:
    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome")
# 持久专属 profile：登录态留在里面，日常访问=自动续命；放在 .secrets 内（不走网盘/git）
PERSIST = os.path.join(DIR, ".secrets", "bailian-profile")
PLAN_URL = ("https://bailian.console.aliyun.com/cn-beijing"
            "?tab=plan#/efm/subscription/token-plan/personal")
PORT = 9333  # 避开日常可能用到的 9222
LOGIN_WAIT_SEC = 600  # 短信验证/扫码可能慢，给 10 分钟

# 只复制会话必需文件，最小化敏感数据落盘面
COPY_FILES = ["Local State",
              "Default/Cookies", "Default/Cookies-wal",
              "Default/Preferences", "Default/Secure Preferences",
              "Default/Login Data", "Default/Local Storage"]

GRAB_JS = r"""
// CDP 提取 sec_token + aliyun cookies（Node 22+ 全局 WebSocket，零依赖）
import fs from 'fs';
const port = process.argv[2];
const seedFile = process.argv[3];   // 传入则先注入 cookie（种子模式）
if (seedFile) {
  let t0 = null;
  for (let i = 0; i < 20; i++) {   // 刚启动时 page target 可能未建，重试等待
    const targets0 = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    t0 = targets0.find(t => t.type === 'page');
    if (t0) break;
    await new Promise(r => setTimeout(r, 500));
  }
  if (!t0) { console.log(JSON.stringify({seedError: 'no_page'})); process.exit(1); }
  const ws0 = new WebSocket(t0.webSocketDebuggerUrl);
  let i0 = 0; const q = new Map();
  const send0 = (m, p) => new Promise((rs, rj) => { const id = ++i0; q.set(id, {rs, rj});
    ws0.send(JSON.stringify({id, method: m, params: p})); });
  ws0.onmessage = e => { const m = JSON.parse(e.data);
    if (m.id && q.has(m.id)) { const {rs, rj} = q.get(m.id); q.delete(m.id); m.error ? rj(m.error) : rs(m.result); } };
  await new Promise(r => ws0.onopen = r);
  await send0('Network.enable', {});
  await send0('Page.enable', {});   // Page.navigate 前置依赖
  const cs = JSON.parse(fs.readFileSync(seedFile, 'utf8'));
  await send0('Network.setCookies', {cookies: cs});
  await send0('Page.navigate', {url: process.argv[4]});
  console.log(JSON.stringify({seeded: cs.length}));
  ws0.close(); process.exit(0);
}
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find(t => t.type === 'page' && t.url.includes('bailian'));
if (!page) { console.log(JSON.stringify({ready: false, why: 'no_page'})); process.exit(0); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
const send = (m, p = {}) => new Promise((res, rej) => {
  const mid = ++id; pend.set(mid, {res, rej});
  ws.send(JSON.stringify({id: mid, method: m, params: p})); });
ws.onmessage = e => { const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { const {res, rej} = pend.get(m.id); pend.delete(m.id);
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
await new Promise(r => ws.onopen = r);
await send('Network.enable');
const ev = await send('Runtime.evaluate', {expression:
  '(window.ALIYUN_CONSOLE_CONFIG && window.ALIYUN_CONSOLE_CONFIG.SEC_TOKEN) || ""',
  returnByValue: true});
const {cookies} = await send('Network.getAllCookies');
const rel = cookies.filter(c => c.domain.includes('aliyun'));
const seen = new Set(), uniq = [];
for (const c of rel) { const k = c.name + c.domain;
  if (!seen.has(k)) { seen.add(k); uniq.push(c); } }  // 同名跨域去重
const cs = uniq.map(c => `${c.name}=${c.value}`).join('; ');
console.log(JSON.stringify({ready: !!ev.result.value && uniq.length > 0,
  secToken: ev.result.value, cookieStr: cs}));
ws.close();
"""


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def wait_port(port, timeout=30):
    for _ in range(timeout):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                        timeout=2):
                return True
        except Exception:
            time.sleep(1)
    return False


def grab(port):
    """跑一次 CDP 提取，返回 dict 或 None"""
    fd, js = tempfile.mkstemp(suffix=".mjs")
    with os.fdopen(fd, "w") as f:
        f.write(GRAB_JS)
    try:
        r = sh("node", js, str(port), timeout=30)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    finally:
        try:
            os.unlink(js)
        except OSError:
            pass


def verify(cred):
    """用提取的会话直连 usage 接口验证"""
    sys.path.insert(0, DIR)
    import fetch_balances as fb
    try:
        pct, reset = fb.fetch_token_plan(cred)
        return True, f"已用 {pct}%（{reset} 重置）"
    except Exception as e:
        return False, str(e)[:100]


def copy_profile(tmp):
    """只读复制登录态文件到临时 profile；返回复制数"""
    n = 0
    for rel in COPY_FILES:
        src = os.path.join(PROFILE, rel)
        hits = glob.glob(src)  # Cookies-wal 等可能不存在，glob 兼容
        for s in hits:
            d = os.path.join(tmp, os.path.relpath(s, PROFILE))
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)  # Local Storage 是目录
            else:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
            n += 1
    return n


def seed_cookies(port, cookie_str):
    """把 .secrets 里的 cookie 注入持久 profile（种子模式，免重登录）"""
    cs = []
    for part in cookie_str.split('; '):
        if '=' in part:
            name, val = part.split('=', 1)
            cs.append({"name": name, "value": val, "domain": ".aliyun.com",
                       "path": "/", "expires": int(time.time()) + 30 * 86400,
                       "secure": True, "sameSite": "None"})
    fd, jf = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cs, f)
    fd, js = tempfile.mkstemp(suffix=".mjs")
    with os.fdopen(fd, "w") as f:
        f.write(GRAB_JS)
    try:
        r = sh("node", js, str(port), jf, PLAN_URL, timeout=30)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        for x in (js, jf):
            try:
                os.unlink(x)
            except OSError:
                pass


def main():
    if shutil.which("node") is None:
        sys.exit("需要 node（Chrome DevTools Protocol 客户端）")
    keys = json.load(open(SECRETS, encoding="utf-8"))
    old = keys.get("aliyun_console", {})

    # 持久 profile：首次用 .secrets 现有会话做种子；种子也没有才复制日常 Chrome
    dflt = os.path.join(PERSIST, "Default")
    os.makedirs(dflt, exist_ok=True)
    try:
        os.chmod(PERSIST, 0o700)  # 内含登录凭证，权限收紧
    except OSError:
        pass
    fresh = os.path.exists(os.path.join(dflt, "Cookies"))
    if not fresh and old.get("cookie"):
        print("🌱 持久 profile 首建，用 .secrets 现有会话做种子")
    elif not fresh:
        n = copy_profile(PERSIST)
        print(f"📋 持久 profile 首建，从日常 Chrome 复制 {n} 个会话文件引导")

    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PERSIST}", "--no-first-run",
         "--no-default-browser-check", PLAN_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("🌐 调试 Chrome 已弹出（持久 profile），等待页面加载…")
    try:
        if not wait_port(PORT):
            sys.exit("❌ CDP 端口未就绪：本脚本需在登录 GUI 会话运行（cron/ssh 弹不出窗口）")
        if not fresh and old.get("cookie"):
            seed_cookies(PORT, old["cookie"])
        print("   ↳ 若页面停在登录页，请【在这个窗口里登录阿里云】——登录态将永久留存于此 profile")
        time.sleep(8)

        deadline = time.time() + LOGIN_WAIT_SEC
        hint = False
        t0 = time.time()
        while time.time() < deadline:
            g = grab(PORT)
            if not (g and g.get("ready")):
                if not hint and time.time() - t0 > 25:
                    print("🔑 页面未就绪（大概率停在登录页）——请在弹出的 Chrome 窗口完成登录，"
                          f"脚本自动继续（最长 {LOGIN_WAIT_SEC // 60} 分钟）。"
                          "\n   本次登录后，以后每天自动续命，无需再登。")
                    hint = True
                time.sleep(5)
                continue
            cred = dict(old, cookie=g["cookieStr"], sec_token=g["secToken"])
            ok, msg = verify(cred)
            if ok:
                ac = keys.setdefault("aliyun_console", {})
                ac["cookie"] = g["cookieStr"]
                ac["sec_token"] = g["secToken"]
                ac["captured_at"] = time.strftime("%Y-%m-%d %H:%M")
                tmpf = SECRETS + ".tmp"
                with open(tmpf, "w", encoding="utf-8") as f:
                    json.dump(keys, f, ensure_ascii=False, indent=2)
                os.replace(tmpf, SECRETS)
                print(f"✅ 会话已刷新：{msg}")
                return
            time.sleep(5)
        sys.exit("❌ 超时未拿到有效会话")
    finally:
        # 只关浏览器，profile 保留（这就是"续命"的关键）
        if IS_WIN:
            sh("taskkill", "/F", "/T", "/PID", str(proc.pid))
        else:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
