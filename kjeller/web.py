from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import controller
import state
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

STATIC_DIR = Path(__file__).parent / "static"


def start_before_end(value: str) -> str:
    start, end = value.split("-")
    if start >= end:
        raise ValueError(f"{value}: start must be before end")
    return value


# adjust_temperature() falls back to 10 for anything outside 6-24
Temperature = Annotated[int, Field(ge=6, le=24)]
TimeRange = Annotated[
    str,
    StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"),
    AfterValidator(start_before_end),
]
Schedule = dict[controller.Weekday, list[str] | None]


class SettingsUpdate(BaseModel):
    """None means fall back to config.yaml."""

    temperature: Temperature | None = None
    night_temperature: Temperature | None = None
    schedule: dict[controller.Weekday, list[TimeRange] | None] | None = None


class Settings(BaseModel):
    temperature: int | None
    night_temperature: int | None
    schedule: Schedule | None


class Reading(BaseModel):
    temperature: float | None
    floor_temperature: float | None
    heat_set_point: float | None
    heating: bool | None
    target: int
    reason: Literal["schedule", "night", "price"]
    updated_at: datetime


class RoomView(BaseModel):
    name: str
    reading: Reading | None
    config: Settings
    override: Settings
    effective: Settings


class Overview(BaseModel):
    price_now: float | None
    rooms: list[RoomView]


app = FastAPI(title="Kjeller")


def all_days(schedule: Schedule) -> Schedule:
    return {day: schedule.get(day) for day in controller.Weekday}


def resolved(room: controller.RoomConfig, config: controller.Config) -> Settings:
    return Settings(
        temperature=controller.day_temperature(room, config.global_config),
        night_temperature=controller.night_temperature(room, config.global_config),
        schedule=all_days(room.schedule),
    )


def room_view(
    room: controller.RoomConfig,
    override: state.RoomOverride | None,
    config: controller.Config,
) -> RoomView:
    override = override or state.RoomOverride()
    reading = None
    if (room_status := controller.status.rooms.get(room.name)) is not None:
        termostat = room_status.termostat
        reading = Reading(
            temperature=termostat.temperature if termostat else None,
            floor_temperature=termostat.floor_temperatur if termostat else None,
            heat_set_point=termostat.heat_set_point if termostat else None,
            heating=termostat.heating if termostat else None,
            target=room_status.target,
            reason=room_status.reason,
            updated_at=room_status.updated_at,
        )
    return RoomView(
        name=room.name,
        reading=reading,
        config=resolved(room, config),
        override=Settings.model_validate(override.model_dump()),
        effective=resolved(controller.apply_override(room, override), config),
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/config", include_in_schema=False)
def config_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "config.html")


@app.get("/api/rooms")
def get_rooms() -> Overview:
    config = controller.load_file_config()
    overrides = state.get_overrides()
    return Overview(
        price_now=controller.status.price_now,
        rooms=[
            room_view(room, overrides.get(room.name), config) for room in config.room
        ],
    )


@app.put("/api/rooms/{name}")
def update_room(name: str, settings: SettingsUpdate) -> RoomView:
    config = controller.load_file_config()
    room = next((room for room in config.room if room.name == name), None)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Unknown room {name}")

    override = state.RoomOverride.model_validate(settings.model_dump(mode="json"))
    state.set_override(name, override)
    controller.wake.set()
    return room_view(room, override, config)
