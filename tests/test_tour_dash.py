"""Node-harness tests for the 'What's new' walkthrough in _DASH_HTML.

Same convention as the dashboard's other JS tests: the inline <script>
is lifted out of conductor._DASH_HTML and run inside node:vm against a
stub DOM - no jsdom, no dependencies. Covered: card rendering, the async
badge fed by /api/state's tour_unseen, anchored vs centered (missing
anchor) cards, related run/request chips opening the existing modals,
Mark-viewed persistence-then-advance, Skip/Escape/backdrop dismissal
never POSTing, the viewed-history section, and listener cleanup.

Convention: C:\\Projects\\TESTING.md
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import conductor  # noqa: E402


class TourDashboardTests(unittest.TestCase):
    """Driver.js-style spotlight without the library: #tourring's
    box-shadow is the dim, the card anchors beside the lit element, and
    the whole layer suspends while a details modal is open."""

    _STUB = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8')
  .replace(/\ntick\(\);\s*$/, '');
const mkCls=init=>{const s=new Set((init||'').split(' ').filter(Boolean));
  return {contains:c=>s.has(c),add:c=>s.add(c),remove:c=>s.delete(c),
    toggle:(c,f)=>{const on=f===undefined?!s.has(c):f;
      on?s.add(c):s.delete(c);return on;}};};
const nodes=new Map(), anchors={}, listeners={}, posts=[], fetches=[];
let queue=[], defers=null, activeEl=null;
const mkRect=o=>Object.assign(
  {left:0,top:0,right:0,bottom:0,width:0,height:0},o);
const elem=id=>{
  if(!nodes.has(id)){
    const n={id,innerHTML:'',hidden:true,textContent:'',value:'',style:{},
      attrs:{},dataset:{},disabled:false,scrollTop:0,offsetHeight:0,
      isConnected:true,_rect:null,
      classList:mkCls(''),
      setAttribute(k,v){this.attrs[k]=String(v);},
      getAttribute(k){return this.attrs[k];},
      removeAttribute(k){delete this.attrs[k];},
      querySelector:()=>null,querySelectorAll:()=>[],closest:()=>null,
      matches:()=>false,
      focus(){this.focused=(this.focused||0)+1;activeEl=n;},
      scrollIntoView(){this.scrolled=(this.scrolled||0)+1;},
      getBoundingClientRect(){return this._rect||mkRect();}};
    nodes.set(id,n);}
  return nodes.get(id);};
nodes.get('tour-btn');
// an anchor is any element with a nonzero rect - register it here and
// document.querySelector resolves the stop selector to it
const anchor=(sel,rect)=>{anchors[sel]={getBoundingClientRect:()=>rect,
  scrollIntoView(){this.scrolled=(this.scrolled||0)+1;},
  isConnected:true};};
const document={
  getElementById:elem,
  querySelector:sel=>anchors[sel]||null,
  querySelectorAll:()=>[],
  get activeElement(){return activeEl;},
  body:{classList:mkCls('')},
  addEventListener:(t,h,cap)=>{const k=t+':'+(!!cap);
    (listeners[k]??=[]).push(h);},
  removeEventListener:(t,h,cap)=>{const k=t+':'+(!!cap);
    const i=(listeners[k]||[]).indexOf(h); if(i>=0)listeners[k].splice(i,1);},
  dispatchEvent:()=>true};
const resizeHs=[];
const window={innerWidth:1200,innerHeight:800,
  addEventListener:(t,h)=>{if(t==='resize')resizeHs.push(h);},
  removeEventListener:(t,h)=>{const i=resizeHs.indexOf(h);
    if(i>=0)resizeHs.splice(i,1);},
  getSelection:()=>({isCollapsed:true,toString:()=>''})};
const fetch=(u,opt)=>{fetches.push(u);
  if(opt&&opt.body)posts.push({url:u,body:JSON.parse(opt.body)});
  if(defers)return new Promise(r=>defers.push({u,r}));
  const r=queue.shift()||{ok:true,json:async()=>({}),text:async()=>''};
  return Promise.resolve(r);};
const ctx=vm.createContext({document,window,fetch,confirm:()=>true,
  alert:()=>{},location:{reload:()=>{}},setTimeout:(f)=>0,console});
vm.runInContext(src,ctx);
const DATA={serve_boot:'b',generated_at:'g0',tour_unseen:2,cpu_count:8,
  free_ram_gb:4,agents:[],defaults:{},model_options:{},project_options:[],
  runs:[{run:'req one',key:'request:req1',request_id:'req1',
    counts:{done:1}}]};
const FEED={generated_at:'g1',boundary:{commit:'b',at:'t'},range:{},
  unseen_count:2,warnings:[],
  entries:[
    {id:'area:power:aaa111bbb222',kind:'change',stop:'power',
     stop_title:'Dashboard power',title:'Power <img src=x>',
     blurb:'Restart & stop safely',tag:'improved',anchor:'#power-btn',
     commits:[{sha:'aaa111bbb222ccc',subject:'Improve <b>power</b> dialog'}],
     runs:['request:req1'],requests:['req1'],
     at:'2026-10-17T01:00:00',range:{},state:'new'},
    {id:'area:mixed:ddd444eee555',kind:'change',stop:'mixed',
     stop_title:'Cross-cutting changes',title:'Mixed bag',
     blurb:'assorted',tag:'fixed',anchor:null,
     commits:[],runs:[],requests:[],
     at:'2026-10-17T02:00:00',range:{},state:'new'}],
  history:[{id:'area:tour:fff000aaa111',kind:'change',stop:'tour',
     stop_title:"What's new tour",title:'Tour added',blurb:'old card',
     tag:'new',anchor:null,commits:[],runs:[],requests:[],
     at:'2026-10-16T00:00:00',range:{},state:'viewed',
     viewed_at:'2026-10-16T01:00:00'}]};
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
const click=async t=>{await listeners['click:false'][0](
  {target:t,preventDefault:()=>{}});await flush();};
const key=async k=>{await listeners['keydown:false'][0]({key:k,
  target:{id:'',tagName:'DIV',closest:()=>null,matches:()=>false,
    classList:{contains:()=>false}},
  preventDefault:()=>{}});await flush();};
const tourBtn=ds=>({id:ds==='btn'?'tour-btn':'',dataset:{},
  closest:()=>null});
const act=(a,ds)=>({id:'',dataset:{},
  closest:s=>s==='[data-tour]'?{dataset:Object.assign({tour:a},ds)}:null});
const open=async()=>{await click(tourBtn('btn'));};
const card=()=>elem('tourcard').innerHTML;
'''

    def _node(self, body):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        p = subprocess.run(["node", "-e", self._STUB + body], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_badge_fills_from_state_poll_not_tour_fetch(self):
        """The unread pill is painted from /api/state's tour_unseen on
        every poll - null stays hidden (the async build hasn't landed),
        a count shows, zero hides again, and no /api/tour fetch is ever
        issued just to fill it."""
        self._node(r'''
vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
const b=()=>elem('tour-badge');
assert.equal(b().hidden,false);
assert.equal(b().textContent,'2');
assert.equal(elem('tour-btn').classList.contains('unread'),true);
// null = never computed - hide, don't fake a 0
const d0=Object.assign({},DATA,{tour_unseen:null,generated_at:'g1'});
vm.runInContext('render('+JSON.stringify(d0)+')',ctx);
assert.equal(b().hidden,true);
const d3=Object.assign({},DATA,{tour_unseen:3,generated_at:'g2'});
vm.runInContext('render('+JSON.stringify(d3)+')',ctx);
assert.equal(b().hidden,false);
assert.equal(b().textContent,'3');
const dZ=Object.assign({},DATA,{tour_unseen:0,generated_at:'g3'});
vm.runInContext('render('+JSON.stringify(dZ)+')',ctx);
assert.equal(b().hidden,true);
assert.equal(elem('tour-btn').classList.contains('unread'),false);
assert.ok(!fetches.some(u=>u.includes('/api/tour')));   // badge never GETs the feed
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_card_rendering_anchor_and_escaping(self):
        """A feed entry renders Update i of N, escaped title/blurb/commit
        text, the tag chip, related chips, and an anchored spotlight:
        the ring frames the anchor's rect, the card lands beside it, and
        the wrap's own dim stays off."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  anchor('#power-btn',mkRect({left:980,top:14,width:34,height:28,
    right:1014,bottom:42}));
  queue.push({ok:true,json:async()=>FEED});
  await open();
  assert.equal(elem('tourwrap').hidden,false);
  const h=card();
  assert.match(h,/Update 1 of 2/);
  assert.match(h,/Dashboard power/);
  assert.match(h,/Power &lt;img src=x&gt;/);          // escaped, not markup
  assert.doesNotMatch(h,/<img src=x>/);
  assert.match(h,/Improve &lt;b&gt;power&lt;\/b&gt; dialog/);
  assert.doesNotMatch(h,/<b>power<\/b>/);
  assert.match(h,/class="ttag"[^>]*>improved</);
  assert.match(h,/aaa111bbb2/);                       // short sha
  assert.match(h,/data-run-details="request:req1"/);
  assert.match(h,/data-open-request="req1"/);
  // anchored: ring visible at the padded rect, card .anchored, no wrap dim
  const ring=elem('tourring');
  assert.equal(ring.hidden,false);
  assert.equal(ring.style.left,'972px');              // 980 - pad 8
  assert.equal(ring.style.top,'6px');
  assert.equal(ring.style.width,'50px');              // 34 + 2*8
  assert.equal(ring.style.height,'44px');
  assert.equal(elem('tourwrap').classList.contains('dim'),false);
  assert.equal(elem('tourcard').classList.contains('anchored'),true);
  assert.ok(elem('tourcard').style.top);              // explicit position
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_missing_or_hidden_anchor_centers_card(self):
        """A null anchor, an unresolvable selector, and a zero-rect
        (hidden) element all fall back the same way: wrap dim on, ring
        hidden, card centered (no .anchored, no inline position)."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED});
  await open();
  // card 1 anchors #power-btn but nothing is registered -> centered
  assert.equal(elem('tourring').hidden,true);
  assert.equal(elem('tourwrap').classList.contains('dim'),true);
  assert.equal(elem('tourcard').classList.contains('anchored'),false);
  assert.equal(elem('tourcard').style.top,'');
  // a zero-rect anchor (element present but display:none) falls back too
  anchors['#power-btn']={getBoundingClientRect:()=>mkRect({})};
  await click(act('prev'));                            // noop at idx 0
  assert.equal(elem('tourring').hidden,true);
  // bad selector text must never throw the walkthrough
  const bad=JSON.parse(JSON.stringify(FEED));
  bad.entries[0].anchor='###';
  vm.runInContext('tourSt.feed='+JSON.stringify(bad)+';tourShow()',ctx);
  assert.equal(elem('tourring').hidden,true);
  assert.equal(elem('tourwrap').classList.contains('dim'),true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_mark_viewed_persists_then_advances(self):
        """Mark viewed POSTs first; only a persisted success advances
        the deck and repaints the badge from the response count. A
        failed mark keeps the same card with a retryable error, and
        clearing the deck lands on the caught-up card."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED});
  await open();
  // failed persist: stay on Update 1, error shown, nothing marked
  queue.push({ok:false,json:async()=>({error:'db locked'})});
  await click(act('mark'));
  assert.match(card(),/Mark viewed failed: db locked/);
  assert.match(card(),/Update 1 of 2/);
  assert.equal(posts.length,1);
  // success: badge repaints from unseen_count, deck advances
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',changed:true,
    advanced:false,unseen_count:1})});
  await click(act('mark'));
  assert.deepEqual(posts[1].url,'/api/tour/area%3Apower%3Aaaa111bbb222/viewed');
  assert.equal(elem('tour-badge').textContent,'1');
  // the marked card leaves the deck - '1 of 1' counts what is left,
  // never a card already seen
  assert.match(card(),/Update 1 of 1/);
  assert.match(card(),/Mixed bag/);
  // the viewed card joined the viewed section
  assert.match(card(),/viewed earlier \(2\)/);
  // last mark -> caught up + still no wrap dim issues
  queue.push({ok:true,json:async()=>({id:'y',state:'viewed',changed:true,
    advanced:true,unseen_count:0})});
  await click(act('mark'));
  assert.equal(elem('tour-badge').hidden,true);        // count 0 hides
  assert.match(card(),/All caught up/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_mark_unknown_entry_resyncs_the_deck(self):
        """A card renamed/absorbed since the deck was pulled (a new
        commit re-grouped its area) 404s 'unknown tour entry' on mark -
        a retry could never land, so the tour re-pulls the feed and
        lands on the first still-unviewed card, keeping the failure
        notice for context."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED});
  await open();
  queue.push({ok:false,json:async()=>({
    error:'unknown tour entry: area:power:aaa111bbb222'})});
  const f2=JSON.parse(JSON.stringify(FEED));
  f2.entries=[f2.entries[1]];                       // card 1 renamed away
  f2.unseen_count=1;
  queue.push({ok:true,json:async()=>f2});
  await click(act('mark'));
  assert.equal(fetches.filter(u=>u==='/api/tour').length,2); // open+resync
  assert.match(card(),/Mark viewed failed: unknown tour entry/);
  assert.match(card(),/Update 1 of 1/);              // resynced deck
  assert.match(card(),/Mixed bag/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_skip_escape_backdrop_never_post(self):
        """Skip, Escape and a click on the dim all close the walkthrough
        via the same path - the entry stays unread and no viewed/snooze
        POST ever leaves. Focus returns to the What's-new button and the
        scroll/resize listeners are unbound on close."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const opened=async()=>{queue.push({ok:true,json:async()=>FEED});
    await open(); assert.equal(elem('tourwrap').hidden,false);};
  const scroll0=(listeners['scroll:true']||[]).length,
        resize0=resizeHs.length;
  // Skip
  await opened();
  assert.equal((listeners['scroll:true']||[]).length,scroll0+1);
  assert.equal(resizeHs.length,resize0+1);
  await click(act('skip'));
  assert.equal(elem('tourwrap').hidden,true);
  assert.equal(posts.length,0);
  assert.equal((listeners['scroll:true']||[]).length,scroll0);
  assert.equal(resizeHs.length,resize0);
  assert.equal(elem('tour-btn').focused>0,true);       // focus restored
  // Escape - exactly Skip (still no POST)
  await opened();
  await key('Escape');
  assert.equal(elem('tourwrap').hidden,true);
  assert.equal(posts.length,0);
  // backdrop click lands on #tourwrap itself - same dismissal
  await opened();
  await click({id:'tourwrap',dataset:{},closest:()=>null});
  assert.equal(elem('tourwrap').hidden,true);
  assert.equal(posts.length,0);
  assert.ok(fetches.every(u=>!/\/api\/tour\/.*\/(viewed|snooze)/.test(u)));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_related_chips_open_modals_and_suspend(self):
        """Run/request chips reuse the existing modal openers: the
        details modal takes the screen (.suspended drops the tour layer)
        and closing it resumes the same tour card."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  anchor('#power-btn',mkRect({left:980,top:14,width:34,height:28,
    right:1014,bottom:42}));
  queue.push({ok:true,json:async()=>FEED});
  await open();
  const runChip=ds=>({id:'',dataset:{},closest:s=>
    s==='[data-run-details]'?{dataset:{runDetails:ds}}:null});
  await click(runChip('request:req1'));
  assert.equal(elem('backdrop').hidden,false);         // run modal up
  assert.equal(elem('tourwrap').classList.contains('suspended'),true);
  await click({id:'dx',dataset:{},closest:()=>null});  // close modal
  assert.equal(elem('backdrop').hidden,true);
  assert.equal(elem('tourwrap').classList.contains('suspended'),false);
  assert.match(card(),/Update 1 of 2/);                // same card resumes
  // request chip -> openRequest (GETs the record, modal suspends too)
  queue.push({ok:true,json:async()=>({id:'req1',status:'ready',
    label:'req one',manifest_text:'[run]',proposed_steps:[]})});
  const reqChip=ds=>({id:'',dataset:{},closest:s=>
    s==='[data-open-request]'?{dataset:{openRequest:ds}}:null});
  await click(reqChip('req1'));
  assert.ok(fetches.some(u=>u==='/api/requests/req1'));
  assert.equal(elem('backdrop').hidden,false);
  assert.equal(elem('tourwrap').classList.contains('suspended'),true);
  // Escape while suspended closes the MODAL, the tour survives
  await key('Escape');
  assert.equal(elem('backdrop').hidden,true);
  assert.equal(elem('tourwrap').hidden,false);
  assert.match(card(),/Update 1 of 2/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_viewed_section_reopens_history_and_unview(self):
        """The viewed section lists feed.history behind the watermark;
        reopening one shows its card with Back/mark-unread, and unview
        POSTs then re-pulls the feed so the card is back in the deck."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED});
  await open();
  assert.match(card(),/viewed earlier \(1\)/);
  assert.match(card(),/data-tour="hist" data-hi="0"/);
  await click(act('hist',{hi:'0'}));
  assert.match(card(),/viewed 1 of 1/);
  assert.match(card(),/Tour added/);
  assert.match(card(),/data-tour="unview"/);
  assert.match(card(),/data-tour="back"/);
  await click(act('back'));
  assert.match(card(),/Update 1 of 2/);
  // unview: POST -> fresh count -> feed re-pulled -> entry in the deck
  await click(act('hist',{hi:'0'}));
  queue.push({ok:true,json:async()=>({id:'h',state:'new',changed:true,
    unseen_count:3})});
  const back=JSON.parse(JSON.stringify(FEED));
  back.entries.unshift(JSON.parse(JSON.stringify(back.history[0])));
  back.entries[0].state='new'; back.unseen_count=3; back.history=[];
  queue.push({ok:true,json:async()=>back});
  await click(act('unview'));
  assert.deepEqual(posts.map(p=>p.url),
    ['/api/tour/area%3Atour%3Afff000aaa111/unview']);
  assert.equal(elem('tour-badge').textContent,'3');
  assert.match(card(),/Update 1 of 3/);
  assert.match(card(),/Tour added/);
  assert.match(card(),/Mark viewed/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_loading_error_retry_and_empty_states(self):
        """Opening paints a loading card first; a failed feed GET shows
        the error with a Retry that refetches; an empty feed renders the
        caught-up card (still reachable for the viewed section)."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  defers=[];
  await click(tourBtn('btn'));
  assert.match(card(),/loading the change feed/);      // paint precedes feed
  defers[0].r({ok:false,json:async()=>({error:'boom'})}); await flush();
  defers=null;                                   // back to queue mode
  assert.match(card(),/notice error">boom/);
  queue.push({ok:true,json:async()=>({generated_at:'g',boundary:{},
    range:{},entries:[],history:[],unseen_count:0,warnings:[]})});
  await click(act('retry'));
  assert.match(card(),/All caught up/);
  assert.equal(elem('tourwrap').classList.contains('dim'),true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_snoozed_deck_and_prev_next_browse(self):
        """Snoozed entries still ride the deck with a 'deferred earlier'
        hint (a snooze never counts as read), and prev/next browse
        without marking anything."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const f=JSON.parse(JSON.stringify(FEED));
  f.entries[1].state='snoozed';
  queue.push({ok:true,json:async()=>f});
  await open();
  await click(act('next'));
  assert.match(card(),/Update 2 of 2/);
  assert.match(card(),/deferred earlier/);
  assert.match(card(),/Mark viewed/);                 // still acknowledgeable
  await click(act('prev'));
  assert.match(card(),/Update 1 of 2/);
  assert.equal(posts.length,0);                        // browsing never POSTs
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__ == "__main__":
    unittest.main()
