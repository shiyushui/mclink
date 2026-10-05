// 独立验证 index.html 里端口池那段纯逻辑（不依赖浏览器 DOM）
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('function allowedRanges()');
const end = html.indexOf('function setRemotePort(');
if (start < 0 || end < 0) { console.error('未能定位端口池函数'); process.exit(2); }
const src = html.slice(start, end);

let S = { state: null, editing: null, formProto: 'tcp' };
const $ = () => null;
const sandbox = { S: null, $, Object, parseInt, isNaN, String };
const fn = new Function('S', '$', src + `
  return { allowedRanges, fmtRanges, portInPool, usedPortKeys, freePortInPool };`);

let pass = 0, fail = 0;
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { pass++; console.log(`  [PASS] ${name}  -> ${g}`); }
  else { fail++; console.log(`  [FAIL] ${name}  -> got ${g}, want ${w}`); }
}

// 场景 1：服务端下发端口池
S = { state: { server: { allowed_ports: [[25000, 26000], [25565, 25565]] },
               mappings: [] }, editing: null, formProto: 'tcp' };
let a = fn(S, $);
eq('解析范围', a.allowedRanges(), [[25000, 26000], [25565, 25565]]);
eq('格式化范围', a.fmtRanges(a.allowedRanges()), '25000-26000、25565');
eq('池内(池)', a.portInPool(25e3), true);
eq('池内(单端口)', a.portInPool(25565), true);
eq('池外', a.portInPool(1234), false);
eq('空池默认放行', fn({ state: { server: {} }, editing: null }).portInPool(1234), true);
eq('首个空闲端口', a.freePortInPool('tcp'), 25000);

// 场景 2：已有映射占用（tcp:25000/25001，udp:25565）
S = { state: { server: { allowed_ports: [[25000, 25003]] },
               mappings: [
                 { id: 'a', proto: 'tcp', remote_port: 25000 },
                 { id: 'b', proto: 'tcp', remote_port: 25001 },
                 { id: 'c', proto: 'udp', remote_port: 25003 }
               ] }, editing: null, formProto: 'tcp' };
a = fn(S, $);
eq('TCP 跳过已占用 -> 25002', a.freePortInPool('tcp'), 25002);
eq('UDP 独立命名空间 -> 25000', a.freePortInPool('udp'), 25000);
eq('TCP 池满后返回 null', (() => {
  const s2 = { state: { server: { allowed_ports: [[25000, 25001]] },
                        mappings: [{ id: 'x', proto: 'tcp', remote_port: 25000 },
                                   { id: 'y', proto: 'tcp', remote_port: 25001 }] },
               editing: null, formProto: 'tcp' };
  return fn(s2, $).freePortInPool('tcp');
})(), null);

// 场景 3：编辑自己时不应把自己算作占用
S = { state: { server: { allowed_ports: [[25000, 25002]] },
               mappings: [{ id: 'me', proto: 'tcp', remote_port: 25000 }] },
      editing: { id: 'me' }, formProto: 'tcp' };
a = fn(S, $);
eq('编辑自己时端口不被视为占用', a.freePortInPool('tcp'), 25000);

// 场景 4：脏数据不应导致崩溃
S = { state: { server: { allowed_ports: [[25000], 'x', [26000, 25000], [1, 65535], null] },
               mappings: [] }, editing: null, formProto: 'tcp' };
a = fn(S, $);
eq('过滤非法范围', a.allowedRanges(), [[1, 65535]]);

console.log(`\n端口池逻辑: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
