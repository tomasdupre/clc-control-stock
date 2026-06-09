"""
Capa de almacenamiento en la nube (Supabase) para CLC Control Inteligente.

Guarda y lee datos organizados por CLIENTE, con HISTORIAL de corridas:
- Tabla `clientes`     : cada empresa.
- Tabla `corridas`     : cada análisis ejecutado (parámetros + KPIs + rutas de archivos).
- Storage `reportes`   : los archivos pesados (control en parquet, xlsx, diagnóstico).

Credenciales: se leen de st.secrets (en la nube) o variables de entorno / .env (local):
    SUPABASE_URL=https://xxxxx.supabase.co
    SUPABASE_KEY=<clave privada de Supabase>

La clave de Supabase es sensible. En produccion debe vivir en los secrets de
Streamlit o variables de entorno del servidor, nunca en el repositorio ni en el
codigo fuente.

Si no están configuradas, `is_configured()` devuelve False y la app sigue funcionando
solo con los archivos locales.
"""
import io
import os
from datetime import datetime, timezone

import pandas as pd

BUCKET = "reportes"
_client = None


def _get_credentials():
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    # En Streamlit Cloud las credenciales viven en st.secrets.
    if not (url and key):
        try:
            import streamlit as st
            url = url or st.secrets.get("SUPABASE_URL", "")
            key = key or st.secrets.get("SUPABASE_KEY", "")
        except Exception:
            pass
    return url.strip(), key.strip()


def is_configured():
    url, key = _get_credentials()
    return bool(url and key)


def get_client():
    """Devuelve el cliente de Supabase (cacheado). Lanza error claro si falta config."""
    global _client
    if _client is not None:
        return _client
    url, key = _get_credentials()
    if not (url and key):
        raise RuntimeError(
            "Faltan credenciales de Supabase. Definí SUPABASE_URL y SUPABASE_KEY "
            "en el archivo .env (local) o en los secrets de Streamlit (nube)."
        )
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError(
            "Falta la librería 'supabase'. Instalala con: python -m pip install supabase"
        ) from exc
    _client = create_client(url, key)
    return _client


# ── Clientes ────────────────────────────────────────────────────────────────

def list_clientes():
    """Lista los clientes (nombre) ordenados alfabéticamente."""
    client = get_client()
    res = client.table("clientes").select("id,nombre").order("nombre").execute()
    return res.data or []


def upsert_cliente(nombre):
    """Devuelve el id del cliente, creándolo si no existe."""
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("El nombre de cliente no puede estar vacío.")
    client = get_client()
    existing = client.table("clientes").select("id").eq("nombre", nombre).execute()
    if existing.data:
        return existing.data[0]["id"]
    inserted = client.table("clientes").insert({"nombre": nombre}).execute()
    return inserted.data[0]["id"]


# ── Corridas (historial de análisis por cliente) ──────────────────────────────

def _upload_bytes(path, data, content_type):
    client = get_client()
    client.storage.from_(BUCKET).upload(
        path,
        data,
        {"content-type": content_type, "upsert": "true"},
    )
    return path


def save_corrida(nombre_cliente, parametros, resumen, control_df,
                 xlsx_path=None, diagnostico_path=None, nota=""):
    """
    Guarda una corrida completa en la nube:
      - sube el control (parquet), el xlsx y el diagnóstico al Storage,
      - inserta la fila en `corridas` con parámetros y KPIs.
    Devuelve el id de la corrida.
    """
    cliente_id = upsert_cliente(nombre_cliente)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prefijo = f"{cliente_id}/{ts}"

    # Control en parquet (compacto)
    buf = io.BytesIO()
    control_df.to_parquet(buf, index=False)
    archivo_control = _upload_bytes(
        f"{prefijo}/control.parquet", buf.getvalue(), "application/octet-stream"
    )

    archivo_xlsx = None
    if xlsx_path and os.path.exists(xlsx_path):
        with open(xlsx_path, "rb") as fh:
            archivo_xlsx = _upload_bytes(
                f"{prefijo}/reporte.xlsx", fh.read(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    archivo_diag = None
    if diagnostico_path and os.path.exists(diagnostico_path):
        with open(diagnostico_path, "rb") as fh:
            archivo_diag = _upload_bytes(
                f"{prefijo}/diagnostico.txt", fh.read(), "text/plain"
            )

    client = get_client()
    fila = {
        "cliente_id": cliente_id,
        "parametros": parametros or {},
        "resumen": resumen or {},
        "archivo_control": archivo_control,
        "archivo_reporte_xlsx": archivo_xlsx,
        "archivo_diagnostico": archivo_diag,
        "nota": nota or "",
    }
    inserted = client.table("corridas").insert(fila).execute()
    return inserted.data[0]["id"]


def list_corridas(cliente_id):
    """Lista las corridas de un cliente, de la más nueva a la más vieja."""
    client = get_client()
    res = (
        client.table("corridas")
        .select("id,creado_en,parametros,resumen,archivo_reporte_xlsx,archivo_diagnostico,nota")
        .eq("cliente_id", cliente_id)
        .order("creado_en", desc=True)
        .execute()
    )
    return res.data or []


def load_corrida_control(archivo_control):
    """Descarga el parquet de control de una corrida y lo devuelve como DataFrame."""
    client = get_client()
    data = client.storage.from_(BUCKET).download(archivo_control)
    return pd.read_parquet(io.BytesIO(data))


def download_file(path):
    """Descarga cualquier archivo del bucket (xlsx, txt) como bytes."""
    client = get_client()
    return client.storage.from_(BUCKET).download(path)
