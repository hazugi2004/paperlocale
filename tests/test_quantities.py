"""数值—单位配对回归同时服务GPT/Qwen；不请求网络或改变模型协议。"""
import unittest
from paperlocale.contracts import validate_translation, scientific_quantities
from paperlocale.domains import load_domain_pack
from paperlocale.providers.base import Segment, TranslationContext
from paperlocale.providers.codex_local import _keyed_request

class QuantityTests(unittest.TestCase):
    def test_natural_aliases_and_compound_notation(self):
        for source, target in [
            ('50 km', '50千米'), ('10 m', '10米'), ('500 hPa', '500百帕'),
            ('20 °C', '20摄氏度'), ('5 m s−1', '5米/秒'),
            ('3.2 mm d−1', '3.2毫米/天'), ('18.5 kg m−2', '18.5千克/平方米'),
            ('250 W m−2', '250瓦/平方米'), ('12 μmol m−2 s−1', '12 µmol/m²/s'),
            ('2 Pg C yr−1', '2 PgC/year'), ('10 gC m−2 yr−1', '10 g C/m²/year'),
            ('2 km²', '2平方公里'), ('5 m3', '5立方米'), ('5 m³', '5 m^3'),
            ('850-hPa', '850百帕'), ('2-m temperature', '2米温度'),
            ('0–10 cm', '0-10厘米'), ('0.25° × 0.25°', '0.25度 × 0.25度'),
            ('400 ppm', '400 ppm'), ('10−6 K−1', '10−6 K−1'),
            ('2 µmol CO2 m−2 s−1', '2 μmol CO2/m²/s'),
        ]:
            with self.subTest(source=source, target=target):
                self.assertTrue(scientific_quantities(source))
                self.assertEqual(validate_translation(source,target), [])

    def test_omission_wrong_scale_temperature_and_binding_fail(self):
        for source,target in [('50 km','50'), ('500 hPa','500 Pa'), ('10 km','10 m'),
            ('20 °C','20 K'), ('5 m s−1','5 m'), ('10 m and 50 km','10 km和50 m'),
            ('10 km and 10 km','10 km'), ('5 m²','5 m³'), ('50 km','50000 m')]:
            with self.subTest(source=source,target=target):
                self.assertTrue(any('quantity' in error for error in validate_translation(source,target)))

    def test_words_identifiers_and_decades_do_not_become_quantities(self):
        for text in ['models 12 measurements', 'mid-1960s', 'GLDAS-2.0 data',
                     'https://example.test/50km', '{v50}', 'Météorologiques']:
            self.assertEqual(scientific_quantities(text), [], text)
        self.assertEqual(validate_translation('mid-1960s', '1960年代中期'), [])
        self.assertEqual(validate_translation('15°N', '北纬15度'), [])

    def test_standalone_units_allow_aliases_but_not_lost_factors(self):
        self.assertEqual(validate_translation('km','千米'), [])
        self.assertEqual(validate_translation('m/s','米/秒'), [])
        self.assertTrue(validate_translation('m s−1','m'))

    def test_gpt_prompt_has_joint_constraints_without_changing_key_schema(self):
        context=TranslationContext('en','zh-CN',load_domain_pack('atmospheric-science'))
        prompt,schema=_keyed_request([Segment('original-hash','Measure 500 hPa and 50 km.')],context)
        self.assertIn('scientific_quantities',prompt)
        self.assertIn('500 hPa',prompt)
        self.assertEqual(schema['properties']['translations']['required'],['s1'])
        self.assertNotIn('original-hash',prompt)
