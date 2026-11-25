"""Strictly validated read-only SQL query builders for EMMASA Grafana PostgreSQL datasource."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def validate_contract_uuid(contract_id: str) -> str:
    """Validate and normalize a contract UUID string before SQL interpolation."""
    return str(uuid.UUID(contract_id))


def _format_utc_iso(dt: datetime) -> str:
    """Format an aware datetime as an ISO-8601 UTC timestamp literal."""
    dt_utc = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def build_hourly_telelectura_query(contract_id: str, since_utc: datetime) -> str:
    """Build read-only query for hourly consumption (vol_h in m³) since a UTC timestamp.

    Note: Uses SUM(tw.vol_h) without DISTINCT to avoid the duplicate-value undercounting
    present in EMMASA's own Grafana panel query.
    """
    cid = validate_contract_uuid(contract_id)
    since_iso = _format_utc_iso(since_utc)
    return (
        "SELECT "
        "  DATE_TRUNC('hour', tw.timestamptz) AS ts_hour, "
        "  ROUND(SUM(tw.vol_h)::numeric, 6)::double precision AS vol_h "
        "FROM proj_14353_custom.contrato_contador cc "
        "JOIN proj_14353_data.ts_telelectura tw "
        "  ON cc.id_contador = tw.id_item "
        "  AND tw.timestamptz BETWEEN cc.start AND cc.end "
        f"WHERE cc.id_contrato = '{cid}' "
        f"  AND tw.timestamptz >= TIMESTAMPTZ '{since_iso}' "
        "  AND tw.vol_h IS NOT NULL "
        "GROUP BY 1 "
        "ORDER BY 1 ASC;"
    )


def build_latest_meter_reading_query(contract_id: str) -> str:
    """Build read-only query for the most recent cumulative meter index (vol_acc in m³)."""
    cid = validate_contract_uuid(contract_id)
    return (
        "SELECT "
        "  tw.timestamptz AS ts, "
        "  tw.vol_acc::double precision AS vol_acc "
        "FROM proj_14353_custom.contrato_contador cc "
        "JOIN proj_14353_data.ts_telelectura tw "
        "  ON cc.id_contador = tw.id_item "
        "  AND tw.timestamptz BETWEEN cc.start AND cc.end "
        f"WHERE cc.id_contrato = '{cid}' "
        "  AND tw.timestamptz <= NOW() "
        "  AND tw.vol_acc IS NOT NULL "
        "ORDER BY tw.timestamptz DESC "
        "LIMIT 1;"
    )


def build_current_period_query(contract_id: str) -> str:
    """Build read-only query for consumption accrued since the last billed period (m³)."""
    cid = validate_contract_uuid(contract_id)
    return (
        "WITH ultimo_facturado AS ("
        "  SELECT id_item, (MAX(timestamptz) + INTERVAL '1 day')::timestamptz AS fecha_ultimo_periodo "
        "  FROM proj_14353_data.ts_consumos_registrados "
        f"  WHERE id_item = '{cid}' AND consumo_registrado IS NOT NULL "
        "  GROUP BY id_item"
        "), "
        "volumen_ultimo_periodo AS ("
        "  SELECT DISTINCT ON (tw.id_item) "
        "    tw.id_item, tw.vol_acc AS vol_ultimo_facturado, uf.fecha_ultimo_periodo "
        "  FROM proj_14353_custom.contrato_contador cc "
        "  INNER JOIN proj_14353_data.ts_telelectura tw "
        "    ON cc.id_contador = tw.id_item "
        "    AND tw.timestamptz BETWEEN cc.start AND cc.end "
        "  INNER JOIN ultimo_facturado uf ON cc.id_contrato = uf.id_item "
        "  WHERE tw.timestamptz < uf.fecha_ultimo_periodo AND tw.vol_acc IS NOT NULL "
        "  ORDER BY tw.id_item, tw.timestamptz DESC"
        "), "
        "volumen_actual AS ("
        "  SELECT DISTINCT ON (tw.id_item) "
        "    tw.id_item, tw.vol_acc AS vol_acc_actual "
        "  FROM proj_14353_custom.contrato_contador cc "
        "  INNER JOIN proj_14353_data.ts_telelectura tw "
        "    ON cc.id_contador = tw.id_item "
        "    AND tw.timestamptz BETWEEN cc.start AND cc.end "
        f"  WHERE cc.id_contrato = '{cid}' AND tw.timestamptz <= NOW() AND tw.vol_acc IS NOT NULL "
        "  ORDER BY tw.id_item, tw.timestamptz DESC"
        ") "
        "SELECT "
        "  MAX(vup.fecha_ultimo_periodo) AS fecha_ultimo_periodo, "
        "  CASE WHEN COUNT(vup.vol_ultimo_facturado) > 0 THEN "
        "    ROUND(SUM(GREATEST(va.vol_acc_actual - vup.vol_ultimo_facturado, 0))::numeric, 3)::double precision "
        "  END AS consumo_periodo "
        "FROM volumen_actual va "
        "LEFT JOIN volumen_ultimo_periodo vup ON va.id_item = vup.id_item;"
    )


def build_latest_daily_consumption_query(contract_id: str) -> str:
    """Build read-only query for the most recent complete day consumption in Atlantic/Canary (L)."""
    cid = validate_contract_uuid(contract_id)
    return (
        "WITH lecturas_validas AS ("
        "  SELECT cc.id_contador, "
        "    DATE_TRUNC('day', tw.timestamptz AT TIME ZONE 'Atlantic/Canary') AS dia, "
        "    tw.timestamptz, tw.vol_acc "
        "  FROM proj_14353_custom.contrato_contador cc "
        "  JOIN proj_14353_data.ts_telelectura tw "
        "    ON cc.id_contador = tw.id_item "
        "    AND tw.timestamptz BETWEEN cc.start AND cc.end "
        f"  WHERE cc.id_contrato = '{cid}' "
        "    AND tw.timestamptz >= NOW() - INTERVAL '14 days' "
        "    AND tw.vol_acc IS NOT NULL"
        "), "
        "lectura_diaria AS ("
        "  SELECT DISTINCT ON (id_contador, dia) id_contador, dia, vol_acc "
        "  FROM lecturas_validas "
        "  ORDER BY id_contador, dia, timestamptz DESC"
        "), "
        "volumen_diario AS ("
        "  SELECT hoy.dia, SUM(manana.vol_acc - hoy.vol_acc) AS volumen_m3 "
        "  FROM lectura_diaria hoy "
        "  INNER JOIN lectura_diaria manana "
        "    ON manana.id_contador = hoy.id_contador "
        "    AND manana.dia = hoy.dia + INTERVAL '1 day' "
        "    AND manana.vol_acc >= hoy.vol_acc "
        "  GROUP BY hoy.dia"
        ") "
        "SELECT "
        "  TO_CHAR(dia, 'YYYY-MM-DD') AS fecha_simple, "
        "  ROUND((volumen_m3 * 1000.0)::numeric, 1)::double precision AS volumen_litros "
        "FROM volumen_diario "
        "ORDER BY dia DESC "
        "LIMIT 1;"
    )


def build_latest_billed_query(contract_id: str) -> str:
    """Build read-only query for the latest officially billed bimonthly consumption (m³)."""
    cid = validate_contract_uuid(contract_id)
    return (
        "SELECT "
        "  codigo_periodo::integer AS codigo_periodo, "
        "  consumo_registrado::double precision AS consumo_registrado, "
        "  CASE (codigo_periodo::integer % 100) "
        "    WHEN 1 THEN 'Ene-Feb ' || (codigo_periodo::integer / 100) "
        "    WHEN 2 THEN 'Mar-Abr ' || (codigo_periodo::integer / 100) "
        "    WHEN 3 THEN 'May-Jun ' || (codigo_periodo::integer / 100) "
        "    WHEN 4 THEN 'Jul-Ago ' || (codigo_periodo::integer / 100) "
        "    WHEN 5 THEN 'Sep-Oct ' || (codigo_periodo::integer / 100) "
        "    WHEN 6 THEN 'Nov-Dic ' || (codigo_periodo::integer / 100) "
        "    ELSE 'Periodo ' || (codigo_periodo::integer % 100) || ' ' || (codigo_periodo::integer / 100) "
        "  END AS periodo_texto "
        "FROM proj_14353_data.ts_consumos_registrados "
        f"WHERE id_item = '{cid}' "
        "  AND codigo_periodo IS NOT NULL "
        "  AND consumo_registrado IS NOT NULL "
        "ORDER BY codigo_periodo::integer DESC "
        "LIMIT 1;"
    )


def build_latest_invoice_query(contract_id: str) -> str:
    """Build read-only query for the most recent invoice amount (EUR) and billing period."""
    cid = validate_contract_uuid(contract_id)
    return (
        "SELECT "
        "  cod_periodo::text AS cod_periodo, "
        "  importe_factura::double precision AS importe_factura, "
        "  timestamptz AS fecha_factura "
        "FROM proj_14353_data.ts_facturas "
        f"WHERE id_item = '{cid}' "
        "  AND importe_factura IS NOT NULL "
        "ORDER BY timestamptz DESC "
        "LIMIT 1;"
    )
