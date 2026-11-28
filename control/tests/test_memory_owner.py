"""Owner actions share the ledger chain and have independent, recoverable identities."""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi import cli, memory_ingest as ingest, memory_store as ledger
from rsi.init import locked
from rsi.paths import Layout
from rsi.records import Records


class OwnerLedger(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(Path(self.enterContext(tempfile.TemporaryDirectory())).resolve())
        target = self.layout.library / 'objects' / 'sha256'
        target.mkdir(parents=True)
        self.original, self.parent = [hashlib.sha256(data).hexdigest() for data in (b'text', b'parent')]
        (target / self.original).write_bytes(b'text')
        (target / self.parent).write_bytes(b'parent')
        self.identity = 'passage_' + ledger.digest('passage')
        obj = {'id': self.identity, 'revision': 1, 'content': {'type': 'passage', 'source_sha256': self.original}}
        obj['record_sha256'] = ledger.digest(obj)
        delivery = {'id': self.identity, 'revision': 1, 'derived_from': {'source_sha256': self.parent}}
        self.registration = ledger.publish(self.layout, ledger.State(), 'R0001',
                                           [('object', obj), ('delivery', delivery)], {'delivery_count': 1})
        self.enterContext(patch.dict(os.environ, RSI_ROOT=str(self.layout.root)))

    def command(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli.main(['memory', *args])
        self.assertEqual(result, code, err.getvalue())
        return json.loads(out.getvalue()) if code == 0 else err.getvalue()

    def test_owner_batch_names_identity_and_targets_survive_reload_and_retry(self):
        changed = self.command('source-changed', self.parent)
        withdrawn = self.command('withdraw', self.identity, '1')
        self.assertEqual(changed, self.command('source-changed', self.parent))
        self.assertEqual(withdrawn, self.command('withdraw', self.identity, '1'))
        state = ledger.load(self.layout)
        self.assertEqual(state.reports, {'R0001': self.registration})
        self.assertEqual(state.owner_reports, {r['owner']: r for r in (changed, withdrawn)})
        self.assertEqual([r['version_before'] for r in (changed, withdrawn)], [3, 5])
        self.assertEqual(state.owner_events, [
            {'seq': 4, 'type': 'source_changed', 'source_sha256': self.parent},
            {'seq': 6, 'type': 'withdrawn', 'id': self.identity, 'revision': 1},
        ])
        for report in (changed, withdrawn):
            owner = report['owner']
            self.assertRegex(owner, r'^O[0-9a-f]{64}$')
            batch, = (self.layout.memory / 'batches').glob(f'*-{owner}.jsonl')
            events = list(map(json.loads, batch.read_bytes().splitlines()))
            self.assertEqual([e['owner'] for e in events], [owner, owner])
            self.assertTrue(all('run' not in e for e in events))
            self.assertNotIn('run', report)
            self.assertEqual(json.loads((self.layout.records / 'memory' / f'{owner}.json').read_bytes()), report)
        check = self.command('verify')
        self.assertEqual((check['runs'], check['originals'], check['owner_actions']), (1, 2, 2))
        self.assertEqual(self.command('backfill'), [])
        self.assertEqual([r['run'] for r in self.command('status')['recent']], ['R0001'])

    def test_early_interrupt_and_invalid_targets_leave_existing_chain_intact(self):
        for args, code in [(('source-changed', '0' * 64), 3), (('source-changed', '../bad'), 3),
                           (('withdraw', self.identity, '2'), 3), (('withdraw', 'absent', '1'), 3),
                           (('withdraw', self.identity, '0'), 2)]:
            self.command(*args, code=code)
        with locked(self.layout):
            self.assertIn('Another rsi', self.command('source-changed', self.original, code=3))
        with patch('rsi.records.publish_new', side_effect=InterruptedError), self.assertRaises(InterruptedError):
            ledger.record_owner_action(self.layout, 'source_changed', {'source_sha256': self.original})
        self.assertEqual(ledger.load(self.layout).tail, self.registration['tail_sha256'])
        self.assertEqual(self.command('source-changed', self.original)['version_before'], 3)

    def test_late_interrupt_reanchors_identical_batch_and_detects_truncation(self):
        args = 'withdraw', self.identity, '1'
        with patch.object(Records, 'write_json', side_effect=OSError('full disk')):
            self.assertIn('full disk', self.command(*args, code=1))
        state = ledger.load(self.layout)
        owner, = state.missing_owner_reports
        self.assertEqual(state.missing_reports, set())
        self.assertIn(owner, self.command('verify', code=1))
        status = self.command('status')
        self.assertEqual((status['pending'], status['pending_owner_reports']), (0, 1))
        batch, = (self.layout.memory / 'batches').glob(f'*-{owner}.jsonl')
        original = batch.read_bytes()
        report, = self.command('backfill')
        self.assertEqual(report, self.command(*args))
        self.assertEqual(batch.read_bytes(), original)
        anchor = self.layout.records / 'memory' / f'{owner}.json'
        anchor.unlink()
        self.assertEqual(self.command(*args), report)
        self.assertEqual(batch.read_bytes(), original)
        anchor.write_text(json.dumps({**report, 'revision': 99}))
        with self.assertRaisesRegex(ValueError, 'report disagrees'):
            ingest.verify(self.layout)
        anchor.write_text(json.dumps(report))
        batch.unlink()
        with self.assertRaisesRegex(ValueError, 'no matching memory batch'):
            ingest.verify(self.layout)

    def test_registered_run_cannot_supply_owner_actions(self):
        registered, = (self.layout.memory / 'batches').glob('*.jsonl')
        rows = registered.read_bytes().splitlines(keepends=True)
        event = json.loads(rows[0])
        rows[0] = ledger.encode({**event, 'type': 'withdrawn', 'value': {'id': self.identity, 'revision': 1}}) + b'\n'
        registered.write_bytes(b''.join(rows))
        with self.assertRaisesRegex(ValueError, 'Unknown memory event type'):
            ledger.load(self.layout)
