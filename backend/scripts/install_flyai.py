"""Install the unmodified official CLI; no npm hooks/global install/runtime download."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

VERSION = '1.0.16'
URL = f'https://registry.npmjs.org/@fly-ai/flyai-cli/-/flyai-cli-{VERSION}.tgz'
INTEGRITY = 'Ksi06xvJSJcdhmfoDbpAnA84K4/pF+SkLsa5ZLvNruUc3e2EpoGaI6FcJXKTODvB/evprfy5pdRzW3lVazqfkA=='
TARGET = Path(__file__).resolve().parents[1] / '.runtime' / f'flyai-{VERSION}'
FILES = {'package/dist/flyai-bundle.cjs': 'flyai-bundle.cjs',
         'package/package.json': 'package.json', 'package/README.md': 'README.md'}


def install(archive: bytes) -> Path:
    if base64.b64encode(hashlib.sha512(archive).digest()).decode() != INTEGRITY:
        raise ValueError('Official package integrity mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
        # Extract only known regular files; never extract paths supplied by tar.
        contents = {}
        for source, dest in FILES.items():
            member = package.getmember(source)
            if not member.isfile() or member.size > 2_000_000:
                raise ValueError('Unexpected official package entry')
            contents[dest] = package.extractfile(member).read()
    meta = json.loads(contents['package.json'])
    if meta.get('name') != '@fly-ai/flyai-cli' or meta.get('version') != VERSION:
        raise ValueError('Unexpected official package identity')
    TARGET.mkdir(parents=True, exist_ok=True)
    for dest, content in contents.items():
        temporary = TARGET / (dest + '.tmp')
        temporary.write_bytes(content)
        temporary.replace(TARGET / dest)
    return TARGET


if __name__ == '__main__':
    with urlopen(URL, timeout=30) as response:
        archive = response.read(2_000_001)
    if len(archive) > 2_000_000:
        raise ValueError('Package too large')
    print(f'Official CLI {VERSION} installed: {install(archive)}')
