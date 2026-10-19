"""
Estudio de voz: servidor local con interfaz web.
  · Clonar voz   → Chatterbox (multilingüe)
  · Voces        → crear voces nuevas: Kokoro (inglés) y Qwen3-TTS VoiceDesign (español)
  · Transcribir  → WhisperX
Solo usa la librería estándar de Python + chatterbox-tts; los demás modelos
corren en sus propios entornos (.venv-kokoro, .venv-qwen, .venv-whisper).

    python servidor.py                 # abre http://127.0.0.1:7860
    python servidor.py --red           # accesible desde otros equipos de tu red
    python servidor.py --dispositivo cpu
    python servidor.py --simular       # sin modelo, para probar la interfaz
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import inspect
import io
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
VOCES = BASE / "voces"
SALIDAS = BASE / "salidas"
STATIC = BASE / "static"
VOCES_JSON = VOCES / "voces.json"
TRANSCRIPCIONES = BASE / "transcripciones"
PRUEBAS = BASE / "pruebas"


def python_de(entorno: str) -> Path:
    return BASE / entorno / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


WHISPER_PY = python_de(".venv-whisper")
MOTORES_VOCES = {"kokoro": python_de(".venv-kokoro"), "qwen": python_de(".venv-qwen")}
MAX_SUBIDA_AUDIO_LARGO = 600 * 1024 * 1024  # para transcribir (≈ 5 h a 16 kHz)
FORMATOS_TRANSCRIPCION = re.compile(r"^(original|traduccion)\.(txt|srt|vtt)$|^transcripcion\.json$|^entrada\.wav$")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
WAV_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}\.wav$")
MAX_TROZO = 260            # caracteres por trozo que se envía al modelo
PAUSA_ENTRE_TROZOS = 0.22  # segundos de silencio entre trozos
MAX_SUBIDA = 50 * 1024 * 1024

IDIOMAS_RESERVA = {
    "es": "Español", "en": "Inglés", "fr": "Francés", "de": "Alemán",
    "it": "Italiano", "pt": "Portugués", "ar": "Árabe", "nl": "Neerlandés",
}


class ErrorApi(Exception):
    def __init__(self, status: int, mensaje: str) -> None:
        super().__init__(mensaje)
        self.status = status
        self.mensaje = mensaje


# ───────────────────────────── Utilidades ─────────────────────────────

def slug(texto: str, maximo: int = 40) -> str:
    s = texto.lower()
    for a, b in zip("áéíóúüñ", "aeiouun"):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maximo].strip("-") or "audio"


def duracion_wav(ruta: Path) -> float:
    with wave.open(str(ruta), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def trocear(texto: str, maximo: int = MAX_TROZO) -> list[str]:
    """Parte un texto largo en trozos que el modelo maneja bien, respetando las frases."""
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) <= maximo:
        return [texto] if texto else []
    piezas: list[str] = []
    for f in re.split(r"(?<=[.!?…;:])\s+", texto):
        if len(f) <= maximo:
            piezas.append(f)
            continue
        actual = ""  # frase muy larga: se corta por palabras
        for palabra in f.split(" "):
            if actual and len(actual) + len(palabra) + 1 > maximo:
                piezas.append(actual)
                actual = palabra
            else:
                actual = f"{actual} {palabra}".strip()
        if actual:
            piezas.append(actual)
    trozos: list[str] = []
    actual = ""
    for p in piezas:
        if len(actual) + len(p) + 1 <= maximo:
            actual = f"{actual} {p}".strip()
        else:
            if actual:
                trozos.append(actual)
            actual = p
    if actual:
        trozos.append(actual)
    return trozos


def recortar_silencio(m: list[float], sr: int, umbral: float = 0.01, margen: float = 0.08) -> list[float]:
    ini = next((i for i, x in enumerate(m) if abs(x) > umbral), None)
    if ini is None:
        return m
    fin = len(m) - next(i for i, x in enumerate(reversed(m)) if abs(x) > umbral)
    pad = int(margen * sr)
    return m[max(0, ini - pad): min(len(m), fin + pad)]


def escribir_wav(ruta: Path, muestras: list[float], sr: int) -> None:
    pico = max((abs(x) for x in muestras), default=0.0)
    escala = 0.97 / pico if pico > 0.97 else 1.0  # evita saturar
    datos = struct.pack(
        f"<{len(muestras)}h",
        *(int(max(-1.0, min(1.0, x * escala)) * 32767) for x in muestras),
    )
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(datos)


def leer_voces() -> list[dict]:
    if VOCES_JSON.exists():
        return json.loads(VOCES_JSON.read_text(encoding="utf-8"))
    return []


def guardar_voces(voces: list[dict]) -> None:
    VOCES_JSON.write_text(json.dumps(voces, indent=2, ensure_ascii=False), encoding="utf-8")


def buscar_voz(voz_id: str) -> dict:
    for v in leer_voces():
        if v["id"] == voz_id:
            return v
    raise ErrorApi(404, "Esa voz no existe.")


def ip_local() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u200d\ufe0f]+")


def limpiar_marcas(texto: str) -> str:
    """Quita las marcas de subtítulos de los guiones (*clave*) y los emojis."""
    t = re.sub(r"\s+", " ", EMOJI_RE.sub("", texto.replace("*", "")))
    return re.sub(r"\s+([.,;:!?…])", r"\1", t).strip()


def numero(d: dict, clave: str, defecto: float, minimo: float, maximo: float) -> float:
    try:
        v = float(d.get(clave, defecto))
    except (TypeError, ValueError):
        raise ErrorApi(400, f"Valor no válido para {clave}.")
    return max(minimo, min(maximo, v))


# ───────────────────────────── Motor de voz ─────────────────────────────

class Motor:
    """Carga el modelo una vez y genera los audios de uno en uno."""

    def __init__(self, dispositivo: str, simular: bool) -> None:
        self.pedido = dispositivo
        self.simular = simular
        self.estado = "cargando"  # cargando | listo | error
        self.error: Optional[str] = None
        self.dispositivo = "?"
        self.gpu: Optional[str] = None
        self.idiomas: dict[str, str] = dict(IDIOMAS_RESERVA)
        self.sr = 24000
        self.model: Any = None
        self.torch: Any = None
        # Un solo candado para la GPU: o se clona voz o se transcribe, nunca a la vez
        self.lock = threading.Lock()
        self.progreso: Optional[dict] = None
        self.progreso_tr: Optional[dict] = None
        self.progreso_voz: Optional[dict] = None
        self.gpu_gb = 0.0

    def liberar(self) -> bool:
        """Saca Chatterbox de la GPU para dejar sitio a otro modelo. Se recarga solo al generar."""
        if self.estado != "listo" or (self.dispositivo != "cuda" and not self.simular):
            return False
        print("[clonador] Libero la GPU (Chatterbox en pausa hasta que vuelvas a generar).")
        self.model = None
        gc.collect()
        try:
            self.torch.cuda.empty_cache()
        except Exception:
            pass
        self.estado = "dormido"
        return True

    def cargar(self) -> None:
        self.estado, self.error = "cargando", None
        if self.simular:
            time.sleep(1.0)
            self.dispositivo, self.estado = "simulado", "listo"
            return
        try:
            import torch

            self.torch = torch
            try:
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            except ImportError:
                raise RuntimeError(
                    "Tu versión de chatterbox-tts no trae el modelo multilingüe (español). "
                    "Actualiza con:  .venv\\Scripts\\pip install -U chatterbox-tts"
                )
            try:
                from chatterbox.mtl_tts import SUPPORTED_LANGUAGES  # type: ignore

                self.idiomas = {
                    k: IDIOMAS_RESERVA.get(k, str(v).capitalize()) for k, v in dict(SUPPORTED_LANGUAGES).items()
                }
            except Exception:
                pass

            if self.pedido != "auto":
                dev = self.pedido
            elif torch.cuda.is_available():
                dev = "cuda"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                dev = "mps"
            else:
                dev = "cpu"
            if dev == "cuda":
                p = torch.cuda.get_device_properties(0)
                gb = p.total_memory / 1024**3
                self.gpu_gb = gb
                if self.pedido == "auto" and gb < 3.5:
                    print(f"[clonador] {p.name} solo tiene {gb:.1f} GB: no basta para Chatterbox, uso la CPU.")
                    dev = "cpu"
                else:
                    self.gpu = f"{p.name} · {gb:.1f} GB"
            self.dispositivo = dev
            self.model = None
            # 1) Modelo descargado a mano en modelos/chatterbox (ver modelo_manual.py)
            try:
                from modelo_manual import CARPETA, archivos_necesarios

                if all((CARPETA / n).exists() and (CARPETA / n).stat().st_size > 0 for n in archivos_necesarios()):
                    print(f"[clonador] Cargando el modelo desde {CARPETA} en {dev}…")
                    self.model = ChatterboxMultilingualTTS.from_local(str(CARPETA), dev)
            except Exception as e:  # noqa: BLE001
                print(f"[clonador] No he podido usar la carpeta modelos/chatterbox ({e}); lo descargo.")
                self.model = None
            # 2) Si no, descarga automática desde Hugging Face
            if self.model is None:
                print(f"[clonador] Cargando el modelo en {dev}… (la primera vez descarga varios GB;"
                      " si va lenta, mira modelo_manual.py)")
                self.model = ChatterboxMultilingualTTS.from_pretrained(device=dev)
            self.sr = int(getattr(self.model, "sr", 24000))
            self.estado = "listo"
            print("[clonador] Modelo listo.")
        except Exception as e:  # noqa: BLE001
            self.estado, self.error = "error", str(e)
            print(f"[clonador] ERROR al cargar el modelo: {e}")

    def _trozo(self, texto: str, voz: Path, idioma: str, aj: dict) -> list[float]:
        if self.simular:
            time.sleep(0.4)
            n = int(self.sr * max(0.6, len(texto) / 15))
            return [0.2 * math.sin(2 * math.pi * 220 * i / self.sr) * math.sin(math.pi * i / n) for i in range(n)]
        torch = self.torch
        torch.manual_seed(int(aj["semilla"]))
        posibles = dict(
            language_id=idioma,
            audio_prompt_path=str(voz),
            exaggeration=aj["expresividad"],
            cfg_weight=aj["cfg"],
            temperature=aj["temperatura"],
        )
        params = inspect.signature(self.model.generate).parameters
        kwargs = {k: v for k, v in posibles.items() if k in params}
        with torch.inference_mode():
            wav = self.model.generate(texto, **kwargs)
        return wav.detach().cpu().float().reshape(-1).tolist()

    def sintetizar(self, texto: str, voz: Path, idioma: str, aj: dict) -> list[float]:
        trozos = trocear(texto)
        if not trozos:
            raise ErrorApi(400, "El texto está vacío.")
        salida: list[float] = []
        pausa = [0.0] * int(PAUSA_ENTRE_TROZOS * self.sr)
        for i, t in enumerate(trozos):
            if i:
                salida.extend(pausa)
            salida.extend(recortar_silencio(self._trozo(t, voz, idioma, aj), self.sr))
        return salida


# ───────────────────────────── Acciones de la API ─────────────────────────────

def api_subir_voz(nombre: str, datos: bytes, extra: Optional[dict] = None) -> dict:
    if len(datos) > MAX_SUBIDA:
        raise ErrorApi(413, "El archivo es demasiado grande (máx. 50 MB).")
    if datos[:4] != b"RIFF" or datos[8:12] != b"WAVE":
        raise ErrorApi(400, "Formato no válido: se esperaba un WAV.")
    nombre = (nombre or "").strip()[:60] or "Sin nombre"
    voz_id = f"{slug(nombre, 30)}-{uuid.uuid4().hex[:6]}"
    ruta = VOCES / f"{voz_id}.wav"
    ruta.write_bytes(datos)
    try:
        dur = duracion_wav(ruta)
    except Exception:
        ruta.unlink(missing_ok=True)
        raise ErrorApi(400, "No he podido leer ese WAV.")
    voz = {
        "id": voz_id,
        "nombre": nombre,
        "archivo": ruta.name,
        "duracion": round(dur, 2),
        "creada": dt.datetime.now().isoformat(timespec="seconds"),
        **(extra or {}),
    }
    voces = leer_voces()
    voces.insert(0, voz)
    guardar_voces(voces)
    return voz


def api_borrar_voz(voz_id: str) -> dict:
    voz = buscar_voz(voz_id)
    (VOCES / voz["archivo"]).unlink(missing_ok=True)
    guardar_voces([v for v in leer_voces() if v["id"] != voz_id])
    return {"ok": True}


def api_generar(motor: Motor, d: dict, voces: "GestorVoces") -> dict:
    if motor.estado not in ("listo", "dormido"):
        raise ErrorApi(503, motor.error or "El modelo aún se está cargando, espera un momento.")
    texto = str(d.get("texto", "")).strip()
    if not texto:
        raise ErrorApi(400, "Escribe algún texto.")
    if len(texto) > 20000:
        raise ErrorApi(400, "Texto demasiado largo (máx. 20.000 caracteres).")
    voz = buscar_voz(str(d.get("voz_id", "")))
    idioma = str(d.get("idioma", "es"))
    if idioma not in motor.idiomas:
        raise ErrorApi(400, f"Idioma no soportado: {idioma}")
    idioma_texto = str(d.get("idioma_texto", idioma))
    if not re.match(r"^[a-z]{2}$", idioma_texto):
        raise ErrorApi(400, "Idioma del texto no válido.")
    aj = {
        "expresividad": numero(d, "expresividad", 0.5, 0.0, 2.0),
        "cfg": numero(d, "cfg", 0.5, 0.0, 1.0),
        "temperatura": numero(d, "temperatura", 0.8, 0.05, 2.0),
        "semilla": int(numero(d, "semilla", 42, 0, 2**31 - 1)),
    }
    por_lineas = bool(d.get("por_lineas", False))

    lineas = [l.strip() for l in texto.splitlines() if l.strip()] if por_lineas else [texto]
    items: list[tuple[str, str]] = []
    usados: set[str] = set()
    for i, linea in enumerate(lineas, 1):
        nombre = None
        m = re.match(r"^([^|]{1,40})\|\s*(.+)$", linea)
        if por_lineas and m:
            nombre, linea = slug(m.group(1), 40), m.group(2).strip()
        base = nombre or (f"{i:02d}-{slug(linea, 30)}" if por_lineas else slug(linea, 40))
        while base in usados:
            base += "-b"
        usados.add(base)
        items.append((base, limpiar_marcas(linea)))

    if not motor.lock.acquire(timeout=0.1):
        raise ErrorApi(409, "Ya hay una generación en marcha. Espera a que termine.")
    trabajo_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    carpeta = SALIDAS / trabajo_id
    try:
        if motor.estado == "dormido":
            voces.parar_gpu()
            motor.progreso = {"actual": 0, "total": len(items), "texto": "Recargando Chatterbox…", "inicio": time.time()}
            motor.cargar()
            if motor.estado != "listo":
                raise ErrorApi(500, motor.error or "No se pudo recargar el modelo de voz.")
        carpeta.mkdir(parents=True)
        t0 = time.time()
        originales = [linea for _, linea in items]
        if idioma_texto != idioma:
            motor.progreso = {"actual": 0, "total": len(items), "texto": "Traduciendo el texto…", "inicio": t0}
            try:
                from traductor import traducir_lista

                traducidos = traducir_lista(originales, idioma_texto, idioma, dispositivo="cpu", simular=motor.simular)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                raise ErrorApi(500, f"No se pudo traducir: {e}")
            items = [(base, tr) for (base, _), tr in zip(items, traducidos)]
        archivos = []
        for n, (base, linea) in enumerate(items, 1):
            motor.progreso = {"actual": n, "total": len(items), "texto": linea[:90], "inicio": t0}
            try:
                muestras = motor.sintetizar(linea, VOCES / voz["archivo"], idioma, aj)
            except ErrorApi:
                raise
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "out of memory" in msg.lower():
                    try:
                        motor.torch.cuda.empty_cache()
                    except Exception:
                        pass
                    msg = ("la GPU se ha quedado sin memoria. Cierra otros programas o arranca "
                           "con «iniciar.bat --dispositivo cpu».")
                traceback.print_exc()
                raise ErrorApi(500, f"Error generando «{linea[:40]}»: {msg}")
            ruta = carpeta / f"{base}.wav"
            escribir_wav(ruta, muestras, motor.sr)
            archivo = {
                "archivo": ruta.name,
                "nombre": base,
                "texto": linea,
                "duracion": round(len(muestras) / motor.sr, 3),
            }
            if idioma_texto != idioma:
                archivo["texto_original"] = originales[n - 1]
            archivos.append(archivo)
        meta = {
            "id": trabajo_id,
            "creado": dt.datetime.now().isoformat(timespec="seconds"),
            "voz": {"id": voz["id"], "nombre": voz["nombre"]},
            "idioma": idioma,
            "idioma_texto": idioma_texto,
            "ajustes": aj,
            "segundos_generando": round(time.time() - t0, 1),
            "archivos": archivos,
        }
        (carpeta / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta
    except Exception:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise
    finally:
        motor.progreso = None
        motor.lock.release()


def carpeta_trabajo(trabajo_id: str) -> Path:
    if not ID_RE.match(trabajo_id):
        raise ErrorApi(400, "Id no válido.")
    c = SALIDAS / trabajo_id
    if not (c / "meta.json").exists():
        raise ErrorApi(404, "No existe.")
    return c


def api_trabajos() -> list[dict]:
    out = []
    for m in sorted(SALIDAS.glob("*/meta.json"), reverse=True):
        try:
            out.append(json.loads(m.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def zip_trabajo(trabajo_id: str) -> bytes:
    c = carpeta_trabajo(trabajo_id)
    meta = json.loads((c / "meta.json").read_text(encoding="utf-8"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in meta["archivos"]:
            z.write(c / a["archivo"], a["archivo"])
        z.writestr("duraciones.json", json.dumps({a["nombre"]: a["duracion"] for a in meta["archivos"]}, indent=2))
        z.writestr("guion.txt", "\n".join(f'{a["nombre"]} | {a["texto"]}' for a in meta["archivos"]))
    return buf.getvalue()


# ───────────────────────────── Transcripción (WhisperX) ─────────────────────────────

def whisper_instalado(motor: Motor) -> bool:
    return motor.simular or WHISPER_PY.exists()


def api_transcribir(motor: Motor, q: dict, datos: bytes, voces: "GestorVoces") -> dict:
    if not whisper_instalado(motor):
        raise ErrorApi(503, "La transcripción no está instalada. Cierra el servidor y ejecuta «instalar-whisper.bat».")
    if datos[:4] != b"RIFF" or datos[8:12] != b"WAVE":
        raise ErrorApi(400, "Formato no válido: se esperaba un WAV.")
    entrada = q.get("entrada", "auto")
    salida = q.get("salida", "igual")
    modelo = q.get("modelo", "equilibrado")
    if not re.match(r"^(auto|[a-z]{2})$", entrada) or not re.match(r"^(igual|[a-z]{2})$", salida):
        raise ErrorApi(400, "Idioma no válido.")
    if modelo not in ("rapido", "equilibrado", "preciso"):
        raise ErrorApi(400, "Modelo no válido.")
    nombre = (q.get("nombre") or "audio").strip()[:80]

    if not motor.lock.acquire(timeout=0.1):
        raise ErrorApi(409, "La GPU está ocupada con otra tarea. Espera a que termine.")
    tid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    carpeta = TRANSCRIPCIONES / tid
    t0 = time.time()
    try:
        carpeta.mkdir(parents=True)
        (carpeta / "entrada.wav").write_bytes(datos)
        try:
            dur = duracion_wav(carpeta / "entrada.wav")
        except Exception:
            raise ErrorApi(400, "No he podido leer ese WAV.")
        motor.progreso_tr = {"etapa": "Arrancando WhisperX…", "inicio": t0, "duracion": dur}
        voces.parar_gpu()

        py = sys.executable if motor.simular else str(WHISPER_PY)
        base_cmd = [py, "-u", str(BASE / "transcriptor.py"), "--audio", str(carpeta / "entrada.wav"),
                    "--salida", str(carpeta), "--entrada", entrada, "--idioma-salida", salida, "--modelo", modelo]
        if q.get("palabras", "1") == "1":
            base_cmd.append("--palabras")
        if motor.simular:
            base_cmd.append("--simular")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        avisos: list[str] = []

        def ejecutar(dispositivo: str) -> tuple[Optional[dict], Optional[str], list[str]]:
            cmd = base_cmd + ["--dispositivo", dispositivo]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(BASE),
                                    text=True, encoding="utf-8", errors="replace", env=env)
            fin_: Optional[dict] = None
            error_: Optional[str] = None
            ultimas: list[str] = []
            assert proc.stdout is not None
            for linea in proc.stdout:
                linea = linea.rstrip()
                if not linea.startswith("@@"):
                    if linea:
                        print(f"  [whisper] {linea}")
                        ultimas = (ultimas + [linea])[-6:]
                    continue
                try:
                    ev = json.loads(linea[2:])
                except json.JSONDecodeError:
                    continue
                if ev.get("tipo") == "progreso":
                    motor.progreso_tr = {**motor.progreso_tr, "etapa": ev.get("etapa", "")}
                elif ev.get("tipo") == "aviso":
                    avisos.append(ev.get("mensaje", ""))
                    motor.progreso_tr = {**motor.progreso_tr, "aviso": ev.get("mensaje", "")}
                elif ev.get("tipo") == "fin":
                    fin_ = ev
                elif ev.get("tipo") == "error":
                    error_ = ev.get("mensaje")
            proc.wait()
            return fin_, error_, ultimas

        primero = motor.pedido if motor.pedido in ("cuda", "cpu") else "auto"
        fin, error, ultimas = ejecutar(primero)
        fallo_gpu = error is None or any(k in error.lower() for k in ("cuda", "cudnn", "cublas", "memory", "dll"))
        if fin is None and primero != "cpu" and not motor.simular and fallo_gpu:
            # Si la GPU falla de forma brusca (DLL de CUDA, memoria…), se reintenta con la CPU
            aviso = "La GPU ha fallado al transcribir; lo he repetido con la CPU."
            avisos.append(aviso)
            motor.progreso_tr = {**motor.progreso_tr, "aviso": aviso, "etapa": "Reintentando con la CPU…"}
            fin, error2, ultimas = ejecutar("cpu")
            error = error2 or error
        if fin is None:
            detalle = error or (" | ".join(ultimas[-3:]) if ultimas else f"código {proc.returncode}")
            raise ErrorApi(500, f"La transcripción ha fallado: {detalle}")

        resultado = json.loads((carpeta / "transcripcion.json").read_text(encoding="utf-8"))
        meta = {
            "id": tid,
            "creado": dt.datetime.now().isoformat(timespec="seconds"),
            "nombre": nombre,
            "duracion": round(dur, 2),
            "entrada": entrada,
            "idioma": resultado["idioma_detectado"],
            "destino": fin.get("destino"),
            "modelo": modelo,
            "dispositivo": fin.get("dispositivo"),
            "segundos": fin.get("segundos"),
            "avisos": avisos,
            "archivos": fin.get("archivos", []),
            "texto": resultado["texto"],
            "traduccion": resultado.get("traduccion", {}).get("texto"),
        }
        (carpeta / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta
    except Exception:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise
    finally:
        motor.progreso_tr = None
        motor.lock.release()


def carpeta_transcripcion(tid: str) -> Path:
    if not ID_RE.match(tid):
        raise ErrorApi(400, "Id no válido.")
    c = TRANSCRIPCIONES / tid
    if not (c / "meta.json").exists():
        raise ErrorApi(404, "No existe.")
    return c


def api_transcripciones() -> list[dict]:
    out = []
    for m in sorted(TRANSCRIPCIONES.glob("*/meta.json"), reverse=True):
        try:
            out.append(json.loads(m.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


# ───────────────────────────── Voces nuevas (Kokoro y Qwen) ─────────────────────────────

KOKORO_RE = re.compile(r"^[ab][fm]_[a-z]{2,20}$")
PRUEBA_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ACENTOS = {
    "espana": "Native Spanish speaker from Spain, with a clear Castilian (Madrid) accent.",
    "latino": "Native Spanish speaker with a neutral Latin American accent.",
    "us": "Native speaker with a general American English accent.",
    "uk": "Native speaker with a standard British English accent.",
}
INACTIVO = 15 * 60  # se cierra un motor que lleva este tiempo sin usarse


class Trabajador:
    """Un proceso con Kokoro o Qwen cargado, al que se le piden audios de uno en uno."""

    def __init__(self, nombre: str, simular: bool, dispositivo: str) -> None:
        self.nombre = nombre
        self.simular = simular
        self.pedido = dispositivo
        self.proc: Optional[subprocess.Popen] = None
        self.dispositivo: Optional[str] = None
        self.ultimo_uso = 0.0
        self.lock = threading.Lock()

    def instalado(self) -> bool:
        return self.simular or MOTORES_VOCES[self.nombre].exists()

    def vivo(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _leer(self, cb, hasta: tuple[str, ...]) -> dict:
        assert self.proc is not None and self.proc.stdout is not None
        ultimas: list[str] = []
        for linea in self.proc.stdout:
            linea = linea.rstrip()
            if not linea.startswith("@@"):
                if linea:
                    print(f"  [{self.nombre}] {linea}")
                    ultimas = (ultimas + [linea])[-6:]
                continue
            try:
                ev = json.loads(linea[2:])
            except json.JSONDecodeError:
                continue
            if ev.get("tipo") in hasta or ev.get("tipo") == "error":
                return ev
            cb(ev)
        detalle = " | ".join(ultimas[-3:]) or "el proceso se ha cerrado"
        self.proc = None
        return {"tipo": "error", "mensaje": detalle, "fatal": True}

    def _arrancar(self, cb, dispositivo: str) -> None:
        py = sys.executable if self.simular else str(MOTORES_VOCES[self.nombre])
        cmd = [py, "-u", str(BASE / "voces_motor.py"), "--motor", self.nombre, "--dispositivo", dispositivo]
        if self.simular:
            cmd.append("--simular")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        env.setdefault("HF_HUB_DISABLE_XET", "1")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     cwd=str(BASE), text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)
        ev = self._leer(cb, ("listo",))
        if ev["tipo"] != "listo":
            self.parar()
            raise ErrorApi(500, ev.get("mensaje") or "No se pudo arrancar el motor.")
        self.dispositivo = ev.get("dispositivo")

    def pedir(self, peticion: dict, cb) -> dict:
        with self.lock:
            if not self.vivo():
                self._arrancar(cb, self.pedido)
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(peticion, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            ev = self._leer(cb, ("fin",))
            self.ultimo_uso = time.time()
            if ev["tipo"] != "fin":
                if ev.get("fatal"):
                    self.parar()
                raise ErrorApi(500, ev.get("mensaje") or "Error al generar la voz.")
            return ev

    def parar(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        print(f"[clonador] Motor {self.nombre} cerrado.")


class GestorVoces:
    def __init__(self, motor: Motor, simular: bool, dispositivo: str) -> None:
        self.motor = motor
        d = dispositivo if dispositivo in ("cuda", "cpu") else "auto"
        self.trabajadores = {n: Trabajador(n, simular, d) for n in MOTORES_VOCES}
        threading.Thread(target=self._vigilar, daemon=True).start()

    def _vigilar(self) -> None:
        while True:
            time.sleep(60)
            for t in self.trabajadores.values():
                if t.vivo() and not t.lock.locked() and time.time() - t.ultimo_uso > INACTIVO:
                    t.parar()

    def parar_gpu(self) -> None:
        """Cierra los motores que estén ocupando la GPU (antes de usar Chatterbox o WhisperX)."""
        for t in self.trabajadores.values():
            if t.vivo() and (t.dispositivo or "").startswith("cuda") and not t.lock.locked():
                t.parar()

    def estado(self) -> dict:
        return {
            n: {"instalado": t.instalado(), "cargado": t.vivo(), "dispositivo": t.dispositivo if t.vivo() else None}
            for n, t in self.trabajadores.items()
        }


def traducir_descripcion(texto: str, simular: bool) -> str:
    """Qwen entiende mejor las descripciones en inglés: si parece español, se traducen."""
    if not re.search(r"[áéíóúñ¿¡]|\b(voz|hombre|mujer|años|habla|tono|grave|aguda|cálida|joven)\b", texto, re.I):
        return texto
    try:
        from traductor import traducir_lista

        return traducir_lista([texto], "es", "en", dispositivo="cpu", simular=simular)[0] or texto
    except Exception as e:  # noqa: BLE001
        print(f"[clonador] No he podido traducir la descripción ({e}); la uso tal cual.")
        return texto


def api_disenar(motor: Motor, voces: GestorVoces, d: dict) -> dict:
    nombre_motor = str(d.get("motor", ""))
    if nombre_motor not in MOTORES_VOCES:
        raise ErrorApi(400, "Motor no válido.")
    trab = voces.trabajadores[nombre_motor]
    if not trab.instalado():
        raise ErrorApi(503, "Este motor no está instalado. Cierra el servidor y ejecuta «instalar-voces.bat».")
    idioma = str(d.get("idioma", "es"))
    if idioma not in ("es", "en"):
        raise ErrorApi(400, "Idioma no válido.")
    texto = limpiar_marcas(str(d.get("texto", "")))
    if not texto:
        raise ErrorApi(400, "Escribe el texto de prueba.")
    if len(texto) > 700:
        raise ErrorApi(400, "El texto de prueba es demasiado largo (máx. 700 caracteres).")

    peticion: dict = {"texto": texto, "idioma": idioma}
    meta: dict = {"motor": nombre_motor, "idioma": idioma, "texto": texto}
    if nombre_motor == "kokoro":
        voz = str(d.get("voz", ""))
        if not KOKORO_RE.match(voz):
            raise ErrorApi(400, "Voz de Kokoro no válida.")
        if idioma != "en":
            raise ErrorApi(400, "Las voces de Kokoro son en inglés; para español usa «Diseñar».")
        peticion.update(voz=voz, velocidad=numero(d, "velocidad", 1.0, 0.6, 1.5))
        meta.update(voz=voz, descripcion=str(d.get("etiqueta") or voz)[:120])
    else:
        desc = str(d.get("instruccion", "")).strip()
        if len(desc) < 3:
            raise ErrorApi(400, "Describe cómo es la voz.")
        if len(desc) > 600:
            raise ErrorApi(400, "La descripción es demasiado larga (máx. 600 caracteres).")
        acento = ACENTOS.get(str(d.get("acento", "")), "")
        semilla = int(numero(d, "semilla", 7, 0, 2**31 - 1))
        instruccion = f"{traducir_descripcion(desc, motor.simular)} {acento}".strip()
        peticion.update(instruccion=instruccion, semilla=semilla)
        meta.update(descripcion=desc, instruccion=instruccion, acento=d.get("acento"), semilla=semilla)

    usa_gpu = nombre_motor == "qwen"
    if usa_gpu and not motor.lock.acquire(timeout=0.1):
        raise ErrorApi(409, "La GPU está ocupada con otra tarea. Espera a que termine.")
    pid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    t0 = time.time()
    try:
        motor.progreso_voz = {"etapa": "Preparando el motor…", "inicio": t0}
        if usa_gpu and not trab.vivo() and trab.pedido != "cpu" and 0 < motor.gpu_gb < 12:
            # Qwen y Chatterbox no caben a la vez en esta gráfica: Chatterbox espera en pausa
            # (se recarga solo al volver a generar) y Qwen aprovecha toda la memoria
            motor.liberar()

        def cb(ev: dict) -> None:
            if ev.get("tipo") == "progreso":
                motor.progreso_voz = {**(motor.progreso_voz or {}), "etapa": ev.get("etapa", "")}
            elif ev.get("tipo") == "aviso":
                motor.progreso_voz = {**(motor.progreso_voz or {}), "aviso": ev.get("mensaje", "")}

        PRUEBAS.mkdir(exist_ok=True)
        fin = trab.pedir({**peticion, "salida": str(PRUEBAS / f"{pid}.wav")}, cb)
        meta.update(id=pid, creado=dt.datetime.now().isoformat(timespec="seconds"), duracion=fin.get("duracion"),
                    segundos=fin.get("segundos"), dispositivo=fin.get("dispositivo"),
                    aviso=(motor.progreso_voz or {}).get("aviso"))
        (PRUEBAS / f"{pid}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta
    finally:
        motor.progreso_voz = None
        if usa_gpu:
            motor.lock.release()


def prueba(pid: str) -> tuple[Path, dict]:
    if not PRUEBA_RE.match(pid) or not (PRUEBAS / f"{pid}.json").exists():
        raise ErrorApi(404, "Esa prueba no existe.")
    return PRUEBAS / f"{pid}.wav", json.loads((PRUEBAS / f"{pid}.json").read_text(encoding="utf-8"))


def api_pruebas() -> list[dict]:
    out = []
    for m in sorted(PRUEBAS.glob("*.json"), reverse=True)[:40]:
        try:
            out.append(json.loads(m.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def api_guardar_prueba(pid: str, d: dict) -> dict:
    wav, meta = prueba(pid)
    nombre = str(d.get("nombre", "")).strip()[:60]
    if not nombre:
        raise ErrorApi(400, "Ponle un nombre a la voz.")
    origen = f"Kokoro · {meta.get('voz')}" if meta["motor"] == "kokoro" else "Diseñada con Qwen3-TTS"
    voz = api_subir_voz(nombre, wav.read_bytes(), {
        "idioma": meta["idioma"], "origen": origen, "descripcion": meta.get("descripcion", ""),
    })
    meta["guardada"] = {"id": voz["id"], "nombre": voz["nombre"]}
    (PRUEBAS / f"{pid}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return voz


# ───────────────────────────── Servidor HTTP ─────────────────────────────

def crear_handler(motor: Motor, voces: GestorVoces):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClonadorVoz/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # consola limpia
            pass

        def _json(self, obj: Any, status: int = 200) -> None:
            datos = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(datos)

        def _bytes(self, datos: bytes, tipo: str, descarga: Optional[str] = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            if tipo.startswith(("text/html", "application/javascript", "text/css")):
                self.send_header("Cache-Control", "no-cache")  # tras actualizar, siempre la versión nueva
            self.send_header("Content-Length", str(len(datos)))
            if descarga:
                self.send_header("Content-Disposition", f'attachment; filename="{descarga}"')
            self.end_headers()
            self.wfile.write(datos)

        def _archivo(self, ruta: Path, descarga: bool = False) -> None:
            tipo = mimetypes.guess_type(ruta.name)[0] or "application/octet-stream"
            if tipo.startswith("text/") or tipo in ("application/javascript", "application/json"):
                tipo += "; charset=utf-8"
            self._bytes(ruta.read_bytes(), tipo, ruta.name if descarga else None)

        def _cuerpo(self, limite: int = MAX_SUBIDA) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limite:
                raise ErrorApi(413, "El archivo es demasiado grande.")
            return self.rfile.read(n) if n else b""

        def _json_cuerpo(self) -> dict:
            try:
                d = json.loads(self._cuerpo() or b"{}")
            except json.JSONDecodeError:
                raise ErrorApi(400, "JSON no válido.")
            if not isinstance(d, dict):
                raise ErrorApi(400, "JSON no válido.")
            return d

        def _despachar(self, metodo: str) -> None:
            url = urlparse(self.path)
            partes = [unquote(p) for p in url.path.split("/") if p]
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if partes[:1] == ["api"]:
                    self._api(metodo, partes[1:], q)
                elif metodo == "GET":
                    self._estatico(url.path)
                else:
                    raise ErrorApi(404, "No encontrado.")
            except ErrorApi as e:
                self._json({"detail": e.mensaje}, e.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._json({"detail": f"Error interno: {e}"}, 500)

        def do_GET(self) -> None:
            self._despachar("GET")

        def do_POST(self) -> None:
            self._despachar("POST")

        def do_DELETE(self) -> None:
            self._despachar("DELETE")

        def _api(self, metodo: str, p: list[str], q: dict) -> None:
            if metodo == "GET" and p == ["estado"]:
                return self._json({
                    "estado": motor.estado,
                    "error": motor.error,
                    "dispositivo": motor.dispositivo,
                    "gpu": motor.gpu,
                    "idiomas": motor.idiomas,
                    "progreso": motor.progreso,
                    "simulado": motor.simular,
                    "whisper": whisper_instalado(motor),
                    "progreso_tr": motor.progreso_tr,
                    "progreso_voz": motor.progreso_voz,
                    "motores_voces": voces.estado(),
                })
            if p == ["disenar"] and metodo == "POST":
                return self._json(api_disenar(motor, voces, self._json_cuerpo()))
            if p == ["pruebas"] and metodo == "GET":
                return self._json(api_pruebas())
            if len(p) == 2 and p[0] == "pruebas":
                wav, _ = prueba(p[1].removesuffix(".wav"))
                if metodo == "GET":
                    return self._archivo(wav, descarga=q.get("descargar") == "1")
                if metodo == "DELETE":
                    wav.unlink(missing_ok=True)
                    (PRUEBAS / f"{wav.stem}.json").unlink(missing_ok=True)
                    return self._json({"ok": True})
            if len(p) == 3 and p[0] == "pruebas" and p[2] == "guardar" and metodo == "POST":
                return self._json(api_guardar_prueba(p[1], self._json_cuerpo()))
            if p == ["transcribir"] and metodo == "POST":
                return self._json(api_transcribir(motor, q, self._cuerpo(MAX_SUBIDA_AUDIO_LARGO), voces))
            if p == ["transcripciones"] and metodo == "GET":
                return self._json(api_transcripciones())
            if len(p) == 2 and p[0] == "transcripciones" and metodo == "DELETE":
                shutil.rmtree(carpeta_transcripcion(p[1]), ignore_errors=True)
                return self._json({"ok": True})
            if len(p) == 3 and p[0] == "transcripciones" and metodo == "GET":
                c = carpeta_transcripcion(p[1])
                if not FORMATOS_TRANSCRIPCION.match(p[2]) or not (c / p[2]).exists():
                    raise ErrorApi(404, "No existe.")
                return self._archivo(c / p[2], descarga=q.get("descargar") == "1")
            if p == ["voces"]:
                if metodo == "GET":
                    return self._json(leer_voces())
                if metodo == "POST":
                    return self._json(api_subir_voz(q.get("nombre", ""), self._cuerpo()))
            if len(p) == 2 and p[0] == "voces" and metodo == "DELETE":
                return self._json(api_borrar_voz(p[1]))
            if len(p) == 3 and p[0] == "voces" and p[2] == "audio" and metodo == "GET":
                return self._archivo(VOCES / buscar_voz(p[1])["archivo"])
            if p == ["generar"] and metodo == "POST":
                return self._json(api_generar(motor, self._json_cuerpo(), voces))
            if p == ["trabajos"] and metodo == "GET":
                return self._json(api_trabajos())
            if len(p) == 2 and p[0] == "trabajos" and metodo == "DELETE":
                shutil.rmtree(carpeta_trabajo(p[1]), ignore_errors=True)
                return self._json({"ok": True})
            if len(p) == 3 and p[0] == "trabajos" and metodo == "GET":
                if p[2] == "zip":
                    return self._bytes(zip_trabajo(p[1]), "application/zip", f"voz-{p[1]}.zip")
                c = carpeta_trabajo(p[1])
                if not WAV_RE.match(p[2]) or not (c / p[2]).exists():
                    raise ErrorApi(404, "No existe.")
                return self._archivo(c / p[2], descarga=q.get("descargar") == "1")
            raise ErrorApi(404, "Ruta de la API no encontrada.")

        def _estatico(self, ruta: str) -> None:
            raiz = STATIC.resolve()
            destino = (STATIC / (ruta.lstrip("/") or "index.html")).resolve()
            if destino != raiz and raiz not in destino.parents:
                raise ErrorApi(404, "No encontrado.")
            if destino.is_dir():
                destino = destino / "index.html"
            if not destino.is_file():
                raise ErrorApi(404, "No encontrado.")
            self._archivo(destino)

    return Handler


class ServidorExclusivo(ThreadingHTTPServer):
    """En Windows, Python deja que dos servidores escuchen en el mismo puerto y el navegador
    acaba hablando con el antiguo. Así, si el puerto está ocupado, falla y se avisa."""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    ap = argparse.ArgumentParser(description="Estudio de voz local")
    ap.add_argument("--puerto", type=int, default=7860)
    ap.add_argument("--red", action="store_true", help="Escuchar en la red local (otros equipos de casa)")
    ap.add_argument("--dispositivo", choices=["auto", "cuda", "cpu", "mps"], default="auto")
    ap.add_argument("--simular", action="store_true", help="Sin modelo: genera tonos para probar la interfaz")
    ap.add_argument("--no-abrir", action="store_true", help="No abrir el navegador al arrancar")
    args = ap.parse_args()

    VOCES.mkdir(exist_ok=True)
    SALIDAS.mkdir(exist_ok=True)
    TRANSCRIPCIONES.mkdir(exist_ok=True)
    PRUEBAS.mkdir(exist_ok=True)
    mimetypes.add_type("application/javascript", ".js")
    for ext, tipo in ((".srt", "text/plain"), (".vtt", "text/vtt"), (".txt", "text/plain")):
        mimetypes.add_type(tipo, ext)

    motor = Motor(args.dispositivo, args.simular)
    threading.Thread(target=motor.cargar, daemon=True).start()
    voces = GestorVoces(motor, args.simular, args.dispositivo)

    host = "0.0.0.0" if args.red else "127.0.0.1"
    try:
        servidor = ServidorExclusivo((host, args.puerto), crear_handler(motor, voces))
    except OSError:
        print(f"\n  [!] El puerto {args.puerto} ya está en uso: hay otro Estudio de voz abierto (quizá una")
        print("      versión anterior en segundo plano). Cierra esa ventana negra, o ejecuta:")
        print("        taskkill /f /im python.exe")
        print("      y vuelve a abrir iniciar.bat.  (También puedes usar otro puerto: iniciar.bat --puerto 7861)\n")
        for t in voces.trabajadores.values():
            t.parar()
        sys.exit(1)
    url = f"http://127.0.0.1:{args.puerto}"
    print(f"\n  Estudio de voz (con pestaña Voces)  ->  {url}")
    if args.red:
        print(f"  Desde otro equipo de tu red  ->  http://{ip_local()}:{args.puerto}")
    print("  (Ctrl+C para parar)\n")
    if not args.no_abrir:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nHasta luego.")
        for t in voces.trabajadores.values():
            t.parar()
        servidor.server_close()


if __name__ == "__main__":
    main()
