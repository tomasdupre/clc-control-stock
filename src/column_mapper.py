import re
import unicodedata
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process


LOW_CONFIDENCE_LIMIT = 75
HIGH_CONFIDENCE_LIMIT = 92
PENDING_FIELD = "PendienteConfirmacion"


def normalize_text(value):
    """Normaliza texto para comparar columnas aunque tengan acentos o simbolos."""
    if pd.isna(value):
        return ""
    text = str(value).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_column_dictionary(dictionary_path):
    dictionary_path = Path(dictionary_path)
    return pd.read_csv(dictionary_path, encoding="utf-8-sig")


def _build_dictionary_options(dictionary_df):
    options = []
    for _, row in dictionary_df.iterrows():
        options.append({
            "campo_clc": row["CampoCLC"],
            "equivalencia": row["Equivalencia"],
            "normalized": normalize_text(row["Equivalencia"]),
        })
    return options


def propose_column_mapping(columns, dictionary_path):
    """
    Propone un mapeo entre columnas originales y campos CLC.

    Primero intenta coincidencia exacta normalizada. Si no aparece, usa fuzzy
    matching para sugerir la equivalencia mas parecida.
    """
    dictionary_df = load_column_dictionary(dictionary_path)
    options = _build_dictionary_options(dictionary_df)
    exact_lookup = {item["normalized"]: item for item in options}
    normalized_choices = [item["normalized"] for item in options]

    proposals = []
    for column in columns:
        normalized_column = normalize_text(column)
        observation = "Coincidencia exacta"
        confidence = 100
        selected = exact_lookup.get(normalized_column)

        if selected is None:
            match = process.extractOne(
                normalized_column,
                normalized_choices,
                scorer=fuzz.token_sort_ratio,
            )
            if match:
                matched_text, confidence, index = match
                selected = options[index]
                observation = f"Coincidencia aproximada contra '{selected['equivalencia']}'"
            else:
                confidence = 0
                selected = None
                observation = "No se encontro equivalencia"

        if selected and confidence >= LOW_CONFIDENCE_LIMIT:
            field = selected["campo_clc"]
            requires_confirmation = confidence < HIGH_CONFIDENCE_LIMIT
        else:
            field = PENDING_FIELD
            requires_confirmation = True

        proposals.append({
            "ColumnaOriginal": column,
            "CampoCLC": field,
            "Confianza": round(float(confidence), 2),
            "Observacion": observation,
            "RequiereConfirmacion": requires_confirmation,
        })

    return enforce_unique_standard_fields(pd.DataFrame(proposals))


def enforce_unique_standard_fields(mapping_df):
    """
    Garantiza que cada campo CLC quede asignado a una sola columna.

    Si varias columnas parecen corresponder al mismo campo, conserva la de mayor
    confianza y deja las demas pendientes para confirmacion manual.
    """
    if mapping_df.empty or "CampoCLC" not in mapping_df.columns:
        return mapping_df

    result = mapping_df.copy()
    if "OrdenMapeo" not in result.columns:
        result["OrdenMapeo"] = range(len(result))

    mapped_fields = result[result["CampoCLC"] != PENDING_FIELD]["CampoCLC"].dropna().unique()
    for field in mapped_fields:
        field_mask = result["CampoCLC"] == field
        candidates = result[field_mask].copy()
        if len(candidates) <= 1:
            continue

        candidates["PrioridadExacta"] = candidates["Observacion"].astype(str).str.contains(
            "exacta",
            case=False,
            na=False,
        ).astype(int)
        candidates = candidates.sort_values(
            by=["Confianza", "PrioridadExacta", "OrdenMapeo"],
            ascending=[False, False, True],
        )
        keep_index = candidates.index[0]
        duplicate_indexes = [index for index in candidates.index if index != keep_index]

        for duplicate_index in duplicate_indexes:
            previous_observation = result.loc[duplicate_index, "Observacion"]
            result.loc[duplicate_index, "CampoCLC"] = PENDING_FIELD
            result.loc[duplicate_index, "RequiereConfirmacion"] = True
            result.loc[duplicate_index, "Observacion"] = (
                f"Posible {field}, pero ya hay otra columna asignada a ese campo. "
                f"Revisar manualmente. Observacion original: {previous_observation}"
            )

        result.loc[keep_index, "Observacion"] = (
            f"{result.loc[keep_index, 'Observacion']} | Seleccionada como unica columna para {field}"
        )

    return result.drop(columns=["OrdenMapeo"], errors="ignore")


def mapping_dataframe_to_dict(mapping_df):
    """
    Convierte el DataFrame de mapeo en un diccionario:
    columna original -> campo CLC.
    """
    result = {}
    safe_mapping_df = enforce_unique_standard_fields(mapping_df)
    for _, row in safe_mapping_df.iterrows():
        field = row["CampoCLC"]
        if field and field != PENDING_FIELD:
            result[row["ColumnaOriginal"]] = field
    return result
