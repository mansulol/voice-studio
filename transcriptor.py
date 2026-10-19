"""
Transcriptor con WhisperX. Se ejecuta en su propio entorno (.venv-whisper) como
proceso aparte, así no choca con las versiones de Chatterbox y al terminar
libera toda la memoria de la GPU.

El servidor lo lanza así:
    python transcriptor.py --audio entrada.wav --salida carpeta/ --entrada auto
                           --idioma-salida es --modelo equilibrado --palabras

Comunica el progreso por la salida estándar con líneas que empiezan por "@@".
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import wave
from pathlib import Path

MODELOS = {"rapido": "small", "equilibrado": "large-v3-turbo", "preciso": "large-v3"}


def emitir(**datos) -> None:
    print("@@" + json.dumps(datos, ensure_ascii=False), flush=True)


def etapa(texto: str) -> None:
    emitir(tipo="progreso", etapa=texto)


# ───────────────────────────── Formatos ─────────────────────────────

def _ts(seg: float, sep: str) -> str:
    seg = max(0.0, seg)
    h, rem = divmod(int(seg), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((seg - int(seg)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def a_srt(segs: list[dict]) -> str:
    return "\n".join(
        f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text'].strip()}\n" for i, s in enumerate(segs, 1)
    )


def a_vtt(segs: list[dict]) -> str:
    return "WEBVTT\n\n" + "\n".join(
        f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text'].strip()}\n" for s in segs
    )


def a_txt(segs: list[dict]) -> str:
    return "\n".join(s["text"].strip() for s in segs if s["text"].strip()) + "\n"


def guardar(carpeta: Path, base: str, segs: list[dict]) -> list[str]:
    archivos = []
    for ext, fn in (("txt", a_txt), ("srt", a_srt), ("vtt", a_vtt)):
        (carpeta / f"{base}.{ext}").write_text(fn(segs), encoding="utf-8")
        archivos.append(f"{base}.{ext}")
    return archivos


# ───────────────────────────── Transcripción ─────────────────────────────

def leer_wav(ruta: Path):
    import numpy as np

    with wave.open(str(ruta), "rb") as w:
        if w.getsampwidth() != 2:
            raise RuntimeError("Se esperaba WAV de 16 bits.")
        sr, ch = w.getframerate(), w.getnchannels()
        datos = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        datos = datos.reshape(-1, ch).mean(axis=1)
    if sr != 16000:  # la interfaz ya manda 16 kHz; esto es por si acaso
        n = int(len(datos) * 16000 / sr)
        datos = np.interp(np.linspace(0, len(datos) - 1, n), np.arange(len(datos)), datos).astype(np.float32)
    return datos


def liberar() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def transcribir(audio, idioma: str | None, modelo: str, palabras: bool, dispositivo: str) -> tuple[list[dict], str]:
    import whisperx

    tipo = "int8_float16" if dispositivo == "cuda" else "int8"
    etapa(f"Cargando Whisper ({MODELOS[modelo]}) en {dispositivo.upper()}…")
    model = whisperx.load_model(MODELOS[modelo], dispositivo, compute_type=tipo, language=idioma)
    etapa("Transcribiendo…")
    res = model.transcribe(audio, batch_size=8 if dispositivo == "cuda" else 4, language=idioma)
    lang = res.get("language") or idioma or "es"
    segs = res["segments"]
    del model
    liberar()

    if palabras and segs:
        etapa("Ajustando los tiempos de cada palabra…")
        try:
            model_a, meta = whisperx.load_align_model(language_code=lang, device=dispositivo)
            alin = whisperx.align(segs, model_a, meta, audio, dispositivo, return_char_alignments=False)
            segs = alin["segments"]
            del model_a
            liberar()
        except Exception as e:  # noqa: BLE001
            emitir(tipo="aviso", mensaje=f"No se pudieron alinear las palabras ({e}); se usan tiempos por frase.")
    return segs, lang


def es_error_gpu(e: Exception) -> bool:
    m = str(e).lower()
    return any(k in m for k in ("out of memory", "cuda", "cudnn", "cublas"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--salida", required=True)
    ap.add_argument("--entrada", default="auto", help="auto | es | en | …")
    ap.add_argument("--idioma-salida", default="igual", help="igual | es | en")
    ap.add_argument("--modelo", default="equilibrado", choices=list(MODELOS))
    ap.add_argument("--palabras", action="store_true")
    ap.add_argument("--dispositivo", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--simular", action="store_true")
    args = ap.parse_args()

    carpeta = Path(args.salida)
    carpeta.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    idioma = None if args.entrada == "auto" else args.entrada

    try:
        if args.simular:
            etapa("Transcribiendo (simulado)…")
            time.sleep(1.2)
            with wave.open(args.audio, "rb") as w:
                dur = w.getnframes() / w.getframerate()
            lang = idioma or "es"
            frases = ["Esto es una transcripción de prueba.", "Aquí iría lo que dices en el audio.",
                      "Cada frase lleva sus tiempos."] if lang == "es" else \
                     ["This is a test transcription.", "Here goes what you say in the audio.",
                      "Each sentence has its timing."]
            paso = max(dur / len(frases), 0.5)
            segs = []
            for i, f in enumerate(frases):
                ini = i * paso
                ws = f.split()
                words = [{"word": w_, "start": round(ini + j * paso / len(ws), 3),
                          "end": round(ini + (j + 1) * paso / len(ws), 3), "score": 0.9} for j, w_ in enumerate(ws)]
                segs.append({"start": round(ini, 3), "end": round(ini + paso, 3), "text": f, "words": words})
            dispositivo = "simulado"
        else:
            import torch

            dispositivo = args.dispositivo
            if dispositivo == "auto":
                dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
                if dispositivo == "cuda" and torch.cuda.get_device_properties(0).total_memory / 1024**3 < 3:
                    emitir(tipo="aviso", mensaje="La tarjeta gráfica tiene poca memoria: transcribo con la CPU.")
                    dispositivo = "cpu"
            etapa("Leyendo el audio…")
            audio = leer_wav(Path(args.audio))
            try:
                segs, lang = transcribir(audio, idioma, args.modelo, args.palabras, dispositivo)
            except Exception as e:  # noqa: BLE001
                if dispositivo != "cuda" or not es_error_gpu(e):
                    raise
                liberar()
                emitir(tipo="aviso", mensaje="La GPU no ha podido (memoria o CUDA): sigo con la CPU, irá más lento.")
                dispositivo = "cpu"
                segs, lang = transcribir(audio, idioma, args.modelo, args.palabras, "cpu")

        limpios = []
        for s in segs:
            item = {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)), "text": str(s.get("text", "")).strip()}
            if s.get("words"):
                item["words"] = [
                    {k: (round(float(v), 3) if k in ("start", "end", "score") else v)
                     for k, v in w.items() if k in ("word", "start", "end", "score") and v is not None}
                    for w in s["words"]
                ]
            limpios.append(item)

        archivos = guardar(carpeta, "original", limpios)
        resultado = {
            "idioma_detectado": lang,
            "dispositivo": dispositivo,
            "segmentos": limpios,
            "texto": a_txt(limpios).strip(),
        }

        destino = lang if args.idioma_salida == "igual" else args.idioma_salida
        if destino != lang:
            etapa(f"Traduciendo de {lang} a {destino}…")
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from traductor import traducir_lista

            disp_tr = "cuda" if dispositivo == "cuda" else "cpu"
            textos = traducir_lista([s["text"] for s in limpios], lang, destino, dispositivo=disp_tr,
                                    simular=args.simular)
            trad = [{"start": s["start"], "end": s["end"], "text": t} for s, t in zip(limpios, textos)]
            archivos += guardar(carpeta, "traduccion", trad)
            resultado["traduccion"] = {"idioma": destino, "segmentos": trad, "texto": a_txt(trad).strip()}

        (carpeta / "transcripcion.json").write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
        archivos.append("transcripcion.json")
        emitir(tipo="fin", archivos=archivos, idioma=lang, destino=destino,
               segundos=round(time.time() - t0, 1), dispositivo=dispositivo)
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        emitir(tipo="error", mensaje=str(e) or e.__class__.__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
