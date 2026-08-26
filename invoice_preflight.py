"""
invoice_preflight.py —— 票据收集上线前自检

在跑得通 HoyoWave 的那台机器上执行：

    python invoice_preflight.py                    # 只做不发消息的检查
    python invoice_preflight.py --to <user_id>     # 额外真发一条测试消息给你自己

逐项 PASS / FAIL，FAIL 会直接说下一步做什么。不改任何线上数据，
唯一的副作用是 --to 时会发出一条测试消息。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_ICON = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "－"}
_results: list[tuple[str, str, str]] = []


def record(status: str, title: str, detail: str = "") -> str:
    _results.append((status, title, detail))
    print(f"{_ICON[status]} {title}")
    if detail:
        for line in detail.rstrip().split("\n"):
            print(f"     {line}")
    return status


def load_dotenv(path: str = ".env") -> None:
    """没装 python-dotenv 也能读 .env。"""
    if not os.path.exists(path):
        return
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── 1. 配置 ───────────────────────────────────────────────────────────
def check_env() -> bool:
    need = ["HOYOWAVE_BASE_URL", "HOYOWAVE_APP_ID", "HOYOWAVE_APP_SECRET",
            "HOYOWAVE_AES_KEY", "HOYOWAVE_VERIFY_TOKEN"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        record(FAIL, "配置项", "缺少：" + ", ".join(missing) +
               "\n照 .env.example 补进 .env（与本脚本同目录），或直接 export。")
        return False
    masked = {k: (os.environ[k][:6] + "…" if len(os.environ[k]) > 8 else "***") for k in need}
    record(PASS, "配置项", "\n".join(f"{k} = {v}" for k, v in masked.items()))
    return True


# ── 2. 依赖 ───────────────────────────────────────────────────────────
def check_crypto() -> None:
    try:
        import Crypto.Cipher.AES  # noqa: F401
        return record(PASS, "AES 实现", "pycryptodome") and None
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        return record(PASS, "AES 实现", "cryptography") and None
    except Exception:                                       # noqa: BLE001
        pass
    record(WARN, "AES 实现", "两个库都没有，将用内置的纯 Python 解密（能跑，只是慢一点）。"
                             "\n想快一点：pip install pycryptodome")


# ── 3. 存储 ───────────────────────────────────────────────────────────
def check_storage() -> None:
    import invoice_collect as ic
    try:
        conn = ic.connect()
        n = len(ic.list_tasks(conn))
        conn.close()
        os.makedirs(ic.FILE_DIR, exist_ok=True)
        probe = os.path.join(ic.FILE_DIR, ".preflight")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        record(PASS, "本地存储", f"库 {ic.DB_PATH}（现有 {n} 条任务）\n发票目录 {ic.FILE_DIR} 可写")
    except Exception as e:                                  # noqa: BLE001
        record(FAIL, "本地存储", f"{e}\n检查 INVOICE_DB / INVOICE_DIR 的路径与权限。")


# ── 4. 换 token（真连 HoyoWave）────────────────────────────────────────
def check_token() -> "object|None":
    import invoice_collect as ic
    try:
        ch = ic.HoyowaveChannel()
    except Exception as e:                                  # noqa: BLE001
        record(FAIL, "HoyoWave 渠道初始化", str(e))
        return None
    try:
        tk = ch.access_token()
    except Exception as e:                                  # noqa: BLE001
        record(FAIL, "换 access_token", f"{e}\n"
               "若是路径不对，在 .env 里指定 HOYOWAVE_TOKEN_URL；"
               "装了官方 SDK 也可以改成 HoyowaveChannel(sdk_app=app)。")
        return None
    record(PASS, "换 access_token", f"命中路径 {ch._token_path or '(静态 token)'}，"
                                    f"token {tk[:8]}…")
    return ch


# ── 5. 发消息（只有 --to 时才真发）──────────────────────────────────────
def check_send(ch, to: str) -> None:
    if not ch:
        return record(SKIP, "发测试消息", "上一步没拿到 token") and None
    if not to:
        return record(SKIP, "发测试消息",
                      "想真发一条：python invoice_preflight.py --to <你的 user_id>") and None
    try:
        mid = ch.send_text(to, "【票据收集自检】收到这条说明发消息链路已通，可以忽略。")
        record(PASS, "发测试消息", f"已发给 {to}，message_id={mid}\n去 HoyoWave 里确认收到没有。")
    except Exception as e:                                  # noqa: BLE001
        record(FAIL, "发测试消息", f"{e}\n"
               "常见原因：user_id 类型不对（试试 .env 里 HOYOWAVE_RECEIVER_ID_TYPE=email 之类）、"
               "应用没有发消息权限、或这个人没和机器人建立过会话。")


# ── 6. 回调端口 ───────────────────────────────────────────────────────
def check_port(port: int) -> None:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        record(PASS, f"回调端口 {port}", "空闲，可以起服务")
    except OSError as e:
        s.close()
        probe = socket.socket()
        probe.settimeout(1)
        busy = probe.connect_ex(("127.0.0.1", port)) == 0
        probe.close()
        if busy:
            record(WARN, f"回调端口 {port}", "已被占用 —— 如果就是你的 webhook 服务在跑，那没问题")
        else:
            record(FAIL, f"回调端口 {port}", str(e))


# ── 7. 回调闭环（本机自打一次）─────────────────────────────────────────
def check_callback_loop(port: int) -> None:
    import hoyowave_callback as cb
    import invoice_collect as ic

    tmp = tempfile.mkdtemp(prefix="preflight-")
    conn = ic.connect(os.path.join(tmp, "t.db"))
    ch = ic.MockChannel({"pf-file": ("preflight.pdf", b"%PDF-1.7 preflight")})
    ic.create_tasks(conn, [{"doc_code": "PREFLIGHT-001", "user_id": "pf_user"}])
    ic.send_pending(conn, ch)

    lp = port + 1                                            # 用相邻端口，别占正式的
    t = threading.Thread(target=cb.run_server,
                         kwargs=dict(host="127.0.0.1", port=lp, path="/webhook",
                                     on_event=lambda ev: ic.handle_chat_event(conn, ch, ev, tmp)),
                         daemon=True)
    t.start()
    time.sleep(0.6)
    body = json.dumps({
        "token": os.environ.get("HOYOWAVE_VERIFY_TOKEN", ""),
        "event_id": "preflight-1",
        "event": {"sender": {"sender_id": {"open_id": "pf_user"}},
                  "message": {"file": {"file_key": "pf-file", "name": "preflight.pdf"}}},
    }).encode()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{lp}/webhook", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            out = json.loads(resp.read().decode())
        ok = (out.get("result") or {}).get("result") == "saved"
        record(PASS if ok else FAIL, "回调 → 归档闭环",
               f"服务应答：{json.dumps(out, ensure_ascii=False)[:200]}")
    except Exception as e:                                   # noqa: BLE001
        record(FAIL, "回调 → 归档闭环", str(e))
    finally:
        conn.close()


# ── 8. 外网可达性（只能提醒，测不了）────────────────────────────────────
def check_reachability() -> None:
    url = os.environ.get("HOYOWAVE_CALLBACK_URL", "")
    record(WARN, "回调地址可达性",
           f"后台配的是 {url or '(未在 .env 里写)'}\n"
           "这一项脚本测不了：得由 HoyoWave 那边能访问到你这台机器。\n"
           "在后台点一次「验证回调地址」，服务日志里应该出现一条请求；没有就是网络不通。")


def check_userid_source() -> None:
    import invoice_collect as ic
    sample = [{"prCode": "PR260513000041", "skuName": "ChatGPT", "version": "Pro",
               "currency": "USD", "unitPrice": "200",
               "requesterName": "示例", "requesterDomain": "sample.domain"}]
    tasks, _ = ic.tasks_from_ledger(sample)
    ok = tasks and tasks[0]["user_id"] == "sample.domain"
    record(PASS if ok else FAIL, "收件人 user_id",
           "域账号即 user_id，台账行可直接转成收票任务（tasks_from_ledger），不需要映射表。\n"
           "批量发：python invoice_collect.py --from-ledger 台账.json --dry-run 先看名单")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="", help="真发一条测试消息给这个 user_id")
    ap.add_argument("--port", type=int, default=9000, help="webhook 端口，默认 9000")
    args = ap.parse_args()

    load_dotenv()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    print("\n票据收集 · 上线前自检\n" + "─" * 44)
    have_env = check_env()
    check_crypto()
    check_storage()
    ch = check_token() if have_env else record(SKIP, "换 access_token", "配置不全") and None
    check_send(ch, args.to)
    check_port(args.port)
    check_callback_loop(args.port)
    check_reachability()
    check_userid_source()

    print("─" * 44)
    n_fail = sum(1 for s, _, _ in _results if s == FAIL)
    n_warn = sum(1 for s, _, _ in _results if s == WARN)
    print(f"{len(_results)} 项：{sum(1 for s, _, _ in _results if s == PASS)} 通过 / "
          f"{n_fail} 失败 / {n_warn} 待确认\n")
    if n_fail:
        print("先解决上面标 ❌ 的，再跑一次。\n")
    elif n_warn:
        print("代码侧没问题了，剩下 ⚠️  的几项要在 HoyoWave 后台和网络上确认。\n")
    else:
        print("全通过，可以起服务：python -c \"import hoyowave_callback as cb; cb.serve_invoice_collect()\"\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
