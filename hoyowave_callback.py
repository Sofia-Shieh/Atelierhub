"""
hoyowave_callback.py —— HoyoWave 回调接收（解密 + 验签 + URL 验证 + 落到票据收集）

HoyoWave 后台配的回调地址是加密推送的，这个模块负责把它拆开：

    收到 POST /webhook
      → 验签（timestamp + nonce + aes_key + body 的 sha256）
      → AES-256-CBC 解密 {"encrypt": "..."}
      → 校验 verify_token
      → url_verification 就回 challenge；消息事件就交给 invoice_collect 归档

关于加密方案：HoyoWave 的开放平台是飞书那一套的形制（同样的 encrypt 字段、
file_key、receiver_id_type），所以这里按同一套方案实现：

    key        = sha256(aes_key).digest()           # 32 字节
    明文       = AES-256-CBC-decrypt(base64(encrypt))，前 16 字节是 IV，PKCS#7 去填充
    signature  = sha256(timestamp + nonce + aes_key + body).hexdigest()

如果实测下来 HoyoWave 有出入，只需要改 decrypt_payload / calc_signature 两个函数。
第一次收到回调时模块会把请求头原样打到日志（HEADER_DUMP），照着调即可。

AES 依赖：装了 pycryptodome 或 cryptography 就用它们；都没有就走本文件内置的
纯 Python 实现（只实现解密，够用；已与 pycryptodome 逐比特比对过）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any, Callable, Optional

# ══════════════════════════════════════════════════════════════════════
# AES-256-CBC 解密：优先用已装的库，没有就用内置纯 Python 实现
# ══════════════════════════════════════════════════════════════════════
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
         0x6C, 0xD8, 0xAB, 0x4D]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        b >>= 1
        a = _xtime(a)
    return out


def _expand_key(key: bytes) -> tuple[list[list[int]], int]:
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w, nr


def _decrypt_block(block: bytes, w: list[list[int]], nr: int) -> bytes:
    # state 按列主序展开：state[r + 4c] 对应 FIPS-197 里的 s[r][c]
    s = list(block)

    def add_round_key(rnd: int) -> None:
        for c in range(4):
            for r in range(4):
                s[r + 4 * c] ^= w[rnd * 4 + c][r]

    def inv_shift_rows() -> None:
        old = list(s)
        for r in range(1, 4):
            for c in range(4):
                s[r + 4 * c] = old[r + 4 * ((c - r) % 4)]

    def inv_sub_bytes() -> None:
        for i in range(16):
            s[i] = _INV_SBOX[s[i]]

    def inv_mix_columns() -> None:
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c], s[1 + 4 * c], s[2 + 4 * c], s[3 + 4 * c]
            s[0 + 4 * c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
            s[1 + 4 * c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
            s[2 + 4 * c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
            s[3 + 4 * c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)

    add_round_key(nr)
    for rnd in range(nr - 1, 0, -1):
        inv_shift_rows()
        inv_sub_bytes()
        add_round_key(rnd)
        inv_mix_columns()
    inv_shift_rows()
    inv_sub_bytes()
    add_round_key(0)
    return bytes(s)


def _aes_cbc_decrypt_pure(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("密文长度不是 16 的整数倍")
    w, nr = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        plain = _decrypt_block(block, w, nr)
        out += bytes(x ^ y for x, y in zip(plain, prev))
        prev = block
    return bytes(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """有库用库，没库用内置实现。"""
    try:
        from Crypto.Cipher import AES                     # pycryptodome
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    except Exception:                                      # noqa: BLE001 - 装了但不可用也回落
        pass
    return _aes_cbc_decrypt_pure(key, iv, data)


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    return data[:-n] if 1 <= n <= 16 and data[-n:] == bytes([n]) * n else data


# ══════════════════════════════════════════════════════════════════════
# 解密 / 验签
# ══════════════════════════════════════════════════════════════════════
class CallbackError(Exception):
    pass


def decrypt_payload(encrypt_b64: str, aes_key: str) -> dict:
    """把 {"encrypt": "..."} 里的密文还原成事件 dict。"""
    try:
        raw = base64.b64decode(encrypt_b64)
    except Exception as e:                                 # noqa: BLE001
        raise CallbackError(f"encrypt 不是合法 base64：{e}") from e
    if len(raw) <= 16:
        raise CallbackError("密文太短，缺少 IV")
    key = hashlib.sha256(aes_key.encode("utf-8")).digest()
    plain = _unpad(aes_cbc_decrypt(key, raw[:16], raw[16:]))
    try:
        return json.loads(plain.decode("utf-8"))
    except Exception as e:                                 # noqa: BLE001
        raise CallbackError(f"解密后不是合法 JSON（AES_KEY 可能不对）：{e}") from e


def calc_signature(timestamp: str, nonce: str, aes_key: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(str(timestamp).encode("utf-8"))
    h.update(str(nonce).encode("utf-8"))
    h.update(aes_key.encode("utf-8"))
    h.update(body if isinstance(body, bytes) else str(body).encode("utf-8"))
    return h.hexdigest()


# 请求头名字各家不同，这几种拼法都认；实际收到什么会打在 HEADER_DUMP 里
_H_TIMESTAMP = ("x-wave-request-timestamp", "x-hoyowave-request-timestamp",
                "x-lark-request-timestamp", "timestamp")
_H_NONCE = ("x-wave-request-nonce", "x-hoyowave-request-nonce",
            "x-lark-request-nonce", "nonce")
_H_SIGN = ("x-wave-signature", "x-hoyowave-signature", "x-lark-signature", "signature")

HEADER_DUMP: list[dict] = []          # 前几次回调的请求头，方便对齐字段名


def _header(headers: dict, names: tuple) -> str:
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    for n in names:
        if lower.get(n):
            return str(lower[n])
    return ""


def handle_webhook(raw_body: bytes,
                   headers: dict = None,
                   aes_key: str = None,
                   verify_token: str = None,
                   on_event: Callable[[dict], Any] = None,
                   require_signature: bool = False) -> tuple[int, dict]:
    """
    处理一次回调请求，返回 (http_status, 响应体)。

    on_event 收到的是解密后的事件 dict —— 接票据收集就传
    lambda ev: invoice_collect.handle_chat_event(conn, channel, ev)
    """
    aes_key = aes_key if aes_key is not None else os.environ.get("HOYOWAVE_AES_KEY", "")
    verify_token = verify_token if verify_token is not None else os.environ.get("HOYOWAVE_VERIFY_TOKEN", "")
    headers = headers or {}

    if len(HEADER_DUMP) < 5:
        HEADER_DUMP.append(dict(headers))
        print(f"[hoyowave-callback] 收到回调，请求头：{json.dumps(dict(headers), ensure_ascii=False)}")

    # 1) 验签（拿不到签名头时默认放行，可用 require_signature=True 强制）
    sign = _header(headers, _H_SIGN)
    if sign:
        expect = calc_signature(_header(headers, _H_TIMESTAMP), _header(headers, _H_NONCE),
                                aes_key, raw_body)
        if not hmac.compare_digest(sign, expect):
            return 401, {"ok": False, "error": "签名校验失败"}
    elif require_signature:
        return 401, {"ok": False, "error": "缺少签名头"}

    # 2) 解析 + 解密
    try:
        body = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception as e:                                 # noqa: BLE001
        return 400, {"ok": False, "error": f"请求体不是 JSON：{e}"}
    if isinstance(body, dict) and body.get("encrypt"):
        try:
            body = decrypt_payload(body["encrypt"], aes_key)
        except CallbackError as e:
            return 400, {"ok": False, "error": str(e)}

    # 3) verify_token 校验
    token = body.get("token") or (body.get("header") or {}).get("token")
    if verify_token and token and not hmac.compare_digest(str(token), verify_token):
        return 401, {"ok": False, "error": "verify_token 不匹配"}

    # 4) URL 验证：原样回 challenge
    kind = body.get("type") or (body.get("header") or {}).get("event_type")
    if body.get("challenge") or kind == "url_verification":
        return 200, {"challenge": body.get("challenge", "")}

    # 5) 业务事件
    if on_event is None:
        return 200, {"ok": True, "ignored": "未挂 on_event", "event": kind or ""}
    try:
        return 200, {"ok": True, "result": on_event(body)}
    except Exception as e:                                 # noqa: BLE001 - 回调不能因业务异常一直重推
        print(f"[hoyowave-callback] 处理事件出错：{e}")
        return 200, {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════
# 独立 webhook 服务：后台配的是 10.5.52.26:9000/webhook，就按这个默认值起
# ══════════════════════════════════════════════════════════════════════
def run_server(host: str = "0.0.0.0", port: int = 9000, path: str = "/webhook",
               on_event: Callable[[dict], Any] = None) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _reply(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") not in (path.rstrip("/"), "/api/invoice/callback"):
                return self._reply(404, {"ok": False, "error": "no route"})
            n = int(self.headers.get("Content-Length") or 0)
            code, obj = handle_webhook(self.rfile.read(n), dict(self.headers), on_event=on_event)
            self._reply(code, obj)

        def do_GET(self):                                   # 有些平台会先 GET 探活
            self._reply(200, {"ok": True})

    print(f"[hoyowave-callback] 监听 http://{host}:{port}{path}")
    HTTPServer((host, port), Handler).serve_forever()


def serve_invoice_collect(host: str = "0.0.0.0", port: int = 9000, path: str = "/webhook") -> None:
    """开箱即用：起服务并把事件直接接到票据收集上。"""
    import invoice_collect as ic

    conn = ic.connect()
    channel = ic.HoyowaveChannel()
    run_server(host, port, path, on_event=lambda ev: ic.handle_chat_event(conn, channel, ev))


# ══════════════════════════════════════════════════════════════════════
# 自测：python hoyowave_callback.py
# ══════════════════════════════════════════════════════════════════════
def _selftest() -> None:
    import os as _os
    import secrets as _secrets
    import shutil
    import tempfile

    AES_KEY = "3B9eqlwJdU9ear8fQ53j5GZ"          # 长度与真实 key 一致，值是占位
    VERIFY_TOKEN = "verify-token-for-test"

    def check(label, got, want=True):
        flag = "OK  " if got == want else "FAIL"
        print(f"  [{flag}] {label}: {got!r}" + ("" if got == want else f"  期望 {want!r}"))
        assert got == want, label

    def encrypt(obj: dict, aes_key: str = AES_KEY) -> str:
        """按同一套方案加密，用来造回调请求（真实环境是 HoyoWave 那边加密）。"""
        from Crypto.Cipher import AES
        key = hashlib.sha256(aes_key.encode()).digest()
        iv = _secrets.token_bytes(16)
        raw = json.dumps(obj, ensure_ascii=False).encode()
        pad = 16 - len(raw) % 16
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(raw + bytes([pad]) * pad)
        return base64.b64encode(iv + ct).decode()

    print("\n1) 纯 Python AES 与 pycryptodome 逐比特一致")
    from Crypto.Cipher import AES as _AES
    for n, size in enumerate([16, 32, 64, 160]):
        key = _secrets.token_bytes(32)
        iv = _secrets.token_bytes(16)
        data = _secrets.token_bytes(size)
        ct = _AES.new(key, _AES.MODE_CBC, iv).encrypt(data)
        check(f"AES-256-CBC {size} 字节", _aes_cbc_decrypt_pure(key, iv, ct), data)
    key128 = _secrets.token_bytes(16)
    iv = _secrets.token_bytes(16)
    data = _secrets.token_bytes(48)
    ct = _AES.new(key128, _AES.MODE_CBC, iv).encrypt(data)
    check("AES-128-CBC 也对", _aes_cbc_decrypt_pure(key128, iv, ct), data)

    print("2) 解密回调密文")
    ev = {"token": VERIFY_TOKEN, "type": "event_callback", "event_id": "d1"}
    check("解密结果", decrypt_payload(encrypt(ev), AES_KEY), ev)
    try:
        decrypt_payload(encrypt(ev), "wrong-key")
        check("错的 key 应该失败", False)
    except CallbackError:
        check("错的 key 会明确报错", True)

    print("3) URL 验证：回 challenge")
    body = json.dumps({"encrypt": encrypt({"challenge": "abc123", "token": VERIFY_TOKEN,
                                           "type": "url_verification"})}).encode()
    code, obj = handle_webhook(body, {}, AES_KEY, VERIFY_TOKEN)
    check("状态码", code, 200)
    check("challenge 原样返回", obj, {"challenge": "abc123"})

    print("4) 验签")
    payload = json.dumps({"encrypt": encrypt(ev)}).encode()
    ts, nonce = "1756000000", "n1"
    good = calc_signature(ts, nonce, AES_KEY, payload)
    hdr = {"X-Wave-Request-Timestamp": ts, "X-Wave-Request-Nonce": nonce, "X-Wave-Signature": good}
    code, _ = handle_webhook(payload, hdr, AES_KEY, VERIFY_TOKEN)
    check("签名正确放行", code, 200)
    bad = dict(hdr, **{"X-Wave-Signature": "deadbeef"})
    code, obj = handle_webhook(payload, bad, AES_KEY, VERIFY_TOKEN)
    check("签名错误拒绝", (code, obj["error"]), (401, "签名校验失败"))
    code, obj = handle_webhook(payload, {}, AES_KEY, VERIFY_TOKEN, require_signature=True)
    check("强制验签时缺头拒绝", code, 401)

    print("5) verify_token 不匹配要拒绝")
    bad_tok = json.dumps({"encrypt": encrypt({"token": "someone-else", "type": "event_callback"})}).encode()
    code, obj = handle_webhook(bad_tok, {}, AES_KEY, VERIFY_TOKEN)
    check("拒绝", (code, obj["error"]), (401, "verify_token 不匹配"))

    print("6) 加密的文件消息 → 一路归档进票据收集")
    import invoice_collect as ic
    tmp = tempfile.mkdtemp(prefix="cb-test-")
    conn = ic.connect(_os.path.join(tmp, "t.db"))
    ch = ic.MockChannel({"fk-9": ("发票.pdf", b"%PDF-1.7 x")})
    ic.create_tasks(conn, [{"doc_code": "PO261001", "user_id": "u_alice"}])
    ic.send_pending(conn, ch)
    real_event = {
        "token": VERIFY_TOKEN, "type": "event_callback", "event_id": "cb-1",
        "event": {"sender": {"sender_id": {"open_id": "u_alice"}},
                  "message": {"content": "发票来了",
                              "file": {"file_key": "fk-9", "name": "发票.pdf", "size": 1024}}},
    }
    code, obj = handle_webhook(
        json.dumps({"encrypt": encrypt(real_event)}).encode(), {}, AES_KEY, VERIFY_TOKEN,
        on_event=lambda e: ic.handle_chat_event(conn, ch, e, tmp))
    check("状态码", code, 200)
    check("归档结果", obj["result"]["result"], "saved")
    check("挂到的单号", obj["result"]["doc_code"], "PO261001")
    check("台账已改已收", ic.list_tasks(conn, ic.STATUS_RECEIVED)[0].doc_code, "PO261001")

    print("7) 明文回调（万一平台没开加密）也认")
    ic.create_tasks(conn, [{"doc_code": "PO261002", "user_id": "u_bob"}])
    ic.send_pending(conn, ch)
    code, obj = handle_webhook(json.dumps({
        "token": VERIFY_TOKEN, "event_id": "cb-2",
        "event": {"sender": {"sender_id": {"open_id": "u_bob"}},
                  "message": {"file": {"file_key": "fk-9", "name": "b.pdf"}}}}).encode(),
        {}, AES_KEY, VERIFY_TOKEN, on_event=lambda e: ic.handle_chat_event(conn, ch, e, tmp))
    check("归档结果", obj["result"]["result"], "saved")

    print("8) 业务异常不把 500 抛给平台（否则会被反复重推）")
    code, obj = handle_webhook(
        json.dumps({"encrypt": encrypt(ev)}).encode(), {}, AES_KEY, VERIFY_TOKEN,
        on_event=lambda e: (_ for _ in ()).throw(RuntimeError("炸了")))
    check("仍然 200", code, 200)
    check("但标记失败", obj["ok"], False)

    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n回调链路验证通过 ✅\n")


if __name__ == "__main__":
    _selftest()
