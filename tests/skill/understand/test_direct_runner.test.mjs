import { describe, it, expect } from "vitest";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RUNNER = resolve(__dirname, "../../../understand-anything-plugin/skills/understand/direct-provider-runner.py");

function runPython(source) {
  const result = spawnSync("python3", ["-c", source], {
    encoding: "utf-8",
    env: { ...process.env, UA_DIRECT_TEST_RUNNER: RUNNER },
  });
  if (result.status !== 0) {
    throw new Error(`python failed status=${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`);
  }
  return JSON.parse(result.stdout);
}

describe("direct Understand runner", () => {
  it("prompts for full graph surfaces when deterministic extraction found functions and classes", () => {
    const data = runPython(String.raw`
import importlib.util, json, os
spec = importlib.util.spec_from_file_location('direct_runner', os.environ['UA_DIRECT_TEST_RUNNER'])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
batch = {
  'batchIndex': 1,
  'files': [{'path':'src/service.py','language':'python','sizeLines':80,'fileCategory':'code'}],
  'batchImportData': {},
  'neighborMap': {},
}
scan = {'projectName':'sample','description':'sample project','languages':['python'],'frameworks':[]}
structure = {'results': [{
  'path':'src/service.py','language':'python','totalLines':80,
  'metrics': {'functionCount': 1, 'classCount': 1},
  'functions': [{'name':'handle_request','startLine':10,'endLine':35,'isExported':True}],
  'classes': [{'name':'Service','startLine':40,'endLine':75,'isExported':True}],
  'imports': [], 'exports': []
}]}
messages = mod.make_prompt(batch, scan, structure)
prompt = '\n'.join(m['content'] for m in messages)
print(json.dumps({'prompt': prompt}))
`);
    expect(data.prompt).not.toContain("Emit NO class nodes and NO function nodes");
    expect(data.prompt).toContain("function:path:name");
    expect(data.prompt).toContain("class:path:name");
    expect(data.prompt).toMatch(/lineRange/i);
    expect(data.prompt).toMatch(/contains/i);
  });

  it("supports selecting Codex OAuth execution without requiring a DeepSeek or OpenAI API key", () => {
    const data = runPython(String.raw`
import importlib.util, json, os
spec = importlib.util.spec_from_file_location('direct_runner', os.environ['UA_DIRECT_TEST_RUNNER'])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
args = mod.parse_args(['--provider', 'codex-cli', '--model', 'gpt-5.5']) if getattr(mod.parse_args, '__call__', None) else None
print(json.dumps({'provider': args.provider, 'model': args.model}))
`);
    expect(data).toEqual({ provider: "codex-cli", model: "gpt-5.5" });
  });

  it("emits a Codex structured-output schema accepted by OpenAI strict JSON schema rules", () => {
    const data = runPython(String.raw`
import importlib.util, json, os, tempfile, pathlib
spec = importlib.util.spec_from_file_location('direct_runner', os.environ['UA_DIRECT_TEST_RUNNER'])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory() as d:
    mod.TMP = pathlib.Path(d)
    stale = mod.TMP / 'ua-direct-codex-output-schema.json'
    stale.write_text('{"type":"object","properties":{"nodes":{"type":"array","items":{"type":"object"}},"edges":{"type":"array","items":{"type":"object"}}}}')
    schema = json.loads(mod.codex_output_schema_path().read_text())
print(json.dumps({
    'rootAdditionalProperties': schema.get('additionalProperties'),
    'nodeItemAdditionalProperties': schema['properties']['nodes']['items'].get('additionalProperties'),
    'edgeItemAdditionalProperties': schema['properties']['edges']['items'].get('additionalProperties'),
    'nodeRequired': schema['properties']['nodes']['items'].get('required'),
    'edgeRequired': schema['properties']['edges']['items'].get('required'),
    'lineRangeItems': schema['properties']['nodes']['items']['properties']['lineRange']['anyOf'][0].get('items'),
}))
`);
    expect(data.rootAdditionalProperties).toBe(false);
    expect(data.nodeItemAdditionalProperties).toBe(false);
    expect(data.edgeItemAdditionalProperties).toBe(false);
    expect(data.nodeRequired).toContain("id");
    expect(data.edgeRequired).toContain("source");
    expect(data.lineRangeItems).toEqual({ type: "integer" });
  });

  it("uses a model-assisted architecture post-pass instead of Hermes-specific hard-coded layers", () => {
    const data = runPython(String.raw`
import importlib.util, json, os, tempfile, pathlib
spec = importlib.util.spec_from_file_location('direct_runner', os.environ['UA_DIRECT_TEST_RUNNER'])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory() as d:
    mod.TMP = pathlib.Path(d); mod.INTER = pathlib.Path(d)
    mod.PROVIDER = 'codex-cli'; mod.MODEL = 'gpt-5.5'
    calls = []
    def fake_call(messages, max_tokens=12000, schema_path=None):
        calls.append({'prompt': messages[-1]['content'], 'schema': str(schema_path or '')})
        return json.dumps({'layers': [
            {'id':'layer:package-api','name':'Package API','description':'Public package exports and callers.', 'nodeIds':['file:src/__init__.py']},
            {'id':'layer:validation','name':'Validation','description':'Frontmatter and UID validation code.', 'nodeIds':['file:src/validators.py']},
            {'id':'layer:tests','name':'Tests','description':'Regression coverage for validation behavior.', 'nodeIds':['file:tests/test_validators.py']},
        ]}), {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
    mod.call_model = fake_call
    nodes = [
        {'id':'file:src/__init__.py','type':'file','name':'__init__.py','filePath':'src/__init__.py','summary':'exports package','tags':['python'],'complexity':'simple'},
        {'id':'file:src/validators.py','type':'file','name':'validators.py','filePath':'src/validators.py','summary':'validates markdown','tags':['python'],'complexity':'moderate'},
        {'id':'file:tests/test_validators.py','type':'file','name':'test_validators.py','filePath':'tests/test_validators.py','summary':'tests validators','tags':['test'],'complexity':'simple'},
    ]
    edges = [{'source':'file:tests/test_validators.py','target':'file:src/validators.py','type':'tested_by','direction':'forward','weight':0.8}]
    layers = mod.make_layers(nodes, edges, {'projectName':'atrium-core'})
print(json.dumps({'layers': layers, 'callCount': len(calls), 'prompt': calls[0]['prompt']}))
`);
    expect(data.callCount).toBe(1);
    expect(data.prompt).toContain("architecture layers");
    expect(data.layers.map((l) => l.id)).toEqual(["layer:package-api", "layer:validation", "layer:tests"]);
  });

  it("uses a model-assisted tour-builder post-pass with language lessons", () => {
    const data = runPython(String.raw`
import importlib.util, json, os, tempfile, pathlib
spec = importlib.util.spec_from_file_location('direct_runner', os.environ['UA_DIRECT_TEST_RUNNER'])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory() as d:
    mod.TMP = pathlib.Path(d); mod.INTER = pathlib.Path(d)
    mod.PROVIDER = 'codex-cli'; mod.MODEL = 'gpt-5.5'
    calls = []
    def fake_call(messages, max_tokens=12000, schema_path=None):
        calls.append(messages[-1]['content'])
        return json.dumps({'tour': [
            {'order':1,'title':'Project Orientation','description':'Read the README to establish project purpose and vocabulary before inspecting code.', 'nodeIds':['document:README.md'], 'languageLesson': None},
            {'order':2,'title':'Package API','description':'The package initializer exposes the supported import surface and frames how callers enter the library.', 'nodeIds':['file:src/__init__.py'], 'languageLesson':'Python package __init__.py files define package import boundaries.'},
            {'order':3,'title':'Validation Core','description':'The validator module enforces the project rules that keep notes and IDs structurally consistent.', 'nodeIds':['file:src/validators.py'], 'languageLesson': None},
            {'order':4,'title':'Regression Tests','description':'The tests document expected validator behavior and prevent silent schema drift.', 'nodeIds':['file:tests/test_validators.py'], 'languageLesson': None},
            {'order':5,'title':'Deep Function Anchor','description':'This function is a useful implementation-level stop for understanding how validation decisions are made.', 'nodeIds':['function:src/validators.py:validate_frontmatter'], 'languageLesson':'Typed Python functions make validation contracts easier to inspect.'},
        ]}), {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
    mod.call_model = fake_call
    nodes = [
        {'id':'document:README.md','type':'document','name':'README.md','filePath':'README.md','summary':'project docs','tags':['docs'],'complexity':'simple'},
        {'id':'file:src/__init__.py','type':'file','name':'__init__.py','filePath':'src/__init__.py','summary':'exports package','tags':['python'],'complexity':'simple'},
        {'id':'file:src/validators.py','type':'file','name':'validators.py','filePath':'src/validators.py','summary':'validates markdown','tags':['python'],'complexity':'moderate'},
        {'id':'function:src/validators.py:validate_frontmatter','type':'function','name':'validate_frontmatter','filePath':'src/validators.py','lineRange':[10,40],'summary':'validates frontmatter','tags':['function'],'complexity':'moderate'},
        {'id':'file:tests/test_validators.py','type':'file','name':'test_validators.py','filePath':'tests/test_validators.py','summary':'tests validators','tags':['test'],'complexity':'simple'},
    ]
    edges = [{'source':'file:src/validators.py','target':'function:src/validators.py:validate_frontmatter','type':'contains','direction':'forward','weight':0.95}]
    layers = [{'id':'layer:validation','name':'Validation','description':'validation layer','nodeIds':['file:src/validators.py']}]
    tour = mod.make_tour(nodes, layers, edges, {'projectName':'atrium-core'})
print(json.dumps({'tour': tour, 'callCount': len(calls), 'prompt': calls[0]}))
`);
    expect(data.callCount).toBe(1);
    expect(data.prompt).toContain("guided learning tour");
    expect(data.tour).toHaveLength(5);
    expect(data.tour[1].languageLesson).toMatch(/__init__\.py/);
    expect(data.tour[4].nodeIds).toContain("function:src/validators.py:validate_frontmatter");
  });
});
