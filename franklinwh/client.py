"""Client for interacting with FranklinWH gateway API.

This module provides classes and functions to authenticate, send commands,
and retrieve statistics from FranklinWH energy gateway devices.

THIS version fakes the API responses for testing and development purposes, no actual gateways required.
Leave just enough API for https://github.com/jkt628/homeassistant-franklinwh
or https://github.com/richo/homeassistant-franklinwh, everything else breaks.
Two gateways with different accessories are simulated with statistics based on ID and random noise:

* 10060005A02X24456789 - has a generator module
* 10060005A02X24123456 - has a smart circuit module, NEVER merged

The last three digits of the gateway ID form the basis for the fake statistics.
Current stats add +/- 2% random noise to the basis, i.e.,
77.3-80.4 for the first gateway or 44.6-46.5 for the second.
Totals stats increase by random 2% of the basis.
States have a 2% chance of changing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from enum import Enum
import json
import random
import re
from typing import Any, Final, Self

import httpx


class AccessoryType(Enum):
    """Represents the type of accessory connected to the FranklinWH gateway.

    Attributes:
        SMART_CIRCUIT_MODULE (int): A Smart Circuit module, see https://www.franklinwh.com/document/download/smart-circuits-module-installation-guide-sku-accy-scv2-us
        GENERATOR_MODULE (int): A Generator module, see https://www.franklinwh.com/document/download/generator-module-installation-guide-sku-accy-genv2-us
    """

    GENERATOR_MODULE = 3
    SMART_CIRCUIT_MODULE = 4


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


conversions: Final = {
    "p_sun": "solar_production",
    "p_gen": "generator_production",
    "p_fhp": "battery_use",
    "p_uti": "grid_use",
    "p_load": "home_load",
    "soc": "battery_soc",
    "SW1ExpPower": "switch_1_load",
    "SW2ExpPower": "switch_2_load",
    "CarSWPower": "v2l_use",
    "kwh_fhp_chg": "battery_charge",
    "kwh_fhp_di": "battery_discharge",
    "kwh_uti_in": "grid_import",
    "kwh_uti_out": "grid_export",
    "kwh_sun": "solar",
    "kwh_gen": "generator",
    "kwh_load": "home_use",
    "SW1ExpEnergy": "switch_1_use",
    "SW2ExpEnergy": "switch_2_use",
    "CarSWExpEnergy": "v2l_export",
    "CarSWImpEnergy": "v2l_import",
}
no_generator: Final = {k: 0 for k in conversions if "gen" in k}
no_switch_usage: Final = {k: 0 for k in conversions if "SW" in k}


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


class Mode:
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

    _modes: dict[int, Any] = {
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
    async def get_modes(cls, client: Client) -> dict[int, Any]:
        """Fake the available modes for the FranklinWH gateway."""
        return cls._modes

    @classmethod
    def time_of_use(cls, soc: int | None = None) -> Mode:
        """Create a time of use mode instance.

        Parameters
        ----------
        soc : int, optional
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
    def emergency_backup(cls, soc: int | None = None) -> Mode:
        """Create an emergency backup mode instance.

        Parameters
        ----------
        soc : int, optional
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
    def self_consumption(cls, soc: int | None = None) -> Mode:
        """Create a self consumption mode instance.

        Parameters
        ----------
        soc : int, optional
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
    def vpp_mode(cls, _: int | None = None) -> Mode:
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

    def __init__(self, workMode: int, soc: int) -> None:
        """Initialize a Mode instance with the given state of charge.

        Parameters
        ----------
        soc : int
            The state of charge value for the mode.
        """
        self.workMode = workMode
        self.soc = soc
        mode = self._modes[workMode]
        self.name = WorkMode.from_id(workMode).value
        self.currendId = mode["id"]
        self.oldIndex = mode["oldIndex"]

    def payload(self, gateway, soc: int | None = None) -> dict:
        """Generate the payload dictionary for API requests to set the gateway's operating mode.

        Parameters
        ----------
        gateway : str
            The gateway identifier.
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
            "stromEn": "1",
            "workMode": str(self.workMode),
        }
        if soc is not None:
            params["soc"] = str(soc)
        return params


class SwitchState(tuple[bool | None, bool | None, bool | None]):
    """Represents the state of the smart switches connected to the FranklinWH gateway.

    Each element in the tuple corresponds to a switch:
        - True: Switch is ON
        - False: Switch is OFF
        - None: Switch state is unchanged
    """

    __slots__ = ()

    def __new__(cls, lst: Sequence[bool | None] | None = None):
        """Convert a sequence to a SwitchState tuple.

        Parameters
        ----------
        lst : optional Sequence[bool | None]
            The sequence to convert, defaults to [None, None, None].

        Returns:
        -------
        SwitchState
            The converted SwitchState tuple.
        """
        if lst is None:
            lst = [None, None, None]
        elif len(lst) != 3:
            raise ValueError(
                "Sequence must have exactly 3 elements to convert to SwitchState."
            )
        # tuple constructor only needs an iterable, convert to list for safety
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
    async def login(username: str, password: str) -> str:
        """Log in to the FranklinWH API and retrieve an authentication token."""
        return await TokenFetcher(username, password).get_token()

    async def fetch_token(self) -> dict:
        """Log in to the FranklinWH API and retrieve account information."""
        return {
            "token": "fake_token",
            "account": self.username,
        }


class Client(HttpClientFactory):
    """Client for interacting with FranklinWH gateway API."""

    def __init__(self, fetcher: TokenFetcher, gateway: str) -> None:
        """Initialize the Client with the provided TokenFetcher, gateway ID, and optional URL base."""
        self.fetcher = fetcher
        self.gateway = gateway
        self.has_generator = "9" in gateway
        self.has_smart_circuit = "3" in gateway
        self.basis = (
            float(gateway[-3:]) / 10
            if re.search(r"\d{3}$", gateway) is not None
            else 1.0
        )  # use last three digits of gateway ID for stats basis
        self.two_percent = (
            self.basis * 0.02
        )  # pre-calculate 2% of the basis for noise generation
        self.stats = empty_stats()
        self.switches = SwitchState() if self.has_smart_circuit else None
        self.mode = Mode.time_of_use()

    def get_current(self) -> float:
        """Generate a fake statistic value from the basis and random noise."""
        return self.basis + random.uniform(-self.two_percent, self.two_percent)

    def get_increase(self) -> float:
        """Generate a fake increase value from the basis and random noise."""
        return random.uniform(0.0, self.two_percent)

    def get_enum(self, current: Enum) -> Enum:
        """Generate a fake enum value based on the current value and random chance."""
        if random.random() < 0.02:  # 2% chance to change state
            values = list(current.__class__)
            values.remove(current)
            return random.choice(values)
        return current

    def get_id(self, current: Id) -> Id:
        """Generate a fake state value based on the current state and random chance."""
        if random.random() < 0.02:  # 2% chance to change state
            return current.__class__.from_id(
                random.choice(list(current.__class__.ids()))
            )
        return current

    async def get_accessories(self):
        """Fake the list of accessories connected to the gateway."""
        accessories = []
        if self.has_generator:
            accessories.append(
                {
                    "accessoryType": AccessoryType.GENERATOR_MODULE.value,
                }
            )
        if self.has_smart_circuit:
            accessories.append(
                {
                    "accessoryType": AccessoryType.SMART_CIRCUIT_MODULE.value,
                }
            )
        return accessories

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

    async def _status(self):
        status = {
            "pro_load": [0, 0, 0],
        }
        if self.has_smart_circuit:
            for i in range(3):
                if random.random() < 0.02:  # 2% chance to change state
                    status["pro_load"][i] = 1 - status["pro_load"][i]
        return status

    # Sends a 311 which appears to be a more specific switch command
    async def _switch_status(self):
        payload = self._build_payload(311, {"opt": 0, "order": self.gateway})
        data = (await self._mqtt_send(payload))["result"]["dataArea"]
        return json.loads(data)

    async def _switch_usage(self):
        if not self.has_smart_circuit:
            return no_switch_usage
        return {k: self.get_current() for k in conversions if k.endswith("Power")} | {
            k: getattr(self.stats.totals, v) + self.get_increase()
            for k, v in conversions.items()
            if k.endswith("Energy")
        }

    async def set_mode(self, mode: Mode):
        """Set the operating mode of the FranklinWH gateway."""
        if mode.workMode > WorkMode.EMERGENCY_BACKUP.id:
            raise ValueError(mode.name + " cannot be set directly.")
        url = self.url_base + "hes-gateway/terminal/tou/updateTouModeV2"
        payload = mode.payload(self.gateway)
        await self._post_form(url, payload)

    async def get_mode(self) -> Mode:
        """Fake the current operating mode of the FranklinWH gateway."""
        modes = await Mode.get_modes(self)
        status = await self.get_composite_info()
        for v in modes.values():
            if v["id"] == status["runtimeData"]["mode"]:
                return Mode(v["workMode"], v.get("soc"))
        return modes[status["currentWorkMode"]]

    async def set_backup_reserve(self, soc: int) -> None:
        """Set the backup reserve for the FranklinWH gateway.

        Parameters
        ----------
        soc : int
            The desired State of Charge percentage to set for backup reserve.
        """
        mode = await self.get_mode()
        if mode.workMode > WorkMode.EMERGENCY_BACKUP.id:
            raise ValueError("Backup Reserve cannot be set in " + mode.name + ".")
        url = self.url_base + "hes-gateway/terminal/tou/updateSocV2"
        params = {
            "soc": soc,
            "workMode": mode.workMode,
        }
        await self._post(url, None, params)

    async def get_stats(self) -> Stats:
        """Get current statistics for the FHP.

        This includes instantaneous measurements for current power, as well as totals for today (in local time)
        """
        tasks = [f() for f in [self.get_composite_info, self._switch_usage]]
        data, sw_data = await asyncio.gather(*tasks)
        data = data["runtimeData"]
        grid_status: GridStatus = GridStatus.NORMAL
        if "offgridreason" in data:
            grid_status = GridStatus.from_offgridreason(data["offgridreason"])

        self.stats = Stats(
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
        return self.stats

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

    async def get_composite_info(self) -> dict:
        """Fake composite information about the FranklinWH gateway."""
        data = (
            no_generator
            | {
                k: self.get_current()
                for k in conversions
                if k.startswith(("p_", "soc"))
                and (self.has_generator or "gen" not in k)
            }
            | {
                k: getattr(self.stats.totals, v) + self.get_increase()
                for k, v in conversions.items()
                if k.startswith("kwh_") and (self.has_generator or "gen" not in k)
            }
        )
        data["genStat"] = (
            2
            if self.has_generator
            and random.random() < 0.02
            and not self.stats.current.generator_enabled
            else 0
        )
        data["offgridreason"] = (
            self.get_enum(self.stats.current.grid_status).value
            if random.random() < 0.02
            else self.stats.current.grid_status.value
        ) - 1
        data["run_status"] = (
            self.get_id(self.stats.current.run_status).id
            if random.random() < 0.02
            else self.stats.current.run_status.id
        )
        data["mode"] = (
            self.get_id(WorkMode.from_id(self.mode.workMode)).id
            if random.random() < 0.02
            else self.mode.workMode
        )
        return {"runtimeData": data}

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
        return [
            {
                "id": "10060005A02X24456789",
            },
            {
                "id": "10060005A02X24123456",
            },
        ]
