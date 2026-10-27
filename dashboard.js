'use strict';
const $ = id => document.getElementById(id);
const LIMITS = {expiry:[1,3650], maxConcurrent:[1,16], rpm:[1,600]};
let csrf = '', offset = 0, refreshNumber = 0, currentKeys = [];
function status(message, kind='info') {
  const box = $('status'); box.textContent = message; box.dataset.kind = kind;
}
function clearErrors() {
  document.querySelectorAll('.form-error').forEach(node => node.remove());
  document.querySelectorAll('[aria-invalid="true"]').forEach(node => node.removeAttribute('aria-invalid'));
}
function invalid(input, message) {
  const error = new Error(message); error.input = input; throw error;
}
function showField(input, message) {
  if (!input) return;
  input.setAttribute('aria-invalid', 'true');
  const hint = document.createElement('small'); hint.className = 'form-error'; hint.textContent = message;
  input.insertAdjacentElement('afterend', hint);
}
function fail(error, fields={}) {
  clearErrors();
  if (error.fields?.length) {
    for (const item of error.fields) showField(fields[item.field] || $(item.field), item.message);
  } else if (error.input) showField(error.input, error.message);
  status(error.message || 'The request failed. Please try again.', 'error');
  const first = document.querySelector('[aria-invalid="true"]');
  if (first && !first.closest('[hidden]')) first.focus();
}
function textValue(input, max, label, optional=false) {
  const value = input.value.trim();
  if ((!optional && !value) || value.length > max) invalid(input, `${label} must contain ${optional?'0':'1'}–${max} characters.`);
  return value;
}
function wholeNumber(input, low, high) {
  const raw = input.value.trim();
  if (!/^[0-9]+$/.test(raw) || !Number.isSafeInteger(Number(raw)) || Number(raw)<low || Number(raw)>high)
    invalid(input, `Enter a whole number from ${low.toLocaleString()} to ${high.toLocaleString()}.`);
  return Number(raw);
}
function ipEntryValid(value, permitRange=true) {
  const parts = value.split('/'); if (parts.length > (permitRange?2:1)) return false;
  const ip = parts[0], prefix = parts[1];
  if (prefix !== undefined && !/^[0-9]+$/.test(prefix)) return false;
  let bits;
  if (ip.includes(':')) {
    try { new URL(`http://[${ip}]/`); } catch { return false; }
    bits = 128;
  } else {
    const octets = ip.split('.');
    if (octets.length !== 4 || octets.some(part => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part)>255)) return false;
    bits = 32;
  }
  return prefix === undefined || Number(prefix) <= bits;
}
function ipList(input) {
  const raw = input.value.trim(); if (raw.length > 7000) invalid(input, 'Use at most 100 IP addresses or CIDR ranges.');
  const items = raw ? raw.split(/[\n,]/).map(x=>x.trim()).filter(Boolean) : [];
  if (items.length>100) invalid(input, 'Use at most 100 IP addresses or CIDR ranges.');
  const bad = items.find(x=>x.length>64 || !ipEntryValid(x));
  if (bad) invalid(input, `Invalid IP address or CIDR range: ${bad.slice(0,64)}`);
  return items;
}
function displayError(body, code) {
  const error = new Error(typeof body?.detail === 'string' ? body.detail : `Request failed (HTTP ${code}).`);
  error.fields = Array.isArray(body?.errors) ? body.errors : [];
  return error;
}
async function api(path, options={}) {
  const method = options.method || 'GET';
  const headers = {'Content-Type':'application/json', ...(options.headers || {})};
  if (method !== 'GET' && path !== '/admin/api/login') headers['X-CSRF-Token'] = csrf;
  let response;
  try { response = await fetch(path, {...options, method, headers, credentials:'same-origin', cache:'no-store'}); }
  catch { throw Error('Could not connect to the dashboard. Check your connection and try again.'); }
  let body;
  try { body = await response.json(); }
  catch { throw Error(`The server returned an unexpected response (HTTP ${response.status}). Try again.`); }
  if (!response.ok) {
    if (response.status === 401 && path !== '/admin/api/login') {
      csrf = ''; $('content').hidden = true; $('loginPanel').hidden = false;
    }
    throw displayError(body, response.status);
  }
  return body;
}
function cell(row, value, label='') {
  const td=row.insertCell(); td.textContent=value ?? '—';
  if (label) td.dataset.label=label;
  return td;
}
function metric(name,value) {
  const box=document.createElement('div'); box.className='metric';
  const number=document.createElement('strong'); number.textContent=value;
  const label=document.createElement('span'); label.textContent=name;
  box.append(number,label); return box;
}
function filterParams() {
  const ip=$('filterIp').value.trim();
  if (ip && !ipEntryValid(ip,false)) invalid($('filterIp'),'Enter one valid IPv4 or IPv6 address.');
  if ($('filterVoice') && $('filterVoice').value.trim().length>80) invalid($('filterVoice'),'Voice ID must be at most 80 characters.');
  const params=new URLSearchParams({offset:String(offset)});
  if ($('hours').value==='custom') {
    const from=$('fromDate').value, through=$('toDate').value;
    if (!from) invalid($('fromDate'),'Choose the first date.');
    if (!through) invalid($('toDate'),'Choose the last date.');
    if (from>through) invalid($('toDate'),'The last date must be on or after the first date.');
    const start=new Date(`${from}T00:00:00`), end=new Date(`${through}T00:00:00`);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) invalid($('fromDate'),'Choose valid dates.');
    end.setDate(end.getDate()+1);
    params.set('start_at',start.toISOString());params.set('end_at',end.toISOString());
  } else params.set('hours',$('hours').value);
  for (const [id,name] of [['filterStatus','status'],['filterTransport','transport'],['filterRoute','route'],['filterClient','key_id'],['filterIp','ip'],['filterVoice','voice']]) {
    const input=$(id); if (input?.value.trim()) params.set(name,input.value.trim());
  }
  return params;
}
async function refresh(options={}) {
  clearErrors(); const turn=++refreshNumber;
  try {
    const params=filterParams();
    const [report,keyData,allowlist]=await Promise.all([
      api(`/admin/api/overview?${params}`),api('/admin/api/keys'),api('/admin/api/allowlist')
    ]);
    if (turn!==refreshNumber) return true;
    $('content').hidden=false; $('loginPanel').hidden=true;
    const dateTime=new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short'});
    $('rangeSummary').textContent=`Showing ${dateTime.format(new Date(report.period_start))} to ${dateTime.format(new Date(report.period_end))} (${Intl.DateTimeFormat().resolvedOptions().timeZone}).`;
    $('metrics').replaceChildren(
      metric($('filterVoice')?'Generated audio':'Received audio',`${Number(report.audio_minutes || 0).toFixed(2)} min`),
      metric('Metered requests',report.metered_requests || 0),
      metric('All requests',report.total_requests),metric($('filterVoice')?'Speech requests':'Transcription requests',report.speech_requests),
      metric('Successful',report.successful_speech),metric('Denied',report.denied_requests),
      metric('Active / limit',`${report.active_speech} / ${report.concurrency_limit}`),
      metric('Queued / limit',`${report.queued_speech ?? 0} / ${report.queue_limit ?? 0}`),
      metric($('filterVoice')?'First audio p95':'First transcript p95',report.first_audio_p95_ms==null?'—':`${(report.first_audio_p95_ms/1000).toFixed(2)} s`),
      metric('Request p95',report.completion_p95_ms==null?'—':`${(report.completion_p95_ms/1000).toFixed(2)} s`)
    );
    $('requests').replaceChildren(...report.recent.map(item=>{
      const row=document.createElement('tr');
      const labels=['Time','Client','IP','Transport',$('filterVoice')?'Route / voice':'Route','Status',
        $('filterVoice')?'First audio':'First transcript','Request duration','Audio minutes',$('filterVoice')?'Audio size':'Received'];
      [item.created_at,item.key_label||'Unknown',item.client_ip,item.transport,
       `${item.route}${item.voice?` / ${item.voice}`:''}`,item.status,
       item.first_audio_ms==null?'—':`${item.first_audio_ms} ms`,`${item.duration_ms} ms`,
       item.audio_seconds==null?'—':`${(item.audio_seconds/60).toFixed(2)} min`,
       `${Math.round(item.audio_bytes/1024)} KB`].forEach((value,index)=>cell(row,value,labels[index]));
      return row;
    }));
    $('usageBreakdown').replaceChildren(...report.usage_breakdown.map(item=>{
      const row=document.createElement('tr');
      [item.client,item.route,item.requests,`${item.audio_minutes.toFixed(2)} min`].forEach((value,index)=>cell(row,value,['Client','Endpoint','Requests','Audio minutes'][index]));
      return row;
    }));
    if (!report.usage_breakdown.length) {
      const row=document.createElement('tr'); const entry=cell(row,'No metered audio in this period yet.');entry.colSpan=4;entry.className='empty';
      $('usageBreakdown').append(row);
    }
    const peak=Math.max(0,...report.usage_trend.map(item=>item.audio_minutes));
    $('usageTrend').replaceChildren(...report.usage_trend.map(item=>{
      const bar=document.createElement('div');bar.className='usage-bar';
      const height=document.createElement('i');height.style.height=`${Math.max(2,Math.round(68*item.audio_minutes/(peak||1)))}px`;
      const label=document.createElement('span');label.textContent=item.period.slice(5);
      bar.title=`${item.period}: ${item.audio_minutes.toFixed(2)} min, ${item.requests} requests`;
      bar.append(height,label);return bar;
    }));
    if (!report.usage_trend.length) $('usageTrend').textContent='No metered audio in this period yet.';
    $('pageInfo').textContent=report.total_requests?`${offset+1}–${offset+report.recent.length} of ${report.total_requests}`:'No matching requests';
    $('previous').disabled=offset===0; $('next').disabled=offset+report.recent.length>=report.total_requests || offset>=100000;
    if (document.activeElement!==$('globalIps')) $('globalIps').value=allowlist.allowed_ips.join('\n');
    const selected=$('filterClient').value;
    $('filterClient').replaceChildren(new Option('All clients',''),...keyData.keys.map(item=>new Option(`${item.label} (${item.prefix}…)`,item.id)));
    if (selected && !keyData.keys.some(item=>item.id===selected))
      $('filterClient').add(new Option(`Deleted key (${selected.slice(0,8)}…)`,selected));
    $('filterClient').value=selected;
    const queued=report.queued_by_priority || {};
    $('queueSummary').textContent=`Waiting: ${queued.high || 0} High, ${queued.normal || 0} Normal, ${queued.low || 0} Low. Maximum queue wait: ${report.queue_wait_seconds ?? 30} seconds.`;
    renderKeys(keyData.keys);
    if (!options.quiet) status(`Updated ${new Date().toLocaleTimeString()}.`);
    return true;
  } catch(error) { fail(error,{'ip':$('filterIp'),'voice':$('filterVoice'),'start_at':$('fromDate'),'end_at':$('toDate')}); return false; }
}
async function action(button, work, fields={}) {
  if (button.disabled) return false;
  clearErrors(); button.disabled=true;
  try { await work(); return true; }
  catch(error) { fail(error,fields); return false; }
  finally { button.disabled=false; }
}
async function afterSave(message) {
  const updated=await refresh({quiet:true});
  status(updated?message:`${message} The reports could not be refreshed.`,updated?'success':'error');
}
function renderKeys(keys) {
  currentKeys=keys;
  const search=$('keySearch').value.trim().toLocaleLowerCase();
  const from=$('keyFromDate').value, through=$('keyToDate').value;
  const dateField=$('keyDateField').value;
  const priorityFilter=$('keyPriorityFilter').value;
  const visible=keys.filter(item=>{
    const dated=item[dateField]?new Date(item[dateField]):null;
    const localDate=dated && !Number.isNaN(dated.getTime())?`${dated.getFullYear()}-${String(dated.getMonth()+1).padStart(2,'0')}-${String(dated.getDate()).padStart(2,'0')}`:'';
    return (!search || `${item.label} ${item.prefix}`.toLocaleLowerCase().includes(search)) &&
      (!from || localDate>=from) && (!through || localDate<=through) &&
      (!priorityFilter || String(item.priority ?? 2)===priorityFilter);
  });
  $('keyCount').textContent=`Showing ${visible.length} of ${keys.length} keys`;
  $('keys').replaceChildren(...visible.map(item=>{
    const row=document.createElement('tr'); row.className='key-row';
    cell(row,`${item.label} (${item.prefix}…)`,'Client / prefix'); cell(row,item.revoked_at?'Revoked':item.expires_at,'Expiry');
    const ipCell=row.insertCell(), ipInput=document.createElement('input');
    ipCell.dataset.label='Allowed IPs';ipInput.type='text';ipInput.value=item.allowed_ips.join(', ');ipInput.maxLength=7000;ipInput.disabled=!!item.revoked_at;ipInput.setAttribute('aria-label',`Allowed IPs for ${item.label}`);ipCell.append(ipInput);
    const concurrentCell=row.insertCell(), concurrent=document.createElement('input');
    concurrentCell.dataset.label='Concurrent';concurrent.type='number';concurrent.min='1';concurrent.max='16';concurrent.step='1';concurrent.value=item.max_concurrent;concurrent.disabled=!!item.revoked_at;concurrent.setAttribute('aria-label',`Concurrent requests for ${item.label}`);concurrentCell.append(concurrent);
    const rpmCell=row.insertCell(),rpm=document.createElement('input');
    rpmCell.dataset.label='Per minute';rpm.type='number';rpm.min='1';rpm.max='600';rpm.step='1';rpm.value=item.requests_per_minute;rpm.disabled=!!item.revoked_at;rpm.setAttribute('aria-label',`Requests per minute for ${item.label}`);rpmCell.append(rpm);
    const priorityCell=row.insertCell(),priority=document.createElement('select');
    priorityCell.dataset.label='Priority';priority.setAttribute('aria-label',`Priority for ${item.label}`);
    priority.replaceChildren(new Option('High','1'),new Option('Normal','2'),new Option('Low','3'));
    priority.value=String(item.priority ?? 2);priority.disabled=!!item.revoked_at;priorityCell.append(priority);
    const actions=row.insertCell(),save=document.createElement('button'),revoke=document.createElement('button'),remove=document.createElement('button');
    actions.dataset.label='Actions';actions.className='key-actions';
    save.textContent='Save';save.className='secondary';save.disabled=!!item.revoked_at;
    save.onclick=()=>action(save,async()=>{
      const data={allowed_ips:ipList(ipInput),max_concurrent:wholeNumber(concurrent,1,16),requests_per_minute:wholeNumber(rpm,1,600),priority:wholeNumber(priority,1,3)};
      await api(`/admin/api/keys/${item.id}`,{method:'PATCH',body:JSON.stringify(data)});
      await afterSave('Client rules saved.');
    },{'allowed_ips':ipInput,'max_concurrent':concurrent,'requests_per_minute':rpm,'priority':priority});
    revoke.textContent='Revoke';revoke.className='danger';revoke.disabled=!!item.revoked_at;
    revoke.onclick=()=>{if (!confirm(`Revoke ${item.label}? Existing requests may finish.`)) return;
      action(revoke,async()=>{await api(`/admin/api/keys/${item.id}/revoke`,{method:'POST'});await afterSave('Client key revoked.');});};
    remove.textContent='Delete';remove.className='danger';remove.disabled=!item.revoked_at;
    remove.onclick=()=>{if (!confirm(`Permanently delete revoked key ${item.label} from this panel? Its request history remains.`)) return;
      action(remove,async()=>{await api(`/admin/api/keys/${item.id}`,{method:'DELETE'});await afterSave('Revoked key deleted.');});};
    actions.append(save,revoke,remove);return row;
  }));
  if (!visible.length) {
    const row=document.createElement('tr'); const entry=cell(row,'No client keys match these filters.');entry.colSpan=7;entry.className='empty';$('keys').append(row);
  }
}
$('loginForm').onsubmit=event=>{
  event.preventDefault(); const button=event.submitter || $('loginForm').querySelector('button');
  action(button,async()=>{
    const username=textValue($('username'),80,'Username');
    if (!/^[A-Za-z0-9_-]{3,80}$/.test(username)) invalid($('username'),'Use 3–80 letters, numbers, underscores or hyphens.');
    const password=$('password').value;
    if (!password || password.length>1024) invalid($('password'),'Enter the dashboard password (up to 1,024 characters).');
    const data=await api('/admin/api/login',{method:'POST',body:JSON.stringify({username,password})});
    csrf=data.csrf_token;$('password').value='';
    await refresh({quiet:true});
    if (!$('content').hidden) status('Logged in.','success');
  },{'username':$('username'),'password':$('password')});
};
$('logout').onclick=()=>action($('logout'),async()=>{
  await api('/admin/api/logout',{method:'POST'});
  csrf='';$('content').hidden=true;$('loginPanel').hidden=false;clearErrors();status('Logged out.','success');
});
function applyFilters(){offset=0;refresh();}
$('applyFilters').onclick=applyFilters;
$('clearFilters').onclick=()=>{
  for (const id of ['filterStatus','filterTransport','filterRoute','filterClient','filterIp','filterVoice']) if ($(id)) $(id).value='';
  $('hours').value='24';$('fromDate').value='';$('toDate').value='';applyFilters();
};
$('previous').onclick=()=>{offset=Math.max(0,offset-100);refresh();};
$('next').onclick=()=>{offset=Math.min(100000,offset+100);refresh();};
$('refresh').onclick=()=>refresh();
$('hours').onchange=()=>{
  if ($('hours').value==='custom') $('fromDate').focus();
  else { $('fromDate').value='';$('toDate').value='';applyFilters(); }
};
for (const id of ['fromDate','toDate']) $(id).onchange=()=>{
  $('hours').value='custom';
  if ($('fromDate').value && $('toDate').value) applyFilters();
};
for (const id of ['keySearch','keyDateField','keyFromDate','keyToDate','keyPriorityFilter']) $(id).oninput=()=>renderKeys(currentKeys);
$('clearKeyFilters').onclick=()=>{
  for (const id of ['keySearch','keyFromDate','keyToDate','keyPriorityFilter']) $(id).value='';
  $('keyDateField').value='created_at';
  renderKeys(currentKeys);
};
$('saveGlobal').onclick=()=>action($('saveGlobal'),async()=>{
  const ips=ipList($('globalIps'));
  await api('/admin/api/allowlist',{method:'PUT',body:JSON.stringify({allowed_ips:ips})});
  await afterSave('Global allowlist saved.');
},{'allowed_ips':$('globalIps')});
$('createKey').onclick=()=>action($('createKey'),async()=>{
  const data={label:textValue($('label'),80,'Client name'),allowed_ips:ipList($('keyIps')),
    expires_days:wholeNumber($('expiry'),...LIMITS.expiry),
    max_concurrent:wholeNumber($('maxConcurrent'),...LIMITS.maxConcurrent),
    requests_per_minute:wholeNumber($('rpm'),...LIMITS.rpm),priority:wholeNumber($('priority'),1,3)};
  const issued=await api('/admin/api/keys',{method:'POST',body:JSON.stringify(data)});
  $('issuedKey').textContent=issued.key;$('issued').hidden=false;
  await afterSave('Client key created. Copy it before leaving this page.');
},{'label':$('label'),'allowed_ips':$('keyIps'),'expires_days':$('expiry'),'max_concurrent':$('maxConcurrent'),'requests_per_minute':$('rpm'),'priority':$('priority')});
$('copyKey').onclick=async()=>{try{await navigator.clipboard.writeText($('issuedKey').textContent);status('Key copied.','success');}catch{status('Copy failed. Select the key and copy it manually.','error');}};
document.addEventListener('input',event=>{
  if (event.target.hasAttribute('aria-invalid')) {
    event.target.removeAttribute('aria-invalid');
    event.target.nextElementSibling?.classList.contains('form-error') && event.target.nextElementSibling.remove();
  }
});
api('/admin/api/session').then(data=>{csrf=data.csrf_token;refresh();}).catch(error=>{
  if (error.message==='Login required') status('Log in to open the dashboard.');
  else fail(error);
});
