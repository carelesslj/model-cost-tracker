@echo off
rem 模型费用仪表盘 - 百炼会话每日续命（headless 种子模式：全程无窗口、无需人工；
rem 会话真失效时静默失败，由每小时 fetch 报 TP error，届时人工跑一次带窗口的 bailian_reauth.py）
cd /d E:\Project\model-cost-dashboard
.venv\Scripts\python.exe -X utf8 bailian_reauth.py 120 --headless >> reauth_keepalive.log 2>&1
