import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import numpy as np

import auto_parry_kirae as app


ALVO = (101, 9001)
OUTRA_JANELA = (102, 9001)
MESMO_HWND_OUTRO_PROCESSO = (101, 9002)
PROPRIA = (103, os.getpid())
INSTANTE = 42.0


class TestTrabalhadorDestino(unittest.TestCase):
    def executar_rodada(self, destino, foco, modo_teste=False, novo_destino=None):
        compartilhado = app.Shared()
        compartilhado.region = (0, 0, 2, 2)
        compartilhado.janela_destino = destino
        motor = Mock(spec=app.Engine, armed=True, dry=modo_teste)
        motor.step.side_effect = [INSTANTE, None]
        teclado = Mock()
        capturador = Mock()

        def capturar(_regiao):
            # Encerra depois da primeira tentativa, sem iniciar outra rodada.
            if capturador.grab.call_count == 2:
                compartilhado.running = False
            return np.zeros((2, 2, 4), dtype=np.uint8)

        def esperar(_instante):
            if novo_destino is not None:
                compartilhado.janela_destino = novo_destino

        def evitar_retentativa(segundos):
            if segundos >= 0.25:
                compartilhado.running = False

        capturador.grab.side_effect = capturar
        with (
            patch.object(app, "mss") as mss,
            patch.object(app, "janela_em_primeiro_plano", return_value=foco),
            patch.object(app, "espera_precisa", side_effect=esperar) as espera,
            patch.object(app.time, "sleep", side_effect=evitar_retentativa),
        ):
            mss.mss.return_value.__enter__.return_value = capturador
            app.trabalhador(compartilhado, motor, teclado, "espaço simulado")

        espera.assert_called_once_with(INSTANTE)
        motor.reset.assert_not_called()
        return motor, teclado

    def test_envia_somente_quando_identidade_do_foco_corresponde(self):
        motor, teclado = self.executar_rodada(ALVO, ALVO)

        self.assertEqual(teclado.mock_calls, [
            call.press("espaço simulado"),
            call.release("espaço simulado"),
        ])
        motor.notify_pressed.assert_called_once()
        motor.block_current_round.assert_not_called()

    def test_bloqueia_foco_ausente_ou_em_outra_janela(self):
        for foco in (None, OUTRA_JANELA, MESMO_HWND_OUTRO_PROCESSO, PROPRIA):
            with self.subTest(foco=foco):
                motor, teclado = self.executar_rodada(ALVO, foco)

                self.assertEqual(teclado.mock_calls, [])
                motor.notify_pressed.assert_not_called()
                motor.block_current_round.assert_called_once()

    def test_bloqueia_quando_nao_ha_destino_registrado(self):
        for foco in (None, ALVO):
            with self.subTest(foco=foco):
                motor, teclado = self.executar_rodada(None, foco)

                self.assertEqual(teclado.mock_calls, [])
                motor.notify_pressed.assert_not_called()
                motor.block_current_round.assert_called_once()

    def test_bloqueia_se_destino_mudou_durante_a_espera(self):
        mesmo_alvo_revinculado = (ALVO[0], ALVO[1])
        for novo in (OUTRA_JANELA, mesmo_alvo_revinculado):
            with self.subTest(novo=novo):
                motor, teclado = self.executar_rodada(ALVO, novo, novo_destino=novo)

                self.assertEqual(teclado.mock_calls, [])
                motor.notify_pressed.assert_not_called()
                motor.block_current_round.assert_called_once()

    def test_modo_teste_preve_sem_destino_e_sem_teclado(self):
        motor, teclado = self.executar_rodada(None, None, modo_teste=True)

        self.assertEqual(teclado.mock_calls, [])
        motor.notify_test_prediction.assert_called_once_with(INSTANTE)
        motor.notify_pressed.assert_not_called()
        motor.block_current_round.assert_not_called()


class TestAtalhoDestino(unittest.TestCase):
    def setUp(self):
        self.compartilhado = app.Shared()
        self.compartilhado.region = (0, 0, 2, 2)
        self.motor = app.Engine()
        self.interface = SimpleNamespace(
            shared=self.compartilhado, engine=self.motor, avisar=Mock(), _aviso=None,
        )

    def alternar(self, pelo_atalho=False, janela_ativa=None):
        with patch.object(app, "salvar_config"):
            app.Interface.alternar(self.interface, pelo_atalho=pelo_atalho, janela_ativa=janela_ativa)

    def test_f8_registra_destino_e_desarma_de_outra_janela(self):
        self.alternar(pelo_atalho=True, janela_ativa=ALVO)
        self.assertTrue(self.motor.armed)
        self.assertEqual(self.compartilhado.janela_destino, ALVO)

        self.alternar(pelo_atalho=True, janela_ativa=OUTRA_JANELA)
        self.assertFalse(self.motor.armed)
        self.assertEqual(self.compartilhado.janela_destino, ALVO)

        self.alternar(pelo_atalho=True, janela_ativa=OUTRA_JANELA)
        self.assertTrue(self.motor.armed)
        self.assertEqual(self.compartilhado.janela_destino, ALVO)

        self.alternar()
        self.assertFalse(self.motor.armed)

        self.alternar()
        self.assertTrue(self.motor.armed)
        self.assertEqual(self.compartilhado.janela_destino, ALVO)

    def test_f8_sem_destino_externo_nao_arma_envio(self):
        for foco in (None, PROPRIA):
            with self.subTest(foco=foco):
                self.alternar(pelo_atalho=True, janela_ativa=foco)

                self.assertFalse(self.motor.armed)
                self.assertIsNone(self.compartilhado.janela_destino)

    def test_botao_sem_destino_nao_arma_envio(self):
        self.alternar()

        self.assertFalse(self.motor.armed)
        self.assertIsNone(self.compartilhado.janela_destino)

    def test_botao_em_modo_teste_arma_sem_destino(self):
        self.motor.dry = True

        self.alternar()

        self.assertTrue(self.motor.armed)
        self.assertIsNone(self.compartilhado.janela_destino)


if __name__ == "__main__":
    unittest.main()
