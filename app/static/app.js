let repos=[],groups=[],state={page:'dashboard',repo:null};
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); const fmt=s=>s?new Date(s).toLocaleString():'—'; const bytes=n=>{n=Number(n||0);if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';if(n<1073741824)return (n/1048576).toFixed(1)+' MB';return (n/1073741824).toFixed(2)+' GB'};
async function api(u,o){let r=await fetch(u,o),j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.detail||j.error||r.statusText);return j}
async function init(){repos=await api('/api/repos');groups=await api('/api/groups');page('dashboard')}
async function refresh(){repos=await api('/api/repos');groups=await api('/api/groups')}
function nav(p){document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));document.getElementById('n-'+p)?.classList.add('active')}
async function page(p){state.page=p;nav(p);await refresh(); if(p==='dashboard')return dashboard();if(p==='repos')return reposPage();if(p==='queue')return queuePage();if(p==='browser')return browserPage();if(p==='storage')return storagePage();if(p==='integrity')return integrityPage();if(p==='groups')return groupsPage();if(p==='settings')return settingsPage();if(p==='api')return apiPage()}
function dashboard(){let total=repos.reduce((a,r)=>a+Number(r.archive_size||0),0),failed=repos.reduce((a,r)=>a+Number(r.failed_files||0),0);$('app').innerHTML='<h2>Dashboard</h2><div class="grid"><div class="stat">Repositories<b>'+repos.length+'</b></div><div class="stat">Archived data<b>'+bytes(total)+'</b></div><div class="stat">Versions<b>'+repos.reduce((a,r)=>a+Number(r.version_count||0),0)+'</b></div><div class="stat">Failed files<b>'+failed+'</b></div></div><div class="card"><h3>What’s New?</h3><div id="activity">Loading…</div></div><div class="card"><h3>Repository health</h3>'+repoRows(repos)+'</div>' ;loadActivity()}
function repoRows(rs){return '<table><thead><tr><th>Repository</th><th>Group</th><th>Latest</th><th>Versions</th><th>Size</th><th>Status</th><th>Monitoring</th><th></th></tr></thead><tbody>'+rs.map(r=>'<tr><td><b>'+esc(r.name)+'</b><br><small>'+esc(r.full_name)+'</small></td><td>'+esc(r.group_name||'—')+'</td><td>'+esc(r.latest_version||'—')+'</td><td>'+r.version_count+'</td><td>'+bytes(r.archive_size)+'</td><td class="'+esc(r.status)+'">'+esc(r.status)+'</td><td><label class="switch"><input type="checkbox" '+(r.monitoring?'checked':'')+' data-change="setMonitoring('+r.id+',this.checked)"> '+(r.monitoring?'On':'Off')+'</label></td><td><button data-click="repoPage('+r.id+')">Open</button></td></tr>').join('')+'</tbody></table>'}
async function loadActivity(){let a=await api('/api/activity');$('activity').innerHTML=a.length?'<table><thead><tr><th>Time</th><th>Repository</th><th>Item</th><th>Status</th></tr></thead><tbody>'+a.slice(0,30).map(x=>'<tr><td>'+fmt(x.timestamp)+'</td><td>'+esc(x.full_name)+'</td><td>'+esc(x.version||'')+'</td><td class="'+esc(x.status)+'">'+esc(x.status)+'</td></tr>').join('')+'</tbody></table>':'<p class="muted">No activity yet.</p>'}
function reposPage(){$('app').innerHTML='<h2>Repositories</h2><div class="toolbar"><input id="rq" placeholder="Search repositories..." data-input="filterRepos()"><button data-click="addRepo()">Add Repository</button><button data-click="addUser()">Add User</button></div><div id="rtable">'+repoRows(repos)+'</div>'}
function filterRepos(){let q=$('rq').value.toLowerCase();$('rtable').innerHTML=repoRows(repos.filter(r=>(r.full_name+' '+(r.group_name||'')).toLowerCase().includes(q)))}
async function addRepo(){
  document.body.insertAdjacentHTML('beforeend',`<div class="modal-backdrop" id="addRepoModal"><div class="modal">
    <h3>Add repository</h3><p class="muted">Start tracking a GitHub repository.</p>
    <div class="field"><input id="repoUrlInput" type="text" placeholder="https://github.com/owner/repository"></div>
    <label class="toggle"><input id="archiveHistory" type="checkbox"><span><b>Download all previous releases</b><br><small>Queue every existing release, its assets and exact source before continuing with new releases.</small></span></label>
    <div class="modal-actions"><button data-click="document.getElementById('addRepoModal').remove()">Cancel</button><button class="primary" data-click="submitAddRepo()">Add Repository</button></div>
  </div></div>`);
  setTimeout(()=>document.getElementById('repoUrlInput')?.focus(),0);
}
async function submitAddRepo(){
  let url=document.getElementById('repoUrlInput')?.value.trim(), history=document.getElementById('archiveHistory')?.checked;
  if(!url)return;
  try{await api('/api/repos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,archive_history:history})});document.getElementById('addRepoModal').remove();await page('repos')}
  catch(e){alert(e.message)}
}
async function checkAll(){try{await api('/api/check-all',{method:'POST'});await page(state.page)}catch(e){alert(e.message)}}
async function repoPage(id){await refresh();let r=repos.find(x=>x.id===id);let vs=await api('/api/repos/'+id+'/versions');let pol={releases:true,assets:true,source:true,current:true,tags:true,artifacts:false,commits:false,prereleases:false,...JSON.parse(r.policy||'{}')};$('app').innerHTML='<div class="toolbar"><button data-click="page(\'repos\')">← Repositories</button><button data-click="checkOne('+id+')">Check Now</button><button class="danger" data-click="delRepo('+id+')">Delete</button></div><div class="card"><h2>'+esc(r.full_name)+'</h2><p class="muted">'+esc(r.url)+' · branch: '+esc(r.default_branch||'—')+'</p><div class="grid"><div class="stat">Latest<b>'+esc(r.latest_version||'—')+'</b></div><div class="stat">Versions<b>'+r.version_count+'</b></div><div class="stat">Archive size<b>'+bytes(r.archive_size)+'</b></div><div class="stat">Status<b class="'+esc(r.status)+'">'+esc(r.status)+'</b></div></div></div><div class="card"><h3>Archive policy</h3><div class="toolbar"><label>Detection mode <select id="pmode"><option value="release_fallback" '+((pol.mode||'release_fallback')==='release_fallback'?'selected':'')+'>Release + tag fallback</option><option value="tags_only" '+(pol.mode==='tags_only'?'selected':'')+'>Tags only</option><option value="both" '+(pol.mode==='both'?'selected':'')+'>Both releases and tags</option></select></label></div><div class="check">'+Object.entries({releases:'Releases',assets:'Release assets',source:'Exact source',current:'Current source',tags:'Tags',artifacts:'Actions artifacts',commits:'Commit snapshots',prereleases:'Prereleases'}).map(([k,v])=>'<label><input type="checkbox" id="p-'+k+'" '+(pol[k]?'checked':'')+'> '+v+'</label>').join('')+'</div><br><button class="primary" data-click="savePolicy('+id+')">Save Policy</button></div><div class="card"><h3>Version history</h3>'+(vs.length?vs.map(v=>'<div class="card"><div class="actions" style="justify-content:space-between"><div><b>'+esc(v.version)+'</b> <span class="pill">'+esc(v.kind)+'</span><br><small>'+fmt(v.published_at)+'</small></div><span class="'+esc(v.status)+'">'+esc(v.status)+'</span><button data-click="versionPage('+v.id+')">Files</button></div></div>').join(''):'<p class="muted">No versions yet.</p>')+'</div>'}
async function savePolicy(id){let keys=['releases','assets','source','current','tags','artifacts','commits','prereleases'],p={mode:document.getElementById('pmode').value};keys.forEach(k=>p[k]=document.getElementById('p-'+k).checked);try{await api('/api/repos/'+id+'/policy',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({policy:p})});alert('Policy saved')}catch(e){alert(e.message)}}
async function setMonitoring(id,enabled){try{await api("/api/repos/"+id+"/monitoring",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled})});repos=await api("/api/repos")}catch(e){alert(e.message);page("repos")}}
async function checkOne(id){try{await api('/api/repos/'+id+'/check',{method:'POST'});await repoPage(id)}catch(e){alert(e.message)}}
async function delRepo(id){if(!confirm('Delete repository and its database record? Files on disk are NOT automatically deleted.'))return;await api('/api/repos/'+id,{method:'DELETE'});page('repos')}
async function versionPage(id){let d=await api('/api/versions/'+id),v=d.version;$('app').innerHTML='<div class="toolbar"><button data-click="repoPage('+v.repo_id+')">← Versions</button></div><div class="card"><h2>'+esc(v.repo_name)+' — '+esc(v.version)+'</h2><p><span class="'+esc(v.status)+'">'+esc(v.status)+'</span> · '+esc(v.kind)+' · '+fmt(v.published_at)+'</p><p class="muted">SHA: '+esc(v.target_sha||'not resolved by release API')+'</p></div><div class="card"><h3>Archived files</h3><table><thead><tr><th>Type</th><th>File</th><th>Size</th><th>Status</th><th>Verification</th><th></th></tr></thead><tbody>'+d.files.map(f=>'<tr><td>'+esc(f.category)+'</td><td>'+esc(f.name)+'</td><td>'+bytes(f.size||f.expected_size)+'</td><td class="'+esc(f.status)+'">'+esc(f.status)+(f.error?'<br><small>'+esc(f.error)+'</small>':'')+'</td><td class="'+esc(f.verify_status||'')+'">'+esc(f.verify_status||'unverified')+'</td><td>'+(f.status==='complete'?'<a class="btn" href="/download/'+f.id+'">Download</a>':'')+'</td></tr>').join('')+'</tbody></table></div>'}
async function queuePage(){let q=await api('/api/queue');$('app').innerHTML='<h2>Download Queue</h2><div class="card"><p class="muted">Downloads run with a persistent queue and '+((await api('/api/settings')).workers)+' worker(s). Interrupted .part files resume automatically.</p></div><table><thead><tr><th>Time</th><th>Repository</th><th>Version</th><th>File</th><th>Status</th><th>Attempts</th><th></th></tr></thead><tbody>'+q.map(x=>'<tr><td>'+fmt(x.queued_at)+'</td><td>'+esc(x.full_name)+'</td><td>'+esc(x.version)+'</td><td>'+esc(x.name)+'</td><td class="'+esc(x.status)+'">'+esc(x.status)+'</td><td>'+x.attempts+'</td><td>'+(x.status==='failed'?'<button data-click="retry('+x.id+')">Retry</button>':'')+'</td></tr>').join('')+'</tbody></table>'}
async function retry(id){await api('/api/queue/'+id+'/retry',{method:'POST'});queuePage()}
async function browserPage(){$('app').innerHTML='<h2>Archive Browser</h2><div class="card"><p class="muted">Browse the archive from the web UI. The database-backed file list is used for direct downloads.</p></div>'+repoRows(repos)}
async function storagePage(){let s=await api('/api/storage'),rs=await api('/api/storage/repos');$('app').innerHTML='<h2>Storage</h2><div class="grid"><div class="stat">Total capacity<b>'+bytes(s.total_bytes)+'</b></div><div class="stat">Free<b>'+bytes(s.free_bytes)+'</b></div><div class="stat">Archive<b>'+bytes(s.archive_bytes)+'</b></div><div class="stat">Files<b>'+s.files+'</b></div></div><div class="card"><h3>Largest repositories</h3><table><thead><tr><th>Repository</th><th>Versions</th><th>Size</th></tr></thead><tbody>'+rs.map(x=>'<tr><td>'+esc(x.full_name)+'</td><td>'+x.versions+'</td><td>'+bytes(x.size)+'</td></tr>').join('')+'</tbody></table></div>'}
async function integrityPage(){$('app').innerHTML='<h2>Integrity Verification</h2><div class="card"><p>Recalculate SHA-256 for every completed archive file and compare it with GitHub's digest when one is available.</p><button class="primary" data-click="verify()">Verify Everything</button><div id="vr"></div></div>'}
async function verify(){$('vr').innerHTML='Checking…';let r=await api('/api/verify');$('vr').innerHTML='<h3>Result</h3><p class="complete">Verified: '+r.ok+'</p><p class="'+(r.failed?'failed':'complete')+'">Failed: '+r.failed+'</p>'}
async function groupsPage(){$('app').innerHTML='<h2>Repository Groups</h2><div class="toolbar"><input id="gn" placeholder="New group name"><button data-click="addGroup()">Create Group</button></div><div class="grid">'+groups.map(g=>'<div class="card"><h3>'+esc(g.name)+'</h3><p class="muted">'+g.repo_count+' repositories</p><button class="danger" data-click="delGroup('+g.id+')">Delete</button></div>').join('')+'</div><div class="card"><h3>Assign repositories</h3>'+repos.map(r=>'<div class="toolbar"><b style="min-width:260px">'+esc(r.full_name)+'</b><select data-change="setGroup('+r.id+',this.value)"><option value="">No group</option>'+groups.map(g=>'<option value="'+g.id+'" '+(String(g.id)===String(r.group_id)?'selected':'')+'>'+esc(g.name)+'</option>').join('')+'</select></div>').join('')+'</div>'}
async function addGroup(){if(!$('gn').value.trim())return;await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('gn').value})});page('groups')}
async function delGroup(id){await api('/api/groups/'+id,{method:'DELETE'});page('groups')}
async function setGroup(rid,gid){if(gid)await api('/api/repos/'+rid+'/group/'+gid,{method:'PUT'});else await api('/api/repos/'+rid+'/group',{method:'DELETE'});await refresh()}
async function settingsPage(){let s=await api('/api/settings');$('app').innerHTML='<h2>Settings</h2><div class="card"><h3>Scheduler</h3><p>Check interval is stored in the archive database and can be changed without rebuilding the container.</p><div class="toolbar"><label>Minutes <input id="interval" type="number" min="1" value="'+s.check_interval_minutes+'"></label><label><input id="pre" type="checkbox" '+(s.include_prereleases?'checked':'')+'> Include prereleases by default</label></div><button class="primary" data-click="saveSettings()">Save Settings</button></div><div class="card"><h3>Connection</h3><p>GitHub token: <b>'+ (s.token_configured?'configured':'not configured')+'</b></p><p>Webhook secret: <b>'+ (s.webhook_configured?'configured':'not configured')+'</b></p><p>Workers: <b>'+s.workers+'</b> (set with DOWNLOAD_WORKERS)</p></div><div class="card"><h3>Webhook</h3><p>Configure a GitHub repository webhook to POST release/tag events to <code>/api/webhook</code>. Set <code>WEBHOOK_SECRET</code> in TrueNAS for signature verification.</p></div>}
async function saveSettings(){await api('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({check_interval_minutes:Number($('interval').value),include_prereleases:$('pre').checked})});alert('Saved')}
function apiPage(){$('app').innerHTML='<h2>REST API</h2><div class="card"><p>The app exposes JSON endpoints for repositories, checks, versions, queue, storage, integrity, groups, search, archive browsing and settings.</p><p><code>GET /api/repos</code></p><p><code>POST /api/check-all</code></p><p><code>GET /api/queue</code></p><p><code>GET /api/storage</code></p><p><code>GET /api/search?q=...</code></p><p><code>GET /api/activity</code></p><p><code>POST /api/webhook</code></p></div>'}

async function runAction(code,e){
  const t=e?.target;
  if(code==="addRepo()") return addRepo();
  if(code==="addUser()") return addUser();
  if(code==="checkAll()") return checkAll();
  if(code==="verify()") return verify();
  if(code==="addGroup()") return addGroup();
  if(code==="saveSettings()") return saveSettings();
  if(code==="submitAddRepo()") return submitAddRepo();
  if(code==="filterRepos()") return filterRepos();
  if(code==="document.getElementById('addRepoModal').remove()") return document.getElementById('addRepoModal')?.remove();
  let m;
  if((m=code.match(/^page\('([^']+)'\)$/))) return page(m[1]);
  if((m=code.match(/^repoPage\((\d+)\)$/))) return repoPage(Number(m[1]));
  if((m=code.match(/^checkOne\((\d+)\)$/))) return checkOne(Number(m[1]));
  if((m=code.match(/^delRepo\((\d+)\)$/))) return delRepo(Number(m[1]));
  if((m=code.match(/^savePolicy\((\d+)\)$/))) return savePolicy(Number(m[1]));
  if((m=code.match(/^versionPage\((\d+)\)$/))) return versionPage(Number(m[1]));
  if((m=code.match(/^retry\((\d+)\)$/))) return retry(Number(m[1]));
  if((m=code.match(/^delGroup\((\d+)\)$/))) return delGroup(Number(m[1]));
  if((m=code.match(/^setMonitoring\((\d+),this\.checked\)$/))) return setMonitoring(Number(m[1]),!!t.checked);
  if((m=code.match(/^setGroup\((\d+),this\.value\)$/))) return setGroup(Number(m[1]),t.value);
}
document.addEventListener('click',e=>{
  const el=e.target.closest('[data-click]');
  if(el) runAction(el.getAttribute('data-click'),e).catch(x=>alert(x.message));
});
document.addEventListener('change',e=>{
  const el=e.target.closest('[data-change]');
  if(el) runAction(el.getAttribute('data-change'),e).catch(x=>alert(x.message));
});
document.addEventListener('input',e=>{
  const el=e.target.closest('[data-input]');
  if(el) runAction(el.getAttribute('data-input'),e).catch(x=>alert(x.message));
});

document.addEventListener('DOMContentLoaded',()=>init());