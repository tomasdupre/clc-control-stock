import pandas as pd


REQUIRED_COLUMNS = {
    "maestro": ["CodigoArticulo"],
    "stock": ["Fecha", "CodigoArticulo", "StockInformado"],
    "movimientos": ["CodigoArticulo", "CantidadOriginal"],
}


def _add_issue(issues, severity, file_type, row_number, field, message):
    issues.append({
        "Severidad": severity,
        "TipoArchivo": file_type,
        "Fila": row_number,
        "Campo": field,
        "Mensaje": message,
    })


def validate_required_columns(df, file_type, issues):
    for column in REQUIRED_COLUMNS.get(file_type, []):
        if column not in df.columns:
            _add_issue(
                issues,
                "Error",
                file_type,
                "",
                column,
                "Columna obligatoria faltante",
            )


def validate_common_fields(df, file_type, issues):
    if "CodigoArticulo" in df.columns:
        empty_mask = df["CodigoArticulo"].isna() | (df["CodigoArticulo"].astype(str).str.strip() == "")
        for index in df[empty_mask].index:
            _add_issue(issues, "Error", file_type, index + 2, "CodigoArticulo", "SKU vacio")

    if "Fecha" in df.columns and file_type != "movimientos":
        empty_mask = df["Fecha"].isna() | (df["Fecha"].astype(str).str.strip() == "")
        for index in df[empty_mask].index:
            _add_issue(issues, "Error", file_type, index + 2, "Fecha", "Fecha vacia o invalida")


def validate_numeric_column(df, file_type, column, issues, empty_message, invalid_message):
    if column not in df.columns:
        return

    empty_mask = df[column].isna() | (df[column].astype(str).str.strip() == "")
    for index in df[empty_mask].index:
        _add_issue(issues, "Error", file_type, index + 2, column, empty_message)

    numeric_values = pd.to_numeric(df[column], errors="coerce")
    invalid_mask = ~empty_mask & numeric_values.isna()
    for index in df[invalid_mask].index:
        _add_issue(issues, "Error", file_type, index + 2, column, invalid_message)


def validate_duplicates(df, file_type, issues):
    duplicate_columns = ["Documento", "CodigoArticulo", "Fecha", "CantidadOriginal"]
    if not all(column in df.columns for column in duplicate_columns):
        return

    duplicates = df.duplicated(subset=duplicate_columns, keep=False)
    for index in df[duplicates].index:
        _add_issue(
            issues,
            "Advertencia",
            file_type,
            index + 2,
            "Documento",
            "Documento duplicado para Documento + CodigoArticulo + Fecha + CantidadOriginal",
        )


def validate_unclassified_movements(df, file_type, issues):
    if file_type != "movimientos":
        return

    if "Clasificacion_CLC" in df.columns:
        empty_mask = df["Clasificacion_CLC"].isna() | (df["Clasificacion_CLC"].astype(str).str.strip() == "")
        for index in df[empty_mask].index:
            _add_issue(
                issues,
                "Advertencia",
                file_type,
                index + 2,
                "TipoMovimiento",
                "Tipo de movimiento no clasificado",
            )

    if "AdvertenciaMovimiento" in df.columns:
        warning_mask = df["AdvertenciaMovimiento"].notna() & (
            df["AdvertenciaMovimiento"].astype(str).str.strip() != ""
        )
        for index in df[warning_mask].index:
            _add_issue(
                issues,
                "Advertencia",
                file_type,
                index + 2,
                "TipoMovimiento",
                df.loc[index, "AdvertenciaMovimiento"],
            )


def validate_products_without_master(df, master_df, file_type, issues):
    if file_type not in {"stock", "movimientos"} or master_df is None:
        return
    if "CodigoArticulo" not in df.columns or "CodigoArticulo" not in master_df.columns:
        return

    master_codes = set(master_df["CodigoArticulo"].dropna().astype(str).str.strip())
    codes = df["CodigoArticulo"].dropna().astype(str).str.strip()
    missing_codes = sorted(set(codes) - master_codes)

    for code in missing_codes:
        _add_issue(
            issues,
            "Advertencia",
            file_type,
            "",
            "CodigoArticulo",
            f"Producto sin maestro: {code}",
        )


def validate_dataframe(df, file_type, master_df=None):
    """Ejecuta todas las validaciones aplicables y devuelve un DataFrame."""
    issues = []
    validate_required_columns(df, file_type, issues)
    validate_common_fields(df, file_type, issues)

    if file_type == "stock":
        validate_numeric_column(
            df,
            file_type,
            "StockInformado",
            issues,
            "StockInformado vacio",
            "StockInformado no numerico",
        )

    if file_type == "movimientos":
        validate_numeric_column(
            df,
            file_type,
            "CantidadOriginal",
            issues,
            "Cantidad vacia",
            "Cantidad no numerica",
        )
        validate_duplicates(df, file_type, issues)
        # TipoMovimiento es informativo. No debe bloquear ni ensuciar el control:
        # la cantidad ya viene neta y se respeta tal como la manda el cliente.

    validate_products_without_master(df, master_df, file_type, issues)
    return pd.DataFrame(issues)
