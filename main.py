import os
import re
import time
import asyncio
import threading
import base64
import requests
from urllib.parse import quote
from flask import Flask
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import RPCError

# --- CONFIGURACIÓN DE TELEGRAM Y SHRINKEARN ---
API_ID = 33606964
API_HASH = "756fd02aae2deb1ee44148ebf390d293"
SESSION_STRING = os.environ.get("SESSION_STRING")
ADMIN_CHAT_ID = 6235877284            # Tu ID numérico privado para alertas
CHANNEL_USERNAME = "@comics2035"      # Canal oficial de destino
SHRINKEARN_API_KEY = os.environ.get("SHRINKEARN_API_KEY")

URL_REGEX = r'https?://[^\s]+'

# Cliente de Telethon usando la StringSession de la nube
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- SERVIDOR WEB PARA RENDER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Jarvis Bot Unificado (Monitoreo, Tachado, Sheets y ShrinkEarn) activo.", 200


# --- CONEXIÓN A GOOGLE SHEETS ---
def conectar_spreadsheet():
    """Genera credentials.json de forma segura desde la variable Base64 de Render."""
    if not os.path.exists("credentials.json"):
        b64_creds = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
        if b64_creds:
            decoded_bytes = base64.b64decode(b64_creds)
            with open("credentials.json", "wb") as f:
                f.write(decoded_bytes)
        else:
            raise ValueError("❌ No se encontró la variable GOOGLE_CREDENTIALS_B64 en Render.")

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scopes)
    client_gs = gspread.authorize(creds)
    return client_gs.open("pelicula")


def acortar_enlace(url_original):
    """Acorta un enlace individual usando la API de ShrinkEarn."""
    api_key = os.environ.get("SHRINKEARN_API_KEY") or SHRINKEARN_API_KEY
    if not api_key or not url_original.startswith("http"):
        return url_original

    # Omitir acortar enlaces de Telegram u otros dominios si es necesario
    if "t.me/" in url_original or "telegram.me/" in url_original:
        return url_original

    url_encoded = quote(url_original.strip(), safe=':/?=&')
    api_url = f"https://shrinkearn.com/api?api={api_key}&url={url_encoded}&format=text"

    try:
        response = requests.get(api_url, timeout=15)
        if response.status_code == 200:
            enlace_acortado = response.text.strip()
            if enlace_acortado.startswith("http"):
                return enlace_acortado
    except Exception as e:
        print(f"❌ Error acortando enlace {url_original}: {e}", flush=True)
    
    return url_original


def verificar_enlace(url):
    """Comprueba si un enlace está activo."""
    if "t.me/" in url or "telegram.me/" in url:
        return True
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        respuesta = requests.get(url, headers=headers, timeout=7, allow_redirects=True)
        if respuesta.status_code >= 400:
            return False
            
        cuerpo_html = respuesta.text.lower()
        palabras_error = [
            "404 no encontrado", "no se encontró en este servidor", 
            "link not found", "file deleted", "enlace no encontrado", 
            "archivo borrado", "page not found", "expired", "removed"
        ]
        for palabra in palabras_error:
            if palabra in cuerpo_html:
                return False
        return True
    except Exception:
        return False


async def procesar_pestana_sheets(sheet):
    """Procesa una pestaña del Google Sheet: acorta si hay links nuevos y actualiza el canal."""
    nombre_pestana = sheet.title.strip()
    hashtag_categoria = f"#{nombre_pestana.lower()}"
    print(f"📂 Sincronizando pestaña de Sheets: [{nombre_pestana}]", flush=True)

    try:
        filas = sheet.get_all_values()
    except Exception as e:
        print(f"⚠️ Error al leer la pestaña {nombre_pestana}: {e}", flush=True)
        return

    i = 0
    while i < len(filas):
        fila_actual = filas[i]
        contenido_fila = "".join(fila_actual)

        if not contenido_fila.strip() or "---" in contenido_fila:
            i += 1
            continue

        if i + 1 < len(filas):
            partes_titulo = [celda.strip() for celda in fila_actual if celda.strip()]
            if not partes_titulo:
                i += 1
                continue
            
            titulo = " ".join(partes_titulo)
            mensaje_id_actual = filas[i+1][0].strip() if len(filas[i+1]) > 0 else ""

            enlaces_terabox = []
            enlaces_drive = []
            
            j = i + 2
            leyendo_enlaces = False
            while j < len(filas):
                f_link = filas[j]
                texto_f_link = "".join(f_link)
                if "---" in texto_f_link:
                    break
                if "tera" in texto_f_link.lower() or "drive" in texto_f_link.lower():
                    leyendo_enlaces = True
                    j += 1
                    continue
                
                if leyendo_enlaces:
                    link_t = f_link[0].strip() if len(f_link) > 0 and f_link[0].startswith("http") else ""
                    link_d = f_link[1].strip() if len(f_link) > 1 and f_link[1].startswith("http") else ""
                    if link_t: enlaces_terabox.append(link_t)
                    if link_d: enlaces_drive.append(link_d)
                j += 1

            # Acortar con ShrinkEarn
            tera_acortados = [acortar_enlace(l) for l in enlaces_terabox]
            drive_acortados = [acortar_enlace(l) for l in enlaces_drive]

            if not tera_acortados and not drive_acortados:
                i = j
                continue

            bloque_enlaces = ""
            if tera_acortados:
                bloque_enlaces += "<b>TERABOX:</b>\n"
                for link in tera_acortados: bloque_enlaces += f"{link}\n"
                bloque_enlaces += "\n"
            if drive_acortados:
                bloque_enlaces += "<b>DRIVE:</b>\n"
                for link in drive_acortados: bloque_enlaces += f"{link}\n"
            bloque_enlaces = bloque_enlaces.strip()

            # Autobúsqueda de Message ID si falta en el Sheet
            if not mensaje_id_actual.isdigit():
                palabras_clave = [p for p in titulo.split() if len(p) > 3]
                termino_busqueda = palabras_clave[0] if palabras_clave else titulo
                async for msg in client.iter_messages(CHANNEL_USERNAME, search=termino_busqueda, limit=30):
                    if msg.text and termino_busqueda.lower() in msg.text.lower():
                        mensaje_id_actual = str(msg.id)
                        sheet.update_cell(i + 2, 1, mensaje_id_actual)
                        break

            # Actualizar o publicar en Telegram (y al actualizar con enlaces nuevos, se limpia la tacha automáticamente)
            try:
                if mensaje_id_actual.isdigit():
                    try:
                        msg_existente = await client.get_messages(CHANNEL_USERNAME, ids=int(mensaje_id_actual))
                        texto_actual = msg_existente.text if msg_existente and msg_existente.text else ""
                    except Exception:
                        texto_actual = ""

                    if texto_actual:
                        match = re.search(r'(<b>TERABOX:</b>|<b>DRIVE:</b>)', texto_actual)
                        if match:
                            parte_superior = texto_actual[:match.start()].strip()
                            mensaje_final = f"{parte_superior}\n\n{bloque_enlaces}"
                        else:
                            mensaje_final = f"<b>{titulo}</b>\n{hashtag_categoria}\n\n{bloque_enlaces}"
                    else:
                        mensaje_final = f"<b>{titulo}</b>\n{hashtag_categoria}\n\n{bloque_enlaces}"

                    await client.edit_message(CHANNEL_USERNAME, int(mensaje_id_actual), mensaje_final, parse_mode='html')
                    print(f"✏️ [{nombre_pestana}] Reemplazado y tacha removida en: {titulo}", flush=True)
                else:
                    mensaje_nuevo = f"<b>{titulo}</b>\n{hashtag_categoria}\n\n{bloque_enlaces}"
                    nuevo_msg = await client.send_message(CHANNEL_USERNAME, mensaje_nuevo, parse_mode='html')
                    sheet.update_cell(i + 2, 1, str(nuevo_msg.id))
                    print(f"📢 [{nombre_pestana}] Nuevo post publicado: {titulo}", flush=True)
            except Exception as e:
                print(f"❌ Error actualizando Telegram para '{titulo}': {e}", flush=True)

            i = j
        else:
            i += 1


async def revisar_y_tachar_caidos():
    """Recorre el canal, verifica enlaces y tacha los que estén caídos."""
    print("🤖 Jarvis: Iniciando escaneo de enlaces caídos en el canal...", flush=True)
    async for message in client.iter_messages(CHANNEL_USERNAME):
        texto = message.text
        if not texto:
            continue

        enlaces = re.findall(URL_REGEX, texto)
        if not enlaces:
            continue

        hubo_cambio = False
        nuevo_texto = texto

        lineas_msg = [l.strip() for l in texto.split('\n') if l.strip()]
        titulo_post = lineas_msg[0] if lineas_msg else "Título no identificado"
        titulo_limpio = re.sub(r'<[^>]+>', '', titulo_post)

        for url in enlaces:
            if "t.me/" in url or "telegram.me/" in url:
                continue

            # Verificar si el enlace está caído y no está ya tachado
            if not verificar_enlace(url):
                marcador_caido = f"~~{url}~~ 🔴 [Caído]"
                if marcador_caido not in nuevo_texto and f"~~{url}~~" not in nuevo_texto:
                    nuevo_texto = nuevo_texto.replace(url, marcador_caido)
                    hubo_cambio = True
                    print(f"⚠️ Enlace caído detectado y tachado en: {titulo_limpio}", flush=True)
                    
                    try:
                        await client.send_message(
                            ADMIN_CHAT_ID,
                            f"🚨 **ALERTA JARVIS**\n\n🎬 **Título:** {titulo_limpio}\n🆔 **ID:** `#{message.id}`\n🔗 **Caído:** `{url}`"
                        )
                    except Exception:
                        pass
            await asyncio.sleep(1)

        if hubo_cambio:
            try:
                await client.edit_message(CHANNEL_USERNAME, message.id, nuevo_texto)
            except RPCError as e:
                print(f"Error editando tacha en #{message.id}: {e}", flush=True)


async def ciclo_maestro():
    """Ciclo unificado: primero revisa Google Sheets (actualiza y destacha) y luego escanea caídos."""
    if not SESSION_STRING:
        print("❌ Error crítico: SESSION_STRING no configurada.", flush=True)
        return

    print("Conectando UserBot unificado a Telegram...", flush=True)
    await client.start()
    print("¡Conexión unificada exitosa!", flush=True)

    try:
        await client.send_message(ADMIN_CHAT_ID, "🤖 **Jarvis Unificado Activo:** Sincronización de Sheets, acortamiento y tachado en marcha.")
    except Exception:
        pass

    while True:
        try:
            print("🔄 Iniciando ciclo maestro del bot unificado...", flush=True)
            
            # 1. Sincronizar desde Google Sheets (Inserta nuevos, actualiza reemplazos y quita tachas)
            spreadsheet = conectar_spreadsheet()
            for sheet in spreadsheet.worksheets():
                await procesar_pestana_sheets(sheet)
                await asyncio.sleep(2)

            # 2. Escanear canal en busca de nuevos caídos para tachar
            await revisar_y_tachar_caidos()

        except Exception as e:
            print(f"⚠️ Error en el ciclo maestro: {e}", flush=True)

        print("⏳ Ciclo completo finalizado. Esperando 1 hora...", flush=True)
        await asyncio.sleep(3600)


def iniciar_hilo_async():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ciclo_maestro())


if __name__ == "__main__":
    hilo = threading.Thread(target=iniciar_hilo_async)
    hilo.daemon = True
    hilo.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
