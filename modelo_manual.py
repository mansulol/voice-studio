"""
Descarga manual del modelo de Chatterbox.

Si la descarga automática es muy lenta, bájate los archivos tú (con el navegador
o un gestor de descargas que permita reanudar) y ponlos en:

    clonador-voz\\modelos\\chatterbox\\

El servidor los usará directamente, sin descargar nada.

    .venv\\Scripts\\python modelo_manual.py              # lista de archivos y enlaces
    .venv\\Scripts\\python modelo_manual.py --descargar  # descarga rápida con aria2 (16 conexiones)
    .venv\\Scripts\\python modelo_manual.py --comprobar  # revisa la carpeta
"""

from __future__ import annotations

import argparse
import inspect
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CARPETA = BASE / "modelos" / "chatterbox"
REPO = "ResembleAI/chatterbox"

# Por si no se puede leer de la librería instalada (versión 0.1.4 y posteriores)
RESERVA = ["ve.pt", "t3_mtl23ls_v2.safetensors", "s3gen.pt", "grapheme_mtl_merged_expanded_v1.json", "conds.pt", "Cangjie5_TC.json"]


def archivos_necesarios() -> list[str]:
    """Lee de la versión instalada de chatterbox qué archivos descarga."""
    try:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        fuente = inspect.getsource(ChatterboxMultilingualTTS.from_pretrained)
        m = re.search(r"allow_patterns\s*=\s*\[([^\]]*)\]", fuente)
        if m:
            nombres = re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
            if nombres and not any("{" in n or "*" in n for n in nombres):
                return nombres
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] No he podido leerlo de la librería ({e}); uso la lista conocida.\n")
    return RESERVA


def tamanos(nombres: list[str]) -> dict[str, int]:
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(REPO, files_metadata=True)
        return {s.rfilename: (s.size or 0) for s in info.siblings if s.rfilename in nombres}
    except Exception:
        return {}


def fmt(n: int) -> str:
    return f"{n / 1024**3:.2f} GB" if n > 1024**3 else f"{n / 1024**2:.0f} MB" if n > 1024**2 else f"{n / 1024:.0f} KB"


def descargar_con_aria2(nombres: list[str]) -> None:
    """Descarga todos los archivos por 16 conexiones a la vez, reanudando si se corta."""
    aria2 = shutil.which("aria2c")
    if not aria2:
        sys.exit(
            "[!] No encuentro aria2. Instálalo con este comando y abre una terminal nueva:\n"
            "      winget install aria2.aria2\n"
            "    Luego vuelve a ejecutar:  .venv\\Scripts\\python modelo_manual.py --descargar"
        )
    lista = CARPETA / "descargas-aria2.txt"
    lista.write_text(
        "".join(f"https://huggingface.co/{REPO}/resolve/main/{n}?download=true\n  out={n}\n" for n in nombres),
        encoding="utf-8",
    )
    print(f"Descargando {len(nombres)} archivos en {CARPETA} con 16 conexiones por archivo…\n")
    r = subprocess.run([
        aria2, "-i", str(lista), "-d", str(CARPETA),
        "-x", "16", "-s", "16", "-k", "1M", "-j", "2",
        "-c", "--file-allocation=none", "--max-tries=0", "--retry-wait=3",
        "--summary-interval=0", "--console-log-level=warn",
    ])
    lista.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit("\n[!] aria2 ha terminado con errores. Vuelve a ejecutar el mismo comando: sigue donde lo dejó.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comprobar", action="store_true", help="Solo revisa qué falta en la carpeta")
    ap.add_argument("--descargar", action="store_true", help="Descarga rápida con aria2 (16 conexiones)")
    args = ap.parse_args()

    CARPETA.mkdir(parents=True, exist_ok=True)
    nombres = archivos_necesarios()
    if args.descargar:
        descargar_con_aria2(nombres)
    pesos = {} if args.comprobar else tamanos(nombres)

    if not args.comprobar:
        print("Tu versión de Chatterbox necesita estos archivos. Descárgalos y guárdalos")
        print("con este nombre exacto en:")
        print(f"  {CARPETA}\n")
        total = 0
        for n in nombres:
            p = pesos.get(n, 0)
            total += p
            print(f"  {n:<42} {fmt(p) if p else '':>9}")
            print(f"    https://huggingface.co/{REPO}/resolve/main/{n}?download=true")
        if total:
            print(f"\n  Total: {fmt(total)}")
        print()

    faltan = []
    for n in nombres:
        f = CARPETA / n
        if not f.exists() or f.stat().st_size == 0:
            faltan.append(n)
        elif pesos.get(n) and f.stat().st_size != pesos[n]:
            faltan.append(f"{n} (incompleto: {fmt(f.stat().st_size)} de {fmt(pesos[n])})")
    if faltan:
        print("Faltan en la carpeta:")
        for n in faltan:
            print(f"  - {n}")
        sys.exit(1)
    print("✓ Están todos. Al abrir iniciar.bat se cargará el modelo desde esta carpeta, sin descargar nada.")


if __name__ == "__main__":
    main()
