"""CLI fanctl. Zapis do chipu tylko jako root."""

from __future__ import annotations

import argparse
import json
import sys
import time

from fanctl import __version__
from fanctl.hwmon import (
    Chip,
    FanctlError,
    apply_board_curve,
    apply_saved_overrides,
    format_status,
    hold_curve,
    hold_manual,
    identify_fan,
    pegged_spinning,
    release_to_auto,
    restore_snapshot,
)
from fanctl.web import PORT, serve


def _die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def cmd_status(args: argparse.Namespace) -> int:
    data = Chip().status()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(format_status(data))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    chip = Chip()
    if args.fan:
        indexes = args.fan
        for index in indexes:
            channel = chip.read_channel(index)
            if not channel.spinning and not args.force:
                _die(f"fan{index} ma 0 rpm — nie ruszam pustego headera bez --force")
    else:
        indexes = pegged_spinning(chip)
    if args.dry_run:
        print("dry-run, nic nie zapisuję")
        for index in indexes:
            row = chip.read_channel(index)
            print(f"fan{index}: {row.rpm} rpm, pwm {row.pwm}, pegged={row.pegged}")
        if not indexes:
            print("brak przybitych, kręcących się headerów")
        return 0
    before = {i: chip.read_channel(i) for i in indexes}
    result = apply_board_curve(chip, indexes)
    if result.get("note"):
        print(result["note"])
    if result["applied"]:
        print("zapisane: " + ", ".join(f"fan{i}" for i in result["applied"]))
    for row in result["channels"]:
        old = before[row["fan"]]
        print(
            f"fan{row['fan']}: {old.rpm} rpm / pwm {old.pwm}  →  {row['rpm']} rpm / pwm {row['pwm']}  ({row['mode']})"
        )
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    chip = Chip()
    if args.auto:
        row = release_to_auto(chip, args.fan)
        print(
            f"fan{args.fan}: Smart Fan IV, pwm {row.pwm}, {row.rpm} rpm. "
            "Temperatura znowu steruje."
        )
        return 0
    before = chip.read_channel(args.fan)
    hold_manual(chip, args.fan, args.percent, force=args.force)
    time.sleep(3)
    after = chip.read_channel(args.fan)
    print(
        f"fan{args.fan}: {before.rpm} rpm / pwm {before.pwm}  →  "
        f"{after.rpm} rpm / pwm {after.pwm} ({args.percent}%)"
    )
    print("Ręczny procent NIE rośnie z temperaturą. Zostaje po reboocie.")
    print(f"Wróć na krzywą: sudo bin/fanctl set {args.fan} --auto")
    return 0


def cmd_curve(args: argparse.Namespace) -> int:
    chip = Chip()
    before = chip.read_channel(args.fan)
    after = hold_curve(chip, args.fan, force=args.force)
    print(
        f"fan{args.fan}: {before.rpm} rpm / pwm {before.pwm}  →  "
        f"{after.rpm} rpm / pwm {after.pwm}"
    )
    print("Krzywa śledzi temperaturę i wraca po reboocie.")
    return 0


def cmd_boot(_args: argparse.Namespace) -> int:
    chip = Chip()
    indexes = pegged_spinning(chip)
    result = apply_board_curve(chip, indexes)
    if result.get("note"):
        print(result["note"])
    if result["applied"]:
        print("przybite: " + ", ".join(f"fan{i}" for i in result["applied"]))
    notes = apply_saved_overrides(chip)
    if notes:
        print("zapisane: " + "; ".join(notes))
    else:
        print("brak ręcznych zapisów")
    return 0


def cmd_release(_args: argparse.Namespace) -> int:
    chip = Chip()
    restored = restore_snapshot(chip, delete=True)
    time.sleep(2)
    print("przywrócone: " + ", ".join(f"fan{i}" for i in restored))
    for index in restored:
        row = chip.read_channel(index)
        print(f"fan{index}: {row.rpm} rpm, pwm {row.pwm}, {row.mode}")
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    chip = Chip()
    if not args.force:
        channel = chip.read_channel(args.fan)
        if not channel.spinning:
            _die(f"fan{args.fan} stoi. Nie ma czego identyfikować.")
    result = identify_fan(chip, args.fan, args.percent, args.seconds)
    print(
        f"fan{result['fan']} przed {result['before_rpm']} rpm  →  "
        f"w trakcie {result['during_rpm']} rpm (pwm {result['during_pwm']})  →  "
        f"po {result['after_rpm']} rpm, enable {result['after_enable']}"
    )
    print("PWM SŁUCHA" if result["obeys"] else "RPM NIE SPADŁO")
    return 0 if result["obeys"] else 2


def cmd_serve(args: argparse.Namespace) -> int:
    serve(args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fanctl",
        description="Ciche krzywe Smart Fan IV dla NCT67xx. Chip reguluje sam, bez demona.",
    )
    parser.add_argument("--version", action="version", version=f"fanctl {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="pokaż wentylatory i krzywe")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    apply = sub.add_parser("apply", help="wycisz headery z krzywą przybitą do 100%")
    apply.add_argument("--fan", type=int, action="append", help="konkretny header, można powtórzyć")
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--force", action="store_true", help="rusz też header z 0 rpm")
    apply.set_defaults(func=cmd_apply)

    setp = sub.add_parser("set", help="zostaw jeden header na stałym PWM")
    setp.add_argument("fan", type=int)
    mode = setp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--percent", type=int, help="20-100, nie śledzi temperatury")
    mode.add_argument("--auto", action="store_true", help="oddaj krzywą chipowi")
    setp.add_argument("--force", action="store_true", help="rusz też header z 0 rpm")
    setp.set_defaults(func=cmd_set)

    curve = sub.add_parser("curve", help="załóż cichą krzywą na jeden header")
    curve.add_argument("fan", type=int)
    curve.add_argument("--force", action="store_true")
    curve.set_defaults(func=cmd_curve)

    boot = sub.add_parser("boot", help="po restarcie: przybite krzywe i zapisane ustawienia")
    boot.set_defaults(func=cmd_boot)

    release = sub.add_parser("release", help="odtwórz migawkę BIOS")
    release.set_defaults(func=cmd_release)

    identify = sub.add_parser("identify", help="na chwilę zwolnij jeden header i oddaj BIOS")
    identify.add_argument("fan", type=int)
    identify.add_argument("--percent", type=int, default=40)
    identify.add_argument("--seconds", type=int, default=5)
    identify.add_argument("--force", action="store_true")
    identify.set_defaults(func=cmd_identify)

    web = sub.add_parser("serve", help="panel na 127.0.0.1")
    web.add_argument("--port", type=int, default=PORT)
    web.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except FanctlError as exc:
        _die(str(exc))


if __name__ == "__main__":
    main()
