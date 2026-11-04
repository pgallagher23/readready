# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behavioral regression test for the reading-order reorder flush (browser JS).

Guards the cross-IIFE scope bug where ``initSrOrderBridge.flush()`` called
``setProgrammaticValue`` — a helper that had been defined inside a *different*
IIFE (``initSrEditBridge``). At runtime that threw ``ReferenceError``, so a
reorder never reached the hidden sink and "Update PDF" had nothing to apply.

The test extracts APP_JS from app.py, runs it in a minimal DOM stub under node,
fires Alt+ArrowDown on a fake ``.ro-row``, and asserts the flush reached the
sink with the expected ``{page: [ids]}`` payload. Skips when node is absent.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

HARNESS = r'''
const fs = require('fs');
var listeners = {};
function elem(props){ props=props||{};
  var e = { tagName: props.tagName||'DIV', style:{}, dataset:{}, _attrs: props._attrs||{}, _cls: props._cls||[],
    classList:{ add(){}, remove(){}, toggle(){}, contains(c){ return (e._cls||[]).indexOf(c)>=0; } },
    setAttribute(k,v){ e._attrs[k]=v; }, getAttribute(k){ return e._attrs[k]; },
    appendChild(){}, insertBefore(){}, removeChild(){}, focus(){}, blur(){},
    querySelector(){ return props._qs?props._qs():null; }, querySelectorAll(){ return props._qsa?props._qsa():[]; },
    closest(){ return props._closest?props._closest():null; }, contains(){ return false; },
    getBoundingClientRect(){ return {top:0,height:10}; }, addEventListener(){},
    dispatchEvent(ev){ (e._dispatched=e._dispatched||[]).push(ev.type); return true; },
    parentNode: props.parentNode||null, previousElementSibling:null, nextElementSibling:null,
    textContent:'', innerText:'', value:'' };
  return Object.assign(e, props);
}
global.window = global;
global.navigator = { userAgent:'node' };
global.HTMLInputElement = function(){}; global.HTMLInputElement.prototype = {};
global.HTMLTextAreaElement = function(){}; global.HTMLTextAreaElement.prototype = {};
Object.defineProperty(global.HTMLInputElement.prototype,'value',{set(v){this._v=v;},get(){return this._v;},configurable:true});
Object.defineProperty(global.HTMLTextAreaElement.prototype,'value',{set(v){this._v=v;},get(){return this._v;},configurable:true});
global.CSS = { escape:s=>s };
global.Event = function(t){ this.type=t; };
global.MutationObserver = function(){ return {observe(){},disconnect(){}}; };
global.matchMedia = ()=>({matches:false,addEventListener(){},addListener(){}});
global.getComputedStyle = ()=>({getPropertyValue(){return '';}});
global.requestAnimationFrame = f=>setTimeout(f,0);
global.document = {
  head: elem(), body: elem(), documentElement: elem(),
  createElement:t=>elem({tagName:(t||'div').toUpperCase()}),
  getElementById:id=> (global.__sink && id===global.__sinkId) ? global.__sink : null,
  querySelector:()=>null, querySelectorAll:()=>[],
  addEventListener:(type,h)=>{ (listeners[type]=listeners[type]||[]).push(h); },
  removeEventListener(){}, dispatchEvent(){ return true; },
};
try { new Function(fs.readFileSync(process.argv[2],'utf8'))(); }
catch(e){ console.log('LOAD ERROR:', e.message); process.exit(2); }
var sinkInput = elem({tagName:'TEXTAREA'});
global.__sinkId = 'sr-order-sink-0-0';
global.__sink = { querySelector:()=>sinkInput };
var rowA = elem({_cls:['ro-row'], _attrs:{'data-elem-id':'elem_1'}, _qs:()=>({textContent:'P'})});
var rowB = elem({_cls:['ro-row'], _attrs:{'data-elem-id':'elem_2'}, _qs:()=>({textContent:'P'})});
var list = elem({ _attrs:{'data-ro-sink-id':global.__sinkId,'data-ro-page':'3'}, _qsa:()=>[rowA,rowB] });
rowA.parentNode=list; rowB.parentNode=list; rowA.nextElementSibling=rowB; rowB.previousElementSibling=rowA;
list.insertBefore=()=>{};
var kd = listeners['keydown']||[];
var threw=null;
kd.forEach(h=>{ try{ h({target:rowA, altKey:true, key:'ArrowDown', preventDefault(){}}); }catch(e){ threw=e; } });
if (threw){ console.log('FAIL: handler threw:', threw.message); process.exit(1); }
var d = sinkInput._dispatched||[];
if (d.indexOf('input')>=0 && d.indexOf('change')>=0 && sinkInput._v){
  console.log('PASS ' + sinkInput._v); process.exit(0);
}
console.log('FAIL: flush did not reach sink'); process.exit(1);
'''


def _extract_app_js() -> str:
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    m = re.search(r'APP_JS = r"""(.*?)"""', src, re.S)
    assert m, "APP_JS raw string not found in app.py"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_reorder_flush_reaches_sink(tmp_path):
    app_js = tmp_path / "app.js"
    app_js.write_text(_extract_app_js(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(app_js)],
        capture_output=True, text=True, timeout=30,
    )
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, f"reorder flush harness failed: {out or proc.stderr}"
    assert out.startswith("PASS"), out
    # payload must be the {page: [ids]} shape _on_sr_order_batch parses
    assert '"3":["elem_1","elem_2"]' in out, out
