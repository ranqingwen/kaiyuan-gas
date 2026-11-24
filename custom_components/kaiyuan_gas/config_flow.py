"""配置流程。"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries

try:  # HA 新版把 callback 挪走的情况，兜底成直通装饰器
    from homeassistant.core import callback
except ImportError:  # pragma: no cover
    def callback(func):  # type: ignore[misc]
        return func

from .api import (
    KaiyuanGasApiError,
    KaiyuanGasAuthError,
    KaiyuanGasClient,
    KaiyuanGasConnectionError,
    KaiyuanGasError,
    KaiyuanGasLockedError,
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
    MAX_REFRESH_HOURS,
    MIN_REFRESH_HOURS,
)

_LOGGER = logging.getLogger(__name__)

try:  # 密码框打码显示（新版 HA 才有 selector 模块）
    from homeassistant.helpers import selector

    _PASSWORD = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )
    _MOBILE = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
    )
    _HOURS = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_REFRESH_HOURS,
            max=MAX_REFRESH_HOURS,
            step=1,
            mode=selector.NumberSelectorMode.SLIDER,
            unit="小时",
        )
    )
    _LIMIT = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=100000, step=1, mode=selector.NumberSelectorMode.BOX, unit="m³/年"
        )
    )
    _PRICE = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=99, step=0.001, mode=selector.NumberSelectorMode.BOX, unit="元/m³"
        )
    )
except Exception:  # noqa: BLE001
    _PASSWORD = str
    _MOBILE = str
    _HOURS = vol.All(vol.Coerce(int), vol.Range(min=MIN_REFRESH_HOURS, max=MAX_REFRESH_HOURS))
    _LIMIT = vol.All(vol.Coerce(float), vol.Range(min=0, max=100000))
    _PRICE = vol.All(vol.Coerce(float), vol.Range(min=0, max=99))

# 阶梯气价：接口不返回分档标准，添加集成时让用户填（默认重庆中心城区标准）
LADDER_FIELDS = {
    CONF_LADDER1: (DEFAULT_LADDER1, _LIMIT),
    CONF_LADDER2: (DEFAULT_LADDER2, _LIMIT),
    CONF_PRICE1: (DEFAULT_PRICE1, _PRICE),
    CONF_PRICE2: (DEFAULT_PRICE2, _PRICE),
    CONF_PRICE3: (DEFAULT_PRICE3, _PRICE),
}

# 户号不再让用户填写：登录后从账号信息里自动识别
STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_MOBILE): _MOBILE,
    vol.Required(CONF_PASSWORD): _PASSWORD,
    **{vol.Optional(k, default=v): s for k, (v, s) in LADDER_FIELDS.items()},
})


def entry_title(mobile: str) -> str:
    """集成条目显示名：只写「凯源燃气」。

    和水务一致：上面那行（集成条目）只显示集成名，账号放在下面那台设备上
    （设备名是「凯源燃气 手机号」）。这样集成页上面一行不会顶着手机号。
    """
    return "凯源燃气"


class KaiyuanGasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """手机号 + 密码登录。每个账号一条集成条目，可以重复添加多个账号。"""

    VERSION = 1

    def __init__(self) -> None:
        self._lock_message: str = ""
        self._lock_mobile: str = ""

    # ------------------------------------------------------------------ 工具
    def _mobile_taken(self, mobile: str, skip_entry_id: str | None = None) -> bool:
        """同一手机号只允许一条条目（不同手机号可以并存，用于多账号）。"""
        mobile = str(mobile or "").strip()
        if not mobile:
            return False
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if skip_entry_id and entry.entry_id == skip_entry_id:
                continue
            if str(entry.data.get(CONF_MOBILE) or "").strip() == mobile:
                return True
        return False

    def _reconfigure_entry(self) -> config_entries.ConfigEntry | None:
        """取当前正在「重新配置」的那条条目，兼容新旧两套 API。"""
        getter = getattr(self, "_get_reconfigure_entry", None)
        if getter is not None:
            try:
                return getter()
            except Exception:  # noqa: BLE001 - 旧版本没有这个流程
                pass
        entry_id = (self.context or {}).get("entry_id")
        if entry_id:
            return self.hass.config_entries.async_get_entry(entry_id)
        return None

    # ------------------------------------------------------------------ 添加
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        description: dict[str, str] = {}

        if user_input is not None:
            mobile = str(user_input[CONF_MOBILE]).strip()
            password = str(user_input[CONF_PASSWORD])

            await self.async_set_unique_id(f"{DOMAIN}_{mobile}")
            self._abort_if_unique_id_configured()
            if self._mobile_taken(mobile):
                return self.async_abort(reason="already_configured")

            client = KaiyuanGasClient(mobile, password)

            def _validate() -> str:
                client.login()
                info = client.get_user_info()
                client._cons_no()  # noqa: SLF001  确认能自动识别户号
                client.get_account_info()
                name = info.get("userName") or info.get("name") or mobile
                return str(name)

            try:
                name = await self.hass.async_add_executor_job(_validate)
            except KaiyuanGasLockedError as err:
                # 账号被临时锁定：单独一页醒目提示，不再伪装成"连接失败"
                _LOGGER.warning("凯源燃气账号被临时锁定: %s", err)
                self._lock_message = str(err)
                self._lock_mobile = mobile
                return await self.async_step_locked()
            except KaiyuanGasAuthError as err:
                _LOGGER.warning("凯源燃气登录被拒绝: %s", err)
                errors["base"] = "invalid_auth"
                description["error"] = str(err)
            except KaiyuanGasConnectionError as err:
                _LOGGER.error("凯源燃气连接失败: %s", err)
                errors["base"] = "cannot_connect"
                description["error"] = str(err)
            except KaiyuanGasApiError as err:
                _LOGGER.error("凯源燃气接口业务错误: %s", err)
                errors["base"] = "api_error"
                description["error"] = str(err)
            except KaiyuanGasError as err:
                _LOGGER.error("凯源燃气接口错误: %s", err)
                errors["base"] = "api_error"
                description["error"] = str(err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("凯源燃气未知错误")
                errors["base"] = "unknown"
                description["error"] = str(err)
            else:
                _LOGGER.debug("凯源燃气添加账号 %s（户名 %s）", mobile, name)
                return self.async_create_entry(
                    title=entry_title(mobile),
                    data={
                        CONF_MOBILE: mobile,
                        CONF_PASSWORD: password,
                        CONF_CONS_NO: client.cons_no or "",
                        **{
                            key: float(user_input.get(key, default))
                            for key, (default, _) in LADDER_FIELDS.items()
                        },
                    },
                )

            return self.async_show_form(
                step_id="user",
                data_schema=self.add_suggested_values_to_schema(
                    STEP_USER_SCHEMA, {CONF_MOBILE: mobile}
                ),
                errors=errors,
                description_placeholders={"error": description.get("error", "")},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            description_placeholders={"error": ""},
        )

    async def async_step_locked(self, user_input: dict[str, Any] | None = None):
        """账号被临时锁定：整页提示 + 两个选择，避免用户误以为是连接故障。"""
        return self.async_show_menu(
            step_id="locked",
            menu_options={
                "close": "知道了，稍后再添加",
                "retry": "仍然重试（可能延长锁定时间）",
            },
            description_placeholders={
                "message": self._lock_message,
                "mobile": self._lock_mobile,
            },
        )

    async def async_step_retry(self, user_input: dict[str, Any] | None = None):
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_MOBILE: self._lock_mobile}
            ),
            description_placeholders={"error": ""},
        )

    async def async_step_close(self, user_input: dict[str, Any] | None = None):
        return self.async_abort(
            reason="account_locked",
            description_placeholders={"mobile": self._lock_mobile},
        )

    # -------------------------------------------------------------- 重新配置
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """条目上的「重新配置」：换账号 / 改密码 / 换户号 / 改刷新间隔与阶梯气价。

        密码留空表示不修改；改完先登录验证一遍，验证通过才落盘，并同步更新条目标题。
        """
        entry = self._reconfigure_entry()
        if entry is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}
        data = dict(entry.data)
        options = dict(entry.options)
        current_mobile = str(data.get(CONF_MOBILE) or "")
        current_cons_no = str(data.get(CONF_CONS_NO) or "")

        if user_input is not None:
            mobile = str(user_input.get(CONF_MOBILE) or "").strip() or current_mobile
            password = str(user_input.get(CONF_PASSWORD) or "").strip() or str(
                data.get(CONF_PASSWORD) or ""
            )
            cons_no = str(user_input.get(CONF_CONS_NO) or "").strip()

            if self._mobile_taken(mobile, entry.entry_id):
                return self.async_abort(reason="already_configured")

            client = KaiyuanGasClient(mobile, password)

            def _validate() -> str:
                client.login()
                client.get_user_info()
                return client._cons_no() or ""  # noqa: SLF001

            try:
                detected = await self.hass.async_add_executor_job(_validate)
            except KaiyuanGasLockedError as err:
                _LOGGER.warning("凯源燃气账号被临时锁定: %s", err)
                errors["base"] = "account_locked"
            except KaiyuanGasAuthError as err:
                _LOGGER.warning("凯源燃气登录被拒绝: %s", err)
                errors["base"] = "invalid_auth"
            except KaiyuanGasConnectionError as err:
                _LOGGER.error("凯源燃气连接失败: %s", err)
                errors["base"] = "cannot_connect"
            except KaiyuanGasError as err:
                _LOGGER.error("凯源燃气接口错误: %s", err)
                errors["base"] = "api_error"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("凯源燃气未知错误")
                errors["base"] = "unknown"
            else:
                new_data = {
                    **data,
                    CONF_MOBILE: mobile,
                    CONF_PASSWORD: password,
                    CONF_CONS_NO: cons_no or detected or current_cons_no,
                }
                new_options = {
                    **options,
                    CONF_REFRESH_HOURS: int(
                        user_input.get(CONF_REFRESH_HOURS, DEFAULT_REFRESH_HOURS)
                    ),
                    **{
                        key: float(user_input.get(key, default))
                        for key, (default, _) in LADDER_FIELDS.items()
                    },
                }
                return await self._async_apply_reconfigure(
                    entry, new_data, new_options, entry_title(mobile)
                )

        schema = vol.Schema({
            vol.Required(
                CONF_MOBILE, description={"suggested_value": current_mobile}
            ): _MOBILE,
            vol.Optional(CONF_PASSWORD): _PASSWORD,
            vol.Optional(
                CONF_CONS_NO, description={"suggested_value": current_cons_no}
            ): str,
            vol.Required(
                CONF_REFRESH_HOURS,
                default=int(options.get(CONF_REFRESH_HOURS, DEFAULT_REFRESH_HOURS)),
            ): _HOURS,
            **{
                vol.Required(
                    key, default=float(options.get(key, data.get(key, default)))
                ): sel
                for key, (default, sel) in LADDER_FIELDS.items()
            },
        })
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "mobile": current_mobile or "（未设置）",
                "cons_no": current_cons_no or "自动识别",
            },
        )

    async def _async_apply_reconfigure(
        self,
        entry: config_entries.ConfigEntry,
        data: dict[str, Any],
        options: dict[str, Any],
        title: str,
    ):
        """把新配置写回条目并重载，兼容不同 HA 版本的 API。"""
        mobile = str(data.get(CONF_MOBILE) or "").strip()
        updates: dict[str, Any] = {"title": title}
        if mobile:
            updates["unique_id"] = f"{DOMAIN}_{mobile}"
        try:
            self.hass.config_entries.async_update_entry(entry, **updates)
        except (ValueError, TypeError) as err:  # unique_id 冲突等，不阻塞保存
            _LOGGER.debug("更新条目标题/唯一 ID 失败（忽略）: %s", err)
            try:
                self.hass.config_entries.async_update_entry(entry, title=title)
            except Exception:  # noqa: BLE001
                pass

        try:
            params = inspect.signature(self.async_update_reload_and_abort).parameters
        except (TypeError, ValueError):  # pragma: no cover
            params = {}

        kwargs: dict[str, Any] = {"reason": "reconfigure_successful"}
        if "data_updates" in params:
            kwargs["data_updates"] = data
            kwargs["options_updates"] = options
        elif "data" in params:  # 旧版签名
            kwargs["data"] = data
            kwargs["options"] = options
        else:  # pragma: no cover - 极旧版本：手工更新 + 重载
            self.hass.config_entries.async_update_entry(entry, data=data, options=options)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        try:
            return self.async_update_reload_and_abort(entry, **kwargs)
        except TypeError:  # pragma: no cover - 签名差异兜底
            self.hass.config_entries.async_update_entry(entry, data=data, options=options)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return KaiyuanGasOptionsFlow(config_entry)


class KaiyuanGasOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        # 新版 HA 由框架注入 self.config_entry，旧版需要自己保存
        try:
            self.config_entry = config_entry
        except AttributeError:
            self._entry = config_entry

    @property
    def _cfg(self) -> config_entries.ConfigEntry | None:
        return getattr(self, "config_entry", None) or getattr(self, "_entry", None)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """修改账号、密码（可留空不改）、自动刷新间隔（12-48 小时）、阶梯气价。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self._cfg
            new_data = dict(entry.data) if entry else {}
            mobile = str(user_input.get(CONF_MOBILE) or "").strip()
            password = str(user_input.get(CONF_PASSWORD) or "")
            if mobile:
                new_data[CONF_MOBILE] = mobile
            if password:
                new_data[CONF_PASSWORD] = password

            # 账号或密码改了就先验证一遍，避免存进去坏凭据
            if mobile or password:
                client = KaiyuanGasClient(
                    new_data.get(CONF_MOBILE, ""), new_data.get(CONF_PASSWORD, "")
                )

                def _validate() -> None:
                    client.login()
                    client.get_user_info()

                try:
                    await self.hass.async_add_executor_job(_validate)
                except KaiyuanGasLockedError as err:
                    _LOGGER.warning("凯源燃气账号被临时锁定: %s", err)
                    errors["base"] = "account_locked"
                except KaiyuanGasAuthError as err:
                    _LOGGER.warning("凯源燃气登录被拒绝: %s", err)
                    errors["base"] = "invalid_auth"
                except KaiyuanGasConnectionError as err:
                    _LOGGER.error("凯源燃气连接失败: %s", err)
                    errors["base"] = "cannot_connect"
                except KaiyuanGasError as err:
                    errors["base"] = "api_error"

            if not errors and entry is not None:
                if new_data != entry.data:
                    updates: dict[str, Any] = {"data": new_data}
                    if mobile and mobile != entry.data.get(CONF_MOBILE):
                        updates["title"] = entry_title(mobile)
                        updates["unique_id"] = f"{DOMAIN}_{mobile}"
                    try:
                        self.hass.config_entries.async_update_entry(entry, **updates)
                    except (ValueError, TypeError):  # unique_id 冲突时只改数据
                        self.hass.config_entries.async_update_entry(entry, data=new_data)
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_REFRESH_HOURS: int(user_input[CONF_REFRESH_HOURS]),
                        **{
                            key: float(user_input.get(key, default))
                            for key, (default, _) in LADDER_FIELDS.items()
                        },
                    },
                )

        entry = self._cfg
        options = entry.options if entry else {}
        data = entry.data if entry else {}
        schema = vol.Schema({
            vol.Optional(
                CONF_MOBILE,
                description={"suggested_value": options.get(CONF_MOBILE) or data.get(CONF_MOBILE, "")},
            ): _MOBILE,
            vol.Optional(CONF_PASSWORD): _PASSWORD,
            vol.Required(
                CONF_REFRESH_HOURS,
                default=options.get(CONF_REFRESH_HOURS, DEFAULT_REFRESH_HOURS),
            ): _HOURS,
            **{
                vol.Required(
                    key, default=float(options.get(key, data.get(key, default)))
                ): sel
                for key, (default, sel) in LADDER_FIELDS.items()
            },
        })
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "cons_no": (data.get(CONF_CONS_NO) or "自动识别"),
            },
        )
