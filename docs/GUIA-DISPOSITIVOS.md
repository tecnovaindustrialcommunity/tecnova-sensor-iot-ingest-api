# Guía rápida para dispositivos

Cómo enviar lecturas de una estación de aire a la API.

## Lo que necesitas

- **URL de la API:** `https://agroclima-ingest-api-110558122729.europe-southwest1.run.app`
- Y te lo pasa el administrador:
  - **`gatewayId`** de tu estación, por ejemplo `gateway-station-with-pi3`
  - **Token** de esa estación. Es secreto: guárdalo en la configuración del dispositivo, no en el código ni en git.
  - **`sensorId`** de cada sensor, por ejemplo `pi3-air01`

## Llamada

```
POST https://agroclima-ingest-api-110558122729.europe-southwest1.run.app/v1/readings
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

Si todo va bien, responde `202` con `{"accepted": 3}`.

Para probar con `curl`, guarda el JSON en `lote.json` y ejecuta:

```bash
curl -X POST "https://agroclima-ingest-api-110558122729.europe-southwest1.run.app/v1/readings" -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" -d @lote.json
```

Para comprobar que la API está viva: `GET https://agroclima-ingest-api-110558122729.europe-southwest1.run.app/health`. Usa `/health` y no `/healthz`, que en Cloud Run responde `404`.

## Campos

**Envío**

| Clave | Obligatoria | Valor |
|---|---|---|
| `schemaVersion` | Sí | `"1.0"` |
| `gatewayId` | Sí | El de tu token |
| `sentAt` | Sí | Fecha y hora del envío, UTC (`…Z`) |
| `readings` | Sí | Lista de 1 a 1000 lecturas |

**Lectura**

| Clave | Obligatoria | Valor |
|---|---|---|
| `sensorId` | Sí | Identificador del sensor |
| `property` | Sí | Una de la tabla **Propiedades** de abajo |
| `value` | Sí | Número (no `null`, no `NaN`) |
| `ts` | Sí | Momento de la medida, UTC (`…Z`). Para medias, el final del periodo |
| `unit` | No | Si se envía, exactamente la de la tabla de abajo |
| `aggregation` | No | `mean`, `min`, `max` o `last` |
| `period` | No | Duración ISO 8601: `PT5M` = 5 min, `PT1H` = 1 hora |

**Propiedades**

| `property` | `unit` | Rango |
|---|---|---|
| `air_temperature` | `Cel` | -50 … 70 |
| `air_relative_humidity` | `%` | 0 … 100 |
| `global_radiation` | `W/m2` | 0 … 1500 |
| `PPFD` | `umol/m2/s` | 0 … 2500 |

**Si tu sensor mide PPFD (sensor PAR) y no radiación global:** envía el valor medido como `PPFD` (en mayúsculas), en `umol/m2/s`. La unidad se escribe con `u` y no con `µ`: `µmol/m2/s` se rechaza.

```json
{ "sensorId": "pi3-air01", "property": "PPFD", "value": 495.3, "unit": "umol/m2/s", "ts": "2026-11-27T09:05:00Z", "aggregation": "mean", "period": "PT5M" }
```

No lo conviertas a `global_radiation`. Esa conversión divide el PPFD entre un factor que va de 2,0 a 2,3 según esté el cielo, así que solo da una estimación, sin la calidad de un piranómetro. Además, la API no distingue un valor convertido de uno medido. Manda `global_radiation` solo si tienes un piranómetro.

## Reglas

- **Fechas en UTC con `Z`.** Sin zona horaria, la lectura se rechaza. El reloj del dispositivo debe estar sincronizado por NTP antes de enviar nada.
- **No hay claves extra.** Una clave que no está en las tablas, o mal escrita (`timestamp`, `sensorID`…), hace que se rechace el envío.
- **Fuera de rango se rechaza.** Si la radiación sale negativa de noche, envía `0`; si la humedad pasa de 100, envía `100`. Si un sensor falla, no envíes esa lectura.
- **El envío es todo o nada.** Si una lectura está mal, no se guarda ninguna de ese envío.
- **Reenviar no duplica.** Una lectura con el mismo `sensorId` + `property` + `ts` que una ya guardada se ignora. Si no sabes si un envío llegó, reenvíalo.

## Respuestas

| Código | Significa | Qué hacer |
|---|---|---|
| `202` | Guardado | Borrar de la cola del dispositivo |
| `401` / `403` | Token incorrecto, o no corresponde a ese `gatewayId` | No reintentar; revisar la configuración |
| `422` | Envío mal formado (el cuerpo de la respuesta dice qué lectura y por qué) | No reintentar ese envío; corregir |
| `503`, otro `5xx`, timeout o sin red | Fallo temporal | Guardar y reintentar más tarde (por ejemplo, en el siguiente ciclo) |

Recomendado: timeout de 30 s y una cola en la tarjeta SD o en la flash para no perder datos si no hay red.
