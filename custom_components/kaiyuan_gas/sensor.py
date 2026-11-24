"""传感器平台。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

try:  # 新版 HA 把 EntityCategory 收回 homeassistant.const
    from homeassistant.const import EntityCategory
except ImportError:  # pragma: no cover - 兼容旧版本
    from homeassistant.helpers.entity import EntityCategory

from .const import CONF_CONS_NO, CONF_MOBILE, DOMAIN, MANUFACTURER, MODEL
from .coordinator import KaiyuanGasCoordinator

# monetary 传感器用 ISO 4217 代码。旧版 HA 的 homeassistant.const.CURRENCY_YUAN
# 已在新版本里移除（导入就会 ImportError），这里直接用字符串，各版本通用。
CURRENCY_YUAN = "CNY"


@dataclass(frozen=True, kw_only=True)
class KaiyuanSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any] = lambda data: None
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda data: {}


def _as_datetime(value: Any) -> datetime | None:
    """时间戳实体要的是 datetime，缓存/接口里存的可能是字符串。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return dt_util.parse_datetime(str(value))


SENSORS: tuple[KaiyuanSensorDescription, ...] = (
    KaiyuanSensorDescription(
        key="arrearage",
        translation_key="arrearage",
        name="本期气费",
        icon="mdi:cash-clock",
        native_unit_of_measurement=CURRENCY_YUAN,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        # 只给这一期的燃气费，不含垃圾费和违约金；含税费的应缴总额放在 total_fee 属性里
        value_fn=lambda d: d.get("fee"),
        attrs_fn=lambda d: {
            "total_fee": d.get("total_fee"),
            "garbage_fee": d.get("garbage_fee"),
            "penalty": d.get("penalty"),
            "unpaid_count": d.get("unpaid_count"),
            "estimated": d.get("estimated"),
        },
    ),
    KaiyuanSensorDescription(
        key="usage",
        translation_key="usage",
        name="本期气量",
        icon="mdi:fire",
        native_unit_of_measurement="m³",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("usage"),
    ),
    KaiyuanSensorDescription(
        key="cumulative_fee",
        translation_key="cumulative_fee",
        name="全部累计费用",
        icon="mdi:cash-multiple",
        native_unit_of_measurement=CURRENCY_YUAN,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        # 缓存里所有账期（只增不删）的「气费 + 垃圾费」之和，跨年份
        value_fn=lambda d: d.get("cumulative_fee"),
        attrs_fn=lambda d: {
            "cumulative_paid": d.get("cumulative_paid"),
            "bill_count": d.get("bill_count"),
            "first_month": (d.get("statistics") or {}).get("first_month"),
            "last_month": (d.get("statistics") or {}).get("last_month"),
        },
    ),
    KaiyuanSensorDescription(
        key="cumulative_fee_year",
        translation_key="cumulative_fee_year",
        name="本年累计费用",
        icon="mdi:calendar-range",
        native_unit_of_measurement=CURRENCY_YUAN,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        # 只算账期落在今年的那些期（阶梯也是按自然年算的，两者口径一致）
        value_fn=lambda d: d.get("cumulative_fee_year"),
        attrs_fn=lambda d: {
            "year": d.get("year"),
            "bill_count": d.get("year_bill_count"),
            "year_usage": d.get("cumulative_usage_year"),
        },
    ),
    KaiyuanSensorDescription(
        key="cumulative_usage_year",
        translation_key="cumulative_usage_year",
        name="本年累计用量",
        icon="mdi:calendar-month",
        native_unit_of_measurement="m³",
        device_class=SensorDeviceClass.GAS,
        suggested_display_precision=3,
        # 只算账期落在今年的那些期；跨年归零，所以用 MEASUREMENT 而不是 TOTAL_INCREASING
        value_fn=lambda d: d.get("cumulative_usage_year"),
        attrs_fn=lambda d: {
            "year": d.get("year"),
            "bill_count": d.get("year_bill_count"),
            "year_cost": d.get("cumulative_fee_year"),
        },
    ),
    KaiyuanSensorDescription(
        key="reading",
        translation_key="reading",
        name="最新读数",
        icon="mdi:gauge",
        native_unit_of_measurement="m³",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda d: d.get("reading"),
        # 主实体：卡片只挂这一个实体（最新读数本身就是累计用量，不再单列累计用量传感器），
        # 全部明细（逐期账单、缴费日、费用构成）都从属性里取
        attrs_fn=lambda d: {
            "cons_no": d.get("cons_no"),
            "customer_name": d.get("customer_name"),
            "address": d.get("address"),
            "mobile": d.get("mobile"),
            "bill_month": d.get("bill_month"),
            "pay_status": d.get("pay_status"),
            "balance": d.get("balance"),
            "arrearage": d.get("arrearage"),
            "unpaid_count": d.get("unpaid_count"),
            "usage": d.get("usage"),
            "reading": d.get("reading"),
            "prev_reading": d.get("prev_reading"),
            "reading_date": d.get("reading_date"),
            "meter_no": d.get("meter_no"),
            "bill_period": d.get("bill_period"),
            "read_way": d.get("read_way"),
            "receivable": d.get("receivable"),
            "received": d.get("received"),
            "total_fee": d.get("total_fee"),
            "cumulative_usage": d.get("cumulative_usage"),
            "cumulative_fee": d.get("cumulative_fee"),
            "cumulative_fee_year": d.get("cumulative_fee_year"),
            "cumulative_usage_year": d.get("cumulative_usage_year"),
            "year": d.get("year"),
            "cumulative_paid": d.get("cumulative_paid"),
            "settled": d.get("settled"),
            "penalty": d.get("penalty"),
            "penalty_start": d.get("penalty_start"),
            "prev_unpaid": d.get("prev_unpaid"),
            "cur_unpaid": d.get("cur_unpaid"),
            "bill_days": d.get("bill_days"),
            "unit_price": d.get("unit_price"),
            "garbage_fee": d.get("garbage_fee"),
            "settle_way": d.get("settle_way"),
            "last_pay_time": d.get("last_payment_date"),
            "last_pay_at": d.get("last_payment_at"),
            "last_pay_amount": d.get("last_payment_amount"),
            "last_pay_gas": d.get("last_payment_gas"),
            "last_pay_garbage": d.get("last_payment_garbage"),
            "last_pay_penalty": d.get("last_payment_penalty"),
            "tier_usage": d.get("tier_usage"),
            "now_ladder": d.get("now_ladder"),
            "ladder": d.get("ladder"),
            "updated_at": d.get("updated_at"),
            "usage_list": d.get("usage_list", []),
            "payment_days": d.get("payment_days", []),
            "fee_items_by_month": d.get("fee_items_by_month", {}),
            "statistics": d.get("statistics") or {},
            "cache_file": d.get("cache_file"),
            "cache_updated_at": d.get("cache_updated_at"),
            "last_refresh": d.get("last_refresh"),
        },
    ),
    KaiyuanSensorDescription(
        key="prev_reading",
        translation_key="prev_reading",
        name="上期读数",
        icon="mdi:gauge-low",
        native_unit_of_measurement="m³",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda d: d.get("prev_reading"),
        attrs_fn=lambda d: {"reading_date": d.get("reading_date")},
    ),
    KaiyuanSensorDescription(
        key="bill_month",
        translation_key="bill_month",
        name="账单月份",
        icon="mdi:calendar-month",
        value_fn=lambda d: d.get("bill_month"),
        attrs_fn=lambda d: {
            "bill_period": d.get("bill_period"),
            "reading_date": d.get("reading_date"),
        },
    ),
    KaiyuanSensorDescription(
        key="pay_status",
        translation_key="pay_status",
        name="缴费情况",
        icon="mdi:check-decagram-outline",
        value_fn=lambda d: d.get("pay_status") or None,
        attrs_fn=lambda d: {
            "unpaid_count": d.get("unpaid_count"),
            "arrearage": d.get("arrearage"),
            "settled": d.get("settled"),
        },
    ),
    KaiyuanSensorDescription(
        key="last_payment",
        translation_key="last_payment",
        name="最近缴费金额",
        icon="mdi:receipt-text-check",
        native_unit_of_measurement=CURRENCY_YUAN,
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("last_payment_amount"),
        attrs_fn=lambda d: {"pay_date": d.get("last_payment_date")},
    ),
    KaiyuanSensorDescription(
        key="last_payment_date",
        translation_key="last_payment_date",
        name="最近缴费时间",
        icon="mdi:calendar-clock",
        value_fn=lambda d: d.get("last_payment_date"),
        attrs_fn=lambda d: {"at": d.get("last_payment_at")},
    ),
    KaiyuanSensorDescription(
        key="account",
        translation_key="account",
        name="户号",
        icon="mdi:account-details",
        value_fn=lambda d: d.get("cons_no"),
        attrs_fn=lambda d: {
            "customer_name": d.get("customer_name"),
            "address": d.get("address"),
            "mobile": d.get("mobile"),
        },
    ),
    KaiyuanSensorDescription(
        key="data_source",
        translation_key="data_source",
        name="数据来源",
        icon="mdi:database-arrow-down",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("data_source"),
        attrs_fn=lambda d: {
            "cache_updated": d.get("cache_updated"),
            "last_error": d.get("last_error"),
            "updated_at": d.get("updated_at"),
        },
    ),
    KaiyuanSensorDescription(
        key="login_status",
        translation_key="login_status",
        name="登录状态",
        icon="mdi:shield-account",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("login_status"),
        attrs_fn=lambda d: {
            "mobile": d.get("mobile"),
            "last_error": d.get("last_error"),
        },
    ),
    KaiyuanSensorDescription(
        key="last_refresh",
        translation_key="last_refresh",
        name="最新刷新时间",
        icon="mdi:update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _as_datetime(d.get("last_refresh")),
        attrs_fn=lambda d: {
            "data_source": d.get("data_source"),
            "last_error": d.get("last_error"),
            "cache_updated_at": d.get("cache_updated_at"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: KaiyuanGasCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        KaiyuanGasSensor(coordinator, entry, description) for description in SENSORS
    )


class KaiyuanGasSensor(CoordinatorEntity[KaiyuanGasCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: KaiyuanGasCoordinator, entry: ConfigEntry,
                 description: KaiyuanSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # 每个账号一台设备：设备名带账号（凯源燃气 手机号），
        # 而集成条目本身只叫「凯源燃气」，和水务一样上面不显示账号。
        mobile = str(entry.data.get(CONF_MOBILE) or "").strip()
        cons_no = str(entry.data.get(CONF_CONS_NO) or "").strip()
        device_name = f"凯源燃气 {mobile}" if mobile else (
            f"凯源燃气 {cons_no}" if cons_no else "凯源燃气"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "manufacturer": MANUFACTURER,
            "name": device_name,
            "model": MODEL,
            "serial_number": cons_no or None,
            "configuration_url": "http://wx.kaiyuan.qjcode.com/",
            "entry_type": "service",
        }

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return dict(self.entity_description.attrs_fn(self.coordinator.data))
