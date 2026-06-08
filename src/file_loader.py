from pathlib import Path

import pandas as pd


SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def list_input_files(input_dir):
    """Devuelve los archivos Excel/CSV encontrados en data/input."""
    input_path = Path(input_dir)
    input_path.mkdir(parents=True, exist_ok=True)
    return sorted(
        file for file in input_path.iterdir()
        if file.is_file()
        and file.suffix.lower() in SUPPORTED_EXTENSIONS
        and not file.name.startswith("~$")
    )


def detect_excel_sheets(file_path):
    """Lista las hojas disponibles en un Excel sin cargar todo el archivo."""
    excel_file = pd.ExcelFile(file_path)
    return excel_file.sheet_names


def read_file(file_path, sheet_name=None):
    """Lee un CSV o Excel y devuelve un DataFrame."""
    file_path = Path(file_path)
    extension = file_path.suffix.lower()

    if extension == ".csv":
        return pd.read_csv(file_path)

    if extension in {".xlsx", ".xls"}:
        return pd.read_excel(file_path, sheet_name=sheet_name)

    if extension == ".parquet":
        return pd.read_parquet(file_path)

    raise ValueError(f"Formato no soportado: {file_path.name}")


def describe_dataframe(df):
    """Arma un resumen simple para mostrar en consola."""
    return {
        "filas": len(df),
        "columnas": len(df.columns),
        "nombres_columnas": list(df.columns),
    }


def load_input_files(input_dir):
    """
    Carga todos los archivos de data/input.

    Para Excel devuelve una entrada por hoja. Para CSV devuelve una sola entrada.
    Esta funcion es util si mas adelante se quiere automatizar todo el lote.
    """
    loaded = []
    for file_path in list_input_files(input_dir):
        if file_path.suffix.lower() == ".csv":
            df = read_file(file_path)
            loaded.append({
                "file_path": file_path,
                "sheet_name": None,
                "dataframe": df,
                "summary": describe_dataframe(df),
            })
        else:
            for sheet_name in detect_excel_sheets(file_path):
                df = read_file(file_path, sheet_name=sheet_name)
                loaded.append({
                    "file_path": file_path,
                    "sheet_name": sheet_name,
                    "dataframe": df,
                    "summary": describe_dataframe(df),
                })
    return loaded
