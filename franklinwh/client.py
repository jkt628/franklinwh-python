"""Client for interacting with FranklinWH gateway API.

This module provides classes and functions to authenticate, send commands,
and retrieve statistics from FranklinWH energy gateway devices.

THIS version fakes the API responses for testing and development purposes, no actual gateways required.
Leave just enough API for https://github.com/jkt628/homeassistant-franklinwh
or https://github.com/richo/homeassistant-franklinwh, everything else breaks.
Two gateways with different accessories are simulated with statistics based on ID and random noise:

* 10060005A02X24456789 - has a generator module
* 10060005A02X24123456 - has a smart circuit module

Floats are based on the last three digits of the gateway ID +/- 2% random noise, i.e.,
76.9-80.9 for the first gateway or 43.6-47.6 for the second.
States have a 2% chance of changing.
"""

# must work with Python >= 3.13
from __future__ import annotations  # noqa: RUF100, TID251

import asyncio
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
import json
import time
from typing import TYPE_CHECKING, Any, Final, Self
import zlib

import httpx

from .api import DEFAULT_URL_BASE, ISSUES_URL
from .time_cached import also_clear, time_cached

# misspelled in the FranklinHW API:
# currendId: current Id
# runingMode: running mode
# stromEn: Storm Hedge enabled


@dataclass
class Titled:
    """Add a title to an id."""

    title: str
    id: int


class TitledEnum(Titled, Enum):
    """An enumeration with a title and an id."""

    @classmethod
    def titles(cls) -> Generator[str]:
        """Get the titles of all enum members.

        Returns:
        -------
        Generator[str, None, None]
            A generator yielding the titles of all enum members.
        """
        for item in cls:
            yield item.title

    @classmethod
    def ids(cls) -> Generator[int]:
        """Get the ids of all enum members.

        Returns:
        -------
        Generator[int, None, None]
            A generator yielding the ids of all enum members.
        """
        for item in cls:
            yield item.id

    @classmethod
    def from_id(cls, id: int) -> Self:
        """Get the enum member corresponding to the given id.

        Parameters
        ----------
        id : int
            The id to look up.

        Returns:
        -------
        Self
            The enum member corresponding to the given id.

        Raises:
        ------
        ValueError
            If no enum member has the given id.
        """
        for item in cls:
            if item.id == id:
                return item
        raise ValueError(f"No {cls.__name__} with id {id}")

    @classmethod
    def from_title(cls, title: str) -> Self:
        """Get the enum member corresponding to the given title.

        Parameters
        ----------
        title : str
            The title to look up.

        Returns:
        -------
        Self
            The enum member corresponding to the given title.

        Raises:
        ------
        ValueError
            If no enum member has the given title.
        """
        for item in cls:
            if item.title == title:
                return item
        raise ValueError(f"No {cls.__name__} with title {title}")


class AccessoryType(TitledEnum):
    """Represents the type of accessory connected to the FranklinWH gateway.

    Attributes:
        SMART_CIRCUITS_MODULE (int): A Smart Circuit module, see https://www.franklinwh.com/document/download/smart-circuits-module-installation-guide-sku-accy-scv2-us
        GENERATOR_MODULE (int): A Generator module, see https://www.franklinwh.com/document/download/generator-module-installation-guide-sku-accy-genv2-us
    """

    GENERATOR_MODULE = ("Generator", 3)
    SMART_CIRCUITS_MODULE = ("Smart Circuits", 4)


class RunStatus(TitledEnum):
    """Represent run_status values of the FranklinWH gateway."""

    STANDBY = ("Standby", 0)
    CHARGING = ("Charging", 1)
    DISCHARGING = ("Discharging", 2)


def to_hex(inp):
    """Convert an integer to an 8-character uppercase hexadecimal string.

    Parameters
    ----------
    inp : int
        The integer to convert.

    Returns:
    -------
    str
        The hexadecimal string representation of the input.
    """
    return f"{inp:08X}"


def empty_stats():
    """Return a Stats object with all values set to zero.

    Returns:
    -------
    Stats
        A Stats object with zeroed Current and Totals values.
    """
    return Stats(
        Current(
            0.0,
            0.0,
            False,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            GridStatus.NORMAL,
            RunStatus.STANDBY,
        ),
        Totals(
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        ),
    )


class GridStatus(Enum):
    """Represents the status of the grid connection for the FranklinWH gateway.

    Attributes:
        NORMAL (int): Grid connection is normal / up.
        DOWN (int): Grid connection is abnormal / down.
        OFF (int): Grid connection is turned off at the gateway.

    OFF is set by software, specifically Settings / Go Off-Grid in the app.
    DOWN is external to the gateway.
    NORMAL indicates normal operation.
    """

    NORMAL = 0
    DOWN = 1
    OFF = 2

    @staticmethod
    def from_offgridreason(value: int | None) -> GridStatus:
        """Convert an offgridreason value to a GridStatus.

        Parameters
        ----------
        value : int | None
            The offgridreason value to convert.

        Returns:
        -------
        GridStatus
            The corresponding GridStatus.
        """
        match value:
            case None | -1:
                return GridStatus.NORMAL
            case 0:
                return GridStatus.DOWN
            case 1:
                return GridStatus.OFF
            case _:
                raise ValueError(f"Unknown offgridreason value: {value}")


class ExportMode(Enum):
    """Represents the grid export mode for the FranklinWH gateway.

    Attributes:
        SOLAR_ONLY (int): Solar can export to the grid; battery (aPower) cannot.
        SOLAR_AND_APOWER (int): Both solar and battery can export to the grid.
        NO_EXPORT (int): No grid export permitted.
    """

    SOLAR_ONLY = 1
    SOLAR_AND_APOWER = 2
    NO_EXPORT = 3

    @staticmethod
    def from_flag(value: int) -> ExportMode:
        """Convert a gridFeedMaxFlag API value to an ExportMode.

        Parameters
        ----------
        value : int
            The gridFeedMaxFlag value from the API response.

        Returns:
        -------
        ExportMode
            The corresponding ExportMode.
        """
        try:
            return ExportMode(value)
        except ValueError:
            return ExportMode.SOLAR_ONLY


@dataclass
class ExportSettings:
    """Current grid export configuration for the FranklinWH gateway.

    Attributes:
        mode: The active export mode.
        limit_kw: Export power cap in kW, or None if unlimited.
    """

    mode: ExportMode
    limit_kw: float | None


@dataclass
class Current:
    """Current statistics for FranklinWH gateway."""

    solar_production: float
    generator_production: float
    generator_enabled: bool
    battery_use: float
    grid_use: float
    home_load: float
    battery_soc: float
    switch_1_load: float
    switch_2_load: float
    v2l_use: float
    grid_status: GridStatus
    run_status: RunStatus


@dataclass
class Totals:
    """Total energy statistics for FranklinWH gateway."""

    battery_charge: float
    battery_discharge: float
    grid_import: float
    grid_export: float
    solar: float
    generator: float
    home_use: float
    switch_1_use: float
    switch_2_use: float
    v2l_export: float
    v2l_import: float


@dataclass
class Stats:
    """Statistics for FranklinWH gateway."""

    current: Current
    totals: Totals


class WorkMode(TitledEnum):
    """Represents the workMode values of the FranklinWH gateway.

    These are the only operating mode constants in the FranklinWH API.

    Attributes:
        TIME_OF_USE: Time of Use mode, id = 1.
        SELF_CONSUMPTION: Self-Consumption mode, id = 2.
        EMERGENCY_BACKUP: Emergency Backup mode, id = 3.

    These are artificial and controlled by API, support or provider.

    Attributes:
        GENERATOR: Generator mode, id = 7.
        DEBUG: Debug mode, id = 8.
        VPP_MODE: VPP Mode, id = 9.
    """

    TIME_OF_USE = ("Time Of Use (TOU)", 1)
    SELF_CONSUMPTION = ("Self-Consumption", 2)
    EMERGENCY_BACKUP = ("Emergency Backup", 3)
    GENERATOR = ("Generator", 7)
    DEBUG = ("Debug", 8)
    VPP_MODE = ("VPP Mode", 9)


class Mode(dict[str, Any]):
    """Represents an operating mode for the FranklinWH gateway.

    Provides static methods to create specific modes (time of use, emergency backup, self consumption)
    and generates payloads for API requests to set the gateway's operating mode.

    Methods:
    -------
    time_of_use(optional soc)
        Create a time of use mode instance.
    emergency_backup(optional soc)
        Create an emergency backup mode instance.
    self_consumption(optional soc)
        Create a self consumption mode instance.
    payload(gateway)
        Generate the payload dictionary for API requests.
    """

    if TYPE_CHECKING:
        # ruff: disable[N815]
        id: int
        oldIndex: int
        name: str
        soc: float
        maxSoc: float
        minSoc: float
        dischargeDepthSoc: float | None
        editSocFlag: bool
        multiSOCFlag: bool
        workMode: int
        energyIncentivesType: int
        electricityType: int
        displayFlag: int
        # ruff: enable[N815]

    defaults: Final = {
        "id": 0,
        "oldIndex": 3,
        "name": "Unknown",
        "soc": 100.0,
        "maxSoc": 100.0,
        "minSoc": 100.0,
        "dischargeDepthSoc": None,
        "editSocFlag": False,
        "multiSOCFlag": False,
        "workMode": 0,
        "energyIncentivesType": 0,
        "electricityType": 1,
        "displayFlag": None,
    }
    allowed: Final = defaults.keys()

    @classmethod
    def time_of_use(cls, soc: float | None = None) -> Mode:
        """Create a time of use mode instance.

        Parameters
        ----------
        soc : float, optional
            The state of charge value for the mode, defaults to 20.

        Returns:
        -------
        Mode
            An instance of Mode configured for time of use.
        """
        if soc is None:
            soc = 20.0
        return Mode(
            workMode=WorkMode.TIME_OF_USE.id, name=WorkMode.TIME_OF_USE.title, soc=soc
        )

    @classmethod
    def emergency_backup(cls, soc: float | None = None) -> Mode:
        """Create an emergency backup mode instance.

        Parameters
        ----------
        soc : float, optional
            The state of charge value for the mode, defaults to 100.

        Returns:
        -------
        Mode
            An instance of Mode configured for emergency backup.
        """
        if soc is None:
            soc = 100.0
        return Mode(
            workMode=WorkMode.EMERGENCY_BACKUP.id,
            name=WorkMode.EMERGENCY_BACKUP.title,
            soc=soc,
        )

    @classmethod
    def self_consumption(cls, soc: float | None = None) -> Mode:
        """Create a self consumption mode instance.

        Parameters
        ----------
        soc : float, optional
            The state of charge value for the mode, defaults to 20.

        Returns:
        -------
        Mode
            An instance of Mode configured for self consumption.
        """
        if soc is None:
            soc = 20.0
        return Mode(
            workMode=WorkMode.SELF_CONSUMPTION.id,
            name=WorkMode.SELF_CONSUMPTION.title,
            soc=soc,
        )

    @classmethod
    def vpp_mode(cls, _: float | None = None) -> Mode:
        """Create a virtual power plant mode instance.

        Returns:
        -------
        Mode
            An instance of Mode configured for virtual power plant mode.
        """
        return Mode(
            workMode=WorkMode.VPP_MODE.id, name=WorkMode.VPP_MODE.title, soc=100.0
        )

    def __init__(self, *args, **kwargs) -> None:
        """Initialize a Mode instance with the specified work mode and state of charge.

        Parameters
        ----------
        workMode : int
            The work mode id for the FranklinWH gateway.
        soc : float | None
            The state of charge value for the mode.
        """
        sanitized = kwargs.copy()
        for k in kwargs:
            if k not in self.allowed:
                sanitized.pop(k)
        if "workMode" not in sanitized and len(args) > 0:
            sanitized["workMode"] = args[0]
        if "soc" not in sanitized and len(args) > 1:
            sanitized["soc"] = float(args[1])
        if "id" not in sanitized:
            sanitized["id"] = sanitized["workMode"]
        super().__init__(*args, **(self.defaults | sanitized))
        self.__dict__ = self

    def payload(self, gateway, hedge: bool | int, soc: float | None = None) -> dict:
        """Generate the payload dictionary for API requests to set the gateway's operating mode.

        Parameters
        ----------
        gateway : str
            The gateway identifier.
        hedge : bool | int
            Is Storm Hedge enabled?
        soc : float, optional
            New State of Charge value.

        Returns:
        -------
        dict
            The payload dictionary for the API request.
        """
        params = {
            "currendId": str(self.id),
            "gatewayId": gateway,
            "lang": "EN_US",
            "oldIndex": str(self.workMode),
            "stromEn": str(int(bool(hedge))),
            "workMode": str(self.workMode),
        }
        if soc is not None:
            params["soc"] = str(int(soc))
        return params


class SwitchState(tuple[bool | None, bool | None, bool | None]):
    """Represents the state of the smart switches connected to the FranklinWH gateway.

    Each element in the tuple corresponds to a switch:
        - True: Switch is ON
        - False: Switch is OFF
        - None: Switch state is unchanged
    """

    __slots__ = ()

    def __new__(cls, lst: list[bool | None] | None = None):
        """Convert a list to a SwitchState tuple.

        Parameters
        ----------
        lst : optional list[bool | None]
            The list to convert, defaults to [None, None, None].

        Returns:
        -------
        SwitchState
            The converted SwitchState tuple.
        """
        if lst is None:
            lst = [None, None, None]

        if len(lst) != 3:
            raise ValueError(
                "List must have exactly 3 elements to convert to SwitchState."
            )
        return super().__new__(cls, lst)


class TokenExpiredException(Exception):
    """raised when the token has expired to signal upstream that you need to create a new client or inject a new token."""


class AccountLockedException(Exception):
    """raised when the account is locked."""


class InvalidCredentialsException(Exception):
    """raised when the credentials are invalid."""


class DeviceTimeoutException(Exception):
    """raised when the device times out."""


class GatewayOfflineException(Exception):
    """raised when the gateway is offline."""


class InvalidDataException(Exception):
    """raised when the API returns data that is structurally invalid."""


class PermissionDeniedException(Exception):
    """raised when the API returns code 181 (Operation without permission), typically when polling for an optional accessory (Smart Circuit, V2L) that is not provisioned on the account."""


class HttpClientFactory:
    """Factory to create FakeAsyncClient."""

    @classmethod
    def set_client_factory(cls, factory: Callable[..., httpx.AsyncClient]) -> None:
        """Do nothing."""

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        """Create a FakeAsyncClient."""

        class FakeAsyncClient(httpx.AsyncClient):
            """A fake AsyncClient that raises exceptions on use."""

            def __init__(self) -> None:  # pylint: disable=super-init-not-called
                """Short circuit initialization."""

            async def post(self, url, *args, **kwargs):
                """Trash a POST request."""
                raise GatewayOfflineException("Cannot fake a POST request.")

            async def get(self, url, *args, **kwargs):
                """Trash a GET request."""
                raise GatewayOfflineException("Cannot fake a GET request.")

        return FakeAsyncClient()


class TokenFetcher(HttpClientFactory):
    """Fetches and refreshes authentication tokens for FranklinWH API."""

    def __init__(self, username: str, password: str) -> None:
        """Initialize the TokenFetcher with the provided username and password."""
        self.username = username
        self.password = password
        self.info: dict | None = None

    async def get_token(self):
        """Fetch a new authentication token using the stored credentials.

        Store the intermediate account information in self.info.
        """
        self.info = await self.fetch_token()
        return self.info["token"]

    @staticmethod
    async def login(username: str, password: str):
        """Log in to the FranklinWH API and retrieve an authentication token."""
        await TokenFetcher(username, password).get_token()

    async def fetch_token(self) -> dict:
        """Fake a login token."""
        return {
            "account": self.username,
            "token": "APP_ID::fake",
            "userId": "fake",
        }


async def retry(func, filter, refresh_func):
    """Tries calling func, and if filter fails it calls refresh func then tries again."""
    res = await func()
    if filter(res):
        return res
    await refresh_func()
    return await func()


class Client(HttpClientFactory):
    """Client for interacting with FranklinWH gateway API."""

    def __init__(
        self, fetcher: TokenFetcher, gateway: str, url_base: str = DEFAULT_URL_BASE
    ) -> None:
        """Initialize the Client with the provided TokenFetcher, gateway ID, and optional URL base."""
        self.fetcher = fetcher
        self.gateway = gateway
        self.url_base = url_base
        self.token = ""
        self.snno = 0
        self.session = self.get_client()
        self._modes = {
            WorkMode.TIME_OF_USE.id: Mode.time_of_use(),
            WorkMode.SELF_CONSUMPTION.id: Mode.self_consumption(),
            WorkMode.EMERGENCY_BACKUP.id: Mode.emergency_backup(),
            WorkMode.VPP_MODE.id: Mode.vpp_mode(),
        } | {
            mode.id: Mode(
                workMode=mode.id,
                name=mode.title,
            )
            for mode in (WorkMode.GENERATOR, WorkMode.DEBUG)
        }

    async def refresh_token(self):
        """Refresh the authentication token using the TokenFetcher."""
        self.token = await self.fetcher.get_token()

    @time_cached(timedelta(days=1))
    async def get_accessories(self):
        """Get the list of accessories connected to the gateway."""
        url = self.url_base + "hes-gateway/common/getAccessoryList"
        # with no accessories this returns:
        # {"code":200,"message":"Query success!","result":[],"success":true,"total":0}
        return (await self._get(url))["result"]

    async def get_smart_switch_state(self) -> SwitchState:
        """Get the current state of the smart switches."""
        # TODO(richo) This API is super in flux, both because of how vague the
        # underlying API is and also trying to figure out what to do with
        # inconsistency.
        # Whether this should use the _switch_status() API is super unclear.
        # Maybe I will reach out to FranklinWH once I have published.
        status = await self._status()
        switches = [x == 1 for x in status["pro_load"]]
        return SwitchState(switches)

    async def set_smart_switch_state(self, state: SwitchState):
        """Set the state of the smart circuits.

        Setting a value in the state tuple to True will turn on that circuit,
        setting to False will turn it off. Setting to None will make it
        unchanged.
        """

        payload = await self._switch_status()
        payload["opt"] = 1
        payload.pop("modeChoose")
        payload.pop("result")

        if payload["SwMerge"] == 1:
            if state[0] != state[1]:
                raise RuntimeError(
                    "Smart switches 1 and 2 are merged! Setting them to different values could do bad things to your house. Aborting."
                )

        def set_value(keys, value):
            for k in keys:
                payload[k] = value

        for i in range(3):
            sw = i + 1
            if state[i] is not None:
                mode = f"Sw{sw}Mode"
                msg_type = f"Sw{sw}MsgType"
                pro_load = f"Sw{sw}ProLoad"

                payload[msg_type] = 1
                payload[mode] = int(bool(state[i]))
                payload[pro_load] = payload[mode] ^ 1

        wire_payload = self._build_payload(311, payload)
        data = (await self._mqtt_send(wire_payload))["result"]["dataArea"]
        return json.loads(data)

    # Sends a 203 which is a high level status
    @time_cached()
    async def _status(self):
        payload = self._build_payload(203, {"opt": 1, "refreshData": 1})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    # Sends a 311 which appears to be a more specific switch command
    @time_cached()
    async def _switch_status(self):
        payload = self._build_payload(311, {"opt": 0, "order": self.gateway})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    # Sends a 353 which grabs real-time smart-circuit load information
    # https://github.com/richo/homeassistant-franklinwh/issues/27#issuecomment-2714422732
    @time_cached()
    async def _switch_usage(self):
        payload = self._build_payload(353, {"opt": 0, "order": self.gateway})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    @time_cached(timedelta(hours=1))  # eventually consistent with changes via app
    async def get_tou_settings(self) -> dict[str, Any]:
        """Get the current Time of Use settings for the FranklinWH gateway."""
        url = self.url_base + "hes-gateway/terminal/tou/getGatewayTouListV2"
        res = await self._post(url, None, {"showType": 1})
        return res.get("result", {})

    @also_clear(get_tou_settings)
    @time_cached(timedelta(hours=1))
    async def get_modes(self) -> dict[int, Mode]:
        """Get the available modes for the FranklinWH gateway.

        MUST be called once before using other methods, e.g., through get_mode().

        Returns:
        -------
        dict[int, Mode]
            A dictionary of available Mode keyed by workMode.

        get_modes[TIME_OF_USE]["name"] returns the actual rate name
        """
        body = await self.get_tou_settings()
        for v in body["list"]:
            self._modes[v["workMode"]] = Mode(**v)
        return self._modes

    async def get_mode(self) -> Mode:
        """Get the current operating mode of the FranklinWH gateway."""
        settings = await self.get_tou_settings()
        modes = await self.get_modes()
        for v in modes.values():
            if v["id"] == settings["currendId"]:
                return v
        raise ValueError(
            f"Unknown mode ID: {settings['currendId']}, please report at {ISSUES_URL}"
        )

    async def get_mode_by_name(self, name) -> Mode:
        """Get a Mode by actual or WorkMode name."""
        modes = await self.get_modes()
        for v in modes.values():
            if v["name"] == name:
                return v
        for v in WorkMode:
            if v.title == name:
                return modes[v.id]
        raise ValueError(f"Unknown mode name: {name}, please report at {ISSUES_URL}")

    async def set_mode(self, mode: Mode):
        """Set the operating mode of the FranklinWH gateway."""
        if mode.workMode > WorkMode.EMERGENCY_BACKUP.id:
            raise ValueError(mode.name + " cannot be set directly.")
        # refresh Storm Hedge
        self.get_modes.clear()
        hedge = (await self.get_tou_settings()).get("stromEn", 1)
        url = self.url_base + "hes-gateway/terminal/tou/updateTouModeV2"
        payload = mode.payload(self.gateway, hedge)
        await self._post_form(url, payload)
        self.get_modes.clear()

    async def set_backup_reserve(self, soc: int) -> None:
        """Set the backup reserve for the FranklinWH gateway.

        Parameters
        ----------
        soc : int
            The desired State of Charge percentage to set for backup reserve.
        """
        mode = await self.get_mode()
        if mode.workMode >= WorkMode.EMERGENCY_BACKUP.id:
            raise ValueError("Backup Reserve cannot be set in " + mode.name + ".")
        url = self.url_base + "hes-gateway/terminal/tou/updateSocV2"
        params = {
            "soc": soc,
            "workMode": mode.workMode,
        }
        await self._post(url, None, params)
        self.get_modes.clear()

    async def get_stats(self) -> Stats:
        """Get current statistics for the FHP.

        This includes instantaneous measurements for current power, as well as totals for today (in local time)
        """
        tasks = [f() for f in [self.get_composite_info, self._switch_usage]]
        info, sw_data = await asyncio.gather(*tasks)
        if info is None or info["runtimeData"] is None:
            raise InvalidDataException
        data = info["runtimeData"]

        grid_status: GridStatus = GridStatus.NORMAL
        if "offgridreason" in data:
            grid_status = GridStatus.from_offgridreason(data["offgridreason"])

        return Stats(
            Current(
                data["p_sun"],
                data["p_gen"],
                data["genStat"] > 1,
                data["p_fhp"],
                data["p_uti"],
                data["p_load"],
                data["soc"],
                sw_data["SW1ExpPower"],
                sw_data["SW2ExpPower"],
                sw_data["CarSWPower"],
                grid_status,
                RunStatus.from_id(data["run_status"]),
            ),
            Totals(
                data["kwh_fhp_chg"],
                data["kwh_fhp_di"],
                data["kwh_uti_in"],
                data["kwh_uti_out"],
                data["kwh_sun"],
                data["kwh_gen"],
                data["kwh_load"],
                sw_data["SW1ExpEnergy"],
                sw_data["SW2ExpEnergy"],
                sw_data["CarSWExpEnergy"],
                sw_data["CarSWImpEnergy"],
            ),
        )

    def next_snno(self):
        """Get the next sequence number for API requests."""
        self.snno += 1
        return self.snno

    def _build_payload(self, ty, data):
        raw = json.dumps(data, separators=(",", ":"))
        blob = raw.encode("utf-8")
        crc = to_hex(zlib.crc32(blob))
        ts = int(time.time())

        temp = json.dumps(
            {
                "lang": "EN_US",
                "cmdType": ty,
                "equipNo": self.gateway,
                "type": 0,
                "timeStamp": ts,
                "snno": self.next_snno(),
                "len": len(blob),
                "crc": crc,
                "dataArea": "DATA",
            }
        )
        # We do it this way because without a canonical way to generate JSON we can't risk reordering breaking the CRC.
        return temp.replace('"DATA"', raw)

    async def _mqtt_send(self, payload):
        url = DEFAULT_URL_BASE + "hes-gateway/terminal/sendMqtt"

        res = await self._post(url, payload)
        if res["code"] == 102:
            raise DeviceTimeoutException(res["message"])
        if res["code"] == 136:
            raise GatewayOfflineException(res["message"])
        if res["code"] == 181:
            raise PermissionDeniedException(res["message"])
        assert res["code"] == 200, f"{res['code']}: {res['message']}"
        return res

    async def set_grid_status(self, status: GridStatus, soc: int = 5):
        """Set the grid status of the FranklinWH gateway.

        Parameters
        ----------
        status : GridStatus
            The desired grid status to set.
        """
        url = self.url_base + "hes-gateway/terminal/updateOffgrid"
        payload = {
            "gatewayId": self.gateway,
            "offgridSet": int(status != GridStatus.NORMAL),
            "offgridSoc": soc,
        }
        await self._post(url, json.dumps(payload))

    async def get_export_settings(self) -> ExportSettings:
        """Get the current grid export mode and power limit.

        Returns:
        -------
        ExportSettings
            The active export mode and optional kW cap.
        """
        url = self.url_base + "hes-gateway/terminal/tou/getPowerControlSetting"
        result = (await self._get(url))["result"]
        mode = ExportMode.from_flag(result["gridFeedMaxFlag"])
        feed_max = result.get("gridFeedMax", -1.0)
        limit_kw = None if feed_max < 0 else feed_max
        return ExportSettings(mode=mode, limit_kw=limit_kw)

    async def set_export_settings(
        self, mode: ExportMode, limit_kw: float | None = None
    ) -> None:
        """Set the grid export mode and optional power limit.

        Uses a read-modify-write pattern: the setPowerControlV2 endpoint
        requires all existing settings to be echoed back alongside the
        fields being changed.

        Parameters
        ----------
        mode : ExportMode
            The desired export mode.
        limit_kw : float | None, optional
            Export power cap in kW (0.1-10000.0). None means unlimited.
            Ignored when mode is NO_EXPORT.
        """
        get_url = self.url_base + "hes-gateway/terminal/tou/getPowerControlSetting"
        set_url = self.url_base + "hes-gateway/terminal/tou/setPowerControlV2"

        # Read current settings — endpoint requires all fields posted back
        current = (await self._get(get_url))["result"]

        if mode == ExportMode.NO_EXPORT:
            feed_max = 0.0
            discharge_max = 0.0
        elif mode == ExportMode.SOLAR_AND_APOWER:
            feed_max = -1.0 if limit_kw is None else float(limit_kw)
            discharge_max = -1.0
        else:  # SOLAR_ONLY
            feed_max = -1.0 if limit_kw is None else float(limit_kw)
            discharge_max = 0.0

        payload = {k: v for k, v in current.items() if v is not None}
        payload.update(
            {
                "gatewayId": self.gateway,
                "lang": "EN_US",
                "gridFeedMaxFlag": mode.value,
                "gridFeedMax": feed_max,
                "globalGridDischargeMax": discharge_max,
            }
        )

        res = await self.session.post(
            set_url,
            headers={"loginToken": self.token, "Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        res.raise_for_status()
        body = res.json()
        if body.get("code") != 200:
            raise RuntimeError(f"set_export_settings failed: {body}")

    @time_cached()
    async def get_composite_info(self):
        """Get composite information about the FranklinWH gateway."""
        url = self.url_base + "hes-gateway/terminal/getDeviceCompositeInfo"
        params = {"refreshFlag": 1}
        return (await self._get(url, params))["result"]

    async def set_generator(self, enabled: bool):
        """Enable or disable the generator on the FranklinWH gateway.

        Parameters
        ----------
        enabled : bool
            True to enable the generator, False to disable it.
        """
        url = self.url_base + "hes-gateway/terminal/updateIotGenerator"
        payload = {"manuSw": 1 + int(enabled), "gatewayId": self.gateway, "opt": 1}
        await self._post(url, json.dumps(payload))

    async def get_home_gateway_list(self):
        """Get the list of Home Gateways associated with the account.

        Returns:
        -------
        JSON payload containing the list of Home Gateway information
        - email account linked (binded), location, timezone, etc.
        - number of aGates, status (online/offline), model, firmware version, etc
        - connectivity type (4G/WiFi/Ethernet), etc
        """
        url = DEFAULT_URL_BASE + "hes-gateway/terminal/getHomeGatewayList"
        return (await self._get(url))["result"]


class UnknownMethodsClient(Client):
    """A client that also implements some methods that don't obviously work, for research purposes."""

    async def get_controllable_loads(self):
        """Get the list of controllable loads connected to the gateway."""
        url = (
            self.url_base
            + "hes-gateway/terminal/selectTerGatewayControlLoadByGatewayId"
        )
        params = {"id": self.gateway, "lang": "en_US"}
        headers = {"loginToken": self.token}
        res = await self.session.get(url, params=params, headers=headers)
        return res.json()

    async def get_accessory_list(self):
        """Get the list of accessories connected to the gateway."""
        url = self.url_base + "hes-gateway/terminal/getIotAccessoryList"
        params = {"gatewayId": self.gateway, "lang": "en_US"}
        headers = {"loginToken": self.token}
        res = await self.session.get(url, params=params, headers=headers)
        return res.json()

    async def get_equipment_list(self):
        """Get the list of equipment connected to the gateway."""
        url = self.url_base + "hes-gateway/manage/getEquipmentList"
        params = {"gatewayId": self.gateway, "lang": "en_US"}
        headers = {"loginToken": self.token}
        res = await self.session.get(url, params=params, headers=headers)
        return res.json()
