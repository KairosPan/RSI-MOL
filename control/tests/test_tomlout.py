import tomllib
import unittest

from rsi.tomlout import render_file, render_flags, toml_value


class TomlOut(unittest.TestCase):
    CONFIG = {'default_permissions': 'lab', 'note': 'say "hi"\n', 'agents': {'max_depth': 1},
              'permissions': {'lab': {'filesystem': {':minimal': 'read', '/a b/c': 'write'},
                                      'network': {'enabled': False}}},
              'plugins': {'latex@openai-bundled': {'enabled': True}},
              'mcp_servers': {'lit': {'command': 'codex', 'args': ['sandbox', '--', 'x y']}}}

    def test_file_round_trip(self):
        self.assertEqual(tomllib.loads(render_file(self.CONFIG)), self.CONFIG)

    def test_flags_are_sorted_pairs_that_parse(self):
        flags = render_flags({'b': {'x': 1}, 'a': 'v'})
        self.assertEqual(flags[0::2], ['-c', '-c'])
        self.assertEqual([pair.split('=', 1)[0] for pair in flags[1::2]], ['a', 'b'])
        for pair in flags[1::2]:
            key, value = pair.split('=', 1)
            tomllib.loads(key + ' = ' + value)

    def test_unsupported_value(self):
        with self.assertRaises(TypeError):
            toml_value(None)


if __name__ == '__main__':
    unittest.main()
