from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
import asyncio
import random
from PIL import Image
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "edomex_plantilla_alta_res.pdf"
PLANTILLA_FLASK = "labuena3.0.pdf"
ENTIDAD = "edomex"

PRECIO_PERMISO = 180

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - 36 HORAS ------------
timers_activos = {}
user_folios = {}
pending_comprobantes = {}

TOTAL_MINUTOS_TIMER = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - EDOMEX\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"Para iniciar un nuevo trámite use /chuleta"
            )
        
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - EDOMEX\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")
        
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
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ---------------- COORDENADAS EDOMEX ----------------
coords_edomex = {
    "folio": (535,135,14,(1,0,0)),
    "marca": (109,190,9,(0,0,0)),
    "serie": (230,233,9,(0,0,0)),
    "linea": (238,190,9,(0,0,0)),
    "motor": (104,233,9,(0,0,0)),
    "anio":  (410,190,9,(0,0,0)),
    "color": (400,233,9,(0,0,0)),
    "fecha_exp": (190,280,9,(0,0,0)),
    "fecha_ven": (380,280,9,(0,0,0)),
    "nombre": (394,320,9,(0,0,0)),
}

# ------------ FUNCIÓN GENERAR FOLIO EDOMEX CON PREFIJO 331 ------------
def generar_folio_edomex():
    """Genera folio inteligente para Estado de México con prefijo 331"""
    prefijo = "331"
    max_intentos = 100000
    
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", ENTIDAD) \
            .like("folio", f"{prefijo}%") \
            .execute()
        
        folios_existentes = set()
        if response.data:
            folios_existentes = {item["folio"] for item in response.data if item["folio"]}
        
        numeros_usados = []
        for folio in folios_existentes:
            if folio.startswith(prefijo) and len(folio) > len(prefijo):
                try:
                    numero = int(folio[len(prefijo):])
                    numeros_usados.append(numero)
                except ValueError:
                    continue
        
        if not numeros_usados:
            siguiente_numero = 2
        else:
            siguiente_numero = max(numeros_usados) + 1
        
        for intento in range(max_intentos):
            folio_candidato = f"{prefijo}{siguiente_numero}"
            
            if folio_candidato not in folios_existentes:
                verificacion = supabase.table("folios_registrados") \
                    .select("folio") \
                    .eq("folio", folio_candidato) \
                    .execute()
                
                if not verificacion.data:
                    print(f"[FOLIO EDOMEX] Generado exitosamente: {folio_candidato}")
                    return folio_candidato
                else:
                    folios_existentes.add(folio_candidato)
            
            siguiente_numero += 1
            print(f"[FOLIO EDOMEX] Folio {folio_candidato} ocupado, probando siguiente...")
        
        numero_aleatorio = random.randint(10000, 99999)
        folio_fallback = f"{prefijo}{numero_aleatorio}"
        
        print(f"[FOLIO EDOMEX] Usando fallback aleatorio: {folio_fallback}")
        return folio_fallback
        
    except Exception as e:
        print(f"[ERROR] Al generar folio EDOMEX: {e}")
        numero_emergencia = random.randint(50000, 99999)
        folio_emergencia = f"{prefijo}{numero_emergencia}"
        print(f"[FOLIO EDOMEX] Fallback de emergencia: {folio_emergencia}")
        return folio_emergencia

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

URL_CONSULTA_BASE = "https://sfpyaedomexicoconsultapermisodigital.onrender.com"

def generar_qr_dinamico_edomex(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_directa)
        qr.make(fit=True)

        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR EDOMEX] Generado para folio {folio} -> {url_directa}")
        return img_qr, url_directa
        
    except Exception as e:
        print(f"[ERROR QR EDOMEX] {e}")
        return None, None

# ------------ GENERACIÓN PDF UNIFICADO (2 PÁGINAS EN 1 ARCHIVO) ------------
def generar_pdf_unificado(datos: dict) -> str:
    """Genera UN SOLO PDF con ambas plantillas (2 páginas)"""
    fol = datos["folio"]
    fecha_exp_dt = datos["fecha_exp"]
    fecha_ven_str = datos["fecha_ven"]
    fecha_exp_str = datos["fecha_exp_str"]
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_completo.pdf")
    
    try:
        # ===== PÁGINA 1: PLANTILLA PRINCIPAL (edomex_plantilla_alta_res.pdf) =====
        doc1 = fitz.open(PLANTILLA_PDF)
        pg1 = doc1[0]

        pg1.insert_text(coords_edomex["folio"][:2], fol,
                       fontsize=coords_edomex["folio"][2],
                       color=coords_edomex["folio"][3])
        
        pg1.insert_text(coords_edomex["fecha_exp"][:2], fecha_exp_str,
                       fontsize=coords_edomex["fecha_exp"][2],
                       color=coords_edomex["fecha_exp"][3])
        pg1.insert_text(coords_edomex["fecha_ven"][:2], fecha_ven_str,
                       fontsize=coords_edomex["fecha_ven"][2],
                       color=coords_edomex["fecha_ven"][3])

        for campo in ["marca", "serie", "linea", "motor", "anio", "color"]:
            if campo in coords_edomex and campo in datos:
                x, y, s, col = coords_edomex[campo]
                pg1.insert_text((x, y), str(datos.get(campo, "")), fontsize=s, color=col)

        pg1.insert_text(coords_edomex["nombre"][:2], datos.get("nombre", ""),
                       fontsize=coords_edomex["nombre"][2],
                       color=coords_edomex["nombre"][3])

        # QR dinámico
        img_qr, url_qr = generar_qr_dinamico_edomex(fol)
        
        if img_qr:
            from io import BytesIO
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())

            x_qr = 493
            y_qr = 35
            ancho_qr = 82
            alto_qr = 82

            pg1.insert_image(
                fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
                pixmap=qr_pix,
                overlay=True
            )
            print(f"[QR EDOMEX] Insertado en página 1")

        # ===== PÁGINA 2: PLANTILLA SIMPLE (labuena3.0.pdf) =====
        doc2 = fitz.open(PLANTILLA_FLASK)
        pg2 = doc2[0]
        
        pg2.insert_text((80,142), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0,0,0))
        pg2.insert_text((218,142), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=15, fontname="helv", color=(0,0,0))
        pg2.insert_text((182,283), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=9, fontname="helv", color=(0,0,0))
        pg2.insert_text((130,435), fecha_exp_dt.strftime("%d/%m/%Y"), fontsize=20, fontname="helv", color=(0,0,0))
        pg2.insert_text((162,185), datos["serie"], fontsize=9, fontname="helv", color=(0,0,0))

        # ===== UNIR AMBAS PÁGINAS =====
        doc_final = fitz.open()
        doc_final.insert_pdf(doc1)
        doc_final.insert_pdf(doc2)
        
        doc_final.save(out)
        
        doc_final.close()
        doc1.close()
        doc2.close()
        
        print(f"[PDF UNIFICADO EDOMEX] ✅ Generado: {out} (2 páginas)")
        
    except Exception as e:
        print(f"[ERROR] Generando PDF unificado EDOMEX: {e}")
        doc_fallback = fitz.open()
        page = doc_fallback.new_page()
        page.insert_text((50, 50), f"ERROR - Folio: {fol}", fontsize=12)
        doc_fallback.save(out)
        doc_fallback.close()
    
    return out

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE MÉXICO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer de 36 horas)"

    await message.answer(
        f"🚗 NUEVO PERMISO - EDOMEX\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        f"⏰ Plazo de pago: 36 horas"
        f"{mensaje_folios}\n\n"
        f"Primer paso: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    if not marca or len(marca) < 2:
        await message.answer("⚠️ Proporcione una MARCA válida (mínimo 2 caracteres):")
        return
    await state.update_data(marca=marca)
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    if not linea:
        await message.answer("⚠️ Proporcione la LÍNEA/MODELO:")
        return
    await state.update_data(linea=linea)
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Formato inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5 or len(serie) > 25:
        await message.answer("⚠️ Serie inválida (5 a 25 caracteres):")
        return
    await state.update_data(serie=serie)
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    if len(motor) < 5 or len(motor) > 25:
        await message.answer("⚠️ Motor inválido (5 a 25 caracteres):")
        return
    await state.update_data(motor=motor)
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    if not color or len(color) > 20:
        await message.answer("⚠️ Color inválido (máx. 20 caracteres):")
        return
    await state.update_data(color=color)
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()

    if len(nombre) < 5 or len(nombre) > 60 or len(nombre.split()) < 2:
        await message.answer("⚠️ Nombre completo inválido (mínimo nombre y apellido, máx. 60 caracteres):")
        return

    datos["nombre"] = nombre
    datos["folio"] = generar_folio_edomex()

    hoy = datetime.now()
    vigencia_dias = 30
    fecha_ven = hoy + timedelta(days=vigencia_dias)
    
    datos["fecha_exp"] = hoy
    datos["fecha_exp_str"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")

    try:
        await message.answer(
            f"🔄 Generando documentación...\n"
            f"<b>Folio:</b> {datos['folio']}\n"
            f"<b>Titular:</b> {nombre}",
            parse_mode="HTML"
        )

        # Generar PDF UNIFICADO (2 páginas en 1 archivo)
        pdf_unificado = generar_pdf_unificado(datos)

        await message.answer_document(
            FSInputFile(pdf_unificado),
            caption=f"📋 PERMISO DE CIRCULACIÓN - EDOMEX (COMPLETO)\nFolio: {datos['folio']}\nVigencia: 30 días\n\n✅ Documento con 2 páginas unificadas"
        )

        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "nombre": datos["nombre"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": ENTIDAD,
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "entidad": "EDOMEX",
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "color": datos["color"],
            "fecha_expedicion": hoy.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "contribuyente": datos["nombre"],
            "estado": "PENDIENTE",
            "user_id": message.from_user.id
        }).execute()

        await iniciar_timer_eliminacion(message.from_user.id, datos['folio'])

        await message.answer(
            "💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Monto: ${PRECIO_PERMISO}\n"
            "⏰ Tiempo límite: 36 horas\n\n"
            "🏦 TRANSFERENCIA:\n"
            "• Banco: AZTECA\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cuenta: 127180013037579543\n"
            f"• Concepto: Permiso {datos['folio']}\n\n"
            "🏪 OXXO:\n"
            "• Referencia: 2242170180385581\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Monto: ${PRECIO_PERMISO}\n\n"
            "📸 Envía la foto del comprobante para validar.\n"
            "⚠️ Si no pagas en 36 horas, el folio se elimina automáticamente.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ Error generando documentación: {str(e)}")
        print(f"Error: {e}")
    finally:
        await state.clear()

@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) > 4:
        folio_admin = texto[4:]
        
        if not folio_admin.startswith("331"):
            await message.answer(
                f"❌ FOLIO INVÁLIDO\n"
                f"El folio {folio_admin} no es EDOMEX.\n"
                f"Debe comenzar con 331"
            )
            return
        
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            cancelar_timer_folio(folio_admin)
            
            try:
                supabase.table("folios_registrados").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
                supabase.table("borradores_registros").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
            except Exception as e:
                print(f"Error actualizando BD para folio {folio_admin}: {e}")
            
            await message.answer(
                f"✅ VALIDACIÓN ADMINISTRATIVA OK\n"
                f"Folio: {folio_admin}\n"
                f"Timer cancelado y estado actualizado."
            )
            
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - EDOMEX\n"
                    f"Folio: {folio_admin}\n"
                    f"Tu permiso está activo para circular."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
                f"Folio consultado: {folio_admin}"
            )
    else:
        await message.answer(
            "⚠️ Formato: SERO[número_de_folio]\n"
            "Ejemplo: SERO3312"
        )

@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes de pago.\n"
                "Para iniciar uno nuevo usa /chuleta"
            )
            return
        
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista_folios}\n\n"
                f"Responde con el NÚMERO DE FOLIO al que corresponde este comprobante."
            )
            return
        
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer("❌ Error procesando el comprobante. Intenta enviar la foto nuevamente.")

@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario = obtener_folios_usuario(user_id)
        
        if folio_especificado not in folios_usuario:
            await message.answer(
                "❌ Ese folio no está entre tus expedientes activos.\n"
                "Responde con uno de tu lista actual."
            )
            return
        
        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[user_id]
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            await message.answer(
                f"✅ Comprobante asociado.\n"
                f"📄 Folio: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ Folio confirmado: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("❌ Error procesando el folio especificado. Intenta de nuevo.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
                "No tienes folios pendientes de pago.\n"
                "Para nuevo permiso use /chuleta"
            )
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, tiempo_restante)
                horas = tiempo_restante // 60
                minutos = tiempo_restante % 60
                lista_folios.append(f"• {folio} ({horas}h {minutos}min restantes)")
            else:
                lista_folios.append(f"• {folio} (sin timer)")
        
        await message.answer(
            f"📋 FOLIOS EDOMEX ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
            f"📸 Para enviar comprobante, use imagen."
        )
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("❌ Error consultando expedientes activos.")

@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "Para iniciar su trámite use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital EDOMEX.")

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
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url, allowed_updates=["message"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Modo sin webhook")
        print("[SISTEMA] ¡Sistema Digital EDOMEX iniciado correctamente!")
        yield
    except Exception as e:
        print(f"[ERROR CRÍTICO] Iniciando sistema: {e}")
        yield
    finally:
        print("[CIERRE] Cerrando sistema...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema EDOMEX Digital", version="4.0")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    return {
        "ok": True,
        "bot": "EDOMEX Permisos Sistema",
        "status": "running",
        "version": "4.0 - PDF Unificado + Timer 36h + SERO + /chuleta",
        "entidad": "EDOMEX",
        "vigencia": "30 días",
        "timer_eliminacion": "36 horas",
        "active_timers": len(timers_activos),
        "prefijo_folio": "331",
        "comando_secreto": "/chuleta (invisible)",
        "caracteristicas": [
            "PDF unificado (2 páginas en 1 archivo)",
            "Folios con prefijo 331 consecutivos",
            "Timer 36 horas con avisos 90/60/30/10",
            "Reintentos automáticos ante duplicados (100000 intentos)",
            "Comando admin: SERO[folio]",
            "Timers independientes por folio"
        ]
    }

@app.get("/status")
async def status_detail():
    return {
        "sistema": "EDOMEX Digital v4.0 - PDF Unificado",
        "entidad": "EDOMEX",
        "vigencia_dias": 30,
        "tiempo_eliminacion": "36 horas con avisos 90/60/30/10",
        "total_timers_activos": len(timers_activos),
        "folios_con_timer": list(timers_activos.keys()),
        "usuarios_con_folios": len(user_folios),
        "prefijo_folio": "331",
        "pdf_output": "UN archivo con 2 páginas (principal + simple)",
        "continuidad": "Folios desde último en DB; +1 con lock y reintentos",
        "comando_secreto": "/chuleta (invisible)",
        "timestamp": datetime.now().isoformat(),
        "status": "Operacional"
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] EDOMEX v4.0 - PDF Unificado + Timer 36h + SERO")
        print(f"[COMANDO SECRETO] /chuleta")
        print(f"[PREFIJO] 331")
        print(f"[PDF OUTPUT] 1 archivo unificado con 2 páginas")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
