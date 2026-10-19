"""
Motor de la pestaña «Voces»: crea voces nuevas sin grabar a nadie.

  · kokoro → Kokoro-82M. Voces listas en inglés (americano y británico).
             Modelo pequeño: funciona bien con la CPU. Licencia Apache 2.0.
  · qwen   → Qwen3-TTS 1.7B VoiceDesign. Inventa una voz a partir de una
             descripción («hombre de 40 años, grave, narrador…»). Es el que
             mejor habla español. Licencia Apache 2.0.

Cada motor vive en su propio entorno (.venv-kokoro, .venv-qwen) para no chocar
con las versiones de Chatterbox. El servidor lo arranca una vez y le manda
peticiones por la entrada estándar, una por línea en JSON; así el modelo se
queda cargado entre prueba y prueba:

    {"texto": "...", "idioma": "es", "salida": "pruebas/x.wav",
     "voz": "af_heart"            (kokoro)
     "instruccion": "...", "semilla": 7}   (qwen)

Responde con líneas que empiezan por "@@" (progreso, fin o error).

    python voces_motor.py --motor kokoro
    python voces_motor.py --motor qwen --dispositivo cpu
    python voces_motor.py --motor qwen --simular   # sin modelo, para probar
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
import wave
from pathlib import Path

QWEN_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
KOKORO_REPO = "hexgrad/Kokoro-82M"
IDIOMA_QWEN = {"es": "Spanish", "en": "English"}
GB_LIBRES_QWEN = 5.0  # VRAM libre necesaria para Qwen en la GPU


def emitir(**datos) -> None:
    print("@@" + json.dumps(datos, ensure_ascii=False), flush=True)


def etapa(texto: str) -> None:
    emitir(tipo="progreso", etapa=texto)


def ram_gb() -> float:
    """Memoria RAM total del equipo (GB); 0 si no se puede saber."""
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("c", ctypes.c_ulong), ("t", ctypes.c_ulonglong),
                            ("a", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]

            m = M()
            m.l = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.t / 1024**3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except Exception:
        return 0.0


# ───────────────────────────── Audio ─────────────────────────────

def pulir(muestras, sr: int):
    """Quita silencios de los extremos y normaliza el volumen (−1 dBFS de pico)."""
    import numpy as np

    x = np.asarray(muestras, dtype=np.float32).reshape(-1)
    if not x.size:
        return x
    umbral = max(0.01, float(np.abs(x).max()) * 0.02)
    voz = np.where(np.abs(x) > umbral)[0]
    if voz.size:
        margen = int(0.12 * sr)
        x = x[max(0, voz[0] - margen): min(x.size, voz[-1] + margen)]
    pico = float(np.abs(x).max()) if x.size else 0.0
    if pico > 0:
        x = x * (0.89 / pico)
    return x


def escribir_wav(ruta: Path, muestras, sr: int) -> float:
    import numpy as np

    x = np.clip(np.asarray(muestras, dtype=np.float32), -1, 1)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return x.size / sr


# ───────────────────────────── Motores ─────────────────────────────

class Simulado:
    sr = 24000
    dispositivo = "simulado"

    def generar(self, p: dict):
        time.sleep(1.0)
        import numpy as np

        n = int(self.sr * max(1.5, len(p["texto"]) / 14))
        t = np.arange(n) / self.sr
        voz, instr = p.get("voz") or "", (p.get("instruccion") or "").lower()
        grave = (len(voz) > 1 and voz[1] == "m") or (" male" in f" {instr}" and "female" not in instr)
        base = 140 if grave else 230
        return 0.3 * np.sin(2 * math.pi * base * t) * np.sin(math.pi * t / t[-1]) * (0.6 + 0.4 * np.sin(7 * t))


class Kokoro:
    sr = 24000

    def __init__(self, dispositivo: str) -> None:
        import torch
        from kokoro import KPipeline

        self.torch = torch
        self.KPipeline = KPipeline
        if dispositivo == "auto":
            dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
        self.dispositivo = dispositivo
        self.tuberias: dict[str, object] = {}
        etapa("Cargando Kokoro…")
        self._tuberia("a")  # inglés americano: carga el modelo una vez

    def _tuberia(self, letra: str):
        if letra not in self.tuberias:
            # Las tuberías comparten el mismo modelo: solo cambia la pronunciación
            modelo = next((t.model for t in self.tuberias.values()), True)  # type: ignore[attr-defined]
            self.tuberias[letra] = self.KPipeline(lang_code=letra, repo_id=KOKORO_REPO, model=modelo,
                                                  device=self.dispositivo)
        return self.tuberias[letra]

    def generar(self, p: dict):
        import numpy as np

        voz = p["voz"]
        tub = self._tuberia(voz[0])
        etapa(f"Generando con {voz}…")
        trozos = []
        for r in tub(p["texto"], voice=voz, speed=float(p.get("velocidad", 1.0)), split_pattern=r"\n+"):
            audio = getattr(r, "audio", None)
            if audio is None and isinstance(r, tuple):
                audio = r[-1]
            if audio is not None:
                trozos.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio))
                trozos.append(np.zeros(int(0.18 * self.sr), dtype=np.float32))
        if not trozos:
            raise RuntimeError("Kokoro no ha devuelto audio.")
        return np.concatenate(trozos)


class Qwen:
    """Qwen3-TTS VoiceDesign. Tres modos según la memoria de la gráfica:
    · cuda  → todo en la GPU (≈ 5 GB libres)
    · mixto → «offloading»: en la GPU lo que cabe; el resto de capas se guarda en la RAM
              y se pasa a la GPU por turnos para ejecutarse. Mucho más rápido que la CPU.
    · cpu   → todo en el procesador (lo más lento)
    """

    def __init__(self, dispositivo: str) -> None:
        import torch
        from qwen_tts import Qwen3TTSModel

        self.torch = torch
        self.Qwen3TTSModel = Qwen3TTSModel
        modo = dispositivo
        libre = 0.0
        if torch.cuda.is_available():
            libre = torch.cuda.mem_get_info()[0] / 1024**3
        if dispositivo in ("auto", "cuda"):
            if not torch.cuda.is_available():
                modo = "cpu"
            elif dispositivo == "auto" or libre < GB_LIBRES_QWEN:
                modo = "cuda" if libre >= GB_LIBRES_QWEN else "mixto" if libre >= 1.5 else "cpu"
        if modo == "mixto":
            emitir(tipo="aviso", mensaje=f"La gráfica tiene {libre:.1f} GB libres: cargo en ella lo que cabe y el resto "
                                         "va a la RAM (offloading). Algo más lento que todo en la GPU, mucho más rápido que la CPU.")
        elif modo == "cpu" and torch.cuda.is_available():
            emitir(tipo="aviso", mensaje=f"La gráfica solo tiene {libre:.1f} GB libres: diseño con la CPU (lento).")
        try:
            self._cargar(modo, libre)
        except Exception as e:  # noqa: BLE001
            if modo == "cpu" or not es_error_gpu(e):
                raise
            siguiente = "mixto" if modo == "cuda" else "cpu"
            emitir(tipo="aviso", mensaje=f"No ha cabido en la GPU ({modo}); pruebo en modo {siguiente}.")
            self._liberar()
            self._cargar(siguiente, torch.cuda.mem_get_info()[0] / 1024**3)

    def _liberar(self) -> None:
        import gc

        self.model = None
        gc.collect()
        try:
            self.torch.cuda.empty_cache()
        except Exception:
            pass

    def _cargar(self, modo: str, libre: float) -> None:
        torch = self.torch
        if modo == "cuda":
            args = dict(device_map="cuda:0", dtype=torch.bfloat16)
        elif modo == "mixto":
            ram = ram_gb() or 16
            args = dict(
                device_map="auto",
                # Se deja ~0,9 GB de margen en la GPU para los cálculos de cada paso
                max_memory={0: f"{max(0.6, libre - 0.9):.1f}GiB", "cpu": f"{max(6, int(ram * 0.6))}GiB"},
                offload_folder=str(Path(__file__).resolve().parent / "modelos" / "offload"),
                dtype=torch.bfloat16,
            )
        else:
            # En la CPU, float32 es bastante más rápido que bfloat16 pero ocupa el doble (≈ 8 GB)
            args = dict(device_map="cpu", dtype=torch.float32 if ram_gb() >= 14 else torch.bfloat16)
        etapa(f"Cargando Qwen3-TTS VoiceDesign ({'GPU + RAM' if modo == 'mixto' else modo.upper()})… "
              "(la primera vez descarga ≈ 4,5 GB)")
        try:
            self.model = self.Qwen3TTSModel.from_pretrained(QWEN_REPO, attn_implementation="sdpa", **args)
        except TypeError:  # versiones que no aceptan sdpa
            self.model = self.Qwen3TTSModel.from_pretrained(QWEN_REPO, **args)
        self.dispositivo = {"cuda": "cuda", "mixto": "cuda+ram", "cpu": "cpu"}[modo]

    def generar(self, p: dict):
        torch = self.torch
        torch.manual_seed(int(p.get("semilla", 7)))
        aviso = {"cpu": " (con la CPU tarda varios minutos)", "cuda+ram": " (GPU + RAM)"}.get(self.dispositivo, "")
        etapa("Diseñando la voz…" + aviso)
        with torch.inference_mode():
            wavs, sr = self.model.generate_voice_design(
                text=p["texto"], language=IDIOMA_QWEN.get(p["idioma"], "Auto"), instruct=p["instruccion"]
            )
        self.sr = int(sr)
        w = wavs[0]
        return w.detach().cpu().float().numpy() if hasattr(w, "detach") else w


def es_error_gpu(e: Exception) -> bool:
    m = str(e).lower()
    return any(k in m for k in ("out of memory", "cuda", "cudnn", "cublas"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor", choices=["kokoro", "qwen"], required=True)
    ap.add_argument("--dispositivo", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--simular", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # descarga más estable

    try:
        if args.simular:
            motor = Simulado()
        elif args.motor == "kokoro":
            motor = Kokoro(args.dispositivo)
        else:
            motor = Qwen(args.dispositivo)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        emitir(tipo="error", mensaje=f"No se pudo cargar el modelo: {e}", fatal=True)
        sys.exit(1)

    emitir(tipo="listo", dispositivo=motor.dispositivo)
    for linea in sys.stdin:
        linea = linea.strip()
        if not linea:
            continue
        t0 = time.time()
        try:
            p = json.loads(linea)
            muestras = motor.generar(p)
            sr = int(getattr(motor, "sr", 24000))
            dur = escribir_wav(Path(p["salida"]), pulir(muestras, sr), sr)
            emitir(tipo="fin", duracion=round(dur, 2), segundos=round(time.time() - t0, 1),
                   dispositivo=motor.dispositivo)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if es_error_gpu(e):
                try:
                    import torch

                    torch.cuda.empty_cache()
                except Exception:
                    pass
            emitir(tipo="error", mensaje=str(e) or e.__class__.__name__)


if __name__ == "__main__":
    main()
