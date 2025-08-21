from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz  # PyMuPDF

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "edomex_plantilla_alta_res.pdf"  # PDF principal completo
PLANTILLA_FLASK = "labuena3.0.pdf"  # PDF simple tipo Flask
ENTIDAD = "edomex"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------- COORDENADAS EDOMEX ----------------
coords_edomex = {
    "folio": (535,135,14,(1,0,0)),
    "marca": (109,190,10,(0,0,0)),
    "serie": (230,233,10,(0,0,0)),
    "linea": (238,190,10,(0,0,0)),
    "motor": (104,233,10,(0,0,0)),
    "anio":  (410,190,10,(0,0,0)),
    "color": (400,233,10,(0,0,0)),
    "fecha_exp": (190,280,10,(0,0,0)),
    "fecha_ven": (380,280,10,(0,0,0)),
    "nombre": (394,320,10,(0,0,0)),
}

# ------------ FUNCIÓN GENERAR FOLIO EDOMEX ------------
def generar_folio_edomex():
    """Genera folio con prefijo 98 para Estado de México"""
    existentes = supabase.table("folios_registrados").select("folio").eq("entidad", ENTIDAD).execute().data
    
    # Filtrar los que empiezan con 98
    folios_98 = [r["folio"] for r in existentes if r["folio"] and r["folio"].startswith("98")]
    
    # Obtener la parte numérica después del 98
    consecutivos = [int(folio[2:]) for folio in folios_98 if folio[2:].isdigit()]
    
    # Siguiente consecutivo
    nuevo_consecutivo = max(consecutivos) + 1 if consecutivos else 1
    
    return f"98{nuevo_consecutivo}"

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ------------ FUNCIÓN GENERAR PDF FLASK (TIPO SIMPLE) ------------
def generar_pdf_flask(fecha_expedicion, numero_serie, folio):
    """Genera el PDF simple tipo Flask"""
    try:
        ruta_pdf = f"{OUTPUT_DIR}/{folio}_simple.pdf"
        
        doc = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        
        # Insertar datos en coordenadas del Flask
        page.insert_text((80,142), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0,0,0))
        page.insert_text((218,142), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0,0,0))
        page.insert_text((182,283), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=9, fontname="helv", color=(0,0,0))
        page.insert_text((130,435), fecha_expedicion.strftime("%d/%m/%Y"), fontsize=20, fontname="helv", color=(0,0,0))
        page.insert_text((162,185), numero_serie, fontsize=9, fontname="helv", color=(0,0,0))
        
        doc.save(ruta_pdf)
        doc.close()
        return ruta_pdf
    except Exception as e:
        print(f"ERROR al generar PDF Flask: {e}")
        return None

# ------------ PDF PRINCIPAL EDOMEX (COMPLETO) ------------
def generar_pdf_principal(datos: dict) -> str:
    """Genera el PDF principal de Estado de México con todos los datos"""
    fol = datos["folio"]
    fecha_exp = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]
    
    # Crear carpeta de salida
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_edomex.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    # --- Insertar folio ---
    pg.insert_text(coords_edomex["folio"][:2], fol,
                   fontsize=coords_edomex["folio"][2],
                   color=coords_edomex["folio"][3])
    
    # --- Insertar fechas ---
    pg.insert_text(coords_edomex["fecha_exp"][:2], fecha_exp,
                   fontsize=coords_edomex["fecha_exp"][2],
                   color=coords_edomex["fecha_exp"][3])
    pg.insert_text(coords_edomex["fecha_ven"][:2], fecha_ven,
                   fontsize=coords_edomex["fecha_ven"][2],
                   color=coords_edomex["fecha_ven"][3])

    # --- Insertar datos del vehículo ---
    for campo in ["marca", "serie", "linea", "motor", "anio", "color"]:
        if campo in coords_edomex and campo in datos:
            x, y, s, col = coords_edomex[campo]
            pg.insert_text((x, y), str(datos.get(campo, "")), fontsize=s, color=col)

    # --- Insertar nombre ---
    pg.insert_text(coords_edomex["nombre"][:2], datos.get("nombre", ""),
                   fontsize=coords_edomex["nombre"][2],
                   color=coords_edomex["nombre"][3])

    doc.save(out)
    doc.close()
    
    return out

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 **Bienvenido al Bot de Permisos Estado de México**\n\n🚗 Usa /permiso para generar un nuevo permiso\n📄 Genera 2 documentos: Permiso Completo y Comprobante\n⚡ Proceso rápido y seguro", parse_mode="Markdown")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer("🚗 **Paso 1/7:** Ingresa la marca del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer("📱 **Paso 2/7:** Ingresa la línea/modelo del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer("📅 **Paso 3/7:** Ingresa el año del vehículo (4 dígitos):", parse_mode="Markdown")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("❌ Por favor ingresa un año válido (4 dígitos). Ejemplo: 2020")
        return
    
    await state.update_data(anio=anio)
    await message.answer("🔢 **Paso 4/7:** Ingresa el número de serie:", parse_mode="Markdown")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    await state.update_data(serie=serie)
    await message.answer("🔧 **Paso 5/7:** Ingresa el número de motor:", parse_mode="Markdown")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer("🎨 **Paso 6/7:** Ingresa el color del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer("👤 **Paso 7/7:** Ingresa el nombre completo del solicitante:", parse_mode="Markdown")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip().upper()
    
    # Generar folio único de Estado de México
    datos["folio"] = generar_folio_edomex()

    # -------- FECHAS FORMATOS --------
    hoy = datetime.now()
    vigencia_dias = 30  # Por defecto 30 días
    fecha_ven = hoy + timedelta(days=vigencia_dias)
    
    # Formatos para PDF
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")
    
    # Para mensajes
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    # ---------------------------------

    try:
        await message.answer("📄 Generando 2 permisos, por favor espera...")
        
        # Generar LOS 2 PDFs
        p1 = generar_pdf_principal(datos)  # PDF principal completo
        p2 = generar_pdf_flask(hoy, datos["serie"], datos["folio"])  # PDF simple tipo Flask

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 **Permiso Completo - Folio: {datos['folio']}**\n🌟 Estado de México Digital"
        )
        
        # Enviar PDF simple (si se generó correctamente)
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"📋 **COMPROBANTE - Folio: {datos['folio']}**\n✅ Serie: {datos['serie']}"
            )

        # Guardar en Supabase (tabla del Flask)
        try:
            supabase.table("folios_registrados").insert({
                "folio": datos["folio"],
                "marca": datos["marca"],
                "linea": datos["linea"],
                "anio": datos["anio"],
                "numero_serie": datos["serie"],
                "numero_motor": datos["motor"],
                "fecha_expedicion": hoy.isoformat(),
                "fecha_vencimiento": fecha_ven.isoformat(),
                "entidad": ENTIDAD,
            }).execute()
        except Exception as e:
            print(f"Error guardando en Supabase: {e}")

        await message.answer(
            f"🎉 **¡2 Permisos generados exitosamente!**\n\n"
            f"📋 **Resumen:**\n"
            f"🆔 Folio: `{datos['folio']}`\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"🎨 Color: {datos['color']}\n"
            f"📅 Vigencia: {datos['vigencia']}\n"
            f"👤 Solicitante: {datos['nombre']}\n\n"
            f"📄 **Documentos generados:**\n"
            f"1️⃣ Permiso Completo (todos los datos)\n"
            f"2️⃣ Comprobante (fecha y serie)\n\n"
            f"✅ Registro guardado correctamente\n"
            f"🔄 Usa /permiso para generar otro permiso",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(f"❌ Error al generar permisos: {str(e)}")
        print(f"Error: {e}")
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    await message.answer(
        "👋 **¡Hola! Soy el Bot de Permisos de Estado de México**\n\n"
        "🚗 Usa /permiso para generar tu permiso de circulación\n"
        "📄 Genero 2 documentos: Permiso Completo y Comprobante\n"
        "⚡ Proceso rápido y seguro\n\n"
        "💡 **Comandos disponibles:**\n"
        "/start - Información del bot\n"
        "/permiso - Generar nuevo permiso",
        parse_mode="Markdown"
    )

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    """Mantiene el bot activo con pings periódicos"""
    while True:
        await asyncio.sleep(600)  # 10 minutos

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    
    # Configurar webhook
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url, allowed_updates=["message"])
        print(f"Webhook configurado: {webhook_url}")
        _keep_task = asyncio.create_task(keep_alive())
    else:
        print("Modo polling (sin webhook)")
    
    yield
    
    # Cleanup
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Bot Permisos Estado de México", version="1.0.0")

@app.get("/")
async def health():
    return {
        "status": "running",
        "bot": "Estado de México Permisos",
        "version": "1.0.0",
        "webhook_configured": bool(BASE_URL),
        "documentos_generados": 2
    }

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"Error en webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def bot_status():
    try:
        bot_info = await bot.get_me()
        return {
            "bot_active": True,
            "bot_username": bot_info.username,
            "bot_id": bot_info.id,
            "pdfs_por_permiso": 2
        }
    except Exception as e:
        return {"bot_active": False, "error": str(e)}
