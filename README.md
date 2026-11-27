# agroclima-ingest-api

API de ingesta de datos de **estaciones de aire**. Solo hay un tipo de estación, con tres sensores:

| Propiedad | Unidad (UCUM) | Rango válido |
|---|---|---|
| `air_temperature` | `Cel` | -50 … 70 |
| `air_relative_humidity` | `%` | 0 … 100 |
| `global_radiation` | `W/m2` | 0 … 1500 |

Cualquier otra propiedad se rechaza con `422`.

```
Estación de aire (ESP32 / Raspberry)
        │  POST /v1/readings  (Bearer token)
        ▼
Cloud Run  ──valida──►  Pub/Sub "readings-raw"  ──suscripción BigQuery──►  agro.readings
   (422 si el lote no es válido)        │
                                        └─ mensajes rechazados ► "readings-dlq"
```

- **Cloud Run** valida el lote (formato, catálogo de propiedades, unidades, rangos, zona horaria) y responde `202` cuando todas las lecturas están en cola.
- **Pub/Sub** desacopla: si BigQuery va lento o falla, los datos esperan en la cola y no se pierden.
- La **suscripción de BigQuery** escribe cada lectura en la tabla sin código adicional. La tabla está particionada por día (`ts`) y agrupada (clustering) por `sensor_id`, `property`.
- La vista `agro.readings_clean` elimina los duplicados de lotes reenviados (mismo `reading_id`).

## Archivos

| Archivo | Qué es |
|---|---|
| `main.py` | La API (FastAPI) |
| `catalog.json` | Las tres propiedades de la estación de aire, con su unidad canónica (UCUM) y rango válido |
| `bq_schema.json`, `views.sql` | Tabla y vistas de BigQuery |
| `deploy.sh` | Crea toda la infraestructura y despliega (se puede relanzar) |
| `tools/new_token.py` | Genera el token de un gateway |
| `examples/air-station.json` | Payload de ejemplo de una estación de aire |
| `tests/test_api.py` | Pruebas locales sin Google Cloud |

## Despliegue

Requisitos: un proyecto de Google Cloud con facturación activa, y `gcloud` y `bq` instalados y autenticados (`gcloud auth login`).

```bash
# 1. Un token por gateway (guarda el token: solo se muestra una vez)
python3 tools/new_token.py gw-gh1-01

# 2. Desplegar (región por defecto: europe-southwest1, Madrid)
PROJECT_ID=mi-proyecto ./deploy.sh
```

`gateway-tokens.json` guarda solo los **hashes** de los tokens y se sube a Secret Manager. No lo subas a git (ya está en `.gitignore`).

Para **añadir un gateway** más tarde: `python3 tools/new_token.py <id>` y vuelve a lanzar `./deploy.sh`, que crea una versión nueva del secreto y redespliega el servicio para que la cargue.

## Probar

```bash
URL=$(gcloud run services describe agroclima-ingest-api --region europe-southwest1 --format='value(status.url)')

curl -s -X POST "$URL/v1/readings" \
  -H "Authorization: Bearer <token de gw-gh1-01>" \
  -H "Content-Type: application/json" \
  -d @examples/air-station.json
# {"accepted":3}
```

En BigQuery, unos segundos después:

```sql
SELECT sensor_id, property, value_num, unit, ts
FROM `mi-proyecto.agro.readings_clean`
ORDER BY ts DESC LIMIT 20;
```

Pruebas locales: `pip install fastapi pydantic httpx && python3 tests/test_api.py`

## Respuestas de la API

| Código | Significado | Qué debe hacer el dispositivo |
|---|---|---|
| `202` | Lote aceptado | Borrar el lote del búfer |
| `401` / `403` | Token ausente o inválido / el token es de otro `gatewayId` | No reintentar; revisar la configuración |
| `422` | Lote inválido (el cuerpo explica qué lectura y por qué) | No reintentar el mismo lote; registrar el error |
| `503` / `5xx` / timeout | Problema temporal | Reintentar con espera creciente. Los duplicados no importan |

Un lote se acepta o se rechaza **entero**: si una lectura es inválida, no se guarda ninguna.

## Reglas del payload

- Sobre: `schemaVersion` (`"1.0"`), `gatewayId`, `sentAt`, `readings[]` (1–1000).
- Lectura: `sensorId`, `property` (una de las tres del catálogo), `value` (número), `ts`. Opcionales: `unit`, `aggregation` (`mean`, `min`, `max`, `last`), `period` (ISO 8601, p. ej. `PT10M`).
- Fechas con zona horaria (`Z`). Se guardan en UTC.
- `unit`, si se envía, debe coincidir con la del catálogo (todavía no hay conversión de unidades).
- No se admiten campos desconocidos. Los datos de instalación (sitio, zona, altura) van en el registro de sensores, no aquí.

## Pendiente

- **Registro de sensores:** comprobar que cada `sensorId` pertenece al `gatewayId` (hay un `TODO` en `main.py`) y unir las lecturas con los datos de instalación de la estación.
- **Exportadores** a NGSI-LD (Smart Data Models) y CGO a partir de `readings_clean` + registro + catálogo.
- **Alertas:** en Cloud Monitoring, sobre mensajes en `readings-dlq-sub` y errores 5xx de Cloud Run.

## Problemas habituales

- **`--allow-unauthenticated` falla:** la organización tiene una política que impide servicios públicos. Pide una excepción para este proyecto o pon un API Gateway o balanceador delante. La autenticación por token de la API sigue funcionando igual.
- **El build falla por permisos** en un proyecto nuevo: la cuenta de servicio que usa Cloud Build necesita permisos para compilar y para escribir en Artifact Registry. El error de `gcloud` indica qué rol falta.
- **No llegan filas a BigQuery:** mira `readings-dlq-sub` (`gcloud pubsub subscriptions pull readings-dlq-sub --limit=5`). Casi siempre es un campo que no encaja con el esquema de la tabla.
