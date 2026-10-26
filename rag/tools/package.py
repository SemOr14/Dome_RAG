from pathlib import Path
import hashlib
import json
import os
import zipfile

root = Path(__file__).resolve().parents[1]
validation = json.loads((root/'evaluation/build_validation.json').read_text(encoding='utf-8'))
if validation.get('unit_test_result') != 'PASS':
    raise SystemExit('Package requires a passing validation report')
excluded = {'.git','.venv','__pycache__','models','logs','dist'}
files = sorted(p for p in root.rglob('*') if p.is_file()
               and not any(x in excluded for x in p.relative_to(root).parts)
               and not p.name.endswith(('.pyc','.tmp'))
               and not (p.name.startswith('.env') and p.name!='.env.example'))
secrets = []
config = root / '.env'
if config.exists():
    for line in config.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line:
            name,value=line.split('=',1)
            if any(x in name for x in ['KEY','TOKEN','PASSWORD']) and len(value.strip())>8:
                secrets.append(value.strip().encode())
for path in files:
    content=path.read_bytes()
    if any(secret in content for secret in secrets):
        raise SystemExit('Secret found in package candidate: '+str(path.relative_to(root)))
dest=root/'dist';dest.mkdir(exist_ok=True)
archive=dest/'dome-rag-20261026.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for path in files:
        z.write(path,'dome-rag/'+path.relative_to(root).as_posix())
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    assert len(z.namelist())==len(files)
    for path in files:
        assert z.read('dome-rag/'+path.relative_to(root).as_posix())==path.read_bytes()
sha=hashlib.sha256(archive.read_bytes()).hexdigest()
(dest/(archive.name+'.sha256')).write_text(sha+'  '+archive.name+'\n',encoding='ascii')
print(json.dumps(dict(zip=str(archive),files=len(files),bytes=archive.stat().st_size,sha256=sha,
                      secrets_included=False,weights_included=False),ensure_ascii=False))
