/* Shared shell: navigation and helpers. Every page includes this first, inside <body>, so the header is in the document before the first paint. */
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const J=async(u,opt)=>{const r=await fetch(u,opt);if(!r.ok)throw new Error(await r.text());return r.json()};
const POST=(u,body)=>J(u,{method:'POST',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
const fmtd=d=>d?String(d).slice(0,10):'';
/* what a page had open, kept in this browser until the page clears it; storage can be absent or full, so every call is guarded */
const mem={get:k=>{try{return JSON.parse(localStorage.getItem(k))}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}},del:k=>{try{localStorage.removeItem(k)}catch(e){}}};
const TOKRE=/\[(PERSON|ORG|PHONE|EMAIL|ADDRESS|FINANCIAL|FAMILY|NATIONALITY|DATE_OF_BIRTH|ID_NUMBER)_\d{4}\]/g;
const NAV=[["/network","Network"],["/research","Research"],["/documents","Review"]], GOV=[["/compliance","Compliance"]];
/* internal names -> words an analyst reads */
const PLAIN={presidio:"rules engine",claude:"model","claude+presidio":"both detectors",
  leaked_value:"a detected value survived",contact_pattern:"looks like a contact detail",ner_person:"rules engine: possible name",model_name:"second model: possible name",model_contact:"second model: possible contact detail",
  delete_raw:"raw file deleted",subject_lookup:"subject lookup",expire_vault:"vault rows expired",erase:"erasure",manual_redaction:"manual redaction",confirm:"span kept redacted",restore:"span restored",confirmed:"identity confirmed",rejected:"identity rejected"};
const plain=k=>PLAIN[k]||k;
const KIND={source:'src',mentioned:'men',private:'priv',contributor:'con'};
function shell(){
  document.title='Atlas · '+document.title;
  const here=location.pathname, link=([h,l])=>`<a href="${h}" class="${here.startsWith(h)?'on':''}" data-h="${h}">${l}</a>`;
  document.body.insertAdjacentHTML('afterbegin',`<header><a class="brand lock" href="/" title="Atlas"><img src="/static/atlas-mark.svg" alt="" height=24><span>Atlas</span></a><nav>${NAV.map(link).join('')}</nav>
  <nav class=sec>${GOV.map(link).join('')}</nav><details class=acct id=acct></details></header>`);
  /* the server inlines the signed-in user and the review queue into the page (window.ATLAS), so the header is complete at first paint; the fetches are the fallback */
  const G=window.ATLAS||{};
  (G.me?Promise.resolve(G.me):J('/api/me')).then(m=>{const name=m.name||m.upn||'',words=name.split(/\s+/);const init=words.map(w=>(w.replace(/[^A-Za-z]/g,'')[0]||'')).join('').slice(0,2).toUpperCase();
    $('#acct').innerHTML=`<summary title="signed in as ${esc(m.upn||'')}">${m.photo?'<img class=av src="/api/me/photo" alt="">':'<span class=av>'+esc(init)+'</span>'}<span>${esc(words[0])}</span></summary>
    <div class=menu><b>${esc(name)}</b><div class="xs muted">${esc(m.upn||'')}</div>${m.auth?'<a href="/auth/logout">Sign out</a>':'<div class="xs muted" style="margin-top:6px">development sign-in, no sign-out</div>'}</div>`;
    document.addEventListener('click',e=>{if(!$('#acct').contains(e.target))$('#acct').open=false})}).catch(()=>{});
  (G.queue!=null?Promise.resolve({queue:{review:G.queue}}):J('/api/home')).then(s=>{if(s.queue&&s.queue.review){const a=$('header nav a[data-h="/documents"]');a.insertAdjacentHTML('beforeend',`<span class=n title="documents waiting for a person">${s.queue.review}</span>`)}}).catch(()=>{});
}
/* reveal one token's value from the vault: a reason is required and every reveal is logged with the signed-in name; null when declined or refused */
async function reveal(tok,docId){const reason=prompt('Reason for revealing '+tok+' (logged with your name):');if(!reason)return null;
  let r;try{r=await POST('/api/vault/reveal?doc_id='+encodeURIComponent(docId)+'&token='+encodeURIComponent(tok)+'&reason='+encodeURIComponent(reason))}catch(e){toast('Reveal failed: '+e.message.slice(0,120),false);return null}
  if(r.error){toast(r.error+' (no vault entry for '+tok+' in this document)',false);return null}
  toast(tok+' = '+r.value+' (vault entry expires '+fmtd(r.expires)+'; access logged)');return r}
/* tokens -> readable chips with the who-is-who legend */
function tokenise(text,people,onclick,hits){const L={};(people||[]).forEach(p=>L[p.token]=p);
  return (hits?markHits(text,hits):esc(text)).replace(TOKRE,m=>{const p=L[m];const k=p?(KIND[p.kind]||''):'';
    const label=p&&p.kind!=='private'&&p.descriptor?`${m} <span class=xs>${esc(p.descriptor.slice(0,40))}</span>`:m;
    return `<span class="tok ${k}" title="${p?esc((p.kind==='private'?'private individual, redacted':p.kind+(p.descriptor?': '+p.descriptor:''))+(p.relationship?' ('+p.relationship+')':'')):m}" ${onclick?`onclick="${onclick}('${m}')"`:''}>${label}</span>`})}
function spinner(el,msg){el.innerHTML=`<div class=muted style="padding:14px"><span class=spin></span>${esc(msg||'Working…')}</div>`}
/* a placeholder in the shape of what is coming: n cards of shimmering lines, so the page never jumps when the data lands */
function skeleton(el,n=3,msg){el.innerHTML=(msg?`<div class="small muted" style="margin:0 0 8px"><span class=spin></span>${esc(msg)}</div>`:'')+Array.from({length:n},()=>'<div class=card aria-hidden=true><div class="sk h"></div><div class=sk></div><div class="sk w"></div></div>').join('')}
function toast(msg,ok=true){const t=document.createElement('div');t.className='toast'+(ok?'':' bad');t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),2600)}
/* one log row (retention, vault access, review) as a sentence */
function logRow(r){const what=r.action?plain(r.action):r.decision?plain(r.decision):r.outcome?'audit: '+esc(r.outcome):'reveal '+esc(r.token||'');
  return `<tr><td class=muted style="white-space:nowrap">${esc((r.ts||'').slice(0,16).replace('T',' '))}</td><td>${esc(r.who||'')}</td><td>${what} ${r.doc_id?'<span class=muted>'+esc(r.doc_id)+'</span>':''}${r.reason?' · '+esc(r.reason):''}${r.rows?' · '+r.rows+' rows':''}${r.count?' · '+r.count+' occurrence(s)':''}</td></tr>`}
if(document.body)shell();else document.addEventListener('DOMContentLoaded',shell);
