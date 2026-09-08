import gzip
import hashlib
from pathlib import Path
import tarfile

body = gzip.decompress(Path('validated-docfx.tar.gz').read_bytes())
assert hashlib.sha256(body).hexdigest() == 'd6022b665c3547f2ecbb6d8e916cfa3879493a93e10d324f1e7266d684e851b9'
Path('artifact.tar').write_bytes(body)
Path('site').mkdir()
with tarfile.open('artifact.tar') as archive:
    Path('site/index.html').write_bytes(archive.extractfile('index.html').read())
