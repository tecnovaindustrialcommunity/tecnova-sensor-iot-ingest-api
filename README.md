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
   │ (422 si el lote no es válido)      │
   │                                    └─ mensajes rechazados ► "readings-dlq"
   └─ registro de sensores ◄── Cloud SQL (Postgres), opcional
```

- **Cloud Run** valida el lote (formato, catálogo de propiedades, unidades, rangos, zona horaria y, si está activado, el registro de sensores) y responde `202` cuando todas las lecturas están en cola.
- **Pub/Sub** desacopla: si BigQuery va lento o falla, los datos esperan en la cola y no se pierden.
- La **suscripción de BigQuery** escribe cada lectura en la tabla sin código adicional. La tabla está particionada por día (`ts`) y agrupada (clustering) por `sensor_id`, `property`.
- La vista `agro.readings_clean` elimina los duplicados de lotes reenviados (mismo `reading_id`).

## Archivos

| Archivo | Qué es |
|---|---|
| `main.py` | La API (FastAPI) |
| `registry.py` | Registro de sensores: lee de Cloud SQL qué sensor estaba en qué gateway y cuándo |
| `catalog.json` | Las tres propiedades de la estación de aire, con su unidad canónica (UCUM) y rango válido |
| `bq_schema.json`, `views.sql` | Tabla y vistas de BigQuery |
| `db/schema.sql` | Tablas del registro en Postgres (solo si se empieza de cero) |
| `db/interface.sql` | Vista `ingest.sensor_gateway`, lo único que lee la API, y sus permisos |
| `db/seed.sql` | Datos de ejemplo coherentes con `examples/` |
| `deploy.sh` | Crea toda la infraestructura y despliega (se puede relanzar) |
| `tools/new_token.py` | Genera el token de un gateway |
| `examples/air-station.json` | Payload de ejemplo de una estación de aire |
| `tests/test_api.py`, `tests/test_registry.py` | Pruebas locales sin Google Cloud |

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

Pruebas locales: `pip install fastapi pydantic httpx "psycopg[binary]" && python3 tests/test_api.py && python3 tests/test_registry.py`

La última prueba de `test_registry.py` usa un Postgres real y solo se ejecuta si defines `DB_HOST` (y `DB_PORT`, `DB_USER`, `DB_PASSWORD`). Por ejemplo, con Docker:

```bash
docker run -d --name registry-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agroclima -p 55432:5432 postgres:16
P="docker exec -i registry-test psql -v ON_ERROR_STOP=1 -U postgres -d agroclima"
$P -c "CREATE ROLE ingest_api LOGIN PASSWORD 'test-pw'"
$P < db/schema.sql && $P < db/interface.sql && $P < db/seed.sql
DB_HOST=localhost DB_PORT=55432 DB_USER=ingest_api DB_PASSWORD=test-pw python3 tests/test_registry.py
docker rm -f registry-test
```

## Registro de sensores (Cloud SQL)

Comprueba que cada `sensorId` de un lote estaba asignado, en el momento de la lectura (`ts`), al `gatewayId` que lo envía. Es opcional: sin `CLOUDSQL_INSTANCE` la API funciona igual que antes.

La API solo lee la vista `ingest.sensor_gateway(sensor_id, gateway_id, valid_from, valid_to)`. Una asignación vale para `valid_from <= ts < valid_to` (`valid_to` NULL = vigente). Si ya tienes un registro propio, adapta `db/interface.sql` a tus tablas y no uses `db/schema.sql`.

Para cambiar un sensor de gateway, cierra su instalación actual (`valid_to`) e inserta una nueva que empiece en ese mismo instante. La base de datos rechaza instalaciones que se solapen.

**Activarlo:**

```bash
# 1. Usuario de solo lectura para la API
gcloud sql users create ingest_api --instance=<instancia> --password='...'

# 2. En la base de datos agroclima (como postgres): tablas, vista y, si quieres, datos de ejemplo
psql "host=... dbname=agroclima user=postgres" -f db/schema.sql -f db/interface.sql -f db/seed.sql

# 3. Desplegar con el registro en modo warn
PROJECT_ID=mi-proyecto CLOUDSQL_INSTANCE=mi-proyecto:europe-southwest1:<instancia> DB_PASSWORD='...' ./deploy.sh

# 4. Cuando los logs estén limpios de "registry problems", pasar a enforce
PROJECT_ID=mi-proyecto CLOUDSQL_INSTANCE=mi-proyecto:europe-southwest1:<instancia> REGISTRY_MODE=enforce ./deploy.sh
```

`DB_PASSWORD` solo hace falta la primera vez; se guarda en Secret Manager (`registry-db-password`). Si lo vuelves a pasar, se añade una versión nueva del secreto.

**Modos** (`REGISTRY_MODE`; `/healthz` indica el activo):

| Modo | Lote con sensores no asignados | Registro no disponible |
|---|---|---|
| `off` | Se acepta, sin comprobar | — |
| `warn` (por defecto con Cloud SQL) | Se acepta y se escribe `registry problems ...` en los logs | Se acepta sin comprobar |
| `enforce` | `422` con `{"detail": {"registry": [...]}}` (máx. 50 mensajes) | `503` |

**Variables:** `CLOUDSQL_INSTANCE` (o `DB_HOST` en local), `DB_PORT` (5432), `DB_NAME` (`agroclima`), `DB_USER` (`ingest_api`), `DB_PASSWORD`, `REGISTRY_MODE`, `REGISTRY_TTL_S` (60), `REGISTRY_LOOKBACK_DAYS` (30).

**Cómo se comporta:**
- Las asignaciones se guardan en memoria y se recargan cada `REGISTRY_TTL_S` segundos. Un sensor recién dado de alta puede tardar hasta 60 s en aceptarse.
- Si una recarga falla, la API sigue con la copia anterior y reintenta a los 10 s.
- Solo se cargan las asignaciones vigentes o cerradas en los últimos `REGISTRY_LOOKBACK_DAYS` días. En `enforce`, las lecturas antiguas de un sensor que cambió de gateway hace más tiempo se rechazan.
- La API conecta por el socket `/cloudsql/<instancia>`, que necesita que la instancia tenga IP pública (no hace falta autorizar redes). Con solo IP privada habría que configurar salida VPC en Cloud Run.

## Respuestas de la API

| Código | Significado | Qué debe hacer el dispositivo |
|---|---|---|
| `202` | Lote aceptado | Borrar el lote del búfer |
| `401` / `403` | Token ausente o inválido / el token es de otro `gatewayId` | No reintentar; revisar la configuración |
| `422` | Lote inválido (el cuerpo explica qué lectura y por qué), o sensores no asignados a este gateway en modo `enforce` | No reintentar el mismo lote; registrar el error |
| `503` / `5xx` / timeout | Problema temporal (incluye registro de sensores no disponible en `enforce`) | Reintentar con espera creciente. Los duplicados no importan |

Un lote se acepta o se rechaza **entero**: si una lectura es inválida, no se guarda ninguna.

## Reglas del payload

- Sobre: `schemaVersion` (`"1.0"`), `gatewayId`, `sentAt`, `readings[]` (1–1000).
- Lectura: `sensorId`, `property` (una de las tres del catálogo), `value` (número), `ts`. Opcionales: `unit`, `aggregation` (`mean`, `min`, `max`, `last`), `period` (ISO 8601, p. ej. `PT10M`).
- Fechas con zona horaria (`Z`). Se guardan en UTC.
- `unit`, si se envía, debe coincidir con la del catálogo (todavía no hay conversión de unidades).
- No se admiten campos desconocidos. Los datos de instalación (sitio, zona, altura) van en el registro de sensores, no aquí.

## Pendiente

- **Datos de instalación en BigQuery:** unir las lecturas con la instalación vigente en su `ts` (zona, altura) desde `registry.sensor_installations`.
- **Exportadores** a NGSI-LD (Smart Data Models) y CGO a partir de `readings_clean` + registro + catálogo.
- **Alertas:** en Cloud Monitoring, sobre mensajes en `readings-dlq-sub` y errores 5xx de Cloud Run.

## Problemas habituales

- **`--allow-unauthenticated` falla:** la organización tiene una política que impide servicios públicos. Pide una excepción para este proyecto o pon un API Gateway o balanceador delante. La autenticación por token de la API sigue funcionando igual.
- **El build falla por permisos** en un proyecto nuevo: la cuenta de servicio que usa Cloud Build necesita permisos para compilar y para escribir en Artifact Registry. El error de `gcloud` indica qué rol falta.
- **No llegan filas a BigQuery:** mira `readings-dlq-sub` (`gcloud pubsub subscriptions pull readings-dlq-sub --limit=5`). Casi siempre es un campo que no encaja con el esquema de la tabla.
