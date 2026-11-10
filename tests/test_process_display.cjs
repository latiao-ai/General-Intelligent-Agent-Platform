const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../src/webpage_agent/api/web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function section(start, end) {
  return script.slice(script.indexOf(start), script.indexOf(end));
}

test('SSE 指令阶段实时展示并在历史记录中恢复', () => {
  const rows = [];
  const body = {children: rows, appendChild: row => rows.push(row)};
  const turn = {classList: {add() {}}, querySelector: selector =>
    selector === '.turn-body' ? body : {textContent: ''}};
  const title = {};
  const context = {
    S: {stages: [], stageSeq: 0, skew: 0, turn: 1},
    Date, Number, Map, TURN_STATUS: {succeeded: '已完成'},
    $: id => id === 'process' ? {appendChild() {}} : title,
    createTurn: () => turn,
    stageRowEl: st => ({st}),
    markStageDone: (row, st) => { row.completed = st.status; },
    eventRowEl: ev => ev,
    appendStageRow: st => rows.push({st}),
  };
  vm.createContext(context);
  vm.runInContext(section('function renderHistory(', 'async function newSession('), context);
  vm.runInContext(section('function onStageStart(', 'function onStageEnd('), context);
  const start = {type: 'stage_start', stage: 'instruction', label: '收到指令 → 开始执行', at: Date.now() / 1000};
  context.onStageStart(start);
  assert.equal(rows.length, 1, '实时事件应产生阶段行');
  rows.length = 0;
  context.renderHistory([{status: 'succeeded', events: [start,
    {type: 'stage_end', stage: 'instruction', seconds: 1.2, status: '已回答'}]}]);
  assert.equal(rows.length, 1, '历史记录应恢复阶段行');
  assert.equal(rows[0].completed, '已回答');
  assert.match(title.textContent, /历史会话/);
});

test('指令阶段与其他阶段重叠时不重复累计耗时', () => {
  const title = {};
  const context = {
    S: {turn: 1, stages: [
      {turn: 1, stage: 'instruction', done: true, seconds: 5},
      {turn: 1, stage: 'plan', done: true, seconds: 3},
    ]},
    $: () => title,
  };
  vm.createContext(context);
  vm.runInContext(section('function elapsedOf(', 'function onStageStart('), context);
  vm.runInContext(section('function updateTotal(', 'function closeOpenStages('), context);
  context.updateTotal();
  assert.match(title.textContent, /本轮 3\.00s（累计 3\.00s）/);
  context.S.stages.pop();
  context.updateTotal();
  assert.match(title.textContent, /本轮 5\.00s（累计 5\.00s）/);
});
