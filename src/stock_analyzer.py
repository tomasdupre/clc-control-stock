from pathlib import Path

import numpy as np
import pandas as pd


STOCK_FILE = "stock_normalizado.parquet"
MOVEMENTS_FILE = "movimientos_normalizado.parquet"
MASTER_FILE = "maestro_normalizado.parquet"

STOCK_REQUIRED_COLUMNS = ["Fecha", "CodigoArticulo", "Deposito", "StockInformado"]
MOVEMENT_REQUIRED_COLUMNS = ["CodigoArticulo"]
MASTER_REQUIRED_COLUMNS = ["CodigoArticulo", "Descripcion"]


def normalize_code(value):
    """Normaliza codigos para evitar diferencias como 123 vs 123.0."""
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    if text.isdigit():
        # NO se quitan los ceros a la izquierda: son significativos y pueden
        # diferenciar un producto de otro (ej. 0658325663198 != 658325663198).
        return text
    return text.upper()


def normalize_deposit(value):
    if pd.isna(value):
        return "Sin deposito"
    text = str(value).strip()
    if not text:
        return "Sin deposito"
    return text.upper()


def parse_date_series(series):
    """
    Convierte fechas con reglas tolerantes.

    Soporta fechas reales de Excel, textos tipo dia/mes/anio y seriales numericos
    de Excel. Devuelve valores date o NaT.
    """
    original = series.copy()
    text_values = original.astype(str).str.strip()
    year_first_mask = text_values.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", na=False)

    parsed = pd.Series(pd.NaT, index=original.index, dtype="datetime64[ns]")
    if year_first_mask.any():
        parsed.loc[year_first_mask] = pd.to_datetime(
            original.loc[year_first_mask],
            errors="coerce",
            dayfirst=False,
        )

    regular_mask = ~year_first_mask
    if regular_mask.any():
        parsed.loc[regular_mask] = pd.to_datetime(
            original.loc[regular_mask],
            errors="coerce",
            dayfirst=True,
        )

    fallback_mask = parsed.isna() & original.notna() & (text_values != "")
    if fallback_mask.any():
        parsed.loc[fallback_mask] = pd.to_datetime(
            original.loc[fallback_mask],
            errors="coerce",
            dayfirst=False,
        )

    numeric_values = pd.to_numeric(original, errors="coerce")
    numeric_mask = parsed.isna() & numeric_values.between(20000, 80000)
    if numeric_mask.any():
        parsed.loc[numeric_mask] = pd.to_datetime(
            numeric_values.loc[numeric_mask],
            unit="D",
            origin="1899-12-30",
            errors="coerce",
        )

    return parsed.dt.date


def read_normalized_files(normalized_dir):
    """Lee los tres archivos normalizados que alimentan el control de stock."""
    normalized_dir = Path(normalized_dir)
    paths = {
        "stock": normalized_dir / STOCK_FILE,
        "movimientos": normalized_dir / MOVEMENTS_FILE,
        "maestro": normalized_dir / MASTER_FILE,
    }

    missing_files = [str(path) for path in paths.values() if not path.exists()]
    if missing_files:
        raise FileNotFoundError("Faltan archivos normalizados: " + ", ".join(missing_files))

    return {
        "stock": pd.read_parquet(paths["stock"]),
        "movimientos": pd.read_parquet(paths["movimientos"]),
        "maestro": pd.read_parquet(paths["maestro"]),
    }


def add_error(errors, table_name, row_number, field, message):
    errors.append({
        "Tabla": table_name,
        "Fila": row_number,
        "Campo": field,
        "Mensaje": message,
    })


def validate_required_columns(df, table_name, required_columns, errors):
    for column in required_columns:
        if column not in df.columns:
            add_error(errors, table_name, "", column, "Columna obligatoria faltante")


def ensure_columns(df, required_columns):
    result = df.copy()
    for column in required_columns:
        if column not in result.columns:
            result[column] = ""
    return result


def prepare_common_columns(df):
    """Normaliza campos comunes sin descartar filas."""
    result = df.copy()

    if "Deposito" not in result.columns:
        result["Deposito"] = "Sin deposito"
    result["Deposito"] = result["Deposito"].apply(normalize_deposit)

    if "CodigoArticulo" in result.columns:
        result["CodigoArticulo"] = result["CodigoArticulo"].apply(normalize_code)

    return result


def prepare_stock(stock_df, errors):
    stock = prepare_common_columns(stock_df)
    validate_required_columns(stock, "stock", STOCK_REQUIRED_COLUMNS, errors)
    stock = ensure_columns(stock, STOCK_REQUIRED_COLUMNS)

    if "Fecha" in stock.columns:
        original_dates = stock["Fecha"].copy()
        stock["Fecha"] = parse_date_series(stock["Fecha"])
        invalid_dates = stock["Fecha"].isna() & original_dates.notna() & (original_dates.astype(str).str.strip() != "")
        for index in stock[invalid_dates].index:
            add_error(errors, "stock", index + 2, "Fecha", "Fecha invalida")

    if "StockInformado" in stock.columns:
        stock["StockInformado"] = pd.to_numeric(stock["StockInformado"], errors="coerce")
        for index in stock[stock["StockInformado"].isna()].index:
            add_error(errors, "stock", index + 2, "StockInformado", "StockInformado vacio o no numerico")

    return stock


def prepare_movements(movements_df, errors):
    movements = prepare_common_columns(movements_df)
    validate_required_columns(movements, "movimientos", MOVEMENT_REQUIRED_COLUMNS, errors)
    movements = ensure_columns(movements, MOVEMENT_REQUIRED_COLUMNS)
    if "CantidadOriginal" not in movements.columns and "CantidadNormalizada" not in movements.columns:
        add_error(
            errors,
            "movimientos",
            "",
            "CantidadOriginal",
            "Debe existir CantidadOriginal o CantidadNormalizada",
        )
        movements["CantidadNormalizada"] = np.nan
    if "Fecha" not in movements.columns:
        movements["Fecha"] = ""

    if "Fecha" in movements.columns:
        original_dates = movements["Fecha"].copy()
        movements["Fecha"] = parse_date_series(movements["Fecha"])
        invalid_dates = movements["Fecha"].isna() & original_dates.notna() & (original_dates.astype(str).str.strip() != "")
        for index in movements[invalid_dates].index:
            add_error(errors, "movimientos", index + 2, "Fecha", "Fecha invalida")

    # Regla de integridad: el calculo usa la cantidad informada TAL CUAL.
    # Si existe CantidadOriginal, esa es la fuente de verdad: nunca se le cambia
    # el signo. Asi, datos normalizados de versiones anteriores (que podian tener
    # un CantidadNormalizada con el signo invertido) se corrigen al analizar, sin
    # necesidad de volver a normalizar.
    if "CantidadOriginal" in movements.columns:
        original = pd.to_numeric(movements["CantidadOriginal"], errors="coerce")
        previa = pd.to_numeric(movements.get("CantidadNormalizada"), errors="coerce")
        movements["CantidadNormalizada"] = original.where(original.notna(), previa)
    elif "CantidadNormalizada" in movements.columns:
        movements["CantidadNormalizada"] = pd.to_numeric(movements["CantidadNormalizada"], errors="coerce")

    if "CantidadNormalizada" in movements.columns:
        for index in movements[movements["CantidadNormalizada"].isna()].index:
            add_error(
                errors,
                "movimientos",
                index + 2,
                "CantidadNormalizada",
                "CantidadNormalizada vacia o no numerica",
            )

    return movements


def prepare_master(master_df, errors):
    master = master_df.copy()
    validate_required_columns(master, "maestro", MASTER_REQUIRED_COLUMNS, errors)
    master = ensure_columns(master, MASTER_REQUIRED_COLUMNS)

    master["CodigoArticulo"] = master["CodigoArticulo"].apply(normalize_code)
    master["Descripcion"] = master["Descripcion"].fillna("").astype(str).str.strip()
    return master


def aggregate_without_deposit(stock_df, movements_df):
    """Agrupa stock y movimientos por SKU ignorando deposito."""
    stock = stock_df.copy()
    movements = movements_df.copy()

    stock["Deposito"] = "TODOS"
    movements["Deposito"] = "TODOS"

    stock_group_columns = ["Fecha", "CodigoArticulo", "Deposito"]
    stock = (
        stock.groupby(stock_group_columns, dropna=False, as_index=False)
        .agg({"StockInformado": "sum"})
    )

    movement_group_columns = ["Fecha", "CodigoArticulo", "Deposito"]
    extra_columns = [
        column
        for column in ["ArchivoOrigen", "HojaOrigen", "TipoMovimiento", "Clasificacion_CLC"]
        if column in movements.columns
    ]
    movements_aggregated = (
        movements.groupby(movement_group_columns, dropna=False, as_index=False)
        .agg({"CantidadNormalizada": "sum"})
    )
    for column in extra_columns:
        movements_aggregated[column] = "Agrupado sin deposito"

    return stock, movements_aggregated


def get_description_lookup(master_df):
    valid_master = master_df[master_df["CodigoArticulo"] != ""].copy()
    valid_master = valid_master.drop_duplicates(subset=["CodigoArticulo"], keep="first")
    return dict(zip(valid_master["CodigoArticulo"], valid_master["Descripcion"]))


def build_description_lookup(*dfs):
    """
    Arma CodigoArticulo -> Descripcion combinando varias fuentes (maestro, stock,
    movimientos). La primera fuente que aporte una descripcion no vacia para un codigo
    es la que gana. Asi, si el maestro no tiene un codigo (por ejemplo porque usa otro
    sistema de codigos), igual se toma la descripcion del stock o de los movimientos.
    """
    lookup = {}
    for df in dfs:
        if df is None or getattr(df, "empty", True):
            continue
        if "CodigoArticulo" not in df.columns or "Descripcion" not in df.columns:
            continue
        codigos = df["CodigoArticulo"].astype(str)
        descripciones = df["Descripcion"].fillna("").astype(str).str.strip()
        for cod, desc in zip(codigos, descripciones):
            if cod and desc and cod not in lookup:
                lookup[cod] = desc
    return lookup


def get_control_initial_date(stock_df):
    valid_dates = stock_df["Fecha"].dropna() if "Fecha" in stock_df.columns else pd.Series(dtype=object)
    if valid_dates.empty:
        return None
    return valid_dates.min()


def get_control_final_date(stock_df):
    """Última fecha de stock informada (la 'foto final' del período)."""
    valid_dates = stock_df["Fecha"].dropna() if "Fecha" in stock_df.columns else pd.Series(dtype=object)
    if valid_dates.empty:
        return None
    return valid_dates.max()


def add_missing_final_as_zero(stock_df, final_date):
    """
    Agrega filas de control sintéticas para productos que NO aparecen en la última
    foto de stock (la final). Asume StockInformado = 0 en la fecha final.

    Sirve para controlar productos que el cliente deja de listar cuando llegan a 0:
    si StockInicial + Movimientos cerró en 0, da OK; si no, marca la diferencia.
    Las filas sintéticas quedan marcadas con StockFinalAsumidoCero = True.
    """
    if stock_df.empty or final_date is None:
        return stock_df

    work = stock_df.dropna(subset=["Fecha"])
    if work.empty:
        return stock_df

    ultima_fecha = work.groupby(["CodigoArticulo", "Deposito"], as_index=False)["Fecha"].max()
    faltan = ultima_fecha[ultima_fecha["Fecha"] < final_date]
    if faltan.empty:
        return stock_df

    nuevos = pd.DataFrame({
        "CodigoArticulo": faltan["CodigoArticulo"].values,
        "Deposito": faltan["Deposito"].values,
        "Fecha": final_date,
        "StockInformado": 0,
        "StockFinalAsumidoCero": True,
    })
    result = pd.concat([stock_df, nuevos], ignore_index=True)
    result["StockFinalAsumidoCero"] = result.get(
        "StockFinalAsumidoCero", pd.Series(False, index=result.index)
    ).fillna(False)
    return result


def calculate_initial_stock(stock_df, required_initial_date=None):
    """
    Obtiene el stock inicial por CodigoArticulo + Deposito.

    Si required_initial_date viene informado, solo considera como stock inicial
    los registros de esa fecha. Esto evita tratar un stock final como inicial
    cuando el SKU no tenia stock inicial informado.
    """
    valid_stock = stock_df.dropna(subset=["Fecha"]).copy()
    if required_initial_date is not None:
        valid_stock = valid_stock[valid_stock["Fecha"] == required_initial_date].copy()

    valid_stock = valid_stock.sort_values(["CodigoArticulo", "Deposito", "Fecha"])
    initial = valid_stock.groupby(["CodigoArticulo", "Deposito"], as_index=False).first()
    initial = initial[["CodigoArticulo", "Deposito", "Fecha", "StockInformado"]]
    initial = initial.rename(columns={
        "Fecha": "FechaInicial",
        "StockInformado": "StockInicial",
    })
    return initial


def calculate_movement_totals_for_controls(
    control_points,
    movements_df,
    include_initial_date_movements=False,
):
    """
    Calcula movimientos netos por punto de control sin cruzar todas las filas.

    La regla de negocio sigue siendo:
    StockCalculado = StockInicial + movimientos netos del mismo producto.
    """
    result = control_points.copy()
    result["MovimientosAcumulados"] = 0.0

    if result.empty or movements_df.empty:
        return result[["__control_id", "MovimientosAcumulados"]]

    required_columns = {"CodigoArticulo", "Deposito", "CantidadNormalizada"}
    if not required_columns.issubset(movements_df.columns):
        return result[["__control_id", "MovimientosAcumulados"]]

    controls = result.copy()
    controls["FechaControlDt"] = pd.to_datetime(controls["FechaControl"], errors="coerce")
    controls["FechaInicialDt"] = pd.to_datetime(controls["FechaInicial"], errors="coerce")
    controls = controls.dropna(subset=["FechaControlDt", "FechaInicialDt"])
    if controls.empty:
        return result[["__control_id", "MovimientosAcumulados"]]

    movements = movements_df.dropna(subset=["CantidadNormalizada"]).copy()
    if "Fecha" not in movements.columns:
        movements["Fecha"] = ""
    movements["FechaDt"] = pd.to_datetime(movements["Fecha"], errors="coerce")
    movements["CantidadNormalizada"] = pd.to_numeric(
        movements["CantidadNormalizada"],
        errors="coerce",
    )
    movements = movements.dropna(subset=["CantidadNormalizada"])
    if movements.empty:
        return result[["__control_id", "MovimientosAcumulados"]]

    undated_movements = movements[movements["FechaDt"].isna()].copy()
    movements = movements[movements["FechaDt"].notna()].copy()

    result = result.set_index("__control_id")

    if not undated_movements.empty:
        undated_totals = (
            undated_movements.groupby(["CodigoArticulo", "Deposito"], dropna=False)["CantidadNormalizada"]
            .sum()
            .reset_index()
        )
        for _, row in undated_totals.iterrows():
            control_group = controls[
                (controls["CodigoArticulo"] == row["CodigoArticulo"])
                & (controls["Deposito"] == row["Deposito"])
                & (controls["FechaControlDt"] > controls["FechaInicialDt"])
            ]
            if not control_group.empty:
                result.loc[
                    control_group["__control_id"].to_numpy(),
                    "MovimientosAcumulados",
                ] += row["CantidadNormalizada"]

    if movements.empty:
        return result.reset_index()[["__control_id", "MovimientosAcumulados"]]

    movements_daily = (
        movements.groupby(
            ["CodigoArticulo", "Deposito", "FechaDt"],
            dropna=False,
            as_index=False,
        )["CantidadNormalizada"]
        .sum()
        .sort_values(["CodigoArticulo", "Deposito", "FechaDt"])
    )

    for key, movement_group in movements_daily.groupby(
        ["CodigoArticulo", "Deposito"],
        dropna=False,
        sort=False,
    ):
        code, deposit = key
        control_group = controls[
            (controls["CodigoArticulo"] == code)
            & (controls["Deposito"] == deposit)
        ]
        if control_group.empty:
            continue

        movement_dates = movement_group["FechaDt"].to_numpy(dtype="datetime64[ns]")
        cumulative_quantities = movement_group["CantidadNormalizada"].cumsum().to_numpy(dtype=float)

        control_dates = control_group["FechaControlDt"].to_numpy(dtype="datetime64[ns]")
        initial_dates = control_group["FechaInicialDt"].to_numpy(dtype="datetime64[ns]")

        control_positions = np.searchsorted(movement_dates, control_dates, side="right") - 1
        control_totals = np.where(
            control_positions >= 0,
            cumulative_quantities[control_positions],
            0.0,
        )

        initial_side = "left" if include_initial_date_movements else "right"
        initial_positions = np.searchsorted(movement_dates, initial_dates, side=initial_side) - 1
        initial_totals = np.where(
            initial_positions >= 0,
            cumulative_quantities[initial_positions],
            0.0,
        )

        result.loc[
            control_group["__control_id"].to_numpy(),
            "MovimientosAcumulados",
        ] += control_totals - initial_totals

    return result.reset_index()[["__control_id", "MovimientosAcumulados"]]


def calculate_control_table(
    stock_df,
    movements_df,
    master_df,
    include_initial_date_movements=False,
    required_initial_date=None,
    description_lookup=None,
):
    """
    Calcula StockCalculado y Diferencia para cada stock informado.

    StockCalculado = StockInicial + MovimientosAcumulados.
    Los movimientos acumulados se calculan por CodigoArticulo + Deposito,
    sin unir cada movimiento contra cada punto de control.
    """
    if description_lookup is None:
        description_lookup = get_description_lookup(master_df)
    initial_stock = calculate_initial_stock(stock_df, required_initial_date=required_initial_date)

    swi = stock_df.merge(initial_stock, on=["CodigoArticulo", "Deposito"], how="left")
    swi["__control_id"] = np.arange(len(swi))

    if required_initial_date is not None:
        swi["FechaInicial"] = swi["FechaInicial"].fillna(required_initial_date)
        swi["StockInicial"] = swi["StockInicial"].fillna(0)

    swi["FechaInicialEsperada"] = required_initial_date

    # Máscara de filas calculables
    can_calc = (
        swi["Fecha"].notna()
        & swi["FechaInicial"].notna()
        & swi["StockInicial"].notna()
        & swi["StockInformado"].notna()
    )

    # Puntos de control con fechas validas para calcular movimientos netos.
    control_keys = (
        swi.loc[can_calc, ["__control_id", "CodigoArticulo", "Deposito", "Fecha", "FechaInicial"]]
        .rename(columns={"Fecha": "FechaControl"})
    )

    swi["MovimientosAcumulados"] = np.nan

    if not control_keys.empty:
        movement_totals = calculate_movement_totals_for_controls(
            control_keys,
            movements_df,
            include_initial_date_movements=include_initial_date_movements,
        )
        if not movement_totals.empty:
            totals = movement_totals.set_index("__control_id")["MovimientosAcumulados"]
            swi.loc[totals.index, "MovimientosAcumulados"] = totals

        # Filas calculables sin movimientos: 0 en lugar de NaN
        swi.loc[can_calc & swi["MovimientosAcumulados"].isna(), "MovimientosAcumulados"] = 0.0

    swi["StockCalculado"] = np.where(
        can_calc,
        swi["StockInicial"] + swi["MovimientosAcumulados"].fillna(0),
        np.nan,
    )
    swi["Diferencia"] = np.where(
        can_calc,
        swi["StockInformado"] - swi["StockCalculado"],
        np.nan,
    )
    swi["DiferenciaAbsoluta"] = swi["Diferencia"].abs()
    swi["EstadoControl"] = np.select(
        [
            swi["Diferencia"].isna(),
            swi["DiferenciaAbsoluta"] == 0,
            swi["DiferenciaAbsoluta"] <= 2,
        ],
        ["Sin datos", "OK", "Revisar"],
        default="Critico",
    )
    swi["Descripcion"] = swi["CodigoArticulo"].map(description_lookup).fillna("")
    if "StockFinalAsumidoCero" not in swi.columns:
        swi["StockFinalAsumidoCero"] = False
    swi["StockFinalAsumidoCero"] = swi["StockFinalAsumidoCero"].fillna(False)

    warnings_list = [
        {
            "CodigoArticulo": row["CodigoArticulo"],
            "Deposito": row["Deposito"],
            "Fecha": row["Fecha"],
            "Mensaje": "No se pudo calcular por fecha o stock informado faltante",
        }
        for _, row in swi[~can_calc].iterrows()
    ]

    output_columns = [
        "Fecha", "CodigoArticulo", "Descripcion", "Deposito",
        "FechaInicialEsperada", "FechaInicial", "StockInicial",
        "MovimientosAcumulados", "StockCalculado", "StockInformado",
        "Diferencia", "DiferenciaAbsoluta", "EstadoControl",
        "StockFinalAsumidoCero",
    ]
    return swi[output_columns].reset_index(drop=True), pd.DataFrame(warnings_list)


def get_movements_without_stock(movements_df, stock_df):
    """Detecta movimientos cuyo CodigoArticulo + Deposito no aparece en stock."""
    stock_keys = set(zip(stock_df["CodigoArticulo"], stock_df["Deposito"]))
    mask = ~movements_df.apply(
        lambda row: (row["CodigoArticulo"], row["Deposito"]) in stock_keys,
        axis=1,
    )
    return movements_df[mask].copy()


def classify_movements_application(
    movements_df,
    stock_df,
    include_initial_date_movements=False,
    required_initial_date=None,
):
    """Clasifica movimientos aplicados/no aplicados contra el rango controlable."""
    initial_stock = calculate_initial_stock(stock_df, required_initial_date=required_initial_date)
    if stock_df.empty:
        result = movements_df.copy()
        result["MotivoNoAplicado"] = "No existe stock informado"
        return pd.DataFrame(), result

    stock_ranges = (
        stock_df.dropna(subset=["Fecha"])
        .groupby(["CodigoArticulo", "Deposito"], as_index=False)
        .agg(FechaMaximaControl=("Fecha", "max"))
    )
    stock_ranges = stock_ranges.merge(
        initial_stock[["CodigoArticulo", "Deposito", "FechaInicial"]],
        on=["CodigoArticulo", "Deposito"],
        how="left",
    )
    if required_initial_date is not None:
        stock_ranges["FechaInicial"] = stock_ranges["FechaInicial"].fillna(required_initial_date)

    reviewed = movements_df.merge(
        stock_ranges,
        on=["CodigoArticulo", "Deposito"],
        how="left",
    )

    fecha = pd.to_datetime(reviewed["Fecha"], errors="coerce")
    cantidad = pd.to_numeric(reviewed["CantidadNormalizada"], errors="coerce")
    fecha_inicial = pd.to_datetime(reviewed["FechaInicial"], errors="coerce")
    fecha_max = pd.to_datetime(reviewed["FechaMaximaControl"], errors="coerce")

    if include_initial_date_movements:
        before_initial = fecha.notna() & fecha_inicial.notna() & (fecha < fecha_inicial)
        before_initial_msg = "Movimiento anterior a la fecha inicial; no se suma al stock calculado"
    else:
        before_initial = fecha.notna() & fecha_inicial.notna() & (fecha <= fecha_inicial)
        before_initial_msg = "Movimiento en fecha inicial o anterior; no se suma al stock calculado"

    after_last_control = fecha.notna() & fecha_max.notna() & (fecha > fecha_max)

    conditions = [
        cantidad.isna(),
        fecha_inicial.isna(),
        before_initial.fillna(False),
        after_last_control.fillna(False),
    ]
    messages = [
        "CantidadNormalizada invalida o vacia",
        "No existe stock informado para ese CodigoArticulo/Deposito",
        before_initial_msg,
        "Movimiento posterior a la ultima fecha de stock informado",
    ]
    reviewed["MotivoNoAplicado"] = np.select(conditions, messages, default="")
    applied = reviewed[reviewed["MotivoNoAplicado"] == ""].copy()
    unapplied = reviewed[reviewed["MotivoNoAplicado"] != ""].copy()
    return applied, unapplied


def build_summary(control_df):
    if control_df.empty:
        return pd.DataFrame([{
            "total_registros_controlados": 0,
            "total_registros_ok": 0,
            "total_registros_con_diferencia": 0,
            "total_diferencia_absoluta": 0,
            "cantidad_skus_con_diferencia": 0,
            "cantidad_depositos_con_diferencia": 0,
        }])

    differences = control_df[pd.to_numeric(control_df["Diferencia"], errors="coerce").fillna(0) != 0]
    return pd.DataFrame([{
        "total_registros_controlados": len(control_df),
        "total_registros_ok": int((control_df["EstadoControl"] == "OK").sum()),
        "total_registros_con_diferencia": len(differences),
        "total_diferencia_absoluta": pd.to_numeric(
            control_df["DiferenciaAbsoluta"],
            errors="coerce",
        ).fillna(0).sum(),
        "cantidad_skus_con_diferencia": differences["CodigoArticulo"].nunique(),
        "cantidad_depositos_con_diferencia": differences["Deposito"].nunique(),
    }])


def detect_possible_duplicates(movements_df):
    """
    DETECTA posibles movimientos duplicados, pero NO elimina ninguno.

    Regla de integridad: el sistema nunca borra movimientos. Esta funcion solo
    marca filas que comparten Documento + CodigoArticulo + Fecha + CantidadNormalizada
    para que una persona las revise. Todos los movimientos siguen entrando al calculo
    tal cual los informa el cliente (dos lineas identicas en un mismo documento suelen
    ser dos unidades reales, no un error).

    Devuelve un DataFrame con las filas sospechosas (informativo), sin modificar nada.
    """
    duplicate_columns = ["Documento", "CodigoArticulo", "Fecha", "CantidadNormalizada"]
    missing = [c for c in duplicate_columns if c not in movements_df.columns]
    if missing:
        return pd.DataFrame()

    work = movements_df.copy()
    for col in duplicate_columns:
        work[col] = work[col].fillna("").astype(str).str.strip()

    evaluable_mask = work[duplicate_columns].ne("").all(axis=1)
    evaluable = work[evaluable_mask]
    duplicate_mask = evaluable.duplicated(subset=duplicate_columns, keep=False)
    sospechosos = evaluable[duplicate_mask].index
    if len(sospechosos) == 0:
        return pd.DataFrame()
    return movements_df.loc[sospechosos].copy()


def summarize_applied_movements(applied_movements_df):
    if applied_movements_df is None or applied_movements_df.empty:
        return pd.DataFrame({"Mensaje": ["No hay movimientos aplicados"]})

    group_columns = ["CodigoArticulo", "Deposito"]
    summary = (
        applied_movements_df.groupby(group_columns, dropna=False)
        .agg(
            FechaMinima=("Fecha", "min"),
            FechaMaxima=("Fecha", "max"),
            CantidadMovimientos=("CantidadNormalizada", "count"),
            SumaMovimientos=("CantidadNormalizada", "sum"),
        )
        .reset_index()
    )
    return summary


def export_control_report(
    control_df,
    movements_without_stock_df,
    applied_movements_df,
    unapplied_movements_df,
    movement_duplicates_df,
    errors_df,
    warnings_df,
    output_path,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    differences_df = control_df[pd.to_numeric(control_df["Diferencia"], errors="coerce").fillna(0) != 0].copy()
    summary_df = build_summary(control_df)
    applied_movements_summary_df = summarize_applied_movements(applied_movements_df)

    # xlsxwriter es bastante más rápido que openpyxl para escribir hojas grandes
    # (como movimientos_no_aplicados). Si no está instalado, se usa openpyxl.
    try:
        import xlsxwriter  # noqa: F401
        engine = "xlsxwriter"
    except ImportError:
        engine = "openpyxl"

    with pd.ExcelWriter(output_path, engine=engine) as writer:
        control_df.to_excel(writer, sheet_name="control_stock", index=False)
        differences_df.to_excel(writer, sheet_name="solo_diferencias", index=False)
        summary_df.to_excel(writer, sheet_name="resumen", index=False)
        movements_without_stock_df.to_excel(writer, sheet_name="movimientos_sin_stock", index=False)
        applied_movements_summary_df.to_excel(writer, sheet_name="movimientos_aplicados", index=False)
        unapplied_movements_df.to_excel(writer, sheet_name="movimientos_no_aplicados", index=False)
        movement_duplicates_df.to_excel(writer, sheet_name="duplicados_movimientos", index=False)
        errors_df.to_excel(writer, sheet_name="errores", index=False)
        warnings_df.to_excel(writer, sheet_name="advertencias", index=False)

    return output_path, summary_df


def run_stock_analysis(
    normalized_dir,
    reports_dir,
    output_file_name="control_stock_resultado.xlsx",
    use_deposit=True,
    include_initial_date_movements=False,
    assume_missing_final_zero=False,
):
    """Funcion principal del modulo: lee, calcula y exporta el control."""
    errors = []
    data = read_normalized_files(normalized_dir)

    stock_df = prepare_stock(data["stock"], errors)
    movements_df = prepare_movements(data["movimientos"], errors)
    master_df = prepare_master(data["maestro"], errors)

    # Regla de integridad: NO se elimina ningun movimiento. Solo se detectan
    # posibles duplicados para informar al usuario; todos entran al calculo tal cual.
    possible_duplicates_df = detect_possible_duplicates(movements_df)

    movements_for_audit_df = movements_df.copy()
    # Guardamos las descripciones del stock ANTES de agrupar (la agrupacion las descarta).
    stock_desc_source = stock_df.copy()

    if not use_deposit:
        stock_df, movements_df = aggregate_without_deposit(stock_df, movements_df)
        movements_for_audit_df["Deposito"] = "TODOS"

    # Productos sin foto en el stock final: controlarlos asumiendo stock final = 0.
    if assume_missing_final_zero:
        final_date = get_control_final_date(stock_df)
        stock_df = add_missing_final_as_zero(stock_df, final_date)

    required_initial_date = get_control_initial_date(stock_df)

    # Descripcion combinada: maestro primero; si un codigo no esta ahi (porque el
    # maestro usa otro sistema de codigos), se toma del stock o de los movimientos.
    description_lookup = build_description_lookup(master_df, stock_desc_source, movements_for_audit_df)

    control_df, calculation_warnings_df = calculate_control_table(
        stock_df,
        movements_df,
        master_df,
        include_initial_date_movements=include_initial_date_movements,
        required_initial_date=required_initial_date,
        description_lookup=description_lookup,
    )
    movements_without_stock_df = get_movements_without_stock(movements_for_audit_df, stock_df)
    applied_movements_df, unapplied_movements_df = classify_movements_application(
        movements_for_audit_df,
        stock_df,
        include_initial_date_movements=include_initial_date_movements,
        required_initial_date=required_initial_date,
    )

    warnings = []
    if not possible_duplicates_df.empty:
        warnings.append({
            "Mensaje": "Se detectaron POSIBLES movimientos duplicados (NO se eliminaron; todos entraron al calculo). Revisar en la hoja duplicados_movimientos por si alguno fuera carga repetida.",
            "CantidadFilas": len(possible_duplicates_df),
        })

    if not movements_without_stock_df.empty:
        warnings.append({
            "Mensaje": "Existen movimientos de CodigoArticulo + Deposito que no aparecen en stock informado",
            "CantidadFilas": len(movements_without_stock_df),
        })

    if not unapplied_movements_df.empty:
        warnings.append({
            "Mensaje": "Existen movimientos que no fueron aplicados a ningun control de stock",
            "CantidadFilas": len(unapplied_movements_df),
        })

    if calculation_warnings_df is not None and not calculation_warnings_df.empty:
        for _, row in calculation_warnings_df.iterrows():
            warnings.append({
                "Mensaje": row["Mensaje"],
                "CodigoArticulo": row["CodigoArticulo"],
                "Deposito": row["Deposito"],
                "Fecha": row["Fecha"],
            })

    errors_df = pd.DataFrame(errors)
    warnings_df = pd.DataFrame(warnings)
    output_path, summary_df = export_control_report(
        control_df,
        movements_without_stock_df,
        applied_movements_df,
        unapplied_movements_df,
        possible_duplicates_df,
        errors_df,
        warnings_df,
        Path(reports_dir) / output_file_name,
    )

    return {
        "control_df": control_df,
        "summary_df": summary_df,
        "output_path": output_path,
        "errors_df": errors_df,
        "warnings_df": warnings_df,
    }


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    result = run_stock_analysis(
        project_root / "data" / "normalized",
        project_root / "data" / "reports",
    )
    print(f"Control generado: {result['output_path']}")
    print(result["summary_df"].to_string(index=False))
