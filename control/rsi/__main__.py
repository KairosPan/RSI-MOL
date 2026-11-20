"""Entry of the control program, as the `rsi` launcher starts it: `<python> -I -B control/rsi/__main__.py <args>`.

-I leaves the script's folder (and PYTHONPATH, and the user's site-packages) off sys.path, so control/ is put there
here. -B only stops Python from writing bytecode: it would still load a matching file from an existing __pycache__,
which the gate's fingerprint of the sources does not see. So bytecode is looked for under a folder that does not exist
and that only root could create, and every module of the program is compiled from its source.
"""
import os
import sys

sys.dont_write_bytecode = True
sys.pycache_prefix = '/var/empty/rsi-no-bytecode'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from rsi.cli import main  # noqa: E402

sys.exit(main())
