# -*- coding: utf-8 -*-
"""Auto-Parry Kirae v1.1.1 (Vesper) — por RCM/Caliroe — ferramenta experimental para Windows 11.

Mecânica modelada a partir do vídeo medido (1920x1080, 60 fps): o ponteiro
fica vertical por cerca de 473 ms; depois gira no sentido horário a ~325,5°/s.
A faixa de sucesso começa no arco BRANCO (Ótimo, ~10°) e continua no CINZA
(Bom, ~35°). O espaço congela o ponteiro; o erro entre o congelamento e o
centro branco pode alimentar uma compensação automática de latência.

O motor de visão é independente da interface: Engine.step(img_rgb, agora)
recebe um quadro RGB e um timestamp monotônico, e retorna o instante
(time.perf_counter) em que a tecla deveria ser enviada, ou None.

AVISO: automação pode contrariar as regras do jogo/site. Use por sua conta
em risco. A ferramenta não garante resultado nem elimina latência/jitter.
"""

import os
import sys
import json
import time
import math
import threading
import ctypes
from ctypes import wintypes

# DPI awareness precisa ser definido antes de importar/instanciar ferramentas
# que consultam coordenadas de tela (MSS, Tk e pynput).
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    import numpy as np
except ImportError as exc:
    print("\nNão foi possível iniciar: falta a biblioteca NumPy.")
    print("Instale com: py -m pip install mss numpy pillow pynput")
    print("Detalhe:", exc)
    input("\nPressione Enter para fechar...")
    raise SystemExit(1)

# mss/Pillow/pynput são opcionais durante a importação do Engine, mas
# obrigatórios ao executar a interface. Assim o motor também pode ser testado
# offline sem iniciar Tkinter ou capturar a tela.
try:
    import mss
except ImportError:
    mss = None

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = ImageDraw = ImageTk = None

# Tkinter também é opcional na importação: assim o Engine continua testável
# offline (simulador) em ambientes sem interface gráfica.
try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:
    tk = tkfont = None
_Canvas = tk.Canvas if tk is not None else object

# ---------------------------------------------------------------------------
# Constantes medidas no vídeo
# ---------------------------------------------------------------------------
OMEGA0 = 325.5                  # graus por segundo, sentido horário
HOLD0 = 0.473                   # segundos de ponteiro parado
R_OVER_L = 0.88                # raio do anel / comprimento do ponteiro
AMOSTRAS_ANGULARES = 720       # uma amostra a cada 0,5 grau
ANG = np.arange(AMOSTRAS_ANGULARES, dtype=np.float64) * (2.0 * math.pi / AMOSTRAS_ANGULARES)
COS, SIN = np.cos(ANG), np.sin(ANG)
if getattr(sys, "frozen", False):                      # executável do PyInstaller
    BASE_DIR = os.path.dirname(sys.executable)          # configuração fica ao lado do .exe
    RECURSOS_DIR = getattr(sys, "_MEIPASS", BASE_DIR)   # assets embutidos no pacote
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RECURSOS_DIR = BASE_DIR
CONFIG_PATH = os.path.join(BASE_DIR, "auto_parry_config.json")


def mascara_vermelha(im_rgb):
    """Máscara do ponteiro vermelho; a entrada deve ser RGB uint8/int16."""
    im = im_rgb.astype(np.int16, copy=False)
    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    return (
        (r > 170) & (g < 140) & (b > 45)
        & ((r - g) > 80) & ((r - b) > 50)
    )


def detectar_ponteiro_vertical(vermelho):
    """Detecta ponteiro parado às 12h e deriva centro/raio sem calibração.

    Retorna (cx, cy, raio) no sistema de coordenadas do recorte ou None.
    O grupo vertical precisa ter pelo menos 40 linhas e um hub mais largo
    encostado à base, o que rejeita as bolinhas de progresso inferiores.
    """
    if vermelho.ndim != 2 or vermelho.size == 0:
        return None

    contagem_colunas = vermelho.sum(axis=0)
    x0 = int(contagem_colunas.argmax())
    if contagem_colunas[x0] < 40:
        return None

    largura = vermelho.shape[1]
    esquerda, direita = max(0, x0 - 7), min(largura, x0 + 8)
    sub = vermelho[:, esquerda:direita]
    larguras_linha = sub.sum(axis=1)
    ys = np.flatnonzero(larguras_linha > 0)
    if len(ys) < 40:
        return None

    grupos = [[int(ys[0])]]
    for y in ys[1:]:
        if int(y) - grupos[-1][-1] <= 2:
            grupos[-1].append(int(y))
        else:
            grupos.append([int(y)])
    grupo = max(grupos, key=len)
    if len(grupo) < 40:
        return None

    y_topo, y_base = grupo[0], grupo[-1]
    metade_superior = grupo[:max(10, len(grupo) // 2)]
    largura_fina = float(np.median(larguras_linha[metade_superior]))
    hub_linhas = [y for y in grupo if larguras_linha[y] >= largura_fina + 2]
    if not hub_linhas:
        return None

    hub_topo, hub_base = min(hub_linhas), max(hub_linhas)
    if hub_base < y_base - 2:
        return None

    linhas_finas = [y for y in grupo if y < hub_topo - 1]
    if len(linhas_finas) < 30:
        return None

    y0, y1 = linhas_finas[0], linhas_finas[-1]
    yy, xx = np.nonzero(sub[y0:y1 + 1])
    if len(xx) < 30:
        return None

    cx = float(xx.mean() + esquerda)
    cy = (hub_topo + hub_base) / 2.0
    comprimento = cy - y_topo
    if not (20.0 <= comprimento <= 500.0):
        return None

    raio = R_OVER_L * comprimento
    return cx, cy, raio


def angulo_ponteiro(vermelho, geometria):
    """Ângulo do ponteiro: 0° leste, 90° baixo, 180° oeste, 270° cima."""
    cx, cy, raio = geometria
    ys, xs = np.nonzero(vermelho)
    if len(xs) < 12:
        return None

    dx = xs.astype(np.float64) - cx
    dy = ys.astype(np.float64) - cy
    dist = np.hypot(dx, dy)
    valido = (dist >= 0.17 * raio) & (dist <= 1.22 * raio)
    if int(valido.sum()) < 10:
        return None

    return math.degrees(math.atan2(float(dy[valido].sum()), float(dx[valido].sum()))) % 360.0


def trechos_circulares(mascara, intervalo_fechamento=0):
    """Trechos circulares True: lista de (índice inicial, comprimento).

    Une pequenos buracos (por exemplo, o ponteiro passando sobre a faixa).
    """
    n = len(mascara)
    if n == 0 or not np.any(mascara):
        return []
    if np.all(mascara):
        return [(0, n)]

    inicio_falso = int(np.flatnonzero(~mascara)[0])
    m = np.roll(mascara, -inicio_falso).astype(np.int8)
    diff = np.diff(np.concatenate(([0], m, [0])))
    inicios = np.flatnonzero(diff == 1)
    finais = np.flatnonzero(diff == -1)

    unidos = []
    for a, b in zip(inicios, finais):
        if unidos and int(a) - unidos[-1][1] <= intervalo_fechamento:
            unidos[-1][1] = int(b)
        else:
            unidos.append([int(a), int(b)])

    # Junta também um trecho que atravessa 359,5° -> 0°.
    if len(unidos) > 1 and (n - unidos[-1][1]) + unidos[0][0] <= intervalo_fechamento:
        ultimo = unidos.pop()
        unidos[0][0] = ultimo[0] - n

    return [((a + inicio_falso) % n, b - a) for a, b in unidos]


def detectar_faixa(im_rgb, geometria):
    """Encontra arco branco Ótimo seguido imediatamente pelo cinza Bom.

    A cor precisa concordar em dois raios, um por fora e outro por dentro do
    anel, reduzindo falsos positivos causados pelo próprio anel e pelo cursor.
    Retorna centro branco, largura branca e fim/largura cinza em graus.
    """
    cx, cy, raio = geometria
    h, w = im_rgb.shape[:2]
    deslocamento = 0.065 * raio
    classes = []

    for rr in (raio + deslocamento, raio - deslocamento):
        xx = np.clip(np.rint(cx + rr * COS).astype(np.int32), 0, w - 1)
        yy = np.clip(np.rint(cy + rr * SIN).astype(np.int32), 0, h - 1)
        px = im_rgb[yy, xx].astype(np.int16, copy=False)
        minimo = px.min(axis=1)
        maximo = px.max(axis=1)
        branco = minimo >= 205
        cinza = (minimo >= 105) & (maximo <= 205) & ((maximo - minimo) <= 18)
        classes.append((branco, cinza))

    branco = classes[0][0] & classes[1][0]
    cinza = classes[0][1] & classes[1][1]
    brancos = [r for r in trechos_circulares(branco, 8) if 10 <= r[1] <= 36]
    cinzas = [r for r in trechos_circulares(cinza, 8) if r[1] >= 24]

    for inicio_branco, largura_branco in brancos:
        fim_branco = (inicio_branco + largura_branco) % AMOSTRAS_ANGULARES
        for inicio_cinza, largura_cinza in cinzas:
            distancia = (inicio_cinza - fim_branco) % AMOSTRAS_ANGULARES
            if distancia <= 6:  # branco seguido de cinza em até 3 graus
                centro_branco = ((inicio_branco + largura_branco / 2.0) % AMOSTRAS_ANGULARES) * 0.5
                fim_cinza = ((inicio_cinza + largura_cinza) % AMOSTRAS_ANGULARES) * 0.5
                return {
                    "center": float(centro_branco),
                    "width": float(largura_branco * 0.5),
                    "gend": float(fim_cinza),
                    "gwidth": float(largura_cinza * 0.5),
                }
    return None


def diferenca_angular_180(valor):
    """Diferença angular assinada no intervalo [-180°, 180°)."""
    return (valor + 180.0) % 360.0 - 180.0


class Engine:
    """Motor sem dependência de GUI: step(img_rgb, agora) -> t_press ou None."""

    def __init__(self):
        self.comp = 0.035          # latência total estimada em segundos
        self.auto = True
        self.armed = False
        self.dry = False
        self.parrys = 0
        self.last_err = None       # (graus, milissegundos)
        self.msg = "Aguardando a roleta..."
        self.reset()

    def reset(self):
        """Reinicia somente a máquina da rodada; preserva preferências/telemetria."""
        self.state = "IDLE"
        self.geom = None
        self.t_appear = None
        self.base = None
        self.prev_ang = None
        self.p = 0.0
        self.samples = []
        self.last_unique_p = 0.0
        self.t_last_change = None
        self.band = None
        self.cands = []
        self.p_target = None
        self.p_gend = None
        self.band_ang = None
        self.committed = False
        self.late = False
        self.t_press = None
        self.freeze_done = False
        self.lost_since = None
        self.omega = OMEGA0
        self.t_hit = None

    def estimar_t0(self):
        """Estima origem da rotação conforme a quantidade de amostras únicas."""
        if not self.samples:
            return self.t_appear + HOLD0, OMEGA0

        ts = np.asarray([amostra[0] for amostra in self.samples], dtype=np.float64)
        ps = np.asarray([amostra[1] for amostra in self.samples], dtype=np.float64)
        t0_mediana = float(np.median(ts - ps / OMEGA0))
        if len(self.samples) >= 6:
            try:
                a, b = np.polyfit(ts, ps, 1)
                if a > 0 and abs(float(a) - OMEGA0) <= 0.12 * OMEGA0:
                    self.omega = float(a)
                    return float(-b / a), self.omega
            except (ValueError, np.linalg.LinAlgError, FloatingPointError):
                pass
        self.omega = OMEGA0
        return t0_mediana, OMEGA0

    def step(self, img_rgb, agora):
        """Processa um quadro RGB.

        `agora` deve usar a mesma base monotônica de `time.perf_counter()`.
        Retorna o timestamp em que o worker deve enviar ESPAÇO, ou None.
        """
        if img_rgb is None or getattr(img_rgb, "ndim", 0) != 3 or img_rgb.shape[2] < 3:
            return None

        imagem = img_rgb[..., :3]
        vermelho = mascara_vermelha(imagem)

        if self.state == "IDLE":
            geometria = detectar_ponteiro_vertical(vermelho)
            if geometria is None:
                return None
            self.reset()
            self.geom = geometria
            self.t_appear = float(agora)
            self.state = "TRACK"
            self.msg = "Roleta detectada..."

        angulo = angulo_ponteiro(vermelho, self.geom)
        if angulo is None:
            if self.lost_since is None:
                self.lost_since = float(agora)
            elif agora - self.lost_since > 0.25:
                self.reset()
                self.msg = "Aguardando a roleta..."
            return None

        self.lost_since = None
        if self.base is None:
            self.base = angulo
            self.prev_ang = angulo
            self.p = 0.0
        else:
            self.p += diferenca_angular_180(angulo - self.prev_ang)
            self.prev_ang = angulo

        if self.p >= 2.0 and (self.p - self.last_unique_p) >= 1.5:
            if self.state == "TRACK":
                self.samples.append((float(agora), float(self.p)))
            self.last_unique_p = float(self.p)
            self.t_last_change = float(agora)

        if self.state == "TRACK":
            return self._track(imagem, float(agora))
        if self.state == "FROZEN":
            self._frozen(float(agora))
        return None

    def _track(self, imagem, agora):
        if self.band is None:
            faixa = detectar_faixa(imagem, self.geom)
            if faixa is None:
                self.cands = []
            else:
                self.cands.append(faixa)
                self.cands = self.cands[-2:]
                if len(self.cands) == 2:
                    concordancia = abs(diferenca_angular_180(
                        self.cands[-1]["center"] - self.cands[-2]["center"]
                    )) <= 1.5
                    if concordancia:
                        self.band = faixa
                        self.band_ang = faixa["center"]
                        self.p_target = (faixa["center"] - self.base) % 360.0
                        # O fim cinza pode vir depois dos 360° de progresso.
                        self.p_gend = self.p_target + (faixa["gend"] - faixa["center"]) % 360.0
                        self.msg = f"Mirando: faixa ótima a {self.p_target:.0f}° do topo"

        if self.band is None:
            return None

        t0, omega = self.estimar_t0()
        self.t_hit = t0 + self.p_target / omega
        t_fim_cinza = t0 + self.p_gend / omega
        t_previsto = self.t_hit - max(0.0, min(0.2, self.comp))

        # Não compromete uma rodada se a automação estiver desligada.
        if not self.armed or self.committed:
            return None

        # Guarda adicional: se a observação já passou muito do branco, não
        # envia uma tecla tardia apenas para obter o parry cinza de 1 ponto.
        limite_angular = self.p_target + 0.5 * self.band["width"] + 12.0
        if self.p > limite_angular:
            self.committed = True
            self.msg = "Faixa ótima já passou; aguardando a próxima rodada."
            return None

        if agora >= t_fim_cinza - 0.005:
            self.committed = True
            self.msg = "Janela segura encerrada; aguardando a próxima rodada."
            return None

        if agora >= t_previsto - 0.022:
            self.committed = True
            self.late = agora > t_previsto + 0.012
            return max(t_previsto, agora)
        return None

    def notify_pressed(self, instante_registro):
        """Informa ao motor que ESPAÇO foi realmente enviado ao sistema."""
        self.t_press = float(instante_registro)
        self.state = "FROZEN"
        self.t_last_change = float(instante_registro)
        self.msg = "Parry enviado, lendo resultado..."

    def notify_test_prediction(self, instante_previsto):
        """Marca previsão no modo teste sem fingir que o ponteiro congelou."""
        self.committed = True
        self.msg = f"[TESTE] previsão de ESPAÇO em {instante_previsto:.3f}s — tecla não enviada"

    def block_current_round(self, mensagem):
        """Impede tentativas repetidas nesta rodada após um bloqueio de segurança."""
        self.committed = True
        self.msg = mensagem

    def _frozen(self, agora):
        if self.freeze_done or self.t_press is None or self.t_last_change is None:
            return
        if (agora - self.t_last_change >= 0.070) and (agora >= self.t_press + 0.070):
            erro_graus = float(self.p - self.p_target)
            erro_ms = erro_graus / self.omega * 1000.0
            self.last_err = (erro_graus, erro_ms)
            self.parrys += 1
            self.freeze_done = True
            self.msg = f"Parry: {erro_graus:+.1f}° do centro ({erro_ms:+.0f} ms)"
            if self.auto and not self.late and abs(erro_ms) < 120.0:
                self.comp = min(0.2, max(0.0, self.comp + 0.6 * erro_ms / 1000.0))


# ---------------------------------------------------------------------------
# Configuração e utilitários de tempo/captura
# ---------------------------------------------------------------------------
class Shared:
    def __init__(self):
        self.region = None              # (left, top, width, height)
        self.janela_destino = None       # (HWND, PID), válido apenas nesta sessão
        self.running = True
        self.frame = None
        self.fps = 0.0
        self.lock = threading.RLock()


def carregar_config(shared, engine):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as arquivo:
            config = json.load(arquivo)
        regiao = config.get("region")
        if isinstance(regiao, (list, tuple)) and len(regiao) == 4:
            shared.region = tuple(int(v) for v in regiao)
        engine.comp = min(0.2, max(0.0, float(config.get("comp", engine.comp))))
        engine.auto = bool(config.get("auto", engine.auto))
    except FileNotFoundError:
        pass
    except Exception as exc:
        engine.msg = f"Configuração ignorada: {exc}"


def salvar_config(shared, engine):
    try:
        with shared.lock:
            config = {
                "region": list(shared.region) if shared.region else None,
                "comp": float(engine.comp),
                "auto": bool(engine.auto),
            }
        temporario = CONFIG_PATH + ".tmp"
        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(config, arquivo, ensure_ascii=False, indent=2)
        os.replace(temporario, CONFIG_PATH)
    except Exception as exc:
        engine.msg = f"Não foi possível salvar a configuração: {exc}"


def espera_precisa(alvo):
    """Sleep grosso + espera ativa nos ~15 ms finais."""
    while True:
        restante = alvo - time.perf_counter()
        if restante <= 0:
            return
        if restante > 0.015:
            time.sleep(restante - 0.012)
        # Nos últimos 12–15 ms, o laço curto evita oversleep de time.sleep.


def _carregar_user32_foco():
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        return user32
    except (AttributeError, OSError):
        return None


_USER32_FOCO = _carregar_user32_foco()


def _identificar_janela(hwnd):
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    if _USER32_FOCO.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) and pid.value:
        return hwnd, pid.value
    return None


def janela_em_primeiro_plano():
    if _USER32_FOCO is None:
        return None
    try:
        hwnd = _USER32_FOCO.GetForegroundWindow()
        janela = _identificar_janela(hwnd)
        if janela is not None and _USER32_FOCO.GetForegroundWindow() == hwnd:
            return janela
    except (OSError, ctypes.ArgumentError):
        pass
    return None


def janela_no_ponto(x, y):
    if _USER32_FOCO is None:
        return None
    try:
        hwnd = _USER32_FOCO.WindowFromPoint(wintypes.POINT(x, y))
        if hwnd:
            janela = _identificar_janela(_USER32_FOCO.GetAncestor(hwnd, 2))  # GA_ROOT
            if janela is not None and janela[1] != os.getpid():
                return janela
    except (OSError, ctypes.ArgumentError):
        pass
    return None


def _enviar_parry(shared, engine, teclado, tecla_espaco, instante, destino):
    espera_precisa(instante)
    tentou_enviar = False
    try:
        with shared.lock:
            if not (engine.armed and shared.running):
                engine.block_current_round("Automação desligada antes do envio.")
                return
            if engine.dry:
                engine.notify_test_prediction(instante)
                return
            if destino is None or destino is not shared.janela_destino:
                engine.block_current_round("Envio bloqueado: vínculo da janela ausente ou alterado.")
                return
            # A última operação antes do press consulta apenas o Win32, sem acessar o Tk.
            if janela_em_primeiro_plano() != destino:
                engine.block_current_round("Envio bloqueado: a janela vinculada do Vesper está fora de foco.")
                return
            tentou_enviar = True
            teclado.press(tecla_espaco)
            registrado = time.perf_counter()
        time.sleep(0.020)
    finally:
        if tentou_enviar:
            try:
                teclado.release(tecla_espaco)
            except Exception:
                pass

    with shared.lock:
        engine.notify_pressed(registrado)


def trabalhador(shared, engine, teclado, tecla_espaco):
    """Thread de captura: MSS é instanciado aqui e erros nunca encerram a thread."""
    quadros = 0
    inicio_fps = time.perf_counter()

    while True:
        with shared.lock:
            if not shared.running:
                return

        if mss is None:
            with shared.lock:
                engine.reset()
                engine.msg = "Dependência mss ausente. Execute o arquivo .bat."
            time.sleep(1.0)
            continue

        try:
            # A instância MSS nasce e morre na mesma thread que a utiliza.
            with mss.mss() as capturador:
                while True:
                    with shared.lock:
                        if not shared.running:
                            return
                        regiao = shared.region

                    if not regiao:
                        time.sleep(0.04)
                        continue

                    esquerda, topo, largura, altura = regiao
                    try:
                        t_inicio = time.perf_counter()
                        bruto = capturador.grab({
                            "left": int(esquerda), "top": int(topo),
                            "width": int(largura), "height": int(altura),
                        })
                        t_fim = time.perf_counter()
                        agora = (t_inicio + t_fim) / 2.0
                        # MSS entrega BGRA; o motor trabalha com RGB.
                        imagem = np.asarray(bruto)[:, :, :3][:, :, ::-1].copy()
                        with shared.lock:
                            shared.frame = imagem
                            instante = engine.step(imagem, agora)
                            destino = shared.janela_destino

                        if instante is not None:
                            _enviar_parry(shared, engine, teclado, tecla_espaco, instante, destino)

                        quadros += 1
                        if t_fim - inicio_fps >= 1.0:
                            with shared.lock:
                                shared.fps = quadros / max(0.001, t_fim - inicio_fps)
                            quadros = 0
                            inicio_fps = t_fim

                    except Exception as exc:
                        with shared.lock:
                            engine.reset()
                            engine.msg = f"Falha na captura/processamento: {exc} — tentando novamente..."
                        time.sleep(0.25)
                        break  # recria o contexto MSS

        except Exception as exc:
            with shared.lock:
                engine.reset()
                engine.msg = f"Captura indisponível: {exc} — tentando novamente..."
            time.sleep(0.75)


# ---------------------------------------------------------------------------
# Identidade visual RcM  —  SOMENTE camada visual. Nada desta seção altera o
# motor de visão, a estimativa de tempo, a compensação ou a thread de captura.
# ---------------------------------------------------------------------------
APP_NOME = "Auto-Parry Kirae"
APP_VERSAO = "1.1.1"
ASSINATURA = "RCM/Caliroe"
APP_ID = f"RcM.AutoParryKirae.{APP_VERSAO}"
ICONE_PNG = os.path.join(RECURSOS_DIR, "assets", "rcm_icon.png")
ICONE_ICO = os.path.join(RECURSOS_DIR, "assets", "rcm_icon.ico")
CLAIM_PATH = os.path.join(BASE_DIR, "auto_parry_claim.json")
TEMA_PATH = os.path.join(BASE_DIR, "auto_parry_tema.json")
TEMA_PADRAO = "escuro"

# As cores de marca aparecem nos controles e nas amostras. Cada paleta
# escolhe tons próprios para texto de estado, especialmente sobre branco.
CORES_RCM = {
    "vermelho": "#F70047",
    "vermelho_hover": "#FF2A66",
    "verde": "#00F71E",
    "verde_hover": "#45FF5C",
    "magenta": "#FF00FF",
}
TEMAS = {
    "escuro": {
        **CORES_RCM,
        "nome": "RcM Escuro",
        "fundo": "#0F0F0F", "superficie": "#191919", "superficie2": "#242424", "superficie3": "#303030",
        "borda": "#373737", "borda_forte": "#767676",
        "texto": "#FFFFFF", "suave": "#BCB7B9",
        "acento_texto": "#FF7398", "verde_texto": "#00F71E", "ambar": "#FFB020",
        "barra_escura": True,
    },
    "vermelho": {
        **CORES_RCM,
        "nome": "RcM Vermelho",
        "fundo": "#270910", "superficie": "#3C111C", "superficie2": "#511524", "superficie3": "#66172C",
        "borda": "#74253A", "borda_forte": "#AC546B",
        "texto": "#FFF9FB", "suave": "#F5B4C5",
        "acento_texto": "#FF92AE", "verde_texto": "#00F71E", "ambar": "#FFB020",
        "barra_escura": True,
    },
    "claro": {
        **CORES_RCM,
        "nome": "RcM Claro",
        "fundo": "#FFFFFF", "superficie": "#FFF4F7", "superficie2": "#F8E8ED", "superficie3": "#F2DCE3",
        "borda": "#DEB8C5", "borda_forte": "#9E6279",
        "texto": "#0F0F0F", "suave": "#5F414C",
        "acento_texto": "#B40035", "verde_texto": "#006B1B", "ambar": "#7B4600",
        "barra_escura": False,
    },
}


def carregar_tema():
    try:
        with open(TEMA_PATH, encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
    except (OSError, ValueError):
        return TEMA_PADRAO
    tema = dados.get("tema") if isinstance(dados, dict) else None
    return tema if isinstance(tema, str) and tema in TEMAS else TEMA_PADRAO


def _salvar_json_visual(caminho, dados):
    temporario = f"{caminho}.{os.getpid()}.tmp"
    try:
        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(dados, arquivo)
        os.replace(temporario, caminho)
    except OSError:
        try:
            os.remove(temporario)
        except OSError:
            pass
        return False
    return True

_ICONE_RCM_B64 = """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"""


def _lanczos():
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _rgb(hexa):
    hexa = hexa.lstrip("#")
    return tuple(int(hexa[i:i + 2], 16) for i in (0, 2, 4))


def misturar(cor_a, cor_b, t):
    """Mistura duas cores hex: t=0 -> a, t=1 -> b."""
    a, b = _rgb(cor_a), _rgb(cor_b)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def carregar_icone_rcm():
    """Devolve o ícone RcM ORIGINAL (arquivo em assets/ ou a cópia embutida idêntica)."""
    try:
        if os.path.isfile(ICONE_PNG):
            with Image.open(ICONE_PNG) as arq:
                return arq.convert("RGBA")
    except Exception:
        pass
    try:
        import base64
        import io
        with Image.open(io.BytesIO(base64.b64decode(_ICONE_RCM_B64))) as arq:
            return arq.convert("RGBA")
    except Exception:
        return None


def renderizar_retangulo(w, h, raio, preenchimento, borda=None, espessura=1,
                         fundo=None, ss=4, ponto=None, reduzir=True):
    """Retângulo arredondado com antialiasing (supersampling), opaco sobre `fundo`.
    Com reduzir=False devolve a imagem em escala ss (para desenhar glifos antes de reduzir)."""
    W, H = w * ss, h * ss
    imagem = Image.new("RGB", (W, H), fundo or preenchimento)
    d = ImageDraw.Draw(imagem)
    d.rounded_rectangle(
        (0, 0, W - 1, H - 1), radius=raio * ss, fill=preenchimento,
        outline=borda, width=int(espessura * ss) if borda else 0,
    )
    if ponto:                                    # (cx, cy, r, cor)
        cx, cy, r, cor = ponto
        d.ellipse(((cx - r) * ss, (cy - r) * ss, (cx + r) * ss, (cy + r) * ss), fill=cor)
    return imagem.resize((w, h), _lanczos()) if reduzir else imagem


class Pilula(_Canvas):
    """Etiqueta arredondada (versão, estado do motor)."""

    def __init__(self, master, ui, texto, cor, bg, fonte, ponto=False):
        super().__init__(master, highlightthickness=0, bd=0, bg=bg, takefocus=0)
        self.ui, self.bg_cor, self.fonte, self.ponto = ui, bg, fonte, ponto
        self._atual = None
        self.definir(texto, cor)

    def definir(self, texto, cor):
        if self._atual == (texto, cor):
            return
        self._atual = (texto, cor)
        ui = self.ui
        extra = 26 if self.ponto else 18
        largura = self.fonte.measure(texto) + ui.px(extra)
        altura = ui.px(22)
        ponto = (ui.px(11), altura / 2, ui.u * 3, cor) if self.ponto else None
        imagem = renderizar_retangulo(
            largura, altura, altura // 2, misturar(cor, self.bg_cor, 0.86),
            borda=misturar(cor, self.bg_cor, 0.55), fundo=self.bg_cor, ponto=ponto,
        )
        self.configure(width=largura, height=altura)
        self.delete("all")
        self._foto = ui.foto(imagem)
        self.create_image(0, 0, anchor="nw", image=self._foto)
        x_texto = ui.px(19) if self.ponto else largura / 2
        self.create_text(
            x_texto, altura / 2 + 0.5, text=texto, fill=cor, font=self.fonte,
            anchor="w" if self.ponto else "center",
        )


class BotaoCanvas(_Canvas):
    """Botão plano arredondado com estados normal/hover/pressionado."""

    def __init__(self, master, ui, texto, comando, largura, altura, bg, fonte,
                 raio=8, fundo_cor=None, hover_cor=None, borda_cor=None, texto_cor=None):
        w, h = ui.px(largura), ui.px(altura)
        super().__init__(master, width=w, height=h, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=0)
        self.comando = comando
        self.w, self.h = w, h
        cores = ui.cor
        fundo_cor = fundo_cor or cores["superficie2"]
        hover_cor = hover_cor or cores["superficie3"]
        borda_cor = borda_cor or cores["borda"]
        r = ui.px(raio)
        self._imgs = {
            "normal": ui.foto(renderizar_retangulo(w, h, r, fundo_cor, borda_cor, 1, bg)),
            "hover": ui.foto(renderizar_retangulo(w, h, r, hover_cor, cores["borda_forte"], 1, bg)),
            "press": ui.foto(renderizar_retangulo(w, h, r, borda_cor, cores["borda_forte"], 1, bg)),
        }
        self._fundo = self.create_image(0, 0, anchor="nw", image=self._imgs["normal"])
        self._texto = self.create_text(w / 2, h / 2, text=texto, fill=texto_cor or cores["texto"], font=fonte)
        self.bind("<Enter>", lambda _e: self.itemconfigure(self._fundo, image=self._imgs["hover"]))
        self.bind("<Leave>", lambda _e: self.itemconfigure(self._fundo, image=self._imgs["normal"]))
        self.bind("<ButtonPress-1>", lambda _e: self.itemconfigure(self._fundo, image=self._imgs["press"]))
        self.bind("<ButtonRelease-1>", self._soltar)

    def _soltar(self, evento):
        dentro = 0 <= evento.x <= self.w and 0 <= evento.y <= self.h
        self.itemconfigure(self._fundo, image=self._imgs["hover" if dentro else "normal"])
        if dentro and self.comando:
            self.comando()


class BotaoEnergia(_Canvas):
    """Botão grande LIGAR/DESLIGAR: verde (ligado) / vermelho (desligado)."""

    def __init__(self, master, ui, comando, bg, largura=366, altura=60):
        w, h = ui.px(largura), ui.px(altura)
        super().__init__(master, width=w, height=h, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=0)
        self.ui, self.comando, self.w, self.h = ui, comando, w, h
        self._imgs = {}
        for ligado in (False, True):
            for hover in (False, True):
                self._imgs[(ligado, hover)] = ui.foto(self._renderizar(ligado, hover, bg))
        self._ligado, self._hover = False, False
        self._fundo = self.create_image(0, 0, anchor="nw", image=self._imgs[(False, False)])
        x = int(h * 1.02)
        self._t1 = self.create_text(x, h / 2, anchor="w", text="",
                                    font=ui.f["grande_c"] if ui.compacto else ui.f["grande"])
        self.definir(False)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._soltar)

    def _renderizar(self, ligado, hover, bg, ss=4):
        w, h = self.w, self.h
        if ligado:
            base = self.ui.cor["verde_hover"] if hover else self.ui.cor["verde"]
            icone = "#04240B"
        else:
            base = self.ui.cor["vermelho_hover"] if hover else self.ui.cor["vermelho"]
            icone = "#FFFFFF"
        grande = renderizar_retangulo(w, h, self.ui.px(12), base, fundo=bg, ss=ss, reduzir=False)
        d = ImageDraw.Draw(grande)
        cx, cy = h * 0.50 * ss, h * 0.50 * ss
        r = h * 0.19 * ss
        larg = max(2, int(h * 0.055 * ss))
        d.arc((cx - r, cy - r, cx + r, cy + r), start=305, end=235, fill=icone, width=larg)
        d.line((cx, cy - r * 1.18, cx, cy - r * 0.10), fill=icone, width=larg)
        for ex, ey in ((cx, cy - r * 1.18), (cx, cy - r * 0.10)):          # pontas arredondadas
            d.ellipse((ex - larg / 2, ey - larg / 2, ex + larg / 2, ey + larg / 2), fill=icone)
        return grande.resize((w, h), _lanczos())

    def _set_hover(self, valor):
        self._hover = valor
        self.itemconfigure(self._fundo, image=self._imgs[(self._ligado, self._hover)])

    def _soltar(self, evento):
        if 0 <= evento.x <= self.w and 0 <= evento.y <= self.h and self.comando:
            self.comando()

    def definir(self, ligado):
        self._ligado = bool(ligado)
        self.itemconfigure(self._fundo, image=self._imgs[(self._ligado, self._hover)])
        if ligado:
            self.itemconfigure(self._t1, text="DESLIGAR", fill="#04240B")
        else:
            self.itemconfigure(self._t1, text="LIGAR", fill="#FFFFFF")


class Interruptor(_Canvas):
    """Chave liga/desliga (substitui o Checkbutton padrão do Tk)."""

    def __init__(self, master, ui, variavel, comando, bg):
        w, h = ui.px(40), ui.px(22)
        super().__init__(master, width=w, height=h, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=0)
        self.variavel, self.comando = variavel, comando
        self._imgs = {est: ui.foto(self._renderizar(w, h, est, bg, ui.cor)) for est in (False, True)}
        self._id = self.create_image(0, 0, anchor="nw", image=self._imgs[bool(variavel.get())])
        self.bind("<ButtonRelease-1>", lambda _e: self.alternar())

    @staticmethod
    def _renderizar(w, h, ligado, bg, cores, ss=6):
        trilho = cores["vermelho"] if ligado else cores["superficie3"]
        grande = renderizar_retangulo(w, h, h // 2, trilho, fundo=bg, ss=ss, reduzir=False)
        d = ImageDraw.Draw(grande)
        r = h * 0.36 * ss
        cx = (w - h * 0.5) * ss if ligado else h * 0.5 * ss
        cy = h * 0.5 * ss
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#FFFFFF" if ligado else cores["suave"])
        return grande.resize((w, h), _lanczos())

    def alternar(self):
        self.variavel.set(not self.variavel.get())
        self.atualizar()
        if self.comando:
            self.comando()

    def atualizar(self):
        self.itemconfigure(self._id, image=self._imgs[bool(self.variavel.get())])


class Deslizante(_Canvas):
    """Controle deslizante da compensação (ms). Salva só ao soltar o mouse."""

    def __init__(self, master, ui, minimo, maximo, valor, ao_mudar, ao_soltar, bg, largura):
        self.ui = ui
        self.w, self.h = ui.px(largura), ui.px(26)
        super().__init__(master, width=self.w, height=self.h, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=0)
        self.bg_cor, self.min, self.max = bg, minimo, maximo
        self.ao_mudar, self.ao_soltar = ao_mudar, ao_soltar
        self.valor = valor
        self._id = self.create_image(0, 0, anchor="nw")
        self._desenhar()
        self.bind("<ButtonPress-1>", self._mouse)
        self.bind("<B1-Motion>", self._mouse)
        self.bind("<ButtonRelease-1>", lambda _e: self.ao_soltar(self.valor))
        self.bind("<MouseWheel>", lambda e: self.passo(1 if e.delta > 0 else -1, salvar=True))

    def _desenhar(self, ss=4):
        ui, w, h = self.ui, self.w, self.h
        margem = ui.px(9)
        trilho_h = max(3, ui.px(4))
        x0, x1 = margem, w - margem
        frac = (self.valor - self.min) / float(self.max - self.min)
        xt = x0 + (x1 - x0) * frac
        grande = Image.new("RGB", (w * ss, h * ss), self.bg_cor)
        d = ImageDraw.Draw(grande)
        cy = h * ss / 2
        th = trilho_h * ss / 2
        d.rounded_rectangle((x0 * ss, cy - th, x1 * ss, cy + th), radius=th, fill=ui.cor["borda_forte"])
        d.rounded_rectangle((x0 * ss, cy - th, max(xt, x0 + 1) * ss, cy + th), radius=th, fill=ui.cor["vermelho"])
        r = ui.px(8) * ss
        d.ellipse((xt * ss - r, cy - r, xt * ss + r, cy + r), fill="#FFFFFF",
                  outline=ui.cor["borda_forte"], width=ss)
        r2 = ui.px(3.2) * ss
        d.ellipse((xt * ss - r2, cy - r2, xt * ss + r2, cy + r2), fill=ui.cor["vermelho"])
        self._foto = ui.foto(grande.resize((w, h), _lanczos()))
        self.itemconfigure(self._id, image=self._foto)

    def _mouse(self, evento):
        margem = self.ui.px(9)
        frac = (evento.x - margem) / float(max(1, self.w - 2 * margem))
        self.definir(int(round(self.min + max(0.0, min(1.0, frac)) * (self.max - self.min))), notificar=True)

    def passo(self, delta, salvar=False):
        self.definir(self.valor + delta, notificar=True)
        if salvar:
            self.ao_soltar(self.valor)

    def definir(self, valor, notificar=False):
        valor = max(self.min, min(self.max, int(round(valor))))
        if valor == self.valor:
            return
        self.valor = valor
        self._desenhar()
        if notificar and self.ao_mudar:
            self.ao_mudar(valor)


class Interface:
    """Interface RcM. Liga-se ao motor somente por Shared/Engine (sob shared.lock)."""

    LARGURA = 392            # largura externa dos cartões (px lógicos, 100% de escala)
    INTERNA = 366            # largura útil dentro de um cartão (borda + margens de 12 px)

    def __init__(self, root, shared, engine, compacto=None):
        self.root, self.shared, self.engine = root, shared, engine
        self._compacto_forcado = compacto
        self.tema = carregar_tema()
        self.aba = "painel"
        self._erro_tema = False
        self.u = max(0.75, root.winfo_fpixels("1i") / 96.0)
        self._shell = self._visor = None
        self._aviso = None            # (texto, cor, expira_em)
        self._prazo_claim = self._carregar_prazo_claim()
        self._claim_avisado = self._prazo_claim is not None and self._prazo_claim <= time.time()
        with self.shared.lock:
            auto, teste = bool(engine.auto), bool(engine.dry)
        self.auto_var = tk.BooleanVar(master=root, value=auto)
        self.teste_var = tk.BooleanVar(master=root, value=teste)
        self._definir_fontes()
        self.icone_pil = carregar_icone_rcm()
        self._aplicar_icone_janela()
        root.bind("<MouseWheel>", self._rolar_painel, add="+")
        self._remontar()
        aplicar_barra_tema(root, self.cor)

    @property
    def cor(self):
        return TEMAS[self.tema]

    def _remontar(self):
        """Recria somente os controles, mantendo a sessão e o cronômetro."""
        root = self.root
        if self._shell is not None:
            self._shell.destroy()
        self._visor = None
        self._cache = {}
        self._preview = {"foto": None, "tam": None}
        self.compacto = bool(self._compacto_forcado)
        root.configure(bg=self.cor["fundo"])
        self._shell = tk.Frame(root, bg=self.cor["fundo"])
        self._shell.pack()
        self._montar()
        root.update_idletasks()
        limite = int(root.winfo_screenheight() * 0.90)
        if self._compacto_forcado is None and self._shell.winfo_reqheight() > limite:
            self.compacto = True
            self._cont.destroy()
            self._montar()
            root.update_idletasks()
        if self._shell.winfo_reqheight() > limite:
            self._limitar_altura(limite)
        self.atualizar()

    def _limitar_altura(self, limite):
        """Mantém todo o painel compacto acessível quando a tela é baixa."""
        largura = self._shell.winfo_reqwidth()
        altura = self._shell.winfo_reqheight()
        self._cont.pack_forget()
        visor = tk.Canvas(self._shell, bg=self.cor["fundo"], bd=0, highlightthickness=0,
                          width=largura, height=limite, yscrollincrement=self.px(16),
                          scrollregion=(0, 0, largura, altura))
        barra = tk.Scrollbar(self._shell, orient="vertical", command=visor.yview, takefocus=0)
        visor.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        visor.pack(side="left")
        visor.create_window(self.px(14), self.px(6 if self.compacto else 12),
                            window=self._cont, anchor="nw")
        self._visor = visor

    def _rolar_painel(self, evento):
        # Sobre o controle deslizante, a roda continua reservada à compensação.
        if self._visor is not None and not isinstance(evento.widget, Deslizante) and evento.delta:
            self._visor.yview_scroll(-1 if evento.delta > 0 else 1, "units")

    # ------------------------------------------------------------ utilidades
    def px(self, n):
        return max(1, int(round(n * self.u)))

    def foto(self, imagem):
        return ImageTk.PhotoImage(imagem, master=self.root)

    def _definir_fontes(self):
        raiz = self.root
        familias = set(tkfont.families(raiz))

        def escolher(candidatas):
            for nome in candidatas:
                if nome in familias:
                    return nome
            return tkfont.nametofont("TkDefaultFont").actual("family")

        ui = escolher(("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"))
        mono = escolher(("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"))

        def fonte(familia, tamanho, peso="normal"):
            return tkfont.Font(root=raiz, family=familia, size=tamanho, weight=peso)

        self.f = {
            "titulo": fonte(ui, 14, "bold"),
            "grande": fonte(ui, 16, "bold"),
            "grande_c": fonte(ui, 14, "bold"),
            "texto": fonte(ui, 9),
            "texto_b": fonte(ui, 9, "bold"),
            "pequeno": fonte(ui, 8),
            "pequeno_b": fonte(ui, 8, "bold"),
            "mono": fonte(mono, 9),
            "mono_b": fonte(mono, 9, "bold"),
        }

    def _carregar_icones(self):
        self.icones = {}
        if self.icone_pil is not None:
            for nome, tam in (("cabecalho", 38 if self.compacto else 46), ("rodape", 16)):
                t = self.px(tam)
                self.icones[nome] = self.foto(self.icone_pil.resize((t, t), _lanczos()))

    def _aplicar_icone_janela(self):
        aplicado = False
        if sys.platform == "win32" and os.path.isfile(ICONE_ICO):
            try:
                self.root.iconbitmap(default=ICONE_ICO)
                aplicado = True
            except Exception:
                aplicado = False
        if not aplicado and self.icone_pil is not None:
            try:
                self._icones_janela = [self.foto(self.icone_pil.resize((s, s), _lanczos())) for s in (16, 32, 48, 64)]
                self.root.iconphoto(True, *self._icones_janela)
            except Exception:
                pass

    # ------------------------------------------------------------ construção
    def _cartao(self, pai, titulo):
        quadro = tk.Frame(pai, bg=self.cor["superficie"], highlightbackground=self.cor["borda"],
                          highlightcolor=self.cor["borda"], highlightthickness=1, bd=0)
        quadro.pack(fill="x", pady=(self.px(4 if self.compacto else 6), 0))
        interno = tk.Frame(quadro, bg=self.cor["superficie"])
        interno.pack(fill="x", padx=self.px(12), pady=self.px(4 if self.compacto else 9))
        if titulo:
            linha = tk.Frame(interno, bg=self.cor["superficie"])
            linha.pack(fill="x", pady=(0, self.px(5 if self.compacto else 8)))
            tk.Frame(linha, bg=self.cor["vermelho"], width=self.px(3), height=self.px(11)).pack(side="left")
            tk.Label(linha, text=titulo, font=self.f["pequeno_b"], fg=self.cor["suave"],
                     bg=self.cor["superficie"]).pack(side="left", padx=(self.px(7), 0))
        return interno

    def _montar(self):
        self._carregar_icones()
        cont = tk.Frame(self._shell, bg=self.cor["fundo"])
        cont.pack(padx=self.px(14), pady=(self.px(6 if self.compacto else 12), self.px(5 if self.compacto else 8)))
        self._cont = cont
        self._montar_cabecalho(cont)
        self._montar_abas(cont)
        if self.aba == "painel":
            self._montar_controle(cont)
            self._montar_configuracao(cont)
            if not self.compacto:
                self._montar_claim(self._cartao(cont, None))
            self._montar_monitoramento(cont)
        else:
            self._montar_temas(cont)
        self._montar_rodape(cont)

    def _montar_abas(self, cont):
        faixa = tk.Frame(cont, bg=self.cor["fundo"], width=self.px(self.LARGURA), height=self.px(34))
        faixa.pack(fill="x", pady=(self.px(4), 0))
        faixa.pack_propagate(False)
        self._botoes_abas = {}
        for chave, rotulo in (("painel", "Painel"), ("temas", "Temas")):
            ativo = self.aba == chave
            grupo = tk.Frame(faixa, bg=self.cor["fundo"])
            grupo.pack(side="left", padx=(0, self.px(5)), fill="y")
            botao = self._botao_acao(grupo, rotulo, lambda aba=chave: self._selecionar_aba(aba), 9,
                                    destaque=ativo)
            botao.pack(fill="both", expand=True)
            tk.Frame(grupo, bg=self.cor["vermelho"] if ativo else self.cor["borda"],
                     height=self.px(2)).pack(fill="x")
            self._botoes_abas[chave] = botao

    def _montar_temas(self, cont):
        cartao = self._cartao(cont, "PALETAS RcM")
        for tema, paleta in TEMAS.items():
            ativo = tema == self.tema
            linha = tk.Frame(cartao, bg=self.cor["superficie2"],
                             highlightthickness=1, highlightbackground=self.cor["borda_forte"] if ativo
                             else self.cor["borda"], bd=0)
            linha.pack(fill="x", pady=(0, self.px(6)))
            amostra = tk.Canvas(linha, width=self.px(58), height=self.px(34), bd=0,
                                bg=self.cor["superficie2"], highlightthickness=0, takefocus=0)
            amostra.pack(side="left", padx=(self.px(7), self.px(7)), pady=self.px(5))
            for i, tinta in enumerate((paleta["fundo"], paleta["vermelho"], paleta["verde"])):
                x = self.px(2 + i * 19)
                amostra.create_rectangle(x, self.px(9), x + self.px(15), self.px(25),
                                          fill=tinta, outline=self.cor["borda_forte"])
            tk.Label(linha, text=paleta["nome"], bg=self.cor["superficie2"], fg=self.cor["texto"],
                     font=self.f["texto_b"], anchor="w").pack(side="left", fill="x", expand=True)
            if ativo:
                rotulo = tk.Label(linha, text="Em uso", bg=self.cor["superficie2"],
                                  fg=self.cor["verde_texto"], font=self.f["pequeno_b"], width=9)
                rotulo.pack(side="right", padx=self.px(7))
            else:
                botao = self._botao_acao(linha, "Aplicar", lambda escolhido=tema: self._selecionar_tema(escolhido),
                                         8, destaque=True)
                botao.pack(side="right", padx=self.px(7), pady=self.px(5))
        if self._erro_tema:
            tk.Label(cartao, text="Tema aplicado. Não foi possível salvar a escolha.",
                     bg=self.cor["superficie"], fg=self.cor["ambar"], font=self.f["pequeno"],
                     wraplength=self.px(self.INTERNA), anchor="w").pack(fill="x")

    def _selecionar_aba(self, aba):
        if aba != self.aba:
            self.aba = aba
            self._remontar()
            self._botoes_abas[aba].focus_set()

    def _selecionar_tema(self, tema):
        self.tema = tema
        self._erro_tema = not _salvar_json_visual(TEMA_PATH, {"tema": tema})
        self._remontar()
        aplicar_barra_tema(self.root, self.cor)
        self._botoes_abas["temas"].focus_set()

    def _montar_cabecalho(self, cont):
        cab = tk.Frame(cont, bg=self.cor["fundo"])
        cab.pack(fill="x")
        if "cabecalho" in self.icones:
            tk.Label(cab, image=self.icones["cabecalho"], bg=self.cor["fundo"], bd=0).pack(
                side="left", padx=(0, self.px(11)))
        bloco = tk.Frame(cab, bg=self.cor["fundo"])
        bloco.pack(side="left")
        tk.Label(bloco, text=APP_NOME, font=self.f["titulo"], fg=self.cor["texto"],
                 bg=self.cor["fundo"]).pack(anchor="w")
        sub = tk.Frame(bloco, bg=self.cor["fundo"])
        sub.pack(anchor="w")
        tk.Label(sub, text="por ", font=self.f["pequeno"], fg=self.cor["suave"], bg=self.cor["fundo"]).pack(side="left")
        tk.Label(sub, text=ASSINATURA, font=self.f["pequeno_b"],
                 fg=self.cor["acento_texto"], bg=self.cor["fundo"]).pack(side="left")
        tk.Label(sub, text="  ·  automação de skill check", font=self.f["pequeno"],
                 fg=self.cor["suave"], bg=self.cor["fundo"]).pack(side="left")
        Pilula(cab, self, f"v{APP_VERSAO}", self.cor["suave"], self.cor["fundo"], self.f["pequeno_b"]).pack(side="right")
        linha = tk.Canvas(cont, height=self.px(2), bg=self.cor["borda"], highlightthickness=0, bd=0)
        linha.pack(fill="x", pady=(self.px(7 if self.compacto else 10), 0))
        linha.create_rectangle(0, 0, self.px(56), self.px(2), fill=self.cor["vermelho"], outline="")

    def _montar_controle(self, cont):
        c = self._cartao(cont, None)
        self.botao_energia = BotaoEnergia(c, self, self.alternar, self.cor["superficie"], self.INTERNA,
                                          42 if self.compacto else 60)
        self.botao_energia.pack()
        linha = tk.Frame(c, bg=self.cor["superficie"])
        linha.pack(fill="x", pady=(self.px(5 if self.compacto else 9), 0))
        self.chip = Pilula(linha, self, "DESLIGADO", self.cor["suave"], self.cor["superficie"],
                           self.f["pequeno_b"], ponto=True)
        self.chip.pack(side="left")
        tk.Label(linha, text="F8 liga/desliga em qualquer janela", font=self.f["pequeno"],
                 fg=self.cor["suave"], bg=self.cor["superficie"]).pack(side="right")
        area = tk.Frame(c, bg=self.cor["superficie"], height=self.px(26 if self.compacto else 30))
        area.pack(fill="x", pady=(self.px(4 if self.compacto else 7), 0))
        area.pack_propagate(False)
        self.status = tk.Label(area, text="", font=self.f["texto"], fg=self.cor["suave"],
                               bg=self.cor["superficie"], wraplength=self.px(self.INTERNA - 4),
                               justify="left", anchor="nw")
        self.status.pack(fill="both", expand=True)
        if self.compacto:
            self._montar_claim(c)

    def _montar_configuracao(self, cont):
        c = self._cartao(cont, "CONFIGURAÇÃO")
        BotaoCanvas(c, self, "Selecionar área do Skill Check", self.selecionar_area,
                    self.INTERNA, 30 if self.compacto else 34, self.cor["superficie"],
                    self.f["texto_b"]).pack(fill="x")
        self.area_info = tk.Label(c, text="", font=self.f["pequeno"], fg=self.cor["suave"],
                                  bg=self.cor["superficie"], anchor="w")
        self.area_info.pack(fill="x", pady=(self.px(3 if self.compacto else 5), self.px(5 if self.compacto else 10)))

        topo = tk.Frame(c, bg=self.cor["superficie"])
        topo.pack(fill="x")
        tk.Label(topo, text="Compensação de latência", font=self.f["texto"], fg=self.cor["texto"],
                 bg=self.cor["superficie"]).pack(side="left")
        self.valor_comp = tk.Label(topo, text="", font=self.f["mono_b"], fg=self.cor["texto"], bg=self.cor["superficie"])
        self.valor_comp.pack(side="right")
        linha = tk.Frame(c, bg=self.cor["superficie"])
        linha.pack(fill="x", pady=(self.px(2 if self.compacto else 4), self.px(4 if self.compacto else 8)))
        BotaoCanvas(linha, self, "−", lambda: self.slider.passo(-1, salvar=True), 28, 26,
                    self.cor["superficie"], self.f["texto_b"], raio=6).pack(side="left")
        with self.shared.lock:
            comp_ms = self.engine.comp * 1000.0
        self.slider = Deslizante(linha, self, 0, 200, int(round(comp_ms)), self._comp_mudou,
                                 self._comp_soltou, self.cor["superficie"], self.INTERNA - 28 * 2 - 16)
        self.slider.pack(side="left", padx=self.px(4))
        BotaoCanvas(linha, self, "+", lambda: self.slider.passo(1, salvar=True), 28, 26,
                    self.cor["superficie"], self.f["texto_b"], raio=6).pack(side="left")

        self._linha_chave(c, "Auto-ajustar compensação",
                          "usa o ponto onde o ponteiro congelou", self.auto_var, self.alterar_auto)
        self._linha_chave(c, "Modo teste",
                          "mira e prevê, mas NÃO envia ESPAÇO", self.teste_var, self.alterar_teste)

    def _linha_chave(self, pai, titulo, descricao, variavel, comando):
        linha = tk.Frame(pai, bg=self.cor["superficie"])
        linha.pack(fill="x", pady=(self.px(2 if self.compacto else 4), 0))
        textos = tk.Frame(linha, bg=self.cor["superficie"])
        textos.pack(side="left")
        tk.Label(textos, text=titulo, font=self.f["texto"], fg=self.cor["texto"], bg=self.cor["superficie"]).pack(anchor="w")
        if not self.compacto:
            tk.Label(textos, text=descricao, font=self.f["pequeno"], fg=self.cor["suave"], bg=self.cor["superficie"]).pack(anchor="w")
        chave = Interruptor(linha, self, variavel, comando, self.cor["superficie"])
        chave.pack(side="right")
        for w in textos.winfo_children() + [textos]:
            w.bind("<ButtonRelease-1>", lambda _e, ch=chave: ch.alternar())
        return chave

    def _montar_claim(self, c):
        linha = tk.Frame(c, bg=self.cor["superficie"])
        linha.pack(fill="x", pady=(self.px(4), 0) if self.compacto else 0)
        linha.columnconfigure(1, weight=1)
        tk.Frame(linha, bg=self.cor["vermelho"], width=self.px(3), height=self.px(11)).grid(row=0, column=0)
        self.claim_status = tk.Label(linha, anchor="w", font=self.f["texto_b"], bg=self.cor["superficie"])
        self.claim_status.grid(row=0, column=1, sticky="w", padx=(self.px(7), self.px(4)))

        for coluna, (titulo, minutos) in enumerate((("Ganhei", 120), ("Perdi", 60)), start=2):
            botao = self._botao_acao(linha, titulo, lambda duracao=minutos: self._registrar_claim(duracao), 7)
            botao.grid(row=0, column=coluna, padx=(self.px(4), 0))

    def _botao_acao(self, pai, texto, comando, largura, destaque=False):
        def acionar_por_enter(evento):
            evento.widget.invoke()
            return "break"

        botao = tk.Button(pai, text=texto, width=largura, font=self.f["texto_b"], command=comando,
                          bg=self.cor["superficie3" if destaque else "superficie2"],
                          fg=self.cor["texto"], relief="flat", bd=0,
                          activebackground=self.cor["superficie3"], activeforeground=self.cor["texto"],
                          highlightthickness=self.px(1), highlightbackground=self.cor["borda_forte"],
                          highlightcolor=self.cor["acento_texto"], cursor="hand2", takefocus=1)
        botao.bind("<Return>", acionar_por_enter)
        botao.bind("<space>", lambda _evento: "break")
        return botao

    @staticmethod
    def _carregar_prazo_claim():
        try:
            with open(CLAIM_PATH, encoding="utf-8") as arquivo:
                prazo = json.load(arquivo)
        except (OSError, ValueError):
            return None
        if isinstance(prazo, float) and math.isfinite(prazo) and prazo > 0:
            return prazo
        return None

    def _registrar_claim(self, minutos):
        # Um prazo absoluto preserva a contagem ao suspender ou reabrir o app.
        self._prazo_claim = time.time() + minutos * 60
        self._claim_avisado = False
        if not _salvar_json_visual(CLAIM_PATH, self._prazo_claim):
            self.avisar("Cronômetro ativo. Não foi possível salvar ao lado do programa.")
        self._atualizar_claim()

    def _atualizar_claim(self):
        cor = self.cor["suave"]
        if self._prazo_claim is None:
            texto = "Claim · sem registro"
        else:
            segundos = math.ceil(self._prazo_claim - time.time())
            if segundos > 0 and not self._claim_avisado:
                horas, resto = divmod(segundos, 3600)
                minutos, segundos = divmod(resto, 60)
                texto = f"Claim em {horas:02d}:{minutos:02d}:{segundos:02d}"
                cor = self.cor["texto"]
            else:
                texto, cor = "Claim disponível", self.cor["verde_texto"]
                if not self._claim_avisado:
                    self._claim_avisado = True
                    self.root.bell()
        if self.aba == "painel":
            self._set(self.claim_status, "claim", text=texto, fg=cor)

    def _montar_monitoramento(self, cont):
        c = self._cartao(cont, "MONITORAMENTO")
        corpo = tk.Frame(c, bg=self.cor["superficie"])
        corpo.pack(fill="x")
        self.lado = self.px(110 if self.compacto else 176)
        self.canvas = tk.Canvas(corpo, width=self.lado, height=self.lado, bg=self.cor["fundo"],
                                highlightthickness=1, highlightbackground=self.cor["borda"],
                                highlightcolor=self.cor["borda"], bd=0)
        self.canvas.pack(side="left")
        self._placeholder_preview()

        tabela = tk.Frame(corpo, bg=self.cor["superficie"])
        tabela.pack(side="left", fill="both", expand=True, padx=(self.px(12), 0))
        tabela.columnconfigure(1, weight=1)
        self.valores = {}
        itens = (("motor", "MOTOR"), ("fps", "CAPTURA"), ("parrys", "PARRYS"),
                 ("comp", "COMPENSAÇÃO"), ("erro_ang", "ERRO ANGULAR"), ("erro_ms", "ERRO TEMPORAL"))
        for i, (chave, rotulo) in enumerate(itens):
            tabela.rowconfigure(2 * i, weight=1)
            tk.Label(tabela, text=rotulo, font=self.f["pequeno_b"], fg=self.cor["suave"],
                     bg=self.cor["superficie"]).grid(row=2 * i, column=0, sticky="w")
            rotulo_valor = tk.Label(tabela, text="—", font=self.f["mono_b"], fg=self.cor["texto"],
                                    bg=self.cor["superficie"])
            rotulo_valor.grid(row=2 * i, column=1, sticky="e", padx=(self.px(8), 0))
            self.valores[chave] = rotulo_valor
            if i < len(itens) - 1:
                tk.Frame(tabela, bg=self.cor["borda"], height=1).grid(row=2 * i + 1, column=0,
                                                                  columnspan=2, sticky="ew")

        if self.compacto:
            return
        legenda = tk.Frame(c, bg=self.cor["superficie"])
        legenda.pack(fill="x", pady=(self.px(8), 0))
        marca = tk.Canvas(legenda, width=self.px(34), height=self.px(10), bg=self.cor["superficie"],
                          highlightthickness=0, bd=0)
        marca.pack(side="left")
        marca.create_oval(1, 1, self.px(9), self.px(9), outline=self.cor["vermelho"], width=2)
        marca.create_line(self.px(16), self.px(5), self.px(32), self.px(5), fill=self.cor["magenta"], width=2)
        tk.Label(legenda, text="anel detectado  ·  linha = centro da faixa ótima",
                 font=self.f["pequeno"], fg=self.cor["suave"], bg=self.cor["superficie"]).pack(side="left", padx=(self.px(4), 0))

    def _montar_rodape(self, cont):
        rodape = tk.Frame(cont, bg=self.cor["fundo"])
        rodape.pack(fill="x", pady=(self.px(5 if self.compacto else 8), 0))
        if "rodape" in self.icones:
            tk.Label(rodape, image=self.icones["rodape"], bg=self.cor["fundo"], bd=0).pack(side="left")
        tk.Label(rodape, text=ASSINATURA, font=self.f["pequeno_b"], fg=self.cor["acento_texto"],
                 bg=self.cor["fundo"]).pack(side="left", padx=(self.px(5), 0))
        tk.Label(rodape, text="  ·  ferramenta independente", font=self.f["pequeno"],
                 fg=self.cor["suave"], bg=self.cor["fundo"]).pack(side="left")
        tk.Label(rodape, text=f"{APP_NOME} v{APP_VERSAO}", font=self.f["pequeno"],
                 fg=self.cor["suave"], bg=self.cor["fundo"]).pack(side="right")

    # ------------------------------------------------------------ ações (mesma lógica da versão anterior)
    def avisar(self, texto, cor="ambar", segundos=5.0):
        self._aviso = (texto, cor, time.monotonic() + segundos)

    def alternar(self, pelo_atalho=False, janela_ativa=None):
        with self.shared.lock:
            if self.engine.armed:
                self.engine.armed = False
            else:
                if not self.shared.region:
                    self.avisar("Primeiro selecione a área do quadro Skill Check.")
                    return
                if (self.shared.janela_destino is None and pelo_atalho
                        and janela_ativa is not None and janela_ativa[1] != os.getpid()):
                    self.shared.janela_destino = janela_ativa
                if self.shared.janela_destino is None and not self.engine.dry:
                    self.avisar("Com a janela do Vesper em foco, pressione F8 para vincular e ligar.")
                    return
                self.engine.armed = True
        salvar_config(self.shared, self.engine)
        self._aviso = None

    def alterar_auto(self):
        with self.shared.lock:
            self.engine.auto = bool(self.auto_var.get())
        salvar_config(self.shared, self.engine)

    def alterar_teste(self):
        with self.shared.lock:
            self.engine.dry = bool(self.teste_var.get())
        if self.teste_var.get():
            self.avisar("MODO TESTE — previsão ativa, ESPAÇO não será enviado.")

    def _comp_mudou(self, valor_ms):
        with self.shared.lock:
            self.engine.comp = min(0.2, max(0.0, float(valor_ms) / 1000.0))

    def _comp_soltou(self, _valor_ms):
        salvar_config(self.shared, self.engine)

    def selecionar_area(self):
        root = self.root
        root.withdraw()
        root.update_idletasks()
        time.sleep(0.20)
        overlay = tk.Toplevel(root)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black", cursor="crosshair")
        # Tela virtual: inclui monitores adicionais quando o Windows os expõe.
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        else:
            vx, vy = 0, 0
            vw, vh = root.winfo_screenwidth(), root.winfo_screenheight()
        overlay.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
        try:
            overlay.attributes("-alpha", 0.25)
        except Exception:
            pass
        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        estado = {}

        def pressionar(evento):
            estado.update(x0=evento.x_root, y0=evento.y_root, cx=evento.x, cy=evento.y)
            estado["id"] = canvas.create_rectangle(
                evento.x, evento.y, evento.x, evento.y, outline=self.cor["vermelho_hover"], width=2)

        def arrastar(evento):
            if "id" in estado:
                canvas.coords(estado["id"], estado["cx"], estado["cy"], evento.x, evento.y)

        def soltar(evento):
            if "x0" not in estado:
                return
            x0, y0 = estado["x0"], estado["y0"]
            x1, y1 = evento.x_root, evento.y_root
            largura, altura = abs(x1 - x0), abs(y1 - y0)
            area_valida = largura > 120 and altura > 120
            if area_valida:
                overlay.withdraw()
                overlay.update_idletasks()
                destino = janela_no_ponto((x0 + x1) // 2, (y0 + y1) // 2)
            overlay.destroy()
            root.deiconify()
            if area_valida:
                with self.shared.lock:
                    self.shared.region = (min(x0, x1), min(y0, y1), largura, altura)
                    self.shared.frame = None
                    self.shared.janela_destino = destino
                salvar_config(self.shared, self.engine)
                if destino is None:
                    self.avisar("Área salva. Pressione F8 com o Vesper em foco para vincular a janela.")
                else:
                    self.avisar(f"Área salva: {largura} × {altura} px. Janela vinculada.", "verde_texto")
            else:
                self.avisar("Área muito pequena; selecione novamente.")

        def cancelar(_evento=None):
            overlay.destroy()
            root.deiconify()

        canvas.bind("<ButtonPress-1>", pressionar)
        canvas.bind("<B1-Motion>", arrastar)
        canvas.bind("<ButtonRelease-1>", soltar)
        overlay.bind("<Escape>", cancelar)
        overlay.focus_force()

    # ------------------------------------------------------------ monitoramento
    def _placeholder_preview(self):
        c, L = self.canvas, self.lado
        c.delete("all")
        m, t = self.px(14), self.px(16)
        for (x, y, dx, dy) in ((m, m, 1, 1), (L - m, m, -1, 1), (m, L - m, 1, -1), (L - m, L - m, -1, -1)):
            c.create_line(x, y, x + dx * t, y, fill=self.cor["borda_forte"], width=2)
            c.create_line(x, y, x, y + dy * t, fill=self.cor["borda_forte"], width=2)
        c.create_text(L / 2, L / 2, text="Aguardando captura\nda área selecionada",
                      fill=self.cor["suave"], font=self.f["pequeno"], justify="center")
        self._preview = {"foto": None, "tam": None}

    def _desenhar_preview(self, frame, geom, angulo_faixa):
        L = self.lado
        alt, larg = frame.shape[0], frame.shape[1]
        fator = min(L / float(larg), L / float(alt))
        tam = (max(1, int(larg * fator)), max(1, int(alt * fator)))
        imagem = Image.fromarray(np.ascontiguousarray(frame)).resize(tam, getattr(getattr(Image, "Resampling", Image), "BILINEAR"))
        if self._preview["foto"] is not None and self._preview["tam"] == tam:
            self._preview["foto"].paste(imagem)
        else:
            self._preview["foto"] = self.foto(imagem)
            self._preview["tam"] = tam
        ox, oy = (L - tam[0]) / 2.0, (L - tam[1]) / 2.0
        c = self.canvas
        c.delete("all")
        c.create_image(ox, oy, anchor="nw", image=self._preview["foto"])
        if geom is not None:
            cx, cy, raio = geom
            cx, cy, raio = ox + cx * fator, oy + cy * fator, raio * fator
            c.create_oval(cx - raio, cy - raio, cx + raio, cy + raio, outline=self.cor["vermelho"], width=2)
            if angulo_faixa is not None:
                a = math.radians(angulo_faixa)
                c.create_line(cx, cy, cx + raio * 1.15 * math.cos(a), cy + raio * 1.15 * math.sin(a),
                              fill=self.cor["magenta"], width=2)

    @staticmethod
    def _rotulo_motor(estado):
        return {"IDLE": "Ocioso", "TRACK": "Rastreando", "FROZEN": "Congelado"}.get(estado, estado)

    def _set(self, widget, chave, **opcoes):
        """Só reconfigura o widget quando algo mudou (reduz trabalho na thread da interface)."""
        valor = tuple(sorted(opcoes.items()))
        if self._cache.get(chave) != valor:
            self._cache[chave] = valor
            widget.configure(**opcoes)

    def atualizar(self):
        self._atualizar_claim()
        if self.aba != "painel":
            return
        with self.shared.lock:
            frame = None if self.shared.frame is None else self.shared.frame.copy()
            regiao = self.shared.region
            geom = self.engine.geom
            angulo_faixa = self.engine.band_ang
            estado = self.engine.state
            mensagem = self.engine.msg
            fps = self.shared.fps
            parrys = self.engine.parrys
            comp = self.engine.comp
            ultimo_erro = self.engine.last_err
            ligado = self.engine.armed
            teste = self.engine.dry

        # --- botão e chip de estado ---
        self.botao_energia.definir(ligado)
        if not ligado:
            self.chip.definir("DESLIGADO", self.cor["suave"])
        elif estado == "FROZEN":
            self.chip.definir("PARRY ENVIADO" if not teste else "PARRY (TESTE)", self.cor["verde_texto"])
        elif estado == "TRACK" and angulo_faixa is not None:
            self.chip.definir("MIRANDO" + (" · TESTE" if teste else ""), self.cor["verde_texto"])
        elif estado == "TRACK":
            self.chip.definir("DETECTANDO" + (" · TESTE" if teste else ""), self.cor["ambar"])
        else:
            self.chip.definir("AGUARDANDO" + (" · TESTE" if teste else ""), self.cor["suave"])

        # --- mensagem de status (avisos da interface têm prioridade por alguns segundos) ---
        agora = time.monotonic()
        if self._aviso and agora < self._aviso[2]:
            texto, cor = self._aviso[0], self.cor[self._aviso[1]]
        else:
            self._aviso = None
            if not regiao:
                texto, cor = "1) Selecione a área do quadro Skill Check   2) Ligue (F8)", self.cor["suave"]
            else:
                texto = mensagem
                baixo = mensagem.lower()
                if any(p in baixo for p in ("foco", "falha", "indisponível", "ausente", "ignorada", "erro", "bloqueado")):
                    cor = self.cor["ambar"]
                else:
                    cor = self.cor["suave"]
        self._set(self.status, "status", text=texto, fg=cor)

        # --- área e compensação ---
        if regiao:
            self._set(self.area_info, "area", text=f"●  {regiao[2]} × {regiao[3]} px   ·   x {regiao[0]}  y {regiao[1]}",
                      fg=self.cor["verde_texto"])
        else:
            self._set(self.area_info, "area", text="●  Nenhuma área definida", fg=self.cor["ambar"])
        self._set(self.valor_comp, "comp_txt", text=f"{comp * 1000.0:.0f} ms")
        self.slider.definir(comp * 1000.0)              # sincroniza com o autoajuste, sem notificar

        # --- números do monitoramento ---
        self._set(self.valores["motor"], "v_motor", text=self._rotulo_motor(estado))
        self._set(self.valores["fps"], "v_fps", text=f"{fps:.0f} fps")
        self._set(self.valores["parrys"], "v_parrys", text=f"{parrys}")
        self._set(self.valores["comp"], "v_comp", text=f"{comp * 1000.0:.1f} ms")
        if ultimo_erro is None:
            self._set(self.valores["erro_ang"], "v_ea", text="—", fg=self.cor["texto"])
            self._set(self.valores["erro_ms"], "v_em", text="—", fg=self.cor["texto"])
        else:
            dentro = abs(ultimo_erro[0]) <= 5.0          # ±5° ≈ meia largura da faixa branca
            cor_erro = self.cor["verde_texto"] if dentro else self.cor["ambar"]
            self._set(self.valores["erro_ang"], "v_ea", text=f"{ultimo_erro[0]:+.1f}°", fg=cor_erro)
            self._set(self.valores["erro_ms"], "v_em", text=f"{ultimo_erro[1]:+.0f} ms", fg=cor_erro)

        # --- preview (a cada ciclo; pulado se a janela estiver minimizada) ---
        try:
            minimizada = self.root.state() == "iconic"
        except Exception:
            minimizada = False
        if not minimizada:
            if frame is not None and regiao:
                self._desenhar_preview(frame, geom, angulo_faixa)
            elif frame is None and self._preview["foto"] is not None:
                self._placeholder_preview()


def aplicar_barra_tema(root, cores):
    """Aplica as cores do tema à barra de título do Windows."""
    if _USER32_FOCO is None:
        return
    try:
        root.update_idletasks()
        hwnd = _USER32_FOCO.GetAncestor(root.winfo_id(), 2)
        dwm = ctypes.windll.dwmapi

        def atributo(numero, valor):
            v = ctypes.c_int(valor)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), numero, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hexa):
            r, g, b = _rgb(hexa)
            return r | (g << 8) | (b << 16)

        atributo(20, int(cores["barra_escura"]))
        atributo(35, colorref(cores["fundo"]))
        atributo(36, colorref(cores["texto"]))
        atributo(34, colorref(cores["borda"]))
    except Exception:
        pass


def main():
    try:
        if tk is None:
            raise ImportError("tkinter não está disponível nesta instalação do Python")
        from pynput.keyboard import Controller, Key, GlobalHotKeys
    except ImportError as exc:
        print("\nNão foi possível iniciar: falta uma dependência da interface.")
        print("Instale com: py -m pip install mss numpy pillow pynput")
        print("Detalhe:", exc)
        input("\nPressione Enter para fechar...")
        return

    if mss is None or Image is None or ImageDraw is None or ImageTk is None:
        print("\nNão foi possível iniciar: falta mss ou Pillow.")
        print("Instale com: py -m pip install mss numpy pillow pynput")
        input("\nPressione Enter para fechar...")
        return

    if sys.platform == "win32":
        try:    # identidade própria na barra de tarefas (em vez do ícone do Python)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    shared = Shared()
    engine = Engine()
    carregar_config(shared, engine)

    root = tk.Tk()
    root.title(f"{APP_NOME} v{APP_VERSAO} — {ASSINATURA}")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    # Impede que ESPAÇO ative botões/checkboxes da própria interface.
    for classe in ("Button", "TButton", "Checkbutton", "TCheckbutton"):
        root.bind_class(classe, "<space>", lambda _evento: "break")

    interface = Interface(root, shared, engine)

    def ciclo():
        try:
            interface.atualizar()
        except Exception as exc:          # a interface nunca deve derrubar a captura
            with shared.lock:
                engine.msg = f"Erro na interface: {exc}"
        root.after(100, ciclo)

    threading.Thread(
        target=trabalhador,
        args=(shared, engine, Controller(), Key.space),
        daemon=True,
        name="CapturaKirae",
    ).start()

    def alternar_por_f8():
        janela_ativa = janela_em_primeiro_plano()
        root.after(0, interface.alternar, True, janela_ativa)

    atalho = GlobalHotKeys({"<f8>": alternar_por_f8})
    atalho.daemon = True
    atalho.start()

    def fechar():
        with shared.lock:
            shared.running = False
            engine.armed = False
        try:
            atalho.stop()
        except Exception:
            pass
        salvar_config(shared, engine)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", fechar)
    ciclo()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        detalhe = traceback.format_exc()
        try:
            print(detalhe)
            input("\nOcorreu um erro acima. Pressione Enter para fechar...")
        except Exception:        # sem console (executável --windowed): grava um log
            try:
                with open(os.path.join(BASE_DIR, "auto_parry_erro.log"), "w", encoding="utf-8") as log:
                    log.write(detalhe)
            except Exception:
                pass
