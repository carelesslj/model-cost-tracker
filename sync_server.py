#!/usr/bin/env python3
"""模型费用追踪 - 本地同步服务 v2
运行后浏览器打开 http://localhost:8765  (推荐从仪表盘操作)

API:
  GET  /api/balances  返回 balances.json
  POST /api/refresh   即时拉取全部渠道余额/额度并落盘+回写 md
  POST /api/manual    手动补录 {"date":"2026-09-01","cash":{"百炼":123.4},"plans":{"Token Plan":{"monthly_pct":45}}}

启动方式: python sync_server.py
"""
import http.server
import importlib
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime

PORT = 8765
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

import fetch_balances as fb  # noqa: E402


def fb_reload():
    """每次请求前热重载拉取模块，避免改了 fetch_balances.py 还要重启服务"""
    try:
        importlib.reload(fb)
    except Exception:
        pass


def json_response(handler, code, obj):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/balances":
            try:
                with open(fb.BALANCES_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
                json_response(self, 200, data)
            except FileNotFoundError:
                json_response(self, 404, {"error": "balances.json 不存在，请先刷新"})
            return
        if self.path == "/api/prices":
            try:
                import fetch_prices as _fp
                with open(_fp.PRICES_JSON, "r", encoding="utf-8") as f:
                    json_response(self, 200, json.load(f))
            except FileNotFoundError:
                json_response(self, 404, {"error": "prices.json 不存在"})
            except Exception as e:
                json_response(self, 500, {"error": str(e)[:200]})
            return
        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        if self.path == "/api/refresh":
            try:
                fb_reload()
                secrets = fb.load_secrets()
                cash, plans, _ = fb.query_all(secrets)
                hist = fb.load_history()
                fb.merge_day(hist, cash, plans)
                fb.write_outputs(hist)
                fb.sync_md(hist)
                # 顺手推云端（Actions 的 schedule 高峰延迟 1~2h，不可依赖）
                # 失败不影响本地刷新结果；带凭证泄漏扫描护栏，命中即拒推
                try:
                    import push_to_cloud
                    cp_ok, cp_msg = push_to_cloud.push()
                except Exception as e:
                    cp_ok, cp_msg = False, str(e)[:120]
                json_response(self, 200, {"ok": True, "data": hist,
                                          "cloudPush": {"ok": cp_ok, "msg": cp_msg}})
            except Exception as e:
                json_response(self, 500, {"ok": False, "error": str(e)[:300]})
            return

        if self.path == "/api/manual":
            try:
                fb_reload()
                hist = fb.load_history()
                day = body.get("date") or datetime.now().strftime("%Y-%m-%d")
                entry = hist["daily"].setdefault(day, {"cash": {}, "plans": {}})
                for p, v in (body.get("cash") or {}).items():
                    if v is None:
                        continue
                    entry.setdefault("cash", {})[p] = {
                        "value": round(float(v), 2), "source": "manual",
                        "manual_at": datetime.now().isoformat(timespec="seconds")}
                for p, obj in (body.get("plans") or {}).items():
                    merged = dict(entry.get("plans", {}).get(p, {}))
                    merged.update(obj)
                    merged["source"] = "manual"
                    entry.setdefault("plans", {})[p] = merged
                hist["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                fb.write_outputs(hist)
                fb.sync_md(hist)
                json_response(self, 200, {"ok": True, "data": hist})
            except Exception as e:
                json_response(self, 500, {"ok": False, "error": str(e)[:300]})
            return

        json_response(self, 404, {"error": "not found"})

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            super().log_message(format, *args)


if __name__ == "__main__":
    os.chdir(DIR)
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"📊 同步服务已启动: http://localhost:{PORT}/model-cost-tracker.html")
    print("按 Ctrl+C 停止")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}/model-cost-tracker.html")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
