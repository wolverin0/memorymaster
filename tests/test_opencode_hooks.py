"""Execute installed JS plugins with native-shaped host callbacks and process doubles.

Contracts: https://opencode.ai/docs/plugins/ and
https://opencode.ai/v2/docs/build/plugins (prompt/context, abortable event stream).
No OpenCode account, model, home configuration or authoritative DB is used.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from memorymaster.surfaces.setup_hooks import install_opencode_hooks


PROCESS_DOUBLE = '''
// The Windows sandbox denies Node's named-pipe spawn (EPERM). Keep the actual
// plugin/hooks/timers and model only the Python process streams; Python entry
// points are separately exercised in test_hook_recall/test_hook_capture.
import childProcess from "node:child_process";
import {syncBuiltinESMExports} from "node:module";
import {EventEmitter} from "node:events";
import {appendFileSync,existsSync} from "node:fs";
childProcess.spawn=(python,args,options)=>{
  if (!existsSync(python)) throw new Error("missing interpreter");
  const child=new EventEmitter();
  child.stdin=new EventEmitter(); child.stdout=new EventEmitter();
  child.stdout.setEncoding=()=>{};
  let timer;
  child.kill=()=>{clearTimeout(timer); child.emit("close",null)};
  child.stdin.end=(text)=>{
    const payload=JSON.parse(text);
    const argv=[args[1].replaceAll(".","/")+".py",...args.slice(2)];
    appendFileSync(process.env.TEST_HOOK_TRACE,JSON.stringify({argv,payload,options,python})+"\\n");
    timer=setTimeout(()=>{
      if (args[1].endsWith("hook_recall")) child.stdout.emit("data","[MemoryMaster recall]\\n- [mm-42] "+payload.prompt);
      child.emit("close",0);
    },args[1].endsWith("hook_recall") ? Number(process.env.TEST_RECALL_SLEEP??0)*1000 : 0);
  };
  return child;
};
syncBuiltinESMExports();
const module=await import("./plugins/memorymaster.js");
'''


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    if not shutil.which("node"):
        pytest.fail("Node is required to execute the OpenCode JavaScript contract")
    root = tmp_path / "python source with spaces"
    root.mkdir()
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("TEST_HOOK_TRACE", str(trace))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    config = tmp_path / "config"
    config.mkdir()
    (config / "package.json").write_text('{"type":"module"}', encoding="utf-8")

    def run(version, script, *, python_exe=None):
        install_opencode_hooks(config, version=version, project_root=root, python_exe=python_exe or sys.executable)
        harness = config / "harness.mjs"
        harness.write_text(PROCESS_DOUBLE + script, encoding="utf-8")
        result = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=12,
                                env=os.environ.copy(), cwd=config)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
        return json.loads(result.stdout), rows

    return run


V1_INIT = '''
const hooks = await module.MemoryMaster({directory: "/fixture/project"});
const prompt = async (id, text) => hooks["chat.message"]({sessionID:"ses_1"}, {
  message:{id}, parts:[{type:"text",text}, {type:"tool",text:"never recall tools"}]
});
const context = async (system, id="ses_1") => {
  await hooks["experimental.chat.system.transform"]({sessionID:id}, {system});
};
'''

V2_INIT = '''
const hooks = {};
let release;
const ready = new Promise(resolve => {release=resolve});
let disposed = 0;
const cleanup = await module.default.setup({
  location:{directory:"/fixture/project"},
  session:{hook:async (name, callback) => {
    hooks[name]=callback; return {dispose:async()=>{disposed++}};
  }},
  event:{async *subscribe({signal}) {
    await ready;
    yield {type:"session.execution.succeeded",data:{sessionID:"foreign"},location:null};
    yield {type:"session.execution.succeeded",data:{sessionID:"ses_1"},location:null};
    await new Promise(resolve => {
      if (signal.aborted) resolve();
      else signal.addEventListener("abort",resolve,{once:true});
    });
  }},
});
const prompt = async (id,text) => hooks.prompt({sessionID:"ses_1",messageID:id,prompt:{text}});
const context = async (system,id="ses_1") => hooks.context({sessionID:id,system,messages:[]});
'''


@pytest.mark.parametrize("version", [1, 2])
def test_plugin_latest_prompt_injection_continuation_and_capture(plugin, version):
    init = V1_INIT if version == 1 else V2_INIT
    ending = '''
await hooks.event({event:{type:"session.idle",properties:{sessionID:"foreign"}}});
await hooks.event({event:{type:"session.idle",properties:{sessionID:"ses_1"}}});
''' if version == 1 else '''
release();
await new Promise(resolve => setTimeout(resolve,100));
await cleanup();
'''
    output, rows = plugin(version, init + '''
await prompt("msg_old", "old user question");
await prompt("msg_latest", "Why did we choose SQLite?");
const system = [];
await context(system);
await context(system);
const foreign = [];
await context(foreign,"foreign");
''' + ending + 'console.log(JSON.stringify({system,foreign}));')
    text = output["system"][0] if version == 1 else output["system"][0]["text"]
    assert len(output["system"]) == 1
    assert text == "[MemoryMaster recall]\n- [mm-42] Why did we choose SQLite?"
    assert output["foreign"] == []
    recalls = [row for row in rows if row["argv"][0].endswith("hook_recall.py")]
    captures = [row for row in rows if row["argv"][0].endswith("hook_capture.py")]
    receipts = [row for row in rows if row["argv"][0].endswith("hook_events.py")]
    assert [row["payload"] for row in recalls] == [{
        "session_id": "ses_1", "cwd": "/fixture/project", "prompt": "Why did we choose SQLite?",
    }]
    assert recalls[0]["options"]["cwd"].endswith("python source with spaces")
    assert recalls[0]["options"]["windowsHide"] is True
    assert [row["payload"] for row in captures] == [{"session_id": "ses_1", "cwd": "/fixture/project"}]
    assert len(receipts) == 2
    assert all(row["argv"][1:] == ["--cli", "opencode", "--event", "injection", "--count", "1"] for row in receipts)


@pytest.mark.parametrize("version", [1, 2])
def test_plugin_slow_recall_is_killed_silently(plugin, monkeypatch, version):
    monkeypatch.setenv("TEST_RECALL_SLEEP", "6")
    init = V1_INIT if version == 1 else V2_INIT
    ending = "" if version == 1 else "release(); await cleanup();"
    output, rows = plugin(version, init + '''
await prompt("msg_1", "Why did we choose SQLite?");
const system=["host system"];
const start=Date.now();
await context(system);
const elapsed=Date.now()-start;
''' + ending + 'console.log(JSON.stringify({system,elapsed}));')
    assert output["system"] == ["host system"]
    assert 2500 <= output["elapsed"] < 3600
    assert all(row["argv"][0].endswith("hook_recall.py") for row in rows)


@pytest.mark.parametrize("version", [1, 2])
def test_missing_interpreter_does_not_break_model_request(plugin, tmp_path, version):
    init = V1_INIT if version == 1 else V2_INIT
    ending = "" if version == 1 else "release(); await cleanup();"
    output, rows = plugin(version, init + '''
await prompt("msg_1", "Why did we choose SQLite?");
const system=["host system"];
await context(system);
''' + ending + 'console.log(JSON.stringify(system));', python_exe=str(tmp_path / "missing-python.exe"))
    assert output == ["host system"]
    assert rows == []


def test_v1_resumed_session_reads_only_latest_authored_user(plugin):
    output, rows = plugin(1, '''
const hooks=await module.MemoryMaster({directory:"/fixture/project",client:{session:{
  messages:async ({path})=>({data:[
    {info:{id:"old",role:"user"},parts:[{type:"text",text:"old"}]},
    {info:{id:"new",role:"user"},parts:[{type:"text",text:"Why use WAL?"}]},
    {info:{id:"answer",role:"assistant"},parts:[{type:"text",text:"Do not query me"}]}
  ]})
}}});
const system=[];
await hooks["experimental.chat.system.transform"]({sessionID:"ses_1"},{system});
console.log(JSON.stringify(system));
''')
    assert output == ["[MemoryMaster recall]\n- [mm-42] Why use WAL?"]
    assert rows[0]["payload"]["prompt"] == "Why use WAL?"


def test_v2_resumed_context_native_user_shape(plugin):
    output, rows = plugin(2, V2_INIT + '''
const system=[];
await hooks.context({sessionID:"ses_1",system,messages:[
  {role:"user",content:[{type:"text",text:"Why use WAL?"}]},
  {role:"assistant",content:[{type:"text",text:"Do not query me"}]}
]});
release(); await cleanup();
console.log(JSON.stringify(system));
''')
    assert output == [{"type": "text", "text": "[MemoryMaster recall]\n- [mm-42] Why use WAL?"}]
    assert rows[0]["payload"]["prompt"] == "Why use WAL?"


def test_v1_timeout_cannot_mutate_system_after_hook_returns(plugin):
    output, rows = plugin(1, '''
const hooks=await module.MemoryMaster({directory:"/fixture/project",client:{session:{
  messages:async ()=>{await new Promise(resolve=>setTimeout(resolve,3100)); return {data:[
    {info:{role:"user"},parts:[{type:"text",text:"Why use WAL?"}]}
  ]}}
}}});
const system=[];
await hooks["experimental.chat.system.transform"]({sessionID:"ses_1"},{system});
await new Promise(resolve=>setTimeout(resolve,200));
console.log(JSON.stringify(system));
''')
    assert output == []
    assert rows == []


def test_v2_reconnects_release_listeners_and_capture_terminal_events(plugin):
    output, rows = plugin(2, '''
const originalTimeout=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>originalTimeout(fn,ms===1000?1:ms,...args);
let attempts=0;
let done;
const finished=new Promise(resolve=>{done=resolve});
const hooks={};
const cleanup=await module.default.setup({location:{directory:"/fixture/project"},
  session:{hook:async(name,cb)=>{hooks[name]=cb; return {dispose:async()=>{}}}},
  event:{async *subscribe({signal}) {
    attempts++;
    if(attempts<=12) throw Error("disconnected");
    for(const suffix of ["succeeded","failed","interrupted"]) {
      yield {type:"session.execution."+suffix,data:{sessionID:"ses_1"}};
    }
    done();
    await new Promise(resolve=>signal.addEventListener("abort",resolve,{once:true}));
  }}
});
await hooks.prompt({sessionID:"ses_1",messageID:"msg_1",prompt:{text:"Why use WAL?"}});
const alive=setInterval(()=>{},100);
await finished;
clearInterval(alive);
await cleanup();
console.log(JSON.stringify({attempts}));
''')
    assert output == {"attempts": 13}
    assert len(rows) == 3
    assert all(row["argv"][0].endswith("hook_capture.py") for row in rows)
