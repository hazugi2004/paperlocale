"""数值—单位配对回归同时服务GPT/Qwen；不请求网络或改变模型协议。"""
import unittest
from paperlocale.contracts import validate_translation, scientific_quantities, unresolved_statistical_denominator
from paperlocale.domains import load_domain_pack
from paperlocale.quantities import find_quantities
from paperlocale.providers.base import Segment, TranslationContext
from paperlocale.providers.codex_local import _keyed_request

class QuantityTests(unittest.TestCase):
    def test_return_period_event_is_not_year_times_event(self):
        for source, target in [
            ('a 100-year event', '100年一遇事件'),
            ('20-year events become 8-year events', '20年一遇事件变为8年一遇事件'),
            ('20- and 50-year events', '20年和50年一遇事件'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(validate_translation(source, target), [])
        for source, target in [
            ('a 100-year event', '50年一遇事件'),
            ('a 100-year event', '100次事件'),
            ('100 events per year', '100年一遇事件'),
            ('100 year·event', '100年'),
        ]:
            with self.subTest(source=source):
                self.assertTrue(validate_translation(source, target))

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

    def test_year_named_plural_events_require_geographic_context(self):
        source = 'such as the 1976 events of the Am region.'
        self.assertEqual(validate_translation(source, '例如Am地区1976年的事件。'), [])
        self.assertTrue(validate_translation(source, '例如Am地区的事件。'))
        self.assertTrue(find_quantities('The 1976 events were counted.'))
        self.assertTrue(find_quantities('1976 events yr−1'))
        self.assertEqual(validate_translation('in the 1995 and 1998 events', '在1995年和1998年的事件中'), [])
        self.assertTrue(find_quantities('We counted 1995 and 1998 events.'))
        self.assertTrue(find_quantities('the 1995 and 1998 events yr−1'))

    def test_calendar_event_and_ordinal_are_not_units(self):
        source = 'The 2019 event was long-lasting. The second event lasted longer.'
        self.assertEqual(scientific_quantities(source), [])
        self.assertEqual(validate_translation(source,
                         '2019年的事件持续较久。第二次事件持续时间更长。'), [])
        # 放行普通语义不能丢失年份；显式次数、频率、秒及乘积仍严格保护。
        self.assertTrue(validate_translation(source,
                        '2020年的事件持续较久。第二次事件持续时间更长。'))
        for text in ('2019 events', '2 event', '2019 event yr−1', '5 seconds'):
            with self.subTest(text=text):
                self.assertTrue(scientific_quantities(text))
                self.assertTrue(validate_translation(text, '发生了事件。'))
        self.assertEqual(validate_translation('2 events', '2次事件'), [])
        self.assertEqual(validate_translation('5 seconds', '5秒'), [])
        self.assertTrue(validate_translation('second·event', '事件'))
        self.assertTrue(validate_translation('event s−1', '事件'))

    def test_chinese_event_frequency_keeps_per_year(self):
        self.assertEqual(validate_translation('less than one event per year', '每年不到1个事件/年'), [])
        self.assertEqual(validate_translation('less than one event per year', '平均每年不到1次事件'), [])
        self.assertEqual(validate_translation('2 events per year', '每年2次事件'), [])
        self.assertTrue(validate_translation('2 events per year', '每年3次事件'))
        self.assertTrue(validate_translation('2 events per year', '每天2次事件'))
        self.assertTrue(validate_translation('event per year', '事件'))
        self.assertTrue(validate_translation('2 events/year', '2个事件/天'))

    def test_wet_day_hour_alternatives_are_not_a_divided_unit(self):
        self.assertEqual(validate_translation(
            'All-day/hour percentiles differ from wet-day/hour percentiles.',
            '全日/全小时百分位数与湿日/湿小时百分位数不同。'), [])
        self.assertTrue(validate_translation('The rate is 2 day/hour.', '速率为2天。'))
        self.assertTrue(validate_translation('day/hour', 'day'))

    def test_days_per_event_occurring_summer_keep_the_whole_denominator(self):
        source = 'The mean is 10.43 days per event-occurring summer and the maximum is 7.73 days (Fig. 3d–i).'
        quantities = scientific_quantities(source)
        self.assertEqual(
            [(source[q.start:q.end], q.signature) for q in quantities],
            [('10.43 days per event-occurring summer', '10.43 d / event-occurring summer'),
             ('7.73 days', '7.73 d')],
        )
        # 旧版面计划继续使用原字形扫描。这里的统计限定语只参与翻译与内容校验。
        self.assertEqual(find_quantities(source)[0].signature, '10.43 d event^-1')
        for good in ('平均值为10.43天/发生事件的夏季', '每个发生过事件的夏季平均 10.43 天',
                     '每个有事件发生的夏季平均 10.43 天', '在发生过事件的夏季中，平均为10.43天'):
            with self.subTest(good=good):
                self.assertEqual(validate_translation(source, good + '，最大值为7.73天（图3d–i）。'), [])
        for bad in ('每个夏季平均10.43天', '每个未发生过事件的夏季平均10.43天',
                    '10.43天/发生过事件的夏季以外', '10.43天/次事件',
                    '每次事件平均10.43天', '每个发生过事件的夏季平均11.43天',
                    '每个发生过事件的夏季平均10.43小时', '每个发生过事件的夏季平均7.73天'):
            with self.subTest(bad=bad):
                self.assertTrue(validate_translation(source, bad + '，最大值为7.73天（图3d–i）。'))
        self.assertEqual(validate_translation('10.43 days per event', '每次事件平均10.43天'), [])
        self.assertEqual(validate_translation('10.43 days event−1', '10.43天/每次事件'), [])
        self.assertEqual(validate_translation('10.43 days per event', '10.43天/次事件'), [])
        self.assertTrue(validate_translation('10.43 days per event', '10.43天'))

    def test_unrecognized_denominator_requires_exact_scoped_confirmation(self):
        source = '10.43 days per event-contributing month'
        target = '10.43天/有事件贡献的月份'
        phrase = 'event-contributing month'
        self.assertEqual(unresolved_statistical_denominator(source), phrase)
        self.assertTrue(validate_translation(source, target)[0].startswith('quantity_review '))
        self.assertEqual(validate_translation(source, target, confirmed_denominator=phrase), [])
        self.assertEqual(validate_translation(source, '统计平均为10.43天/event-contributing month'), [])
        for bad in ('10.43天', '11.43天/有事件贡献的月份', '10.43小时/有事件贡献的月份',
                    '10.43天/次事件', '每次事件平均10.43天', '10.43天/每次事件',
                    '10.43天/event-contributing year'):
            with self.subTest(bad=bad):
                self.assertTrue(validate_translation(source, bad, confirmed_denominator=phrase))
        self.assertTrue(validate_translation(source, target, confirmed_denominator='other'))
        self.assertIsNone(unresolved_statistical_denominator(source + ' or ' + source))
        self.assertTrue(validate_translation(source + ' or ' + source, target + '或' + target,
                                             confirmed_denominator=phrase))

    def test_figure_suffix_is_not_a_quantity(self):
        self.assertEqual([q.signature for q in scientific_quantities('Fig. 3d–i depicts the 3 d trend.')], ['3 d'])
        self.assertEqual(validate_translation('Fig. 3d–i shows the trends.', '图3d–i展示了变化趋势。'), [])
        self.assertTrue(validate_translation('Fig. 3d–i shows the trends.', '图4d–i展示了变化趋势。'))

    def test_gpt_prompt_has_joint_constraints_without_changing_key_schema(self):
        context=TranslationContext('en','zh-CN',load_domain_pack('atmospheric-science'))
        prompt,schema=_keyed_request([Segment('original-hash','Measure 500 hPa and 50 km.')],context)
        self.assertIn('scientific_quantities',prompt)
        self.assertIn('500 hPa',prompt)
        self.assertEqual(schema['properties']['translations']['required'],['s1'])
        self.assertNotIn('original-hash',prompt)

    def test_gpt_prompt_preserves_the_complete_statistical_phrase(self):
        context = TranslationContext('en', 'zh-CN', load_domain_pack('atmospheric-science'))
        phrase = '10.43 days per event-occurring summer'
        prompt, _schema = _keyed_request([Segment('s1', phrase)], context)
        self.assertIn('"source": "' + phrase + '"', prompt)
        self.assertIn('"signature": "10.43 d / event-occurring summer"', prompt)
        self.assertNotIn('10.43 d event^-1', prompt)

    def test_layout_split_decimals_and_math_variables_remain_literal(self):
        from paperlocale.contracts import scientific_literal_spans
        for source in ['{v1}.12', '0.{v1}.0', '{v1}.{v2}-0.8',
                       'P(SPEI {v1}spei, STI {v2}sti) {v3}P(SPEI {v4}spei)']:
            self.assertEqual(scientific_literal_spans(source), [(0,len(source))])
            self.assertEqual(validate_translation(source,source), [])
            bad=source.replace('.', '。').replace('spei','斯佩伊')
            self.assertTrue(any('scientific_literal' in e for e in validate_translation(source,bad)))
        self.assertEqual(scientific_literal_spans('We use spei and {v1}.'), [])

    def test_qwen_math_only_is_preserved_without_remote_translation(self):
        from unittest.mock import patch
        from paperlocale.providers.qwen_mt import QwenMTProvider
        context=TranslationContext('en','zh-CN',load_domain_pack('atmospheric-science'))
        provider=QwenMTProvider(base_url='https://example.test/v1',api_key='test-key',model='qwen-mt-plus')
        with patch('urllib.request.urlopen') as request:
            for source in ['{v1}.12','0.{v1}.0','P(SPEI {v1}spei)']:
                result=provider.translate([Segment('s1',source)],context)
                self.assertEqual(result[0].target,source)
            request.assert_not_called()
