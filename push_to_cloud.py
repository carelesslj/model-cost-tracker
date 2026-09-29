#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本项目的数据与代码推送/同步到 GitHub 仓库（本地刷新 = 云端刷新）。

⚠️ 2026-09-29 架构变更（迁移 + 用户拍板）：
  - 本项目目录 **本身就是 git 工作副本**（旧架构"vault 目录 + 独立部署克隆
    ~/Library/Application Support/ModelCostTracker/deploy"两处制已废除）。
  - 本 Mac（本 Bot）接管 **代码 + 数据** 权威；Windows 降为纯数据端。
  - 旧纪律"Mac 永不推代码"随之作废——那是 09-28 双端互推打回事故的临时措施。

纪律（不可省略）：
  1. 推送前对每个白名单文件做凭证泄漏扫描（AK / sk- / 登录 ticket / sec_token 等），
     任一命中即放弃本次推送并报出文件名。
  2. `git add` 只加白名单路径（不是 -A），避免误提交 .secrets / docs / archive 等。
  3. push 失败即回滚本地提交，保持工作区干净，下次刷新会重试。

用法：
  python3 push_to_cloud.py           # 提交白名单变更并推送
  python3 push_to_cloud.py --pull    # 只从远端同步（每小时链第一步用）
"""
import os
import re
import subprocess
import sys
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = sys.platform.startswith("win")
# Mac：仓库 owner 的 SSH key；Windows：HTTPS + credential helper（gh auth setup-git）
KEY = os.path.expanduser("~/.ssh/id_ed25519_github_photiq")

# 白名单：本项目里会随运行变化的全部受控文件（数据四件套 + 日志 + 代码 + 忽略规则）
FILES = [
    "data.js", "balances.json", "prices.js", "prices.json", "fetch_log.txt",
    "model-cost-tracker.html", "fetch_balances.py", "fetch_prices.py",
    "sync_server.py", "push_to_cloud.py", "bailian_reauth.py", ".gitignore",
]

# 凭证特征：命中任何一条就拒绝推送（扫的是「值」，不是字段名）
LEAK = re.compile(
    r"LTAI[A-Za-z0-9]{12,}|sk-sp-[A-Za-z0-9._-]{20,}|sk-[A-Za-z0-9]{24,}"
    r"|login_aliyunid_ticket=[A-Za-z0-9%$.]{20,}|sec_token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9]{15,}"
    r"|x-group-id[\"']?\s*[:=]\s*[\"']?\d{15,}")


def _git(*args, timeout=120):
    env = dict(os.environ)
    if not IS_WIN:
        env["GIT_SSH_COMMAND"] = f"ssh -i {KEY} -o IdentitiesOnly=yes"
    return subprocess.run(["git", "-C", DIR, *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def _is_repo():
    return os.path.isdir(os.path.join(DIR, ".git"))


def leak_scan():
    """返回首个含凭证特征的白名单文件名，无则 None。"""
    for f in FILES:
        p = os.path.join(DIR, f)
        if not os.path.exists(p):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if LEAK.search(txt):
            return f
    return None


def pull():
    """只同步远端（每小时链第一步）。冲突则中止本次拉取并还原，不影响本地文件。"""
    try:
        if not _is_repo():
            return False, "当前目录不是 git 工作副本"
        r = _git("pull", "--rebase", "origin", "main", timeout=180)
        if r.returncode != 0:
            _git("rebase", "--abort")
            return False, "pull 失败: " + (r.stderr or r.stdout)[-140:]
        return True, "已同步远端"
    except Exception as e:
        return False, f"pull 异常: {str(e)[:140]}"


def _dirty_outside_whitelist():
    """返回被改动但不在白名单里的已跟踪文件（rebase 会被它们卡住）。"""
    r = _git("status", "--porcelain", "--untracked-files=no")
    out = []
    for line in (r.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if path not in FILES:
            out.append(path)
    return out


def push():
    """返回 (ok, msg)。任何异常都吞成 (False, msg)，不阻塞主刷新流程。"""
    try:
        if not _is_repo():
            return False, "当前目录不是 git 工作副本"
        hit = leak_scan()
        if hit:
            return False, f"泄漏扫描拦截: {hit} 含凭证特征（已放弃推送）"
        # 只暂存白名单路径；仓库里其余文件（README/workflow 等）不由本机维护
        _git("add", "--", *FILES)
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return True, "无变化，跳过推送"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        c = _git("commit", "-m", f"{'windows' if IS_WIN else 'mac'}-sync: {ts}")
        if c.returncode != 0:
            return False, "commit 失败: " + c.stderr[:120]
        # ⚠️ 2026-09-29 事故修正：旧代码失败回滚用 `git reset --hard origin/main`，
        # 会把白名单之外的本地改动（如 .gitignore）一起抹掉。改为 `--mixed HEAD~1`：
        # 只撤销提交、完整保留工作区内容 —— 任何失败都不得造成文件丢失。
        stray = _dirty_outside_whitelist()
        if stray:
            _git("reset", "--mixed", "-q", "HEAD~1")
            return False, ("白名单外有未暂存改动，已放弃本次推送（无文件丢失）: "
                           + ", ".join(stray[:5]))
        # 其他设备可能已先行提交：rebase 远端。
        # 注意 rebase 语义与 merge 相反：theirs = 本机侧 → 冲突时以本机为准（本机是权威）
        r = _git("pull", "--rebase", "-X", "theirs", "origin", "main", timeout=180)
        if r.returncode != 0:
            _git("rebase", "--abort")
            _git("reset", "--mixed", "-q", "HEAD~1")
            return False, "rebase 失败: " + (r.stderr or r.stdout)[-160:]
        p = _git("push", "origin", "main", timeout=180)
        if p.returncode != 0:
            # 网络/冲突失败：只撤销本地提交（保留文件），下次刷新会再推
            _git("reset", "--mixed", "-q", "HEAD~1")
            return False, "push 失败: " + (p.stderr or p.stdout)[-160:]
        return True, "已推送云端"
    except Exception as e:
        return False, f"push 异常: {str(e)[:150]}"


if __name__ == "__main__":
    ok, msg = (pull() if "--pull" in sys.argv else push())
    print(("✅" if ok else "❌"), msg)
    sys.exit(0 if ok else 1)