# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test for the Reading-order default view sticking on "Loading page…".

``initSrDefaultView`` auto-selects the Reading-order view once per shell. It
used to mark the shell defaulted *before* the switch, so when the
``.ro-page-sink`` Textbox had not mounted yet ``_roLoadPage`` returned silently
and nothing ever retried — the shell sat on the order view showing only the
"Loading page…" placeholder until the user clicked a view button by hand.

Mount order makes that deterministic in production: the ``sr-edit-container``
HTML (which carries the toggle button the auto-select looks for) is declared
before the two sink Textboxes, so there is always a window where the button
exists and the sink does not.

The test runs APP_JS under node with a shell whose page sink appears only
*after* the first auto-select tick, and asserts the page fetch still reaches
the sink. Skips when node is absent.
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
  var e = { tagName: props.tagName||'DIV', style:{setProperty(){}}, dataset:{},
    _attrs: props._attrs||{}, _cls: props._cls||[],
    classList:{ add(){}, remove(){}, toggle(){}, contains(c){ return (e._cls||[]).indexOf(c)>=0; } },
    setAttribute(k,v){ e._attrs[k]=v; }, getAttribute(k){ return e._attrs[k]!==undefined?e._attrs[k]:null; },
    appendChild(){}, insertBefore(){}, removeChild(){}, focus(){}, blur(){},
    querySelector(s){ return e._qs?e._qs(s):null; },
    querySelectorAll(s){ return e._qsa?e._qsa(s):[]; },
    closest(s){ return e._closest?e._closest(s):null; }, contains(){ return false; },
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

// --- the shell, mounted the way Gradio mounts it -------------------------
// Phase 1: toggle button present (sr-edit-container HTML is declared first),
// page sink NOT yet mounted. Phase 2 (later): sink appears.
var sinkInput = elem({tagName:'TEXTAREA'});
var sinkRoot  = elem({_cls:['ro-page-sink'], _qs:()=>sinkInput});
var SINK_MOUNTED = false;

var host = elem({_cls:['sr-view-host'], _qsa:()=>[orderBtn]});
var orderBtn = elem({tagName:'BUTTON', _attrs:{'data-view':'order'}});
var firstPageEl = elem({_attrs:{'data-ro-first-page':'7'}});
var shell = elem({_cls:['sr-editor-shell'], _qs:function(sel){
  if (sel.indexOf('.sr-view-toggle button[data-view="order"]') >= 0) return orderBtn;
  if (sel.indexOf('.ro-page-sink') >= 0) return SINK_MOUNTED ? sinkRoot : null;
  if (sel.indexOf('.ro-view-inner') >= 0) return null;   // never loaded yet
  if (sel.indexOf('data-ro-first-page') >= 0) return firstPageEl;
  return null;
}});
orderBtn._closest = function(sel){
  if (sel.indexOf('sr-view-host') >= 0) return host;
  if (sel.indexOf('sr-editor-shell') >= 0) return shell;
  return null;
};

global.document = {
  head: elem(), body: elem(), documentElement: elem(),
  createElement:t=>elem({tagName:(t||'div').toUpperCase()}),
  getElementById:()=>null,
  querySelector:()=>null,
  querySelectorAll:function(sel){
    return (sel.indexOf('sr-editor-shell') >= 0) ? [shell] : [];
  },
  addEventListener:(type,h)=>{ (listeners[type]=listeners[type]||[]).push(h); },
  removeEventListener(){}, dispatchEvent(){ return true; },
};

try { new Function(fs.readFileSync(process.argv[2],'utf8'))(); }
catch(e){ console.log('LOAD ERROR:', e.message); process.exit(2); }

// Let the auto-select run at least one tick with NO sink mounted...
setTimeout(function(){
  SINK_MOUNTED = true;            // ...then Gradio finishes mounting the sink.
  setTimeout(function(){
    var d = sinkInput._dispatched || [];
    if (sinkInput._v && d.indexOf('input') >= 0 && d.indexOf('change') >= 0) {
      console.log('PASS page=' + sinkInput._v + ' view=' + shell.getAttribute('data-view'));
      process.exit(0);
    }
    console.log('FAIL: page never fetched (sink value=' + JSON.stringify(sinkInput._v)
                + ', view=' + shell.getAttribute('data-view') + ') — stuck on "Loading page…"');
    process.exit(1);
  }, 700);
}, 400);
'''


def _extract_app_js() -> str:
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    m = re.search(r'APP_JS = r"""(.*?)"""', src, re.S)
    assert m, "APP_JS raw string not found in app.py"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_default_view_waits_for_page_sink(tmp_path):
    """The Reading-order default view must fetch its page even when the sink
    mounts after the first auto-select tick."""
    app_js = tmp_path / "app.js"
    app_js.write_text(_extract_app_js(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(app_js)],
        capture_output=True, text=True, timeout=30,
    )
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, f"default-view harness failed: {out or proc.stderr}"
    assert out.startswith("PASS"), out
    # it must open the flagged first page from data-ro-first-page, not page 1
    assert "page=7" in out, out
    assert "view=order" in out, out


# --- second harness: the sink is present but NOT yet bound by Gradio --------
# The write goes through the prototype setter (as setProgrammaticValue does) and
# is swallowed until Gradio binds the field — exactly what Svelte does before
# hydration, and what production showed: sink present, value "", never loaded.
# The loader must keep re-issuing instead of firing once and stranding the view.
HARNESS_UNBOUND = HARNESS.replace(
    "var SINK_MOUNTED = false;",
    "var SINK_MOUNTED = true;   // present from the start, but not yet bound",
).replace(
    """Object.defineProperty(global.HTMLTextAreaElement.prototype,'value',{set(v){this._v=v;},get(){return this._v;},configurable:true});""",
    """global.BOUND = false; global.WRITES = 0;
Object.defineProperty(global.HTMLTextAreaElement.prototype,'value',{
  set(v){ global.WRITES++; this._v = global.BOUND ? v : ''; },
  get(){ return this._v || ''; }, configurable:true});
setTimeout(function(){ global.BOUND = true; }, 900);""",
).replace(
    """setTimeout(function(){
  SINK_MOUNTED = true;            // ...then Gradio finishes mounting the sink.
  setTimeout(function(){""",
    """setTimeout(function(){
  setTimeout(function(){""",
).replace(
    """    var d = sinkInput._dispatched || [];
    if (sinkInput._v && d.indexOf('input') >= 0 && d.indexOf('change') >= 0) {
      console.log('PASS page=' + sinkInput._v + ' view=' + shell.getAttribute('data-view'));
      process.exit(0);
    }""",
    """    if (sinkInput._v === '7' && global.WRITES > 1) {
      console.log('PASS page=' + sinkInput._v + ' writes=' + global.WRITES);
      process.exit(0);
    }""",
).replace(
    """    console.log('FAIL: page never fetched (sink value=' + JSON.stringify(sinkInput._v)
                + ', view=' + shell.getAttribute('data-view') + ') — stuck on "Loading page…"');""",
    """    console.log('FAIL: page never fetched (sink value=' + JSON.stringify(sinkInput._v)
                + ', writes=' + global.WRITES + ') — stuck on "Loading page…"');""",
).replace("}, 2500);", "}, 2500);").replace("}, 700);", "}, 2500);")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_page_load_retries_until_sink_is_bound(tmp_path):
    """A sink in the DOM but not yet bound swallows the write; the loader must
    re-issue it rather than strand the view on "Loading page…"."""
    app_js = tmp_path / "app.js"
    app_js.write_text(_extract_app_js(), encoding="utf-8")
    harness = tmp_path / "harness_unbound.js"
    harness.write_text(HARNESS_UNBOUND, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, f"unbound-sink harness failed: {out or proc.stderr}"
    assert out.startswith("PASS"), out
    assert "page=7" in out, out
