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
PLANTILLA_PDF = "edomex_plantilla_alta_res.pdf"  # PDF principal completo
PLANTILLA_FLASK = "labuena3.0.pdf"  # PDF simple tipo Flask
ENTIDAD = "edomex"

# Precio del permiso
PRECIO_PERMISO = 180

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT MEJORADO - TIMERS INDEPENDIENTES POR FOLIO ------------
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        # Obtener user_id del folio
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario si está disponible
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO\n\n"
                f"El folio {folio} ha sido eliminado del sistema por falta de pago.\n\n"
                f"Para tramitar un nuevo permiso utilize /permiso"
            )
        
        # Limpiar timers
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        if folio not in timers_activos:
            return  # Timer ya fue cancelado
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO EDOMEX\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO} MXN\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios para un folio específico"""
    async def timer_task():
        start_time = datetime.now()
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if folio not in timers_activos:
                print(f"[TIMER] Cancelado para folio {folio}")
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if folio in timers_activos:
            await enviar_recordatorio(folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio}")
            await eliminar_folio_automatico(folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    # Agregar folio a la lista del usuario
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        # Remover de estructuras de datos
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:  # Si no quedan folios, eliminar entrada
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}, timers restantes: {len(timers_activos)}")

def limpiar_timer_folio(folio: str):
    """Limpia todas las referencias de un folio tras expirar"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario"""
    return user_folios.get(user_id, [])

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

# ------------ FUNCIÓN GENERAR FOLIO EDOMEX CON PREFIJO 98100 ------------
def generar_folio_edomex():
    """Genera folio con prefijo 98 para Estado de México"""
    try:
        # Buscar el último folio que comience con 98100
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", ENTIDAD) \
            .like("folio", "98100%") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            if isinstance(ultimo_folio, str) and ultimo_folio.startswith("98100"):
                numero = int(ultimo_folio[2:])  # Quitar el prefijo 98100
                nuevo_numero = numero + 1
                return f"98100{nuevo_numero}"
        
        # Si no hay folios, empezar desde 98100
        return "98100"
        
    except Exception as e:
        print(f"[ERROR] Al generar folio EDOMEX: {e}")
        # Fallback seguro
        import random
        return f"98{random.randint(1000, 9999)}"

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# URL de consulta para QRs
URL_CONSULTA_BASE = "https://sfpyaedomexicoconsultapermisodigital.onrender.com"

def generar_qr_dinamico_edomex(folio):
    """Genera QR dinámico para Estado de México"""
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

# ------------ PDF PRINCIPAL EDOMEX (COMPLETO CON QR) ------------
def generar_pdf_principal(datos: dict) -> str:
    """Genera el PDF principal de Estado de México con todos los datos y QR dinámico"""
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

    # AGREGAR QR DINÁMICO
    img_qr, url_qr = generar_qr_dinamico_edomex(datos["folio"])
    
    if img_qr:
        from io import BytesIO
        buf = BytesIO()
        img_qr.save(buf, format="PNG")
        buf.seek(0)
        qr_pix = fitz.Pixmap(buf.read())

        # Coordenadas del QR para EDOMEX (ajustar según tu PDF)
        x_qr = 100  
        y_qr = 100
        ancho_qr = 82
        alto_qr = 82

        pg.insert_image(
            fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
            pixmap=qr_pix,
            overlay=True
        )
        print(f"[QR EDOMEX] Insertado en PDF: {url_qr}")

    doc.save(out)
    doc.close()
    
    return out

# ------------ HANDLERS EDOMEX CON FUNCIONES MEJORADAS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos EDOMEX\n"
        "Servicio oficial automatizado para trámites vehiculares\n\n"
        f"💰 Costo del permiso: ${PRECIO_PERMISO} MXN\n"
        "⏰ Tiempo límite para pago: 2 horas\n"
        "📸 Métodos de pago: Transferencia bancaria y OXXO\n\n"
        "📋 Use /permiso para iniciar su trámite\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Mostrar folios activos si los hay
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer independiente)"
    
    await message.answer(
        f"🚗 TRÁMITE DE PERMISO EDOMEX\n\n"
        f"📋 Costo: ${PRECIO_PERMISO} MXN\n"
        f"⏰ Tiempo para pagar: 2 horas\n"
        f"📱 Concepto de pago: Su folio asignado\n\n"
        f"Al continuar acepta que su folio será eliminado si no paga en el tiempo establecido."
        + mensaje_folios + "\n\n"
        f"**Paso 1/7:** Ingresa la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca}\n\n"
        "**Paso 2/7:** Ingresa la LÍNEA/MODELO del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea}\n\n"
        "**Paso 3/7:** Ingresa el AÑO del vehículo (4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ El año debe contener exactamente 4 dígitos.\n"
            "Ejemplo válido: 2020, 2015, 2023\n\n"
            "Por favor, ingrese nuevamente el año:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio}\n\n"
        "**Paso 4/7:** Ingresa el NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece incompleto.\n"
            "Verifique que haya ingresado todos los caracteres.\n\n"
            "Intente nuevamente:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie}\n\n"
        "**Paso 5/7:** Ingresa el NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor}\n\n"
        "**Paso 6/7:** Ingresa el COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color}\n\n"
        "**Paso 7/7:** Ingresa el NOMBRE COMPLETO del titular:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
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

    await message.answer(
        f"🔄 PROCESANDO PERMISO EDOMEX...\n\n"
        f"📄 Folio asignado: {datos['folio']}\n"
        f"👤 Titular: {nombre}\n\n"
        "Generando 2 documentos oficiales..."
    )

    try:
        # Generar LOS 2 PDFs
        p1 = generar_pdf_principal(datos)  # PDF principal completo con QR
        p2 = generar_pdf_flask(hoy, datos["serie"], datos["folio"])  # PDF simple tipo Flask

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 PERMISO COMPLETO EDOMEX\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento oficial con QR dinámico"
        )
        
        # Enviar PDF simple (si se generó correctamente)
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"📋 DOCUMENTO DE VERIFICACIÓN\n"
                       f"Serie: {datos['serie']}\n"
                       f"🔍 Comprobante adicional de autenticidad"
            )

        # Guardar en base de datos con estado PENDIENTE
        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": ENTIDAD,
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        # También en la tabla borradores (compatibilidad)
        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "entidad": "EDOMEX",
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "fecha_expedicion": hoy.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "contribuyente": datos["nombre"],
            "estado": "PENDIENTE",
            "user_id": message.from_user.id
        }).execute()

        # INICIAR TIMER DE PAGO CON SISTEMA MEJORADO
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        # Mensaje de instrucciones de pago
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Monto: ${PRECIO_PERMISO} MXN\n"
            f"⏰ Tiempo límite: 2 horas\n\n"
            
            "🏦 TRANSFERENCIA BANCARIA:\n"
            "• Banco: AZTECA\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cuenta: 127180013037579543\n"
            "• Concepto: Permiso " + datos['folio'] + "\n\n"
            
            "🏪 PAGO EN OXXO:\n"
            "• Referencia: 2242170180385581\n"
            "• TARJETA SPIN\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Cantidad exacta: ${PRECIO_PERMISO} MXN\n\n"
            
            f"📸 IMPORTANTE: Una vez realizado el pago, envíe la fotografía de su comprobante.\n\n"
            f"⚠️ ADVERTENCIA: Si no completa el pago en 2 horas, el folio {datos['folio']} será eliminado automáticamente del sistema."
        )
        
    except Exception as e:
        await message.answer(
            f"❌ ERROR EN EL SISTEMA\n\n"
            f"Se ha presentado un inconveniente técnico: {str(e)}\n\n"
            "Por favor, intente nuevamente con /permiso\n"
            "Si el problema persiste, contacte al soporte técnico."
        )
    finally:
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN MEJORADO PARA EDOMEX ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    
    # Verificar formato: SERO + número de folio
    if len(texto) > 4:
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
        # Validar que sea folio EDOMEX
        if not folio_admin.startswith("98"):
            await message.answer(
                f"⚠️ FOLIO INVÁLIDO\n\n"
                f"El folio {folio_admin} no es un folio EDOMEX válido.\n"
                f"Los folios de EDOMEX deben comenzar con 98.\n\n"
                f"Ejemplo correcto: SERO985"
            )
            return
        
        # Buscar si hay un timer activo con ese folio
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            
            # Cancelar timer específico
            cancelar_timer_folio(folio_admin)
            
            # Actualizar estado en base de datos
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            supabase.table("borradores_registros").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            await message.answer(
                f"✅ TIMER DEL FOLIO {folio_admin} SE DETUVO CON ÉXITO\n\n"
                f"🔐 Código admin ejecutado correctamente\n"
                f"⏰ Timer cancelado exitosamente\n"
                f"📄 Estado actualizado a VALIDADO_ADMIN\n"
                f"👤 Usuario ID: {user_con_folio}\n"
                f"📊 Timers restantes activos: {len(timers_activos)}\n\n"
                f"El usuario ha sido notificado automáticamente."
            )
            
            # Notificar al usuario
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN\n\n"
                    f"📄 Folio: {folio_admin}\n"
                    f"Su permiso ha sido validado por administración.\n"
                    f"El documento está completamente activo para circular.\n\n"
                    f"Gracias por utilizar el Sistema Digital EDOMEX."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"❌ ERROR: TIMER NO ENCONTRADO\n\n"
                f"📄 Folio: {folio_admin}\n"
                f"⚠️ No se encontró ningún timer activo para este folio.\n\n"
                f"Posibles causas:\n"
                f"• El timer ya expiró automáticamente\n"
                f"• El usuario ya envió comprobante\n"
                f"• El folio no existe o es incorrecto\n"
                f"• El folio ya fue validado anteriormente"
            )
    else:
        await message.answer(
            "⚠️ FORMATO INCORRECTO\n\n"
            "Use el formato: SERO[número de folio]\n"
            "Ejemplo: SERO985"
        )

# Handler para recibir comprobantes de pago (MEJORADO PARA MÚLTIPLES FOLIOS)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ No se encontró ningún permiso pendiente de pago.\n\n"
            "Si desea tramitar un nuevo permiso, use /permiso"
        )
        return
    
    # Si tiene varios folios, preguntar cuál
    if len(folios_usuario) > 1:
        lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
        await message.answer(
            f"📄 MÚLTIPLES FOLIOS ACTIVOS\n\n"
            f"Tienes {len(folios_usuario)} folios pendientes de pago:\n\n"
            f"{lista_folios}\n\n"
            f"Por favor, responda con el NÚMERO DE FOLIO al que corresponde este comprobante.\n"
            f"Ejemplo: {folios_usuario[0]}"
        )
        return
    
    # Solo un folio activo, procesar automáticamente
    folio = folios_usuario[0]
    
    # Cancelar timer específico del folio
    cancelar_timer_folio(folio)
    
    # Actualizar estado en base de datos
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    supabase.table("borradores_registros").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
        f"📄 Folio: {folio}\n"
        f"📸 Gracias por la imagen, este comprobante será revisado por un segundo filtro de verificación\n"
        f"⏰ Timer específico del folio detenido exitosamente\n\n"
        f"🔍 Su comprobante está siendo verificado por nuestro equipo especializado.\n"
        f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
        f"Agradecemos su confianza en el Sistema Digital EDOMEX."
    )

# Comando para ver folios activos
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
            "No tienes folios pendientes de pago en este momento.\n\n"
            "Para crear un nuevo permiso utilice /permiso"
        )
        return
    
    lista_folios = []
    for folio in folios_usuario:
        if folio in timers_activos:
            tiempo_restante = 120 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
            tiempo_restante = max(0, tiempo_restante)
            lista_folios.append(f"• {folio} ({tiempo_restante} min restantes)")
        else:
            lista_folios.append(f"• {folio} (sin timer)")
    
    await message.answer(
        f"📋 SUS FOLIOS ACTIVOS ({len(folios_usuario)})\n\n"
        + '\n'.join(lista_folios) +
        f"\n\n⏰ Cada folio tiene su propio timer independiente.\n"
        f"📸 Para enviar comprobante, use una imagen."
    )

# Handler para preguntas sobre costo/precio/depósito
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO} MXN.\n\n"
        "Para iniciar su trámite use /permiso"
    )

@dp.message()
async def fallback(message: types.Message):
    respuestas_elegantes = [
        "🏛️ Sistema Digital EDOMEX. Para tramitar su permiso utilice /permiso",
        "📋 Servicio automatizado. Comando disponible: /permiso para iniciar trámite",
        "⚡ Sistema en línea. Use /permiso para generar su documento oficial",
        "🚗 Plataforma de permisos EDOMEX. Inicie su proceso con /permiso"
    ]
    await message.answer(random.choice(respuestas_elegantes))

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

app = FastAPI(lifespan=lifespan, title="Bot Permisos Estado de México", version="2.0.0")

@app.get("/")
async def health():
    return {
        "status": "running",
        "bot": "Estado de México Permisos",
        "version": "2.0.0 - Sistema Mejorado",
        "webhook_configured": bool(BASE_URL),
        "documentos_generados": 2,
        "timers_activos": len(timers_activos),
        "sistema": "Timers independientes por folio + QR dinámico",
        "prefijo_folio": "98"
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
            "pdfs_por_permiso": 2,
            "timers_sistema": "Independientes por folio",
            "codigo_admin": "SERO + folio",
            "qr_dinamico": True,
            "prefijo_edomex": "98"
        }
    except Exception as e:
        return {"bot_active": False, "error": str(e)}

# Agregar después de las otras rutas de FastAPI, antes del if __name__ == '__main__':

@app.get("/consulta/{folio}")
async def consulta_qr(folio: str):
    from fastapi.responses import HTMLResponse
    
    folio = folio.strip().upper()
    
    try:
        resp = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
        
        if not resp.data:
            html = f"<h1>Folio {folio} no encontrado</h1>"
            return HTMLResponse(content=html)
        
        reg = resp.data[0]
        fe = datetime.fromisoformat(reg['fecha_expedicion'])
        fv = datetime.fromisoformat(reg['fecha_vencimiento'])
        estado = "VIGENTE" if datetime.now() <= fv else "VENCIDO"
        
        html = f"""
        <h1>Permiso EDOMEX</h1>
        <p>Folio: {folio}</p>
        <p>Estado: {estado}</p>
        <p>Marca: {reg['marca']}</p>
        <p>Serie: {reg['numero_serie']}</p>
        <p>Vigencia: {fv.strftime("%d/%m/%Y")}</p>
        """
        return HTMLResponse(content=html)
        
    except:
        return HTMLResponse(content=f"<h1>Error consultando folio {folio}</h1>")

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
