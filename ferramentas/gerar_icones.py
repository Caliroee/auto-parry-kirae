# -*- coding: utf-8 -*-
"""Gera os PNGs derivados e o ICO do Windows a partir do ícone RcM ORIGINAL.

O arquivo assets/rcm_icon.png (70x70) NUNCA é modificado: todas as outras
versões são apenas redimensionamentos dele (LANCZOS), sem redesenho e sem
alteração de cores. Observação honesta: como a origem tem 70 px, as camadas
de 128 e 256 px do ICO são ampliações; para um resultado mais nítido nelas,
substitua assets/rcm_icon.png por uma versão maior do MESMO desenho e rode
este script novamente.

Uso:  py ferramentas/gerar_icones.py
"""
import io
import os
import struct

from PIL import Image

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(RAIZ, "assets")
ORIGINAL = os.path.join(ASSETS, "rcm_icon.png")
TAMANHOS = (16, 24, 32, 48, 64, 128, 256)
LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def png_bytes(imagem):
    buf = io.BytesIO()
    imagem.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def main():
    with Image.open(ORIGINAL) as arq:
        original = arq.convert("RGBA")
    camadas = {}
    for t in TAMANHOS:
        derivada = original.resize((t, t), LANCZOS)
        camadas[t] = png_bytes(derivada)
        with open(os.path.join(ASSETS, f"rcm_icon_{t}.png"), "wb") as f:
            f.write(camadas[t])

    # ICO manual: cada camada é um PNG embutido (suportado desde o Windows Vista).
    cabecalho = struct.pack("<HHH", 0, 1, len(TAMANHOS))
    entradas, dados = b"", b""
    deslocamento = 6 + 16 * len(TAMANHOS)
    for t in TAMANHOS:
        blob = camadas[t]
        entradas += struct.pack("<BBBBHHII", 0 if t >= 256 else t, 0 if t >= 256 else t,
                                0, 0, 1, 32, len(blob), deslocamento)
        dados += blob
        deslocamento += len(blob)
    with open(os.path.join(ASSETS, "rcm_icon.ico"), "wb") as f:
        f.write(cabecalho + entradas + dados)
    print("Gerados:", ", ".join(f"rcm_icon_{t}.png" for t in TAMANHOS), "e rcm_icon.ico")


if __name__ == "__main__":
    main()
