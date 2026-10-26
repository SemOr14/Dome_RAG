from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .common import ROOT, INDEX, load_env, read_json, write_json


def main(argv=None):
    load_env()
    parser = argparse.ArgumentParser(description='Dome RAG: grounded retrieval, context and optional DeepSeek proposals')
    commands = parser.add_subparsers(dest='command',required=True)
    build = commands.add_parser('build')
    build.add_argument('--semantic',action='store_true')
    build.add_argument('--model')
    for cmd in ['search','ask','context']:
        sub = commands.add_parser(cmd)
        sub.add_argument('question')
        sub.add_argument('--lexical-only',action='store_true')
        sub.add_argument('--output',type=Path)
        if cmd=='search':
            sub.add_argument('--kind',choices=['faq','situation','reference'])
            sub.add_argument('--include-reference',action='store_true')
            sub.add_argument('--limit',type=int,default=5)
        else:
            sub.add_argument('--observation',type=Path)
            sub.add_argument('--max-chars',type=int)
        if cmd=='ask':
            sub.add_argument('--llm',action='store_true')
    evaluate = commands.add_parser('evaluate')
    evaluate.add_argument('--lexical-only',action='store_true')
    evaluate.add_argument('--output',type=Path,default=ROOT/'evaluation'/'retrieval_report.json')
    serve = commands.add_parser('serve')
    serve.add_argument('--port',type=int,default=8790)
    serve.add_argument('--allow-llm',action='store_true')
    serve.add_argument('--lexical-only',action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command=='build':
            from .corpus import build
            result = build(args.semantic,args.model)
        elif args.command=='serve':
            from .server import serve
            serve(args.port,args.allow_llm,not args.lexical_only)
            return 0
        else:
            from .search import SearchEngine
            engine = SearchEngine(semantic=not args.lexical_only)
            if args.command=='search':
                result = dict(results=engine.search(args.question,args.limit,args.kind,args.include_reference),
                              semantic_fallback_error=engine.last_semantic_error)
            elif args.command=='evaluate':
                from .evaluate import evaluate
                result = evaluate(engine)
            elif args.command=='context':
                from .context import build_context
                result,_ = build_context(engine,args.question,read_json(args.observation) if args.observation else None,args.max_chars)
            else:
                from .app import RAG
                app = RAG.__new__(RAG)
                app.engine = engine
                result = app.answer(args.question,observation=read_json(args.observation) if args.observation else None,
                                    use_llm=args.llm,max_chars=args.max_chars)
        if getattr(args,'output',None):
            write_json(args.output,result)
            print(str(args.output))
        else:
            print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,ImportError) as exc:
        print('RAG error: '+str(exc),file=sys.stderr)
        return 2
