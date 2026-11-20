import json
import time
import unittest

from rsi.decision import SCHEMA, parse_decision
from rsi.labrun import PROBE_LIMIT
from tests.support import decision


def objects(schema: dict):
    """Every object schema inside schema, itself included."""
    if schema.get('type') == 'object':
        yield schema
        for value in schema['properties'].values():
            yield from objects(value)
    if schema.get('type') == 'array':
        yield from objects(schema['items'])


class DecisionTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(parse_decision(decision('no_change')).kind, 'no_change')
        good = parse_decision(decision('trial', trials=[{'task': 'T1', 'probe': None, 'version': 'candidate'}]))
        self.assertEqual(good.trials[0]['task'], 'T1')
        for bad in ('not json', decision('publish'), decision('trial'), decision('release'),
                    decision('trial', trials=[{'task': 'T1', 'probe': 'x', 'version': 'candidate'}]),
                    decision('trial', trials=[{'task': '../T1', 'probe': None, 'version': 'candidate'}]),
                    decision('release', evidence=['../R0001']), decision('release', evidence=['M0001']),
                    json.dumps({'decision': 'no_change'})):
            with self.assertRaises(ValueError): parse_decision(bad)

    # Beyond the brief.

    def test_the_schema_is_strict(self):
        found = list(objects(SCHEMA))
        self.assertEqual(len(found), 4)                     # the decision, an observation, a trial, an application
        for schema in found:
            self.assertIs(schema['additionalProperties'], False)
            self.assertEqual(sorted(schema['required']), sorted(schema['properties']))
        self.assertEqual(sorted(SCHEMA['properties']), sorted(json.loads(decision('no_change'))))

    def test_the_parts_of_a_decision(self):
        text = decision('release', evidence=['R0002', 'R0003'], observations=[{'record': 'R0002', 'note': '看到了'}],
                        applications=[{'what': '连接器 X', 'why': 'w', 'records': ['R0002']}], problem='问题')
        parsed = parse_decision('\n  ' + text + '\n')
        self.assertEqual((parsed.kind, parsed.evidence, parsed.trials), ('release', ['R0002', 'R0003'], []))
        self.assertEqual(parsed.notes, {'observations': [{'record': 'R0002', 'note': '看到了'}], 'problem': '问题',
                                        'likely_cause': 'c', 'other_explanations': 'o', 'change': 'ch',
                                        'expected_effect': 'e', 'risks': 'r', 'uncertain': 'u'})
        self.assertEqual(parsed.applications, [{'what': '连接器 X', 'why': 'w', 'records': ['R0002']}])
        self.assertEqual(parsed.as_dict(), json.loads(text))
        probe = parse_decision(decision('trial', trials=[{'task': None, 'probe': '说你好', 'version': 'current'}]))
        self.assertEqual(probe.trials, [{'task': None, 'probe': '说你好', 'version': 'current'}])

    def test_every_rule_has_a_one_line_chinese_reason(self):
        trial = {'task': 'T1', 'probe': None, 'version': 'candidate'}
        cases = {
            '[]': 'JSON 对象',
            decision('no_change') + 'x': 'JSON',
            '{"decision": "no_change", "decision": "trial"}': '重复',
            json.dumps({**json.loads(decision('no_change')), 'extra': 1}): 'extra',
            decision('trial', trials=[{**trial, 'version': 'head'}]): 'version',
            decision('trial', trials=[{'task': None, 'probe': None, 'version': 'current'}]): 'task',
            decision('trial', trials=[{'task': None, 'probe': '  ', 'version': 'current'}]): 'probe',
            decision('trial', trials=[{'task': None, 'probe': 'x' * (PROBE_LIMIT + 1), 'version': 'current'}]):
                'probe',
            decision('trial', trials=[{'task': 'T1', 'probe': None}]): 'version',
            decision('trial', trials=[{**trial, 'extra': 1}]): 'extra',
            decision('trial', trials=['T1']): 'trials',
            decision('trial', trials=[trial] * 21): '20',
            decision('release', evidence=['R0002\n']): 'R0002',
            decision('release', evidence=['R０００２']): 'evidence',                 # full-width digits
            decision('release', evidence=['R' + '1' * 30]): 'evidence',
            decision('release', evidence=[2]): 'evidence',
            decision('no_change', problem=None): 'problem',
            decision('no_change', observations=[{'record': 'V0001', 'note': 'n'}]): 'V0001',
            decision('no_change', observations=[{'record': 'R0001'}]): 'note',
            decision('no_change', applications=[{'what': '', 'why': 'w', 'records': []}]): 'what',
            decision('no_change', applications=[{'what': 'x', 'why': 'w', 'records': ['A0001']}]): 'A0001',
            decision('no_change', problem='x' * (1 << 20)): '字符',
            '[' * 100000 + ']' * 100000: 'JSON',
        }
        for text, word in cases.items():
            with self.assertRaises(ValueError, msg=text[:200]) as caught:
                parse_decision(text)
            reason = str(caught.exception)
            self.assertIn(word, reason, text[:200])
            self.assertNotIn('\n', reason); self.assertLess(len(reason), 400, reason)
        with self.assertRaises(ValueError): parse_decision(None)
        with self.assertRaises(ValueError): parse_decision(b'{}')

    def test_text_that_cannot_be_recorded_is_refused(self):
        lone = '\\ud800'                                  # a JSON escape for a lone surrogate: UTF-8 cannot hold it
        trial = '{"task": null, "probe": "a%sb", "version": "current"}' % lone
        cases = {decision('no_change').replace('"problem": "p"', f'"problem": "{lone}"'): 'problem',
                 decision('no_change', applications=[{'what': 'WHAT', 'why': 'w', 'records': []}])
                 .replace('"WHAT"', f'"x{lone}"'): 'what',
                 decision('trial', trials=['TRIAL']).replace('"TRIAL"', trial): 'probe',
                 decision('no_change', observations=[{'record': 'R0001', 'note': 'NOTE'}])
                 .replace('"NOTE"', f'"{lone}"'): 'note'}
        for text, word in cases.items():
            with self.assertRaises(ValueError, msg=text) as caught:
                parse_decision(text)
            reason = str(caught.exception)
            self.assertIn(word, reason); self.assertIn('UTF-8', reason); self.assertNotIn('\n', reason)
            reason.encode('utf-8')                        # the reason itself can be recorded and sent

    def test_duplicate_keys_are_found_in_linear_time_and_objects_are_capped(self):
        many = '{' + ', '.join(f'"k{number}": 1' for number in range(80_000)) + '}'
        started = time.monotonic()
        with self.assertRaises(ValueError) as caught:
            parse_decision(many)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn('键', str(caught.exception))
        repeated = '{' + ', '.join(['"decision": "no_change"'] * 2) + '}'
        with self.assertRaisesRegex(ValueError, '重复'):
            parse_decision(repeated)

    def test_repeated_evidence_is_kept_once_in_order(self):
        parsed = parse_decision(decision('release', evidence=['R0003', 'R0002', 'R0003', 'R0002']))
        self.assertEqual(parsed.evidence, ['R0003', 'R0002'])


if __name__ == '__main__':
    unittest.main()
