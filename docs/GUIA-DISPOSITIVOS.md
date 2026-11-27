# Guía para dispositivos: cómo enviar lecturas a la API

Esta guía es para quien programa el firmware o el software de la estación (ESP32, Raspberry Pi…). Explica qué enviar, cómo, y qué hacer con cada respuesta. No hace falta saber nada de Google Cloud.

**En resumen:** cada 5 minutos, la estación hace un `POST` HTTPS con las medias de temperatura, humedad y radiación, firmado con su token. Si la respuesta es `202`, lo borra de su cola. Si no hay respuesta o es `503`, lo guarda y lo reintenta más tarde.

---

## 1. Lo que necesitas antes de empezar

Pídeselo al administrador de la API:

| Dato | Ejemplo | Para qué |
|---|---|---|
| **URL de la API** | `https://agroclima-ingest-api-…run.app` | Dirección a la que se envía |
| **`gatewayId`** | `gateway-station-with-pi3` | Identifica la estación (el equipo que envía) |
| **Token** | `_3RT…Qe8` (43 caracteres) | Contraseña de esa estación. **Es secreto** |
| **`sensorId`** de cada sensor | `pi3-air01` | Identifica cada sensor físico |

El `gatewayId` y los `sensorId` tienen que estar **dados de alta en el registro de sensores** (lo hace el administrador; ver el [anexo](#anexo-para-el-administrador-dar-de-alta-una-estación)). Ahora mismo la API acepta sensores no registrados y solo lo anota en los logs, pero más adelante los rechazará.

**El token no se pone en el código** ni se sube a git: el repositorio es público. Guárdalo en un archivo de configuración o variable de entorno del dispositivo. Si se pierde o se filtra, el administrador genera otro.

---

## 2. La petición

```
POST <URL de la API>/v1/readings
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "schemaVersion": "1.0",
  "gatewayId": "gateway-station-with-pi3",
  "sentAt": "2026-11-27T09:05:02Z",
  "readings": [
    { "sensorId": "pi3-air01", "property": "air_temperature",       "value": 22.6, "unit": "Cel",  "ts": "2026-11-27T09:05:00Z", "aggregation": "mean", "period": "PT5M" },
    { "sensorId": "pi3-air01", "property": "air_relative_humidity", "value": 78.4, "unit": "%",    "ts": "2026-11-27T09:05:00Z", "aggregation": "mean", "period": "PT5M" },
    { "sensorId": "pi3-air01", "property": "global_radiation",      "value": 236,  "unit": "W/m2", "ts": "2026-11-27T09:05:00Z", "aggregation": "mean", "period": "PT5M" }
  ]
}
```

Respuesta correcta: código **`202`** y cuerpo `{"accepted": 3}`. En ese momento las lecturas ya están guardadas en la base de datos.

Solo HTTPS. Las peticiones por HTTP no funcionan.

### Campos del envío

| Campo | Obligatorio | Formato | Notas |
|---|---|---|---|
| `schemaVersion` | Sí | `"1.0"` | Siempre este valor |
| `gatewayId` | Sí | texto, 1–128 caracteres | Debe ser el del token; si no, `403` |
| `sentAt` | Sí | fecha ISO 8601 con zona (`…Z`) | Momento en que se envía |
| `readings` | Sí | lista de 1 a 1000 lecturas | Puedes juntar varios periodos en un envío |

### Campos de cada lectura

| Campo | Obligatorio | Formato | Notas |
|---|---|---|---|
| `sensorId` | Sí | texto, 1–128 caracteres | Fijo para cada sensor físico. No lo reutilices para otro sensor |
| `property` | Sí | ver tabla de propiedades | Exactamente como está escrito |
| `value` | Sí | número | No `null`, no `NaN`, no texto |
| `ts` | Sí | fecha ISO 8601 con zona | Momento de la medida (ver [sección 4](#4-la-hora)) |
| `unit` | No | ver tabla | Si lo envías, tiene que ser exactamente la unidad de la tabla. Si no, se asume esa |
| `aggregation` | No | `mean`, `min`, `max` o `last` | Qué representa `value` en el periodo. Recomendado: `mean` |
| `period` | No | duración ISO 8601 | `PT5M` = 5 minutos, `PT1M` = 1 minuto, `PT1H` = 1 hora |

**No se admiten campos extra.** Un campo con nombre mal escrito (`sensorID`, `timestamp`…) o que no está en la tabla hace que se rechace el envío entero con `422`.

---

## 3. Propiedades y rangos

Solo hay tres propiedades. Cualquier otra se rechaza.

| `property` | `unit` | Rango válido | Qué es |
|---|---|---|---|
| `air_temperature` | `Cel` | -50 … 70 | Temperatura del aire en °C |
| `air_relative_humidity` | `%` | 0 … 100 | Humedad relativa del aire |
| `global_radiation` | `W/m2` | 0 … 1500 | Radiación solar global |

Las unidades se escriben **exactamente** así (`Cel`, no `°C` ni `C`; `W/m2`, no `W/m²`).

**Un valor fuera de rango rechaza el envío entero.** Los sensores reales a veces dan valores un poco fuera:

- La radiación de noche puede salir ligeramente **negativa** (-0,3 W/m²) por el offset del piranómetro. Envía `0`.
- La humedad puede pasar de **100 %** con niebla o condensación. Envía `100`.
- Si una lectura falla (el sensor devuelve `NaN`, `None`, `-999`…), **no la envíes**. Envía las otras dos.

---

## 4. La hora

- **Todas las fechas en UTC con `Z` al final**: `2026-11-27T09:05:00Z`. También vale con zona (`2026-11-27T10:05:00+01:00`), pero no sin zona: `2026-11-27T09:05:00` se rechaza.
- **El reloj tiene que estar sincronizado por NTP** antes de enviar nada. Una Raspberry Pi 3 o un ESP32 no tienen reloj con pila: si arrancan sin red, creen que es 1970 o la última fecha guardada. Si no hay hora fiable, guarda las medidas sin enviarlas o descártalas, pero no envíes horas falsas.
  - Raspberry Pi: `timedatectl` debe mostrar `System clock synchronized: yes`.
  - ESP32: `configTime(0, 0, "pool.ntp.org")` y esperar a que `time(nullptr)` sea una fecha actual.
- **Convención de `ts` para medias**: `ts` es el **final** del periodo. La media de 09:00:00 a 09:04:59 se envía con `"ts": "2026-11-27T09:05:00Z"` y `"period": "PT5M"`. Alinea los periodos a múltiplos exactos (09:00, 09:05, 09:10…), no a la hora en que arrancó el equipo.

### Una lectura por sensor, propiedad y hora

La API guarda **una sola lectura** por cada combinación de `sensorId` + `property` + `ts`. Si llega otra con los mismos tres valores, se ignora y se conserva la primera. Esto permite reenviar sin miedo a duplicar datos (ver [sección 6](#6-cola-y-reintentos)), pero tiene dos consecuencias:

- No envíes la media y el máximo del mismo periodo con el mismo `ts`: solo se guardaría uno. Envía solo la media.
- Si corriges un valor, reenviarlo con el mismo `ts` no lo cambia. Avisa al administrador.

---

## 5. Respuestas y qué hacer con cada una

| Código | Qué significa | Qué debe hacer el dispositivo |
|---|---|---|
| **`202`** | Guardado | Borrar ese envío de la cola |
| **`401`** | Falta el token o no es válido | **No reintentar.** Revisar configuración. Avisar al administrador |
| **`403`** | El token es de otra estación (no coincide con `gatewayId`) | **No reintentar.** Revisar `gatewayId` |
| **`422`** | El envío tiene algo mal (el cuerpo explica qué) | **No reintentar el mismo envío.** Guardarlo aparte o registrar el error, y corregir el firmware |
| **`503`**, otro `5xx`, timeout o sin conexión | Problema temporal (red, base de datos) | **Guardar y reintentar** más tarde |

Un envío se acepta o se rechaza **entero**: si una sola lectura está mal, no se guarda ninguna de ese envío.

Ejemplo de respuesta `422`: dice qué lectura (`readings`, posición `1`) y por qué.

```json
{"detail": [{"type": "value_error", "loc": ["body", "readings", 1],
             "msg": "Value error, 'air_relative_humidity'=130.0 outside [0, 100]", "...": "..."}]}
```

Y cuando el registro de sensores esté en modo estricto, un sensor no asignado a la estación:

```json
{"detail": {"registry": ["sensor 'pi3-air01' is not assigned to gateway 'gateway-station-with-pi3' at 2026-11-27T09:05:00+00:00"]}}
```

---

## 6. Cola y reintentos

Para no perder datos cuando falle la red o la API:

1. **Guarda cada periodo en una cola persistente** (archivo en la tarjeta SD, LittleFS en el ESP32) antes de intentar enviarlo.
2. **Envía lo más antiguo primero.** Si hay mucho acumulado, junta varios periodos en un envío (hasta 1000 lecturas, unos 330 periodos de 3 lecturas).
3. **Borra de la cola solo con `202`.**
4. **Si falla por un problema temporal, espera y reintenta.** Lo más sencillo es reintentar en el siguiente ciclo de 5 minutos. Si reintentas antes, espera cada vez más (30 s, 1 min, 2 min… hasta 5 min).
5. **Reenviar no duplica datos.** Si no sabes si un envío llegó (por ejemplo, timeout esperando la respuesta), vuelve a enviarlo tal cual.
6. Usa un **timeout de 30 segundos** por petición.
7. Pon un **límite a la cola** (por ejemplo, 7 días). Si se llena, descarta lo más antiguo.

---

## 7. Ejemplos

### Probar con `curl`

Desde cualquier ordenador, para comprobar que URL, token y `gatewayId` son correctos:

```bash
URL="<URL de la API>"
TOKEN="<token>"

curl -s "$URL/health"
# {"status":"ok","registry":"warn"}

curl -s -w '  [%{http_code}]\n' -X POST "$URL/v1/readings" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"schemaVersion":"1.0","gatewayId":"gateway-station-with-pi3","sentAt":"2026-11-27T09:05:02Z",
       "readings":[{"sensorId":"pi3-air01","property":"air_temperature","value":21.5,"unit":"Cel",
                    "ts":"2026-11-27T09:05:00Z","aggregation":"mean","period":"PT5M"}]}'
# {"accepted":1}  [202]
```

La ruta de salud es `/health`. **No uses `/healthz`**: Cloud Run la reserva y responde `404`.

### Raspberry Pi (Python 3)

Necesita `requests` (`sudo apt install python3-requests`). Guarda cada periodo en un archivo de la cola y envía todo lo pendiente cada 5 minutos. Solo tienes que escribir `read_means()`.

```python
#!/usr/bin/env python3
"""Envía cada 5 minutos las medias de la estación a la API, con cola en disco."""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = os.environ["AGRO_API_URL"].rstrip("/") + "/v1/readings"
TOKEN = os.environ["AGRO_TOKEN"]                  # nunca en el código
GATEWAY_ID = os.environ.get("AGRO_GATEWAY_ID", "gateway-station-with-pi3")
SENSOR_ID = os.environ.get("AGRO_SENSOR_ID", "pi3-air01")
PERIOD_S = 300                                    # 5 minutos
QUEUE = Path(os.environ.get("AGRO_QUEUE_DIR", "/var/lib/agroclima/queue"))
QUEUE.mkdir(parents=True, exist_ok=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_means():
    """TU CÓDIGO: medias de los últimos 5 minutos. Devuelve None en la que haya fallado."""
    raise NotImplementedError
    # return temperatura_c, humedad_pct, radiacion_wm2


def clock_ok() -> bool:
    # Sin RTC, al arrancar sin red el reloj puede estar mal. Comprueba también timedatectl.
    return datetime.now(timezone.utc).year >= 2026


def enqueue(temp, rh, rad, ts: datetime) -> None:
    if rad is not None:
        rad = max(0.0, rad)                       # offset nocturno del piranómetro
    if rh is not None:
        rh = min(100.0, max(0.0, rh))             # saturación con niebla
    readings = []
    for prop, value, unit in (("air_temperature", temp, "Cel"),
                              ("air_relative_humidity", rh, "%"),
                              ("global_radiation", rad, "W/m2")):
        if value is None or value != value:       # None o NaN: lectura fallida, no se envía
            continue
        readings.append({"sensorId": SENSOR_ID, "property": prop, "value": round(value, 2),
                         "unit": unit, "ts": iso(ts), "aggregation": "mean", "period": "PT5M"})
    if readings:
        tmp = QUEUE / f".{uuid.uuid4().hex}.tmp"  # escribir y renombrar: nunca queda un archivo a medias
        tmp.write_text(json.dumps(readings))
        tmp.rename(QUEUE / f"{ts.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.json")


def flush() -> None:
    """Envía la cola, lo más antiguo primero, en envíos de hasta 1000 lecturas."""
    files = sorted(QUEUE.glob("*.json"))
    while files:
        batch, used = [], []
        for f in files:
            items = json.loads(f.read_text())
            if batch and len(batch) + len(items) > 1000:
                break
            batch += items
            used.append(f)
        body = {"schemaVersion": "1.0", "gatewayId": GATEWAY_ID,
                "sentAt": iso(datetime.now(timezone.utc)), "readings": batch}
        try:
            r = requests.post(API_URL, json=body, timeout=30,
                              headers={"Authorization": f"Bearer {TOKEN}"})
        except requests.RequestException as e:
            print("sin conexión, se reintenta en el próximo ciclo:", e)
            return
        if r.status_code == 202:
            for f in used:
                f.unlink()
        elif r.status_code == 422:
            print("envío rechazado, se aparta:", r.text)
            for f in used:
                f.rename(f.with_suffix(".rejected"))
        elif r.status_code in (401, 403):
            print("token o gatewayId incorrectos, revisa la configuración:", r.text)
            return
        else:
            print("error temporal", r.status_code, "- se reintenta en el próximo ciclo")
            return
        files = files[len(used):]


def wait_next_period() -> datetime:
    now = time.time()
    end = (int(now) // PERIOD_S + 1) * PERIOD_S   # 09:00, 09:05, 09:10…
    time.sleep(end - now)
    return datetime.fromtimestamp(end, timezone.utc)


def main() -> None:
    while True:
        ts = wait_next_period()
        if not clock_ok():
            print("reloj sin sincronizar, no se envía")
            continue
        try:
            enqueue(*read_means(), ts)
        except Exception as e:
            print("fallo leyendo sensores:", e)
        flush()


if __name__ == "__main__":
    main()
```

Para que arranque solo y con el token fuera del código, un servicio de systemd:

```ini
# /etc/systemd/system/agroclima.service
[Unit]
Description=Envío de lecturas a agroclima
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/agroclima/send.py
EnvironmentFile=/etc/agroclima.env
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
# /etc/agroclima.env  (sudo chmod 600 /etc/agroclima.env)
AGRO_API_URL=<URL de la API>
AGRO_TOKEN=<token>
AGRO_GATEWAY_ID=gateway-station-with-pi3
AGRO_SENSOR_ID=pi3-air01
```

```bash
sudo mkdir -p /var/lib/agroclima/queue
sudo systemctl daemon-reload && sudo systemctl enable --now agroclima
journalctl -u agroclima -f      # ver qué hace
```

### ESP32 (Arduino)

Usa las librerías `WiFi`, `HTTPClient` y [ArduinoJson](https://arduinojson.org/) v7. Este ejemplo envía un periodo; la cola persistente (LittleFS) se añade siguiendo la [sección 6](#6-cola-y-reintentos).

```cpp
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <math.h>

const char* API_URL    = "<URL de la API>/v1/readings";
const char* TOKEN      = "<token>";              // mejor desde NVS/Preferences que en el código
const char* GATEWAY_ID = "gateway-station-esp32-01";
const char* SENSOR_ID  = "esp32-air01";

// Certificado raíz de Google Trust Services (GTS Root R1 y GTS Root R4),
// descargados de https://pki.goog/repository/ en formato PEM.
const char* ROOT_CA = R"PEM(
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
)PEM";

String isoUtc(time_t t) {
  char buf[25];
  strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%SZ", gmtime(&t));
  return String(buf);
}

// Devuelve el código HTTP, o un número negativo si no hubo respuesta (reintentar).
int sendPeriod(float temp, float rh, float rad, time_t periodEnd) {
  if (!isnan(rad)) rad = fmaxf(0, rad);
  if (!isnan(rh))  rh  = fminf(100, fmaxf(0, rh));

  JsonDocument doc;
  doc["schemaVersion"] = "1.0";
  doc["gatewayId"] = GATEWAY_ID;
  doc["sentAt"] = isoUtc(time(nullptr));
  JsonArray readings = doc["readings"].to<JsonArray>();

  struct { const char* prop; float value; const char* unit; } items[] = {
    {"air_temperature", temp, "Cel"},
    {"air_relative_humidity", rh, "%"},
    {"global_radiation", rad, "W/m2"},
  };
  for (auto& it : items) {
    if (isnan(it.value)) continue;               // lectura fallida: no se envía
    JsonObject r = readings.add<JsonObject>();
    r["sensorId"] = SENSOR_ID;
    r["property"] = it.prop;
    r["value"] = it.value;
    r["unit"] = it.unit;
    r["ts"] = isoUtc(periodEnd);
    r["aggregation"] = "mean";
    r["period"] = "PT5M";
  }
  if (readings.size() == 0) return 202;          // nada que enviar

  String body;
  serializeJson(doc, body);

  WiFiClientSecure client;
  client.setCACert(ROOT_CA);                     // no uses setInsecure() en producción
  HTTPClient http;
  http.begin(client, API_URL);
  http.setTimeout(30000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + TOKEN);
  int code = http.POST(body);
  if (code == 422 || code == 401 || code == 403) {
    Serial.printf("Rechazado %d: %s\n", code, http.getString().c_str());
  }
  http.end();
  return code;                                   // 202 = borrar de la cola; <0 o 5xx = reintentar
}

void setup() {
  Serial.begin(115200);
  WiFi.begin("<wifi>", "<contraseña>");
  while (WiFi.status() != WL_CONNECTED) delay(500);
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  while (time(nullptr) < 1767225600) delay(500); // esperar a tener hora real (posterior a 2026-01-01)
}

void loop() {
  time_t now = time(nullptr);
  time_t periodEnd = (now / 300 + 1) * 300;      // siguiente múltiplo de 5 minutos
  // ... muestrear los sensores hasta periodEnd y calcular las medias ...
  float temp = NAN, rh = NAN, rad = NAN;         // TU CÓDIGO
  while (time(nullptr) < periodEnd) delay(1000);
  int code = sendPeriod(temp, rh, rad, periodEnd);
  Serial.printf("Envío: %d\n", code);
}
```

---

## 8. Lista de comprobación antes de instalar una estación

- [ ] El `curl` de la sección 7 con el token de la estación devuelve `202`.
- [ ] El reloj se sincroniza por NTP al arrancar y no se envía nada sin hora válida.
- [ ] Las fechas salen con `Z` y los `ts` caen en múltiplos de 5 minutos.
- [ ] Radiación negativa → `0`; humedad > 100 → `100`; lecturas fallidas no se envían.
- [ ] Los envíos se guardan en una cola persistente y solo se borran con `202`.
- [ ] Desenchufando la red un rato y volviéndola a conectar, lo pendiente se envía y llega.
- [ ] El token está fuera del código y del repositorio.
- [ ] El administrador ha dado de alta el `gatewayId` y los `sensorId`.

---

## 9. Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `401 invalid token` | Token mal copiado (espacios, saltos de línea), o el administrador no ha redesplegado tras crearlo |
| `403 token does not belong to this gatewayId` | El `gatewayId` del JSON no es el de ese token |
| `422 … unknown property` | `property` mal escrita, o una propiedad que no es de las tres |
| `422 … must be sent in 'Cel'` | Unidad distinta de la tabla. Envía exactamente `Cel`, `%`, `W/m2`, o no envíes `unit` |
| `422 … outside [0, 1500]` | Radiación negativa o humedad > 100. Ajusta antes de enviar |
| `422 … timestamp must include a timezone` | Falta la `Z` al final de la fecha |
| `422 … Extra inputs are not permitted` | Campo que no existe (`timestamp`, `sensorID`, `location`…) |
| `404` en `/healthz` | Usa `/health` |
| `202` pero los datos tienen fecha de 1970 o de ayer | Reloj sin sincronizar al medir |
| Timeouts al primer envío tras un rato sin tráfico | La API arranca bajo demanda (unos segundos). Con timeout de 30 s y reintento no se pierde nada |

---

## Anexo para el administrador: dar de alta una estación

**1. Token de la estación** (en Cloud Shell, carpeta del repositorio):

```bash
python3 tools/new_token.py gateway-station-with-pi3
./deploy.sh        # con PROJECT_ID y CLOUDSQL_INSTANCE, para que la API cargue el token nuevo
```

Pasa el token al responsable del dispositivo **por un canal privado**. Si se filtra, borra su línea de `gateway-tokens.json`, genera otro y vuelve a desplegar.

**2. Registro de la estación y sus sensores** (Cloud SQL Studio, usuario `postgres`, base de datos `agroclima`):

```sql
INSERT INTO registry.gateways (gateway_id, hardware_id, description)
VALUES ('gateway-station-with-pi3', '<MAC o nº de serie>', 'Estación con Raspberry Pi 3');

INSERT INTO registry.sensors (sensor_id, model, serial_number)
VALUES ('pi3-air01', '<modelo>', '<nº de serie>');

INSERT INTO registry.sensor_installations
  (sensor_id, gateway_id, site_id, zone_id, feature_of_interest, height_m, position, valid_from)
VALUES
  ('pi3-air01', 'gateway-station-with-pi3', 'farm-01', 'gh1', 'greenhouse-1-air', 2.0,
   'centro del invernadero, sobre el cultivo', '2026-11-27T00:00:00Z');
```

`valid_from` debe ser anterior a la primera lectura que vaya a enviar ese sensor.

**Mover un sensor a otra estación:** cierra la instalación actual y abre otra en el mismo instante:

```sql
UPDATE registry.sensor_installations SET valid_to = '2026-12-01T10:00:00Z'
WHERE sensor_id = 'pi3-air01' AND valid_to IS NULL;

INSERT INTO registry.sensor_installations (sensor_id, gateway_id, site_id, zone_id, height_m, valid_from)
VALUES ('pi3-air01', '<otra estación>', 'farm-01', 'gh2', 2.0, '2026-12-01T10:00:00Z');
```

**Ver qué está llegando:**

```sql
SELECT gateway_id, sensor_id, property, value, unit, ts, received_at
FROM ingest.readings
ORDER BY received_at DESC
LIMIT 50;
```
