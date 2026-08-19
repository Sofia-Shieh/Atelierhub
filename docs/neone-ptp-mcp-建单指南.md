# 用 neone-ptp-mcp 提采购需求单（PR）· 给新会话的操作说明

把这份文件丢给**本机那个能连内网的 Claude Code 会话**，它照着做就能提单。

> **本文档的可信度分级**
> - ✅ **已验证**：REST 接口路径、请求报文、业务规则 —— 全部来自 `swboard.html` 里已经跑通的建单流程（`createPrForRow`）
> - ⚠️ **未验证**：MCP 的**工具名**。写这份文档的会话在云端容器里，被零信任网关挡在外面（HTTP 511），从未成功连上过这个 MCP。**所以下面不会出现任何我编的工具名。**

---

## 0. 环境前提

MCP 配置（Claude Code 格式，写在 `~/.claude.json` 的 user scope）：

```json
"neone-ptp-mcp": {
  "command": "oh-my-mcp",
  "args": ["connect", "https://api.agw.mihoyo.com/_mcp/neone-ptp-svc/mcp"]
}
```

必须同时满足，缺一不可：

1. 本机装了**米哈游内部**的 `oh-my-mcp`（内部源 `npm.mihoyo.com`）
   - ⚠️ 公共 npm 上有个同名的 `didrod205/oh-my-mcp`，是第三方 MCP linter，**没有 `connect` 子命令**，装了没用
2. 机器在内网 / VPN 里，且已有 SSO 登录态
   - `oh-my-mcp connect` 负责过零信任网关的登录态与设备认证；没有登录态时网关直接返 `HTTP 511 Network Authentication Required`（`x-moat-gw: 1`）

自检：

```bash
which oh-my-mcp          # 有路径才算装上
claude mcp list          # 应看到 neone-ptp-mcp ✓ connected
```

---

## 1. 第一步：先列工具，不要猜名字

**这是最重要的一步。** 不同版本的 neone-ptp-mcp 工具名可能不同（`create_pr` / `demand_add` / `add_demand` 都出现在过往文档里，彼此矛盾）。猜名字只会在错误的名字上反复试错。

连上后先做工具发现：

```
列出 neone-ptp-mcp 提供的所有工具，输出每个工具的名称、描述和入参 schema。
```

拿到真实工具清单后，按语义对号入座：

| 你要做的事 | 找这类工具 | 对应 REST（见第 3 节） |
|---|---|---|
| 建 PR 草稿头 | 名字含 `demand` + `add`/`create` | `POST /neone-ptp-svc/demand/add` |
| 写 PR 明细行 | 名字含 `demand` + `update` | `POST /neone-ptp-svc/demand/update` |
| 查预算/受益部门 | 名字含 `budget` | `POST /neone-ptp-svc/budget/selection/list` |
| 查软件 SKU | 名字含 `ledger`/`sku` | `POST /neone-sam-svc/v1/ledger/page` |
| 查申请单事由 | 名字含 `claim`/`detail` | `POST /neone-sam-svc/out/v1/claim/info/detail` |

**如果 MCP 里没有对应工具，直接走第 3 节的 REST，报文完全一样。**

---

## 2. 建单四步

顺序不能变：**必须先 `add` 拿到 PR 单号，再用这个单号 `update` 写明细行。** 一次 `add` 是建不出带明细的单子的。

```
Step 1  查 SKU        → sku_code、purchase_type_path_code   （查不到留空，最后手动选）
Step 2  匹配受益部门  → expense_department_path_code         （查不到留空，最后手动选）
Step 3  demand/add    → 拿到 PR 单号 code 和 version
Step 4  demand/update → 写入明细行（带上 Step 3 的 code 和 version）
```

Step 1 / 2 失败**不要中断**，留空继续建单，最后在采购系统里手动补选即可。Step 3 / 4 失败才算建单失败。

建完打开编辑页人工核对后再点「提交」：

```
https://neone.mihoyo.com/procurement/demand-edit/edit/{PR单号}?hide_company_name=y&hide_expense_attribute_name=y
```

> ⚠️ **不要自动送审。** 提交审批的真实接口未知，猜报文可能误提交采购单。停在草稿，人工提交。

---

## 3. 真实报文（✅ 已验证）

Base：走本机代理 `http://127.0.0.1:9003`（`server.py`），上游是 `https://api.agw.mihoyo.com`。
用 MCP 工具时只需按 schema 传等价字段，下面的值就是标准答案。

### Step 3 · demand/add

```jsonc
POST /neone-ptp-svc/demand/add
{
  "demand_head": {
    "title": "{软件名}-{领用人}-{申请单号}",
    "state": 1,
    "demand_type": 1,
    "applicant": "yuqing.xie02",
    "department_code": "0780>2136>1396",
    "currency": "CNY",
    "apply_reason": "见 4.2 文案模板",
    "expected_effect": "见 4.2 文案模板",
    "attachment_list": [],
    "source": "PTP",
    "apply_type": "common",
    "merch_execute_method": "",
    "recipient_is_obtain_packing_list": 0
  },
  "rows": []
}
```

返回：`data.code` = PR 单号，`data.version` = 版本号（Step 4 要用）。

### Step 4 · demand/update

`demand_head` 与 Step 3 相同，但**要多带** `code`（PR 单号）、`owner`、`inform_users` 等字段；顶层还要 `code` 和 `version`：

```jsonc
POST /neone-ptp-svc/demand/update
{
  "demand_head": {
    "...": "同 add，另加以下字段",
    "code": "{PR单号}",
    "owner": "yuqing.xie02",
    "inform_users": [],
    "associated_demand_code_list": [],
    "execution_cycle_start": "",
    "execution_cycle_end": "",
    "srm_ppr_code": ""
  },
  "rows": [ /* 见下方明细行 */ ],
  "code": "{PR单号}",
  "version": 1
}
```

明细行（**不要带 `code` / `row_code`**，新建行留空）：

```jsonc
{
  "demand_code": "{PR单号}",
  "type": 1,
  "purchase_type_path_code": "{Step1 查到，查不到留空}",
  "business_type_path_code": "S00016030>S00001511>S00016086",
  "quantity": "1",
  "quantity_unit_code": "U001",
  "use_date": "{明天，YYYY-MM-DD}",
  "use_date_timezone_type": 8,
  "benefit_center_code": "",
  "is_pay_advance": 2,
  "attachment_list": [],
  "description": "",
  "unit_price": "{单价}",
  "currency": "{USD / CNY}",
  "counterpart_list": [],
  "state": 1,
  "company_code": "20",
  "flow_type": 0,
  "sku_code": "{Step1 查到，查不到留空}",
  "title": "",
  "budget_space_id": "8",
  "budget_no": "{见 4.1 预算选择}",
  "channel_type_path_code": "",
  "version_code": "FBB_COM",
  "financial_tag": "BQ00001016",
  "active_name": "",
  "receipt_address_type": 1,
  "receipt_address": "",
  "receipt_warehouse_code": "",
  "software_purchase_type": "account_add",
  "subscribe_period": 1,
  "merch_usage_code": "",
  "outbound_warehouse_code": "",
  "merch_collector_list": [],
  "activity_code": "",
  "expense_department_path_code": "{Step2 匹配，查不到留空}",
  "ip_code": "",
  "budget_area_code": "",
  "collection_pr_row_code": ""
}
```

### 成功判定

- `demand/add`：`retcode === 0` 或 `code === 0`，且 `data.code` 有值
- `demand/update`：`retcode === 0`

---

## 4. 业务规则

### 4.1 预算选择（二选一）

按供应商/软件名判断是否「非代采」：

| 情况 | budget_no | 说明 |
|---|---|---|
| 命中非代采关键词 | `BZNX2026390866-3` | 非代采-Q2 |
| 其余 | `BZNX2026390868-3` | 代采-Q2 |

非代采关键词表见 `swboard.html` 的 `NON_PROXY_KEYWORDS`（Figma、Notion、Tableau、OpenAI/ChatGPT、微软、JetBrains、Zoom、Cursor 等）。

> 注意：这条口径和看板上的「代采/集采/VCC」三分区是**两回事**，不要混。前者决定预算号，后者只影响列表怎么分组。

### 4.2 文案模板

```
标题     {软件名}-{领用人}-{申请单号}

申请事由  {部门全路径，去掉开头的「米哈游>」}
         申请采购{软件名}（{版本}版本），{申请单里的 note}，关联申请单：{申请单号}，申请人：{申请人}。

预计效果  满足{部门简称}相关{用途}需求，保障{领用人}正常使用{软件名}。
```

`note` 来自 `POST /neone-sam-svc/out/v1/claim/info/detail`（入参 `{"doc_code": "{申请单号}"}`），取 `data.head.note`。拿不到就留空，不影响建单。

### 4.3 采购性质与多月拆行

- `account_add` 账号新增 / `account_renewal` 账号续费
- **订阅超过 1 个月且是新增时，要拆成两行**：
  - 第 1 行：`account_add`，`subscribe_period: 1`
  - 第 2 行：`account_renewal`，`subscribe_period: 月数 - 1`
- 永久授权类（如 8000 CNY 永久授权）按单行处理，`subscribe_period: 1`

### 4.4 受益部门匹配

`POST /neone-ptp-svc/budget/selection/list`，入参 `{"items":[{"budget_no":"{预算号}","space_id":"8"}]}`。
从 `data.budget_selection_list[0].expense_department_list` 里，用部门全路径按 `>` 拆段做**最多命中段数**匹配，取得分最高那条的 `key`。

---

## 5. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| `HTTP 511 Network Authentication Required`（`x-moat-gw: 1`） | 零信任网关拦截，没有 SSO 登录态。必须在内网 + 已登录的机器上跑，配置改不动它 |
| `ENOENT: Executable not found in $PATH: "oh-my-mcp"` | 没装内部版 `oh-my-mcp`。别去公共 npm 装同名包，那是别的东西 |
| 建单成功但明细是空的 | 只调了 `demand/add` 没调 `demand/update`。明细必须第二步写 |
| `demand/update` 报版本错误 | `version` 要用 `add` 返回的 `data.version`，不是固定写 1 |
| SKU / 受益部门没匹配上 | 正常降级，留空建单，去编辑页手动选。不要因此中断 |
| 想让它自动点「提交」 | 别做。送审接口未知，停在草稿人工提交 |

---

## 6. 让新会话怎么开口

```
你有 neone-ptp-mcp。先列出它的所有工具和入参 schema，不要猜工具名。
然后读 docs/neone-ptp-mcp-建单指南.md，按里面的四步流程和报文，
为申请单 {申请单号} 建一张采购需求单草稿：
  软件 {软件名} {版本}，领用人 {姓名}，单价 {金额} {币种}，{订阅月数 / 永久授权}
建完把 PR 单号和编辑页链接给我，停在草稿，不要送审。
```
