/*!
 * sku-local-fill.js —— 「智能填充」本地规则兜底补丁
 *
 * 作用：后端 /api/sku/lookup 不可用时（缺 ANTHROPIC_API_KEY 返回 503、5xx、网络错误、
 *       或返回 0 条），自动改用本地规则解析你粘贴的文字，把结果按后端原本的响应格式
 *       交回给页面 —— 页面自己的填表逻辑照常工作，不需要改动它一行代码。
 *
 * 用法二选一：
 *   1) 临时试一下：F12 → Console → 粘贴本文件全部内容 → 回车 → 再点「识别并填入表格」
 *   2) 永久生效：把本文件放到 board 页面同目录，在 HTML 的 </body> 之前加一行
 *        <script src="sku-local-fill.js"></script>
 *      （或者直接把本文件内容包在 <script> ... </script> 里贴进去）
 *
 * 只拦截 /api/sku/lookup，其它请求原样放行；AI 恢复可用后自动优先走 AI。
 */
(function () {
  "use strict";

  const SKU_CURRENCY_COUNTRY = { CNY: "中国", USD: "美国", EUR: "欧元区" };

  // ── 本地规则解析（不依赖 AI / ANTHROPIC_API_KEY）────────────────────
  // AI 不可用时用它兜底：能从整句、逗号分隔、Excel 粘贴的多列里
  // 抽出 软件名 / 版本 / 厂商 / 单价 / 币种，抽不出的字段留空由人补。
  const SKU_CURRENCY_TOKENS = [
    [/US\s*\$|\$|USD|美元|美金/i, "USD"],
    [/€|EUR|欧元/i, "EUR"],
    [/￥|¥|RMB|CNY|人民币|元/i, "CNY"],
  ];
  const SKU_MONEY_PATTERNS = [
    /(?:US\s*\$|\$|€|￥|¥)\s*([\d][\d,]*(?:\.\d+)?)/i,
    /(?:USD|CNY|RMB|EUR)\s*([\d][\d,]*(?:\.\d+)?)/i,
    /([\d][\d,]*(?:\.\d+)?)\s*(?:元|美元|美金|欧元|USD|CNY|RMB|EUR)/i,
  ];
  // 长的排前面，避免 "Pro" 抢在 "Professional" 前面命中
  const SKU_VERSION_KEYWORDS = [
    "单一游戏项目内嵌+全媒体永久授权", "单一游戏项目全媒体永久授权", "全媒体永久授权",
    "永久授权", "商业授权", "买断制", "买断",
    "年度订阅", "年订阅", "月订阅", "按年订阅", "按月订阅", "包年", "包月", "年费", "订阅",
    "企业版", "专业版", "旗舰版", "标准版", "基础版", "教育版", "个人版", "团队版",
    "Enterprise", "Professional", "Business", "Premium", "Ultimate", "Standard",
    "Education", "Individual", "Starter", "Desktop", "Studio", "Team", "Plus", "Pro",
  ].sort((a, b) => b.length - a.length);
  // 只放能确定的对应关系；认不出的厂商宁可留空让人填，不瞎猜
  const SKU_VENDOR_HINTS = [
    [/figma/i,                                   "Figma"],
    [/cursor|anysphere/i,                        "Anysphere"],
    [/chat\s?gpt|openai/i,                       "OpenAI"],
    [/claude|anthropic/i,                        "Anthropic"],
    [/adobe|photoshop|illustrator|premiere|after\s?effects|substance/i, "Adobe"],
    [/jetbrains|intellij|pycharm|webstorm|clion|goland|rider/i,         "JetBrains"],
    [/microsoft|office\s?365|visual\s?studio|windows/i,                 "Microsoft"],
    [/autodesk|maya|3ds\s?max|autocad/i,         "Autodesk"],
    [/unity/i,                                   "Unity"],
    [/unreal|epic\s?games/i,                     "Epic Games"],
    [/notion/i,                                  "Notion Labs"],
    [/blender/i,                                 "Blender Foundation"],
    [/汉仪/,                                     "汉仪字库", true],
    [/方正/,                                     "方正字库", true],
    [/造字工房/,                                 "造字工房", true],
    [/文鼎/,                                     "文鼎科技", true],
    [/蒙纳|monotype/i,                           "Monotype", true],
  ];
  const SKU_CORP_SUFFIX = /(Inc\.?|Ltd\.?|LLC|GmbH|Corp\.?|Technologies|公司|科技|字库|信息技术)$/i;
  const SKU_HEADER_RE = /^(软件名称|软件|名称|版本|版本\/授权类型|授权类型|厂商|供应商|预估单价|单价|价格|币种|汇率基准国|采购方式|备注|说明|name|version|vendor|price|currency|remark)$/i;

  function skuEscapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // ASCII 关键词加词边界，避免 "Pro" 命中 "Product"
  function skuKeywordRe(kw) {
    const esc = skuEscapeRe(kw);
    return /[A-Za-z]/.test(kw) ? new RegExp(`(?<![A-Za-z])${esc}(?![A-Za-z])`, "i") : new RegExp(esc);
  }

  function skuMoney(text) {
    for (const re of SKU_MONEY_PATTERNS) {
      const m = text.match(re);
      if (!m) continue;
      const price = parseFloat(m[1].replace(/,/g, ""));
      if (!Number.isFinite(price)) continue;
      let currency = "";
      for (const [cre, code] of SKU_CURRENCY_TOKENS) { if (cre.test(m[0])) { currency = code; break; } }
      return { price, currency, raw: m[0] };
    }
    return { price: "", currency: "", raw: "" };
  }

  // 最多取两个版本词拼在一起（如 "Professional 年订阅"）；
  // "Pro / Studio" 这种并列则拆成多行
  function skuVersions(text) {
    const hits = [];
    let rest = text;
    for (const kw of SKU_VERSION_KEYWORDS) {
      if (hits.length >= 2) break;
      const re = skuKeywordRe(kw);
      const m = rest.match(re);
      if (!m) continue;
      hits.push({ text: kw, index: text.search(re) });
      rest = rest.replace(re, " ");
    }
    hits.sort((a, b) => a.index - b.index);
    const raws = hits.map((h) => h.text);
    const pair = text.match(/([A-Za-z一-龥]+)\s*[\/／、]\s*([A-Za-z一-龥]+)/);
    if (pair && raws.length >= 2) {
      const canon = (s) => SKU_VERSION_KEYWORDS.find((k) => k.toLowerCase() === s.toLowerCase());
      const a = canon(pair[1]), b = canon(pair[2]);
      if (a && b) return { list: [a, b], raws: [pair[1], pair[2]], rest: text.replace(pair[0], " ") };
    }
    return { list: raws.length ? [raws.join(" ")] : [], raws, rest };
  }

  function skuVendor(text) {
    for (const [re, vendor, isFont] of SKU_VENDOR_HINTS) {
      if (re.test(text)) return { vendor, isFont: !!isFont };
    }
    const t = text.trim();
    if (t && SKU_CORP_SUFFIX.test(t)) return { vendor: t, isFont: /字库/.test(t) };
    return { vendor: "", isFont: false };
  }

  // 清掉分隔符和残留标点；顺带去掉与厂商完全重合的整词（去完为空则保留）
  function skuCleanName(text, vendorName) {
    let s = String(text || "")
      .replace(/[，,；;|\t]+/g, " ")
      .replace(/[（(【]\s*[)）】]/g, " ")
      .replace(/\s{2,}/g, " ")
      .trim()
      .replace(/^[·\-—、\/／]+|[·\-—、\/／]+$/g, "")
      .trim();
    if (vendorName) {
      const re = new RegExp(`(^|\\s)${skuEscapeRe(vendorName)}(\\s|$)`, "i");
      if (re.test(s) && s.replace(re, " ").trim()) s = s.replace(re, " ");
    }
    return s.replace(/\s{2,}/g, " ").trim();
  }

  // 一行 → 一条或多条建议
  function skuParseRecord(raw) {
    // 先抹掉千分位逗号，否则 "￥50,000" 会在按逗号分列时被拆成 "￥50" 和 "000"
    const line = String(raw).replace(/(?<=\d),(?=\d{3}(?:\D|$))/g, "").trim();
    if (!line) return [];
    const fields = line.split(/\t+|[，,；;|]+/).map((f) => f.trim()).filter(Boolean);
    if (fields.length && fields.every((f) => SKU_HEADER_RE.test(f))) return []; // 表头行

    const acc = { name: "", vendor: "", price: "", currency: "", isFont: false };
    const extras = [];
    let versions = [];

    fields.forEach((field) => {
      const money = skuMoney(field);
      // 整个字段就是价格（允许 "$144/人" 这类后缀）
      const moneyOnly = money.price !== ""
        && field.replace(money.raw, "").replace(/[\/／每人年月位seat\s]/gi, "").length === 0;
      if (moneyOnly) {
        if (acc.price === "") { acc.price = money.price; acc.currency = money.currency; }
        return;
      }
      // 多列里的裸数字（"30"）基本就是单价
      if (fields.length > 1 && acc.price === "" && /^\d+(\.\d+)?$/.test(field)) {
        acc.price = parseFloat(field);
        return;
      }
      let text = field;
      if (money.price !== "") {
        if (acc.price === "") { acc.price = money.price; acc.currency = money.currency; }
        text = text.replace(money.raw, " ");
      }
      const ver = skuVersions(text);
      const useVersion = ver.list.length && !versions.length;
      if (useVersion) {
        versions = ver.list;
        text = ver.rest;
        ver.raws.forEach((r) => { text = text.replace(r, " "); });
      }

      const vend = skuVendor(field);
      if (vend.vendor && !acc.vendor) { acc.vendor = vend.vendor; acc.isFont = vend.isFont; }

      const rest = skuCleanName(text, vend.vendor);
      if (!rest) return;                       // 该字段只是厂商/版本/价格
      const isVendorField = vend.vendor
        && rest.replace(/\s/g, "").toLowerCase() === vend.vendor.replace(/\s/g, "").toLowerCase();
      if (isVendorField && acc.name) return;   // 这一段就是厂商，别再塞进备注
      if (!acc.name) acc.name = rest; else extras.push(rest);
    });

    // 没版本、没厂商、没价格的纯中文长句，多半是随手写的一句话而不是软件名
    const weak = !versions.length && !acc.vendor && acc.price === "";
    if (weak && (/[。？！]/.test(line) || (line.length >= 10 && !/[A-Za-z0-9]/.test(line)))) return [];

    let name = acc.name;
    if (name && acc.isFont && !/[【】]/.test(name)) name = `【${name}】`; // 字体按台账约定加【】
    if (!name && !acc.vendor) return [];

    return (versions.length ? versions : [""]).map((v) => ({
      name,
      version: v,
      vendor: acc.vendor,
      price: acc.price,
      currency: acc.currency,
      country: acc.currency ? SKU_CURRENCY_COUNTRY[acc.currency] : "",
      note: extras.join(" "),
    }));
  }

  function skuLocalParse(input) {
    const out = [];
    String(input).split(/\r?\n/).forEach((line) => { out.push(...skuParseRecord(line)); });
    const seen = new Set();
    return out.filter((s) => {
      const key = `${s.name}|${s.version}|${s.vendor}`.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }
  // ── 把解析结果包成后端那样的响应 ─────────────────────────────────
  // 不确定你后端用的是哪套字段名，这里把常见别名都带上，页面取哪个都能拿到值
  function toApiRow(r) {
    const row = {
      name: r.name, sku_name: r.name, software: r.name, software_name: r.name, "软件名称": r.name,
      version: r.version, sw_version: r.version, license: r.version, license_type: r.version, "版本": r.version,
      vendor: r.vendor, manufacturer: r.vendor, supplier: r.vendor, "厂商": r.vendor,
      price: r.price, unit_price: r.price, estimate_price: r.price, estimated_price: r.price, "预估单价": r.price,
      currency: r.currency, "币种": r.currency,
      country: r.country, exchange_country: r.country, "汇率基准国": r.country,
      note: r.note, remark: r.note, "备注": r.note,
      purchase_type: "普通", "采购方式": "普通",
      _source: "local-rule",
    };
    if (/^https?:\/\//i.test(r.note || "")) { row.url = r.note; row.official_url = r.note; }
    return row;
  }

  function toApiPayload(rows) {
    const list = rows.map(toApiRow);
    const data = list.slice();
    data.list = list; data.suggestions = list; data.items = list; // 兼容 data.list 这类取法
    return {
      ok: true, success: true, code: 0, message: "本地规则解析", msg: "本地规则解析",
      suggestions: list, items: list, rows: list, results: list, list: list, data: data,
      source: "local-rule",
    };
  }

  function fakeResponse(obj) {
    return {
      ok: true, status: 200, statusText: "OK", redirected: false, type: "basic", url: "", bodyUsed: false,
      headers: new Headers({ "Content-Type": "application/json" }),
      clone() { return fakeResponse(obj); },
      json: async () => obj,
      text: async () => JSON.stringify(obj),
    };
  }

  // ── 从请求里取出用户粘贴的原文 ───────────────────────────────────
  async function readInput(reqInput, init) {
    let body = init && init.body;
    if (!body && reqInput && typeof reqInput.clone === "function") {
      try { body = await reqInput.clone().text(); } catch (e) { /* ignore */ }
    }
    if (body && typeof body === "object" && typeof body.get === "function") { // FormData
      for (const k of ["input", "text", "content", "raw", "query", "q"]) {
        const v = body.get(k);
        if (v) return String(v);
      }
    }
    if (typeof body === "string") {
      try {
        const obj = JSON.parse(body);
        for (const k of ["input", "text", "content", "raw", "query", "q", "prompt"]) {
          if (obj && obj[k]) return String(obj[k]);
        }
      } catch (e) { return body; }
    }
    // 兜底：直接读页面上有内容的那个 textarea
    const ta = [...document.querySelectorAll("textarea")].find((t) => t.value.trim());
    return ta ? ta.value : "";
  }

  // ── 拦截 /api/sku/lookup ─────────────────────────────────────────
  const LOOKUP_RE = /\/api\/sku\/lookup(\?|$)/;
  const origFetch = window.fetch.bind(window);

  window.fetch = async function (reqInput, init) {
    const url = typeof reqInput === "string" ? reqInput : (reqInput && reqInput.url) || "";
    if (!LOOKUP_RE.test(String(url))) return origFetch(reqInput, init);

    const text = await readInput(reqInput, init);
    let resp = null, reason = "";

    try {
      resp = await origFetch(reqInput, init);
      if (!resp.ok) {
        reason = "HTTP " + resp.status;
      } else {
        const data = await resp.clone().json().catch(() => null);
        if (!data || data.ok === false || data.success === false) {
          reason = (data && (data.error || data.message)) || "后端未返回结果";
        } else {
          const arr = data.suggestions || data.items || data.rows || data.list || data.results || data.data;
          if (!Array.isArray(arr) || !arr.length) reason = "后端返回 0 条";
        }
      }
    } catch (e) {
      reason = e && e.message ? e.message : "网络错误";
    }

    if (!reason) return resp;               // AI 正常，原样返回

    const rows = skuLocalParse(text);
    if (!rows.length) {
      console.warn("[sku-local-fill] AI 不可用（" + reason + "），本地规则也没认出内容");
      return resp || fakeResponse({ ok: false, error: reason });
    }
    console.warn("[sku-local-fill] AI 不可用（" + reason + "），已用本地规则解析出 " + rows.length + " 行", rows);
    return fakeResponse(toApiPayload(rows));
  };

  window.skuLocalParse = skuLocalParse;     // 方便在 Console 里单独试：skuLocalParse("figma pro 年订阅 $144")
  console.info("[sku-local-fill] 已启用：AI 不可用时自动改用本地规则解析");
})();
