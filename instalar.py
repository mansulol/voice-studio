"""
Instala lo necesario. Lo llaman iniciar.bat e instalar-whisper.bat; también puedes
ejecutarlo a mano para actualizar:

    .venv\\Scripts\\python instalar.py              # todo
    .venv\\Scripts\\python instalar.py --solo-voz     # Chatterbox (clonar)
    .venv\\Scripts\\python instalar.py --solo-whisper # WhisperX (transcribir)
    .venv\\Scripts\\python instalar.py --solo-voces   # Kokoro y Qwen3-TTS (pestaña Voces)

Cada modelo pide versiones distintas de PyTorch y Transformers, así que van en
entornos separados:  .venv (Chatterbox), .venv-whisper, .venv-kokoro y .venv-qwen.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
WHISPER_DIR = BASE / ".venv-whisper"
WHISPER_PY = WHISPER_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def entorno(nombre: str) -> str:
    """Crea (si no existe) el entorno .venv-<nombre> y devuelve su python."""
    carpeta = BASE / f".venv-{nombre}"
    py = carpeta / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        print(f"Creando el entorno {carpeta.name}…")
        base_py = getattr(sys, "_base_executable", sys.executable)
        subprocess.check_call([base_py, "-m", "venv", str(carpeta)])
    run(str(py), "-m", "pip", "install", "--upgrade", "pip")
    return str(py)


def run(py: str, *args: str) -> None:
    print("\n> " + " ".join(args))
    subprocess.check_call([py, *args])


def versiones_torch(py: str) -> tuple[str, str, bool]:
    out = subprocess.run(
        [py, "-c", "import torch\ntry:\n import torchaudio; ta = torchaudio.__version__\nexcept ImportError:\n ta = '-'\n"
                   "print(torch.__version__);print(ta);print(torch.cuda.is_available())"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return "", "", False
    t, ta, cuda = out.stdout.strip().splitlines()[-3:]
    return t, ta, cuda == "True"


# Índices de PyTorch con CUDA y la versión de CUDA que piden al driver de NVIDIA
INDICES_CUDA = [("cu130", (13, 0)), ("cu129", (12, 9)), ("cu128", (12, 8)), ("cu126", (12, 6)),
                ("cu124", (12, 4)), ("cu121", (12, 1))]


def cuda_del_driver() -> tuple[int, int] | None:
    """Versión máxima de CUDA que admite el driver instalado (la que muestra nvidia-smi)."""
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=20).stdout
        import re

        m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None


def pip_torch(py: str, indice: str, paquetes: list[str]) -> bool:
    print(f"\n> pip install {' '.join(paquetes)}  (desde {indice})")
    r = subprocess.run([py, "-m", "pip", "install", "--force-reinstall", "--no-deps", *paquetes,
                        "--index-url", f"https://download.pytorch.org/whl/{indice}"])
    return r.returncode == 0


def asegurar_cuda(py: str, nombre: str) -> None:
    """Si hay tarjeta NVIDIA y PyTorch se instaló solo para CPU, lo cambia por la versión CUDA.

    PyPI a veces trae una versión de PyTorch más nueva que las que hay con CUDA (p. ej.
    2.14 frente a 2.11). Primero se busca la misma versión con CUDA; si no existe, se
    instala la más reciente con CUDA que admita tu driver.
    """
    t, ta, cuda = versiones_torch(py)
    if not t:
        sys.exit(f"[!] PyTorch no se ha instalado bien en el entorno de {nombre}.")
    if shutil.which("nvidia-smi") and not cuda:
        tb, tab = t.split("+")[0], ta.split("+")[0]
        driver = cuda_del_driver()
        indices = [i for i, v in INDICES_CUDA if driver is None or v <= driver] or ["cu121"]
        print(f"\n[{nombre}] Tarjeta NVIDIA detectada (driver con CUDA "
              f"{'.'.join(map(str, driver)) if driver else '?'}): instalando PyTorch con CUDA (tarda un rato)…")
        con_audio = tab != "-"
        hecho = False
        for idx in indices:  # 1) la misma versión que ya hay
            if pip_torch(py, idx, [f"torch=={tb}"] + ([f"torchaudio=={tab}"] if con_audio else [])):
                hecho = True
                break
        if not hecho:  # 2) la más reciente con CUDA para tu driver
            print(f"[{nombre}] PyTorch {tb} aún no existe con CUDA: instalo la más reciente que sí.")
            for idx in indices[:3]:
                if pip_torch(py, idx, ["torch"] + (["torchaudio"] if con_audio else [])):
                    hecho = True
                    break
        if not hecho:
            print(f"[aviso] No se pudo instalar PyTorch con CUDA para {nombre}: usará la CPU (más lento).")
    t, _, cuda = versiones_torch(py)
    print(f"[{nombre}] PyTorch {t} · GPU: {'sí' if cuda else 'no (CPU)'}")


def instalar_voz() -> None:
    py = sys.executable
    print("\n══════ Voz (Chatterbox) ══════")
    run(py, "-m", "pip", "install", "--upgrade", "pip")
    run(py, "-m", "pip", "install", "-U", "chatterbox-tts", "sentencepiece", "sacremoses")
    asegurar_cuda(py, "voz")


def instalar_whisper() -> None:
    print("\n══════ Transcripción (WhisperX) ══════")
    if not WHISPER_PY.exists():
        print("Creando el entorno .venv-whisper…")
        base_py = getattr(sys, "_base_executable", sys.executable)
        subprocess.check_call([base_py, "-m", "venv", str(WHISPER_DIR)])
    py = str(WHISPER_PY)
    run(py, "-m", "pip", "install", "--upgrade", "pip")
    run(py, "-m", "pip", "install", "-U", "whisperx", "sentencepiece", "sacremoses")
    asegurar_cuda(py, "transcripción")


def instalar_kokoro() -> None:
    print("\n══════ Voces en inglés (Kokoro-82M) ══════")
    py = entorno("kokoro")
    # Kokoro es pequeño y va rápido con la CPU: PyTorch sin CUDA (descarga mucho menor)
    run(py, "-m", "pip", "install", "torch", "--index-url", "https://download.pytorch.org/whl/cpu")
    run(py, "-m", "pip", "install", "-U", "kokoro", "soundfile")
    try:
        run(py, "-m", "spacy", "download", "en_core_web_sm")
    except subprocess.CalledProcessError:
        print("[aviso] No se pudo bajar el diccionario de inglés; Kokoro lo intentará la primera vez que lo uses.")


def instalar_qwen() -> None:
    print("\n══════ Diseño de voces (Qwen3-TTS VoiceDesign) ══════")
    py = entorno("qwen")
    run(py, "-m", "pip", "install", "-U", "qwen-tts", "soundfile", "accelerate")
    asegurar_cuda(py, "Qwen3-TTS")


def instalar_voces() -> bool:
    ok = True
    for nombre, f in (("Kokoro", instalar_kokoro), ("Qwen3-TTS", instalar_qwen)):
        try:
            f()
        except subprocess.CalledProcessError:
            ok = False
            print(f"\n[aviso] No se pudo instalar {nombre}. Lo demás funciona igual;")
            print("        vuelve a intentarlo más tarde con instalar-voces.bat")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--solo-voz", action="store_true")
    g.add_argument("--solo-whisper", action="store_true")
    g.add_argument("--solo-voces", action="store_true")
    args = ap.parse_args()

    if args.solo_voces:
        ok = instalar_voces()
        print("\n" + "=" * 60)
        print("  Pestaña Voces lista." if ok else "  Instalación de Voces incompleta (mira los avisos).")
        print("=" * 60)
        sys.exit(0 if ok else 1)
    if not args.solo_whisper:
        instalar_voz()
    if not args.solo_voz:
        try:
            instalar_whisper()
        except subprocess.CalledProcessError:
            print("\n[aviso] No se pudo instalar WhisperX. La parte de clonar voz funciona igual;")
            print("        vuelve a intentarlo más tarde con instalar-whisper.bat")
            if args.solo_whisper:
                sys.exit(1)
    if not args.solo_voz and not args.solo_whisper:
        instalar_voces()

    print("\n" + "=" * 60)
    print("  Instalación completa.")
    print("=" * 60)


if __name__ == "__main__":
    main()
