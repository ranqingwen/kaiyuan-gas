"""数据协调器：在线拉取 -> 合并进本地缓存 -> 输出给实体与卡片。

流程
----
1. 启动 / 定时刷新时先读配置目录下的 ``kaiyuan_gas_info_<户号>.json``；
2. 在线拉到新数据后，按「账期」「缴费日」合并进缓存（只增不删），再写回文件；
3. 实体属性与卡片统一由缓存文件派生，因此在线失败时看到的是文件里的历史数据；
4. 只有「文件不存在 + 在线失败」才把实体置为不可用。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LADDER_ORDER = ("一", "二", "三", "四")  # 档位序号的汉字写法，下标 = 档位 - 1

from . import cache
from .api import (
    KaiyuanGasAuthError,
    KaiyuanGasClient,
    KaiyuanGasConnectionError,
    KaiyuanGasError,
    KaiyuanGasLockedError,
    deep_get,
)
from .const import (
    CONF_CONS_NO,
    CONF_LADDER1,
    CONF_LADDER2,
    CONF_MOBILE,
    CONF_PASSWORD,
    CONF_PRICE1,
    CONF_PRICE2,
    CONF_PRICE3,
    CONF_REFRESH_HOURS,
    DEFAULT_LADDER1,
    DEFAULT_LADDER2,
    DEFAULT_PRICE1,
    DEFAULT_PRICE2,
    DEFAULT_PRICE3,
    DEFAULT_REFRESH_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# 接口字段名候选（凯源这套后端不同模块命名并不统一：同一份数据在抄表接口里是
# startNum，在账单接口里又可能是 scds），解析时统一按这几组去匹配。
KEYS_READING = (
    "readNum", "curReadNum", "thisReadNum", "endNum", "endReadNum", "curNum", "thisNum",
    "meterNum", "reading", "currentNum", "endReading", "currentRead", "meterReading",
    "nowReadNum", "bdz", "num",
)
KEYS_PREV = (
    "lastReadNum", "prevReadNum", "preReadNum", "lastNum", "previousNum", "startNum",
    "beginNum", "beginReadNum", "startReadNum", "lastReading", "preReading",
    "startReading", "scds",
)
KEYS_USAGE = (
    "gasNum", "gasAmount", "useGas", "useNum", "dosage", "monthNum", "consumption",
    "gasUse", "useAmount", "gasVolume", "qty", "yql",
)
KEYS_FEE = (
    # feeRecord（费用记录）用的是 payableGasFee / arreasAll，ncBill 用 gasAmt / totalAmt，
    # 这两个名字以前没覆盖，导致「燃气费」一列全是「-」
    "payableGasFee", "payableFee", "payableMoney", "shouldGasFee",
    "shouldPay", "shouldMoney", "gasFee", "gasAmt", "fee", "money", "payable", "amount",
    "payMoney", "billAmount", "billFee", "totalFee", "totalMoney", "totalAmt", "receivable",
    "shouldAmount", "yjje", "amt",
    # 未结清账期常常只给「欠费金额」，没有单独的燃气费字段
    "arreasAll", "arreas", "oweMoney", "oweFee", "oweAmount", "unpaidFee", "unpaidMoney",
    "shouldFee", "feeShould", "arrearMoney", "totalOwe",
)
KEYS_PAY_AMOUNT = (
    # payMentRecord（缴费记录）用 paymentAmount 总额、gasFee 燃气费、lateFee 违约金
    "paymentAmount", "payMoney", "payAmount", "money", "amount", "tradeAmount", "payAmt",
    "totalMoney", "sumMoney", "jfje", "skje", "gasFee",
)
KEYS_PAY_DATE = (
    # paymentDate 可能是毫秒时间戳，取值后统一转成 YYYY-MM-DD
    "paymentDate", "payDate", "payTime", "payDatetime", "tradeTime", "tradeDate",
    "settleTime", "date", "createTime", "jfsj", "jfDate",
)
KEYS_STATUS = ("status", "settleFlag", "payFlag", "state", "settleStatus", "isSettle")


def to_float(value: Any) -> float | None:
    """把 '¥12.30'、'12.3元'、12.3 统一成 float。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    for token in ("¥", "￥", "元", "m³", "M3", "方", " "):
        text = text.replace(token, "")
    try:
        return float(text)
    except ValueError:
        return None


def to_text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return str(value).strip()


# 只在「用户/客户」这类对象的顶层取字段。
# 之前用 deep_get 递归找 userName/name，会一路钻进别的子结构里，把「抄表方式」
# 的 name（自助抄表）当成户名，户名和地址就全错了。
_USER_CONTAINERS = ("userInfo", "customer", "custInfo", "consInfo", "user", "account")


def pick_field(obj: Any, *keys: str) -> Any:
    """先看顶层，再看几个已知的用户容器，绝不深挖任意字段。"""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return value
    for name in _USER_CONTAINERS:
        inner = obj.get(name)
        if isinstance(inner, dict):
            for key in keys:
                value = inner.get(key)
                if value not in (None, "", [], {}):
                    return value
    return None


# 登录名常是 u84742444 这种，不是户名；地址里的 *** 是接口自己打的码，保留
_LOGIN_NAME_RE = re.compile(r"^[uU]\d{4,}$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NAME_KEYS = (
    "userName", "consName", "consNameSecret", "custName", "customerName", "realName",
    "linkMan", "name",
)
# 用气地址：H5「用气详情」页用的是 addrSecret（服务端已打码，如「示例小区***101」），
# 其它页面还叫 consAddr / gasAddress，全都收进来
_ADDR_KEYS = (
    "addrSecret", "consAddr", "gasAddress", "consAddress", "userAddr", "custAddr",
    "linkAddress", "address", "addr", "addressDetail", "homeAddress", "useAddress",
)


def _ts_day(value: Any) -> str | None:
    """把毫秒/秒时间戳转成 YYYY-MM-DD；本身是日期字符串就原样规范化。

    payMentRecord 的 paymentDate 是 '1793328281000' 这种毫秒数，
    直接丢给正则只会得到一串数字，卡片上就会显示成乱七八糟的日期。
    """
    if value in (None, "", []):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{10}|\d{13}", text):
        try:
            stamp = int(text)
            if len(text) == 13:
                stamp //= 1000
            return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return None
    match = re.match(r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return None


def _ts_full(value: Any) -> str | None:
    """时间戳/日期串 -> 'YYYY-MM-DD HH:MM:SS'；只有日期就返回日期本身。

    缴费记录里 paymentDate 是毫秒数，H5 页面显示成「2026/9/20 0:59:41」，
    卡片「最近缴费」也按这个粒度显示——拿不到具体时刻时只显示日期。
    """
    if value in (None, "", []):
        return None
    text = str(value).strip()
    stamp: int | None = None
    if re.fullmatch(r"\d{10}|\d{13}", text):
        stamp = int(text)
        if len(text) == 13:
            stamp //= 1000
    if stamp is None:
        match = re.match(
            r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"
            r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?",
            text,
        )
        if not match:
            return None
        day = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        if match.group(4) is None:
            return day
        return (
            f"{day} {int(match.group(4)):02d}:{match.group(5)}:{match.group(6) or '00'}"
        )
    try:
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return None


def _clean_person(value: Any) -> str | None:
    text = to_text(value)
    if not text:
        return None
    if _LOGIN_NAME_RE.match(text) or text.isdigit():
        return None
    return text


def pick_person(*sources: Any) -> str | None:
    """从多个对象里挑户名：优先含中文的，其次才接受其它写法。"""
    for require_cjk in (True, False):
        for src in sources:
            value = _clean_person(pick_field(src, *_NAME_KEYS))
            if value and (not require_cjk or _CJK_RE.search(value)):
                return value
    return None


def pick_address(*sources: Any) -> str | None:
    for src in sources:
        value = to_text(pick_field(src, *_ADDR_KEYS))
        if value:
            return value
    return None


_FULL_DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_COMPACT_DATE_RE = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")


def find_full_date(record: Any) -> str | None:
    """在一条记录里找「完整日期」，字段名叫什么都行（接口各模块命名不统一）。

    只在值里认 2026-10-24 / 2026/10/24 / 2026年10月24日 / 20261024 这几种写法，
    取最晚的一个；纯月份（2026-10、202610）不算，避免把「账期」当成抄表日。
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            text = node.strip()
            hit = _FULL_DATE_RE.search(text)
            if hit:
                found.append(f"{hit.group(1)}-{int(hit.group(2)):02d}-{int(hit.group(3)):02d}")
                return
            hit = _COMPACT_DATE_RE.match(text)
            if hit:
                found.append(f"{hit.group(1)}-{hit.group(2)}-{hit.group(3)}")

    walk(record)
    return max(found) if found else None


class KaiyuanGasCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """一个账号对应一个 coordinator。"""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.entry = entry
        hours = int(entry.options.get(CONF_REFRESH_HOURS, DEFAULT_REFRESH_HOURS))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.data.get(CONF_MOBILE)}",
            update_interval=timedelta(hours=hours),
        )
        self.client = KaiyuanGasClient(
            mobile=entry.data[CONF_MOBILE],
            password=entry.data[CONF_PASSWORD],
            cons_no=entry.options.get(CONF_CONS_NO) or entry.data.get(CONF_CONS_NO) or None,
        )
        self._cons_no = (
            entry.options.get(CONF_CONS_NO) or entry.data.get(CONF_CONS_NO) or None
        )
        self.last_raw: dict[str, Any] | None = None
        # 最近一次「在线刷新成功」的时间：诊断里显示，卡片也能看出数据新旧
        self.last_refresh: datetime | None = None

        # 阶梯气价（接口不给，取配置里的值；改了选项重载后生效）
        opts = {**entry.data, **(entry.options or {})}
        self.ladder_limits = (
            float(opts.get(CONF_LADDER1, DEFAULT_LADDER1)),
            float(opts.get(CONF_LADDER2, DEFAULT_LADDER2)),
        )
        self.ladder_prices = (
            float(opts.get(CONF_PRICE1, DEFAULT_PRICE1)),
            float(opts.get(CONF_PRICE2, DEFAULT_PRICE2)),
            float(opts.get(CONF_PRICE3, DEFAULT_PRICE3)),
        )

    # ------------------------------------------------------------------ 阶梯用量
    def _build_ladder(self, usage_list: list[dict[str, Any]]) -> dict[str, Any]:
        """按自然年累计用气量算当前阶梯（分档与单价来自配置）。"""
        limit1, limit2 = self.ladder_limits
        if limit2 <= limit1:  # 配置写反了也不至于算出负数
            limit2 = limit1 + 160
        tiers = [
            {"name": "第一档", "from": 0, "to": limit1, "price": self.ladder_prices[0]},
            {"name": "第二档", "from": limit1, "to": limit2, "price": self.ladder_prices[1]},
            {"name": "第三档", "from": limit2, "to": None, "price": self.ladder_prices[2]},
        ]

        year = None
        for row in usage_list:
            month = str(row.get("month") or "")
            if len(month) >= 4:
                year = month[:4]
                break
        used = 0.0
        if year:
            used = sum(
                float(row.get("usage") or 0)
                for row in usage_list
                if str(row.get("month") or "").startswith(year)
            )
        used = round(used, 3)

        if used <= limit1:
            tier = 1
        elif used <= limit2:
            tier = 2
        else:
            tier = 3
        cap = tiers[tier - 1]["to"] or used
        return {
            "year": year,
            "used": used,
            "tier": tier,
            "tier_name": tiers[tier - 1]["name"],
            "limit": cap,
            "remaining": round(max(cap - used, 0), 3),
            "max": limit2,
            "unit": "m³",
            "tiers": tiers,
        }

    def _with_ladder(self, data: dict[str, Any]) -> dict[str, Any]:
        data["ladder"] = self._build_ladder(data.get("usage_list") or [])
        return data

    # ------------------------------------------------------------------ 缓存文件
    @property
    def cache_file(self):
        """缓存文件路径（户号未知时先用占位名，首次查询成功后改名）。"""
        return cache.cache_path(self.hass, self._cons_no)

    def _load_cache(self):
        return cache.load(self.cache_file)

    def _save_cache(self, doc: dict[str, Any]) -> None:
        cache.save(self.cache_file, doc)

    def _rename_cache(self, old_cons_no: str | None, new_cons_no: str, doc: dict[str, Any]) -> None:
        """首次识别出户号后，把占位名的缓存文件换成正式文件名。"""
        if not old_cons_no or old_cons_no == new_cons_no:
            return
        old_path = cache.cache_path(self.hass, old_cons_no)
        if old_path == self.cache_file or not old_path.exists():
            return
        try:
            old_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ 刷新
    async def _async_update_data(self) -> dict[str, Any]:
        cached = await self.hass.async_add_executor_job(self._load_cache)
        # 缓存里已经有金额的月份，逐期详情不用再拉一遍（历史账期不会变），
        # 否则每月翻 24 期账单一刷新就是几十个请求。
        known_months = {
            str(row.get("month"))
            for row in (cached or {}).get("billList") or []
            if row.get("month") and row.get("fee")
        }

        raw: dict[str, Any] | None = None
        fetch_err: Exception | None = None
        try:
            raw = await self.hass.async_add_executor_job(self.client.fetch_all, known_months)
        except KaiyuanGasLockedError as err:
            # 账号被锁：绝对不要再重试登录，否则会延长锁定时间
            fetch_err = err
        except KaiyuanGasAuthError as err:
            # 会话过期：清 token 重登一次
            self.client.token = None
            self.client._session.headers.pop("token", None)  # noqa: SLF001
            try:
                await self.hass.async_add_executor_job(self.client.login)
                raw = await self.hass.async_add_executor_job(
                    self.client.fetch_all, known_months
                )
            except KaiyuanGasError as retry_err:
                fetch_err = retry_err
        except KaiyuanGasError as err:
            fetch_err = err

        if fetch_err is not None or raw is None:
            if cached and cached.get("billList"):
                _LOGGER.warning(
                    "在线查询失败（%s），改用本地缓存 %s（%s 更新）",
                    fetch_err, self.cache_file, cached.get("cache_updated_at"),
                )
                data = cache.to_data(cached)
                data.update({
                    "data_source": "本地缓存",
                    "login_status": "离线",
                    "last_error": str(fetch_err or "未知错误"),
                    "cache_file": str(self.cache_file),
                })
                # 走缓存时不能把「现在」当成刷新时间：那会让诊断和卡片误以为刚刷新成功。
                # 真正能代表数据新鲜度的，是缓存里最后一次成功在线写入的时间。
                return self._with_ladder(
                    self._stamp_refresh(data, stamp=cached.get("updated_at"))
                )
            raise UpdateFailed(str(fetch_err or "在线查询失败，且本地还没有缓存文件"))

        live = self._normalize(raw)
        # 留一份在线原始返回：接口字段改名时，诊断文件里能直接看出真实字段名
        self.last_raw = raw
        self.last_refresh = datetime.now().astimezone()

        # 首次拿到户号：缓存文件名跟着改
        new_cons = live.get("cons_no")
        if new_cons and new_cons != self._cons_no:
            old_cons = self._cons_no
            self._cons_no = new_cons
            draft = cache.merge(cached, live)
            await self.hass.async_add_executor_job(self._save_cache, draft)
            await self.hass.async_add_executor_job(self._rename_cache, old_cons, new_cons, draft)
            cached = draft
        else:
            cached = cache.merge(cached, live)
            await self.hass.async_add_executor_job(self._save_cache, cached)

        data = cache.to_data(cached)
        mobile = str(self.entry.data.get(CONF_MOBILE) or "")
        data.update({
            "mobile": f"{mobile[:3]}****{mobile[-4:]}" if len(mobile) >= 7 else mobile,
            "data_source": "在线获取",
            "login_status": "在线",
            "last_error": None,
            "cache_file": str(self.cache_file),
        })
        return self._with_ladder(self._stamp_refresh(data))

    def _stamp_refresh(self, data: dict[str, Any], stamp: str | None = None) -> dict[str, Any]:
        """把最近一次成功刷新的时间写进数据（诊断与卡片都用它）。

        ``stamp`` 用于「在线查询失败、退回本地缓存」的分支：那时并没有刷新成功，
        只能沿用缓存里最后一次成功写入的时间，否则界面会显示一个假的「刚刚刷新」。
        """
        if stamp:
            data["last_refresh"] = str(stamp)
            return data
        value = self.last_refresh or datetime.now().astimezone()
        data["last_refresh"] = value.isoformat(timespec="seconds")
        return data

    # ------------------------------------------------------------------ 归一化
    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        account = raw.get("account") or {}
        arrearage_obj = raw.get("arrearage")
        if isinstance(arrearage_obj, list):
            arrearage_obj = arrearage_obj[0] if arrearage_obj else None
        user = raw.get("user") or {}

        # ---- 账单查询接口（/micro/bill/ncBill）----
        # H5「账单查询」页直接吃这份数据，字段名是全套接口里最稳的：
        #   resConsFee：lastRead 上期读数 / nearRead 本期读数 / allFee 累计欠费 /
        #               amt 欠燃气费 / totalRcvblPenalty 违约金 / otherFee 初装费 /
        #               lastBalance 当前余额
        #   resBillInfos[]：readMeterDate 抄表日期 / gasNum 用气量 / gasAmt 燃气费 /
        #                   serviceAmt 服务费 / totalAmt 本期合计 / lastReadNum /
        #                   thisReadNum
        nc_bill = raw.get("nc_bill")
        nc_bill = nc_bill if isinstance(nc_bill, dict) else {}
        nc_fee = nc_bill.get("resConsFee")
        if isinstance(nc_fee, list):
            nc_fee = nc_fee[0] if nc_fee else {}
        nc_fee = nc_fee if isinstance(nc_fee, dict) else {}
        nc_bills = nc_bill.get("resBillInfos")
        nc_bills = [b for b in nc_bills if isinstance(b, dict)] if isinstance(nc_bills, list) else []
        latest_nc = nc_bills[0] if nc_bills else {}

        balance = to_float(
            deep_get(nc_fee, "lastBalance", "lastBalanceAmt", "balance", "ye", "kyye")
        )
        if balance is None:
            balance = to_float(
                deep_get(account, "presBalance", "presMoney", "balance", "accountBalance",
                         "surplus", "surplusMoney", "remainMoney", "ye", "kyye")
            )
        arrearage = to_float(deep_get(nc_fee, "allFee", "allFeeAmt", "totalFee"))
        if arrearage is None:
            arrearage = to_float(deep_get(nc_fee, "amt", "oweFee", "oweMoney"))
        if arrearage is None:
            # 欠费接口本身用的就是 allFee（累计欠费）/ amt（欠燃气费）这两个名字
            arrearage = to_float(
                deep_get(arrearage_obj, "allFee", "arrearage", "arrearageMoney", "oweMoney",
                         "amt", "shouldPay", "payable", "totalArrearage", "yjje", "arrears",
                         "totalOwe", "oweTotal")
            )
        if arrearage is None:
            arrearage = to_float(
                deep_get(account, "arrearage", "oweMoney", "shouldPay", "payable", "yjje")
            )

        meters = raw.get("meters") or []
        latest_meter = meters[0] if meters else {}
        reading = to_float(deep_get(latest_meter, *KEYS_READING))
        prev_reading = to_float(deep_get(latest_meter, *KEYS_PREV))
        usage = to_float(deep_get(latest_meter, *KEYS_USAGE))
        if usage is None and reading is not None and prev_reading is not None:
            usage = round(reading - prev_reading, 3)
        reading_date = self._norm_day(
            deep_get(latest_meter, "readDate", "readTime", "date", "meterDate", "cbDate")
        ) or find_full_date(latest_meter) or to_text(
            deep_get(latest_meter, "readDate", "readTime", "date", "ym", "meterDate")
        )
        meter_no = to_text(
            deep_get(latest_meter, "meterNo", "meterCode", "assetNo", "meterBodyNo", "bdh", "sbh")
        )

        payments = raw.get("payments") or []
        latest_pay = payments[0] if payments else {}
        pay_amount = to_float(deep_get(latest_pay, *KEYS_PAY_AMOUNT))
        pay_date = _ts_day(deep_get(latest_pay, *KEYS_PAY_DATE)) or to_text(
            deep_get(latest_pay, *KEYS_PAY_DATE)
        )

        bills = raw.get("bills") or []
        latest_bill = bills[0] if bills else {}

        # ---- 逐期账单详情（H5「用气详情」页 /micro/bill/feeDetail）----
        # 首页缺的「用气地址」「用户姓名」，以及未缴费那期缺的「费用构成」，
        # 都只在这个接口里有：addrSecret / userName / payableGasFee / payableLateFee
        details = [d for d in (raw.get("bill_details") or []) if isinstance(d, dict)]
        latest_detail = self._latest_detail(details)
        penalty = to_float(
            deep_get(latest_bill, "penalty", "penaltyMoney", "wyj", "lateFee", "breakFee")
        )
        bill_period = to_text(
            deep_get(latest_bill, "period", "billPeriod", "feePeriod", "cycle", "jfzq", "periodStr")
        )
        read_way = to_text(
            deep_get(latest_meter, "readWay", "readType", "cbType", "readMode", "meterReadType")
        )

        # ---- 账单明细（对应 H5「用气详情」页）----
        def pick(*keys: str) -> Any:
            """先在最新一期账单里找，找不到再在抄表记录里找。"""
            found = deep_get(latest_bill, *keys)
            if found in (None, "", [], {}):
                found = deep_get(latest_meter, *keys)
            return found

        receivable = to_float(pick("shouldMoney", "shouldPay", "receivable", "ysje", "shouldAmount"))
        received = to_float(pick("realMoney", "actualMoney", "paidMoney", "received", "ssje", "realAmount"))
        penalty_start = to_text(
            pick("penaltyStartDate", "penaltyDate", "lateFeeStartDate", "wyjqsrq")
        )
        prev_unpaid = to_float(pick("lastUnpaid", "prevUnpaid", "preUnpaid", "scws", "lastOwe"))
        cur_unpaid = to_float(pick("curUnpaid", "thisUnpaid", "bcws", "currentOwe"))
        settled_raw = pick("settleFlag", "isSettle", "settleStatus", "settleState", "sfjq", "isSettled")
        settled = None
        if settled_raw is not None:
            settled = str(settled_raw).strip().lower() in {"1", "true", "y", "yes", "已结清", "已缴", "结清"}
        bill_days = to_float(pick("days", "billDays", "dayNum", "ts"))
        unpaid = [
            b for b in bills
            if str(deep_get(b, *KEYS_STATUS) or "").lower()
            in {"0", "未缴", "false", "n", "unpaid"}
        ]
        # 欠费接口偶尔只给「是否欠费」这种标记，金额要自己从账期记录里加总
        if arrearage is None:
            owed = sum(
                float(deep_get(b, *KEYS_FEE) or 0)
                for b in bills
                if str(deep_get(b, *KEYS_STATUS) or "").lower() in {"0", "未缴", "false", "n", "unpaid"}
            )
            if owed > 0:
                arrearage = round(owed, 2)

        # ---- 供卡片使用的结构化列表 ----
        usage_list, fee_items_by_month = self._build_usage_list(
            meters, bills, payments, nc_bills, details
        )
        payment_list = self._build_payment_list(payments)

        # ---- 缴费按「日」归并：H5 把「燃气费 / 垃圾费 / 违约金」拆成多条同日记录 ----
        last_pay_total = last_pay_gas = last_pay_garbage = last_pay_penalty = None
        payment_days: list[dict[str, Any]] = []
        if payment_list:
            by_day: dict[str, list[dict[str, Any]]] = {}
            for row in payment_list:
                day = self._day_key(row.get("time"))
                if day:
                    by_day.setdefault(day, []).append(row)

            buckets: dict[str, dict[str, Any]] = {}
            for day, rows in by_day.items():
                bucket = buckets.setdefault(
                    day,
                    {"gas": 0.0, "garbage": 0.0, "penalty": 0.0,
                     "channel": "", "settle": "", "at": ""},
                )
                # 同一天里只要有一条带燃气费，其余「有金额、gasFee 为空」的记录就是垃圾费：
                # H5 缴费页把一笔缴费拆成两条（燃气费一条、垃圾费一条），两条金额都不为空。
                has_gas = any((r.get("gas") or 0) > 0 for r in rows)
                for row in rows:
                    amount = float(row.get("amount") or 0)
                    gas_part = float(row.get("gas") or 0)
                    service_part = float(row.get("service") or 0)
                    penalty_part = float(row.get("penalty") or 0)
                    items = row.get("items") or []

                    if items and not (gas_part or service_part or penalty_part):
                        rest = amount
                        for it in items:
                            name = str(it.get("item") or "")
                            money = float(it.get("money") or 0)
                            if "垃圾" in name:
                                bucket["garbage"] += money
                                rest -= money
                            elif "违约金" in name or "滞纳" in name:
                                bucket["penalty"] += money
                                rest -= money
                        if rest > 0.005:
                            bucket["gas"] += rest
                    elif gas_part or service_part or penalty_part:
                        bucket["gas"] += gas_part
                        bucket["garbage"] += service_part
                        bucket["penalty"] += penalty_part
                    elif amount > 0:
                        if has_gas:
                            bucket["garbage"] += amount
                        else:
                            bucket["gas"] += amount

                    if not bucket["channel"] and row.get("channel"):
                        bucket["channel"] = row["channel"]
                    if not bucket["settle"] and row.get("settle"):
                        bucket["settle"] = row["settle"]
                    # 同一天拆成「燃气费 / 垃圾费」两条时，取较晚的那条时刻
                    at = str(row.get("at") or "")
                    if at > str(bucket["at"] or ""):
                        bucket["at"] = at

            for day in sorted(buckets, reverse=True):
                b = buckets[day]
                gas = round(b["gas"], 2)
                garb = round(b["garbage"], 2)
                pen = round(b["penalty"], 2)
                payment_days.append({
                    "date": day,
                    "at": b["at"] or day,
                    "gas": gas,
                    "garbage": garb,
                    "penalty": pen,
                    "total": round(gas + garb + pen, 2),
                    "channel": b["channel"] or "-",
                    # cache 里读的是 settle_way，这里两个都写，免得结算方式丢掉
                    "settle": b["settle"] or "",
                    "settle_way": b["settle"] or "",
                })

            if buckets:
                latest = payment_days[0]
                last_pay_gas = latest["gas"] or None
                last_pay_garbage = latest["garbage"] or None
                last_pay_penalty = latest["penalty"] or None
                last_pay_total = latest["total"]
                pay_date = latest["date"]

        # 气价（元/m³）：用「燃气费 / 用气量」反推。
        # 不能只看最近一期——那一期常常还没结清，金额里掺了固定服务费/违约金，
        # 除出来的「单价」是假的；取各期里出现次数最多的那个更靠谱。
        unit_price = self._pick_unit_price(usage_list)

        # 垃圾费（随燃气费一并代扣的固定费用）与结算方式
        garbage_fee = None
        settle_way = None
        for p in payments:
            if garbage_fee is None:
                for it in (deep_get(p, "items", "feeList", "details", "itemList", "subList") or []):
                    if not isinstance(it, dict):
                        continue
                    name = str(deep_get(it, "item", "name", "feeTypeName", "typeName") or "")
                    if "垃圾" in name:
                        garbage_fee = to_float(deep_get(it, "amount", "money", "fee", "payMoney"))
                        if garbage_fee is not None:
                            break
            if garbage_fee is None:
                garbage_fee = to_float(deep_get(p, "trashFee", "rubbishFee", "wasteFee", "ljf"))
            if settle_way is None:
                settle_way = to_text(deep_get(p, "settleWay", "settleType", "settleName", "deductWay"))
            if garbage_fee is not None and settle_way:
                break

        # 缴费记录里没拆出垃圾费时，用账单详情算出来的那一份（最新一期优先）
        if garbage_fee is None:
            for row in usage_list:
                if row.get("garbage_fee"):
                    garbage_fee = row["garbage_fee"]
                    break
        if garbage_fee is None and payment_days:
            garbage_fee = payment_days[0].get("garbage") or None

        # 最新一期的账单级字段并入对应月份的记录
        if usage_list:
            row = usage_list[0]
            if settled is not None:
                row["settled"] = settled
            if bill_period:
                row["bill_period"] = bill_period
            if bill_days is not None:
                row["bill_days"] = bill_days
            if penalty is not None:
                row["penalty"] = penalty
            if penalty_start:
                row["penalty_start"] = penalty_start
            if garbage_fee is not None:
                row["garbage_fee"] = garbage_fee
            if not row.get("items"):
                row["items"] = fee_items_by_month.get(row["month"])

        # 交给 cache.merge() 的「在线快照」：只有账户标量 + 两张列表
        return {
            "cons_no": to_text(pick_field(nc_fee, "cardNum", "consNo", "gasCardNo"))
            or to_text(pick_field(latest_nc, "cardNum", "consNo", "gasCardNo"))
            or raw.get("cons_no")
            or to_text(pick_field(arrearage_obj, "cardNum", "consNo", "gasCardNo"))
            or to_text(pick_field(latest_bill, "cardNum", "consNo", "gasCardNo")),
            # 户名/地址优先取「客户信息」那一类字段：登录用户里的 userName 往往是
            # u84742444 这种登录名，不是户名。H5「用气详情」页给的 userName /
            # addrSecret 才是页面上显示的户名和地址，所以放第一位。
            "customer_name": pick_person(latest_detail, account, nc_fee, latest_nc,
                                         arrearage_obj, user, latest_bill, latest_meter),
            "address": pick_address(latest_detail, account, nc_fee, latest_nc,
                                    arrearage_obj, user, latest_bill, latest_meter),
            "meter_no": meter_no,
            "unit_price": unit_price,
            "garbage_fee": garbage_fee,
            "settle_way": settle_way,
            "read_way": read_way,
            "balance": balance,
            "arrearage": arrearage,
            "usage_list": usage_list,
            "payment_days": payment_days,
        }

    # ------------------------------------------------------------------ 列表构建
    @staticmethod
    def _day_key(value: Any) -> str | None:
        """'2026-10-01 10:22' / '2026/10/1' -> '2026-10-01'（同日多条缴费合并用）。"""
        text = str(value or "").strip()
        if not text:
            return None
        m = re.match(r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})", text)
        if not m:
            return text[:10]
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    @staticmethod
    def _norm_day(value: Any) -> str | None:
        """'2026-09-24' / '2026/9/24' / '2026年09月24日' -> '2026-09-24'。"""
        text = str(value or "").strip()
        m = re.match(r"^(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", text)
        if not m:
            return None
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    @staticmethod
    def _norm_month(value: Any) -> str | None:
        """'202610' / '2026-10' / '2026-9-25' / '2026年9月' -> '2026-09'。"""
        if value in (None, ""):
            return None
        text = str(value).strip()
        m = re.match(r"^(\d{4})\s*[-/年.]\s*(\d{1,2})", text)
        if m:
            month = int(m.group(2))
            if 1 <= month <= 12:
                return f"{m.group(1)}-{month:02d}"
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 6:
            month = int(digits[4:6])
            if 1 <= month <= 12:
                return f"{digits[:4]}-{month:02d}"
        if len(digits) == 4:  # 只有年份
            return digits
        return None

    @staticmethod
    def _norm_status(value: Any) -> str:
        # 注意别写成 str(value or "")：接口的「未缴」标记正好是数字 0，
        # 那样会被当成空串，未缴费全被判成已缴费。
        s = "" if value is None else str(value).strip().lower()
        if s in {"0", "false", "n", "未缴", "未交", "unpaid", "待缴", "未结清"}:
            return "未缴费"
        return "已缴费"

    def _latest_detail(self, details: list[dict[str, Any]]) -> dict[str, Any]:
        """取账期最新的一期账单详情（首页地址 / 户名从这里来）。"""
        best: dict[str, Any] = {}
        best_key = ""
        for d in details:
            key = (
                self._norm_day(deep_get(d, "thisReadTime", "readTime", "readDate"))
                or self._norm_month(deep_get(d, "ym", "billYm", "feeMonth", "month"))
                or ""
            )
            if key >= best_key:
                best_key, best = key, d
        return best

    def _build_usage_list(
        self, meters: list[Any], bills: list[Any], payments: list[Any] | None = None,
        nc_bills: list[dict[str, Any]] | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """把抄表记录、账单、交费记录按月合并成 usage_list + 费用构成。

        账单接口通常只返回最近一期，其余月份的气费从交费记录里回填：
        交费日期可能比计费月晚几天，所以按「用气量 × 单价 ≈ 交费金额」挑最像的那个月。

        ``nc_bills``（/micro/bill/ncBill 的 resBillInfos）按账期给出「抄表日期 + 用气量
        + 燃气费 + 本期合计」；``details``（/micro/bill/feeDetail，H5「用气详情」页）
        优先级最高——它连未缴费那期的燃气费、违约金、起止读数都给了。
        """
        by_month: dict[str, dict[str, Any]] = {}
        for m in meters:
            month = self._norm_month(deep_get(m, "readDate", "readTime", "date", "ym", "meterDate"))
            if not month:
                continue
            row = by_month.setdefault(month, {"month": month})
            num = to_float(deep_get(m, *KEYS_READING))
            if num is not None:
                row["reading"] = num
            prev = to_float(deep_get(m, *KEYS_PREV))
            if prev is not None:
                row["_prev"] = prev
            gas = to_float(deep_get(m, *KEYS_USAGE))
            if gas is not None:
                row["usage"] = gas
            # 抄表日期：优先完整日期（接口给的字段名不固定，就按值找），
            # 实在只有月份才退回月份，卡片上不会再把「2026-10」当抄表日。
            read_date = (
                self._norm_day(deep_get(m, "readDate", "readTime", "date", "meterDate", "cbDate"))
                or find_full_date(m)
                or to_text(deep_get(m, "readDate", "readTime", "date", "ym", "meterDate"))
            )
            if read_date:
                row["read_date"] = read_date
            read_way = to_text(
                deep_get(m, "readWay", "readType", "cbType", "readMode", "meterReadType")
            )
            if read_way:
                row["read_way"] = read_way

        fee_items: dict[str, list[dict[str, Any]]] = {}
        for b in bills:
            if not isinstance(b, dict):
                continue
            month = self._norm_month(
                deep_get(b, "billMonth", "month", "ym", "feeMonth", "rcvblym", "kpMonth", "billYm")
            )
            if not month:
                continue
            row = by_month.setdefault(month, {"month": month})
            # 费用记录（feeRecord）里真正的字段是 billAmt（该期账单金额）和
            # payableGasFee（应收，结清后就是 0.00）。只认 payableGasFee 会让
            # 已结清的月份全部变成「-」，这正是以前 4 月及更早没有金额的原因。
            bill_amt = to_float(
                deep_get(b, "billAmt", "billAmount", "billMoney", "totalAmt", "totalFee")
            )
            if bill_amt is not None and bill_amt > 0:
                row["_bill_amt"] = bill_amt
                row["total_fee"] = bill_amt
            bill_id = b.get("id") or b.get("billId") or b.get("rcvblId")
            if bill_id not in (None, ""):
                row["_bill_id"] = str(bill_id)

            # 只认「燃气费」语义的字段。payableGasFee 看着像燃气费，其实是「应收金额」：
            # 未结清的月份它等于含垃圾费的账单总额，结清后又变成 0.00，两头都不能当气费。
            fee = to_float(deep_get(b, "gasFee", "gasAmt", "gasMoney"))
            # 只给了「欠费金额 0」的已结清账期：0 不是气费，交给缴费记录去回填
            if fee == 0:
                fee = None
            if fee is not None:
                row["fee"] = fee
            # 账单里往往也直接带本期气量，抄表记录只给起数时靠它补上
            bill_usage = to_float(deep_get(b, *KEYS_USAGE))
            if bill_usage is not None and row.get("usage") is None:
                row["usage"] = bill_usage
            row["status"] = self._norm_status(
                deep_get(b, "settleFlagCis", *KEYS_STATUS)
            )
            if fee is not None:
                fee_items.setdefault(month, []).append({
                    "item": to_text(deep_get(b, "feeTypeName", "feeType", "typeName")) or "燃气费",
                    "name": to_text(deep_get(b, "priceName", "ladderName")) or "",
                    "money": fee,
                })

        # 账单查询接口的逐期明细：放在最后处理，作为最可靠的一手数据覆盖前面的推断值
        for b in nc_bills or []:
            read_day = (
                self._norm_day(deep_get(b, "readMeterDate", "readDate", "meterReadDate"))
                or find_full_date(b)
            )
            month = (
                (read_day[:7] if read_day else None)
                or self._norm_month(deep_get(b, "billMonth", "month", "ym", "feeMonth", "rcvblym"))
            )
            if not month:
                continue
            row = by_month.setdefault(month, {"month": month})
            if read_day:
                row["read_date"] = read_day

            gas_amt = to_float(deep_get(b, "gasAmt", "gasFee", "gasMoney"))
            service_amt = to_float(
                deep_get(b, "serviceAmt", "serviceFee", "serviceMoney", "trashFee", "rubbishFee")
            )
            # 账单金额：H5 账单列表里叫 billAmt，缺了它就没有「本期合计」
            total_amt = to_float(
                deep_get(b, "billAmt", "totalAmt", "totalFee", "totalMoney", "total",
                         "billAmount")
            )
            if gas_amt is None and total_amt is not None and service_amt:
                # 只有明确知道服务费时才敢从「本期合计」里减：不知道服务费就把
                # 合计当燃气费，会把垃圾费算进气费，单价、档位跟着全错
                gas_amt = round(total_amt - service_amt, 2)
            # 卡片按「气费 + 垃圾(服务)费 + 违约金」拼本期账单：
            # 这里 fee 只放燃气费，服务费落到 garbage_fee，两者相加正好等于本期合计
            if gas_amt is not None and gas_amt > 0:
                row["fee"] = gas_amt
            if service_amt is not None and service_amt > 0:
                row["garbage_fee"] = service_amt
            elif total_amt is not None and gas_amt is not None and total_amt - gas_amt > 0.005:
                row["garbage_fee"] = round(total_amt - gas_amt, 2)
            if total_amt is not None and total_amt > 0:
                row["total_fee"] = total_amt

            usage = to_float(deep_get(b, "gasNum", "gasAmount", "useGas", "gasUse"))
            if usage is not None:
                row["usage"] = usage
            reading = to_float(deep_get(b, "thisReadNum", "readNum", "endNum"))
            prev = to_float(deep_get(b, "lastReadNum", "prevReadNum", "startNum"))
            if reading is not None:
                row["reading"] = reading
            if prev is not None:
                row["_prev"] = prev

            settled_flag = deep_get(b, "settleFlag", "isSettle", "settleStatus", "status")
            if settled_flag is not None:
                row["status"] = self._norm_status(settled_flag)
                row["settled"] = row["status"] == "已缴费"

            items = []
            if gas_amt is not None and gas_amt > 0:
                items.append({"item": "燃气费", "name": "", "money": gas_amt})
            extra = service_amt if (service_amt or 0) > 0 else 0
            if not extra and total_amt and gas_amt and total_amt - gas_amt > 0.005:
                extra = round(total_amt - gas_amt, 2)
            if extra:
                items.append({"item": "服务费", "name": "", "money": extra})
            if items:
                fee_items[month] = items

        # 逐期账单详情：最权威的一手数据（H5「用气详情」页调的就是它）。
        # 未缴费那期在别的接口里只剩一个「待结清」标记，这里才有燃气费和违约金。
        # 归月时优先按 billId 反查它属于哪一期：detail 自带的 ym 有时是划线月，
        # 直接按 ym 归月会把整期挂到隔壁月份上（以前 4 月及更早因此拿不到费用）。
        id_to_month: dict[str, str] = {}
        for month, row in by_month.items():
            bill_id = row.get("_bill_id")
            if bill_id:
                id_to_month[str(bill_id)] = month

        for d in details or []:
            read_day = self._norm_day(
                deep_get(d, "thisReadTime", "readTime", "readDate", "thisReadDate")
            )
            # 月份按「抄表月」算：整个 usage_list 都是以抄表日期组织的
            month = (
                id_to_month.get(str(d.get("billId") or ""))
                or (read_day[:7] if read_day else None)
                or self._norm_month(deep_get(d, "ym", "billYm", "feeMonth", "month"))
            )
            if not month:
                continue
            row = by_month.setdefault(month, {"month": month})
            if read_day:
                row["read_date"] = read_day
            period_start = self._norm_day(
                deep_get(d, "lastReadTime", "lastReadDate", "prevReadDate")
            )
            if period_start and read_day:
                row["bill_period"] = f"{period_start} 至 {read_day}"

            gas_amt = to_float(deep_get(d, "payableGasFee", "gasFee", "payableGasMoney"))
            if not gas_amt:
                # 已结清的月份「应收」会被清成 0.00，真正的气费在「实收金额」里
                gas_amt = to_float(deep_get(d, "paidInGasFee", "realGasFee", "paidGasFee"))
            # 详情的「应收金额」含垃圾费时，它和账单金额一模一样；减掉这一期已知的
            # 固定费用，免得「气费」里混进垃圾费、卡片上再把垃圾费加一遍
            row_bill = to_float(row.get("_bill_amt"))
            if gas_amt and row_bill and float(gas_amt) >= float(row_bill) - 0.01:
                cut = to_float(row.get("garbage_fee")) or 0.0
                if cut and float(gas_amt) - float(cut) > 0:
                    gas_amt = round(float(gas_amt) - float(cut), 2)
            penalty = to_float(
                deep_get(d, "payableLateFee", "lateFee", "penalty", "penaltyMoney")
            )
            arreas = to_float(deep_get(d, "arreasAll", "arrearsAll", "oweFee", "shouldPay"))
            total_amt = to_float(
                deep_get(d, "totalAmt", "totalFee", "shouldMoney", "receivable", "allFee")
            )

            # 垃圾费（随燃气费代扣的服务费）：接口不单独给字段，用「该期应收 - 燃气费
            # - 违约金」倒推。只在差额落在合理区间时采纳，避免把别的东西算进来。
            service_amt = to_float(
                deep_get(d, "serviceFee", "serviceAmt", "trashFee", "rubbishFee", "garbageFee")
            )
            if service_amt is None and gas_amt is not None:
                # 可能是「本期合计」，也可能是「该户总欠费」（欠了两期以上时会偏大），
                # 所以两个都算一遍，取落在合理区间里最小的那个
                diffs = []
                for base in (total_amt, arreas):
                    if base is None or float(base) <= float(gas_amt):
                        continue
                    diff = round(float(base) - float(gas_amt) - float(penalty or 0), 2)
                    if 0 < diff <= 100:
                        diffs.append(diff)
                if diffs:
                    service_amt = min(diffs)
            # 已结清的那期 arreasAll 是 0，倒推不出垃圾费，沿用别的接口已经算好的值
            if service_amt is None and row.get("garbage_fee"):
                service_amt = row["garbage_fee"]

            if gas_amt is not None and gas_amt > 0:
                row["fee"] = gas_amt
            if service_amt is not None and service_amt > 0:
                row["garbage_fee"] = service_amt
            if penalty is not None:
                row["penalty"] = penalty

            usage = to_float(deep_get(d, "gasAmount", "gasNum", "useGas", "gasUse"))
            if usage is not None:
                row["usage"] = usage
            this_rd = to_float(deep_get(d, "thisRdNum", "thisReadNum", "readNum", "endNum"))
            last_rd = to_float(deep_get(d, "lastRdNum", "lastReadNum", "prevReadNum", "startNum"))
            if this_rd is not None:
                row["reading"] = this_rd
            if last_rd is not None:
                row["_prev"] = last_rd

            settled_flag = deep_get(d, "settleFlag", "isSettle", "settleStatus", "status")
            if settled_flag is not None:
                row["status"] = self._norm_status(settled_flag)
                row["settled"] = row["status"] == "已缴费"

            items = []
            if gas_amt is not None and gas_amt > 0:
                items.append({"item": "燃气费", "name": "", "money": gas_amt})
            if service_amt is not None and service_amt > 0:
                items.append({"item": "垃圾费", "name": "", "money": service_amt})
            if penalty is not None and penalty > 0:
                items.append({"item": "违约金", "name": "", "money": penalty})
            if items:
                fee_items[month] = items

        usage_list = [by_month[k] for k in sorted(by_month, reverse=True)]
        asc = sorted(usage_list, key=lambda r: r["month"])

        # 抄表记录只有月份时，用账单里带的完整日期补上
        for row in usage_list:
            if len(str(row.get("read_date") or "")) >= 10:
                continue
            for b in bills:
                if self._norm_month(deep_get(b, "billMonth", "month", "ym", "feeMonth")) != row["month"]:
                    continue
                day = find_full_date(b)
                if day:
                    row["read_date"] = day
                    break

        # 有些抄表记录只给「起数」不给「止数」：本期的止数就是下一期的起数。
        # 先按这个关系把止数补齐，否则用气量、单价、气费全都算不出来。
        for i, row in enumerate(asc):
            if row.get("reading") is None and i + 1 < len(asc):
                nxt = asc[i + 1].get("reading") or asc[i + 1].get("_prev")
                if nxt is not None and (row.get("_prev") is None or float(nxt) >= float(row["_prev"])):
                    row["reading"] = nxt

        # 没有直接给用气量时，用「本次读数 - 上次读数」补出来（按月份升序回填）
        for i, row in enumerate(asc):
            row.setdefault("status", "")
            prev = row.get("_prev")
            if prev is None and i > 0:
                prev = asc[i - 1].get("reading")
            # 起数 / 止数：卡片「用气明细」表格用（末列为最高读数）
            if prev is not None:
                row["prev_reading"] = prev
            if row.get("usage") is None:
                if prev is not None and row.get("reading") is not None:
                    diff = round(float(row["reading"]) - float(prev), 3)
                    if diff >= 0:
                        row["usage"] = diff
            # 反过来：只有账单气量、没有止数时，用「起数 + 用气量」还原止数
            if row.get("reading") is None and prev is not None and row.get("usage") is not None:
                row["reading"] = round(float(prev) + float(row["usage"]), 3)
            row.pop("_prev", None)

        # 用交费记录回填没有账单的月份
        if payments:
            self._fill_fee_from_payments(by_month, payments)

        # 还是没金额的月份：用账单接口的 billAmt（该期账单金额）补。
        # 已缴的月份 payableGasFee 是 0.00，但 billAmt 一直在，这是历史月份的
        # 唯一来源——卡片上 4 月及更早的「-」就是缺了这一步。
        self._fill_fee_from_bill_amt(by_month)

        # 还没结清的账期（本月最常见）：公众号的「费用明细」要等缴费后才生成，
        # 接口只给本期应缴合计。这里用「用气量 × 已核实的档位单价」把这期的燃气费
        # 算出来，卡片上就能和已缴费月份一样显示「第一档 · 单价 × 用量」。
        # 单价只认能对上配置档位价的那个（strict），算不出来就保持空，绝不乱填。
        estimate_price = self._pick_unit_price(by_month.values(), strict=True)
        if estimate_price:
            for row in by_month.values():
                if row.get("fee") or not row.get("usage"):
                    continue
                try:
                    used = float(row["usage"])
                except (TypeError, ValueError):
                    continue
                if used <= 0:
                    continue
                fee = round(used * float(estimate_price), 2)
                if fee <= 0:
                    continue
                row["fee"] = fee
                row["estimated"] = True

        # 垃圾费是随燃气费一起代扣的固定费用。某一期的接口没给时，用同户其它期的
        # 众数补上——否则「费用构成」里只剩燃气费一项，看着像数据没取到。
        # 只在至少两期一致时才补，避免把偶发的其它费用当成垃圾费。
        garbage_counts: dict[float, int] = {}
        for row in usage_list:
            value = row.get("garbage_fee")
            if value:
                garbage_counts[float(value)] = garbage_counts.get(float(value), 0) + 1
        if garbage_counts:
            common = max(garbage_counts, key=lambda v: garbage_counts[v])
            if garbage_counts[common] >= 2:
                for row in usage_list:
                    if not row.get("fee") or row.get("garbage_fee"):
                        continue
                    row["garbage_fee"] = common
                    items = fee_items.get(row["month"])
                    if items and not any("垃圾" in str(i.get("item") or "") for i in items):
                        items.append({"item": "垃圾费", "name": "", "money": common})

        # 「本期合计」= 气费 + 垃圾费 + 违约金。接口没给这个字段的月份（未结清期、
        # 只有缴费记录回填的月份）自己拼出来，卡片大数字和累计费用都按它算。
        for row in usage_list:
            if row.get("total_fee") or not row.get("fee"):
                continue
            row["total_fee"] = round(
                float(row["fee"])
                + float(row.get("garbage_fee") or 0)
                + float(row.get("penalty") or 0),
                2,
            )

        # 把「费用构成」挂到对应月份上，缓存文件里一眼能看到这一期由哪些费用组成
        # 同时补上阶梯名 / 单价 / 用量，卡片按「燃气费 · 居民户表一阶 / 单价 × 用量」的样式显示
        for month, items in fee_items.items():
            if month in by_month and items:
                usage = by_month[month].get("usage")
                for it in items:
                    self._enrich_fee_item(it, usage)
                by_month[month]["items"] = items

        # 只拿到金额、没有账单明细的月份：补一条「燃气费」，金额就是该期气费，
        # 单价 × 用量由真实数据反推，不会凭空造钱
        for month, row in by_month.items():
            if row.get("items") or not row.get("fee"):
                continue
            item = {"item": "燃气费", "name": "", "money": row["fee"]}
            self._enrich_fee_item(item, row.get("usage"))
            row["items"] = [item]
            fee_items[month] = [item]

        self._fill_ladder_from_price(fee_items)

        self._fill_bill_periods(usage_list)
        # 内部临时字段不往外传（缓存文件里只留卡片用得到的）
        for row in usage_list:
            row.pop("_bill_amt", None)
            row.pop("_bill_id", None)
        return usage_list, fee_items

    _LADDER_CN = {"1": "一", "一": "一", "2": "二", "二": "二", "3": "三", "三": "三", "4": "四", "四": "四"}

    def _enrich_fee_item(self, item: dict[str, Any], usage: Any) -> None:
        """给一条费用补上「阶梯名 / 单价 / 用量」。

        单价优先用接口文案里的「2.19元/m³」；没有就用 金额 ÷ 用量 反推。
        """
        name = str(item.get("name") or "")
        label = name + str(item.get("item") or "")  # 「第1阶 2.19元/m³」+「燃气费」
        price = to_float(item.get("price"))
        if price is None:
            hit = re.search(r"(\d+(?:\.\d+)?)\s*元", name)
            if hit:
                price = float(hit.group(1))
        amount = to_float(item.get("money"))
        # 只有燃气费才谈得上「单价」，服务费/违约金按金额除以气量算出来的
        # 数没有物理意义，别在卡片上显示。
        if price is None and amount and usage and "燃气" in label:
            try:
                if float(usage) > 0:
                    price = round(float(amount) / float(usage), 4)
            except (TypeError, ValueError):
                price = None
        item["price"] = price
        item["usage"] = usage if usage not in ("", None) else None
        item["ladder"] = self._ladder_label(name)

    @classmethod
    def _ladder_label(cls, name: str) -> str:
        """'第1阶 2.19元/m³' / '第一档' -> '第一档'；认不出阶梯就返回空串。"""
        hit = re.search(r"第?\s*([一二三四1-4])\s*(?:阶|档)", name or "")
        if not hit:
            return ""
        return f"第{cls._LADDER_CN.get(hit.group(1), hit.group(1))}档"

    def _fill_ladder_from_price(self, fee_items: dict[str, list[dict[str, Any]]]) -> None:
        """补全每条燃气费的档位名，并把单价对齐到配置里的官方档位单价。

        接口只给最近一期的档位名，其余月份是按金额反推的：跟配置里的三档单价对上号
        才认；反推出来的单价常常只有两位（45.99 ÷ 21 = 2.19），接近就换成配置的
        三位单价（2.196）。对不上号的一律不标档位、不显示单价。
        """
        tier_of_cn = {cn: i for i, cn in enumerate(_LADDER_ORDER)}
        for items in fee_items.values():
            for it in items:
                if "燃气" not in str(it.get("item") or ""):
                    continue
                price = to_float(it.get("price"))
                idx: int | None = None
                # 1) 接口已经给了档位名
                hit = re.search(r"第?\s*([一二三四1-4])\s*(?:阶|档)", str(it.get("ladder") or ""))
                if hit:
                    cn = self._LADDER_CN.get(hit.group(1), hit.group(1))
                    idx = tier_of_cn.get(cn)
                # 2) 与配置的档位单价匹配（以前还有一步「按已知单价排序推档」，
                #    那一步会拿这一期自己的价格去比自己，反而给错档位，已去掉）
                if idx is None and price:
                    idx = next(
                        (i for i, p in enumerate(self.ladder_prices) if p and abs(price - p) <= 0.03),
                        None,
                    )
                if idx is None:
                    # 金额里掺了固定服务费/违约金时，算出来的「单价」对不上任何档位，
                    # 别硬贴一个价格上去，卡片上只显示金额更诚实。
                    if price:
                        it["price"] = None
                    continue
                it["ladder"] = f"第{_LADDER_ORDER[idx]}档"
                official = self.ladder_prices[idx] if idx < len(self.ladder_prices) else None
                if official and (price is None or abs(price - official) <= 0.05):
                    it["price"] = official

    def _fill_bill_periods(self, usage_list: list[dict[str, Any]]) -> None:
        """补全每一期的计费周期。

        账单接口一般只给最近一期的 period，其余账期用相邻两期抄表日推出来：
        「上期抄表日 至 本期抄表日」。接口给了 period 的一律保留接口值。
        """
        asc = sorted(usage_list, key=lambda r: str(r.get("month") or ""))
        for i, row in enumerate(asc):
            if row.get("bill_period") or i == 0:
                continue
            start = self._norm_day(asc[i - 1].get("read_date"))
            end = self._norm_day(row.get("read_date"))
            if start and end:
                row["bill_period"] = f"{start} 至 {end}"

    @staticmethod
    def _pay_fields(p: dict[str, Any]) -> dict[str, Any]:
        """从一条缴费记录里取出「时间 / 燃气费 / 垃圾费 / 违约金 / 合计 / 渠道」。

        payMentRecord 的字段是 paymentDate（毫秒时间戳）、paymentAmount（合计）、
        gasFee（燃气费）、lateFee（违约金）、payChannel、settlementMode。
        垃圾费有两种给法：同一天多一条记录（那条 gasFee 为空、paymentAmount 就是垃圾费），
        或者干脆不漏出来 —— 后一种用「合计 - 燃气费 - 违约金」倒推。
        """
        day = _ts_day(deep_get(p, *KEYS_PAY_DATE)) or to_text(
            deep_get(p, *KEYS_PAY_DATE)
        )
        gas = to_float(deep_get(p, "gasFee", "gasMoney", "gasAmount"))
        service = to_float(deep_get(p, "serviceFee", "serviceAmt", "trashFee", "rubbishFee", "garbageFee"))
        penalty = to_float(deep_get(p, "lateFee", "penalty", "penaltyFee", "wyj", "lateMoney"))
        total = to_float(deep_get(p, "paymentAmount", "payAmount", "money", "amount", "payMoney"))

        items: list[dict[str, Any]] = []
        for it in (deep_get(p, "items", "feeList", "details", "itemList", "subList", "detailList") or []):
            if not isinstance(it, dict):
                continue
            name = to_text(deep_get(it, "item", "name", "feeTypeName", "typeName"))
            money = to_float(deep_get(it, "amount", "money", "fee", "payMoney"))
            if name:
                items.append({"item": name, "money": money or 0})

        if gas is None and items:
            gas = round(sum(float(i["money"]) for i in items
                            if "垃圾" not in str(i["item"]) and "违约金" not in str(i["item"])
                            and "滞纳" not in str(i["item"])), 2)
        if total is None:
            parts = [v for v in (gas, service, penalty) if v]
            total = round(sum(parts), 2) if parts else None

        # 一条记录里同时有燃气费和合计，差额就是同笔代扣的垃圾费
        if service is None and gas and total and total - gas - float(penalty or 0) > 0.005:
            diff = round(float(total) - float(gas) - float(penalty or 0), 2)
            if diff <= 100:
                service = diff

        return {
            "day": day,
            # 具体到秒的缴费时刻（卡片「最近缴费」按「2026/9/20 0:59:41」显示）
            "at": _ts_full(deep_get(p, *KEYS_PAY_DATE)) or day,
            "gas": gas,
            "service": service,
            "penalty": penalty,
            "total": total,
            "items": items,
            "channel": to_text(deep_get(p, "payChannel", "channel", "payType", "payWay", "remark")),
            "settle": to_text(deep_get(p, "settlementMode", "settleWay", "settleType",
                                       "settleName", "deductWay")),
        }

    def _fill_fee_from_bill_amt(self, by_month: dict[str, dict[str, Any]]) -> None:
        """用账单金额（billAmt）补上没有燃气费的月份。

        billAmt 是「该期账单金额」，各租户口径不完全一样：有的直接就是燃气费，
        有的含随气费一起代扣的垃圾费（同户通常是个固定值）。先用已经有燃气费的
        月份（来自交费记录 / 逐期详情）反推口径，再套到缺金额的月份上；
        判不出口径就不减，只把 billAmt 当金额用，绝不凭空调价。
        """
        diffs: list[float] = []
        for row in by_month.values():
            amt = to_float(row.get("_bill_amt"))
            fee = to_float(row.get("fee"))
            if amt and fee and float(amt) >= float(fee) - 0.01:
                diff = round(float(amt) - float(fee), 2)
                diffs.append(0.0 if -0.02 <= diff <= 0.02 else diff)

        extra: float | None = None
        if diffs:
            counts: dict[float, int] = {}
            for diff in diffs:
                counts[diff] = counts.get(diff, 0) + 1
            best = max(counts, key=lambda v: counts[v])
            # 至少两期一致才认，且差额落在垃圾费的合理区间，否则当作没判出来
            if counts[best] >= 2 and (best == 0.0 or 0.5 <= best <= 30):
                extra = best
                _LOGGER.debug("账单金额口径：每期含固定费用 %.2f 元", extra)

        for row in by_month.values():
            if row.get("fee"):
                continue
            amt = to_float(row.get("_bill_amt"))
            if not amt or float(amt) <= 0:
                continue
            # 口径没判出来时，退一步用这一期自己已知的垃圾费，避免重复计入
            cut = extra if extra is not None else (to_float(row.get("garbage_fee")) or 0.0)
            fee = round(float(amt) - float(cut), 2)
            if fee <= 0:
                continue
            row["fee"] = fee
            if extra:
                row.setdefault("garbage_fee", extra)

    def _pick_unit_price(self, rows: Any, strict: bool = False) -> float | None:
        """从各期「燃气费 ÷ 用气量」里挑一个最可信的单价（众数，贴近配置价就用配置价）。

        ``strict``：只认能对上配置档位价的结果。拿它去反查交费记录该归到哪个月
        时要用严格模式——某一期的金额里掺了垃圾费，除出来的「单价」是假的，
        拿它去比会把交费记录匹配到错误的月份。
        """
        counts: dict[float, int] = {}
        for row in rows:
            usage = row.get("usage")
            fee = row.get("fee")
            if not usage or not fee:
                continue
            try:
                price = round(float(fee) / float(usage), 4)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            counts[price] = counts.get(price, 0) + 1
        if not counts:
            return None
        best = max(counts, key=lambda price: (counts[price], -price))
        for official in self.ladder_prices:
            if official and abs(best - float(official)) <= 0.03:
                return float(official)
        return None if strict else best

    def _fill_fee_from_payments(
        self, by_month: dict[str, dict[str, Any]], payments: list[Any]
    ) -> None:
        """把交费记录里的「燃气费」补进对应月份（垃圾费、违约金不计入气费）。"""
        price = self._pick_unit_price(by_month.values(), strict=True)

        for p in payments:
            if not isinstance(p, dict):
                continue
            fields = self._pay_fields(p)
            month = self._norm_month(fields["day"])
            if not month:
                continue
            gas_fee = fields["gas"]
            if gas_fee is None:
                gas_fee = fields["total"]
            if gas_fee is None:
                continue
            # 违约金不算气费，扣掉后再去找月份
            if fields["penalty"] and fields["total"] and gas_fee == fields["total"]:
                gas_fee = round(float(gas_fee) - float(fields["penalty"]), 2)

            target = self._pick_month(by_month, month, gas_fee, price)
            if target is None:
                continue
            row = by_month[target]
            if row.get("fee") is None:
                row["fee"] = gas_fee
            row["status"] = row.get("status") or "已缴费"

    @staticmethod
    def _pick_month(
        by_month: dict[str, dict[str, Any]], month: str, amount: float, price: float | None
    ) -> str | None:
        """交费日期通常比计费月晚几天：在当月/上月里挑气量×单价最接近交费额的一个。

        月初交费一般是结上月账单，所以优先看上月；月末交费则优先看当月。
        """
        try:
            base = datetime.strptime(month + "-01", "%Y-%m-%d")
        except ValueError:
            return month if month in by_month else None
        prev = (base - timedelta(days=1)).strftime("%Y-%m")
        order = [prev, month] if base.day <= 20 else [month, prev]
        candidates = [m for m in order if m in by_month]
        if not candidates:
            return None
        if price is None or amount <= 0:
            return candidates[0]

        def score(m: str) -> float:
            usage = by_month[m].get("usage")
            if not usage:
                return float("inf")
            return abs(float(usage) * price - amount)

        best = min(candidates, key=score)
        # 只有「像」的时候才认，否则放回交费当月
        if score(best) <= max(1.0, amount * 0.08):
            return best
        return candidates[0] if candidates[0] == month else None

    def _build_payment_list(self, payments: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in payments:
            if not isinstance(p, dict):
                continue
            fields = self._pay_fields(p)
            if fields["total"] is None and not fields["day"]:
                continue
            out.append({
                "time": fields["day"],
                "at": fields["at"],
                "amount": fields["total"],
                "gas": fields["gas"],
                "service": fields["service"],
                "penalty": fields["penalty"],
                "channel": fields["channel"] or "-",
                "settle": fields["settle"] or "",
                "settle_way": fields["settle"] or "",
                "items": fields["items"],
            })
        return out
