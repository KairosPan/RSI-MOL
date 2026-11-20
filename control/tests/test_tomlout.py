import tomllib
import unittest

from rsi.tomlout import render_file, render_flags, toml_value


class TomlOut(unittest.TestCase):
    CONFIG = {'default_permissions': 'lab', 'note': 'say "hi"\n', 'agents': {'max_depth': 1},
              'permissions': {'lab': {'filesystem': {':minimal': 'read', '/a b/c': 'write'},
                                      'network': {'enabled': False}}},
              'plugins': {'latex@openai-bundled': {'enabled': True}},
              'mcp_servers': {'lit': {'command': 'codex', 'args': ['sandbox', '--', 'x y']}}}
    AWKWARD = {'backslash': 'C:\\dir\\ and \\n stays two characters',
               'control': ''.join(chr(code) for code in range(32)) + '\x7f',
               'non_ascii': '中文 é ü 🙂 \u2028',
               'empty_list': [], 'empty_table': {}, 'nested_empty': {'a': [], 'b': {}},
               'dotted': {'a.b': {'c.d': 'x', '': 'empty key'}, '"quoted"': 1},
               'numbers': [0, -1, 2 ** 63 - 1, -2 ** 63, 1.5, -0.25]}

    def test_file_round_trip(self):
        self.assertEqual(tomllib.loads(render_file(self.CONFIG)), self.CONFIG)

    def test_awkward_values_round_trip(self):
        self.assertEqual(tomllib.loads(render_file(self.AWKWARD)), self.AWKWARD)
        for key, value in self.AWKWARD.items():
            self.assertEqual(tomllib.loads('x = ' + toml_value(value)), {'x': value})

    def test_awkward_flags_round_trip(self):
        flags = render_flags(self.AWKWARD)
        parsed = {}
        for pair in flags[1::2]:
            self.assertNotIn('\n', pair)
            key, value = pair.split('=', 1)
            parsed.update(tomllib.loads(key + ' = ' + value))
        self.assertEqual(parsed, self.AWKWARD)

    def test_flags_are_sorted_pairs_that_parse(self):
        flags = render_flags({'b': {'x': 1}, 'a': 'v'})
        self.assertEqual(flags[0::2], ['-c', '-c'])
        self.assertEqual([pair.split('=', 1)[0] for pair in flags[1::2]], ['a', 'b'])
        for pair in flags[1::2]:
            key, value = pair.split('=', 1)
            tomllib.loads(key + ' = ' + value)

    def test_flags_refuse_a_top_level_key_that_is_not_bare(self):
        for key in ('a.b', 'a b', '', 'a=b', '中', '"a"'):
            with self.assertRaises(ValueError):
                render_flags({key: 1})

    def test_unsupported_value(self):
        with self.assertRaises(TypeError):
            toml_value(None)

    def test_int_out_of_range(self):
        for number in (2 ** 63, -2 ** 63 - 1, 10 ** 30):
            with self.assertRaises(ValueError):
                toml_value(number)
            with self.assertRaises(ValueError):
                toml_value({'nested': [number]})


if __name__ == '__main__':
    unittest.main()
