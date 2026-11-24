"""本地缓存：把每次在线查询的结果「合并」进一份可读的 JSON 文件。

设计要点
--------
1. 文件放在 Home Assistant 配置目录下，文件名 ``kaiyuan_gas_info_<户号>.json``，
   纯 JSON + 缩进，方便直接用编辑器打开查看、备份、导出。
2. 只增不删：在线接口往往只返回最近一年（甚至最近几期），如果每次都用新结果覆盖，
   历史账期就会丢。这里按「账期（月）」和「缴费日」做键，新数据覆盖同键的字段，
   没出现过的键原样保留，因此越用历史越长。
3. 每条记录带 ``source_updated_at``，能看出这条数据是哪次在线查询更新的。
4. 文件即数据源：实体属性与卡片都从这份缓存派生（见 :func:`to_data`），
   在线失败时卡片展示的仍是文件里的内容。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .const import CACHE_PREFIX, CACHE_SUFFIX

_LOGGER = logging.getLogger(__name__)

SCHEMA = "kaiyuan-gas-cache/1"
SOURCE = "kaiyuan-gas-ha"

# 账户级的标量字段：在线查到非空值就更新
SCALAR_FIELDS = (
    "cons_no",
    "customer_name",
    "address",
    "meter_no",
    "unit_price",
    "garbage_fee",
    "settle_way",
    "read_way",
    "balance",
    "arrearage",
)

# 单期账单里会随在线数据刷新的字段
BILL_FIELDS = (
    "prev_reading",
    "reading",
    "usage",
    "fee",
    "total_fee",
    "garbage_fee",
    "penalty",
    "penalty_start",
    "status",
    "settled",
    "estimated",
    "bill_period",
    "bill_days",
    "read_date",
    "read_way",
    "items",
)

# 单次缴费（按缴费日合并后）的字段
RECHARGE_FIELDS = ("gas", "garbage", "penalty", "total", "settle_way", "channel", "at")

# 阶梯气价档位（按自然年累计用气量划分，可在文件里手工改）
LADDER = {"ladder1": "0-500", "ladder2": "501-1000", "ladder3": "1001+"}


def now_str() -> str:
    """人类可读的本地时间，写进文件里方便排查。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cache_path(hass: Any, cons_no: str | None) -> Path:
    """缓存文件路径：<配置目录>/kaiyuan_gas_info_<户号>.json。"""
    root = Path(hass.config.path())
    return root / f"{CACHE_PREFIX}{cons_no or 'unknown'}{CACHE_SUFFIX}"


def empty() -> dict[str, Any]:
    """空缓存骨架（写盘时的字段顺序也按这个来）。"""
    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "cons_no": None,
        "customer_name": None,
        "address": None,
        "meter_no": None,
        "unit_price": None,
        "garbage_fee": None,
        "settle_way": None,
        "read_way": None,
        "balance": None,
        "arrearage": None,
        "last_payment": {},
        "billList": [],
        "rechargeList": [],
        "statistics": {},
        "updated_at": None,
        "cache_updated_at": None,
    }


# --------------------------------------------------------------------------- 读写
def load(path: Path) -> dict[str, Any] | None:
    """读取缓存文件；不存在或损坏都返回 None（不阻断在线查询）。"""
    try:
        with open(path, encoding="utf-8") as fp:
            doc = json.load(fp)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:  # noqa: BLE001
        _LOGGER.warning("缓存文件解析失败，将重新生成：%s (%s)", path, err)
        return None
    if not isinstance(doc, dict):
        return None
    doc.setdefault("billList", [])
    doc.setdefault("rechargeList", [])
    return doc


def save(path: Path, doc: dict[str, Any]) -> None:
    """原子写入（先写临时文件再替换），避免断电留半个文件。"""
    payload = deepcopy(doc)
    payload["schema"] = SCHEMA
    payload["source"] = SOURCE
    payload["cache_updated_at"] = now_str()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- 合并
def _merge_row(old: dict[str, Any] | None, new: dict[str, Any], fields: tuple[str, ...],
               stamp: str) -> dict[str, Any]:
    row = dict(old or {})
    for key in fields:
        value = new.get(key)
        if value is None or value == "" or value == [] or value == {}:
            continue
        row[key] = value
    row["source_updated_at"] = stamp
    return row


def merge(old: dict[str, Any] | None, live: dict[str, Any], stamp: str | None = None) -> dict[str, Any]:
    """把一次在线结果合并进旧缓存，返回新的缓存文档（不改动入参）。"""
    stamp = stamp or now_str()
    doc = deepcopy(old) if old else empty()
    doc.setdefault("billList", [])
    doc.setdefault("rechargeList", [])

    for key in SCALAR_FIELDS:
        value = live.get(key)
        if value is not None and value != "":
            doc[key] = value

    # ---- 账期（月）合并 ----
    bills: dict[str, dict[str, Any]] = {
        str(row.get("month")): row for row in doc["billList"] if row.get("month")
    }
    for row in live.get("usage_list") or []:
        month = row.get("month")
        if not month:
            continue
        merged = _merge_row(bills.get(month), row, BILL_FIELDS, stamp)
        merged["month"] = month  # 主键单独补回（字段过滤会漏掉它）
        # 布尔字段要能翻回去：这一期结清、拿到真实金额后，「估算」标记必须清掉，
        # 否则卡片会一直挂着小字「按单价估算」。
        merged["estimated"] = bool(row.get("estimated"))
        bills[month] = {"month": month, **merged}
    doc["billList"] = sorted(bills.values(), key=lambda r: str(r.get("month")), reverse=True)

    # ---- 缴费（按日）合并 ----
    recharges: dict[str, dict[str, Any]] = {
        str(row.get("date")): row for row in doc["rechargeList"] if row.get("date")
    }
    for row in live.get("payment_days") or []:
        day = row.get("date")
        if not day:
            continue
        if not row.get("settle_way") and row.get("settle"):
            row = {**row, "settle_way": row["settle"]}
        merged = _merge_row(recharges.get(day), row, RECHARGE_FIELDS, stamp)
        merged["date"] = day
        recharges[day] = {"date": day, **merged}
    doc["rechargeList"] = sorted(recharges.values(), key=lambda r: str(r.get("date")), reverse=True)

    # ---- 最近缴费快照 ----
    if doc["rechargeList"]:
        latest = doc["rechargeList"][0]
        doc["last_payment"] = {
            "date": latest.get("date"),
            "at": latest.get("at"),
            "gas": latest.get("gas"),
            "garbage": latest.get("garbage"),
            "penalty": latest.get("penalty"),
            "total": latest.get("total"),
            "settle_way": latest.get("settle_way"),
        }

    doc["updated_at"] = stamp
    doc["statistics"] = statistics(doc)
    return doc


def _row_total(row: dict[str, Any]) -> float:
    """一期的应缴总额：有「本期合计」就用它，否则气费 + 服务(垃圾)费。"""
    value = row.get("total_fee")
    if value not in (None, ""):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    try:
        return float(row.get("fee") or 0) + float(row.get("garbage_fee") or 0)
    except (TypeError, ValueError):
        return 0.0


def statistics(doc: dict[str, Any]) -> dict[str, Any]:
    """从合并后的列表重算汇总值（文件里也存一份，肉眼可核对）。"""
    bills = doc.get("billList") or []
    recharges = doc.get("rechargeList") or []
    year = datetime.now().strftime("%Y")
    # 本年 = 账期落在今年；用来把「全部累计」和「本年累计」分开，两者差一年以上
    this_year = [b for b in bills if str(b.get("month") or "").startswith(year)]
    total_usage = round(sum(float(b.get("usage") or 0) for b in bills), 3)
    total_cost = round(sum(_row_total(b) for b in bills), 2)
    total_paid = round(sum(float(r.get("total") or 0) for r in recharges), 2)
    return {
        "bill_count": len(bills),
        "recharge_count": len(recharges),
        "total_usage": total_usage,
        "total_cost": total_cost,
        "total_paid": total_paid,
        "year": year,
        "year_bill_count": len(this_year),
        "year_usage": round(sum(float(b.get("usage") or 0) for b in this_year), 3),
        "year_cost": round(sum(_row_total(b) for b in this_year), 2),
        "first_month": bills[-1].get("month") if bills else None,
        "last_month": bills[0].get("month") if bills else None,
    }


def _real_at(value: Any) -> Any:
    """只保留真实存在的时刻。

    缴费接口的项目里 `paymentDate` 是「当天 0 点」的毫秒时间戳，格式化出来就是
    `2026-10-01 00:00:00`——这不是缴费时刻，只是没有时刻。0 点整一律退化成日期，
    免得界面把假时间当真的显示。
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) >= 19 and text[10] in " T" and text[11:19] in ("00:00:00", "0:00:00"):
        return text[:10]
    return text


# --------------------------------------------------------------------------- 输出
def to_data(doc: dict[str, Any]) -> dict[str, Any]:
    """把缓存文档摊平成实体属性 / 卡片要用的结构。"""
    bills = list(doc.get("billList") or [])
    recharges = list(doc.get("rechargeList") or [])

    usage_list: list[dict[str, Any]] = []
    fee_items_by_month: dict[str, list[dict[str, Any]]] = {}
    for row in bills:
        month = row.get("month")
        if not month:
            continue
        usage_list.append({
            "month": month,
            "prev_reading": row.get("prev_reading"),
            "reading": row.get("reading"),
            "usage": row.get("usage"),
            "fee": row.get("fee"),
            "total_fee": row.get("total_fee"),
            "garbage_fee": row.get("garbage_fee"),
            "penalty": row.get("penalty"),
            "status": row.get("status") or "",
            "bill_period": row.get("bill_period"),
            "read_date": row.get("read_date"),
            "read_way": row.get("read_way"),
            "settled": row.get("settled"),
            # 这期的燃气费是按「单价 × 用量」估的（未结清账期公众号还没生成明细）
            "estimated": bool(row.get("estimated")),
        })
        if row.get("items"):
            fee_items_by_month[month] = row["items"]

    payment_days = [
        {
            "date": row.get("date"),
            "at": _real_at(row.get("at") or row.get("date")),
            "gas": row.get("gas"),
            "garbage": row.get("garbage"),
            "penalty": row.get("penalty"),
            "total": row.get("total"),
            "channel": row.get("channel") or "-",
            "settle": row.get("settle_way") or "",
        }
        for row in recharges
    ]

    latest = bills[0] if bills else {}
    last_pay = doc.get("last_payment") or (payment_days[0] if payment_days else {})

    total_usage = round(sum(float(b.get("usage") or 0) for b in bills), 3)
    total_cost = round(sum(_row_total(b) for b in bills), 2)
    total_paid = round(sum(float(r.get("total") or 0) for r in recharges), 2)
    unpaid = [b for b in bills if b.get("status") == "未缴费"]

    year = datetime.now().strftime("%Y")
    annual = round(sum(float(b.get("usage") or 0) for b in bills
                       if str(b.get("month", "")).startswith(year)), 3)
    # 本年累计费用：只算账期在今年的那些期，和「累计费用」（缓存里全部账期）区分开
    year_bills = [b for b in bills if str(b.get("month", "")).startswith(year)]
    annual_cost = round(sum(_row_total(b) for b in year_bills), 2)
    ladder_now = 3 if annual > 1000 else 2 if annual > 500 else 1

    garbage = doc.get("garbage_fee")
    latest_fee = latest.get("fee")
    receivable = latest_fee
    total_fee = None
    if latest.get("total_fee") is not None:
        # 账单查询接口直接给了「本期合计」，不用自己拼
        try:
            total_fee = round(float(latest["total_fee"]), 2)
        except (TypeError, ValueError):
            total_fee = None
    if total_fee is None and latest_fee is not None:
        # 单期自带的服务(垃圾)费优先，没有再退回账级垃圾费
        row_garbage = latest.get("garbage_fee")
        extra = row_garbage if row_garbage is not None else garbage
        total_fee = round(float(latest_fee) + float(extra or 0), 2)
    if total_fee is None and doc.get("arrearage") is not None:
        # 最新一期的燃气费没取到（未结清账期常见），按账上欠费展示，比 ¥- 有用。
        # 欠费接口给的通常已经是「含垃圾费」的应缴总额，这里不再叠加垃圾费。
        try:
            receivable = float(doc["arrearage"])
        except (TypeError, ValueError):
            receivable = None
        total_fee = receivable

    return {
        # 账户
        "cons_no": doc.get("cons_no"),
        "customer_name": doc.get("customer_name"),
        "address": doc.get("address"),
        "meter_no": doc.get("meter_no"),
        "balance": doc.get("balance"),
        "arrearage": doc.get("arrearage"),
        # 最新一期
        "bill_month": latest.get("month"),
        "pay_status": latest.get("status") or "",
        "estimated": bool(latest.get("estimated")),
        "reading": latest.get("reading"),
        "prev_reading": latest.get("prev_reading"),
        "usage": latest.get("usage"),
        "reading_date": latest.get("read_date"),
        "read_way": latest.get("read_way") or doc.get("read_way"),
        "bill_period": latest.get("bill_period"),
        "bill_days": latest.get("bill_days"),
        # 这一期单独的燃气费（不含垃圾费、违约金），「本期气费」实体用它；
        # 含税费的应缴总额仍是下面的 total_fee
        "fee": latest_fee,
        "penalty": latest.get("penalty"),
        "penalty_start": latest.get("penalty_start"),
        "settled": latest.get("settled"),
        "receivable": receivable,
        "received": receivable if latest.get("settled") else None,
        "total_fee": total_fee,
        # 缴费
        "last_payment_amount": last_pay.get("total"),
        "last_payment_date": last_pay.get("date"),
        # 带时刻的版本（2026-09-20，公众号只给日期；真有时刻才带上）
        "last_payment_at": _real_at(last_pay.get("at") or last_pay.get("date")),
        "last_payment_gas": last_pay.get("gas"),
        "last_payment_garbage": last_pay.get("garbage"),
        "last_payment_penalty": last_pay.get("penalty"),
        "settle_way": doc.get("settle_way") or last_pay.get("settle_way"),
        # 计费参数
        "unit_price": doc.get("unit_price"),
        "garbage_fee": garbage,
        # 汇总
        "bill_count": len(bills),
        "unpaid_count": len(unpaid),
        # 「累计费用」优先用逐期账单加总；万一某期费用没取到，就退回累计缴费额，
        # 卡片上至少有个能对的数，而不是整块消失。
        "cumulative_usage": total_usage or None,
        "cumulative_fee": total_cost or total_paid or None,
        "cumulative_paid": total_paid or None,
        # 本年口径（阶梯、卡片「本年累计」用）：只含今年账期，跟「累计」不是一回事
        "year": year,
        "year_bill_count": len(year_bills),
        "cumulative_usage_year": annual or None,
        "cumulative_fee_year": annual_cost or None,
        "tier_usage": annual,
        "now_ladder": ladder_now,
        "ladder": dict(LADDER),
        "statistics": doc.get("statistics") or statistics(doc),
        # 列表
        "usage_list": usage_list,
        "payment_days": payment_days,
        "fee_items_by_month": fee_items_by_month,
        # 元信息
        "cache_updated_at": doc.get("cache_updated_at"),
        "updated_at": doc.get("updated_at"),
    }
