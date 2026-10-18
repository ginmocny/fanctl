# fanctl

Quiet Smart Fan IV curves for Nuvoton NCT67xx on Linux. The chip tracks temperature itself. No daemon that can die and leave the fans stuck at a low PWM.

Written for the ASUS PRIME X670E-PRO WIFI (NCT6799, `nct6775` driver). On other boards with the same chip, `status` tells you whether there is anything to touch.

## What was wrong

The BIOS held fan6 (and the empty fan7 header) on a `0°C / 100%` curve across the whole scale. The other headers already had a normal curve. That is the noise on a cold CPU. `apply` only rewrites spinning headers pegged at 100%, using the curve this board already runs on fan1/fan2:

| °C | PWM |
|----|-----|
| 30 | 20% |
| 50 | 40% |
| 70 | 70% |
| 75 | 100% |
| 125 | 100% |

`temp_sel` is left alone. The tool does not guess which sensor is the CPU. The mode stays Smart Fan IV (`pwm_enable=5`). `enable=0` is never written — on this driver that means full speed.

The original curve is saved to `/var/lib/fanctl/bios.json`. `release` restores it. A reboot returns to the BIOS curve until the unit in `deploy/` is enabled.

## Usage

```bash
bin/fanctl status
sudo bin/fanctl apply --dry-run
sudo bin/fanctl apply
sudo bin/fanctl curve 5
sudo bin/fanctl set 5 --percent 40
sudo bin/fanctl set 5 --auto
sudo bin/fanctl release
sudo bin/fanctl identify 6
sudo bin/fanctl serve
```

`set` holds a fixed percent. The chip does not raise RPM with temperature while that hold is on. `set --auto` hands the header back to the curve already in the registers. `curve` installs the quiet curve on the named header, not only on headers pegged at 100%. Both holds come back after reboot. An empty header (0 rpm) is refused unless you pass `--force`.

The panel listens only on `http://127.0.0.1:8765`. Each spinning header has a percent field plus manual, curve, and auto. A POST without the `X-Fanctl: 1` header is rejected.

The manual PWM floor is 20%. An empty header (0 rpm) is not touched without `--force`. Kernel 7 returns `EBUSY` if you write `pwmN` before `pwmN_enable=1`. The tachometer lags the PWM register by a few seconds — `apply` waits for the RPM drop before it prints the result.

## After reboot

The module and the curve do not survive a restart on their own. Once a live test looks right:

```bash
echo nct6775 | sudo tee /etc/modules-load.d/nct6775.conf
# Set ExecStart in deploy/fanctl.service to the absolute path of bin/fanctl.
sudo cp deploy/fanctl.service /etc/systemd/system/fanctl.service
sudo systemctl daemon-reload
sudo systemctl enable --now fanctl.service
```

The unit runs `boot`: pegged curves plus saved holds from `/var/lib/fanctl/overrides.json`. It starts only when the snapshot already exists. Without a snapshot there is nothing to compare against.
