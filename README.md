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
Cloud Run  ──valida──►  Cloud SQL (Postgres)
   (422 si el lote       ├─ ingest.readings        ◄── inserta las lecturas
    no es válido)        └─ ingest.sensor_gateway  ──► registro de sensores (lectura)
```

- **Cloud Run** valida el lote (formato, catálogo de propiedades, unidades, rangos, zona horaria y registro de sensores) y lo guarda en Postgres en una sola transacción. Responde `202` cuando el lote está guardado.
- **Postgres** guarda una fila por lectura en `ingest.readings`. La clave primaria `(sensor_id, property, ts)` hace que los lotes reenviados no dupliquen datos.
- Si Postgres no responde, la API devuelve `503` y no guarda nada; el dispositivo reintenta el lote más tarde.

## Archivos

| Archivo | Qué es |
|---|---|
| `main.py` | La API (FastAPI) |
| `storage.py` | Conexión a Postgres: guarda lecturas y lee el registro |
| `registry.py` | Registro de sensores: qué sensor estaba en qué gateway y cuándo (en memoria, recargado cada 60 s) |
| `catalog.json` | Las tres propiedades de la estación de aire, con su unidad canónica (UCUM) y rango válido |
| `db/schema.sql` | Tablas del registro de sensores (solo si se empieza de cero) |
| `db/interface.sql` | Esquema `ingest`: tabla de lecturas, vista del registro y permisos de `ingest_api` |
| `db/seed.sql` | Datos de ejemplo del registro, coherentes con `examples/` |
| `deploy.sh` | Configura Google Cloud y despliega (se puede relanzar) |
| `tools/new_token.py` | Genera el token de un gateway |
| `examples/air-station.json` | Payload de ejemplo de una estación de aire |
| `tests/test_api.py`, `tests/test_registry.py` | Pruebas locales sin Google Cloud |

## Despliegue

Requisitos: un proyecto de Google Cloud con facturación activa, `gcloud` instalado y autenticado (`gcloud auth login`), y una **instancia de Cloud SQL para PostgreSQL** con IP pública (no hace falta autorizar redes; la API conecta por el conector de Cloud SQL).

```bash
# 1. Un token por gateway (guarda el token: solo se muestra una vez)
python3 tools/new_token.py gw-gh1-01

# 2. Configurar y desplegar: crea la base de datos agroclima y el usuario ingest_api si no existen
#    (región por defecto: europe-southwest1, Madrid)
PROJECT_ID=mi-proyecto \
CLOUDSQL_INSTANCE=mi-proyecto:europe-southwest1:<instancia> \
DB_PASSWORD='...' \
./deploy.sh

# 3. Crear las tablas en la base de datos agroclima, como postgres (necesita psql instalado)
gcloud sql connect <instancia> --user=postgres --database=agroclima
#   y dentro de psql:
#   \i db/schema.sql      registro de sensores (omítelo si ya tienes uno; ver "Base de datos")
#   \i db/interface.sql   lecturas, vista del registro y permisos
#   \i db/seed.sql        opcional: datos de ejemplo
```

Sin `psql`, puedes pegar el contenido de los tres archivos, en ese orden, en Cloud SQL Studio (consola de Google Cloud).

Hasta que existan las tablas, la API responde `503`: los dispositivos reintentan y no se pierde nada.

`gateway-tokens.json` guarda solo los **hashes** de los tokens y se sube a Secret Manager. No lo subas a git (ya está en `.gitignore`). La contraseña de `ingest_api` se guarda en el secreto `ingest-db-password`; `DB_PASSWORD` solo hace falta la primera vez (si lo vuelves a pasar, se cambia la contraseña y se añade una versión del secreto).

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

En Postgres:

```sql
SELECT sensor_id, property, value, unit, ts
FROM ingest.readings
ORDER BY ts DESC LIMIT 20;
```

Pruebas locales: `pip install -r requirements.txt httpx && python3 tests/test_api.py && python3 tests/test_registry.py`

La última prueba de `test_registry.py` usa un Postgres real y solo se ejecuta si defines `DB_HOST`. Envía el ejemplo dos veces a la API, comprueba que se guarda una sola vez y que `ingest_api` no puede modificar ni borrar lecturas. Por ejemplo, con Docker:

```bash
docker run -d --name registry-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agroclima -p 55432:5432 postgres:16
P="docker exec -i registry-test psql -v ON_ERROR_STOP=1 -U postgres -d agroclima"
$P -c "CREATE ROLE ingest_api LOGIN PASSWORD 'test-pw'"
$P < db/schema.sql && $P < db/interface.sql && $P < db/seed.sql
DB_HOST=localhost DB_PORT=55432 DB_USER=ingest_api DB_PASSWORD=test-pw python3 tests/test_registry.py
docker rm -f registry-test
```

## Base de datos

Todo lo que usa la API está en el esquema `ingest` (`db/interface.sql`):

- **`ingest.readings`**: una fila por lectura (`sensor_id`, `property`, `ts`, `value`, `unit`, `gateway_id`, `aggregation`, `period_s`, `sent_at`, `received_at`). Fechas en `timestamptz` (UTC).
- **`ingest.sensor_gateway`**: vista `(sensor_id, gateway_id, valid_from, valid_to)` con el historial de asignaciones de sensores a gateways.

El usuario `ingest_api` solo puede **leer la vista e insertar lecturas**: no puede modificar ni borrar lecturas, ni leer las tablas del registro.

El registro de sensores de referencia (`db/schema.sql`, esquema `registry`) guarda gateways, sensores e instalaciones (sitio, zona, altura, posición, configuración). Si ya tienes un registro propio, no uses `db/schema.sql`: adapta el `SELECT` de la vista en `db/interface.sql` a tus tablas.

Para cambiar un sensor de gateway, cierra su instalación actual (`valid_to`) e inserta una nueva que empiece en ese mismo instante. La base de datos rechaza instalaciones que se solapen.

Cada instancia de Cloud Run abre como máximo 5 conexiones (con `--max-instances 3`, 15 en total). Ten en cuenta el límite de conexiones de la instancia de Cloud SQL si subes `--max-instances`.

## Registro de sensores

Comprueba que cada `sensorId` de un lote estaba asignado, en el momento de la lectura (`ts`), al `gatewayId` que lo envía. Una asignación vale para `valid_from <= ts < valid_to` (`valid_to` NULL = vigente).

**Modos** (`REGISTRY_MODE`; `/healthz` indica el activo):

| Modo | Lote con sensores no asignados | Registro no disponible |
|---|---|---|
| `off` | Se guarda, sin comprobar | — |
| `warn` (por defecto) | Se guarda y se escribe `registry problems ...` en los logs | Se intenta guardar sin comprobar |
| `enforce` | `422` con `{"detail": {"registry": [...]}}` (máx. 50 mensajes) | `503` |

Empieza en `warn`. Cuando los logs estén limpios de `registry problems`, pasa a `enforce`:

```bash
PROJECT_ID=mi-proyecto CLOUDSQL_INSTANCE=mi-proyecto:europe-southwest1:<instancia> REGISTRY_MODE=enforce ./deploy.sh
```

**Cómo se comporta:**
- Las asignaciones se guardan en memoria y se recargan cada `REGISTRY_TTL_S` segundos (60). Un sensor recién dado de alta puede tardar hasta 60 s en aceptarse.
- Si una recarga falla, la API sigue con la copia anterior y reintenta a los 10 s.
- Solo se cargan las asignaciones vigentes o cerradas en los últimos `REGISTRY_LOOKBACK_DAYS` días (30). En `enforce`, las lecturas antiguas de un sensor que cambió de gateway hace más tiempo se rechazan.

## Variables de entorno

| Variable | Por defecto | Qué es |
|---|---|---|
| `CLOUDSQL_INSTANCE` | — | Nombre de conexión `PROJECT:REGION:INSTANCE` (en Cloud Run) |
| `DB_HOST`, `DB_PORT` | —, `5432` | Alternativa a `CLOUDSQL_INSTANCE` para conectar en local |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `agroclima`, `ingest_api`, — | Base de datos y credenciales |
| `REGISTRY_MODE` | `warn` | `off`, `warn` o `enforce` |
| `REGISTRY_TTL_S` | `60` | Segundos entre recargas del registro |
| `REGISTRY_LOOKBACK_DAYS` | `30` | Días de historial de asignaciones que se cargan |
| `TOKENS_FILE` | `/secrets/gateway-tokens.json` | Hashes de los tokens de los gateways |

## Respuestas de la API

| Código | Significado | Qué debe hacer el dispositivo |
|---|---|---|
| `202` | Lote guardado | Borrar el lote del búfer |
| `401` / `403` | Token ausente o inválido / el token es de otro `gatewayId` | No reintentar; revisar la configuración |
| `422` | Lote inválido (el cuerpo explica qué lectura y por qué), o sensores no asignados a este gateway en modo `enforce` | No reintentar el mismo lote; registrar el error |
| `503` / `5xx` / timeout | Problema temporal (base de datos no disponible) | Reintentar con espera creciente. Los duplicados no importan |

Un lote se acepta o se rechaza **entero**: si una lectura es inválida, no se guarda ninguna.

## Reglas del payload

- Sobre: `schemaVersion` (`"1.0"`), `gatewayId`, `sentAt`, `readings[]` (1–1000).
- Lectura: `sensorId`, `property` (una de las tres del catálogo), `value` (número), `ts`. Opcionales: `unit`, `aggregation` (`mean`, `min`, `max`, `last`), `period` (ISO 8601, p. ej. `PT5M`).
- Fechas con zona horaria (`Z`). Se guardan en UTC.
- `unit`, si se envía, debe coincidir con la del catálogo (todavía no hay conversión de unidades).
- No se admiten campos desconocidos. Los datos de instalación (sitio, zona, altura) van en el registro de sensores, no aquí.
- Si se reenvía una lectura con el mismo `sensorId`, `property` y `ts`, se conserva la primera.

## Pendiente

- **Exportadores** a NGSI-LD (Smart Data Models) y CGO a partir de `ingest.readings` + registro + catálogo.
- **Alertas:** en Cloud Monitoring, sobre errores 5xx de Cloud Run y conexiones de Cloud SQL.
- **Retención:** si el volumen crece mucho, particionar `ingest.readings` por mes.

## Problemas habituales

- **`--allow-unauthenticated` falla:** la organización tiene una política que impide servicios públicos. Pide una excepción para este proyecto o pon un API Gateway o balanceador delante. La autenticación por token de la API sigue funcionando igual.
- **El build falla por permisos** en un proyecto nuevo: la cuenta de servicio que usa Cloud Build necesita permisos para compilar y para escribir en Artifact Registry. El error de `gcloud` indica qué rol falta.
- **Todo responde `503`:** mira los logs de Cloud Run (`could not store batch ...`). Lo habitual: no se ha ejecutado `db/interface.sql` (`relation "ingest.readings" does not exist`), contraseña incorrecta, o la instancia de Cloud SQL no tiene IP pública.
