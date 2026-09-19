export const DAY = 86400000;
export const GROUPS = {
  dns: {label: 'DNS resolution', color: '#d79828', short: 'DNS'},
  routing: {label: 'Routing / unreachable', color: '#de625b', short: 'Routing'},
  timeout: {label: 'Request timeout', color: '#cc7c45', short: 'Timeout'},
  selection: {label: 'Server selection · uncertain', color: '#9177cc', short: 'Server selection'},
  service: {label: 'Test service HTTP 502', color: '#a06895', short: 'Test service'},
  collector: {label: 'Collector parser · recovered', color: '#8190a6', short: 'Collector'},
  unknown: {label: 'Unclassified failure', color: '#8190a6', short: 'Unclassified'},
};
export function failureGroup(kind) {
  if (!kind) return null;
  return ({dns_resolution_failed:'dns', no_route_to_host:'routing', network_unreachable:'routing',
    request_timeout:'timeout', server_selection_failed:'selection', test_service_http_502:'service',
    collector_parse_error:'collector'})[kind] || 'unknown';
}
export function mean(values) {
  const a = values.filter(Number.isFinite);
  return a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
}
export function percentile(values, fraction) {
  const a = values.filter(Number.isFinite).sort((a,b) => a-b);
  if (!a.length) return null;
  const i = (a.length-1)*fraction, lo = Math.floor(i), hi = Math.ceil(i);
  return a[lo] + (a[hi]-a[lo])*(i-lo);
}
export function decode(payload) {
  return payload.rows.map(values => {
    const r = Object.fromEntries(payload.fields.map((key, i) => [key, values[i]]));
    r.date = r.timestamp_local.slice(0,10);
    r.hour = +r.timestamp_local.slice(11,13);
    // A virtual clock keeps the collector's naive wall time unchanged in any browser timezone.
    r.clock = Date.parse(r.timestamp_local + 'Z');
    r.weekday = new Date(r.date + 'T12:00:00Z').getUTCDay();
    r.group = failureGroup(r.failure_kind);
    return r;
  });
}
export function select(rows, start, end, client='', server='') {
  const observed = rows.filter(r => r.date >= start && r.date <= end);
  const measured = observed.filter(r => Number.isFinite(r.download_mbps) && Number.isFinite(r.upload_mbps)
    && (!client || r.client === client) && (!server || r.server_id === server));
  return {observed, measured};
}
export function summarize(observed, measured) {
  const kinds = Object.fromEntries(Object.keys(GROUPS).map(k => [k, 0]));
  observed.forEach(r => { if (r.group) kinds[r.group]++; });
  return {count: observed.length, measured: measured.length, down: mean(measured.map(r=>r.download_mbps)),
    up: mean(measured.map(r=>r.upload_mbps)), latency: percentile(measured.map(r=>r.latency_ms), .5),
    kinds, connection: kinds.dns + kinds.routing + kinds.timeout,
    invalid: measured.filter(r=>r.latency_status === 'invalid_cli_failure_sentinel').length};
}
export function dailySeries(observed, measured, start, end) {
  const bins = new Map();
  for (let t = Date.parse(start+'T00:00:00Z'); t <= Date.parse(end+'T00:00:00Z'); t += DAY) {
    const date = new Date(t).toISOString().slice(0,10);
    bins.set(date, {date, down:[], up:[], latency:[], count:0, invalid:0, failures:{}});
  }
  observed.forEach(r => {
    const b = bins.get(r.date); b.count++;
    if (r.group) b.failures[r.group] = (b.failures[r.group] || 0)+1;
  });
  measured.forEach(r => {
    const b = bins.get(r.date); b.down.push(r.download_mbps); b.up.push(r.upload_mbps);
    if (Number.isFinite(r.latency_ms)) b.latency.push(r.latency_ms);
    else b.invalid++;
  });
  return [...bins.values()].map(b => ({...b, n:b.down.length,
    meanDown:mean(b.down), meanUp:mean(b.up), medianPing:percentile(b.latency,.5), p95Ping:percentile(b.latency,.95)}));
}
export function rolling(days, field, window=7) {
  return days.map((day,i) => day.n ? mean(days.slice(Math.max(0,i-window+1),i+1).flatMap(d=>d[field])) : null);
}
export function hourlyProfile(rows, dayType='all') {
  const cells = new Map();
  rows.filter(r => dayType==='all' || (dayType==='weekend') === [0,6].includes(r.weekday)).forEach(r => {
    const key = `${r.date}/${r.hour}`;
    if (!cells.has(key)) cells.set(key, {hour:r.hour, weekday:r.weekday, down:[], up:[]});
    const c = cells.get(key); c.down.push(r.download_mbps); c.up.push(r.upload_mbps);
  });
  const hours = Array.from({length:24},(_,hour)=>({hour, cells:[]}));
  [...cells.values()].forEach(c=>hours[c.hour].cells.push(c));
  return hours.map(h => {
    const result = {hour:h.hour, days:h.cells.length, tests:h.cells.reduce((s,c)=>s+c.down.length,0)};
    for (const field of ['down','up']) {
      result[field] = {mean:mean(h.cells.map(c=>mean(c[field]))),
        low:mean(h.cells.map(c=>Math.min(...c[field]))), high:mean(h.cells.map(c=>Math.max(...c[field])))};
    }
    return result;
  });
}
export function weekHeatmap(rows, field='download_mbps') {
  const dayHours = new Map();
  rows.forEach(r => {
    const key = `${r.date}/${r.hour}`;
    if (!dayHours.has(key)) dayHours.set(key, {weekday:(r.weekday+6)%7,hour:r.hour,values:[]});
    dayHours.get(key).values.push(r[field]);
  });
  const cells = Array.from({length:7},()=>Array.from({length:24},()=>[]));
  dayHours.forEach(c=>cells[c.weekday][c.hour].push(mean(c.values)));
  return {z:cells.map(row=>row.map(mean)), n:cells.map(row=>row.map(c=>c.length))};
}
export function episodes(rows) {
  const result = []; let active = null;
  for (const r of rows) {
    const gap = active ? r.clock-active.endClock : Infinity;
    if (!r.group) {active=null; continue;}
    if (!active || active.group!==r.group || gap<0 || gap>30*60000) {
      active={group:r.group,start:r.timestamp_local,end:r.timestamp_local,endClock:r.clock,count:0,span:0};
      result.push(active);
    }
    active.end=r.timestamp_local; active.endClock=r.clock; active.count++;
    active.span=Date.parse(active.end+'Z')-Date.parse(active.start+'Z');
  }
  return result.sort((a,b)=>b.span-a.span || b.count-a.count);
}
export function recordingGaps(rows) {
  const result = [];
  for (let i=1;i<rows.length;i++) {
    const elapsed = rows[i].clock-rows[i-1].clock;
    // Two-hour threshold avoids treating a one-hour DST jump as a recording gap.
    if (elapsed > 2*3600000) result.push({start:rows[i-1].timestamp_local,end:rows[i].timestamp_local,elapsed});
  }
  return result.sort((a,b)=>b.elapsed-a.elapsed);
}
export function minuteProfile(rows) {
  const bins = Array.from({length:60},(_,minute)=>({minute,total:0,measured:0,selection:0,connection:0,other:0}));
  rows.forEach(r=>{
    const b=bins[+r.timestamp_local.slice(14,16)]; b.total++;
    if(Number.isFinite(r.download_mbps)) b.measured++;
    else if(r.group==='selection') b.selection++;
    else if(['dns','routing','timeout'].includes(r.group)) b.connection++;
    else b.other++;
  });
  return bins.filter(b=>b.total);
}
export function serverSummary(rows, servers) {
  const groups=new Map();
  rows.forEach(r => {
    const key = `${r.client}/${r.server_id}`;
    if (!groups.has(key)) groups.set(key,{client:r.client,id:r.server_id,values:[]});
    groups.get(key).values.push(r);
  });
  return [...groups.values()].map(g=>({...g,server:servers.get(g.id),n:g.values.length,
    down:mean(g.values.map(r=>r.download_mbps)),up:mean(g.values.map(r=>r.upload_mbps))}))
    .sort((a,b)=>b.n-a.n);
}
