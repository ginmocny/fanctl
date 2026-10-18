"""Lokalny panel. Tylko 127.0.0.1. POST wymaga nagłówka, którego obca strona nie ustawi."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fanctl.hwmon import (
    BOARD_CURVE,
    Chip,
    FanctlError,
    apply_board_curve,
    hold_curve,
    hold_manual,
    identify_fan,
    pegged_spinning,
    release_to_auto,
    restore_snapshot,
)

HOST = "127.0.0.1"
PORT = 8765
HEADER = "X-Fanctl"


PAGE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<title>fanctl</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 24px; font: 15px/1.4 ui-sans-serif, sans-serif; background: #12140f; color: #e7e1d1; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  p.sub { margin: 0 0 18px; color: #a39b86; }
  table { border-collapse: collapse; width: 100%; max-width: 920px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #2c2a22; vertical-align: top; }
  th { color: #a39b86; font-weight: 600; }
  .bad { color: #e15a3a; }
  .ok { color: #b6c46a; }
  button, input { font: inherit; }
  button { background: #2a2e22; color: #e7e1d1; border: 1px solid #4d5340; padding: 6px 12px; cursor: pointer; }
  button.danger { border-color: #8a3d2c; }
  button:disabled { opacity: 0.5; cursor: wait; }
  input { width: 4.2em; background: #1b1e16; color: #e7e1d1; border: 1px solid #4d5340; padding: 4px 6px; }
  #log { max-width: 920px; white-space: pre-wrap; color: #a39b86; min-height: 1.4em; }
  .rowbtns { display: flex; gap: 6px; align-items: center; }
</style>
</head>
<body>
<h1>fanctl</h1>
<p class="sub" id="head">czytam chip…</p>
<p class="sub">Ręczny procent zostaje i nie rośnie z temperaturą. Krzywa śledzi temperaturę. Pusty header jest zablokowany.</p>
<p>
  <button id="quiet">Wycisz pełne krzywe</button>
  <button id="release" class="danger">Przywróć BIOS</button>
</p>
<table>
  <thead><tr><th>header</th><th>rpm</th><th>pwm</th><th>tryb</th><th>krzywa</th><th></th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<p id="log"></p>
<script>
const hdr = { "X-Fanctl": "1", "Content-Type": "application/json" };
function curve(points) {
  return points.map(p => Math.round(p.temp_mc / 1000) + "°C/" + Math.round(p.pwm * 100 / 255) + "%").join(" → ");
}
function paint(data) {
  document.getElementById("head").textContent =
    data.chip + " · " + (data.k10temp.Tctl != null ? ("Tctl " + data.k10temp.Tctl + "°C") : "");
  const tb = document.getElementById("rows");
  tb.replaceChildren();
  for (const row of data.channels) {
    const tr = document.createElement("tr");
    const warn = row.pegged && row.spinning;
    const held = row.override;
    const mode = row.mode + (warn ? " · pełna" : "") +
      (held && held.mode === "manual" ? " · zapisane " + held.percent + "%" : "");
    tr.innerHTML = "<td>fan" + row.fan + "</td><td>" + (row.rpm ?? "–") + "</td><td>" +
      (row.percent ?? "–") + "%</td><td class='" + (row.enable === 1 ? "bad" : "ok") + "'>" + mode +
      "</td><td>" + curve(row.points) + "</td>";
    const td = document.createElement("td");
    td.className = "rowbtns";
    const input = document.createElement("input");
    input.type = "number"; input.min = "20"; input.max = "100"; input.dataset.fan = String(row.fan);
    const old = document.querySelector("#rows input[data-fan='" + row.fan + "']");
    input.value = old ? old.value : String(row.percent || 40);
    const manual = document.createElement("button");
    manual.textContent = "ręcznie";
    manual.onclick = () => act("/api/manual", { fan: row.fan, percent: Number(input.value) });
    const curveBtn = document.createElement("button");
    curveBtn.textContent = "krzywa";
    curveBtn.onclick = () => act("/api/curve", { fan: row.fan });
    const back = document.createElement("button");
    back.textContent = "auto";
    back.onclick = () => act("/api/auto", { fan: row.fan });
    if (!row.spinning) {
      input.disabled = manual.disabled = curveBtn.disabled = back.disabled = true;
    }
    td.append(input, manual, curveBtn, back);
    tr.append(td);
    tb.append(tr);
  }
}
let busy = false;
async function refresh() {
  if (busy) return;
  const res = await fetch("/api/status");
  paint(await res.json());
}
async function act(url, body) {
  if (busy) return;
  busy = true;
  document.getElementById("log").textContent = "piszę do chipu…";
  try {
    const res = await fetch(url, { method: "POST", headers: hdr, body: JSON.stringify(body || {}) });
    const data = await res.json();
    document.getElementById("log").textContent = data.message || JSON.stringify(data);
  } finally {
    busy = false;
  }
  await refresh();
}
document.getElementById("quiet").onclick = () => act("/api/apply", {});
document.getElementById("release").onclick = () => act("/api/release", {});
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    raw = json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _guard(self) -> bool:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            _json(self, 403, {"ok": False, "message": "tylko localhost"})
            return False
        if self.headers.get(HEADER) != "1":
            _json(self, 403, {"ok": False, "message": "brak nagłówka X-Fanctl"})
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if length > 4096:
            raise FanctlError("za duży request")
        return json.loads(self.rfile.read(length).decode() or "{}")

    def do_GET(self) -> None:  # noqa: N802
        chip = Chip()
        if self.path in {"/", "/index.html"}:
            raw = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path == "/api/status":
            _json(self, 200, chip.status())
            return
        _json(self, 404, {"ok": False, "message": "nie ma takiej ścieżki"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        chip = Chip()
        try:
            body = self._body()
            if self.path == "/api/apply":
                indexes = pegged_spinning(chip)
                result = apply_board_curve(chip, indexes)
                fans = ", ".join(f"fan{i}" for i in result["applied"])
                message = result.get("note") or f"cicha krzywa na {fans}"
                _json(self, 200, {"ok": True, "message": message, "result": result})
                return
            if self.path == "/api/release":
                restored = restore_snapshot(chip, delete=True)
                fans = ", ".join(f"fan{i}" for i in restored)
                _json(self, 200, {"ok": True, "message": f"BIOS wrócił na {fans}"})
                return
            if self.path == "/api/identify":
                result = identify_fan(chip, int(body["fan"]), int(body.get("percent") or 40), 5)
                word = "spadło" if result["obeys"] else "nie spadło"
                _json(
                    self,
                    200,
                    {
                        "ok": result["obeys"],
                        "message": (
                            f"fan{result['fan']}: {result['before_rpm']} → {result['during_rpm']} rpm, {word}. "
                            f"Tryb z powrotem {result['after_enable']}."
                        ),
                        "result": result,
                    },
                )
                return
            if self.path == "/api/auto":
                fan = int(body["fan"])
                row = release_to_auto(chip, fan)
                _json(self, 200, {"ok": True, "message": f"fan{fan} z powrotem na krzywej, pwm {row.pwm}"})
                return
            if self.path == "/api/manual":
                fan = int(body["fan"])
                percent = int(body["percent"])
                row = hold_manual(chip, fan, percent)
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "message": (
                            f"fan{fan}: ręcznie {percent}% (pwm {row.pwm}). "
                            "Nie rośnie z temperaturą, dopóki nie wciśniesz auto."
                        ),
                    },
                )
                return
            if self.path == "/api/curve":
                fan = int(body["fan"])
                row = hold_curve(chip, fan)
                _json(
                    self,
                    200,
                    {"ok": True, "message": f"fan{fan}: cicha krzywa, pwm {row.pwm}. Temperatura steruje."},
                )
                return
            _json(self, 404, {"ok": False, "message": "nie ma takiej ścieżki"})
        except (FanctlError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _json(self, 400, {"ok": False, "message": str(exc)})


def serve(port: int = PORT) -> None:
    server = ThreadingHTTPServer((HOST, port), Handler)
    print(f"fanctl na http://{HOST}:{port}  (tylko ta maszyna)")
    print(f"krzywa docelowa: {[(t // 1000, round(p * 100 / 255)) for t, p in BOARD_CURVE]}")
    server.serve_forever()
