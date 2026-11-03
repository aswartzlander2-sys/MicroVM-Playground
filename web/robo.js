const $=s=>document.querySelector(s)||dummyEl();
/* A detached placeholder: wiring a control that is absent (stale cached
   markup, disabled feature) becomes a silent no-op instead of a TypeError
   that aborts the whole script and leaves dialogs stuck open. */
function dummyEl(){return dummyEl.d||(dummyEl.d=document.createElement('div'))}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const known=[['debian','Debian 12','Original playground image with command-line tools.','debian','Original image']];
let host={},images=[],vms=[],filter='all',query='',term=null,socket=null,resizeFn=null;
async function api(path,opt={}){let r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});let data=await r.json().catch(()=>({}));if(!r.ok)throw Error(data.detail||'Request failed');return data}
function toast(msg){$('#toast').textContent=msg;$('#toast').classList.remove('hidden');setTimeout(()=>$('#toast').classList.add('hidden'),3500)}
function status(){let mode=host.mode||'connecting';$('#sidehost').textContent=mode==='container'?'Container backend':mode==='mock'?'Demo backend':mode==='connecting'?'Checking host':mode.toUpperCase()+' backend';$('#sidehostdetail').textContent=mode==='container'?'Shared host kernel':mode==='mock'?'Simulated machines only':'Virtualization runtime';$('#hostbadge').textContent=mode==='mock'?'Demo mode':mode==='container'?'Container mode':mode==='connecting'?'Connecting':mode.toUpperCase()+' host';$('#navcount').textContent=vms.length}
async function refresh(){try{[host,images,vms]=await Promise.all([api('/api/host'),api('/api/images'),api('/api/vms')]);status();render()}catch(e){toast(e.message)}}
function catalog(){let entries=known.map(x=>{let image=images.find(i=>i.id===x[0]);return {id:x[0],name:x[1],description:x[2],logo:x[3],family:x[4],image}});for(let im of images){if(im.id==='default'&&host.mode==='container'){entries.unshift({id:'default',name:'Debian · Full toolset',description:'Debian with useful tools pre-installed.',logo:'debian',family:'Included',image:im})}else if(!entries.some(x=>x.id===im.id)){entries.unshift({id:im.id,name:im.label,description:im.note,logo:'linux',family:host.mode==='mock'?'Demo':'Local image',image:im})}}return entries.sort((a,b)=>Number(!!b.image)-Number(!!a.image))}
function render(){const page=location.hash==='#explore'?'explore':location.hash==='#domains'?'domains':location.hash==='#updates'?'updates':'vms';document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.dataset.page===page));$('#breadcrumb').textContent=page==='vms'?'Virtual machines':page==='domains'?'Domains & ports':page==='updates'?'Updates':'Explore';if(page==='vms')renderVMs();else if(page==='domains')renderDomains();else if(page==='updates')renderUpdates();else renderExplore()}
function logo(x){return `<div class="os-logo">${x.logo?`<img src="/logos/${esc(x.logo)}.svg" alt="">`:esc(x.name.slice(0,1))}</div>`}
function renderExplore(){let all=catalog(), list=all.filter(x=>(filter!=='available'||x.image)&&(`${x.name} ${x.family} ${x.description}`.toLowerCase().includes(query.toLowerCase())));$('#content').innerHTML=`<div class="intro"><div><div class="eyebrow">YOUR WORKSPACE</div><h1>Explore</h1><p>Pick a command-line operating system for your next machine.</p></div><div class="intro-right">A WORLD OF POSSIBILITIES<br>ONE TERMINAL AWAY &nbsp; ↗</div></div><div class="overview-well"><div class="overview-copy"><span class="eyebrow">YOUR WORKSPACE</span><h2>Machines on your own box.</h2><p>Choose an environment, configure your machine, and open a terminal.</p></div><div class="overview-stat"><span>Machines</span><b>${vms.length}</b><small>${vms.filter(v=>v.state==='running').length} running</small></div><div class="overview-stat"><span>Runtime</span><b>${esc(host.mode||'—')}</b><small>${host.mode==='container'?'Shared kernel':host.mode==='mock'?'Simulation':'Host backend'}</small></div><button class="primary" id="heroCreate">New machine</button></div><div class="sectionhead"><div><div class="eyebrow">THE CATALOG &nbsp; / &nbsp; ${String(list.length).padStart(2,'0')} ENVIRONMENTS</div><h2>Find your starting point.</h2><p>Browse command-line systems. Availability depends on your host and images.</p></div><div class="tools"><input class="search" id="search" placeholder="⌕   Search systems..." value="${esc(query)}"><button class="filter ${filter==='available'?'active':''}" id="available">Available</button></div></div><div class="grid">${list.map(x=>`<article class="os-card"><div class="cardtop">${logo(x)}<span class="tag ${x.image?.ready?'ready':''}">${x.image?(host.mode==='mock'?'SIMULATED':x.image.ready?'READY':'DOWNLOAD ON CREATE'):'UNAVAILABLE'}</span></div><h3>${esc(x.name)}</h3><p>${esc(x.description)}</p><div class="cardfoot"><span>${esc(x.family)} &nbsp; / &nbsp; CLI</span><button ${x.image?'':'disabled'} data-create="${esc(x.id)}">${x.image?'Launch ↗':'Unavailable'}</button></div></article>`).join('')}</div><p class="tip">Only entries marked ready or download on create can be started. Other entries are discovery suggestions, not installed images. Linux containers share the host kernel; QEMU and Firecracker run VMs. FreeDOS and MS-DOS need a compatible graphical console and are not currently launchable here.</p>`;$('#search').oninput=e=>{query=e.target.value;let pos=e.target.selectionStart;renderExplore();$('#search').focus();$('#search').setSelectionRange(pos,pos)};$('#available').onclick=()=>{filter=filter==='available'?'all':'available';renderExplore()};$('#heroCreate').onclick=()=>openCreate();document.querySelectorAll('[data-create]').forEach(b=>b.onclick=()=>openCreate(b.dataset.create))}
function renderVMs(){
  const running=vms.filter(v=>v.state==='running').length,stopped=vms.filter(v=>v.state==='stopped').length,starting=vms.filter(v=>v.state==='starting').length;
  $('#content').innerHTML=`<div class="intro"><div><h1>Virtual machines</h1><p>Command-line machines running on your own box. Create one and open its terminal.</p></div></div>
  <div class="figures"><div class="fig"><div class="k">Machines</div><div class="v">${vms.length}</div><div class="f">${running} running</div></div><div class="fig"><div class="k">Running</div><div class="v">${running}</div><div class="f">Ready to connect</div></div><div class="fig"><div class="k">Stopped</div><div class="v">${stopped}</div><div class="f">Kept on this host</div></div><div class="fig"><div class="k">Preparing</div><div class="v">${starting}</div><div class="f">Downloading or booting</div></div><div class="fig"><div class="k">Runtime</div><div class="v">${esc(host.mode||'—')}</div><div class="f">${host.mode==='mock'?'Simulated, not a real OS':host.mode==='container'?'Shared host kernel':'Virtual machine backend'}</div></div></div>
  <div class="toolbar"><input class="search" id="vmsearch" placeholder="⌕   Filter machines…"><button class="filter" id="reload">Refresh ↻</button></div>
  <div class="machine-grid">${vms.map(v=>{let im=v.image&&v.image!=='default'?v.image.replace(/-.*$/,''):'linux';let icon=`<div class="machine-icon"><img src="/logos/${esc(im)}.svg" alt="${esc(im)}" onerror="this.outerHTML='▣'"></div>`;let live=v.state==='running';return `<article class="machine-card" data-name="${esc(v.name.toLowerCase())}"><div class="machine-top">${icon}<div class="machine-title"><strong>${esc(v.name)}</strong><small>/${esc(v.id)}</small></div><span class="machine-pill pill-${esc(v.state)}">${esc(v.state).toUpperCase()}</span></div><div class="chiprow"><span class="mchip"><b>${esc(v.vcpus)}</b>&nbsp;vCPU</span><span class="mchip"><b>${fmtMem(v.mem_mib)}</b></span><span class="mchip"><b>${esc(v.disk_gb)}</b>&nbsp;GB</span><span class="mchip dim">${esc(v.image_label||v.image||'rootfs')}</span></div><div class="mrows"><div class="mrow"><span>address</span><b class="mono">${esc(v.ip||'—')}</b></div><div class="mrow"><span>boot</span><b>${v.boot_ms!=null?bootfmt(v.boot_ms):'—'}</b></div><div class="mrow"><span>uptime</span><b class="u-uptime" data-start="${v.started_at?String(Math.round(v.started_at*1000)):''}">—</b></div><div class="mrow"><span>destroys in</span><b class="u-destroy" data-exp="${v.expires_at?String(Math.round(v.expires_at*1000)):''}">—</b></div></div>${v.error?`<span class="error">${esc(v.error)}</span>`:(v.state==='starting'&&v.progress?`<span class="progress-detail">${esc(v.progress.message)}</span>${Number.isFinite(v.progress.percent)?`<div class="progress-track"><i style="width:${Math.max(0,Math.min(100,v.progress.percent))}%"></i></div>`:''}`:'')}<div class="vm-actions main-actions">${v.state==='stopped'?`<button class="primary sm" data-action="start" data-id="${esc(v.id)}">▶ Start machine</button>`:v.state==='running'?`<button class="ghost" data-terminal="${esc(v.id)}">Console</button>`:'<button class="ghost" disabled>Console</button>'}${v.state==='running'&&v.can_suspend?`<button class="ghost sm" data-action="stop" data-id="${esc(v.id)}" title="Graceful shutdown; the disk is kept and the machine can be started again">Stop</button>`:''}${v.state==='running'?`<button class="ghost sm" data-action="restart" data-id="${esc(v.id)}" title="Graceful reboot on the same disk">Restart ↻</button>`:''}<button class="ghost danger" data-action="delete" data-id="${esc(v.id)}">Destroy</button></div><div class="vm-actions sub-actions">${v.state==='running'&&host.mode==='qemu'?`<button class="ghost sm" data-files="${esc(v.id)}">Files</button><button class="ghost sm" data-ports="${esc(v.id)}">Ports</button>`:''}</div></article>`}).join('')}<button class="new-card" id="vmcreate"><b>New machine</b><span>Choose an OS and create a VM</span></button></div>${host.problems?.length?`<p class="tip">${host.problems.map(esc).join('<br>')}</p>`:''}`;
  $('#vmcreate').onclick=()=>openCreate();$('#reload').onclick=refresh;$('#vmsearch').oninput=e=>document.querySelectorAll('.machine-card').forEach(c=>c.hidden=!c.dataset.name.includes(e.target.value.toLowerCase()));
  document.querySelectorAll('[data-terminal]').forEach(b=>b.onclick=()=>openTerminal(b.dataset.terminal));document.querySelectorAll('[data-files]').forEach(b=>b.onclick=()=>openFiles(b.dataset.files));document.querySelectorAll('[data-ports]').forEach(b=>b.onclick=()=>openPorts(b.dataset.ports));
  document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{let action=b.dataset.action,id=b.dataset.id;if(action==='delete'&&!confirm('Destroy this machine and its data?'))return;try{await api('/api/vms/'+id+(action==='delete'?'':'/'+action),{method:action==='delete'?'DELETE':'POST'});await refresh()}catch(e){toast(e.message)}})
}
// Live rows on machine cards: uptime / destroys-in tick every second without a re-render.
function fmtDur(s){s=Math.max(0,Math.floor(s));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);if(d)return d+'d '+h+'h';if(h)return h+'h '+m+'m';if(m)return m+'m '+(s%60)+'s';return s+'s'}
function fmtMem(m){return m>=1024?(m/1024)+' GiB':m+' MiB'}
function bootfmt(ms){return ms<20000?Math.max(1,Math.round(ms/1000))+'s':Math.round(ms)+' ms'}
setInterval(()=>{const now=Date.now();document.querySelectorAll('.u-uptime').forEach(b=>{let st=Number(b.dataset.start);if(st)b.textContent=fmtDur((now-st)/1000)});document.querySelectorAll('.u-destroy').forEach(b=>{let ex=Number(b.dataset.exp);if(ex)b.textContent=fmtDur((ex-now)/1000)})},1000);
let createWired=false,ttlSel='never';
function syncTtlChips(){document.querySelectorAll('#ttlchips [data-ttl]').forEach(b=>{let on=b.dataset.ttl===ttlSel;b.classList.toggle('is-on',on);b.setAttribute('aria-checked',on?'true':'false')});$('#ttlvalue').value=ttlSel}
function syncCreateSummary(){let g=n=>$('#createform').querySelector('[name='+n+']');
$('#createsummary').innerHTML=`<b>${esc(g('vcpus')?.value??'—')}</b> vCPU · <b>${esc(g('mem_mib')?.value??'—')}</b> MiB memory · <b>${esc(g('disk_gb')?.value??'—')}</b> GB disk · network <b>${$('#guestnet')?.checked?'outbound internet':'isolated (default)'}</b> · auto-destroy <b>${esc(ttlSel==='never'?'until deleted':ttlSel)}</b>`}
function openCreate(id){
  const select=$('#imageselect');
  select.innerHTML=images.map(i=>`<option value="${esc(i.id)}">${esc(i.label)}</option>`).join('');
  if(id && images.some(i=>i.id===id))select.value=id;
  $('#imagechoices').innerHTML=images.map(i=>{const c=catalog().find(x=>x.id===i.id)||{name:i.label,logo:'linux'};return `<button type="button" class="image-choice" data-choice="${esc(i.id)}">${logo(c)}<span><b>${esc(c.name)}</b><small>${host.mode==='mock'?'Demo only':i.ready?'Ready':'Downloads on creation'}</small></span></button>`}).join('');
  const update=()=>document.querySelectorAll('[data-choice]').forEach(b=>b.classList.toggle('selected',b.dataset.choice===select.value));
  document.querySelectorAll('[data-choice]').forEach(b=>b.onclick=()=>{select.value=b.dataset.choice;update()});update();
  if(!createWired){createWired=true;
    $('#ttlchips').innerHTML=['15m','1h','4h','12h','never'].map(v=>`<button type="button" class="chip-btn" role="radio" aria-checked="false" data-ttl="${v}">${v==='never'?'Never':v}</button>`).join('');
    document.querySelectorAll('#ttlchips [data-ttl]').forEach(b=>b.onclick=()=>{ttlSel=b.dataset.ttl;syncTtlChips();syncCreateSummary()});
    ['vcpus','mem_mib','disk_gb'].forEach(n=>$('#createform').querySelector('[name='+n+']')?.addEventListener('input',syncCreateSummary));
    $('#guestnet')?.addEventListener('change',syncCreateSummary);
  }
  syncTtlChips();
  $('#launch').disabled=!images.length;
  $('#createhint').textContent=!images.length?'No launchable images found. Check your host setup.':host.mode==='mock'?'Demo mode creates a simulated machine, not a real OS.':host.mode==='container'?'This host uses containers, which share the host kernel.':'Preparing and booting a guest can take several minutes, especially without KVM.';
  $('#createerror').textContent='';$('#modal').classList.remove('hidden');
}
$('#cancelcreate').onclick=()=>$('#modal').classList.add('hidden');
$('#createform').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),payload=Object.fromEntries(f);payload.internet=!!$('#guestnet')?.checked;for(let k of ['vcpus','mem_mib','disk_gb'])payload[k]=Number(payload[k]);$('#launch').disabled=true;$('#launch').textContent='Launching…';try{let vm=await api('/api/vms',{method:'POST',body:JSON.stringify(payload)});$('#modal').classList.add('hidden');location.hash='vms';await refresh();if(vm.error)toast(vm.error);else toast(vm.state==='starting'?'Downloading the OS. Progress appears on the machine card.':'Machine created')}catch(err){$('#createerror').textContent=err.message}finally{$('#launch').disabled=false;$('#launch').textContent='Launch machine'}};
// ------------------------------------------------------------- terminal flow
// Picker (built-in vs ttyd, rememberable) -> console modal with a live switch.
let consoleVM=null,termMode='builtin',pickerVM=null;
function openTerminal(id){let vm=vms.find(v=>v.id===id);if(!vm)return;let saved=localStorage.getItem('robo-terminal')||'',ttydOk=host.terminal==='ttyd';
  if(!ttydOk)return openConsole(id,'builtin');           // no real choice, don't ask
  if(saved==='builtin'||saved==='ttyd')return openConsole(id,saved);
  pickerVM=id;$('#pickttyd').disabled=false;$('#pickttydnote').textContent=(host.ttyd||'ttyd')+' available on this host';
  $('#pickremember').checked=false;$('#termpick').classList.remove('hidden')}
$('#closepick').onclick=()=>{$('#termpick').classList.add('hidden');pickerVM=null};
$('#pickbuiltin').onclick=()=>{if(!pickerVM||!vms.some(v=>v.id===pickerVM))return;if($('#pickremember').checked)localStorage.setItem('robo-terminal','builtin');let id=pickerVM;$('#termpick').classList.add('hidden');openConsole(id,'builtin')};
$('#pickttyd').onclick=()=>{if(!pickerVM||!vms.some(v=>v.id===pickerVM))return;if($('#pickremember').checked)localStorage.setItem('robo-terminal','ttyd');let id=pickerVM;$('#termpick').classList.add('hidden');openConsole(id,'ttyd')};

function teardownConsole(){if(socket){socket.onclose=null;try{socket.close()}catch(e){}socket=null}if(resizeFn){window.removeEventListener('resize',resizeFn);resizeFn=null}if(term){try{term.dispose()}catch(e){}term=null}const f=$('#ttydframe');f.hidden=true;f.src='about:blank'}
/* ── echo suppression ──
   A real tty already handles passwords (sudo/ssh/su turn echo off), so no
   special password row is needed. The one blind window that remains is for
   PASTE: the guest may echo what this browser sends, and a quiet beat after
   a resize avoids visible stty chatter. */
let quietUntil=0;
function quiet(ms){quietUntil=Date.now()+ms}
function rawSend(data){if(socket&&socket.readyState===1)socket.send(new TextEncoder().encode(data))}
const FONTS=[10,11,12,13,14,16,18,21];const fontSize={n:3};fontSize.get=()=>FONTS[fontSize.n];
$('#consolecopy').onclick=()=>{(function(){let txt=term&&term.getSelection?term.getSelection():'';if(!txt){toast('Select text first');return}
  const legacy=()=>{let ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();let ok=false;try{ok=document.execCommand('copy')}catch(e){}ta.remove();toast(ok?'Copied':'Copy blocked — plain-HTTP pages cannot use the Clipboard; add HTTPS')};
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(txt).then(()=>toast('Copied')).catch(legacy);
  else legacy()})()};
$('#consolepaste').onclick=()=>{const send=t=>{if(t){quiet(280);rawSend(t.replace(/\r?\n/g,'\r'))}term&&term.focus()};
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.readText().then(send).catch(()=>{toast('Clipboard blocked — paste into the box instead');term&&term.focus()});
  else{let t=prompt('Plain-HTTP pages block the Clipboard API. Paste into this box (hint: switch to HTTPS or localhost for one-click):');t&&send(t)}};
function setFontSize(delta){let n=Math.min(FONTS.length-1,Math.max(0,fontSize.n+delta));if(n===fontSize.n)return;fontSize.n=n;if(term){term.options.fontSize=FONTS[n];refitConsole()}}
$('#fontup').onclick=()=>setFontSize(1);
$('#fontdown').onclick=()=>setFontSize(-1);
function syncConsoleChrome(vm){let both=host.terminal==='ttyd'&&!!vm.terminal_url;$('#termswitch').hidden=!both;$('#swBuiltin').classList.toggle('is-on',termMode==='builtin');$('#swTtyd').classList.toggle('is-on',termMode==='ttyd');$('#consoledot').className='cdot st-'+esc(vm.state);$('#consolemeta').textContent=(termMode==='ttyd'?('terminal: ttyd '+(host.ttyd||'')):'terminal: built-in')+' · UTF-8 · truecolor';
  /* These tools ride on the built-in xterm only: ttyd's iframe has its own
     native copy/paste, so hiding the buttons avoids promising both. */
  for(const id of ['consolecopy','consolepaste','fontdown','fontup']){const el=document.getElementById(id);if(el)el.hidden=termMode!=='builtin'}
  $('#swBuiltin').classList.toggle('is-on',termMode==='builtin');$('#swTtyd').classList.toggle('is-on',termMode==='ttyd')}
function startTtydFrame(vm){$('#terminal').hidden=true;$('#ttydframe').hidden=false;$('#ttydframe').src=vm.terminal_url||('/terminal/'+encodeURIComponent(consoleVM));$('#btnclear').hidden=true;$('#consolestatus').textContent='connected'}
function openConsole(id,mode='builtin'){closeConsoleCore(false);let vm=vms.find(v=>v.id===id);if(!vm)return;consoleVM=id;termMode=(host.terminal==='ttyd'&&vm.terminal_url)?mode:'builtin';
  $('#consoletitle').textContent=vm.name||'Terminal';$('#consolevcpu').textContent=vm.vcpus+' vCPU';$('#consolevcpu').hidden=false;$('#consolemem').textContent=fmtMem(vm.mem_mib);$('#consolemem').hidden=false;$('#consoleip').textContent=vm.ip||'—';$('#consoleip').hidden=false;
  $('#consolemodal').classList.remove('hidden');$('#consolestatus').textContent='connecting…';syncConsoleChrome(vm);
  if(termMode==='ttyd'){startTtydFrame(vm);return}
  $('#btnclear').hidden=false;$('#terminal').hidden=false;term=new Terminal({cursorBlink:true,fontSize:fontSize.get(),fontFamily:'monospace',theme:{background:'#090b0b',foreground:'#e7e7df'}});let fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open($('#terminal'));fit.fit();socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/vms/${encodeURIComponent(id)}/console`);socket.binaryType='arraybuffer';socket.onopen=()=>{$('#consolestatus').textContent='connected';quiet(250);socket.send(JSON.stringify({type:'resize',rows:term.rows,cols:term.cols}));term.focus()};
  socket.onmessage=e=>{if(Date.now()<quietUntil)return;    // a blind window swallows the guest's echo of what was just sent
    term.write(typeof e.data==='string'?e.data:new Uint8Array(e.data))};
  const activeTerm=term;socket.onclose=()=>{if(term===activeTerm){activeTerm.write('\r\n\x1b[90mConnection closed — press ↻ to reconnect.\x1b[0m');if($('#consolestatus'))$('#consolestatus').textContent='closed'}};term.onData(d=>{if(socket?.readyState===1)socket.send(new TextEncoder().encode(d))});resizeFn=()=>{fit.fit();if(socket?.readyState===1){quiet(250);socket.send(JSON.stringify({type:'resize',rows:term.rows,cols:term.cols}))}};window.addEventListener('resize',resizeFn)}
function closeConsoleCore(hide=true){teardownConsole();consoleVM=null;if(hide)$('#consolemodal').classList.add('hidden')}
$('#closeconsole').onclick=()=>closeConsoleCore(true);
$('#swBuiltin').onclick=()=>{if(!consoleVM||termMode==='builtin')return;let vm=vms.find(v=>v.id===consoleVM);if(!vm)return;teardownConsole();termMode='builtin';if(localStorage.getItem('robo-terminal'))localStorage.setItem('robo-terminal','builtin');syncConsoleChrome(vm);$('#consolestatus').textContent='connecting…';openConsole(consoleVM,'builtin')};
$('#swTtyd').onclick=()=>{if(!consoleVM||termMode==='ttyd')return;let vm=vms.find(v=>v.id===consoleVM);if(!vm||!(host.terminal==='ttyd'&&vm.terminal_url))return;teardownConsole();termMode='ttyd';if(localStorage.getItem('robo-terminal'))localStorage.setItem('robo-terminal','ttyd');syncConsoleChrome(vm);$('#terminal').hidden=true;$('#consolestatus').textContent='reconnecting…';startTtydFrame(vm)};
$('#reloadconsole').onclick=()=>{if(!consoleVM)return;let id=consoleVM;teardownConsole();openConsole(id,termMode)};
/* ── fullscreen, upstream style: native Fullscreen API on the console box, with
   an is-max fallback for when the browser refuses. Button repaints its icon. ── */
const FS_ENTER='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>';
const FS_EXIT='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h3a2 2 0 0 0 2-2V3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M21 16h-3a2 2 0 0 0-2 2v3"/></svg>';
function consoleBox(){return $('#consolemodal .console-dialog')}
function paintFsBtn(){const b=$('#fullscreenconsole');if(!b)return;let box=consoleBox();const on=document.fullscreenElement===box||box?.classList.contains('is-max');b.innerHTML=on?FS_EXIT:FS_ENTER;b.title=on?'Exit fullscreen':'Fullscreen'}
function refitConsole(){if(term&&resizeFn)resizeFn();if(term)term.focus()}
/* Double-refit: once immediately, once after layout (and web fonts) settle --
   a single fit against the not-yet-final box leaves rows running off screen. */
function refitConsoleTwice(){refitConsole();setTimeout(refitConsole,120);setTimeout(refitConsole,400)}
async function toggleFullscreen(){const box=consoleBox();if(!box)return;try{if(document.fullscreenElement===box)await document.exitFullscreen();else if(!box.classList.contains('is-max'))await box.requestFullscreen();else box.classList.remove('is-max')}catch(e){box.classList.toggle('is-max')}paintFsBtn();refitConsoleTwice()}
document.addEventListener('fullscreenchange',()=>{paintFsBtn();refitConsoleTwice()});
if(document.fonts&&document.fonts.ready)document.fonts.ready.then(()=>refitConsoleTwice());
$('#fullscreenconsole').onclick=toggleFullscreen;paintFsBtn();
$('#btnclear').onclick=()=>{if(term){term.clear();term.focus()}};$('#newvm').onclick=()=>openCreate();$('#closemodal').onclick=()=>$('#modal').classList.add('hidden');$('#theme').onclick=()=>{let t=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=t;localStorage.setItem('robo-theme',t)};document.documentElement.dataset.theme=localStorage.getItem('robo-theme')||'light';window.onhashchange=render;
$('#logout').onclick=async()=>{await api('/api/auth/logout',{method:'POST'});location.reload()};$('#loginform').onsubmit=async e=>{e.preventDefault();$('#loginerror').textContent='';try{await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('#username').value.trim(),password:$('#password').value})});$('#login').classList.add('hidden');$('#app').classList.remove('hidden');await refresh()}catch(e){$('#loginerror').textContent=e.message}};
(async()=>{try{let s=await api('/api/auth/session');if(s.signedIn){$('#app').classList.remove('hidden');await refresh()}else{$('#login').classList.remove('hidden');if(!s.configured)$('#loginerror').textContent='Login not configured. Run ./run.sh in a terminal.'}}catch(e){$('#login').classList.remove('hidden');$('#loginerror').textContent=e.message}})();

let fileVM='',selectedFile='';
function openFiles(id){fileVM=id;selectedFile='';$('#filetitle').textContent=(vms.find(v=>v.id===id)?.name||'Machine')+' files';$('#filepath').value='/root';$('#filelist').innerHTML='';$('#sshuser').value='';$('#sshpass').value='';$('#filetext').value='';$('#filetext').disabled=true;$('#filesave').disabled=true;$('#filedownload').disabled=true;$('#fileerror').textContent='';$('#sshuser').placeholder='root (default)';$('#sshpass').placeholder="this machine's password is used automatically";$('#filemodal').classList.remove('hidden');listFiles($('#filepath').value)}
async function fileRequest(action,path,content){let payload={action,path,username:$('#sshuser').value,password:$('#sshpass').value};if(content!==undefined)payload.content=content;let res=await fetch('/api/vms/'+encodeURIComponent(fileVM)+'/files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!res.ok){let err=await res.json().catch(()=>({}));throw Error(err.detail||'File request failed')}return action==='download'?res.blob():res.json()}
async function listFiles(path){$('#fileerror').textContent='';try{let data=await fileRequest('list',path);$('#filepath').value=data.path;$('#filelist').innerHTML=data.entries.length?data.entries.map((f,i)=>`<button class="fileentry" data-file-index="${i}"><span class="fi">${f.directory?'▸':'▤'}</span><span class="fn">${esc(f.name)}${f.symlink?' ↗':''}</span><span class="fm">${f.directory?'DIR':esc(f.size)+' B'}</span></button>`).join(''):'<p class="notice">This directory is empty.</p>';document.querySelectorAll('[data-file-index]').forEach(b=>b.onclick=()=>{let f=data.entries[Number(b.dataset.fileIndex)],p=(path==='/'?'':path.replace(/\/$/,''))+'/'+f.name;if(f.directory)listFiles(p);else selectFile(p)})}catch(e){$('#fileerror').textContent=e.message}}
async function renderUpdates(){
  /* Updates tab, release-panel style: current version beside the newest on
     GitHub, the changelog in between, and one clearly-scoped action. Updating
     never overwrites local edits: it refuses while the source tree is dirty
     and only ever fast-forwards -- it cannot destroy anything. */
  $('#content').innerHTML=`<div class="intro"><div><h1>Updates</h1><p>This dashboard updates itself from GitHub — fast-forward only, and never over local edits.</p></div></div><div class="table" style="padding:26px 28px;max-width:880px"><div class="eyebrow">DASHBOARD / RELEASES</div>
  <div id="updpanel" style="margin-top:10px">
    <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:center">
      <div style="border:1px solid #1d2321;border-radius:12px;padding:16px 18px">
        <div class="hint" style="margin-bottom:6px">RUNNING NOW</div>
        <div style="font-size:26px;font-weight:700" class="mono" id="updcur">—</div>
        <div class="hint" id="updcurmsg" style="margin-top:6px">checking…</div>
      </div>
      <div style="text-align:center;color:#48e08a;font-size:22px" id="updarrow">•</div>
      <div style="border:1px solid #1d2321;border-radius:12px;padding:16px 18px;background:#0d1110">
        <div class="hint" style="margin-bottom:6px">ON GITHUB (origin/main)</div>
        <div style="font-size:26px;font-weight:700" class="mono" id="updrem">—</div>
        <div class="hint" id="updremmsg" style="margin-top:6px">checking…</div>
      </div>
    </div>
    <div id="updchangelog" style="margin-top:14px"></div>
    <div style="display:flex;gap:10px;margin-top:18px;align-items:center">
      <button class="primary" id="updapply" disabled>Update now ↻</button>
      <button class="ghost" id="updrefresh">Re-check</button>
      <span class="hint" id="updnote"></span>
    </div>
    <div id="upderror" class="error" style="margin-top:10px"></div>
  </div></div>`;
  const paint=async()=>{
    const panel=$('#updpanel');
    try{
      let x=await api('/api/updates');
      if(x.error&&!x.current)throw Error(x.error);
      let cur=x.current||{};
      $('#updcur').textContent=cur.short||'?';
      $('#updcurmsg').textContent=cur.subject||'';
      $('#updrem').textContent=x.remote_short||(x.available?'':'(unreachable)');
      if(x.dirty){$('#updnote').innerHTML='<b>Local edits exist</b> in the source directory — commit or hand them to GitHub first; the update refuses to overwrite anything.'}
      else if(x.behind){$('#updnote').textContent=x.behind+' commit'+(x.behind===1?'':'s')+' behind GitHub'}
      else $('#updnote').textContent=x.error?('GitHub check failed: '+x.error):(x.available?'':'You are on the newest commit.');
      $('#updarrow').textContent=x.behind?'↓':'✓';
      $('#updremmsg').textContent=x.available?'newer version available':(x.remote_short?'same as running':'');
      let log='';
      if(Array.isArray(x.log)&&x.log.length){
        log='<div class="hint" style="margin:2px 0 8px">WHAT UPDATES</div><div style="display:flex;flex-direction:column;gap:6px">'+x.log.map(l=>`<div style="display:flex;gap:10px;align-items:baseline"><span class="mono" style="color:#8a8f8b;min-width:70px">${esc(l.short)}</span><span>${esc(l.subject)}</span></div>`).join('')+'</div>';
      }
      $('#updchangelog').innerHTML=log;
      $('#updapply').disabled=!x.available||x.dirty;
    }catch(e){
      $('#updcurmsg').textContent='Cannot check GitHub: '+e.message;
      $('#updnote').textContent='';
    }
  };
  const show=async()=>{await paint()};
  $('#updrefresh').onclick=show;
  $('#updapply').onclick=async()=>{if(!confirm('Update the dashboard from GitHub and restart it? The page will reconnect automatically.'))return;
    $('#updapply').disabled=true;$('#upderror').textContent='';$('#updnote').textContent='Updating and restarting…';
    try{let x=await api('/api/updates/apply',{method:'POST'});
      if(x.updated){toast('Updated — dashboard is restarting…');$('#updnote').textContent='Restarting on the new version…';let n=0;const t=setInterval(async()=>{n++;try{await fetch('/api/auth/session');clearInterval(t);location.reload()}catch(e){if(n>30){clearInterval(t);location.reload()}}},1500)}
      else{toast('Already up to date');await paint()}}
    catch(e){$('#upderror').textContent=e.message;await paint()}
    finally{$('#updapply').disabled=false}};
  await show();
}

async function selectFile(path){selectedFile=path;$('#editorname').textContent=path;$('#filedownload').disabled=false;$('#filetext').disabled=true;$('#filesave').disabled=true;$('#filetext').value='';try{let result=await fileRequest('read',path);$('#filetext').value=result.content;$('#filetext').disabled=false;$('#filesave').disabled=false;$('#fileerror').textContent=''}catch(e){$('#fileerror').textContent=e.message}}
$('#fileconnect').onclick=()=>listFiles($('#filepath').value);$('#filego').onclick=()=>listFiles($('#filepath').value);$('#fileup').onclick=()=>{let p=$('#filepath').value.replace(/\/+$/,'');listFiles(p.substring(0,p.lastIndexOf('/'))||'/')};$('#filesave').onclick=async()=>{if(!confirm('Overwrite '+selectedFile+'?'))return;try{await fileRequest('write',selectedFile,$('#filetext').value);toast('File saved')}catch(e){$('#fileerror').textContent=e.message}};$('#filedownload').onclick=async()=>{try{let blob=await fileRequest('download',selectedFile);let url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=selectedFile.split('/').pop()||'download';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){$('#fileerror').textContent=e.message}};$('#closefiles').onclick=()=>{$('#sshpass').value='';$('#filemodal').classList.add('hidden')};

let cfZones=[];let routeSlug=makeSlug();
function makeSlug(){let words=[['quiet','silver','happy','cosmic','gentle','bright','clever'],['space','forest','river','cloud','orbit','ocean'],['train','fox','comet','bird','station','whale']];let a=new Uint32Array(3),b=new Uint8Array(7);crypto.getRandomValues(a);crypto.getRandomValues(b);return words.map((w,i)=>w[a[i]%w.length]).join('-')+'-'+Array.from(b,x=>x.toString(16).padStart(2,'0')).join('')}
function tokenTemplate(){let u=new URL('https://dash.cloudflare.com/profile/api-tokens');u.searchParams.set('permissionGroupKeys',JSON.stringify([{key:'zone',type:'read'},{key:'dns',type:'edit'},{key:'argotunnel',type:'edit'}]));u.searchParams.set('accountId','*');u.searchParams.set('zoneId','all');u.searchParams.set('name','Robo Universal Tunnel');return u.href}
async function renderDomains(){
  $('#content').innerHTML=`<div class="intro"><div><h1>Domains & ports</h1><p>Connect Cloudflare directly to publish services from your machines.</p></div></div>
<div class="table" style="padding:24px;max-width:850px"><div class="eyebrow">CLOUDFLARE / DIRECT CONNECTION</div><h2>Connect your domain</h2><p class="notice">Create a token with Zone Read, DNS Edit and Cloudflare Tunnel Edit. Limit it to your account and zones. Paste it below; Robo stores it on the host to manage tunnels. Use HTTPS to access this dashboard.</p><a class="primary small" target="_blank" rel="noopener" href="${esc(tokenTemplate())}">Create Cloudflare token ↗</a><p class="notice">Review the permissions and resources on Cloudflare, create the token, and copy it here.</p><div class="fileauth"><input id="cftoken" type="password" placeholder="Cloudflare API token" autocomplete="off"><button class="primary small" id="cfconnect">Find domains</button></div><div id="cferror" class="error" role="alert"></div><div id="cfzones" class="fileauth"></div><div id="cfstatus" class="notice">Checking connection…</div><p class="notice">Publishing requires cloudflared installed on this Linux host. Each published port is public; private access is not available in this version.</p></div>
<div class="table" style="padding:24px;max-width:850px"><div class="eyebrow">DOMAINS / WILDCARD GATEWAYS</div><h2>Gateways per domain</h2><p class="notice">A gateway is a root subdomain (e.g. <span class="mono">burrows</span>) backed by ONE wildcard DNS record. Every published port of any machine becomes a label under it — <span class="mono">web-8080.burrows.robo-universe.top</span> — with instant DNS and no per-port records. The root domain itself is never addressed directly.</p><div id="cfconnector" class="notice">Loading…</div></div>`;
  const showGates=async()=>{
    const box=$('#cfconnector');if(!box)return;
    try{
      let gs=await api('/api/cloudflare/gates');
      if(!gs.connected){box.textContent='Connect Cloudflare above to configure gateways.';return}
      let zonesList=gs.zones||[];
      if(!zonesList.length){box.textContent='No usable domain on this connection.';return}
      const defZone=zonesList.find(z=>z.default)||zonesList[0];
      box.innerHTML=`<div style="display:flex;flex-direction:column;gap:25px;margin-top:16px">`+zonesList.map(z=>`
        <div class="zonebox">
          <div class="zonehead">
            <b>${esc(z.name)}</b>
            <label class="gatepick" title="Make this the domain new machines publish on by default"><input type="radio" name="defzone" value="${esc(z.id)}" ${z.id===defZone.id?'checked':''}><span>default domain</span></label>
            <span class="hint">${(z.gates||[]).length} gateway${(z.gates||[]).length===1?'':'s'}</span>
          </div>
          <div data-gatelist="${esc(z.id)}">
            ${(z.gates||[]).map(g=>`
              <div class="gaterow"><div class="gateinfo"><span class="gname">${g.name==='_'?esc(z.name):esc(g.name)+'.'+esc(z.name)}</span>
              <span class="ghint">${g.name==='_'?'every label directly under the domain — one wildcard record, free TLS':'labels like web-8080.'+esc(g.name)+'.'+esc(z.name)+' here — the TLS for these two levels comes from a linked VPS relay, not Cloudflare'}</span>
              <span class="pub-access">${(g.status==='connected')?'connected ✓':(g.status==='down'?'connector down — will retry':'not set up yet')}</span></div>
              <span class="gateacts"><label class="gatepick" title="Publish here by default when this machine has no explicit choice"><input type="radio" name="defgate-${esc(z.id)}" value="${esc(g.name)}" ${z.default_gate===g.name||(!z.default_gate&&g.name==='_')?'checked':''}><span>default gateway</span>${z.default_gate===g.name||(!z.default_gate&&g.name==='_')?'<span class="default-tag">DEFAULT</span>':''}</label>
              <button class="ghost sm" data-ensure="${esc(z.id)}" data-gate="${esc(g.name)}">Repair</button>
              <button class="ghost sm danger" data-removegate="${esc(g.id||'')}" data-gate="${esc(g.name)}" style="${g.id?'':'visibility:hidden'}">Remove</button></span></div>`).join('')||'<div class="gaterow"><span class="ghint">No gateways on this domain yet. Add one below — the catch-all (${esc(z.name)}) is the usual pick.</span></div>'}
          </div>
          <div class="fileauth newgate"><input placeholder="new gateway, e.g. burrows" data-newgate="${esc(z.id)}" maxlength="63" autocapitalize="off" spellcheck="false"><button class="ghost sm" data-addgate="${esc(z.id)}">Add gateway</button></div>
          <div class="ghint" style="color:var(--muted)">Need a gate subdomain? Link a VPS relay first — on this host run <span class="mono">python3 tools/robo-vps-link link user@your-vps-ip --zone ${esc(z.name)} --gate burrows</span>. The catch-all above works without one.</div>
        </div>`).join('')+`</div>
        <div style="margin-top:16px;display:flex;align-items:center;gap:12px"><button class="primary small" id="cfconfigsave">Save defaults</button> <span class="hint" id="cfgatesmsg"></span></div>`;
      box.querySelectorAll('[data-addgate]').forEach(b=>b.onclick=async()=>{
        const zid=b.dataset.addgate,input=box.querySelector('[data-newgate="'+zid+'"]'),name=((input&&input.value)||'').trim().toLowerCase();
        $('#cfgatesmsg').textContent='';if(!name)return;
        $('#cfgatesmsg').textContent='Setting up wildcard record + tunnel…';
        try{let x=await api('/api/cloudflare/gates',{method:'POST',body:JSON.stringify({zone_id:zid,gate:name})});
          $('#cfgatesmsg').textContent='';toast('Gateway ready: '+x.wildcard);await showGates()}
        catch(e){$('#cfgatesmsg').textContent=e.message;await showGates()}});
      box.querySelectorAll('[data-ensure]').forEach(b=>b.onclick=async()=>{
        $('#cfgatesmsg').textContent='Checking tunnel + wildcard…';
        try{await api('/api/cloudflare/gates',{method:'POST',body:JSON.stringify({zone_id:b.dataset.ensure,gate:b.dataset.gate})});
          $('#cfgatesmsg').textContent='';toast('Gateway connected');await showGates()}
        catch(e){$('#cfgatesmsg').textContent=e.message;await showGates()}});
      box.querySelectorAll('[data-removegate]').forEach(b=>b.onclick=async()=>{
        if(!b.dataset.removegate)return;
        if(!confirm('Remove gateway "'+b.dataset.gate+'"? Its wildcard DNS record and tunnel are torn down; everything published through it stops resolving.'))return;
        $('#cfgatesmsg').textContent='Tearing down…';
        try{await api('/api/cloudflare/gates/'+encodeURIComponent(b.dataset.removegate),{method:'DELETE'});
          $('#cfgatesmsg').textContent='';toast('Gateway removed');await showGates()}
        catch(e){$('#cfgatesmsg').textContent=e.message;await showGates()}});
      const gname=(zid)=>{const r=document.querySelector('input[name="defgate-'+zid+'"]:checked');return r?r.value:''};
      $('#cfconfigsave').onclick=async()=>{
        try{
          let gs2=await api('/api/cloudflare/gates');let zl=(gs2&&gs2.zones)||[];
          let payload={default_zone_id:(box.querySelector('input[name="defzone"]:checked')||{}).value||'',gates:{},defaults:{}};
          zl.forEach(z=>{payload.gates[z.id]=(z.gates||[]).map(g=>g.name);payload.defaults[z.id]=gname(z.id)});
          await api('/api/cloudflare/zones-config',{method:'POST',body:JSON.stringify(payload)});
          $('#cfgatesmsg').textContent='Saved.';toast('Defaults saved');await showGates()}
        catch(e){$('#cfgatesmsg').textContent=e.message}};
    }catch(e){box.textContent=e.message}
  };
  const show=async()=>{
    try{
      let x=await api('/api/cloudflare/setup');
      let zn=(x.connected&&Array.isArray(x.zones))?x.zones.length:0;
      $('#cfstatus').textContent=(x.connected?('Connected. '+zn+' domain'+(zn===1?'':'s')+' available: '+x.zones.map(z=>z.name).join(', ')):'Not connected. ')+(x.cloudflared?'cloudflared is installed.':'cloudflared is missing on this host.')+(x.pending_cleanup?' Some routes need cleanup.':'');
      if(x.connected){let b=document.createElement('button');b.className='ghost';b.textContent='Disconnect';b.onclick=async()=>{if(!confirm('Disconnect Cloudflare? Published ports must be removed first.'))return;try{await api('/api/cloudflare/setup',{method:'DELETE'});await show()}catch(e){$('#cferror').textContent=e.message}};$('#cfstatus').append(' ',b)}
    }catch(e){$('#cfstatus').textContent=e.message}
    await showGates();
  };
  $('#cfconnect').onclick=async()=>{let input=$('#cftoken');$('#cferror').textContent='';try{let r=await api('/api/cloudflare/zones',{method:'POST',body:JSON.stringify({token:input.value.trim()})});cfZones=r.zones;if(!cfZones.length)throw Error('No active zones found. The token connected but lists no zones you can use.');$('#cfzones').innerHTML=`<select id="cfzone" aria-label="Choose default domain">${cfZones.map(z=>`<option value="${esc(z.id)}">${esc(z.name)}</option>`).join('')}</select><button class="primary small" id="cfsave">Connect domains</button><span class="hint">All ${cfZones.length} of your Cloudflare domains become available for publishing; the one you pick is the default.</span>`;$('#cfsave').onclick=async()=>{try{await api('/api/cloudflare/setup/connect',{method:'POST',body:JSON.stringify({token:input.value.trim(),zone_id:$('#cfzone').value})});input.value='';$('#cfzones').innerHTML='';await show();toast('Cloudflare connected')}catch(e){$('#cferror').textContent=e.message}}}catch(e){$('#cferror').textContent=e.message}};
  await show();
}

let portVM='',portsWired=false,portsZones=[],portPick=[];
function makePortName(port){let v=vms.find(v=>v.id===portVM),base=((v&&v.name)||'vm').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,63-1-String(port).length).replace(/-+$/,'')||'vm';return base+'-'+port}
function portSuggestions(port){return [makePortName(port),'port-'+port,'app-'+port]}
/* The address block: every domain+gateway pair (a domain can host several
   gateways) appears in the dropdown; the default one is preselected. The
   publish name rides as a label UNDER the gateway, github-codespace style,
   so one wildcard DNS record serves all ports and the root domain is never
   addressed directly. */
function portURL(p,sub){return p.gate==='_' ? (sub+'.'+p.zone) : (sub+'.'+p.gate+'.'+p.zone)}
/* The address block: the zone catch-all (gate '_', built on demand right
   before the first publish of a domain) and, when a VPS relay is linked for
   it, any gate subdomain. The publish name rides UNDER the gateway,
   github-codespace style, so one wildcard record serves every port and the
   bare root is never addressed. */
async function populatePortDomains(){
  const sel=$('#portdomain'),pick=$('#subpick');
  try{let gs=await api('/api/cloudflare/gates');portPick=[]
    for(const z of (gs&&Array.isArray(gs.zones)?gs.zones:[]))
      for(const g of (z.gates||[]).filter(g=>g.name==='_'||g.status!=='unbuilt'))
        portPick.push({zone_id:z.id,zone:z.name,gate:g.name,default:!!z.default&&g.default,auto:g.name==='_'&&g.status==='unbuilt'});
  }catch(e){portPick=[]}
  if(portPick.length){
    let def=portPick.findIndex(p=>p.default&&!p.auto);
    if(def<0)def=portPick.findIndex(p=>p.default);
    if(def<0)def=0;
    sel.innerHTML=portPick.map((p,i)=>`<option value="${i}">${esc(p.zone)}${p.gate==='_'?' — labels under the domain':' — via '+esc(p.gate)}${p.auto?' (built on first publish)':p.default?' (default)':''}</option>`).join('');
    sel.selectedIndex=def;sel.disabled=false;
    $('#domainhint').textContent=`One wildcard record per domain; the port becomes a label: ${esc(portSuggestions(3000)[0])}.${esc(portURL(portPick[def],'').slice(0))}. Gate subdomains appear here too once a VPS relay is linked (Domains page).`;
  }else{
    sel.innerHTML='<option value="">No domain connected</option>';sel.disabled=true;
    pick.disabled=true;pick.innerHTML='<option value="">—</option>';
    $('#domainhint').textContent='Connect your domains on the “Domains & ports” page first — then every published port gets an instant address under one of them.';
  }
  portSubSuggestions();
}
function portSubSuggestions(){
  const pick=$('#subpick'),port=$('#guestport').value;
  pick.innerHTML='<option value="">Custom — type your own</option>'+portSuggestions(port).map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');
  pick.disabled=!portPick.length;
  pick.value=portSuggestions(port)[0];
  $('#portlabel').value=pick.value||'';
  syncPortPreview();
}
function syncPortPreview(){
  const p=portPick[Number($('#portdomain').value||0)];
  const sub=($('#portlabel').value.trim()||'port-'+$('#guestport').value).toLowerCase().replace(/\s+/g,'-');
  const ok=!!p&&/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(sub);
  $('#urlpreview').hidden=!ok;
  if(ok)$('#prevurl').textContent='https://'+portURL(p,sub)
}
function openPorts(id){portVM=id;$('#guestport').value=3000;
  if(!portsWired){portsWired=true;document.querySelectorAll('#accesschips [data-access]').forEach(b=>b.onclick=()=>{$('#portaccess').value=b.dataset.access;document.querySelectorAll('#accesschips [data-access]').forEach(x=>x.classList.toggle('is-on',x===b))});
    $('#portdomain').addEventListener('change',()=>{syncPortPreview()});
    $('#subpick').addEventListener('change',()=>{if($('#subpick').value){$('#portlabel').value=$('#subpick').value}syncPortPreview()});
    $('#portlabel').addEventListener('input',()=>{$('#subpick').value=$('#portlabel').value?'':portSuggestions($('#guestport').value)[0];syncPortPreview()});
    $('#guestport').addEventListener('change',()=>{portSubSuggestions()});
  }
  $('#portaccess').value='public';document.querySelectorAll('#accesschips [data-access]').forEach(x=>x.classList.toggle('is-on',x.dataset.access==='public'));
  $('#urlpreview').hidden=true;$('#porterror').textContent='';$('#portsmodal').classList.remove('hidden');loadPorts();populatePortDomains()}
async function loadPorts(){try{let rows=await api('/api/publishing');let own=rows.filter(r=>r.vm===portVM);$('#publishedlist').innerHTML=own.length?own.map(r=>{let st=r.tunnel?.status||r.phase,dns=st==='provisioning'?`<span class="pub-access" title="The DNS record was created; resolvers may need a moment. Retry the link shortly or clear the browser cache.">DNS propagating…</span>`:'<span class="pub-access">'+esc(r.access==='login'?'Aegis sign-in':'Public')+'</span>';return `<div class="pub-row"><div class="pub-info"><strong>Port ${esc(r.guest_port)}</strong>${r.tunnel?.domain?`<span class="pub-access">${esc(r.tunnel.domain)}${r.tunnel.wildcard?' via gateway':''}</span>`:''}${dns}${r.tunnel?.url?`<a href="${esc(r.tunnel.url)}" target="_blank" rel="noopener">${esc(r.tunnel.url)} ↗</a>`:'<span class="pub-wait">Address provisioning or unavailable</span>'}</div><button class="ghost sm danger" data-unpublish="${esc(r.guest_port)}">Unpublish</button></div>`}).join(''):'<span class="pub-wait">Nothing published from this machine yet.</span>';document.querySelectorAll('[data-unpublish]').forEach(b=>b.onclick=async()=>{if(!confirm('Unpublish port '+b.dataset.unpublish+'? Its public address stops working.'))return;try{await api('/api/vms/'+encodeURIComponent(portVM)+'/ports/'+encodeURIComponent(b.dataset.unpublish),{method:'DELETE'});await loadPorts();toast('Port unpublished')}catch(e){toast(e.message)}})}catch(e){$('#publishedlist').innerHTML='<span class="error">'+esc(e.message)+'</span>'}}
$('#portform').onsubmit=async e=>{e.preventDefault();let access=$('#portaccess').value,p=portPick[Number($('#portdomain').value||0)];if(!p)return toast('No domain selected');if(access==='public'&&!confirm('This will expose the guest service publicly to the internet. Continue?'))return;$('#porterror').textContent='';$('#portpublish').disabled=true;try{if(p.auto){await api('/api/cloudflare/gates',{method:'POST',body:JSON.stringify({zone_id:p.zone_id,gate:'_'})});p.auto=false}let x=await api('/api/vms/'+encodeURIComponent(portVM)+'/ports',{method:'POST',body:JSON.stringify({port:Number($('#guestport').value),sub:$('#portlabel').value.trim().toLowerCase(),access,zone_id:p.zone_id,gate:p.gate})});$('#portsmodal').classList.add('hidden');toast('Published at '+x.tunnel.url);await loadPorts()}catch(err){$('#porterror').textContent=err.message}finally{$('#portpublish').disabled=false}};

// Keep the dashboard live: poll every 2s while a machine is starting, every 4s
// otherwise, always on the VMs tab. The busy-guard stops overlapping fetches.
let refreshBusy=false,lastStateCount=0;
setInterval(async()=>{
  if(document.hidden||refreshBusy)return;
  if(!vms&&(location.hash==="#vms"||!location.hash))return;      // not loaded yet
  if(location.hash&&location.hash!=='#vms')return;
  refreshBusy=true;
  try{
    const starting=vms.some(v=>v.state==='starting');
    if(!starting&&Date.now()-lastStateCount<4000)return;
    lastStateCount=Date.now();
    await refresh();
  }catch(e){}finally{refreshBusy=false}
},2000);

document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(document.fullscreenElement)return;for(const id of ['consolemodal','termpick','filemodal','portsmodal','modal']){const el=document.getElementById(id);if(el&&!el.classList.contains('hidden')){if(id==='consolemodal')$('#closeconsole').click();else if(id==='termpick')$('#closepick').click();else if(id==='filemodal')$('#closefiles').click();else el.classList.add('hidden');break}}});
