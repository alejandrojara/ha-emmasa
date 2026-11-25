# EMMASA (Portal Ciudadano) para Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Integración personalizada para **Home Assistant** (compatible con **HACS**) que conecta con el **Portal Ciudadano de EMMASA** (Santa Cruz de Tenerife / Elliot Cloud / Oficina Virtual Sacyr Acuama) para importar tu consumo horario real de agua de telelectura, lecturas de contador, facturación y estado de alertas de forma segura, asíncrona y respetuosa.

> **Aviso:** Proyecto comunitario no oficial, sin afiliación ni respaldo de EMMASA ni de Grupo Sacyr. Solo accede de forma **exclusivamente de solo lectura** a los contratos del usuario autenticado.

---

## Características principales

- **Precisión horaria real en el Panel de Energía de Home Assistant:**
  EMMASA publica las lecturas horarias de telelectura (`vol_h`) por lotes diferidos (típicamente con un retardo de ~9–24 h). En lugar de generar picos artificiales en la hora del sondeo, esta integración importa cada hora en su marca de tiempo UTC real mediante la API oficial de estadísticas externas del Recorder (`async_add_external_statistics`, compatible con Home Assistant Core 2026.10+/2026.11+ usando `StatisticMeanType.NONE` y `unit_class="volume"`).
- **Backfill automático e incremental:** importa hasta 30 días (configurable de 7 a 365 días) en la primera sincronización y añade únicamente las horas nuevas en sondeos sucesivos.
- **Un dispositivo por contrato de suministro:** soporta múltiples contratos en la misma cuenta.
- **Autenticación OAuth2 + PKCE (`S256`) 100% HTTP asíncrona (`aiohttp`):** sin navegador *headless*, con renovación silenciosa de tokens (`refresh_token` rotatorio persistido de forma segura) y flujos nativos de **reautenticación** y **reconfiguración** desde la UI.
- **Sondeo responsable:** por defecto consulta cada **2 horas** (configurable entre 1 y 6 h) agrupando todas las métricas en una única petición batch.
- **Privacidad y Diagnóstico:** descarga de diagnóstico integrada con redacción automática (`async_redact_data`) de credenciales, tokens, correos e identificadores personales.

---

## Instalación mediante HACS

1. En Home Assistant, abre **HACS → Integraciones**.
2. Pulsa el menú superior derecho (**⋮**) → **Repositorios personalizados**.
3. Añade `https://github.com/alejandrojara/ha-emmasa` con categoría **Integración**.
4. Busca **EMMASA**, descárgalo y reinicia Home Assistant.
5. Ve a **Ajustes → Dispositivos y servicios → Añadir integración → EMMASA (Portal Ciudadano)** e introduce tu usuario (DNI/NIF) y contraseña de la Oficina Virtual de EMMASA.

---

## Configuración del Panel de Energía (Consumo de agua)

Para ver tus gráficas horarias exactas sin desplazamientos por la latencia de telelectura:

1. Abre **Ajustes → Paneles de control → Energía**.
2. En la sección **Consumo de agua**, pulsa **Añadir consumo de agua**.
3. Selecciona la estadística externa **`EMMASA <código_contrato> Consumo de agua`** (`emmasa:water_consumption_<código>`).
4. *(Opcional)* Configura un precio fijo por $m^3$ o una entidad de tarifa para estimar el coste.

> Los sensores convencionales de lectura del contador y consumo del periodo usan `state_class=None` intencionadamente para evitar que el Panel de Energía muestre picos agrupados en la hora del sondeo.

---

## Entidades creadas (por contrato)

| Entidad | Tipo | Unidad | Categoría | Descripción |
| :--- | :--- | :---: | :--- | :--- |
| `emmasa:water_consumption_<contrato>` | Estadística externa | `m³` | Panel de Energía | Serie horaria exacta (`vol_h`) importada con marca de tiempo real |
| **Lectura del contador** | `sensor` (`water`) | `m³` | Primaria | Último índice acumulado publicado (`vol_acc`) |
| **Consumo del periodo actual** | `sensor` (`water`) | `m³` | Primaria | Volumen acumulado desde el cierre del último periodo facturado |
| **Consumo de ayer** | `sensor` (`water`) | `L` | Primaria | Consumo del último día completo cerrado (`Atlantic/Canary`) |
| **Último consumo facturado** | `sensor` (`water`) | `m³` | Primaria | Consumo oficial del último bimestre facturado |
| **Importe de la última factura** | `sensor` (`monetary`) | `EUR` | Primaria | Importe de la factura más reciente |
| **Última telelectura** | `sensor` (`timestamp`) | — | Diagnóstico | Fecha/hora UTC del último dato recibido |
| **Retardo de telelectura** | `sensor` (`duration`) | `h` | Diagnóstico *(deshab. por defecto)* | Horas transcurridas desde la última lectura publicada |
| **Telelectura disponible** | `binary_sensor` (`connectivity`) | — | Diagnóstico | Indica si el contador dispone de telelectura activa |
| **Umbral alerta diurna / nocturna** | `sensor` (`water`) | `L` | Diagnóstico *(deshab. por defecto)* | Umbrales configurados en EMMASA (solo lectura en v0.1.0) |
| **Alerta diurna / nocturna activa** | `binary_sensor` | — | Diagnóstico *(deshab. por defecto)* | Estado de las alertas en EMMASA (solo lectura en v0.1.0) |

---

## Actualización de datos y limitaciones conocidas

- **Frecuencia de actualización:** por defecto cada **2 horas** (configurable en *Configurar* entre 1 y 6 horas).
- **Retardo de telelectura:** los contadores publican datos en lotes con un retardo habitual de **9 a 24 horas**; el consumo del día en curso puede aparecer incompleto hasta el siguiente volcado de EMMASA.
- **Autenticación multifactor (MFA SMS):** si EMMASA activase la verificación por SMS (`loginA2P`) en una cuenta, la integración lo detectará y avisará claramente en el flujo de configuración/reautenticación.
- **API no documentada oficialmente:** depende del Portal Ciudadano de EMMASA / Elliot Cloud; cambios en su backend o dashboards embebidos pueden requerir actualizar la integración.

---

## Desarrollo y pruebas

```bash
python -m venv .venv
source .venv/bin/activate
pip install pytest-homeassistant-custom-component mypy ruff pytest-cov
ruff check custom_components tests
mypy custom_components/emmasa
pytest
```
