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
                       "modalQA: modalQA", "pool: poolView",
                       "trash: showTrash", "globalThis.aiNav = {",
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

    def test_nav_links_become_in_app_affordances(self):
        """`nav:<view>/<key>` (the model's citation grammar) renders as a
        data-ai-nav affordance the delegated handler routes to aiNav -
        never an external href. %XX stays inside the escaped key."""
        out = self._render(
            "[Open summary](nav:summary/request%3Aabc%7Cstep-1) "
            "[x](nav:run/request%3Aabc) "
            "[evil](nav:summary/k%22%20onclick%3Dalert(1))")
        self.assertIn('data-ai-nav="summary"', out)
        self.assertIn('data-ai-key="request:abc|step-1"', out)
        self.assertIn('data-ai-nav="run"', out)
        self.assertNotIn('href="nav:', out)
        # %22 decodes to a literal quote - it must stay ESCAPED inside
        # the key attribute, never break out into a real attribute
        self.assertIn('data-ai-key="k&quot; onclick=alert(1"', out)
        self.assertNotIn('" onclick=', out)

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
assert.equal(nodes.get('ai-name').textContent,'gpt-6-luna');
  assert.match(nodes.get('ai-name').title,/gpt-6-luna.*medium.*fast/);
  // badges are model-only too; the full snapshot moves into the title
  const html=window.AskAI._msgHtml({id:'m2',role:'assistant',
    text:'old',status:'complete',ctx_ref:'',
    settings_json:'{"model":"gpt-6-luna","effort":"low","fast":true,"readonly":true}'});
  assert.match(html,/>gpt-6-luna<\/button>/);
  assert.match(html,/title="gpt-6-luna · low · fast/);
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

    def test_readonly_checkbox_reflects_saved_state(self):
        """sqlite stores readonly/fast as INTEGER - a saved readonly=0
        must open the settings dialog UNCHECKED (`0 !== false` in JS made
        it always-checked), the header pill reads 'actions on', and the
        pill itself opens the dialog."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  // the wire shape: sqlite ints, exactly what GET /conversations returns
  S.conv={id:'conv-1',model:'gpt-6-luna',effort:'low',fast:0,readonly:0};
  window.AskAI._paint();
  // the pill reports the saved state, not a default
  assert.equal(nodes.get('ai-state').textContent,'actions on');
  // the pill is a button - clicking it opens assistant settings
  nodes.get('aichat')._h.click({target:{
    closest:s=>s==='#ai-state'?nodes.get('ai-state'):null}});
  await flush();
  assert.equal(nodes.get('aisetwrap').hidden,false,
    'state pill opens assistant settings');
  assert.equal(nodes.get('ais-ro').checked,false,
    'readonly=0 must render unchecked, not always-checked');
  assert.equal(nodes.get('ais-fast').checked,false);
  assert.equal(S.sel.readonly,false,
    'S.sel carries the saved off-state - a no-change save stays off');
  // and a raw-int conv still seeds correctly if ingest is bypassed
  S.conv.readonly=0;
  window.AskAI._settingsForm();
  assert.equal(nodes.get('ais-ro').checked,false);
  // the true branch too: readonly=1 stays checked
  S.conv.readonly=1;
  window.AskAI._settingsForm();
  assert.equal(nodes.get('ais-ro').checked,true);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/models'))return{models:[
    {uid:'gpt-6-luna-low',family:'gpt-6-luna',label:'G6L',
     in:0.2,out:1.0,vision:true,fast_variant:false}],
   default_resolution:{ok:true,uid:'gpt-6-luna-low'}};
  if(url.endsWith('/settings'))return{defaults:{model:'gpt-6-luna',
    effort:'low',fast:false,readonly:true}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_usage_dropdown_light_dismiss(self):
        """The header usage <details> has no native light-dismiss: a
        click on neutral page surface closes it, a click inside leaves
        it open, and Escape drops the topmost layer before minimizing."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const ud=nodes.get('ai-usage');
  const outside={closest:()=>null};
  // outside click while closed: no-op
  handlers.click({target:outside});
  assert(!ud.open);
  // open -> outside click closes it
  ud.open=true;
  handlers.click({target:outside});
  assert.equal(ud.open,false,'outside click dismisses usage popup');
  // open -> click INSIDE the details leaves it alone
  ud.open=true;
  handlers.click({target:{closest:s=>s==='#ai-usage'?ud:null}});
  assert.equal(ud.open,true,'click inside stays open');
  // Escape peels the popup first, the chat stays up
  nodes.get('aichat')._h.keydown({key:'Escape',
    target:{},stopPropagation(){},preventDefault(){}});
  assert.equal(ud.open,false,'Escape closes the popup');
  assert.equal(nodes.get('aichat').hidden,false,'chat still open');
  // Escape again -> now the chat minimizes
  nodes.get('aichat')._h.keydown({key:'Escape',
    target:{},stopPropagation(){},preventDefault(){}});
  assert.equal(nodes.get('aichat').hidden,true,'second Escape minimizes');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_header_layout_keeps_controls(self):
        """Header text cells (name/ctx/usage) must be the shrinkable
        ones - flex:none on them pushed the right-side icon buttons out
        of the fixed 400px card when the usage label grew."""
        css = (STATIC_DIR / "chat.css").read_text(encoding="utf-8")
        self.assertRegex(css, r'\.ai-ctx[^{]*\{[^}]*flex: 0 1 auto')
        self.assertRegex(css, r'\.ai-ctx[^{]*\{[^}]*min-width: 0')
        self.assertRegex(css, r'\.ai-usage \{[^}]*flex: 0 1 auto')
        self.assertRegex(css, r'\.ai-usage summary \{[^}]*max-width: 100%')
        self.assertRegex(css, r'\.ai-name \{[^}]*min-width: 0')
        self.assertRegex(css, r'\.ai-hbtn \{[^}]*flex: none')
        # the state pill is a clickable control now
        self.assertRegex(css, r'\.ai-pill \{[^}]*cursor: pointer')

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

    def test_action_cards_propose_arm_execute_deny(self):
        """The guarded-action card: proposed -> Run/Deny (confirm tier) or
        Review&run -> armed exact-target block -> Confirm&run (launch
        tier, ack posted verbatim). Read-only renders the buttons inert
        with a hint; deny posts approve:false; card text escapes."""
        p = run_node(r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',effort:'',fast:0,readonly:0};
  const ac1={id:'act-1',conversation_id:'conv-1',exchange_id:'x1',
    kind:'set_run_archived',
    payload:'{"run":"r1","archived":true}',state:'proposed',
    requires_confirm:1,confirm_text:'Archive this run',explain:'it done',
    result:null,expected_ack:null};
  const ac2={id:'act-2',conversation_id:'conv-1',exchange_id:'x1',
    kind:'stop_run',payload:'{"run":"r1"}',state:'proposed',
    requires_confirm:1,confirm_text:'Stop this run - tasks die',
    explain:'<img src=x onerror=alert(1)>',result:null,
    expected_ack:'stop_run:r1'};
  S.actions={x1:[ac1,ac2]};
  // confirm-tier proposed: Run + Deny, explanation + params shown
  let h=window.AskAI._actHtml(ac1);
  assert.match(h,/proposed: set run archived/);
  assert.match(h,/it done/);
  assert.match(h,/Run</);
  assert.match(h,/Deny</);
  assert.doesNotMatch(h,/Review &/);
  // launch tier proposes with 'Review & run' (no instant Run)
  h=window.AskAI._actHtml(ac2);
  assert.match(h,/Review &amp; run/);
  assert.doesNotMatch(h,/>Run</);
  assert.doesNotMatch(h,/<img/, 'model text is escaped');
  assert.match(h,/&lt;img/);
  // read-only: inert buttons + the hint (never hidden)
  S.conv.readonly=1;
  h=window.AskAI._actHtml(ac1);
  assert.match(h,/disabled/);
  assert.match(h,/read-only is on/);
  S.conv.readonly=0;
  // arm -> the card shows the exact target + effect, then disarm
  await window.AskAI._actDecide('act-2','arm');
  h=window.AskAI._actHtml(ac2);
  assert.match(h,/Confirm &amp; run/);
  assert.match(h,/target: r1/);
  assert.match(h,/Stop this run - tasks die/);
  await window.AskAI._actDecide('act-2','disarm');
  assert(!S.armed['act-2']);
  // arm again + confirm-run posts approve+execute+the verbatim ack
  await window.AskAI._actDecide('act-2','arm');
  await window.AskAI._actDecide('act-2','exec');
  await flush();
  const post=fetches.find(f=>f.url.includes('/actions/act-2/confirm'));
  assert(post,'confirm POST happened');
  assert.deepEqual(JSON.parse(post.opt.body),
    {approve:true,execute:true,ack:'stop_run:r1'});
  // the executed row lands in the cache and renders the done state
  h=window.AskAI._actHtml(S.actions['x1'][1]);
  assert.equal(S.actions['x1'][1].state,'executed');
  assert.match(h,/done/);
  // deny posts approve:false - no execute, no ack
  await window.AskAI._actDecide('act-1','deny');
  const dpost=fetches.find(f=>f.url.includes('/actions/act-1/confirm'));
  assert.deepEqual(JSON.parse(dpost.opt.body),{approve:false});
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
const rows={
  'act-1':{id:'act-1',conversation_id:'conv-1',exchange_id:'x1',
    kind:'set_run_archived',payload:'{}',state:'proposed',
    requires_confirm:1,confirm_text:'x',explain:null,result:null,
    expected_ack:null},
  'act-2':{id:'act-2',conversation_id:'conv-1',exchange_id:'x1',
    kind:'stop_run',payload:'{}',state:'proposed',requires_confirm:1,
    confirm_text:'x',explain:null,result:null,expected_ack:'stop_run:r1'}};
setRoute((url,opt)=>{
  if(url.includes('/actions/')&&url.endsWith('/confirm')){
    const aid=url.split('/actions/')[1].split('/confirm')[0];
    const b=JSON.parse(opt.body);
    rows[aid].state=b.approve?(b.execute?'executed':'confirmed'):'denied';
    rows[aid].result=JSON.stringify({stopping:[]});
    return rows[aid];
  }
  if(url.includes('/actions'))
    return{items:Object.values(rows)};
  return{items:[]};
});
''')
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
  // attachments-only send is legal (text optional); a real uploaded
  // slot carries the conversation scope it was uploaded under
  S.attach=[{id:'att-7',name:'c.png',ctype:'image/png',bytes:9,
    status:'',conv:'conv-9'}];
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

    def test_usage_details_expandable_and_honest(self):
        """Every terminal exchange renders a compact usage chip that
        expands into the internal-request breakdown: provider-reported
        counters, the frozen settings snapshot, rate provenance, extra
        counters and an explicit 'calculated estimate - not an invoice'
        label. Missing usage reads 'unknown', never $0; cancelled rows
        keep their usage visible."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',effort:'',fast:0,readonly:1};
  S.msgs=[
    {id:'u1',role:'user',text:'q',ctx_ref:'',
     settings_json:'{"model":"gpt-6-luna","effort":"low","fast":true,"readonly":true}',
     attachments:'[{"id":"att-1","name":"a.png"}]'},
    {id:'a1',role:'assistant',text:'ok',status:'complete',ctx_ref:'',
     settings_json:'{"model":"gpt-6-luna","effort":"low","fast":true,"readonly":true}'},
    {id:'u2',role:'user',text:'q2',ctx_ref:'',settings_json:'{}'},
    {id:'a2',role:'assistant',text:'err',status:'error',ctx_ref:'',
     settings_json:'{}'},
    {id:'u3',role:'user',text:'q3',ctx_ref:'',settings_json:'{}'},
    {id:'a3',role:'assistant',text:'stopped',status:'cancelled',
     ctx_ref:'',settings_json:'{}'}];
  S.exchByAid['a1']={id:'x1',status:'done',user_msg_id:'u1',
    prompt_t:1234,completion_t:56,cached_t:500,llm_steps:2,
    model_uid:'gpt-6-luna-low-priority',
    model_label:'GPT-6 Luna Low Thinking Fast',
    provider_session_id:'sess-1',usd:0.000289,
    price_in:0.2,price_cached:0.02,price_out:1.0,
    price_fetched_at:1760000000.0,price_source:'docs-test',
    requests:[{n:1,kind:'fresh',request_id:'sess-1#h1',pid:1,
      session_id:'sess-1',started_at:1,ended_at:4,exit_code:0,
      usage:{prompt_t:1000,completion_t:50,cached_t:400,llm_steps:1}},
      {n:2,kind:'tool',request_id:'sess-1#h2',pid:1,
      session_id:'sess-1',started_at:4,ended_at:6,exit_code:0,
      usage:{prompt_t:234,completion_t:6,cached_t:100,llm_steps:1,
             extra:{total_cache_write_tokens:12}}}],
    usage_extra:{total_cache_write_tokens:12}};
  S.exchByAid['a2']={id:'x2',status:'error',user_msg_id:'u2',
    error:'provider died'};
  S.exchByAid['a3']={id:'x3',status:'cancelled',user_msg_id:'u3',
    prompt_t:70,usd:0.000014};
  window.AskAI._paint();
  const html=nodes.get('ai-list').innerHTML;
  // priced exchange: compact summary + the expandable hop breakdown
  assert.match(html,/<details class="aim-use" data-xu="x1">/);
  assert.match(html,/in 1\.2k \(500 cached\)/);
  assert.match(html,/out 56/);
  assert.match(html,/2 turns/);
  assert.match(html,/~\$0\.0003 est\./);
  assert.match(html,/internal requests/);
  assert.match(html,/sess-1#h1/);
  assert.match(html,/#2 tool/);
  assert.match(html,/cache write tokens 12/);
  assert.match(html,/gpt-6-luna-low-priority \(GPT-6 Luna/);
  assert.match(html,/read-only \u00b7 status done \u00b7 session sess-1/);
  assert.match(html,/rates \$0\.2 in \/ \$0\.02 cached \/ \$1 out/);
  assert.match(html,/source docs-test/);
  assert.match(html,/a calculated estimate, not an invoice/);
  // attachments turn notes there is no separate image charge
  assert.match(html,/no separate image charge/);
  // the failed row with no usage: honest 'unknown', never $0
  assert.match(html,/usage not reported/);
  assert.match(html,/cost unknown/);
  assert.doesNotMatch(html,/~\$0\.00([^0-9]|$)/);
  // a cancelled row keeps its billable usage + estimate visible,
  // and a real-but-tiny cost never rounds to a fake $0.00
  assert.match(html,/stopped/);
  assert.match(html,/in 70/);
  assert.match(html,/~&lt;\$0\.0001 est\./);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_usage_view_conversation_breakdown(self):
        """The header chip is an expandable breakdown: grand totals, a
        per-model split for mixed-model conversations, the unpriced
        count and pricing provenance - never presented as an invoice."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',effort:'',fast:0,readonly:1};
  S.usage={exchanges:[],totals:{exchanges:3,prompt_t:300,
    completion_t:60,cached_t:50,llm_steps:4,usd:0.001,unpriced:1,
    currency:'USD',cost_kind:'calculated',price_source:'docs-test',
    price_fetched_at:1760000000.0,
    by_model:{'m-a':{model_uid:'m-a',exchanges:2,prompt_t:200,
      completion_t:40,cached_t:50,llm_steps:2,usd:0.0008,unpriced:0},
     'm-b':{model_uid:'m-b',exchanges:1,prompt_t:100,completion_t:20,
      cached_t:0,llm_steps:2,usd:0,unpriced:1}}}};
  window.AskAI._paint();
  const uv=nodes.get('ai-useview').innerHTML;
  assert.match(uv,/by model/);
  assert.match(uv,/m-a/);
  assert.match(uv,/2 exch \u00b7 in 200 \u00b7 out 40 \(50 cached\)/);
  assert.match(uv,/1 unpriced/);
  assert.match(uv,/total 3 exchanges \u00b7 in 300 \u00b7 out 60 \(50 cached\)/);
  assert.match(uv,/1 exchange has no price data - their cost is unknown, not zero/);
  assert.match(uv,/calculated USD estimates/);
  assert.match(uv,/docs-test/);
  assert.match(uv,/not an invoice/);
  // the collapsed chip keeps a compact grand total - token sum, with
  // the in/out split preserved on the tooltip instead of the cramped
  // 'in/out' pair that truncated to '22.0/...' in the 400px card
  const sum=nodes.get('ai-usage-sum').textContent;
  assert.match(sum,/tok 360 · cached 50 · ~\$0\.0010/);
  assert.match(nodes.get('ai-usage-sum').title,
    /300 in \(50 cached\) \/ 60 out/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_settings_price_provenance_and_fast_compare(self):
        """The pre-selection line shows the resolved variant's rates,
        the standard-vs-fast comparison when fast reprices, image
        capability and rate provenance (USD + fetched + estimate, not
        an invoice) - informed choice, never an automatic switch."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,readonly:1};
  window.AskAI.openSettings();
  await flush();
  const txt=nodes.get('ais-price').textContent;
  assert.match(txt,/resolves to gpt-6-luna-low-priority/);
  assert.match(txt,/~\$0\.2 in \/ \$1 out per 1M tok \(\$0\.02 cached\)/);
  assert.match(txt,/vs standard low: ~\$0\.1 \/ \$0\.5/);
  assert.match(txt,/image input verified/);
  assert.match(txt,/estimates in USD/);
  assert.match(txt,/rates fetched/);
  assert.match(txt,/docs\.devin\.ai modelCostData/);
  assert.match(txt,/not an invoice/);
  // uncheck fast -> comparison disappears, plain rates shown
  const setw=nodes.get('aisetwrap');
  setw._h.change({target:{id:'ais-fast',checked:false}});
  const t2=nodes.get('ais-price').textContent;
  assert.doesNotMatch(t2,/vs standard/);
  assert.match(t2,/resolves to gpt-6-luna-low \u00b7/);
  assert.match(t2,/~\$0\.1 in \/ \$0\.5 out/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/models'))return{models:[
    {uid:'gpt-6-luna-low',family:'gpt-6-luna',label:'Luna Low',
     in:0.1,cached:0.01,out:0.5,vision:true,fast_variant:false},
    {uid:'gpt-6-luna-low-priority',family:'gpt-6-luna',
     label:'Luna Low Fast',in:0.2,cached:0.02,out:1.0,vision:true,
     fast_variant:true}],
   default_resolution:{ok:true,uid:'gpt-6-luna-low-priority'},
   pricing:{currency:'USD',unit:'per 1M tokens',
     tier:'TEAMS_TIER_PRO',fetched_at:1760000000.0,
     source:'docs.devin.ai modelCostData (TEAMS_TIER_PRO)'}};
  if(url.endsWith('/settings'))return{defaults:{model:'gpt-6-luna',
    effort:'low',fast:true,readonly:true}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ChatTransportTests(unittest.TestCase):
    """Fast-transport UI surface: the per-reply transport chip (with its
    honest fallback form), the settings select (feature-detected so an
    old server never gets a field it would reject), transport on the
    usage breakdown, and the unchanged send/poll behavior on either
    transport."""

    def test_transport_indicator_chips(self):
        """Every reply that names its answering transport carries the
        chip; a requested transport that could not handle the turn
        renders as a fallback (requested->answered, amber), the
        read-only routing explains itself, and legacy rows with no
        transport field render no chip at all."""
        p = run_node(
            r'''
const TH=window.AskAI._transportHtml;
// a reply the fast transport answered
let h=TH({transport:'fast'},{model:'m'});
assert.match(h,/aim-tp/);
assert.match(h,/>fast<\/span>/);
assert.match(h,/answered via the fast transport/);
assert.doesNotMatch(h,/aim-tp fb/);
// a reply the CLI transport answered
h=TH({transport:'cli'},{});
assert.match(h,/>cli<\/span>/);
assert.doesNotMatch(h,/fb/);
// the runtime's fallback record ({from,to,reason} - raw JSON string
// as exchange_view ships it) renders the requested->answered chip and
// its own recorded reason on the tooltip
h=TH({transport:'cli',
  fallback:'{"from":"fast","to":"cli","reason":"the fast transport '+
    'failed to start: auth"}'},
 {transport:'fast'});
assert.match(h,/aim-tp fb/);
assert.match(h,/fast\u2192cli/);
assert.match(h,/the fast transport failed to start: auth/);
// fast was requested but cli answered with no fallback record ->
// derived fallback chip with the generic explanation
h=TH({transport:'cli'},{transport:'fast'});
assert.match(h,/aim-tp fb/);
assert.match(h,/fast\u2192cli/);
assert.match(h,/could not handle this turn/);
// a pre-thawed fallback dict works the same
h=TH({transport:'cli',fallback:{from:'fast',to:'cli',
  reason:'routed'}},{});
assert.match(h,/aim-tp fb/);
assert.match(h,/fast\u2192cli/);
// read-only routing explains WHY cli answered a 'fast' request
h=TH({transport:'cli'},{transport:'fast',readonly:false});
assert.match(h,/read-only by construction/);
// concrete id mismatch is a fallback; a matching id is not
h=TH({transport:'acp'},{transport:'warm'});
assert.match(h,/aim-tp fb/);
h=TH({transport:'warm'},{transport:'warm'});
assert.doesNotMatch(h,/aim-tp fb/);
// cli requested + cli answered is never a fallback
h=TH({transport:'cli'},{transport:'cli'});
assert.doesNotMatch(h,/aim-tp fb/);
// legacy rows (no transport anywhere) render no chip
assert.equal(TH({status:'done'},{}),'');
assert.equal(TH(null,{}),'');
// an empty/corrupt fallback string is not a fallback
assert.doesNotMatch(TH({transport:'cli',fallback:'{'},{}),/fb/);
// alternate wire spellings tolerated
h=TH({transport_used:'cli',fell_back:true},{});
assert.match(h,/aim-tp fb/);
// and msgHtml places the chip next to the model badge
const S=window.AskAI._s;
S.exchByAid={a1:{id:'x1',status:'done',transport:'fast'}};
h=window.AskAI._msgHtml({id:'a1',role:'assistant',text:'ok',
  status:'complete',ctx_ref:'',
  settings_json:'{"model":"gpt-6-luna","transport":"fast"}'});
assert.match(h,/aim-modelb/);
assert.match(h,/aim-tp/);
assert.match(h,/>fast<\/span>/);
process.exitCode=0;
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_settings_transport_select_posts_override(self):
        """The Transport select seeds from the conversation's stored
        override, lists auto + cli + the server-advertised fast
        transport, the note explains routing, and Save posts the new
        transport key alongside the existing settings."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,
    readonly:1,transport:'cli'};
  window.AskAI.openSettings();
  await flush();
  const ts=nodes.get('ais-transport');
  assert(ts,'transport select exists');
  assert.equal(ts.disabled,false);
  assert.match(ts.innerHTML,/value="cli"/);
  assert.match(ts.innerHTML,/value="fast"/);
  assert.equal(ts.value,'cli','seeded from the conversation');
  assert.match(nodes.get('ais-transport-note').textContent,
    /CLI transport only/);
  const setw=nodes.get('aisetwrap');
  // the fast transport explains the read-only fast path
  setw._h.change({target:{id:'ais-transport',value:'fast'}});
  assert.match(nodes.get('ais-transport-note').textContent,
    /fast answers read-only questions/);
  // read-write + non-cli explains the routing rule
  setw._h.change({target:{id:'ais-ro',checked:false}});
  assert.match(nodes.get('ais-transport-note').textContent,
    /read-write conversations still run on the CLI transport/);
  // save posts the new transport with the rest of the settings
  const saveBtn=nodes.get('ais-save');
  setw._h.click({target:{closest:s=>s==='#ais-save'?saveBtn:null}});
  await flush();
  const posted=fetches.find(f=>f.url.includes('/settings')&&
    f.opt&&f.opt.method==='POST');
  const body=JSON.parse(posted.opt.body);
  assert.equal(body.transport,'fast');
  assert.equal(body.model,'gpt-6-luna');
  assert.equal(body.readonly,false);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/models'))return{models:[
    {uid:'gpt-6-luna-low',family:'gpt-6-luna',label:'G6L',
     in:0.2,out:1.0,vision:true,fast_variant:false}],
   transports:[{id:'cli',label:'CLI (devin --print)',available:true},
     {id:'fast',label:'Fast (acp warm session)',available:true}],
   default_resolution:{ok:true,uid:'gpt-6-luna-low'}};
  if(url.endsWith('/settings')&&opt&&opt.method==='POST')
    return{id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,
      readonly:0,transport:'fast'};
  if(url.endsWith('/settings'))return{defaults:{model:'gpt-6-luna',
    effort:'low',fast:true,readonly:true,transport:'fast'}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_settings_transport_unsupported_server(self):
        """Against a pre-transport server (no transport key on
        defaults/conversation, no transports list): the select renders
        disabled with the honest note, and Save omits the field - an
        older core rejects unknown settings keys, so a phantom control
        must never break saving model/effort/fast/readonly."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,
    readonly:1};   // no transport key
  window.AskAI.openSettings();
  await flush();
  const ts=nodes.get('ais-transport');
  assert.equal(ts.disabled,true,'unsupported server disables select');
  assert.match(nodes.get('ais-transport-note').textContent,
    /does not advertise a transport choice/);
  const saveBtn=nodes.get('ais-save');
  const setw=nodes.get('aisetwrap');
  setw._h.click({target:{closest:s=>s==='#ais-save'?saveBtn:null}});
  await flush();
  const posted=fetches.find(f=>f.url.includes('/settings')&&
    f.opt&&f.opt.method==='POST');
  const body=JSON.parse(posted.opt.body);
  assert(!('transport' in body),'transport not posted to old server');
  assert.equal(body.model,'gpt-6-luna');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/models'))return{models:[
    {uid:'gpt-6-luna-low',family:'gpt-6-luna',label:'G6L',
     in:0.2,out:1.0,vision:true,fast_variant:false}],
   default_resolution:{ok:true,uid:'gpt-6-luna-low'}};
  if(url.endsWith('/settings')&&opt&&opt.method==='POST')
    return{id:'conv-1',model:'gpt-6-luna',effort:'low',fast:1,
      readonly:1};
  if(url.endsWith('/settings'))return{defaults:{model:'gpt-6-luna',
    effort:'low',fast:true,readonly:true}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_transport_in_usage_details(self):
        """The expandable usage block names the answering transport (and
        a fell-back marker), and each internal-request hop that carries
        its own transport shows it inline."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',model:'m',effort:'',fast:0,readonly:1};
  S.msgs=[
    {id:'u1',role:'user',text:'q',ctx_ref:'',settings_json:'{}'},
    {id:'a1',role:'assistant',text:'ok',status:'complete',ctx_ref:'',
     settings_json:'{"transport":"fast"}'},
    {id:'u2',role:'user',text:'q2',ctx_ref:'',settings_json:'{}'},
    {id:'a2',role:'assistant',text:'ok2',status:'complete',ctx_ref:'',
     settings_json:'{"transport":"fast"}'}];
  S.exchByAid['a1']={id:'x1',status:'done',user_msg_id:'u1',
    transport:'fast',prompt_t:10,completion_t:5,
    requests:[{n:1,kind:'fresh',transport:'fast',
      request_id:'sess-9#h1',started_at:1,ended_at:2,exit_code:0,
      usage:{prompt_t:10,completion_t:5}}]};
  S.exchByAid['a2']={id:'x2',status:'done',user_msg_id:'u2',
    transport:'cli',
    fallback:'{"from":"fast","to":"cli","reason":"init"}',
    prompt_t:20,completion_t:9};
  window.AskAI._paint();
  const html=nodes.get('ai-list').innerHTML;
  assert.match(html,/transport fast/);
  assert.match(html,/via fast/);          // per-hop transport
  assert.match(html,/transport cli \(fell back\)/);
  // and the meta chips read 'fast' / 'fast→cli' respectively
  assert.match(html,/aim-tp/);
  assert.match(html,/fast\u2192cli/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_progressive_reply_repaints_mid_run(self):
        """A transport that streams partial text repaints through the
        existing poll loop: the mid-run tick shows the partial reply
        with the typing caret, the terminal tick lands the full text -
        no new code path, pollX already repaints on exchange updates."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  nodes.get('ai-text').value='stream me';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  await flush();
  assert(timers.length>0,'poll scheduled');
  // first tick: running, partial text -> bubble shows it + caret
  timers.shift()();
  await flush();
  let html=nodes.get('ai-list').innerHTML;
  assert.match(html,/thinking out/);
  assert.match(html,/ai-caret/);
  assert.match(html,/aim-tp/);          // transport chip mid-flight
  // second tick: done -> full text, no caret
  timers.shift()();
  await flush();
  html=nodes.get('ai-list').innerHTML;
  assert.match(html,/thinking out loud/);
  assert.doesNotMatch(html,/ai-caret/);
  assert.match(html,/done/);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
let polls=0;
setRoute((url,opt)=>{
  if(url.endsWith('/conversations')&&opt&&opt.method==='POST')
    return{id:'conv-9',client_ref:'w',title:'',model:'gpt-6-luna',
      effort:'low',fast:1,readonly:1,origin_view:{}};
  if(url.endsWith('/messages')&&opt&&opt.method==='POST')
    return{message:{id:'mu',conversation_id:'conv-9',
        client_msg_id:'m-x',role:'user',text:'stream me',
        ctx_ref:'{}',settings_json:'{}',status:'complete'},
      assistant_message:{id:'ma',conversation_id:'conv-9',
        role:'assistant',text:'',ctx_ref:'{}',settings_json:'{}',
        status:'pending'},
      exchange:{id:'xch-1',conversation_id:'conv-9',user_msg_id:'mu',
        assistant_msg_id:'ma',status:'queued',transport:'fast'}};
  if(url.includes('/exchanges/xch-1')){
    polls++;
    return polls===1
      ? {id:'xch-1',user_msg_id:'mu',assistant_msg_id:'ma',
        status:'running',text:'thinking out',transport:'fast',
        sources:[],requests:[]}
      : {id:'xch-1',user_msg_id:'mu',assistant_msg_id:'ma',
        status:'done',text:'thinking out loud',transport:'fast',
        sources:[],requests:[],prompt_t:9,completion_t:4};
  }
  if(url.endsWith('/conversations/conv-9/exchanges'))
    return{items:[{id:'xch-1',user_msg_id:'mu',assistant_msg_id:'ma',
      status:'done',transport:'fast',sources:[]}]};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_failed_send_clears_optimistic_bubble(self):
        """Send failure is transport-independent: the optimistic user
        bubble appears before the POST lands and is removed when it
        fails, with the honest 'Send failed' note - identical behavior
        whichever transport the conversation would have used."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  nodes.get('ai-text').value='this will fail';
  nodes.get('aichat')._h.click({target:{closest:s=>
    s==='#ai-send'?nodes.get('ai-send'):null}});
  // optimistic bubble renders immediately (before the failed POST)
  assert.match(nodes.get('ai-list').innerHTML,/this will fail/);
  await flush();
  assert.match(nodes.get('ai-note').innerHTML,/Send failed/);
  assert.doesNotMatch(nodes.get('ai-list').innerHTML,/this will fail/);
  assert.equal(window.AskAI._s.sending,false);
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/conversations')&&opt&&opt.method==='POST')
    return{id:'conv-9',client_ref:'w',title:'',model:'gpt-6-luna',
      effort:'low',fast:1,readonly:1,transport:'fast',origin_view:{}};
  if(url.endsWith('/messages'))throw new Error('server exploded');
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)


class ChatHistoryRailTests(unittest.TestCase):
    """The history panel is a floating sibling docked to the chat's
    LEFT edge - not a column inside the card - and its rows carry
    rename/delete/size management."""

    def test_rail_markup_is_a_sibling_not_a_column(self):
        # the rail markup lives outside .ai-main entirely
        main = CHAT_JS.split("'<div class=\"ai-main\">'", 1)[1]
        main = main.split("'<div class=\"ai-compose\">'", 1)[0]
        self.assertNotIn('ai-rail', main)
        # and is emitted as a top-level sibling after the chat card
        status = CHAT_JS.find('id="ai-status"')
        rail = CHAT_JS.find('id="ai-rail"')
        self.assertLess(status, rail)
        css = (STATIC_DIR / "chat.css").read_text(encoding="utf-8")
        # docked to the card's left edge via a fixed position calc
        self.assertRegex(css, r'#ai-rail \{[^}]*position: fixed')
        self.assertRegex(css, r'#ai-rail \{[^}]*right: calc\(18px')
        self.assertIn('#ai-rail[hidden] { display: none; }', css)
        # second header line carries the view chip + usage chip
        self.assertIn('class="ai-sub"', CHAT_JS)
        sub = CHAT_JS.split('class="ai-sub"', 1)[1]
        self.assertIn('id="ai-ctx"', sub.split('</div>', 1)[0])
        self.assertIn('id="ai-usage"', sub.split('</div>', 1)[0])

    def test_settings_opens_instantly_and_caches_catalog(self):
        """Opening settings must not await the /models subprocess call -
        the dialog is visible synchronously and the last catalog is
        reused from localStorage on later opens."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const w=nodes.get('aisetwrap');
  // the dialog is visible BEFORE the /models fetch resolves
  const clicked={target:{closest:s=>s==='#ai-name'
    ?nodes.get('ai-name'):null}};
  nodes.get('aichat')._h.click(clicked);
  assert.equal(w.hidden,false,'dialog opens without awaiting fetch');
  await flush();
  // the fetched catalog was persisted for the next open
  const cached=JSON.parse(store.getItem('ac-ai-catalog'));
  assert(cached.data.models.length,'catalog cached to localStorage');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/models'))return{models:[
    {uid:'m1',family:'m1',label:'M1',in:0.1,out:0.5}]};
  if(url.endsWith('/settings'))return{defaults:{}};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_rail_rows_size_rename_delete(self):
        """Rows show a stored-size estimate and hover ops; rename swaps
        the row for an input that commits via POST /title; delete is a
        two-click arm (no modal in the harness) that POSTs /delete and
        resets the open chat when the current conv is removed."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.rail=true;
  S.conv={id:'conv-1',readonly:1};
  S.convList=[{id:'conv-1',title:'chat one',last_active_at:'2026-01-01',
    model:'m',size_bytes:2048},
    {id:'conv-2',title:'chat two',last_active_at:'2026-01-02',
     model:'m',size_bytes:512}];
  window.AskAI._paint();
  const html=window.AskAI._railHtml();
  assert(html.includes('data-ai-ren="conv-1"'),'rename op rendered');
  assert(html.includes('data-ai-del="conv-2"'),'delete op rendered');
  assert.match(html,/2 KB/,'size estimate shown');
  assert.match(html,/KB/,'KB unit present');
  // the ellipsized sub line is always fully readable on hover
  assert(html.includes('title="chat one\n'),
    'row tooltip carries title + full sub line');
  assert.match(html,/title="[^"]*2 KB[^"]*"/,'tooltip keeps full KB');
  // rename: clicking the pen swaps the row to an input
  nodes.get('ai-rail')._h.click({target:{closest:s=>
    s==='[data-ai-ren]'?{getAttribute:()=>'conv-2'}:null}});
  await flush();
  assert.equal(S.renaming,'conv-2');
  assert(window.AskAI._railHtml().includes('data-ai-renin="conv-2"'));
  // Enter commits -> POST /title, list + current title update
  nodes.get('ai-rail')._h.keydown({key:'Enter',
    preventDefault(){},
    target:{closest:s=>s==='[data-ai-renin]'?{
      getAttribute:()=>'conv-2',value:'new name'}:null}});
  await flush();
  const ren=fetches.find(f=>f.url.endsWith('/title'));
  assert(ren,'POST /title fired');
  assert.equal(JSON.parse(ren.opt.body).title,'new name');
  assert.equal(S.convList[1].title,'new name');
  assert.equal(S.renaming,null);
  // delete: first click arms, second click posts /delete
  await window.AskAI._delConv('conv-1');
  assert(!fetches.some(f=>f.url.endsWith('/delete')),
    'first click only arms');
  await window.AskAI._delConv('conv-1');
  const del=fetches.find(f=>f.url.endsWith('/delete'));
  assert(del,'second click POSTs /delete');
  assert.equal(S.convList.length,1,'row removed from the list');
  assert.equal(S.conv,null,'the open chat was the deleted one - reset');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.endsWith('/title'))return{id:'conv-2',title:'new name'};
  if(url.endsWith('/delete'))return{deleted:'conv-1'};
  return{items:[]};
});
''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_thinking_is_boxless_and_expandable(self):
        """An in-flight reply renders as a bare gray 'thinking' line
        (no bubble) with the honest recorded breadcrumb expandable."""
        p = run_node(
            r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'conv-1',readonly:1};
  S.msgs=[{id:'am1',role:'assistant',text:'',status:'pending',
    attachments:'[]'}];
  S.exchByAid={am1:{id:'x1',status:'running',text:'',
    requests:[{n:1,kind:'fresh'},{n:2,kind:'tool'}],
    sources:[{kind:'run',key:'r1|s1',label:'run alpha'}]}};
  const html=window.AskAI._msgHtml(S.msgs[0]);
  assert.match(html,/aim-body think/,'boxless class applied');
  assert.match(html,/ai-dots">thinking<i>\.<\/i>/,'animated dots');
  assert.match(html,/ai-trace/,'trace details rendered');
  assert.match(html,/looking at run alpha/,'last source summarized');
  assert.match(html,/request 2 \u00b7 tool/,'hop listed on expand');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''')
        self.assertEqual(p.returncode, 0, p.stderr)


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ViewContextTests(unittest.TestCase):
    """View-keyed conversations + the two-line header: launchers only
    open (never minimize) and aim the chat at the newest conversation
    for the view on screen; a fresh chat's about selector can retarget
    until the first send locks it."""

    def test_header_two_line_layout(self):
        """Line 1: plain-text title + new-chat/history/size/minimize.
        Line 2: the metadata chips (model, read-only, about, usage)."""
        head = CHAT_JS.split('class="ai-head"', 1)[1]
        head = head.split("'<div class=\"ai-sub\">'", 1)[0]
        for needle in ('id="ai-title"', 'data-ai-new', 'id="ai-hist"',
                       'id="ai-size"', 'id="ai-min"'):
            self.assertIn(needle, head)
        self.assertNotIn('id="ai-name"', head)
        self.assertNotIn('id="ai-state"', head)
        sub = CHAT_JS.split('class="ai-sub"', 1)[1].split('</div>', 1)[0]
        for needle in ('id="ai-name"', 'id="ai-state"', 'id="ai-ctx"',
                       'id="ai-ctxsel"', 'id="ai-usage"'):
            self.assertIn(needle, sub)
        # the rail's own new-chat button shares the delegated handler
        self.assertIn('id="ai-new" data-ai-new', CHAT_JS)

    def test_launcher_opens_view_scoped_conversation(self):
        """A launcher click never minimizes an open chat, and it lands
        on the newest conversation whose origin matches the live view -
        or a fresh chat pinned to that view when none exists."""
        p = run_node(r'''
(async()=>{
  const dask=nodes.get('dask');
  const click=()=>handlers.click({target:{closest:s=>
    s==='[data-ask-ai]'?dask:null},preventDefault:()=>{}});
  // the agent-pool modal is on screen
  window.aiView=()=>({pool:true,agents:[]});
  click();
  assert.equal(nodes.get('aichat').hidden,false);
  await flush();
  // a second launcher click keeps the chat open (never toggles shut)
  click();
  await flush();
  assert.equal(nodes.get('aichat').hidden,false);
  // it landed on the existing pool conv rather than a fresh chat
  assert(fetches.some(f=>f.url.includes('/conversations/conv-pool')),
    'matched conv loaded');
  assert.equal(window.AskAI._s.conv.id,'conv-pool');
  assert.equal(window.AskAI._s.ctxPin.view,'pool');
  // navigate to a view with no conv -> a fresh chat pins to it
  window.aiView=()=>({detailKey:'r9|build',agents:
    [{run_key:'r9',step:'build'}]});
  click();
  await flush();
  assert.equal(window.AskAI._s.conv,null,'no conv for that view yet');
  assert.equal(window.AskAI._s.ctxPin.view,'agent');
  assert.equal(window.AskAI._s.ctxPin.refs.step,'build');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js=r'''
setRoute((url,opt)=>{
  if(url.includes('/exchanges')||url.includes('/usage')||
     url.includes('/actions')) return {items:[],totals:{}};
  if(url.includes('/conversations/conv-pool'))
    return {conversation:{id:'conv-pool',title:'pool chat',
      origin_view:{view:'pool',refs:{}}},items:[]};
  if(url.includes('/conversations'))
    return {items:[{id:'conv-dash',title:'dash',
        origin_view:{view:'dashboard',refs:{}},last_active_at:2},
      {id:'conv-pool',title:'pool chat',
        origin_view:{view:'pool',refs:{}},last_active_at:1}]};
  return {items:[]};
});''')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_fresh_conv_about_selector_locks_on_first_send(self):
        """Fresh convs get an <select> about control (follow screen /
        pin live view / dashboard); the first user message freezes the
        topic and the slot collapses back to the static chip."""
        p = run_node(r'''
(async()=>{
  const S=window.AskAI._s;
  window.AskAI.open();
  await flush();
  const sel=nodes.get('ai-ctxsel'), chip=nodes.get('ai-ctx');
  window.aiView=()=>({pool:true,agents:[]});
  window.aiSync();
  assert.equal(sel.hidden,false,'fresh chat offers the selector');
  assert.equal(chip.hidden,true,'chip yields to the select');
  assert.match(sel.innerHTML,/follows the screen/);
  // "pin the live view" adopts the agent-pool view
  nodes.get('aichat')._h.change({target:{id:'ai-ctxsel',
    value:'pinlive'}});
  assert.equal(S.ctxPin.view,'pool');
  assert.equal(window.AskAI.ctx().view,'pool','next question pinned');
  // back to follow-the-screen, then an explicit dashboard pin
  nodes.get('aichat')._h.change({target:{id:'ai-ctxsel',
    value:'dash'}});
  assert.equal(S.ctxPin.view,'dashboard');
  nodes.get('aichat')._h.change({target:{id:'ai-ctxsel',
    value:'pinlive'}});
  assert.equal(S.ctxPin.view,'pool');
  // first send: the optimistic user message flips fresh->started, the
  // select goes away and the chip shows the frozen topic
  S.conv={id:'c9',model:'m',effort:'',fast:0,readonly:1};
  S.msgs=[{id:'u1',role:'user',text:'q',ctx_ref:'',
    settings_json:'{}'}];
  window.AskAI._paint();
  assert.equal(sel.hidden,true,'started conv loses the selector');
  assert.equal(chip.hidden,false);
  assert.equal(chip.textContent,'about: agent pool');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_new_chat_inherits_section(self):
        """'+' keeps the outgoing conversation's section (its pin or
        stored origin); with nothing to inherit it defaults to the
        dashboard."""
        p = run_node(r'''
(async()=>{
  window.AskAI.open();
  await flush();
  const S=window.AskAI._s;
  S.conv={id:'c1',model:'m',effort:'',fast:0,readonly:1,
    origin_view:{view:'run',refs:{run_key:'request:abc'}}};
  S.msgs=[{id:'u1',role:'user',text:'q',ctx_ref:'',settings_json:'{}'}];
  S.ctxPin={view:'run',refs:{run_key:'request:abc'}};
  const newBtn={closest:s=>s==='[data-ai-new]'?{getAttribute:()=>'1'}
    :null};
  nodes.get('aichat')._h.click({target:newBtn});
  assert.equal(S.conv,null);
  assert.equal(S.ctxPin.view,'run','new chat inherits the section');
  assert.equal(S.ctxPin.refs.run_key,'request:abc');
  // and with nothing to inherit, the default is the dashboard
  S.ctxPin=null; S.conv=null; S.msgs=[];
  nodes.get('aichat')._h.click({target:newBtn});
  assert.equal(S.ctxPin.view,'dashboard','falls back to dashboard');
})().then(()=>process.exitCode=0).catch(e=>{console.error(e);
  process.exitCode=1;});
''',
            route_js="setRoute(()=>({items:[]}));")
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
