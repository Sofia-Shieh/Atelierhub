"""
invoice_collect.py —— 票据（发票）收集闭环

流程：
    台账里挑出待收票的单子
      → 通过内部聊天软件（hoyowave）给对应的人发一条消息
      → 对方直接把发票文件发给机器人
      → 机器人回调进来，落盘归档、回执确认、台账状态改「已收」
      → 超时未回传的自动催办

设计要点：
  * 聊天渠道被隔离在 ChatChannel 适配器里。hoyowave 的接口我拿不到文档，
    HoyowaveChannel 里三个方法标了 TODO，填上就能上线；在那之前用
    MockChannel 可以把整条链路完整跑通（见文件末尾的自测）。
  * 只用标准库（sqlite3 / urllib / hashlib），不引入任何依赖，
    可以直接丢进现有后端目录 import 使用。
  * 所有状态落在 SQLite，重启不丢；回调按 event_id 去重，重复推送不会重复归档。

挂载方式见 README_MOUNT（文件末尾）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import secrets
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Protocol

DB_PATH = os.environ.get("INVOICE_DB", "invoice_collect.db")
FILE_DIR = os.environ.get("INVOICE_DIR", "invoices")

# 状态机：new（待发） → sent（已通知） → received（已收票） / cancelled（作废）
STATUS_NEW = "new"
STATUS_SENT = "sent"
STATUS_RECEIVED = "received"
STATUS_CANCELLED = "cancelled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_code      TEXT NOT NULL,              -- 单号（PO / 合同号 / 采购单号）
    title         TEXT NOT NULL DEFAULT '',   -- 软件名等，消息里展示用
    amount        TEXT NOT NULL DEFAULT '',   -- 金额（含币种，纯展示）
    payee         TEXT NOT NULL DEFAULT '',   -- 供应商/收款方
    user_id       TEXT NOT NULL,              -- 聊天软件里的用户标识
    user_name     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new',
    token         TEXT NOT NULL,              -- 备用：上传页/追踪用的一次性票据
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    received_at   TEXT,
    remind_count  INTEGER NOT NULL DEFAULT 0,
    last_remind_at TEXT,
    file_path     TEXT,
    file_name     TEXT,
    note          TEXT NOT NULL DEFAULT '',
    UNIQUE(doc_code, user_id)
);

CREATE TABLE IF NOT EXISTS invoice_events (
    event_id   TEXT PRIMARY KEY,              -- 回调去重
    handled_at TEXT NOT NULL,
    result     TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ──────────────────────────────────────────────────────────────────────
# 聊天渠道适配层
# ──────────────────────────────────────────────────────────────────────
class ChatChannel(Protocol):
    """把「发消息 / 取文件」抽象出来，换渠道只换这一层。"""

    def send_text(self, user_id: str, text: str) -> str:
        """发一条文本消息，返回消息 id。"""

    def download_file(self, file_ref: str) -> tuple[str, bytes]:
        """按回调里给的文件引用取回文件，返回 (文件名, 内容)。"""


class MockChannel:
    """本地假渠道：只记录发了什么、文件从本地目录取。用来跑通链路和写测试。"""

    def __init__(self, files: dict[str, tuple[str, bytes]] = None):
        self.sent: list[tuple[str, str]] = []
        self.files = files or {}

    def send_text(self, user_id: str, text: str) -> str:
        self.sent.append((user_id, text))
        print(f"[MockChannel] → {user_id}\n{text}\n")
        return f"mock-msg-{len(self.sent)}"

    def download_file(self, file_ref: str) -> tuple[str, bytes]:
        if file_ref not in self.files:
            raise KeyError(f"MockChannel 没有这个文件：{file_ref}")
        return self.files[file_ref]


class HoyowaveChannel:
    """
    hoyowave 渠道实现。

    ⚠️ 下面三处 TODO 是我拿不到的信息（内部系统，无公开文档）：
       1. 发消息的 URL 和请求体字段
       2. 鉴权方式（是 app_id/app_secret 换 token，还是固定 token / 签名）
       3. 回调里文件字段叫什么、怎么按引用把文件下载下来

    填这三处即可上线，其余流程（匹配单号、归档、回执、催办、去重）都不用动。
    """

    def __init__(self,
                 base_url: str = None,
                 app_id: str = None,
                 app_secret: str = None,
                 token: str = None,
                 timeout: int = 15):
        self.base_url = (base_url or os.environ.get("HOYOWAVE_BASE_URL", "")).rstrip("/")
        self.app_id = app_id or os.environ.get("HOYOWAVE_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("HOYOWAVE_APP_SECRET", "")
        self._token = token or os.environ.get("HOYOWAVE_TOKEN", "")
        self.timeout = timeout
        if not self.base_url:
            raise RuntimeError("未配置 HOYOWAVE_BASE_URL，请在 .env 里补上，或先用 MockChannel")

    # -- 内部：带鉴权的 HTTP --------------------------------------------
    def _request(self, path: str, payload: dict = None, method: str = "POST") -> dict:
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self._token:
            # TODO(hoyowave-2)：确认鉴权头。可能是 Authorization: Bearer，
            # 也可能是 X-Access-Token / 签名（app_id + timestamp + nonce + sign）。
            req.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}

    def send_text(self, user_id: str, text: str) -> str:
        # TODO(hoyowave-1)：换成真实的发消息接口与字段名
        result = self._request("/open-apis/im/v1/messages", {
            "receive_id": user_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        })
        return str(result.get("data", {}).get("message_id", "")) or "sent"

    def download_file(self, file_ref: str) -> tuple[str, bytes]:
        # TODO(hoyowave-3)：换成真实的文件下载接口
        url = f"{self.base_url}/open-apis/im/v1/files/{file_ref}"
        req = urllib.request.Request(url)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            name = resp.headers.get_filename() or f"{file_ref}.pdf"
            return name, resp.read()


# ──────────────────────────────────────────────────────────────────────
# 任务
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Task:
    id: int
    doc_code: str
    title: str
    amount: str
    payee: str
    user_id: str
    user_name: str
    status: str
    file_name: Optional[str] = None
    sent_at: Optional[str] = None
    received_at: Optional[str] = None
    remind_count: int = 0

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Task":
        return Task(
            id=row["id"], doc_code=row["doc_code"], title=row["title"],
            amount=row["amount"], payee=row["payee"], user_id=row["user_id"],
            user_name=row["user_name"], status=row["status"],
            file_name=row["file_name"], sent_at=row["sent_at"],
            received_at=row["received_at"], remind_count=row["remind_count"],
        )


def create_tasks(conn: sqlite3.Connection, rows: Iterable[dict]) -> list[int]:
    """
    登记待收票任务。同一个 (单号, 用户) 重复登记会被忽略，不会重复发消息。
    rows 里每条至少要有 doc_code 和 user_id。
    """
    ids: list[int] = []
    for r in rows:
        doc_code = str(r.get("doc_code") or "").strip()
        user_id = str(r.get("user_id") or "").strip()
        if not doc_code or not user_id:
            raise ValueError(f"doc_code / user_id 不能为空：{r}")
        cur = conn.execute(
            """INSERT OR IGNORE INTO invoice_tasks
               (doc_code, title, amount, payee, user_id, user_name, status, token, created_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (doc_code, str(r.get("title") or ""), str(r.get("amount") or ""),
             str(r.get("payee") or ""), user_id, str(r.get("user_name") or ""),
             STATUS_NEW, secrets.token_urlsafe(16), _now(), str(r.get("note") or "")))
        if cur.lastrowid and cur.rowcount:
            ids.append(cur.lastrowid)
    conn.commit()
    return ids


def list_tasks(conn: sqlite3.Connection, status: str = None) -> list[Task]:
    sql = "SELECT * FROM invoice_tasks"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY id DESC"
    return [Task.from_row(r) for r in conn.execute(sql, args)]


def _ask_text(task: sqlite3.Row, remind: bool = False) -> str:
    head = "【发票催收】" if remind else "【发票收集】"
    lines = [
        f"{head}麻烦提供下面这单的发票：",
        f"单号：{task['doc_code']}",
    ]
    if task["title"]:
        lines.append(f"内容：{task['title']}")
    if task["amount"]:
        lines.append(f"金额：{task['amount']}")
    if task["payee"]:
        lines.append(f"收款方：{task['payee']}")
    lines.append("")
    lines.append("直接把发票文件（PDF / 图片 / OFD）发到这个对话里就行，我会自动归档。")
    lines.append(f"如果一次要发多张，请在文件消息里带上单号 {task['doc_code']}。")
    if remind:
        lines.append(f"（第 {task['remind_count'] + 1} 次提醒，如已线下提供请回复「已提供」）")
    return "\n".join(lines)


def send_pending(conn: sqlite3.Connection, channel: ChatChannel) -> dict:
    """把 new 状态的任务逐条发出去。单条失败不影响其它条。"""
    sent, failed = 0, []
    for row in conn.execute("SELECT * FROM invoice_tasks WHERE status = ?", (STATUS_NEW,)).fetchall():
        try:
            channel.send_text(row["user_id"], _ask_text(row))
            conn.execute("UPDATE invoice_tasks SET status = ?, sent_at = ? WHERE id = ?",
                         (STATUS_SENT, _now(), row["id"]))
            sent += 1
        except Exception as e:                       # noqa: BLE001 - 单条失败要继续
            failed.append({"id": row["id"], "doc_code": row["doc_code"], "error": str(e)})
    conn.commit()
    return {"sent": sent, "failed": failed}


def remind_overdue(conn: sqlite3.Connection, channel: ChatChannel,
                   overdue_hours: int = 48, max_reminds: int = 3,
                   min_gap_hours: int = 24) -> dict:
    """超过 overdue_hours 还没回传的催办，最多催 max_reminds 次，两次间隔至少 min_gap_hours。"""
    now = datetime.now()
    reminded, skipped = 0, 0
    for row in conn.execute("SELECT * FROM invoice_tasks WHERE status = ?", (STATUS_SENT,)).fetchall():
        if row["remind_count"] >= max_reminds:
            skipped += 1
            continue
        anchor = row["last_remind_at"] or row["sent_at"]
        if not anchor:
            continue
        gap_needed = min_gap_hours if row["last_remind_at"] else overdue_hours
        if now - datetime.strptime(anchor, "%Y-%m-%d %H:%M:%S") < timedelta(hours=gap_needed):
            skipped += 1
            continue
        channel.send_text(row["user_id"], _ask_text(row, remind=True))
        conn.execute(
            "UPDATE invoice_tasks SET remind_count = remind_count + 1, last_remind_at = ? WHERE id = ?",
            (_now(), row["id"]))
        reminded += 1
    conn.commit()
    return {"reminded": reminded, "skipped": skipped}


# ──────────────────────────────────────────────────────────────────────
# 回调：对方在聊天里把发票发给机器人
# ──────────────────────────────────────────────────────────────────────
DOC_CODE_RE = re.compile(r"\b((?:PR|PO|REQ|DIS|HT|CT)[0-9A-Z\-]{6,})\b", re.I)


def _pick_task(conn: sqlite3.Connection, user_id: str, hint_text: str) -> tuple[Optional[sqlite3.Row], str]:
    """
    把收到的文件挂到哪一单上：
      1. 消息文本/文件名里带了单号 → 按单号（最准）
      2. 该用户只有一单待收 → 就是它
      3. 多单且没写单号 → 不猜，回问
    """
    pending = conn.execute(
        "SELECT * FROM invoice_tasks WHERE user_id = ? AND status = ? ORDER BY id",
        (user_id, STATUS_SENT)).fetchall()
    if not pending:
        return None, "no_pending"
    hit = DOC_CODE_RE.search(hint_text or "")
    if hit:
        code = hit.group(1).upper()
        for row in pending:
            if row["doc_code"].upper() == code:
                return row, "by_code"
        return None, "code_not_matched"
    if len(pending) == 1:
        return pending[0], "only_one"
    return None, "ambiguous"


def _save_file(doc_code: str, file_name: str, data: bytes, base_dir: str = None) -> str:
    safe_doc = re.sub(r"[^\w\-.]", "_", doc_code)
    safe_name = re.sub(r"[^\w\-.一-龥]", "_", file_name or "invoice")
    folder = os.path.join(base_dir or FILE_DIR, safe_doc)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe_name)
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):                      # 同名不覆盖
        path = f"{stem}({n}){ext}"
        n += 1
    with open(path, "wb") as f:
        f.write(data)
    return path


def handle_chat_event(conn: sqlite3.Connection, channel: ChatChannel,
                      payload: dict, base_dir: str = None) -> dict:
    """
    处理一条聊天回调。payload 里需要能取到：event_id / 发送者 / 文件引用 / 文本。
    各家字段名不同，这里做了别名兼容；hoyowave 的真实字段确定后按需补进 _pick 列表即可。
    """
    def _pick(d: dict, *keys, default=""):
        for k in keys:
            cur: Any = d
            for part in k.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            if cur not in (None, "", [], {}):
                return cur
        return default

    event_id = str(_pick(payload, "event_id", "header.event_id", "id", "msg_id",
                         default=secrets.token_hex(8)))
    row = conn.execute("SELECT result FROM invoice_events WHERE event_id = ?", (event_id,)).fetchone()
    if row:
        return {"ok": True, "duplicated": True, "result": row["result"]}      # 重复推送

    user_id = str(_pick(payload, "user_id", "sender", "sender.sender_id.open_id",
                        "event.sender.sender_id.open_id", "from_user", "open_id"))
    text = str(_pick(payload, "text", "content", "event.message.content", "message.text"))
    file_ref = _pick(payload, "file_ref", "file_key", "file_id",
                     "event.message.file_key", "message.file_key")
    file_name = str(_pick(payload, "file_name", "event.message.file_name", "message.file_name"))

    def _finish(result: str, **extra) -> dict:
        conn.execute("INSERT OR REPLACE INTO invoice_events (event_id, handled_at, result) VALUES (?,?,?)",
                     (event_id, _now(), result))
        conn.commit()
        return {"ok": result == "saved", "result": result, **extra}

    if not user_id:
        return _finish("no_sender")
    if not file_ref:
        # 纯文本消息：只对「已提供」这类回复做个记录，其余忽略
        if text and re.search(r"已(线下)?提供|已发(过|送)|已开(好|票)", text):
            channel.send_text(user_id, "收到，我先标记为线下已提供，稍后人工核对。")
            return _finish("text_ack")
        return _finish("ignored_no_file")

    task, why = _pick_task(conn, user_id, f"{text} {file_name}")
    if task is None:
        if why == "no_pending":
            channel.send_text(user_id, "收到文件，但目前没查到需要你提供发票的单子，我先不归档了。")
        elif why == "ambiguous":
            codes = ", ".join(r["doc_code"] for r in conn.execute(
                "SELECT doc_code FROM invoice_tasks WHERE user_id = ? AND status = ?",
                (user_id, STATUS_SENT)))
            channel.send_text(user_id, f"你名下有多单待收票（{codes}），麻烦把单号写在文件消息里再发一次。")
        else:
            channel.send_text(user_id, "消息里的单号和待收清单对不上，麻烦确认下单号。")
        return _finish(why)

    try:
        real_name, data = channel.download_file(file_ref)
    except Exception as e:                            # noqa: BLE001
        channel.send_text(user_id, "文件没取下来，麻烦重发一次。")
        return _finish("download_failed", error=str(e))

    path = _save_file(task["doc_code"], file_name or real_name, data, base_dir)
    conn.execute(
        "UPDATE invoice_tasks SET status = ?, received_at = ?, file_path = ?, file_name = ? WHERE id = ?",
        (STATUS_RECEIVED, _now(), path, os.path.basename(path), task["id"]))
    conn.commit()
    channel.send_text(user_id, f"发票已收到并归档：{task['doc_code']} · {os.path.basename(path)}，谢谢！")
    return _finish("saved", task_id=task["id"], doc_code=task["doc_code"], path=path)


# ──────────────────────────────────────────────────────────────────────
# 给前端「票据收集」板块用的 API
# ──────────────────────────────────────────────────────────────────────
def api(method: str, path: str, body: dict = None, *,
        conn: sqlite3.Connection = None, channel: ChatChannel = None) -> tuple[int, dict]:
    """
    极简路由，方便挂到任何框架上（见 README_MOUNT）。
      GET  /api/invoice/tasks          列表（?status=sent 可筛）
      POST /api/invoice/send           {items:[{doc_code,user_id,...}]} 登记并发消息
      POST /api/invoice/remind         催办
      POST /api/invoice/callback       聊天软件回调入口
    """
    own = conn is None
    conn = conn or connect()
    try:
        body = body or {}
        if method == "GET" and path.rstrip("/") == "/api/invoice/tasks":
            status = (body.get("status") or "").strip() or None
            return 200, {"ok": True, "items": [asdict(t) for t in list_tasks(conn, status)]}

        if method == "POST" and path.rstrip("/") == "/api/invoice/send":
            items = body.get("items") or []
            if not items:
                return 400, {"ok": False, "error": "items 不能为空"}
            created = create_tasks(conn, items)
            result = send_pending(conn, channel) if channel else {"sent": 0, "failed": [], "note": "未配置渠道"}
            return 200, {"ok": True, "created": len(created), **result}

        if method == "POST" and path.rstrip("/") == "/api/invoice/remind":
            if not channel:
                return 400, {"ok": False, "error": "未配置聊天渠道"}
            return 200, {"ok": True, **remind_overdue(conn, channel,
                                                      int(body.get("overdue_hours", 48)),
                                                      int(body.get("max_reminds", 3)))}

        if method == "POST" and path.rstrip("/") == "/api/invoice/callback":
            if not channel:
                return 400, {"ok": False, "error": "未配置聊天渠道"}
            return 200, handle_chat_event(conn, channel, body)

        return 404, {"ok": False, "error": f"no route for {method} {path}"}
    finally:
        if own:
            conn.close()


README_MOUNT = """
挂到现有后端（三选一）：

Flask：
    from invoice_collect import api, connect, HoyowaveChannel
    _conn, _ch = connect(), HoyowaveChannel()
    @app.route("/api/invoice/<path:rest>", methods=["GET", "POST"])
    def _invoice(rest):
        body = request.get_json(silent=True) or dict(request.args)
        code, obj = api(request.method, "/api/invoice/" + rest, body, conn=_conn, channel=_ch)
        return jsonify(obj), code

FastAPI：
    @app.api_route("/api/invoice/{rest:path}", methods=["GET", "POST"])
    async def _invoice(rest: str, request: Request):
        body = await request.json() if request.method == "POST" else dict(request.query_params)
        code, obj = api(request.method, "/api/invoice/" + rest, body, conn=_conn, channel=_ch)
        return JSONResponse(obj, status_code=code)

http.server：在 do_GET / do_POST 里调 api(...) 即可。

还要做的两件事：
  1. 在 hoyowave 后台把机器人的「消息事件」回调地址配成  <你的后端>/api/invoice/callback
     （内网的话需要内网穿透或走公司网关）
  2. .env 补上 HOYOWAVE_BASE_URL / HOYOWAVE_APP_ID / HOYOWAVE_APP_SECRET（或 HOYOWAVE_TOKEN）
"""


# ──────────────────────────────────────────────────────────────────────
# 自测：用 MockChannel 把整条链路跑一遍（python invoice_collect.py）
# ──────────────────────────────────────────────────────────────────────
def _selftest() -> None:
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="invoice-test-")
    conn = connect(os.path.join(tmp, "t.db"))
    files = {
        "ref-1": ("发票_PO260801.pdf", b"%PDF-1.4 fake"),
        "ref-2": ("invoice.pdf", b"%PDF-1.4 fake2"),
        "ref-3": ("x.pdf", b"%PDF-1.4 fake3"),
    }
    ch = MockChannel(files)

    def check(label, got, want):
        flag = "OK  " if got == want else "FAIL"
        print(f"  [{flag}] {label}: {got!r}" + ("" if got == want else f"  期望 {want!r}"))
        assert got == want, label

    print("\n1) 登记 + 发消息")
    create_tasks(conn, [
        {"doc_code": "PO260801", "title": "Figma 年订阅", "amount": "USD 144",
         "user_id": "u_alice", "user_name": "Alice"},
        {"doc_code": "PO260802", "title": "汉仪字库", "amount": "CNY 30000",
         "user_id": "u_bob", "user_name": "Bob"},
        {"doc_code": "PO260803", "title": "Cursor", "amount": "USD 480",
         "user_id": "u_bob", "user_name": "Bob"},
    ])
    check("重复登记被忽略", len(create_tasks(conn, [{"doc_code": "PO260801", "user_id": "u_alice"}])), 0)
    check("发出条数", send_pending(conn, ch)["sent"], 3)

    print("2) 单人单单：不写单号也能挂对")
    r = handle_chat_event(conn, ch, {"event_id": "e1", "user_id": "u_alice", "file_ref": "ref-1"}, tmp)
    check("结果", r["result"], "saved")
    check("挂到的单号", r["doc_code"], "PO260801")
    check("文件落盘", os.path.exists(r["path"]), True)

    print("3) 回调重复推送")
    check("去重", handle_chat_event(conn, ch, {"event_id": "e1", "user_id": "u_alice",
                                               "file_ref": "ref-1"}, tmp)["duplicated"], True)

    print("4) 一人多单：不写单号 → 不瞎猜，回问")
    r = handle_chat_event(conn, ch, {"event_id": "e2", "user_id": "u_bob", "file_ref": "ref-2"}, tmp)
    check("结果", r["result"], "ambiguous")
    check("回问了", "多单待收票" in ch.sent[-1][1], True)

    print("5) 一人多单：写了单号 → 挂对")
    r = handle_chat_event(conn, ch, {"event_id": "e3", "user_id": "u_bob",
                                     "file_ref": "ref-2", "text": "这是 PO260803 的"}, tmp)
    check("挂到的单号", r["doc_code"], "PO260803")

    print("6) 文件名里带单号也认")
    r = handle_chat_event(conn, ch, {"event_id": "e4", "user_id": "u_bob", "file_ref": "ref-3",
                                     "file_name": "PO260802发票.pdf"}, tmp)
    check("挂到的单号", r["doc_code"], "PO260802")

    print("7) 没有待收单的人发文件")
    check("结果", handle_chat_event(conn, ch, {"event_id": "e5", "user_id": "u_carol",
                                               "file_ref": "ref-1"}, tmp)["result"], "no_pending")

    print("8) 嵌套字段的回调格式（飞书风格）也能解析")
    create_tasks(conn, [{"doc_code": "PO260804", "user_id": "u_dave"}])
    send_pending(conn, ch)
    r = handle_chat_event(conn, ch, {
        "header": {"event_id": "e6"},
        "event": {"sender": {"sender_id": {"open_id": "u_dave"}},
                  "message": {"file_key": "ref-1", "file_name": "a.pdf"}}}, tmp)
    check("结果", r["result"], "saved")

    print("9) 催办：没到时间不催")
    create_tasks(conn, [{"doc_code": "PO260810", "user_id": "u_frank"}])   # 一直不回传的那种
    send_pending(conn, ch)
    check("跳过", remind_overdue(conn, ch, overdue_hours=48)["reminded"], 0)
    conn.execute("UPDATE invoice_tasks SET sent_at = ? WHERE status = ?",
                 ((datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S"), STATUS_SENT))
    conn.commit()
    check("超时后催", remind_overdue(conn, ch, overdue_hours=48)["reminded"] > 0, True)

    print("10) HTTP 路由")
    code, obj = api("GET", "/api/invoice/tasks", {"status": STATUS_RECEIVED}, conn=conn, channel=ch)
    check("状态码", code, 200)
    check("已收票条数", len(obj["items"]), 4)
    code, obj = api("POST", "/api/invoice/send",
                    {"items": [{"doc_code": "PO260805", "user_id": "u_eve"}]}, conn=conn, channel=ch)
    check("新建并发出", (obj["created"], obj["sent"]), (1, 1))

    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n全部通过 ✅  （渠道用的 MockChannel；换成 HoyowaveChannel 后流程不变）\n")


if __name__ == "__main__":
    _selftest()
