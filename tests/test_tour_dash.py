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
  assert.match(h,/Tool updates<\/span> · card 1 of 2/);
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
  // failed persist: stay on the same card, error shown, nothing marked
  queue.push({ok:false,json:async()=>({error:'db locked'})});
  await click(act('mark'));
  assert.match(card(),/Mark viewed failed: db locked/);
  assert.match(card(),/card 1 of 2/);
  assert.equal(posts.length,1);
  // success: badge repaints from unseen_count, deck advances
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',changed:true,
    advanced:false,unseen_count:1})});
  await click(act('mark'));
  assert.deepEqual(posts[1].url,'/api/tour/area%3Apower%3Aaaa111bbb222/viewed');
  assert.equal(elem('tour-badge').textContent,'1');
  // the marked card leaves the deck - '1 of 1' counts what is left,
  // never a card already seen
  assert.match(card(),/card 1 of 1/);
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
  assert.match(card(),/card 1 of 1/);              // resynced deck
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
  assert.match(card(),/card 1 of 2/);                // same card resumes
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
  assert.match(card(),/card 1 of 2/);
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
  assert.match(card(),/card 1 of 2/);
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
  assert.match(card(),/card 1 of 3/);
  assert.match(card(),/Tour added/);
  assert.match(card(),/Mark viewed/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_loading_error_retry_and_empty_states(self):
        """Opening paints a loading card first; a failed feed GET shows
        the error with a Retry that refetches; an empty feed lands on
        the deck picker with both sections reading 'all caught up'."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  defers=[];
  await click(tourBtn('btn'));
  assert.match(card(),/loading the change feed/);      // paint precedes feed
  assert.match(card(),/asking the server/);            // stage narration
  defers[0].r({ok:false,json:async()=>({error:'boom'})}); await flush();
  defers=null;                                   // back to queue mode
  assert.match(card(),/notice error">boom/);
  queue.push({ok:true,json:async()=>({generated_at:'g',boundary:{},
    range:{},entries:[],history:[],unseen_count:0,warnings:[]})});
  await click(act('retry'));
  const h=card();
  assert.match(h,/What's new/);
  assert.equal((h.match(/all caught up/g)||[]).length,2);
  assert.match(h,/data-sec="" disabled/);         // nothing to walk
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
  assert.match(card(),/card 2 of 2/);
  assert.match(card(),/deferred earlier/);
  assert.match(card(),/Mark viewed/);                 // still acknowledgeable
  await click(act('prev'));
  assert.match(card(),/card 1 of 2/);
  assert.equal(posts.length,0);                        // browsing never POSTs
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    # a mixed feed: one tool-update + two work-digest cards, deliberately
    # out of section order and with the run event NEWER than the change
    _SPLIT = r'''
const FEED2={generated_at:'g1',boundary:{commit:'b',at:'t'},range:{},
  unseen_count:3,warnings:[],
  entries:[
    {id:'run:2026-r1:2026-10-18T01:00',kind:'run',section:'work',
     section_title:'Work digest',stop:'runs',stop_title:'Runs',
     title:'Run finished: job one',blurb:'desc',tag:'done',
     outcome:'done',anchor:'#runs',
     commits:[],runs:['request:req1'],requests:['req1'],
     completed_at:'2026-10-18T01:00:00',proposed_at:'2026-10-17T20:00:00',
     steps_done:2,steps_total:3,unreviewed:2,
     at:'2026-10-18T01:00:00',range:{},state:'new'},
    {id:'area:power:ccc333ddd444',kind:'change',section:'tool',
     section_title:'Tool updates',stop:'power',
     stop_title:'Dashboard power',title:'Power polish',blurb:'b',
     tag:'improved',anchor:'#power-btn',commits:[],runs:[],requests:[],
     at:'2026-10-17T01:00:00',range:{},state:'new'},
    {id:'request:req9',kind:'request',section:'work',
     section_title:'Work digest',stop:'requests',
     stop_title:'Proposals & requests',title:'New proposal: nine',
     blurb:'d',tag:'ready',anchor:'#runs',commits:[],requests:['req9'],
     runs:['request:req1'],at:'2026-10-17T05:00:00',
     proposed_at:'2026-10-17T05:00:00',range:{},state:'new'}],
  history:[]};
'''

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_split_sections_and_work_card_chrome(self):
        """The deck walks Tool updates first, then the Work digest,
        regardless of interleaved timestamps; each header names its
        section + per-section position and the card edge takes the
        section accent. Work cards paint the filled record-event tag
        (the run's real outcome), the proposed/finished/steps meta, and
        the unreviewed-count chip into the run modal; a run card shows
        ONLY its run chip while a request card adds its own Request chip
        beside the launched Run chip - each labelled by its own record."""
        self._node(self._SPLIT + r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED2});
  await open();
  // two populated decks -> the picker, not a card: each row names its
  // section + remaining count, 'walk everything' is the combined run
  let h=card();
  assert.match(h,/What's new/);
  assert.match(h,/data-sec="tool"><span class="tmh"><b>Tool updates<\/b>/);
  assert.match(h,/tsecname tool">1 card</);
  assert.match(h,/data-sec="work"><span class="tmh"><b>Work digest<\/b>/);
  assert.match(h,/tsecname work">2 cards</);
  assert.match(h,/data-sec="">walk everything</);
  // Work digest deck: each card counts within its own section and the
  // card edge takes the section accent
  await click(act('sec',{sec:'work'}));
  h=card();
  assert.match(h,/Work digest<\/span> · card 1 of 2/);
  assert.match(h,/Run finished: job one/);
  assert.equal(elem('tourcard').classList.contains('sec-work'),true);
  assert.equal(elem('tourcard').classList.contains('sec-tool'),false);
  // event tag: filled (.ev), the actual outcome word
  assert.match(h,/class="ttag ev"[^>]*>done</);
  assert.match(h,/title="run outcome - a record event"/);
  // meta line: proposed + finished + steps
  assert.match(h,/proposed [^<]* · finished [^<]* · 2\/3 steps done/);
  // unreviewed chip -> run modal; never a mark-viewed substitute
  assert.match(h,/class="chip trev" data-run-details="request:req1"/);
  assert.match(h,/>2 unreviewed</);
  // run card shows ONLY the run chip - no request chip link
  assert.match(h,/data-run-details="request:req1"/);
  assert.doesNotMatch(h,/data-open-request/);
  // -> Work digest card 2: the request with a launched run - both chips,
  //   explicitly labelled, each its own identity
  await click(act('next'));
  h=card();
  assert.match(h,/Work digest<\/span> · card 2 of 2/);
  assert.match(h,/class="ttag ev"[^>]*>ready</);
  assert.match(h,/<b class="relk">Request<\/b> req9</);   // own id, not run name
  assert.match(h,/<b class="relk">Run<\/b> req one</);    // the run's name
  assert.match(h,/data-open-request="req9"/);
  assert.match(h,/data-run-details="request:req1"/);
  assert.doesNotMatch(h,/unreviewed/);                    // no count on requests
  // a request chip opens the REQUEST modal, never the run's
  queue.push({ok:true,json:async()=>({id:'req9',status:'ready',
    label:'nine',manifest_text:'[run]',proposed_steps:[]})});
  const reqChip=ds=>({id:'',dataset:{},closest:s=>
    s==='[data-open-request]'?{dataset:{openRequest:ds}}:null});
  await click(reqChip('req9'));
  assert.ok(fetches.some(u=>u==='/api/requests/req9'));
  // back to the picker, then the Tool-updates deck on its own
  await click({id:'dx',dataset:{},closest:()=>null});   // close the modal
  await click(act('menu'));
  h=card();
  assert.match(h,/tsecname tool">1 card</);
  await click(act('sec',{sec:'tool'}));
  h=card();
  assert.match(h,/Tool updates<\/span> · card 1 of 1/);
  assert.match(h,/Power polish/);
  assert.equal(elem('tourcard').classList.contains('sec-tool'),true);
  assert.match(h,/class="ttag"[^>]*>improved</);       // outline change tag
  assert.doesNotMatch(h,/ttag ev/);
  await click(act('next'));            // nav can't leave the deck
  assert.match(card(),/card 1 of 1/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_empty_section_and_deferred_count_no_reinsert(self):
        """A feed with only one populated section walks it cleanly - no
        blank card, no broken nav. A mark whose response carries a
        deferred (null) unseen_count leaves the badge on its last-known
        value, and a re-pulled feed that still contains the acknowledged
        card never reinserts it."""
        self._node(self._SPLIT + r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const onlyWork=Object.assign({},FEED2,
    {entries:[FEED2.entries[0]],unseen_count:1});
  queue.push({ok:true,json:async()=>onlyWork});
  await open();
  let h=card();
  assert.match(h,/Work digest<\/span> · card 1 of 1/);
  assert.doesNotMatch(h,/Tool updates/);
  await click(act('next'));            // nav can't run off the section
  assert.match(card(),/card 1 of 1/);
  // mark viewed with a DEFERRED count: badge keeps the last-known '2',
  // the card still leaves the deck (acknowledgment is local truth)
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',changed:true,
    unseen_count:null})});
  await click(act('mark'));
  assert.equal(elem('tour-badge').textContent,'2');    // no flicker-to-0
  assert.match(card(),/All caught up/);
  // a re-pull serves a STALE feed still listing the acked card - the
  // session acked-set keeps it out of every deck, so the picker lands
  // with both sections reading 'all caught up' (no card is dealt)
  queue.push({ok:true,json:async()=>onlyWork});
  await click(act('retry'));
  h=card();
  assert.match(h,/<h3 id="tourcard-title">What's new<\/h3>/);
  assert.equal((h.match(/all caught up/g)||[]).length,2); // both decks
  assert.doesNotMatch(h,/class="tpos"/);               // no card dealt
  // and the acknowledged card stays reachable through viewed history
  assert.match(h,/viewed earlier \(1\)/);
  assert.match(h,/Run finished: job one/);             // listed, not dealt
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_tool_only_feed_walks_single_section(self):
        """A feed with only Tool updates walks cleanly: no Work digest
        header, no blank card, and 'next' can't leave the section."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const toolOnly={generated_at:'g',boundary:{},range:{},
    unseen_count:2,warnings:[],history:[],entries:[
    {id:'area:power:aaa111bbb222',kind:'change',section:'tool',
     section_title:'Tool updates',stop:'power',stop_title:'Dashboard power',
     title:'Power <img>',blurb:'b',tag:'improved',anchor:'#power-btn',
     commits:[],runs:[],requests:[],at:'2026-10-17T01:00:00',range:{},
     state:'new'},
    {id:'area:mixed:ddd444eee555',kind:'change',section:'tool',
     section_title:'Tool updates',stop:'mixed',
     stop_title:'Cross-cutting changes',title:'Mixed',blurb:'b',
     tag:'fixed',anchor:null,commits:[],runs:[],requests:[],
     at:'2026-10-17T02:00:00',range:{},state:'new'}]};
  queue.push({ok:true,json:async()=>toolOnly});
  await open();
  assert.match(card(),/Tool updates<\/span> · card 1 of 2/);
  assert.doesNotMatch(card(),/Work digest/);
  await click(act('next'));
  assert.match(card(),/Tool updates<\/span> · card 2 of 2/);
  await click(act('next'));          // at the end - stays put
  assert.match(card(),/card 2 of 2/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_section_fallback_from_id_prefix(self):
        """Cards served without a section stamp (a feed predating the
        split) still partition by id prefix - record ids into the Work
        digest, the rest into Tool updates - and viewed-history rows
        label their section the same way."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const f={generated_at:'g',boundary:{},range:{},unseen_count:2,
    warnings:[],entries:[
    {id:'run:2026-r1:2026-10-18T01:00:00',kind:'run',stop:'runs',
     stop_title:'Runs',title:'Run finished: x',blurb:'b',tag:'done',
     outcome:'done',anchor:'#runs',commits:[],runs:['request:req1'],
     requests:['req1'],completed_at:'2026-10-18T01:00:00',
     steps_done:3,steps_total:3,unreviewed:0,
     at:'2026-10-18T01:00:00',range:{},state:'new'},
    {id:'area:power:aaa111bbb222',kind:'change',stop:'power',
     stop_title:'Dashboard power',title:'Power',blurb:'b',
     tag:'improved',anchor:'#power-btn',commits:[],runs:[],requests:[],
     at:'2026-10-17T01:00:00',range:{},state:'new'}],
    history:[{id:'followup:fu1',kind:'followup',stop:'requests',
     stop_title:'Proposals & requests',title:'Follow-up proposal: old',
     blurb:'b',tag:'ready',anchor:null,commits:[],requests:['fu1'],
     runs:[],at:'2026-10-16T00:00:00',range:{},state:'viewed',
     viewed_at:'2026-10-16T01:00:00'}]};
  queue.push({ok:true,json:async()=>f});
  await open();
  // no section fields at all: the picker still splits both decks by id
  // prefix - record ids into the Work digest, the rest into Tool
  // updates
  let h=card();
  assert.match(h,/tsecname tool">1 card</);
  assert.match(h,/tsecname work">1 card</);
  // walk everything keeps the old order: the run card lands in Work
  // digest AFTER the tool card even though its stamp is newer
  await click(act('sec',{sec:''}));
  assert.match(card(),/Tool updates<\/span> · card 1 of 1/);
  await click(act('next'));
  assert.match(card(),/Work digest<\/span> · card 1 of 1 · Runs/);
  // the viewed-history row labels its section the same way
  assert.match(card(),/viewed earlier \(1\)/);
  assert.match(card(),/<b class="tsecname work">Work digest<\/b>/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_reopen_after_partial_review_shows_only_unread(self):
        """Marking one card and reopening the tour deals only the
        unread remainder: the re-pulled feed moved the acknowledged
        card to 'viewed earlier', the section counters count only the
        deck, and the viewed card still reopens from history."""
        self._node(self._SPLIT + r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED2});
  await open();
  // two live decks -> the picker; enter the Tool-updates deck and mark
  // its card - the picker then shows that deck caught up
  await click(act('sec',{sec:'tool'}));
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',
    changed:true,unseen_count:2})});
  await click(act('mark'));
  assert.match(card(),/All caught up/);
  assert.match(card(),/nothing left in Tool updates/);
  await click(act('menu'));
  assert.match(card(),/tsecname tool">all caught up</);
  assert.match(card(),/tsecname work">2 cards</);
  await click(act('sec',{sec:'work'}));
  assert.match(card(),/Work digest<\/span> · card 1 of 2/);
  await click(act('skip'));
  assert.equal(elem('tourwrap').hidden,true);
  // reopen: the post-mark feed serves the two unread cards and the
  // acknowledged one as viewed history - FEED2 itself was mutated by
  // the mark (the deck splice + history unshift write back through
  // tourSt.feed), so a clone of it IS the post-mark feed shape
  const f2=JSON.parse(JSON.stringify(FEED2));
  f2.unseen_count=2;
  queue.push({ok:true,json:async()=>f2});
  await open();
  const h=card();
  assert.match(h,/Work digest<\/span> · card 1 of 2/);
  assert.doesNotMatch(h,/card 1 of 3/);              // unread-only deck
  assert.match(h,/viewed earlier \(1\)/);
  assert.match(h,/Power polish/);                    // listed, not dealt
  await click(act('hist',{hi:'0'}));
  assert.match(card(),/viewed 1 of 1/);
  assert.match(card(),/Power polish/);
  await click(act('back'));
  assert.match(card(),/Work digest<\/span> · card 1 of 2/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_unknown_outcome_and_zero_unreviewed(self):
        """An outcome word the palette doesn't list still prints
        verbatim (color defaults grey) - the chip never lies about the
        event; unreviewed:0 renders the muted 'all reviewed' chip still
        wired to the run modal."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const f={generated_at:'g',boundary:{},range:{},unseen_count:2,
    warnings:[],history:[],entries:[
    {id:'run:2026-r1:t',kind:'run',section:'work',
     section_title:'Work digest',stop:'runs',stop_title:'Runs',
     title:'Run finished: x',blurb:'b',tag:'melted',outcome:'melted',
     anchor:'#runs',commits:[],runs:['request:req1'],requests:['req1'],
     completed_at:'2026-10-18T01:00:00',steps_done:3,steps_total:3,
     unreviewed:0,at:'2026-10-18T01:00:00',range:{},state:'new'},
    {id:'run:2026-r2:t',kind:'run',section:'work',
     section_title:'Work digest',stop:'runs',stop_title:'Runs',
     title:'Run finished: y',blurb:'b',tag:'failed',outcome:'failed',
     anchor:'#runs',commits:[],runs:['request:req1'],requests:[],
     completed_at:'2026-10-18T02:00:00',steps_done:1,steps_total:2,
     unreviewed:1,at:'2026-10-18T02:00:00',range:{},state:'new'}]};
  queue.push({ok:true,json:async()=>f});
  await open();
  const h=card();
  assert.match(h,/class="ttag ev"[^>]*>melted</);       // verbatim word
  assert.match(h,/style="color:#8b949e;border-color:#8b949e/); // default
  assert.match(h,/class="chip trev ok" data-run-details="request:req1"/);
  assert.match(h,/>all reviewed</);
  assert.match(h,/every finished task in this run has been reviewed/);
  await click(act('next'));
  const h2=card();
  assert.match(h2,/class="chip trev" data-run-details/);
  assert.match(h2,/>1 unreviewed</);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_partial_outcome_tag_renders_amber_in_work_deck(self):
        """A mostly-done run's 'partial' word rides the same FILLED
        event chip as done/failed - amber #d29922, verbatim text, the
        'run outcome' title - inside the Work digest deck, which keeps
        its own position counter and section chrome; the tool deck and
        its outline change tag are untouched."""
        self._node(self._SPLIT + r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const f=JSON.parse(JSON.stringify(FEED2));
  // the workflow-ux-safety shape: 29/30 steps clean + 1 lost -> partial
  f.entries[0]=Object.assign({},f.entries[0],{
    title:'Run finished: workflow-ux-safety',
    tag:'partial',outcome:'partial',steps_done:29,steps_total:30});
  queue.push({ok:true,json:async()=>f});
  await open();
  // sectioning unchanged: same two decks, same counts
  let h=card();
  assert.match(h,/tsecname tool">1 card</);
  assert.match(h,/tsecname work">2 cards</);
  await click(act('sec',{sec:'work'}));
  h=card();
  assert.match(h,/Work digest<\/span> · card 1 of 2/);
  assert.equal(elem('tourcard').classList.contains('sec-work'),true);
  // the partial word: verbatim, amber, FILLED - never red 'failed'
  assert.match(h,/class="ttag ev"[^>]*>partial</);
  assert.match(h,/style="color:#d29922;border-color:#d29922/);
  assert.match(h,/background:#d2992226/);
  assert.match(h,/title="run outcome - a record event"/);
  assert.doesNotMatch(h,/>failed</);
  assert.match(h,/29\/30 steps done/);
  // the rest of the deck is untouched: request card + its tag
  await click(act('next'));
  h=card();
  assert.match(h,/Work digest<\/span> · card 2 of 2/);
  assert.match(h,/class="ttag ev"[^>]*>ready</);
  // and the tool card keeps its own outline change tag
  await click(act('menu'));
  await click(act('sec',{sec:'tool'}));
  h=card();
  assert.match(h,/Tool updates<\/span> · card 1 of 1/);
  assert.match(h,/class="ttag"[^>]*>improved</);
  assert.doesNotMatch(h,/ttag ev/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_unlaunched_request_shows_no_run_chip(self):
        """A proposal no run ever launched links ONLY its own request
        chip - no fabricated run chip, and (being the only chip) no
        Request/Run prefix labels."""
        self._node(self._SPLIT + r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  const f=JSON.parse(JSON.stringify(FEED2));
  f.entries=[Object.assign({},FEED2.entries[2],
    {id:'request:req8',requests:['req8'],runs:[],
     title:'New proposal: eight'})];
  f.unseen_count=1;
  queue.push({ok:true,json:async()=>f});
  await open();
  const h=card();
  assert.match(h,/data-open-request="req8"/);
  assert.doesNotMatch(h,/data-run-details/);
  assert.doesNotMatch(h,/relk/);                     // single chip: plain
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_chip_identities_request_vs_run(self):
        """When a launched run shares the request's dashboard key, the
        Request chip still shows the REQUEST's own name (the staged
        proposal row's label) while the Run chip prefers the real run
        row - two records, two identities, two modal targets."""
        self._node(self._SPLIT + r'''
(async()=>{
  const d2=Object.assign({},DATA,{runs:[
    {run:'proposal label',key:'request:req9',request_id:'req9',
     stage:'ready'},
    {run:'the finished run',key:'request:req9',request_id:'req9',
     counts:{done:1}}]});
  vm.runInContext('render('+JSON.stringify(d2)+')',ctx);
  const f=JSON.parse(JSON.stringify(FEED2));
  f.entries=[Object.assign({},FEED2.entries[2],
    {runs:['request:req9']})];          // the launch shares the key
  f.unseen_count=1;
  queue.push({ok:true,json:async()=>f});
  await open();
  const h=card();
  // Request chip: the proposal's own identity, never the run's name
  assert.match(h,/<b class="relk">Request<\/b> proposal label</);
  assert.doesNotMatch(h,/Request<\/b> the finished run/);
  // Run chip: prefers the non-stage row sharing the key
  assert.match(h,/<b class="relk">Run<\/b> the finished run</);
  assert.match(h,/data-open-request="req9"/);
  assert.match(h,/data-run-details="request:req9"/);
  // the request chip opens the REQUEST modal (GETs the record)
  queue.push({ok:true,json:async()=>({id:'req9',status:'ready',
    label:'nine',manifest_text:'[run]',proposed_steps:[]})});
  const reqChip=ds=>({id:'',dataset:{},closest:s=>
    s==='[data-open-request]'?{dataset:{openRequest:ds}}:null});
  await click(reqChip('req9'));
  assert.ok(fetches.some(u=>u==='/api/requests/req9'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_pipeline_card_followups_anchor_and_modal(self):
        """A pipeline card renders the run chip only (its covered
        request never gets a duplicate chip), the folded follow-up's
        own .tfu row (request chip + run link + stamp), the runs-table
        spotlight anchor, and the run modal via its unreviewed chip."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  anchor('#runs',mkRect({left:8,top:300,width:900,height:200,
    right:908,bottom:500}));
  const f={generated_at:'g',boundary:{},range:{},unseen_count:1,
    warnings:[],history:[],entries:[
    {id:'run:2026-r1:t',kind:'pipeline',section:'work',
     section_title:'Work digest',stop:'runs',stop_title:'Runs',
     title:'Run finished: pipe',blurb:'b',tag:'done',outcome:'done',
     anchor:'#runs',commits:[],runs:['request:req1'],requests:['req1'],
     covers:['request:req1'],proposed_at:'2026-10-17T20:00:00',
     completed_at:'2026-10-18T01:00:00',steps_done:3,steps_total:3,
     unreviewed:1,
     followups:[{id:'followup:fu9',label:'fu label nine',
       at:'2026-10-18T02:00:00',requests:['fu9'],
       runs:['request:req1']}],
     at:'2026-10-18T01:00:00',range:{},state:'new'}]};
  queue.push({ok:true,json:async()=>f});
  await open();
  const h=card();
  assert.match(h,/Work digest<\/span> · card 1 of 1 · Runs/);
  // the covered proposal earns no duplicate request chip on the card
  assert.doesNotMatch(h,/data-open-request="req1"/);
  assert.match(h,/data-run-details="request:req1"/);
  // the folded follow-up renders its own row + identity
  assert.match(h,/follow-ups/);
  assert.match(h,/class="tfu"/);
  assert.match(h,/data-open-request="fu9"/);
  assert.match(h,/fu label nine/);
  assert.match(h,/Run<\/b> req one/);
  // anchored to the runs table, not centered
  assert.equal(elem('tourring').hidden,false);
  assert.equal(elem('tourcard').classList.contains('anchored'),true);
  // the unreviewed chip opens the run modal, suspending the tour
  const runChip=ds=>({id:'',dataset:{},closest:s=>
    s==='[data-run-details]'?{dataset:{runDetails:ds}}:null});
  await click(runChip('request:req1'));
  assert.equal(elem('backdrop').hidden,false);
  assert.equal(elem('tourwrap').classList.contains('suspended'),true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_loading_progress_narrates_the_build(self):
        """The feed protocol IS the progress channel: a 'partial'
        snapshot keeps the client polling /api/tour, paints elapsed
        time + done/pending group marks (never a fabricated percent),
        keeps the first cards usable while later groups land, and stops
        polling when the complete snapshot arrives."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  // give the tour a live interval: capture the snapshot-poll callback
  // (globalThis inside the vm IS ctx, so this lands where we can call it)
  vm.runInContext(
    'setInterval=f=>{globalThis.__iv=f;return 7};' +
    'clearInterval=i=>{globalThis.__cl=i}',ctx);
  defers=[];                              // every fetch now pends
  const p=open();                         // /api/tour is mid-flight
  await flush();
  assert.match(card(),/loading the change feed/);
  assert.match(card(),/asking the server/);
  assert.match(card(),/not an\s+agent task/);   // honesty footnote
  // the first snapshot is PARTIAL - the tool card is already usable
  // and the loading line narrates the groups still publishing
  const p1={generated_at:'g',boundary:{},range:{},seq:1,partial:true,
    unseen_count:1,warnings:[],history:[],entries:[
    {id:'area:power:aaa111bbb222',kind:'change',section:'tool',
     section_title:'Tool updates',stop:'power',
     stop_title:'Dashboard power',title:'Power <img>',blurb:'b',
     tag:'improved',anchor:'#power-btn',commits:[],runs:[],requests:[],
     at:'2026-10-17T01:00:00',range:{},state:'new'}],
    loading:{busy:true,elapsed_s:2.5,stage:'loading run records',
      groups:{collect:'done',tool:'done',work:'running',
              history:'pending'},
      sources:{commits:'ok',runs:'ok',requests:'pending'}}};
  defers.find(d=>d.u==='/api/tour').r({ok:true,json:async()=>p1});
  await p; await flush();
  let h=card();
  assert.match(h,/Tool updates<\/span> · card 1 of 1/);
  assert.match(h,/2\.5s/);
  assert.match(h,/sources done/);
  assert.match(h,/tool updates done/);
  assert.match(h,/work digest \u2026|work digest \.\.\./);
  assert.match(h,/viewed history pending/);
  // nav + mark are live while the rest of the feed builds
  assert.match(h,/data-tour="mark"/);
  // the poll fires: it re-GETs /api/tour - no separate progress probe
  ctx.__iv(); await flush();
  assert.ok(defers.filter(d=>d.u==='/api/tour').length>=2,
    'the poll must re-issue GET /api/tour');
  assert.ok(!defers.some(d=>d.u==='/api/tour/progress'));
  // the next snapshot merges the work group in place - the tool card's
  // spot is kept (no selection reset), the new card appends
  const p2=Object.assign({},p1,{seq:2,
    entries:p1.entries.concat([
      {id:'request:req9',kind:'request',section:'work',
       section_title:'Work digest',stop:'requests',
       stop_title:'Proposals & requests',title:'New proposal: nine',
       blurb:'d',tag:'ready',anchor:'#runs',commits:[],runs:[],
       requests:['req9'],at:'2026-10-17T05:00:00',
       proposed_at:'2026-10-17T05:00:00',range:{},state:'new'}]),
    loading:{busy:true,elapsed_s:3.1,stage:'rebuilding viewed history',
      groups:{collect:'done',tool:'done',work:'done',
              history:'running'},
      sources:{commits:'ok',runs:'ok',requests:'ok',curated:'ok'}}});
  defers[defers.length-1].r({ok:true,json:async()=>p2});
  await flush();
  h=card();
  assert.match(h,/Tool updates<\/span> · card 1 of 1/);   // still on it
  assert.match(h,/3\.1s/);                              // elapsed advances
  assert.match(h,/work digest done/);
  // a repeat seq is a no-op (stale publications never reapply)
  ctx.__iv(); await flush();
  defers[defers.length-1].r({ok:true,json:async()=>p2});
  await flush();
  // the complete snapshot lands: the poll interval is cleared
  ctx.__iv(); await flush();
  const fin=Object.assign({},p2,{seq:3,partial:false,
    unseen_count:2,loading:{busy:false}});
  defers[defers.length-1].r({ok:true,json:async()=>fin});
  await flush();
  assert.equal(ctx.__cl,7);
  h=card();
  assert.doesNotMatch(h,/still loading|elapsed_s/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_partial_shell_and_acked_card_never_reinserts(self):
        """An empty partial shell lands on the picker with 'loading'
        rows (not a fake 'all caught up'); a card marked viewed while
        later groups still publish is never reinserted by a stale
        partial snapshot that still lists it - tourAcked filters every
        apply, and the card waits for the user in viewed history."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  vm.runInContext(
    'setInterval=f=>{globalThis.__iv=f;return 7};' +
    'clearInterval=i=>{globalThis.__cl=i}',ctx);
  defers=[];
  const p=open();
  await flush();
  // snapshot 1: an empty shell - the build hasn't published content
  const shell={generated_at:'g',boundary:{},range:{},seq:1,
    partial:true,unseen_count:null,warnings:[],entries:[],history:[],
    loading:{busy:true,elapsed_s:0.4,stage:'reading commit log',
      groups:{collect:'running',tool:'pending',work:'pending',
              history:'pending'},sources:{}}};
  defers.find(d=>d.u==='/api/tour').r({ok:true,json:async()=>shell});
  await p; await flush();
  let h=card();
  assert.match(h,/What's new/);                  // picker, not a card
  assert.equal((h.match(/loading&hellip;/g)||[]).length,2);
  assert.doesNotMatch(h,/all caught up/);        // never lies 'empty'
  // snapshot 2: the tool group lands - auto-walks its single deck
  const tool={id:'area:power:aaa111bbb222',kind:'change',section:'tool',
    section_title:'Tool updates',stop:'power',
    stop_title:'Dashboard power',title:'Power',blurb:'b',
    tag:'improved',anchor:'#power-btn',commits:[],runs:[],requests:[],
    at:'2026-10-17T01:00:00',range:{},state:'new'};
  ctx.__iv(); await flush();
  defers[defers.length-1].r({ok:true,json:async()=>
    Object.assign({},shell,{seq:2,entries:[tool],
      loading:Object.assign({},shell.loading,{elapsed_s:1.2,
        groups:{collect:'done',tool:'done',work:'running',
                history:'pending'}})})});
  await flush();
  assert.match(card(),/Tool updates<\/span> · card 1 of 1/);
  // mark it viewed NOW - mid-build; the POST returns a deferred count
  await click(act('mark'));
  defers.find(d=>d.u.includes('/viewed')).r(
    {ok:true,json:async()=>({id:tool.id,state:'viewed',
      changed:true,unseen_count:null})});
  await flush();
  assert.match(card(),/All caught up|What's new/);
  // snapshot 3 (stale): still lists the acked card + lands a work card
  // - the session acked-set must keep the marked one out
  const work={id:'request:req9',kind:'request',section:'work',
    section_title:'Work digest',stop:'requests',
    stop_title:'Proposals & requests',title:'New proposal: nine',
    blurb:'d',tag:'ready',anchor:'#runs',commits:[],runs:[],
    requests:['req9'],at:'2026-10-17T05:00:00',
    proposed_at:'2026-10-17T05:00:00',range:{},state:'new'};
  ctx.__iv(); await flush();
  defers[defers.length-1].r({ok:true,json:async()=>
    Object.assign({},shell,{seq:3,entries:[tool,work],
      loading:Object.assign({},shell.loading,{elapsed_s:2.0,
        groups:{collect:'done',tool:'done',work:'done',
                history:'running'}})})});
  await flush();
  h=card();
  assert.doesNotMatch(h,/class="tpos"/);         // no card dealt back
  await click(act('menu'));
  h=card();
  assert.match(h,/tsecname work">1 card</);      // work card landed
  await click(act('sec',{sec:'work'}));
  assert.match(card(),/New proposal: nine/);
  // the complete lands: acked card sits in viewed history, never dealt
  ctx.__iv(); await flush();
  defers[defers.length-1].r({ok:true,json:async()=>
    Object.assign({},shell,{seq:4,partial:false,unseen_count:1,
      entries:[work],history:[tool],
      loading:{busy:false}})});
  await flush();
  h=card();
  assert.match(h,/viewed earlier \(1\)/);
  assert.doesNotMatch(h,/<h3 id="tourcard-title">Power<\/h3>/);
  assert.match(h,/New proposal: nine/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_work_card_spotlights_the_run_row(self):
        """A Work digest card's spotlight is the dashboard row it
        describes: tourAnchorEl prefers the tr[data-run-key] row over
        the card's generic stop anchor, so the ring hugs the row the
        update is about."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  // the generic stop AND a live row both resolve - the row wins
  anchor('#runs',mkRect({left:8,top:300,width:900,height:200,
    right:908,bottom:500}));
  const row={dataset:{runKey:'request:req1'},isConnected:true,
    classList:mkCls('runrow'),scrolled:0,
    scrollIntoView(){this.scrolled++;},
    getBoundingClientRect:()=>mkRect(
      {left:10,top:780,width:900,height:24,right:910,bottom:804})};
  document.querySelectorAll=sel=>
    sel.indexOf('data-run-key')>=0?[row]:[];
  const f={generated_at:'g',boundary:{},range:{},unseen_count:1,
    warnings:[],history:[],entries:[
    {id:'run:2026-r9:t',kind:'pipeline',section:'work',
     section_title:'Work digest',stop:'runs',stop_title:'Runs',
     title:'Run finished: nine',blurb:'b',tag:'done',outcome:'done',
     anchor:'#runs',commits:[],runs:['request:req1'],requests:['req1'],
     covers:['request:req1'],proposed_at:'2026-10-17T20:00:00',
     completed_at:'2026-10-18T01:00:00',steps_done:2,steps_total:2,
     unreviewed:0,at:'2026-10-18T01:00:00',range:{},state:'new'}]};
  queue.push({ok:true,json:async()=>f});
  await open();
  // the ring hugs the ROW's rect (780-804, peeking below the fold),
  // not the table's (300-500): reveal scrolled the row into view
  const ring=elem('tourring');
  assert.equal(ring.hidden,false);
  assert.equal(ring.style.top,'772px');       // 780 - pad
  assert.equal(ring.style.height,'40px');     // 24 + pad*2
  assert.equal(row.scrolled,1);               // reveal scrolls to it
  assert.equal(elem('tourcard').classList.contains('anchored'),true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_card_parks_beside_a_tall_anchor(self):
        """A clamped card must never sit on the thing it lights: when
        the anchor fills the viewport's middle (neither above nor below
        fits), the card parks beside the ring instead of covering it."""
        self._node(r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  // tall mid-viewport anchor: below needs ~1020px, above ~-220px,
  // both clamp inside the anchor's own span - the left lane is free
  anchor('#power-btn',mkRect({left:600,top:100,width:200,height:600,
    right:800,bottom:700}));
  queue.push({ok:true,json:async()=>FEED});
  await open();
  const c=elem('tourcard');
  assert.equal(c.classList.contains('anchored'),true);
  // parked left of the ring: left = anchor.left - gap - cardWidth
  assert.equal(c.style.left,'148px');         // 600 - 12 - 440
  assert.equal(c.style.top,'100px');          // aligned to anchor top
  // and the ring still hugs the anchor itself
  assert.equal(elem('tourring').hidden,false);
  assert.equal(elem('tourring').style.left,'592px');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_walk_everything_and_end_of_deck_returns_to_menu(self):
        """'walk everything' covers both decks in feed order; marking
        the last card lands on the deck's own caught-up screen, and its
        '← Decks' link returns to the picker - never a silent hop into
        another deck the user didn't pick."""
        self._node(self._SPLIT + r'''
(async()=>{
  vm.runInContext('render('+JSON.stringify(DATA)+')',ctx);
  queue.push({ok:true,json:async()=>FEED2});
  await open();
  // the picker: both decks with counts
  assert.match(card(),/tsecname tool">1 card</);
  assert.match(card(),/tsecname work">2 cards</);
  // 'walk everything' (data-sec '') walks tool first, then work
  await click(act('sec',{sec:''}));
  assert.match(card(),/Tool updates<\/span> · card 1 of 1/);
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',
    changed:true,unseen_count:2})});
  await click(act('mark'));
  assert.match(card(),/Work digest<\/span> · card 1 of 2/);
  // single-deck flow: work only; its last mark lands on caught-up
  await click(act('menu'));
  await click(act('sec',{sec:'work'}));
  assert.match(card(),/Work digest<\/span> · card 1 of 2/);
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',
    changed:true,unseen_count:1})});
  await click(act('mark'));
  assert.match(card(),/Work digest<\/span> · card 1 of 1/);
  queue.push({ok:true,json:async()=>({id:'x',state:'viewed',
    changed:true,unseen_count:0})});
  await click(act('mark'));
  assert.match(card(),/All caught up/);
  assert.match(card(),/nothing left in Work digest/);
  // '← Decks' returns to the picker with both decks now empty
  await click(act('menu'));
  assert.match(card(),/tsecname tool">all caught up</);
  assert.match(card(),/tsecname work">all caught up</);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
