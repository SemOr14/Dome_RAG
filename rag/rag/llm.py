from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from .common import log_event
from .context import SYSTEM


class DeepSeekError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeepSeekError('Redirect rejected; authentication was not forwarded')


def validate_reply(reply, context):
    required = {'summary','interpretations','proposed_checks','cited_record_ids','source_keys'}
    if not isinstance(reply,dict) or set(reply)!=required:
        raise DeepSeekError('Invalid model response schema')
    if not isinstance(reply['summary'],str) or not 1<=len(reply['summary'])<=6000:
        raise DeepSeekError('Invalid model summary')
    for key in required-{'summary'}:
        if not isinstance(reply[key],list) or len(reply[key])>30 or any(not isinstance(x,str) or len(x)>3000 for x in reply[key]):
            raise DeepSeekError('Invalid model list')
    allowed_ids = {r['id'] for r in context['approved_records']} | {c['id'] for c in context['conflicts']}
    allowed_sources = {s['key'] for s in context['sources']}
    if not reply['cited_record_ids'] or not set(reply['cited_record_ids'])<=allowed_ids:
        log_event('llm_validation',reason='record_citations',allowed_ids=sorted(allowed_ids),
                  received_ids=[x[:60] for x in reply['cited_record_ids']])
        raise DeepSeekError('Unknown or missing record citation')
    if not reply['source_keys'] or not set(reply['source_keys'])<=allowed_sources:
        raise DeepSeekError('Unknown or missing source citation')
    return reply


def generate(context, *, base_url=None, api_key=None, timeout=None, model=None):
    if context['guard'] or not context['approved_records']:
        raise DeepSeekError('No eligible context for a model call')
    api_key = api_key if api_key is not None else os.getenv('DEEPSEEK_API_KEY','')
    if not api_key:
        raise DeepSeekError('DEEPSEEK_API_KEY is not configured')
    base_url = base_url or os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com')
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeepSeekError('Invalid DeepSeek endpoint')
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'}):
        raise DeepSeekError('HTTPS required for a remote model endpoint')
    endpoint = base_url.rstrip('/') + '/chat/completions'
    model = model or os.getenv('DEEPSEEK_MODEL','deepseek-flash')
    body = dict(model=model, messages=[dict(role='system',content=SYSTEM),
                dict(role='user',content=json.dumps(context,ensure_ascii=False))],
                stream=False, max_tokens=1800, response_format={'type':'json_object'},
                thinking={'type':os.getenv('DEEPSEEK_THINKING','disabled')})
    request = urllib.request.Request(endpoint, data=json.dumps(body,ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},method='POST')
    opener = urllib.request.build_opener(NoRedirect())
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout or float(os.getenv('DEEPSEEK_TIMEOUT','30'))) as response:
            raw = response.read(1000001)
            if len(raw)>1000000:
                raise DeepSeekError('Model response is too large')
        data = json.loads(raw)
        choice = data['choices'][0]
        if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls'):
            raise DeepSeekError('Incomplete response or unexpected tool call')
        reply = validate_reply(json.loads(choice['message']['content']), context)
    except urllib.error.HTTPError as exc:
        log_event('llm_call',model=model,ok=False,http_status=exc.code,latency_ms=round((time.monotonic()-started)*1000))
        raise DeepSeekError('DeepSeek HTTP '+str(exc.code)) from None
    except DeepSeekError as exc:
        log_event('llm_call',model=model,ok=False,error=str(exc),latency_ms=round((time.monotonic()-started)*1000))
        raise
    except (TimeoutError,OSError,ValueError,KeyError,IndexError) as exc:
        log_event('llm_call',model=model,ok=False,error_type=type(exc).__name__,latency_ms=round((time.monotonic()-started)*1000))
        raise DeepSeekError('DeepSeek call or grounded-response validation failed') from None
    usage = data.get('usage',{})
    log_event('llm_call',model=model,ok=True,purpose='RAG diagnostic proposal',
              prompt_tokens=usage.get('prompt_tokens'),completion_tokens=usage.get('completion_tokens'),
              latency_ms=round((time.monotonic()-started)*1000),cited_record_ids=reply['cited_record_ids'])
    return dict(model=model, proposal=reply, usage=usage,
                status='model_proposal_citations_checked_not_factual_verification')
