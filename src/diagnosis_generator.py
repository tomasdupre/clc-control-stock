from pathlib import Path

import pandas as pd


def read_control_results(control_report_path):
    control_report_path = Path(control_report_path)
    if not control_report_path.exists():
        raise FileNotFoundError(f"No existe el archivo de control: {control_report_path}")

    return {
        "control": pd.read_excel(control_report_path, sheet_name="control_stock"),
        "summary": pd.read_excel(control_report_path, sheet_name="resumen"),
    }


def format_number(value):
    if pd.isna(value):
        return "0"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}"


def format_date(value):
    if pd.isna(value):
        return "Sin fecha"
    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        return str(value)


def build_general_summary(control_df, summary_df):
    summary = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
    total = int(summary.get("total_registros_controlados", 0))
    ok = int(summary.get("total_registros_ok", 0))
    differences = int(summary.get("total_registros_con_diferencia", 0))
    ok_percentage = (ok / total * 100) if total else 0

    return [
        "Resumen general",
        f"- Cantidad total de controles realizados: {total}",
        f"- Cantidad de controles OK: {ok}",
        f"- Cantidad de controles con diferencia: {differences}",
        f"- Porcentaje de registros OK: {ok_percentage:.2f}%",
        "",
    ]


def build_top_products_section(control_df):
    differences = control_df[pd.to_numeric(control_df["Diferencia"], errors="coerce").fillna(0) != 0].copy()
    lines = ["Top 10 productos con mayor diferencia absoluta"]
    if differences.empty:
        lines.append("- No se detectaron diferencias por producto.")
        lines.append("")
        return lines

    grouped = (
        differences.groupby(["CodigoArticulo", "Descripcion"], dropna=False)["DiferenciaAbsoluta"]
        .sum()
        .reset_index()
        .sort_values("DiferenciaAbsoluta", ascending=False)
        .head(10)
    )

    for _, row in grouped.iterrows():
        description = row["Descripcion"] if pd.notna(row["Descripcion"]) else ""
        lines.append(
            f"- {row['CodigoArticulo']} | {description}: "
            f"{format_number(row['DiferenciaAbsoluta'])}"
        )
    lines.append("")
    return lines


def build_deposit_section(control_df):
    differences = control_df[pd.to_numeric(control_df["Diferencia"], errors="coerce").fillna(0) != 0].copy()
    lines = ["Diferencias por deposito"]
    if differences.empty:
        lines.append("- No se detectaron diferencias por deposito.")
        lines.append("")
        return lines

    grouped = (
        differences.groupby("Deposito", dropna=False)
        .agg(
            registros_con_diferencia=("Diferencia", "count"),
            diferencia_absoluta=("DiferenciaAbsoluta", "sum"),
            diferencia_neta=("Diferencia", "sum"),
        )
        .reset_index()
        .sort_values("diferencia_absoluta", ascending=False)
    )

    for _, row in grouped.iterrows():
        lines.append(
            f"- {row['Deposito']}: {int(row['registros_con_diferencia'])} registros, "
            f"diferencia absoluta {format_number(row['diferencia_absoluta'])}, "
            f"diferencia neta {format_number(row['diferencia_neta'])}"
        )
    lines.append("")
    return lines


def build_date_section(control_df):
    differences = control_df[pd.to_numeric(control_df["Diferencia"], errors="coerce").fillna(0) != 0].copy()
    lines = ["Diferencias por fecha"]
    if differences.empty:
        lines.append("- No se detectaron diferencias por fecha.")
        lines.append("")
        return lines

    grouped = (
        differences.groupby("Fecha", dropna=False)
        .agg(
            registros_con_diferencia=("Diferencia", "count"),
            diferencia_absoluta=("DiferenciaAbsoluta", "sum"),
            diferencia_neta=("Diferencia", "sum"),
        )
        .reset_index()
        .sort_values("Fecha")
    )

    for _, row in grouped.iterrows():
        lines.append(
            f"- {format_date(row['Fecha'])}: {int(row['registros_con_diferencia'])} registros, "
            f"diferencia absoluta {format_number(row['diferencia_absoluta'])}, "
            f"diferencia neta {format_number(row['diferencia_neta'])}"
        )
    lines.append("")
    return lines


def build_possible_causes(control_df):
    differences = control_df[pd.to_numeric(control_df["Diferencia"], errors="coerce").fillna(0) != 0].copy()
    lines = ["Posibles causas generales"]

    if differences.empty:
        lines.append("- No hay diferencias relevantes para diagnosticar.")
        lines.append("")
        return lines

    positive_count = int((differences["Diferencia"] > 0).sum())
    negative_count = int((differences["Diferencia"] < 0).sum())
    critical_count = int((differences["EstadoControl"] == "Critico").sum())
    deposit_counts = differences.groupby("Deposito")["CodigoArticulo"].count()

    if positive_count > negative_count:
        lines.append("- Hay mas diferencias positivas: el stock informado es mayor al calculado.")
    elif negative_count > positive_count:
        lines.append("- Hay mas diferencias negativas: el stock informado es menor al calculado.")
    else:
        lines.append("- Las diferencias positivas y negativas estan equilibradas.")

    if critical_count:
        lines.append("- Aparecen diferencias grandes; conviene revisar ajustes, ventas, ingresos o transferencias.")

    if not deposit_counts.empty and deposit_counts.max() >= 2:
        lines.append("- Hay depositos con varias diferencias; conviene revisar movimientos internos.")

    lines.append("")
    return lines


def build_recommendations():
    return [
        "Recomendaciones para CLC",
        "- Revisar los productos criticos.",
        "- Validar si todos los movimientos del periodo fueron enviados.",
        "- Revisar signos de tipos de movimiento.",
        "- Revisar transferencias internas.",
        "- Validar que el stock inicial sea correcto.",
        "- Confirmar si el stock informado corresponde a cierre de dia, cierre mensual o stock actual.",
        "",
    ]


def generate_diagnosis(control_report_path, output_path):
    data = read_control_results(control_report_path)
    control_df = data["control"]
    summary_df = data["summary"]

    lines = ["Diagnostico automatico de stock", "=" * 33, ""]
    lines.extend(build_general_summary(control_df, summary_df))
    lines.extend(build_top_products_section(control_df))
    lines.extend(build_deposit_section(control_df))
    lines.extend(build_date_section(control_df))
    lines.extend(build_possible_causes(control_df))
    lines.extend(build_recommendations())

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    path = generate_diagnosis(
        project_root / "data" / "reports" / "control_stock_resultado.xlsx",
        project_root / "data" / "reports" / "diagnostico_stock.txt",
    )
    print(f"Diagnostico generado: {path}")
