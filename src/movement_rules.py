from pathlib import Path

import pandas as pd

from column_mapper import normalize_text


def load_movement_rules(rules_path):
    rules_df = pd.read_csv(Path(rules_path), encoding="utf-8-sig")
    rules_df["TipoMovimiento_Normalizado"] = rules_df["TipoMovimiento"].apply(normalize_text)
    return rules_df


def classify_movement_value(value, rules_df):
    """
    Clasifica un tipo de movimiento.

    Usa coincidencia exacta normalizada y luego una busqueda simple por contenido.
    Si no puede clasificar, devuelve una advertencia para el reporte.
    """
    normalized_value = normalize_text(value)
    if not normalized_value:
        return {
            "Clasificacion_CLC": "",
            "Signo": "",
            "Advertencia": "TipoMovimiento vacio",
        }

    exact = rules_df[rules_df["TipoMovimiento_Normalizado"] == normalized_value]
    if not exact.empty:
        row = exact.iloc[0]
        return {
            "Clasificacion_CLC": row["Clasificacion_CLC"],
            "Signo": row["Signo"],
            "Advertencia": "",
        }

    for _, row in rules_df.iterrows():
        rule_text = row["TipoMovimiento_Normalizado"]
        if rule_text and (rule_text in normalized_value or normalized_value in rule_text):
            return {
                "Clasificacion_CLC": row["Clasificacion_CLC"],
                "Signo": row["Signo"],
                "Advertencia": f"Clasificado por coincidencia parcial con '{row['TipoMovimiento']}'",
            }

    return {
        "Clasificacion_CLC": "",
        "Signo": "",
        "Advertencia": f"TipoMovimiento no clasificado: {value}",
    }


def apply_movement_rules(df, rules_path):
    """
    Agrega Clasificacion_CLC y CantidadNormalizada a movimientos.

    Si el signo requiere revisar, mantiene la cantidad original y deja advertencia.
    """
    rules_df = load_movement_rules(rules_path)
    result = df.copy()

    if "TipoMovimiento" not in result.columns:
        result["Clasificacion_CLC"] = ""
        result["CantidadNormalizada"] = pd.to_numeric(
            result.get("CantidadOriginal", pd.Series(dtype=float)),
            errors="coerce",
        )
        result["AdvertenciaMovimiento"] = "Falta columna TipoMovimiento"
        return result

    classifications = result["TipoMovimiento"].apply(lambda value: classify_movement_value(value, rules_df))
    result["Clasificacion_CLC"] = classifications.apply(lambda item: item["Clasificacion_CLC"])
    result["_SignoCLC"] = classifications.apply(lambda item: item["Signo"])
    result["AdvertenciaMovimiento"] = classifications.apply(lambda item: item["Advertencia"])

    quantities = pd.to_numeric(result.get("CantidadOriginal"), errors="coerce")

    def normalize_quantity(quantity, sign):
        if pd.isna(quantity):
            return quantity
        if str(sign) == "1":
            return abs(quantity)
        if str(sign) == "-1":
            return -abs(quantity)
        return quantity

    result["CantidadNormalizada"] = [
        normalize_quantity(quantity, sign)
        for quantity, sign in zip(quantities, result["_SignoCLC"])
    ]

    revisar_mask = result["_SignoCLC"].astype(str).str.lower() == "revisar"
    result.loc[revisar_mask, "AdvertenciaMovimiento"] = (
        "Transferencia interna: revisar deposito origen/destino antes de usar el signo"
    )

    return result.drop(columns=["_SignoCLC"])
