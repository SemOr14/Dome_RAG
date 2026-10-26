from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
from urllib.parse import urlsplit
from .app import RAG


def make_server(port=8790, allow_llm=False, rag=None):
    service = rag or RAG()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # HTTP paths/bodies may contain user data; use the redacted structured RAG log.

        def reply(self, status, data):
            body = json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path!='/health':
                return self.reply(404,{'error':'Not found; endpoints: GET /health, POST /query'})
            return self.reply(200,dict(status='ok',approved_records=len(service.engine.records),
                semantic_index=service.engine.semantic,deepseek_configured=bool(os.getenv('DEEPSEEK_API_KEY')),
                llm_requests_allowed=allow_llm,equipment_executor=False))

        def do_POST(self):
            if self.path!='/query':
                return self.reply(404,{'error':'Not found'})
            origin = self.headers.get('Origin')
            if origin:
                p = urlsplit(origin)
                if p.scheme!='http' or p.hostname not in {'127.0.0.1','localhost'} or p.port!=self.server.server_port:
                    return self.reply(403,{'error':'Origin rejected'})
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 1<=size<=40000:
                    return self.reply(413,{'error':'Request body must be 1..40000 bytes'})
                request = json.loads(self.rfile.read(size))
                if set(request)-{'question','observation','use_llm','max_chars'}:
                    raise ValueError('Unsupported request fields')
                if request.get('use_llm') not in {None,True,False}:
                    raise ValueError('use_llm must be boolean')
                if request.get('use_llm') and not allow_llm:
                    return self.reply(403,{'error':'Cloud calls disabled; start serve with --allow-llm'})
                with lock:
                    answer = service.answer(request['question'],observation=request.get('observation'),
                        use_llm=bool(request.get('use_llm')),max_chars=request.get('max_chars'))
                return self.reply(200,answer)
            except (ValueError,KeyError,TypeError) as exc:
                return self.reply(400,{'error':str(exc)[:180]})
            except Exception:
                return self.reply(500,{'error':'Internal RAG error; no equipment action was executed'})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def serve(port=8790,allow_llm=False,semantic=True):
    with make_server(port,allow_llm,RAG(semantic)) as server:
        print(f'RAG API: http://127.0.0.1:{server.server_port}/health',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
