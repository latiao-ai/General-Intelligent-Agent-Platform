// 假 Pi 桥接进程：纯协议回显，不含 Pi SDK，用于 Python 侧假进程测试。
//
// 通过环境变量控制行为：
//   FAKE_MODE = echo      收到 start 后直接回 finished(answered, answer="你好")
//             = tool      收到 start 后回 tool_call(web_search)，等 tool_result 回 finished
//             = multi_tool 收到 start 后回两个 tool_call（测取消后不派发剩余）
//             = error_stop 收到 start 后回 finished(outcome=failed)（测错误停止原因）
//             = tool_err  收到 start 后回 tool_call(web_search 参数类型错)
//                         （测工具参数 schema 校验）
//             = tool_fail 收到 start 后回 tool_call(nope)（测未知工具拒绝）
//             = missing_query 收到 start 后回 tool_call(web_search 缺 query)（测必填拒绝）
//             = duplicate_call_id 收到 start 后回两个相同 call_id（测重复 ID）
//             = badenvelope 收到 start 后回一条 protocol_version 错误的消息（测
//                         收发路径的信封校验确实生效）
//             = crash     收到 start 后直接 exit(1)
//             = noisy     启动时往 stderr 写大量输出
//             = hang      收到 start 后不响应（测启动/模型超时）
//             = badjson   启动后输出一行非法 JSON
//   FAKE_READY = no        不输出 ready（测启动超时）
const readline = require('readline');

const execId = process.env.PI_EXECUTION_ID || 'unknown';
const mode = process.env.FAKE_MODE || 'echo';
const ready = process.env.FAKE_READY !== 'no';

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function envelope(type, extra) {
  return Object.assign({ protocol_version: 1, execution_id: execId, type }, extra || {});
}

if (mode === 'noisy') {
  for (let i = 0; i < 5000; i++) {
    process.stderr.write('noise line ' + i + ' padding padding padding padding padding\n');
  }
}

if (ready) {
  send(envelope('ready'));
}

if (mode === 'badjson') {
  process.stdout.write('this is not json\n');
  setTimeout(() => process.exit(0), 50);
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (e) {
    return;
  }
  if (msg.type === 'start') {
    if (mode === 'echo') {
      // 分片输出文本增量，测跨分片/连续 JSON 与 Unicode
      send(envelope('event', { kind: 'text_delta', delta: '你' }));
      send(envelope('event', { kind: 'text_delta', delta: '好😀' }));
      send(envelope('finished', { outcome: 'answered', answer: '你好😀' }));
    } else if (mode === 'tool') {
      send(envelope('tool_call', { call_id: 'call-1', tool_name: 'web_search', arguments: { query: '今天天气' } }));
    } else if (mode === 'multi_tool') {
      // 同一模型轮次多个工具调用：取消后应不再派发剩余
      send(envelope('tool_call', { call_id: 'call-1', tool_name: 'web_search', arguments: { query: 'a' } }));
      send(envelope('tool_call', { call_id: 'call-2', tool_name: 'web_search', arguments: { query: 'b' } }));
    } else if (mode === 'error_stop') {
      send(envelope('finished', { outcome: 'failed', error: '模型返回失败' }));
    } else if (mode === 'badenvelope') {
      // 协议版本错误的信封：验证 Python 收发路径的信封校验确实生效
      send({ protocol_version: 999, execution_id: execId, type: 'event', kind: 'text_delta', delta: 'x' });
    } else if (mode === 'tool_err') {
      // 参数类型错误：query 不是字符串
      send(envelope('tool_call', { call_id: 'call-1', tool_name: 'web_search', arguments: { query: 123 } }));
    } else if (mode === 'tool_fail') {
      send(envelope('tool_call', { call_id: 'call-1', tool_name: 'nope', arguments: {} }));
    } else if (mode === 'missing_query') {
      // 缺少必填 query：应在搜索前拒绝
      send(envelope('tool_call', { call_id: 'call-1', tool_name: 'web_search', arguments: {} }));
    } else if (mode === 'duplicate_call_id') {
      // 同一轮重复 call_id：应被 Node 侧拒绝
      send(envelope('tool_call', { call_id: 'dup-1', tool_name: 'web_search', arguments: { query: 'a' } }));
      send(envelope('tool_call', { call_id: 'dup-1', tool_name: 'web_search', arguments: { query: 'b' } }));
    } else if (mode === 'crash') {
      process.exit(1);
    } else if (mode === 'hang') {
      // 不响应
    }
  } else if (msg.type === 'tool_result') {
    if (mode === 'tool') {
      send(envelope('finished', { outcome: 'answered', answer: '搜索结果' }));
    } else if (mode === 'multi_tool') {
      send(envelope('finished', { outcome: 'answered', answer: 'multi done' }));
    } else if (mode === 'tool_err') {
      // 工具失败：回传时保留错误正文
      send(envelope('finished', { outcome: 'answered', answer: '工具参数非法，已上报错误' }));
    } else if (mode === 'tool_fail') {
      send(envelope('finished', { outcome: 'answered', answer: '工具失败但继续' }));
    } else if (mode === 'missing_query') {
      send(envelope('finished', { outcome: 'answered', answer: '缺参数已拒绝' }));
    }
  } else if (msg.type === 'cancel') {
    send(envelope('finished', { outcome: 'cancelled' }));
    process.exit(0);
  } else if (msg.type === 'shutdown') {
    process.exit(0);
  }
});

rl.on('close', () => {
  process.exit(0);
});
