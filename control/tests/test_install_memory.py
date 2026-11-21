import hashlib
import json
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.install_literature import ROLE
from rsi.install_memory import PREVIOUS_TEMPLATE_HASHES, install_memory
from rsi.meta import rollback
from rsi.paths import Refused
from rsi.versions import VersionStore
from tests.support import Root

SEED = Path(__file__).resolve().parents[1] / 'seed/work'


class InstallMemory(unittest.TestCase):
    def test_first_snapshot_preserves_uncommitted_legacy_memory_and_reports_its_location(self):
        with Root() as root, patch('rsi.install_literature.write_home_config'):
            work = root.layout.work
            (work / 'MeMoRy').mkdir()
            (work / 'MeMoRy/notes.md').write_text('Only copy of my notes.')
            notes = []
            baseline, installed = install_memory(root.layout, SEED, notes)
            self.assertEqual(baseline, 'V0001')
            kept, = root.layout.sealed.glob('*/legacy-work/MeMoRy/notes.md')
            self.assertEqual(kept.read_text(), 'Only copy of my notes.')
            location = str(kept.parent.relative_to(root.path))
            self.assertTrue(any(location in line for line in notes))
            index = [json.loads(line) for line in (root.layout.records / 'index.jsonl').read_text().splitlines()]
            self.assertTrue(any(item['kind'] == 'sealed' and item['path'] == location for item in index))
            self.assertTrue((work / 'memory/status.json').is_file())
            self.assertFalse(root.store.tracks_workspace_memory(installed))
            (work / 'memory/session-only').write_text('A change inside the managed snapshot.')
            self.assertEqual(install_memory(root.layout, SEED), (installed, installed))
            self.assertFalse((work / 'memory/session-only').exists())
            self.assertEqual(list(root.layout.sealed.glob('*/legacy-work/*')), [kept.parent])

    def test_rollback_checks_a_legacy_version_before_replacing_work_or_moving_main(self):
        with Root() as root, patch('rsi.install_literature.write_home_config'):
            legacy_rules = tuple(entry for entry in VersionStore.EXCLUDED if entry != 'memory/')
            with patch.object(VersionStore, 'EXCLUDED', legacy_rules):
                old_store = VersionStore(root.layout.versions)
                (root.layout.work / 'MEMORY').write_text('Old tracked file.')
                old_version = old_store.commit(root.layout.work, 'owner', 'Before scientific memory')
            _, installed = install_memory(root.layout, SEED)
            kept, = root.layout.sealed.glob('*/legacy-work/MEMORY')
            self.assertEqual(kept.read_text(), 'Old tracked file.')
            (root.layout.work / 'unsaved').write_text('still here')
            with self.assertRaisesRegex(Refused, 'tracks the reserved work/memory'):
                rollback(root.layout, old_version, 'owner')
            self.assertEqual(root.store.head(), installed)
            self.assertEqual((root.layout.work / 'unsaved').read_text(), 'still here')
            self.assertTrue((root.layout.work / 'memory/status.json').is_file())

    def test_upgrade_only_known_earlier_templates_as_one_version(self):
        with Root() as root, patch('rsi.install_literature.write_home_config'):
            work = root.layout.work
            install_memory(root.layout, SEED)
            paths = ('tools/memory/query.py', 'tools/memory/server.py', 'skills/scientific-memory/SKILL.md')
            self.assertTrue(set(paths).issubset(PREVIOUS_TEMPLATE_HASHES))
            self.assertIn('654fcd548e1e0d7554f8a1f15b08c4055488e0b83afbda9858910843c58a172c',
                          PREVIOUS_TEMPLATE_HASHES[paths[0]])
            # The first release lives in the owner's version store. Stand-ins exercise the same digest gate in CI.
            old = {relative: f'first release of {relative}\n'.encode() for relative in paths}
            for relative, data in old.items():
                (work / relative).write_bytes(data)
            (work / 'AGENTS.md').write_text((work / 'AGENTS.md').read_text() + '\nPreserve these owner instructions.\n')
            owner_text = (work / 'AGENTS.md').read_bytes()
            hashes = {relative: {hashlib.sha256(data).hexdigest()} for relative, data in old.items()}
            with patch.dict(PREVIOUS_TEMPLATE_HASHES, hashes, clear=True):
                custom_skill = old[paths[-1]] + b'Local addition\n'
                (work / paths[-1]).write_bytes(custom_skill)
                with self.assertRaisesRegex(Refused, 'preserve the workspace entry'):
                    install_memory(root.layout, SEED)
                self.assertEqual((work / paths[-1]).read_bytes(), custom_skill)
                self.assertEqual((work / paths[0]).read_bytes(), old[paths[0]])
                self.assertEqual((work / paths[1]).read_bytes(), old[paths[1]])
                (work / paths[-1]).write_bytes(old[paths[-1]])
                baseline, upgraded = install_memory(root.layout, SEED)
                self.assertNotEqual(upgraded, baseline)
                for relative in paths:
                    self.assertEqual((work / relative).read_bytes(), (SEED / relative).read_bytes())
                self.assertEqual((work / 'AGENTS.md').read_bytes(), owner_text)
                self.assertEqual(install_memory(root.layout, SEED), (upgraded, upgraded))

    def test_merge_preserves_owner_instructions_and_repeats_without_a_commit(self):
        with Root() as root, patch('rsi.install_literature.write_home_config'):
            work = root.layout.work
            (work / 'codex/agents').mkdir(parents=True)
            settings = work / 'codex/settings.toml'
            settings.write_text('# owner\ntool_servers = { demo = { command = "demo" } }\n')
            (work / ROLE).write_text('name = "literature"\ndescription = "read"\n'
                                     'developer_instructions = "Keep my instructions." # owner\n')
            self.assertEqual(install_memory(root.layout, SEED), ('V0002', 'V0003'))
            self.assertEqual(install_memory(root.layout, SEED), ('V0003', 'V0003'))
            self.assertEqual(set(tomllib.loads(settings.read_text())['tool_servers']), {'demo', 'memory'})
            self.assertTrue((work / 'AGENTS.md').read_text().startswith('hello\n'))
            self.assertIn('Keep my instructions.', (work / ROLE).read_text())
            self.assertIn('# owner', (work / ROLE).read_text())
            self.assertTrue((work / 'tools/memory/server.py').is_file())
            self.assertTrue((work / 'skills/scientific-memory/SKILL.md').is_file())

    def test_conflict_keeps_the_manual_version_and_the_existing_reader(self):
        with Root() as root, patch('rsi.install_literature.write_home_config'):
            work = root.layout.work
            entry = work / 'tools/memory/server.py'
            entry.parent.mkdir(parents=True)
            entry.write_text('print("my reader")\n')
            with self.assertRaisesRegex(Refused, 'conflict'):
                install_memory(root.layout, SEED)
            self.assertEqual(root.store.head(), 'V0002')
            self.assertEqual(entry.read_text(), 'print("my reader")\n')
            self.assertFalse((work / 'skills/scientific-memory').exists())
