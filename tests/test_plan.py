import unittest

from fanctl.hwmon import percent_to_pwm, plan_monotonic


def _replay(current, writes):
    cur = list(current)
    seen = [cur.copy()]
    for index, value in writes:
        if index > 0 and value < cur[index - 1]:
            raise AssertionError(f"lewy sąsiad: {index}={value} przy {cur}")
        if index + 1 < len(cur) and value > cur[index + 1]:
            raise AssertionError(f"prawy sąsiad: {index}={value} przy {cur}")
        cur[index] = value
        seen.append(cur.copy())
    return cur, seen


class PlanTests(unittest.TestCase):
    def test_fan6_temps_down_and_back(self):
        bios = [0, 100_000, 100_000, 100_000, 100_000]
        quiet = [30_000, 50_000, 70_000, 75_000, 125_000]
        writes = plan_monotonic(bios, quiet)
        self.assertEqual(_replay(bios, writes)[0], quiet)
        back = plan_monotonic(quiet, bios)
        self.assertEqual(_replay(quiet, back)[0], bios)

    def test_pwm_pegged_to_board(self):
        current = [255, 255, 255, 255, 255]
        target = [51, 102, 178, 255, 255]
        writes = plan_monotonic(current, target)
        self.assertEqual(_replay(current, writes)[0], target)

    def test_already_there(self):
        current = [51, 102, 178, 255, 255]
        self.assertEqual(plan_monotonic(current, current), [])

    def test_rejects_falling_target(self):
        with self.assertRaises(ValueError):
            plan_monotonic([0, 0, 0], [10, 5, 20])

    def test_floor(self):
        with self.assertRaises(Exception):
            percent_to_pwm(10)
        self.assertEqual(percent_to_pwm(40), 102)

    def test_override_modes(self):
        from fanctl.hwmon import FanctlError, normalize_override

        self.assertEqual(normalize_override({"mode": "curve", "percent": 99}), {"mode": "curve"})
        self.assertEqual(normalize_override({"mode": "manual", "percent": 40}), {"mode": "manual", "percent": 40})
        with self.assertRaises(FanctlError):
            normalize_override({"mode": "manual", "percent": 10})
        with self.assertRaises(FanctlError):
            normalize_override({"mode": "off"})


if __name__ == "__main__":
    unittest.main()
