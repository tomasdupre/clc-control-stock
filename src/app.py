import io
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import anthropic
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from column_mapper import PENDING_FIELD, mapping_dataframe_to_dict, normalize_text, propose_column_mapping
from normalizer import detect_footer_rows, export_normalized, normalize_code, normalize_master, normalize_movements, normalize_stock
from stock_analyzer import build_calculation_consistency, run_stock_analysis
from diagnosis_generator import generate_diagnosis
import cloud_store


def _to_py(value):
    """Convierte tipos numpy/pandas a tipos Python nativos (para guardar como JSON)."""
    try:
        import numpy as np
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            return float(value)
    except Exception:
        pass
    if pd.isna(value) if not isinstance(value, (list, dict)) else False:
        return None
    return value

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
NORMALIZED_DIR = PROJECT_ROOT / "data" / "normalized"
INPUT_DIR = PROJECT_ROOT / "data" / "input"
DICTIONARY_PATH = PROJECT_ROOT / "rules" / "diccionario_columnas.csv"
MOVEMENT_RULES_PATH = PROJECT_ROOT / "rules" / "reglas_movimientos.csv"

# Campos CLC disponibles POR TIPO de hoja. Solo estos aparecen en el desplegable
# "Campo CLC" al mapear (más PendienteConfirmacion para lo que no se usa).
CLC_FIELDS_BY_TYPE = {
    # Categoria (Unidad de Gestión) es OPCIONAL: si está en el maestro, se usa para
    # las visualizaciones por categoría (Balance de Masa).
    "maestro": ["CodigoArticulo", "Descripcion", "Categoria"],
    "stock": ["CodigoArticulo", "Fecha", "StockInformado"],
    # TipoMovimiento y Documento son OPCIONALES.
    "movimientos": ["CodigoArticulo", "Descripcion", "Fecha", "CantidadOriginal", "TipoMovimiento", "Documento"],
}


def clc_options_for(file_type):
    """Opciones del desplegable Campo CLC para un tipo de hoja (+ pendiente)."""
    return CLC_FIELDS_BY_TYPE.get(file_type, []) + [PENDING_FIELD]


# Signo manual por hoja de movimientos (a mano, sin IA).
SIGNO_HOJA_OPCIONES = {
    "Mantener (como viene)": "mantener",
    "Todas entradas (+)": "entrada",
    "Todas salidas (−)": "salida",
}


def aplicar_signo_hoja(norm_df, signo):
    """
    Aplica un signo a TODA una hoja de movimientos (elección manual del usuario).
    'entrada' → todo positivo; 'salida' → todo negativo; 'mantener' → sin cambio
    (respeta el signo de cada fila, ideal para ajustes + o −).
    Se aplica a CantidadOriginal y CantidadNormalizada.
    """
    if signo not in ("entrada", "salida"):
        return norm_df
    for col in ("CantidadOriginal", "CantidadNormalizada"):
        if col in norm_df.columns:
            q = pd.to_numeric(norm_df[col], errors="coerce")
            norm_df[col] = q.abs() if signo == "entrada" else -q.abs()
    return norm_df


# Todos los campos válidos (unión), por compatibilidad.
VALID_CLC_FIELDS = [
    "CodigoArticulo", "Descripcion", "Fecha", "StockInformado",
    "CantidadOriginal", "TipoMovimiento", "Categoria", PENDING_FIELD,
]

# Campos que el calculo necesita si o si por cada tipo de archivo.
# Si alguno queda sin asignar, el normalizado sale incompleto: hay que bloquear.
REQUIRED_BY_TYPE = {
    "maestro": ["CodigoArticulo"],
    "stock": ["CodigoArticulo", "Fecha", "StockInformado"],
    "movimientos": ["CodigoArticulo", "CantidadOriginal"],
}

# Campos que conviene tener pero no bloquean (solo advertencia).
RECOMMENDED_BY_TYPE = {
    "maestro": ["Descripcion", "Categoria"],
    "stock": [],
    "movimientos": ["Descripcion", "Fecha"],
}

STOCK_LIKE_QUANTITY_COLUMNS = {
    "stock", "stock final", "saldo", "existencia", "inventario", "cd",
}


def get_mapped_fields(mapping_df):
    """Devuelve el conjunto de campos CLC ya asignados (sin pendientes)."""
    if mapping_df is None or mapping_df.empty:
        return set()
    return {
        field for field in mapping_df["CampoCLC"].tolist()
        if field and field != PENDING_FIELD
    }


def missing_required_fields(mapping_df, file_type):
    mapped = get_mapped_fields(mapping_df)
    return [f for f in REQUIRED_BY_TYPE.get(file_type, []) if f not in mapped]


def missing_recommended_fields(mapping_df, file_type):
    mapped = get_mapped_fields(mapping_df)
    return [f for f in RECOMMENDED_BY_TYPE.get(file_type, []) if f not in mapped]


def mapping_blocking_issues(mapping_df, file_type):
    """Detecta mapeos peligrosos que pueden mezclar stock con movimientos."""
    issues = []
    if mapping_df is None or mapping_df.empty:
        return issues

    if file_type == "movimientos":
        quantity_rows = mapping_df[mapping_df["CampoCLC"] == "CantidadOriginal"]
        for _, row in quantity_rows.iterrows():
            original_column = str(row["ColumnaOriginal"])
            normalized_column = normalize_text(original_column)
            looks_like_stock = (
                normalized_column in STOCK_LIKE_QUANTITY_COLUMNS
                or "stock" in normalized_column
                or "saldo" in normalized_column
                or "existencia" in normalized_column
                or "inventario" in normalized_column
            )
            if looks_like_stock:
                issues.append(
                    f"En movimientos, la columna '{original_column}' parece ser stock/saldo, "
                    "no una cantidad de movimiento. Usar una columna de movimientos netos "
                    "(por ejemplo: Cantidad)."
                )

    return issues

def slugify(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text.strip().lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")

st.set_page_config(
    page_title="CLC Control Inteligente de Stock",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size: 2rem; font-weight: 700; }
[data-testid="stMetricLabel"] { font-size: 0.85rem; color: #888; }
.stDataFrame { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)


# ── Utilidades ────────────────────────────────────────────────────────────────

def find_available_reports():
    if not REPORTS_DIR.exists():
        return []
    return sorted(
        REPORTS_DIR.glob("control_stock_resultado*.xlsx"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )


def movements_path_for_report(report_path):
    """Parquet local con los movimientos usados por un reporte especifico."""
    path = Path(report_path)
    return path.with_name(f"{path.stem}_movimientos_calculo.parquet")


def metadata_path_for_report(report_path):
    """JSON local con parametros y totales de una corrida."""
    path = Path(report_path)
    return path.with_name(f"{path.stem}_metadata.json")


@st.cache_data(show_spinner=False)
def load_local_run_metadata(report_path_str):
    path = metadata_path_for_report(report_path_str)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


@st.cache_data(show_spinner="Cargando corrida de la nube...")
def load_cloud_control(archivo_control):
    """Descarga el control de una corrida guardada en la nube (parquet)."""
    return cloud_store.load_corrida_control(archivo_control)


@st.cache_data(show_spinner="Cargando movimientos...")
def load_active_movements(cloud_archivo_control_param, report_path_str):
    """
    Movimientos para las visualizaciones.

    - En modo NUBE: SOLO los movimientos de ESA corrida (nunca los locales, que
      pertenecen a otro cliente y mezclarían los datos).
    - En modo LOCAL: del parquet de movimientos guardado junto al reporte activo.

    Nunca lanza error: ante cualquier problema devuelve vacío (la página muestra solo KPIs).
    """
    try:
        if cloud_archivo_control_param:
            # Modo nube: solo lo guardado en la corrida. Sin fallback local.
            if hasattr(cloud_store, "load_corrida_movimientos"):
                df = cloud_store.load_corrida_movimientos(cloud_archivo_control_param)
                if df is not None and not df.empty:
                    return df
            return pd.DataFrame()
        if report_path_str:
            p = movements_path_for_report(report_path_str)
            if p.exists():
                return pd.read_parquet(p)
    except Exception:
        pass
    return pd.DataFrame()


@st.cache_data(show_spinner="Cargando movimientos...")
def load_viz_movimientos(cloud_archivo_control_param, report_path_str):
    """Alias semantico para visualizaciones: usa los movimientos activos."""
    return load_active_movements(cloud_archivo_control_param, report_path_str)


@st.cache_data(show_spinner=False)
def prepare_viz_movimientos(cloud_archivo_control_param, report_path_str):
    """
    Movimientos ya enriquecidos para el dashboard (Tipo, Cantidad, Fecha_dt, Dia).

    PERFORMANCE: este enriquecimiento (to_numeric, parseo de fechas y clasificacion
    de tipos sobre cientos de miles de filas) NO depende de los filtros del dashboard
    y antes se rehacia en cada click. Cacheado por corrida, corre una sola vez.
    La clasificacion de hoja generica es vectorizada (sin apply fila por fila).
    """
    mov = load_viz_movimientos(cloud_archivo_control_param, report_path_str)
    if mov is None or mov.empty:
        return pd.DataFrame()
    mov = mov.copy()
    mov["Cantidad"] = pd.to_numeric(mov.get("CantidadNormalizada"), errors="coerce").fillna(0)
    tipo = mov.get("TipoMovimiento", pd.Series("", index=mov.index)).fillna("").astype(str).str.strip()
    hoja = mov.get("HojaOrigen", pd.Series("", index=mov.index)).fillna("").astype(str).str.strip()
    # Tipo = TipoMovimiento (si esta); si no, el nombre de la hoja de origen.
    # Los nombres de hoja genericos (Sheet, Hoja1, etc.) no son un tipo real:
    # se muestran como "Sin tipo" para no ensuciar el desglose.
    GENERICOS = {"sheet", "sheet1", "sheet 1", "hoja", "hoja1", "hoja 1", "hoja1 ", "", "nan"}
    hoja_limpia = hoja.where(~hoja.str.lower().isin(GENERICOS), "Sin tipo")
    mov["Tipo"] = tipo.where(tipo != "", hoja_limpia).replace("", "Sin tipo")
    mov["Fecha_dt"] = pd.to_datetime(mov.get("Fecha"), errors="coerce")
    mov["Dia"] = mov["Fecha_dt"].dt.date
    return mov


@st.cache_data(show_spinner=False)
def prepare_mov_periodo(cache_key, incluye_inicial, fecha_inicial, fecha_final, _mov):
    """
    Movimientos del periodo (entre foto inicial y final) cacheados por corrida.

    PERFORMANCE: el recorte (mascara booleana + copy sobre cientos de miles de filas)
    se rehacia en cada rerun del dashboard. La ventana depende solo de la corrida y
    del flag de fecha inicial; `_mov` no se hashea (es grande). Logica identica.
    """
    if _mov is None or _mov.empty or "Fecha_dt" not in _mov.columns:
        return pd.DataFrame(columns=["Cantidad", "Fecha_dt"])
    if incluye_inicial:
        desde_inicial = _mov["Fecha_dt"] >= fecha_inicial
    else:
        desde_inicial = _mov["Fecha_dt"] > fecha_inicial
    en_periodo = desde_inicial & (_mov["Fecha_dt"] <= fecha_final)
    return _mov[en_periodo].copy()


@st.cache_data(show_spinner=False)
def prepare_balance_control(cache_key, _control_full):
    """
    Fotos del balance de masa (inicial/final) calculadas una sola vez por corrida.

    PERFORMANCE: antes se copiaba y re-parseaba control_full en cada rerun. `cache_key`
    identifica la corrida; `_control_full` lleva guion bajo para que Streamlit no lo
    hashee (es grande). Devuelve (fecha_inicial, fecha_final, fin, stock_inicial,
    stock_final_foto). La logica de calculo es identica a la anterior.
    """
    cf = _control_full.copy()
    cf["Fecha_dt"] = pd.to_datetime(cf["Fecha"], errors="coerce")
    cf["_StockInf_num"] = pd.to_numeric(cf.get("StockInformado"), errors="coerce").fillna(0)
    fecha_inicial = cf["Fecha_dt"].min()
    fecha_final = cf["Fecha_dt"].max()
    fin = cf[cf["Fecha_dt"] == fecha_final].copy()
    for c in ["StockInicial", "MovimientosAcumulados", "StockCalculado", "StockInformado", "Diferencia"]:
        fin[c] = pd.to_numeric(fin.get(c), errors="coerce").fillna(0)
    stock_inicial = cf.loc[cf["Fecha_dt"] == fecha_inicial, "_StockInf_num"].sum()
    stock_final_foto = cf.loc[cf["Fecha_dt"] == fecha_final, "_StockInf_num"].sum()
    return fecha_inicial, fecha_final, fin, stock_inicial, stock_final_foto


@st.cache_data(show_spinner=False)
def cached_calculation_consistency(cache_key, include_initial, _control_full, _movements):
    """
    Auditoria de consistencia cacheada por corrida.

    PERFORMANCE: build_calculation_consistency hace merge + recalculo sobre todos los
    puntos de control y se llamaba en cada rerun (incluso clickeando el dashboard, que
    no la usa). Cacheada por corrida + flag de fecha inicial. Logica intacta.
    """
    return build_calculation_consistency(
        _control_full, _movements, include_initial_date_movements=include_initial,
    )


@st.cache_data(show_spinner="Cargando reporte...")
def load_report(path_str):
    """Carga las hojas livianas del reporte. Las pesadas (movimientos) se leen aparte."""
    path = Path(path_str)
    # Se excluye movimientos_no_aplicados a propósito: puede tener cientos de miles
    # de filas y haría lento cada cambio de pantalla. Se carga bajo demanda.
    sheets = [
        "control_stock", "resumen", "solo_diferencias",
        "duplicados_movimientos", "consistencia_calculo", "advertencias",
    ]
    data = {}
    for sheet in sheets:
        try:
            data[sheet] = pd.read_excel(path, sheet_name=sheet)
        except Exception:
            data[sheet] = pd.DataFrame()
    return data


@st.cache_data(show_spinner="Cargando hoja...")
def load_report_sheet(path_str, sheet_name):
    """Lee una sola hoja del reporte, bajo demanda (para hojas grandes)."""
    try:
        return pd.read_excel(Path(path_str), sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame()


# El argumento _file_bytes empieza con guion bajo para que Streamlit NO lo hashee
# (hashear 16 MB en cada llamada sería lento). La cache se indexa por nombre + tamaño.
@st.cache_data(show_spinner=False)
def get_sheet_names_cached(name, size, _file_bytes):
    """Detecta las hojas de un Excel una sola vez por archivo subido."""
    return pd.ExcelFile(io.BytesIO(_file_bytes)).sheet_names


@st.cache_data(show_spinner=False)
def read_uploaded_sheet(name, size, sheet_name, _file_bytes):
    """Lee una hoja de un archivo subido, con cache para no releer en cada rerun."""
    if name.lower().endswith(".csv"):
        return pd.read_csv(io.BytesIO(_file_bytes))
    return pd.read_excel(io.BytesIO(_file_bytes), sheet_name=sheet_name)


# Queries a Supabase cacheadas 60 s: evita ir a la red en cada re-render
# (cada click en un filtro de pills dispara un re-run completo).
@st.cache_data(ttl=60, show_spinner=False)
def _cached_list_clientes():
    return cloud_store.list_clientes()

@st.cache_data(ttl=60, show_spinner=False)
def _cached_list_corridas(cliente_id):
    return cloud_store.list_corridas(cliente_id)

@st.cache_data(ttl=300, show_spinner=False)
def _cached_load_corrida_by_id(corrida_id):
    return cloud_store.load_corrida_by_id(corrida_id)

@st.cache_data(ttl=300, show_spinner=False)
def _cached_get_cliente_nombre(cliente_id):
    return cloud_store.get_cliente_nombre(cliente_id)


def build_claude_context(data, report_name):
    resumen = data["resumen"].iloc[0].to_dict() if not data["resumen"].empty else {}
    control = data["control_stock"]
    diffs = data["solo_diferencias"]
    advertencias = data["advertencias"]
    duplicados = data["duplicados_movimientos"]
    consistencia = data.get("consistencia_calculo", pd.DataFrame())

    total = int(resumen.get("total_registros_controlados", 0))
    ok = int(resumen.get("total_registros_ok", 0))
    con_diff = int(resumen.get("total_registros_con_diferencia", 0))
    abs_diff = resumen.get("total_diferencia_absoluta", 0)
    pct_ok = ok / total * 100 if total else 0

    periodo = ""
    if not control.empty and "Fecha" in control.columns:
        fechas = pd.to_datetime(control["Fecha"], errors="coerce").dropna()
        if not fechas.empty:
            periodo = f"Período controlado: {fechas.min().date()} a {fechas.max().date()}"

    top_diffs_str = "Sin diferencias."
    if not diffs.empty:
        top = (
            diffs.groupby(["CodigoArticulo", "Descripcion"], dropna=False)
            .agg(DiferenciaAbsoluta=("DiferenciaAbsoluta", "sum"), Diferencia=("Diferencia", "sum"))
            .nlargest(20, "DiferenciaAbsoluta")
            .reset_index()
        )
        top_diffs_str = top.to_string(index=False)

    estados_str = control["EstadoControl"].value_counts().to_string() if not control.empty else ""

    deposito_str = ""
    if not diffs.empty and "Deposito" in diffs.columns:
        dep = (
            diffs.groupby("Deposito", dropna=False)
            .agg(registros=("Diferencia", "count"), diferencia_abs=("DiferenciaAbsoluta", "sum"))
            .sort_values("diferencia_abs", ascending=False)
        )
        deposito_str = dep.to_string()

    adv_str = advertencias.to_string(index=False) if not advertencias.empty else "Sin advertencias."
    dup_str = (
        f"{len(duplicados)} posibles movimientos duplicados detectados; no se eliminaron del calculo."
        if not duplicados.empty else "Sin duplicados."
    )
    if not consistencia.empty and "EstadoConsistencia" in consistencia.columns:
        revisar_cons = int((consistencia["EstadoConsistencia"] == "Revisar").sum())
        consistencia_str = (
            "Consistencia interna OK."
            if revisar_cons == 0
            else f"Consistencia interna a revisar: {revisar_cons} filas no cierran contra los movimientos guardados."
        )
    else:
        consistencia_str = "Sin auditoria interna de consistencia disponible para esta corrida."

    return f"""Sos un asistente experto en control de stock logístico para CLC Consultora Logística.
Respondé siempre en español rioplatense, de forma clara, concreta y profesional.
Cuando haya datos numéricos relevantes, incluilos en tu respuesta.
No inventes datos: si necesitás un dato puntual que no está en este resumen, usá las herramientas.

TENÉS HERRAMIENTAS PARA CONSULTAR LA DATA COMPLETA:
- `buscar_producto(codigo)`: trae todas las filas de control de un código o producto (stock inicial, movimientos acumulados, stock calculado, stock informado, diferencia, fechas, depósito). Usala SIEMPRE que pregunten por un producto/código específico, por qué tiene varias filas o por qué tiene diferencia.
- `buscar_movimientos(codigo)`: trae el detalle y los totales de movimientos de un código.
Este resumen de abajo cubre lo general; para cualquier detalle puntual de un producto, llamá a las herramientas antes de responder. No digas que no podés acceder a la data: podés, con las herramientas.

REPORTE ACTIVO: {report_name}
{periodo}

RESUMEN GENERAL:
- Total registros controlados: {total:,}
- Registros OK (diferencia = 0): {ok:,} ({pct_ok:.1f}%)
- Registros con diferencia: {con_diff:,}
- Diferencia absoluta total: {abs_diff:,.0f} unidades

DISTRIBUCIÓN POR ESTADO:
{estados_str}

DIFERENCIAS POR DEPÓSITO:
{deposito_str if deposito_str else 'Sin datos por depósito.'}

TOP 20 SKUs CON MAYOR DIFERENCIA ABSOLUTA:
{top_diffs_str}

ADVERTENCIAS DEL SISTEMA:
{adv_str}

DUPLICADOS:
{dup_str}

CONSISTENCIA INTERNA:
{consistencia_str}
"""


def clasificar_tipos_movimiento(tipos, api_key):
    """
    Pide a Claude clasificar cada TIPO de movimiento como ingreso/egreso/revisar.
    Devuelve un dict {tipo_exacto: 'ingreso'|'egreso'|'revisar'}.
    Solo manda los tipos distintos (no las filas), así es rápido y barato.
    """
    client = anthropic.Anthropic(api_key=api_key)
    lista = "\n".join(f"- {t}" for t in tipos)
    prompt = (
        "Sos un experto en logística y control de stock. Te paso una lista de TIPOS DE "
        "MOVIMIENTO de un sistema de inventario. Clasificá CADA UNO según su efecto en el stock:\n"
        "- 'ingreso': SIEMPRE aumenta el stock (ej. recepción, compra, alta, devolución de cliente).\n"
        "- 'egreso': SIEMPRE disminuye el stock (ej. venta, salida, remito de salida, despacho, baja, consumo).\n"
        "- 'mantener': tipos que pueden aumentar O disminuir según el caso. El típico es "
        "'AJUSTE' / 'Ajuste de inventario' (puede ser positivo o negativo). Para estos se respeta "
        "el signo que ya trae cada movimiento, no se fuerza una dirección.\n"
        "- 'revisar': si no estás seguro o es ambiguo.\n\n"
        f"Tipos:\n{lista}\n\n"
        "Respondé SOLO con un objeto JSON válido, sin texto extra, con la forma "
        '{"<tipo exacto tal cual te lo pasé>": "ingreso"|"egreso"|"mantener"|"revisar", ...}.'
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    inicio = text.find("{")
    if inicio == -1:
        return {}
    try:
        # raw_decode lee el primer objeto JSON e ignora cualquier texto extra después.
        obj, _ = json.JSONDecoder().raw_decode(text[inicio:])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ── Herramientas que la IA puede usar para consultar la data del reporte ───────

CHAT_TOOLS = [
    {
        "name": "buscar_producto",
        "description": (
            "Busca un producto/código de artículo en la tabla de control de stock y "
            "devuelve TODAS sus filas (puede tener varias fechas/depósitos), con stock "
            "inicial, movimientos acumulados, stock calculado, stock informado, diferencia "
            "y estado. Usala siempre que te pregunten por un código o producto puntual, "
            "por qué tiene varias filas/stocks, o por qué tiene diferencia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "codigo": {
                    "type": "string",
                    "description": "Código del artículo (completo o parte). También sirve parte de la descripción.",
                }
            },
            "required": ["codigo"],
        },
    },
    {
        "name": "buscar_movimientos",
        "description": (
            "Devuelve el detalle de movimientos de un código de artículo (fechas y "
            "cantidades netas), con totales de entradas y salidas. Usala cuando pregunten "
            "qué movimientos tuvo un producto o por qué su stock calculado da cierto valor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "codigo": {"type": "string", "description": "Código del artículo (completo o parte)."}
            },
            "required": ["codigo"],
        },
    },
]


def _json_safe_records(df, columns, limit=50):
    sub = df[[c for c in columns if c in df.columns]].head(limit).copy()
    for c in sub.columns:
        sub[c] = sub[c].astype(str)
    return sub.to_dict("records")


def tool_buscar_producto(control_df, codigo):
    if control_df is None or control_df.empty:
        return {"encontrado": False, "mensaje": "No hay tabla de control cargada."}
    raw = str(codigo).strip()
    norm = normalize_code(raw)
    codes = control_df["CodigoArticulo"].astype(str)
    # Exacto (normalizado), luego contiene (texto crudo o en descripción)
    sub = control_df[codes.str.upper() == norm.upper()]
    if sub.empty:
        mask = codes.str.contains(re.escape(raw), case=False, na=False)
        if "Descripcion" in control_df.columns:
            mask = mask | control_df["Descripcion"].astype(str).str.contains(re.escape(raw), case=False, na=False)
        sub = control_df[mask]
    if sub.empty:
        return {"encontrado": False, "mensaje": f"No se encontró ningún producto para '{codigo}'."}

    cols = [
        "Fecha", "CodigoArticulo", "Descripcion", "Deposito", "FechaInicial",
        "StockInicial", "MovimientosAcumulados", "StockCalculado",
        "StockInformado", "Diferencia", "DiferenciaAbsoluta", "EstadoControl",
    ]
    return {
        "encontrado": True,
        "cantidad_filas": int(len(sub)),
        "explicacion_filas": (
            "Si hay más de una fila para el mismo código suele ser por fechas de stock "
            "distintas (ej. cierre inicial y cierre final del período) o por depósitos distintos."
        ),
        "registros": _json_safe_records(sub, cols, limit=50),
    }


@st.cache_data(show_spinner=False)
def _load_movimientos_norm(path_str):
    p = Path(path_str)
    if p.exists():
        return pd.read_parquet(p)
    xlsx_path = p.with_suffix(".xlsx")
    if xlsx_path.exists():
        return pd.read_excel(xlsx_path)
    return pd.DataFrame()


def load_movimientos_for_sign_config():
    """
    Durante el paso de procesamiento todavia no hay corrida activa.
    Para configurar signos por tipo se usa el normalizado recien generado.
    """
    return _load_movimientos_norm(str(NORMALIZED_DIR / "movimientos_normalizado.parquet"))


def tool_buscar_movimientos(codigo, movements_df=None):
    mov = movements_df.copy() if movements_df is not None else pd.DataFrame()
    if mov.empty:
        return {
            "disponible": False,
            "mensaje": "No hay movimientos de calculo guardados para la corrida activa. Reejecuta el analisis.",
        }
    raw = str(codigo).strip()
    norm = normalize_code(raw)
    codes = mov["CodigoArticulo"].astype(str)
    sub = mov[codes.str.upper() == norm.upper()]
    if sub.empty:
        sub = mov[codes.str.contains(re.escape(raw), case=False, na=False)]
    if sub.empty:
        return {"encontrado": False, "mensaje": f"No hay movimientos para '{codigo}'."}

    qty = pd.to_numeric(sub.get("CantidadNormalizada"), errors="coerce").fillna(0)
    cols = [
        "Fecha", "CodigoArticulo", "Descripcion", "Deposito",
        "TipoMovimiento", "CantidadNormalizada", "Documento",
        "ArchivoOrigen", "HojaOrigen",
    ]
    return {
        "encontrado": True,
        "cantidad_movimientos": int(len(sub)),
        "suma_neta": float(qty.sum()),
        "total_entradas": float(qty[qty > 0].sum()),
        "total_salidas": float(qty[qty < 0].sum()),
        "muestra_movimientos": _json_safe_records(sub, cols, limit=40),
        "nota": "Se muestran hasta 40 movimientos de ejemplo; los totales consideran todos.",
    }


def build_movement_trace(control_df, codigo, movements_df=None, selected_index=None):
    """Reconstruye, fila por fila, como se forma MovimientosAcumulados."""
    if control_df is None or control_df.empty:
        return None, pd.DataFrame(), pd.DataFrame(), "No hay reporte de control cargado."

    raw = str(codigo).strip()
    if not raw:
        return None, pd.DataFrame(), pd.DataFrame(), "Ingresá un código de producto."

    norm = normalize_code(raw)
    codes = control_df["CodigoArticulo"].astype(str)
    control_rows = control_df[codes.str.upper() == norm.upper()].copy()
    if control_rows.empty:
        mask = codes.str.contains(re.escape(raw), case=False, na=False)
        if "Descripcion" in control_df.columns:
            mask = mask | control_df["Descripcion"].astype(str).str.contains(re.escape(raw), case=False, na=False)
        control_rows = control_df[mask].copy()
    if control_rows.empty:
        return None, pd.DataFrame(), pd.DataFrame(), f"No se encontró '{codigo}' en el reporte activo."

    control_rows["Fecha_dt"] = pd.to_datetime(control_rows["Fecha"], errors="coerce")
    if "FechaInicial" in control_rows.columns:
        control_rows["FechaInicial_dt"] = pd.to_datetime(control_rows["FechaInicial"], errors="coerce")
    else:
        control_rows["FechaInicial_dt"] = pd.NaT

    control_rows = control_rows.sort_values(["Fecha_dt", "Deposito"], na_position="last")
    if selected_index is None or selected_index not in control_rows.index:
        non_base = control_rows[control_rows["Fecha_dt"] != control_rows["FechaInicial_dt"]]
        selected_index = non_base.index[-1] if not non_base.empty else control_rows.index[-1]
    control_row = control_rows.loc[selected_index]

    movements = movements_df.copy() if movements_df is not None else pd.DataFrame()
    if movements.empty:
        return control_row, pd.DataFrame(), pd.DataFrame(), (
            "No hay movimientos de calculo guardados para esta corrida. "
            "Reejecuta el analisis para habilitar la traza exacta."
        )
    if "CodigoArticulo" not in movements.columns:
        return control_row, pd.DataFrame(), pd.DataFrame(), "El normalizado de movimientos no tiene CodigoArticulo."

    mov = movements.copy()
    control_code = normalize_code(control_row["CodigoArticulo"])
    mov["CodigoArticuloNorm"] = mov["CodigoArticulo"].apply(normalize_code)
    mov = mov[mov["CodigoArticuloNorm"].astype(str).str.upper() == str(control_code).upper()].copy()

    if str(control_row.get("Deposito", "")).strip().upper() != "TODOS" and "Deposito" in mov.columns:
        deposito_control = str(control_row.get("Deposito", "")).strip().upper()
        mov = mov[mov["Deposito"].fillna("").astype(str).str.strip().str.upper() == deposito_control].copy()

    if mov.empty:
        return control_row, pd.DataFrame(), pd.DataFrame(), "No hay movimientos normalizados para ese producto."

    if "Fecha" not in mov.columns:
        mov["Fecha"] = ""
    mov["Fecha_dt"] = pd.to_datetime(mov["Fecha"], errors="coerce")
    cantidad_final = pd.to_numeric(mov.get("CantidadNormalizada"), errors="coerce")
    if "CantidadOriginal" in mov.columns:
        cantidad_original = pd.to_numeric(mov["CantidadOriginal"], errors="coerce")
        mov["CantidadNormalizada"] = cantidad_final.where(cantidad_final.notna(), cantidad_original)
    else:
        mov["CantidadNormalizada"] = cantidad_final
    fecha_inicial = pd.to_datetime(control_row.get("FechaInicial"), errors="coerce")
    fecha_control = pd.to_datetime(control_row.get("Fecha"), errors="coerce")

    dated_in_period = (
        mov["Fecha_dt"].notna()
        & mov["CantidadNormalizada"].notna()
        & (mov["Fecha_dt"] > fecha_inicial)
        & (mov["Fecha_dt"] <= fecha_control)
    )
    undated_in_period = (
        mov["Fecha_dt"].isna()
        & mov["CantidadNormalizada"].notna()
        & (fecha_control > fecha_inicial)
    )
    included_mask = dated_in_period | undated_in_period
    incluidos = mov[included_mask].copy()
    excluidos = mov[~included_mask].copy()
    if not incluidos.empty:
        incluidos["Fecha"] = incluidos["Fecha"].where(incluidos["Fecha_dt"].notna(), "Sin fecha")

    sort_cols = ["Fecha_dt"]
    if "Documento" in incluidos.columns:
        sort_cols.append("Documento")
    incluidos = incluidos.sort_values(sort_cols, na_position="last").reset_index(drop=True)
    incluidos["Paso"] = range(1, len(incluidos) + 1)
    incluidos["Operacion"] = incluidos["CantidadNormalizada"].apply(
        lambda value: f"+{value:g}" if value >= 0 else f"{value:g}"
    )
    incluidos["Acumulado"] = incluidos["CantidadNormalizada"].cumsum()

    detalle_cols = [
        "Paso", "Fecha", "CodigoArticulo", "Descripcion", "Deposito", "TipoMovimiento",
        "CantidadOriginal", "CantidadNormalizada", "Operacion", "Acumulado",
        "Documento", "ArchivoOrigen", "HojaOrigen",
    ]
    incluidos = incluidos[[c for c in detalle_cols if c in incluidos.columns]]

    if not excluidos.empty:
        excluidos["MotivoExclusion"] = "Fuera del periodo calculado o dato invalido"
        excluidos.loc[excluidos["Fecha_dt"].isna(), "MotivoExclusion"] = "Fecha invalida o vacia"
        excluidos.loc[excluidos["CantidadNormalizada"].isna(), "MotivoExclusion"] = "Cantidad invalida o vacia"
        excluidos.loc[excluidos["Fecha_dt"] <= fecha_inicial, "MotivoExclusion"] = "En fecha inicial o anterior"
        excluidos.loc[excluidos["Fecha_dt"] > fecha_control, "MotivoExclusion"] = "Posterior a la fecha controlada"
        excluded_cols = [
            "Fecha", "CodigoArticulo", "Deposito", "TipoMovimiento",
            "CantidadOriginal", "CantidadNormalizada", "Documento",
            "ArchivoOrigen", "HojaOrigen", "MotivoExclusion",
        ]
        excluidos = excluidos[[c for c in excluded_cols if c in excluidos.columns]]

    return control_row, incluidos, excluidos, ""


def run_chat_tool(name, tool_input, control_df, movements_df=None):
    try:
        if name == "buscar_producto":
            return tool_buscar_producto(control_df, tool_input.get("codigo", ""))
        if name == "buscar_movimientos":
            return tool_buscar_movimientos(tool_input.get("codigo", ""), movements_df)
        return {"error": f"Herramienta desconocida: {name}"}
    except Exception as exc:
        return {"error": f"Fallo al ejecutar {name}: {exc}"}


def export_normalized_to_excel():
    """Convierte los normalizados .parquet a .xlsx, para abrir en Excel/Power BI."""
    nombres = {
        "maestro_normalizado": "maestro_normalizado",
        "stock_normalizado": "stock_normalizado",
        "movimientos_normalizado": "movimientos_normalizado",
    }
    generados = []
    for base in nombres:
        parquet_path = NORMALIZED_DIR / f"{base}.parquet"
        if parquet_path.exists():
            df = pd.read_parquet(parquet_path)
            xlsx_path = NORMALIZED_DIR / f"{base}.xlsx"
            df.to_excel(xlsx_path, index=False)
            generados.append(xlsx_path.name)
    return generados


def reset_proc_state():
    """Reinicia el asistente de Procesar para empezar de cero con otros archivos."""
    for k in [
        "proc_step", "proc_entries", "proc_mappings", "proc_current_mappings",
        "proc_normalized", "proc_log", "proc_result", "proc_report_name", "proc_sign_class",
    ]:
        st.session_state.pop(k, None)
    st.session_state.proc_step = "upload"


def color_estado(val):
    colors = {
        "OK": "background-color: #d4edda; color: #155724",
        "Revisar": "background-color: #fff3cd; color: #856404",
        "Critico": "background-color: #f8d7da; color: #721c24",
        "Sin datos": "background-color: #e2e3e5; color: #383d41",
    }
    return colors.get(val, "")


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📦 CLC Control de Stock")
    st.divider()

    if "pending_nav_page" in st.session_state:
        st.session_state.nav_page = st.session_state.pop("pending_nav_page")

    page = st.radio(
        "Navegación",
        ["⚙️ Procesar", "📊 Resumen", "🔍 Detalle", "📈 Visualización de datos", "💬 Consultar con IA"],
        label_visibility="collapsed",
        key="nav_page",
    )

    if page == "⚙️ Procesar":
        paso_actual = st.session_state.get("proc_step", "upload")
        if paso_actual != "upload":
            st.caption(f"Paso actual: {paso_actual}")
            if st.button("↺ Cancelar y empezar de nuevo", use_container_width=True):
                reset_proc_state()
                st.rerun()

    st.divider()

    # Fuentes de reporte: la NUBE (persistente, historial por cliente) o LOCAL
    # (archivos en data/reports, que en la nube son efímeros).
    report_path = None          # ruta local (si se usa fuente local)
    cloud_control_full = None    # control cargado desde la nube (si se usa fuente nube)
    cloud_archivo_control = None  # ruta del control en la nube (para cargar sus movimientos)
    cloud_include_initial = False
    local_run_metadata = {}
    report_label = None
    cloud_on = cloud_store.is_configured()

    # Query params: ?cliente=<nombre> permite compartir o pre-seleccionar un cliente
    _qp = st.query_params

    fuente = "Nube"
    if cloud_on:
        fuente = st.radio("Fuente de reportes", ["Nube", "Local"], horizontal=True)
    else:
        fuente = "Local"

    if fuente == "Nube":
        try:
            clientes = _cached_list_clientes()
        except Exception as exc:
            clientes = []
            st.warning(f"No se pudo leer la nube: {exc}")

        if clientes:
            nombres = {c["nombre"]: c["id"] for c in clientes}

            # Si viene ?cliente=X en la URL, pre-seleccionar ese cliente
            _qp_cliente = _qp.get("cliente", "")
            _default_idx = list(nombres.keys()).index(_qp_cliente) if _qp_cliente in nombres else 0

            cli_sel = st.selectbox("Cliente", list(nombres.keys()), index=_default_idx)

            # Actualizar la URL con el cliente seleccionado (link compartible)
            st.query_params["cliente"] = cli_sel

            try:
                corridas = _cached_list_corridas(nombres[cli_sel])
            except Exception:
                corridas = []

            if corridas:
                # Siempre carga la más reciente (primera en la lista, que viene desc)
                corrida = corridas[0]
                cloud_include_initial = bool(
                    (corrida.get("parametros") or {}).get("include_initial_date_movements", False)
                )
                try:
                    cloud_control_full = load_cloud_control(corrida["archivo_control"])
                    cloud_archivo_control = corrida["archivo_control"]
                    fecha = str(corrida.get("creado_en", ""))[:16].replace("T", " ")
                    report_label = f"{cli_sel} · {fecha}"
                    st.success(f"Datos actuales de **{cli_sel}** · procesados el {fecha}")
                except Exception as exc:
                    st.error(f"No se pudo cargar los datos: {exc}")
            else:
                st.warning(f"**{cli_sel}** todavía no tiene datos guardados. Procesalo en **Procesar**.")
        else:
            st.info("Todavía no hay clientes en la nube. Generá un análisis en **Procesar**.")
    else:
        reports = find_available_reports()
        if reports:
            report_names = [r.name for r in reports]
            selected_name = st.selectbox("Reporte activo", report_names)
            report_path = REPORTS_DIR / selected_name
            local_run_metadata = load_local_run_metadata(str(report_path))
            mtime = pd.Timestamp(report_path.stat().st_mtime, unit="s").strftime("%d/%m/%Y %H:%M")
            st.caption(f"Generado: {mtime}")
            report_label = selected_name
            st.success("Reporte local cargado")
        else:
            st.warning("Sin reporte local en esta sesión. Generá uno en **Procesar**.")

    hay_reporte = (cloud_control_full is not None and not cloud_control_full.empty) or (report_path is not None)

    # Las "líneas de stock inicial" (Fecha == FechaInicial) son la foto base de cada
    # producto: siempre dan diferencia 0 y no son un control real. Por defecto se ocultan
    # para ver una línea por producto.
    mostrar_lineas_base = False
    if hay_reporte:
        st.divider()
        mostrar_lineas_base = st.toggle(
            "Mostrar líneas de stock inicial",
            value=False,
            help="Las líneas de stock inicial son la foto base de cada producto (siempre "
                 "diferencia 0). Ocultas, ves un control real por producto.",
        )

    # Opción de análisis disponible desde el panel izquierdo (se aplica al ejecutar
    # el análisis en Procesar). Por defecto False (stock inicial = cierre del período).
    incluir_mov_inicial = False
    if page == "⚙️ Procesar":
        st.divider()
        st.markdown("**Opciones de análisis**")
        incluir_mov_inicial = st.toggle(
            "Incluir movimientos de la fecha inicial",
            value=False,
            help="Activalo si el stock inicial es apertura del día (suma los movimientos de "
                 "esa misma fecha). Desactivado: stock inicial = cierre del período.",
        )

    st.divider()
    if cloud_store.is_configured():
        st.caption("☁️ Nube: conectada")
    else:
        st.caption("💾 Modo local (nube no configurada)")
    st.caption("CLC Consultora Logística · v1.5")


# ── Sin reporte (solo bloquea páginas que lo necesitan) ───────────────────────

if not hay_reporte and page != "⚙️ Procesar":
    st.title("📦 CLC Control Inteligente de Stock")
    st.info("No hay reporte para mostrar. Generá un análisis en **⚙️ Procesar** "
            "o elegí una corrida guardada en el panel izquierdo.")
    st.stop()

def marcar_lineas_base(control_df):
    """Marca como base las filas donde Fecha == FechaInicial (foto inicial, diff 0)."""
    if control_df.empty or "Fecha" not in control_df.columns or "FechaInicial" not in control_df.columns:
        return pd.Series(False, index=control_df.index)
    f = pd.to_datetime(control_df["Fecha"], errors="coerce")
    fi = pd.to_datetime(control_df["FechaInicial"], errors="coerce")
    return f.notna() & fi.notna() & (f == fi)


# En "Procesar" no se necesita el reporte: evitamos leer Excel grande en cada rerun.
if hay_reporte and page != "⚙️ Procesar":
    active_movements = load_active_movements(
        cloud_archivo_control,
        str(report_path) if report_path is not None else "",
    )
    # Clave estable de la corrida: identifica nube o local para cachear los
    # preprocesamientos pesados (consistencia, fotos del balance) por corrida.
    _corrida_key = cloud_archivo_control or (str(report_path) if report_path is not None else "")
    # La auditoria de consistencia (merge + recalculo) solo la consumen el Resumen
    # y el chat. En el dashboard y el detalle no se usa: la diferimos para no pagar
    # ni siquiera la copia del dataframe cacheado en cada interaccion del dashboard.
    _needs_consistencia = page in ("📊 Resumen", "💬 Consultar con IA")
    if cloud_control_full is not None:
        # Reporte cargado desde la nube (parquet con la tabla de control completa).
        # Reconstruimos las hojas que usan el chat y el detalle a partir del control.
        control_full = cloud_control_full
        dif_full = pd.to_numeric(control_full.get("Diferencia"), errors="coerce").fillna(0)
        resumen_cloud = pd.DataFrame([{
            "total_registros_controlados": len(control_full),
            "total_registros_ok": int((control_full.get("EstadoControl") == "OK").sum()) if "EstadoControl" in control_full.columns else 0,
            "total_registros_con_diferencia": int((dif_full != 0).sum()),
            "total_diferencia_absoluta": pd.to_numeric(control_full.get("DiferenciaAbsoluta"), errors="coerce").fillna(0).sum(),
        }])
        data = {
            "control_stock": control_full,
            "solo_diferencias": control_full[dif_full != 0].copy(),
            "resumen": resumen_cloud,
            "advertencias": pd.DataFrame(),
            "duplicados_movimientos": pd.DataFrame(),
            "consistencia_calculo": (
                cached_calculation_consistency(
                    _corrida_key, bool(cloud_include_initial),
                    control_full, active_movements,
                )
                if _needs_consistencia else pd.DataFrame()
            ),
        }
    else:
        data = load_report(str(report_path))
        control_full = data["control_stock"]
        if (_needs_consistencia
                and data.get("consistencia_calculo", pd.DataFrame()).empty
                and active_movements is not None and not active_movements.empty):
            params = local_run_metadata.get("parametros") or {}
            data["consistencia_calculo"] = cached_calculation_consistency(
                _corrida_key, bool(params.get("include_initial_date_movements", False)),
                control_full, active_movements,
            )
    es_base = marcar_lineas_base(control_full)
    lineas_base_ocultas = 0
    if mostrar_lineas_base:
        control = control_full
    else:
        control = control_full[~es_base].copy()
        lineas_base_ocultas = int(es_base.sum())

    # KPIs recalculados desde la vista actual (consistentes con lo que se muestra).
    total = len(control)
    ok_count = int((control["EstadoControl"] == "OK").sum()) if "EstadoControl" in control.columns else 0
    dif_num = pd.to_numeric(control.get("Diferencia"), errors="coerce").fillna(0) if not control.empty else pd.Series(dtype=float)
    diff_count = int((dif_num != 0).sum())
    abs_diff = pd.to_numeric(control.get("DiferenciaAbsoluta"), errors="coerce").fillna(0).sum() if not control.empty else 0
    pct_ok = ok_count / total * 100 if total else 0
    diffs = control[dif_num != 0].copy() if not control.empty else pd.DataFrame()
else:
    data = {}
    control = pd.DataFrame()
    control_full = pd.DataFrame()
    active_movements = pd.DataFrame()
    diffs = pd.DataFrame()
    lineas_base_ocultas = 0


# ══════════════════════════════════════════════════════════════════════════════
# PÁGINA: PROCESAR
# ══════════════════════════════════════════════════════════════════════════════

if page == "⚙️ Procesar":
    st.title("⚙️ Procesar archivos")

    # ── Estado del proceso ────────────────────────────────────────────────────
    for key, default in [
        ("proc_step", "upload"),
        ("proc_entries", []),       # [{file_name, sheet, file_type, df}]
        ("proc_mappings", {}),      # {entry_key: propuesta ESTABLE que alimenta el editor}
        ("proc_current_mappings", {}),  # {entry_key: mapeo con los cambios EN VIVO del editor}
        ("proc_normalized", {}),    # {tipo: df acumulado}
        ("proc_log", []),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    steps = ["upload", "mapping", "analyze", "done"]
    step_labels = ["1. Subir archivos", "2. Mapear columnas", "3. Analizar", "4. Listo"]
    current = st.session_state.proc_step
    cols_steps = st.columns(len(steps))
    for i, (s, label) in enumerate(zip(steps, step_labels)):
        style = "**" if s == current else ""
        cols_steps[i].markdown(f"{style}{label}{style}")

    st.divider()

    # ══ STEP 1: SUBIR ═════════════════════════════════════════════════════════
    if current == "upload":
        st.subheader("Subir archivos del cliente")

        limpiar = st.checkbox(
            "Reemplazar normalizados y reportes anteriores (se borran recién al generar los nuevos)",
            value=False,
            help="La limpieza se ejecuta solo cuando los nuevos normalizados ya están listos. "
                 "Nunca borra antes, así no perdés datos si algo falla.",
        )
        st.session_state.proc_limpiar = limpiar

        uploaded = st.file_uploader(
            "Arrastrá archivos o hacé click para seleccionar",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
        )

        if uploaded:
            entries = []
            for uf in uploaded:
                # getvalue() devuelve los bytes completos sin consumir el buffer.
                file_bytes = uf.getvalue()
                is_excel = uf.name.lower().endswith((".xlsx", ".xls"))
                with st.expander(f"📄 {uf.name}", expanded=True):
                    if not is_excel:
                        # CSV: una sola "hoja"
                        file_type = st.selectbox(
                            "Tipo de archivo",
                            ["stock", "movimientos", "maestro", "saltar"],
                            key=f"tipo_{uf.name}",
                        )
                        if file_type != "saltar":
                            entries.append({
                                "file_name": uf.name,
                                "file_bytes": file_bytes,
                                "file_type": file_type,
                                "sheet_name": None,
                            })
                        continue

                    # Excel: se pueden elegir TODAS las hojas que se quieran,
                    # y asignarle un tipo a cada una (stock / movimientos / maestro).
                    try:
                        sheets = get_sheet_names_cached(uf.name, len(file_bytes), file_bytes)
                    except Exception as e:
                        st.warning(f"No se pudieron leer las hojas: {e}")
                        sheets = []

                    if not sheets:
                        continue

                    hojas_sel = st.multiselect(
                        "Hojas a procesar (podés elegir varias)",
                        sheets,
                        default=sheets[:1],
                        key=f"hojas_{uf.name}",
                        help="Elegí cada hoja que quieras analizar. A cada una le asignás su tipo abajo.",
                    )
                    if not hojas_sel:
                        st.info("Elegí al menos una hoja para procesar este archivo.")

                    for hoja in hojas_sel:
                        c1, c2, c3 = st.columns([2, 1.5, 2])
                        c1.markdown(f"Hoja: **{hoja}**")
                        tipo_hoja = c2.selectbox(
                            "Tipo",
                            ["stock", "movimientos", "maestro"],
                            key=f"tipo_{uf.name}__{hoja}",
                            label_visibility="collapsed",
                        )
                        # Signo manual por hoja (solo para movimientos): útil cuando la hoja
                        # es toda de un tipo (devoluciones=+, salidas=−) y vienen en positivo.
                        signo_hoja = "mantener"
                        if tipo_hoja == "movimientos":
                            signo_label = c3.selectbox(
                                "Signo",
                                list(SIGNO_HOJA_OPCIONES.keys()),
                                key=f"signo_{uf.name}__{hoja}",
                                label_visibility="collapsed",
                                help="Cómo firmar los movimientos de esta hoja. 'Mantener' respeta "
                                     "el signo de cada fila (usalo para AJUSTES + o −).",
                            )
                            signo_hoja = SIGNO_HOJA_OPCIONES[signo_label]
                        entries.append({
                            "file_name": uf.name,
                            "file_bytes": file_bytes,
                            "file_type": tipo_hoja,
                            "sheet_name": hoja,
                            "signo_hoja": signo_hoja,
                        })

            if st.button("Continuar →", type="primary"):
                ok = False
                try:
                    INPUT_DIR.mkdir(parents=True, exist_ok=True)
                    a_procesar = [e for e in entries if e["file_type"] != "saltar"]
                    proc_entries = []
                    progress = st.progress(0.0, text="Leyendo archivos...")
                    for i, entry in enumerate(a_procesar):
                        progress.progress(
                            i / max(len(a_procesar), 1),
                            text=f"Leyendo {entry['file_name']} (hoja: {entry['sheet_name'] or 'única'})...",
                        )
                        # Guardar copia del original en data/input
                        (INPUT_DIR / entry["file_name"]).write_bytes(entry["file_bytes"])
                        df = read_uploaded_sheet(
                            entry["file_name"], len(entry["file_bytes"]),
                            entry["sheet_name"], entry["file_bytes"],
                        )
                        proc_entries.append({
                            "file_name": entry["file_name"],
                            "sheet_name": entry["sheet_name"] or "",
                            "file_type": entry["file_type"],
                            "signo_hoja": entry.get("signo_hoja", "mantener"),
                            "df": df,
                        })
                    progress.progress(1.0, text="Archivos leídos.")

                    if not proc_entries:
                        st.warning("No hay archivos para procesar (todos marcados como 'saltar').")
                    else:
                        st.session_state.proc_entries = proc_entries
                        st.session_state.proc_mappings = {}
                        st.session_state.proc_current_mappings = {}
                        st.session_state.pop("proc_sign_class", None)
                        st.session_state.proc_normalized = {"maestro": [], "stock": [], "movimientos": []}
                        st.session_state.proc_log = []
                        st.session_state.proc_step = "mapping"
                        ok = True
                except Exception as e:
                    st.error(f"Error procesando los archivos: {e}")

                if ok:
                    st.rerun()

    # ══ STEP 2: MAPEO ═════════════════════════════════════════════════════════
    elif current == "mapping":
        st.subheader("Revisar mapeo de columnas")
        st.caption(
            "Solo importan los campos **obligatorios** de cada tipo. "
            "Las columnas que queden en `PendienteConfirmacion` se ignoran y no bloquean el avance."
        )

        entries = st.session_state.proc_entries
        if not entries:
            st.warning("No hay archivos para mapear.")
            if st.button("← Volver"):
                st.session_state.proc_step = "upload"
                st.rerun()
        else:
            def entry_key(entry):
                return f"{entry['file_name']}__{entry['sheet_name']}__{entry['file_type']}"

            for entry in entries:
                key = entry_key(entry)
                file_type = entry["file_type"]
                label = f"**{entry['file_name']}**"
                if entry["sheet_name"]:
                    label += f" · Hoja: {entry['sheet_name']}"
                label += f" · Tipo: `{file_type}`"

                requeridos = REQUIRED_BY_TYPE.get(file_type, [])
                mapeo_previo = st.session_state.proc_current_mappings.get(
                    key, st.session_state.proc_mappings.get(key)
                )
                faltantes_previos = missing_required_fields(mapeo_previo, file_type) if mapeo_previo is not None else requeridos
                icono = "✅" if not faltantes_previos else "⚠️"

                with st.expander(f"{icono} {label}", expanded=bool(faltantes_previos)):
                    df = entry["df"]
                    st.caption(
                        f"{len(df):,} filas · {len(df.columns)} columnas · "
                        f"Obligatorios para `{file_type}`: {', '.join(requeridos)}"
                    )

                    # ── Detección de filas que no son datos (totales, recuentos) ──
                    # Clave en session_state para guardar la decisión del usuario.
                    excluir_key = f"excluir_footer__{key}"
                    footer_rows = detect_footer_rows(df)
                    if footer_rows:
                        if excluir_key not in st.session_state:
                            st.session_state[excluir_key] = True
                        filas_desc = "; ".join(
                            f"fila {r['idx'] + 1}: «{r['contenido']}» ({r['motivo']})"
                            for r in footer_rows
                        )
                        excluir = st.checkbox(
                            f"Excluir {len(footer_rows)} fila(s) al final que parecen no ser datos "
                            f"(totales / recuentos del sistema)",
                            value=st.session_state[excluir_key],
                            key=excluir_key,
                            help=f"Filas detectadas: {filas_desc}",
                        )
                        if excluir:
                            indices_excluir = {r["idx"] for r in footer_rows}
                            df = df[~df.index.isin(indices_excluir)].reset_index(drop=True)
                            entry["df"] = df
                            st.info(
                                f"Se excluirán {len(footer_rows)} fila(s) al final. "
                                f"La hoja tendrá {len(df):,} filas al normalizarse."
                            )
                        else:
                            st.warning(
                                f"Se incluirán las filas al final detectadas como posibles totales: {filas_desc}. "
                                f"Si son filas de datos reales, podés dejar este casillero desmarcado."
                            )

                    opciones_clc = clc_options_for(file_type)
                    if key not in st.session_state.proc_mappings:
                        propuesta = propose_column_mapping(df.columns, DICTIONARY_PATH)
                        # Solo se permiten los campos válidos para este tipo de hoja:
                        # cualquier otra propuesta (ej. Deposito en un stock) queda pendiente.
                        propuesta["CampoCLC"] = propuesta["CampoCLC"].where(
                            propuesta["CampoCLC"].isin(opciones_clc), PENDING_FIELD
                        )
                        st.session_state.proc_mappings[key] = propuesta

                    mapping_df = st.session_state.proc_mappings[key]

                    # Valores de ejemplo y cantidad de valores únicos por columna, para ver
                    # de un vistazo cuál tiene códigos y cuál descripciones (evita confundir
                    # un campo con otro al mapear). Ej: un código tiene muchos únicos; un
                    # tipo de movimiento o categoría, pocos.
                    ejemplos = {}
                    unicos = {}
                    for col in df.columns:
                        muestra = df[col].dropna().astype(str).str.strip()
                        muestra = muestra[muestra != ""].head(2).tolist()
                        ejemplos[str(col)] = " · ".join(s[:30] for s in muestra)
                        unicos[str(col)] = int(df[col].nunique(dropna=True))

                    display_df = mapping_df[["ColumnaOriginal", "CampoCLC", "Confianza", "Observacion"]].copy()
                    display_df.insert(1, "Ejemplo", display_df["ColumnaOriginal"].astype(str).map(ejemplos).fillna(""))
                    display_df.insert(2, "ValoresUnicos", display_df["ColumnaOriginal"].astype(str).map(unicos).fillna(0).astype(int))

                    edited = st.data_editor(
                        display_df,
                        column_config={
                            "CampoCLC": st.column_config.SelectboxColumn(
                                "Campo CLC",
                                options=opciones_clc,
                            ),
                            "Ejemplo": st.column_config.TextColumn("Valores de ejemplo", disabled=True),
                            "ValoresUnicos": st.column_config.NumberColumn("Valores únicos", disabled=True, format="%d"),
                            "Confianza": st.column_config.NumberColumn("Confianza", disabled=True),
                            "Observacion": st.column_config.TextColumn("Observación", disabled=True),
                            "ColumnaOriginal": st.column_config.TextColumn("Columna original", disabled=True),
                        },
                        hide_index=True,
                        use_container_width=True,
                        key=f"editor_{key}",
                    )
                    # Los cambios del editor se guardan en proc_current_mappings,
                    # NO en proc_mappings (que es la propuesta estable que alimenta
                    # el editor). Sobrescribir la entrada del editor con su propia
                    # salida causaba el desfasaje de "hay que hacerlo dos veces".
                    cur = mapping_df.copy()
                    cur["CampoCLC"] = edited["CampoCLC"].fillna(PENDING_FIELD).values
                    st.session_state.proc_current_mappings[key] = cur

                    faltantes = missing_required_fields(cur, file_type)
                    recomendados = missing_recommended_fields(cur, file_type)
                    problemas_mapeo = mapping_blocking_issues(cur, file_type)
                    if faltantes:
                        st.error(
                            "Faltan campos **obligatorios** sin asignar: "
                            f"**{', '.join(faltantes)}**. "
                            "Elegí en la columna *Campo CLC* qué columna del archivo corresponde a cada uno."
                        )
                    else:
                        st.success("Todos los campos obligatorios están asignados.")
                    if recomendados:
                        st.info(f"Campos recomendados sin asignar (opcionales): {', '.join(recomendados)}")

            # ── Validación global antes de habilitar el avance ────────────────
            bloqueos = []
            for entry in entries:
                key = entry_key(entry)
                mapeo_actual = st.session_state.proc_current_mappings.get(
                    key, st.session_state.proc_mappings.get(key)
                )
                faltan = missing_required_fields(mapeo_actual, entry["file_type"])
                problemas = mapping_blocking_issues(mapeo_actual, entry["file_type"])
                if faltan:
                    nombre = entry["file_name"]
                    if entry["sheet_name"]:
                        nombre += f" ({entry['sheet_name']})"
                    bloqueos.append(f"- **{nombre}** [{entry['file_type']}]: falta {', '.join(faltan)}")
                if problemas:
                    nombre = entry["file_name"]
                    if entry["sheet_name"]:
                        nombre += f" ({entry['sheet_name']})"
                    for problema in problemas:
                        bloqueos.append(f"- **{nombre}** [{entry['file_type']}]: {problema}")

            st.divider()
            col_back, col_next = st.columns([1, 4])
            if col_back.button("← Volver"):
                st.session_state.proc_step = "upload"
                st.rerun()

            if bloqueos:
                st.warning(
                    "No se puede normalizar todavía. Asigná los campos obligatorios faltantes:\n\n"
                    + "\n".join(bloqueos)
                )
                col_next.button("Normalizar →", type="primary", disabled=True)
            elif col_next.button("Normalizar →", type="primary"):
                normalized = {"maestro": [], "stock": [], "movimientos": []}
                log = []
                normalize_errors = []
                go_to_analyze = False

                # +1 paso para la exportación final
                total_steps = max(len(entries) + 1, 1)
                progress = st.progress(0.0, text="Iniciando normalización...")
                done = 0

                for entry in entries:
                    key = entry_key(entry)
                    file_type = entry["file_type"]
                    progress.progress(
                        done / total_steps,
                        text=f"Normalizando {entry['file_name']} [{file_type}] · {len(entry['df']):,} filas...",
                    )
                    mapping_df = st.session_state.proc_current_mappings.get(
                        key, st.session_state.proc_mappings.get(key)
                    )
                    if mapping_df is None:
                        mapping_df = propose_column_mapping(entry["df"].columns, DICTIONARY_PATH)
                    mapping = mapping_dataframe_to_dict(mapping_df)
                    df = entry["df"]

                    try:
                        if file_type == "maestro":
                            norm_df = normalize_master(df, mapping)
                        elif file_type == "stock":
                            norm_df = normalize_stock(df, mapping)
                        elif file_type == "movimientos":
                            norm_df = normalize_movements(df, mapping, MOVEMENT_RULES_PATH)
                            # Signo manual elegido para esta hoja (entrada/salida/mantener).
                            norm_df = aplicar_signo_hoja(norm_df, entry.get("signo_hoja", "mantener"))
                        else:
                            done += 1
                            continue

                        norm_df.insert(0, "ArchivoOrigen", entry["file_name"])
                        norm_df.insert(1, "HojaOrigen", entry["sheet_name"])
                        normalized[file_type].append(norm_df)
                        log.append({
                            "Archivo": entry["file_name"], "Hoja": entry["sheet_name"],
                            "Tipo": file_type, "Filas": len(norm_df), "Estado": "OK",
                        })
                    except Exception as e:
                        log.append({
                            "Archivo": entry["file_name"], "Hoja": entry["sheet_name"],
                            "Tipo": file_type, "Filas": 0, "Estado": f"Error: {e}",
                        })
                        normalize_errors.append(f"{entry['file_name']} [{file_type}]: {e}")
                    done += 1

                if normalize_errors:
                    st.error("Errores al normalizar:\n\n" + "\n".join(f"- {m}" for m in normalize_errors))

                exported_any = any(dfs for dfs in normalized.values())

                if not exported_any:
                    progress.empty()
                    st.error("No se generó ningún archivo normalizado. Revisá los errores de arriba.")
                else:
                    progress.progress(
                        done / total_steps,
                        text="Guardando archivos normalizados (escribir Excel grande puede tardar)...",
                    )
                    # Recién ahora, con los normalizados ya armados en memoria, es
                    # seguro borrar los anteriores si el usuario pidió reemplazar.
                    if st.session_state.get("proc_limpiar", False):
                        for d in [NORMALIZED_DIR, REPORTS_DIR]:
                            d.mkdir(parents=True, exist_ok=True)
                            for f in d.iterdir():
                                if f.is_file() and not f.name.startswith("~$"):
                                    f.unlink()

                    for file_type, dfs in normalized.items():
                        if dfs:
                            combined = pd.concat(dfs, ignore_index=True)
                            export_normalized(combined, file_type, NORMALIZED_DIR)

                    progress.progress(1.0, text="Normalización completa.")
                    st.session_state.proc_normalized = normalized
                    st.session_state.proc_log = log
                    st.session_state.proc_step = "analyze"
                    go_to_analyze = True

                if go_to_analyze:
                    st.rerun()

    # ══ STEP 3: ANALIZAR ══════════════════════════════════════════════════════
    elif current == "analyze":
        st.subheader("Resultado de normalización")

        log_df = pd.DataFrame(st.session_state.proc_log)
        if not log_df.empty:
            st.dataframe(log_df, use_container_width=True, hide_index=True)

        tipos_ok = {t for t, dfs in st.session_state.proc_normalized.items() if dfs}
        if "stock" not in tipos_ok:
            st.error("Falta el archivo de stock. Sin stock no se puede ejecutar el control.")
        opcionales_faltantes = {"maestro", "movimientos"} - tipos_ok
        if opcionales_faltantes:
            st.info(
                "No se normalizo "
                + ", ".join(sorted(opcionales_faltantes))
                + ". Es opcional: el analisis sigue usando tablas vacias para lo que falte."
            )

        st.divider()
        st.subheader("Configurar análisis de stock")

        col1, col2 = st.columns(2)
        cliente = col1.text_input("Nombre del cliente (para el reporte)", placeholder="ej. Palacio")
        use_deposit = col2.toggle("El depósito importa", value=False)

        if use_deposit:
            col2.caption("Calcula por CodigoArticulo + Deposito")
        else:
            col2.caption("Agrupa todo por CodigoArticulo")

        # "Incluir movimientos de la fecha inicial" se controla desde el panel izquierdo.
        include_initial = incluir_mov_inicial
        st.caption(
            "ℹ️ *Incluir movimientos de la fecha inicial*: "
            + ("**ACTIVADO** (stock inicial = apertura del día)" if include_initial
               else "**desactivado** (stock inicial = cierre del período)")
            + " — se cambia desde el panel izquierdo."
        )

        assume_zero = st.toggle(
            "Controlar productos sin foto final asumiendo stock 0",
            value=False,
            help="Los productos que están en el stock inicial pero NO en la foto final se "
                 "controlan asumiendo que terminaron en 0 (StockInicial + Movimientos debería dar 0). "
                 "Detecta los que no cerraron bien (stock fantasma o movimientos faltantes).",
        )
        if assume_zero:
            st.caption("Se agregarán controles para los productos ausentes de la última foto de stock (final = 0).")

        # ── Asignar signos por tipo de movimiento (con IA) ────────────────────
        st.divider()
        sign_map = None
        respect_neg = True
        usar_signos_ia = st.toggle(
            "Asignar signos por tipo de movimiento (con IA)",
            value=False,
            help="Para clientes cuyos movimientos vienen TODOS en positivo y la dirección "
                 "(entra/sale) está en el tipo de movimiento. La IA propone ingreso(+)/egreso(−) "
                 "por cada tipo y vos lo confirmás. Requiere haber mapeado 'TipoMovimiento'.",
        )
        if usar_signos_ia:
            tipos = []
            mv = load_movimientos_for_sign_config()
            if not mv.empty and "TipoMovimiento" in mv.columns:
                tipos = sorted(
                    t for t in mv["TipoMovimiento"].fillna("").astype(str).str.strip().unique() if t
                )
            if not tipos:
                st.warning(
                    "No hay tipos de movimiento. Volvé al mapeo y asigná la columna de tipo "
                    "de movimiento al campo **TipoMovimiento** en la(s) hoja(s) de movimientos."
                )
            else:
                if st.button("🤖 Clasificar tipos con IA"):
                    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
                    if not api_key:
                        st.error("Falta ANTHROPIC_API_KEY (definila en .env o en los secrets).")
                    else:
                        with st.spinner("Clasificando tipos de movimiento..."):
                            try:
                                clas = clasificar_tipos_movimiento(tipos, api_key)
                            except Exception as exc:
                                clas = {}
                                st.error(f"No se pudo clasificar: {exc}")
                        st.session_state.proc_sign_class = pd.DataFrame({
                            "TipoMovimiento": tipos,
                            "Direccion": [str(clas.get(t, "revisar")).lower() for t in tipos],
                        })

                # Inicializar la tabla si no existe o si cambiaron los tipos
                actual = st.session_state.get("proc_sign_class")
                if actual is None or set(actual["TipoMovimiento"]) != set(tipos):
                    st.session_state.proc_sign_class = pd.DataFrame({
                        "TipoMovimiento": tipos,
                        "Direccion": ["revisar"] * len(tipos),
                    })

                st.caption(
                    "Revisá/corregí cada tipo: **ingreso** (+), **egreso** (−), "
                    "**mantener** (respeta el signo de cada fila — usalo para AJUSTES, que pueden ser + o −) "
                    "o **revisar** (no toca el signo)."
                )
                edit_clas = st.data_editor(
                    st.session_state.proc_sign_class,
                    column_config={
                        "TipoMovimiento": st.column_config.TextColumn("Tipo de movimiento", disabled=True),
                        "Direccion": st.column_config.SelectboxColumn(
                            "Dirección", options=["ingreso", "egreso", "mantener", "revisar"],
                        ),
                    },
                    hide_index=True,
                    use_container_width=True,
                    key="editor_signos",
                )
                respect_neg = st.checkbox(
                    "Respetar movimientos que ya vienen con signo negativo", value=True,
                    help="Si está activado, los movimientos que ya vienen negativos no se tocan; "
                         "la IA solo asigna signo a los positivos.",
                )
                sign_map = {}
                n_mantener = 0
                for _, r in edit_clas.iterrows():
                    d = str(r["Direccion"]).strip().lower()
                    tipo = str(r["TipoMovimiento"]).strip()
                    if d == "ingreso":
                        sign_map[tipo] = 1
                    elif d == "egreso":
                        sign_map[tipo] = -1
                    elif d == "mantener":
                        n_mantener += 1
                ing = sum(1 for v in sign_map.values() if v == 1)
                egr = sum(1 for v in sign_map.values() if v == -1)
                revisar = len(tipos) - ing - egr - n_mantener
                st.caption(
                    f"Clasificados: **{ing}** ingreso(s), **{egr}** egreso(s), "
                    f"**{n_mantener}** mantener (respeta signo), **{revisar}** a revisar (sin cambio)."
                )

        col_back, col_run = st.columns([1, 4])
        if col_back.button("← Volver al mapeo"):
            st.session_state.proc_step = "mapping"
            st.rerun()

        stock_ready = "stock" in tipos_ok
        if col_run.button("▶ Ejecutar análisis", type="primary", disabled=not stock_ready):
            client_slug = slugify(cliente)
            control_name = f"control_stock_resultado_{client_slug}.xlsx" if client_slug else "control_stock_resultado.xlsx"
            diag_name = f"diagnostico_stock_{client_slug}.txt" if client_slug else "diagnostico_stock.txt"

            analysis_ok = False
            try:
                with st.status("Procesando control de stock...", expanded=True) as status:
                    st.write("⏳ Leyendo normalizados y calculando diferencias (puede tardar con muchos movimientos)...")
                    result = run_stock_analysis(
                        NORMALIZED_DIR,
                        REPORTS_DIR,
                        output_file_name=control_name,
                        use_deposit=use_deposit,
                        include_initial_date_movements=include_initial,
                        assume_missing_final_zero=assume_zero,
                        sign_map=sign_map if usar_signos_ia else None,
                        respect_existing_negatives=respect_neg,
                    )
                    controlados = int(result["summary_df"].iloc[0]["total_registros_controlados"])
                    st.write(f"✅ Control calculado: {controlados:,} registros. Excel guardado.")
                    st.write("⏳ Generando diagnóstico escrito...")
                    diag_path = generate_diagnosis(result["output_path"], REPORTS_DIR / diag_name)
                    st.write("✅ Diagnóstico guardado.")

                    # Guardado en la nube (por cliente, con historial). Opcional:
                    # si no está configurado Supabase, se omite sin romper nada.
                    if cloud_store.is_configured():
                        nombre_cli = (cliente or "Sin nombre").strip()
                        st.write(f"☁️ Guardando corrida en la nube para **{nombre_cli}**...")
                        try:
                            cloud_store.save_corrida(
                                nombre_cliente=nombre_cli,
                                parametros={
                                    "use_deposit": use_deposit,
                                    "include_initial_date_movements": include_initial,
                                    "assume_missing_final_zero": assume_zero,
                                    "signos_por_tipo_ia": bool(usar_signos_ia and sign_map),
                                    "respetar_negativos": respect_neg,
                                },
                                resumen={k: _to_py(v) for k, v in result["summary_df"].iloc[0].to_dict().items()},
                                control_df=result["control_df"],
                                xlsx_path=str(result["output_path"]),
                                diagnostico_path=str(diag_path),
                                movimientos_df=result.get("movimientos_finales"),
                                movimientos_path=str(NORMALIZED_DIR / "movimientos_normalizado.parquet"),
                            )
                            st.write("✅ Corrida guardada en la nube.")
                        except Exception as cloud_exc:
                            st.warning(f"No se pudo guardar en la nube (la corrida local sí quedó): {cloud_exc}")

                    status.update(label="Análisis completo", state="complete", expanded=False)

                st.session_state.proc_result = result["summary_df"].iloc[0].to_dict()
                st.session_state.proc_report_name = control_name
                st.session_state.proc_step = "done"
                st.cache_data.clear()
                analysis_ok = True
            except Exception as e:
                import traceback as _tb
                st.error(f"Error en el análisis: {e}")
                st.code(_tb.format_exc(), language="python")

            if analysis_ok:
                st.rerun()

    # ══ STEP 4: LISTO ═════════════════════════════════════════════════════════
    elif current == "done":
        st.success("✅ Proceso completado")
        res = st.session_state.get("proc_result", {})
        total_r = int(res.get("total_registros_controlados", 0))
        ok_r = int(res.get("total_registros_ok", 0))
        diff_r = int(res.get("total_registros_con_diferencia", 0))
        abs_r = res.get("total_diferencia_absoluta", 0)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total controlados", f"{total_r:,}")
        c2.metric("Registros OK", f"{ok_r:,}", f"{ok_r/total_r*100:.1f}%" if total_r else "")
        c3.metric("Con diferencia", f"{diff_r:,}", delta_color="inverse")
        c4.metric("Diferencia absoluta", f"{abs_r:,.0f} u.")

        st.info(f"Reporte guardado: **{st.session_state.get('proc_report_name', '')}**")

        col1, col2 = st.columns(2)
        if col1.button("📊 Ver resultados completos", type="primary"):
            st.session_state.proc_step = "upload"
            st.session_state.pending_nav_page = "📊 Resumen"
            st.rerun()
        if col2.button("🔄 Procesar otro cliente"):
            reset_proc_state()
            st.rerun()

        st.divider()
        st.caption(
            "Los archivos normalizados se guardan en formato parquet (rápido). "
            "Si necesitás abrirlos en Excel o Power BI, generá copias .xlsx:"
        )
        if st.button("📤 Exportar normalizados a Excel"):
            with st.spinner("Generando copias .xlsx (puede tardar con movimientos grandes)..."):
                generados = export_normalized_to_excel()
            if generados:
                st.success(f"Generados en data/normalized: {', '.join(generados)}")
            else:
                st.warning("No se encontraron normalizados para exportar.")


# ══════════════════════════════════════════════════════════════════════════════
# PÁGINA: RESUMEN
# ══════════════════════════════════════════════════════════════════════════════

if page == "📊 Resumen":
    st.title("📊 Resumen del Control de Stock")
    st.caption(f"Reporte: **{report_label or '—'}**")
    with st.expander("Estado de la corrida", expanded=False):
        local_mov_path = movements_path_for_report(report_path) if report_path is not None else None
        if report_path is not None and local_mov_path is not None and local_mov_path.exists():
            st.success("Foto de movimientos de la corrida: disponible")
        elif cloud_archivo_control and active_movements is not None and not active_movements.empty:
            st.success("Foto de movimientos de la corrida: disponible en nube")
        elif active_movements is not None and active_movements.empty:
            mov_control = pd.to_numeric(control_full.get("MovimientosAcumulados"), errors="coerce").fillna(0)
            if mov_control.abs().sum() == 0:
                st.info("No hay movimientos guardados y el control no tiene movimientos acumulados.")
            else:
                st.warning("Falta la foto de movimientos de esta corrida. Reejecuta el analisis para habilitar traza completa.")

        filas_mov = len(active_movements) if active_movements is not None else 0
        suma_mov = (
            pd.to_numeric(active_movements.get("CantidadNormalizada"), errors="coerce").fillna(0).sum()
            if active_movements is not None and not active_movements.empty else 0
        )
        c_estado1, c_estado2, c_estado3 = st.columns(3)
        c_estado1.metric("Filas movimientos", f"{filas_mov:,}")
        c_estado2.metric("Suma neta movimientos", f"{suma_mov:,.0f}")
        c_estado3.metric("Filas control", f"{len(control_full):,}")

        meta = local_run_metadata if report_path is not None else {}
        params = meta.get("parametros", {}) if isinstance(meta, dict) else {}
        if params:
            st.caption(
                "Parametros: "
                f"deposito={'si' if params.get('use_deposit') else 'no'} · "
                f"mov. fecha inicial={'si' if params.get('include_initial_date_movements') else 'no'} · "
                f"stock final faltante=0 {'si' if params.get('assume_missing_final_zero') else 'no'}"
            )

    if lineas_base_ocultas:
        st.caption(
            f"Mostrando un control por producto. {lineas_base_ocultas:,} líneas de stock inicial "
            "ocultas (activá el toggle del panel izquierdo para verlas)."
        )
    st.divider()

    # Diferencia porcentual: cuánto se desvía el stock calculado del informado,
    # como % del stock informado final (sum |diferencia| / sum stock informado).
    consistencia = data.get("consistencia_calculo", pd.DataFrame())
    if consistencia is not None and not consistencia.empty and "EstadoConsistencia" in consistencia.columns:
        revisar_consistencia = consistencia[consistencia["EstadoConsistencia"] == "Revisar"]
        if revisar_consistencia.empty:
            st.success("Consistencia interna OK: el control cierra contra los movimientos guardados de esta corrida.")
        else:
            st.error(
                f"Consistencia interna a revisar: {len(revisar_consistencia):,} filas no cierran "
                "contra los movimientos guardados de esta corrida."
            )

    total_informado = pd.to_numeric(control.get("StockInformado"), errors="coerce").fillna(0).sum() if not control.empty else 0
    dif_pct = (abs_diff / total_informado * 100) if total_informado else 0.0

    # KPIs
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total controlados", f"{total:,}")
    c2.metric("Registros OK", f"{ok_count:,}", f"{pct_ok:.1f}%")
    c3.metric(
        "Con diferencia",
        f"{diff_count:,}",
        f"{diff_count / total * 100:.1f}%" if total else "",
        delta_color="inverse",
    )
    c4.metric("Diferencia absoluta", f"{abs_diff:,.0f} u.")
    c5.metric(
        "Diferencia %",
        f"{dif_pct:.2f}%",
        help="Diferencia entre stock calculado e informado, como porcentaje del stock "
             "informado final (diferencia absoluta total ÷ stock informado total).",
    )

    st.divider()

    col_izq, col_der = st.columns(2)

    with col_izq:
        st.subheader("Estado del control")
        if not control.empty:
            estados = control["EstadoControl"].value_counts().reset_index()
            estados.columns = ["Estado", "Cantidad"]
            color_map = {
                "OK": "#28a745",
                "Revisar": "#ffc107",
                "Critico": "#dc3545",
                "Sin datos": "#adb5bd",
            }
            fig = px.bar(
                estados,
                x="Estado",
                y="Cantidad",
                color="Estado",
                color_discrete_map=color_map,
                text="Cantidad",
            )
            fig.update_layout(showlegend=False, margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)

    with col_der:
        st.subheader("Top 10 SKUs con mayor diferencia")
        if not diffs.empty:
            top10 = (
                diffs.groupby(["CodigoArticulo", "Descripcion"], dropna=False)["DiferenciaAbsoluta"]
                .sum()
                .nlargest(10)
                .reset_index()
            )
            top10["label"] = top10["CodigoArticulo"].astype(str) + " - " + top10["Descripcion"].fillna("").astype(str).str[:25]
            fig2 = px.bar(
                top10,
                x="DiferenciaAbsoluta",
                y="label",
                orientation="h",
                color="DiferenciaAbsoluta",
                color_continuous_scale="Reds",
                labels={"DiferenciaAbsoluta": "Diferencia absoluta", "label": ""},
            )
            fig2.update_layout(
                showlegend=False,
                coloraxis_showscale=False,
                margin=dict(t=10, b=10),
                yaxis={"autorange": "reversed"},
            )
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.success("¡Sin diferencias! El control cerró al 100%.")

    # Tabla de diferencias
    if not diffs.empty:
        st.divider()
        st.subheader(f"Registros con diferencia ({len(diffs):,})")
        cols_show = [
            "Fecha", "CodigoArticulo", "Descripcion", "Deposito",
            "StockInicial", "MovimientosAcumulados", "StockCalculado",
            "StockInformado", "Diferencia", "EstadoControl", "StockFinalAsumidoCero",
        ]
        cols_ok = [c for c in cols_show if c in diffs.columns]
        st.dataframe(
            diffs[cols_ok].style.map(color_estado, subset=["EstadoControl"]).format(precision=2),
            use_container_width=True,
            hide_index=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# PÁGINA: DETALLE
# ══════════════════════════════════════════════════════════════════════════════

elif page == "🔍 Detalle":
    st.title("🔍 Detalle del Control")
    if lineas_base_ocultas:
        st.caption(
            f"Mostrando un control por producto. {lineas_base_ocultas:,} líneas de stock inicial "
            "ocultas (activá el toggle del panel izquierdo para verlas)."
        )

    if control.empty:
        st.warning("No hay datos de control disponibles.")
        st.stop()

    # Filtros
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        estados_disp = ["Todos"] + sorted(control["EstadoControl"].dropna().unique().tolist())
        estado_sel = st.selectbox("Estado", estados_disp)
    with col2:
        if "Deposito" in control.columns:
            deps = ["Todos"] + sorted(control["Deposito"].dropna().unique().tolist())
            deposito_sel = st.selectbox("Depósito", deps)
        else:
            deposito_sel = "Todos"
    with col3:
        buscar = st.text_input("Buscar SKU", placeholder="Código o descripción...")

    filtrado = control.copy()
    if estado_sel != "Todos":
        filtrado = filtrado[filtrado["EstadoControl"] == estado_sel]
    if deposito_sel != "Todos" and "Deposito" in filtrado.columns:
        filtrado = filtrado[filtrado["Deposito"] == deposito_sel]
    if buscar:
        mask = filtrado["CodigoArticulo"].astype(str).str.contains(buscar, case=False, na=False)
        if "Descripcion" in filtrado.columns:
            mask |= filtrado["Descripcion"].astype(str).str.contains(buscar, case=False, na=False)
        filtrado = filtrado[mask]

    st.caption(f"Mostrando **{len(filtrado):,}** de **{len(control):,}** registros")

    cols_show = [
        "Fecha", "CodigoArticulo", "Descripcion", "Deposito",
        "FechaInicial", "StockInicial", "MovimientosAcumulados",
        "StockCalculado", "StockInformado", "Diferencia", "EstadoControl",
        "StockFinalAsumidoCero",
    ]
    cols_ok = [c for c in cols_show if c in filtrado.columns]

    st.dataframe(
        filtrado[cols_ok].style.map(color_estado, subset=["EstadoControl"]).format(precision=2),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Cálculo de movimientos acumulados")
    codigo_traza = st.text_input(
        "Código para auditar",
        value=buscar if buscar else "",
        placeholder="Ej. TS18S",
        key="trace_codigo",
    )
    if codigo_traza:
        control_row, traza_df, excluidos_df, trace_message = build_movement_trace(
            control_full,
            codigo_traza,
            active_movements,
        )
        if trace_message:
            st.warning(trace_message)
        if control_row is not None:
            movimiento_reporte = pd.to_numeric(
                pd.Series([control_row.get("MovimientosAcumulados")]),
                errors="coerce",
            ).fillna(0).iloc[0]
            movimiento_detalle = (
                pd.to_numeric(traza_df.get("CantidadNormalizada"), errors="coerce").fillna(0).sum()
                if not traza_df.empty else 0
            )
            stock_inicial_num = pd.to_numeric(
                pd.Series([control_row.get("StockInicial")]),
                errors="coerce",
            ).fillna(0).iloc[0]
            diferencia_traza = movimiento_detalle - movimiento_reporte

            st.caption(
                f"Producto: **{control_row.get('CodigoArticulo', '')}** | "
                f"Fecha inicial: **{control_row.get('FechaInicial', '')}** | "
                f"Fecha control: **{control_row.get('Fecha', '')}** | "
                f"Depósito: **{control_row.get('Deposito', '')}**"
            )

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Stock inicial", f"{stock_inicial_num:,.0f}")
            m2.metric("Suma movimientos", f"{movimiento_detalle:,.0f}")
            m3.metric("Movimientos reporte", f"{movimiento_reporte:,.0f}")
            m4.metric("Diferencia traza", f"{diferencia_traza:,.0f}")

            if abs(diferencia_traza) > 0.000001:
                st.error(
                    "La suma detallada no coincide con MovimientosAcumulados del reporte. "
                    "Revisar movimientos normalizados o regenerar el análisis."
                )
            elif not traza_df.empty:
                st.success("La suma detallada coincide con MovimientosAcumulados.")

            if traza_df.empty:
                st.info("No hay movimientos aplicados para este punto de control.")
            else:
                st.dataframe(traza_df, use_container_width=True, hide_index=True)
                csv = traza_df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Descargar detalle de cálculo CSV",
                    data=csv,
                    file_name=f"calculo_movimientos_{slugify(str(control_row.get('CodigoArticulo', codigo_traza)))}.csv",
                    mime="text/csv",
                )

            with st.expander("Ver movimientos excluidos del cálculo para este producto"):
                if excluidos_df.empty:
                    st.info("No hay movimientos excluidos para este producto.")
                else:
                    st.dataframe(excluidos_df, use_container_width=True, hide_index=True)

    # Movimientos no aplicados: hoja potencialmente enorme, se lee solo al pedirlo.
    st.divider()
    with st.expander("Ver movimientos no aplicados al cálculo"):
        if report_path is None:
            st.info("El detalle de movimientos no aplicados está disponible en el reporte Excel "
                    "(fuente Local). Desde la nube se muestra el control; descargá el Excel para el detalle.")
        else:
            st.caption("Esta hoja puede tener cientos de miles de filas. Se carga solo si la pedís.")
            if st.button("Cargar movimientos no aplicados"):
                no_ap = load_report_sheet(str(report_path), "movimientos_no_aplicados")
                if no_ap.empty:
                    st.info("Todos los movimientos fueron aplicados.")
                else:
                    st.caption(f"{len(no_ap):,} movimientos no aplicados (se muestran los primeros 1.000)")
                    st.dataframe(no_ap.head(1000), use_container_width=True, hide_index=True)

    with st.expander("Ver posibles movimientos duplicados"):
        dups = data["duplicados_movimientos"]
        if dups.empty:
            st.info("No se detectaron posibles duplicados.")
        else:
            st.caption(f"{len(dups):,} filas sospechosas detectadas. No se eliminaron del calculo.")
            st.dataframe(dups, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# PÁGINA: VISUALIZACIÓN DE DATOS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "📈 Visualización de datos":
    st.title("📈 Visualización de datos")
    viz = st.selectbox("Visualización", ["Balance de Masa"])

    if viz == "Balance de Masa":
        st.subheader("⚖️ Balance de Masa")
        st.caption(f"Reporte: **{report_label or '—'}**")

        if control_full.empty:
            st.warning("No hay datos de control para mostrar.")
            st.stop()

        # ── Totales de stock (las FOTOS de fin de mes son la fuente de verdad) ─
        # El stock se mide como foto a fin de período: el primer corte es el Stock
        # Inicial real y el último corte el Stock Final real. NO usamos la columna
        # StockInicial del control para el agregado: esa columna es per-producto y
        # descarta los productos que existían al inicio pero ya no están al final,
        # lo que distorsiona el balance de masa. El balance correcto es:
        #     Foto inicial  +  (todos los movimientos del período)  =  Foto final
        # Fotos del balance: cacheadas por corrida (ver prepare_balance_control).
        # Antes esto se recalculaba en cada click; ahora corre una vez por corrida.
        fecha_inicial, fecha_final, fin, stock_inicial, stock_final_foto = prepare_balance_control(
            _corrida_key, control_full,
        )

        f_ini = fecha_inicial.date().isoformat() if pd.notna(fecha_inicial) else "—"
        f_fin = fecha_final.date().isoformat() if pd.notna(fecha_final) else "—"

        # ── Movimientos (para el desglose por tipo y por mes) ──────────────────
        # Ya vienen enriquecidos y cacheados por corrida (Tipo, Cantidad, Fecha_dt).
        mov = prepare_viz_movimientos(
            cloud_archivo_control,
            str(report_path) if report_path is not None else "",
        )
        hay_mov = not mov.empty
        if hay_mov:
            mov_f = mov
        else:
            mov_f = pd.DataFrame()

        # Movimientos del PERÍODO: posteriores a la foto inicial y hasta la foto final.
        # NO se filtra por producto a propósito: el balance de masa es agregado y todos
        # los movimientos del período afectan la masa total. (Filtrar por código sería
        # frágil porque el control y los movimientos pueden venir con el código en
        # formatos distintos —ceros a la izquierda, floats— y no matchear.) El neto de
        # todos los movimientos debe explicar el cambio entre fotos.
        # La ventana respeta el parámetro de la corrida: si el análisis incluyó los
        # movimientos de la fecha inicial (foto = apertura del día), acá también.
        if cloud_archivo_control:
            incluye_inicial = cloud_include_initial
        else:
            incluye_inicial = bool(
                (local_run_metadata.get("parametros") or {}).get("include_initial_date_movements", False)
            )
        # El recorte del período está cacheado por corrida (ver prepare_mov_periodo):
        # antes la máscara + copy sobre todos los movimientos corría en cada rerun.
        mov_periodo = prepare_mov_periodo(
            _corrida_key, bool(incluye_inicial),
            fecha_inicial, fecha_final, mov_f,
        )
        mov_netos = mov_periodo["Cantidad"].sum() if not mov_periodo.empty else 0.0

        # El stock calculado del balance arranca en la foto inicial y le suma el neto
        # de los movimientos; idealmente cierra en la foto final.
        stock_final_calc = stock_inicial + mov_netos
        dif_unidades = stock_final_foto - stock_final_calc
        dif_pct = (dif_unidades / stock_final_foto * 100) if stock_final_foto else 0.0

        # ── KPIs ──────────────────────────────────────────────────────────────
        st.divider()
        a1, a2, a3, a4 = st.columns(4)
        a1.metric(f"Stock Inicial ({f_ini})", f"{stock_inicial:,.0f}")
        a2.metric("Movimientos Netos", f"{mov_netos:,.0f}")
        a3.metric(f"Stock Final foto ({f_fin})", f"{stock_final_foto:,.0f}")
        a4.metric(
            "Stock Final Calculado", f"{stock_final_calc:,.0f}",
            delta=f"{-dif_unidades:,.0f} vs foto" if dif_unidades else "cierra con la foto",
            delta_color="inverse" if dif_unidades else "off",
        )

        b1, b2, b3, b4 = st.columns(4)
        b1.metric("Dif. Stock Unidades", f"{dif_unidades:,.0f}", delta_color="inverse")
        b2.metric("Dif. Stock %", f"{dif_pct:.2f}%", delta_color="inverse")
        if hay_mov:
            b3.metric("Movimientos del período", f"{len(mov_periodo):,}")
            b4.metric("Productos Movidos", f"{mov_periodo['CodigoArticulo'].nunique():,}")

        if hay_mov:
            if abs(dif_unidades) < 0.5:
                st.success(
                    f"✅ El balance cierra: Foto inicial ({stock_inicial:,.0f}) + Movimientos netos "
                    f"({mov_netos:,.0f}) = Foto final ({stock_final_foto:,.0f})."
                )
            else:
                st.warning(
                    f"⚠️ Los movimientos no explican {dif_unidades:,.0f} unidades del cambio entre fotos "
                    f"({stock_inicial:,.0f} → {stock_final_foto:,.0f}). Puede faltar una hoja de movimientos, "
                    f"haber movimientos fuera del período, o un signo mal configurado."
                )

        st.divider()

        if not hay_mov:
            st.info(
                "El desglose por tipo de movimiento y por mes no está disponible para esta corrida "
                "(se guarda al ejecutar el análisis). Volvé a ejecutar el análisis del cliente para verlo."
            )
        else:
            # ── Waterfall: cómo se llega de la foto inicial a la final ─────────
            st.markdown("**Balance: de la foto inicial a la final** (neto por tipo de movimiento)")
            neto_por_tipo = (
                mov_periodo.groupby("Tipo")["Cantidad"].sum().sort_values(ascending=False)
            )
            etiquetas = (
                [f"Stock Inicial<br>{f_ini}"]
                + neto_por_tipo.index.tolist()
                + ["Stock Final<br>Calculado"]
            )
            valores = [stock_inicial] + neto_por_tipo.values.tolist() + [0]
            medidas = ["absolute"] + ["relative"] * len(neto_por_tipo) + ["total"]
            fig_w = go.Figure(go.Waterfall(
                orientation="v", measure=medidas, x=etiquetas, y=valores,
                text=[f"{v:,.0f}" for v in valores[:-1]] + [f"{stock_final_calc:,.0f}"],
                textposition="outside",
                connector={"line": {"color": "#bbbbbb"}},
                increasing={"marker": {"color": "#28a745"}},
                decreasing={"marker": {"color": "#dc3545"}},
                totals={"marker": {"color": "#1f77b4"}},
            ))
            fig_w.add_hline(
                y=stock_final_foto, line_dash="dash", line_color="#6c757d",
                annotation_text=f"Foto final: {stock_final_foto:,.0f}",
                annotation_position="top right",
            )
            fig_w.update_layout(showlegend=False, margin=dict(t=30, b=10), yaxis_title="Unidades")
            st.plotly_chart(fig_w, use_container_width=True)

            st.divider()
            col_izq, col_der = st.columns([3, 2])

            with col_izq:
                st.markdown("**Movimientos por tipo** (verde suma stock, rojo lo resta)")
                desglose = neto_por_tipo.reset_index().sort_values("Cantidad")
                desglose["Color"] = desglose["Cantidad"].apply(lambda v: "Ingreso" if v >= 0 else "Egreso")
                fig = px.bar(
                    desglose, x="Cantidad", y="Tipo", orientation="h",
                    color="Color", color_discrete_map={"Ingreso": "#28a745", "Egreso": "#dc3545"},
                    text="Cantidad",
                )
                fig.update_traces(texttemplate="%{text:,.0f}")
                fig.update_layout(showlegend=False, margin=dict(t=10, b=10), yaxis_title="", xaxis_title="")
                st.plotly_chart(fig, use_container_width=True)

                st.markdown("**Detalle por tipo** (entradas y salidas por separado)")
                tabla = mov_periodo.groupby("Tipo").agg(
                    Movimientos=("Cantidad", "count"),
                    Entradas=("Cantidad", lambda s: s[s > 0].sum()),
                    Salidas=("Cantidad", lambda s: s[s < 0].sum()),
                    Neto=("Cantidad", "sum"),
                ).reset_index().sort_values("Neto")
                st.dataframe(
                    tabla.style.format({
                        "Movimientos": "{:,.0f}", "Entradas": "{:,.0f}",
                        "Salidas": "{:,.0f}", "Neto": "{:,.0f}",
                    }),
                    use_container_width=True, hide_index=True,
                )

            with col_der:
                st.markdown("**Stock acumulado por mes**")
                st.caption(
                    "Acumulado = Stock Inicial (foto) + movimientos acumulados. "
                    "El último mes cierra en el Stock Final Calculado."
                )
                # Usa exactamente los mismos movimientos del período que los KPIs
                # (mov_periodo). Los meses sin movimientos también se muestran (con 0),
                # así la serie es continua entre la foto inicial y la final.
                mov_mes = mov_periodo.dropna(subset=["Fecha_dt"]).copy()
                mov_mes["Mes"] = mov_mes["Fecha_dt"].dt.to_period("M")
                mensual = mov_mes.groupby("Mes")["Cantidad"].sum()
                if pd.notna(fecha_inicial) and pd.notna(fecha_final):
                    # Incluye SIEMPRE el mes de la foto inicial (con cantidad 0):
                    # ese es el punto de partida del acumulado = stock_inicial.
                    todos_meses = pd.period_range(fecha_inicial, fecha_final, freq="M")
                    mensual = mensual.reindex(todos_meses, fill_value=0)
                mensual = mensual.reset_index()
                mensual.columns = ["Mes", "Cantidad"]
                mensual["Mes"] = mensual["Mes"].astype(str)
                # El primer mes (foto inicial) tiene Cantidad=0 → Acumulado = stock_inicial.
                mensual["Acumulado"] = stock_inicial + mensual["Cantidad"].cumsum()

                tab_graf, tab_tabla = st.tabs(["📈 Gráfico", "📋 Tabla"])
                with tab_graf:
                    fig_m = px.line(mensual, x="Mes", y="Acumulado", markers=True,
                                    color_discrete_sequence=["#1f77b4"])
                    fig_m.add_hline(
                        y=stock_final_foto, line_dash="dash", line_color="#6c757d",
                        annotation_text=f"Foto final: {stock_final_foto:,.0f}",
                        annotation_position="bottom right",
                    )
                    fig_m.update_layout(margin=dict(t=8, b=5, l=5, r=5), height=230,
                                        xaxis_title="", yaxis_title="Unidades")
                    st.plotly_chart(fig_m, use_container_width=True)
                with tab_tabla:
                    st.dataframe(
                        mensual.rename(columns={"Mes": "Mes-Año", "Cantidad": "Movimientos del mes"})
                               .style.format({"Movimientos del mes": "{:,.0f}", "Acumulado": "{:,.0f}"}),
                        use_container_width=True, hide_index=True,
                    )

        # ── Dashboard de distribuciones + evolución temporal ─────────────────
        st.divider()
        _TEAL = "#17a2b8"
        _TEAL_DARK = "#0d7b8a"

        def _pct_bar(df_dist, x_col, y_col, title):
            """Bar chart con porcentajes encima, estilo teal."""
            total = df_dist[y_col].sum()
            df_dist = df_dist.copy()
            df_dist["Pct"] = df_dist[y_col] / total * 100 if total else 0
            df_dist["PctLabel"] = df_dist["Pct"].apply(lambda v: f"{v:.0f}%")
            fig = px.bar(
                df_dist, x=x_col, y=y_col, text="PctLabel",
                color_discrete_sequence=[_TEAL],
                title=f"<b>{title}</b>",
            )
            fig.update_traces(textposition="outside", marker_line_width=0)
            _ymax = df_dist[y_col].max() if not df_dist.empty else 1
            fig.update_layout(
                title=dict(font=dict(size=13, color="#0c4a6e"), x=0, xref="paper"),
                showlegend=False, margin=dict(t=40, b=5, l=5, r=5),
                height=210,
                plot_bgcolor="white", paper_bgcolor="white",
                yaxis=dict(showgrid=True, gridcolor="#eeeeee", title="",
                           range=[0, _ymax * 1.25]),
                xaxis=dict(title=""),
                clickmode="event+select",
            )
            return fig

        def _date_xaxis(df, x_col):
            """
            Configuración del eje X de fechas:
            - Período completo (varios puntos): eje de fechas con ticks mes/año,
              auto-espaciados por Plotly (limpio, como antes).
            - Un solo día seleccionado (1 punto): se pasa a categoría y se muestra
              solo esa fecha, sin que Plotly interpole horas alrededor de la medianoche.
            Modifica df[x_col] in-place a string solo en el caso de un punto (el caller
            siempre pasa una copia).
            """
            base = dict(title="", showgrid=False)
            if len(df) <= 2:
                df[x_col] = df[x_col].astype(str)
                base["type"] = "category"
            else:
                base["type"] = "date"
                base["tickformat"] = "%b %Y"
            return base

        def _ts_line(df_ts, x_col, y_col, title, y_label=""):
            """Serie temporal con marcadores clickeables y línea de promedio punteada."""
            df_ts = df_ts.copy()
            _xaxis = _date_xaxis(df_ts, x_col)
            avg = df_ts[y_col].mean() if not df_ts.empty else 0
            fig = px.line(df_ts, x=x_col, y=y_col, color_discrete_sequence=[_TEAL])
            fig.update_traces(line_width=1.5, mode="lines+markers",
                              marker=dict(size=4, color=_TEAL))
            fig.add_hline(
                y=avg, line_dash="dash", line_color="#555555", line_width=1,
                annotation_text=f"Prom: {avg:,.0f}",
                annotation_position="top right",
                annotation_font_size=10,
            )
            _layout = dict(
                showlegend=False, margin=dict(t=8, b=5, l=5, r=5),
                height=190,
                plot_bgcolor="white", paper_bgcolor="white",
                yaxis=dict(showgrid=True, gridcolor="#eeeeee", title=y_label,
                           title_font_size=11, tickformat=",.0f"),
                xaxis=_xaxis,
                clickmode="event+select",
            )
            if title:
                _layout["title"] = dict(text=f"<b>{title}</b>", font_size=13, x=0.5)
            fig.update_layout(**_layout)
            return fig

        # ── Cross-filter: estado por corrida ──────────────────────────────────
        # Si el usuario cambia de corrida, los filtros se reinician automáticamente.
        _viz_scope = cloud_archivo_control or "local"
        if st.session_state.get("_viz_scope") != _viz_scope:
            st.session_state.pop("viz_filtros", None)
            st.session_state["_viz_scope"] = _viz_scope
        _filtros = st.session_state.setdefault("viz_filtros", {})

        # ── Barra de filtros activos ──────────────────────────────────────────
        _LABEL_K = {"fecha": "Día", "tipo": "Tipo", "estado": "Estado"}
        if _filtros:
            _bar_c = st.columns([5, 1])
            with _bar_c[0]:
                _parts = [f"**{_LABEL_K.get(k, k)}:** `{v}`" for k, v in _filtros.items()]
                st.markdown("🔍 " + "  ·  ".join(_parts))
            with _bar_c[1]:
                if st.button("✕ Limpiar filtros", use_container_width=True, key="viz_clear_all"):
                    st.session_state.viz_filtros.clear()
                    st.rerun()

        # ── Helpers de filtrado ────────────────────────────────────────────────
        # No se copia de entrada: el indexado booleano ya devuelve un frame nuevo y
        # los consumidores solo hacen groupby/lectura (no mutan). Sin filtros activos
        # se devuelve el dataframe tal cual (vista por defecto, sin copias).
        def _filtrar_diario(d, f_fin, filtros):
            if not filtros:
                return d
            r = d
            if "fecha" in filtros:
                try:
                    _fd = pd.to_datetime(filtros["fecha"]).date()
                    r = r[r["Dia"] == _fd]
                except Exception:
                    pass
            if "tipo" in filtros:
                r = r[r["Tipo"] == filtros["tipo"]]
            if "estado" in filtros and not f_fin.empty and "CodigoArticulo" in f_fin.columns:
                _skus = f_fin[
                    f_fin["EstadoControl"].astype(str).str.strip() == filtros["estado"]
                ]["CodigoArticulo"].unique()
                r = r[r["CodigoArticulo"].isin(_skus)]
            return r

        def _filtrar_fin(f, d, filtros):
            if not filtros:
                return f
            r = f
            if "estado" in filtros:
                r = r[r["EstadoControl"].astype(str).str.strip() == filtros["estado"]]
            if ("fecha" in filtros or "tipo" in filtros) and not d.empty and "CodigoArticulo" in d.columns:
                _otros = {k: v for k, v in filtros.items() if k != "estado"}
                _d_f = _filtrar_diario(d, f, _otros)
                r = r[r["CodigoArticulo"].isin(_d_f["CodigoArticulo"].unique())]
            return r

        # Preparar diario_base (movimientos del período con Dia). mov_periodo ya viene
        # con la columna Dia desde prepare_viz_movimientos; dropna devuelve un frame
        # nuevo, así que no hace falta copiar de nuevo.
        _diario_base = pd.DataFrame()
        if not mov_periodo.empty and "Fecha_dt" in mov_periodo.columns:
            _diario_base = mov_periodo.dropna(subset=["Fecha_dt"])
            if "Dia" not in _diario_base.columns:
                _diario_base = _diario_base.assign(Dia=_diario_base["Fecha_dt"].dt.date)

        # _diario_f: con TODOS los filtros (fecha+tipo+estado) → para distribuciones
        # _diario_ts: sin filtro de fecha → para series temporales (muestran período completo)
        _diario_f = _filtrar_diario(_diario_base, fin, _filtros) if not _diario_base.empty else pd.DataFrame()
        _filtros_sin_fecha = {k: v for k, v in _filtros.items() if k != "fecha"}
        _diario_ts = _filtrar_diario(_diario_base, fin, _filtros_sin_fecha) if not _diario_base.empty else pd.DataFrame()
        _fin_f = _filtrar_fin(fin, _diario_base, _filtros)
        _fecha_hl = _filtros.get("fecha")

        # ── Distribuciones (fila de 4 columnas) ──────────────────────────────
        _cd1, _cd2, _cd3, _cd4 = st.columns(4)

        with _cd1:
            ev_dist_dif = None
            if "DiferenciaAbsoluta" in fin.columns:
                dif_abs = pd.to_numeric(_fin_f["DiferenciaAbsoluta"], errors="coerce").fillna(0)
                bins_dif = [0, 1, 5, 20, 100, float("inf")]
                labels_dif = ["Sin dif.", "1-5", "5-20", "20-100", "100+"]
                cats = pd.cut(dif_abs, bins=bins_dif, labels=labels_dif, right=False)
                dist_dif = cats.value_counts().reindex(labels_dif, fill_value=0).reset_index()
                dist_dif.columns = ["Rango", "SKUs"]
                ev_dist_dif = st.plotly_chart(
                    _pct_bar(dist_dif, "Rango", "SKUs", "SKUs por diferencia absoluta"),
                    on_select="rerun", key="ch_dist_dif", use_container_width=True,
                )

        with _cd2:
            ev_dist_mov = None
            if not _diario_f.empty:
                q_abs = _diario_f["Cantidad"].abs()
                bins_mov = [0, 1, 5, 10, 50, 100, float("inf")]
                labels_mov = ["0-1", "1-5", "5-10", "10-50", "50-100", "100+"]
                cats_mov = pd.cut(q_abs, bins=bins_mov, labels=labels_mov, right=False)
                dist_mov = cats_mov.value_counts().reindex(labels_mov, fill_value=0).reset_index()
                dist_mov.columns = ["Rango", "Movimientos"]
                ev_dist_mov = st.plotly_chart(
                    _pct_bar(dist_mov, "Rango", "Movimientos", "Movimientos por cantidad"),
                    on_select="rerun", key="ch_dist_mov", use_container_width=True,
                )

        with _cd3:
            ev_dist_est = None
            if "EstadoControl" in fin.columns:
                dist_est = fin["EstadoControl"].astype(str).str.strip().value_counts().reset_index()
                dist_est.columns = ["Estado", "SKUs"]
                dist_est = dist_est.sort_values("SKUs", ascending=False)
                total_est = dist_est["SKUs"].sum()
                dist_est["PctLabel"] = (dist_est["SKUs"] / total_est * 100).apply(
                    lambda v: f"{v:.0f}%"
                )
                _est_sel = _filtros.get("estado")
                _est_cmap = {
                    "OK": "#28a745" if (_est_sel is None or _est_sel == "OK") else "#cccccc",
                    "Diferencia": "#dc3545" if (_est_sel is None or _est_sel == "Diferencia") else "#cccccc",
                }
                fig_est = px.bar(
                    dist_est, x="Estado", y="SKUs", text="PctLabel",
                    color="Estado", color_discrete_map=_est_cmap,
                    title="<b>SKUs por estado de control</b>",
                )
                fig_est.update_traces(textposition="outside", marker_line_width=0)
                _est_ymax = dist_est["SKUs"].max() if not dist_est.empty else 1
                fig_est.update_layout(
                    title=dict(font=dict(size=13, color="#0c4a6e"), x=0, xref="paper"),
                    showlegend=False, margin=dict(t=40, b=5, l=5, r=5), height=210,
                    plot_bgcolor="white", paper_bgcolor="white",
                    yaxis=dict(showgrid=True, gridcolor="#eeeeee", title="",
                               range=[0, _est_ymax * 1.25]),
                    xaxis=dict(title=""),
                    clickmode="event+select",
                )
                ev_dist_est = st.plotly_chart(
                    fig_est, on_select="rerun", key="ch_dist_est", use_container_width=True,
                )

        with _cd4:
            ev_dist_tipo = None
            if not _diario_base.empty and "Tipo" in _diario_base.columns:
                tipo_counts = (
                    _diario_base.groupby("Tipo").size().reset_index(name="Movimientos")
                    .sort_values("Movimientos", ascending=True)
                )
                _tipo_sel = _filtros.get("tipo")
                _tipo_colors = [
                    _TEAL if (_tipo_sel is None or t == _tipo_sel) else "#cccccc"
                    for t in tipo_counts["Tipo"]
                ]
                _total_tipo = tipo_counts["Movimientos"].sum()
                tipo_counts["PctLabel"] = (tipo_counts["Movimientos"] / _total_tipo * 100).apply(
                    lambda v: f"{v:.0f}%"
                )
                fig_tipo = px.bar(
                    tipo_counts, x="Movimientos", y="Tipo", orientation="h",
                    text="PctLabel", title="<b>Movimientos por tipo</b>",
                    color_discrete_sequence=[_TEAL],
                )
                fig_tipo.update_traces(
                    textposition="outside", marker_color=_tipo_colors, marker_line_width=0,
                )
                fig_tipo.update_layout(
                    title=dict(font=dict(size=13, color="#0c4a6e"), x=0, xref="paper"),
                    showlegend=False, margin=dict(t=40, b=5, l=5, r=5), height=210,
                    plot_bgcolor="white", paper_bgcolor="white",
                    yaxis=dict(showgrid=False, title=""),
                    xaxis=dict(showgrid=True, gridcolor="#eeeeee", title=""),
                    clickmode="event+select",
                )
                ev_dist_tipo = st.plotly_chart(
                    fig_tipo, on_select="rerun", key="ch_dist_tipo", use_container_width=True,
                )

        # ── Panel KPI del día seleccionado ────────────────────────────────────
        if _fecha_hl and not _diario_f.empty:
            try:
                st.markdown(f"##### {_fecha_hl}")
                _k1, _k2, _k3, _k4 = st.columns(4)
                _k1.metric("Líneas", f"{len(_diario_f):,}")
                _k2.metric("SKUs distintos", f"{_diario_f['CodigoArticulo'].nunique():,}")
                _k3.metric("Neto del día", f"{_diario_f['Cantidad'].sum():,.0f}")
                _doc_kpi = "Documento"
                if _doc_kpi in _diario_f.columns:
                    _k4.metric("Facturas", f"{_diario_f[_doc_kpi].replace('', pd.NA).dropna().nunique():,}")
            except Exception:
                pass

        # ── Series temporales ─────────────────────────────────────────────────
        # Comportamiento Power BI: todos los gráficos usan _diario_f.
        # Cuando hay filtro de fecha, _diario_f ya solo tiene ese día → todos
        # los gráficos muestran un solo punto/barra (igual que Power BI).
        # Sin filtro de fecha, _diario_f == _diario_ts → período completo.
        # El stock diario es especial: necesita la acumulación completa para
        # obtener el valor correcto; la hace con _diario_ts y después filtra.
        _ev_stock = _ev_neto = _ev_skus = _ev_lineas = _ev_fact = None
        if not _diario_base.empty:
            if pd.notna(fecha_inicial) and pd.notna(fecha_final):
                st.markdown("**Evolución del stock diario**")
                # Acumular siempre sobre el período completo (sin filtro fecha)
                _neto_d1 = _diario_ts.groupby("Dia")["Cantidad"].sum()
                dias_rango = pd.date_range(
                    fecha_inicial + pd.Timedelta(days=1), fecha_final, freq="D"
                )
                stock_d = pd.Series(0.0, index=[d.date() for d in dias_rango])
                stock_d.update(_neto_d1)
                stock_acum = (stock_inicial + stock_d.cumsum()).reset_index()
                stock_acum.columns = ["Día", "Stock"]
                # Si hay fecha seleccionada, mostrar solo ese punto
                if _fecha_hl:
                    try:
                        _hl_d = pd.to_datetime(_fecha_hl).date()
                        stock_show = stock_acum[stock_acum["Día"] == _hl_d]
                    except Exception:
                        stock_show = stock_acum
                    _s_mode = "markers"
                    _s_msize = 12
                else:
                    stock_show = stock_acum
                    _s_mode = "lines+markers"
                    _s_msize = 3
                stock_show = stock_show.copy()
                _xaxis_stock = _date_xaxis(stock_show, "Día")
                fig_stock = px.line(stock_show, x="Día", y="Stock",
                                    color_discrete_sequence=[_TEAL])
                fig_stock.update_traces(line_width=1.5, mode=_s_mode,
                                        marker=dict(size=_s_msize, color=_TEAL))
                if not _fecha_hl:
                    fig_stock.add_hline(
                        y=stock_final_foto, line_dash="dash", line_color="#555555", line_width=1,
                        annotation_text=f"Foto final: {stock_final_foto:,.0f}",
                        annotation_position="top right", annotation_font_size=10,
                    )
                fig_stock.update_layout(
                    showlegend=False, margin=dict(t=8, b=5, l=5, r=5), height=210,
                    plot_bgcolor="white", paper_bgcolor="white",
                    yaxis=dict(showgrid=True, gridcolor="#eeeeee", title="Unidades",
                               tickformat=",.0f"),
                    xaxis=_xaxis_stock,
                    clickmode="event+select",
                )
                _ev_stock = st.plotly_chart(
                    fig_stock, on_select="rerun", key="ch_stock", use_container_width=True,
                )

            _tc1, _tc2 = st.columns(2)

            with _tc1:
                st.markdown("**Neto diario (entradas − salidas)**")
                _neto_d2 = _diario_f.groupby("Dia")["Cantidad"].sum() if not _diario_f.empty else pd.Series(dtype=float)
                if not _neto_d2.empty:
                    neto_dia = _neto_d2.reset_index()
                    neto_dia.columns = ["Día", "Neto"]
                    _xaxis_neto = _date_xaxis(neto_dia, "Día")
                    neto_dia["Color"] = neto_dia["Neto"].apply(
                        lambda v: "Ingreso" if v >= 0 else "Egreso"
                    )
                    fig_neto = px.bar(neto_dia, x="Día", y="Neto", color="Color",
                                      color_discrete_map={"Ingreso": _TEAL, "Egreso": "#dc3545"})
                    fig_neto.add_hline(y=0, line_dash="dash", line_color="#888888", line_width=1)
                    fig_neto.update_layout(
                        showlegend=False, margin=dict(t=8, b=5, l=5, r=5), height=190,
                        plot_bgcolor="white", paper_bgcolor="white",
                        yaxis=dict(showgrid=True, gridcolor="#eeeeee", title="Unidades",
                                   tickformat=",.0f"),
                        xaxis=_xaxis_neto,
                        clickmode="event+select",
                    )
                    _ev_neto = st.plotly_chart(
                        fig_neto, on_select="rerun", key="ch_neto", use_container_width=True,
                    )

                st.markdown("**SKUs distintos movidos por día**")
                if not _diario_f.empty:
                    skus_dia = _diario_f.groupby("Dia")["CodigoArticulo"].nunique().reset_index()
                    skus_dia.columns = ["Día", "SKUs"]
                    _ev_skus = st.plotly_chart(
                        _ts_line(skus_dia, "Día", "SKUs", "", "SKUs"),
                        on_select="rerun", key="ch_skus", use_container_width=True,
                    )

            with _tc2:
                st.markdown("**Líneas por día**")
                if not _diario_f.empty:
                    trans_dia = _diario_f.groupby("Dia").size().reset_index()
                    trans_dia.columns = ["Día", "Líneas"]
                    _ev_lineas = st.plotly_chart(
                        _ts_line(trans_dia, "Día", "Líneas", "", "Líneas"),
                        on_select="rerun", key="ch_lineas", use_container_width=True,
                    )

                doc_col = "Documento"
                if doc_col in _diario_base.columns and _diario_base[doc_col].replace("", pd.NA).notna().any():
                    st.markdown("**Facturas por día**")
                    if not _diario_f.empty:
                        fact_dia = (
                            _diario_f[_diario_f[doc_col].replace("", pd.NA).notna()]
                            .groupby("Dia")[doc_col].nunique().reset_index()
                        )
                        fact_dia.columns = ["Día", "Facturas"]
                        if not fact_dia.empty:
                            _ev_fact = st.plotly_chart(
                                _ts_line(fact_dia, "Día", "Facturas", "", "Facturas"),
                                on_select="rerun", key="ch_fact", use_container_width=True,
                            )
        else:
            st.info("Las series temporales requieren movimientos guardados. Volvé a ejecutar el análisis del cliente.")

        # ── Procesar eventos de click (cross-filter) ──────────────────────────
        _new_filtros = dict(_filtros)
        _changed = False

        # Fecha: click en cualquier serie temporal (x = fecha)
        for _ev_ts in [_ev_stock, _ev_neto, _ev_skus, _ev_lineas, _ev_fact]:
            if (
                _ev_ts is not None
                and hasattr(_ev_ts, "selection")
                and _ev_ts.selection
                and _ev_ts.selection.get("points")
            ):
                _clicked_x = str(_ev_ts.selection["points"][0].get("x", ""))[:10]
                if _clicked_x:
                    if _new_filtros.get("fecha") == _clicked_x:
                        _new_filtros.pop("fecha", None)
                    else:
                        _new_filtros["fecha"] = _clicked_x
                    _changed = True
                    break

        # Tipo: click en el gráfico horizontal de tipos (y = nombre del tipo)
        if not _changed and ev_dist_tipo is not None and hasattr(ev_dist_tipo, "selection"):
            _pts_tipo = (ev_dist_tipo.selection or {}).get("points", [])
            if _pts_tipo:
                _clicked_tipo = str(_pts_tipo[0].get("y", _pts_tipo[0].get("x", "")))
                if _clicked_tipo:
                    if _new_filtros.get("tipo") == _clicked_tipo:
                        _new_filtros.pop("tipo", None)
                    else:
                        _new_filtros["tipo"] = _clicked_tipo
                    _changed = True

        # Estado: click en el gráfico de estado (x = "OK" / "Diferencia")
        if not _changed and ev_dist_est is not None and hasattr(ev_dist_est, "selection"):
            _pts_est = (ev_dist_est.selection or {}).get("points", [])
            if _pts_est:
                _clicked_est = str(_pts_est[0].get("x", ""))
                if _clicked_est in ("OK", "Diferencia"):
                    if _new_filtros.get("estado") == _clicked_est:
                        _new_filtros.pop("estado", None)
                    else:
                        _new_filtros["estado"] = _clicked_est
                    _changed = True

        if _changed:
            st.session_state.viz_filtros = _new_filtros
            st.rerun()

        # ── Tabla por Unidad de Gestión (Categoria) ───────────────────────────
        st.divider()
        st.markdown("**Por Unidad de Gestión (categoría)**")
        if "Categoria" not in fin.columns or (fin["Categoria"].astype(str).str.strip() == "").all():
            st.info(
                "Esta corrida no tiene categoría. Mapeá la columna de **Categoria** (Unidad de "
                "Gestión) en el maestro y re-ejecutá el análisis para ver este desglose."
            )
        else:
            cat = fin.copy()
            cat["Categoria"] = cat["Categoria"].astype(str).str.strip().replace("", "Sin categoría")
            por_cat = cat.groupby("Categoria").agg(
                Productos=("CodigoArticulo", "nunique"),
                StockInformado=("StockInformado", "sum"),
                StockCalculado=("StockCalculado", "sum"),
                DifUnidades=("Diferencia", "sum"),
            ).reset_index()
            por_cat["DifPct"] = por_cat.apply(
                lambda r: (r["DifUnidades"] / r["StockInformado"] * 100) if r["StockInformado"] else 0.0,
                axis=1,
            )
            por_cat = por_cat.sort_values("DifUnidades")
            st.dataframe(
                por_cat.rename(columns={
                    "Categoria": "Unidad de Gestión", "DifUnidades": "Dif. Stock Unidades",
                    "DifPct": "Dif. Stock %",
                }).style.format({
                    "Productos": "{:,.0f}", "StockInformado": "{:,.0f}", "StockCalculado": "{:,.0f}",
                    "Dif. Stock Unidades": "{:,.0f}", "Dif. Stock %": "{:.1f}%",
                }),
                use_container_width=True, hide_index=True,
            )


# ══════════════════════════════════════════════════════════════════════════════
# PÁGINA: CHAT
# ══════════════════════════════════════════════════════════════════════════════

elif page == "💬 Consultar con IA":
    st.title("💬 Consultar con IA")
    st.caption("Hacé preguntas sobre el control de stock en lenguaje natural.")

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        st.error("No se encontró ANTHROPIC_API_KEY en el archivo .env")
        st.stop()

    nombre_reporte = report_label or "reporte"
    context_key = f"ctx_{nombre_reporte}"
    if context_key not in st.session_state:
        st.session_state[context_key] = build_claude_context(data, nombre_reporte)

    if "messages" not in st.session_state:
        st.session_state.messages = []

    col_chat, col_sugeridas = st.columns([3, 1])

    with col_sugeridas:
        st.markdown("**Preguntas sugeridas**")
        sugeridas = [
            "¿Cuántos SKUs tienen diferencia crítica?",
            "¿Cuál es el SKU con mayor diferencia?",
            "¿Qué porcentaje del control cerró OK?",
            "¿Hay posibles movimientos duplicados para revisar?",
            "¿Qué recomendás revisar primero?",
            "¿Las diferencias son mayormente positivas o negativas?",
        ]
        for sug in sugeridas:
            if st.button(sug, use_container_width=True, key=f"sug_{sug[:20]}"):
                st.session_state.messages.append({"role": "user", "content": sug})
                st.rerun()

        st.divider()
        if st.button("🗑️ Limpiar conversación", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

    with col_chat:
        chat_container = st.container(height=480)
        with chat_container:
            if not st.session_state.messages:
                st.markdown(
                    "_Hola! Puedo responder preguntas sobre este reporte de control de stock. "
                    "Usá las sugerencias de la derecha o escribí tu propia pregunta._"
                )
            for msg in st.session_state.messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

        # Generar respuesta si el último mensaje es del usuario
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            with chat_container:
                with st.chat_message("assistant"):
                    with st.spinner("Analizando..."):
                        try:
                            client = anthropic.Anthropic(api_key=api_key)
                            # Historial visible (solo texto) como base de la conversación
                            api_messages = [
                                {"role": m["role"], "content": m["content"]}
                                for m in st.session_state.messages
                            ]
                            answer = ""
                            herramientas_usadas = []
                            # Loop de tool use: la IA puede pedir datos antes de responder
                            for _ in range(6):
                                response = client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=1024,
                                    system=st.session_state[context_key],
                                    tools=CHAT_TOOLS,
                                    messages=api_messages,
                                )
                                if response.stop_reason == "tool_use":
                                    api_messages.append({"role": "assistant", "content": response.content})
                                    tool_results = []
                                    for block in response.content:
                                        if block.type == "tool_use":
                                            herramientas_usadas.append(f"{block.name}({block.input.get('codigo','')})")
                                            resultado = run_chat_tool(
                                                block.name,
                                                block.input,
                                                control_full,
                                                active_movements,
                                            )
                                            tool_results.append({
                                                "type": "tool_result",
                                                "tool_use_id": block.id,
                                                "content": json.dumps(resultado, ensure_ascii=False, default=str),
                                            })
                                    api_messages.append({"role": "user", "content": tool_results})
                                    continue
                                # Respuesta final (texto)
                                answer = "".join(b.text for b in response.content if b.type == "text")
                                break

                            if not answer:
                                answer = "No pude generar una respuesta. Probá reformular la pregunta."
                            if herramientas_usadas:
                                st.caption("🔎 Consulté: " + ", ".join(herramientas_usadas))
                            st.markdown(answer)
                            st.session_state.messages.append({"role": "assistant", "content": answer})
                        except Exception as exc:
                            st.error(f"Error al conectar con Claude: {exc}")

        prompt = st.chat_input("Preguntá sobre el control de stock...")
        if prompt:
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.rerun()
