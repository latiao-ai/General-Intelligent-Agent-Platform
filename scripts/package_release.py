"""Create a source release with a fixed allowlist and secret checks.

`pi-runtime` 要带上源码、锁文件和已经打好的 `dist/main.js`。`dist/main.js`
必须是 esbuild 打出来的单文件，这样解包后的测试不用 `node_modules` 也能拉起
Node 桥接。`node_modules` 和密钥仍然排除。
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ('src', 'scripts', 'contracts', 'docs', 'deploy', 'tests', '.github',
               'pi-runtime')
FILES = ('.env.example', '.gitignore', 'THIRD-PARTY-NOTICES.md', 'requirements.txt',
         'requirements.lock.txt', 'requirements-dev.txt', 'pyproject.toml',
         '智能体中台运行机制说明.md')
SECRET = re.compile(rb'(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9_-]{24,})')


def _excluded(rel: Path) -> bool:
    """node_modules、缓存和点目录不进发布包，遍历时直接跳过。

    `node_modules/.bin` 里全是符号链接。若先做符号链接检查再排除，
    打包会在还没被排除的依赖目录上失败。
    """
    return any(
        (part.startswith('.') and part not in ('.github', '.env.example', '.gitignore'))
        or part in ('__pycache__', 'node_modules', 'secrets', 'profile', 'artifacts')
        for part in rel.parts)


def _walk(directory: Path, root: Path):
    for path in directory.iterdir():
        rel = path.relative_to(root)
        if _excluded(rel):
            continue
        if path.is_symlink():
            raise ValueError('Symlink is not allowed: ' + str(rel))
        if path.is_dir():
            yield from _walk(path, root)
        elif path.is_file():
            yield path


def source_files(root: Path) -> list[Path]:
    paths = [root / name for name in FILES if (root / name).is_file()]
    for name in DIRECTORIES:
        directory = root / name
        if directory.is_symlink():
            raise ValueError('Symlink is not allowed: ' + name)
        if directory.is_dir():
            paths.extend(_walk(directory, root))
    out = []
    for path in sorted(set(paths)):
        if path.is_symlink():
            raise ValueError('Symlink is not allowed: ' + str(path.relative_to(root)))
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] == 'deploy' and (len(rel.parts) != 2 or not path.name.endswith('.example')):
            continue
        if _excluded(rel):
            continue
        if path.suffix in ('.pyc', '.pyo', '.pem', '.key', '.sqlite', '.db') or path.name.startswith('.env.') and path.name != '.env.example':
            continue
        if path.name == '.env':
            continue
        if SECRET.search(path.read_bytes()):
            raise ValueError('Potential secret found in ' + str(rel))
        out.append(path)
    return out


def local_secrets(root: Path) -> set[bytes]:
    values: set[bytes] = set()
    def collect(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if re.search(r'key|token|password|secret', str(key), re.I) and isinstance(value, str) and len(value) >= 12:
                    values.add(value.encode())
                else:
                    collect(value)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(dict(os.environ))
    env = root / '.env'
    if env.is_file():
        for line in env.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            value = value.strip().strip("\"'")
            collect({key: value})
            try:
                collect(json.loads(value))
            except ValueError:
                pass
    folder = root / 'secrets'
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536:
                content = path.read_bytes().strip()
                if 12 <= len(content) <= 1024:
                    values.add(content)
    return {value for value in values if not value.lower().startswith((b'replace-', b'your-', b'changeme'))}


def _require_bundled_bridge(root: Path) -> None:
    """解包后的 unittest 会直接 `node pi-runtime/dist/main.js`。

    tsc 产物还在 `import '@mariozechner/pi-ai'`，没有 node_modules 就会
    PiBridgeError。发布前必须已经跑过 `npm run bundle`。没有 `pi-runtime`
    目录的树（打包逻辑自己的夹具）跳过这道检查。
    """
    if not (root / 'pi-runtime').is_dir():
        return
    main = root / 'pi-runtime' / 'dist' / 'main.js'
    if not main.is_file():
        raise ValueError(
            'pi-runtime/dist/main.js 缺失：先在 pi-runtime 执行 npm run check')
    text = main.read_text(encoding='utf-8', errors='replace')
    if re.search(r'''from\s+['"]@mariozechner/pi-ai''', text):
        raise ValueError(
            'pi-runtime/dist/main.js 仍依赖 node_modules：'
            '先在 pi-runtime 执行 npm run bundle')


def package(root: Path, output: Path) -> str:
    root = root.resolve()
    _require_bundled_bridge(root)
    paths = source_files(root)
    # Complete validation before creating the output; never leave a partial release.
    bodies = {str(p.relative_to(root)): p.read_bytes() for p in paths}
    known_secrets = local_secrets(root)
    for name, body in bodies.items():
        if SECRET.search(body) or any(value in body for value in known_secrets):
            raise ValueError('Potential secret found in ' + name)
    sums = ''.join(hashlib.sha256(body).hexdigest() + '  ' + name + '\n'
                   for name, body in sorted(bodies.items()))
    bodies['SHA256SUMS'] = sums.encode()
    if output.exists():
        raise FileExistsError('Refusing to overwrite existing release')
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open('xb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                for name, body in sorted(bodies.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(body)
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(body))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'webpage-agent.tar.gz')
    args = parser.parse_args()
    digest = package(ROOT, args.output)
    print(str(args.output.resolve()) + '\nSHA256 ' + digest)


if __name__ == '__main__':
    main()
