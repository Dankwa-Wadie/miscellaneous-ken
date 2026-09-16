'use strict';
let state, page='overview', filter='all', editing=null, saving=false, renderedPage=null, lastDiskLow=null;
const $=s=>document.querySelector(s), esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={ready:'Ready to upload',uploaded:'Uploaded',upload_unknown:'Check upload',needs_render:'Needs rendering',render_failed:'Render failed',rendering:'Rendering',uploading:'Uploading'};
const date=s=>s?new Date(s).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const media=(v,kind)=>'/api/media?id='+encodeURIComponent(v.id)+'&kind='+kind;
function message(text,error=false){$('#notice').textContent=text;$('#notice').className=error?'error':'';if($('#editor').open){let n=$('#editor-notice');if(!n){n=document.createElement('p');n.id='editor-notice';$('#editor-content').prepend(n);}n.className=error?'error-text':'status';n.textContent=text;}}
async function api(path,body){let r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':state.csrf},body:JSON.stringify(body)});let data=await r.json();if(!r.ok)throw Error(data.error);return data;}
async function act(fn){if(saving)return;saving=true;try{await fn();await refresh(true,true);}catch(e){message(e.message,true);}finally{saving=false;}}

function formatCountdown(targetEpoch){
  if(!targetEpoch) return 'When online';
  let diff = Math.floor(targetEpoch - Date.now() / 1000);
  if(diff <= 0) return 'Due now';
  let h = Math.floor(diff / 3600);
  let m = Math.floor((diff % 3600) / 60);
  let s = diff % 60;
  let pad = n => String(n).padStart(2, '0');
  if(h > 0) return `${h}h ${pad(m)}m ${pad(s)}s`;
  return `${pad(m)}:${pad(s)}`;
}

function getCountdownText(){
  if(!state) return '';
  let a = state.automation;
  if(!a.enabled) return 'Paused';
  if(!a.next_run || a.next_run <= Date.now()/1000){
    if(state.busy) return 'After current job';
    if(state.online === false) return 'Waiting for internet';
    if(state.free_disk_bytes < 512*1024*1024) return 'Waiting for disk space';
    return 'Due now';
  }
  return formatCountdown(a.next_run);
}

function getCountdownSub(){
  if(!state) return '';
  let a = state.automation;
  if(!a.enabled || !a.next_run) return '';
  return `at ${new Date(a.next_run*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}`;
}

function getAutomationNextRunText(){
  if(!state) return '';
  let a = state.automation;
  if(!a.enabled) return 'Paused';
  return `${getCountdownText()}${a.next_run ? ` (${date(a.next_run*1000)})` : ''}`;
}

function tickCountdown(){
  if(!state) return;
  let countdownEl = document.getElementById('next-run-countdown');
  let timeEl = document.getElementById('next-run-time');
  if(countdownEl){
    let txt = getCountdownText();
    if(countdownEl.textContent !== txt) countdownEl.textContent = txt;
  }
  if(timeEl){
    let sub = getCountdownSub();
    if(timeEl.textContent !== sub) timeEl.textContent = sub;
  }
  let autoRow = document.getElementById('automation-next-run');
  if(autoRow){
    let autoTxt = getAutomationNextRunText();
    if(autoRow.textContent !== autoTxt) autoRow.textContent = autoTxt;
  }
}

function updateCharCounts(){
  let f = $('#video-form');
  if(!f) return;
  ['headline', 'title'].forEach(k => {
    let inp = f.elements[k];
    let el = document.getElementById('char-count-' + k);
    if(inp && el){
      let max = inp.getAttribute('maxlength') || '100';
      el.textContent = `${inp.value.length}/${max}`;
    }
  });
  let desc = f.elements['description'];
  let descEl = document.getElementById('char-count-description');
  if(desc && descEl){
    descEl.textContent = `${desc.value.length}/4900`;
  }
}

function cardHtml(v){
  let updatedParam = v.updated_at ? `&t=${encodeURIComponent(v.updated_at)}` : '';
  return `<button class="video-card" data-video="${esc(v.id)}" data-updated="${esc(v.updated_at||'')}"><img src="${media(v,'poster')}${updatedParam}" alt="Preview of ${esc(v.headline)}" loading="lazy"><div class="info"><span class="status">${esc(labels[v.status]||v.status)}</span><strong>${esc(v.headline)}</strong>${v.source?`<span class="source-tag">${esc(v.source)}</span>`:''}<span class="meta">${date(v.created_at)}</span></div></button>`;
}
function cards(videos){
  return videos.length ? `<div class="grid">${videos.map(cardHtml).join('')}</div>` : '<div class="empty"><h3>No videos here yet</h3><p>Create a preview to see the pipeline’s next story.</p></div>';
}
function updateCards(container, videos){
  if(!container) return;
  if(!videos || !videos.length){
    let emptyHtml = '<div class="empty"><h3>No videos here yet</h3><p>Create a preview to see the pipeline’s next story.</p></div>';
    if(container.innerHTML !== emptyHtml) container.innerHTML = emptyHtml;
    return;
  }
  let grid = container.querySelector('.grid');
  if(!grid){
    container.innerHTML = '<div class="grid"></div>';
    grid = container.querySelector('.grid');
  }
  let existingCards = new Map();
  grid.querySelectorAll('.video-card').forEach(el => {
    if(el.dataset.video) existingCards.set(el.dataset.video, el);
  });
  let currentIds = new Set(videos.map(v => v.id));
  for(let [id, el] of existingCards){
    if(!currentIds.has(id)) el.remove();
  }
  videos.forEach((v, index) => {
    let card = existingCards.get(v.id);
    let expectedStatus = labels[v.status] || v.status;
    let expectedHeadline = v.headline || '';
    let expectedMeta = date(v.created_at);
    let expectedUpdated = String(v.updated_at || '');

    if(card){
      let statusEl = card.querySelector('.status');
      if(statusEl && statusEl.textContent !== expectedStatus) statusEl.textContent = expectedStatus;
      let strongEl = card.querySelector('strong');
      if(strongEl && strongEl.textContent !== expectedHeadline) strongEl.textContent = expectedHeadline;
      let metaEl = card.querySelector('.meta');
      if(metaEl && metaEl.textContent !== expectedMeta) metaEl.textContent = expectedMeta;
      
      let sourceTag = card.querySelector('.source-tag');
      if(v.source){
        if(!sourceTag){
          sourceTag = document.createElement('span');
          sourceTag.className = 'source-tag';
          let info = card.querySelector('.info');
          if(info) info.insertBefore(sourceTag, metaEl || null);
        }
        if(sourceTag.textContent !== v.source) sourceTag.textContent = v.source;
      } else if(sourceTag){
        sourceTag.remove();
      }

      if(expectedUpdated && card.dataset.updated !== expectedUpdated){
        card.dataset.updated = expectedUpdated;
        let img = card.querySelector('img');
        if(img) img.src = media(v, 'poster') + '&t=' + encodeURIComponent(expectedUpdated);
      }

      if(grid.children[index] !== card){
        grid.insertBefore(card, grid.children[index] || null);
      }
    } else {
      let t = document.createElement('template');
      t.innerHTML = cardHtml(v).trim();
      let newCard = t.content.firstElementChild;
      grid.insertBefore(newCard, grid.children[index] || null);
    }
  });
}

function jobPanel(){let j=state.jobs.find(j=>j.status==='running')||state.jobs[0];if(!j)return '';return `<div class="panel"><div class="provider-head"><h3>${j.status==='running'?'<span class="busy-dot"></span>':''}${esc(j.stage)}</h3><span class="meta">${date(j.created_at)}</span></div><span class="muted">${esc(j.action)}${j.automatic?' · automatic':''}</span>${j.summary?`<p>${j.summary.rendered??j.summary.posted??0} completed · ${j.summary.failed??0} failed</p>`:''}<details><summary>Technical details</summary><pre>${esc(j.log||'Waiting for output…')}</pre></details></div>`;}
function updateJobPanel(container){
  if(!container) return;
  let j = state.jobs.find(j=>j.status==='running')||state.jobs[0];
  if(!j){
    if(container.innerHTML !== '') container.innerHTML = '';
    return;
  }
  let details = container.querySelector('details');
  let wasOpen = details ? details.open : false;
  let pre = container.querySelector('pre');
  let wasAtBottom = pre ? (pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30) : true;
  let prevScroll = pre ? pre.scrollTop : 0;
  
  let newHtml = jobPanel();
  if(container.innerHTML !== newHtml){
    container.innerHTML = newHtml;
    let newDetails = container.querySelector('details');
    if(newDetails && wasOpen) newDetails.open = true;
    let newPre = container.querySelector('pre');
    if(newPre){
      newPre.scrollTop = wasAtBottom ? newPre.scrollHeight : prevScroll;
    }
  }
}

function updateOverviewStats(stats){
  if(!stats) return;
  let readyCount = state.videos.filter(v=>v.status==='ready').length;
  let uploadedCount = state.videos.filter(v=>v.status==='uploaded').length;
  let strongs = stats.querySelectorAll('strong');
  if(strongs.length >= 2){
    if(strongs[0].textContent !== String(readyCount)) strongs[0].textContent = String(readyCount);
    if(strongs[1].textContent !== String(uploadedCount)) strongs[1].textContent = String(uploadedCount);
  }
  tickCountdown();
}

function field(label,section,key,value,type='text',attrs=''){return `<label>${label}<input data-setting="${section}.${key}" type="${type}" value="${esc(value)}" ${attrs}></label>`;}
function area(label,section,key,value){return `<label>${label}<textarea data-setting="${section}.${key}">${esc(value)}</textarea></label>`;}
function check(label,section,key,value){return `<label class="check"><input data-setting="${section}.${key}" type="checkbox" ${value?'checked':''}>${label}</label>`;}
function select(label,section,key,value,options){return `<label>${label}<select data-setting="${section}.${key}">${options.map(([v,l])=>`<option value="${v}" ${value===v?'selected':''}>${l}</option>`).join('')}</select></label>`;}
function channelRow(c){if(typeof c==='string')c={channel:c};return `<div class="channel"><input aria-label="Channel name" data-channel="name" placeholder="Name" value="${esc(c.name||'')}"><input aria-label="Channel handle or ID" data-channel="channel" placeholder="@handle or channel ID" value="${esc(c.channel)}"><select aria-label="Source licence" data-channel="licence"><option value="">Verify automatically</option><option value="cc" ${c.licence==='cc'?'selected':''}>Declared CC</option><option value="public-domain" ${c.licence==='public-domain'?'selected':''}>Declared public domain</option></select><button type="button" data-remove-channel aria-label="Remove channel">×</button></div>`;}

function render(force=false){
  if(!state)return;
  let c=state.config,a=state.automation;
  let diskLow=state.free_disk_bytes<512*1024*1024;
  $('#page-title').textContent={overview:'Overview',videos:'Video library',content:'Content & style',connections:'Connections',automation:'Automation'}[page];
  $('#mode').textContent=diskLow?'Waiting for disk space':state.busy?'Pipeline running':a.enabled?'Automation on':'Automation paused';
  $('nav').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b.dataset.page===page));

  let pageChanged = (page !== renderedPage) || (diskLow !== lastDiskLow) || force;
  lastDiskLow = diskLow;
  let diskBanner = diskLow?'<div class="panel"><h3>More disk space is needed</h3><p class="help">Free at least 512 MB on this Mac. Automatic rendering will resume when there is enough room; your existing videos are preserved.</p></div>':'';

  if(pageChanged){
    renderedPage = page;
    let html='';
    if(page==='overview'){
      html=`${diskBanner}<div class="stats" id="overview-stats"><div class="stat"><span>Ready to upload</span><strong>${state.videos.filter(v=>v.status==='ready').length}</strong></div><div class="stat"><span>Uploaded</span><strong>${state.videos.filter(v=>v.status==='uploaded').length}</strong></div><div class="stat"><span>Next automatic run</span><strong id="next-run-countdown">${getCountdownText()}</strong><small class="meta" id="next-run-time">${getCountdownSub()}</small><button type="button" id="automation-toggle" class="switch${state.automation.enabled?' on':''}" role="switch" aria-checked="${state.automation.enabled}" ${state.busy?'disabled':''}><span class="track"><span class="thumb"></span></span>${state.automation.enabled?`Automation on · ${state.automation.mode==='upload'?'uploads':'previews'}`:'Automation off'}</button></div></div><div id="overview-job">${jobPanel()}</div><div class="section-head"><h2>Latest videos</h2><div class="actions"><button type="button" id="btn-import-reddit">＋ Import Reddit post</button><button type="button" id="btn-import-link">＋ Import from link</button><button type="button" id="btn-make-slideshow">＋ Slideshow from images</button><button type="button" id="btn-upload-video">＋ Add video from Mac</button><button data-page="videos">View library →</button></div></div><div id="overview-videos"></div>`;
      $('#view').innerHTML=html;
      updateCards($('#overview-videos'), state.videos.slice(0,4));
      return;
    }
    if(page==='videos'){
      let videos=state.videos.filter(v=>filter==='all'||filter==='uploaded'&&v.status==='uploaded'||filter==='drafts'&&v.status!=='uploaded');
      html=`${diskBanner}<div class="section-head"><p>Preview, refine, and upload your videos.</p><div class="actions"><button type="button" id="btn-import-reddit">＋ Import Reddit post</button><button type="button" id="btn-import-link">＋ Import from link</button><button type="button" id="btn-make-slideshow">＋ Slideshow from images</button><button type="button" id="btn-upload-video">＋ Add video from Mac</button><button class="primary" data-job="preview" ${state.busy?'disabled':''}>＋ Create preview</button></div></div><div class="filters">${[['all','All videos'],['drafts','Drafts'],['uploaded','Uploaded']].map(([v,l])=>`<button data-filter="${v}" class="${filter===v?'selected':''}">${l}</button>`).join('')}</div><div id="videos-grid"></div>`;
      $('#view').innerHTML=html;
      updateCards($('#videos-grid'), videos);
      return;
    }
    if(page==='connections'){
      html=`${diskBanner}<p class="help">Add or replace keys for the providers your pipeline supports. Keys stay on this Mac and are never sent back to the interface. “Key saved” means configured, not verified with the provider.</p><div class="two-col">${Object.entries({gemini:'Google Gemini',anthropic:'Anthropic',openai:'OpenAI',youtube:'YouTube discovery',reddit_client_id:'Reddit Client ID',reddit_client_secret:'Reddit Client Secret'}).map(([id,title])=>`<section class="panel"><div class="provider-head"><h2>${title}</h2><span class="status">${state.connections[id]?'Key saved':'Not configured'}</span></div><form data-key="${id}"><label>API key / credential<input type="password" name="key" autocomplete="off" placeholder="Paste a new value" required></label><div class="actions"><button class="primary" type="submit">Save</button><button type="button" data-clear-key="${id}">Remove</button></div></form></section>`).join('')}</div><section class="panel"><div class="provider-head"><h2>YouTube uploads</h2><span class="status">${state.youtube_connected?'Authorization saved':'Not connected'}</span></div><p class="help">Upload your Google Desktop app OAuth JSON, then connect your channel. Authorization opens in your default browser.</p><label>OAuth client file<input type="file" id="oauth-file" accept=".json,application/json"></label><button data-job="youtube_connect" ${state.busy||!state.oauth_client?'disabled':''}>Connect / reconnect YouTube</button></section><section class="panel"><h2>AI provider order & models</h2><p class="help">Providers are tried from top to bottom. Model names are editable so you can update them when your provider changes availability.</p><form id="models-form">${field('Provider order (comma separated)','editorial','providers',c.editorial.providers.join(', '))}${Object.entries(c.editorial.models).map(([k,v])=>`<label>${esc(k)} ${k==='gemini'?'models (one per line)':'model (one name)'}<textarea data-model="${esc(k)}">${esc(Array.isArray(v)?v.join('\n'):v)}</textarea></label>`).join('')}<button class="primary">Save provider settings</button></form></section>`;
      $('#view').innerHTML=html;
      return;
    }
    if(page==='content'){
      html=`${diskBanner}<form id="content-form"><div class="two-col"><section class="panel"><h2>Channel identity</h2>${field('Channel name','account','name',c.account.name)}${field('Handle','account','handle',c.account.handle)}<div class="form-grid">${field('Headline size','layout','headline_size',c.layout.headline_size,'number','min="30" max="110"')}${field('Video border','layout','border_color',c.layout.border_color,'color')}</div></section><section class="panel"><h2>Format & sound</h2>${check('Enable image stories','story','enabled',c.story.enabled)}${field('Story duration (seconds)','story','duration_seconds',c.story.duration_seconds,'number','min="3" max="60"')}${check('Background music','audio','enabled',c.audio.enabled)}<div class="form-grid">${field('Music volume','audio','music_volume',c.audio.music_volume,'number','min="0" max="2" step="0.05"')}${field('Source volume','audio','source_audio_volume',c.audio.source_audio_volume,'number','min="0" max="2" step="0.05"')}</div><div class="form-grid">${select('Story template','story_layout','template',(c.story_layout||{}).template||'auto',[['auto','Auto · pick per post'],['reaction_card','Reaction / tweet card'],['classic','Classic story card']])}${select('Card theme','story_layout','theme',(c.story_layout||{}).theme||'auto',[['auto','Auto · follows template'],['light','Light (clean white)'],['dark','Dark (charcoal)']])}</div>${check('Show subscribe badge on reaction cards','story_layout','show_subscribe',!!(c.story_layout||{}).show_subscribe)}<p class="help">Auto sends a single image with a punchline to the reaction card, pairs and text-only posts to the classic card, and splits the rest by a hash of the headline — so re-rendering a draft never changes its look.</p></section></div><section class="panel"><h2>Editorial direction</h2>${area('How should the editor choose and write stories?','editorial','system_prompt',c.editorial.system_prompt)}<div class="two-col">${area('YouTube search topics · one per line','discovery','youtube_queries',c.discovery.youtube_queries.join('\n'))}${area('Wikipedia search topics · one per line','discovery','wikipedia_queries',c.discovery.wikipedia_queries.join('\n'))}</div></section><section class="panel"><h2>Reddit communities</h2><p class="help">7 curated topic categories (44 subreddits) are checked with rotation pacing to keep rate limits safe. Posts with videos become clip cards; images become story cards.</p><div class="reddit-grid">${Object.entries(c.discovery.reddit_categories||{}).map(([cat,subs])=>`<div class="reddit-card"><strong>${esc(cat)}</strong><p>${esc(subs.map(s=>'r/'+s).join(', '))}</p></div>`).join('')}</div></section><section class="panel"><h2>Source channels</h2><p class="help">New channels use automatic licence verification. Only declare a licence when you have verified the source’s reuse terms.</p><div id="channels">${c.discovery.youtube_channels.map(channelRow).join('')}</div><button type="button" id="add-channel">＋ Add channel</button><div class="section-head"><h3>News context</h3></div>${area('RSS feeds · one HTTPS address per line','discovery','news_rss',c.discovery.news_rss.join('\n'))}</section><button class="primary">Save content settings</button> <button type="button" data-job="sources">Check sources</button> <button type="button" data-job="music">Check music library</button><footer>Changes apply to new previews. Open an existing draft and render it again to apply your current style.</footer></form>`;
      $('#view').innerHTML=html;
      return;
    }
    if(page==='automation'){
      html=`${diskBanner}<form id="automation-form"><section class="panel"><h2>Keep the pipeline working</h2><p class="help">The background service checks connectivity every 30 seconds. When online and due, it runs once, then waits for your chosen interval. Missed runs are not piled up.</p><label class="check"><input name="enabled" type="checkbox" ${a.enabled?'checked':''}>Enable background automation</label><div class="form-grid"><label>Run every (hours)<input name="interval" type="number" min="1" max="168" step="1" value="${a.interval_hours}"></label><label>Automatic action<select name="mode"><option value="preview" ${a.mode==='preview'?'selected':''}>Create previews for review</option><option value="upload" ${a.mode==='upload'?'selected':''}>Create and upload automatically</option></select></label></div><button class="primary">Save automation</button></section></form><form id="posting-form"><section class="panel"><h2>Posting defaults</h2><div class="form-grid">${field('Videos per run','posting','clips_per_run',c.posting.clips_per_run,'number','min="1" max="6"')}${field('Maximum clip length (seconds)','posting','max_clip_seconds',c.posting.max_clip_seconds,'number','min="3" max="180"')}</div>${select('YouTube visibility','posting','privacy',c.posting.privacy,[['private','Private · only you'],['unlisted','Unlisted · anyone with the link'],['public','Public · everyone']])}<button class="primary">Save posting defaults</button></section></form><section class="panel"><h2>Runs on this Mac</h2><p class="help">Closing the app window leaves the service running. It starts again when you log in. Your Mac must be awake and online; a closed lid or shutdown stops work until the Mac wakes. Each run keeps the Mac from idle-sleeping while it finishes.</p><div class="row"><span>Internet</span><span>${state.online===null?'Checking…':state.online?'Available':'Offline · waiting'}</span></div><div class="row"><span>Next run</span><span id="automation-next-run">${getAutomationNextRunText()}</span></div></section><div id="automation-job">${jobPanel()}</div>`;
      $('#view').innerHTML=html;
      return;
    }
  }

  // Smooth in-place updates without tearing down the DOM
  if(page==='overview'){
    updateOverviewStats($('#overview-stats'));
    updateJobPanel($('#overview-job'));
    let importBtn = $('#view').querySelector('#btn-import-reddit');
    if(importBtn) importBtn.disabled = !!state.busy;
    updateCards($('#overview-videos'), state.videos.slice(0,4));
  } else if(page==='videos'){
    let btn = $('#view').querySelector('[data-job="preview"]');
    if(btn) btn.disabled = !!state.busy;
    let importBtn = $('#view').querySelector('#btn-import-reddit');
    if(importBtn) importBtn.disabled = !!state.busy;
    $('#view').querySelectorAll('.filters button').forEach(b => b.classList.toggle('selected', b.dataset.filter === filter));
    let filtered = state.videos.filter(v=>filter==='all'||filter==='uploaded'&&v.status==='uploaded'||filter==='drafts'&&v.status!=='uploaded');
    updateCards($('#videos-grid'), filtered);
  } else if(page==='automation'){
    updateJobPanel($('#automation-job'));
    tickCountdown();
  }
}

async function refresh(draw=true, force=false){try{let r=await fetch('/api/status');if(!r.ok)throw Error('Local service unavailable');state=await r.json();$('#connection').textContent=state.online===null?'Checking internet…':state.online?'● Online':'○ Waiting for internet';if(draw)render(force);tickCountdown();}catch(e){$('#connection').textContent='Service disconnected';message('The local service is unavailable. Reopen the app to reconnect.',true);}}
function gather(form){let output={};form.querySelectorAll('[data-setting]').forEach(el=>{let [s,k]=el.dataset.setting.split('.'),v=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.value;if(s==='discovery')v=v.split('\n').map(x=>x.trim()).filter(Boolean);if(k==='providers')v=v.split(',').map(x=>x.trim()).filter(Boolean);(output[s]??={})[k]=v;});return output;}
function openVideo(id){let v=state.videos.find(v=>v.id===id);editing=id;let uploaded=v.status==='uploaded';$('#editor-content').innerHTML=`<div class="video-detail"><div><video controls preload="metadata" poster="${media(v,'poster')}" src="${media(v,'video')}"></video><p class="status">${esc(labels[v.status]||v.status)}</p>${v.youtube_id?`<a href="https://www.youtube.com/watch?v=${encodeURIComponent(v.youtube_id)}" target="_blank" rel="noopener">Open on YouTube ↗</a>`:''}<p><a href="${media(v,'video')}" download="${esc(v.id)}.mp4">Export video ↓</a></p></div><div><h2>Video details</h2>${v.error?`<p class="error-text">${esc(v.error)}</p>`:''}${v.legacy?'<p class="help">Existing video · only upload title and description can be edited.</p>':''}${v.source?`<p class="source-credit"><strong>Source:</strong> ${esc(v.source)} ${v.source_url?`<a href="${esc(v.source_url)}" target="_blank" rel="noopener">Open post ↗</a>`:''}</p>`:''}<form id="video-form">${!v.legacy?`<label>On-screen headline (optional) <span class="char-counter" id="char-count-headline"></span><input name="headline" value="${esc(v.headline)}" maxlength="90" ${uploaded?'disabled':''}></label><label>Story caption<textarea name="caption" ${uploaded?'disabled':''}>${esc(v.caption)}</textarea></label>`:''}<label>YouTube title <span class="char-counter" id="char-count-title"></span><input name="title" value="${esc(v.title)}" maxlength="100" required ${uploaded?'disabled':''}></label><label>Description & source credit <span class="char-counter" id="char-count-description"></span><textarea name="description" maxlength="4900" ${uploaded?'disabled':''}>${esc(v.description)}</textarea></label><div class="actions">${!uploaded?`<button ${state.busy?'disabled':''}>Save edits</button>${!v.legacy?`<button type="button" data-render="${esc(id)}" ${state.busy?'disabled':''}>Save & render again</button>`:''}${v.status==='upload_unknown'?'<button class="primary" type="button" id="check-upload">Check YouTube for me</button> <button type="button" id="reset-upload">I checked: not uploaded</button>':`<button class="primary" type="button" data-upload="${esc(id)}" ${state.busy||v.status!=='ready'?'disabled':''}>Upload ${esc(state.config.posting.privacy)}</button>`}`:''}<button type="button" class="danger" data-delete="${esc(id)}" ${state.busy?'disabled':''}>Delete video</button></div></form></div></div>`;updateCharCounts();$('#editor').showModal();}

let inspectTimeout=null;
function updateRedditCharCounts(){
  let h=$('#reddit-headline-input'), c=$('#char-count-reddit-headline');
  if(h&&c) c.textContent=`${h.value.length}/90`;
}

function openSlideshow(){
  $('#upload-dialog-content').innerHTML=`<h2>Slideshow from images</h2><p class="help">Pick the slides in the order you want them — a TikTok photo post, a set of screenshots, anything. They are assembled into a clip, then given the same treatment as any other import: mood-matched music, your template and framing.</p><form id="slideshow-form"><label>Slideshow post link (optional)<input name="link" id="slideshow-link" type="url" placeholder="https://vt.tiktok.com/... — leave blank to use your own images"></label><p class="help" style="margin:-10px 0 18px">Paste a TikTok photo-post link and the slides are fetched for you. If that fails, screenshot them and pick the files below instead.</p><label>Images (choose several)<input name="files" id="slideshow-files" type="file" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp,.heic" multiple></label><label>On-screen headline (optional) <span class="char-counter" id="char-count-slideshow-headline">0/90</span><input name="headline" id="slideshow-headline" maxlength="90" placeholder="Leave blank if the slides already have their own text"></label><label>Caption / punchline (optional)<textarea name="caption" id="slideshow-caption" placeholder="Shown under the slides on the reaction card..."></textarea></label><label>Credit (optional)<input name="credit" maxlength="200" placeholder="e.g. Slides: @creator on TikTok"></label><div class="form-grid"><label>Seconds per slide<input name="seconds_each" type="number" min="0.5" max="15" step="0.5" value="2.5"></label><label>Story template<select name="template"><option value="auto">Auto (Smart select)</option><option value="reaction_card">Reaction / tweet card</option><option value="classic">Classic story card</option><option value="caption_card">Caption card (full-bleed clip)</option></select></label></div><label>Framing &amp; Crop<select name="framing"><option value="auto">Smart AI (Auto-detect &amp; prevent cutoff)</option><option value="contain">Fit full content (Contain / Letterbox)</option><option value="cover">Fill window (Cover / Centered)</option><option value="top">Focus on top (Preserve top captions)</option></select></label><div id="slideshow-progress" class="status" style="display:none"></div><div class="actions"><button class="primary" type="submit" id="slideshow-submit-btn" ${state.busy?'disabled':''}>Create draft</button><button type="button" id="cancel-upload-dialog">Cancel</button></div></form>`;
  $('#upload-dialog').showModal();
  $('#slideshow-headline').addEventListener('input',e=>{$('#char-count-slideshow-headline').textContent=`${e.target.value.length}/90`;});
}

function stageSlide(token,file,index){
  return new Promise((resolve,reject)=>{
    let xhr=new XMLHttpRequest();
    let q=new URLSearchParams({token,index:String(index),filename:file.name});
    xhr.open('POST','/api/slideshow_add?'+q.toString());
    xhr.setRequestHeader('X-Studio-Token',state.csrf);
    xhr.setRequestHeader('Content-Type','application/octet-stream');
    xhr.onload=()=>{let d={};try{d=JSON.parse(xhr.responseText);}catch(_){}
      xhr.status===200?resolve(d):reject(Error(d.error||'Could not stage that image'));};
    xhr.onerror=()=>reject(Error('Lost the connection to the local service'));
    xhr.send(file);
  });
}

function openLinkImport(){
  $('#reddit-dialog-content').innerHTML=`<h2>Import from link</h2><p class="help">Paste a public video link — TikTok, YouTube, X, Instagram, anything yt-dlp can read. We fetch the clip, pick music from its mood, and produce a draft.</p><form id="link-import-form"><label>Video link<input name="url" id="link-url-input" type="url" placeholder="https://www.tiktok.com/@someone/video/..." required autocomplete="off"></label><div id="link-inspect-status" style="display:none"></div><label>On-screen headline (optional) <span class="char-counter" id="char-count-link-headline">0/90</span><input name="headline" id="link-headline-input" maxlength="90" placeholder="Leave blank if the clip already has its own text"></label><label>Caption / punchline (optional)<textarea name="caption" id="link-caption-input" placeholder="Shown under the clip on the reaction card..."></textarea></label><label>Clip length (seconds, optional)<input name="max_seconds" id="link-max-seconds" type="number" min="3" max="180" placeholder="Leave blank for the default cap"></label><div class="form-grid"><label>Story template<select name="template"><option value="auto">Auto (Smart select)</option><option value="reaction_card">Reaction / tweet card</option><option value="classic">Classic story card</option><option value="caption_card">Caption card (full-bleed clip)</option></select></label><label>Framing &amp; Crop<select name="framing"><option value="auto">Smart AI (Auto-detect &amp; prevent cutoff)</option><option value="contain">Fit full content (Contain / Letterbox)</option><option value="cover">Fill window (Cover / Centered)</option><option value="top">Focus on top (Preserve top captions)</option></select></label></div><div class="form-grid"><label>Visibility<select name="privacy"><option value="">Use posting default</option><option value="public">Public · everyone</option><option value="unlisted">Unlisted · anyone with the link</option><option value="private">Private · only you</option></select></label><label>Schedule (optional)<input name="publish_at" type="datetime-local"></label></div><p class="help" style="margin:-10px 0 18px">A scheduled video uploads as private and YouTube publishes it at that time.</p><div class="actions"><button class="primary" type="submit" id="link-submit-btn" ${state.busy?'disabled':''}>Create draft</button><button type="button" id="cancel-reddit-dialog">Cancel</button></div></form>`;
  $('#reddit-dialog').showModal();
  let input=$('#link-url-input'), timer=null;
  input.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>inspectLink(input.value),700);});
  $('#link-headline-input').addEventListener('input',e=>{delete e.target.dataset.autofilled;$('#char-count-link-headline').textContent=`${e.target.value.length}/90`;});
}

async function inspectLink(url){
  url=(url||'').trim();
  let status=$('#link-inspect-status');
  if(!status) return;
  if(url.length<8||!url.includes('.')){status.style.display='none';return;}
  status.style.display='block';status.className='loading';status.textContent='Reading link\u2026';
  try{
    let res=await fetch('/api/link_inspect?url='+encodeURIComponent(url));
    let data=await res.json();
    if(!res.ok) throw new Error(data.error||'Could not read that link');
    status.className='';
    let who=data.uploader?` by ${data.uploader}`:'';
    let len=data.duration?` \u00b7 ${Math.round(data.duration)}s`:'';
    status.textContent=data.is_slideshow
      ? `\u2713 ${data.platform} photo post${who} \u00b7 ${data.slides} slide${data.slides===1?'':'s'} \u2014 built as a slideshow`
      : `\u2713 ${data.platform} video${who}${len}`;
    let h=$('#link-headline-input');
    if(h&&(!h.value.trim()||h.dataset.autofilled)){
      h.value=(data.title||'').slice(0,90);
      h.dataset.autofilled='true';
      $('#char-count-link-headline').textContent=`${h.value.length}/90`;
    }
  }catch(err){
    status.className='error-text';
    status.textContent=err.message||'Could not read that link';
  }
}

async function handleRedditUrlChange(url){
  url=(url||'').trim();
  let status=$('#reddit-inspect-status');
  if(!status) return;
  if(!url.includes('reddit.com')&&!url.includes('redd.it')){status.style.display='none';return;}
  status.style.display='block';
  status.className='loading';
  status.textContent='Inspecting Reddit post…';
  try{
    let res=await fetch('/api/reddit_inspect?url='+encodeURIComponent(url));
    let data=await res.json();
    if(!res.ok) throw new Error(data.error||'Could not inspect post');
    status.className='';
    let sub=data.subreddit?`r/${data.subreddit}`:'Reddit';
    let author=data.author?` by u/${data.author}`:'';
    let mediaType = data.is_video ? 'video' : (data.is_gallery ? `gallery (${data.image_urls.length} images)` : (data.image_urls.length ? 'image' : 'post'));
    status.textContent=`✓ Found ${sub} ${mediaType}${author}`;
    let hInput=$('#reddit-headline-input');
    if(hInput&&(!hInput.value.trim()||hInput.dataset.autofilled)){
      hInput.value=(data.title||'').toUpperCase();
      hInput.dataset.autofilled='true';
      updateRedditCharCounts();
    }
    let cInput=$('#reddit-caption-input');
    if(cInput&&!cInput.value.trim()&&data.caption){
      cInput.value=data.caption.slice(0, 280);
    }
    let mInput=$('#reddit-media-url');
    let directMedia = data.media_url || data.video_url || data.thumbnail_url || '';
    if(mInput&&directMedia&&!mInput.value.trim()){
      mInput.value=directMedia;
    }
    let kSelect=$('#reddit-kind-select');
    if(kSelect){
      if(data.is_video) kSelect.value = 'video';
      else if(data.is_gallery || data.image_urls.length > 0) kSelect.value = 'story';
    }
  }catch(err){
    status.className='error';
    status.textContent='Note: '+(err.message||'Could not fetch post info automatically; enter headline manually');
  }
}

function openRedditImport(){
  $('#reddit-dialog-content').innerHTML=`<h2>Import Reddit post</h2><p class="help">Paste any Reddit post link (r/interestingasfuck, r/todayilearned, r/technology, etc.) or packaged media link. We will inspect the media, craft your on-screen card, and produce a draft ready for review and upload.</p><form id="reddit-import-form"><label>Reddit post URL<input name="url" id="reddit-url-input" type="url" placeholder="https://www.reddit.com/r/.../comments/... or packaged-media.redd.it" required autocomplete="off"></label><div id="reddit-inspect-status" style="display:none"></div><label>On-screen headline (optional) <span class="char-counter" id="char-count-reddit-headline">0/90</span><input name="headline" id="reddit-headline-input" maxlength="90" placeholder="Leave blank if the media already has its own text"></label><label>Story caption / commentary (optional)<textarea name="caption" id="reddit-caption-input" placeholder="Add custom commentary, or leave blank to use the Reddit context..."></textarea></label><div class="form-grid"><label>Format kind<select name="kind" id="reddit-kind-select"><option value="auto">Auto-detect (Story card or Video)</option><option value="story">Image Story Card</option><option value="video">Direct Video Clip</option></select></label><label>Story template<select name="template" id="reddit-template-select"><option value="auto">Auto (Smart select)</option><option value="reaction_card">Reaction / tweet card</option><option value="classic">Classic story card</option><option value="caption_card">Caption card (full-bleed clip)</option></select></label><label>Framing & Crop<select name="framing" id="reddit-framing-select"><option value="auto">Smart AI (Auto-detect joke & prevent cutoff)</option><option value="contain">Fit full content (Contain / Letterbox)</option><option value="cover">Fill window (Cover / Centered)</option><option value="top">Focus on top (Preserve top captions)</option></select></label></div><label>Media / image URL (optional)<input name="media_url" id="reddit-media-url" placeholder="https://... (.jpg, .png, .mp4, packaged-media)"></label><div class="actions"><button class="primary" type="submit" id="reddit-submit-btn" ${state.busy?'disabled':''}>Create Reddit draft</button><button type="button" id="cancel-reddit-dialog">Cancel</button></div></form>`;
  updateRedditCharCounts();
  $('#reddit-dialog').showModal();
}

function openVideoUpload(){
  $('#upload-dialog-content').innerHTML=`<h2>Add a video from this Mac</h2><p class="help">Pick a clip already on your machine. It is analysed for mood, given a matching track from your music library, framed automatically and saved as a draft — the same treatment a discovered clip gets.</p><form id="upload-form"><label>Video file<input name="file" id="upload-file-input" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.m4v,.webm,.mkv" required></label><label>On-screen headline (optional) <span class="char-counter" id="char-count-upload-headline">0/90</span><input name="headline" id="upload-headline-input" maxlength="90" placeholder="Leave blank if the clip already has its own text"></label><label>Caption / description (optional)<textarea name="caption" id="upload-caption-input" placeholder="Extra context for the video description..."></textarea></label><label>Clip length (seconds, optional)<input name="max_seconds" id="upload-max-seconds" type="number" min="3" max="180" placeholder="Leave blank for the default cap"></label><div class="form-grid"><label>Story template<select name="template"><option value="auto">Auto (Smart select)</option><option value="reaction_card">Reaction / tweet card</option><option value="classic">Classic story card</option><option value="caption_card">Caption card (full-bleed clip)</option></select></label><label>Framing &amp; Crop<select name="framing"><option value="auto">Smart AI (Auto-detect subject &amp; prevent cutoff)</option><option value="contain">Fit full content (Contain / Letterbox)</option><option value="cover">Fill window (Cover / Centered)</option><option value="top">Focus on top (Preserve top captions)</option></select></label></div><div class="form-grid"><label>Visibility<select name="privacy"><option value="">Use posting default</option><option value="public">Public · everyone</option><option value="unlisted">Unlisted · anyone with the link</option><option value="private">Private · only you</option></select></label><label>Schedule (optional)<input name="publish_at" type="datetime-local"></label></div><p class="help" style="margin:-10px 0 18px">A scheduled video uploads as private and YouTube publishes it at that time.</p><div id="upload-progress" class="status" style="display:none"></div><div class="actions"><button class="primary" type="submit" id="upload-submit-btn" ${state.busy?'disabled':''}>Create draft</button><button type="button" id="cancel-upload-dialog">Cancel</button></div></form>`;
  $('#upload-dialog').showModal();
}

function uploadVideo(file,params,onProgress){
  // XHR rather than fetch: it reports upload progress, and a large clip needs it.
  return new Promise((resolve,reject)=>{
    let xhr=new XMLHttpRequest();
    xhr.open('POST','/api/upload_video?'+new URLSearchParams(params).toString());
    xhr.setRequestHeader('X-Studio-Token',state.csrf);
    xhr.setRequestHeader('Content-Type','application/octet-stream');
    xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(e.loaded/e.total);};
    xhr.onload=()=>{let d={};try{d=JSON.parse(xhr.responseText);}catch(_){}
      xhr.status===200?resolve(d):reject(Error(d.error||'Upload failed'));};
    xhr.onerror=()=>reject(Error('Lost the connection to the local service'));
    xhr.send(file);
  });
}

async function saveVideo(){let b={id:editing};new FormData($('#video-form')).forEach((v,k)=>b[k]=v);await api('video',b);}
document.addEventListener('input',e=>{
  if(e.target.closest('#video-form'))updateCharCounts();
  if(e.target.id==='reddit-headline-input'){delete e.target.dataset.autofilled;updateRedditCharCounts();}
  if(e.target.id==='upload-headline-input'){let c=$('#char-count-upload-headline');if(c)c.textContent=e.target.value.length+'/90';}
  if(e.target.id==='reddit-url-input'){
    clearTimeout(inspectTimeout);
    inspectTimeout=setTimeout(()=>handleRedditUrlChange(e.target.value),350);
  }
});
document.addEventListener('click',e=>{let b=e.target.closest('button');if(!b)return;
if(b.dataset.page){page=b.dataset.page;message('');render();}
if(b.dataset.filter){filter=b.dataset.filter;render();}
if(b.dataset.video)openVideo(b.dataset.video);
if(b.id==='btn-import-reddit')openRedditImport();
if(b.id==='btn-import-link')openLinkImport();
if(b.id==='btn-make-slideshow')openSlideshow();
if(b.id==='btn-upload-video')openVideoUpload();
if(b.id==='close-upload-dialog'||b.id==='cancel-upload-dialog'){$('#upload-dialog').close();}
if(b.id==='automation-toggle')act(async()=>{let a=state.automation,on=!a.enabled;
  // Enabling clears next_run, so the scheduler fires on its next 30s tick.
  if(on&&a.mode==='upload'&&!confirm('Turn automation on?\n\nIt will start a run within a minute and upload the result to YouTube as '+(state.config&&state.config.posting?state.config.posting.privacy:'public')+'. Switch the automatic action to "Create previews for review" first if you would rather check it before it goes out.'))return;
  await api('automation',{enabled:on,interval_hours:a.interval_hours,mode:a.mode});
  message(on?'Background automation on.':'Background automation paused.');});
if(b.id==='close-dialog'){$('#editor').close();editing=null;render();}
if(b.id==='close-reddit-dialog'||b.id==='cancel-reddit-dialog'){$('#reddit-dialog').close();}
if(b.dataset.delete)act(async()=>{let v=state.videos.find(x=>x.id===b.dataset.delete);let name=v?`"${v.headline||v.title}"`:'this video';if(!confirm(`Delete ${name} from your library? This will permanently delete the local video file.`))return;await api('video',{id:b.dataset.delete,delete:true});$('#editor').close();editing=null;message('Video deleted from library.');await refresh(true,true);});
if(b.dataset.job)act(async()=>{if(b.dataset.job==='run'&&!confirm(`Create and upload ${state.config.posting.clips_per_run} video(s) with ${state.config.posting.privacy} visibility?`))return;await api('jobs',{action:b.dataset.job});message(b.dataset.job==='youtube_connect'?'Complete YouTube authorization in your browser.':'Job started. You can close the app window.');await refresh();});
if(b.dataset.clearKey)act(async()=>{if(!confirm('Remove this saved API key?'))return;await api('keys',{provider:b.dataset.clearKey,value:''});message('API key removed.');await refresh();});
if(b.id==='add-channel')$('#channels').insertAdjacentHTML('beforeend',channelRow({channel:''}));
if(b.hasAttribute('data-remove-channel'))b.closest('.channel').remove();
if(b.dataset.render)act(async()=>{await saveVideo();await api('jobs',{action:'render',id:editing});$('#editor').close();editing=null;message('Rendering your edits.');await refresh();});
if(b.dataset.upload)act(async()=>{if(!confirm(`Upload this video to YouTube as ${state.config.posting.privacy}?`))return;await saveVideo();await api('jobs',{action:'upload',id:editing});$('#editor').close();editing=null;message('Upload started.');await refresh();});
if(b.id==='check-upload')act(async()=>{
  message('Checking YouTube…');
  let res=await api('video',{id:editing,check_upload:true});
  if(res.found){
    message(`It did upload — found it on YouTube (${res.youtube_id}). Marked as uploaded.`);
  }else{
    message('No matching video on your channel in the last 90 minutes — safe to upload again.');
  }
  await refresh(false); openVideo(editing);});
if(b.id==='reset-upload')act(async()=>{if(!confirm('Confirm you checked YouTube Studio and this video was not uploaded.'))return;await api('video',{id:editing,reset_upload:true});await refresh(false);openVideo(editing);});
});
document.addEventListener('submit',e=>{e.preventDefault();let f=e.target;act(async()=>{
if(f.dataset.key){await api('keys',{provider:f.dataset.key,value:f.elements.key.value});f.reset();message('API key saved on this Mac.');}
else if(f.id==='video-form'){await saveVideo();message('Draft saved. On-screen changes require rendering again.');await refresh(false);openVideo(editing);return;}
else if(f.id==='upload-form'){
  let btn=$('#upload-submit-btn'),prog=$('#upload-progress'),file=f.elements.file.files[0];
  if(!file){message('Choose a video file first',true);return;}
  btn.disabled=true;btn.textContent='Uploading…';
  prog.style.display='block';prog.textContent='Uploading 0%';
  try{
    let res=await uploadVideo(file,{
      filename:file.name,
      headline:f.elements.headline.value.trim(),
      caption:f.elements.caption.value.trim(),
      framing:f.elements.framing.value,
      template:f.elements.template.value,
      max_seconds:f.elements.max_seconds.value.trim(),
      privacy:f.elements.privacy.value,
      publish_at:f.elements.publish_at.value,
    },p=>{
      let pct=Math.round(p*100);
      prog.textContent=pct<100?`Uploading ${pct}%`:'Analysing mood and rendering… this can take a minute.';
    });
    $('#upload-dialog').close();
    message(`Draft created: "${res.headline}"${res.mood?` · ${res.mood} music`:''}`);
    await refresh(true,true);
    if(res.id)openVideo(res.id);
  }catch(err){
    btn.disabled=false;btn.textContent='Create draft';
    prog.style.display='none';
    message(err.message||'Could not add that video',true);
  }
  return;
}
else if(f.id==='slideshow-form'){
  let btn=$('#slideshow-submit-btn'),prog=$('#slideshow-progress');
  let files=Array.from(f.elements.files.files);
  let link=f.elements.link.value.trim();
  if(!files.length&&!link){message('Paste a slideshow link or choose images',true);return;}
  if(link){
    btn.disabled=true;btn.textContent='Fetching slides…';
    prog.style.display='block';prog.textContent='Reading the post and downloading its slides…';
    try{
      let res=await api('slide_link_import',{url:link,
        headline:f.elements.headline.value.trim(),
        caption:f.elements.caption.value.trim(),
        credit:f.elements.credit.value.trim(),
        seconds_each:f.elements.seconds_each.value,
        template:f.elements.template.value,
        framing:f.elements.framing.value});
      $('#upload-dialog').close();
      message(`Draft created: "${res.headline}"${res.mood?` \u00b7 ${res.mood} music`:''}`);
      await refresh(true,true);
      if(res.id)openVideo(res.id);
    }catch(err){
      btn.disabled=false;btn.textContent='Create draft';
      prog.style.display='none';
      message(err.message||'Could not fetch that slideshow',true);
    }
    return;
  }
  let token=Array.from(crypto.getRandomValues(new Uint8Array(10))).map(b=>b.toString(36).replace(/[^a-z0-9]/g,'0')[0]).join('')+Date.now().toString(36);
  token=token.replace(/[^A-Za-z0-9]/g,'').slice(0,32);
  btn.disabled=true;btn.textContent='Uploading…';
  prog.style.display='block';
  try{
    for(let i=0;i<files.length;i++){
      prog.textContent=`Uploading slide ${i+1} of ${files.length}…`;
      await stageSlide(token,files[i],i);
    }
    prog.textContent='Assembling slides and rendering… this can take a minute.';
    let res=await api('slideshow_import',{token,
      headline:f.elements.headline.value.trim(),
      caption:f.elements.caption.value.trim(),
      credit:f.elements.credit.value.trim(),
      seconds_each:f.elements.seconds_each.value,
      template:f.elements.template.value,
      framing:f.elements.framing.value});
    $('#upload-dialog').close();
    message(`Draft created: "${res.headline}"${res.mood?` \u00b7 ${res.mood} music`:''}`);
    await refresh(true,true);
    if(res.id)openVideo(res.id);
  }catch(err){
    btn.disabled=false;btn.textContent='Create draft';
    prog.style.display='none';
    message(err.message||'Could not build that slideshow',true);
  }
  return;
}
else if(f.id==='link-import-form'){
  let btn=$('#link-submit-btn');
  try{
    if(btn){btn.disabled=true;btn.textContent='Fetching and rendering…';}
    let res=await api('link_import',{url:f.elements.url.value.trim(),
      headline:f.elements.headline.value.trim(),
      caption:f.elements.caption.value.trim(),
      template:f.elements.template.value,
      framing:f.elements.framing.value,
      max_seconds:f.elements.max_seconds.value.trim(),
      privacy:f.elements.privacy.value,
      publish_at:f.elements.publish_at.value});
    $('#reddit-dialog').close();
    message(`Draft created: "${res.headline}"${res.mood?` \u00b7 ${res.mood} music`:''}`);
    await refresh(true,true);
    if(res.id)openVideo(res.id);
  }catch(err){
    if(btn){btn.disabled=false;btn.textContent='Create draft';}
    message(err.message||'Could not import that link',true);
  }
  return;
}
else if(f.id==='reddit-import-form'){
  let btn=$('#reddit-submit-btn');
  if(btn){btn.disabled=true;btn.textContent='Rendering & creating draft…';}
  try{
    let body={
      url:f.elements.url.value.trim(),
      headline:f.elements.headline.value.trim(),
      caption:f.elements.caption.value.trim(),
      kind:f.elements.kind.value,
      framing:f.elements.framing ? f.elements.framing.value : 'auto',
      template:f.elements.template ? f.elements.template.value : 'auto',
      media_url:f.elements.media_url.value.trim()
    };
    let res=await api('reddit_import',body);
    $('#reddit-dialog').close();
    message(`Reddit draft created: "${res.headline}"`);
    await refresh(true,true);
    if(res.id)openVideo(res.id);
  }catch(err){
    if(btn){btn.disabled=false;btn.textContent='Create Reddit draft';}
    message(err.message||'Could not import Reddit post',true);
  }
  return;
}
else if(f.id==='automation-form'){await api('automation',{enabled:f.elements.enabled.checked,interval_hours:Number(f.elements.interval.value),mode:f.elements.mode.value});message('Background automation updated.');}
else{let b=gather(f);if(f.id==='models-form'){b.editorial.models={};f.querySelectorAll('[data-model]').forEach(el=>{let values=el.value.split('\n').map(x=>x.trim()).filter(Boolean);b.editorial.models[el.dataset.model]=values.length===1?values[0]:values;});}if(f.id==='content-form'){b.discovery.youtube_channels=[...f.querySelectorAll('.channel')].map(row=>Object.fromEntries([...row.querySelectorAll('[data-channel]')].map(el=>[el.dataset.channel,el.value])));}await api('config',b);message('Settings saved.');}await refresh();});});
document.addEventListener('change',e=>{if(e.target.id==='oauth-file'&&e.target.files[0])act(async()=>{let client=JSON.parse(await e.target.files[0].text());await api('oauth-client',{client});message('OAuth client saved. You can now connect YouTube.');await refresh();});});
$('#editor').addEventListener('cancel',()=>{editing=null;});
$('#reddit-dialog').addEventListener('cancel',()=>{});
refresh();
setInterval(()=>refresh((page==='overview'||page==='videos'||page==='automation')&&!editing&&!saving),4000);
setInterval(tickCountdown, 1000);

