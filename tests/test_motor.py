import math
import unittest

import numpy as np

from auto_parry_kirae import Engine, detectar_faixa, diferenca_angular_180


CENTRO = 120
COMPRIMENTO = 96
RAIO = 84.5
GEOMETRIA = (CENTRO, CENTRO, RAIO)
FAIXA = (355, 5, 30)
VELOCIDADE = 325.5
INICIO_ROTACAO = 0.473

Y, X = np.ogrid[-CENTRO:CENTRO + 1, -CENTRO:CENTRO + 1]
DISTANCIA = np.hypot(X, Y)
ANGULOS = np.degrees(np.arctan2(Y, X)) % 360


def quadro(ponteiro=None, faixa=None, anel_branco=False):
    """Desenha o mostrador RGB sem depender de vídeo, relógio ou interface."""
    imagem = np.empty((*DISTANCIA.shape, 3), dtype=np.uint8)
    imagem[:] = (10, 11, 18)
    anel = np.abs(DISTANCIA - RAIO) <= 3
    imagem[anel] = (252, 254, 255) if anel_branco else (48, 49, 56)

    if faixa is not None:
        inicio, divisao, fim = faixa
        arco = np.abs(DISTANCIA - RAIO) <= 7
        desde_inicio = (ANGULOS - inicio) % 360
        imagem[arco & (desde_inicio < (fim - inicio) % 360)] = (159, 161, 163)
        imagem[arco & (desde_inicio < (divisao - inicio) % 360)] = (252, 254, 255)

    if ponteiro is not None:
        angulo = math.radians(ponteiro)
        seno = math.sin(angulo)
        cosseno = math.cos(angulo)
        longitudinal = X * cosseno + Y * seno
        transversal = np.abs(X * seno - Y * cosseno)
        haste = (longitudinal >= 0) & (longitudinal <= COMPRIMENTO) & (transversal <= 1.9)
        imagem[haste | (DISTANCIA <= 4)] = (229, 65, 96)

    return imagem


class TestMotor(unittest.TestCase):
    def test_anel_branco_de_abertura_sem_faixa_nao_agenda(self):
        motor = Engine()
        motor.armed = True
        imagem = quadro(ponteiro=270, anel_branco=True)

        for instante in (0.0, 0.12, 0.24, 0.36, 0.48):
            self.assertIsNone(motor.step(imagem, instante))

        self.assertEqual(motor.state, "TRACK")
        self.assertIsNone(motor.band)
        self.assertFalse(motor.committed)

    def test_rejeita_branco_isolado_e_cinza_no_sentido_errado(self):
        isolado = quadro(faixa=(355, 5, 5))
        ordem_invertida = quadro(faixa=(355, 5, 5))
        cinza = (np.abs(DISTANCIA - RAIO) <= 7) & (ANGULOS >= 330) & (ANGULOS < 355)
        ordem_invertida[cinza] = (159, 161, 163)

        for imagem in (isolado, ordem_invertida):
            self.assertIsNone(detectar_faixa(imagem, GEOMETRIA))

    def test_rejeita_branco_presente_em_apenas_um_raio(self):
        imagem = quadro(faixa=FAIXA)
        branco = ((ANGULOS - 355) % 360) < 10
        imagem[branco & (DISTANCIA < RAIO)] = (10, 11, 18)

        self.assertIsNone(detectar_faixa(imagem, GEOMETRIA))

    def test_encontra_faixa_branca_que_atravessa_zero_grau(self):
        faixa = detectar_faixa(quadro(faixa=FAIXA), GEOMETRIA)

        self.assertIsNotNone(faixa)
        self.assertAlmostEqual(diferenca_angular_180(faixa["center"]), 0, delta=1)
        self.assertAlmostEqual(faixa["width"], 10, delta=1)
        self.assertAlmostEqual(faixa["gend"], 30, delta=1)

    def test_agenda_quando_o_fim_cinza_cruza_as_doze_horas(self):
        motor = Engine()
        motor.armed = True
        previsoes = []
        faixa = (245, 255, 292)

        # O centro fica a 340° de progresso e o fim cinza a 382°.
        for numero in range(192):
            agora = numero / 120
            progresso = VELOCIDADE * max(0.0, agora - INICIO_ROTACAO)
            agendamento = motor.step(quadro(ponteiro=270 + progresso, faixa=faixa), agora)
            if agendamento is not None:
                previsoes.append(agendamento)

        self.assertEqual(len(previsoes), 1, motor.msg)
        self.assertAlmostEqual(motor.p_target, 340, delta=2)
        self.assertAlmostEqual(motor.p_gend, 382, delta=2)
        t_previsto = INICIO_ROTACAO + 340 / VELOCIDADE - motor.comp
        self.assertAlmostEqual(previsoes[0], t_previsto, delta=0.006)
        self.assertFalse(motor.late)

    def test_agenda_uma_vez_e_so_ajusta_apos_congelamento(self):
        motor = Engine()
        motor.armed = True
        comp_inicial = motor.comp
        previsoes = []

        # Avança até a janela de decisão com timestamps simulados de 120 fps.
        for numero in range(88):
            agora = numero / 120
            progresso = VELOCIDADE * max(0.0, agora - INICIO_ROTACAO)
            agendamento = motor.step(quadro(ponteiro=270 + progresso, faixa=FAIXA), agora)
            if agendamento is not None:
                previsoes.append(agendamento)

        self.assertEqual(len(previsoes), 1)
        t_previsto = INICIO_ROTACAO + 90 / VELOCIDADE - comp_inicial
        self.assertAlmostEqual(previsoes[0], t_previsto, delta=0.006)
        self.assertFalse(motor.late)

        # O jogo teria registrado um aperto tardio em relação ao centro visual.
        registro = agora + 0.01
        motor.notify_pressed(registro)
        congelado = quadro(ponteiro=5, faixa=FAIXA)
        self.assertIsNone(motor.step(congelado, registro + 0.01))
        self.assertIsNone(motor.step(congelado, registro + 0.06))
        self.assertEqual(motor.parrys, 0)
        self.assertEqual(motor.comp, comp_inicial)

        self.assertIsNone(motor.step(congelado, registro + 0.09))
        self.assertEqual(motor.state, "FROZEN")
        self.assertEqual(motor.parrys, 1)
        self.assertAlmostEqual(motor.last_err[0], 5, delta=1)
        self.assertAlmostEqual(motor.last_err[1], 5 / VELOCIDADE * 1000, delta=4)
        comp_ajustada = comp_inicial + 0.6 * motor.last_err[1] / 1000
        self.assertAlmostEqual(motor.comp, comp_ajustada)

        self.assertIsNone(motor.step(congelado, registro + 0.20))
        self.assertEqual(motor.parrys, 1)
        self.assertAlmostEqual(motor.comp, comp_ajustada)

        ausente = quadro()
        for instante in (registro + 0.30, registro + 0.56):
            self.assertIsNone(motor.step(ausente, instante))
        self.assertEqual(motor.state, "IDLE")


if __name__ == "__main__":
    unittest.main()
