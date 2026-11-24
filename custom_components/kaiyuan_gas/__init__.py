"""凯源燃气 Home Assistant 集成。"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .coordinator import KaiyuanGasCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# 卡片随集成一起安装，通过静态路径暴露给前端，地址风格与水务一致：
#   /kaiyuan_gas/www/kaiyuan-gas-card.js
CARD_DIR = Path(__file__).parent / "www"
CARD_FILENAME = "kaiyuan-gas-card.js"
CARD_URL_PATH = "/kaiyuan_gas/www"
CARD_URL = f"{CARD_URL_PATH}/{CARD_FILENAME}"

_CARD_READY = "card_ready"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 老版本条目标题里放的是户名（如「凯源燃气 张三」），统一改成「凯源燃气 手机号」，
    # 和水务一样：上面只写集成名，下面条目里带账号。升级后自动生效，不用删了重加。
    _sync_entry_title(hass, entry)
    # 1.0.3 删掉了「账户余额」「账务记录」两个实体，顺手清掉注册表里的旧条目，
    # 否则升级后它们会变成「无法使用」一直挂在设备页上。
    _async_purge_removed_entities(hass, entry)

    # 卡片与数据无关，先准备好；出错也只记日志，不影响集成本身
    await _async_setup_card(hass)

    coordinator = KaiyuanGasCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except UpdateFailed as err:
        # 首次刷新失败：把业务/网络原因原样告诉用户，不要统一成"初始化失败"
        raise ConfigEntryNotReady(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"初始化失败: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_purge_removed_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """删掉已经不再提供的实体（账户余额 / 账务记录 / 累计用量）。

    注册表里的条目不会因为平台不再添加就自动消失，留着会变成「无法使用」的僵尸行，
    设备页上还是看得见。这里按 unique_id 精确删掉，只删自己这几个，不动别的。

    1.0.11 起「累计用量」不再单列（最新读数本身就是累计用量，卡片主实体改挂
    「最新读数」），它的注册表条目要一并清掉；「本年累计用量」保留。
    """
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover - 老版本没有独立注册表
        return
    registry = er.async_get(hass)
    for key in ("balance", "records", "cumulative_usage"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        if not entity_id:
            continue
        try:
            registry.async_remove(entity_id)
        except KeyError:
            continue
        _LOGGER.debug("已移除不再提供的实体 %s（%s）", entity_id, key)


def _sync_entry_title(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """条目标题固定为「凯源燃气」：账号显示在下面的设备名上，不顶在上面。"""
    want = "凯源燃气"
    if entry.title == want:
        return
    try:
        hass.config_entries.async_update_entry(entry, title=want)
    except Exception as err:  # noqa: BLE001 - 改标题失败不影响加载
        _LOGGER.debug("同步凯源燃气条目标题失败: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


# ---------------------------------------------------------------- 前端资源
async def _async_setup_card(hass: HomeAssistant) -> None:
    """注册卡片静态路径，并把它挂进 Lovelace 资源（只做一次）。"""
    store = hass.data.setdefault(DOMAIN, {})
    if store.get(_CARD_READY):
        return
    store[_CARD_READY] = True

    await _async_register_static_path(hass)
    version = await _async_card_version(hass)
    url = f"{CARD_URL}?v={version}"

    async def _register(_event: Any = None) -> None:
        if await _async_register_lovelace_resource(hass, url):
            _LOGGER.debug("凯源燃气卡片已加入 Lovelace 资源: %s", url)
            return
        # 资源是 YAML 模式、或 lovelace 还没起来时，退回全局注入
        _add_extra_js_url(hass, url)

    if hass.is_running:
        await _register()
    else:
        # 等所有组件（lovelace / frontend）就绪后再注册
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register)


async def _async_register_static_path(hass: HomeAssistant) -> None:
    """把 www/ 目录映射到 /kaiyuan_gas/www/。"""
    try:
        register = getattr(hass.http, "async_register_static_paths", None)
        if register is not None:
            from homeassistant.components.http import StaticPathConfig

            await register([StaticPathConfig(CARD_URL_PATH, str(CARD_DIR), False)])
            return

        register = getattr(hass.http, "async_register_static_path", None)
        if register is not None:
            result = register(CARD_URL_PATH, str(CARD_DIR), False)
            if inspect.isawaitable(result):
                await result
            return

        await hass.async_add_executor_job(
            hass.http.register_static_path, CARD_URL_PATH, str(CARD_DIR), False
        )
    except Exception as err:  # noqa: BLE001 - 静态资源失败不该拖垮集成
        _LOGGER.warning("注册凯源燃气卡片静态资源失败: %s", err)


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """把卡片写进 Lovelace 资源列表；不可写（YAML 模式）时返回 False。"""
    try:
        resources = _lovelace_resources(hass.data.get("lovelace"))
        if resources is None or not hasattr(resources, "async_create_item"):
            return False

        await resources.async_get_info()
        for item in resources.async_items():
            if str(item.get("url", "")).split("?")[0] != CARD_URL:
                continue
            if item.get("url") != url:
                await resources.async_update_item(
                    item["id"], {"url": url, "res_type": "module"}
                )
            return True

        await resources.async_create_item({"res_type": "module", "url": url})
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("自动写入 Lovelace 资源失败，改用全局注入: %s", err)
        return False


def _lovelace_resources(lovelace: Any) -> Any:
    """兼容不同 HA 版本里 hass.data["lovelace"] 的结构。"""
    if lovelace is None:
        return None
    if isinstance(lovelace, dict):
        if lovelace.get("resources") is not None:
            return lovelace["resources"]
        for dashboard in (lovelace.get("dashboards") or {}).values():
            resources = getattr(dashboard, "resources", None)
            if resources is not None:
                return resources
        return None
    return getattr(lovelace, "resources", None)


def _add_extra_js_url(hass: HomeAssistant, url: str) -> None:
    try:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, url)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "凯源燃气卡片未能自动加载，请手动添加资源 %s（类型: JavaScript 模块）: %s",
            url,
            err,
        )


async def _async_card_version(hass: HomeAssistant) -> str:
    """URL 缓存参数 = manifest 版本号 + 卡片文件内容指纹。

    只放版本号的话，改完卡片忘了升版本，浏览器还会拿旧缓存；加上内容指纹后，
    只要 www/kaiyuan-gas-card.js 一变 URL 就变，缓存必然失效——所以直接替换
    整个 custom_components/kaiyuan_gas 文件夹、重启 HA 就够了，不用手改任何地址。
    """

    def _read() -> str:
        version = "0"
        try:
            data = json.loads((Path(__file__).parent / "manifest.json").read_text("utf-8"))
            version = str(data.get("version") or "0")
        except Exception:  # noqa: BLE001 - 读不到版本号不算错，有指纹也能失效缓存
            pass
        try:
            digest = hashlib.sha1((CARD_DIR / CARD_FILENAME).read_bytes()).hexdigest()[:8]
        except Exception:  # noqa: BLE001 - 卡片文件缺失时退回纯版本号
            return version
        return f"{version}-{digest}"

    try:
        return await hass.async_add_executor_job(_read)
    except Exception:  # noqa: BLE001
        return "0"
