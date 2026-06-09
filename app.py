from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
import asyncio
import aiohttp
import random
from PIL import Image
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL     = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR   = "documentos"
PLANTILLA_PDF   = "edomex_plantilla_alta_res.pdf"
PLANTILLA_FLASK = "labuena3.0.pdf"
ENTIDAD = "edomex"

PRECIO_PERMISO = 180

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT con timeout 300s — evita HTTP timeout error ------------
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=300))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT ------------
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

# ------------ FOLIO CONFIG ------------
FOLIO_PREFIJO      = "331"
folio_counter      = {"siguiente": 2}
MAX_INTENTOS_FOLIO = 10_000_000
_folio_lock        = asyncio.Lock()

# ── WATERMARK ─────────────────────────────────────────────────────────────────

def _sb_leer_watermark() -> int | None:
    try:
        r = supabase.table("folio_watermark").select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO).execute()
        if r.data:
            return r.data[0]["ultimo_asignado"]
        return None
    except Exception as e:
        print(f"[ERROR] leer_watermark EDOMEX: {e}")
        return None

def _sb_guardar_watermark(numero: int):
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo":         FOLIO_PREFIJO,
            "ultimo_asignado": numero
        }).execute()
        print(f"[WATERMARK] Guardado: {FOLIO_PREFIJO}{numero}")
    except Exception as e:
        print(f"[ERROR] guardar_watermark EDOMEX: {e}")

def _sb_inicializar_folio():
    try:
        watermark = _sb_leer_watermark()
        if watermark is not None:
            folio_counter["siguiente"] = watermark + 1
            print(f"[INFO] Folio EDOMEX desde watermark: {FOLIO_PREFIJO}{watermark} -> siguiente: {folio_counter['siguiente']}")
            return
        r = supabase.table("folios_registrados").select("folio").like("folio", f"{FOLIO_PREFIJO}%").execute()
        consecutivos = []
        for row in r.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_PREFIJO):
                sufijo = f[len(FOLIO_PREFIJO):]
                if sufijo.isdigit():
                    consecutivos.append(int(sufijo))
        if consecutivos:
            maximo = max(consecutivos)
            folio_counter["siguiente"] = maximo + 1
            _sb_guardar_watermark(maximo)
            print(f"[INFO] Folio EDOMEX desde DB (primera vez): {FOLIO_PREFIJO}{maximo} -> siguiente: {folio_counter['siguiente']}")
        else:
            folio_counter["siguiente"] = 2
            print("[INFO] Sin folios 331 previos, empezando desde 3312")
    except Exception as e:
        print(f"[ERROR] inicializar_folio EDOMEX: {e}")
        folio_counter["siguiente"] = 2

def _sb_folio_existe(folio: str) -> bool:
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] Verificando folio {folio}: {e}")
        return False

def _generar_folio_edomex_sync() -> str:
    candidato = folio_counter["siguiente"]
    for _ in range(MAX_INTENTOS_FOLIO):
        folio = f"{FOLIO_PREFIJO}{candidato}"
        if not _sb_folio_existe(folio):
            folio_counter["siguiente"] = candidato + 1
            _sb_guardar_watermark(candidato)
            print(f"[FOLIO EDOMEX] Asignado: {folio}  (siguiente: {folio_counter['siguiente']})")
            return folio
        print(f"[FOLIO EDOMEX] {folio} ocupado -> probando siguiente")
        candidato += 1
    numero_fallback = random.randint(10000, 99999)
    folio_fallback  = f"{FOLIO_PREFIJO}{numero_fallback}"
    print(f"[FOLIO EDOMEX] Fallback: {folio_fallback}")
    return folio_fallback

async def generar_folio_edomex() -> str:
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_edomex_sync)

# ── TIMERS ────────────────────────────────────────────────────────────────────

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").delete().eq("folio", folio).execute(),
            supabase.table("borradores_registros").delete().eq("folio", folio).execute(),
        ))
        if user_id:
            await bot.send_message(user_id,
                f"TIEMPO AGOTADO - EDOMEX\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"Para generar otro permiso use /banamex")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos: return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(user_id,
            f"RECORDATORIO DE PAGO - EDOMEX\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Envie su comprobante de pago (imagen) para validar el tramite.\n\n"
            f"Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str, nombre: str = ""):
    async def timer_task():
        print(f"[TIMER] Iniciado folio {folio}, usuario {user_id} (36h)")
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task":       task,
        "user_id":    user_id,
        "start_time": datetime.now(),
        "nombre":     nombre,
    }
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio} ({nombre}), total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]: del user_folios[user_id]
        print(f"[SISTEMA] Timer cancelado folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]: del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ------------ COORDENADAS EDOMEX ------------
coords_edomex = {
    "folio":     (535, 135, 14, (1, 0, 0)),
    "marca":     (109, 190,  9, (0, 0, 0)),
    "serie":     (230, 233,  9, (0, 0, 0)),
    "linea":     (238, 190,  9, (0, 0, 0)),
    "motor":     (104, 233,  9, (0, 0, 0)),
    "anio":      (410, 190,  9, (0, 0, 0)),
    "color":     (400, 233,  9, (0, 0, 0)),
    "fecha_exp": (190, 280,  9, (0, 0, 0)),
    "fecha_ven": (380, 280,  9, (0, 0, 0)),
    "nombre":    (394, 320,  9, (0, 0, 0)),
}

# ------------ FSM ------------
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

URL_CONSULTA_BASE = "https://sfpyaedomexicoconsultapermisodigital.onrender.com"

def generar_qr_dinamico_edomex(folio):
    try:
        url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR EDOMEX] Generado folio {folio} -> {url}")
        return img_qr, url
    except Exception as e:
        print(f"[ERROR QR EDOMEX] {e}")
        return None, None

def generar_pdf_unificado(datos: dict) -> str:
    fol           = datos["folio"]
    fecha_exp_dt  = datos["fecha_exp"]
    fecha_ven_str = datos["fecha_ven"]
    fecha_exp_str = datos["fecha_exp_str"]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_completo.pdf")

    try:
        doc1 = fitz.open(PLANTILLA_PDF)
        pg1  = doc1[0]

        pg1.insert_text(coords_edomex["folio"][:2], fol,
                        fontsize=coords_edomex["folio"][2], color=coords_edomex["folio"][3])
        pg1.insert_text(coords_edomex["fecha_exp"][:2], fecha_exp_str,
                        fontsize=coords_edomex["fecha_exp"][2], color=coords_edomex["fecha_exp"][3])
        pg1.insert_text(coords_edomex["fecha_ven"][:2], fecha_ven_str,
                        fontsize=coords_edomex["fecha_ven"][2], color=coords_edomex["fecha_ven"][3])

        for campo in ["marca", "serie", "linea", "motor", "anio", "color"]:
            if campo in coords_edomex and campo in datos:
                x, y, s, col = coords_edomex[campo]
                pg1.insert_text((x, y), str(datos.get(campo, "")), fontsize=s, color=col)

        pg1.insert_text(coords_edomex["nombre"][:2], datos.get("nombre", ""),
                        fontsize=coords_edomex["nombre"][2], color=coords_edomex["nombre"][3])

        img_qr, _ = generar_qr_dinamico_edomex(fol)
        if img_qr:
            from io import BytesIO
            buf = BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            pg1.insert_image(fitz.Rect(493, 35, 493+82, 35+82),
                             pixmap=qr_pix, overlay=True)
            print(f"[QR EDOMEX] Insertado en pagina 1")

        doc2 = fitz.open(PLANTILLA_FLASK)
        pg2  = doc2[0]
        pg2.insert_text((80,  142), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0,0,0))
        pg2.insert_text((218, 142), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0,0,0))
        pg2.insert_text((182, 283), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=9,  fontname="helv", color=(0,0,0))
        pg2.insert_text((130, 435), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=20, fontname="helv", color=(0,0,0))
        pg2.insert_text((162, 185), datos["serie"],                    fontsize=9,  fontname="helv", color=(0,0,0))

        doc_final = fitz.open()
        doc_final.insert_pdf(doc1)
        doc_final.insert_pdf(doc2)
        doc_final.save(out)
        doc_final.close(); doc1.close(); doc2.close()
        print(f"[PDF UNIFICADO EDOMEX] Generado: {out}")

    except Exception as e:
        print(f"[ERROR] Generando PDF EDOMEX: {e}")
        doc_fallback = fitz.open()
        doc_fallback.new_page().insert_text((50, 50), f"ERROR - Folio: {fol}", fontsize=12)
        doc_fallback.save(out); doc_fallback.close()

    return out

# ── BACKGROUND TASK ───────────────────────────────────────────────────────────

async def _generar_y_enviar_background(chat_id: int, datos: dict, user_id: int):
    """
    PDF, insert y envío en background.
    El webhook ya respondió — Telegram no manda duplicados.
    """
    nombre    = datos["nombre"]
    hoy       = datos["fecha_exp"]
    fecha_ven = hoy + timedelta(days=30)
    folio_final = datos["folio"]

    try:
        pdf_path = await asyncio.to_thread(generar_pdf_unificado, datos)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Validar Admin",  callback_data=f"validar_{folio_final}"),
            InlineKeyboardButton(text="Detener Timer",  callback_data=f"detener_{folio_final}")
        ]])

        await bot.send_document(
            chat_id,
            FSInputFile(pdf_path),
            caption=(
                f"PERMISO DE CIRCULACION - EDOMEX\n"
                f"Folio: {folio_final}\n"
                f"Titular: {nombre}\n"
                f"Vigencia: 30 dias\n\n"
                f"Documento con 2 paginas\n"
                f"TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        def _insert(folio_usar: str):
            supabase.table("folios_registrados").insert({
                "folio":             folio_usar,
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "anio":              datos["anio"],
                "numero_serie":      datos["serie"],
                "numero_motor":      datos["motor"],
                "color":             datos["color"],
                "nombre":            nombre,
                "fecha_expedicion":  hoy.date().isoformat(),
                "fecha_vencimiento": fecha_ven.date().isoformat(),
                "entidad":           ENTIDAD,
                "estado":            "PENDIENTE",
                "user_id":           user_id,
                "username":          datos.get("username", "Sin username")
            }).execute()
            supabase.table("borradores_registros").insert({
                "folio":             folio_usar,
                "entidad":           "EDOMEX",
                "numero_serie":      datos["serie"],
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "numero_motor":      datos["motor"],
                "anio":              datos["anio"],
                "color":             datos["color"],
                "fecha_expedicion":  hoy.isoformat(),
                "fecha_vencimiento": fecha_ven.isoformat(),
                "contribuyente":     nombre,
                "estado":            "PENDIENTE",
                "user_id":           user_id
            }).execute()

        for _ in range(20):
            try:
                await asyncio.to_thread(_insert, folio_final)
                print(f"[DB] Insertado folio {folio_final}")
                break
            except Exception as e:
                em = str(e).lower()
                if any(k in em for k in ("duplicate", "unique", "23505")):
                    print(f"[DB] Folio {folio_final} duplicado — obteniendo nuevo...")
                    folio_final = await generar_folio_edomex()
                else:
                    print(f"[DB ERROR] {e}"); break

        await iniciar_timer_eliminacion(user_id, folio_final, nombre)

        await bot.send_message(user_id,
            f"INSTRUCCIONES DE PAGO\n\n"
            f"Folio: {folio_final}\n"
            f"Monto: ${PRECIO_PERMISO}\n"
            f"Tiempo limite: 36 horas\n\n"
            f"TRANSFERENCIA:\n"
            f"Banco: AZTECA\n"
            f"Titular: LIZBETH LAZCANO MOSCO\n"
            f"Cuenta: 127180013037579543\n"
            f"Concepto: Permiso {folio_final}\n\n"
            f"OXXO:\n"
            f"Referencia: 2242170180385581\n"
            f"Titular: LIZBETH LAZCANO MOSCO\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Envia la foto del comprobante para validar.\n"
            f"Si no pagas en 36 horas el folio se elimina automaticamente.\n\n"
            f"Para generar otro permiso use /banamex")

    except Exception as e:
        print(f"[ERROR] background folio {folio_final}: {e}")
        try:
            await bot.send_message(user_id,
                f"Error generando documentacion: {e}\n\nUse /banamex para reintentar.")
        except Exception:
            pass

# ------------ HANDLERS ------------

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "SISTEMA DIGITAL DEL ESTADO DE MEXICO\n\n"
        f"Costo: ${PRECIO_PERMISO}\n"
        "Tiempo limite: 36 horas\n\n"
        "Su folio sera eliminado automaticamente si no realiza el pago dentro del tiempo limite"
    )

@dp.message(Command("banamex"))
async def banamex_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    mis_folios = [f for f in timers_activos
                  if timers_activos[f].get("user_id") == message.from_user.id]

    if mis_folios:
        texto   = "FOLIOS ACTIVOS CON TIMER\n" + "─" * 28 + "\n\n"
        botones = []
        for f in mis_folios:
            info   = timers_activos[f]
            nombre = info.get("nombre", "Sin nombre")
            mins   = max(0, 2160 - int((datetime.now() - info["start_time"]).total_seconds() / 60))
            texto += f"Folio: {f}\n{nombre}\n{mins//60}h {mins%60}min restantes\n\n"
            botones.append([InlineKeyboardButton(
                text=f"Detener timer {f}", callback_data=f"detener_{f}")])
        await message.answer(texto.strip(),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
        await message.answer(
            f"Para NUEVO permiso escribe la MARCA del vehiculo:\n\nCosto: ${PRECIO_PERMISO} | Plazo: 36h")
    else:
        await message.answer(
            f"NUEVO PERMISO - EDOMEX\n\n"
            f"Costo: ${PRECIO_PERMISO}\n"
            f"Plazo de pago: 36 horas\n\n"
            f"Primer paso: MARCA del vehiculo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LINEA/MODELO del vehiculo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("ANO del vehiculo (4 digitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("Formato invalido. Use 4 digitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NUMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NUMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR del vehiculo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos           = await state.get_data()
    nombre          = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["username"] = message.from_user.username or "Sin username"

    datos["folio"] = await generar_folio_edomex()

    hoy       = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

    datos["fecha_exp"]     = hoy
    datos["fecha_exp_str"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"]     = fecha_ven.strftime("%d/%m/%Y")

    # state.clear() ANTES del create_task — evita re-triggers
    await state.clear()

    await message.answer(
        f"Generando documentacion...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}"
    )

    # Webhook regresa inmediatamente — PDF en background
    asyncio.create_task(
        _generar_y_enviar_background(message.chat.id, datos, message.from_user.id)
    )

# ------------ CALLBACKS ------------

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("331"):
        await callback.answer("Folio invalido", show_alert=True); return
    if folio in timers_activos:
        uid    = timers_activos[folio]["user_id"]
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"Error BD validar {folio}: {e}")
        await callback.answer("Folio validado por administracion", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"PAGO VALIDADO POR ADMINISTRACION - EDOMEX\n"
                f"Folio: {folio}\nTitular: {nombre}\n"
                f"Tu permiso esta activo para circular.\n\n"
                f"Para generar otro permiso use /banamex")
        except Exception as e:
            print(f"Error notificando usuario {uid}: {e}")
    else:
        await callback.answer("Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update(
                {"estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()}
            ).eq("folio", folio).execute())
        except Exception as e:
            print(f"Error BD detener {folio}: {e}")
        await callback.answer("Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"TIMER DETENIDO\nFolio: {folio}\nTitular: {nombre}\n\n"
            f"El folio ya NO se eliminara automaticamente.\n\n"
            f"Para generar otro permiso use /banamex")
    else:
        await callback.answer("Timer ya no esta activo", show_alert=True)

@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) <= 4:
        await message.answer(
            "Formato: SERO[folio]  Ejemplo: SERO3312\n\nPara generar otro permiso use /banamex"); return
    folio_admin = texto[4:]
    if not folio_admin.startswith("331"):
        await message.answer(
            f"FOLIO INVALIDO\nEl folio {folio_admin} no es EDOMEX.\n"
            f"Debe comenzar con 331\n\nPara generar otro permiso use /banamex"); return
    if folio_admin in timers_activos:
        uid    = timers_activos[folio_admin]["user_id"]
        nombre = timers_activos[folio_admin].get("nombre", "")
        cancelar_timer_folio(folio_admin)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio_admin).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio_admin).execute()
            ))
        except Exception as e:
            print(f"Error BD SERO {folio_admin}: {e}")
        await message.answer(
            f"VALIDACION ADMINISTRATIVA OK\nFolio: {folio_admin}\nTitular: {nombre}\n"
            f"Timer cancelado.\n\nPara generar otro permiso use /banamex")
        try:
            await bot.send_message(uid,
                f"PAGO VALIDADO POR ADMINISTRACION - EDOMEX\n"
                f"Folio: {folio_admin}\nTu permiso esta activo.\n\n"
                f"Para generar otro permiso use /banamex")
        except Exception as e:
            print(f"Error notificando usuario {uid}: {e}")
    else:
        await message.answer(
            f"FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\nFolio: {folio_admin}\n\n"
            f"Para generar otro permiso use /banamex")

@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        uid    = message.from_user.id
        folios = obtener_folios_usuario(uid)
        if not folios:
            await message.answer(
                "No hay tramites pendientes de pago.\n\n"
                "Para generar otro permiso use /banamex"); return
        if len(folios) > 1:
            lista = '\n'.join([f"- {f}" for f in folios])
            pending_comprobantes[uid] = "waiting_folio"
            await message.answer(
                f"Tienes varios folios activos:\n\n{lista}\n\n"
                f"Responde con el NUMERO DE FOLIO al que corresponde este comprobante.\n\n"
                f"Para generar otro permiso use /banamex"); return
        folio = folios[0]; cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
        await message.answer(
            f"Comprobante recibido.\nFolio: {folio}\nTimer detenido.\n\n"
            f"Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            f"Error procesando el comprobante.\n\nPara generar otro permiso use /banamex")

@dp.message(lambda message: message.from_user.id in pending_comprobantes
            and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        uid                = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario     = obtener_folios_usuario(uid)
        if folio_especificado not in folios_usuario:
            await message.answer(
                "Ese folio no esta entre tus expedientes activos.\n\n"
                "Para generar otro permiso use /banamex"); return
        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[uid]
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio_especificado).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio_especificado).execute()
            ))
        except Exception as e:
            print(f"Error actualizando estado: {e}")
        await message.answer(
            f"Comprobante asociado.\nFolio: {folio_especificado}\nTimer detenido.\n\n"
            f"Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if message.from_user.id in pending_comprobantes:
            del pending_comprobantes[message.from_user.id]
        await message.answer(
            f"Error procesando el folio.\n\nPara generar otro permiso use /banamex")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        uid    = message.from_user.id
        folios = obtener_folios_usuario(uid)
        if not folios:
            await message.answer(
                "NO HAY FOLIOS ACTIVOS\n\n"
                "Para generar otro permiso use /banamex"); return
        lista   = []
        botones = []
        for folio in folios:
            if folio in timers_activos:
                info   = timers_activos[folio]
                nombre = info.get("nombre", "Sin nombre")
                mins   = max(0, 2160 - int(
                    (datetime.now() - info["start_time"]).total_seconds() / 60))
                lista.append(f"- {folio} — {nombre}\n  {mins//60}h {mins%60}min restantes")
            else:
                lista.append(f"- {folio} (sin timer)")
            botones.append([InlineKeyboardButton(
                text=f"Detener timer {folio}", callback_data=f"detener_{folio}")])
        await message.answer(
            f"FOLIOS EDOMEX ACTIVOS ({len(folios)})\n\n" + '\n\n'.join(lista) +
            f"\n\nCada folio tiene timer de 36 horas.\n\n"
            f"Para generar otro permiso use /banamex",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer(f"Error consultando expedientes.\n\nPara generar otro permiso use /banamex")

@dp.message(lambda message: message.text and any(p in message.text.lower() for p in
    ['costo','precio','cuanto','cuánto','deposito','depósito','pago','valor','monto']))
async def responder_costo(message: types.Message):
    await message.answer(
        f"INFORMACION DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "Para generar otro permiso use /banamex")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("Sistema Digital EDOMEX.")

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema EDOMEX activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        await asyncio.to_thread(_sb_inicializar_folio)
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Modo sin webhook")
        print("[SISTEMA] Sistema Digital EDOMEX v5.3 iniciado!")
        yield
    except Exception as e:
        print(f"[ERROR CRITICO] {e}"); yield
    finally:
        print("[CIERRE] Cerrando sistema...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError): await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema EDOMEX Digital", version="5.3")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    return {
        "ok":             True,
        "sistema":        "EDOMEX v5.3",
        "vigencia":       "30 dias",
        "precio":         f"${PRECIO_PERMISO}",
        "timer":          "36 horas",
        "active_timers":  len(timers_activos),
        "siguiente_folio":f"{FOLIO_PREFIJO}{folio_counter['siguiente']}",
        "cambios_v5.3":   [
            "AiohttpSession timeout=300s — elimina HTTP timeout error",
            "PDF en background task — webhook responde inmediatamente",
            "/banamex en lugar de /chuleta",
            "state.clear() antes del create_task — sin duplicados",
        ]
    }

@app.get("/status")
async def status_detail():
    activos = {}
    for f, info in timers_activos.items():
        mins = max(0, 2160 - int((datetime.now() - info["start_time"]).total_seconds() / 60))
        activos[f] = {
            "nombre":    info.get("nombre", ""),
            "restantes": f"{mins//60}h {mins%60}min",
            "user_id":   info.get("user_id"),
        }
    return {
        "sistema":         "EDOMEX Digital v5.3",
        "timers_activos":  len(timers_activos),
        "folios":          activos,
        "siguiente_folio": f"{FOLIO_PREFIJO}{folio_counter['siguiente']}",
        "timestamp":       datetime.now().isoformat(),
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
