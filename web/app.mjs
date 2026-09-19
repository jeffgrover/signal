import {DAY, GROUPS, decode, summarize, select, dailySeries, rolling, hourlyProfile,
  weekHeatmap, episodes, recordingGaps, minuteProfile, serverSummary} from './analytics.mjs';

const $ = id => document.getElementById(id);
const fmt = (n, digits=0) => Number.isFinite(n) ? n.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:digits}) : '—';
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clientLabel = client => client === 'ookla' ? 'Ookla' : client === 'python_speedtest_cli' ? 'Python CLI' : 'Unknown';
const dateLabel = date => new Date(date.slice(0,10)+'T12:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'});
const stampLabel = stamp => `${dateLabel(stamp)} · ${stamp.slice(11,16)}`;
const duration = ms => ms < 60000 ? 'Single sample' : ms >= DAY ? `${fmt(ms/DAY,1)} days` : ms >= 3600000 ? `${fmt(ms/3600000,1)} hours` : `${fmt(ms/60000)} min`;
const COLORS = {down:'#4767e8',up:'#0b9d88',muted:'#8290a6',grid:'#edf0f6',purple:'#9177cc'};
const CONFIG = {responsive:true,displaylogo:false,scrollZoom:false,modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d'],toImageButtonOptions:{format:'png',scale:2}};
let rows=[], servers=new Map(), payload, firstDate, lastDate, observed=[], measured=[], days=[], focus=null, trend='daily';
let renderVersion=0;

function layout(extra={}) {
  return {paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',
    font:{family:'Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',size:11,color:'#77869e'},
    margin:{l:62,r:24,t:30,b:48},hovermode:'x unified',dragmode:'zoom',
    hoverlabel:{bgcolor:'#fff',bordercolor:'#e0e6f1',font:{color:'#394a65',size:11}},
    legend:{orientation:'h',x:0,y:1.16,font:{size:10},traceorder:'normal'},
    xaxis:{gridcolor:COLORS.grid,showgrid:false,zeroline:false,tickfont:{size:10},automargin:true},
    yaxis:{gridcolor:COLORS.grid,zeroline:false,tickfont:{size:10},automargin:true},...extra};
}
function emptyPlot(id,message) {
  return Plotly.react(id,[],layout({xaxis:{visible:false},yaxis:{visible:false},annotations:[{text:message,
    x:.5,y:.5,xref:'paper',yref:'paper',showarrow:false,font:{size:12,color:'#8a97aa'}}]}),CONFIG);
}
function plot(id,traces,extra={}) {return Plotly.react(id,traces,layout(extra),CONFIG);}
function clickDay(id) {
  $(id).removeAllListeners('plotly_click');
  $(id).on('plotly_click',e=>{if(e.points?.[0]?.x) openDay(String(e.points[0].x).slice(0,10));});
}
function trace(name,x,y,color,more={}) {
  return {name,x,y,type:'scatter',mode:'lines',connectgaps:false,line:{color,width:2.2},
    hovertemplate:'%{y:,.1f}<extra>%{fullData.name}</extra>',...more};
}
function setPressed(selector, match) {
  document.querySelectorAll(selector).forEach(b=>{const active=match(b);b.classList.toggle('selected',active);b.setAttribute('aria-pressed',active);});
}
function updateRangeButtons() {
  setPressed('[data-range]',b=>{
    const start=b.dataset.range==='all'?firstDate:new Date(Date.parse(lastDate+'T00:00:00Z')-(+b.dataset.range-1)*DAY).toISOString().slice(0,10);
    return $('start').value=== (start<firstDate?firstDate:start) && $('end').value===lastDate;
  });
}
function applyRange(range) {
  $('end').value=lastDate;
  $('start').value=range==='all'?firstDate:new Date(Math.max(Date.parse(firstDate+'T00:00:00Z'),Date.parse(lastDate+'T00:00:00Z')-(+range-1)*DAY)).toISOString().slice(0,10);
  render();
}

async function render() {
  const start=$('start').value,end=$('end').value;
  if(!start||!end||start>end) return;
  const version=++renderVersion;
  ({observed,measured}=select(rows,start,end,$('client').value,$('server').value));
  days=dailySeries(observed,measured,start,end);
  updateRangeButtons();
  const s=summarize(observed,measured);
  $('avg-down').textContent=fmt(s.down,1);$('avg-up').textContent=fmt(s.up,1);
  $('down-caption').textContent=`Sample mean · ${fmt(s.measured)} measured tests`;
  $('up-caption').textContent=`Sample mean · ${fmt(s.measured)} measured tests`;
  $('connection-count').textContent=fmt(s.connection);$('row-count').textContent=fmt(s.count);
  $('row-caption').textContent=`${fmt(s.measured)} match performance filters`;
  $('selection-label').textContent=`${dateLabel(start)} – ${dateLabel(end)} · ${fmt(days.length)} calendar days`;
  $('inspect-date').min=start;$('inspect-date').max=end;
  if(!$('inspect-date').value||$('inspect-date').value<start||$('inspect-date').value>end) $('inspect-date').value=end;
  $('latency-caption').textContent=`${fmt(s.invalid)} known failure placeholders excluded in this performance selection. High measured latency remains. Client methods differ.`;
  $('export').disabled=!measured.length;
  renderFailureCards(s);renderEpisodes();renderEndpoints();
  await Promise.all([renderTrend(),renderFailures(),renderMinutes(),renderHourly(),renderHeat(),renderLatency()]);
  if(version===renderVersion) {
    $('load-status').hidden=true;
    $('selection-label').setAttribute('aria-label',`${fmt(s.count)} observations from ${start} through ${end}`);
  }
}
async function renderTrend() {
  $('trend-note').textContent=trend==='weekly'?'Trailing 7 calendar days, sample-weighted within this date range. Empty days stay empty.':'Hover to compare · drag to zoom · click a day to inspect';
  if(!measured.length) return emptyPlot('trend-chart','No measurements match these performance filters.');
  const x=days.map(d=>d.date), isWeekly=trend==='weekly';
  const down=isWeekly?rolling(days,'down'):days.map(d=>d.meanDown);
  const up=isWeekly?rolling(days,'up'):days.map(d=>d.meanUp);
  await plot('trend-chart',[
    trace('Download',x,down,COLORS.down,{customdata:days.map(d=>d.n),hovertemplate:'%{y:,.1f} Mbps<br>%{customdata:,} measured tests on this day<extra>Download</extra>'}),
    trace('Upload',x,up,COLORS.up,{customdata:days.map(d=>d.n),hovertemplate:'%{y:,.1f} Mbps<br>%{customdata:,} measured tests on this day<extra>Upload</extra>'})
  ],{yaxis:{title:{text:'Bandwidth · Mbps',standoff:10,font:{size:10}},rangemode:'tozero',gridcolor:COLORS.grid,zeroline:false,tickfont:{size:10}},
    xaxis:{type:'date',showgrid:false,tickformat:days.length>60?'%b %Y':'%b %d',nticks:window.innerWidth<600?4:8,zeroline:false,tickfont:{size:10}},
    margin:{l:65,r:24,t:39,b:46}});
  clickDay('trend-chart');
}
function renderFailureCards(s) {
  $('failure-cards').innerHTML=Object.entries(GROUPS).filter(([k])=>k!=='unknown'||s.kinds[k]).map(([key,g])=>`
    <button class="failure-card" data-failure="${key}" aria-pressed="${focus===key}"><span class="failure-label"><span class="dot" style="background:${g.color}"></span>${esc(g.short)}</span><span class="failure-number">${fmt(s.kinds[key])}</span></button>`).join('');
}
async function renderFailures() {
  const keys=Object.keys(GROUPS).filter(k=>(!focus||focus===k)&&days.some(d=>d.failures[k]));
  if(!keys.length) return emptyPlot('failure-chart','No failures of this type were recorded in this date range.');
  const traces=keys.map(k=>{
    const points=days.filter(d=>d.failures[k]);
    return {name:GROUPS[k].label,type:'scatter',mode:'markers',x:points.map(d=>d.date),y:points.map(()=>GROUPS[k].label),
      customdata:points.map(d=>d.failures[k]),marker:{size:points.map(d=>7+Math.sqrt(d.failures[k])*1.05),color:GROUPS[k].color,opacity:.8},
      hovertemplate:'%{x|%b %d, %Y}<br>%{customdata:,} failed-test observations<extra>%{fullData.name}</extra>'};
  });
  await plot('failure-chart',traces,{hovermode:'closest',showlegend:false,margin:{l:window.innerWidth<600?112:175,r:30,t:22,b:40},
    xaxis:{type:'date',range:[Date.parse(days[0].date+'T00:00:00Z')-DAY/2,Date.parse(days.at(-1).date+'T00:00:00Z')+DAY/2],showgrid:true,gridcolor:'#f1f3f8',tickfont:{size:10},nticks:window.innerWidth<600?4:8},
    yaxis:{type:'category',categoryorder:'array',categoryarray:keys.map(k=>GROUPS[k].label).reverse(),gridcolor:'#f1f3f8',tickfont:{size:window.innerWidth<600?8:10},automargin:true}});
  clickDay('failure-chart');
}
function renderEpisodes() {
  const all=episodes(observed).filter(e=>!focus||e.group===focus), shown=all.slice(0,6);
  $('episodes').innerHTML=shown.length?shown.map(e=>`<tr><td><span class="dot" style="background:${GROUPS[e.group].color};margin-right:7px"></span><strong>${esc(GROUPS[e.group].short)}</strong></td><td>${stampLabel(e.start)}</td><td>${stampLabel(e.end)}</td><td class="numeric">${fmt(e.count)}</td><td class="numeric">${duration(e.span)}</td><td><button class="row-button" data-day="${e.start.slice(0,10)}">Inspect →</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty-row">No failure episodes in this selection.</td></tr>';
  $('episode-caption').textContent=`${fmt(all.length)} episodes${focus?' · '+GROUPS[focus].short:''}. Showing up to 6 longest observed spans; these are not outage durations.`;
}
function renderMinutes() {
  const bins=minuteProfile(observed);
  if(!bins.length) return emptyPlot('minute-chart','No observations in this date range.');
  return plot('minute-chart',[
    ['measured','Measured result',COLORS.down],['selection','Server selection',COLORS.purple],
    ['connection','DNS / routing / timeout',GROUPS.dns.color],['other','Other failures','#8190a6']
  ].map(([key,name,color])=>({type:'bar',name,x:bins.map(b=>':'+String(b.minute).padStart(2,'0')),
    y:bins.map(b=>b[key]/b.total*100),customdata:bins.map(b=>[b[key],b.total]),marker:{color},
    hovertemplate:'%{y:.1f}%<br>%{customdata[0]:,} of %{customdata[1]:,} observations<extra>%{fullData.name}</extra>'})),
  {barmode:'stack',bargap:.45,margin:{l:58,r:24,t:window.innerWidth<600?110:65,b:45},
    legend:{orientation:'h',x:0,y:1.02,yanchor:'bottom',traceorder:'normal',font:{size:10}},
    xaxis:{type:'category',tickvals:bins.filter(b=>b.minute%15===0).map(b=>':'+String(b.minute).padStart(2,'0')),
      title:{text:'Recorded minute of hour',font:{size:10}},tickfont:{size:10}},
    yaxis:{range:[0,100],ticksuffix:'%',gridcolor:COLORS.grid,tickfont:{size:10},zeroline:false}});
}
async function renderHourly() {
  const profile=hourlyProfile(measured,$('day-type').value),x=profile.map(h=>h.hour);
  await Promise.all(['down','up'].map(async field=>{
    const id=`hourly-${field}`,color=COLORS[field],band=field==='down'?'rgba(71,103,232,.11)':'rgba(11,157,136,.11)';
    if(!profile.some(h=>h.days)) return emptyPlot(id,'No matching measurements.');
    await plot(id,[
      trace('Average daily low',x,profile.map(h=>h[field].low),color,{showlegend:false,line:{width:0},hoverinfo:'skip',hovertemplate:null}),
      trace('Average daily high',x,profile.map(h=>h[field].high),color,{showlegend:false,line:{width:0},fill:'tonexty',fillcolor:band,hoverinfo:'skip',hovertemplate:null}),
      trace('Hourly average',x,profile.map(h=>h[field].mean),color,{showlegend:false,mode:'lines+markers',marker:{size:4},
        customdata:profile.map(h=>[h[field].low,h[field].high,h.days,h.tests]),
        hovertemplate:'Mean %{y:,.1f} Mbps<br>Avg. daily low %{customdata[0]:,.1f}<br>Avg. daily high %{customdata[1]:,.1f}<br>%{customdata[2]} days · %{customdata[3]} tests<extra></extra>'})
    ],{margin:{l:57,r:20,t:14,b:49},xaxis:{title:{text:'Hour of day · collector local time',font:{size:10},standoff:13},tickvals:[0,4,8,12,16,20,23],ticktext:['00','04','08','12','16','20','23'],range:[0,23],showgrid:false,zeroline:false,tickfont:{size:10}},
      yaxis:{title:{text:'Mbps',font:{size:10},standoff:8},rangemode:'tozero',gridcolor:COLORS.grid,zeroline:false,tickfont:{size:10}}});
  }));
}
function renderHeat() {
  if(!measured.length) return emptyPlot('heat-chart','No matching measurements.');
  const h=weekHeatmap(measured,$('heat-metric').value);
  return plot('heat-chart',[{type:'heatmap',x:Array.from({length:24},(_,i)=>i),y:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],z:h.z,customdata:h.n,
    xgap:3,ygap:4,colorscale:[[0,'#eef2fc'],[.35,'#bdd0f3'],[.7,'#728fdf'],[1,'#415fc2']],
    hoverongaps:false,hovertemplate:'%{y} · %{x}:00<br>%{z:,.1f} Mbps<br>%{customdata} observed days<extra></extra>',
    colorbar:{title:{text:'Mbps',font:{size:9}},thickness:7,len:.85,tickfont:{size:9},outlinewidth:0}}],
    {margin:{l:43,r:32,t:15,b:45},hovermode:'closest',xaxis:{title:{text:'Hour of day',font:{size:10}},tickvals:[0,4,8,12,16,20,23],tickfont:{size:10},showgrid:false,zeroline:false},
      yaxis:{autorange:'reversed',tickfont:{size:10},showgrid:false,zeroline:false}});
}
async function renderLatency() {
  if(!days.some(d=>d.latency.length)) return emptyPlot('latency-chart','No valid latency measurements in this selection.');
  const log=$('latency-log').checked,x=days.map(d=>d.date);
  await plot('latency-chart',[
    trace('Median',x,days.map(d=>log&&d.medianPing===0?null:d.medianPing),COLORS.up),
    trace('95th percentile',x,days.map(d=>log&&d.p95Ping===0?null:d.p95Ping),COLORS.purple,{line:{color:COLORS.purple,width:1.4,dash:'dot'}})
  ],{margin:{l:58,r:20,t:35,b:43},legend:{orientation:'h',x:0,y:1.18,font:{size:9}},
    xaxis:{type:'date',showgrid:false,nticks:4,tickformat:'%b %Y',tickfont:{size:9}},
    yaxis:{title:{text:log?'Latency · ms (log)':'Latency · ms',font:{size:10}},type:log?'log':'linear',gridcolor:COLORS.grid,zeroline:false,tickfont:{size:9}}});
  clickDay('latency-chart');
}
function renderEndpoints() {
  const grouped=serverSummary(measured,servers);
  $('endpoint-count').textContent=`${fmt(grouped.length)} client / endpoint pairs`;
  $('endpoint-rows').innerHTML=grouped.length?grouped.slice(0,8).map(g=>`<tr><td><strong>${esc(g.server?.name||'Unknown endpoint')}</strong><span class="subtext">Server ${esc(g.id)}</span></td><td>${esc(g.server?.location||'Not recorded')}</td><td>${clientLabel(g.client)}</td><td class="numeric">${fmt(g.n)}</td><td class="numeric">${fmt(g.down,1)} <span class="subtext-inline">Mbps</span></td><td class="numeric">${fmt(g.up,1)} Mbps</td><td><button class="row-button" data-endpoint="${esc(g.id)}" data-client="${esc(g.client)}">Focus →</button></td></tr>`).join(''):'<tr><td colspan="7" class="empty-row">No endpoints match these filters.</td></tr>';
}
function renderArchiveNotes() {
  const full=select(rows,firstDate,lastDate),s=summarize(rows,full.measured),gaps=recordingGaps(rows);
  const corrected=rows.filter(r=>r.bandwidth_status==='corrected_x8').length,recovered=rows.filter(r=>r.bandwidth_status==='recovered_from_log').length;
  $('quality-summary').innerHTML=[
    [fmt(rows.length),'original rows preserved'],[fmt(corrected),'bandwidth records corrected ×8'],[fmt(s.invalid),'invalid latency values excluded'],[fmt(recovered),'parser-error result recovered']
  ].map(([n,label])=>`<div class="quality-item"><strong>${n}</strong><span>${label}</span></div>`).join('');
  $('gap-rows').innerHTML=gaps.length?gaps.map(g=>`<tr><td>${stampLabel(g.start)}</td><td>${stampLabel(g.end)}</td><td class="numeric">${duration(g.elapsed)}</td></tr>`).join(''):'<tr><td colspan="3" class="empty-row">No recording gaps longer than two hours.</td></tr>';
  const pythonTop=serverSummary(full.measured.filter(r=>r.client==='python_speedtest_cli'),servers)[0];
  $('recommendations').innerHTML=`
    <article class="recommendation"><div class="recommendation-top"><span class="recommendation-number">01</span><span class="recommendation-tag">PERFORMANCE</span></div><h3>Is the connection slower,<br>or the test different?</h3><p>The later slowdown survives the units correction. But clients and test endpoints change. Start by holding both constant.</p><p class="next-step"><strong>Build next:</strong> a fixed-server baseline, annotated with router or ISP changes.</p><button id="recommend-performance">Compare a consistent endpoint →</button></article>
    <article class="recommendation"><div class="recommendation-top"><span class="recommendation-number">02</span><span class="recommendation-tag">DNS & CONNECTIVITY</span></div><h3>${fmt(s.kinds.dns)} DNS failures.<br>Which layer failed?</h3><p>Resolution errors cluster in the archive. Your redundant Pi-holes give us two useful points to compare, but these tests don’t identify the resolver.</p><p class="next-step"><strong>Build next:</strong> separate probes to each Pi-hole, the gateway, and an external IP.</p><button id="recommend-dns">Explore DNS failure episodes →</button></article>
    <article class="recommendation"><div class="recommendation-top"><span class="recommendation-number">03</span><span class="recommendation-tag">MEASUREMENT QUALITY</span></div><h3>The monitor needs<br>a pulse check, too.</h3><p>${fmt(s.kinds.selection)} server-selection failures and ${gaps.length?`a ${fmt(gaps[0].elapsed/DAY,1)}-day recording gap`:'missing observations'} complicate the story. A quiet collector can look like a quiet network.</p><p class="next-step"><strong>Build next:</strong> a supervised collector, heartbeat and explicit test-stage results.</p><button id="recommend-collector">Examine the half-hour pattern →</button></article>`;
  $('recommend-performance').onclick=()=>{if(pythonTop){$('client').value=pythonTop.client;$('server').value=pythonTop.id;}applyRange('all');$('history').scrollIntoView({behavior:'smooth'});};
  $('recommend-dns').onclick=()=>{focus='dns';applyRange('all');$('failures').scrollIntoView({behavior:'smooth'});};
  $('recommend-collector').onclick=()=>{focus='selection';applyRange('all');$('schedule').scrollIntoView({behavior:'smooth'});};
  $('archive-footer').textContent=`${dateLabel(lastDate)} snapshot · ${fmt(rows.length)} observations · offline charts`;
}
async function openDay(date) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
  const selected=rows.filter(r=>r.date===date),valid=selected.filter(r=>Number.isFinite(r.download_mbps)),s=summarize(selected,valid);
  $('day-title').textContent=dateLabel(date);
  $('day-summary').textContent=`${fmt(selected.length)} observations · ${fmt(valid.length)} measured results · ${fmt(s.connection)} DNS/routing/timeout observations`;
  $('day-failures').innerHTML=Object.entries(s.kinds).filter(([,n])=>n).map(([k,n])=>`<span><span class="dot" style="background:${GROUPS[k].color};margin-right:5px"></span>${esc(GROUPS[k].short)}: ${fmt(n)}</span>`).join('');
  $('day-rows').innerHTML=selected.length?selected.map(r=>{
    let label=r.failure_kind?GROUPS[r.group].short:'Measured';
    if(r.bandwidth_status==='corrected_x8') label='Corrected ×8';
    if(r.bandwidth_status==='recovered_from_log') label='Recovered parser result';
    if(r.latency_status==='invalid_cli_failure_sentinel') label='Measured · invalid latency';
    const detail=`Row ${r.source_rowid}${r.source_log_line?' · Log line '+r.source_log_line:''}. Original down: ${r.original_download}; up: ${r.original_upload}; ping: ${r.original_ping}. ${r.original_error||''}`;
    return `<tr><td>${esc(r.timestamp_local.slice(11,19))}</td><td class="numeric">${fmt(r.download_mbps,1)}</td><td class="numeric">${fmt(r.upload_mbps,1)}</td><td class="numeric">${fmt(r.latency_ms,1)}</td><td><span class="quality-tag ${r.group||r.latency_status==='invalid_cli_failure_sentinel'?'warn':''}" title="${esc(detail)}">${esc(label)}</span></td><td>${esc(servers.get(r.server_id)?.name||'Not recorded')}</td></tr>`;
  }).join(''):'<tr><td colspan="6" class="empty-row">No observations were recorded on this day.</td></tr>';
  if(!$('day-dialog').open) $('day-dialog').showModal();
  if(!selected.length) return emptyPlot('day-chart','No observations recorded. This is not evidence of an outage.');
  // Break lines at recorded failures, long recording gaps, and backward clock jumps.
  const points=selected.flatMap((r,i)=>i&&(r.clock-selected[i-1].clock>30*60000||r.clock<selected[i-1].clock)
    ? [{timestamp_local:r.timestamp_local,download_mbps:null,upload_mbps:null},r] : [r]);
  await plot('day-chart',[
    trace('Download',points.map(r=>r.timestamp_local),points.map(r=>r.download_mbps),COLORS.down,{mode:'lines+markers',marker:{size:4}}),
    trace('Upload',points.map(r=>r.timestamp_local),points.map(r=>r.upload_mbps),COLORS.up,{mode:'lines+markers',marker:{size:4}})
  ],{margin:{l:60,r:25,t:35,b:40},xaxis:{type:'date',tickformat:'%H:%M',nticks:6,showgrid:false,tickfont:{size:10}},
    yaxis:{title:{text:'Mbps',font:{size:10}},rangemode:'tozero',gridcolor:COLORS.grid,zeroline:false,tickfont:{size:10}}});
}
function exportSamples() {
  const fields=['source_rowid','timestamp_local','download_mbps','upload_mbps','latency_ms','client','server_id','bandwidth_status','latency_status','original_download','original_upload','original_ping','source_log_line'];
  const cell=value=>{let s=String(value??'');if(typeof value==='string'&&/^[=+\-@\t\r]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};
  const csv=[fields.join(','),...measured.map(r=>fields.map(k=>cell(r[k])).join(','))].join('\r\n');
  const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download=`signal-samples-${$('start').value}-${$('end').value}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function wireEvents() {
  document.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>applyRange(b.dataset.range));
  document.querySelectorAll('[data-trend]').forEach(b=>b.onclick=()=>{trend=b.dataset.trend;setPressed('[data-trend]',x=>x===b);renderTrend();});
  for(const id of ['start','end']) $(id).onchange=()=>{
    if(!$(id).value||!$(id).checkValidity()){ $(id).reportValidity();return; }
    if($('start').value>$('end').value) $(id==='start'?'end':'start').value=$(id).value;
    render();
  };
  $('client').onchange=render;$('server').onchange=render;
  $('reset').onclick=()=>{$('client').value='';$('server').value='';focus=null;$('day-type').value='all';$('heat-metric').value='download_mbps';$('latency-log').checked=true;trend='daily';setPressed('[data-trend]',b=>b.dataset.trend==='daily');applyRange('all');};
  $('clear-performance').onclick=()=>{$('client').value='';$('server').value='';render();};
  $('failure-cards').onclick=e=>{const b=e.target.closest('[data-failure]');if(b){focus=focus===b.dataset.failure?null:b.dataset.failure;render();}};
  $('all-failures').onclick=()=>{focus=null;render();};
  $('day-type').onchange=renderHourly;$('heat-metric').onchange=renderHeat;$('latency-log').onchange=renderLatency;
  $('inspect-day').onclick=()=>{if($('inspect-date').checkValidity())openDay($('inspect-date').value);else $('inspect-date').reportValidity();};
  $('close-day').onclick=()=>$('day-dialog').close();
  $('day-dialog').onclick=e=>{if(e.target===$('day-dialog')){const r=$('day-dialog').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('day-dialog').close();}};
  $('episodes').onclick=e=>{const b=e.target.closest('[data-day]');if(b)openDay(b.dataset.day);};
  $('endpoint-rows').onclick=e=>{const b=e.target.closest('[data-endpoint]');if(b){$('client').value=b.dataset.client;$('server').value=b.dataset.endpoint;render();$('history').scrollIntoView({behavior:'smooth'});}};
  $('export').onclick=exportSamples;
  const navLinks=[...document.querySelectorAll('nav a')];
  const observer=new IntersectionObserver(entries=>{for(const entry of entries){if(entry.isIntersecting){navLinks.forEach(a=>a.classList.toggle('active',a.hash==='#'+entry.target.id));}}},{rootMargin:'-10% 0px -65% 0px'});
  ['overview','history','failures','rhythm','investigate'].forEach(id=>observer.observe($(id)));
}
async function init() {
  try {
    const response=await fetch('/api/data');if(!response.ok)throw new Error(`Archive request failed (${response.status})`);
    payload=await response.json();rows=decode(payload);
    if(!rows.length)throw new Error('The analysis database has no observations.');
    const dates=rows.map(r=>r.date).sort();firstDate=dates[0];lastDate=dates.at(-1);
    payload.servers.forEach(([id,name,location,country])=>servers.set(id,{id,name,location,country}));
    const counts=new Map();rows.forEach(r=>{if(r.server_id)counts.set(r.server_id,(counts.get(r.server_id)||0)+1);});
    const options=[...servers.values()].sort((a,b)=>(counts.get(b.id)||0)-(counts.get(a.id)||0));
    $('server').innerHTML=`<option value="">All ${fmt(options.length)} test endpoints</option>`+options.map(s=>`<option value="${esc(s.id)}">${esc(s.name||s.id)} · ${esc(s.location||'')} (${fmt(counts.get(s.id)||0)} tests)</option>`).join('');
    for(const id of ['start','end']) {$(id).min=firstDate;$(id).max=lastDate;$(id).required=true;}
    $('start').value=firstDate;$('end').value=lastDate;$('inspect-date').value=lastDate;$('inspect-date').required=true;
    wireEvents();renderArchiveNotes();await render();
  } catch(error) {$('load-status').hidden=false;$('load-status').classList.add('error');$('load-status').textContent=`Could not open the dashboard: ${error.message}`;console.error(error);}
}
init();
