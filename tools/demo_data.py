"""生成演示数据：合成一份假的接口返回 -> 归一化 -> 合并进缓存文件。

用途有两个：
1. 本地预览卡片（不需要真实账号）；
2. 演示「只增不删」的缓存合并：第二段故意只给最近 6 期，合并后历史仍然完整。

运行：python tools/demo_data.py
产出：tools/demo_cache.json（缓存文件样例）、tools/demo_attrs.json（实体属性）、
      preview.html 里的演示数据块。
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "custom_components", "kaiyuan_gas")

# ---------------------------------------------------------------- stub homeassistant
ha = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
helpers = types.ModuleType("homeassistant.helpers")
uc = types.ModuleType("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass


class UpdateFailed(Exception):
    pass


uc.DataUpdateCoordinator = DataUpdateCoordinator
uc.UpdateFailed = UpdateFailed
sys.modules.update({
    "homeassistant": ha,
    "homeassistant.core": core,
    "homeassistant.helpers": helpers,
    "homeassistant.helpers.update_coordinator": uc,
})

pkg = types.ModuleType("kgpkg")
pkg.__path__ = [PKG_DIR]
sys.modules["kgpkg"] = pkg
coordinator_mod = importlib.import_module("kgpkg.coordinator")
cache_mod = importlib.import_module("kgpkg.cache")

# ---------------------------------------------------------------- 合成接口数据
PRICE = 2.19          # 元/m³
GARBAGE = 8.0         # 元/月
DEMO_CONS_NO = "0000000000"
DEMO_METER_NO = "000000000000"

# 24 期的用气量（从最新一期往前）
USAGES = [
    21, 6, 8, 11, 15, 18, 20, 24, 27, 22, 17, 9,
    6, 5, 8, 12, 14, 16, 19, 23, 26, 21, 15, 10,
]


def month_list(count: int) -> list[str]:
    """从 2026-09 往前数 count 个月。"""
    start = dt.date(2026, 9, 1)
    out = []
    y, m = start.year, start.month
    for _ in range(count):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


MONTHS = month_list(len(USAGES))
READINGS: list[dict] = []
TOTAL = 500
for idx, month in enumerate(MONTHS):
    usage = USAGES[idx]
    READINGS.append({
        "meterNo": DEMO_METER_NO,
        "readDate": f"{month}-24",
        "lastReadNum": str(TOTAL - usage),
        "readNum": str(TOTAL),
        "gasNum": str(usage),
        "readWay": "远程",
    })
    TOTAL += USAGES[idx + 1] if idx + 1 < len(USAGES) else 10

PAYMENTS = []
for month, usage in zip(MONTHS, USAGES):
    y, m = int(month[:4]), int(month[5:])
    pay_day = dt.date(y + (m // 12), m % 12 + 1, 1)
    PAYMENTS.append({
        "payDate": f"{pay_day.isoformat()} 00:59:41",
        "payMoney": f"{round(usage * PRICE, 2)}",
        "payType": "第三方",
        "settleWay": "支付宝代扣",
        "items": [{"item": "燃气费", "money": f"{round(usage * PRICE, 2)}"}],
    })
    PAYMENTS.append({
        "payDate": f"{pay_day.isoformat()} 00:59:41",
        "payMoney": f"{GARBAGE}",
        "payType": "第三方",
        "settleWay": "支付宝代扣",
        "items": [{"item": "垃圾费", "money": f"{GARBAGE}"}],
    })

latest_month = MONTHS[0]
latest_usage = USAGES[0]
latest_fee = round(latest_usage * PRICE, 2)
RAW = {
    "cons_no": DEMO_CONS_NO,
    "user": {"userName": "示例用户", "address": "示例小区 1 栋 101", "mobile": "138****0000"},
    "account": {"balance": "0.00"},
    "arrearage": None,
    "meters": READINGS,
    "payments": PAYMENTS,
    "bills": [
        {
            "billMonth": latest_month,
            "shouldPay": f"{latest_fee}",
            "realMoney": f"{latest_fee}",
            "settleFlag": "1",
            "billPeriod": f"{MONTHS[1]}-24 至 {MONTHS[0]}-24",
            "days": "31",
            "penalty": "0",
            "penaltyStartDate": "",
            "feeTypeName": "燃气费",
            "priceName": f"第1阶 {PRICE}元/m³",
        }
    ],
}

entry = types.SimpleNamespace(
    data={"mobile": "13800000000", "password": "demo", "cons_no": DEMO_CONS_NO},
    options={"refresh_hours": 24},
    entry_id="demo",
)
coordinator = coordinator_mod.KaiyuanGasCoordinator(None, entry)

# ---------------------------------------------------------------- 离线跑一遍
live = coordinator._normalize(RAW)  # noqa: SLF001
doc = cache_mod.merge(None, live)

# 模拟「第二次在线只返回最近 6 期」：合并后历史不能丢
slim = json.loads(json.dumps(live))
slim["usage_list"] = slim["usage_list"][:6]
slim["payment_days"] = slim["payment_days"][:12]
doc = cache_mod.merge(doc, slim)

data = coordinator._with_ladder(cache_mod.to_data(doc))  # noqa: SLF001

cache_out = os.path.join(ROOT, "tools", "demo_cache.json")
attrs_out = os.path.join(ROOT, "tools", "demo_attrs.json")
with open(cache_out, "w", encoding="utf-8") as fp:
    json.dump(doc, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
with open(attrs_out, "w", encoding="utf-8") as fp:
    json.dump(data, fp, ensure_ascii=False, indent=1)

print(f"缓存文件 -> {cache_out}")
print(f"  billList {len(doc['billList'])} 期 / rechargeList {len(doc['rechargeList'])} 行")
print(f"  statistics {doc['statistics']}")
print(f"实体属性 -> {attrs_out}")
print(f"  最新一期 {data['usage_list'][0]}")
print(f"  最近缴费 {data['last_payment_date']} 合计 {data['last_payment_amount']}")
