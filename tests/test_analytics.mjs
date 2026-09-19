import test from 'node:test';
import assert from 'node:assert/strict';
import {decode, select, summarize, dailySeries, rolling, hourlyProfile, weekHeatmap,
  episodes, recordingGaps, minuteProfile} from '../web/analytics.mjs';

const fields=['timestamp_local','download_mbps','upload_mbps','latency_ms','failure_kind','client','server_id'];
const read=rows=>decode({fields,rows});

test('Missing readings stay absent, zero is valid, and endpoint filters retain failure evidence',()=>{
  const rows=read([
    ['2026-01-01T00:00:00',0,0,0,null,'ookla','1'],
    ['2026-01-01T00:15:00',null,null,null,'dns_resolution_failed'],
    ['2026-01-01T00:30:00',100,200,6000,null,'python_speedtest_cli','2'],
    ['2026-01-03T00:00:00',300,400,null,null,'python_speedtest_cli','2'],
  ]);
  const all=select(rows,'2026-01-01','2026-01-03');
  const filtered=select(rows,'2026-01-01','2026-01-03','ookla','1');
  assert.equal(summarize(all.observed,all.measured).down,400/3);
  assert.equal(summarize(filtered.observed,filtered.measured).down,0);
  assert.equal(summarize(filtered.observed,filtered.measured).connection,1);
  assert.equal(select(rows,'2026-01-01','2026-01-01','','absent').measured.length,0);
  const days=dailySeries(all.observed,all.measured,'2026-01-01','2026-01-03');
  assert.deepEqual(days.map(d=>d.meanDown),[50,null,300]);
  assert.equal(days[0].p95Ping,5700); // retain real high latency; null is never a zero
  assert.equal(days[2].medianPing,null);
  assert.deepEqual(rolling(days,'down'),[50,null,400/3]);
  assert.equal(rows[0].clock,Date.UTC(2026,0,1));
});

test('Intraday bands and heatmaps give each observed day equal weight',()=>{
  const rows=read([
    ['2026-01-05T12:00:00',0,0],['2026-01-05T12:15:00',100,200],
    ['2026-01-12T12:00:00',200,400],['2026-01-10T12:00:00',900,900],
  ]);
  const weekdays=hourlyProfile(rows,'weekday');
  assert.deepEqual(weekdays[12].down,{mean:125,low:100,high:150});
  assert.deepEqual(weekdays[12].up,{mean:250,low:200,high:300});
  assert.equal(weekdays[12].days,2);
  assert.equal(weekdays[12].tests,3);
  assert.equal(weekdays[13].down.mean,null);
  assert.equal(hourlyProfile(rows,'weekend')[12].down.mean,900);
  assert.equal(weekHeatmap(rows).z[0][12],125);
  assert.equal(weekHeatmap(rows).n[0][12],2);
  assert.equal(weekHeatmap(rows).z[1][12],null);
});

test('Episodes break at successes, type changes, long gaps and backward clock jumps',()=>{
  const fail=(time,kind='dns_resolution_failed')=>[`2026-01-01T${time}:00`,null,null,null,kind];
  const rows=read([
    fail('00:00'),fail('00:15'),['2026-01-01T00:30:00',1,1],fail('00:45'),
    fail('01:00','request_timeout'),fail('02:00','request_timeout'),fail('01:15','request_timeout'),
  ]);
  const e=episodes(rows);
  assert.equal(e.length,5);
  assert.equal(e[0].count,2);
  assert.equal(e[0].span,15*60000);
  assert.ok(e.every(x=>x.span>=0));
  assert.equal(recordingGaps(read([fail('00:00'),fail('01:15'),fail('04:15')])).length,1);
});

test('Schedule profile partitions every observation, including the recovered parser result',()=>{
  const p=minuteProfile(read([
    ['2026-01-01T00:00:00',100,100,1,'collector_parse_error'],
    ['2026-01-01T00:30:00',null,null,null,'server_selection_failed'],
    ['2026-01-01T01:30:00',null,null,null,'dns_resolution_failed'],
    ['2026-01-01T02:30:00',null,null,null,'test_service_http_502'],
  ]));
  assert.deepEqual(p[0],{minute:0,total:1,measured:1,selection:0,connection:0,other:0});
  assert.deepEqual(p[1],{minute:30,total:3,measured:0,selection:1,connection:1,other:1});
});
