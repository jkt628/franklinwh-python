"""Client for interacting with FranklinWH gateway API.

This module provides classes and functions to authenticate, send commands,
and retrieve statistics from FranklinWH energy gateway devices.
"""

# must work with Python >= 3.13
from __future__ import annotations  # noqa: RUF100, TID251

import asyncio
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
import hashlib
import json
import logging
import time
from typing import Any, Self
import zlib

import httpx

from .api import DEFAULT_URL_BASE, ISSUES_URL
from .time_cached import time_cached

# misspelled in the FranklinHW API:
# currendId: current Id
# runingMode: running mode
# stromEn: Storm Hedge enabled


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


@dataclass
class Stats:
    """Statistics for FranklinWH gateway."""

    current: Current
    totals: Totals


class Id(Enum):
    """Add identification to an enum."""

    id: int

    def __new__(cls, title, id) -> Self:
        """Add identification to an enum."""
        obj = object.__new__(cls)
        obj._value_ = title
        obj.id = id
        return obj

    @classmethod
    def ids(cls) -> Generator[int]:
        """Generate the ids of the enum members."""
        for item in cls:
            yield item.id

    @classmethod
    def names(cls) -> Generator[str]:
        """Generate the names of the enum members."""
        for item in cls:
            yield item.name

    @classmethod
    def values(cls) -> Generator[str]:
        """Generate the values of the enum members."""
        for item in cls:
            yield item.value

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
    def from_value(cls, value: str) -> Self:
        """Get the enum member corresponding to the given value.

        Parameters
        ----------
        value : str
            The value to look up.

        Returns:
        -------
        Self
            The enum member corresponding to the given value.

        Raises:
        ------
        ValueError
            If no enum member has the given value.
        """
        for item in cls:
            if item.value == value:
                return item
        raise ValueError(f"No {cls.__name__} with value {value}")


class AccessoryType(Id):
    """Represents the type of accessory connected to the FranklinWH gateway."""

    GENERATOR_MODULE = ("Generator", 3)
    SMART_CIRCUITS_MODULE = ("Smart Circuits", 4)


class RunStatus(Id):
    """Represent run_status values of the FranklinWH gateway."""

    STANDBY = ("Standby", 0)
    CHARGING = ("Charging", 1)
    DISCHARGING = ("Discharging", 2)

    @staticmethod
    def from_id(id: int) -> RunStatus:
        """Convert a run_status id to a RunStatus enum member.

        Parameters
        ----------
        value : int
            The run_status id to convert.

        Returns:
        -------
        RunStatus
            The corresponding RunStatus enum member.
        """
        match id:
            case 0:
                return RunStatus.STANDBY
            case 1:
                return RunStatus.CHARGING
            case 2:
                return RunStatus.DISCHARGING
            case _:
                raise ValueError(f"Unknown run_status id: {id}")


class WorkMode(Id):
    """Represents the workMode values of the FranklinWH gateway.

    These are the only operating mode constants in the FranklinWH API.

    Attributes:
        TIME_OF_USE (int): Time of Use mode, id = 1.
        SELF_CONSUMPTION (int): Self-Consumption mode, id = 2.
        EMERGENCY_BACKUP (int): Emergency Backup mode, id = 3.

    These are artificial and controlled by API, support or provider.

    Attributes:
        GENERATOR (int): Generator mode, id = 7.
        DEBUG (int): Debug mode, id = 8.
        VPP_MODE (int): VPP Mode, id = 9.
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

    _modes: dict[int, dict[str, Any]] = {
        mode.id: {  # compatible with result of getGatewayTouListV2
            "id": mode.id,
            "oldIndex": 3,
            "name": mode.value,
            "soc": 100.0,
            "maxSoc": 100.0,
            "minSoc": 100.0,
            "dischargeDepthSoc": None,
            "editSocFlag": False,
            "multiSOCFlag": False,
            "workMode": mode.id,
            "energyIncentivesType": 0,
            "electricityType": 1,
            "displayFlag": None,
        }
        for mode in WorkMode
    }

    @classmethod
    async def get_modes(cls, client: Client) -> dict[int, dict[str, Any]]:
        """Get the available modes for the FranklinWH gateway.

        MUST be called once before using other methods, e.g., through get_mode().

        Parameters
        ----------
        client : Client
            The FranklinWH client instance.

        Returns:
        -------
        dict[int, Any]
            A dictionary of available modes keyed by workMode.

        get_modes[TIME_OF_USE]["name"] returns the actual rate name
        """
        body = await client.get_tou_settings()
        for v in body["list"]:
            cls._modes[v["workMode"]] = v
        return cls._modes

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
            soc = 20
        return Mode(WorkMode.TIME_OF_USE.id, soc)

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
            soc = 100
        return Mode(WorkMode.EMERGENCY_BACKUP.id, soc)

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
            soc = 20
        return Mode(WorkMode.SELF_CONSUMPTION.value, soc)

    @classmethod
    def vpp_mode(cls, _: float | None = None) -> Mode:
        """Create a virtual power plant mode instance.

        Returns:
        -------
        Mode
            An instance of Mode configured for virtual power plant mode.
        """
        return Mode(WorkMode.VPP_MODE.value, 100)

    @classmethod
    def get_by_name(cls, name: str) -> Mode:
        """Get a Mode instance by its name.

        Parameters
        ----------
        name : str
            The name of the mode.

        Returns:
        -------
        Mode
            An instance of Mode corresponding to the given name.

        Raises:
        ------
        ValueError
            If the mode name is unknown.
        """
        for mode in WorkMode:
            if mode.value == name:
                return Mode(mode.id, cls._modes[mode.id].get("soc"))
        raise ValueError(f"Unknown mode name: {name}")

    def __init__(self, *args, **kwargs) -> None:
        """Initialize a Mode instance with the specified work mode and state of charge.

        Parameters
        ----------
        workMode : int
            The work mode id for the FranklinWH gateway.
        soc : float | None
            The state of charge value for the mode.
        """
        super().__init__()
        self.__dict__ = self
        self.workMode = kwargs.get("workMode", args[0])
        self.soc = float(kwargs.get("soc", args[1]))
        mode = self._modes[self.workMode]
        self.name = WorkMode.from_id(self.workMode).value
        self.currendId = mode["id"]
        self.oldIndex = mode["oldIndex"]

    def payload(self, gateway, hedge: bool | int, soc: int | None = None) -> dict:
        """Generate the payload dictionary for API requests to set the gateway's operating mode.

        Parameters
        ----------
        gateway : str
            The gateway identifier.
        hedge : bool | int
            Is Storm Hedge enabled?
        soc : int, optional
            New State of Charge value.

        Returns:
        -------
        dict
            The payload dictionary for the API request.
        """
        params = {
            "currendId": str(self.currendId),
            "gatewayId": gateway,
            "lang": "EN_US",
            "oldIndex": str(self.oldIndex),
            "stromEn": str(int(bool(hedge))),
            "workMode": str(self.workMode),
        }
        if soc is not None:
            params["soc"] = str(soc)
        return params


@dataclass
class Circuit:
    """Represents the basic state of a smart circuit."""

    on: bool


@dataclass
class EnhancedCircuit(Circuit):
    """Represents the enhanced state of a smart circuit.

    Attributes:
    ----------
    name : str
        The name of the circuit.
    load : float
        The current load value for the circuit.
    export : float
        The total export value for the circuit.
    soc_threshold : float
        The state of charge threshold for the circuit.
    """

    # these come from configuration
    name: str
    soc_threshold: int
    """The state of charge threshold for the circuit."""
    schedule: dict[str, Any]
    # these come from status
    power: float
    """The current consumption in kW for the circuit."""
    export_energy: float
    """The total export energy in kWh for the circuit."""
    import_energy: float
    """The total import energy in kWh for the circuit."""


class SmartCircuits:
    """Represents the state of the SmartCircuits module.

    Attributes:
    ----------
    merged : bool
        Indicates whether Circuits 1 and 2 are merged.
    circuits : dict[int, Circuit]
        A represention of the state of each active Circuit.
        Keys 1 and 3 are always present, key 2 is present only if merged is False.
        Depending on which API you call, these may be either Circuit or EnhancedCircuit objects.
    """

    @staticmethod
    def is_merged(data: dict) -> bool:
        """Determine if Circuits 1 and 2 are merged based on the provided data.

        Parameters
        ----------
        data : dict
            The data dictionary containing the "merge" key.

        Returns:
        -------
        bool
            True if Circuits 1 and 2 are merged, False otherwise.
        """
        return "merge" in data and data["merge"][0] == 1

    @staticmethod
    def openAction(_on: bool) -> int:
        """Convert a boolean on/off value to the corresponding openAction integer.

        Parameters
        ----------
        on : bool
            True to turn the circuit on, False to turn it off.

        Returns:
        -------
        int
        """
        return 2 if _on else 1

    on = openAction(True)

    def __init__(self, merged: bool, circuits: list[Circuit | None]) -> None:
        """Initialize a SmartCircuits instance."""
        self.merged = merged
        # simulate list by controlling insertion
        self.circuits: dict[int, Circuit] = {
            1: circuits[0],
        }
        if not merged:
            self.circuits[2] = circuits[1]
        elif isinstance(circuits[1], EnhancedCircuit):
            self.circuits[1].power += circuits[1].power
            self.circuits[1].export_energy += circuits[1].export_energy
            self.circuits[1].import_energy += circuits[1].import_energy
        self.circuits[3] = circuits[2]
        # fix statistics
        for c in self.circuits.values():
            if isinstance(c, EnhancedCircuit):
                if not c.on:
                    c.power = 0.0


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
    """Factory to create AsyncClient."""

    @staticmethod
    def default_get_client() -> httpx.AsyncClient:
        """Create an HTTP/2 AsyncClient."""
        return httpx.AsyncClient(http2=True)

    factory: Callable[..., httpx.AsyncClient] = default_get_client

    @classmethod
    def set_client_factory(cls, factory: Callable[..., httpx.AsyncClient]) -> None:
        """Set AsyncClient factory method."""
        cls.factory = factory

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        """Create an AsyncClient via factory method."""
        return cls.factory()


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
    async def login(username: str, password: str) -> str:
        """Log in to the FranklinWH API and retrieve an authentication token."""
        return await TokenFetcher(username, password).get_token()

    async def fetch_token(self) -> dict:
        """Log in to the FranklinWH API and retrieve account information."""
        url = (
            DEFAULT_URL_BASE + "hes-gateway/terminal/initialize/appUserOrInstallerLogin"
        )
        form = {
            "account": self.username,
            "password": hashlib.md5(bytes(self.password, "ascii")).hexdigest(),
            "lang": "en_US",
            "type": 1,
        }
        async with self.get_client() as client:
            res = await client.post(url, data=form, timeout=10)
        res.raise_for_status()
        js = res.json()

        if js["code"] == 401:
            raise InvalidCredentialsException(js["message"])

        if js["code"] == 400:
            raise AccountLockedException(js["message"])

        return js["result"]


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

        # to enable detailed logging add this to configuration.yaml:
        # logger:
        #   logs:
        #     franklinwh: debug

        self.logger = logging.getLogger("franklinwh")
        self.logger.debug("Session class: %s", type(self.session))
        if self.logger.isEnabledFor(logging.DEBUG):

            async def debug_request(request: httpx.Request):
                body = request.content
                if body and request.headers.get("Content-Type", "").startswith(
                    "application/json"
                ):
                    body = json.dumps(json.loads(body), ensure_ascii=False)
                self.logger.debug(
                    "Request: %s %s %s %s",
                    request.method,
                    request.url,
                    request.headers,
                    body,
                )
                return request

            async def debug_response(response: httpx.Response):
                await response.aread()
                self.logger.debug(
                    "Response: %s %s %s %s",
                    response.status_code,
                    response.url,
                    response.headers,
                    response.json(),
                )
                return response

            self.session.event_hooks["request"].append(debug_request)
            self.session.event_hooks["response"].append(debug_response)

    # TODO(richo) Setup timeouts and deal with them gracefully.
    async def _post(self, url, payload, params: dict | None = None):
        if params is None:
            params = {}
        else:
            params = params.copy()
        params.update({"gatewayId": self.gateway, "lang": "en_US"})

        async def __post():
            return (
                await self.session.post(
                    url,
                    params=params,
                    headers={
                        "loginToken": self.token,
                        "Content-Type": "application/json",
                    },
                    data=payload,
                )
            ).json()

        return await retry(__post, lambda j: j["code"] != 401, self.refresh_token)

    async def _post_form(self, url, payload):
        async def __post():
            return (
                await self.session.post(
                    url,
                    headers={
                        "loginToken": self.token,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "optsource": "3",
                    },
                    data=payload,
                )
            ).json()

        return await retry(__post, lambda j: j["code"] != 401, self.refresh_token)

    async def _get(self, url, params: dict | None = None):
        if params is None:
            params = {}
        else:
            params = params.copy()
        params.update({"gatewayId": self.gateway, "lang": "en_US"})

        async def __get():
            return (
                await self.session.get(
                    url, params=params, headers={"loginToken": self.token}
                )
            ).json()

        return await retry(__get, lambda j: j["code"] != 401, self.refresh_token)

    async def refresh_token(self):
        """Refresh the authentication token using the TokenFetcher."""
        self.token = await self.fetcher.get_token()

    async def get_accessories(self):
        """Get the list of accessories connected to the gateway."""
        url = self.url_base + "hes-gateway/common/getAccessoryList"
        # with no accessories this returns:
        # {"code":200,"message":"Query success!","result":[],"success":true,"total":0}
        return (await self._get(url))["result"]

    # Sends a 203 which is a high level status
    @time_cached()
    async def _status(self):
        payload = self._build_payload(203, {"opt": 1, "refreshData": 1})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    @time_cached(timedelta(hours=1))  # eventually consistent with changes via app
    async def get_tou_settings(self) -> dict[str, Any]:
        """Get the current Time of Use settings for the FranklinWH gateway."""
        url = self.url_base + "hes-gateway/terminal/tou/getGatewayTouListV2"
        res = await self._post(url, None, {"showType": 1})
        return res.get("result", {})

    async def set_mode(self, mode: Mode):
        """Set the operating mode of the FranklinWH gateway."""
        if mode.workMode > WorkMode.EMERGENCY_BACKUP.id:
            raise ValueError(mode.name + " cannot be set directly.")
        # refresh Storm Hedge
        self.get_tou_settings.clear()
        hedge = (await self.get_tou_settings()).get("stromEn", 1)
        url = self.url_base + "hes-gateway/terminal/tou/updateTouModeV2"
        payload = mode.payload(self.gateway, hedge)
        await self._post_form(url, payload)
        self.get_tou_settings.clear()

    async def get_mode(self) -> Mode:
        """Get the current operating mode of the FranklinWH gateway."""
        settings = await self.get_tou_settings()
        modes = await Mode.get_modes(self)
        for v in modes.values():
            if v["id"] == settings["currendId"]:
                return v
        raise ValueError(
            f"Unknown mode ID: {settings['currendId']}, please report at {ISSUES_URL}"
        )

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
        self.get_tou_settings.clear()

    async def get_stats(self) -> Stats:
        """Get current statistics for the FHP.

        This includes instantaneous measurements for current power, as well as totals for today (in local time)
        """
        data = (await self.get_composite_info())["runtimeData"]
        if data is None:
            raise InvalidDataException

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

    @time_cached(ttl=timedelta(seconds=5))
    async def __387(self):
        """Get SmartCircuits module configuration."""
        payload = self._build_payload(387, {"opt": 0})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    @time_cached()
    async def __389(self):
        """Get SmartCircuits module status."""
        payload = self._build_payload(389, {"opt": 0})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    async def get_smart_circuits(self, data: dict | None = None) -> SmartCircuits:
        """Get the basic state of the SmartCircuits module."""
        if data is None:
            data = await self.__387()
        circuits = [
            Circuit(on=x["openAction"] == SmartCircuits.on) for x in data["smartSwitch"]
        ]
        return SmartCircuits(SmartCircuits.is_merged(data), circuits)

    async def get_smart_circuits_enhanced(self) -> SmartCircuits:
        """Get the enhanced state of the SmartCircuits module."""
        tasks = [f() for f in (self.__387, self.__389)]
        data, status = await asyncio.gather(*tasks)
        circuits = [
            EnhancedCircuit(
                on=d["openAction"] == SmartCircuits.on,
                name=d["name"],
                soc_threshold=d["socThreshold"],
                schedule=d["schedule"],
                power=s["power"] / 1000.0,
                export_energy=s["exportEnergy"] / 1000.0,
                import_energy=s["importEnergy"] / 1000.0,
            )
            for d, s in zip(data["smartSwitch"], status["smartSwitchData"], strict=True)
        ]
        return SmartCircuits(SmartCircuits.is_merged(data), circuits)

    async def set_circuit(self, circuit: int, on: bool) -> SmartCircuits:
        """Set the state of a specific circuit on the SmartCircuits module.

        When merged, Circuit 1 also affects Circuit 2.

        Parameters
        ----------
        circuit : int
            The circuit number to set (1,3) and (2) if not merged.
        on : bool
            True to turn the circuit on, False to turn it off.
        """
        data = await self.__387()
        match circuit:
            case 3:
                pass
            case 2:
                if SmartCircuits.is_merged(data):
                    raise ValueError("Circuit 2 cannot be set when merged")
            case 1:
                if SmartCircuits.is_merged(data):
                    # if merged also set Circuit 2 the same way
                    data["smartSwitch"][1]["openAction"] = SmartCircuits.openAction(on)
            case _:
                raise ValueError("Circuit must be 1-3")
        data["smartSwitch"][circuit - 1]["openAction"] = SmartCircuits.openAction(on)
        data["opt"] = 1
        payload = self._build_payload(387, data)
        return await self.get_smart_circuits(
            json.loads((await self._mqtt_send(payload))["result"]["dataArea"])
        )

    async def set_smart_circuits_merged(self, merged: bool) -> SmartCircuits:
        """Set whether Circuits 1 and 2 are merged and adjust Circuit 2 accordingly."""
        data = await self.__387()
        if not merged:
            # separate
            data["merge"] = [0, 0]
        else:
            # align Circuit 2 with Circuit 1
            data["smartSwitch"][1]["openAction"] = data["smartSwitch"][0]["openAction"]
            # and merge
            data["merge"] = [1, 2]
        data["opt"] = 1
        payload = self._build_payload(387, data)
        return await self.get_smart_circuits(
            json.loads((await self._mqtt_send(payload))["result"]["dataArea"])
        )


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
