"""Tests for the Ask-AI dashboard surface: the chat client
(assistant/static/chat.js), its escape-first markdown twin (md.js) and
the conductor.py wiring that serves them.

JS tests run under a Node vm with the same fake-DOM harness convention as
test_conductor.py - chat.js is bundled as a string and executed against
stub document/window/fetch/localStorage. Convention: C:\\Projects\\TESTING.md
"""
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import conductor                                        # noqa: E402

STATIC_DIR = ROOT / "assistant" / "static"
CHAT_JS = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
MD_JS = (STATIC_DIR / "md.js").read_text(encoding="utf-8")

# Shared harness preamble: a minimal DOM good enough for chat.js.
# - document.getElementById resolves only registered ids; the injected
#   skeleton registers its ids when insertAdjacentHTML runs.
# - element stubs carry the fields chat.js touches plus ._h[event]
#   handler capture and .closest-style navigation via test-supplied
#   event targets.
_HARNESS_PRELUDE = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const bundle=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes=new Map(), handlers={}, timers=[], fetches=[];
function mkEl(id){return {id,innerHTML:'',hidden:true,textContent:'',
  value:'',checked:false,disabled:false,className:'',title:'',
  placeholder:'',style:{},dataset:{},attrs:{},scrollTop:0,
  scrollHeight:0,clientHeight:0,_h:{},
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},
  addEventListener(ev,h){this._h[ev]=h;},
  click(){this._clicked=(this._clicked||0)+1;},
  focus(){this._focused=(this._focused||0)+1;},
  remove(){}};}
const document={
  getElementById:id=>nodes.get(id)||null,
  _reg(id){if(!nodes.has(id))nodes.set(id,mkEl(id));return nodes.get(id);},
  querySelector:()=>null,querySelectorAll:()=>[],activeElement:null,
  body:{insertAdjacentHTML(pos,html){
    document._html=(document._html||'')+html;
    for(const m of html.matchAll(/id="([^"]+)"/g))document._reg(m[1]);},
    insertBefore(){},appendChild(){}},
  addEventListener(ev,h){handlers[ev]=h;}};
// the dashboard's static markup carries #dask - it exists before chat.js
document._reg('dask').setAttribute('data-ask-ai','');
const store={_m:{},getItem(k){return k in this._m?this._m[k]:null;},
  setItem(k,v){this._m[k]=String(v);},removeItem(k){delete this._m[k];}};
const sess={_m:{},getItem(k){return k in this._m?this._m[k]:null;},
  setItem(k,v){this._m[k]=String(v);},removeItem(k){delete this._m[k];}};
function responder(url,opt){return {ok:true,status:200,
  headers:{get:()=>'application/json'},
  json:async()=>route(url,opt),text:async()=>''};}
const fetch=async(url,opt)=>{fetches.push({url,opt});
  const out=responder(url,opt);if(!out.ok)return out;return out;};
const window={localStorage:store,sessionStorage:sess,
  getSelection:()=>({toString:()=>''}),aiView:null,aiNav:null,
  location:{reload:()=>{}}};
const ctx=vm.createContext({document,window,fetch,
  localStorage:store,sessionStorage:sess,setTimeout:(fn,ms)=>{timers.push(fn);return timers.length;},
  clearTimeout:()=>{},console,setInterval:()=>0,clearInterval:()=>{},
  crypto:{randomUUID:(()=>{let n=0;
    return()=> 'uuid0000-0000-4000-8000-'+String(++n).padStart(12,'0');})()},
  Promise,Date,JSON,Math,encodeURIComponent,decodeURIComponent});
let _route=(url,opt)=>({});
function route(url,opt){return _route(url,opt);}
function setRoute(fn){_route=fn;}
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
vm.runInContext(bundle.md,ctx);
vm.runInContext(bundle.chat,ctx);
'''


def run_node(body: str, route_js: str = "setRoute(()=>({}));") -> subprocess.CompletedProcess:
    """Run the harness + one test body under `node -e`. `route_js`
    installs the fetch responder (url,opt)->json payload."""
    harness = (_HARNESS_PRELUDE + route_js + "\n" + body)
    return subprocess.run(
        ["node", "-e", harness],
        input=json.dumps({"md": MD_JS, "chat": CHAT_JS}),
        capture_output=True, text=True, encoding="utf-8", timeout=15)


class MarkupWiringTests(unittest.TestCase):
    """Static coverage: the shell launcher covers every modal uniformly
    (it sits on #detail, outside #dbody, so no view can opt out), the
    client assets are wired into the page, and the extraction split the
    test suite relies on still sees one inline script."""

    def setUp(self):
        self.h = conductor._DASH_HTML

    def test_assets_linked_and_shell_launcher_present(self):
        for needle in ('href="/api/assistant/static/chat.css"',
                       'src="/api/assistant/static/md.js"',
                       'src="/api/assistant/static/chat.js" defer',
                       'id="dask"', 'data-ask-ai',
                       'aria-label="Ask AI about this view"'):
            self.assertIn(needle, self.h)
        # the launcher is inside #detail but OUTSIDE the repainted #dbody
        detail = self.h.split('id="detail"', 1)[1]
        self.assertIn('id="dask"', detail.split('id="dbody"', 1)[0])
        # exactly one static launcher - the page FAB is injected by
        # chat.js, so no duplicates ever coexist
        self.assertEqual(self.h.count('data-ask-ai'), 1)

    def test_inline_script_split_still_intact(self):
        js = self.h.split("<script>\n", 1)[1].split("</script>", 1)[0]
        self.assertIn("globalThis.aiView", js)
        self.assertIn("globalThis.aiNav", js)
        self.assertIn("globalThis.aiSync", js)

    def test_bridge_exports_present(self):
        js = self.h.split("<script>\n", 1)[1].split("</script>", 1)[0]
        for needle in ("globalThis.aiView = () => ({",
                       "modalQA: modalQA", "globalThis.aiNav = {",
                       "agent(k)", "openSummary(k)", "openRunSummary(k)",
                       "showRunDetails(k)", "openRequest(id)"):
            self.assertIn(needle, js)

    def test_client_files_exist_and_are_sane(self):
        for name in ("chat.js", "md.js", "chat.css"):
            self.assertTrue((STATIC_DIR / name).is_file(), name)
        self.assertIn("window.AskAI", CHAT_JS)
        self.assertIn("window.aiSync", CHAT_JS)
        self.assertIn("[data-ask-ai]", CHAT_JS)
        self.assertIn("Aimd.render", CHAT_JS)
        self.assertIn("role=\"dialog\"", CHAT_JS)
        self.assertIn("aria-live", CHAT_JS)
        self.assertIn("#aifab", (STATIC_DIR / "chat.css")
                      .read_text(encoding="utf-8"))


class StaticRouteTests(unittest.TestCase):
    """The handler serves the whitelisted client files without needing a
    live core, and assistant JSON routes get query strings forwarded."""

    def _server(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        runs = root / "runs"; runs.mkdir()
        reqs = root / "requests"; reqs.mkdir()
        handler = conductor.make_dashboard_handler(
            runs, ["no-such-devin"], reqs, root / "manifests")
        srv = conductor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        # cleanups run LIFO: close the lazily-opened assistant core (its
        # sqlite handle would pin the tempdir on Windows), then shut the
        # server, then close its socket - td.cleanup runs last
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(handler.assistant_close)
        return srv

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path,
                     headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read()
        ctype = res.getheader("Content-Type")
        conn.close()
        return res.status, ctype, body

    def test_static_files_served(self):
        srv = self._server()
        for name, ctype in (("chat.js", "javascript"),
                            ("md.js", "javascript"),
                            ("chat.css", "text/css")):
            status, ct, body = self._get(
                srv.server_port, f"/api/assistant/static/{name}")
            self.assertEqual(status, 200, name)
            self.assertIn(ctype, ct)
            self.assertEqual(body, (STATIC_DIR / name).read_bytes())

    def test_static_whitelist(self):
        srv = self._server()
        for path in ("/api/assistant/static/nope.js",
                     "/api/assistant/static/..%2Fconductor.py",
                     "/api/assistant/static/conductor.py",
                     "/api/assistant/static/"):
            status, _, body = self._get(srv.server_port, path)
            self.assertEqual(status, 404, path)

    def test_assistant_api_routes_with_query(self):
        """/api/assistant/* reaches dispatch with the query forwarded -
        this exercises both the lazy core open and parse_qs passing."""
        srv = self._server()
        status, ct, body = self._get(
            srv.server_port, "/api/assistant/conversations?limit=5")
        # a fresh tempdir opens an empty core cleanly (the devin CLI is
        # not touched until a dispatch happens)
        self.assertEqual(status, 200)
        out = json.loads(body.decode("utf-8"))
        self.assertEqual(out["items"], [])

    def _post(self, port, path, body, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", path, body=body,
                     headers={"Content-Type": ctype})
        res = conn.getresponse()
        payload = res.read()
        status, hdr = res.status, res.getheader("Content-Type")
        conn.close()
        return status, hdr, payload

    def test_assistant_attachment_upload_and_bytes_route(self):
        """POST stores raw image bytes as a conversation-scoped row;
        GET <id> serves the bytes back through the row-id boundary -
        never a client-supplied path. Non-image bodies are refused by
        content, bad ids are 404."""
        srv = self._server()
        port = srv.server_port
        st, _, payload = self._post(
            port, "/api/assistant/conversations",
            json.dumps({"origin": {"view": "dashboard"}}))
        self.assertEqual(st, 201)
        cid = json.loads(payload)["id"]
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        st, _, payload = self._post(
            port, f"/api/assistant/attachments?conversation={cid}",
            png, "application/octet-stream")
        self.assertEqual(st, 201)
        att = json.loads(payload)
        self.assertEqual(att["conversation_id"], cid)
        self.assertEqual(att["ctype"], "image/png")
        status, ctype, body = self._get(
            port, f"/api/assistant/attachments/{att['id']}")
        self.assertEqual(status, 200)
        self.assertIn("image/png", ctype)
        self.assertEqual(body, png)
        # non-image bytes refused by content; bad ids -> 404; the id is
        # a row key - no filesystem path is ever taken from the client
        st, _, _ = self._post(
            port, f"/api/assistant/attachments?conversation={cid}",
            b"MZ" + b"x" * 64, "application/octet-stream")
        self.assertEqual(st, 415)
        status, _, _ = self._get(
            port, "/api/assistant/attachments/att-nope")
        self.assertEqual(status, 404)
        status, _, _ = self._get(
            port, "/api/assistant/attachments/..%2Fconductor.py")
        self.assertEqual(status, 404)

    def test_assistant_json_posts_keep_the_small_body_cap(self):
        """The 20 MB allowance is scoped to the attachments route -
        ordinary JSON POSTs stay at the dashboard's 512KB bound."""
        srv = self._server()
        st, _, body = self._post(
            srv.server_port, "/api/assistant/conversations",
            json.dumps({"origin": {"view": "dashboard"},
                        "pad": "x" * 600_000}))
        self.assertEqual(st, 413)


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class MdRenderTests(unittest.TestCase):

    def _render(self, src):
        p = run_node(
            'process.stdout.write(JSON.stringify('
            'window.Aimd.render(' + json.dumps(src) + ')))'
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_never_executes_markup(self):
        out = self._render('<script>alert(1)</script><img src=x '
                           'onerror=y>**b**')
        self.assertIn('&lt;script&gt;', out)
        self.assertNotIn('<script>', out)
        self.assertNotIn('onerror', out.replace('onerror=', 'x'))
        self.assertIn('<strong>b</strong>', out)

    def test_tables_code_links(self):
        out = self._render(
            "head\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```py\nx<1\n```\n"
            "[ok](https://ex.com) [bad](javascript:evil) http://x.io\n")
        self.assertIn('<table class="mdtable">', out)
        self.assertIn('<td>1</td>', out)
        self.assertIn('<code class="lang-py">', out)
        self.assertIn('href="https://ex.com"', out)
        self.assertNotIn('href="javascript', out)
        self.assertIn('href="http://x.io"', out)

    def test_lists_and_headings(self):
        out = self._render("## h\n- one\n- two\n1. first\n---\n> note\n")
        self.assertIn('<h2>h</h2>', out)
        self.assertIn('<ul><li>one</li>', out)
        self.assertIn('<ol><li>first</li>', out)
        self.assertIn('<hr>', out)
        self.assertIn('<blockquote>note</blockquote>', out)


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ChatClientTests(unittest.TestCase):
    """The required UI behaviors, driven against the real chat.js."""

    def test_launcher_toggle_and_modal_coverage(self):
        """The injected FAB + the shell #dask both open the same surface
        (delegated [data-ask-ai] clicks), and Escape minimizes without
        letting a keydown leak to a modal behind it."""
        p = run_node(r'''
assert(nodes.get('aifab'), 'FAB injected');
assert.equal(nodes.get('aifab').hidden, false);
assert.equal(nodes.get('dask').hidden, false);   // init reveals it
const dask = nodes.get('dask');
// a click on the in-modal launcher (closest resolves to it, like a real
// event on the button or its inner span)
handlers.click({target:{closest:s=>s==='[data-ask-ai]'?dask:null},
  preventDefault:()=>{}});
assert.equal(nodes.get('aichat').hidden, false);
assert.equal(nodes.get('aifab').hidden, true);   // open chat hides FAB
// Escape inside the chat minimizes only the chat: stopPropagation must
// be called so the modal handler behind it cannot also fire
let stopped=false;
nodes.get('aichat')._h.keydown({key:'Escape',
  target:{tagName:'DIV'},preventDefault:()=>{},
  stopPropagation:()=>{stopped=true;}});
assert.equal(stopped, true);
assert.equal(nodes.get('aichat').hidden, true);
// reopen via the FAB
const fab=nodes.get('aifab');
handlers.click({target:{closest:s=>s==='[data-ask-ai]'?fab:null},
  preventDefault:()=>{}});
assert.equal(nodes.get('aichat').hidden, false);
process.exitCode=0;
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_navigation_persistence(self):
        """Open chat, hold a draft + conversation, move the host through
        modal views (aiView changes, aiSync fires) and minimize/reopen:
        conversation, messages, draft and panel state all survive."""
        p = run_node(r'''
(async()=>{
  // pretend the modal stack currently shows task modal 'r|s1'
  window.aiView=()=>({detailKey:'r|s1',summaryView:null,
    runModalKey:null,modalRequest:null,modalQA:null,power:false,
    only:[],facets:[],projs:[],selectedRun:null,agents:[]});
  window.AskAI.open();
  const ctxel=nodes.get('ai-ctx');
  window.aiSync();
  assert.match(ctxel.textContent,/task s1/);
  // a conversation + messages + a draft in flight
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,readonly:1};
  S.msgs=[{id:'m1',role:'user',text:'q1',ctx_ref:'',settings_json:'{}'},
          {id:'m2',role:'assistant',text:'a1',status:'complete',
           ctx_ref:'',settings_json:'{}'}];
  nodes.get('ai-text').value='half-typed draft';
  window.AskAI._paint();
  assert.match(nodes.get('ai-list').innerHTML,/a1/);
  // navigate the modal: run modal replaces the task view
  window.aiView=()=>({detailKey:null,summaryView:null,
    runModalKey:'request:x',modalRequest:null,modalQA:null,power:false,
    only:[],facets:[],projs:[],selectedRun:null,agents:[]});
  window.aiSync();
  assert.match(ctxel.textContent,/run request:x/);
  assert.match(nodes.get('ai-list').innerHTML,/a1/);
  assert.equal(nodes.get('ai-text').value,'half-typed draft');
  // minimize then reopen - same conversation, same messages, same draft
  window.AskAI.close();
  assert.equal(nodes.get('aichat').hidden,true);
  window.AskAI.open();
  await flush();
  assert.equal(S.conv.id,'conv-1');
  assert.match(nodes.get('ai-list').innerHTML,/a1/);
  assert.equal(nodes.get('ai-text').value,'half-typed draft');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_history_restoration(self):
        """A reload with a persisted open state + conversation id fetches
        the stored conversation and paints its messages."""
        p = run_node(
            r'''
store.setItem('ac-ai-open','compact');
store.setItem('ac-ai-conv','conv-7');
store.setItem('ac-ai-draft:conv-7','my draft');
// init() already ran at load - reopen so it re-reads storage
window.AskAI.close();
store.setItem('ac-ai-open','compact');
window.AskAI.open();
(async()=>{
  await flush();
  assert(fetches.some(f=>f.url.includes('/conversations/conv-7')),
    'conversation refetched');
  assert.match(nodes.get('ai-list').innerHTML,/hello world/);
  assert.equal(nodes.get('ai-text').value,'my draft');
  assert.equal(window.AskAI._s.conv.id,'conv-7');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.includes('/conversations/conv-7/exchanges'))
    return {items:[]};
  if(url.includes('/conversations/conv-7/usage'))
    return {totals:{exchanges:1,prompt_t:10,completion_t:5}};
  if(url.includes('/conversations/conv-7/actions'))
    return {items:[]};
  if(url.includes('/conversations/conv-7'))
    return {conversation:{id:'conv-7',title:'old chat',
      model:'gpt-6-luna',effort:'low',fast:1,readonly:1},
      items:[{id:'m1',role:'user',text:'hi',ctx_ref:'',
        settings_json:'{}'},
        {id:'m2',role:'assistant',text:'hello world',
        status:'complete',ctx_ref:'',settings_json:'{}'}],
      next_before:null};
  return {items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_settings_dialog(self):
        """The agent-name button opens the settings dialog; catalog
        families populate Model, efforts filter per family, Save POSTs
        the snapshot shape, the header relabels, and a historical reply
        keeps ITS recorded settings."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,readonly:1};
  window.AskAI._paint();
  window.AskAI.openSettings();
  await flush();
  const setw=nodes.get('aisetwrap');
  assert.equal(setw.hidden,false);
  const sel=nodes.get('ais-model');
  assert.match(sel.innerHTML,/gpt-6-luna/);
  assert.match(sel.innerHTML,/swe-1\.6/);
  // effort select is scoped to the chosen family
  assert.match(nodes.get('ais-effort').innerHTML,/low/);
  // fast allowed: the family carries a fast marker
  assert.equal(nodes.get('ais-fast').disabled,false);
  assert.match(nodes.get('ais-fast-note').textContent,/priority/);
  // change effort to medium via the dialog's change event, keep
  // fast+readonly
  setw._h.change({target:{id:'ais-effort',value:'medium',
    checked:false}});
  const saveBtn=nodes.get('ais-save');
  setw._h.click({target:{closest:s=>s==='#ais-save'?saveBtn:null}});
  await flush();
  const posted=fetches.find(f=>f.url.includes('/settings')&&
    f.opt&&f.opt.method==='POST');
  assert(posted,'settings POST happened');
  assert.equal(posted.url,'/api/assistant/conversations/conv-1/settings');
  const body=JSON.parse(posted.opt.body);
  assert.deepEqual(body,{model:'gpt-6-luna',effort:'medium',
    fast:true,readonly:true});
  // header now shows the new snapshot
  assert.match(nodes.get('ai-name').textContent,/gpt-6-luna.*medium/);
  // ...while the old reply badge still shows what it ran under
  const html=window.AskAI._msgHtml({id:'m2',role:'assistant',
    text:'old',status:'complete',ctx_ref:'',
    settings_json:'{"model":"gpt-6-luna","effort":"low","fast":true,"readonly":true}'});
  assert.match(html,/gpt-6-luna \u00b7 low \u00b7 fast/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/models'))return{models:[
    {uid:'gpt-6-luna-low',family:'gpt-6-luna',
     label:'GPT-6 Luna Low',in:0.2,cached:0.02,out:1.0,
     vision:true,fast_variant:false},
    {uid:'gpt-6-luna-low-priority',family:'gpt-6-luna',
     label:'GPT-6 Luna Low Fast',in:0.2,cached:0.02,out:1.0,
     vision:true,fast_variant:true},
    {uid:'gpt-6-luna-medium',family:'gpt-6-luna',
     label:'GPT-6 Luna Medium',in:0.4,cached:0.04,out:2.0,
     vision:true,fast_variant:false},
    {uid:'swe-1.6-max',family:'swe-1.6',
     label:'SWE 1.6 Max',in:2.0,out:8.0,vision:null,
     fast_variant:false}],
   default_resolution:{ok:true,uid:'gpt-6-luna-low-priority'}};
  if(url.endsWith('/settings')&&opt&&opt.method==='POST')
    return{id:'conv-1',model:'gpt-6-luna',effort:'medium',fast:1,
      readonly:1};
  if(url.endsWith('/settings'))return{defaults:{model:'gpt-6-luna',
    effort:'low',fast:true,readonly:true}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_citation_navigation(self):
        """A citation chip routes through window.aiNav with its frozen
        nav descriptor and does not reset the open conversation."""
        p = run_node(
            r'''
(async()=>{
  const navCalls=[];
  window.aiNav={agent:k=>{navCalls.push(['agent',k]);return true;},
    run:k=>{navCalls.push(['run',k]);return true;},
    summary:k=>{navCalls.push(['summary',k]);return true;},
    runSummary:k=>{navCalls.push(['runSummary',k]);return true;},
    request:k=>{navCalls.push(['request',k]);return true;}};
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',effort:'',fast:0,readonly:1};
  S.msgs=[{id:'u1',role:'user',text:'q',ctx_ref:'',settings_json:'{}'},
          {id:'a1',role:'assistant',text:'see [1]',status:'complete',
           ctx_ref:'',settings_json:'{}'}];
  S.exchByAid['a1']={id:'x1',status:'done',
    sources:[{kind:'agent',key:'r|s1',label:'task s1',
              nav:{view:'agent',key:'r|s1'}},
             {kind:'run',key:'request:z',label:'run z',
              nav:{view:'run',key:'request:z'}}]};
  window.AskAI._paint();
  const html=nodes.get('ai-list').innerHTML;
  assert.match(html,/aim-src/);
  assert.match(html,/task s1/);
  // click the agent citation
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='[data-ai-nav]'?{getAttribute:k=>({'data-ai-nav':'agent',
      'data-ai-key':'r|s1'})[k]}:null}});
  assert.deepEqual(navCalls,[['agent','r|s1']]);
  // a run citation routes to the run-details opener
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='[data-ai-nav]'?{getAttribute:k=>({'data-ai-nav':'run',
      'data-ai-key':'request:z'})[k]}:null}});
  assert.deepEqual(navCalls,[['agent','r|s1'],['run','request:z']]);
  // conversation untouched, chat still open
  assert.equal(S.msgs.length,2);
  assert.equal(nodes.get('aichat').hidden,false);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_paste_image_uploads_text_paste_untouched(self):
        """Clipboard handling: an image item becomes an eager upload +
        thumbnail (default paste suppressed only for it), while a
        text-only clipboard falls through to the browser's normal
        paste - and the upload created the conversation first, since
        attachments are conversation-scoped."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const ta=nodes.get('ai-text');
  let prevented=0;
  const pd=()=>{prevented++;};
  // text-only paste: untouched - no preventDefault, no upload, no conv
  ta._h.paste({clipboardData:{items:[
    {kind:'string',type:'text/plain',getAsFile:()=>null},
    {kind:'string',type:'text/html',getAsFile:()=>null}]},
    preventDefault:pd});
  assert.equal(prevented,0);
  assert(!fetches.some(f=>f.url.includes('/attachments')),
    'no upload for text');
  // image item -> suppressed + uploaded; conversation materializes
  const img={name:'shot.png',type:'image/png',size:64};
  ta._h.paste({clipboardData:{items:[
    {kind:'file',type:'image/png',getAsFile:()=>img}]},
    preventDefault:pd});
  assert.equal(prevented,1);
  await flush();
  const up=fetches.find(f=>f.url.includes('/attachments?'));
  assert(up,'attachment POST happened');
  assert(up.opt.body===img,'raw file bytes posted, not JSON');
  assert.match(up.url,/conversation=conv-9/);
  const convPost=fetches.findIndex(f=>f.url.endsWith('/conversations')&&
    f.opt&&f.opt.method==='POST');
  assert(convPost>=0&&convPost<fetches.indexOf(up),
    'conversation created before the upload');
  const strip=nodes.get('ai-ats');
  assert.equal(strip.hidden,false);
  assert.match(strip.innerHTML,
    /api\/assistant\/attachments\/att-1/);
  assert.match(strip.innerHTML,/data-ai-atrm="0"/);
  // a second paste with a NON-image file is skipped with a note
  ta._h.paste({clipboardData:{items:[
    {kind:'file',type:'application/pdf',
     getAsFile:()=>({name:'doc.pdf',type:'application/pdf',size:10})}]},
    preventDefault:pd});
  await flush();
  assert.match(nodes.get('ai-note').innerHTML,/Skipped/);
  assert.equal(fetches.filter(f=>f.url.includes('/attachments?'))
    .length,1);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/conversations')&&opt&&opt.method==='POST')
    return{id:'conv-9',client_ref:'w',title:'',model:'gpt-6-luna',
      effort:'low',fast:1,readonly:1,origin_view:{}};
  if(url.includes('/attachments?'))
    return{id:'att-1',conversation_id:'conv-9',message_id:null,
      name:'shot.png',ctype:'image/png',bytes:64};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_file_picker_remove_and_send_binds_ids(self):
        """The paperclip opens the hidden file input; picked images
        upload + thumbnail; the remove control drops a pending image;
        send posts the surviving attachment ids and clears the strip."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const fi=nodes.get('ai-file');
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-attach'?nodes.get('ai-attach'):null}});
  assert.equal(fi._clicked,1,'attach button opens the file picker');
  const S=window.AskAI._s;
  S.conv={id:'conv-9',model:'gpt-6-luna',effort:'low',fast:1,
    readonly:1};
  const a={name:'a.png',type:'image/png',size:10};
  const b={name:'b.png',type:'image/png',size:10};
  fi.files=[a,b];
  fi._h.change({target:fi});
  await flush();
  assert.equal(S.attach.length,2);
  // remove the first pending image; the second (att-3) survives
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='[data-ai-atrm]'?{getAttribute:k=>'0'}:null}});
  assert.equal(S.attach.length,1);
  assert.equal(S.attach[0].name,'att-3.png');
  // an in-flight upload gates send with an explanation
  S.attach.push({id:null,name:'c.png',status:'uploading'});
  nodes.get('ai-text').value='look';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  await flush();
  assert(!fetches.some(f=>f.url.endsWith('/messages')),
    'send waits for uploads');
  assert.match(nodes.get('ai-note').innerHTML,/in flight/);
  // upload settles; send carries the surviving ids + clears the strip
  S.attach.pop();
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  await flush();
  const post=fetches.find(f=>f.url.endsWith('/messages'));
  const body=JSON.parse(post.opt.body);
  assert.deepEqual(body.attachments,['att-3']);
  assert.equal(S.attach.length,0);
  assert.equal(nodes.get('ai-ats').hidden,true);
  // attachments-only send is legal (text optional)
  S.attach=[{id:'att-7',name:'c.png',ctype:'image/png',bytes:9,
    status:''}];
  nodes.get('ai-text').value='';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  await flush();
  const post2=fetches.filter(f=>f.url.endsWith('/messages'))[1];
  const b2=JSON.parse(post2.opt.body);
  assert.equal(b2.text,'');
  assert.deepEqual(b2.attachments,['att-7']);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
let n=0;
setRoute((url,opt)=>{
  if(url.includes('/attachments?'))
    return{id:'att-'+(++n+1),conversation_id:'conv-9',
      message_id:null,name:'att-'+(n+1)+'.png',ctype:'image/png',
      bytes:10};
  if(url.endsWith('/messages')&&opt&&opt.method==='POST')
    return{message:{id:'mu'+n,conversation_id:'conv-9',
      client_msg_id:'m-x',role:'user',text:'',
      attachments:'[]',ctx_ref:'{}',settings_json:'{}',
      status:'complete'},
      assistant_message:{id:'ma'+n,conversation_id:'conv-9',
        role:'assistant',text:'',ctx_ref:'{}',settings_json:'{}',
        status:'pending'},
      exchange:{id:'xch-'+n,conversation_id:'conv-9',
        user_msg_id:'mu'+n,assistant_msg_id:'ma'+n,
        status:'done',text:'ok'}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_message_attachments_render_thumbnails(self):
        """A user message's attachments JSON renders as previews served
        by the id-gated binary route - including on reopened
        conversations - and the stored path never reaches the markup."""
        p = run_node(
            r'''
(async()=>{
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',readonly:1};
  S.msgs=[{id:'u1',role:'user',text:'look',
    attachments:'[{"id":"att-9","name":"shot.png","ctype":"image/png",'+
      '"bytes":12,"path":"D:/data/attachments/x.png"}]',
    ctx_ref:'',settings_json:'{}'},
    {id:'u2',role:'user',text:'',
     attachments:'[{"id":"att-8","name":"only.png"}]',ctx_ref:'',
     settings_json:'{}'},
    {id:'a1',role:'assistant',text:'nice image',status:'complete',
     ctx_ref:'',settings_json:'{}'}];
  window.AskAI._paint();
  const html=nodes.get('ai-list').innerHTML;
  assert.match(html,/class="aim-ats"/);
  assert.match(html,/api\/assistant\/attachments\/att-9/);
  assert.match(html,/api\/assistant\/attachments\/att-8/);
  assert.match(html,/shot\.png/);
  assert(!html.includes('D:/data'),'server path stays out of markup');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_vision_refusal_note_points_to_settings(self):
        """When the exchange errors with the vision refusal, the note
        names the explanation AND points at the model settings."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  nodes.get('ai-text').value='what is this';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  await flush();
  assert(timers.length>0,'poll scheduled');
  timers.shift()();
  await flush();
  const n=nodes.get('ai-note').innerHTML;
  assert.match(n,/no verified image support/);
  assert.match(n,/vision-capable model/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/conversations')&&opt&&opt.method==='POST')
    return{id:'conv-9',client_ref:'w',model:'swe-2',readonly:1};
  if(url.endsWith('/messages')&&opt&&opt.method==='POST')
    return{message:{id:'mu',conversation_id:'conv-9',role:'user',
      text:'what is this',ctx_ref:'{}',settings_json:'{}'},
      assistant_message:{id:'ma',conversation_id:'conv-9',
        role:'assistant',text:'',ctx_ref:'{}',settings_json:'{}',
        status:'pending'},
      exchange:{id:'xch-9',user_msg_id:'mu',assistant_msg_id:'ma',
        status:'queued'}};
  if(url.includes('/exchanges/xch-9'))
    return{id:'xch-9',user_msg_id:'mu',assistant_msg_id:'ma',
      status:'error',text:'',
      error:'image attachment refused - the selected model `swe-2-max` '+
        'has no verified image support on this provider; verified '+
        'alternatives: gpt-6-luna-low-priority'};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_mic_unsupported_browser_explains(self):
        """No SpeechRecognition (the harness default = the Firefox
        case): the mic renders visibly inert with an explanatory
        tooltip, and clicking surfaces the honest note - it never
        pretends to work."""
        p = run_node(
            r'''
const mic=nodes.get('ai-mic');
assert(mic,'mic button exists');
assert(mic.className.indexOf('na')>=0,'inert state painted at init');
assert.equal(mic.getAttribute('aria-pressed'),'false');
assert.match(mic.title,/Firefox does not/i);
nodes.get('aichat')._h.click({target:{closest:s=>
  s==='#ai-mic'?mic:null}});
assert.match(nodes.get('ai-note').innerHTML,
  /not available in this browser/);
assert.equal(window.AskAI._s.dict,null);
process.exitCode=0;
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_dictation_lifecycle(self):
        """With a stub SpeechRecognition: explicit start paints the
        recording state, interim+final results land in the box (typed
        text intact, nothing auto-sent), the transcript stays
        editable, permission denial produces a visible note, and
        close/switch aborts the session."""
        p = run_node(
            r'''
(async()=>{
  const recs=[];
  window.SpeechRecognition=function(){
    const r={continuous:null,interimResults:null,onresult:null,
      onerror:null,onend:null,started:0,stopped:0,aborted:0,
      start(){this.started++;},stop(){this.stopped++;},
      abort(){this.aborted++;}};
    recs.push(r);return r;};
  window.AskAI._dictUI();
  window.AskAI.open();
  await flush();
  const mic=nodes.get('ai-mic');
  assert(!/na/.test(mic.className),'mic live where supported');
  nodes.get('ai-text').value='typed bit';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-mic'?mic:null}});
  assert.equal(recs.length,1);
  const rec=recs[0];
  assert.equal(rec.started,1);
  assert.equal(rec.continuous,true);
  assert.equal(rec.interimResults,true);
  assert.match(mic.className,/rec/);
  assert.equal(mic.getAttribute('aria-pressed'),'true');
  // the once-per-tab external-processing disclosure landed
  assert.match(nodes.get('ai-note').innerHTML,/speech service/i);
  assert.match(nodes.get('ai-status').textContent,/Listening/);
  // interim + final append after the typed text (results carries the
  // whole list; resultIndex marks where the changed run begins)
  rec.onresult({resultIndex:0,results:[
    {isFinal:true,0:{transcript:'hello'}},
    {isFinal:false,0:{transcript:'wor'}}]});
  assert.equal(nodes.get('ai-text').value,'typed bit hello wor');
  // interim refines without losing the final part
  rec.onresult({resultIndex:1,results:[
    {isFinal:true,0:{transcript:'hello'}},
    {isFinal:false,0:{transcript:'world'}}]});
  assert.equal(nodes.get('ai-text').value,'typed bit hello world');
  // dictation NEVER posts the message
  assert(!fetches.some(f=>f.url.endsWith('/messages')));
  // transcript stays editable
  nodes.get('ai-text').value+='!';
  // explicit stop -> rec.stop, onend clears state
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-mic'?mic:null}});
  assert.equal(rec.stopped,1);
  rec.onend();
  assert.equal(window.AskAI._s.dict,null);
  // permission denial -> visible actionable note + abort
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-mic'?mic:null}});
  const rec2=recs[1];
  rec2.onerror({error:'not-allowed'});
  assert.match(nodes.get('ai-note').innerHTML,/permission denied/i);
  assert.equal(rec2.aborted,1);
  assert.equal(window.AskAI._s.dict,null);
  // conversation switch aborts a live session too
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',readonly:1};
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-mic'?mic:null}});
  const rec3=recs[2];
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='[data-ai-conv]'?{getAttribute:k=>'conv-2'}:null}});
  assert.equal(rec3.aborted,1);
  assert.equal(S.dict,null);
  // and minimize() kills it - a closed composer never keeps recording
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-mic'?mic:null}});
  const rec4=recs[3];
  window.AskAI.close();
  assert.equal(rec4.aborted,1);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_send_flow_and_poll(self):
        """Send queues an exchange; the poll follows it to done and the
        reply text lands in the bubble."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  nodes.get('ai-text').value='how many runs failed?';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  await flush();
  // created conversation then posted the message with the ctx envelope
  const post=fetches.find(f=>f.url.endsWith('/messages'));
  assert(post,'message POST happened');
  const b=JSON.parse(post.opt.body);
  assert.equal(b.text,'how many runs failed?');
  assert(b.client_msg_id && /^[A-Za-z0-9_-]{8,64}$/.test(b.client_msg_id),
    'client_msg_id matches the server contract');
  assert.equal(b.client_ctx.view,'dashboard');
  assert(b.client_ctx.window,'window identity sent');
  // exchange is queued -> poll timer armed; step it
  assert(timers.length>0,'poll scheduled');
  timers.shift()();
  await flush();
  assert.match(nodes.get('ai-list').innerHTML,/the answer is 2/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/conversations')&&opt&&opt.method==='POST')
    return{id:'conv-9',client_ref:'w',title:'',model:'gpt-6-luna',
      effort:'low',fast:1,readonly:1,origin_view:{}};
  if(url.endsWith('/messages')&&opt&&opt.method==='POST')
    return{message:{id:'mu',conversation_id:'conv-9',
        client_msg_id:'m-x',role:'user',text:'how many runs failed?',
        ctx_ref:'{}',settings_json:'{}',status:'complete'},
      assistant_message:{id:'ma',conversation_id:'conv-9',
        role:'assistant',text:'',ctx_ref:'{}',settings_json:'{}',
        status:'pending'},
      exchange:{id:'xch-1',conversation_id:'conv-9',user_msg_id:'mu',
        assistant_msg_id:'ma',status:'queued'}};
  if(url.includes('/exchanges/xch-1'))
    return{id:'xch-1',user_msg_id:'mu',assistant_msg_id:'ma',
      status:'done',text:'the answer is 2',sources:[],
      prompt_t:10,completion_t:5,usd:0.001};
  if(url.endsWith('/conversations/conv-9/exchanges'))
    return{items:[{id:'xch-1',user_msg_id:'mu',assistant_msg_id:'ma',
      status:'done',sources:[]}]};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
