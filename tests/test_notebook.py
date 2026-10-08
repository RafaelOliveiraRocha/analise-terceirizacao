"""Regressões das células reais, com entradas e saídas em diretório temporário."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parents[1]
CELLS = [''.join(c['source']) for c in json.loads((ROOT / 'analise.ipynb').read_text())['cells']
         if c['cell_type'] == 'code']


class NotebookRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='terceirizacao-teste-')
        self.folder = Path(self.temp.name)
        self.inputs = self.folder / 'inputs'
        self.output = self.folder / 'outputs'
        self.log = io.StringIO()

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, scenario='principal', headers=False):
        self.inputs.mkdir(exist_ok=True)
        source = ROOT if headers else ROOT / 'examples' / scenario
        for name in ['extrato-rede_parceira.csv', 'terceirizados.csv']:
            shutil.copy2(source / name, self.inputs / name)
        ns = {}
        with contextlib.redirect_stdout(self.log):
            exec(CELLS[0], ns)
        ns.update(CENARIO=scenario, PASTA_ENTRADAS=self.inputs, PASTA_SAIDAS=self.output,
                  SAIDA_CSV=self.output / 'resultado.csv')
        return ns

    def execute(self, ns, indices=range(1, 5)):
        with contextlib.redirect_stdout(self.log), patch.object(go.Figure, 'write_html'):
            for index in indices:
                exec(compile(CELLS[index], f'analise.ipynb:celula-{index + 1}', 'exec'), ns)
        return ns

    def change_plataforma(self, column, value, row=0):
        p = self.inputs / 'terceirizados.csv'
        df = pd.read_csv(p).astype(object)
        df.loc[row, column] = value
        df.to_csv(p, index=False)

    def change_prestador(self, column, value, row=0):
        p = self.inputs / 'extrato-rede_parceira.csv'
        prefix = p.read_text().splitlines(keepends=True)[:5]
        df = pd.read_csv(p, skiprows=5).astype(object)
        df.loc[row, column] = value
        p.write_text(''.join(prefix) + df.to_csv(index=False))

    def test_principal_and_reference_csv(self):
        ns = self.execute(self.prepare())
        expected = pd.read_csv(ROOT / 'examples/principal/resultado-esperado.csv')
        pd.testing.assert_frame_equal(pd.read_csv(ns['SAIDA_CSV']), expected)
        self.assertEqual(len(ns['resultado']), 3)
        self.assertEqual(ns['contagens']['Resultado'], {'linhas': 3, 'ids_distintos': 3})
        self.assertEqual((ns['soma_total_prestador'], ns['soma_total_cliente'], ns['soma_diferenca']),
                         (30.5, 30.0, 0.5))
        self.assertEqual(ns['resultado']['DIFERENÇA PRESTADOR - CLIENTE'].tolist(),
                         ['R$ 2,50', 'R$ -2,00', 'R$ 0,00'])
        self.assertEqual(ns['dados_agrupados']['DATA'].tolist(),
                         ['2024-01-02', '2024-01-03', '2024-01-04'])
        self.assertFalse(ns['duplicidades_no_pareamento'])

    def test_duplicate_reference_and_exclusions(self):
        ns = self.execute(self.prepare('duplicidades'))
        pd.testing.assert_frame_equal(pd.read_csv(ns['SAIDA_CSV']),
                                      pd.read_csv(ROOT / 'examples/duplicidades/resultado-esperado.csv'))
        self.assertEqual(ns['contagens']['Resultado'], {'linhas': 5, 'ids_distintos': 2})
        self.assertEqual(ns['resultado']['ID PEDIDO'].value_counts().to_dict(), {1002: 4, 1001: 1})
        self.assertEqual((ns['soma_total_prestador'], ns['soma_total_cliente'], ns['soma_diferenca']),
                         (52.5, 38.0, 14.5))
        self.assertEqual(ns['contagens']['Prestador'], dict(entrada=8, excluidas_TYPE=1,
                         excluidas_STATUS=1, sem_identificador=1, chaves_repetidas=1,
                         linhas_em_chaves_repetidas=2, nao_pareadas=2))
        self.assertEqual(ns['contagens']['Plataforma'], dict(entrada=5, excluidas_TELEFONE=1,
                         chaves_repetidas=1, linhas_em_chaves_repetidas=2, nao_pareadas=1))
        self.assertIn('multiplicar custos', self.log.getvalue())
        self.assertIn('R$ 14,50', ns['fig'].layout.title.text)
        # Datas históricas/textuais continuam sem nova regra de parsing ou período.
        self.assertEqual(ns['dados_agrupados']['DATA'].tolist(),
                         ['10 de jan. de 2024', '2 de jan. de 2024'])

    def test_empty_inputs_write_only_headers_without_chart(self):
        ns = self.execute(self.prepare(headers=True))
        self.assertTrue(pd.read_csv(ns['SAIDA_CSV']).empty)
        self.assertIsNone(ns['fig'])
        self.assertIn('Resultado vazio', self.log.getvalue())
        self.assertEqual(ns['contagens']['Resultado'], {'linhas': 0, 'ids_distintos': 0})

    def test_no_matches_counts_both_sides_without_chart(self):
        ns = self.prepare()
        p = self.inputs / 'terceirizados.csv'
        df = pd.read_csv(p)
        df['PEDIDO'] += 9000
        df.to_csv(p, index=False)
        self.execute(ns)
        self.assertEqual(ns['contagens']['Prestador']['nao_pareadas'], 3)
        self.assertEqual(ns['contagens']['Plataforma']['nao_pareadas'], 3)
        self.assertTrue(pd.read_csv(ns['SAIDA_CSV']).empty)
        self.assertIsNone(ns['fig'])

    def test_title_positive_negative_and_zero(self):
        for cost, client, delta, text in [(2, 1, 1, 'acima'), (1, 2, -1, 'abaixo'),
                                          (1, 1, 0, 'igual')]:
            with self.subTest(delta=delta):
                ns = self.prepare()
                # Um par, para observar o sinal sem compensações entre datas.
                for name, skip in [('extrato-rede_parceira.csv', 5), ('terceirizados.csv', 0)]:
                    p = self.inputs / name
                    prefix = p.read_text().splitlines(keepends=True)[:skip]
                    df = pd.read_csv(p, skiprows=skip).head(1)
                    if skip: df['CREDITS (+/-)'] = -cost
                    else: df['VALOR PAGO CLIENTE'] = client
                    p.write_text(''.join(prefix) + df.to_csv(index=False))
                self.execute(ns)
                self.assertEqual(ns['soma_diferenca'], delta)
                self.assertIn(text, ns['fig'].layout.title.text)
                self.assertIn(f'R$ {delta:.2f}'.replace('.', ','), ns['fig'].layout.title.text)

    def test_required_columns_messages(self):
        for name, column, skip in [('extrato-rede_parceira.csv', 'REFUND DATE', 5),
                                   ('terceirizados.csv', 'NOME', 0)]:
            with self.subTest(column=column):
                ns = self.prepare()
                p = self.inputs / name
                prefix = p.read_text().splitlines(keepends=True)[:skip]
                df = pd.read_csv(p, skiprows=skip).drop(columns=[column])
                p.write_text(''.join(prefix) + df.to_csv(index=False))
                with self.assertRaisesRegex(ValueError, column): self.execute(ns)
                self.assertFalse(ns['resultado_pronto'])

    def test_invalid_or_fractional_ids(self):
        for value in [1001.9, None, 'pedido-invalido', 'inf']:
            with self.subTest(value=value):
                ns = self.prepare()
                self.change_plataforma('PEDIDO', value)
                with self.assertRaisesRegex(ValueError, 'PEDIDO.*inteiros.*fracionária'):
                    self.execute(ns)
                self.assertFalse(ns['resultado_pronto'])
                self.assertFalse(ns['SAIDA_CSV'].exists())

    def test_incompatible_or_missing_money(self):
        for value in [None, '10,50', 'R$ 10', 'inf']:
            with self.subTest(value=value):
                ns = self.prepare()
                self.change_plataforma('VALOR PAGO CLIENTE', value)
                with self.assertRaisesRegex(ValueError, 'VALOR PAGO CLIENTE.*ponto decimal'):
                    self.execute(ns)
        ns = self.prepare()
        self.change_prestador('CREDITS (+/-)', None)
        with self.assertRaisesRegex(ValueError, 'CREDITS'):
            self.execute(ns)

    def test_missing_date_is_not_silently_dropped(self):
        for value in [None, ' ', '']:
            with self.subTest(value=value):
                ns = self.prepare()
                self.change_plataforma('DATA', value)
                with self.assertRaisesRegex(ValueError, 'DATA ausente'):
                    self.execute(ns)
                self.assertFalse(ns['resultado_pronto'])

    def test_old_result_blocked_after_failure_and_out_of_order(self):
        ns = self.execute(self.prepare())
        previous = ns['SAIDA_CSV'].read_bytes()
        self.change_plataforma('PEDIDO', 1001.9)
        # Reiniciar configuração invalida estado, mesmo havendo CSV anterior no disco.
        with contextlib.redirect_stdout(self.log): exec(CELLS[0], ns)
        ns.update(PASTA_ENTRADAS=self.inputs, PASTA_SAIDAS=self.output,
                  SAIDA_CSV=self.output / 'resultado.csv')
        with self.assertRaises(ValueError): self.execute(ns)
        self.assertEqual(ns['SAIDA_CSV'].read_bytes(), previous)
        with self.assertRaisesRegex(RuntimeError, 'CSV antigo'): self.execute(ns, [4])
        self.assertIsNone(ns['fig'])
        with self.assertRaisesRegex(RuntimeError, 'CSV antigo'): self.execute({}, [4])
        ns = self.prepare()
        with self.assertRaisesRegex(RuntimeError, 'fora de ordem'): self.execute(ns, [2])

    def test_modified_result_blocked(self):
        ns = self.execute(self.prepare())
        ns['SAIDA_CSV'].write_text('arquivo substituido\n')
        with self.assertRaisesRegex(RuntimeError, 'alterado'): self.execute(ns, [4])
        self.assertFalse(ns['resultado_pronto'])
        self.assertIsNone(ns['fig'])

    def test_original_rules_and_precision_preserved(self):
        ns = self.prepare()
        self.change_prestador('ORDER REMARK', 'Referência #001001abc e #9999')
        self.change_prestador('CREDITS (+/-)', -2.675)
        self.change_plataforma('TELEFONE', ' ')
        self.change_plataforma('VALOR PAGO CLIENTE', 0)
        self.execute(ns)
        row = ns['resultado'].iloc[0]
        self.assertEqual(row['ID PEDIDO'], 1001)
        self.assertEqual(row['TOTAL PAGO PRESTADOR'], 2.675)
        self.assertEqual(row['DIFERENÇA PRESTADOR - CLIENTE'], 'R$ 2,67')
        ns = self.prepare()
        self.change_prestador('CREDITS (+/-)', 12.5)
        self.execute(ns)
        self.assertEqual(ns['resultado'].iloc[0]['TOTAL PAGO PRESTADOR'], -12.5)
        ns = self.prepare()
        self.change_prestador('TYPE', 'order')
        self.change_plataforma('TELEFONE', None, row=1)
        self.execute(ns)
        self.assertEqual(ns['contagens']['Prestador']['excluidas_TYPE'], 1)
        self.assertEqual(ns['contagens']['Plataforma']['excluidas_TELEFONE'], 1)


if __name__ == '__main__':
    unittest.main()
