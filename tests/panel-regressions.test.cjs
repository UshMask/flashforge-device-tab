const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const source = readFileSync(path.join(__dirname, '..', 'flashforge_device_tab.py'), 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const localization = source.split("UI_TRANSLATION_SCRIPT = r'''")[1].split("'''")[0];

function panelContext(values) {
  const document = {documentElement:{lang:'en'},
    getElementById:() => ({textContent:'{"messages":{},"states":{}}'})};
  const t = vm.runInNewContext(localization + '\nt', {document});
  return vm.createContext({t, uiLocale:'en-US', ...values});
}

test('manual panel hides unavailable model controls and locks supported controls while busy', () => {
  const nodes = Object.fromEntries(['calibrationStatus','calibrationProgress','calibrationMessage','manualPanel','manualActivity','manualState','manualHomeNotice','manualBody','jogStep','manualTimer','zPart','xyPosition','zPosition'].map(id=>[id,{}]));
  const motion = {dataset:{group:'motion'}}, calibration = {dataset:{group:'calibration'}};
  nodes.manualTimer.dataset = {};
  nodes.jogStep = {querySelector:()=>({value:'1'})};
  const level = {dataset:{manual:'level'}, hasAttribute:()=>false};
  nodes.manualBody.querySelectorAll = selector => selector === 'button' ? [level] : [motion,calibration];
  const context = panelContext({el:id=>nodes[id]});
  vm.runInContext(section('let manual =', 'async function sendManual'), context);
  vm.runInContext("manual = {groups:['calibration'], allowed:['level']}; renderManual()", context);
  assert.equal(motion.hidden, true);
  assert.equal(calibration.hidden, false);
  assert.equal(level.disabled, false);
  vm.runInContext("Object.assign(manual, {description:'Ready', timerValue:'30', filamentNeedsHeat:true}); renderManual()", context);
  assert.equal(nodes.manualHomeNotice.textContent, '');
  assert.equal(nodes.manualTimer.value, '30');
  vm.runInContext("Object.assign(manual, {description:'Locked · printer must be idle', allowed:[], filamentNeedsHeat:false}); renderManual()", context);
  assert.equal(nodes.manualHomeNotice.textContent, 'Manual controls require an idle printer');
  assert.equal(level.disabled, true);
  vm.runInContext('manual = null; renderManual()', context);
  assert.equal(nodes.manualPanel.hidden, true);
});

test('manual feedback distinguishes Home, connection failures and travel limits', async () => {
  const nodes = Object.fromEntries(['calibrationStatus','calibrationProgress','calibrationMessage','manualPanel','manualActivity','manualState','manualHomeNotice','manualResult','manualBody','manualTimer','zPart','xyPosition','zPosition'].map(id=>[id,{}]));
  nodes.manualTimer.dataset = {};
  nodes.jogStep = {querySelector:()=>({value:'1'})};
  nodes.manualBody.querySelectorAll = () => [];
  let failure = Error('Printer denied manual control.');
  let completeHome, expire;
  const context = panelContext({el:id=>nodes[id], endpoint:path=>path, updateStatus:async()=>{},
    setTimeout:fn=>{ expire=fn; }, clearTimeout(){},
    postLocal:async(route, url, {action}) => {
      if (action === 'manual/home') return new Promise(resolve => { completeHome = resolve; });
      throw failure;
    },
  });
  vm.runInContext(section('let manual =', 'manualBody.onclick'), context);
  vm.runInContext("manual = {groups:['motion'], allowed:['home','x+1']}; renderManual()", context);
  assert.equal(nodes.manualHomeNotice.textContent, 'Ready to move');
  await context.sendManual('x+1');
  assert.equal(nodes.manualHomeNotice.textContent, 'Ready to move');
  assert.equal(nodes.manualActivity.textContent, 'Printer denied manual control.');
  failure = Object.assign(Error('Homing required.'), {code:'home_required'});
  await context.sendManual('x+1');
  assert.equal(nodes.manualActivity.textContent, 'Home required');
  assert.match(nodes.manualHomeNotice.textContent, /Press Home/);
  const homing = context.sendManual('home');
  assert.equal(nodes.manualHomeNotice.textContent, 'Ready to move');
  completeHome({state:'Move sent to the printer.'});
  assert.equal(nodes.manualActivity.textContent, 'Sending…');
  await homing;
  assert.equal(nodes.manualActivity.textContent, 'Home · Accepted');
  expire();
  assert.equal(nodes.manualActivity.textContent, '');
  failure = Object.assign(Error('Timeout'), {code:'no_response'});
  await context.sendManual('x+1');
  assert.equal(nodes.manualHomeNotice.textContent, 'Ready to move');
  assert.equal(nodes.manualActivity.textContent, 'Timeout');
  failure = Object.assign(Error('X move exceeds the axis travel limits (-110 to 110 mm).'), {code:'travel_limit'});
  await context.sendManual('x+1');
  assert.equal(nodes.manualActivity.textContent, failure.message);
  assert.equal(nodes.manualHomeNotice.textContent, 'Ready to move');
  failure = Object.assign(Error('Printer must be idle.'), {code:'axes_busy'});
  await context.sendManual('x+1');
  assert.equal(nodes.manualActivity.textContent, 'Printer must be idle.');
});
test('motor release keeps the jog buttons disabled while Home is pending', () => {
  const nodes = Object.fromEntries(['calibrationStatus','calibrationProgress','calibrationMessage','manualPanel','manualActivity','manualState','manualHomeNotice','manualBody','manualTimer','filamentCaption','zPart','xyPosition','zPosition'].map(id=>[id,{}]));
  nodes.manualTimer.dataset = {};
  nodes.jogStep = {querySelector:()=>({value:'1'})};
  const release = {dataset:{manual:'release',group:'release'}, hasAttribute:()=>false,
    setAttribute(name, value) { this[name] = value; }};
  const home = {dataset:{manual:'home'}, hasAttribute:()=>false};
  const z = {dataset:{zStep:'1'}, hasAttribute:()=>false};
  nodes.manualBody.querySelectorAll = selector => selector === 'button' ? [home, release, z] : [release];
  const context = panelContext({el:id=>nodes[id]});
  vm.runInContext(section('let manual =', 'async function sendManual'), context);
  vm.runInContext("manual = {groups:['motion'], allowed:['home','z-1'], zPart:'bed'}; renderManual()", context);
  assert.equal(release.hidden, true);
  assert.equal(z.disabled, false);
  vm.runInContext("Object.assign(manual,{groups:['motion','release'], allowed:['home'], released:true}); renderManual()", context);
  assert.equal(release.hidden, false);
  assert.equal(release.dataset.released, 'true');
  assert.match(release['aria-label'], /Motors released/);
  assert.equal(release.disabled, true);
  assert.equal(z.disabled, true);
  assert.equal(home.disabled, false);
  assert.match(nodes.manualHomeNotice.textContent, /Press Home/);
  vm.runInContext("Object.assign(manual,{allowed:[], releaseHoming:true}); renderManual()", context);
  assert.equal(home.disabled, true);
  assert.match(nodes.manualHomeNotice.textContent, /Waiting for Home/);
  vm.runInContext("Object.assign(manual,{allowed:['home','release','z-1'], released:false, releaseHoming:false}); renderManual()", context);
  assert.equal(release.dataset.released, 'false');
  assert.equal(release['aria-label'], 'Release motors');
  assert.equal(z.disabled, false);
  assert.equal(nodes.manualHomeNotice.textContent, 'Ready to move');
});

test('AD5X jog displays the warning when the position could not be checked', async () => {
  const nodes = Object.fromEntries(['calibrationStatus','calibrationProgress','calibrationMessage','manualPanel','manualActivity','manualState','manualHomeNotice','manualResult','manualBody','manualTimer','zPart','xyPosition','zPosition'].map(id=>[id,{}]));
  nodes.manualTimer.dataset = {};
  nodes.jogStep = {querySelector:()=>({value:'1'})};
  nodes.manualBody.querySelectorAll = () => [];
  const context = panelContext({el:id=>nodes[id], endpoint:path=>path, updateStatus:async()=>{},
    setTimeout(){}, clearTimeout(){},
    postLocal:async()=>({state:'Printer accepted the command. Axis limits could not be checked.'}),
  });
  vm.runInContext(section('let manual =', 'manualBody.onclick'), context);
  vm.runInContext("manual = {groups:['motion'], allowed:['x+1'], tcpMotion:false}", context);
  await context.sendManual('x+1');
  assert.match(nodes.manualActivity.textContent, /Axis limits could not be checked/);
});

function section(from, until) {
  const start = script.indexOf(from);
  const end = script.indexOf(until, start);
  assert.ok(start >= 0 && end > start);
  return script.slice(start, end);
}

test('temperature trend compares recent readings with the selected nozzle five minutes earlier', () => {
  const nodes = {chartTrend:{}, chartTrendCompact:{}};
  const now = Date.now();
  const context = panelContext({el:id=>nodes[id], chartKey:'t3', connectionFailure:'', temperatures:[
    {t:now-301000, nozzle:90, t3:196, bed:63},
    {t:now-10000, nozzle:95, t3:211, bed:60},
    {t:now, nozzle:97, t3:214, bed:59},
  ]});
  vm.runInContext(section('function setTemperatureTrend', 'function drawChart'), context);
  context.updateTemperatureTrend(now);
  assert.equal(nodes.chartTrend.textContent, '5 min · T3 +18° · Bed −4°');
  assert.equal(nodes.chartTrendCompact.textContent, '5m · T3 +18° · B −4°');
  context.updateTemperatureTrend(now + 31000);
  assert.equal(nodes.chartTrend.textContent, 'Trend unavailable');
  context.temperatures.shift();
  context.updateTemperatureTrend(now);
  assert.equal(nodes.chartTrend.textContent, 'Collecting readings…');
});

test('calibration shows the requested operation and only animates a confirmed run', async () => {
  const ids = ['calibrationStatus','calibrationProgress','calibrationMessage','manualPanel','manualActivity',
    'manualState','manualHomeNotice','manualResult','manualBody','manualTimer','zPart','xyPosition','zPosition',
    'pausePrint','resumePrint','cancelPrint'];
  const nodes = Object.fromEntries(ids.map(id=>[id,{}]));
  nodes.manualTimer.dataset = {};
  nodes.jogStep = {querySelector:()=>({value:'1'})};
  nodes.manualBody.querySelectorAll = () => [];
  nodes.cancelPrint.classList = {contains:()=>false};
  let acknowledge;
  const context = panelContext({el:id=>nodes[id], endpoint:path=>path, updateStatus:async()=>{},
    clearTimeout(){}, postLocal:()=>new Promise(resolve=>{acknowledge=resolve;})});
  vm.runInContext(section('let manual =', 'manualBody.onclick'), context);
  vm.runInContext(section('function updateActions', 'async function runAction'), context);
  const manual = {groups:['calibration'],allowed:['level','shaper'],description:'Ready'};
  context.updateActions({manual});
  assert.equal(nodes.calibrationStatus.hidden, true);
  const request = context.sendManual('shaper');
  assert.match(nodes.calibrationMessage.textContent, /Input shaper · Sending request/);
  acknowledge({state:'Printer accepted the command.'});
  await request;
  assert.match(nodes.calibrationMessage.textContent, /Input shaper · Waiting/);
  assert.equal(nodes.calibrationProgress.hidden, true);
  context.updateActions({manual:{...manual, allowed:[]}, calibration:{key:'shaper',phase:'running'}});
  assert.equal(nodes.calibrationMessage.textContent, 'Input shaper · In progress');
  assert.equal(nodes.calibrationProgress.hidden, false);
  context.updateActions({}, true);
  assert.equal(nodes.calibrationMessage.textContent, 'Input shaper · Printer status unavailable.');
  assert.equal(nodes.calibrationProgress.hidden, true);
  context.updateActions({manual, calibration:{key:'shaper',phase:'ready'}});
  assert.match(nodes.calibrationMessage.textContent, /Input shaper · Finished/);
});

test('XY sectors use the selected step while Home and Z keep their own commands', () => {
  let step = '1';
  const nodes = {manualBody:{}, jogStep:{querySelector:()=>({value:step})}};
  const context = panelContext({el:id=>nodes[id]});
  vm.runInContext(section('let manual =', 'function renderManual'), context);
  const key = dataset => context.manualKey({dataset, hasAttribute:()=>false});
  assert.equal(key({jog:'y-'}), 'y-1');
  step = '10';
  assert.equal(key({jog:'y-'}), 'y-10');
  assert.equal(key({manual:'home'}), 'home');
  for (const [part, expected] of [['bed',['z-10','z-1','z+1','z+10']], ['nozzle',['z+10','z+1','z-1','z-10']]]) {
    vm.runInContext('manual = {zPart:' + JSON.stringify(part) + '}', context);
    step = '10';
    assert.deepEqual(['1','-1'].map(zStep=>key({zStep})), [expected[0],expected[3]]);
    step = '1';
    assert.deepEqual(['1','-1'].map(zStep=>key({zStep})), expected.slice(1,3));
  }
});

test('XY shortcuts require pad focus and leave Enter to the focused button', () => {
  const manualBody = {};
  const commands = [];
  const context = panelContext({manualBody, sendManual: key => commands.push(key)});
  vm.runInContext(section('manualBody.onkeydown =', 'function updateActions'), context);
  const keydown = (key, padFocused, shiftKey = false) => {
    let prevented = false;
    manualBody.onkeydown({
      key, shiftKey, target: {matches: selector => padFocused && selector === '.jog-pad'},
      preventDefault: () => { prevented = true; },
    });
    return prevented;
  };
  assert.equal(keydown('Enter', false), false);
  assert.equal(keydown('Enter', true), false);
  assert.equal(keydown('ArrowRight', false), false);
  assert.equal(keydown('ArrowRight', true), true);
  assert.equal(keydown('ArrowLeft', true, true), true);
  assert.deepEqual(commands, ['x+1', 'x-10']);
});

test('an unavailable status preserves manual groups and disables commands until recovery', () => {
  const nodes = {pausePrint:{}, resumePrint:{}, cancelPrint:{classList:{contains:()=>false}}};
  const context = panelContext({el:id=>nodes[id], manual:null, renderManual() {}});
  vm.runInContext(section('function updateActions', 'async function runAction'), context);
  const available = {groups:['motion'], allowed:['home'], description:'Ready'};
  context.updateActions({manual:available});
  context.updateActions({}, true);
  assert.equal(context.manual.groups, available.groups);
  assert.equal(context.manual.allowed.length, 0);
  assert.match(context.manual.description, /^Unavailable/);
  context.updateActions({manual:available});
  assert.equal(context.manual, available);
  context.updateActions({});
  assert.equal(context.manual, null);
});

test('bulk settings preserve credentials, names and legacy rows', () => {
  const context = panelContext({});
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
    const context = panelContext({
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

test('stored print confirms the selected printer, file and leveling before sending', async () => {
  const ids = ['dismissPrint','printSelection','printError','levelOption','printLevel','printSlots',
    'printDialog','printForm','confirmPrint','filePrintStatus','printSource','sourceFile','useSavedSource','sourceStatus','sourceHelp'];
  const nodes = Object.fromEntries(ids.map(id => [id,{}]));
  nodes.printSlots.replaceChildren = () => {};
  nodes.printSlots.querySelectorAll = () => [];
  let opened = false, request;
  nodes.printDialog.showModal = () => { opened = true; };
  nodes.printDialog.close = () => { opened = false; };
  const context = panelContext({el:id=>nodes[id], lastSlots:[], endpoint:()=>new URL('http://localhost/action'),
    postLocal:async (route,url,payload)=>{ request = [route,url.searchParams.get('printer'),payload.action]; },
    updateStatus:async()=>{}});
  vm.runInContext(section('let printOptions =','let selectedCatalogFile'),context);
  vm.runInContext("printOptions={family:'5m',http:true,printer:'192.168.3.37'};openPrint({id:'abc',name:'Test.3mf'})",context);
  assert.equal(opened,true);
  assert.equal(request,undefined);
  assert.match(nodes.printSelection.textContent,/Test.3mf.*192.168.3.37/);
  nodes.printLevel.checked=true;
  await nodes.printForm.onsubmit({preventDefault(){}});
  assert.deepEqual(request,['action','192.168.3.37','print/abc/1/']);
  assert.equal(opened,false);
  assert.equal(nodes.filePrintStatus.textContent,'Print requested · Test.3mf');
  vm.runInContext("printOptions={family:'creator',http:true,printer:'192.168.3.37'};openPrint({id:'abc',name:'Test.3mf',savedSource:true})",context);
  assert.equal(nodes.printSource.hidden, false);
  assert.equal(nodes.sourceHelp.hidden, false);
  assert.equal(nodes.useSavedSource.hidden, false);
  assert.equal(nodes.confirmPrint.disabled, true);
});

test('catalog enables one Print action for the selected card and clears it with selection', () => {
  const nodes = {printSelected:{},fileSelection:{},refresh:{},files:{}};
  const cards = ['a','b'].map(fileId => ({
    dataset:{fileId}, setAttribute(name,value) { this[name] = value; },
  }));
  nodes.files.querySelectorAll = () => cards;
  let opened;
  const context = panelContext({el:id=>nodes[id], printOptions:{family:'5m'},
    openPrint:file=>{ opened=file.id; }});
  vm.runInContext(section('let selectedCatalogFile','function fileCard'),context);
  context.selectCatalogFile(null);
  assert.equal(nodes.printSelected.disabled,true);
  context.selectCatalogFile({id:'a',name:'First file'});
  context.selectCatalogFile({id:'b',name:'Second file'});
  assert.deepEqual(cards.map(card=>card['aria-pressed']),['false','true']);
  assert.match(nodes.fileSelection.textContent,/Second file/);
  nodes.printSelected.onclick();
  assert.equal(opened,'b');
  context.selectCatalogFile(null);
  assert.equal(nodes.printSelected.disabled,true);
  assert.equal(nodes.fileSelection.textContent,'Select a file to print.');
});

test('catalog page field jumps directly and clamps out-of-range page numbers', () => {
  const nodes = {pageJump:{}, pageNumber:{select(){}}, pageTotal:{textContent:'24'}};
  const context = panelContext({el:id=>nodes[id],renderFiles(){}});
  vm.runInContext('let page = 0;'+section("el('pageJump').onsubmit","el('previous').onclick"),context);
  for (const [input, expected] of [['17',16],['99',23],['0',0],['bad',0]]) {
    nodes.pageNumber.value = input;
    nodes.pageJump.onsubmit({preventDefault(){}});
    assert.equal(vm.runInContext('page',context),expected);
  }
});
