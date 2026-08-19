#!/usr/bin/env python3
"""neone PTP MCP 客户端（走 oh-my-mcp serve 暴露的本地端点，即交接文档「方式 B」）。

需在 Titan 沙箱 Pod 内运行，且 ptp-mcp-proxy 处于 RUNNING：
    svc status ptp-mcp-proxy

用法：
    python3 tools/ptp_mcp.py ping                     # 验证代理与 token（401 = token 过期）
    python3 tools/ptp_mcp.py tools                    # 打印工具列表及入参 schema
    python3 tools/ptp_mcp.py schema add_demand        # 只看 add_demand 的完整入参
    python3 tools/ptp_mcp.py call <tool> '<json>'     # 通用调用
    python3 tools/ptp_mcp.py demand <spec.json>       # 预览需求单 payload（默认不提交）
    python3 tools/ptp_mcp.py demand <spec.json> --submit
"""
import json, os, sys, urllib.request, urllib.error

URL = os.environ.get("PTP_MCP_URL", "http://localhost:51023/mcp")
_session = None


def _post(payload, notify=False):
    """发一条 JSON-RPC，返回 (result, headers)。兼容 application/json 与 SSE 两种响应。"""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json,text/event-stream")
    if _session:
        req.add_header("mcp-session-id", _session)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code == 401:
            sys.exit("HTTP 401：oh-my-mcp IAM token 已过期。\n"
                     "  oh-my-mcp login https://api.agw.mihoyo.com/_mcp/neone-ptp-svc/mcp\n"
                     "  svc restart ptp-mcp-proxy")
        sys.exit(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"连不上 {URL}：{e.reason}\n  svc status ptp-mcp-proxy  # 确认 RUNNING")

    if notify:
        return None, headers

    # SSE：取最后一个 data: 行
    if raw.lstrip().startswith("event:") or "\ndata: " in raw or raw.startswith("data: "):
        datas = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
        if not datas:
            sys.exit(f"响应无 data 帧：{raw[:400]}")
        raw = datas[-1]
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"响应不是合法 JSON（可能触发了 12KB 截断，请分页）：\n{raw[:600]}")
    if "error" in msg:
        sys.exit("MCP 报错：" + json.dumps(msg["error"], ensure_ascii=False, indent=2))
    return msg.get("result"), headers


def connect():
    global _session
    result, headers = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "atelierhub-ptp", "version": "1"}}})
    _session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
    return result


def call(name, args):
    result, _ = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": name, "arguments": args}})
    return result


def render(result):
    for item in (result or {}).get("content", []):
        if item.get("type") == "text":
            print(item["text"])
    if (result or {}).get("isError"):
        sys.exit("工具返回 isError=true")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]

    # 预览模式不碰代理，任何机器上都能先看 payload
    if cmd == "demand" and "--submit" not in sys.argv:
        spec = json.load(open(sys.argv[2], encoding="utf-8"))
        print("── add_demand 入参 ──")
        print(json.dumps({"demandHead": spec["demandHead"], "rows": spec["rows"]},
                         ensure_ascii=False, indent=2))
        print("\n（预览模式，未提交。确认无误后加 --submit）")
        return

    info = connect()
    server = (info or {}).get("serverInfo", {})

    if cmd == "ping":
        print(f"✅ {server.get('name')} {server.get('version', '')}  session={_session}")
        render(call("get_user_login_info", {}))
        return

    if cmd in ("tools", "schema"):
        result, _ = _post({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        want = sys.argv[2] if cmd == "schema" and len(sys.argv) > 2 else None
        for t in result.get("tools", []):
            if want and t["name"] != want:
                continue
            print(f"\n── {t['name']} ──\n{(t.get('description') or '').strip()}")
            print(json.dumps(t.get("inputSchema", {}), ensure_ascii=False, indent=2))
        return

    if cmd == "call":
        render(call(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}))
        return

    if cmd == "demand":
        spec = json.load(open(sys.argv[2], encoding="utf-8"))
        args = {"demandHead": spec["demandHead"], "rows": spec["rows"]}
        print("── add_demand 入参 ──")
        print(json.dumps(args, ensure_ascii=False, indent=2))
        if "--submit" not in sys.argv:
            print("\n（预览模式，未提交。确认无误后加 --submit）")
            return
        state = args["demandHead"].get("state")
        print(f"\n提交中… state={state}（1=草稿，2=直接提交）")
        render(call("add_demand", args))
        return

    sys.exit(f"未知子命令 {cmd}\n{__doc__}")


if __name__ == "__main__":
    main()
