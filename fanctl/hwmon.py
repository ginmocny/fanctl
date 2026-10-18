"""Odczyt i zapis krzywych NCT67xx przez sysfs. Bez demonów, bez dodatkowych bibliotek."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HWMON = Path("/sys/class/hwmon")
SNAPSHOT = Path("/var/lib/fanctl/bios.json")
OVERRIDES = Path("/var/lib/fanctl/overrides.json")

# pwm_enable w sterowniku nct6775. 0 = wyłączona regulacja = pełne obroty. Nigdy nie zapisujemy 0.
ENABLE_OFF = 0
ENABLE_MANUAL = 1
ENABLE_SMART_FAN_IV = 5
ENABLE_NAMES = {
    0: "off-full",
    1: "manual",
    2: "thermal-cruise",
    3: "speed-cruise",
    4: "smart-fan-iii",
    5: "smart-fan-iv",
}

# Ta sama krzywa, którą BIOS już trzyma na fan1/fan2 tej płyty.
# Punkty: millistopnie, PWM 0-255.
BOARD_CURVE: tuple[tuple[int, int], ...] = (
    (30_000, 51),
    (50_000, 102),
    (70_000, 178),
    (75_000, 255),
    (125_000, 255),
)

NCT_NAMES = {
    "nct6775",
    "nct6776",
    "nct6779",
    "nct6791",
    "nct6792",
    "nct6793",
    "nct6795",
    "nct6796",
    "nct6797",
    "nct6798",
    "nct6799",
}

PEG_PWM = 250


class FanctlError(RuntimeError):
    pass


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_int(path: Path, value: int) -> None:
    if path.name.endswith("_enable") and int(value) == ENABLE_OFF:
        raise FanctlError(f"odmowa zapisu 0 do {path} — to pełne obroty")
    try:
        path.write_text(f"{int(value)}\n")
    except OSError as exc:
        raise FanctlError(f"zapis {path}={value} nie przeszedł: {exc}") from exc


def find_chip() -> Path:
    if not HWMON.is_dir():
        raise FanctlError("brak /sys/class/hwmon — czujniki nie są wystawione")
    found: list[Path] = []
    for node in sorted(HWMON.glob("hwmon*")):
        name = _read_text(node / "name") or ""
        if name in NCT_NAMES:
            found.append(node)
    if not found:
        raise FanctlError(
            "nie widać chipu NCT67xx. Na tej płycie: sudo modprobe nct6775"
        )
    # nct6799 wygrywa, gdyby kiedyś pojawiły się dwa
    found.sort(key=lambda p: (0 if _read_text(p / "name") == "nct6799" else 1, str(p)))
    return found[0]


def plan_monotonic(current: list[int], target: list[int]) -> list[tuple[int, int]]:
    """Kolejność zapisów, którą sterownik nct6775 przyjmie.

    Każdy punkt musi zostać między sąsiadami. Najpierw podnosimy krzywą do
    sufitu od prawej, potem opuszczamy do celu od lewej.
    """
    if len(current) != len(target) or not current:
        raise ValueError("krzywa ma złą długość")
    if any(target[i] > target[i + 1] for i in range(len(target) - 1)):
        raise ValueError(f"cel nie jest monotoniczny: {target}")
    if list(current) == list(target):
        return []
    cur = list(current)
    writes: list[tuple[int, int]] = []

    def emit(index: int, value: int) -> None:
        if cur[index] == value:
            return
        if index > 0 and value < cur[index - 1]:
            raise RuntimeError(f"zapis {index}={value} poniżej lewego sąsiada {cur}")
        if index + 1 < len(cur) and value > cur[index + 1]:
            raise RuntimeError(f"zapis {index}={value} powyżej prawego sąsiada {cur}")
        cur[index] = value
        writes.append((index, value))

    ceiling = max(max(cur), max(target))
    for index in reversed(range(len(cur))):
        emit(index, ceiling)
    for index, value in enumerate(target):
        emit(index, value)
    return writes


def pwm_percent(pwm: int | None) -> int | None:
    if pwm is None:
        return None
    return round(pwm * 100 / 255)


def percent_to_pwm(percent: int, floor: int = 20) -> int:
    if percent < floor:
        raise FanctlError(f"odmowa {percent}% — podłoga to {floor}%, inaczej wentylator staje")
    if percent > 100:
        raise FanctlError(f"procent poza zakresem: {percent}")
    return max(1, round(percent * 255 / 100))


@dataclass
class Point:
    temp_mc: int
    pwm: int

    def as_dict(self) -> dict[str, int]:
        return {"temp_mc": self.temp_mc, "pwm": self.pwm}


@dataclass
class Channel:
    index: int
    enable: int | None
    pwm: int | None
    rpm: int | None
    temp_sel: int | None
    points: list[Point]

    @property
    def spinning(self) -> bool:
        return (self.rpm or 0) > 0

    @property
    def pegged(self) -> bool:
        return bool(self.points) and all(p.pwm >= PEG_PWM for p in self.points)

    def as_dict(self) -> dict:
        return {
            "fan": self.index,
            "enable": self.enable,
            "mode": ENABLE_NAMES.get(self.enable, "unknown") if self.enable is not None else None,
            "pwm": self.pwm,
            "percent": pwm_percent(self.pwm),
            "rpm": self.rpm,
            "temp_sel": self.temp_sel,
            "spinning": self.spinning,
            "pegged": self.pegged,
            "points": [p.as_dict() for p in self.points],
        }


class Chip:
    def __init__(self, path: Path | None = None):
        self.path = path or find_chip()
        self.name = _read_text(self.path / "name") or "unknown"

    def channels(self) -> list[int]:
        out = []
        for index in range(1, 8):
            if (self.path / f"pwm{index}_enable").exists():
                out.append(index)
        return out

    def read_channel(self, index: int) -> Channel:
        points = []
        for point in range(1, 6):
            temp = _read_int(self.path / f"pwm{index}_auto_point{point}_temp")
            pwm = _read_int(self.path / f"pwm{index}_auto_point{point}_pwm")
            if temp is None or pwm is None:
                break
            points.append(Point(temp, pwm))
        return Channel(
            index=index,
            enable=_read_int(self.path / f"pwm{index}_enable"),
            pwm=_read_int(self.path / f"pwm{index}"),
            rpm=_read_int(self.path / f"fan{index}_input"),
            temp_sel=_read_int(self.path / f"pwm{index}_temp_sel"),
            points=points,
        )

    def temps(self) -> list[dict]:
        rows = []
        for index in range(1, 16):
            label = _read_text(self.path / f"temp{index}_label")
            value = _read_int(self.path / f"temp{index}_input")
            if label is None and value is None:
                continue
            rows.append(
                {
                    "name": label or f"temp{index}",
                    "c": None if value is None else round(value / 1000, 1),
                }
            )
        return rows

    def k10temp(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if not HWMON.is_dir():
            return out
        for node in HWMON.glob("hwmon*"):
            if _read_text(node / "name") != "k10temp":
                continue
            for index in range(1, 8):
                label = _read_text(node / f"temp{index}_label")
                value = _read_int(node / f"temp{index}_input")
                if label and value is not None:
                    out[label] = round(value / 1000, 1)
        return out

    def status(self) -> dict:
        channels = [self.read_channel(i).as_dict() for i in self.channels()]
        overrides = load_overrides()
        for row in channels:
            row["override"] = overrides.get(str(row["fan"]))
        return {
            "chip": self.name,
            "path": str(self.path),
            "temps": self.temps(),
            "k10temp": self.k10temp(),
            "channels": channels,
            "snapshot": str(SNAPSHOT) if SNAPSHOT.exists() else None,
            "overrides": overrides,
        }

    def _require_root(self) -> None:
        if os.geteuid() != 0:
            raise FanctlError("zapis do sysfs chce roota: sudo python3 -m fanctl ...")

    def _write_series(self, index: int, kind: str, current: list[int], target: list[int]) -> None:
        # kind: "temp" | "pwm"
        for point, value in plan_monotonic(current, target):
            suffix = "temp" if kind == "temp" else "pwm"
            _write_int(self.path / f"pwm{index}_auto_point{point + 1}_{suffix}", value)

    def write_curve(self, index: int, curve: tuple[tuple[int, int], ...] = BOARD_CURVE) -> Channel:
        self._require_root()
        if len(curve) != 5:
            raise FanctlError("krzywa musi mieć 5 punktów")
        channel = self.read_channel(index)
        if len(channel.points) != 5:
            raise FanctlError(f"fan{index} nie ma 5 punktów krzywej")
        self._write_series(
            index,
            "temp",
            [p.temp_mc for p in channel.points],
            [p[0] for p in curve],
        )
        # PWM czytamy ponownie — zapis temperatur nie rusza PWM, ale stan ma być świeży.
        fresh = self.read_channel(index)
        self._write_series(
            index,
            "pwm",
            [p.pwm for p in fresh.points],
            [p[1] for p in curve],
        )
        written = self.read_channel(index)
        got = [(p.temp_mc, p.pwm) for p in written.points]
        if list(got) != list(curve):
            raise FanctlError(f"fan{index}: chip oddał inną krzywą niż zapisana: {got}")
        return written

    def set_enable(self, index: int, mode: int) -> int:
        self._require_root()
        if mode == ENABLE_OFF:
            raise FanctlError("odmowa enable=0")
        _write_int(self.path / f"pwm{index}_enable", mode)
        got = None
        for _ in range(6):
            got = _read_int(self.path / f"pwm{index}_enable")
            if got == mode:
                return got
            time.sleep(0.05)
        # EC potrafi przeskoczyć na 0 (pełne obroty). Natychmiast oddaj BIOS.
        if got == ENABLE_OFF:
            _write_int(self.path / f"pwm{index}_enable", ENABLE_SMART_FAN_IV)
        raise FanctlError(
            f"fan{index}: chciałem enable={mode}, chip oddał {got}. "
            "Nie zostawiam go w ręcznym trybie."
        )

    def set_manual(self, index: int, percent: int) -> Channel:
        """Ręczny PWM. Kernel 7 oddaje EBUSY, jeśli pwm piszesz zanim enable=1."""
        self._require_root()
        pwm = percent_to_pwm(percent)
        before = self.read_channel(index)
        mode = before.enable if before.enable not in (None, ENABLE_OFF) else ENABLE_SMART_FAN_IV
        if before.enable != ENABLE_MANUAL:
            self.set_enable(index, ENABLE_MANUAL)
        try:
            _write_int(self.path / f"pwm{index}", pwm)
        except FanctlError:
            self.restore_enable(index, mode)
            raise
        after = self.read_channel(index)
        if after.enable != ENABLE_MANUAL or after.pwm != pwm:
            self.restore_enable(index, mode)
            raise FanctlError(
                f"fan{index}: ręczny PWM nie wszedł (enable={after.enable}, pwm={after.pwm})"
            )
        return after

    def restore_enable(self, index: int, mode: int = ENABLE_SMART_FAN_IV) -> None:
        self.set_enable(index, mode)


def save_snapshot(chip: Chip, indexes: list[int]) -> Path:
    """Pierwszy zapis wygrywa. Drugi apply nie może nadpisać BIOS cichą krzywą."""
    if os.geteuid() != 0:
        raise FanctlError("zapis migawki BIOS chce roota")
    existing: dict = {}
    if SNAPSHOT.exists():
        existing = json.loads(SNAPSHOT.read_text())
    channels = dict(existing.get("channels") or {})
    for index in indexes:
        key = str(index)
        if key in channels:
            continue
        channels[key] = chip.read_channel(index).as_dict()
    payload = {
        "chip": chip.name,
        "saved_at": existing.get("saved_at") or datetime.now(timezone.utc).isoformat(),
        "channels": channels,
    }
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(SNAPSHOT)
    return SNAPSHOT


def load_snapshot() -> dict:
    if not SNAPSHOT.exists():
        raise FanctlError(f"brak migawki {SNAPSHOT} — nie mam czego przywracać")
    return json.loads(SNAPSHOT.read_text())


def restore_snapshot(chip: Chip, delete: bool = False) -> list[int]:
    snap = load_snapshot()
    restored = []
    for key, row in snap.get("channels", {}).items():
        index = int(key)
        points = row.get("points") or []
        if len(points) == 5:
            curve = tuple((int(p["temp_mc"]), int(p["pwm"])) for p in points)
            chip.write_curve(index, curve)
        enable = row.get("enable")
        if enable not in (None, ENABLE_OFF):
            chip.set_enable(index, int(enable))
        restored.append(index)
    if delete:
        SNAPSHOT.unlink(missing_ok=True)
        clear_overrides()
    return restored


def normalize_override(spec: object) -> dict:
    if not isinstance(spec, dict):
        raise FanctlError("zły wpis override")
    mode = spec.get("mode")
    if mode == "curve":
        return {"mode": "curve"}
    if mode == "manual":
        percent = spec.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise FanctlError("ręczny override chce procent 20-100")
        percent_to_pwm(percent)
        return {"mode": "manual", "percent": percent}
    raise FanctlError(f"nieznany tryb override: {mode}")


def load_overrides() -> dict:
    if not OVERRIDES.exists():
        return {}
    try:
        raw = json.loads(OVERRIDES.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FanctlError(f"zepsuty {OVERRIDES}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FanctlError(f"zepsuty {OVERRIDES}")
    return {str(key): normalize_override(value) for key, value in raw.items()}


def clear_overrides() -> None:
    if os.geteuid() != 0:
        raise FanctlError("kasowanie override chce roota")
    OVERRIDES.unlink(missing_ok=True)


def save_override(index: int, spec: dict | None) -> None:
    if os.geteuid() != 0:
        raise FanctlError("zapis override chce roota")
    data = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    if not isinstance(data, dict):
        data = {}
    key = str(index)
    if spec is None:
        data.pop(key, None)
    else:
        data[key] = normalize_override(spec)
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        OVERRIDES.unlink(missing_ok=True)
        return
    tmp = OVERRIDES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(OVERRIDES)
    OVERRIDES.chmod(0o644)


def _refuse_idle(chip: Chip, index: int, force: bool) -> Channel:
    channel = chip.read_channel(index)
    if not channel.spinning and not force:
        raise FanctlError(f"fan{index} ma 0 rpm — nie ruszam pustego headera")
    return channel


def hold_manual(chip: Chip, index: int, percent: int, force: bool = False) -> Channel:
    """Zostaw header na stałym PWM. Nie śledzi temperatury, dopóki nie wróci na auto."""
    _refuse_idle(chip, index, force)
    percent_to_pwm(percent)
    save_snapshot(chip, [index])
    after = chip.set_manual(index, percent)
    save_override(index, {"mode": "manual", "percent": int(percent)})
    return after


def hold_curve(chip: Chip, index: int, force: bool = False) -> Channel:
    """Załóż cichą krzywą na jeden header i zapamiętaj, żeby wróciła po reboocie."""
    channel = _refuse_idle(chip, index, force)
    if not curve_matches(channel):
        save_snapshot(chip, [index])
        try:
            chip.write_curve(index, BOARD_CURVE)
        except Exception:
            row = load_snapshot().get("channels", {}).get(str(index))
            points = (row or {}).get("points") or []
            if len(points) == 5:
                chip.write_curve(index, tuple((int(p["temp_mc"]), int(p["pwm"])) for p in points))
            raise
    if chip.read_channel(index).enable != ENABLE_SMART_FAN_IV:
        chip.set_enable(index, ENABLE_SMART_FAN_IV)
    save_override(index, {"mode": "curve"})
    return chip.read_channel(index)


def release_to_auto(chip: Chip, index: int) -> Channel:
    """Oddaj header chipowi. Krzywej nie rusza, kasuje tylko ręczny zapis."""
    chip.restore_enable(index, ENABLE_SMART_FAN_IV)
    save_override(index, None)
    return chip.read_channel(index)


def apply_saved_overrides(chip: Chip) -> list[str]:
    """Po reboocie BIOS wraca. To zakłada z powrotem to, co użytkownik zostawił."""
    data = load_overrides()
    if not data:
        return []
    save_snapshot(chip, [int(key) for key in data])
    notes = []
    for key, spec in sorted(data.items(), key=lambda item: int(item[0])):
        index = int(key)
        channel = chip.read_channel(index)
        if not channel.spinning:
            notes.append(f"fan{index}: pominięty, 0 rpm")
            continue
        if spec["mode"] == "manual":
            chip.set_manual(index, spec["percent"])
            notes.append(f"fan{index}: ręcznie {spec['percent']}%")
        else:
            if not curve_matches(channel):
                chip.write_curve(index, BOARD_CURVE)
            if chip.read_channel(index).enable != ENABLE_SMART_FAN_IV:
                chip.set_enable(index, ENABLE_SMART_FAN_IV)
            notes.append(f"fan{index}: krzywa")
    return notes


def pegged_spinning(chip: Chip) -> list[int]:
    return [c.index for c in (chip.read_channel(i) for i in chip.channels()) if c.pegged and c.spinning]


def curve_matches(channel: Channel, curve: tuple[tuple[int, int], ...] = BOARD_CURVE) -> bool:
    return [(p.temp_mc, p.pwm) for p in channel.points] == list(curve)


def _pwm_dropped(before: int | None, after: int | None) -> bool:
    return before is not None and after is not None and after < before - 15


def _wait_rpm(chip: Chip, index: int, before_rpm: int | None, timeout: float = 8) -> Channel:
    """Tachometr na NCT6799 późni kilka sekund za rejestrem PWM."""
    deadline = time.time() + timeout
    last = chip.read_channel(index)
    while time.time() < deadline:
        if before_rpm and last.rpm is not None and last.rpm < before_rpm * 0.85:
            return last
        time.sleep(1)
        last = chip.read_channel(index)
    return last


def _wait_pwm(chip: Chip, index: int, before_pwm: int | None, timeout: float = 6) -> Channel:
    deadline = time.time() + timeout
    last = chip.read_channel(index)
    while time.time() < deadline:
        if _pwm_dropped(before_pwm, last.pwm):
            return last
        time.sleep(1)
        last = chip.read_channel(index)
    return last


def _nudge_reload(chip: Chip, index: int) -> None:
    """Niektóre płyty ładują nowe punkty dopiero po zejściu z trybu auto i powrocie."""
    chip.set_manual(index, 40)
    time.sleep(0.3)
    chip.set_enable(index, ENABLE_SMART_FAN_IV)


def apply_board_curve(chip: Chip, indexes: list[int]) -> dict:
    """Podmień krzywą i zostaw Smart Fan IV. Chip sam reguluje, nawet jak proces zniknie."""
    if not indexes:
        return {"applied": [], "channels": [], "note": "nic do zrobienia"}
    todo = [i for i in indexes if not curve_matches(chip.read_channel(i))]
    if not todo:
        return {
            "applied": [],
            "channels": [chip.read_channel(i).as_dict() for i in indexes],
            "note": "krzywa już założona",
        }
    save_snapshot(chip, todo)
    changed = []
    try:
        for index in todo:
            before = chip.read_channel(index)
            chip.write_curve(index, BOARD_CURVE)
            if chip.read_channel(index).enable != ENABLE_SMART_FAN_IV:
                chip.set_enable(index, ENABLE_SMART_FAN_IV)
            after = _wait_pwm(chip, index, before.pwm)
            if before.pegged and not _pwm_dropped(before.pwm, after.pwm):
                _nudge_reload(chip, index)
                after = _wait_pwm(chip, index, before.pwm)
            if before.pegged and not _pwm_dropped(before.pwm, after.pwm):
                raise FanctlError(
                    f"fan{index}: krzywa zapisana, ale PWM został {after.pwm}. Oddaję BIOS."
                )
            changed.append(_wait_rpm(chip, index, before.rpm))
    except Exception:
        restore_snapshot(chip, delete=False)
        raise
    return {
        "applied": [c.index for c in changed],
        "channels": [c.as_dict() for c in changed],
    }


def identify_fan(chip: Chip, index: int, percent: int = 40, seconds: float = 5) -> dict:
    """Krótki zjazd PWM i powrót do poprzedniego trybu. Krzywej nie rusza."""
    before = chip.read_channel(index)
    if not before.spinning:
        raise FanctlError(f"fan{index} ma 0 rpm")
    mode = before.enable if before.enable not in (None, ENABLE_OFF) else ENABLE_SMART_FAN_IV
    try:
        chip.set_manual(index, percent)
        time.sleep(seconds)
        during = chip.read_channel(index)
    finally:
        if mode == ENABLE_MANUAL and before.pwm is not None:
            chip.set_manual(index, max(pwm_percent(before.pwm) or 20, 20))
        else:
            chip.restore_enable(index, mode)
    time.sleep(2)
    after = chip.read_channel(index)
    dropped = (
        during.rpm is not None
        and before.rpm is not None
        and during.rpm < before.rpm * 0.85
    )
    return {
        "fan": index,
        "before_rpm": before.rpm,
        "during_rpm": during.rpm,
        "after_rpm": after.rpm,
        "during_pwm": during.pwm,
        "after_enable": after.enable,
        "obeys": dropped,
    }


def format_point(point: Point) -> str:
    return f"{point.temp_mc / 1000:.0f}°C/{pwm_percent(point.pwm)}%"


def format_status(data: dict) -> str:
    lines = [f"{data['chip']}  {data['path']}"]
    k10 = data.get("k10temp") or {}
    if k10:
        lines.append("CPU: " + "  ".join(f"{k} {v}°C" for k, v in k10.items()))
    interesting = [t for t in data.get("temps", []) if t["name"] in {"SYSTIN", "CPUTIN", "TSI0_TEMP"} and t["c"] is not None]
    if interesting:
        lines.append("płyta: " + "  ".join(f"{t['name']} {t['c']}°C" for t in interesting))
    lines.append("")
    for row in data["channels"]:
        mark = ""
        if row["pegged"] and row["spinning"]:
            mark = "  ← pełna krzywa, to hałasuje"
        elif row["pegged"]:
            mark = "  (pełna krzywa, brak obrotów — header pusty)"
        rpm = row["rpm"] if row["rpm"] is not None else "-"
        pct = row["percent"] if row["percent"] is not None else "-"
        held = (data.get("overrides") or {}).get(str(row["fan"]))
        if held and held.get("mode") == "manual":
            mark += f"  zapisane: ręcznie {held['percent']}% — nie śledzi temperatury"
        elif held and held.get("mode") == "curve":
            mark += "  zapisane: krzywa wraca po reboocie"
        lines.append(
            f"fan{row['fan']}: {rpm} rpm, pwm {row['pwm']} ({pct}%), {row['mode']}{mark}"
        )
        if row["points"]:
            lines.append("  " + " → ".join(format_point(Point(p["temp_mc"], p["pwm"])) for p in row["points"]))
    if data.get("snapshot"):
        lines.append("")
        lines.append(f"migawka BIOS: {data['snapshot']}")
    return "\n".join(lines)
