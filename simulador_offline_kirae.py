# -*- coding: utf-8 -*-
"""Simulador offline de validação do Engine do Auto-Parry Kirae.

Uso:
    py simulador_offline_kirae.py "vesper parry.mov"

Requer ffmpeg no PATH e NumPy. O recorte é 454x480 (dimensões pares), em
x=733, y=318 para o vídeo 1920x1080 fornecido. Cada quadro alimenta o Engine
com t=índice/60, sem enviar teclas ao Windows.
"""

import os
import sys
import math
import subprocess
import numpy as np

from auto_parry_kirae import Engine

LARGURA = 454
ALTURA = 480
ORIGEM_X = 733
ORIGEM_Y = 318
FPS = 60.0
BYTES_QUADRO = LARGURA * ALTURA * 3


def executar(video):
    if not os.path.isfile(video):
        raise FileNotFoundError(f"Vídeo não encontrado: {video}")

    comando = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video,
        "-vf", f"crop={LARGURA}:{ALTURA}:{ORIGEM_X}:{ORIGEM_Y},format=rgb24",
        "-an", "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        processo = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg não foi encontrado. Instale o FFmpeg e adicione-o ao PATH.") from exc
    engine = Engine()
    engine.armed = True
    engine.auto = False       # mantém os 35 ms para avaliar o agendamento inicial
    engine.comp = 0.035

    rodadas = []
    atual = None
    transicoes = []
    indice = 0

    try:
        while True:
            bruto = processo.stdout.read(BYTES_QUADRO)
            if not bruto:
                break
            if len(bruto) != BYTES_QUADRO:
                raise RuntimeError(f"Quadro incompleto no índice {indice}: {len(bruto)} bytes")

            imagem = np.frombuffer(bruto, dtype=np.uint8).reshape((ALTURA, LARGURA, 3))
            agora = indice / FPS
            estado_anterior = engine.state
            instante_tecla = engine.step(imagem, agora)

            if estado_anterior == "IDLE" and engine.state == "TRACK":
                atual = {
                    "numero": len(rodadas) + 1,
                    "t_appear": engine.t_appear,
                    "geom": engine.geom,
                    "series": [],
                    "estimativas": {},
                    "faixa": None,
                    "p_target": None,
                    "disparo": None,
                    "congelado": None,
                }
                rodadas.append(atual)
                transicoes.append((indice, "IDLE", "TRACK"))

            if atual is not None and engine.state in ("TRACK", "FROZEN"):
                if engine.t_appear == atual["t_appear"]:
                    atual["series"].append((agora, float(engine.p)))
                    if engine.band is not None:
                        atual["faixa"] = dict(engine.band)
                        atual["p_target"] = float(engine.p_target)
                    if engine.t_hit is not None:
                        n = len(engine.samples)
                        if n not in atual["estimativas"]:
                            atual["estimativas"][n] = {
                                "tempo": agora,
                                "t_hit": float(engine.t_hit),
                                "omega": float(engine.omega),
                            }

            if instante_tecla is not None and atual is not None and atual["disparo"] is None:
                atual["disparo"] = {
                    "tempo": float(instante_tecla),
                    "t_hit_estimado": float(engine.t_hit),
                    "amostras": len(engine.samples),
                    "comp": float(engine.comp),
                }
                # Simula o agendamento sem enviar ESPAÇO. O ponteiro real do
                # vídeo continua sendo lido, inclusive até seu congelamento.
                engine.notify_pressed(instante_tecla)
                transicoes.append((indice, "TRACK", "FROZEN (simulado)"))

            if atual is not None and engine.freeze_done and atual["congelado"] is None:
                atual["congelado"] = {
                    "tempo": agora,
                    "p": float(engine.p),
                    "erro_graus": float(engine.last_err[0]),
                    "erro_ms": float(engine.last_err[1]),
                }

            if estado_anterior != engine.state:
                if engine.state == "IDLE":
                    transicoes.append((indice, estado_anterior, "IDLE"))
                    atual = None

            indice += 1
    finally:
        if processo.stdout:
            processo.stdout.close()
        erro_ffmpeg = processo.stderr.read().decode("utf-8", errors="replace") if processo.stderr else ""
        codigo = processo.wait()
        if codigo != 0:
            raise RuntimeError(f"ffmpeg retornou código {codigo}: {erro_ffmpeg}")

    # Calcula métricas de trajetória e precisão da estimativa em relação ao
    # instante real inferido por regressão sobre o movimento capturado.
    for rodada in rodadas:
        serie = np.asarray(rodada["series"], dtype=np.float64)
        congelado = rodada["congelado"]
        if len(serie) < 8 or congelado is None or rodada["p_target"] is None:
            rodada["metricas"] = None
            continue

        p_final = congelado["p"]
        movimento = serie[(serie[:, 1] >= 5.0) & (serie[:, 1] <= p_final - 1.0)]
        if len(movimento) < 5:
            movimento = serie[(serie[:, 1] >= 2.0) & (serie[:, 1] <= p_final - 0.5)]
        if len(movimento) < 5:
            rodada["metricas"] = None
            continue

        omega, intercepto = np.polyfit(movimento[:, 0], movimento[:, 1], 1)
        t_hit_real = (rodada["p_target"] - intercepto) / omega

        em_movimento = np.flatnonzero(serie[:, 1] >= 0.8)
        # A regressão fornece a origem temporal da rotação (subquadro),
        # enquanto o vídeo de 60 fps limita a incerteza de observação a ~1 quadro.
        t0_real = t_hit_real - rodada["p_target"] / omega
        hold_estimado = t0_real - rodada["t_appear"]

        disparo = rodada["disparo"]
        if disparo:
            erro_agendamento_ms = (
                disparo["tempo"] + disparo["comp"] - t_hit_real
            ) * 1000.0
            erro_agendamento_graus = erro_agendamento_ms * omega / 1000.0
        else:
            erro_agendamento_ms = erro_agendamento_graus = float("nan")

        erros_estimativa = {}
        for n, estimativa in rodada["estimativas"].items():
            erros_estimativa[n] = (estimativa["t_hit"] - t_hit_real) * 1000.0

        rodada["metricas"] = {
            "omega": float(omega),
            "t_hit_real": float(t_hit_real),
            "hold_estimado": float(hold_estimado),
            "erro_agendamento_ms": float(erro_agendamento_ms),
            "erro_agendamento_graus": float(erro_agendamento_graus),
            "erros_estimativa": erros_estimativa,
        }

    return rodadas, indice, transicoes



def verificar_desligado(video):
    """Confirma que Engine.armed=False não devolve nenhum instante de tecla."""
    comando = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video,
        "-vf", f"crop={LARGURA}:{ALTURA}:{ORIGEM_X}:{ORIGEM_Y},format=rgb24",
        "-an", "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        processo = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg não foi encontrado. Instale o FFmpeg e adicione-o ao PATH.") from exc

    engine = Engine()
    engine.armed = False
    quadro = 0
    rodadas = 0
    prazos_tecla = 0
    tamanho = BYTES_QUADRO
    try:
        while True:
            bruto = processo.stdout.read(tamanho)
            if not bruto:
                break
            if len(bruto) != tamanho:
                raise RuntimeError(f"Quadro incompleto no teste desarmado: {quadro}")
            imagem = np.frombuffer(bruto, dtype=np.uint8).reshape((ALTURA, LARGURA, 3))
            anterior = engine.state
            instante = engine.step(imagem, quadro / FPS)
            if anterior == "IDLE" and engine.state == "TRACK":
                rodadas += 1
            if instante is not None:
                prazos_tecla += 1
            quadro += 1
    finally:
        processo.stdout.close()
        erro = processo.stderr.read().decode("utf-8", errors="replace") if processo.stderr else ""
        codigo = processo.wait()
        if codigo != 0:
            raise RuntimeError(f"ffmpeg retornou código {codigo} no teste desarmado: {erro}")
    return {"quadros": quadro, "rodadas": rodadas, "prazos_tecla": prazos_tecla}


def construir_relatorio(rodadas, quadros, transicoes, video, teste_desligado):
    linhas = []
    linhas.append("VALIDAÇÃO OFFLINE — AUTO-PARRY KIRAE")
    linhas.append("=" * 72)
    linhas.append(f"Vídeo: {os.path.basename(video)}")
    linhas.append(f"Quadros processados: {quadros} a {FPS:.0f} fps ({quadros / FPS:.3f} s)")
    linhas.append(f"Recorte FFmpeg: crop={LARGURA}:{ALTURA}:{ORIGEM_X}:{ORIGEM_Y} (largura/altura pares)")
    linhas.append("Teclas enviadas: nenhuma (somente simulação offline)")
    linhas.append("")
    linhas.append(f"Rodadas detectadas: {len(rodadas)}")
    linhas.append(f"Transições registradas: {len(transicoes)}")
    if rodadas and rodadas[0].get("geom"):
        cx, cy, raio = rodadas[0]["geom"]
        linhas.append(
            f"Geometria derivada no recorte: centro=({cx:.2f}, {cy:.2f}) px, raio={raio:.2f} px; "
            f"no vídeo original: centro=({cx + ORIGEM_X:.2f}, {cy + ORIGEM_Y:.2f}) px."
        )
    linhas.append("Detecção adicional fora das rodadas: 0" if len(rodadas) == 5 else
                  f"Aviso: o total de rodadas difere de 5 ({len(rodadas)} detectadas).")
    linhas.append(
        "Teste com automação DESLIGADA: "
        f"{teste_desligado['rodadas']} rodadas reconhecidas e "
        f"{teste_desligado['prazos_tecla']} prazos de tecla retornados "
        f"(quadros: {teste_desligado['quadros']})."
    )
    linhas.append("")
    linhas.append("Nº | centro branco | largura branca | cinza | omega | hold (intervalo)")
    linhas.append("---|---------------|----------------|-------|-------|----------------")

    todos_zero, todos_1a5, todos_6mais = [], [], []
    erros_agendamento = []
    for r in rodadas:
        faixa = r["faixa"] or {}
        m = r["metricas"]
        if not faixa or not m:
            linhas.append(f"{r['numero']}  | métricas incompletas")
            continue
        hold = f"{m['hold_estimado'] * 1000:.1f} ms*"
        linhas.append(
            f"{r['numero']}  | {faixa['center']:.2f}° | {faixa['width']:.2f}° | "
            f"{faixa['gwidth']:.2f}° | {m['omega']:.2f}°/s | {hold}"
        )

        estimativas = m["erros_estimativa"]
        if 0 in estimativas:
            todos_zero.append(abs(estimativas[0]))
        todos_1a5.extend(abs(v) for n, v in estimativas.items() if 1 <= n <= 5)
        todos_6mais.extend(abs(v) for n, v in estimativas.items() if n >= 6)
        if math.isfinite(m["erro_agendamento_ms"]):
            erros_agendamento.append((m["erro_agendamento_ms"], m["erro_agendamento_graus"]))

    linhas.append("")
    linhas.append("CONVERGÊNCIA DE t_hit (erro absoluto vs. trajetória medida)")
    for rotulo, valores in (
        ("0 amostras", todos_zero),
        ("1 a 5 amostras", todos_1a5),
        ("6 ou mais amostras", todos_6mais),
    ):
        if valores:
            linhas.append(
                f"{rotulo}: máximo {max(valores):.2f} ms; média {sum(valores) / len(valores):.2f} ms"
            )
        else:
            linhas.append(f"{rotulo}: sem medições")

    linhas.append("")
    linhas.append("ERRO DE CONVERGÊNCIA POR RODADA (ms, valor absoluto máximo)")
    linhas.append("Nº | 0 amostras | 1–5 amostras | >=6 amostras")
    linhas.append("---|------------|--------------|-------------")
    for r in rodadas:
        m = r["metricas"]
        if not m:
            continue
        es = m["erros_estimativa"]
        e0 = abs(es[0]) if 0 in es else float("nan")
        e15 = max((abs(v) for n, v in es.items() if 1 <= n <= 5), default=float("nan"))
        e6 = max((abs(v) for n, v in es.items() if n >= 6), default=float("nan"))
        fmt = lambda v: f"{v:.2f}" if math.isfinite(v) else "—"
        linhas.append(f"{r['numero']}  | {fmt(e0)} | {fmt(e15)} | {fmt(e6)}")

    linhas.append("")
    if erros_agendamento:
        max_ms = max(abs(v[0]) for v in erros_agendamento)
        max_graus = max(abs(v[1]) for v in erros_agendamento)
        linhas.append("AGENDAMENTO SIMULADO (t_press + compensação − t_hit real)")
        linhas.append(f"Maior erro absoluto: {max_ms:.3f} ms ({max_graus:.3f}°)")
        linhas.append("A compensação usada foi 35 ms; nenhum evento de teclado foi enviado.")
    else:
        linhas.append("AGENDAMENTO SIMULADO: sem eventos")

    linhas.append("")
    linhas.append("ERRO DE CONGELAMENTO LIDO NO VÍDEO")
    linhas.append("Nº | erro angular | erro temporal")
    linhas.append("---|--------------|--------------")
    for r in rodadas:
        f = r["congelado"]
        if f:
            linhas.append(
                f"{r['numero']}  | {f['erro_graus']:+.2f}° | {f['erro_ms']:+.2f} ms"
            )
        else:
            linhas.append(f"{r['numero']}  | não medido | não medido")

    linhas.append("")
    linhas.append("DETALHES POR RODADA")
    for r in rodadas:
        faixa = r["faixa"] or {}
        m = r["metricas"]
        if not faixa or not m:
            continue
        linhas.append(
            f"Rodada {r['numero']}: centro={faixa['center']:.2f}°, "
            f"branco={faixa['width']:.2f}°, início/posição cinza={faixa['gend']:.2f}°, "
            f"largura cinza={faixa['gwidth']:.2f}°, omega={m['omega']:.2f}°/s, "
            f"t_hit={m['t_hit_real']:.6f}s, amostras no disparo="
            f"{r['disparo']['amostras'] if r['disparo'] else '—'}."
        )

    linhas.append("")
    if rodadas:
        omegas = [r["metricas"]["omega"] for r in rodadas if r.get("metricas")]
        if omegas:
            linhas.append(
                f"Velocidade média ajustada: {sum(omegas) / len(omegas):.2f}°/s "
                f"(desvio de {sum(omegas) / len(omegas) - 325.5:+.2f}°/s vs. 325,5°/s esperado)."
            )
    linhas.append("* Hold estimado pela origem da rotação obtida por regressão linear; a incerteza prática é limitada pela captura de 60 fps (cerca de ±8,3 ms).")
    linhas.append("")
    linhas.append("LIMITAÇÕES")
    linhas.append(
        "A precisão de agendamento é uma simulação matemática baseada no vídeo. "
        "Ela não mede a latência real de pynput/Windows, a latência de apresentação "
        "do navegador nem o jitter da máquina durante uma partida."
    )
    linhas.append(
        "O hold foi interpolado pela regressão da trajetória; como a fonte tem 60 fps, "
        "a incerteza prática dessa interpolação é de aproximadamente um quadro."
    )
    return "\n".join(linhas) + "\n"


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "vesper parry.mov")
    rodadas, quadros, transicoes = executar(video)
    teste_desligado = verificar_desligado(video)
    relatorio = construir_relatorio(rodadas, quadros, transicoes, video, teste_desligado)
    print(relatorio)
    destino = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resultado_testes_kirae.txt")
    with open(destino, "w", encoding="utf-8") as arquivo:
        arquivo.write(relatorio)
    print(f"Relatório salvo em: {destino}")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(exc)
        input("Pressione Enter para sair...")
        raise SystemExit(1)
    except RuntimeError as exc:
        print(exc)
        input("Pressione Enter para sair...")
        raise SystemExit(1)
    except Exception as exc:
        print("Falha na simulação:", exc)
        input("Pressione Enter para sair...")
        raise SystemExit(1)
