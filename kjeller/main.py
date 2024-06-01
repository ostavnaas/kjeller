import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from time import sleep
from typing import Annotated

import httpx
import yaml
from pydantic import AfterValidator, AliasPath, BaseModel, Field, ValidationError


class TibberApiError(Exception):
    pass


def convert_to_fraction(v: int) -> float:
    return v / 100


class Termostat(BaseModel):
    name: Annotated[str, AfterValidator(str.lower)]
    temperature: Annotated[
        float,
        AfterValidator(convert_to_fraction),
        Field(validation_alias=AliasPath("state", "temperature")),
    ]
    floor_temperatur: Annotated[
        float,
        AfterValidator(convert_to_fraction),
        Field(validation_alias=AliasPath("state", "floortemperature")),
    ]
    heat_set_point: Annotated[
        float,
        AfterValidator(convert_to_fraction),
        Field(validation_alias=AliasPath("config", "heatsetpoint")),
    ]
    heating: Annotated[bool, Field(validation_alias=AliasPath("state", "heating"))]


class HourlyPrice(BaseModel):
    total: float
    start_at: Annotated[datetime, Field(validation_alias="startsAt")]

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(minutes=60)


class DailyPrices(BaseModel):
    prices: Annotated[
        list[HourlyPrice],
        Field(
            validation_alias=AliasPath(
                "data", "viewer", "home", "currentSubscription", "priceInfo", "today"
            )
        ),
    ]


@dataclass
class PrometheusExport:
    name: str
    temperature: int
    floortemperature: int
    heatsetpoint: int
    heating: bool


class Weekday(str, Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class RoomConfig(BaseModel):
    name: str
    uniqueid: str
    schedule: dict[Weekday, list[str] | None]
    temperature: int | None = None
    night_temperature: int | None = None
    max_price: float | None = None

    @property
    def todays_schedules(self) -> list[str] | None:
        day_of_week = Weekday(datetime.now(UTC).strftime("%A").lower())
        if day_of_week in self.schedule:
            return self.schedule[day_of_week]

        return None

    @property
    def within_time_range(self) -> bool:
        if self.todays_schedules is None:
            return False
        now = datetime.now(UTC)
        try:
            for schdule in self.todays_schedules:
                start = datetime.combine(
                    now, datetime.strptime(schdule.split("-")[0], "%H:%M").time()
                ).astimezone(UTC)

                end = datetime.combine(
                    now, datetime.strptime(schdule.split("-")[1], "%H:%M").time()
                ).astimezone(UTC)
                if start <= now <= end:
                    return True
        except ValueError:
            return False
        return False


class TibberConfig(BaseModel):
    access_token: str
    house_id: str


@dataclass
class deConz:
    endpoint: str
    api_key: str


class GlobalConfig(BaseModel):
    debug: bool
    sleep: int
    lat: float
    long: float
    max_price: float | None = None
    temperature: int | None = None
    night_temperature: int | None = None


class Config(BaseModel):
    global_config: Annotated[GlobalConfig, Field(validation_alias="global")]
    tibber: TibberConfig
    room: list[RoomConfig]
    deconz: deConz


class Tibber:
    def __init__(self, config: TibberConfig):
        self.house_id: str = config.house_id
        self.access_token: str = config.access_token
        self.daily_electricity_prices: DailyPrices | None = None
        self.price_now: float | None = None

    @property
    def date_format(self) -> str:
        return "%Y-%m-%dT%H:%M:%S.%f%z"

    @property
    def api_endpoint(self) -> str:
        return "https://api.tibber.com/v1-beta/gql"

    @property
    def is_stale(self) -> bool:
        return (
            not self.daily_electricity_prices
            or self.daily_electricity_prices.prices[0].start_at.date()
            != datetime.now(UTC).date()
        )

    def update_daily_electricity_prices(self) -> DailyPrices:
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
            logging.error("Tibber api error %s", e)
            raise TibberApiError from e

        return DailyPrices.model_validate_json(response.content)

    def update_electricity_prices(self):
        if self.is_stale:
            try:
                self.daily_electricity_prices = self.update_daily_electricity_prices()
            except (TibberApiError, ValidationError):
                logging.exception("Could not update Tibber price")
                self.daily_electricity_prices = None

        if self.daily_electricity_prices is None:
            self.price_now = None
            return

        for price in self.daily_electricity_prices.prices:
            if price.start_at <= datetime.now(UTC) < price.end_at:
                with Path("/home/oves/python3/gcal/tibber").open(
                    mode="w", encoding="utf-8"
                ) as file:
                    file.write(str(price.total))
                self.price_now = price.total
                logging.info("Electricity price %s kr/KwH", self.price_now)
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


def write_stats_to_prometheuse(termostat: Termostat):
    name = termostat.name
    with open(f"prom/{termostat.name}.prom", "w", encoding="utf-8") as file:
        file.write(f'deconz_temperature{{name="{name}"}} {termostat.temperature}\n')
        file.write(
            f'deconz_floortemperature{{name="{name}"}} {termostat.floor_temperatur}\n'
        )
        file.write(f'deconz_heatsetpoint{{name="{name}"}} {termostat.heat_set_point}\n')
        file.write(f'deconz_heating{{name="{name}"}} {int(termostat.heating)}\n')


def get_heatsetpoint_sensor(uniqueid: str, deconz: deConz) -> Termostat | None:
    url = f"{deconz.endpoint}/api/{deconz.api_key}/sensors/{uniqueid}"

    try:
        response = httpx.get(url)
    except httpx.RequestError as e:
        logging.error("%s API failed: %s", deconz.endpoint, e)
        return None
    if response.status_code != 200:
        print(f"Failed to Adjusting temperature: {response.text}")
        return None

    sensor = Termostat.model_validate_json(response.content)
    logging.info(
        "%s: Temperature: %s, Floortemperature: %s, heatsetpoint: %s, heating: %s",
        sensor.name,
        sensor.temperature,
        sensor.floor_temperatur,
        sensor.heat_set_point,
        sensor.heating,
    )
    return sensor


def load_config() -> Config:
    with open("config.yaml", "r", encoding="utf-8") as file:
        return Config.model_validate(yaml.full_load(file))


def ensure_temperature(
    uniqueid: str, set_temperature: int, room_name: str, deconz: deConz
):
    if (
        termostat := get_heatsetpoint_sensor(uniqueid, deconz)
    ) is not None and termostat.temperature != set_temperature:
        logging.info("%s: Setting new temperature %s", room_name, set_temperature)
        adjust_temperature(uniqueid, set_temperature, deconz)
        write_stats_to_prometheuse(termostat)


def time_in_range(start: str, end: str) -> bool:
    now = datetime.now(UTC)
    try:
        schdule_start = datetime.combine(
            now, datetime.strptime(start, "%H:%M").time()
        ).astimezone(UTC)
        schdule_end = datetime.combine(
            now, datetime.strptime(end, "%H:%M").time()
        ).astimezone(UTC)

        if datetime.strftime(schdule_end, "%H:%M") == "00.00":
            schdule_end = schdule_end - timedelta(minutes=1)

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
        elif room.within_time_range:
            set_temperature = room.temperature or config.global_config.temperature or 10

        ensure_temperature(room.uniqueid, set_temperature, room.name, config.deconz)


def main():
    logging.basicConfig(
        stream=sys.stdout,
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
        sleep(config.global_config.sleep or 60)


if __name__ == "__main__":
    main()
