const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const source = readFileSync(path.join(__dirname, '..', 'flashforge_device_tab.py'), 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];

test('manual panel hides unavailable model controls and locks supported controls while busy', () => {
  const nodes = Object.fromEntries(['manualPanel','manualState','manualBody','jogStep','manualTimer'].map(id=>[id,{}]));
  const motion = {dataset:{group:'motion'}}, calibration = {dataset:{group:'calibration'}};
  nodes.manualBadge = {dataset:{}};
  const level = {dataset:{manual:'level'}, hasAttribute:()=>false};
  nodes.manualBody.querySelectorAll = selector => selector === 'button' ? [level] : [motion,calibration];
  const context = vm.createContext({el:id=>nodes[id]});
  vm.runInContext(section('let manual =', 'async function sendManual'), context);
  vm.runInContext("manual = {groups:['calibration'], allowed:['level']}; renderManual()", context);
  assert.equal(motion.hidden, true);
  assert.equal(calibration.hidden, false);
  assert.equal(level.disabled, false);
  vm.runInContext('manual.allowed = []; renderManual()', context);
  assert.equal(level.disabled, true);
  vm.runInContext('manual = null; renderManual()', context);
  assert.equal(nodes.manualPanel.hidden, true);
});
function section(from, until) {
  const start = script.indexOf(from);
  const end = script.indexOf(until, start);
  assert.ok(start >= 0 && end > start);
  return script.slice(start, end);
}

test('XY sectors use the selected step while Home and Z keep their own commands', () => {
  let step = '1';
  const nodes = {manualBody:{}, jogStep:{querySelector:()=>({value:step})}};
  const context = vm.createContext({el:id=>nodes[id]});
  vm.runInContext(section('let manual =', 'function renderManual'), context);
  const key = dataset => context.manualKey({dataset, hasAttribute:()=>false});
  assert.equal(key({jog:'y-'}), 'y-1');
  step = '10';
  assert.equal(key({jog:'y-'}), 'y-10');
  assert.equal(key({manual:'home'}), 'home');
  assert.equal(key({manual:'z+1'}), 'z+1');
});

test('bulk settings preserve credentials, names and legacy rows', () => {
  const context = vm.createContext({});
  vm.runInContext(section('function parsePrinters', 'const fleetToggle'), context);
  const entries = [
    {host:'127.0.0.1', code:'ab|"cd\\', name:'Left | right', serial:'SN1'},
    {host:'127.0.0.2', code:'legacy\\code', name:'Desk', serial:''},
  ];
  const text = context.formatPrinters(entries);
  assert.ok(text.includes('127.0.0.2 | legacy\\code | Desk'));
  assert.deepEqual(JSON.parse(JSON.stringify(context.parsePrinters(text))), entries);
  assert.throws(() => context.parsePrinters('127.0.0.1 | a | b | c | d'));
});

for (const reject of [false, true]) {
  test('late diagnostics result is ignored after dismissal, failure=' + reject, async () => {
    const dialog = {open:false, showModal() { this.open = true; }};
    const nodes = {infoDialog:dialog, infoTitle:{}, infoBody:{}, copyReport:{}, closeInfo:{}};
    let finish;
    const context = vm.createContext({
      el:id=>nodes[id], endpoint:s=>s,
      postLocal:()=>new Promise((resolve, fail) => {
        finish = () => reject ? fail(Error('Printer offline')) : resolve({transport:'TCP'});
      }),
    });
    vm.runInContext(section('let infoRevision', "el('closeInfo').onclick") +
      section('async function diagnose', "el('diagnose').onclick"), context);
    const pending = context.diagnose();
    dialog.open = false;
    finish();
    await pending;
    assert.equal(dialog.open, false);
    const another = context.diagnose();
    context.showInfo('Newer report', 'Keep this');
    finish();
    await another;
    assert.equal(nodes.infoBody.textContent, 'Keep this');
  });
}
