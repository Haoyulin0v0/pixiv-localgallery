"""A saved Pixiv artwork resolves to its local UID, opens and reveals the original."""
import gc
import importlib.util
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gallery',ROOT/'server.py')
gallery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(gallery)
with tempfile.TemporaryDirectory() as temporary:
    gallery.DATA=Path(temporary)/'data'
    gallery.IMAGES=gallery.DATA/'images'
    gallery.DATABASE=gallery.DATA/'library.sqlite3'
    database=gallery.connection()
    database.execute('INSERT INTO artworks VALUES (?,?,?,?,?,?,?,?,?,?)',
                     ('local_uid','10000001','Example','Artist','42','[]','',json.dumps(['one.png','two.png']),1,1))
    database.commit();database.close()
    for name in ('one.png','two.png'): (gallery.IMAGES/name).write_bytes(b'example')
    service=gallery.LocalHTTPServer(('127.0.0.1',0),gallery.Handler)
    worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
    def request(uid='local_uid',page=0,action='both'):
        payload=json.dumps({'page':page,'action':action}).encode()
        request=urllib.request.Request(f'http://127.0.0.1:{service.server_port}/api/artworks/{uid}/open',
            data=payload,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request) as response: return response.status,json.loads(response.read())
        except urllib.error.HTTPError as error: return error.code,error.read()
    try:
        with patch.object(gallery.os,'startfile',create=True) as opened,patch.object(gallery.subprocess,'Popen') as revealed:
            status,result=request(page=1)
            assert status==200 and result['path']==str((gallery.IMAGES/'two.png').resolve())
            opened.assert_called_once_with(result['path'])
            revealed.assert_called_once_with(['explorer.exe','/select,',result['path']])
            opened.reset_mock();revealed.reset_mock()
            for kwargs in ({'uid':'10000001'},{'page':2},{'page':-1},{'page':'0'},{'action':'unknown'}):
                assert request(**kwargs)[0]==400,kwargs
            opened.assert_not_called();revealed.assert_not_called()
    finally:
        service.shutdown();service.server_close();worker.join();gc.collect()
print('Saved artwork opens and reveals the selected local original; invalid targets perform no native actions')
