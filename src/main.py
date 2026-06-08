from pathlib import Path
from datetime import datetime
import re
import unicodedata

import pandas as pd

from column_mapper import PENDING_FIELD, mapping_dataframe_to_dict, propose_column_mapping
from data_validator import validate_dataframe
from file_loader import describe_dataframe, detect_excel_sheets, list_input_files, read_file
from normalizer import export_normalized, normalize_master, normalize_movements, normalize_stock
from report_generator import generate_quality_report
from diagnosis_generator import generate_diagnosis
from stock_analyzer import run_stock_analysis


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "data" / "input"
NORMALIZED_DIR = PROJECT_ROOT / "data" / "normalized"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
DICTIONARY_PATH = PROJECT_ROOT / "rules" / "diccionario_columnas.csv"
MOVEMENT_RULES_PATH = PROJECT_ROOT / "rules" / "reglas_movimientos.csv"
REQUIRED_NORMALIZED_FILES = [
    "maestro_normalizado.parquet",
    "stock_normalizado.parquet",
    "movimientos_normalizado.parquet",
]
EXAMPLE_FILE_KEYWORDS = ["ejemplo", "sample", "test", "prueba"]


def slugify_client_name(client_name):
    """Convierte el nombre del cliente en un texto seguro para nombres de archivo."""
    if not client_name:
        return ""
    text = unicodedata.normalize("NFKD", client_name.strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def ask_client_name():
    client_name = input("\nNombre del cliente para identificar los reportes (Enter para omitir): ").strip()
    client_slug = slugify_client_name(client_name)
    if client_slug:
        print(f"Los reportes se guardaran con el identificador: {client_slug}")
    else:
        print("No se ingreso cliente. Se usaran los nombres estandar de reportes.")
    return client_slug


def ask_yes_no(question, default=None):
    """
    Pregunta si/no aceptando variantes comunes.

    default puede ser "s", "n" o None. Si hay default, Enter toma ese valor.
    """
    valid_yes = {"s", "si", "y", "yes"}
    valid_no = {"n", "no"}
    while True:
        answer = input(question).strip().lower()
        if not answer and default in {"s", "n"}:
            return default == "s"
        if answer in valid_yes:
            return True
        if answer in valid_no:
            return False
        print("Respuesta no valida. Escribi s o n.")


def build_report_file_name(base_name, extension, client_slug):
    if client_slug:
        return f"{base_name}_{client_slug}.{extension}"
    return f"{base_name}.{extension}"


def ask_main_action():
    action_aliases = {
        "normalizar": "normalizar",
        "n": "normalizar",
        "analizar": "analizar",
        "a": "analizar",
        "limpiar": "limpiar",
        "l": "limpiar",
        "estado": "estado",
        "e": "estado",
        "salir": "salir",
        "s": "salir",
    }
    print("\nOpciones:")
    print("  normalizar - leer data/input, mapear columnas y generar archivos normalizados")
    print("  analizar   - usar data/normalized y calcular diferencias de stock")
    print("  limpiar    - borrar archivos de data/normalized y data/reports")
    print("  estado     - listar archivos actuales y cantidad de filas")
    print("  salir      - cerrar el sistema")
    while True:
        answer = input("\nQue queres hacer? (normalizar / analizar / limpiar / estado / salir): ").strip().lower()
        if answer in action_aliases:
            return action_aliases[answer]
        print("Opcion no valida. Escribi normalizar, analizar, limpiar, estado o salir.")


def list_files(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return sorted(path for path in directory.iterdir() if path.is_file() and not path.name.startswith("~$"))


def detect_example_input_files():
    example_files = []
    for file_path in list_files(INPUT_DIR):
        lowered_name = file_path.name.lower()
        if any(keyword in lowered_name for keyword in EXAMPLE_FILE_KEYWORDS):
            example_files.append(file_path)
    return example_files


def count_rows_and_columns(file_path):
    """Intenta leer un archivo y devolver filas/columnas sin frenar el sistema."""
    try:
        if file_path.suffix.lower() == ".csv":
            df = pd.read_csv(file_path)
            return len(df), list(df.columns), ""
        if file_path.suffix.lower() == ".parquet":
            df = pd.read_parquet(file_path)
            return len(df), list(df.columns), ""
        if file_path.suffix.lower() in {".xlsx", ".xls"}:
            excel_file = pd.ExcelFile(file_path)
            sheet_summaries = []
            total_rows = 0
            all_columns = []
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(file_path, sheet_name=sheet_name)
                total_rows += len(df)
                all_columns.extend(str(column) for column in df.columns)
                sheet_summaries.append(f"{sheet_name}: {len(df)} filas")
            unique_columns = list(dict.fromkeys(all_columns))
            return total_rows, unique_columns, "; ".join(sheet_summaries)
    except Exception as exc:
        return None, [], f"No se pudo leer: {exc}"
    return None, [], "Formato no soportado para conteo"


def summarize_normalized_dataframe(df):
    summary = []
    if "ArchivoOrigen" in df.columns:
        origins = df[["ArchivoOrigen", "HojaOrigen"]].drop_duplicates()
        summary.append(f"archivos/hojas origen: {len(origins)}")

    if "Fecha" in df.columns:
        dates = pd.to_datetime(df["Fecha"], errors="coerce").dropna()
        if not dates.empty:
            summary.append(
                f"fechas: {dates.min().date().isoformat()} a {dates.max().date().isoformat()}"
            )

    if "CodigoArticulo" in df.columns:
        summary.append(f"SKUs unicos: {df['CodigoArticulo'].dropna().astype(str).str.strip().nunique()}")

    return " | ".join(summary)


def print_directory_status(directory, title, include_row_counts=True):
    print(f"\n{title}")
    print("-" * len(title))
    files = list_files(directory)
    if not files:
        print("No hay archivos.")
        return

    for file_path in files:
        modified = datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        size_kb = file_path.stat().st_size / 1024
        print(f"- {file_path.name} | modificado: {modified} | tamano: {size_kb:.1f} KB")
        if include_row_counts:
            rows, columns, detail = count_rows_and_columns(file_path)
            if rows is not None:
                print(f"  filas: {rows} | columnas: {', '.join(columns)}")
            if detail:
                print(f"  detalle: {detail}")


def show_project_status(include_row_counts=True):
    print("\nEstado actual de archivos")
    print("=========================")
    print_directory_status(INPUT_DIR, "data/input", include_row_counts=include_row_counts)
    print_directory_status(NORMALIZED_DIR, "data/normalized", include_row_counts=include_row_counts)
    print_directory_status(REPORTS_DIR, "data/reports", include_row_counts=include_row_counts)
    warn_if_example_files_exist()


def warn_if_example_files_exist():
    example_files = detect_example_input_files()
    if not example_files:
        return

    print("\nAtencion: hay archivos de ejemplo o prueba en data/input. Pueden mezclarse con los archivos del cliente.")
    for file_path in example_files:
        print(f"- {file_path.name}")


def clean_directory_files(directory):
    deleted_files = []
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for file_path in directory.iterdir():
        if file_path.is_file():
            file_path.unlink()
            deleted_files.append(file_path.name)
    return deleted_files


def clean_previous_outputs(require_confirmation=True):
    if require_confirmation:
        confirmed = ask_yes_no(
            "\nEsto borrara archivos dentro de data/normalized y data/reports. Confirmas la limpieza? (s/n): "
        )
        if not confirmed:
            print("Limpieza cancelada.")
            return

    deleted_normalized = clean_directory_files(NORMALIZED_DIR)
    deleted_reports = clean_directory_files(REPORTS_DIR)
    print("\nLimpieza finalizada.")
    print(f"Archivos borrados en data/normalized: {len(deleted_normalized)}")
    print(f"Archivos borrados en data/reports: {len(deleted_reports)}")


def validate_required_normalized_files():
    missing_files = []
    for file_name in REQUIRED_NORMALIZED_FILES:
        file_path = NORMALIZED_DIR / file_name
        if not file_path.exists():
            missing_files.append(file_name)
    return missing_files


def show_normalized_files_for_analysis():
    missing_files = validate_required_normalized_files()
    if missing_files:
        print("\nNo se puede ejecutar el analisis automatico.")
        print("Faltan estos archivos normalizados:")
        for file_name in missing_files:
            print(f"- data/normalized/{file_name}")
        print("Debes volver a normalizar los archivos del cliente antes de analizar.")
        return False

    print("\nEstos son los archivos normalizados que se van a usar:")
    for file_name in REQUIRED_NORMALIZED_FILES:
        file_path = NORMALIZED_DIR / file_name
        modified = datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        rows, columns, detail = count_rows_and_columns(file_path)
        print(f"\n- {file_name}")
        print(f"  fecha de modificacion: {modified}")
        if rows is None:
            print(f"  no se pudo leer el archivo: {detail}")
            return False
        print(f"  cantidad de filas: {rows}")
        print(f"  columnas disponibles: {', '.join(columns)}")
        if detail:
            print(f"  detalle: {detail}")
        try:
            df = pd.read_parquet(file_path)
            normalized_summary = summarize_normalized_dataframe(df)
            if normalized_summary:
                print(f"  resumen: {normalized_summary}")
        except Exception as exc:
            print(f"  no se pudo armar resumen adicional: {exc}")

    confirm = ask_yes_no(
        "\nEstos son los archivos normalizados que se van a usar. Confirmas que corresponden al cliente actual? (s/n): "
    )
    if not confirm:
        print("Analisis cancelado. Debes volver a normalizar los archivos del cliente.")
        return False
    return True


def ask_deposit_mode():
    return ask_yes_no(
        "\nEl deposito importa para este analisis? (s/n). Si no importa, se agrupa todo por CodigoArticulo: ",
        default="n",
    )


def ask_initial_date_mode():
    return ask_yes_no(
        "\nIncluir movimientos de la misma fecha del stock inicial? (s/n). Si el stock inicial es cierre de dia, responde n: ",
        default="n",
    )


def run_automatic_stock_control(client_slug=""):
    if not show_normalized_files_for_analysis():
        return None, None

    use_deposit = ask_deposit_mode()
    if use_deposit:
        print("El control se calculara por CodigoArticulo + Deposito.")
    else:
        print("El control se calculara por CodigoArticulo, ignorando Deposito.")

    include_initial_date_movements = ask_initial_date_mode()
    if include_initial_date_movements:
        print("Se incluiran movimientos de la misma fecha del stock inicial.")
    else:
        print("No se incluiran movimientos de la misma fecha del stock inicial.")

    print("\nEjecutando control automatico de stock...")
    control_file_name = build_report_file_name("control_stock_resultado", "xlsx", client_slug)
    diagnosis_file_name = build_report_file_name("diagnostico_stock", "txt", client_slug)
    analysis_result = run_stock_analysis(
        NORMALIZED_DIR,
        REPORTS_DIR,
        control_file_name,
        use_deposit=use_deposit,
        include_initial_date_movements=include_initial_date_movements,
    )
    control_path = analysis_result["output_path"]
    diagnosis_path = generate_diagnosis(
        control_path,
        REPORTS_DIR / diagnosis_file_name,
    )

    summary = analysis_result["summary_df"].iloc[0].to_dict()
    print("\nResumen del control:")
    print(f"Total registros controlados: {int(summary['total_registros_controlados'])}")
    print(f"Registros con diferencia: {int(summary['total_registros_con_diferencia'])}")
    print(f"Total diferencia absoluta: {summary['total_diferencia_absoluta']}")
    print(f"Excel generado: {control_path}")
    print(f"Diagnostico generado: {diagnosis_path}")
    return analysis_result, diagnosis_path


def ask_file_type(file_name):
    valid_types = {"maestro", "stock", "movimientos", "saltar", "volver"}
    while True:
        answer = input(
            f"\nQue tipo de archivo es '{file_name}'? "
            "(maestro / stock / movimientos / saltar / volver): "
        ).strip().lower()
        if answer in valid_types:
            return answer
        print("Opcion no valida. Escribi maestro, stock, movimientos, saltar o volver.")


def choose_excel_sheet(file_path):
    sheets = detect_excel_sheets(file_path)
    if len(sheets) == 1:
        return sheets[0]

    print(f"\nEl archivo {file_path.name} tiene estas hojas:")
    for number, sheet in enumerate(sheets, start=1):
        print(f"  {number}. {sheet}")

    while True:
        answer = input("Elegi el numero de hoja a procesar, o escribi volver: ").strip().lower()
        if answer == "volver":
            return "volver"
        if answer.isdigit() and 1 <= int(answer) <= len(sheets):
            return sheets[int(answer) - 1]
        print("Numero de hoja no valido.")


def ask_process_another_sheet(file_path):
    if file_path.suffix.lower() not in {".xlsx", ".xls"}:
        return False
    return ask_yes_no(
        f"\nQueres procesar otra hoja de '{file_path.name}' como otro tipo de archivo? (s/n): ",
        default="n",
    )


def print_mapping(mapping_df):
    print("\nMapeo propuesto:")
    columns_to_show = ["ColumnaOriginal", "CampoCLC", "Confianza", "Observacion", "RequiereConfirmacion"]
    print(mapping_df[columns_to_show].to_string(index=False))


def allow_manual_mapping_edits(mapping_df):
    """
    Permite corregir el mapeo desde consola.

    Es intencionalmente simple: se edita una columna por vez indicando el campo CLC.
    """
    editable = mapping_df.copy()
    valid_fields = {
        "CodigoArticulo",
        "Descripcion",
        "Fecha",
        "Deposito",
        "TipoMovimiento",
        "CantidadOriginal",
        "Documento",
        "StockInformado",
        "Categoria",
        "Marca",
        "CostoUnitario",
        "Estado",
        PENDING_FIELD,
    }

    answer = input("\nQueres corregir algun mapeo antes de continuar? (s/n): ").strip().lower()
    if answer != "s":
        return editable

    print("\nCampos validos:")
    print(", ".join(sorted(valid_fields)))

    while True:
        original_column = input("\nNombre exacto de columna a corregir, o Enter para terminar: ").strip()
        if not original_column:
            break
        if original_column not in set(editable["ColumnaOriginal"]):
            print("No encontre esa columna. Copia el nombre tal como aparece en el mapeo.")
            continue

        new_field = input("Campo CLC correcto: ").strip()
        if new_field not in valid_fields:
            print("Campo no valido.")
            continue

        if new_field != PENDING_FIELD:
            duplicate_mask = (
                (editable["CampoCLC"] == new_field)
                & (editable["ColumnaOriginal"] != original_column)
            )
            if duplicate_mask.any():
                editable.loc[duplicate_mask, "CampoCLC"] = PENDING_FIELD
                editable.loc[duplicate_mask, "RequiereConfirmacion"] = True
                editable.loc[duplicate_mask, "Observacion"] = (
                    f"Desasignado automaticamente porque '{original_column}' fue elegido como {new_field}"
                )

        editable.loc[editable["ColumnaOriginal"] == original_column, "CampoCLC"] = new_field
        editable.loc[editable["ColumnaOriginal"] == original_column, "Confianza"] = 100
        editable.loc[editable["ColumnaOriginal"] == original_column, "Observacion"] = "Corregido manualmente"
        editable.loc[editable["ColumnaOriginal"] == original_column, "RequiereConfirmacion"] = False

    return editable


def normalize_by_type(df, file_type, mapping):
    if file_type == "maestro":
        return normalize_master(df, mapping)
    if file_type == "stock":
        return normalize_stock(df, mapping)
    if file_type == "movimientos":
        return normalize_movements(df, mapping, MOVEMENT_RULES_PATH)
    raise ValueError(f"Tipo de archivo no soportado: {file_type}")


def split_issues_and_warnings(issues_df):
    if issues_df.empty:
        return issues_df, issues_df
    errors = issues_df[issues_df["Severidad"].str.lower() == "error"].copy()
    warnings = issues_df[issues_df["Severidad"].str.lower() != "error"].copy()
    return errors, warnings


def main():
    print("CLC Control Inteligente de Stock")
    print("--------------------------------")
    show_project_status(include_row_counts=False)

    while True:
        action = ask_main_action()
        if action == "estado":
            show_project_status(include_row_counts=True)
            continue
        if action == "limpiar":
            clean_previous_outputs(require_confirmation=True)
            show_project_status(include_row_counts=False)
            continue
        break

    if action == "salir":
        print("Proceso cancelado.")
        return

    if action == "analizar":
        client_slug = ask_client_name()
        run_automatic_stock_control(client_slug)
        print("Proceso terminado.")
        return

    files = list_input_files(INPUT_DIR)
    if not files:
        print(f"No se encontraron archivos en {INPUT_DIR}.")
        print("Agrega archivos .csv, .xlsx o .xls y volve a ejecutar python src/main.py.")
        analyze_answer = ask_yes_no("Queres analizar archivos ya normalizados? (s/n): ")
        if analyze_answer:
            client_slug = ask_client_name()
            run_automatic_stock_control(client_slug)
        return

    client_slug = ask_client_name()

    clean_answer = ask_yes_no(
        "\nQueres limpiar los archivos normalizados y reportes anteriores antes de continuar? (s/n): "
    )
    if clean_answer:
        clean_previous_outputs(require_confirmation=False)

    all_mappings = []
    all_issues = []
    processed_files = []
    all_normalized = {
        "maestro": [],
        "stock": [],
        "movimientos": [],
    }
    normalized_master = None

    for file_path in files:
        file_type = ask_file_type(file_path.name)
        if file_type == "volver":
            print("\nVolviendo al menu principal...")
            main()
            return
        if file_type == "saltar":
            processed_files.append({
                "TipoArchivo": "sin_procesar",
                "ArchivoOrigen": file_path.name,
                "HojaOrigen": "",
                "FilasLeidas": "",
                "FilasNormalizadas": 0,
                "Estado": "Saltado",
                "Observacion": "Usuario salto el archivo",
            })
            if ask_process_another_sheet(file_path):
                files.append(file_path)
            continue

        sheet_name = None
        if file_path.suffix.lower() in {".xlsx", ".xls"}:
            try:
                sheet_name = choose_excel_sheet(file_path)
            except Exception as exc:
                print(f"No se pudo leer el Excel {file_path.name}: {exc}")
                processed_files.append({
                    "TipoArchivo": file_type,
                    "ArchivoOrigen": file_path.name,
                    "HojaOrigen": "",
                    "FilasLeidas": "",
                    "FilasNormalizadas": 0,
                    "Estado": "Error",
                    "Observacion": f"No se pudieron detectar hojas: {exc}",
                })
                if ask_process_another_sheet(file_path):
                    files.append(file_path)
                continue
            if sheet_name == "volver":
                print("\nVolviendo al menu principal...")
                main()
                return

        try:
            df = read_file(file_path, sheet_name=sheet_name)
        except Exception as exc:
            print(f"No se pudo leer el archivo {file_path.name}: {exc}")
            processed_files.append({
                "TipoArchivo": file_type,
                "ArchivoOrigen": file_path.name,
                "HojaOrigen": sheet_name or "",
                "FilasLeidas": "",
                "FilasNormalizadas": 0,
                "Estado": "Error",
                "Observacion": f"No se pudo leer el archivo: {exc}",
            })
            if ask_process_another_sheet(file_path):
                files.append(file_path)
            continue

        summary = describe_dataframe(df)
        print(f"\nArchivo: {file_path.name}")
        if sheet_name:
            print(f"Hoja: {sheet_name}")
        print(f"Filas: {summary['filas']} | Columnas: {summary['columnas']}")
        print("Columnas:", ", ".join(summary["nombres_columnas"]))

        mapping_df = propose_column_mapping(df.columns, DICTIONARY_PATH)
        mapping_df.insert(0, "Archivo", file_path.name)
        if sheet_name:
            mapping_df.insert(1, "Hoja", sheet_name)
        else:
            mapping_df.insert(1, "Hoja", "")

        print_mapping(mapping_df)
        mapping_df = allow_manual_mapping_edits(mapping_df)

        continue_answer = ask_yes_no("\nContinuar con este mapeo? (s/n): ")
        if not continue_answer:
            print("Archivo omitido. El mapeo queda documentado en el reporte.")
            all_mappings.append(mapping_df)
            processed_files.append({
                "TipoArchivo": file_type,
                "ArchivoOrigen": file_path.name,
                "HojaOrigen": sheet_name or "",
                "FilasLeidas": summary["filas"],
                "FilasNormalizadas": 0,
                "Estado": "Omitido",
                "Observacion": "Usuario no confirmo el mapeo",
            })
            if ask_process_another_sheet(file_path):
                files.append(file_path)
            continue

        mapping = mapping_dataframe_to_dict(mapping_df)
        normalized_df = normalize_by_type(df, file_type, mapping)
        normalized_df.insert(0, "ArchivoOrigen", file_path.name)
        normalized_df.insert(1, "HojaOrigen", sheet_name or "")
        all_normalized[file_type].append(normalized_df)
        combined_normalized_df = pd.concat(all_normalized[file_type], ignore_index=True)
        output_path = export_normalized(combined_normalized_df, file_type, NORMALIZED_DIR)
        print(f"Archivo normalizado actualizado: {output_path}")
        processed_files.append({
            "TipoArchivo": file_type,
            "ArchivoOrigen": file_path.name,
            "HojaOrigen": sheet_name or "",
            "FilasLeidas": summary["filas"],
            "FilasNormalizadas": len(normalized_df),
            "Estado": "Procesado",
            "Observacion": f"Acumulado actual {file_type}: {len(combined_normalized_df)} filas",
        })

        if file_type == "maestro":
            normalized_master = pd.concat(all_normalized["maestro"], ignore_index=True)

        issues_df = validate_dataframe(normalized_df, file_type, master_df=normalized_master)
        if not issues_df.empty:
            issues_df.insert(0, "Archivo", file_path.name)
            issues_df.insert(1, "Hoja", sheet_name or "")
            all_issues.append(issues_df)

        all_mappings.append(mapping_df)

        if ask_process_another_sheet(file_path):
            files.append(file_path)

    final_mapping_df = pd.concat(all_mappings, ignore_index=True) if all_mappings else pd.DataFrame()
    final_issues_df = pd.concat(all_issues, ignore_index=True) if all_issues else pd.DataFrame()
    processed_files_df = pd.DataFrame(processed_files)
    errors_df, warnings_df = split_issues_and_warnings(final_issues_df)

    report_path = generate_quality_report(
        final_mapping_df,
        errors_df,
        warnings_df,
        REPORTS_DIR / build_report_file_name("reporte_calidad", "xlsx", client_slug),
        processed_files_df,
    )
    print(f"\nReporte generado: {report_path}")
    if not processed_files_df.empty:
        print("\nResumen de archivos procesados:")
        for file_type, group in processed_files_df.groupby("TipoArchivo"):
            processed_rows = group["FilasNormalizadas"].sum()
            print(f"- {file_type}: {len(group)} archivo(s)/hoja(s), {processed_rows} filas normalizadas")
    analyze_answer = ask_yes_no("\nQueres ejecutar el control automatico de stock? (s/n): ")
    if analyze_answer:
        run_automatic_stock_control(client_slug)
    print("Proceso terminado.")


if __name__ == "__main__":
    main()
