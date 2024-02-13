import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from time import sleep

import httpx
import yaml


class TibberApiError(Exception):
    pass


@dataclass
class PrometheusExport:
    name: str
    temperature: int
    floortemperature: int
    heatsetpoint: int
    heating: bool


class Weekday(Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class Schedule:
    def __init__(self, schedule: dict[Weekday, list[str]]):
        self.schedules = schedule

    @property
    def todays_schedules(self) -> list[str] | None:
        day_of_week = Weekday(datetime.now().strftime("%A").lower())
        if day_of_week in self.schedules:
            return self.schedules[day_of_week]

        return None

    @property
    def within_time_range(self) -> bool:
        if self.todays_schedules is None:
            return False
        now = datetime.now()
        try:
            for schdule in self.todays_schedules:
                start = datetime.combine(
                    now, datetime.strptime(schdule.split("-")[0], "%H:%M").time()
                )

                end = datetime.combine(
                    now, datetime.strptime(schdule.split("-")[1], "%H:%M").time()
                )
                if start < now < end:
                    return True
        except ValueError:
            return False
        return False


@dataclass
class RoomConfig:
    name: str
    uniqueid: str
    schedule: Schedule
    temperature: int | None = None
    night_temperature: int | None = None
    max_price: float | None = None

    def __post_init__(self):
        assert isinstance(self.schedule, dict)
        self.schedule = Schedule({Weekday(k): v for k, v in self.schedule.items()})


@dataclass
class TibberConfig:
    access_token: str
    house_id: str


@dataclass
class deConz:
    endpoint: str
    api_key: str


@dataclass
class GlobalConfig:
    debug: bool
    sleep: int
    lat: float
    long: float
    max_price: float | None = None
    temperature: int | None = None
    night_temperature: int | None = None


@dataclass
class Config:
    global_config: GlobalConfig
    tibber: TibberConfig
    room: list[RoomConfig]
    deconz: deConz


class Tibber:
    def __init__(self, config: TibberConfig):
        self.house_id: str = config.house_id
        self.access_token: str = config.access_token
        self.daily_electricity_prices: dict | None = None
        self.price_now: float | None = None

    @property
    def date_format(self) -> str:
        return "%Y-%m-%dT%H:%M:%S.%f%z"

    @property
    def api_endpoint(self) -> str:
        return "https://api.tibber.com/v1-beta/gql"

    @property
    def is_stale(self) -> bool:
        return not self.daily_electricity_prices or (
            datetime.strptime(
                self.daily_electricity_prices[0]["startsAt"], self.date_format
            ).date()
            != datetime.now().date()
        )

    def get_daily_electricity_pricess(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "Kjeller/0.1",
        }
        variables = {"house_id": self.house_id}
        query = """
            query ( $house_id: ID!)  {
                viewer {
                    home(id: $house_id){
                            currentSubscription {
                                priceInfo {
                                    current {
                                        total
                                        startsAt
                                    }
                                    today {
                                        total
                                        startsAt
                                    }
                                }
                            }
                        }
                    }
                }
                """
        try:
            response = httpx.post(
                self.api_endpoint,
                json={"query": query, "variables": variables},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.RequestError as e:
            self.daily_electricity_prices = None
            logging.error("Tibber api error %s", e)
            return

        self.daily_electricity_prices = self.get_from_dict(response.json())

    def get_from_dict(self, data: dict) -> dict:
        nested_keys: list = [
            "data",
            "viewer",
            "home",
            "currentSubscription",
            "priceInfo",
            "today",
        ]

        try:
            for key in nested_keys:
                data = data[key]
            logging.info("Tibber price updated")
        except KeyError as e:
            logging.error("KeyError from TibberAPI json")
            raise TibberApiError from e
        return data

    def update_electricity_prices(self):
        if self.is_stale:
            self.get_daily_electricity_pricess()

        if self.daily_electricity_prices is None:
            self.price_now = None
            return

        for price in self.daily_electricity_prices:
            starts_at = datetime.strptime(
                price["startsAt"], self.date_format
            ).astimezone(UTC)
            ends_at = starts_at + timedelta(minutes=60)

            if starts_at <= datetime.now(UTC) < ends_at:
                with Path("/home/oves/python3/gcal/tibber", "w").open(
                    encoding="utc-8"
                ) as file:
                    file.write(str(price["total"]))
                self.price_now = price["total"]
                logging.info("Electricity price %s KwH", self.price_now)
                break
        else:
            self.price_now = None

    def exceed_max_price(self, price_cut_off: float | None) -> bool:
        if not self.price_now or price_cut_off is None:
            logging.info("Tibber price not avilable or no cut off")
            return False
        if self.price_now < price_cut_off:
            return False

        return True


def adjust_temperature(uniqueid: str, temperature: int, deconz: deConz) -> None:
    set_temp: int = 0

    if temperature <= 5 or temperature >= 25:
        set_temp = 10 * 100
    else:
        set_temp = temperature * 100

    url = f"{deconz.endpoint}/api/{deconz.api_key}/sensors/{uniqueid}/config"

    payload = {"heatsetpoint": set_temp}
    try:
        response = httpx.put(url, json=payload)
        response.raise_for_status()
    except httpx.RequestError as e:
        logging.error("%s API failed: %s", deconz.endpoint, e)


def update_prom(sensors):
    name = sensors["name"].lower()
    temperature = sensors["state"]["temperature"] / 100
    floortemperature = sensors["state"]["floortemperature"] / 100
    heatsetpoint = sensors["config"]["heatsetpoint"] / 100
    heating = sensors["state"]["heating"]

    if not name:
        return
    with open(f"prom/{name}.prom", "w", encoding="utf-8") as file:
        file.write(f'deconz_temperature{{name="{name}"}} {temperature}\n')
        file.write(f'deconz_floortemperature{{name="{name}"}} {floortemperature}\n')
        file.write(f'deconz_heatsetpoint{{name="{name}"}} {heatsetpoint}\n')
        file.write(f'deconz_heating{{name="{name}"}} {int(heating)}\n')


def predict_hourly_consumation(
    accumulated_consumation: float, current_consumption: float, max_hourly_consumation
) -> bool:
    now = datetime.now()
    return (
        accumulated_consumation + current_consumption * ((60 - now.minute) / 60)
    ) >= max_hourly_consumation


def get_heatsetpoint_sensor(uniqueid: str, deconz: deConz) -> float | None:
    url = f"{deconz.endpoint}/api/{deconz.api_key}/sensors/{uniqueid}"

    try:
        response = httpx.get(url)
    except httpx.RequestError as e:
        logging.error("%s API failed: %s", deconz.endpoint, e)
        return None
    if response.status_code != 200:
        print(f"Failed to Adjusting temperature: {response.text}")
        return None

    sensors = response.json()
    logging.info(
        "%s: Temperature: %s, Floortemperature: %s, heatsetpoint: %s, heating: %s",
        sensors["name"],
        sensors["state"]["temperature"],
        sensors["state"]["floortemperature"],
        sensors["config"]["heatsetpoint"],
        sensors["state"]["heating"],
    )
    update_prom(sensors)
    return int((sensors["config"]["heatsetpoint"]) / 100)


def load_config() -> Config:
    config: dict = {}
    with open("config.yaml", "r", encoding="utf-8") as file:
        config = yaml.full_load(file)

    return Config(
        global_config=GlobalConfig(**config.pop("global")),
        room=[RoomConfig(**x) for x in config.pop("room")],
        tibber=TibberConfig(**config.pop("tibber")),
        deconz=deConz(**config.pop("deconz")),
    )


def ensure_temperature(
    uniqueid: str, set_temperature: int, room_name: str, deconz: deConz
):
    if get_heatsetpoint_sensor(uniqueid, deconz) != set_temperature:
        logging.info("%s: Setting new temperature %s", room_name, set_temperature)
        adjust_temperature(uniqueid, set_temperature, deconz)


def time_in_range(start: str, end: str) -> bool:
    now = datetime.now()
    try:
        schdule_start = datetime.combine(now, datetime.strptime(start, "%H:%M").time())

        schdule_end = datetime.combine(now, datetime.strptime(end, "%H:%M").time())
        if schdule_start < now < schdule_end:
            return True
    except ValueError:
        return False

    return False


def set_schedule(config: Config, tibber: Tibber):
    for room in config.room:
        set_temperature = (
            room.night_temperature or config.global_config.night_temperature or 10
        )

        if tibber.exceed_max_price(room.max_price or config.global_config.max_price):
            logging.info(
                "%s: KWH %s NOK, and above maxprice",
                room.name,
                tibber.price_now,
            )
        elif room.schedule.within_time_range:
            set_temperature = room.temperature or config.global_config.temperature or 10

        ensure_temperature(room.uniqueid, set_temperature, room.name, config.deconz)


def main():
    logging.basicConfig(
        filename="heat.log",
        format="%(asctime)s: %(message)s",
        datefmt="%d-%m-%YT%H:%M:%S%z",
        level=logging.INFO,
    )

    config = load_config()
    tbr = Tibber(config.tibber)
    while True:
        config = load_config()
        tbr.update_electricity_prices()
        set_schedule(config, tbr)
        sleep(config.global_config.sleep)


if __name__ == "__main__":
    main()
