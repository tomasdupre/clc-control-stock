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
            "Advertencia": "",
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

    REGLA DE INTEGRIDAD: el sistema NUNCA modifica el signo ni el valor de un
    movimiento. CantidadNormalizada es SIEMPRE igual a CantidadOriginal: se usa
    el numero exactamente como lo informa el cliente.

    La clasificacion (Entrada/Salida) queda solo como informacion. Si el signo del
    dato no coincide con esa clasificacion (por ejemplo, una "Salida" con cantidad
    positiva), se deja una advertencia para que una persona lo revise, pero el
    numero no se toca.
    """
    rules_df = load_movement_rules(rules_path)
    result = df.copy()

    quantities = pd.to_numeric(result.get("CantidadOriginal"), errors="coerce")
    # El valor se usa tal cual: nunca se le cambia el signo.
    result["CantidadNormalizada"] = quantities

    if "TipoMovimiento" not in result.columns:
        result["Clasificacion_CLC"] = ""
        result["AdvertenciaMovimiento"] = ""
        return result

    classifications = result["TipoMovimiento"].apply(lambda value: classify_movement_value(value, rules_df))
    result["Clasificacion_CLC"] = classifications.apply(lambda item: item["Clasificacion_CLC"])
    signos = classifications.apply(lambda item: str(item["Signo"]))
    base_warnings = classifications.apply(lambda item: item["Advertencia"])

    # Advertencias informativas (NO cambian el numero): solo marcan inconsistencias
    # entre el signo informado y la clasificacion, para revision manual.
    avisos = []
    for quantity, sign, base in zip(quantities, signos, base_warnings):
        msg = base or ""
        if pd.notna(quantity):
            if sign == "1" and quantity < 0:
                aviso = "Clasificado como Entrada pero la cantidad es negativa; se respeta el dato informado"
                msg = f"{msg} | {aviso}" if msg else aviso
            elif sign == "-1" and quantity > 0:
                aviso = "Clasificado como Salida pero la cantidad es positiva; se respeta el dato informado"
                msg = f"{msg} | {aviso}" if msg else aviso
            elif sign.lower() == "revisar":
                aviso = "Transferencia interna: revisar deposito origen/destino"
                msg = f"{msg} | {aviso}" if msg else aviso
        avisos.append(msg)
    result["AdvertenciaMovimiento"] = avisos

    return result
