# CLC Control Inteligente de Stock

Primera version local para interpretar archivos de clientes, proponer mapeos de columnas, normalizar datos, calcular diferencias de stock y generar salidas listas para analisis interno o Power BI.

Esta version no usa base de datos, no usa API de OpenAI y no tiene interfaz web. Todo funciona con archivos locales.

## 1. Instalar Python

1. Entrar a https://www.python.org/downloads/
2. Descargar Python para Windows.
3. Durante la instalacion, marcar la opcion **Add Python to PATH**.
4. Para verificar, abrir una terminal y ejecutar:

```powershell
python --version
```

## 2. Abrir el proyecto en Visual Studio Code

1. Abrir Visual Studio Code.
2. Ir a **File > Open Folder**.
3. Elegir la carpeta `CLC_Control_Inteligente`.
4. Abrir una terminal dentro de VS Code con **Terminal > New Terminal**.

## 3. Instalar dependencias

Desde la terminal, ubicado en la carpeta del proyecto:

```powershell
pip install -r requirements.txt
```

Las librerias usadas son:

- `pandas`: lectura, transformacion y exportacion de datos.
- `openpyxl`: lectura y escritura de Excel.
- `python-dateutil`: soporte para fechas.
- `rapidfuzz`: comparacion aproximada de nombres de columnas.

## Seguridad de credenciales

El archivo `.env` guarda claves privadas locales y nunca debe subirse al repositorio.
El proyecto incluye `.env.example` solo como plantilla segura.

Para trabajar localmente:

```powershell
copy .env.example .env
```

Despues completar en `.env` solamente las claves que necesites:

```text
ANTHROPIC_API_KEY=
SUPABASE_URL=
SUPABASE_KEY=
```

Reglas importantes:

- No pegar claves reales en `README.md`, codigo fuente, capturas o issues.
- En Streamlit Cloud, cargar las claves en **Secrets**, no en archivos del repo.
- Si una clave real se compartio por error, rotarla desde el panel del proveedor.
- Antes de subir cambios, revisar que `git status` no muestre `.env`.

## 4. Donde poner los archivos del cliente

Poner los archivos del cliente en:

```text
data/input/
```

Formatos soportados:

- `.csv`
- `.xlsx`
- `.xls`

La carpeta incluye archivos de ejemplo minimos:

- `ejemplo_maestro.csv`
- `ejemplo_stock.csv`
- `ejemplo_movimientos.csv`

Se pueden borrar cuando empieces a usar archivos reales.

## 5. Ejecutar el sistema

Desde la terminal:

```powershell
python src/main.py
```

El sistema va a:

1. Mostrar claramente los archivos actuales en `data/input`, `data/normalized` y `data/reports`.
2. Advertir si hay archivos de ejemplo o prueba en `data/input`.
3. Preguntar si queres `normalizar`, `analizar`, `limpiar`, `estado` o `salir`.
4. Si elegis `estado`, listar archivos actuales y cantidad de filas si el sistema puede leerlos.
5. Si elegis `limpiar`, borrar archivos de `data/normalized` y `data/reports`, sin borrar las carpetas.
6. Si elegis `normalizar`, preguntar si queres limpiar normalizados y reportes anteriores antes de continuar.
7. Preguntar el nombre del cliente para identificar los reportes.
8. Si elegis `normalizar`, listar los archivos encontrados en `data/input`.
9. Preguntar si cada archivo es `maestro`, `stock`, `movimientos`, si queres `saltar` ese archivo o `volver` al menu principal.
10. Mostrar columnas detectadas.
11. Proponer un mapeo contra los campos estandar de CLC.
12. Permitir corregir algun mapeo manualmente.
13. Normalizar el archivo.
14. Validar datos.
15. Generar un reporte de calidad.
16. Preguntar si queres ejecutar el control automatico de stock.

Antes de ejecutar el analisis, el sistema siempre muestra los tres archivos normalizados que va a usar, con fecha de modificacion, filas y columnas. Despues pide confirmacion para evitar analizar archivos viejos.

Flujo recomendado para un cliente real:

1. Elegir `estado` para revisar que archivos hay.
2. Elegir `limpiar` si quedan resultados de pruebas anteriores.
3. Poner archivos reales del cliente en `data/input`.
4. Elegir `normalizar`.
5. Confirmar que queres limpiar salidas anteriores si corresponde.
6. Elegir `analizar` o responder `s` cuando el sistema pregunte si queres ejecutar el control.

Durante la normalizacion:

- `saltar` omite solamente el archivo actual y sigue con el siguiente.
- `volver` cancela la normalizacion en curso y vuelve al menu principal.
- Si un Excel tiene varias hojas, tambien podes escribir `volver` en la seleccion de hoja.
- Si un Excel tiene mas de una hoja util, podes procesar otra hoja del mismo archivo. Por ejemplo, un mismo Excel puede aportar stock y movimientos.
- Las respuestas de si/no aceptan variantes como `s`, `si`, `n` o `no`.
- Si hay varios archivos del mismo tipo, el sistema los acumula en un unico normalizado en lugar de pisar el archivo anterior.
- Los normalizados incluyen `ArchivoOrigen` y `HojaOrigen` para saber de donde vino cada fila.
- Las opciones del menu tambien aceptan atajos: `n` para normalizar, `a` para analizar, `l` para limpiar, `e` para estado y `s` para salir.

## 6. Archivos de salida

Los archivos normalizados se generan en:

```text
data/normalized/
```

Salidas esperadas (desde v1.4, en formato parquet por velocidad):

- `maestro_normalizado.parquet`
- `stock_normalizado.parquet`
- `movimientos_normalizado.parquet`

Parquet es un formato binario que no se abre directo en Excel. Si necesitás verlos en Excel o Power BI, usá el botón **Exportar normalizados a Excel** en la web (genera copias `.xlsx`).
Los archivos normalizados mantienen esos nombres fijos porque el analisis automatico los necesita para saber que leer.
Si normalizas mas de un archivo del mismo tipo, las filas se combinan dentro del mismo archivo normalizado.
El reporte de calidad deja una hoja `archivos_procesados` con tipo de archivo, archivo origen, hoja origen, filas leidas, filas normalizadas, estado y observacion.

El reporte se genera en:

```text
data/reports/reporte_calidad.xlsx
```

Si ingresaste un nombre de cliente, el reporte se guarda con ese identificador. Por ejemplo, para `Cliente ABC`:

```text
data/reports/reporte_calidad_cliente_abc.xlsx
```

El reporte incluye estas hojas:

- `mapeo_columnas`
- `errores_detectados`
- `advertencias`
- `aprendizajes_sugeridos`
- `archivos_procesados`

El control automatico de stock genera:

- `data/reports/control_stock_resultado.xlsx`
- `data/reports/diagnostico_stock.txt`

Si ingresaste un nombre de cliente, genera nombres como:

- `data/reports/control_stock_resultado_cliente_abc.xlsx`
- `data/reports/diagnostico_stock_cliente_abc.txt`

## 7. Usar los archivos en Power BI

1. Abrir Power BI Desktop.
2. Usar **Obtener datos > Excel**.
3. Seleccionar los archivos de `data/normalized`.
4. Conectar esos archivos a tu plantilla de Power BI.
5. Revisar primero `reporte_calidad.xlsx` para entender advertencias antes de publicar resultados.

## 8. Como ejecutar el control automatico de stock sin Power BI

Primero se deben generar los archivos normalizados:

- `data/normalized/maestro_normalizado.parquet`
- `data/normalized/stock_normalizado.parquet`
- `data/normalized/movimientos_normalizado.parquet`

Luego ejecutar:

```powershell
python src/main.py
```

Cuando el sistema pregunte que queres hacer, elegir:

```text
analizar
```

Antes de calcular, el sistema valida que existan:

- `data/normalized/maestro_normalizado.parquet`
- `data/normalized/stock_normalizado.parquet`
- `data/normalized/movimientos_normalizado.parquet`

Luego muestra fecha de modificacion, cantidad de filas y columnas disponibles de cada archivo. Si no corresponden al cliente actual, responder `n` para cancelar el analisis y volver a normalizar.

Antes de ejecutar el control tambien pregunta:

```text
El deposito importa para este analisis? (s/n)
```

Si respondes `s`, calcula por `CodigoArticulo + Deposito`.
Si respondes `n`, ignora deposito y calcula por `CodigoArticulo`, agrupando todos los depositos. Esta opcion es util cuando el cliente informa movimientos y stock sin un criterio confiable de deposito.

Tambien pregunta:

```text
Incluir movimientos de la misma fecha del stock inicial? (s/n)
```

Responder `n` si el stock inicial representa cierre de dia o cierre de periodo, porque esos movimientos ya deberian estar incluidos en ese stock.
Responder `s` si el stock inicial representa apertura de dia y queres sumar los movimientos de esa misma fecha.

Si confirmas que los archivos corresponden al cliente actual, el sistema genera:

```text
data/reports/control_stock_resultado.xlsx
data/reports/diagnostico_stock.txt
```

Con nombre de cliente, por ejemplo `Cliente ABC`, genera:

```text
data/reports/control_stock_resultado_cliente_abc.xlsx
data/reports/diagnostico_stock_cliente_abc.txt
```

El Excel `control_stock_resultado.xlsx` incluye estas hojas:

- `control_stock`: todos los controles calculados.
- `solo_diferencias`: solamente registros con diferencia distinta de cero.
- `resumen`: indicadores generales del control.
- `movimientos_sin_stock`: movimientos cuyo producto y deposito no aparecen en stock informado.
- `movimientos_aplicados`: movimientos que entraron dentro del rango calculable.
- `movimientos_no_aplicados`: movimientos que no entraron en ningun control, con el motivo.
- `duplicados_movimientos`: posibles movimientos repetidos por `Documento + CodigoArticulo + Fecha + CantidadNormalizada`.
- `errores`: problemas de estructura, fechas o cantidades.
- `advertencias`: situaciones que conviene revisar.

Columnas principales del control:

- `StockInicial`: primer stock informado disponible para cada `CodigoArticulo` + `Deposito`.
- `MovimientosAcumulados`: suma de movimientos posteriores a la fecha inicial y hasta la fecha controlada inclusive.
- `StockCalculado`: `StockInicial + MovimientosAcumulados`.
- `StockInformado`: stock enviado por el cliente.
- `Diferencia`: `StockInformado - StockCalculado`.
- `EstadoControl`: `OK` si la diferencia es cero, `Revisar` si la diferencia absoluta es hasta 2, `Critico` si supera 2, o `Sin datos` si no se pudo calcular.

El archivo `diagnostico_stock.txt` resume:

- cantidad de controles realizados;
- cantidad de controles OK;
- cantidad de controles con diferencia;
- top 10 productos con mayor diferencia absoluta;
- diferencias por deposito;
- diferencias por fecha;
- posibles causas y recomendaciones para CLC.

## Caso especial: Excel con stock y movimientos en hojas separadas

Algunos clientes envian un solo Excel con varias hojas utiles. En ese caso, no alcanza con procesar el archivo una sola vez.

Ejemplo usado en las pruebas:

- Archivo: `Movimientos de stock SI SF.xlsx`
- Hoja de stock: `Stocks Mensuales`
- Hoja de movimientos netos: `Movimientos`

Flujo recomendado:

1. Elegir `normalizar`.
2. Procesar `Movimientos de stock SI SF.xlsx` como `stock` usando la hoja `Stocks Mensuales`.
3. Cuando el sistema pregunte si queres procesar otra hoja del mismo archivo, responder `s`.
4. Procesar el mismo archivo como `movimientos` usando la hoja `Movimientos`.
5. Ejecutar el analisis.
6. Si el deposito no importa, responder `n` cuando pregunte por deposito.
7. Si el stock inicial es cierre de dia o cierre de periodo, responder `n` a incluir movimientos de la fecha inicial.

En este caso, usar la hoja de movimientos netos evita duplicaciones del detalle operativo y permite que el control cierre correctamente.

## 9. Como limpiar antes de probar un cliente real

Para evitar mezclar datos de prueba con datos reales, usar la opcion:

```text
limpiar
```

Esta opcion borra archivos dentro de:

- `data/normalized`
- `data/reports`

No borra las carpetas. Tampoco borra los archivos originales en `data/input`.

Antes de normalizar archivos nuevos, el sistema tambien pregunta:

```text
Queres limpiar los archivos normalizados y reportes anteriores antes de continuar? (s/n)
```

Para un cliente real, lo recomendado es responder `s`, salvo que tengas un motivo claro para conservar los resultados anteriores.

## 10. Que carpetas revisar

Antes de correr un cliente real, revisar:

- `data/input`: debe contener solamente archivos del cliente actual.
- `data/normalized`: debe estar vacia o contener solamente normalizados del cliente actual.
- `data/reports`: debe estar vacia o contener solamente reportes del cliente actual.

Tambien podes elegir:

```text
estado
```

La opcion `estado` lista archivos de entrada, normalizados y reportes. Cuando puede, muestra filas y columnas de cada Excel o CSV.

## 11. Como evitar mezclar datos de prueba con datos reales

- Borrar o mover fuera de `data/input` los archivos de ejemplo antes de cargar archivos reales.
- Si hay archivos con nombres que contienen `ejemplo`, `sample`, `test` o `prueba`, el sistema muestra una advertencia.
- Usar `limpiar` antes de normalizar un cliente nuevo.
- Antes de analizar, revisar la fecha de modificacion de los tres archivos normalizados.
- Si el sistema pregunta si los normalizados corresponden al cliente actual y no estas seguro, responder `n`.

## 12. Editar el diccionario de columnas

El diccionario esta en:

```text
rules/diccionario_columnas.csv
```

Columnas:

- `CampoCLC`: campo estandar interno.
- `Equivalencia`: nombre posible recibido desde un cliente.
- `Observacion`: explicacion simple.

Ejemplo:

```csv
CampoCLC,Equivalencia,Observacion
CodigoArticulo,CodProducto,Codigo del articulo o producto
```

Si un cliente manda una columna nueva y el sistema la marca como pendiente, se puede agregar ahi para que la proxima vez la reconozca mejor.

## 13. Editar reglas de movimientos

Las reglas estan en:

```text
rules/reglas_movimientos.csv
```

Columnas:

- `TipoMovimiento`: texto esperado en el archivo del cliente.
- `Clasificacion_CLC`: `Entrada`, `Salida` o `Transferencia`.
- `Signo`: `1`, `-1` o `revisar`.
- `Observacion`: explicacion para usuarios.

Ejemplo:

```csv
TipoMovimiento,Clasificacion_CLC,Signo,Observacion
Venta,Salida,-1,Resta stock
```

## 14. Criterios importantes

- El sistema no inventa datos.
- Si una columna no se interpreta con suficiente confianza, queda pendiente de confirmacion.
- Cada campo CLC se asigna a una sola columna origen. Si varias columnas parecen ser `CodigoArticulo`, `Descripcion` u otro campo, el sistema conserva la mejor candidata y deja las demas pendientes para revision manual.
- Si faltan columnas obligatorias, se informa en el reporte.
- Si una transferencia interna requiere revisar deposito origen/destino, queda marcada como advertencia.
- El sistema NUNCA cambia el signo ni el valor de un movimiento: usa la cantidad exactamente como la informa el cliente (`CantidadNormalizada` = `CantidadOriginal`).
- La clasificacion `Entrada`/`Salida` queda solo como informacion. Si el signo del dato no coincide con esa clasificacion, se deja una advertencia para revision manual, pero el numero no se toca.
- El control automatico no inventa stock inicial: toma el primer stock informado por producto y deposito.
- Si un SKU no aparece en la fecha inicial de stock pero aparece en la fecha final, el sistema usa `StockInicial = 0` y suma sus movimientos del periodo.
- Si no existe deposito, el sistema usa `Sin deposito`.
- Si un producto aparece en stock pero no en maestro, se controla igual y queda sin descripcion.

## 15. Historial de cambios verificados

### v1.6 — 2026-06-11 (estandarización para cualquier cliente)

**App estándar (web + nube), no atada a un cliente:**

- **Maestro y movimientos opcionales.** El análisis solo necesita el **stock** sí o sí. Si falta el maestro (clientes sin catálogo) o los movimientos (clientes que solo comparan dos fotos de stock), la app usa tablas vacías y sigue funcionando, sin crashear.
- **Diccionario de columnas ampliado.** El auto-mapeo reconoce muchos más nombres comunes de ERPs (en español e inglés): EAN/código de barras, Material, Referencia, Existencias, Stock Actual/Real/Disponible, Unidad de Gestión, Línea, Grupo, Tipo de Documento, etc.
- **Campos CLC por tipo de hoja** (desplegable acotado): maestro = `CodigoArticulo` + `Descripcion` + `Categoria` (opc.); stock = `CodigoArticulo` + `Fecha` + `StockInformado`; movimientos = `CodigoArticulo` + `Fecha` + `CantidadOriginal` + `TipoMovimiento` (opc.).
- **Signos de movimientos a elección:** por hoja a mano (entrada/salida/mantener) o por tipo con IA (clasifica ingreso/egreso/mantener y vos confirmás). Los **ajustes** usan "mantener" (respetan el signo +/− de cada fila). El sistema **nunca cambia un signo** salvo que vos lo pidas.
- **Códigos tal cual:** no se quitan ceros a la izquierda (`0658…` ≠ `658…`).
- **Almacenamiento en la nube por cliente** (Supabase): cada análisis se guarda con su historial; se abren corridas anteriores desde el panel izquierdo. Opcional (sin Supabase, funciona local).
- **Página "Visualización de datos" → "Balance de Masa":** KPIs de stock (inicial/movimientos/final/calculado/diferencia/%), desglose de movimientos por tipo, stock acumulado por mes y tabla por Unidad de Gestión (categoría).

**Verificado:** el caso Palacio sigue cerrando al 100% (5350 OK / 0 diferencias) tras todos estos cambios.

**Configuración (`.env` o secrets de Streamlit):** ver `.env.example`. `ANTHROPIC_API_KEY` (chat IA + signos), `SUPABASE_URL` y `SUPABASE_KEY` (nube, opcional).

---

### v1.5 — 2026-06-05

**IA con acceso real a la data (tool use):**

- El chat ahora puede consultar la data completa del reporte mediante herramientas (`buscar_producto`, `buscar_movimientos`). Si le preguntás por un código puntual (ej. por qué tiene dos stocks, qué movimientos tuvo, por qué da diferencia), la IA busca esa fila y sus movimientos antes de responder, en vez de limitarse a un resumen.

**Vista de "un control por producto":**

- Las **líneas de stock inicial** (donde `Fecha == FechaInicial`) son la foto base de cada producto: siempre dan diferencia 0 y no son un control real. En la web se **ocultan por defecto** en Detalle y Resumen (un toggle en el sidebar las muestra). Los KPIs se recalculan sobre la vista visible. El reporte Excel conserva todo.

**Control de productos sin foto final (stock final = 0):**

- Nuevo toggle en el análisis: *"Controlar productos sin foto final asumiendo stock 0"* (parámetro `assume_missing_final_zero`). Los productos que están en el stock inicial pero **no** aparecen en la última foto se controlan asumiendo que terminaron en 0 (`StockInicial + Movimientos` debería dar 0). Detecta productos que no cerraron bien (stock fantasma o movimientos faltantes), que antes quedaban invisibles.
- Estas filas se marcan con la columna `StockFinalAsumidoCero = True`.
- Verificado con el caso Palacio: de 1408 productos sin foto final, **1287 cerraron OK en 0** y **121 mostraron diferencia** (a revisar). Ejemplo: `PROTPARLED` arrancó con 40, tuvo -40 de movimientos, cerró en 0 → OK.

---

### v1.4 — 2026-06-04

**Optimización fuerte de velocidad (sin cambiar resultados):**

- **Archivos normalizados ahora en formato parquet** (`maestro_normalizado.parquet`, `stock_normalizado.parquet`, `movimientos_normalizado.parquet`) en lugar de `.xlsx`. Escribir 200.000 filas con Excel/openpyxl tardaba ~40 segundos; en parquet tarda **menos de 0.2 segundos** (unas 280 veces más rápido). Parquet también preserva los tipos de datos, lo que reduce errores al releer.
- **Reporte de control escrito con `xlsxwriter`** (más rápido que openpyxl para hojas grandes como `movimientos_no_aplicados`). El análisis completo del caso Palacio bajó de ~62s a ~33s. Si `xlsxwriter` no está instalado, usa openpyxl automáticamente.
- **Botón "Exportar normalizados a Excel"** en el paso final de la web: como parquet no se abre directo en Excel, este botón genera copias `.xlsx` cuando las necesites para Power BI o revisión manual.
- Nuevas dependencias: `pyarrow` (lee/escribe parquet) y `xlsxwriter`.

**Resultado verificado tras el cambio de formato (caso Palacio, `use_deposit=False`, `include_initial_date_movements=False`):**

```
total_registros_controlados: 5347
total_registros_ok:          5347
total_registros_con_diferencia: 0
total_diferencia_absoluta:   0
```

El cálculo da exactamente lo mismo que antes: el cambio es solo de formato y velocidad.

> Nota: el análisis lee los `.parquet` directamente. El reporte final de control (`control_stock_resultado*.xlsx`) y el diagnóstico siguen siendo Excel/texto, porque son los entregables.

---

### v1.3 — 2026-06-04

**Nueva interfaz web (Streamlit) + asistente con IA:**

- Se agregó `src/app.py`, una aplicación web local que corre con:

  ```powershell
  python -m streamlit run src\app.py
  ```

  Se abre en `http://localhost:8501`. Tiene cuatro secciones:
  - **⚙️ Procesar**: subir archivos, mapear columnas y correr el análisis sin tocar la terminal.
  - **📊 Resumen**: KPIs, gráficos y tabla de diferencias con colores por estado.
  - **🔍 Detalle**: filtros por estado/depósito, búsqueda de SKU, movimientos no aplicados y duplicados.
  - **💬 Consultar con IA**: preguntas en lenguaje natural sobre el reporte activo (usa la API de Anthropic).

- La API key se lee de un archivo `.env` (`ANTHROPIC_API_KEY=...`). El `.env` está en `.gitignore` y nunca se sube.
- Nuevas dependencias: `streamlit`, `anthropic`, `python-dotenv`, `plotly` (ver `requirements.txt`).

**Correcciones importantes de seguridad de datos y robustez:**

- **La limpieza ya no borra antes de tiempo.** En la versión inicial de la web, el reemplazo de normalizados/reportes se ejecutaba al apretar "Continuar", *antes* de generar los nuevos. Si el mapeo se trababa, se perdían los datos anteriores. Ahora la limpieza ocurre recién cuando los normalizados nuevos ya están armados en memoria, y el checkbox viene **desactivado por defecto**.
- **Validación de campos obligatorios en el mapeo.** El paso de normalización solo se habilita si están asignados los campos que el cálculo necesita por tipo (`stock`: CodigoArticulo, Fecha, StockInformado; `movimientos`: CodigoArticulo, Fecha, CantidadOriginal; `maestro`: CodigoArticulo). Las columnas en `PendienteConfirmacion` que no son obligatorias ya no bloquean el avance. Antes el sistema podía trabarse con un error silencioso.
- **Columna "Valores de ejemplo" en el editor de mapeo.** Muestra datos reales de cada columna para distinguir a simple vista cuál tiene códigos y cuál descripciones, evitando confundir un campo con otro.

**Mejoras al diccionario de columnas:**

- Se agregaron equivalencias de `CodigoArticulo`: `Cod. Producto`, `Cod Producto`, `Codigo Interno`, `Código Interno`. Esto corrige el caso donde la columna `Producto` (que en algunos clientes es la descripción) se llevaba el campo `CodigoArticulo` por error.

**Mapeo correcto del archivo `Movimientos de stock SI SF.xlsx` (cliente Palacio):**

- Hoja `Stocks Mensuales` como `stock`: `Cod. Producto`→CodigoArticulo, `CD`→StockInformado, `Producto`→Descripcion, `Fecha`→Fecha.
- Hoja `Movimientos` como `movimientos`: `Codigo Producto`→CodigoArticulo, `Fecha`→Fecha, `Cantidad`→CantidadOriginal (cantidad neta con signo).
- Maestro `Maesto de productos.xlsx`: `Código Interno`→CodigoArticulo, `Producto`→Descripcion, `categoria`→Categoria, `marca`→Marca.
- Con `use_deposit=False` e `include_initial_date_movements=False`, el control cierra al **100%**:

  ```
  total_registros_controlados: 5347
  total_registros_ok:          5347
  total_registros_con_diferencia: 0
  total_diferencia_absoluta:   0
  ```

> Nota: `CD` (cantidad de stock de este cliente) **no** se agregó al diccionario global porque "CD" es ambiguo (en otros clientes puede ser un centro de distribución). Se mapea a mano en el editor, donde la columna de ejemplos ayuda a identificarlo.

---

### v1.2 — 2026-06-04

**Mejoras de precisión en el cálculo:**

- **Deduplicación real antes del cálculo** (`deduplicate_movements` en `stock_analyzer.py`): los movimientos exactamente duplicados por `Documento + CodigoArticulo + Fecha + CantidadNormalizada` ahora se eliminan antes de alimentar el cálculo. Antes se detectaban pero no se removían, lo que hacía que se sumaran dos veces y produjeran coincidencias falsas (el StockCalculado "cerraba" solo porque el duplicado compensaba la diferencia). Los duplicados eliminados quedan documentados en la hoja `duplicados_movimientos` del Excel de control. Solo se deduplican filas con todos los campos completos — movimientos sin número de documento no se tocan.

- **Vectorización de `calculate_control_table`** (`stock_analyzer.py`): el loop fila por fila fue reemplazado por un range-join vectorizado (merge por SKU/Deposito + filtro de fechas + groupby). El resultado numérico es idéntico al anterior para los mismos movimientos. La mejora es de rendimiento y mantenibilidad.

**Resultado con archivos normalizados del 2026-06-03 (con deduplicación activa):**

```
total_registros_controlados: 5347
total_registros_ok:          4909
total_registros_con_diferencia: 438
total_diferencia_absoluta:   86298
duplicados_eliminados:       4
```

Los 2 registros que pasaron de OK a diferencia son SKUs que "cerraban" por el efecto del movimiento duplicado, no por estar realmente bien.

---

### v1.1 — 2026-06-04

**Correcciones internas (sin impacto en resultados):**

- `count_rows_and_columns` en `main.py`: ahora devuelve siempre 3 valores (filas, columnas, detalle). Antes devolvía 2 para CSV y 3 para Excel, lo que requería un chequeo frágil con `len(result)` en dos lugares. Ya no se necesita ese chequeo.
- `classify_movements_application` en `stock_analyzer.py`: el loop fila por fila fue reemplazado por operaciones vectorizadas con `numpy.select`. El resultado es idéntico; mejora la velocidad y la legibilidad.
- `numpy` agregado explícitamente a `requirements.txt` (ya era dependencia transitiva de pandas).

**Resultado verificado con archivos normalizados del 2026-06-03:**

```
total_registros_controlados: 5347
total_registros_ok:          4911
total_registros_con_diferencia: 436
total_diferencia_absoluta:   86294
```

Parámetros usados: `use_deposit=False`, `include_initial_date_movements=False`.

> Nota: el resultado de 0 diferencias documentado en el contexto original corresponde a una corrida con archivos normalizados distintos (`palacio_final`). El resultado correcto con los archivos normalizados actuales es el que aparece arriba.

---

## 16. Mejoras sugeridas para una version 2

- Guardar mapeos confirmados por cliente.
- Agregar una pantalla simple con Streamlit.
- Detectar automaticamente si un archivo es maestro, stock o movimientos.
- Consolidar multiples archivos en una unica salida historica.
- Agregar tolerancias configurables por cliente o categoria.
- Incorporar reglas especificas por cliente.
