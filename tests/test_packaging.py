# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""What the built distribution contains, and what `import seerdb` loads.

The wheel and the sdist ship the whole package, the Mirror (``seerdb.server``)
included, behind the ``mirror`` extra for its backends' dependencies (#1684).
Importing ``seerdb`` must still load neither the Mirror nor those dependencies.
"""

from __future__ import annotations

import ast
import email
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / 'seerdb'


def _imported_subpackages(module_path: Path) -> set[str]:
    # Every seerdb subpackage (`client` / `common` / `server`) this module
    # imports from, whether written `from seerdb.common...`, `from seerdb import
    # common`, or a relative `from ..common...`. TYPE_CHECKING-only imports count
    # too: the layering must hold in the type graph, not just at runtime.
    rel = module_path.relative_to(_PKG).parts  # e.g. ('common', 'tns.py')
    here = rel[0]
    tree = ast.parse(module_path.read_text(), filename=str(module_path))
    found: set[str] = set()

    def note(dotted: str) -> None:
        parts = dotted.split('.')
        if parts[0] == 'seerdb' and len(parts) > 1:
            found.add(parts[1])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                note(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                note(node.module or '')
            elif node.level == 1:
                # `from . import common` inside seerdb/<here>/ -> sibling subpkg
                for alias in node.names:
                    found.add(alias.name)
            elif node.level == 2:
                # `from ..common import X` -> the named subpackage
                if node.module:
                    found.add(node.module.split('.')[0])
    found.discard(here)
    return found


def test_import_seerdb_does_not_eagerly_load_the_mirror() -> None:
    # A fresh interpreter: `import seerdb` must pull in neither seerdb.server
    # nor a backend's driver, so a client never pays for the Mirror shipping
    # beside it. The lazy API still reaches the Mirror when asked.
    code = (
        'import sys, seerdb;'
        "assert 'seerdb.server' not in sys.modules, 'server loaded eagerly';"
        "assert 'psycopg' not in sys.modules, 'a backend driver loaded';"
        'assert callable(seerdb.connect);'
        'assert callable(seerdb.serve) and isinstance(seerdb.Server, type);'
        "assert 'seerdb.server' in sys.modules, 'lazy access should load it';"
        "assert 'psycopg' not in sys.modules, 'the Mirror loaded a backend';"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, '-c', code], capture_output=True, text=True, cwd=_ROOT
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


def _package_files() -> set[str]:
    # The package as the checkout has it, the way a distribution lists it.
    files = {p for p in _PKG.rglob('*.py') if '__pycache__' not in p.parts}
    files.add(_PKG / 'py.typed')
    return {p.relative_to(_ROOT).as_posix() for p in files}


@pytest.fixture(scope='module')
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip('build')
    out = tmp_path_factory.mktemp('dist')
    subprocess.run(
        [sys.executable, '-m', 'build', '--no-isolation', '--outdir', str(out)],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    return out


def test_the_wheel_ships_the_package_and_nothing_else(dist: Path) -> None:
    (wheel,) = dist.glob('*.whl')
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    shipped = {n for n in names if '.dist-info/' not in n}
    assert shipped == _package_files()


def test_the_sdist_ships_the_whole_package(dist: Path) -> None:
    (sdist,) = dist.glob('*.tar.gz')
    with tarfile.open(sdist) as tar:
        # Each name sits under the sdist's own top directory, seerdb-x.y.z/.
        names = {name.split('/', 1)[1] for name in tar.getnames() if '/' in name}
    assert _package_files() <= names


def test_the_mirror_extra_carries_the_backend_driver(dist: Path) -> None:
    # psycopg comes with the `mirror` extra, never with a plain install.
    (wheel,) = dist.glob('*.whl')
    with zipfile.ZipFile(wheel) as zf:
        (metadata,) = (n for n in zf.namelist() if n.endswith('.dist-info/METADATA'))
        message = email.message_from_bytes(zf.read(metadata))
    assert 'mirror' in message.get_all('Provides-Extra', [])
    requires = message.get_all('Requires-Dist', [])
    psycopg = [r for r in requires if r.startswith('psycopg')]
    assert psycopg and all('extra == "mirror"' in r for r in psycopg), requires


# Which subpackage may import which. `common` is the shared codec leaf: it must
# depend on neither the client nor the Mirror. The client and the Mirror both
# build on `common` but never on each other, so the Mirror stays fully
# decoupled from the DB-API client (and vice versa).
_FORBIDDEN_EDGES = {
    'common': {'client', 'server'},
    'client': {'server'},
    'server': {'client'},
}


@pytest.mark.parametrize('layer', sorted(_FORBIDDEN_EDGES))
def test_subpackage_layering_is_enforced(layer: str) -> None:
    forbidden = _FORBIDDEN_EDGES[layer]
    violations = []
    for module_path in sorted((_PKG / layer).rglob('*.py')):
        bad = _imported_subpackages(module_path) & forbidden
        if bad:
            rel = module_path.relative_to(_ROOT)
            violations.append(f'{rel} imports from {sorted(bad)}')
    assert not violations, 'subpackage layering violated:\n' + '\n'.join(violations)
