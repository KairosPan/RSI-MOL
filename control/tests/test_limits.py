import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rsi.limits import Limits, load_limits, refusal, usage_by_class


class LimitsTests(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 12, 0).astimezone()

    def call(self, day_offset=0, tokens=10, cls='online'):
        started = (self.NOW + timedelta(days=day_offset)).isoformat()
        usage = None if tokens is None else {'input_tokens': tokens, 'output_tokens': 1}
        return {'started': started, 'cost_class': cls, 'usage': usage, 'status': 'finished'}

    def owner(self) -> Path:
        owner = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, owner)
        return owner

    def test_defaults_and_unknown_keys(self):
        owner = self.owner()
        self.assertEqual(load_limits(owner), Limits(1800, 8, 4, 900, 60, 0))
        (owner / 'limits.toml').write_text('[lab]\nseconds_per_run = 5\n[day]\ncalls = 2\n')
        self.assertEqual((load_limits(owner).lab_seconds, load_limits(owner).day_calls), (5, 2))
        (owner / 'limits.toml').write_text('[lab]\nsecnds = 5\n')
        with self.assertRaises(ValueError): load_limits(owner)

    def test_every_key_and_bad_values(self):
        owner = self.owner()
        (owner / 'limits.toml').write_text('[lab]\nseconds_per_run = 60\n[meta]\ncalls_per_round = 3\n'
                                           'trials_per_round = 0\nseconds_per_call = 30\n[day]\ncalls = 0\n'
                                           'tokens = 100\n')
        self.assertEqual(load_limits(owner), Limits(60, 3, 0, 30, 0, 100))
        for text in ('[lab]\nseconds_per_run = "5"\n', '[lab]\nseconds_per_run = 0\n', '[meta]\nseconds_per_call = 0\n',
                     '[day]\ncalls = -1\n', '[day]\ntokens = true\n', '[day]\ntokens = 1.5\n', 'lab = 5\n',
                     '[week]\ncalls = 1\n', 'calls = 1\n', '[lab\n', '[lab]\nseconds_per_run = 5\n[lab.x]\ny = 1\n'):
            (owner / 'limits.toml').write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError): load_limits(owner)
        (owner / 'limits.toml').write_bytes(b'[lab]\nseconds_per_run = 5 # \xff\n')
        with self.assertRaises(ValueError): load_limits(owner)

    def test_refusal_counts_today_only(self):
        limits = Limits(1800, 8, 4, 900, 2, 0)
        self.assertIsNone(refusal(limits, [self.call(), self.call(-1), self.call(-1)], self.NOW))
        message = refusal(limits, [self.call(), self.call(), self.call(), self.call(-1)], self.NOW)
        self.assertIn('已经开始了 3 次调用', message)                    # the count, not a digit of the date
        self.assertIn('每天 2 次的上限', message)
        self.assertNotIn('读不出来', message)
        self.assertIsNotNone(refusal(Limits(1800, 8, 4, 900, 60, 15), [self.call(tokens=20)], self.NOW))

    def test_an_unreadable_record_counts_today_whatever_its_date(self):
        unreadable = {'id': 'R0002', 'status': 'unreadable', 'problem': '调用记录 x 读不出来'}
        limits = Limits(1800, 8, 4, 900, 2, 0)
        self.assertIsNone(refusal(limits, [self.call(-1), unreadable], self.NOW))
        message = refusal(limits, [self.call(), unreadable], self.NOW)
        self.assertIn('已经开始了 2 次调用', message)
        self.assertIn('其中 1 份调用记录读不出来', message)
        self.assertIsNotNone(refusal(limits, [self.call(), None], self.NOW))     # not even a dict
        summary = usage_by_class([unreadable, None, self.call(tokens=4)])
        self.assertEqual(summary['unknown'], {'calls': 2, 'input_tokens': 0, 'output_tokens': 0,
                                              'unknown_usage_calls': 2})
        self.assertEqual(summary['online']['input_tokens'], 4)

    def test_today_is_the_local_calendar_day(self):
        limits = Limits(1800, 8, 4, 900, 1, 0)
        midnight = self.NOW.replace(hour=0, minute=0)
        before, after = self.call(), self.call()
        before['started'] = (midnight - timedelta(minutes=1)).isoformat()
        after['started'] = (midnight + timedelta(minutes=1)).isoformat()
        self.assertIsNone(refusal(limits, [before], self.NOW))
        self.assertIsNotNone(refusal(limits, [after], self.NOW))
        after['started'] = (midnight + timedelta(minutes=1)).astimezone(timezone.utc).isoformat()
        self.assertIsNotNone(refusal(limits, [after], self.NOW))           # the same moment written in UTC
        shift = timedelta(hours=-13 if self.NOW.utcoffset() >= timedelta(0) else 13)
        elsewhere = self.NOW.astimezone(timezone(self.NOW.utcoffset() + shift))   # another calendar day there
        self.assertNotEqual(elsewhere.date(), self.NOW.date())
        self.assertIsNotNone(refusal(limits, [after], elsewhere))
        self.assertIsNotNone(refusal(Limits(1800, 8, 4, 900, 0, 0), [], self.NOW))   # day.calls = 0 stops all

    def test_token_cap(self):
        limits = Limits(1800, 8, 4, 900, 60, 15)
        self.assertIsNone(refusal(limits, [self.call(tokens=13)], self.NOW))
        self.assertIsNotNone(refusal(limits, [self.call(tokens=7), self.call(tokens=7)], self.NOW))
        self.assertIsNone(refusal(limits, [self.call(-1, tokens=100), self.call(tokens=None)], self.NOW))
        self.assertIsNone(refusal(Limits(1800, 8, 4, 900, 60, 0), [self.call(tokens=10 ** 9)], self.NOW))
        mixed = [self.call(tokens=10), self.call(tokens=None), self.call(tokens=6), self.call(tokens=None),
                 self.call(-1, tokens=None)]
        message = refusal(limits, mixed, self.NOW)
        self.assertIn('已用 18 个 token', message)                      # (10 + 1) + (6 + 1), known usage only
        self.assertIn('每天 15 个的上限', message)
        self.assertIn('另有 2 次调用的用量不明，没有算进去', message)      # yesterday's unknown call is not today's
        self.assertNotIn('用量不明', refusal(limits, [self.call(tokens=20)], self.NOW))

    def test_unknown_usage_is_not_zero(self):
        summary = usage_by_class([self.call(tokens=10), self.call(tokens=None), self.call(tokens=5, cls='meta')])
        self.assertEqual(summary['online'],
                         {'calls': 2, 'input_tokens': 10, 'output_tokens': 1, 'unknown_usage_calls': 1})
        self.assertEqual(summary['meta']['calls'], 1)
        self.assertEqual(summary['eval'], {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'unknown_usage_calls': 0})
        odd = [dict(self.call(), usage={'input_tokens': 'x', 'output_tokens': 1}),
               dict(self.call(), usage={'input_tokens': True, 'output_tokens': 1}), dict(self.call(), usage={})]
        self.assertEqual(usage_by_class(odd)['online']['unknown_usage_calls'], 3)
        classless = [dict(self.call(), cost_class=None), dict(self.call(), cost_class=3),
                     dict(self.call(), cost_class=''),
                     {key: value for key, value in self.call().items() if key != 'cost_class'}]
        summary = usage_by_class(classless)
        self.assertEqual(summary['unknown'],
                         {'calls': 4, 'input_tokens': 40, 'output_tokens': 4, 'unknown_usage_calls': 0})
        self.assertEqual(sorted(summary), ['eval', 'meta', 'online', 'unknown'])


if __name__ == '__main__':
    unittest.main()
