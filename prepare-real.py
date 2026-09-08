import gzip
import hashlib
from pathlib import Path
import tarfile

body = gzip.decompress(Path('validated-docfx.tar.gz').read_bytes())
assert hashlib.sha256(body).hexdigest() == 'd8c6be40fabd8e63e61dd74a9993a655598d919dcb09cb20774a481064597f89'
Path('artifact.tar').write_bytes(body)
Path('site').mkdir()
with tarfile.open('artifact.tar') as archive:
    Path('site/index.html').write_bytes(archive.extractfile('./index.html').read())
