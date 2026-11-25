# EMMASA (Portal Ciudadano) para Home Assistant

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=alejandrojara&repository=ha-emmasa&category=integration)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alejandrojara&repository=ha-emmasa&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=emmasa)

> [!CAUTION]
> **AVISO TÉCNICO Y LEGAL — COMPLEMENTO COMUNITARIO NO OFICIAL**
>
> - **Sin afiliación ni soporte oficial:** Este proyecto es un **complemento comunitario independiente y NO oficial**. No está desarrollado, avalado, auditado ni soportado por **EMMASA (Empresa Mixta de Aguas de Santa Cruz de Tenerife, S.A.)**, **Grupo Sacyr / Acuama**, **Elliot Cloud**, ni por el proyecto **Home Assistant / Open Home Foundation**. Todas las marcas mencionadas pertenecen a sus respectivos propietarios y se citan exclusivamente con fines descriptivos de interoperabilidad.
> - **API privada no documentada:** Esta integración interactúa con los endpoints internos del Portal Ciudadano de EMMASA utilizando las credenciales propias del usuario titular del contrato. Al no existir una API pública oficial, cualquier cambio en el portal web, en el flujo OAuth2/SSO, la activación de MFA por SMS (`loginA2P`) o cambios en los dashboards embebidos puede provocar fallos temporales o permanentes sin previo aviso.
> - **Uso estrictamente personal, responsable y de solo lectura:** El cliente accede únicamente en **modo solo lectura** a los contratos del titular autenticado con un intervalo de sondeo respetuoso (por defecto cada **8 horas** en una sola petición *batch*). Es responsabilidad del usuario verificar que su uso cumple los términos de servicio de su contrato y del portal de EMMASA.
> - **Seguridad, privacidad y exención de responsabilidad:** Como ocurre con cualquier integración comunitaria instalada desde el **Home Assistant Marketplace**, este código se ejecuta dentro de tu instancia local de Home Assistant. Tus credenciales y tokens se guardan localmente en el almacenamiento seguro de Home Assistant (`.storage`) y solo se comunican directamente con los servidores oficiales de EMMASA / Sacyr Acuama, nunca con terceros. El software se distribuye bajo licencia **MIT «TAL CUAL» (*AS IS*)**, sin garantía de ningún tipo; los valores mostrados tienen carácter informativo y **no sustituyen ni tienen validez legal o de facturación** frente a las lecturas y facturas oficiales de EMMASA.

---

Integración personalizada para **Home Assistant** (compatible con **Home Assistant Marketplace** en Core 2026.11+ y con **HACS** en Core 2026.10) que conecta con el **Portal Ciudadano de EMMASA** (Santa Cruz de Tenerife / Elliot Cloud / Oficina Virtual Sacyr Acuama) para importar tu consumo horario real de telelectura en el **Panel de Energía**, junto con lecturas acumuladas del contador, resúmenes de facturación y diagnóstico de alertas.

---

## Características principales

- **Precisión horaria real en el Panel de Energía de Home Assistant:**
  EMMASA publica las lecturas horarias de telelectura (`vol_h`) en lotes diferidos (habitualmente con 9–24 h de retardo). En lugar de generar picos artificiales en la hora del sondeo, esta integración importa únicamente **horas UTC cerradas** en su marca de tiempo real mediante la API oficial de estadísticas externas del Recorder (`async_add_external_statistics` con `StatisticMeanType.NONE`, `unit_class="volume"` y `UnitOfVolume.CUBIC_METERS`).
- **Backfill automático e incremental:** importa hasta 30 días (configurable entre 7 y 365 días) en la primera sincronización y añade únicamente las horas nuevas en sondeos posteriores sin duplicados ni saltos negativos.
- **Selección de suministro en cuentas multi-contrato:** si tu cuenta tiene un único contrato se configura automáticamente; si dispone de **más de un suministro**, tras autenticarte se listan todos tus contratos para elegir cuál supervisar (y puedes repetir el asistente para añadir suministros adicionales de forma independiente).
- **Autenticación 100% HTTP asíncrona (`aiohttp`):** OAuth2 + PKCE (`S256`) sin navegador *headless*, sesión HTTP compartida (`async_get_clientsession`), rotación atómica de `refresh_token` protegida contra condiciones de carrera y flujos nativos de **reautenticación** y **reconfiguración** desde la UI.
- **Primera instalación sin reinicio en Home Assistant 2026.11+:** al incluir `config_flow: true`, iconos de marca locales (`brand/icon.png`) y cero dependencias PyPI externas (`requirements: []`), el **Home Assistant Marketplace** carga la integración de inmediato y abre el asistente de configuración sin reiniciar.
- **Privacidad y Diagnóstico:** descarga de diagnóstico integrada con redacción automática (`async_redact_data`) de credenciales, tokens, correos e identificadores personales o de contrato.

---

## Instalación

### Opción 1: Home Assistant Marketplace (Home Assistant 2026.11 o superior — Recomendado)

Desde **Home Assistant 2026.11**, HACS ha sido sustituido por el **Home Assistant Marketplace** integrado de serie en **Ajustes → Marketplace** (no instales el antiguo HACS).

1. **Instalación en un clic:** pulsa el botón **Open repository in Marketplace** al inicio de este README, **o** añade el repositorio manualmente:
   - Abre **Ajustes → Marketplace** en Home Assistant (solo administradores; acepta el aviso de contenido comunitario la primera vez).
   - Selecciona **Añadir desde enlace** (*Add from link*) e introduce:
     ```text
     https://github.com/alejandrojara/ha-emmasa
     ```
   - *(Requisito de Home Assistant Marketplace: para repositorios personalizados fuera del catálogo por defecto, Home Assistant pedirá vincular una cuenta gratuita de GitHub solo con permisos de lectura pública).*
2. Pulsa **Instalar**.
3. Al tratarse de una integración con asistente UI y sin dependencias externas, **no hace falta reiniciar Home Assistant** en la primera instalación: se abrirá directamente el flujo de configuración de **EMMASA (Portal Ciudadano)** (o puedes abrirlo con el botón **Añadir integración** arriba / en **Ajustes → Dispositivos y servicios → Añadir integración**).
4. Introduce tu **Usuario (DNI / NIF)** y **Contraseña** de la Oficina Virtual de EMMASA.
5. Si tu cuenta tiene **más de un suministro**, selecciona en el desplegable el contrato que quieres integrar (puedes repetir el paso 3 para añadir otros contratos de la misma cuenta).
6. Las futuras actualizaciones aparecerán automáticamente en **Ajustes → Actualizaciones** (al actualizar una integración ya cargada, Home Assistant mostrará una reparación para reiniciar).

### Opción 2: HACS clásico (Home Assistant 2026.10)

1. Abre **HACS → Integraciones → ⋮ → Repositorios personalizados**.
2. Añade `https://github.com/alejandrojara/ha-emmasa` con categoría **Integración**.
3. Descarga **EMMASA (Portal Ciudadano)** y **reinicia Home Assistant**.
4. Ve a **Ajustes → Dispositivos y servicios → Añadir integración → EMMASA (Portal Ciudadano)**.

### Opción 3: Instalación manual

1. Descarga la última versión desde la pestaña *Releases* o clona este repositorio.
2. Copia la carpeta `custom_components/emmasa` dentro del directorio `config/custom_components/` de tu instalación de Home Assistant.
3. Reinicia Home Assistant y añade la integración desde **Ajustes → Dispositivos y servicios → Añadir integración**.

---

## Configuración del Panel de Energía (Consumo de agua)

Para visualizar gráficas horarias exactas sin desplazamientos causados por el retardo de telelectura:

1. Abre **Ajustes → Paneles de control → Energía**.
2. En la sección **Consumo de agua**, pulsa **Añadir consumo de agua**.
3. Selecciona la estadística externa **`EMMASA <código_contrato> Consumo de agua`** (`emmasa:water_consumption_<código>`).
4. *(Opcional)* Configura un precio fijo por $m^3$ o una entidad de tarifa para estimar el coste.

> [!NOTE]
> Los sensores convencionales de lectura del contador y consumo del periodo omiten `state_class` (`state_class=None`) intencionadamente para evitar que el Panel de Energía agrupe consumos diferidos en la hora en la que se ejecutó el sondeo.

---

## Entidades creadas (por contrato)

| Entidad | Tipo | Device Class | Unidad | Categoría | Descripción |
| :--- | :--- | :--- | :---: | :--- | :--- |
| `emmasa:water_consumption_<contrato>` | Estadística externa | `volume` | `m³` | Panel de Energía | Serie horaria exacta (`vol_h`) importada con marca de tiempo UTC real |
| **Lectura del contador** | `sensor` | `water` | `m³` | Primaria | Último índice acumulado publicado (`vol_acc`); incluye `energy_dashboard_statistic_id` |
| **Consumo del periodo actual** | `sensor` | `water` | `m³` | Primaria | Volumen acumulado desde el cierre del último periodo facturado |
| **Consumo de ayer** | `sensor` | `water` | `L` | Primaria | Consumo del último día completo cerrado (`Atlantic/Canary`); incluye atributo `reading_date` |
| **Último consumo facturado** | `sensor` | `water` | `m³` | Primaria | Consumo oficial del último bimestre facturado |
| **Importe de la última factura** | `sensor` | `monetary` | `EUR` | Primaria | Importe de la factura más reciente |
| **Última telelectura** | `sensor` | `timestamp` | — | Diagnóstico | Fecha/hora UTC del último dato recibido |
| **Retardo de telelectura** | `sensor` | `duration` | `h` | Diagnóstico *(deshab. por defecto)* | Horas transcurridas desde la última lectura publicada |
| **Telelectura disponible** | `binary_sensor` | `connectivity` | — | Diagnóstico | Indica si el contador dispone de telelectura activa |
| **Umbral alerta diurna / nocturna** | `sensor` | `volume` | `L` | Diagnóstico *(deshab. por defecto)* | Umbrales configurados en EMMASA (solo lectura) |
| **Alerta diurna / nocturna activa** | `binary_sensor` | — | — | Diagnóstico *(deshab. por defecto)* | Estado de las alertas en EMMASA (solo lectura) |

---

## Opciones de configuración y actualización de datos

Desde **Ajustes → Dispositivos y servicios → EMMASA (Portal Ciudadano) → Configurar** puedes ajustar:

| Parámetro | Por defecto | Rango | Descripción |
| :--- | :---: | :---: | :--- |
| **Intervalo de sondeo (horas)** | `8` | `1–24` | Frecuencia de consulta a EMMASA (una sola petición batch por contrato). Acorde al ritmo de publicación diferida de la telelectura (habitualmente 1–2 volcados al día). |
| **Historial horario a importar (días)** | `30` | `7–365` | Días de historial horario a importar en el Recorder. Si aumentas este valor posteriormente, la integración importa automáticamente el histórico anterior disponible en EMMASA desplazando las sumas acumuladas sin picos. |

También dispone de **Reconfigurar** (en el menú **⋮** de la entrada) para actualizar tus credenciales sin perder entidades ni estadísticas históricas, y de **Reautenticación** automática si caducan la contraseña o el `refresh_token`.

---

## Limitaciones conocidas y resolución de problemas

- **Retardo de telelectura (9–24 h):** EMMASA vuelca las lecturas horarias en lotes diferidos; el consumo del día en curso puede aparecer vacío o parcial hasta el siguiente volcado del proveedor.
- **Autenticación multifactor por SMS (`loginA2P`):** si EMMASA activase MFA por SMS en una cuenta, la integración lo detectará y mostrará el aviso `mfa_required` (no es compatible con sondeo desatendido).
- **Cambio obligatorio de contraseña:** si el portal requiere renovar la contraseña web, la integración mostrará `password_change_required`; cámbiala primero en el Portal Ciudadano y después reautentica en Home Assistant.
- **No aparecen datos en el Panel de Energía inmediatamente:** tras la primera sincronización, el Recorder de Home Assistant puede tardar unos minutos (o hasta el siguiente ciclo horario de UI) en refrescar el selector del Panel de Energía. Comprueba en el atributo `energy_dashboard_statistic_id` del sensor **Lectura del contador** el ID exacto de tu estadística externa.
- **Informes de errores:** descarga el archivo de diagnóstico (**⋮ → Descargar diagnósticos**) antes de abrir un *issue*; todos los datos personales, credenciales, tokens y códigos de contrato se redactan automáticamente.

---

## Desinstalación

1. Ve a **Ajustes → Dispositivos y servicios → EMMASA (Portal Ciudadano)**, abre el menú **⋮** de la entrada y selecciona **Eliminar**.
2. Abre **Ajustes → Marketplace** (o **HACS** en 2026.10), busca **EMMASA (Portal Ciudadano)** y pulsa **Desinstalar**.
3. Reinicia Home Assistant cuando el aviso de *Reparaciones* lo indique. *(Las estadísticas históricas ya importadas en el Recorder se conservan salvo que las borres manualmente en Herramientas para desarrolladores → Estadísticas).*

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
