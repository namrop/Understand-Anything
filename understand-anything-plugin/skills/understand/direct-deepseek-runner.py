#!/usr/bin/env python3
"""Direct DeepSeek V4 Pro runner for Understand-Anything batch artifacts.

This is a non-interactive runner for large codebases where an agentic per-batch
Claude/Hermes loop is too slow or too conversational. It expects the normal
Understand Anything scan/batch phase to have already produced:

    <project>/.understand-anything/intermediate/scan-result.json
    <project>/.understand-anything/intermediate/batches.json

It then runs the deterministic structure extractor, sends compact JSON-mode
requests to DeepSeek, writes batch-<n>.json files compatible with
merge-batch-graphs.py, and assembles knowledge-graph.json.

Secrets are read only from the process environment or explicit --env-file paths;
no local home/Hermes credential paths are hardcoded.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import requests
except Exception as e:
    print(f"FATAL: requests unavailable: {e}", file=sys.stderr)
    raise

try:
    from json_repair import repair_json
except Exception:
    repair_json = None

PROJECT_ROOT = Path.cwd()
OUT = PROJECT_ROOT / '.understand-anything'
INTER = OUT / 'intermediate'
TMP = OUT / 'tmp'
SKILL_DIR = Path(__file__).resolve().parent
EXTRACT = SKILL_DIR / 'extract-structure.mjs'
MERGE = SKILL_DIR / 'merge-batch-graphs.py'
MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')
API_URL = os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/v1/chat/completions')
MAX_WORKERS = int(os.environ.get('UA_DIRECT_WORKERS', '4'))
BATCH_LIMIT = int(os.environ.get('UA_DIRECT_LIMIT', '0')) or None
RESUME = os.environ.get('UA_DIRECT_RESUME', '1') != '0'
CONTENT_CHAR_BUDGET = int(os.environ.get('UA_DIRECT_CONTENT_CHARS', '24000'))
MAX_PER_FILE_CHARS = int(os.environ.get('UA_DIRECT_MAX_FILE_CHARS', '2500'))
API_KEY = ''

VALID_NODE_TYPES = {'file','function','class','module','concept','config','document','service','table','endpoint','pipeline','schema','resource','domain','flow','step','article','entity','topic','claim','source'}
VALID_EDGE_TYPES = {'imports','exports','contains','inherits','implements','calls','subscribes','publishes','middleware','reads_from','writes_to','transforms','validates','depends_on','tested_by','configures','related','similar_to','deploys','serves','provisions','triggers','migrates','documents','routes','defines_schema','contains_flow','flow_step','cross_domain','cites','contradicts','builds_on','exemplifies','categorized_under','authored_by'}
VALID_COMPLEXITY = {'simple','moderate','complex'}
FILE_LEVEL_TYPES = {'file','config','document','service','pipeline','table','schema','resource','endpoint'}


def json_dumps(obj: Any, max_chars: int | None = None) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    if max_chars and len(s) > max_chars:
        return s[:max_chars] + '…TRUNCATED'
    return s


def node_prefix_for_file(file_category: str, path: str, language: str = '') -> tuple[str, str]:
    cat = (file_category or 'code').lower()
    p = path.lower()
    lang = (language or '').lower()
    if cat == 'config': return 'config', 'config'
    if cat == 'docs': return 'document', 'document'
    if cat == 'infra':
        if '.github/workflows/' in p or p.endswith(('.gitlab-ci.yml','jenkinsfile')): return 'pipeline', 'pipeline'
        if p.endswith(('.tf','.tfvars')) or 'terraform' in p: return 'resource', 'resource'
        return 'service', 'service'
    if cat == 'data':
        if lang in {'graphql','protobuf','prisma'} or p.endswith(('.graphql','.proto','.prisma')): return 'schema', 'schema'
        if 'openapi' in p or 'swagger' in p: return 'endpoint', 'endpoint'
        return 'table', 'table'
    return 'file', 'file'


def file_node_id(f: dict[str, Any]) -> str:
    prefix, _typ = node_prefix_for_file(f.get('fileCategory','code'), f.get('path',''), f.get('language',''))
    return f'{prefix}:{f["path"]}'


def complexity_from_lines(lines: int) -> str:
    if lines < 50: return 'simple'
    if lines <= 220: return 'moderate'
    return 'complex'


def safe_name(path: str) -> str:
    return Path(path).name or path


def sanitize_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list):
        tags = []
    out = []
    for t in tags:
        if not isinstance(t, str): continue
        v = re.sub(r'[^a-z0-9+._-]+','-', t.strip().lower()).strip('-')[:40]
        if v and v not in out: out.append(v)
    return out[:6] or ['code']


def normalize_batch_output(data: Any, batch: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if isinstance(data, dict) and 'nodes' in data and 'edges' in data:
        nodes = data.get('nodes') or []
        edges = data.get('edges') or []
    else:
        raise ValueError('JSON must be object with nodes and edges arrays')
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError('nodes/edges must be arrays')

    files = batch.get('files') or batch.get('batchFiles') or []
    expected_file_ids = {file_node_id(f): f for f in files if isinstance(f, dict) and f.get('path')}
    seen_ids: set[str] = set()
    norm_nodes: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict): continue
        nid = str(n.get('id') or '').strip()
        typ = str(n.get('type') or '').strip().lower()
        if not nid or typ not in VALID_NODE_TYPES: continue
        if nid in seen_ids: continue
        seen_ids.add(nid)
        name = str(n.get('name') or nid.split(':')[-1]).strip()[:180]
        summary = str(n.get('summary') or name).strip()[:900]
        comp = str(n.get('complexity') or 'moderate').lower()
        if comp not in VALID_COMPLEXITY: comp = 'moderate'
        out = {
            'id': nid,
            'type': typ,
            'name': name,
            'summary': summary,
            'tags': sanitize_tags(n.get('tags')),
            'complexity': comp,
        }
        if isinstance(n.get('filePath'), str) and n['filePath'].strip(): out['filePath'] = n['filePath'].strip()
        if isinstance(n.get('lineRange'), list) and len(n['lineRange']) == 2:
            try: out['lineRange'] = [int(n['lineRange'][0]), int(n['lineRange'][1])]
            except Exception: pass
        if isinstance(n.get('languageNotes'), str) and n['languageNotes'].strip(): out['languageNotes'] = n['languageNotes'].strip()[:500]
        norm_nodes.append(out)

    # Ensure every batch file has a file-level node even if model omitted it.
    for fid, f in expected_file_ids.items():
        if fid not in seen_ids:
            prefix, typ = node_prefix_for_file(f.get('fileCategory','code'), f.get('path',''), f.get('language',''))
            norm_nodes.append({
                'id': fid, 'type': typ, 'name': safe_name(f['path']), 'filePath': f['path'],
                'summary': f"{typ.title()} file `{f['path']}` in the target repository.",
                'tags': [typ, f.get('language','unknown')],
                'complexity': complexity_from_lines(int(f.get('sizeLines') or 0)),
            })
            seen_ids.add(fid)

    norm_edges: list[dict[str, Any]] = []
    edge_seen: set[tuple[str,str,str]] = set()
    for e in edges:
        if not isinstance(e, dict): continue
        src = str(e.get('source') or '').strip()
        tgt = str(e.get('target') or '').strip()
        typ = str(e.get('type') or 'related').strip().lower()
        if typ not in VALID_EDGE_TYPES: typ = 'related'
        if not src or not tgt or src == tgt: continue
        key = (src, tgt, typ)
        if key in edge_seen: continue
        edge_seen.add(key)
        try: w = float(e.get('weight', 0.6))
        except Exception: w = 0.6
        out = {'source': src, 'target': tgt, 'type': typ, 'direction': 'forward', 'weight': max(0.0, min(1.0, w))}
        if str(e.get('direction','forward')).lower() in {'forward','backward','bidirectional'}: out['direction'] = str(e.get('direction')).lower()
        if isinstance(e.get('description'), str) and e['description'].strip(): out['description'] = e['description'].strip()[:350]
        norm_edges.append(out)
    return {'nodes': norm_nodes, 'edges': norm_edges}


def deterministic_fallback(batch: dict[str, Any], structure: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    nodes=[]; edges=[]
    by_path = {r.get('path'): r for r in structure.get('results', []) if isinstance(r, dict)}
    for f in batch.get('files', []) or []:
        fid=file_node_id(f); prefix, typ=node_prefix_for_file(f.get('fileCategory','code'),f.get('path',''),f.get('language',''))
        r=by_path.get(f.get('path'), {})
        tags=[typ, f.get('language','unknown')]
        if Path(f['path']).name.startswith('test') or '.test.' in f['path'] or '.spec.' in f['path']: tags.append('test')
        nodes.append({'id':fid,'type':typ,'name':safe_name(f['path']),'filePath':f['path'],'summary':f"{typ.title()} file `{f['path']}` with {r.get('metrics',{}).get('functionCount',0)} functions and {r.get('metrics',{}).get('classCount',0)} classes.",'tags':sanitize_tags(tags),'complexity':complexity_from_lines(int(f.get('sizeLines') or r.get('totalLines') or 0))})
        for fn in (r.get('functions') or [])[:20]:
            name=fn.get('name') if isinstance(fn,dict) else None
            if not name: continue
            nid=f'function:{f["path"]}:{name}'
            nodes.append({'id':nid,'type':'function','name':name,'filePath':f['path'],'lineRange':[int(fn.get('startLine') or 1), int(fn.get('endLine') or fn.get('startLine') or 1)],'summary':f"Function `{name}` defined in `{f['path']}`.",'tags':['function',f.get('language','code')],'complexity':'moderate'})
            edges.append({'source':fid,'target':nid,'type':'contains','direction':'forward','weight':0.95})
        for cls in (r.get('classes') or [])[:12]:
            name=cls.get('name') if isinstance(cls,dict) else None
            if not name: continue
            nid=f'class:{f["path"]}:{name}'
            nodes.append({'id':nid,'type':'class','name':name,'filePath':f['path'],'lineRange':[int(cls.get('startLine') or 1), int(cls.get('endLine') or cls.get('startLine') or 1)],'summary':f"Class `{name}` defined in `{f['path']}`.",'tags':['class',f.get('language','code')],'complexity':'moderate'})
            edges.append({'source':fid,'target':nid,'type':'contains','direction':'forward','weight':0.95})
    for item in batch.get('batchImportData') or []:
        if isinstance(item, dict):
            src_path=item.get('source') or item.get('from') or item.get('path')
            for imp in item.get('imports',[]) or item.get('resolvedImports',[]) or []:
                tgt_path = imp.get('targetPath') if isinstance(imp,dict) else None
                if src_path and tgt_path:
                    edges.append({'source':f'file:{src_path}','target':f'file:{tgt_path}','type':'imports','direction':'forward','weight':0.8})
    return {'nodes':nodes,'edges':edges}


def extract_json(text: str) -> Any:
    text=text.strip()
    if text.startswith('```'):
        text=re.sub(r'^```(?:json)?\s*','',text)
        text=re.sub(r'\s*```$','',text)
    try:
        return json.loads(text)
    except Exception:
        start=text.find('{'); end=text.rfind('}')
        if start>=0 and end>start:
            candidate = text[start:end+1]
            try:
                return json.loads(candidate)
            except Exception:
                if repair_json is not None:
                    return json.loads(repair_json(candidate))
                raise
        if repair_json is not None:
            return json.loads(repair_json(text))
        raise


def call_deepseek(messages: list[dict[str,str]], max_tokens: int = 12000) -> tuple[str, dict[str,int]]:
    headers={'Authorization':f'Bearer {API_KEY}','Content-Type':'application/json'}
    payload={
        'model': MODEL,
        'messages': messages,
        'temperature': 0.1,
        'top_p': 0.9,
        'max_tokens': max_tokens,
        'response_format': {'type':'json_object'},
    }
    last_err=None
    for attempt in range(5):
        try:
            r=requests.post(API_URL, headers=headers, json=payload, timeout=900)
            if r.status_code in (429,500,502,503,504):
                last_err=f'HTTP {r.status_code}: {r.text[:500]}'
                time.sleep((2**attempt)+random.random()*2)
                continue
            r.raise_for_status()
            data=r.json()
            usage=data.get('usage') or {}
            with LOCK:
                STATS['api_calls'] += 1
                for k in ['prompt_tokens','completion_tokens','total_tokens']:
                    STATS['usage'][k] += int(usage.get(k) or 0)
            return data['choices'][0]['message']['content'], {k:int(usage.get(k) or 0) for k in ['prompt_tokens','completion_tokens','total_tokens']}
        except Exception as e:
            last_err=str(e)
            time.sleep((2**attempt)+random.random()*2)
    raise RuntimeError(last_err or 'DeepSeek request failed')


def source_snippets(files: list[dict[str,Any]]) -> dict[str, str]:
    remaining=CONTENT_CHAR_BUDGET
    snippets={}
    # allocate by order, huge files truncated but marked.
    for f in files:
        p=PROJECT_ROOT/f['path']
        if remaining <= 0: break
        try:
            data=p.read_text(errors='replace')
        except Exception as e:
            snippets[f['path']]=f'<unreadable: {e}>'
            continue
        cap=min(MAX_PER_FILE_CHARS, remaining)
        if len(data) > cap:
            head=cap//2
            tail=cap-head-120
            snippets[f['path']]=data[:head] + '\n\n…[TRUNCATED_MIDDLE]…\n\n' + data[-max(tail,0):]
        else:
            snippets[f['path']]=data
        remaining -= len(snippets[f['path']])
    return snippets


def compact_structure(structure: dict[str, Any]) -> dict[str, Any]:
    """Keep only the symbol/import facts the LLM needs; avoid giant AST payloads."""
    out = {'results': []}
    for r in structure.get('results', []) or []:
        if not isinstance(r, dict):
            continue
        item = {
            'path': r.get('path'),
            'language': r.get('language'),
            'totalLines': r.get('totalLines'),
            'metrics': r.get('metrics'),
            'imports': r.get('imports', [])[:40],
            'exports': r.get('exports', [])[:40],
            'functions': [],
            'classes': [],
        }
        for fn in (r.get('functions') or [])[:24]:
            if isinstance(fn, dict):
                item['functions'].append({k: fn.get(k) for k in ['name','startLine','endLine','isExported','async','visibility']})
        for cls in (r.get('classes') or [])[:16]:
            if isinstance(cls, dict):
                item['classes'].append({k: cls.get(k) for k in ['name','startLine','endLine','isExported','extends','implements']})
        out['results'].append(item)
    if isinstance(structure.get('callGraph'), list):
        out['callGraph'] = structure['callGraph'][:160]
    return out


def make_prompt(batch: dict[str,Any], scan: dict[str,Any], structure: dict[str,Any]) -> list[dict[str,str]]:
    files=batch.get('files') or batch.get('batchFiles') or []
    file_list=[{k:f.get(k) for k in ['path','language','sizeLines','fileCategory']} for f in files]
    snippets=source_snippets(files)
    system=(
        'You are an expert codebase graph analyst. Return ONLY valid JSON. No markdown. '
        'Analyze the provided batch of source files and produce nodes and edges compatible with Understand-Anything. '
        'Be concise, grounded, schema-valid, and keep output compact.'
    )
    user=f"""
Project: {scan.get('projectName') or scan.get('name') or PROJECT_ROOT.name}
Description: {scan.get('description') or scan.get('projectDescription') or 'Target codebase'}
Languages: {scan.get('languages')}
Frameworks: {scan.get('frameworks')}
Batch index: {batch.get('batchIndex')}

Required JSON shape:
{{"nodes":[GraphNode...],"edges":[GraphEdge...]}}

GraphNode fields:
- id string. File-level IDs must use prefix by category: file:path, config:path, document:path, service:path, pipeline:path, table:path, schema:path, resource:path, endpoint:path.
- function IDs: function:path:name. class IDs: class:path:name.
- type one of: file,function,class,module,concept,config,document,service,table,endpoint,pipeline,schema,resource.
- name, filePath, optional lineRange [start,end], summary 1 sentence, tags 3-5 lowercase hyphenated strings, complexity simple|moderate|complex.

GraphEdge fields:
- source, target, type, direction, weight, optional description.
- allowed type examples: imports, contains, calls, depends_on, configures, documents, deploys, serves, triggers, defines_schema, routes, related, tested_by.
- direction must be forward|backward|bidirectional. weight 0..1.

Rules:
1. Emit one file-level node for every file listed.
2. Emit NO class nodes and NO function nodes in this pass. This is a file-level semantic graph pass. Use type values from the file-level set: file, config, document, service, pipeline, table, schema, resource, endpoint.
3. Use batchImportData for imports edges; prefer file-to-file imports.
4. Add semantic/config/docs/test edges when clearly supported by file names, imports, or content.
5. Do not reference nodes that you did not emit unless they are file-level neighbor nodes from neighborMap/imports.
6. Hard cap: nodes <= number of files listed; edges <= 35. If there are more imports, choose the most architecturally important.
7. Keep summaries under 12 words. Tags: max 3 short strings.
8. Omit lineRange. For edges include only: source, target, type, weight. Omit edge descriptions.
9. Return minified JSON, not pretty-printed. Never include comments, trailing commas, or markdown fences.
10. Output ONLY one JSON object; no prose; no markdown fences.

Files:
{json_dumps(file_list)}

Pre-resolved batch import data:
{json_dumps(batch.get('batchImportData') or {}, 80000)}

Cross-batch neighbor map:
{json_dumps(batch.get('neighborMap') or {}, 80000)}

Deterministic extracted structure:
{json_dumps(compact_structure(structure), 65000)}

Source snippets/truncated content:
{json_dumps(snippets, 30000)}
"""
    return [{'role':'system','content':system},{'role':'user','content':user}]


def run_structure(batch: dict[str,Any]) -> dict[str,Any]:
    idx=batch['batchIndex']
    inp=TMP/f'ua-direct-input-{idx}.json'
    out=TMP/f'ua-direct-structure-{idx}.json'
    payload={'projectRoot':str(PROJECT_ROOT),'batchFiles':batch.get('files') or batch.get('batchFiles') or [],'batchImportData':batch.get('batchImportData') or {}}
    inp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    subprocess.run(['node', str(EXTRACT), str(inp), str(out)], cwd=str(PROJECT_ROOT), check=True, timeout=180, capture_output=True, text=True)
    return json.loads(out.read_text(encoding='utf-8'))


def process_batch(batch: dict[str,Any], scan: dict[str,Any]) -> dict[str,Any]:
    idx=int(batch['batchIndex'])
    target=INTER/f'batch-{idx}.json'
    if RESUME and target.exists() and target.stat().st_size > 100:
        try:
            data=json.loads(target.read_text())
            if isinstance(data.get('nodes'), list) and isinstance(data.get('edges'), list):
                return {'idx':idx,'status':'skipped','nodes':len(data['nodes']),'edges':len(data['edges']),'usage':{}}
        except Exception:
            pass
    retries=0
    structure=run_structure(batch)
    fallback_reason=None
    try:
        messages=make_prompt(batch, scan, structure)
        content, usage=call_deepseek(messages, max_tokens=14000)
        try:
            parsed=extract_json(content)
            result=normalize_batch_output(parsed, batch)
        except Exception as e:
            (TMP/f'ua-direct-raw-batch-{idx}-attempt1.txt').write_text(content, encoding='utf-8', errors='replace')
            retries += 1
            repair_messages=messages + [
                {'role':'assistant','content':content[:12000]},
                {'role':'user','content':f'Your previous response was invalid JSON/schema: {e}. Return ONLY a corrected JSON object with nodes and edges arrays for the same batch.'}
            ]
            repaired, usage2=call_deepseek(repair_messages, max_tokens=14000)
            (TMP/f'ua-direct-raw-batch-{idx}-repair.txt').write_text(repaired, encoding='utf-8', errors='replace')
            for k,v in usage2.items(): usage[k]=usage.get(k,0)+v
            parsed=extract_json(repaired)
            result=normalize_batch_output(parsed, batch)
    except Exception as e:
        fallback_reason=str(e)
        result=deterministic_fallback(batch, structure)
        usage={}
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    status='ok' if not fallback_reason else 'fallback'
    with LOCK:
        STATS['completed'] += 1
        STATS['retried'] += retries
        if fallback_reason:
            STATS['fallback'] += 1
        STATS['batches'][str(idx)]={'status':status,'nodes':len(result['nodes']),'edges':len(result['edges']),'retries':retries,'fallback_reason':fallback_reason,'usage':usage}
        if STATS['completed'] % 5 == 0 or status != 'ok':
            write_report()
            print(f"progress: {STATS['completed']} done, fallback={STATS['fallback']}, retried={STATS['retried']}", flush=True)
    return {'idx':idx,'status':status,'nodes':len(result['nodes']),'edges':len(result['edges']),'usage':usage,'retries':retries,'fallback_reason':fallback_reason}


def write_report(extra: dict[str,Any] | None = None) -> None:
    data=dict(STATS)
    data['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    if extra: data.update(extra)
    (OUT/'deepseek-direct-run-report.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def run_merge() -> tuple[dict[str,Any], str]:
    proc=subprocess.run([sys.executable, str(MERGE), str(PROJECT_ROOT)], cwd=str(PROJECT_ROOT), text=True, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f'merge failed {proc.returncode}\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}')
    assembled=INTER/'assembled-graph.json'
    return json.loads(assembled.read_text(encoding='utf-8')), proc.stderr


def make_layers(nodes: list[dict[str,Any]]) -> list[dict[str,Any]]:
    groups: dict[str, tuple[str,str,list[str]]] = {}
    def add(key,name,desc,nid):
        if key not in groups: groups[key]=(name,desc,[])
        groups[key][2].append(nid)
    for n in nodes:
        if n.get('type') not in FILE_LEVEL_TYPES: continue
        fp=n.get('filePath') or n.get('id','').split(':',1)[-1]
        top=fp.split('/',1)[0]
        if top in {'agent','run_agent.py','model_tools.py','toolsets.py','batch_runner.py'}: key=('agent-core','Agent Core','Conversation loop, model routing, tool orchestration, and batch execution primitives.')
        elif top in {'tools'}: key=('tools','Tool Runtime','Hermes tool registry, tool implementations, and execution environments.')
        elif top in {'gateway'}: key=('gateway','Messaging Gateway','Gateway runtime and platform adapters for Telegram, Discord, Slack, webhooks, and other channels.')
        elif top in {'hermes_cli','cli.py','tui_gateway','ui-tui'}: key=('cli-tui','CLI and TUI','Command-line interface, terminal UI, dashboard surfaces, and supporting frontend assets.')
        elif top in {'plugins'}: key=('plugins','Plugin System','Plugin discovery and provider integrations for models, memory, media, web, and platform extensions.')
        elif top in {'skills','optional-skills'}: key=('skills','Skills Library','Procedural skill definitions and optional packaged task workflows.')
        elif top in {'cron'}: key=('scheduler','Scheduler','Cron/job scheduling, autonomous runs, and delivery plumbing.')
        elif top in {'acp_adapter'}: key=('acp','ACP Adapter','Agent Client Protocol adapter for IDE and editor integrations.')
        elif top in {'tests'}: key=('tests','Tests','Pytest and frontend test suites validating Hermes behavior.')
        elif top in {'docs','website'}: key=('docs','Documentation','Documentation site and explanatory guides.')
        elif top in {'environments'}: key=('environments','Execution and Evaluation Environments','Training/evaluation environments and runtime substrates.')
        else: key=('repo-root','Repository Configuration','Root-level configuration, scripts, release metadata, and miscellaneous support files.')
        add('layer:'+key[0], key[1], key[2], n['id'])
    return [{'id':k,'name':v[0],'description':v[1],'nodeIds':sorted(set(v[2]))} for k,v in groups.items()]


def make_tour(nodes: list[dict[str,Any]], layers: list[dict[str,Any]], edges: list[dict[str,Any]]) -> list[dict[str,Any]]:
    node_ids={n['id'] for n in nodes}
    candidates=[
        ('Orientation and docs','Start with the README and repository documentation to understand the project purpose and operator-facing workflow.',['document:README.md','document:AGENTS.md']),
        ('Agent core','Inspect the core conversation loop and model/tool orchestration surfaces.',['file:run_agent.py','file:model_tools.py','file:toolsets.py']),
        ('CLI runtime','Follow how the command-line interface routes user input into agent sessions and slash commands.',['file:cli.py','file:hermes_cli/main.py']),
        ('Tool runtime','Review tool discovery, registry, and execution environment mechanics.',['file:tools/registry.py','file:tools/terminal_tool.py','file:tools/delegate_tool.py']),
        ('Gateway','Trace how external messaging platforms enter Hermes through the gateway runtime and adapters.',['file:gateway/run.py','file:gateway/session.py']),
        ('Plugins','Review provider/plugin loading for extensibility across models, memory, media, and web integrations.',['file:plugins/__init__.py','file:hermes_cli/plugins.py']),
        ('Scheduler and background work','Inspect cron scheduling and autonomous job execution.',['file:cron/scheduler.py','file:cron/jobs.py']),
        ('Validation surface','Use the tests and docs to ground behavior and regression expectations.',['file:tests/conftest.py','document:docs/README.md']),
    ]
    steps=[]
    for i,(title,desc,ids) in enumerate(candidates,1):
        present=[x for x in ids if x in node_ids]
        if not present:
            continue
        steps.append({'order':len(steps)+1,'title':title,'description':desc,'nodeIds':present})
    return steps[:8]


def git_commit() -> str:
    try:
        return subprocess.check_output(['git','rev-parse','HEAD'], cwd=str(PROJECT_ROOT), text=True).strip()
    except Exception:
        return ''


def normalize_string_list(value: Any) -> list[str]:
    """Return a dashboard-schema-compatible string array.

    The scanner may report languages as a histogram object ({language: count}),
    while ProjectMetaSchema requires languages/frameworks to be string arrays.
    """
    if isinstance(value, dict):
        return [str(k) for k, v in sorted(value.items(), key=lambda kv: (-(int(kv[1]) if isinstance(kv[1], int) else 0), str(kv[0])))]
    if isinstance(value, list):
        return [str(x) for x in value]
    if value is None:
        return []
    return [str(value)]


def assemble_final(scan: dict[str,Any], assembled: dict[str,Any]) -> dict[str,Any]:
    nodes=assembled.get('nodes') or []
    edges=assembled.get('edges') or []
    layers=make_layers(nodes)
    tour=make_tour(nodes,layers,edges)
    project={
        'name': str(scan.get('projectName') or scan.get('name') or PROJECT_ROOT.name),
        'languages': normalize_string_list(scan.get('languages')),
        'frameworks': normalize_string_list(scan.get('frameworks')),
        'description': str(scan.get('description') or scan.get('projectDescription') or f'{PROJECT_ROOT.name} repository analysis'),
        'analyzedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
        'gitCommitHash': git_commit(),
    }
    return {'version':'1.0.0','kind':'codebase','project':project,'nodes':nodes,'edges':edges,'layers':layers,'tour':tour}


def validate_graph(g: dict[str,Any]) -> list[str]:
    issues=[]
    ids={n.get('id') for n in g.get('nodes',[]) if isinstance(n,dict)}
    for i,n in enumerate(g.get('nodes',[])):
        for k in ['id','type','name','summary','tags','complexity']:
            if k not in n: issues.append(f'node[{i}] missing {k}')
    for i,e in enumerate(g.get('edges',[])):
        if e.get('source') not in ids: issues.append(f'edge[{i}] missing source {e.get("source")}')
        if e.get('target') not in ids: issues.append(f'edge[{i}] missing target {e.get("target")}')
    for layer in g.get('layers',[]):
        layer['nodeIds']=[nid for nid in layer.get('nodeIds',[]) if nid in ids]
    for step in g.get('tour',[]):
        step['nodeIds']=[nid for nid in step.get('nodeIds',[]) if nid in ids]
    return issues


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Run a direct DeepSeek JSON-mode semantic pass over precomputed Understand Anything batches.'
    )
    parser.add_argument('project_root', nargs='?', default=os.getcwd(), help='Repository/project root containing .understand-anything/intermediate artifacts.')
    parser.add_argument('--skill-dir', default=str(Path(__file__).resolve().parent), help='Directory containing extract-structure.mjs and merge-batch-graphs.py.')
    parser.add_argument('--model', default=os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro'))
    parser.add_argument('--api-url', default=os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/v1/chat/completions'))
    parser.add_argument('--workers', type=int, default=int(os.environ.get('UA_DIRECT_WORKERS', '4')))
    parser.add_argument('--limit', type=int, default=int(os.environ.get('UA_DIRECT_LIMIT', '0') or 0), help='Optional number of batches to process for smoke tests.')
    parser.add_argument('--content-chars', type=int, default=int(os.environ.get('UA_DIRECT_CONTENT_CHARS', '24000')))
    parser.add_argument('--max-file-chars', type=int, default=int(os.environ.get('UA_DIRECT_MAX_FILE_CHARS', '2500')))
    parser.add_argument('--env-file', action='append', default=[], help='Optional local env file containing DEEPSEEK_API_KEY; never commit this file.')
    parser.add_argument('--no-resume', action='store_true', help='Delete existing intermediate batch-*.json files before running.')
    return parser.parse_args()


def configure(args: argparse.Namespace) -> None:
    global PROJECT_ROOT, OUT, INTER, TMP, SKILL_DIR, EXTRACT, MERGE
    global MODEL, API_URL, MAX_WORKERS, BATCH_LIMIT, RESUME, CONTENT_CHAR_BUDGET, MAX_PER_FILE_CHARS, API_KEY
    PROJECT_ROOT = Path(args.project_root).expanduser().resolve()
    OUT = PROJECT_ROOT / '.understand-anything'
    INTER = OUT / 'intermediate'
    TMP = OUT / 'tmp'
    SKILL_DIR = Path(args.skill_dir).expanduser().resolve()
    EXTRACT = SKILL_DIR / 'extract-structure.mjs'
    MERGE = SKILL_DIR / 'merge-batch-graphs.py'
    MODEL = args.model
    API_URL = args.api_url
    MAX_WORKERS = args.workers
    BATCH_LIMIT = args.limit or None
    RESUME = not args.no_resume
    CONTENT_CHAR_BUDGET = args.content_chars
    MAX_PER_FILE_CHARS = args.max_file_chars
    API_KEY = get_api_key([Path(p).expanduser() for p in args.env_file])
    STATS['model'] = MODEL
    STATS['workers'] = MAX_WORKERS


def main() -> None:
    args = parse_args()
    configure(args)
    if not EXTRACT.exists() or not MERGE.exists():
        raise RuntimeError(f'Missing skill scripts: {EXTRACT} {MERGE}')
    INTER.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    scan_path=INTER/'scan-result.json'
    batches_path=INTER/'batches.json'
    if not scan_path.exists() or not batches_path.exists():
        raise RuntimeError('Missing clean scan-result.json or batches.json. Run scan/batch phase first.')
    scan=json.loads(scan_path.read_text(encoding='utf-8'))
    batches_data=json.loads(batches_path.read_text(encoding='utf-8'))
    batches=batches_data.get('batches') or []
    if BATCH_LIMIT:
        batches=batches[:BATCH_LIMIT]
    # Clean non-final leftover batch artifacts from aborted interactive run when not resuming.
    if not RESUME:
        for p in INTER.glob('batch-*.json'):
            p.unlink()
    print(f'Starting direct DeepSeek Understand run: {len(batches)} batches, workers={MAX_WORKERS}, model={MODEL}', flush=True)
    write_report({'total_batches':len(batches),'total_files':batches_data.get('totalFiles')})
    results=[]
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs={ex.submit(process_batch,b,scan): int(b['batchIndex']) for b in batches}
        for fut in cf.as_completed(futs):
            idx=futs[fut]
            try:
                res=fut.result()
                results.append(res)
                print(f"batch {idx} {res['status']} nodes={res['nodes']} edges={res['edges']} retries={res.get('retries',0)}", flush=True)
            except Exception as e:
                with LOCK:
                    STATS['failed'] += 1
                    STATS['batches'][str(idx)]={'status':'error','error':str(e)}
                    write_report()
                print(f"batch {idx} ERROR {e}", file=sys.stderr, flush=True)
    write_report({'phase':'merge'})
    assembled, merge_stderr=run_merge()
    final=assemble_final(scan, assembled)
    issues=validate_graph(final)
    final_path=OUT/'knowledge-graph.json'
    final_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding='utf-8')
    meta={'gitCommitHash':git_commit(),'analyzedAt':final['project']['analyzedAt'],'model':MODEL,'directRunner':True}
    (OUT/'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    report_extra={'phase':'done','finished_at':dt.datetime.now(dt.timezone.utc).isoformat(),'nodes':len(final['nodes']),'edges':len(final['edges']),'layers':len(final['layers']),'tour':len(final['tour']),'validation_issue_count':len(issues),'validation_issues_sample':issues[:100],'merge_stderr_tail':merge_stderr[-4000:]}
    write_report(report_extra)
    print(json.dumps({'status':'done','nodes':len(final['nodes']),'edges':len(final['edges']),'layers':len(final['layers']),'tour':len(final['tour']),'report':str(OUT/'deepseek-direct-run-report.json'),'graph':str(final_path),'fallback_batches':STATS['fallback'],'failed_batches':STATS['failed'],'retried':STATS['retried']}, ensure_ascii=False, indent=2), flush=True)

if __name__ == '__main__':
    main()
