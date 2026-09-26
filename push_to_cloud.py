#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 vault 侧仪表盘文件推送到 GitHub 仓库（本地刷新 = 云端刷新）

背景：GitHub Actions 的 schedule 触发高峰期会延迟 1~2 小时，不可依赖。
本 Mac 已验证 SSH key（id_ed25519_github_photiq）即仓库 owner carelesslj，
故本地每次 /api/refresh 完成后顺手 push，云端 60 秒内（Pages 重建）变新。
永久克隆：~/Library/Application Support/ModelCostTracker/deploy

纪律：推送前对每个文件做泄漏扫描（AK/sk-/cookie值/sec_token值 等），
任一命中即放弃本次推送并返回错误——数据文件绝不允许带上凭证。
"""
import os
import re
import shutil
import subprocess
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.expanduser(
    "~/Library/Application Support/ModelCostTracker/deploy")
KEY = os.path.expanduser("~/.ssh/id_ed25519_github_photiq")
# 只推这些文件（白名单；仓库里的 README/fetch.yml 等不在本地维护，不动）
FILES = ["data.js", "balances.json", "prices.js", "prices.json",
         "model-cost-tracker.html", "fetch_balances.py", "fetch_prices.py"]
# 凭证特征：命中任何一条就拒绝推送（扫的是"值"，不是字段名）
LEAK = re.compile(
    r"LTAI[A-Za-z0-9]{12,}|sk-sp-[A-Za-z0-9._-]{20,}|sk-[A-Za-z0-9]{24,}"
    r"|login_aliyunid_ticket=[A-Za-z0-9%$.]{20,}|sec_token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9]{15,}"
    r"|x-group-id[\"']?\s*[:=]\s*[\"']?\d{15,}")


def _git(*args, timeout=60):
    env = dict(os.environ, GIT_SSH_COMMAND=f"ssh -i {KEY} -o IdentitiesOnly=yes")
    return subprocess.run(["git", "-C", DEPLOY, *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def push():
    """返回 (ok, msg)。任何异常都吞成 (False, msg)，不阻塞主刷新流程。"""
    try:
        if not os.path.isdir(os.path.join(DEPLOY, ".git")):
            return False, "部署克隆不存在"
        for f in FILES:
            src = os.path.join(DIR, f)
            if not os.path.exists(src):
                continue
            if LEAK.search(open(src, encoding="utf-8").read()):
                return False, f"泄漏扫描拦截: {f} 含凭证特征"
            shutil.copy2(src, os.path.join(DEPLOY, f))
        _git("add", "-A")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return True, "无变化，跳过推送"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        c = _git("commit", "-m", f"mac-sync: {ts}")
        if c.returncode != 0:
            return False, "commit 失败: " + c.stderr[:120]
        # Actions 可能已先行提交（其数据较旧）：rebase 远端，冲突文件取本机侧
        r = _git("pull", "--rebase", "-X", "theirs", "origin", "main", timeout=120)
        if r.returncode != 0:
            _git("rebase", "--abort")
            _git("reset", "--hard", "-q", "origin/main")
            return False, "rebase 失败: " + (r.stderr or r.stdout)[-160:]
        p = _git("push", "origin", "main", timeout=120)
        if p.returncode != 0:
            # 网络/冲突失败：回滚本地提交保持克隆干净，下次刷新会再推
            _git("reset", "--hard", "-q", "origin/main")
            return False, "push 失败: " + (p.stderr or p.stdout)[-160:]
        return True, "已推送云端"
    except Exception as e:
        return False, f"push 异常: {str(e)[:150]}"


if __name__ == "__main__":
    ok, msg = push()
    print(("✅" if ok else "❌"), msg)
