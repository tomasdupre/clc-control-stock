from pathlib import Path

import pandas as pd


def _safe_sheet(df, empty_message):
    if df is None or df.empty:
        return pd.DataFrame({"Mensaje": [empty_message]})
    return df


def build_learning_suggestions(mapping_df, issues_df):
    """Sugiere aprendizajes simples para revisar y luego cargar al diccionario."""
    suggestions = []

    if mapping_df is not None and not mapping_df.empty:
        pending = mapping_df[mapping_df["RequiereConfirmacion"] == True]
        for _, row in pending.iterrows():
            suggestions.append({
                "Tipo": "Mapeo de columnas",
                "Sugerencia": (
                    f"Revisar si la columna '{row['ColumnaOriginal']}' corresponde a "
                    f"'{row['CampoCLC']}'. Si es correcto, agregarla a rules/diccionario_columnas.csv."
                ),
            })

    if issues_df is not None and not issues_df.empty:
        movement_issues = issues_df[
            issues_df["Mensaje"].astype(str).str.contains("no clasificado", case=False, na=False)
        ]
        for _, row in movement_issues.iterrows():
            suggestions.append({
                "Tipo": "Regla de movimiento",
                "Sugerencia": (
                    "Agregar una regla en rules/reglas_movimientos.csv para el tipo de movimiento "
                    f"observado en fila {row['Fila']}."
                ),
            })

    return pd.DataFrame(suggestions)


def generate_quality_report(mapping_df, issues_df, warnings_df, report_path, processed_files_df=None):
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    learning_df = build_learning_suggestions(mapping_df, issues_df)

    with pd.ExcelWriter(report_path, engine="openpyxl") as writer:
        _safe_sheet(mapping_df, "No hay mapeos para reportar").to_excel(
            writer,
            sheet_name="mapeo_columnas",
            index=False,
        )
        _safe_sheet(issues_df, "No se detectaron errores").to_excel(
            writer,
            sheet_name="errores_detectados",
            index=False,
        )
        _safe_sheet(warnings_df, "No se detectaron advertencias").to_excel(
            writer,
            sheet_name="advertencias",
            index=False,
        )
        _safe_sheet(learning_df, "No hay aprendizajes sugeridos").to_excel(
            writer,
            sheet_name="aprendizajes_sugeridos",
            index=False,
        )
        _safe_sheet(processed_files_df, "No hay archivos procesados para reportar").to_excel(
            writer,
            sheet_name="archivos_procesados",
            index=False,
        )

    return report_path
