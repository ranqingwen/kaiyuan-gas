// kaiyuan-gas-card — 凯源燃气 Lovelace 卡片
// 仓库：https://github.com/ranqingwen/kaiyuan-gas
// 许可：MIT
// 本文件随集成一起安装，放在 custom_components/kaiyuan_gas/www/ 里，
// 由集成自动注册为前端资源：/kaiyuan_gas/www/kaiyuan-gas-card.js
// 无需手工放到 config/www/。若资源列表里没有自动出现，可手动添加该 URL（类型: JavaScript 模块）。
// 卡片类型:
//       type: custom:kaiyuan-gas-card
//       entity: 指向集成主实体 (如 sensor.kai_yuan_ran_qi_zhang_wu_ji_lu)
//       (只有一个账号时可以不填实体, 卡片会自动选中; 多个账号请在卡片编辑器里选账号)
// 可选配置: hide: ["ladder", "cumulative", ...] 关掉首页某一块（默认全显示）
// 旧写法 show_ladder: false 仍然有效（等价于 hide 里有 "ladder"）
// lit 库三重兜底: HA 内置 -> jsdelivr -> unpkg, 任何一个可用即可
let __lit;
try {
  __lit = await import("lit");
} catch (e1) {
  try {
    __lit = await import("https://cdn.jsdelivr.net/npm/lit@2.7.0/index.js?module");
  } catch (e2) {
    __lit = await import("https://unpkg.com/lit@2.7.0/index.js?module");
  }
}
const { LitElement, html, svg, css } = __lit;

const ORANGE = "#f57c00";   // 用气量柱 (燃气主题橙)
const BLUE = "#1677d2";     // 燃气费折线

// 首页可以被单独关掉的内容块：key 存进 config.hide，默认全部显示。
// 卡片编辑器的勾选框和卡片渲染共用这一份清单，改一处两边同步。
const VIS_ITEMS = [
  ["cumulative", "累计费用"],
  ["ladder", "阶梯用量"],
  ["ladder_price", "阶梯单价"],
  ["ladder_now", "当前档位"],
  ["prev_reading", "上期表读数"],
  ["reading", "本期表读数"],
  ["usage", "本期用气量"],
  ["read_date", "抄表日期"],
  ["bill_period", "计费周期"],
  ["name", "户名"],
  ["address", "用气地址"],
  ["last_pay", "最近缴费"],
];

class KaiyuanGasCard extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      config: { attribute: false },
      _view: { state: true },
      _open: { state: true },
    };
  }

  setConfig(config) {
    // entity 可以不填：只有一个账号时自动选；有多个账号又没指定时，
    // 卡片会显示「请选择账号」而不是随便挑一个，避免看错表。
    this.config = config || {};
    this._view = "main";
  }

  // 找出所有属于本集成的「账务记录」实体（多账号时每台设备一个）
  static findEntities(hass) {
    const out = [];
    if (!hass || !hass.states) return out;
    for (const eid of Object.keys(hass.states)) {
      const s = hass.states[eid];
      const a = s && s.attributes;
      if (!a || !("usage_list" in a) || !("cons_no" in a)) continue;
      const who = a.customer_name || a.mobile || a.cons_no || eid;
      const acc = a.cons_no ? ` · ${a.cons_no}` : "";
      out.push({ entity_id: eid, label: `${who}${acc}` });
    }
    return out.sort((x, y) => String(x.label).localeCompare(String(y.label), "zh"));
  }

  getCardSize() {
    return 9;
  }

  // 添加卡片时的默认配置：只有一个账号就直接带上，多个账号让用户在编辑器里选
  static getStubConfig(hass) {
    const list = KaiyuanGasCard.findEntities(hass);
    return list.length === 1 ? { entity: list[0].entity_id } : {};
  }

  static getConfigElement() {
    return document.createElement("kaiyuan-gas-card-editor");
  }

  static get styles() {
    return css`
      :host { --kg-orange: #f57c00; --kg-deep: #e65100; }
      .card {
        background: var(--card-background-color, #fff);
        border-radius: 16px; overflow: hidden;
        box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.12));
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
        color: var(--primary-text-color, #222);
      }
      /* ===== 头部 ===== */
      .hd {
        display: flex; align-items: center; gap: 12px; padding: 14px 16px;
      }
      .hd .logo {
        width: 44px; height: 44px; border-radius: 12px; flex: none;
        background: linear-gradient(135deg, #ffa726, var(--kg-deep));
        display: flex; align-items: center; justify-content: center;
        font-size: 24px; color: #fff;
      }
      .hd .t { flex: 1; min-width: 0; }
      .hd .t .n { font-size: 17px; font-weight: 700; }
      .hd .t .s { font-size: 12px; color: var(--secondary-text-color, #888); margin-top: 2px; }
      .hd .badge {
        flex: none; font-size: 12px; border-radius: 999px; padding: 4px 12px;
      }
      .badge.ok { background: rgba(52,168,83,.15); color: #2e9e57; }
      .badge.bad { background: rgba(229,57,53,.15); color: #e53935; }
      /* ===== 大数字区 ===== */
      .balance { padding: 2px 16px 12px; }
      .balance .lab { font-size: 13px; color: var(--secondary-text-color, #888); }
      .balance .big { font-size: 30px; font-weight: 700; margin-top: 2px; }
      .balance .top { display: flex; align-items: flex-end; gap: 10px; }
      .balance .top .tag { margin-left: 0; margin-bottom: 6px; }
      .balance .top .cum { margin-left: auto; font-size: 18px; font-weight: 600; line-height: 1.1; color: var(--primary-text-color, #333); white-space: nowrap; text-align: right; }
      .balance .top .cum i { display: block; font-size: 11px; font-weight: 400; font-style: normal; color: var(--secondary-text-color, #888); margin-bottom: 2px; letter-spacing: .2px; }
      .balance .big .u { font-size: 14px; font-weight: 500; color: var(--secondary-text-color,#888); margin-left: 3px; }
      .balance .sub { font-size: 12px; color: var(--secondary-text-color, #888); margin-top: 4px; }
      .balance .sub b { color: var(--primary-text-color, #333); font-weight: 600; }
      .balance .sub b.warn { color: #e53935; }
      .balance .sub i.mut { font-style: normal; font-size: 11px; color: var(--secondary-text-color, #999); }
      /* ===== 磁贴 ===== */
      .tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 2px 16px 12px; }
      .tiles.three { grid-template-columns: repeat(3, 1fr); }
      .tiles.three .tile { padding: 10px 10px; }
      .tiles.three .tile .v { font-size: 17px; }
      /* 只勾了一个磁贴时别只占半行 */
      .tiles.one { grid-template-columns: 1fr; }
      .tile {
        /* 中性灰半透明：亮/暗主题下都只是「比卡片底色浅一点」，不跟着主题跳色 */
        background: rgba(127, 127, 127, .08);
        border-radius: 12px; padding: 10px 12px;
      }
      .tile .k { font-size: 12px; color: var(--secondary-text-color, #888); }
      .tile .v { font-size: 19px; font-weight: 700; margin-top: 3px; }
      .tile .v .u { font-size: 12px; font-weight: 400; color: var(--secondary-text-color,#888); margin-left: 2px; }
      /* ===== 阶梯 ===== */
      .ladder { margin: 2px 16px 12px; background: rgba(127, 127, 127, .08); border-radius: 12px; padding: 12px; }
      .ladder .row1 { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: 13px; font-weight: 600; }
      .ladder .row1 .left { color: var(--primary-text-color); flex: none; }
      .ladder .row1 .right { color: var(--secondary-text-color,#888); font-weight: 400; font-size: 11px; text-align: right; }
      .ladder .bar { display: flex; gap: 3px; margin-top: 10px; }
      .ladder .bar .seg {
        flex: 1; position: relative; height: 24px; border-radius: 7px; overflow: hidden;
        display: flex; align-items: center; justify-content: center;
        /* 比外层再深一档，段落边界才看得清 */
        background: rgba(127, 127, 127, .14);
      }
      .ladder .bar .fill { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg,#ffa726,var(--kg-deep)); opacity: .26; }
      .ladder .bar .txt { position: relative; text-align: center; line-height: 1.45; padding: 0 3px; }
      .ladder .bar .txt b { display: block; font-size: 10px; font-weight: 500; color: var(--secondary-text-color,#888); white-space: nowrap; }
      .ladder .prices { display: flex; gap: 3px; margin-top: 6px; }
      .ladder .prices .p { flex: 1; text-align: center; font-size: 10px; color: var(--secondary-text-color,#888); white-space: nowrap; }
      .ladder .note { font-size: 11px; color: var(--secondary-text-color,#999); margin-top: 10px; }
      /* ===== 计费信息条（气价 / 垃圾费 / 结算方式） ===== */
      .fees { display: flex; margin: 2px 16px 12px; background: var(--secondary-background-color,#f5f6f8); border-radius: 12px; padding: 11px 6px; }
      .fees .fb { flex: 1; text-align: center; position: relative; }
      .fees .fb + .fb::before {
        content: ""; position: absolute; left: 0; top: 4px; bottom: 4px;
        width: 1px; background: var(--divider-color, #e4e7eb);
      }
      .fees .fb .k { font-size: 11px; color: var(--secondary-text-color, #888); }
      .fees .fb .v { font-size: 15px; font-weight: 700; margin-top: 3px; color: var(--primary-text-color); }
      .fees .fb .v .u { display: inline-block; font-size: 11px; font-weight: 400; color: var(--secondary-text-color,#888); margin-left: 2px; }
      .fees .fb .v .mut { display: inline-block; font-style: normal; font-size: 11px; font-weight: 400; color: var(--secondary-text-color,#888); }
      .fees .fb .v.sm { font-size: 13px; font-weight: 600; }
      /* 只剩「违约金」一项时: 左右分列的单行条 */
      .fees.one { padding: 10px 14px; }
      .fees.one .fb { display: flex; align-items: baseline; justify-content: space-between; text-align: left; }
      .fees.one .fb .v { margin-top: 0; }
      /* ===== 明细行 ===== */
      .info { padding: 0 16px; }
      .info .row {
        display: flex; justify-content: space-between; align-items: center; font-size: 13px;
        padding: 8px 0; border-bottom: 1px solid var(--divider-color, #f0f0f0);
      }
      .info .row:last-child { border-bottom: none; }
      .info .row .k { color: var(--secondary-text-color, #888); flex: none; }
      .info .row .v { text-align: right; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 62%; }
      .info .row .v .mut { font-style: normal; font-weight: 400; font-size: 11px; color: var(--secondary-text-color, #999); }
      .info .row .v.warn { color: #e53935; }
      /* 结清状态小标签 */
      .tag {
        display: inline-block; font-size: 11px; font-weight: 600;
        border-radius: 999px; padding: 2px 8px; margin-left: 4px;
      }
      .tag.ok { background: rgba(52,168,83,.15); color: #2e9e57; }
      .tag.wait { background: rgba(245,124,0,.15); color: var(--kg-deep, #e65100); }
      /* ===== 按钮区 ===== */
      .actions { display: flex; gap: 10px; padding: 14px 16px 6px; }
      .btn {
        flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
        border: none; border-radius: 12px; padding: 11px 0;
        font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity .15s, transform .1s;
      }
      .btn:active { transform: scale(.98); }
      /* 两个入口（用气明细 / 缴费记录）统一：橙色半透明底 + 同色描边，
         有块感但透出卡片底色，不像实心渐变那样抢眼 */
      .btn.pri {
        background: rgba(245, 124, 0, .16);
        color: var(--kg-orange);
        border: 1.5px solid var(--kg-orange);
      }
      /* ===== 页脚 ===== */
      .ft {
        display: flex; justify-content: space-between; align-items: center;
        padding: 10px 16px 14px; font-size: 11px; color: var(--secondary-text-color, #999);
      }
      .ft .rf {
        border: 1px solid var(--divider-color,#ddd); border-radius: 999px; background: transparent;
        color: var(--secondary-text-color,#888); font-size: 11px; padding: 4px 12px; cursor: pointer;
      }
      .ft .rf:hover { color: var(--kg-orange); border-color: var(--kg-orange); }
      .ft .rf.off { color: #e53935; border-color: rgba(229,57,53,.4); }
      /* ===== 子页面 ===== */
      .sub .title {
        font-size: 16px; font-weight: 700; padding: 14px 16px 4px;
        display: flex; justify-content: space-between; align-items: center;
      }
      .back { font-size: 13px; color: var(--kg-orange); cursor: pointer; background: none; border: none; }
      .panel { margin: 8px 16px; background: rgba(127, 127, 127, .08); border-radius: 12px; padding: 12px; }
      .panel .pt { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
      .panel .pu { font-size: 11px; color: var(--secondary-text-color,#999); font-weight: 400; margin-left: 4px; }
      svg text { font-family: sans-serif; }
      .empty { text-align: center; color: var(--secondary-text-color,#999); padding: 20px; font-size: 13px; }
      table { width: 100%; border-collapse: collapse; font-size: 12px; }
      th, td { padding: 7px 6px; text-align: center; }
      th { color: var(--secondary-text-color,#888); font-weight: 500; border-bottom: 1px solid var(--divider-color,#e5e5e5); }
      td { border-bottom: 1px solid var(--divider-color,#f2f2f2); }
      tr:last-child td { border-bottom: none; }
      td.tl, th.tl { text-align: left; }
      td.tr, th.tr { text-align: right; }
      .paid { color: #2e9e57; }
      .unpaid { color: #e53935; }
      .amt-cell { color: var(--kg-orange); font-weight: 700; white-space: nowrap; }
      /* 缴费记录表: 金额列向左收进, 方式列左对齐 */
      .paytable th, .paytable td { padding: 7px 6px; }
      .paytable .amt-cell, .paytable th.amt-th { padding-right: 30px; }
      .paytable .meth { text-align: left; }
      .ptable { font-size: 11.5px; }
      .ptable th, .ptable td { padding: 8px 4px; white-space: nowrap; }
      .ptable .meth { text-align: left; }
      .ptable .mlabel { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 88px; display: inline-block; vertical-align: middle; }
      .ptable .total-th, .ptable .total-cell { font-weight: 600; color: var(--kg-orange, #f57c00); padding-right: 4px; }
      .ptable .meth { padding-left: 22px; }
      /* 缴费渠道小徽标 */
      .ch {
        display: inline-flex; align-items: center; justify-content: center;
        width: 18px; height: 18px; border-radius: 4px; font-size: 11px;
        color: #fff; flex: none;
      }
      .chwrap { display: inline-flex; align-items: center; gap: 5px; vertical-align: middle; }
      .ch.wx { background: #09bb07; }
      .ch.alipay { background: #1677ff; }
      .ch.bank { background: #5b7a9d; }
      .ch.cash { background: #c0392b; }
      .ch.other { background: #8e8e93; }
      /* 缴费记录行 (费用构成展开里仍在用) */
      .payrow { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--divider-color,#f2f2f2); }
      .payrow:last-child { border-bottom: none; }
      .payrow .ic {
        width: 34px; height: 34px; border-radius: 10px; flex: none;
        background: rgba(245,124,0,.12); display: flex; align-items: center; justify-content: center; font-size: 16px;
      }
      .payrow .m { flex: 1; min-width: 0; }
      .payrow .m .a { font-size: 13px; font-weight: 600; }
      .payrow .m .b { font-size: 11px; color: var(--secondary-text-color,#888); margin-top: 1px; }
      .payrow .amt { font-size: 15px; font-weight: 700; color: var(--kg-orange); flex: none; }
      /* ===== 月份手风琴 (费用构成) ===== */
      tr.mrow { cursor: pointer; }
      tr.mrow:hover td { background: rgba(127, 127, 127, .10); }
      .utable { font-size: 11.5px; }
      .utable th, .utable td { padding: 7px 3px; white-space: nowrap; }
      .utable td.tl { white-space: normal; }
      .utable td.tr, .utable th.tr { text-align: right; }
      tr.mrow .chev { float: right; color: var(--secondary-text-color, #888); font-weight: 400; }
      tr.xrow td {
        background: rgba(127, 127, 127, .12);
        padding: 8px 12px 10px; text-align: left;
      }
      .xrow .payrow { padding: 7px 0; }
      .xrow .payrow .ic { width: 28px; height: 28px; font-size: 13px; border-radius: 8px; }
      .xrow .payrow .m .a { font-size: 12px; }
      .xrow .payrow .amt { font-size: 13px; }
      .xrow .xsum { text-align: right; font-size: 12px; font-weight: 700; padding-top: 4px; }
      .xrow .xsum .est { font-weight: 400; font-size: 10.5px; color: var(--secondary-text-color, #999); margin-left: 6px; }
      /* ===== 选择账号占位 ===== */
      .pick { padding: 2px 16px 16px; font-size: 13px; color: var(--primary-text-color, #333); }
      .pick-t { color: var(--secondary-text-color, #888); font-size: 12px; margin: 8px 0 6px; }
      .pick ul { margin: 0; padding-left: 18px; }
      .pick li { margin: 4px 0; display: flex; gap: 10px; flex-wrap: wrap; }
      .pick li span { color: var(--secondary-text-color, #888); font-size: 12px; }
      .pick code, .pick pre {
        font-family: var(--code-font-family, monospace); font-size: 12px;
        background: var(--secondary-background-color, #f5f6f8); border-radius: 6px;
      }
      .pick code { padding: 1px 5px; }
      .pick pre { padding: 8px 10px; margin: 0; overflow-x: auto; white-space: pre-wrap; }
    `;
  }

  // ============ 数据访问 ============
  _state() {
    if (!this.hass || !this.hass.states) return undefined;
    const want = this.config && this.config.entity;
    if (want) {
      const st = this.hass.states[want];
      // 老配置可能写死以前的「账务记录」或「累计用量」实体（1.0.3 / 1.0.11 两次迁移），
      // 那些实体已经不在了或者不带明细，就退回自动查找，别让卡片整块空白。
      if (st && st.attributes && "usage_list" in st.attributes) return st;
    }
    // 没配 entity：只有一个账号就自动用它；多个账号时不要乱猜，交给界面提示
    const list = KaiyuanGasCard.findEntities(this.hass);
    if (list.length === 1) return this.hass.states[list[0].entity_id];
    // 多账号且老配置的实体已不存在：按 HA 给重名实体加的后缀（_2 / _3）找回同一台设备
    if (want && list.length > 1) {
      const suffix = String(want).match(/(_\d+)$/);
      if (suffix) {
        const hit = list.filter((it) => String(it.entity_id).endsWith(suffix[1]));
        if (hit.length === 1) return this.hass.states[hit[0].entity_id];
      }
    }
    if (!want && list.length === 0) {
      for (const eid of Object.keys(this.hass.states)) {
        const s = this.hass.states[eid];
        if (s && s.attributes && "usage_list" in s.attributes && "cons_no" in s.attributes) {
          return s;
        }
      }
    }
    return undefined;
  }

  _attr(name) {
    const s = this._state();
    return s && s.attributes ? s.attributes[name] : undefined;
  }

  _fmt(v, unit) {
    if (v === undefined || v === null || v === "") return "-";
    // 抄表日接口有时给 202610 / 20261015 这种纯数字，转成 2026-10 / 2026-10-15
    const s = String(v);
    if (/^\d{6}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4)}`;
    if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`;
    return unit ? `${v} ${unit}` : `${v}`;
  }

  // 「2026-11-02 12:51:58」→「11-02 12:51」：页脚只留到分，别把秒也堆上去
  _stamp(v) {
    const m = String(v || "").match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!m) return String(v || "");
    return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  }

  // 时间字符串排序用的毫秒数（只比大小，不做时区换算，两边口径一致就行）
  _tsNum(v) {
    const m = String(v || "").match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
    if (!m) return 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }

  // 页脚要显示「最新刷新时间」：刷新时间（last_refresh）、数据时间（updated_at）、
  // 缓存落盘时间（cache_updated_at）取最新的那个。集成在「在线失败退回缓存」时会把
  // 刷新时间压回数据的真实时间，正常情况下三者一致；取最大值能兼容旧版后端里这几个
  // 字段更新不同步的情况（以前卡片只认 updated_at，会出现实体在刷新、页脚不动）。
  _refreshStamp() {
    const list = ["last_refresh", "updated_at", "cache_updated_at"]
      .map(k => this._attr(k))
      .filter(v => v);
    if (!list.length) return undefined;
    return list.reduce((a, b) => (this._tsNum(b) > this._tsNum(a) ? b : a));
  }

  _money(v, digits = 2) {
    if (v === undefined || v === null || v === "") return "-";
    const n = Number(v);
    return isNaN(n) ? `${v}` : n.toFixed(digits);
  }

  // 单价按实际小数位显示：2.196 就是 2.196，2.19 就是 2.19，不强行补零
  _price(v) {
    if (v === undefined || v === null || v === "") return "-";
    const n = Number(v);
    if (!isFinite(n)) return `${v}`;
    return String(parseFloat(n.toFixed(4)));
  }

  // 2026-09 → 2026年09月
  _monthCn(m) {
    const s = String(m || "");
    const hit = s.match(/^(\d{4})-(\d{1,2})$/);
    if (!hit) return s || "-";
    return `${hit[1]}年${hit[2].padStart(2, "0")}月`;
  }

  // 「最近缴费」一行：2026/10/1 ¥71.68
  // 公众号缴费记录只有日期、没有时刻，所以这里也只用日期，别拼出 0:00:00 这种假时间。
  _lastPayText() {
    const at = this._attr("last_pay_time") || this._attr("last_pay_at");
    const amt = this._attr("last_pay_amount");
    if (!at) return "-";
    let text = String(at);
    const m = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (m) {
      text = `${m[1]}/${Number(m[2])}/${Number(m[3])}`;
    }
    const money = amt === undefined || amt === null ? "" : ` ¥${this._money(amt)}`;
    return `${text}${money}`;
  }

  _connected() {
    const st = this._state();
    return !!(st && st.state !== "unavailable" && st.state !== "unknown");
  }

  // 缴费渠道小徽标 (微信=绿色气泡图标, 支付宝/银行/现金各有标识)
  _channelBadge(channel) {
    const c = String(channel || "");
    if (c.includes("微信")) {
      return html`<span class="ch wx"><svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
        <path fill="#fff" d="M9.2 3.5C5.3 3.5 2 6.1 2 9.4c0 1.9 1.1 3.5 2.8 4.6l-.8 2.4 2.8-1.5c.5.1 1 .2 1.6.2h.4c-.2-.6-.3-1.2-.3-1.8 0-3.1 2.9-5.6 6.5-5.6h.4C14.7 5.2 12.2 3.5 9.2 3.5z"/>
        <path fill="#fff" d="M22 13.4c0-2.7-2.6-4.9-5.8-4.9s-5.8 2.2-5.8 4.9 2.6 4.9 5.8 4.9c.7 0 1.3-.1 1.9-.3l2.4 1.3-.7-2.1C21.1 16.3 22 14.9 22 13.4z"/>
        <circle cx="6.8" cy="8.2" r="0.9" fill="#09bb07"/>
        <circle cx="11.4" cy="8.2" r="0.9" fill="#09bb07"/>
        <circle cx="14.6" cy="12.6" r="0.75" fill="#09bb07"/>
        <circle cx="17.8" cy="12.6" r="0.75" fill="#09bb07"/>
      </svg></span>`;
    }
    if (c.includes("支付宝")) return html`<span class="ch alipay">支</span>`;
    if (c.includes("银") || c.includes("卡")) return html`<span class="ch bank">卡</span>`;
    if (c.includes("现金")) return html`<span class="ch cash">¥</span>`;
    return html`<span class="ch other">付</span>`;
  }

  // 阶梯阈值: ladder 值形如 "0-500" / "501-1000" / "1001" (年度 m³)
  // 阶梯数据：集成已按配置算好（used / tier / limit / remaining / tiers）
  _ladderData() {
    if (!this._show("ladder")) return null;
    const l = this._attr("ladder");
    if (!l || !Array.isArray(l.tiers) || !l.tiers.length) return null;
    const t1 = Number(l.tiers[0].to) || 0;
    const t2 = Number(l.tiers[1] && l.tiers[1].to) || t1;
    const used = Number(l.used) || 0;
    const mid = Math.max(t2 - t1, 1);
    const segs = [1, 1, 1];  // 进度条三等分，与水务卡一致
    const fills = [
      t1 > 0 ? Math.min(used, t1) / t1 * 100 : 0,
      used > t1 ? Math.min(used - t1, mid) / mid * 100 : 0,
      used > t2 ? 100 : 0,
    ];
    const cn = ["一", "二", "三", "四"];
    const tiers = l.tiers.map((t, i) => ({
      name: t.name || `第${cn[i] || i + 1}档`,
      price: t.price !== undefined && t.price !== null ? Number(t.price) : null,
      range: i < l.tiers.length - 1
        ? `${this._fmt(t.from)}-${this._fmt(t.to)}m³`
        : `${this._fmt(t.from)}m³及以上`,
    }));
    const idx = Math.min(Math.max(Number(l.tier) || 1, 1), tiers.length);
    return {
      used,
      tiers,
      segs,
      fills,
      limit: l.limit !== undefined && l.limit !== null ? l.limit : t1,
      remaining: l.remaining !== undefined && l.remaining !== null
        ? l.remaining : Math.max(t1 - used, 0),
      tier_name: l.tier_name || tiers[idx - 1].name,
    };
  }

  _go(view) { this._view = view; }

  // 某一块要不要显示：默认显示，config.hide 里点名了就隐藏；
  // 老的 show_ladder: false 等同于 hide 里有 "ladder"；
  // 老的 hide: [readings]（上/本期读数合在一个开关里）等同于两个都隐藏
  _show(key) {
    const cfg = this.config || {};
    if (key === "ladder" && cfg.show_ladder === false) return false;
    if (Array.isArray(cfg.hide) && cfg.hide.indexOf(key) >= 0) return false;
    if ((key === "prev_reading" || key === "reading") &&
        Array.isArray(cfg.hide) && cfg.hide.indexOf("readings") >= 0) return false;
    return true;
  }

  _refresh() {
    if (!this.hass) return;
    const st = this._state();
    const eid = st ? st.entity_id : this.config.entity;
    this.hass.callService("homeassistant", "update_entity", { entity_id: eid });
  }

  // ============ 主视图 ============
  _renderMain() {
    const ok = this._connected();
    const account = this._attr("cons_no") || "-";
    const balance = this._attr("balance");
    const arrearage = this._attr("arrearage");
    const unpaidCount = this._attr("unpaid_count");
    const usage = this._attr("usage");
    const reading = this._attr("reading");
    const readDate = this._attr("reading_date");
    const name = this._attr("customer_name") || "-";
    const addr = this._attr("address") || "-";
    const mobile = this._attr("mobile") || "-";
    const lastPay = this._attr("last_pay_time");
    const lastPayAmt = this._attr("last_pay_amount");
    const lastPayGas = this._attr("last_pay_gas");
    const lastPayGarbage = this._attr("last_pay_garbage");
    // 右上角「累计费用」的口径：默认全部（缓存里所有账期），可切成本年（只算今年账期）
    const cumScope = (this.config && this.config.cum_scope) === "year" ? "year" : "all";
    const cumFee = this._attr(cumScope === "year" ? "cumulative_fee_year" : "cumulative_fee");
    const cumLabel = cumScope === "year" ? "本年累计" : "累计费用";
    const lastPayPenalty = this._attr("last_pay_penalty");
    const org = "凯源燃气";
    const updated = this._stamp(this._refreshStamp());
    const ul = this._attr("usage_list") || [];
    const month = ul[0] && ul[0].month ? ul[0].month : "-";
    const status = ul[0] ? ul[0].status : null;

    // 阶梯进度由集成算好，卡片只负责画
    const garbageFee = this._attr("garbage_fee");
    const settleWay = this._attr("settle_way");
    const prevReading = this._attr("prev_reading");
    const receivable = this._attr("receivable");
    const received = this._attr("received");
    const settled = this._attr("settled");
    const totalFee = this._attr("total_fee");
    const penaltyStart = this._attr("penalty_start");
    const penaltyVal = this._attr("penalty");
    const src = this._attr("data_source");
    const offline = !!src && String(src).indexOf("缓存") >= 0;

    // 本期账单 = 最近账期 气费 + 垃圾费 + 违约金（取不到就退回最近一次缴费）
    const cur = ul[0] || {};
    const curFee = cur.fee !== undefined && cur.fee !== null ? Number(cur.fee) : null;
    const curGarbage = Number(cur.garbage_fee) || 0;
    const curPenalty = Number(cur.penalty) || 0;
    // 顺序：本期账单三期费用 → 集成算好的应缴总额（未结清时等于欠费）→ 最近一次缴费
    const billAmt = curFee !== null
      ? curFee + curGarbage + curPenalty
      : (totalFee !== undefined && totalFee !== null ? Number(totalFee) : lastPayAmt);
    // 标题带上账期（就是账单月份），下面的明细里就不用再单列一行了
    const bigLabel = month && /^\d{4}-\d{2}$/.test(String(month))
      ? `本期账单(${month})`
      : "本期账单";
    const bigVal = this._money(billAmt);
    const gasInBill = curFee !== null ? curFee : lastPayGas;
    const garbageInBill = curFee !== null ? curGarbage : lastPayGarbage;
    const penaltyInBill = curFee !== null ? curPenalty : lastPayPenalty;

    // 阶梯（集成按配置算好，卡片只管画）
    const ladder = this._ladderData();

    // 本期账单的构成（和水务一样，只列真正有数的项）
    const billParts = [];
    if (curFee !== null) {
      billParts.push(`气费 ¥${this._money(gasInBill !== undefined && gasInBill !== null ? gasInBill : 0)}`);
      if (Number(garbageInBill) > 0) billParts.push(`垃圾费 ¥${this._money(garbageInBill)}`);
      if (Number(penaltyInBill) > 0) billParts.push(`违约金 ¥${this._money(penaltyInBill)}`);
    } else if (lastPayAmt !== undefined && lastPayAmt !== null) {
      billParts.push(`最近缴费 ¥${this._money(lastPayAmt)}`);
    }

    // 顶部小格子按开关拼：上期读数 / 本期读数 / 本期用气量，三个各自独立开关
    const tiles = [];
    if (this._show("prev_reading")) {
      tiles.push({ k: "上期表读数", v: this._fmt(prevReading) });
    }
    if (this._show("reading")) {
      tiles.push({ k: "本期表读数", v: this._fmt(reading) });
    }
    if (this._show("usage")) tiles.push({ k: "本期用气量", v: this._fmt(usage) });

    // 明细区同理：每一行都能单独关掉，全关掉时整块不占位
    const billPeriod = this._attr("bill_period");
    const infoRows = [];
    if (this._show("read_date")) {
      infoRows.push(html`<div class="row"><span class="k">抄表日期</span><span class="v">${this._fmt(readDate)}</span></div>`);
    }
    if (this._show("bill_period") && billPeriod) {
      infoRows.push(html`<div class="row"><span class="k">计费周期</span><span class="v">${billPeriod}</span></div>`);
    }
    if (this._show("name")) {
      infoRows.push(html`<div class="row"><span class="k">户名</span><span class="v">${name}</span></div>`);
    }
    if (this._show("address")) {
      infoRows.push(html`<div class="row"><span class="k">用气地址</span><span class="v">${addr}</span></div>`);
    }
    if (this._show("last_pay")) {
      infoRows.push(html`<div class="row"><span class="k">最近缴费</span><span class="v">${this._lastPayText()}</span></div>`);
    }
    if (Number(penaltyVal) > 0) {
      infoRows.push(html`<div class="row"><span class="k">违约金起算</span><span class="v warn">${penaltyStart || "-"}</span></div>`);
    }

    return html`
      <div class="card">
        <div class="hd">
          <div class="logo">🔥</div>
          <div class="t">
            <div class="n">凯源燃气</div>
            <div class="s">${account ? "户号 " + account : ""}</div>
          </div>
          <div class="badge ${ok ? "ok" : "bad"}">${ok ? "正常" : "异常"}</div>
        </div>

        <div class="balance">
          <div class="lab">${bigLabel}</div>
          <div class="top">
            <div class="big">¥${bigVal}</div>
            ${settled === true ? html`<span class="tag ok">已结清</span>`
              : settled === false ? html`<span class="tag wait">待结清</span>`
              : (status ? html`<span class="tag">${status}</span>` : "")}
            ${this._show("cumulative") && Number(cumFee) > 0
              ? html`<span class="cum"><i>${cumLabel}</i>¥${this._money(cumFee)}</span>` : ""}
          </div>
          <div class="sub">
            ${billParts.length ? html`${billParts.join(" + ")}` : ""}
            ${arrearage !== undefined && arrearage !== null && Number(arrearage) > 0
              ? html`${billParts.length ? " · " : ""}<b class="warn">待缴费 ¥${this._money(arrearage)}</b>${unpaidCount ? html`（${unpaidCount} 期未缴）` : ""}` : ""}
          </div>
        </div>

        ${tiles.length ? html`
        <div class="tiles ${tiles.length >= 3 ? "three" : (tiles.length === 1 ? "one" : "")}">
          ${tiles.map(t => html`<div class="tile"><div class="k">${t.k}</div><div class="v">${t.v}<span class="u">m³</span></div></div>`)}
        </div>` : ""}

        ${ladder ? html`
        <div class="ladder">
          <div class="row1">
            <span class="left">阶梯用量</span>
          </div>
          <div class="bar">
            ${ladder.tiers.map((t, i) => html`
              <div class="seg">
                <div class="fill" style="width:${ladder.fills[i]}%"></div>
                <div class="txt"><b>${t.name} ${t.range}</b></div>
              </div>`)}
          </div>
          ${this._show("ladder_price") ? html`
          <div class="prices">
            ${ladder.tiers.map(t => html`<div class="p">${t.price !== null ? html`单价 ¥${this._price(t.price)}` : ""}</div>`)}
          </div>` : ""}
          ${this._show("ladder_now") ? html`
          <div class="note">当前档位：${ladder.tier_name} · 累计已用 ${this._fmt(ladder.used)} m³ · 剩余 ${this._fmt(ladder.remaining)} m³</div>` : ""}
        </div>` : ""}

        ${infoRows.length ? html`<div class="info">${infoRows}</div>` : ""}

        <div class="actions">
          <button class="btn pri" @click=${() => this._go("usage")}>📊 用气明细</button>
          <button class="btn pri" @click=${() => this._go("payment")}>💳 缴费记录</button>
        </div>

        <div class="ft">
          <span>${org}${updated ? " · 最新刷新时间 " + updated : (src ? " · " + src : "")}</span>
          <button class="rf ${offline ? "off" : ""}" @click=${() => this._refresh()}>${offline ? "⟳ 离线" : "⟳ 刷新"}</button>
        </div>
      </div>
    `;
  }

  // ============ 用气明细 (图表 + 表格) ============
  _chartData() {
    const ul = (this._attr("usage_list") || []).slice(0, 12).filter(r => r.month);
    const asc = ul.slice().reverse();
    return asc.map(r => ({
      label: (String(r.month).match(/-(\d{1,2})$/) || [])[1]
        ? String(r.month).match(/-(\d{1,2})$/)[1] + "月"
        : r.month,
      usage: Number(r.usage) || 0,
      fee: Number(r.fee) || 0,
    }));
  }

  // 组合图: 柱状=用气量(m³, 左轴), 折线=燃气费(元, 右轴)
  _comboChart(data) {
    if (!data.length) return html`<div class="empty">暂无数据</div>`;
    const W = 340, H = 210, padL = 34, padR = 36, padT = 20, padB = 44;
    const iw = W - padL - padR, ih = H - padT - padB;
    const maxU = Math.max(...data.map(d => d.usage), 1) * 1.2;
    const maxF = Math.max(...data.map(d => d.fee), 1) * 1.2;
    const step = iw / data.length;
    const bw = Math.min(step * 0.45, 22);
    const yU = v => padT + ih - (v / maxU) * ih;
    const yF = v => padT + ih - (v / maxF) * ih;
    const ticks = 4;
    const grid = [];
    for (let i = 0; i <= ticks; i++) {
      const vU = maxU * i / ticks, vF = maxF * i / ticks;
      const y = yU(vU);
      grid.push(svg`<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--divider-color,#ddd)" stroke-width="1" stroke-dasharray="${i === 0 ? "" : "3 3"}"/>
        <text x="${padL - 4}" y="${y + 3}" font-size="9" fill="${ORANGE}" text-anchor="end">${Math.round(vU)}</text>
        <text x="${W - padR + 4}" y="${y + 3}" font-size="9" fill="${BLUE}" text-anchor="start">${Math.round(vF)}</text>`);
    }
    const bars = data.map((d, i) => {
      const bh = Math.max((d.usage / maxU) * ih, 1.5);
      const x = padL + step * i + (step - bw) / 2;
      const cx = padL + step * i + step / 2;
      const topY = padT + ih - bh;
      const inside = bh > 16;
      // 月份多时隔一个显示，避免横轴标签叠在一起
      const showLabel = data.length <= 8 || i % 2 === 0 || i === data.length - 1;
      return svg`<rect x="${x}" y="${topY}" width="${bw}" height="${bh}" rx="2.5" fill="${ORANGE}" opacity="0.85"/>
        <text x="${cx}" y="${inside ? topY + 17 : topY - 4}" font-size="9" font-weight="600" fill="${inside ? "#ffffff" : ORANGE}" text-anchor="middle">${d.usage}</text>
        ${showLabel ? svg`<text x="${cx}" y="${H - 26}" font-size="9.5" fill="var(--secondary-text-color,#888)" text-anchor="middle">${d.label}</text>` : ""}`;
    });
    const pts = data.map((d, i) => `${padL + step * i + step / 2},${yF(d.fee)}`).join(" ");
    const dots = data.map((d, i) => svg`<circle cx="${padL + step * i + step / 2}" cy="${yF(d.fee)}" r="2.5" fill="var(--card-background-color,#fff)" stroke="${BLUE}" stroke-width="1.5"/>
      <text x="${padL + step * i + step / 2}" y="${yF(d.fee) - 7}" font-size="9" font-weight="600" fill="${BLUE}" text-anchor="middle">${Number(d.fee).toFixed(0)}</text>`);
    return html`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">
      ${grid}
      ${bars}
      <polyline points="${pts}" fill="none" stroke="${BLUE}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      ${dots}
      <g transform="translate(${W / 2 - 70}, ${H - 7})">
        <rect x="0" y="-8" width="10" height="10" rx="2" fill="${ORANGE}"/>
        <text x="14" y="1" font-size="10" fill="var(--secondary-text-color,#888)">用气量 m³</text>
        <line x1="86" y1="-3" x2="106" y2="-3" stroke="${BLUE}" stroke-width="1.5"/>
        <circle cx="96" cy="-3" r="2.5" fill="var(--card-background-color,#fff)" stroke="${BLUE}" stroke-width="1.5"/>
        <text x="110" y="1" font-size="10" fill="var(--secondary-text-color,#888)">燃气费 元</text>
      </g>
    </svg>`;
  }

  _renderUsage() {
    const data = this._chartData();
    const ul = this._attr("usage_list") || [];

    return html`
      <div class="card sub">
        <div class="title">用气明细<button class="back" @click=${() => this._go("main")}>← 返回</button></div>

        <div class="panel">
          <div class="pt">用气量 / 燃气费</div>
          ${this._comboChart(data)}
        </div>

        <div class="panel" style="padding:4px 12px 8px">
          <table class="utable">
            <thead><tr><th class="tl">月份</th><th>起数</th><th>止数</th><th>用气量</th><th>燃气费</th><th class="tr">状态</th></tr></thead>
            <tbody>
              ${ul.map(r => html`
                <tr class="mrow" @click=${() => this._toggleMonth(r)}>
                  <td class="tl">${this._monthCn(r.month)}<span class="chev">${this._open === r.month ? "▾" : "›"}</span></td>
                  <td>${this._fmt(r.prev_reading)}</td>
                  <td>${this._fmt(r.reading)}</td>
                  <td>${this._fmt(r.usage)}</td>
                  <td>${r.fee !== undefined && r.fee !== null ? "¥" + this._money(r.fee) : "-"}</td>
                  <td class="tr ${r.status === "已缴费" ? "paid" : (r.status ? "unpaid" : "")}">${r.status || "-"}</td>
                </tr>
                ${this._open === r.month ? this._renderMonthDetail(r) : ""}
              `)}
            </tbody>
          </table>
          ${ul.length === 0 ? html`<div class="empty">暂无数据</div>` : ""}
          <div class="empty" style="padding:8px 0 6px">点击月份展开该期费用构成, 再点一次收起</div>
        </div>
      </div>
    `;
  }

  // ============ 月份费用构成 (手风琴, 展开在该月与上月之间) ============
  _toggleMonth(r) {
    this._open = this._open === r.month ? null : r.month;
  }

  _renderMonthDetail(r) {
    const byMonth = this._attr("fee_items_by_month") || {};
    const items = byMonth[r.month] || [];
    const total = items.reduce((s, f) => s + (Number(f.money) || 0), 0);
    const period = r.bill_period || "";
    // 未结清的账期公众号还没生成明细，金额是集成按「单价 × 用量」算的
    const est = !!r.estimated;
    return html`<tr class="xrow"><td colspan="6">
      ${period ? html`<div class="payrow">
        <div class="ic">🧾</div>
        <div class="m">
          <div class="a">计费周期</div>
          <div class="b">${period}</div>
        </div>
      </div>` : ""}
      ${items.length && total > 0 ? html`
        ${items.map(f => {
          // 与水务卡同款：标题「燃气费 · 第一档」，副行「单价 ¥2.196/m³ × 21 m³」
          const price = f.price !== undefined && f.price !== null ? f.price : null;
          const usage = f.usage !== undefined && f.usage !== null ? f.usage : null;
          const fixed = /垃圾|服务费/.test(String(f.item || ""));
          const sub = fixed
            ? "固定费用 · 每月随燃气费代扣"
            : (price !== null && usage !== null
              ? `单价 ¥${this._price(price)}/m³ × ${this._fmt(usage)} m³`
              : `${this._monthCn(r.month)} 账期 · ${r.status || "-"}`);
          return html`
          <div class="payrow">
            <div class="ic">🧾</div>
            <div class="m">
              <div class="a">${f.item}${f.ladder ? " · " + f.ladder : ""}</div>
              <div class="b">${sub}</div>
            </div>
            <div class="amt">¥${this._money(f.money)}</div>
          </div>`;
        })}
        <div class="xsum">合计 ¥${this._money(total)}${est ? html`<span class="est">未结清 · 按当前单价估算</span>` : ""}</div>
      ` : html`<div class="empty">${
          r.status === "未缴费"
            ? "本期还没结清，公众号暂未生成这期的费用明细"
            : "该期费用明细暂未获取，稍后刷新再试"
        }</div>`}
    </td></tr>`;
  }

  // ============ 缴费记录（按缴费日合并成一行：燃气费 / 垃圾费 / 缴费方式）============
  _renderPayment() {
    const days = this._payDays();
    return html`
      <div class="card sub">
        <div class="title">缴费记录${days.length ? html`<span class="pu" style="font-size:12px;color:var(--secondary-text-color,#888);font-weight:400">${days.length} 次</span>` : ""}<button class="back" @click=${() => this._go("main")}>← 返回</button></div>
        <div class="panel" style="padding:4px 12px 8px">
          ${days.length === 0 ? html`<div class="empty">公众号没有返回缴费记录（可能这户一直没在网厅缴过费）</div>` : html`
            <table class="paytable ptable">
              <thead><tr><th class="tl">缴费日期</th><th class="tr">燃气费</th><th class="tr">垃圾费</th><th class="tr total-th">合计</th><th class="meth">缴费方式</th></tr></thead>
              <tbody>
                ${days.map(r => {
                  // 优先显示结算/支付渠道（如「支付宝代扣」），渠道为空时退回「第三方」
                  const label = r.settle || r.channel || "-";
                  const hasGas = r.gas !== undefined && r.gas !== null;
                  const hasGarb = r.garbage !== undefined && r.garbage !== null && Number(r.garbage) > 0;
                  const hasPen = r.penalty !== undefined && r.penalty !== null && Number(r.penalty) > 0;
                  return html`
                  <tr>
                    <td class="tl">${r.date || "-"}${hasPen ? html`<div style="font-size:10px;color:#e53935;margin-top:2px">含违约金 ¥${this._money(r.penalty)}</div>` : ""}</td>
                    <td class="tr">${hasGas ? "¥" + this._money(r.gas) : "-"}</td>
                    <td class="tr">${hasGarb ? "¥" + this._money(r.garbage) : "-"}</td>
                    <td class="tr total-cell">${r.total !== undefined && r.total !== null ? "¥" + this._money(r.total) : "-"}</td>
                    <td class="meth"><span class="chwrap">${this._channelBadge(label)}<span class="mlabel">${label}</span></span></td>
                  </tr>`;
                })}
              </tbody>
            </table>
          `}
        </div>
      </div>
    `;
  }

  // 缴费日合并列表：优先用集成给的 payment_days，没有就按日期把 payment_list 现算一遍
  _payDays() {
    const days = this._attr("payment_days");
    if (Array.isArray(days) && days.length) return days;
    const list = this._attr("payment_list") || [];
    const map = new Map();
    for (const r of list) {
      const key = String(r.time || "").slice(0, 10);
      if (!key) continue;
      const cur = map.get(key) || { date: key, gas: 0, garbage: 0, penalty: 0, channel: "", settle: "" };
      const amount = Number(r.amount) || 0;
      const items = r.items || [];
      let rest = amount;
      let hasItem = false;
      for (const it of items) {
        const name = String(it.item || "");
        const money = Number(it.money) || 0;
        if (name.indexOf("垃圾") >= 0) { cur.garbage += money; rest -= money; hasItem = true; }
        else if (name.indexOf("违约金") >= 0 || name.indexOf("滞纳") >= 0) { cur.penalty += money; rest -= money; hasItem = true; }
      }
      if (!hasItem || rest > 0.005) cur.gas += rest;
      if (!cur.channel && r.channel) cur.channel = r.channel;
      if (!cur.settle && r.settle) cur.settle = r.settle;
      map.set(key, cur);
    }
    return [...map.values()]
      .map(r => {
        const gas = Math.round(r.gas * 100) / 100;
        const garbage = Math.round(r.garbage * 100) / 100;
        const penalty = Math.round(r.penalty * 100) / 100;
        return { ...r, gas, garbage, penalty, total: Math.round((gas + garbage + penalty) * 100) / 100 };
      })
      .sort((a, b) => (a.date < b.date ? 1 : -1));
  }

  render() {
    if (!this.config || !this.hass) return html``;
    const list = KaiyuanGasCard.findEntities(this.hass);
    if (!this.config.entity && list.length !== 1) return this._renderPick(list);
    if (this._view === "usage") return this._renderUsage();
    if (this._view === "payment") return this._renderPayment();
    return this._renderMain();
  }

  // 没指定账号且账号不止一个：提示去选，不要随便挑一个显示
  _renderPick(list) {
    const items = list.length
      ? list.map((it) => html`<li><code>${it.entity_id}</code><span>${it.label}</span></li>`)
      : [html`<li><span>还没有找到凯源燃气实体，请先添加集成。</span></li>`];
    return html`
      <div class="card">
        <div class="hd">
          <div class="logo">🔥</div>
          <div class="t">
            <div class="n">凯源燃气</div>
            <div class="s">请为这张卡片选择一个账号</div>
          </div>
        </div>
        <div class="pick">
          <div class="pick-t">在卡片编辑里把 <b>实体</b> 指向下面任意一个「账务记录」实体：</div>
          <ul>${items}</ul>
          <div class="pick-t">YAML 写法：</div>
          <pre>type: custom:kaiyuan-gas-card
entity: ${list[0] ? list[0].entity_id : "sensor.kai_yuan_ran_qi_zhang_wu_ji_lu"}</pre>
        </div>
      </div>
    `;
  }
}

// ------------------------------------------------------------------ 卡片编辑器
// 多账号时最关键的就是「这张卡显示哪个账号」，所以给一个账号下拉框 + 阶梯开关，
// 免得用户去翻实体 ID。
class KaiyuanGasCardEditor extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
    };
  }

  setConfig(config) {
    this._config = config || {};
  }

  static get styles() {
    return css`
      .row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
      .row label { flex: none; width: 92px; font-size: 13px; color: var(--primary-text-color, #333); }
      .row select { flex: 1; min-width: 0; padding: 7px 8px; border-radius: 8px;
        border: 1px solid var(--divider-color, #d8dce2);
        background: var(--card-background-color, #fff); color: var(--primary-text-color, #333);
        font-size: 13px; }
      .row input[type="checkbox"] { width: 18px; height: 18px; }
      .hint { font-size: 12px; color: var(--secondary-text-color, #888); padding-top: 4px; }
      .hint code { background: var(--secondary-background-color, #f5f6f8); padding: 1px 4px; border-radius: 4px; }
      /* 显示内容：两列勾选框，窄面板里也能看全 */
      .sec { font-size: 13px; font-weight: 600; padding: 10px 0 2px;
        color: var(--primary-text-color, #333); }
      .vis { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 10px; padding: 2px 0 4px; }
      .vis label { display: flex; align-items: center; gap: 6px; width: auto;
        font-size: 13px; color: var(--primary-text-color, #333); cursor: pointer; }
      .vis input[type="checkbox"] { width: 17px; height: 17px; margin: 0; }
    `;
  }

  _emit(cfg) {
    this._config = cfg;
    this.dispatchEvent(
      new CustomEvent("config-changed", { detail: { config: cfg }, bubbles: true, composed: true })
    );
  }

  _changed(entity) {
    const cfg = { ...(this._config || {}) };
    if (entity) cfg.entity = entity;
    else delete cfg.entity;
    this._emit(cfg);
  }

  // 勾选/取消某一块：没勾就进 hide 列表，全勾上时把 hide 整个删掉（配置保持干净）
  _toggle(key, on) {
    const cfg = { ...(this._config || {}) };
    const hide = new Set(Array.isArray(cfg.hide) ? cfg.hide : []);
    if (on) hide.delete(key);
    else hide.add(key);
    // 老配置里把上/本期读数合成一个 "readings"，一旦按新开关单独勾选就作废它
    if (key === "prev_reading" || key === "reading") hide.delete("readings");
    if (hide.size) cfg.hide = Array.from(hide);
    else delete cfg.hide;
    // 老的 show_ladder 与新的 hide 会打架，动过勾选就以新的为准
    delete cfg.show_ladder;
    this._emit(cfg);
  }

  // 右上角「累计费用」的口径：year = 本年（只算今年账期），all = 全部（默认）
  _setScope(scope) {
    const cfg = { ...(this._config || {}) };
    if (scope === "year") cfg.cum_scope = "year";
    else delete cfg.cum_scope;
    this._emit(cfg);
  }

  _visible(key) {
    const cfg = this._config || {};
    if (key === "ladder" && cfg.show_ladder === false) return false;
    if (Array.isArray(cfg.hide) && cfg.hide.indexOf(key) >= 0) return false;
    if ((key === "prev_reading" || key === "reading") &&
        Array.isArray(cfg.hide) && cfg.hide.indexOf("readings") >= 0) return false;
    return true;
  }

  render() {
    if (!this.hass) return html``;
    const cur = (this._config && this._config.entity) || "";
    const scope = (this._config && this._config.cum_scope) === "year" ? "year" : "all";
    const list = KaiyuanGasCard.findEntities(this.hass);
    const known = list.some((it) => it.entity_id === cur);
    return html`
      <div class="row">
        <label>账号</label>
        <select @change=${(ev) => this._changed(ev.target.value)}>
          ${list.length > 1
            ? html`<option value="" ?selected=${!cur}>请选择账号…</option>`
            : ""}
          ${list.map(
            (it) => html`<option value=${it.entity_id} ?selected=${it.entity_id === cur}>
              ${it.label}
            </option>`
          )}
          ${cur && !known ? html`<option value=${cur} selected>${cur}</option>` : ""}
        </select>
      </div>

      <div class="sec">显示内容</div>
      <div class="vis">
        ${VIS_ITEMS.map(([key, label]) => html`
          <label>
            <input
              type="checkbox"
              .checked=${this._visible(key)}
              @change=${(ev) => this._toggle(key, ev.target.checked)}
            />
            <span>${label}</span>
          </label>`)}
      </div>
      <div class="hint">默认全部勾选；取消勾选的块不会出现在首页卡片上。</div>

      ${this._visible("cumulative")
        ? html`<div class="row">
            <label>累计费用</label>
            <select @change=${(ev) => this._setScope(ev.target.value)}>
              <option value="year" ?selected=${scope === "year"}>本年累计</option>
              <option value="all" ?selected=${scope === "all"}>全部累计</option>
            </select>
          </div>
          <div class="hint">右上角那个金额的算法：本年 = 只加今年账期；全部 = 所有账期加起来（含往年）。</div>`
        : html``}

      ${list.length > 1
        ? html`<div class="hint">
            检测到 ${list.length} 个账号，每条集成对应一个账号，一张卡片选一个即可。
          </div>`
        : html``}
    `;
  }
}

// 防止重复加载（例如集成已自动注册、用户又在资源里手动加了一次）
if (!customElements.get("kaiyuan-gas-card-editor")) {
  customElements.define("kaiyuan-gas-card-editor", KaiyuanGasCardEditor);
}

if (!customElements.get("kaiyuan-gas-card")) {
  customElements.define("kaiyuan-gas-card", KaiyuanGasCard);

  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "kaiyuan-gas-card",
    name: "凯源燃气卡片",
    description: "凯源燃气余额 / 阶梯用气 / 用气明细图表 / 缴费记录",
  });
}
