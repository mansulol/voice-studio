"""
Traducción local con MarianMT (Helsinki-NLP/opus-mt-*). Funciona sin internet una
vez descargado cada modelo (~300 MB por par de idiomas).
Lo usan tanto el servidor (clonar voz) como el transcriptor.
"""

from __future__ import annotations

import re
import threading
from typing import Callable, Optional

_cache: dict[str, tuple] = {}
_lock = threading.Lock()

NOMBRES = {"es": "español", "en": "inglés", "fr": "francés", "de": "alemán", "it": "italiano", "pt": "portugués"}


def _modelo(origen: str, destino: str, dispositivo: str):
    clave = f"{origen}-{destino}-{dispositivo}"
    if clave in _cache:
        return _cache[clave]
    from transformers import MarianMTModel, MarianTokenizer

    nombre = f"Helsinki-NLP/opus-mt-{origen}-{destino}"
    try:
        tok = MarianTokenizer.from_pretrained(nombre)
        model = MarianMTModel.from_pretrained(nombre).to(dispositivo).eval()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"No hay traductor de {NOMBRES.get(origen, origen)} a {NOMBRES.get(destino, destino)} "
            f"({nombre}). Detalle: {e}"
        )
    _cache[clave] = (tok, model)
    return tok, model


def _frases(texto: str) -> list[str]:
    return [f for f in re.split(r"(?<=[.!?…])\s+", texto.strip()) if f]


def traducir_lista(
    textos: list[str],
    origen: str,
    destino: str,
    dispositivo: str = "cpu",
    simular: bool = False,
    progreso: Optional[Callable[[int, int], None]] = None,
) -> list[str]:
    """Traduce cada elemento de la lista conservando el orden (y los vacíos)."""
    if origen == destino or not textos:
        return list(textos)
    if simular:
        return [f"[{destino.upper()}] {t}" if t.strip() else t for t in textos]

    import torch

    with _lock:
        tok, model = _modelo(origen, destino, dispositivo)
        # Cada texto se parte en frases para no superar el límite del modelo
        piezas: list[tuple[int, str]] = []
        for i, t in enumerate(textos):
            for f in _frases(t):
                piezas.append((i, f))
        traducidas: list[str] = []
        lote = 8
        for k in range(0, len(piezas), lote):
            grupo = [p[1] for p in piezas[k:k + lote]]
            enc = tok(grupo, return_tensors="pt", padding=True, truncation=True, max_length=512).to(dispositivo)
            with torch.inference_mode():
                gen = model.generate(**enc, max_new_tokens=512, num_beams=4)
            traducidas += tok.batch_decode(gen, skip_special_tokens=True)
            if progreso:
                progreso(min(k + lote, len(piezas)), len(piezas))
        salida = [""] * len(textos)
        for (i, _), tr in zip(piezas, traducidas):
            salida[i] = f"{salida[i]} {tr}".strip()
        return salida


def traducir_texto(texto: str, origen: str, destino: str, **kw) -> str:
    """Traduce un texto respetando los saltos de línea."""
    lineas = texto.split("\n")
    return "\n".join(traducir_lista(lineas, origen, destino, **kw))
