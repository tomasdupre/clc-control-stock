from pathlib import Path

import pandas as pd

from movement_rules import apply_movement_rules


# Campos que se mapean por tipo de hoja (alineado con la app):
# maestro = código + descripción; stock = fecha + código + stock; movimientos = fecha + código + cantidad.
MASTER_COLUMNS = [
    "CodigoArticulo",
    "Descripcion",
    "Categoria",
]

STOCK_COLUMNS = [
    "Fecha",
    "CodigoArticulo",
    "StockInformado",
]

MOVEMENT_COLUMNS = [
    "Fecha",
    "CodigoArticulo",
    "Descripcion",
    "CantidadOriginal",
    "TipoMovimiento",
]


def parse_date_series(series):
    text_values = series.astype(str).str.strip()
    year_first_mask = text_values.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", na=False)

    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if year_first_mask.any():
        parsed.loc[year_first_mask] = pd.to_datetime(
            series.loc[year_first_mask],
            errors="coerce",
            dayfirst=False,
        )

    regular_mask = ~year_first_mask
    if regular_mask.any():
        parsed.loc[regular_mask] = pd.to_datetime(
            series.loc[regular_mask],
            errors="coerce",
            dayfirst=True,
        )

    fallback_mask = parsed.isna() & series.notna() & (text_values != "")
    if fallback_mask.any():
        parsed.loc[fallback_mask] = pd.to_datetime(
            series.loc[fallback_mask],
            errors="coerce",
            dayfirst=False,
        )
    numeric_values = pd.to_numeric(series, errors="coerce")
    numeric_mask = parsed.isna() & numeric_values.between(20000, 80000)
    if numeric_mask.any():
        parsed.loc[numeric_mask] = pd.to_datetime(
            numeric_values.loc[numeric_mask],
            unit="D",
            origin="1899-12-30",
            errors="coerce",
        )
    return parsed.dt.date


import re as _re

# Patrones que indican filas de pie de página / totales, no datos reales.
_FOOTER_PATTERNS = _re.compile(
    r"^\s*("
    r"recuento\s*=|suma\s*=|total\s*=|count\s*=|sum\s*=|subtotal\s*=|"
    r"recuento:|suma:|total:|grand total"
    r")",
    flags=_re.IGNORECASE,
)


def detect_footer_rows(df, scan_tail=10):
    """
    Detecta filas al final del DataFrame que parecen ser notas/totales del sistema
    y no datos reales (ej. "Recuento=201518", "Suma=141169").

    Estrategia: revisa las últimas `scan_tail` filas buscando:
      1. Filas donde >70% de las celdas están vacías/NaN Y al menos una celda
         contiene un patrón de total/recuento.
      2. O donde cualquier celda encaja directamente con _FOOTER_PATTERNS.

    Devuelve una lista de dicts con info de las filas sospechosas:
      [{"idx": índice_original, "contenido": str_de_la_fila, "motivo": str}, ...]
    """
    if df.empty:
        return []

    tail = df.tail(scan_tail)
    sospechosas = []

    for idx, row in tail.iterrows():
        celdas = [str(v).strip() for v in row if pd.notna(v) and str(v).strip() not in ("", "nan")]
        if not celdas:
            continue

        # ¿Alguna celda tiene patrón de total/recuento?
        tiene_patron = any(_FOOTER_PATTERNS.search(c) for c in celdas)

        # ¿La fila está mayoritariamente vacía?
        total_cols = len(row)
        vacias = total_cols - len(celdas)
        mayoria_vacia = (vacias / total_cols) > 0.7 if total_cols > 1 else False

        if tiene_patron or (mayoria_vacia and len(celdas) <= 2):
            motivo = "patrón de total/recuento detectado" if tiene_patron else "fila casi vacía al final"
            sospechosas.append({
                "idx": idx,
                "contenido": " | ".join(celdas[:5]),
                "motivo": motivo,
            })

    return sospechosas


def normalize_code(value):
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        # NO se quitan los ceros a la izquierda: son significativos y pueden
        # diferenciar un producto de otro (ej. 0658325663198 != 658325663198).
        return text
    return text.upper()


def _empty_standard_dataframe(row_count, standard_columns):
    return pd.DataFrame({column: [""] * row_count for column in standard_columns})


def _apply_mapping(df, mapping, standard_columns):
    """
    Crea un DataFrame con columnas estandar.

    No inventa datos: si una columna no existe en el origen, queda vacia.
    """
    normalized = _empty_standard_dataframe(len(df), standard_columns)

    for original_column, standard_column in mapping.items():
        if original_column in df.columns and standard_column in normalized.columns:
            normalized[standard_column] = df[original_column]

    return normalized


def normalize_master(df, mapping):
    normalized = _apply_mapping(df, mapping, MASTER_COLUMNS)
    normalized["CodigoArticulo"] = normalized["CodigoArticulo"].apply(normalize_code)
    return normalized


def normalize_stock(df, mapping):
    normalized = _apply_mapping(df, mapping, STOCK_COLUMNS)
    normalized["Fecha"] = parse_date_series(normalized["Fecha"])
    normalized["CodigoArticulo"] = normalized["CodigoArticulo"].apply(normalize_code)
    normalized["StockInformado"] = pd.to_numeric(normalized["StockInformado"], errors="coerce")
    return normalized


def normalize_movements(df, mapping, rules_path):
    normalized = _apply_mapping(df, mapping, MOVEMENT_COLUMNS)
    normalized["Fecha"] = parse_date_series(normalized["Fecha"])
    normalized["CodigoArticulo"] = normalized["CodigoArticulo"].apply(normalize_code)
    normalized["CantidadOriginal"] = pd.to_numeric(normalized["CantidadOriginal"], errors="coerce")
    normalized = apply_movement_rules(normalized, rules_path)
    # Salida: lo mapeado + columnas derivadas (cantidad normalizada y advertencias).
    salida = MOVEMENT_COLUMNS + ["CantidadNormalizada", "Clasificacion_CLC", "AdvertenciaMovimiento"]
    return normalized[[c for c in salida if c in normalized.columns]]


NORMALIZED_FILE_NAMES = {
    "maestro": "maestro_normalizado.parquet",
    "stock": "stock_normalizado.parquet",
    "movimientos": "movimientos_normalizado.parquet",
}


def export_normalized(df, file_type, output_dir):
    """
    Guarda el normalizado en parquet (binario, rápido y preserva tipos).

    Antes era .xlsx, pero escribir cientos de miles de filas con openpyxl tardaba
    decenas de segundos. Parquet escribe lo mismo en menos de un segundo. El análisis
    lee estos archivos directamente; para Excel/Power BI hay exportación a demanda.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / NORMALIZED_FILE_NAMES[file_type]

    # Las columnas de texto/identificador (dtype object) pueden traer tipos
    # mezclados: por ejemplo depositos que son numeros ('5') y otros que son
    # texto ('1DA00701'), o fechas guardadas como objetos date. Parquet necesita
    # un tipo unico por columna; si no, falla al inferirlo. Las pasamos todas a
    # texto. Las columnas numericas (StockInformado, cantidades) quedan como estan.
    # El analisis re-parsea fechas y numeros al leer, asi que esto no afecta el calculo.
    safe = df.copy()
    for col in safe.columns:
        if safe[col].dtype == object:
            safe[col] = safe[col].fillna("").astype(str)

    safe.to_parquet(output_path, index=False)
    return output_path
