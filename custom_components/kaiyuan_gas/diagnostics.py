"""诊断信息。

除了配置和归一化后的数据，还会附上「在线接口的原始返回」：
凯源这套接口偶发字段改名（同一个数据在不同模块叫 beginNum / startNum），
出问题时把诊断文件导出来，就能直接看到真实字段名，不用再猜。
敏感字段（手机号、户名、地址、户号等）在导出前统一打码。
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MOBILE, CONF_PASSWORD, DOMAIN

# 这些 key 的值属于个人信息，导出前打码
_SENSITIVE = {
    "mobile", "phone", "tel", "telephone", "linkPhone", "userPhone",
    "userName", "custName", "consName", "consNameSecret", "customerName", "realName",
    "address", "addr", "addrSecret", "consAddress", "addressDetail", "homeAddress",
    "consNo", "cardNum", "accountNo", "cardNo", "meterNo", "meterCode",
    "idCard", "certNo", "openId", "unionId", "token", "password", "pwd",
}


def _mask(value: Any, key: str = "", strict: bool = False) -> Any:
    """递归打码；同时保留字段结构和字段名。

    strict 用在「用户 / 账户」对象上：那里出现的 name 一定是人名。
    账单明细里的 name 是「燃气费」「第一档」这类文案，必须原样保留才能看语义。
    """
    if isinstance(value, dict):
        return {k: _mask(v, k, strict) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, key, strict) for v in value]
    sensitive = key in _SENSITIVE or (strict and key == "name")
    if sensitive and value not in (None, "", 0):
        text = str(value)
        if len(text) <= 2:
            return "**"
        return text[0] + "*" * (len(text) - 2) + text[-1]
    return value


def _shape(value: Any, depth: int = 0) -> Any:
    """只保留字段名和类型，用来一眼看清「接口到底返回了哪些字段」。"""
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = dict(coordinator.data or {})
    data.pop("raw", None)

    raw = getattr(coordinator, "last_raw", None) or {}
    raw_view = {
        key: {
            "count": len(value) if isinstance(value, list) else None,
            "shape": _shape(value),
        }
        for key, value in raw.items()
        if key in ("account", "nc_bill", "arrearage", "bills", "payments", "meters",
                   "bill_details", "user")
    }

    last_refresh = getattr(coordinator, "last_refresh", None)
    if last_refresh is not None and hasattr(last_refresh, "isoformat"):
        last_refresh = last_refresh.isoformat(timespec="seconds")

    return {
        "refresh": {
            "last_refresh": last_refresh,
            "data_source": data.get("data_source"),
            "login_status": data.get("login_status"),
            "last_error": data.get("last_error"),
            "cache_updated_at": data.get("cache_updated_at"),
            "cache_file": data.get("cache_file"),
        },
        # 每个接口的入参、返回条数、字段名：接口改名或参数变了，看这里最直接
        "endpoints": _mask(raw.get("debug") or {}),
        "cache": {
            "bill_count": data.get("bill_count"),
            "payment_count": len(data.get("payment_days") or []),
            "latest_bill": (data.get("usage_list") or [{}])[0],
            "statistics": data.get("statistics"),
        },
        "entry": {
            "data": {**entry.data, CONF_PASSWORD: "**REDACTED**"},
            "options": dict(entry.options),
        },
        "mobile": entry.data.get(CONF_MOBILE),
        "coordinator": data,
        "raw_shape": raw_view,
        "raw_sample": _mask(
            {k: raw.get(k) for k in ("account", "arrearage", "user")}, strict=True
        ),
        "raw_records": _mask({
            "nc_bill": raw.get("nc_bill") or {},
            "bills": (raw.get("bills") or [])[:3],
            # 「用气地址 / 户名 / 未缴期费用构成」的数据源，字段名对不上时先看这里
            "bill_details": (raw.get("bill_details") or [])[:3],
            "payments": (raw.get("payments") or [])[:3],
            "meters": (raw.get("meters") or [])[:3],
        }),
    }
