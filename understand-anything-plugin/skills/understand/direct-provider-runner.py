#!/usr/bin/env python3
"""Direct provider runner for Understand-Anything batch artifacts.

This is a non-interactive runner for large codebases where an agentic per-batch
Claude/Hermes loop is too slow or too conversational. It expects the normal
Understand Anything scan/batch phase to have already produced:

    <project>/.understand-anything/intermediate/scan-result.json
    <project>/.understand-anything/intermediate/batches.json

It then runs the deterministic structure extractor, sends compact JSON-mode
requests to an approved provider, writes batch-<n>.json files compatible with
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
PROVIDER = os.environ.get('UA_DIRECT_PROVIDER', 'deepseek')
MODEL = os.environ.get('UA_DIRECT_MODEL') or os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')
API_URL = os.environ.get('UA_DIRECT_API_URL') or os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/v1/chat/completions')
MAX_WORKERS = int(os.environ.get('UA_DIRECT_WORKERS', '4'))
BATCH_LIMIT = int(os.environ.get('UA_DIRECT_LIMIT', '0')) or None
RESUME = os.environ.get('UA_DIRECT_RESUME', '1') != '0'
CONTENT_CHAR_BUDGET = int(os.environ.get('UA_DIRECT_CONTENT_CHARS', '24000'))
MAX_PER_FILE_CHARS = int(os.environ.get('UA_DIRECT_MAX_FILE_CHARS', '2500'))
CODEX_TIMEOUT = int(os.environ.get('UA_CODEX_TIMEOUT', '1800'))
API_KEY = ''
LOCK = threading.Lock()
STATS: dict[str, Any] = {
    'phase': 'init',
    'provider': PROVIDER,
    'model': MODEL,
    'workers': MAX_WORKERS,
    'completed': 0,
    'failed': 0,
    'fallback': 0,
    'retried': 0,
    'api_calls': 0,
    'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
    'batches': {},
}

VALID_NODE_TYPES = {'file','function','class','module','concept','config','document','service','table','endpoint','pipeline','schema','resource','domain','flow','step','article','entity','topic','claim','source'}
VALID_EDGE_TYPES = {'imports','exports','contains','inherits','implements','calls','subscribes','publishes','middleware','reads_from','writes_to','transforms','validates','depends_on','tested_by','configures','related','similar_to','deploys','serves','provisions','triggers','migrates','documents','routes','defines_schema','contains_flow','flow_step','cross_domain','cites','contradicts','builds_on','exemplifies','categorized_under','authored_by'}
VALID_COMPLEXITY = {'simple','moderate','complex'}
FILE_LEVEL_TYPES = {'file','config','document','service','pipeline','table','schema','resource','endpoint'}


def load_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE env files without exporting or logging secrets."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def get_api_key(env_files: list[Path]) -> str:
    """Resolve an OpenAI-compatible HTTP API key from environment first, then explicit files."""
    key = os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('OPENAI_API_KEY') or ''
    if key:
        return key
    for path in env_files:
        values = load_env_file(path)
        key = values.get('DEEPSEEK_API_KEY') or values.get('OPENAI_API_KEY') or ''
        if key:
            return key
    raise RuntimeError('No DEEPSEEK_API_KEY/OPENAI_API_KEY is set and no explicit --env-file provided one')


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


def merge_structural_nodes(result: dict[str, list[dict[str, Any]]], batch: dict[str, Any], structure: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Guarantee deterministic function/class/contains/import surfaces survive LLM compression.

    The LLM owns semantic summaries and optional extra edges, but the scanner already
    knows concrete symbols and line ranges. This pass adds missing structural nodes
    rather than letting a compact prompt collapse the graph back to file-only output.
    """
    structural = deterministic_fallback(batch, structure)
    nodes = list(result.get('nodes') or [])
    edges = list(result.get('edges') or [])
    seen_nodes = {n.get('id') for n in nodes if isinstance(n, dict)}
    for node in structural.get('nodes', []):
        nid = node.get('id')
        if nid and nid not in seen_nodes:
            nodes.append(node)
            seen_nodes.add(nid)
    seen_edges = {(e.get('source'), e.get('target'), e.get('type')) for e in edges if isinstance(e, dict)}
    for edge in structural.get('edges', []):
        key = (edge.get('source'), edge.get('target'), edge.get('type'))
        if key[0] in seen_nodes and key[1] in seen_nodes and key not in seen_edges:
            edges.append(edge)
            seen_edges.add(key)
    return {'nodes': nodes, 'edges': edges}


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
    raise RuntimeError(last_err or 'HTTP chat-completions request failed')


def codex_output_schema_path() -> Path:
    schema = TMP / 'ua-direct-codex-output-schema.json'
    # Always rewrite: stale schema files from previous runner versions caused
    # Codex/OpenAI structured-output 400s that silently downgraded graph quality.
    node_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['id', 'type', 'name', 'filePath', 'lineRange', 'summary', 'tags', 'complexity', 'languageNotes', 'description'],
        'properties': {
            'id': {'type': 'string'},
            'type': {'type': 'string'},
            'name': {'type': 'string'},
            'filePath': {'type': ['string', 'null']},
            'lineRange': {
                'anyOf': [
                    {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 2, 'maxItems': 2},
                    {'type': 'null'},
                ]
            },
            'summary': {'type': 'string'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'complexity': {'type': 'string'},
            'languageNotes': {'type': ['string', 'null']},
            'description': {'type': ['string', 'null']},
        },
    }
    edge_schema = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['source', 'target', 'type', 'direction', 'weight', 'description'],
        'properties': {
            'source': {'type': 'string'},
            'target': {'type': 'string'},
            'type': {'type': 'string'},
            'direction': {'type': 'string'},
            'weight': {'type': 'number'},
            'description': {'type': ['string', 'null']},
        },
    }
    schema.write_text(json.dumps({
        'type': 'object',
        'additionalProperties': False,
        'required': ['nodes', 'edges'],
        'properties': {
            'nodes': {'type': 'array', 'items': node_schema},
            'edges': {'type': 'array', 'items': edge_schema},
        },
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return schema


def write_codex_schema(schema_obj: dict[str, Any], stem: str) -> Path:
    """Write a strict structured-output schema for Codex/OpenAI CLI calls."""
    path = TMP / f'ua-direct-codex-{stem}-schema.json'
    path.write_text(json.dumps(schema_obj, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def call_codex_cli(messages: list[dict[str, str]], max_tokens: int = 12000, schema_path: Path | None = None) -> tuple[str, dict[str, int]]:
    """Call Codex CLI using its configured OAuth session and capture the final JSON answer."""
    del max_tokens
    prompt = '\n\n'.join(f"<{m.get('role','user')}>\n{m.get('content','')}" for m in messages)
    stamp = f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
    prompt_path = TMP / f'ua-direct-codex-prompt-{stamp}.txt'
    output_path = TMP / f'ua-direct-codex-output-{stamp}.json'
    prompt_path.write_text(prompt, encoding='utf-8')
    cmd = [
        'codex', 'exec',
        '--cd', str(PROJECT_ROOT),
        '--sandbox', 'read-only',
        '--skip-git-repo-check',
        '--ephemeral',
        '--model', MODEL,
        '--output-schema', str(schema_path or codex_output_schema_path()),
        '--output-last-message', str(output_path),
        '-',
    ]
    proc = subprocess.run(cmd, input=prompt, cwd=str(PROJECT_ROOT), text=True, capture_output=True, timeout=CODEX_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f'codex exec failed {proc.returncode}: {proc.stderr[-1200:] or proc.stdout[-1200:]}')
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f'codex exec produced no output file; stdout tail: {proc.stdout[-1200:]} stderr tail: {proc.stderr[-1200:]}')
    with LOCK:
        STATS['api_calls'] += 1
    return output_path.read_text(encoding='utf-8'), {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}


def call_model(messages: list[dict[str, str]], max_tokens: int = 12000, schema_path: Path | None = None) -> tuple[str, dict[str, int]]:
    if PROVIDER == 'codex-cli':
        return call_codex_cli(messages, max_tokens=max_tokens, schema_path=schema_path)
    return call_deepseek(messages, max_tokens=max_tokens)



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
- name, filePath, optional lineRange [start,end], summary 1 sentence, tags 3-5 lowercase hyphenated strings, complexity simple|moderate|complex, optional languageNotes for teaching-worthy syntax/patterns.

GraphEdge fields:
- source, target, type, direction, weight, optional description.
- allowed type examples: imports, contains, calls, depends_on, configures, documents, deploys, serves, triggers, defines_schema, routes, related, tested_by.
- direction must be forward|backward|bidirectional. weight 0..1.

    Rules:
1. Emit one file-level node for every file listed.
2. Emit function/class nodes for significant deterministic symbols in code files: exported symbols, functions with 10+ lines, and classes that are exported or span 20+ lines. Every emitted function/class node MUST include filePath and lineRange.
3. Emit a contains edge from each file-level node to every function/class node it contains.
4. Use batchImportData for imports edges; emit one file-to-file imports edge for every resolved project-internal import.
5. Add calls, inherits, implements, exports, configures, documents, tested_by, deploys, routes, defines_schema, and related edges when clearly supported by deterministic structure, imports, file names, or content.
6. Do not invent file paths. You may reference file-level neighbor nodes from neighborMap/imports, but do not reference function/class neighbor nodes unless the symbol is explicitly present in neighborMap.
7. Soft caps: edges <= 120 plus required contains/imports edges. Prefer complete structural surfaces over artificially tiny output.
8. Keep summaries concise but useful: file summaries <= 24 words; function/class summaries <= 18 words. Tags: 3-5 short strings. Add languageNotes when a file/function/class demonstrates a notable language, framework, schema, config, or architecture pattern.
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
        content, usage=call_model(messages, max_tokens=14000)
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
            repaired, usage2=call_model(repair_messages, max_tokens=14000)
            (TMP/f'ua-direct-raw-batch-{idx}-repair.txt').write_text(repaired, encoding='utf-8', errors='replace')
            for k,v in usage2.items(): usage[k]=usage.get(k,0)+v
            parsed=extract_json(repaired)
            result=normalize_batch_output(parsed, batch)
    except Exception as e:
        fallback_reason=str(e)
        result=deterministic_fallback(batch, structure)
        usage={}
    result=merge_structural_nodes(result, batch, structure)
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
    for name in ('direct-run-report.json', 'deepseek-direct-run-report.json'):
        (OUT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def run_merge() -> tuple[dict[str,Any], str]:
    proc=subprocess.run([sys.executable, str(MERGE), str(PROJECT_ROOT)], cwd=str(PROJECT_ROOT), text=True, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f'merge failed {proc.returncode}\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}')
    assembled=INTER/'assembled-graph.json'
    return json.loads(assembled.read_text(encoding='utf-8')), proc.stderr



def model_available() -> bool:
    """Return True when a semantic post-pass can actually call a provider."""
    return PROVIDER == 'codex-cli' or bool(API_KEY)


def kebab(value: str) -> str:
    out = re.sub(r'[^a-z0-9]+', '-', (value or '').lower()).strip('-')
    return out or 'misc'


def file_level_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in nodes if isinstance(n, dict) and n.get('type') in FILE_LEVEL_TYPES and n.get('id')]


def path_for_node(n: dict[str, Any]) -> str:
    return str(n.get('filePath') or str(n.get('id', '')).split(':', 1)[-1])


def group_for_path(path: str) -> str:
    p = (path or '').strip('/')
    if not p:
        return 'root'
    parts = p.split('/')
    if len(parts) == 1:
        name = parts[0].lower()
        if re.search(r'(test|spec)', name): return 'tests'
        if name.endswith(('.md','.rst')): return 'documentation'
        if re.search(r'(config|settings|toml|yaml|yml|json|lock|ini)$', name): return 'configuration'
        return 'root'
    if parts[0] in {'src','lib','app','packages'} and len(parts) > 2:
        return parts[1]
    return parts[0]


def pattern_label(group: str, paths: list[str]) -> str:
    g = group.lower()
    joined = ' '.join(paths).lower()
    if g in {'routes','api','controllers','endpoints','handlers','routers'}: return 'api'
    if g in {'services','core','domain','logic','engine','analyzer','graph'}: return 'core'
    if g in {'models','db','data','persistence','repository','entities','schema','schemas'}: return 'data'
    if g in {'components','views','pages','ui','layouts','screens'}: return 'ui'
    if g in {'utils','helpers','common','shared','tools','scripts'}: return 'utility'
    if g in {'config','configuration','constants','env','settings'}: return 'configuration'
    if g in {'tests','test','spec','specs','__tests__'} or re.search(r'(\.test\.|\.spec\.|test_)', joined): return 'tests'
    if g in {'docs','documentation','wiki'} or any(p.endswith(('.md','.rst')) for p in paths): return 'documentation'
    if g in {'deploy','deployment','infra','infrastructure','.github','.gitlab','k8s','terraform','docker'}: return 'infrastructure'
    if g in {'types','interfaces','contracts','dtos'}: return 'types'
    if g in {'cli','cmd','commands','bin'}: return 'entrypoints'
    return g or 'core'


def analyze_architecture(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    fns = file_level_nodes(nodes)
    by_group: dict[str, list[str]] = {}
    id_to_group: dict[str, str] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for n in fns:
        nid = str(n['id'])
        path = path_for_node(n)
        group = group_for_path(path)
        by_group.setdefault(group, []).append(nid)
        id_to_group[nid] = group
        summaries[nid] = {'name': n.get('name'), 'type': n.get('type'), 'filePath': path, 'summary': n.get('summary'), 'tags': n.get('tags', [])}
    file_edges = [e for e in edges if isinstance(e, dict) and e.get('source') in id_to_group and e.get('target') in id_to_group]
    fan_in = {nid: 0 for nid in id_to_group}
    fan_out = {nid: 0 for nid in id_to_group}
    group_edges: dict[tuple[str, str], int] = {}
    edge_type_counts: dict[str, int] = {}
    for e in file_edges:
        src = str(e.get('source')); tgt = str(e.get('target'))
        fan_out[src] = fan_out.get(src, 0) + 1
        fan_in[tgt] = fan_in.get(tgt, 0) + 1
        gs, gt = id_to_group[src], id_to_group[tgt]
        group_edges[(gs, gt)] = group_edges.get((gs, gt), 0) + 1
        typ = str(e.get('type') or 'related')
        edge_type_counts[typ] = edge_type_counts.get(typ, 0) + 1
    patterns = {g: pattern_label(g, [path_for_node(next(n for n in fns if n['id'] == nid)) for nid in ids]) for g, ids in by_group.items()}
    return {
        'directoryGroups': by_group,
        'patternMatches': patterns,
        'interGroupEdges': [{'from': a, 'to': b, 'count': c} for (a,b),c in sorted(group_edges.items(), key=lambda kv: -kv[1])[:80]],
        'edgeTypeCounts': edge_type_counts,
        'fileFanIn': dict(sorted(fan_in.items(), key=lambda kv: -kv[1])[:30]),
        'fileFanOut': dict(sorted(fan_out.items(), key=lambda kv: -kv[1])[:30]),
        'nodeSummaryIndex': summaries,
        'fileStats': {'totalFileNodes': len(fns), 'filesPerGroup': {k: len(v) for k,v in by_group.items()}},
    }


def deterministic_layers(nodes: list[dict[str,Any]], edges: list[dict[str,Any]] | None = None) -> list[dict[str,Any]]:
    analysis = analyze_architecture(nodes, edges or [])
    groups: dict[str, dict[str, Any]] = {}
    label_names = {
        'api': ('API Surface', 'Request handlers, routes, endpoints, and external interface code.'),
        'core': ('Core Logic', 'Primary implementation modules and internal orchestration logic.'),
        'data': ('Data and Schemas', 'Data models, schemas, migrations, and persistence surfaces.'),
        'ui': ('User Interface', 'Components, screens, and presentation-layer code.'),
        'utility': ('Utilities and Tools', 'Shared helpers, scripts, and reusable support code.'),
        'configuration': ('Configuration', 'Project manifests, build settings, and runtime configuration.'),
        'tests': ('Tests', 'Automated tests and fixtures that validate behavior.'),
        'documentation': ('Documentation', 'Project documentation and explanatory guides.'),
        'infrastructure': ('Infrastructure', 'Deployment, CI/CD, container, and environment definitions.'),
        'types': ('Types and Contracts', 'Shared type definitions, interfaces, and contracts.'),
        'entrypoints': ('Entrypoints', 'CLI, command, and application entrypoint surfaces.'),
    }
    for group, ids in analysis['directoryGroups'].items():
        label = analysis['patternMatches'].get(group, group)
        name, desc = label_names.get(label, (group.replace('-', ' ').replace('_', ' ').title(), f'Files grouped under `{group}` based on repository structure and dependencies.'))
        lid = f'layer:{kebab(label)}'
        bucket = groups.setdefault(lid, {'id': lid, 'name': name, 'description': desc, 'nodeIds': []})
        bucket['nodeIds'].extend(ids)
    layers = list(groups.values())
    for layer in layers:
        layer['nodeIds'] = sorted(set(layer['nodeIds']))
    return sorted(layers, key=lambda l: (0 if 'documentation' in l['id'] else 1 if 'entry' in l['id'] or 'api' in l['id'] else 2, l['id']))


def layer_schema() -> dict[str, Any]:
    layer_item = {
        'type': 'object', 'additionalProperties': False,
        'required': ['id','name','description','nodeIds'],
        'properties': {
            'id': {'type': 'string'}, 'name': {'type': 'string'}, 'description': {'type': 'string'},
            'nodeIds': {'type': 'array', 'items': {'type': 'string'}},
        },
    }
    return {'type': 'object', 'additionalProperties': False, 'required': ['layers'], 'properties': {'layers': {'type': 'array', 'items': layer_item}}}


def normalize_layers(data: Any, nodes: list[dict[str,Any]], fallback: list[dict[str,Any]]) -> list[dict[str,Any]]:
    raw = data.get('layers') if isinstance(data, dict) else data
    valid_ids = {n['id'] for n in file_level_nodes(nodes)}
    if not isinstance(raw, list):
        return fallback
    seen: set[str] = set(); layers=[]
    for item in raw:
        if not isinstance(item, dict): continue
        ids=[]
        for nid in item.get('nodeIds') or []:
            if nid in valid_ids and nid not in seen:
                ids.append(nid); seen.add(nid)
        if not ids: continue
        lid = str(item.get('id') or f"layer:{kebab(str(item.get('name') or 'group'))}")
        if not lid.startswith('layer:'): lid = 'layer:' + kebab(lid)
        layers.append({'id': lid[:96], 'name': str(item.get('name') or lid.split(':',1)[-1].replace('-', ' ').title())[:80], 'description': str(item.get('description') or 'Project layer.')[:300], 'nodeIds': ids})
    missing = sorted(valid_ids - seen)
    if missing:
        fb_by_id = {l['id']: l for l in fallback}
        for l in fallback:
            ids = [nid for nid in l['nodeIds'] if nid in missing]
            if ids:
                existing = next((x for x in layers if x['id'] == l['id']), None)
                if existing: existing['nodeIds'].extend(ids)
                else: layers.append({**l, 'nodeIds': ids})
                missing -= set(ids)
        if missing:
            layers.append({'id':'layer:unassigned-support','name':'Unassigned Support','description':'Files not assigned by the semantic layer pass.', 'nodeIds': sorted(missing)})
    return layers or fallback


def make_layers(nodes: list[dict[str,Any]], edges: list[dict[str,Any]] | None = None, scan: dict[str,Any] | None = None) -> list[dict[str,Any]]:
    edges = edges or []
    fallback = deterministic_layers(nodes, edges)
    if not model_available():
        return fallback
    analysis = analyze_architecture(nodes, edges)
    project_name = (scan or {}).get('projectName') or (scan or {}).get('name') or PROJECT_ROOT.name
    messages = [
        {'role':'system','content':'You are the Understand Anything architecture-analyzer. Return ONLY valid JSON. Identify semantic architecture layers from structural graph facts; do not invent node IDs.'},
        {'role':'user','content':f"""
Project: {project_name}
Task: Identify 3-10 architecture layers and assign every file-level node to exactly one layer.
Return JSON object: {{"layers":[{{"id":"layer:kebab","name":"Name","description":"project-specific sentence","nodeIds":["..."]}}]}}
Use these deterministic structural facts; do not re-read source files and do not invent IDs.

Structural analysis:
{json_dumps(analysis, 70000)}

Deterministic fallback layers for reference:
{json_dumps(fallback, 30000)}
"""}
    ]
    try:
        schema_path = write_codex_schema(layer_schema(), 'layers') if PROVIDER == 'codex-cli' else None
        content, usage = call_model(messages, max_tokens=8000, schema_path=schema_path)
        for k,v in usage.items(): STATS['usage'][k] = STATS['usage'].get(k,0)+v
        parsed = extract_json(content)
        layers = normalize_layers(parsed, nodes, fallback)
        (INTER/'layers.json').write_text(json.dumps(layers, ensure_ascii=False, indent=2), encoding='utf-8')
        return layers
    except Exception as e:
        (TMP/'ua-direct-layers-error.txt').write_text(str(e), encoding='utf-8', errors='replace')
        return fallback


def analyze_tour(nodes: list[dict[str,Any]], layers: list[dict[str,Any]], edges: list[dict[str,Any]]) -> dict[str, Any]:
    ids = {n['id'] for n in nodes if isinstance(n, dict)}
    node_by_id = {n['id']: n for n in nodes if isinstance(n, dict) and n.get('id')}
    fan_in = {nid: 0 for nid in ids}; fan_out = {nid: 0 for nid in ids}; adj: dict[str, list[str]] = {}
    for e in edges:
        if not isinstance(e, dict): continue
        src, tgt = e.get('source'), e.get('target')
        if src in ids and tgt in ids:
            fan_out[src] += 1; fan_in[tgt] += 1
            if e.get('type') in {'imports','calls','contains','depends_on','configures','documents','tested_by','deploys','routes','defines_schema'}:
                adj.setdefault(src, []).append(tgt)
    def score_entry(n: dict[str,Any]) -> int:
        path = path_for_node(n).lower(); name = str(n.get('name') or '').lower(); typ = n.get('type')
        score = 0
        if typ == 'document' and path in {'readme.md','readme.rst'}: score += 8
        if typ == 'config' and name in {'package.json','pyproject.toml','cargo.toml','go.mod'}: score += 3
        if typ == 'file' and re.search(r'(^|/)(__main__|main|index|app|server|cli|run_agent)\.(py|ts|tsx|js|jsx|go|rs)$', path): score += 6
        if fan_out.get(n['id'],0) >= 2: score += 2
        if fan_in.get(n['id'],0) == 0: score += 1
        return score
    ranked_entries = sorted([{'id': n['id'], 'score': score_entry(n), 'name': n.get('name'), 'summary': n.get('summary')} for n in file_level_nodes(nodes)], key=lambda x: (-x['score'], str(x['id'])))[:10]
    start = next((x['id'] for x in ranked_entries if x['score'] > 0 and not str(x['id']).startswith('document:')), ranked_entries[0]['id'] if ranked_entries else None)
    seen=set(); order=[]; depth={}
    if start:
        q=[start]; depth[start]=0; seen.add(start)
        while q and len(order) < 80:
            cur=q.pop(0); order.append(cur)
            for nxt in adj.get(cur, [])[:12]:
                if nxt not in seen:
                    seen.add(nxt); depth[nxt]=depth[cur]+1; q.append(nxt)
    noncode = {typ: [{'id': n['id'], 'name': n.get('name'), 'summary': n.get('summary')} for n in file_level_nodes(nodes) if n.get('type') == typ][:20] for typ in ['document','config','service','pipeline','schema','table','resource','endpoint']}
    important = sorted([{'id': nid, 'fanIn': fan_in[nid], 'fanOut': fan_out[nid], 'name': node_by_id.get(nid,{}).get('name'), 'summary': node_by_id.get(nid,{}).get('summary')} for nid in ids], key=lambda x: (-(x['fanIn']+x['fanOut']), str(x['id'])))[:40]
    by_depth: dict[str, list[str]] = {}
    for nid,d in depth.items(): by_depth.setdefault(str(d), []).append(nid)
    return {'entryPointCandidates': ranked_entries, 'bfsTraversal': {'startNode': start, 'order': order, 'byDepth': by_depth}, 'nonCodeFiles': noncode, 'importantNodes': important, 'layers': layers, 'nodeSummaryIndex': {nid: {'name': n.get('name'), 'type': n.get('type'), 'summary': n.get('summary'), 'filePath': n.get('filePath')} for nid,n in list(node_by_id.items())[:250]}, 'totalNodes': len(nodes), 'totalEdges': len(edges)}


def deterministic_tour(nodes: list[dict[str,Any]], layers: list[dict[str,Any]], edges: list[dict[str,Any]]) -> list[dict[str,Any]]:
    analysis = analyze_tour(nodes, layers, edges)
    steps=[]; node_ids={n['id'] for n in nodes if isinstance(n, dict)}
    def add(title, desc, ids, lesson=None):
        present=[]
        for nid in ids:
            if nid in node_ids and nid not in present: present.append(nid)
        if present:
            step={'order':len(steps)+1,'title':title,'description':desc,'nodeIds':present[:5]}
            if lesson: step['languageLesson']=lesson
            steps.append(step)
    readme = next((x['id'] for x in analysis['entryPointCandidates'] if str(x['id']).startswith('document:README')), None)
    if readme: add('Project Orientation', 'Start with the README to establish the project purpose, expected workflow, and vocabulary before reading implementation details.', [readme])
    elif analysis['entryPointCandidates']: add('Project Entry', 'Start with the highest-signal entry point so the rest of the graph has execution context.', [analysis['entryPointCandidates'][0]['id']])
    for layer in layers[:8]:
        title = layer.get('name') or 'Architecture Layer'
        desc = layer.get('description') or 'This layer groups related files in the codebase.'
        add(title, desc, layer.get('nodeIds', [])[:4])
    for typ,title,lesson in [('config','Configuration Surface','Config files encode runtime/build assumptions; trace them before debugging behavior.'),('service','Deployment Surface','Infrastructure files explain how code becomes a running service.'),('pipeline','Validation Pipeline','CI/CD YAML defines automated checks and release gates.')]:
        ids=[x['id'] for x in analysis['nonCodeFiles'].get(typ, [])]
        add(title, f'Review the {title.lower()} to understand non-code constraints around the implementation.', ids, lesson)
    for item in analysis['importantNodes'][:8]:
        if len(steps) >= 8: break
        add('High-Connectivity Node', 'This node has many graph connections, so it is useful as a local deep-dive anchor.', [item['id']])
    # renumber and cap; keep at least all possible if small
    for i, step in enumerate(steps[:12], 1): step['order'] = i
    return steps[:12]


def tour_schema() -> dict[str, Any]:
    step_item = {
        'type': 'object', 'additionalProperties': False,
        'required': ['order','title','description','nodeIds','languageLesson'],
        'properties': {
            'order': {'type': 'integer'}, 'title': {'type': 'string'}, 'description': {'type': 'string'},
            'nodeIds': {'type': 'array', 'items': {'type': 'string'}},
            'languageLesson': {'type': ['string','null']},
        },
    }
    return {'type': 'object', 'additionalProperties': False, 'required': ['tour'], 'properties': {'tour': {'type': 'array', 'items': step_item}}}


def normalize_tour(data: Any, nodes: list[dict[str,Any]], fallback: list[dict[str,Any]]) -> list[dict[str,Any]]:
    raw = data.get('tour') if isinstance(data, dict) else data
    ids={n['id'] for n in nodes if isinstance(n, dict) and n.get('id')}
    if not isinstance(raw, list): return fallback
    out=[]
    for item in raw:
        if not isinstance(item, dict): continue
        nids=[]
        for nid in item.get('nodeIds') or []:
            if nid in ids and nid not in nids: nids.append(nid)
        if not nids: continue
        step={'order': len(out)+1, 'title': str(item.get('title') or f'Step {len(out)+1}')[:100], 'description': str(item.get('description') or '')[:1200], 'nodeIds': nids[:5]}
        lesson = item.get('languageLesson')
        if isinstance(lesson, str) and lesson.strip(): step['languageLesson'] = lesson.strip()[:700]
        out.append(step)
        if len(out) >= 15: break
    if len(out) < 3 and len(fallback) > len(out):
        seen = {tuple(s['nodeIds']) for s in out}
        for step in fallback:
            if tuple(step['nodeIds']) not in seen:
                out.append({**step, 'order': len(out)+1})
            if len(out) >= min(5, len(fallback)): break
    for i, step in enumerate(out, 1): step['order']=i
    return out or fallback


def make_tour(nodes: list[dict[str,Any]], layers: list[dict[str,Any]], edges: list[dict[str,Any]], scan: dict[str,Any] | None = None) -> list[dict[str,Any]]:
    fallback = deterministic_tour(nodes, layers, edges)
    if not model_available():
        return fallback
    analysis = analyze_tour(nodes, layers, edges)
    project_name = (scan or {}).get('projectName') or (scan or {}).get('name') or PROJECT_ROOT.name
    messages = [
        {'role':'system','content':'You are the Understand Anything tour-builder: an expert technical educator. Return ONLY valid JSON. Build a pedagogical guided tour from graph facts; do not invent node IDs.'},
        {'role':'user','content':f"""
Project: {project_name}
Task: Produce a 5-15 step guided learning tour. It should start from project orientation, move through entrypoints/core layers, include tests/config/infra when present, and include languageLesson when genuinely useful.
Return JSON object: {{"tour":[{{"order":1,"title":"...","description":"2-4 sentences","nodeIds":["real-node-id"],"languageLesson":null}}]}}
Every nodeIds entry must exist in nodeSummaryIndex/layers. Prefer file-level nodes, but include high-value function/class nodes for deep implementation concepts when useful.

Tour topology analysis:
{json_dumps(analysis, 90000)}

Deterministic fallback tour for reference:
{json_dumps(fallback, 30000)}
"""}
    ]
    try:
        schema_path = write_codex_schema(tour_schema(), 'tour') if PROVIDER == 'codex-cli' else None
        content, usage = call_model(messages, max_tokens=10000, schema_path=schema_path)
        for k,v in usage.items(): STATS['usage'][k] = STATS['usage'].get(k,0)+v
        parsed=extract_json(content)
        tour=normalize_tour(parsed, nodes, fallback)
        (INTER/'tour.json').write_text(json.dumps(tour, ensure_ascii=False, indent=2), encoding='utf-8')
        return tour
    except Exception as e:
        (TMP/'ua-direct-tour-error.txt').write_text(str(e), encoding='utf-8', errors='replace')
        return fallback


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
    layers=make_layers(nodes, edges, scan)
    tour=make_tour(nodes, layers, edges, scan)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Run a direct provider JSON-mode semantic pass over precomputed Understand Anything batches.'
    )
    parser.add_argument('project_root', nargs='?', default=os.getcwd(), help='Repository/project root containing .understand-anything/intermediate artifacts.')
    parser.add_argument('--skill-dir', default=str(Path(__file__).resolve().parent), help='Directory containing extract-structure.mjs and merge-batch-graphs.py.')
    parser.add_argument('--provider', choices=['deepseek', 'openai-compatible', 'codex-cli'], default=os.environ.get('UA_DIRECT_PROVIDER', 'deepseek'), help='Model execution backend. codex-cli uses the local Codex OAuth session, not an API key.')
    parser.add_argument('--model', default=None, help='Model name. Defaults to deepseek-v4-pro for DeepSeek and gpt-5.5 for codex-cli.')
    parser.add_argument('--api-url', default=None, help='OpenAI-compatible chat completions endpoint for HTTP providers.')
    parser.add_argument('--workers', type=int, default=int(os.environ.get('UA_DIRECT_WORKERS', '4')))
    parser.add_argument('--limit', type=int, default=int(os.environ.get('UA_DIRECT_LIMIT', '0') or 0), help='Optional number of batches to process for smoke tests.')
    parser.add_argument('--content-chars', type=int, default=int(os.environ.get('UA_DIRECT_CONTENT_CHARS', '24000')))
    parser.add_argument('--max-file-chars', type=int, default=int(os.environ.get('UA_DIRECT_MAX_FILE_CHARS', '2500')))
    parser.add_argument('--codex-timeout', type=int, default=int(os.environ.get('UA_CODEX_TIMEOUT', '1800')), help='Timeout in seconds for each codex exec model call.')
    parser.add_argument('--env-file', action='append', default=[], help='Optional local env file containing DEEPSEEK_API_KEY or OPENAI_API_KEY; never commit this file.')
    parser.add_argument('--no-resume', action='store_true', help='Delete existing intermediate batch-*.json files before running.')
    return parser.parse_args(argv)


def configure(args: argparse.Namespace) -> None:
    global PROJECT_ROOT, OUT, INTER, TMP, SKILL_DIR, EXTRACT, MERGE
    global PROVIDER, MODEL, API_URL, MAX_WORKERS, BATCH_LIMIT, RESUME, CONTENT_CHAR_BUDGET, MAX_PER_FILE_CHARS, CODEX_TIMEOUT, API_KEY
    PROJECT_ROOT = Path(args.project_root).expanduser().resolve()
    OUT = PROJECT_ROOT / '.understand-anything'
    INTER = OUT / 'intermediate'
    TMP = OUT / 'tmp'
    SKILL_DIR = Path(args.skill_dir).expanduser().resolve()
    EXTRACT = SKILL_DIR / 'extract-structure.mjs'
    MERGE = SKILL_DIR / 'merge-batch-graphs.py'
    PROVIDER = args.provider
    if args.model:
        MODEL = args.model
    elif PROVIDER == 'codex-cli':
        MODEL = os.environ.get('UA_CODEX_MODEL', 'gpt-5.5')
    elif PROVIDER == 'openai-compatible':
        MODEL = os.environ.get('UA_DIRECT_MODEL') or os.environ.get('OPENAI_MODEL', 'gpt-5.5')
    else:
        MODEL = os.environ.get('UA_DIRECT_MODEL') or os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')
    if args.api_url:
        API_URL = args.api_url
    elif PROVIDER == 'openai-compatible':
        API_URL = os.environ.get('UA_DIRECT_API_URL') or os.environ.get('OPENAI_API_URL', 'https://api.openai.com/v1/chat/completions')
    else:
        API_URL = os.environ.get('UA_DIRECT_API_URL') or os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/v1/chat/completions')
    MAX_WORKERS = args.workers
    BATCH_LIMIT = args.limit or None
    RESUME = not args.no_resume
    CONTENT_CHAR_BUDGET = args.content_chars
    MAX_PER_FILE_CHARS = args.max_file_chars
    CODEX_TIMEOUT = args.codex_timeout
    API_KEY = '' if PROVIDER == 'codex-cli' else get_api_key([Path(p).expanduser() for p in args.env_file])
    STATS['provider'] = PROVIDER
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
    print(f'Starting direct Understand run: provider={PROVIDER}, {len(batches)} batches, workers={MAX_WORKERS}, model={MODEL}', flush=True)
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
    meta={'gitCommitHash':git_commit(),'analyzedAt':final['project']['analyzedAt'],'provider':PROVIDER,'model':MODEL,'directRunner':True}
    (OUT/'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    report_extra={'phase':'done','finished_at':dt.datetime.now(dt.timezone.utc).isoformat(),'nodes':len(final['nodes']),'edges':len(final['edges']),'layers':len(final['layers']),'tour':len(final['tour']),'validation_issue_count':len(issues),'validation_issues_sample':issues[:100],'merge_stderr_tail':merge_stderr[-4000:]}
    write_report(report_extra)
    print(json.dumps({'status':'done','nodes':len(final['nodes']),'edges':len(final['edges']),'layers':len(final['layers']),'tour':len(final['tour']),'report':str(OUT/'direct-run-report.json'),'legacy_report':str(OUT/'deepseek-direct-run-report.json'),'graph':str(final_path),'fallback_batches':STATS['fallback'],'failed_batches':STATS['failed'],'retried':STATS['retried']}, ensure_ascii=False, indent=2), flush=True)

if __name__ == '__main__':
    main()
