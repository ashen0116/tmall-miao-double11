'use strict';
const $ = id => document.getElementById(id);
const busyStates = new Set(['connecting', 'scanning', 'running', 'paused', 'stopping']);
const statusLabels = {disconnected:'等待连接',connecting:'正在连接',ready:'设备已就绪',scanning:'识别中',running:'正在执行',paused:'已暂停',stopping:'正在停止',stopped:'已停止',finished:'本轮已结束',needs_attention:'需要手动处理',error:'操作失败'};
const phaseLabels = {locating:'LOCATING CAMPAIGN',ready:'READY TO GO',opening:'OPENING TASK',browsing:'BROWSING IN PROGRESS',returning:'BACK TO YOUR TASKS',scanning:'FINDING MORE TASKS',finished:'SESSION FINISHED',stopped:'SESSION STOPPED',needs_attention:'YOUR ATTENTION NEEDED'};
const taskLabels = {pending:'待执行',running:'执行中',done:'已核验',manual:'手动处理',failed:'未通过',disabled:'已跳过'};
const kindLabels = {browse:'浏览',search:'搜索',visit:'跳转',claim:'领奖',manual:'手动',done:'完成'};
let state, token='', filter='all', taskHash='', logsHash='', initialized=false, requesting=false, toastTimer;
let pollRequest=null, pollTimer, previewTimer, previewRequest=null, previewReady=false, pageActive=true, lastPreviewSequence=null, polling=false, previewing=false;
const clockText=seconds=>{seconds=Math.max(0,Math.floor(seconds||0));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;};
function el(tag, cls, text) {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6000);}
function config(){return {default_browse_seconds:Number($('browse-seconds').value),browse_extra_seconds:Number($('browse-extra').value),max_attempts:Number($('max-attempts').value),max_tasks:Number($('max-tasks').value),enabled_task_kinds:['browse','search','visit','claim'].filter(k=>$('kind-'+k).checked),allow_cross_app:$('cross-app').checked,allowed_cross_apps:[...document.querySelectorAll('#cross-apps input:checked')].map(i=>i.value),ocr:$('ocr').checked,scroll_browse:$('scroll').checked,claim_rewards:$('claim').checked};}
let appsHash='';
function renderApps(){
  const options=state?.cross_app_options||[];if(!options.length)return;
  const applied=state?.config?.allowed_cross_apps||[];
  const hash=JSON.stringify([options,applied]);if(hash===appsHash)return;appsHash=hash;
  const selected=new Set(applied),box=$('cross-apps');
  box.replaceChildren(el('span','field-label','允许跳转的应用'));
  for(const name of options){const label=el('label');const input=document.createElement('input');
    input.type='checkbox';input.value=name;input.checked=selected.has(name);
    label.append(input,document.createTextNode(name));box.append(label);}
}
async function action(name){
  if(requesting)return;
  if(['run','connect','configure'].includes(name)&&!$('settings-form').reportValidity())return;
  if(name==='run'&&!config().enabled_task_kinds.length){toast('请至少选择一种本轮任务类型');return;}
  requesting=true;
  try{const payload={config:config(),auto_open:document.querySelector('input[name=mode]:checked').value==='auto'};
    if(name==='connect'){payload.device_type=$('device-type').value;payload.serial=$('serial').value.trim();}
    const response=await fetch('/api/'+name,{method:'POST',headers:{'Content-Type':'application/json','X-Miao-Token':token},body:JSON.stringify(payload)});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'操作失败');if(name==='shutdown'){toast('助手已退出，可以关闭此页面');return;}if(name==='configure')toast('执行设置已应用');await poll();
  }catch(error){toast(error.message);}finally{requesting=false;}
}
async function discover(){
  $('discover').disabled=true;
  try{const response=await fetch('/api/devices');if(!response.ok)throw new Error('设备检测失败');const data=await response.json();const options=$('device-options');options.replaceChildren();
    for(const d of data.devices){const o=el('option','',`${d.model||d.type} · ${d.status}`);o.value=d.serial;options.append(o);}
    const ready=data.devices.filter(d=>d.status==='device');
    if(ready.length===1&&!$('serial').value){$('serial').value=ready[0].serial;$('device-type').value=ready[0].type;}
    $('device-hint').textContent=data.devices.length?data.devices.map(d=>`${d.model||d.serial} · ${{device:'已连接',unauthorized:'请在手机允许 USB 调试',offline:'设备离线'}[d.status]||d.status}`).join('；'):(data.errors.join('；')||'暂未检测到设备。请检查数据线和 USB 调试，也可填入已连接的无线调试地址。');
  }catch(e){toast(e.message);}finally{$('discover').disabled=false;}
}
function renderTasks(force=false){
  const tasks=state.tasks||[];const hash=JSON.stringify(tasks)+filter;if(!force&&hash===taskHash)return;taskHash=hash;$('task-count').textContent=tasks.length;
  const list=tasks.filter(t=>filter==='all'||(filter==='manual'?['manual','failed','disabled'].includes(t.status):!['manual','disabled'].includes(t.status)));
  const table=$('task-table');table.replaceChildren();
  if(!list.length){const box=el('div','empty-state');box.append(el('div','empty-icon','☷'),el('h3','',tasks.length?'这个分类暂时没有任务':'任务还没入场'),el('p','',tasks.length?'切换其他分类查看执行情况。':'连接设备后点击「开始自动执行」，会先识别当前任务。'));table.append(box);return;}
  for(const t of list){const row=el('div','task-row');const info=el('div');const title=el('div','task-title');title.append(el('span','task-type',kindLabels[t.kind]||t.kind),el('span','',t.title));info.append(title,el('div','task-detail',t.reason||t.detail||'按页面要求浏览并返回'));const progress=el('div','task-progress',`${t.current}/${t.total}`);row.append(info,progress,el('span','task-status '+t.status,taskLabels[t.status]||t.status));table.append(row);}
}
function renderLogs(){const logs=state.logs||[];const hash=logs.length?String(logs[logs.length-1].id):'';if(hash===logsHash)return;logsHash=hash;const list=$('log-list');const atBottom=list.scrollTop+list.clientHeight>=list.scrollHeight-30;list.replaceChildren();for(const log of logs){const line=el('div','log-line '+log.level);line.append(el('time','',log.time.slice(11,19)),el('span','log-level',log.level),el('span','log-text',log.message));list.append(line);}if(atBottom)list.scrollTop=list.scrollHeight;}
function render(){
  $('app-version').textContent=state.version||'';
  const busy=busyStates.has(state.status)||state.busy,running=state.status==='running',paused=state.status==='paused';
  $('connection-label').className='status-pill'+(state.connected?' connected':'');$('connection-label').replaceChildren(el('span'),document.createTextNode(statusLabels[state.status]||state.status));
  $('connect').disabled=busy;$('connect').textContent=state.connected?'重新连接 ↗':'连接设备 ↗';
  for(const id of ['run','capture'])$(id).disabled=!state.connected||busy;
  $('run').textContent=busy?'正在处理…':'▶ 开始自动执行';
  $('pause').disabled=!running&&!paused;$('pause').textContent=paused?'▶ 继续':'Ⅱ 暂停';$('stop').disabled=!busy||state.status==='stopping';
  $('device-type').disabled=busy;$('serial').disabled=busy;
  $('save-settings').disabled=busy;
  $('phase-label').textContent=phaseLabels[state.phase]||'READY WHEN YOU ARE';
  $('execution-title').textContent=paused?'休息一下，进度还在':state.current_task||({disconnected:'准备好，再出发',ready:'手机已就绪',finished:'本轮任务已处理',needs_attention:'这一步，需要你来处理',error:'连接或执行遇到问题',stopped:'已停止执行'}[state.status]||statusLabels[state.status]);
  $('message').textContent=state.message;$('countdown').hidden=!running||!state.remaining;$('remaining').textContent=state.remaining||0;
  document.querySelector('.execution-card').classList.toggle('is-active',running);document.querySelector('.execution-card').classList.toggle('needs-attention',['error','needs_attention'].includes(state.status));
  const round=state.stats||{},daily=state.daily,stats=daily?.stats||{};
  for(const [id,key] of [['completed','completed'],['claims','claims'],['cross','cross_app'],['skipped','skipped'],['failed','failed']])$('stat-'+id).textContent=stats[key]||0;
  $('daily-label').textContent=daily?`${daily.date} · 当前设备今日累计`:'连接后读取本设备今日统计';
  $('daily-note').textContent=daily?`今日运行 ${clockText(stats.elapsed)} · 已确认不可用 ${daily.unavailable_count||0} 项 · 诊断记录 ${daily.failures_count||0} 条`:'同一设备当天累计保存，重新开始不会清零';
  $('round-summary').textContent=`本轮 · 已尝试 ${round.attempts||0} 次 · 核验完成 ${round.completed||0} 次 · 领奖 ${round.claims||0} 次 · 未通过 ${round.failed||0} 项`;
  $('elapsed').textContent=`本轮运行 ${clockText(round.elapsed)}`;
  $('diagnostics').hidden=!daily;
  $('failure-folder').textContent=daily?.failures_dir||'连接后生成本设备当天的诊断目录';
  const failureTask=state.last_failure?.task;
  const failureTitle=typeof failureTask==='string'?failureTask:failureTask?.title;
  $('last-failure').textContent=state.last_failure?`最近记录：${state.last_failure.reason||'执行未通过'}${failureTitle?' · '+failureTitle:''}`:'遇到验证、恢复失败或核验失败时记录现场，原始截图仅保存在本机。';
  $('package-name').textContent=state.package||'设备未连接';$('preview-source').textContent=state.source==='ocr'?'本地 OCR 识别':state.source==='ui'?'页面控件识别':'手机截图预览';
  if(!initialized){const c=state.config;$('browse-seconds').value=c.default_browse_seconds;$('browse-extra').value=c.browse_extra_seconds;$('max-attempts').value=c.max_attempts;$('max-tasks').value=c.max_tasks;$('cross-app').checked=c.allow_cross_app;$('ocr').checked=c.ocr;$('scroll').checked=c.scroll_browse;$('claim').checked=c.claim_rewards;for(const k of ['browse','search','visit','claim'])$('kind-'+k).checked=(c.enabled_task_kinds||['browse','search','visit','claim']).includes(k);initialized=true;}
  renderApps();renderTasks();renderLogs();preview();
}
async function poll(){
  if(pollRequest)return pollRequest;
  pollRequest=(async()=>{try{const response=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!response.ok)throw new Error('控制台连接失败');state=await response.json();token=state.token;render();}catch(e){$('connection-label').replaceChildren(el('span'),document.createTextNode('服务已断开'));for(const id of ['run','connect','capture','pause','stop'])$(id).disabled=true;}})();
  try{await pollRequest;}finally{pollRequest=null;}
}
async function preview(){
  if(!state?.connected||document.hidden||previewRequest||!pageActive)return;
  const sequence=state.image_sequence,updatedAt=state.image_updated_at;
  if(sequence!==undefined&&sequence===lastPreviewSequence)return;
  // The visible image is replaced only after the previous load finishes.
  // Rapid timers must not cancel an in-flight screenshot on slower devices.
  previewRequest=new Promise(resolve=>{
    const image=$('phone-image');
    const complete=ok=>{clearTimeout(timeout);image.removeEventListener('load',loaded);image.removeEventListener('error',failed);if(ok){lastPreviewSequence=sequence;previewReady=true;image.hidden=false;$('phone-placeholder').hidden=true;$('preview-time').textContent=new Date(updatedAt||Date.now()).toLocaleTimeString('zh-CN',{hour12:false});}else if(!previewReady){image.hidden=true;$('phone-placeholder').hidden=false;}resolve();};
    const loaded=()=>complete(true),failed=()=>complete(false);
    const timeout=setTimeout(()=>complete(false),10000);
    image.addEventListener('load',loaded,{once:true});image.addEventListener('error',failed,{once:true});
    image.src='/api/screenshot?v='+(sequence??Date.now());
  });
  try{await previewRequest;}finally{previewRequest=null;}
}
async function pollLoop(){clearTimeout(pollTimer);if(!pageActive||polling)return;polling=true;try{await poll();}finally{polling=false;if(pageActive)pollTimer=setTimeout(pollLoop,document.hidden?5000:300);}}
async function previewLoop(){clearTimeout(previewTimer);if(!pageActive||document.hidden||previewing)return;previewing=true;try{await preview();}finally{previewing=false;if(pageActive&&!document.hidden)previewTimer=setTimeout(previewLoop,300);}}
document.addEventListener('visibilitychange',()=>{clearTimeout(pollTimer);clearTimeout(previewTimer);pollLoop();if(!document.hidden)previewLoop();});
window.addEventListener('pagehide',()=>{pageActive=false;clearTimeout(pollTimer);clearTimeout(previewTimer);});
window.addEventListener('pageshow',event=>{if(event.persisted){pageActive=true;pollLoop();previewLoop();}});
for(const id of ['connect','run','capture','stop'])$(id).addEventListener('click',()=>action(id));
$('pause').addEventListener('click',()=>action(state?.status==='paused'?'resume':'pause'));
$('save-settings').addEventListener('click',()=>action('configure'));
$('shutdown').addEventListener('click',()=>action('shutdown'));
$('discover').addEventListener('click',discover);
$('settings-form').addEventListener('submit',event=>event.preventDefault());
for(const button of document.querySelectorAll('[data-filter]'))button.addEventListener('click',()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));if(state)renderTasks(true);});
for(const name of ['tasks','logs'])$(name+'-tab').addEventListener('click',()=>{for(const other of ['tasks','logs']){const selected=name===other;$(other+'-tab').classList.toggle('active',selected);$(other+'-tab').setAttribute('aria-selected',String(selected));$(other+'-panel').hidden=!selected;}});
poll().then(()=>{discover();previewLoop();pollLoop();});
