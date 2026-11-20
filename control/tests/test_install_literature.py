import json
import shutil
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.install_literature import NOTE, install_literature
from rsi.meta import rollback
from rsi.paths import Refused
from tests.support import Root, needs_codex
from tests.test_cli import run

SEED = Path(__file__).resolve().parents[1] / 'seed' / 'work'
PACKAGE = SEED / 'tools' / 'literature'


def notes(root: Root, kind: str) -> list[dict]:
    lines = (root.layout.records / 'index.jsonl').read_text().splitlines()
    return [entry for entry in map(json.loads, lines) if entry['kind'] == kind]


def package_files(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob('*'))
            if path.is_file() and '__pycache__' not in path.parts}


class InstallLiterature(unittest.TestCase):
    def test_upgrade_preserves_edits_and_can_be_repeated_and_rolled_back(self):
        with Root() as root:
            w = root.layout.work
            (w / 'codex/agents').mkdir(parents=True)
            (w / 'codex/settings.toml').write_text('# My settings\nnetwork = false\n'
                                                  'tool_servers = { other = { command = "other" } }\n')
            role = ('# My research role\nname = "literature"\ndescription = "My researcher"\n'
                    'developer_instructions = "My workflow." # keep this comment\nmodel_reasoning_effort = "low"\n')
            (w / 'codex/agents/literature.toml').write_text(role)
            with patch('rsi.install_literature.write_home_config'):
                before, after = install_literature(root.layout, SEED)
                self.assertEqual((before, after), ('V0002', 'V0003'))
                self.assertEqual(install_literature(root.layout, SEED), (after, after))
            merged = (w / 'codex/agents/literature.toml').read_text()
            self.assertIn('# keep this comment', merged)
            self.assertIn('My workflow.', tomllib.loads(merged)['developer_instructions'])
            settings = (w / 'codex/settings.toml').read_text()
            self.assertIn('# My settings', settings)
            self.assertEqual(set(tomllib.loads(settings)['tool_servers']), {'other', 'literature'})
            installed, unchanged = notes(root, NOTE)
            self.assertEqual((installed['outcome'], installed['baseline'], installed['version'], installed['lines']),
                             ('installed', 'V0002', 'V0003', []))
            self.assertEqual((unchanged['outcome'], unchanged['version']), ('unchanged', 'V0003'))
            new = rollback(root.layout, before, 'owner')
            self.assertEqual(root.store.tree_of(new), root.store.tree_of(before))
            self.assertEqual((w / 'codex/agents/literature.toml').read_text(), role)

    @needs_codex
    def test_the_package_becomes_exactly_the_seeds_and_what_it_replaced_is_listed(self):
        with Root() as root:
            folder = root.layout.work / 'tools/literature'
            folder.mkdir(parents=True)
            (folder / 'server.py').write_text('my own server\n')
            (folder / 'json.py').write_text('# would shadow the standard json module\n')
            code, out, err = run('install-literature')
            self.assertEqual((code, err), (0, ''))
            self.assertIn('Literature installed in V0003. Roll back with: rsi rollback V0002', out)
            self.assertIn('tools/literature/json.py was removed (the seed does not have it)', out)
            self.assertIn("tools/literature/server.py was replaced by the seed's", out)
            self.assertEqual(package_files(folder), package_files(PACKAGE))
            note, = notes(root, NOTE)
            self.assertEqual(note['lines'], ['tools/literature/json.py was removed (the seed does not have it)',
                                             "tools/literature/server.py was replaced by the seed's"])
            rollback(root.layout, 'V0002', 'owner')
            self.assertEqual((folder / 'server.py').read_text(), 'my own server\n')
            self.assertTrue((folder / 'json.py').is_file())

    def test_a_literature_server_the_lab_changed_is_refused_and_work_is_kept(self):
        with Root() as root:
            settings = root.layout.work / 'codex/settings.toml'
            settings.parent.mkdir(parents=True)
            text = '[tool_servers.literature]\ncommand = "python3"\nargs = ["tools/literature/mine.py"]\n'
            settings.write_text(text)
            with self.assertRaisesRegex(Refused, 'conflict'):
                install_literature(root.layout, SEED)
            self.assertEqual(settings.read_text(), text)
            self.assertFalse((root.layout.work / 'tools').exists())
            self.assertEqual(root.store.head(), 'V0002')
            self.assertIn('conflict', notes(root, 'refusal')[-1]['reason'])
            self.assertEqual(notes(root, NOTE)[-1]['outcome'], 'refused')

    def test_invalid_toml_is_a_refusal_naming_the_file(self):
        for name in ('codex/settings.toml', 'codex/agents/literature.toml'):
            with self.subTest(name=name), Root() as root:
                path = root.layout.work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('this is [not toml\n')
                with patch('rsi.install_literature.write_home_config'):
                    code, out, err = run('install-literature')
                self.assertEqual(code, 3, err)
                self.assertIn(name, err); self.assertIn('not valid TOML', err)
                self.assertNotIn('Traceback', err)
                self.assertEqual(path.read_text(), 'this is [not toml\n')       # work/ holds the version again
                self.assertFalse((root.layout.work / 'tools').exists())

    def test_a_failure_while_putting_work_back_does_not_hide_the_refusal(self):
        with Root() as root:
            settings = root.layout.work / 'codex/settings.toml'
            settings.parent.mkdir(parents=True)
            settings.write_text('[tool_servers.literature]\ncommand = "mine"\n')
            with patch('rsi.install_literature.write_home_config', side_effect=RuntimeError('config broken')), \
                    self.assertRaises(Refused) as refusal:
                install_literature(root.layout, SEED)
            self.assertIn('conflict', str(refusal.exception))
            self.assertIn('codex-home/config.toml', str(refusal.exception))
            self.assertIn('config broken', str(refusal.exception))
            note = notes(root, NOTE)[-1]
            self.assertEqual(note['outcome'], 'refused')
            self.assertTrue(any('config broken' in line for line in note['lines']), note)
            self.assertTrue(settings.is_file())                                 # work/ was put back all the same

    def test_seed_install_does_not_create_an_upgrade_version(self):
        with Root() as root:
            shutil.copytree(SEED, root.layout.work, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
            lines = []
            with patch('rsi.install_literature.write_home_config'):
                self.assertEqual(install_literature(root.layout, SEED, lines), ('V0002', 'V0002'))
            self.assertEqual(lines, [])


if __name__ == '__main__':
    unittest.main()
