let repos=[],groups=[],state={page:'dashboard',repo:null},adminToken=sessionStorage.getItem('githubArchiveAdminToken')||'';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=s=>s?new Date(s).toLocaleString():'—';
const bytes=n=>{n=Number(n||0);if(n===0)return'0 B';const k=1024,sizes=['B','KB','MB','GB'];let i=Math.floor(Math.log(n)/Math.log(k));return(n/Math.pow(k,i)).toFixed(2)+' '+sizes[i]};

async function api(u,o={},retry=true){
  const opts={...o,headers:new Headers(o.headers||{})};
  if(adminToken)opts.headers.set('Authorization','Bearer '+adminToken);
  let r=await fetch(u,opts),j=await r.json().catch(()=>({}));
  if(r.status===401&&retry){
    const token=prompt('Archive admin token:');
    if(token){adminToken=token.trim();sessionStorage.setItem('githubArchiveAdminToken',adminToken);return api(u,o,false)}
  }
  if(!r.ok)throw Error(j.detail||j.error||r.statusText||'API error');
  return j;
}

async function init(){
  const app=$('app');
  if(!app){console.warn('App root not found yet'); return;}
  try {
    repos=await api('/api/repos');
    groups=await api('/api/groups');
    await page('dashboard');
  } catch(e) {
    console.error('Init failed:', e);
    app.innerHTML='<h2>Error</h2><p class="error">Failed to load app: '+esc(e.message)+'</p><p class="muted">Check that the backend server is running and reachable.</p>';
  }
}

async function refresh(){
  try { repos=await api('/api/repos'); groups=await api('/api/groups'); }
  catch(e) { console.error('Refresh failed:', e); }
}

function nav(p){
  document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));
  document.getElementById('n-'+p)?.classList.add('active');
}

async function page(p){
  const app=$('app');
  if(!app){console.warn('page('+p+') called before app root exists'); return;}
  state.page=p; nav(p);
  try {
    await refresh();
    if(p==='dashboard')return dashboard();
    if(p==='repos')return reposPage();
    if(p==='queue')return queuePage();
    if(p==='browser')return browserPage();
    if(p==='storage')return storagePage();
    if(p==='integrity')return integrityPage();
    if(p==='groups')return groupsPage();
    if(p==='settings')return settingsPage();
    if(p==='api')return apiPage();
    return Promise.resolve();
  } catch(e) {
    app.innerHTML='<h2>Error loading page</h2><p class="error">'+esc(e.message)+'</p>';
  }
}

function dashboard(){
  const app=$('app');
  if(!app) return;
  let total=repos.reduce((a,r)=>a+Number(r.archive_size||0),0),failed=repos.reduce((a,r)=>a+Number(r.failed_files||0),0);
  app.innerHTML='<h2>Dashboard</h2><div class="grid"><div class="stat">Total archived<b>'+bytes(total)+'</b></div><div class="stat">Repositories<b>'+repos.length+'</b></div><div class="stat">Failed files<b>'+failed+'</b></div></div><div id="githubRate" class="card"><p class="muted">Checking GitHub API rate limit…</p></div><div id="activity" class="card"><p class="muted">Loading activity…</p></div>';
  loadRateLimit();
  loadActivity();
}

async function loadRateLimit(){
  const box=$('githubRate');
  if(!box)return;
  try{
    const r=await api('/api/github/rate-limit');
    if(!r.token_configured){
      box.innerHTML='<p class="error"><b>GitHub API: Unauthenticated</b><br>GITHUB_TOKEN is not configured. GitHub API requests are subject to the low unauthenticated limit.</p>';
      return;
    }
    const remaining=r.remaining==null?'—':Number(r.remaining).toLocaleString();
    const limit=r.limit==null?'—':Number(r.limit).toLocaleString();
    const reset=r.reset_at?fmt(r.reset_at):'—';
    box.innerHTML='<p><b>GitHub API: Authenticated</b> — '+remaining+' / '+limit+' requests remaining</p><small class="muted">Rate-limit window resets: '+esc(reset)+'</small>';
  }catch(e){
    box.innerHTML='<p class="error">GitHub API rate-limit status unavailable: '+esc(e.message)+'</p>';
  }
}

async function loadActivity(){
  const activity=$('activity');
  if(!activity) return;
  try{
    let a=await api('/api/activity');
    activity.innerHTML=a.length?'<table><thead><tr><th>Time</th><th>Repository</th><th>Item</th><th>Status</th></tr></thead><tbody>'+a.map(x=>'<tr><td>'+fmt(x.timestamp)+'</td><td>'+esc(x.full_name)+'</td><td>'+esc(x.version||x.name)+'</td><td><span class="status '+x.status+'">'+x.status+'</span></td></tr>').join('')+'</tbody></table>':'<p class="muted">No activity yet.</p>';
  } catch(e){
    activity.innerHTML='<p class="error">Failed to load activity</p>';
  }
}

function reposPage(){const app=$('app'); if(!app) return; app.innerHTML='<h2>Repositories</h2><div class="toolbar"><input id="rq" placeholder="Search repositories..." data-input="filterRepos()"><button data-click="addRepo()">Add Repository</button><button data-click="addUser()">Import User</button><button data-click="checkAll()">Check All</button></div><div id="rtable" class="card">'+repoRows(repos)+'</div>'}
function repoRows(rs){return '<table><thead><tr><th>Repository</th><th>Group</th><th>Latest</th><th>Versions</th><th>Size</th><th>Status</th><th>Monitoring</th><th></th></tr></thead><tbody>'+rs.map(r=>'<tr><td><a href="https://github.com/'+esc(r.full_name)+'" target="_blank">'+esc(r.full_name)+'</a></td><td>'+(r.group_name||'—')+'</td><td>'+esc(r.latest_version||'—')+'</td><td>'+r.version_count+'</td><td>'+bytes(r.archive_size||0)+'</td><td><span class="status '+r.status+'">'+r.status+'</span>'+(r.error?'<br><small class="error">'+esc(r.error)+'</small>':'')+'</td><td><label class="toggle"><input type="checkbox" data-change="setMonitoring('+r.id+',this.checked)" '+(r.monitoring?'checked':'')+'><span></span></label></td><td><button data-click="repoPage('+r.id+')">…</button></td></tr>').join('')+'</tbody></table>'}
function filterRepos(){let q=$('rq').value.toLowerCase();$('rtable').innerHTML=repoRows(repos.filter(r=>(r.full_name+' '+(r.group_name||'')).toLowerCase().includes(q)))}

async function addUser(){ const username=prompt("GitHub username (for example octocat):"); if(username===null)return; const clean=username.trim().replace(/^@/,""); if(!clean)return; const archiveAll=confirm("Archive ALL existing releases for every repository owned by this user?\n\nOK = historical import\nCancel = monitor from now"); try{const r=await api('/api/users/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:clean,archive_all:archiveAll})}); alert("Imported "+r.count+" repositories. Downloads have been queued in the background."); await page('repos');}catch(e){alert(e.message)}}
async function addRepo(){ document.body.insertAdjacentHTML('beforeend',`<div class="modal-backdrop" id="addRepoModal"><div class="modal"><h3>Add repository</h3><p class="muted">Start tracking a GitHub repository.</p><div class="field"><input id="repoUrlInput" type="text" placeholder="https://github.com/owner/repository"></div><label class="toggle"><input id="archiveHistory" type="checkbox"><span><b>Download all previous releases</b><br><small>Queue every existing release, its assets and exact source before continuing.</small></span></label><div class="modal-actions"><button data-click="document.getElementById('addRepoModal').remove()">Cancel</button><button class="primary" data-click="submitAddRepo()">Add Repository</button></div></div></div>`); setTimeout(()=>document.getElementById('repoUrlInput')?.focus(),0); }
async function submitAddRepo(){ const url=document.getElementById('repoUrlInput')?.value.trim(), history=document.getElementById('archiveHistory')?.checked; if(!url)return; try{await api('/api/repos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,archive_history:history})});document.getElementById('addRepoModal').remove();await page('repos')}catch(e){alert(e.message)} }
async function checkAll(){try{await api('/api/check-all',{method:'POST'});await page(state.page)}catch(e){alert(e.message)}}

async function repoPage(id){
  try { await refresh(); let r=repos.find(x=>x.id===id); let vs=await api('/api/repos/'+id+'/versions'); let pol={releases:true,assets:true,source:true,current:true,tags:true,artifacts:false,commits:false,prereleases:false}; try{pol=JSON.parse(r.policy)}catch(e){} $('app').innerHTML='<div class="toolbar"><button data-click="page(\'repos\')">← Repositories</button></div><div class="grid"><div class="stat">Repository<b>'+esc(r.full_name)+'</b></div><div class="stat">Status<b><span class="status '+r.status+'">'+r.status+'</span></b></div><div class="stat">Versions<b>'+vs.length+'</b></div><div class="stat">Size<b>'+bytes(r.archive_size||0)+'</b></div></div><div class="card"><h3>Policy</h3><label>Mode<select id="pmode"><option value="release_fallback" '+(pol.mode==='release_fallback'?'selected':'')+'>Release with tag fallback</option><option value="tags_only" '+(pol.mode==='tags_only'?'selected':'')+'>Tags only</option><option value="both" '+(pol.mode==='both'?'selected':'')+'>Both releases and tags</option></select></label><label class="toggle"><input type="checkbox" '+(pol.releases?'checked':'')+'> Releases</label><label class="toggle"><input type="checkbox" '+(pol.assets?'checked':'')+'> Assets</label><label class="toggle"><input type="checkbox" '+(pol.tags?'checked':'')+'> Tags</label><label class="toggle"><input type="checkbox" '+(pol.source?'checked':'')+'> Exact source</label><label class="toggle"><input type="checkbox" '+(pol.current?'checked':'')+'> Current default branch</label><label class="toggle"><input type="checkbox" '+(pol.artifacts?'checked':'')+'> Actions artifacts</label><label class="toggle"><input type="checkbox" '+(pol.commits?'checked':'')+'> Commit snapshots</label><label class="toggle"><input type="checkbox" '+(pol.prereleases?'checked':'')+'> Prerelease versions</label><button data-click="savePolicy('+id+')">Save Policy</button></div><div class="card"><h3>Versions</h3>'+vs.map(v=>'<div style="padding:12px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center"><div><b>'+esc(v.version)+'</b><br><small>'+v.kind+' • '+fmt(v.published_at)+'</small></div><button data-click="versionPage('+v.id+')">Details</button></div>').join('')+'</div>'; } catch(e) { $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; }
}

async function savePolicy(id){ let keys=['releases','assets','source','current','tags','artifacts','commits','prereleases'],p={mode:document.getElementById('pmode').value}; keys.forEach(k=>p[k]=document.querySelector('label:has(input:nth-child('+keys.indexOf(k)+1+')) input[type=checkbox]')?.checked||false); try{await api('/api/repos/'+id+'/policy',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({policy:p})});alert('Policy saved');await repoPage(id)}catch(e){alert(e.message)} }
async function setMonitoring(id,enabled){try{await api('/api/repos/'+id+'/monitoring',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});repos=await api('/api/repos');}catch(e){alert(e.message)}}
async function checkOne(id){try{await api('/api/repos/'+id+'/check',{method:'POST'});await repoPage(id)}catch(e){alert(e.message)}}
async function delRepo(id){if(!confirm('Delete repository and its database record? Files on disk are NOT automatically deleted.'))return;try{await api('/api/repos/'+id,{method:'DELETE'});page('repos')}catch(e){alert(e.message)}}

async function versionPage(id){
  try { let d=await api('/api/versions/'+id),v=d.version; $('app').innerHTML='<div class="toolbar"><button data-click="repoPage('+v.repo_id+')">← Versions</button></div><div class="grid"><div class="stat">Version<b>'+esc(v.version)+'</b></div><div class="stat">Repository<b><a href="https://github.com/'+esc(v.full_name)+'" target="_blank">'+esc(v.full_name)+'</a></b></div></div><div class="card"><h3>Files</h3><table><thead><tr><th>Category</th><th>Name</th><th>Status</th><th>Size</th></tr></thead><tbody>'+d.files.map(f=>'<tr><td>'+esc(f.category)+'</td><td>'+esc(f.name)+'</td><td><span class="status '+f.status+'">'+f.status+'</span>'+(f.error?'<br><small class="error">'+esc(f.error)+'</small>':'')+'</td><td>'+bytes(f.size||0)+'</td></tr>').join('')+'</tbody></table></div>';} catch(e){ $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; }
}

async function queuePage(){
  try { let q=await api('/api/queue'); let s=await api('/api/settings'); $('app').innerHTML='<h2>Download Queue</h2><div class="card"><p class="muted">Downloads run with a persistent queue and '+s.check_interval_minutes+' minute interval checks.</p><table><thead><tr><th>Repository</th><th>File</th><th>Category</th><th>Status</th><th>Attempts</th><th></th></tr></thead><tbody>'+(q.length?q.map(i=>'<tr><td>'+esc(i.full_name)+'</td><td>'+esc(i.name)+'</td><td>'+i.category+'</td><td><span class="status '+i.status+'">'+i.file_status+'</span>'+(i.error?'<br><small class="error">'+esc(i.error)+'</small>':'')+'</td><td>'+i.attempts+'</td><td>'+(i.file_status==='failed'?'<button data-click="retry('+i.queue_id+')">Retry</button>':'')+'</td></tr>').join(''):'<tr><td colspan="6" class="empty">Queue is empty.</td></tr>')+'</tbody></table></div>';} catch(e){ $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; }
}

async function retry(id){try{await api('/api/queue/'+id+'/retry',{method:'POST'});await queuePage()}catch(e){alert(e.message)}}

async function browserPage(path='repos'){
  try { let d=await api('/api/archive/list?path='+encodeURIComponent(path)); let parts=path.split('/').filter(Boolean); let crumbs='<button data-click="browserPage(\'repos\')">Archive</button>'; let built=''; for(const p of parts.slice(1)){ built+=('/'+p); crumbs+=' <span class="muted">/</span> <button data-click="browserPage(\'repos'+built.replaceAll("'","")+'\')">'+esc(p)+'</button>'; } $('app').innerHTML='<h2>Archive Browser</h2><div class="toolbar">'+crumbs+'</div><div class="card"><table><thead><tr><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead><tbody>'+(d.entries.length?d.entries.map(x=>x.directory?'<tr><td>📁 <b>'+esc(x.name)+'</b></td><td>Directory</td><td>—</td><td><button data-click="browserPage(\''+esc(x.path).replaceAll("'","")+'\')">Open</button></td></tr>':'<tr><td>📄 '+esc(x.name)+'</td><td>File</td><td>'+bytes(x.size)+'</td><td><a class="btn" href="/download/path/'+x.path.split('/').map(encodeURIComponent).join('/')+'">Download</a></td></tr>').join(''):'<tr><td colspan="4" class="empty">Empty directory.</td></tr>')+'</tbody></table></div>';} catch(e){ $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; }
}

async function storagePage(){
  try { let s=await api('/api/storage'),rs=await api('/api/storage/repos'); $('app').innerHTML='<h2>Storage</h2><div class="grid"><div class="stat">Total capacity<b>'+bytes(s.total_bytes)+'</b></div><div class="stat">Used<b>'+bytes(s.archive_bytes)+'</b></div><div class="stat">Free<b>'+bytes(s.free_bytes)+'</b></div></div><div class="card"><table><thead><tr><th>Repository</th><th>Versions</th><th>Size</th></tr></thead><tbody>'+rs.map(r=>'<tr><td>'+esc(r.full_name)+'</td><td>'+r.versions+'</td><td>'+bytes(r.size)+'</td></tr>').join('')+'</tbody></table></div>';} catch(e){ $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; }
}

async function integrityPage(){ $('app').innerHTML='<h2>Integrity Verification</h2><div class="card"><p>Recalculate SHA-256 for every completed archive file and compare it with GitHub\'s digest when available.</p><button data-click="verify()">Verify Everything</button><div id="vr"></div></div>'; }
async function verify(){ try { $('vr').innerHTML='Checking…'; let r=await api('/api/verify'); $('vr').innerHTML='<h3>Result</h3><p class="complete">Verified: '+r.ok+'</p><p class="'+(r.failed?'failed':'complete')+'">Failed: '+r.failed+'</p><p class="muted">Checked '+r.checked+' files.</p>'; } catch(e){ $('vr').innerHTML='<p class="error">'+esc(e.message)+'</p>'; } }
async function groupsPage(){ try { let grps=await api('/api/groups'); $('app').innerHTML='<h2>Repository Groups</h2><div class="toolbar"><input id="gn" placeholder="New group name"><button data-click="addGroup()">Create Group</button></div><div class="card">'+grps.map(g=>'<div style="padding:8px;margin:8px 0;border:1px solid var(--line);border-radius:4px"><b>'+esc(g.name)+'</b> <small class="muted">('+g.repo_count+' repos)</small> <button data-click="delGroup('+g.id+')" style="float:right">Delete</button></div>').join('')||'<p class="muted">No groups yet.</p>'+'</div>';} catch(e){ $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; } }
async function addGroup(){if(!$('gn').value.trim())return;try{await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('gn').value})});page('groups')}catch(e){alert(e.message)}}
async function delGroup(id){try{await api('/api/groups/'+id,{method:'DELETE'});page('groups')}catch(e){alert(e.message)}}
async function setGroup(rid,gid){try{if(gid)await api('/api/repos/'+rid+'/group/'+gid,{method:'PUT'});else await api('/api/repos/'+rid+'/group',{method:'DELETE'});await refresh()}catch(e){alert(e.message)}}

async function settingsPage(){
  try { let s=await api('/api/settings'); $('app').innerHTML='<h2>Settings</h2><div class="card"><h3>Scheduler</h3><p>Check interval is stored in the archive database and can be modified here.</p><label>Check interval (minutes)<input id="interval" type="number" value="'+s.check_interval_minutes+'" min="1"></label><label class="toggle"><input type="checkbox" id="prereleases" '+(s.include_prereleases?'checked':'')+'> Include prerelease versions</label><button data-click="saveSettings()">Save Settings</button></div><div class="card"><h3>System</h3><p>Workers: '+s.workers+'<br>Token configured: '+(s.token_configured?'Yes':'No')+'<br>Webhook configured: '+(s.webhook_configured?'Yes':'No')+'<br>Admin auth configured: '+(s.admin_auth_configured?'Yes':'No')+'</p></div>';} catch(e){ $('app').innerHTML='<h2>Error</h2><p class="error">'+esc(e.message)+'</p>'; }
}

async function saveSettings(){try{await api('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({check_interval_minutes:Number($('interval').value),include_prereleases:document.getElementById('prereleases').checked})});await page('settings')}catch(e){alert(e.message)}}
function apiPage(){$('app').innerHTML='<h2>REST API</h2><div class="card"><p>The app exposes JSON endpoints for repositories, checks, versions, queue, storage, integrity, groups, search, archive browsing, and integrity verification. See the <a href="https://github.com/sweenyxsky-oss/Github-Archive" target="_blank">GitHub repository</a> for API documentation.</p></div>'}

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
  if((m=code.match(/^browserPage\('([^']*)'\)$/))) return browserPage(m[1]);
  if((m=code.match(/^repoPage\((\d+)\)$/))) return repoPage(Number(m[1]));
  if((m=code.match(/^checkOne\((\d+)\)$/))) return checkOne(Number(m[1]));
  if((m=code.match(/^delRepo\((\d+)\)$/))) return delRepo(Number(m[1]));
  if((m=code.match(/^savePolicy\((\d+)\)$/))) return savePolicy(Number(m[1]));
  if((m=code.match(/^versionPage\((\d+)\)$/))) return versionPage(Number(m[1]));
  if((m=code.match(/^retry\((\d+)\)$/))) return retry(Number(m[1]));
  if((m=code.match(/^delGroup\((\d+)\)$/))) return delGroup(Number(m[1]));
  if((m=code.match(/^setMonitoring\((\d+),this\.checked\)$/))) return setMonitoring(Number(m[1]),!!t.checked);
  if((m=code.match(/^setGroup\((\d+),this\.value\)$/))) return setGroup(Number(m[1]),t.value);
  return Promise.resolve();
}

document.addEventListener('click',e=>{ const el=e.target.closest('[data-click]'); if(el) runAction(el.getAttribute('data-click'),e).catch(x=>alert(x.message)); });
document.addEventListener('change',e=>{ const el=e.target.closest('[data-change]'); if(el) runAction(el.getAttribute('data-change'),e).catch(x=>alert(x.message)); });
document.addEventListener('input',e=>{ const el=e.target.closest('[data-input]'); if(el) runAction(el.getAttribute('data-input'),e).catch(x=>alert(x.message)); });

if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}
