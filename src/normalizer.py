from pathlib import Path

import pandas as pd

from movement_rules import apply_movement_rules


MASTER_COLUMNS = [
    "CodigoArticulo",
    "Descripcion",
    "Categoria",
    "Marca",
    "CostoUnitario",
    "Estado",
]

STOCK_COLUMNS = [
    "Fecha",
    "CodigoArticulo",
    "Deposito",
    "StockInformado",
]

MOVEMENT_COLUMNS = [
    "Fecha",
    "CodigoArticulo",
    "Descripcion",
    "Deposito",
    "TipoMovimiento",
    "Clasificacion_CLC",
    "CantidadOriginal",
    "CantidadNormalizada",
    "Documento",
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


def normalize_code(value):
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        return text.lstrip("0") or "0"
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
    return normalized[MOVEMENT_COLUMNS + ["AdvertenciaMovimiento"]]


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
    # Todo a texto en columnas object mixtas evita errores de parquet por tipos
    # inconsistentes; el análisis re-parsea fechas y números al leer.
    df.to_parquet(output_path, index=False)
    return output_path
