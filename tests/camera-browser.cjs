// Run with Playwright on NODE_PATH and an installed Chrome: node tests/camera-browser.cjs
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const { once } = require('node:events');
const { createHash } = require('node:crypto');
const { readFile } = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const fixture = spawn('python', ['-B', path.join(__dirname, 'camera_simulator.py')], {
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  let browser;
  try {
    const lines = createInterface({input: fixture.stdout});
    const [line] = await Promise.race([once(lines, 'line'), once(fixture, 'exit').then(() => {
      throw Error('Camera simulator exited before startup');
    })]);
    const urls = JSON.parse(line);
    browser = await chromium.launch({channel:'chrome', headless:true});
    const context = await browser.newContext({colorScheme:'dark',viewport:{width:1024,height:800}});
    const stats = async () => (await fetch(urls.stats)).json();
    const waitForClose = async () => {
      for (let i = 0; i < 60; i++) {
        if ((await stats()).events.at(-1) === 'close') return;
        await new Promise(resolve => setTimeout(resolve,100));
      }
      throw Error('Camera did not close');
    };
    const page = await context.newPage();
    let monitorPreviewCalls = 0;
    await page.route(url => url.pathname.includes('/preview/') && !url.searchParams.has('printer'), route => {
      if (++monitorPreviewCalls === 1) return route.fulfill({status:404});
      return route.continue();
    });
    let previewCatalogCalls = 0;
    let previewImageCalls = 0;
    await page.route('**/files?printer=192.0.2.52', route => {
      if (++previewCatalogCalls === 1) return route.abort();
      if (previewCatalogCalls === 2) return route.fulfill({status:502});
      return route.fulfill({json:{files:[{id:'preview-fixture',name:'Cable guide.3mf'}]}});
    });
    await page.route('**/preview/preview-fixture?printer=192.0.2.52', route => {
      if (++previewImageCalls === 1) return route.fulfill({status:503});
      if (previewImageCalls === 2) return route.fulfill({status:404});
      return route.fulfill({path:path.join(__dirname,'camera-frame.jpg'),contentType:'image/jpeg'});
    });
    await page.route('**/telemetry', async route => {
      const response = await route.fetch();
      const telemetry = await response.json();
      if (telemetry.printers[0]?.printing) {
        telemetry.printers.push(
          {host:'192.0.2.51', name:'AD5X', state:'PAUSED', status:'Paused', percent:38, layer:19, layerTotal:50,
           printing:true, observedAt:Date.now()/1000, file:'Badge holder.3mf', cameraUrl:urls.direct},
          {host:'192.0.2.52', name:'Adventurer 5M', state:'PRINTING', status:'Printing', printing:true,
           observedAt:Date.now()/1000, file:'Cable guide.3mf', percent:44, remaining:1058},
          {host:'192.0.2.53', name:'Adventurer 4', state:'READY', status:'Ready', printing:false,
           observedAt:Date.now()/1000, file:'Earlier print.gcode', percent:100, cameraUrl:urls.direct + '?idle=1'},
          {host:'192.0.2.54', name:'Adventurer 3', error:'Timeout', printing:false});
      }
      await route.fulfill({response, json:telemetry});
    });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(urls.panel);
    const jobPanel = page.locator('#monitor > #jobPanel');
    const monitorCamera = jobPanel.locator('#camera');
    await jobPanel.getByRole('heading', {name:'Print job', exact:true}).waitFor();
    assert.equal(await page.locator('main > :first-child').evaluate(node => node.tagName), 'NAV');
    assert.equal(await page.locator('#deviceName').count(), 0);
    assert.ok(await jobPanel.locator('#status').isVisible());
    await page.locator('nav #devicePicker').waitFor({state:'visible'});
    assert.equal(await page.locator('nav #devicePicker option:checked').textContent(), 'Creator 5 Pro');
    await jobPanel.locator('#jobImage').waitFor({state:'visible'});
    assert.ok(monitorPreviewCalls >= 2);
    assert.ok(await jobPanel.locator('progress').isVisible());
    assert.ok(await jobPanel.locator('#pausePrint').isVisible());
    assert.ok(await jobPanel.locator('#cancelPrint').isVisible());
    assert.ok(await jobPanel.getByRole('button', {name:'Print again'}).isHidden());
    assert.ok(await monitorCamera.isHidden());
    assert.equal(await jobPanel.locator('#cameraUrl').count(), 0);
    const cameraSettings = page.getByRole('dialog', {name:'Camera', exact:true});
    await jobPanel.getByRole('button', {name:'Camera', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#cameraUrl').disabled);
    await cameraSettings.getByRole('button', {name:'Connect', exact:true}).click();
    await monitorCamera.locator('#cameraSnapshot').waitFor({state:'attached'});
    await page.waitForFunction(() => !document.querySelector('#camera').hidden);
    await cameraSettings.getByRole('button', {name:'Close', exact:true}).click();
    assert.ok(await jobPanel.evaluate(panel => {
      const outer = panel.parentElement.getBoundingClientRect();
      const card = panel.getBoundingClientRect();
      const job = panel.querySelector('.jobs-body').getBoundingClientRect();
      const camera = panel.querySelector('#camera').getBoundingClientRect();
      return card.left === outer.left && card.right === outer.right &&
        job.right === camera.left && job.top === camera.top;
    }));
    await monitorCamera.locator('#cameraSnapshot').waitFor({state:'visible'});
    assert.ok(await monitorCamera.locator('#cameraImage').evaluate(img => img.naturalWidth > 0));
    const viewer = page.locator('#jobsCameraDialog');
    const expandMonitor = monitorCamera.getByRole('button', {name:'Enlarge camera'});
    const liveImage = monitorCamera.locator('#cameraImage');
    const monitorSessions = (await stats()).sessions;
    const beforeZoom = await liveImage.evaluate(image => image.src);
    await expandMonitor.click();
    await viewer.locator('#cameraImage').waitFor({state:'visible'});
    assert.equal(await viewer.getByRole('heading').textContent(), 'Creator 5 Pro');
    await page.waitForFunction(url => document.querySelector('#jobsCameraFrame #cameraImage')?.src !== url, beforeZoom);
    await page.keyboard.press('Escape');
    await liveImage.waitFor({state:'visible'});
    assert.ok(await expandMonitor.evaluate(button => button === document.activeElement));
    assert.equal((await stats()).sessions, monitorSessions);
    const monitorHeight = (await jobPanel.boundingBox()).height;
    const monitorCameraHeight = (await monitorCamera.boundingBox()).height;
    const monitorLayer = await jobPanel.locator('#layers').textContent();
    const capture = process.env.FF_JOBS_CAPTURE || process.env.FF_GRID_CAPTURE;
    if (capture) await page.locator('main').screenshot({path:capture + '-monitor.png'});
    await page.setViewportSize({width:360,height:800});
    assert.ok(await jobPanel.evaluate(panel => {
      const camera = panel.querySelector('#camera').getBoundingClientRect();
      const job = panel.querySelector('.jobs-body').getBoundingClientRect();
      const preview = panel.querySelector('.jobs-media').getBoundingClientRect();
      return camera.top === job.bottom && camera.top === preview.top && camera.left === preview.right &&
        panel.getBoundingClientRect().bottom <= document.querySelector('#readingsPanel').getBoundingClientRect().top;
    }));
    if (capture) await page.locator('main').screenshot({path:capture + '-monitor-mobile.png'});
    await page.setViewportSize({width:1024,height:800});
    await page.locator('#openJob').click();
    await jobPanel.waitFor({state:'hidden'});
    assert.ok(await page.locator('nav #devicePicker').isHidden());
    const jobs = page.locator('#jobsList');
    const cards = jobs.locator(':scope > li');
    const creator = cards.first();
    const ad5x = cards.filter({hasText:'Badge holder.3mf'});
    const withoutCamera = cards.filter({hasText:'Cable guide.3mf'});
    const idle = cards.filter({hasText:'Adventurer 4'});
    const disconnected = cards.filter({hasText:'Adventurer 3'});
    assert.equal(await page.locator('#fleetToggle').getAttribute('aria-current'), 'page');
    assert.deepEqual(await jobPanel.locator('#job > *').evaluateAll(elements => elements.map(node => node.className)),
      await creator.locator(':scope > :not(.jobs-camera)').evaluateAll(elements => elements.map(node => node.className)));
    assert.equal(await creator.locator('[data-layer]').textContent(), monitorLayer);
    await creator.locator('[data-stream]').waitFor({state:'visible'});
    assert.equal((await creator.locator('.jobs-camera').boundingBox()).height, monitorCameraHeight);
    assert.equal((await creator.boundingBox()).height, monitorHeight);
    assert.ok(await creator.getByRole('heading').evaluate(heading => heading === document.activeElement));
    assert.equal(await idle.locator('.jobs-file').textContent(), 'No active print job');
    assert.ok(await idle.locator('.jobs-fallback').isVisible());
    assert.ok(await idle.locator('[data-preview]').isHidden());
    assert.ok(await idle.locator('progress').isHidden());
    assert.ok(await idle.getByRole('link', {name:'Monitor', exact:true}).isVisible());
    assert.ok((await idle.locator('[data-monitor]').getAttribute('href')).endsWith('printer=192.0.2.53#monitor'));
    assert.ok((await idle.locator('[data-files]').getAttribute('href')).endsWith('printer=192.0.2.53#files-section'));
    assert.equal(await disconnected.locator('.jobs-file').textContent(), 'Job status unavailable');
    await creator.locator('[data-stream]').waitFor({state:'visible'});
    await creator.locator('[data-preview]').waitFor({state:'visible'});
    await ad5x.locator('[data-stream]').waitFor({state:'visible'});
    await idle.locator('[data-stream]').waitFor({state:'visible'});
    await withoutCamera.locator('[data-preview]').waitFor({state:'visible',timeout:45000});
    assert.equal(previewCatalogCalls, 5);
    assert.equal(previewImageCalls, 3);
    assert.ok(await withoutCamera.locator('.jobs-camera').isHidden());
    assert.equal(await withoutCamera.getByText('Camera unavailable').count(), 0);
    assert.equal((await stats()).sessions, 1);
    assert.equal((await stats()).direct_sessions, 2);
    assert.ok((await stats()).direct_frames > 0);
    assert.equal(await creator.locator('.jobs-camera [data-camera]').count(), 1);
    assert.deepEqual(await page.locator('.fleet-filters [data-count]').allTextContents(), ['5','2','1','2']);
    assert.ok(await creator.locator('[data-preview]').evaluate(preview => {
      const stream = preview.closest('li').querySelector('[data-stream]');
      const card = preview.closest('li');
      const header = card.querySelector('.jobs-body .jobs-printer-heading');
      const camera = stream.closest('.jobs-camera');
      const frame = camera.querySelector('.jobs-media-frame');
      const caption = camera.querySelector('figcaption').getBoundingClientRect();
      const frameBox = frame.getBoundingClientRect();
      const media = preview.closest('.jobs-media');
      const mediaBox = media.getBoundingClientRect();
      const previewBox = preview.getBoundingClientRect();
      const buttons = media.querySelector('.jobs-model-actions');
      return mediaBox.right <= header.getBoundingClientRect().left
        && header.getBoundingClientRect().right < frameBox.left
        && previewBox.left === mediaBox.left && previewBox.width === mediaBox.width
        && previewBox.top === frameBox.top && previewBox.bottom === buttons.getBoundingClientRect().top
        && caption.left >= frameBox.left && caption.bottom <= frameBox.bottom
        && getComputedStyle(frame).borderTopWidth === '0px'
        && getComputedStyle(preview.closest('.jobs-media-frame')).borderTopWidth === '0px';
    }));
    for (const width of [320, 736, 1024]) {
      await page.setViewportSize({width,height:800});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const positions = await cards.evaluateAll(items => items.map(card => {
        const {left,top,bottom,width} = card.getBoundingClientRect();
        return {left,top,bottom,width};
      }));
      assert.ok(await creator.locator('.jobs-camera').evaluate(camera => {
        const edge = camera.closest('li').getBoundingClientRect().right;
        const controls = [...camera.querySelectorAll('button:not([hidden])')];
        return camera.getBoundingClientRect().right <= edge
          && controls.every(button => button.getBoundingClientRect().right <= edge);
      }));
      assert.equal(positions.length, 5);
      assert.equal(new Set(positions.map(item => item.left)).size, 1);
      assert.equal(new Set(positions.map(item => item.width)).size, 1);
      assert.ok(positions.slice(1).every((item,index) => item.top >= positions[index].bottom));
    }
    if (capture) {
      if (process.env.FF_GRID_REFERENCE) {
        const reference = await context.newPage();
        await reference.setContent(await readFile(process.env.FF_GRID_REFERENCE, 'utf8'));
        await reference.locator('#ff-camera-grid').screenshot({path:capture + '-reference.png'});
        await reference.close();
        await page.bringToFront();
      }
      await creator.locator('[data-stream]').waitFor({state:'visible'});
      await page.locator('main').screenshot({path:capture + '.png'});
      await page.setViewportSize({width:360,height:800});
      await page.emulateMedia({colorScheme:'light'});
      await page.locator('main').screenshot({path:capture + '-mobile.png'});
      await page.setViewportSize({width:1024,height:800});
      await page.emulateMedia({colorScheme:'dark'});
    }
    await creator.getByRole('button', {name:'Enlarge camera'}).click();
    await page.locator('#jobsCameraDialog [data-stream]').waitFor({state:'visible'});
    assert.equal((await stats()).sessions, 1);
    await page.locator('#jobsCameraDialog button').click();
    await creator.locator('[data-stream]').waitFor({state:'visible'});
    await creator.evaluate(card => {
      card.querySelector('[data-snapshot]').addEventListener('click', () => {
        window.frameAtClick = card.querySelector('[data-stream]').src;
        window.frameBytes = fetch(window.frameAtClick).then(r => r.arrayBuffer())
          .then(buffer => Array.from(new Uint8Array(buffer)));
      }, {capture:true, once:true});
    });
    const download = page.waitForEvent('download');
    await creator.getByRole('button', {name:'Save snapshot'}).click();
    const bytes = await readFile(await (await download).path());
    assert.deepEqual(bytes, Buffer.from(await page.evaluate(() => window.frameBytes)));
    await page.waitForFunction(() => document.querySelector('#jobsList li [data-stream]').src !== window.frameAtClick);
    const first = await stats();
    assert.ok(first.hashes.includes(createHash('sha256').update(bytes).digest('hex')));
    assert.deepEqual(first.events, ['open','video']);
    const commands = [];
    await page.route('**/action?printer=*', route => {
      commands.push(route.request().postDataJSON());
      return route.fulfill({json:{state:'Acknowledged'}});
    });
    const activeFile = await creator.locator('.jobs-file').textContent();
    await creator.getByRole('button',{name:'Pause',exact:true}).click();
    await creator.getByText('Printer confirmed · Acknowledged', {exact:true}).waitFor();
    assert.deepEqual(commands, [{action:'pause',file:activeFile}]);
    await creator.getByRole('button',{name:'Cancel',exact:true}).click();
    assert.equal(commands.length, 1);
    await creator.getByRole('button',{name:'Cancel print?',exact:true}).click();
    await creator.getByText('Printer confirmed · Acknowledged', {exact:true}).waitFor();
    assert.deepEqual(commands.at(-1), {action:'cancel',file:activeFile});
    const currentStream = creator.locator('[data-stream]');
    await currentStream.evaluate(stream => { window.trackedStream = stream; });
    const activeFrame = await creator.locator('.jobs-camera').boundingBox();
    const headerPositions = () => creator.evaluate(card => {
      const origin = card.getBoundingClientRect();
      return ['.jobs-printer-heading h3', '.jobs-state', '[data-monitor]'].map(selector => {
        const {left, top} = card.querySelector(selector).getBoundingClientRect();
        return {left:left - origin.left, top:top - origin.top};
      });
    });
    const activeHeader = await headerPositions();
    const preview = creator.locator('[data-preview]');
    const activePreview = await preview.getAttribute('src');
    assert.equal((await fetch(new URL('/simulator/completed', urls.stats))).status, 204);
    await creator.getByText('Last completed', {exact:false}).waitFor();
    assert.deepEqual(await headerPositions(), activeHeader);
    assert.ok(await preview.isVisible());
    assert.equal(await preview.getAttribute('src'), activePreview);
    assert.equal((await fetch(new URL('/simulator/ready', urls.stats))).status, 204);
    const clearBed = creator.getByRole('button',{name:'Bed is clear'});
    await clearBed.waitFor({state:'visible'});
    assert.equal(await creator.locator('.jobs-state').textContent(), 'Waiting for part removal');
    assert.ok(await creator.locator('[data-repeat]').isHidden());
    await clearBed.click();
    await clearBed.waitFor({state:'hidden'});
    await creator.locator('[data-files]').waitFor({state:'visible'});
    assert.deepEqual(await headerPositions(), activeHeader);
    assert.ok(await preview.isVisible());
    assert.ok(await creator.locator('[data-print-action]').isHidden());
    assert.ok(await creator.locator('[data-print-cancel]').isHidden());
    assert.ok(await creator.locator('progress').isHidden());
    assert.ok(await creator.locator('[data-files]').isVisible());
    assert.equal(await creator.getAttribute('data-ready'), 'true');
    assert.ok(await currentStream.isVisible());
    assert.ok(await currentStream.evaluate(stream => stream === window.trackedStream));
    const readyFrame = await creator.locator('.jobs-camera').boundingBox();
    const readyPreview = await preview.boundingBox();
    assert.equal(readyFrame.x, activeFrame.x);
    assert.equal(readyFrame.width, activeFrame.width);
    assert.equal(readyPreview.y, readyFrame.y);
    assert.equal(readyPreview.height, readyFrame.height);
    const readyFrameUrl = await currentStream.getAttribute('src');
    await creator.getByRole('button', {name:'Enlarge camera'}).click();
    await page.locator('#jobsCameraDialog [data-stream]').waitFor({state:'visible'});
    await page.locator('#jobsCameraDialog button').click();
    await currentStream.waitFor({state:'visible'});
    assert.equal((await stats()).sessions, 1);
    await page.waitForFunction(url => window.trackedStream.src !== url, readyFrameUrl);
    if (capture) await page.locator('main').screenshot({path:capture + '-idle.png'});
    assert.equal((await fetch(new URL('/simulator/printing', urls.stats))).status, 204);
    await creator.getByRole('button',{name:'Pause', exact:true}).waitFor();
    assert.ok(await currentStream.evaluate(stream => stream === window.trackedStream));
    assert.equal((await stats()).sessions, 1);
    await page.locator('#historyTab').click();
    await waitForClose();
    await page.locator('#fleetToggle').click();
    await creator.locator('[data-snapshot]').waitFor({state:'visible'});
    assert.equal((await stats()).sessions, 2);
    await page.close();
    await waitForClose();
    const reopened = await context.newPage();
    reopened.on('pageerror', error => errors.push(error.message));
    await reopened.goto(urls.panel + '#printJobs');
    await reopened.locator('#jobsList [data-snapshot]').waitFor({state:'visible'});
    assert.equal(await reopened.locator('#fleetToggle').getAttribute('aria-current'), 'page');
    await reopened.locator('#historyTab').click();
    await waitForClose();
    // Clear the monitor preference, then check that reloading Monitor stays disconnected.
    await reopened.evaluate(() => {
      for (const key of Object.keys(localStorage)) if (key.endsWith('-resume')) localStorage.removeItem(key);
      window.history.replaceState(null, '', '#monitor');
    });
    await reopened.reload();
    await reopened.waitForFunction(() => document.querySelector('#cameraUrl').disabled);
    assert.ok(await reopened.locator('#cameraStart').isEnabled());
    assert.deepEqual((await stats()).events, ['open','video','close','open','video','close','open','video','close']);
    await reopened.locator('#fleetToggle').click();
    await reopened.locator('#jobsList [data-stream]').waitFor({state:'visible'});
    const cameras = reopened.getByRole('button',{name:'Live cameras'});
    await cameras.click();
    assert.equal(await cameras.getAttribute('aria-pressed'), 'false');
    await waitForClose();
    assert.ok(await reopened.locator('#jobsList .jobs-camera').isHidden());
    await reopened.reload();
    assert.equal(await cameras.getAttribute('aria-pressed'), 'false');
    assert.ok(await reopened.locator('#jobsList .jobs-camera').isHidden());
    await cameras.click();
    await reopened.locator('#jobsList [data-stream]').waitFor({state:'visible'});
    const fleetCameraMode = reopened.locator('#displayFleetCameras');
    const openFleetCameraSettings = async () => {
      await reopened.locator('#settingsButton').click();
      await reopened.locator('#settingsFleetTab').click();
      await fleetCameraMode.waitFor({state:'visible'});
    };
    await openFleetCameraSettings();
    assert.equal(await fleetCameraMode.inputValue(), 'all');
    await fleetCameraMode.selectOption('printing');
    await reopened.locator('#settingsSave').click();
    await reopened.locator('#settingsDialog').waitFor({state:'hidden'});
    const liveFleetCamera = reopened.locator('#jobsList [data-stream]');
    await liveFleetCamera.waitFor({state:'visible'});
    const activeModeSession = (await stats()).sessions;
    assert.equal((await fetch(new URL('/simulator/paused', urls.stats))).status, 204);
    await reopened.locator('#jobsList .jobs-state').filter({hasText:/^Paused$/}).waitFor();
    assert.ok(await liveFleetCamera.isVisible());
    assert.equal((await stats()).sessions, activeModeSession);
    assert.equal((await fetch(new URL('/simulator/ready', urls.stats))).status, 204);
    await liveFleetCamera.waitFor({state:'hidden'});
    await waitForClose();
    await reopened.reload();
    await reopened.locator('#jobsList .jobs-state').filter({hasText:/^Ready$/}).waitFor();
    assert.ok(await liveFleetCamera.isHidden());
    await openFleetCameraSettings();
    assert.equal(await fleetCameraMode.inputValue(), 'printing');
    await reopened.locator('#settingsCancel').click();
    await cameras.click();
    await cameras.click();
    assert.ok(await liveFleetCamera.isHidden());
    assert.equal((await stats()).sessions, activeModeSession);
    await openFleetCameraSettings();
    await fleetCameraMode.selectOption('all');
    await reopened.locator('#settingsSave').click();
    await liveFleetCamera.waitFor({state:'visible'});
    assert.equal((await fetch(new URL('/simulator/printing', urls.stats))).status, 204);
    await reopened.locator('#jobsList .jobs-state').filter({hasText:/^Printing$/}).waitFor();
    await reopened.locator('#monitorTab').click();
    await waitForClose();
    const catalog = await (await reopened.request.get(new URL('files', urls.panel).href)).json();
    assert.ok(catalog.files.length);
    for (const width of [320, 736, 1024]) {
      await reopened.setViewportSize({width,height:800});
      assert.ok(await reopened.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    let attentionNeeded = true;
    let forceFleetReady = true;
    const longFile = 'pressure_advance_test_' + 'ДлинноеИмяБезПробелов'.repeat(8) + '.gcode.3mf';
    await reopened.route('**/telemetry', async route => {
      const response = await route.fetch();
      const telemetry = await response.json();
      if (forceFleetReady) Object.assign(telemetry.printers[0], {printing:false, state:'READY', status:'Ready',
        lastCompleted:{file:catalog.files[0].name,ended:Date.now()/1000}});
      if (attentionNeeded) telemetry.printers.push(
        {host:'192.0.2.51', name:'AD5X', state:'ATTENTION', status:'Needs Attention',
         printing:false, observedAt:Date.now()/1000},
        {host:'192.0.2.52', name:'Adventurer 5M', error:'Timeout', printing:false});
      telemetry.printers.push(
        {host:'192.0.2.53', name:'Adventurer 4', state:'READY', status:'Ready', printing:false,
         observedAt:Date.now()/1000, lastCompleted:{file:'Other bracket.3mf',ended:Date.now()/1000}});
      await route.fulfill({response,json:telemetry});
    });
    await reopened.route('**/history', async route => {
      const response = await route.fetch();
      const data = await response.json();
      const now = Math.floor(Date.now() / 1000);
      const current = data.printers[0];
      current.jobs.unshift({file:longFile, started:now - 3050, ended:now, elapsed:3050, result:'complete'});
      current.stats.jobs++;
      data.printers.push({host:'192.168.1.7',name:'Workshop',stats:{jobs:1,seconds:60,successRate:0},
        jobs:[{file:'Previous bracket.3mf',started:now - 4000,elapsed:60,result:'cancelled'}]});
      data.stats.jobs += 2;
      await route.fulfill({response,json:data});
    });
    await reopened.locator('#fleetToggle').click();
    const finished = reopened.locator('#jobsList li').first();
    await finished.getByText('No active print job', {exact:true}).waitFor();
    const finishedPreview = finished.locator('[data-preview]');
    await finishedPreview.waitFor({state:'visible'});
    assert.equal(await finishedPreview.getAttribute('alt'), 'Preview of ' + catalog.files[0].name);
    await reopened.locator('#jobsList li h3').filter({hasText:'AD5X'}).waitFor();
    const fleetSearch = reopened.getByRole('searchbox',{name:'Search printers and jobs'});
    await fleetSearch.fill('other bracket');
    assert.deepEqual(await reopened.locator('#jobsList li h3').allTextContents(), ['Adventurer 4']);
    assert.ok(await reopened.locator('#jobsList .jobs-fallback').isVisible());
    assert.ok((await reopened.getByRole('link',{name:'Print again'}).getAttribute('href'))
      .endsWith('printer=192.0.2.53&reprint=Other+bracket.3mf#monitor'));
    await fleetSearch.fill('unmatched printer');
    await reopened.getByText('No printers match your search and filters.', {exact:true}).waitFor();
    await reopened.getByRole('button',{name:'Clear filters'}).click();
    await reopened.locator('[data-fleet-filter="attention"]').click();
    assert.deepEqual(await reopened.locator('#jobsList li h3').allTextContents(), ['AD5X', 'Adventurer 5M']);
    attentionNeeded = false;
    await reopened.locator('#fleetRefresh').click();
    await reopened.getByText('No printers need attention.', {exact:true}).waitFor();
    await reopened.locator('[data-fleet-filter="idle"]').click();
    assert.equal(await reopened.locator('#jobsList > li').count(), 2);
    await finished.getByRole('link',{name:'Browse files'}).click();
    assert.equal(await reopened.locator('#monitorTab').getAttribute('aria-current'), 'page');
    assert.ok(await reopened.locator('#files-section summary').evaluate(summary => (
      summary.parentElement.open && summary === document.activeElement)));
    await reopened.locator('#fleetToggle').click();
    await finished.getByRole('link',{name:'Print again'}).click();
    await reopened.locator('#printDialog').waitFor({state:'visible'});
    assert.ok((await reopened.locator('#printSelection').textContent()).includes(catalog.files[0].name));
    await reopened.locator('#dismissPrint').click();
    const monitorJob = reopened.locator('#jobPanel');
    const repeatFromMonitor = monitorJob.getByRole('button', {name:'Print again'});
    assert.equal((await fetch(new URL('/simulator/completed', urls.stats))).status, 204);
    await monitorJob.locator('.jobs-last').waitFor();
    forceFleetReady = false;
    assert.equal((await fetch(new URL('/simulator/busy', urls.stats))).status, 204);
    await reopened.locator('#status').filter({hasText:/^Busy$/}).waitFor();
    assert.ok(await repeatFromMonitor.isHidden());
    const compactHeight = (await monitorJob.boundingBox()).height;
    const settingsCamera = reopened.getByRole('dialog', {name:'Camera', exact:true});
    const openCamera = monitorJob.getByRole('button', {name:'Camera', exact:true});
    await openCamera.click();
    await settingsCamera.waitFor({state:'visible'});
    assert.equal((await monitorJob.boundingBox()).height, compactHeight);
    await settingsCamera.getByRole('button', {name:'Close', exact:true}).click();
    if (capture) await monitorJob.screenshot({path:capture + '-monitor-busy.png'});
    await reopened.locator('#fleetToggle').click();
    await reopened.locator('[data-fleet-filter="all"]').click();
    await cameras.click();
    const clearNextBed = finished.getByRole('button', {name:'Bed is clear'});
    await clearNextBed.click();
    await clearNextBed.waitFor({state:'hidden'});
    await finished.locator('.jobs-state').filter({hasText:/^Busy$/}).waitFor();
    assert.ok(await finished.locator('[data-repeat]').isHidden());
    assert.equal((await finished.boundingBox()).height, compactHeight);
    if (capture) await finished.screenshot({path:capture + '-fleet-busy.png'});
    assert.equal((await fetch(new URL('/simulator/ready', urls.stats))).status, 204);
    await finished.getByRole('link', {name:'Print again'}).waitFor({state:'visible'});
    await cameras.click();
    await reopened.locator('#monitorTab').click();
    await monitorJob.locator('#reprintJob:enabled').waitFor();
    await monitorJob.locator('#jobImage').waitFor({state:'visible'});
    assert.equal(await monitorJob.locator('.jobs-last strong').textContent(), catalog.files[0].name);
    assert.ok(await monitorJob.locator('progress').isHidden());
    assert.ok(await monitorJob.locator('#pausePrint').isHidden());
    const completedCamera = monitorJob.locator('#camera');
    await openCamera.click();
    await settingsCamera.getByRole('button', {name:'Connect', exact:true}).click();
    await reopened.waitForFunction(() => !document.querySelector('#camera').hidden);
    await settingsCamera.getByRole('button', {name:'Close', exact:true}).click();
    await completedCamera.locator('#cameraSnapshot').waitFor({state:'visible'});
    assert.equal((await monitorJob.boundingBox()).height, monitorHeight);
    if (capture) await monitorJob.screenshot({path:capture + '-monitor-completed.png'});
    await openCamera.click();
    await settingsCamera.getByRole('button', {name:'Disconnect', exact:true}).click();
    await settingsCamera.getByRole('button', {name:'Close', exact:true}).click();
    const printRequests = [];
    await reopened.route('**/action?printer=*', route => {
      printRequests.push(route.request().postDataJSON());
      return route.abort();
    });
    await repeatFromMonitor.click();
    await reopened.locator('#printDialog').waitFor({state:'visible'});
    assert.ok((await reopened.locator('#printSelection').textContent()).includes(catalog.files[0].name));
    await reopened.locator('#dismissPrint').click();
    assert.deepEqual(printRequests, []);
    const catalogFiles = [{id:'fixture-long-name', name:longFile},
      ...Array.from({length:46}, (_, index) => ({id:'demo-' + index, name:'Printer file ' + (index + 1) + '.3mf'}))];
    await reopened.route('**/files', route => route.fulfill({
      json:{...catalog, files:catalogFiles},
    }));
    await reopened.setViewportSize({width:1420,height:800});
    await reopened.locator('#refresh').click();
    const fileCard = reopened.getByRole('button',{name:longFile, exact:true});
    await fileCard.waitFor({state:'visible'});
    assert.ok(await monitorJob.locator('#jobImage').isHidden());
    await repeatFromMonitor.click();
    await reopened.getByText('The file is unavailable or cannot be identified on this printer.', {exact:true}).waitFor();
    await reopened.locator('#closeInfo').click();
    assert.deepEqual(printRequests, []);
    const pageNumber = reopened.locator('#pageNumber');
    const cardsOnPage = reopened.locator('#files > .file-card');
    assert.equal(await cardsOnPage.count(), 15);
    assert.equal(await reopened.locator('#pageTotal').textContent(), '4');
    await pageNumber.fill('4');
    await pageNumber.press('Enter');
    assert.equal(await cardsOnPage.count(), 2);
    assert.ok(await reopened.locator('#next').isDisabled());
    await pageNumber.fill('2');
    await pageNumber.press('Enter');
    const selected = reopened.getByRole('button',{name:catalogFiles[24].name, exact:true});
    await selected.click();
    await reopened.setViewportSize({width:320,height:800});
    await reopened.waitForFunction(() => {
      const grid = document.querySelector('#files');
      const columns = getComputedStyle(grid).gridTemplateColumns.split(' ').length;
      return grid.childElementCount === columns * 3;
    });
    assert.equal(await selected.getAttribute('aria-pressed'), 'true');
    assert.equal(Number(await pageNumber.inputValue()), Math.floor(24 / await cardsOnPage.count()) + 1);
    await pageNumber.fill('1');
    await pageNumber.press('Enter');
    assert.equal(await fileCard.locator('.file-name').getAttribute('title'), longFile);
    assert.ok(await fileCard.locator('.file-name-label').evaluate(name => {
      const style = getComputedStyle(name);
      return style.webkitLineClamp === '2' && Math.abs(name.clientHeight - 2 * parseFloat(style.lineHeight)) < 1;
    }));
    await fileCard.click();
    assert.equal(await reopened.locator('#fileSelection').textContent(), 'Selected · ' + longFile);
    await reopened.locator('#historyTab').click();
    const historyFile = reopened.locator('#historyRows').getByRole('button',{name:longFile, exact:true});
    await historyFile.waitFor({state:'visible'});
    const printerHistoryFilter = reopened.locator('#historyPrinter');
    const previousJob = reopened.getByRole('button',{name:'Previous bracket.3mf', exact:true});
    assert.ok(await previousJob.isVisible());
    await printerHistoryFilter.selectOption('192.168.1.7');
    assert.ok(await historyFile.isHidden());
    assert.equal(await reopened.locator('#statJobs').textContent(), '1');
    await reopened.reload();
    await previousJob.waitFor({state:'visible'});
    assert.equal(await printerHistoryFilter.inputValue(), '192.168.1.7');
    await reopened.locator('#monitorTab').click();
    await reopened.locator('#monitorHistory').click();
    await historyFile.waitFor({state:'visible'});
    assert.equal(await printerHistoryFilter.inputValue(), '127.0.0.1');
    assert.ok(await previousJob.isHidden());
    await reopened.locator('#historyTab').click();
    assert.equal(await printerHistoryFilter.inputValue(), '');
    await reopened.waitForFunction(filename => {
      const button = [...document.querySelectorAll('#historyRows .history-filename')]
        .find(item => item.textContent === filename);
      if (!button) return false;
      const table = button.closest('.table-wrap');
      const row = button.closest('tr');
      const name = row.cells[0].getBoundingClientRect();
      return table.scrollWidth <= table.clientWidth && name.left <= row.cells[1].getBoundingClientRect().left
        && name.right >= row.cells[3].getBoundingClientRect().right;
    }, longFile);
    await reopened.locator('#historySearch').fill('ДлинноеИмя');
    assert.equal(await reopened.locator('#historyRows tr').count(), 1);
    await historyFile.click();
    assert.equal(await reopened.locator('#infoTitle').textContent(), longFile);
    await reopened.locator('#closeInfo').click();
    const savedPrinters = [];
    await reopened.route('**/settings', route => {
      if (route.request().method() !== 'POST') return route.continue();
      savedPrinters.push(route.request().postDataJSON().printers);
      return route.fulfill({json:{moved:false}});
    });
    await reopened.setViewportSize({width:1420,height:800});
    await reopened.locator('#monitorTab').click();
    const settings = reopened.locator('#settingsDialog');
    const save = reopened.locator('#settingsSave');
    const settingsCapture = process.env.FF_SETTINGS_CAPTURE;
    const openSettingsPage = async id => {
      await settings.locator('#' + id + 'Tab').click();
      assert.ok(await settings.locator('#settingsContent > #' + id).isVisible());
      assert.equal(await settings.locator('#settingsContent > section:not([hidden])').count(), 1);
    };
    await reopened.locator('#settingsButton').click();
    await openSettingsPage('settingsAppearance');
    assert.equal(await settings.locator('#settingsSections button').count(), 6);
    const layout = settings.locator('#settingsLayout');
    assert.ok(await layout.evaluate(root => root.children[0].getBoundingClientRect().right
      <= root.children[1].getBoundingClientRect().left));
    await reopened.locator('#displayTheme').selectOption('light');
    await reopened.locator('#displayDensity').selectOption('compact');
    if (settingsCapture) await settings.screenshot({path:settingsCapture + '-appearance.png'});
    await openSettingsPage('settingsMonitor');
    await reopened.locator('#displayMonitorChart').uncheck();
    await reopened.locator('#displayPrinterDetails').uncheck();
    await openSettingsPage('settingsFleet');
    await reopened.locator('#displayFleetCameras').selectOption('off');
    await reopened.locator('#displayFleetDetails').uncheck();
    await openSettingsPage('settingsHistory');
    await reopened.locator('#displayHistoryTotals').uncheck();
    await openSettingsPage('settingsFiles');
    await reopened.locator('#displayCardSize').selectOption('large');
    await reopened.locator('#displayRows').selectOption('2');
    await reopened.locator('#displayPreviews').uncheck();
    if (settingsCapture) await settings.screenshot({path:settingsCapture + '-files.png'});
    await reopened.setViewportSize({width:320,height:640});
    assert.ok(await layout.evaluate(root => root.children[0].getBoundingClientRect().bottom
      <= root.children[1].getBoundingClientRect().top && root.scrollWidth <= root.clientWidth));
    assert.ok(await settings.locator('#settingsSave').isVisible());
    if (settingsCapture) await settings.screenshot({path:settingsCapture + '-mobile.png'});
    await reopened.setViewportSize({width:1420,height:800});
    await save.click();
    await settings.waitFor({state:'hidden'});
    await reopened.waitForFunction(() => document.querySelectorAll('#files > .file-card').length === 8);
    assert.equal(await reopened.locator('#files .preview:not([hidden])').count(), 0);
    assert.deepEqual(await reopened.locator('html').evaluate(root => [
      root.dataset.theme, root.dataset.density, root.dataset.cardSize, getComputedStyle(root).colorScheme, root.dataset.fleetDetails
    ]), ['light','compact','large','light','off']);
    assert.ok(await reopened.locator('#temperaturesPanel').isHidden());
    assert.ok(await reopened.locator('#printerDetails').isHidden());
    await reopened.locator('#historyTab').click();
    assert.ok(await reopened.locator('#historyTotals').isHidden());
    assert.ok(await reopened.locator('#historyTable').isVisible());
    await reopened.locator('#monitorTab').click();
    assert.equal(savedPrinters.length, 0);
    await reopened.reload();
    await reopened.locator('#settingsButton').click();
    await openSettingsPage('settingsFiles');
    assert.equal(await reopened.locator('#displayCardSize').inputValue(), 'large');
    assert.equal(await reopened.locator('#displayRows').inputValue(), '2');
    assert.equal(await reopened.locator('#displayPreviews').isChecked(), false);
    await openSettingsPage('settingsFleet');
    assert.equal(await reopened.locator('#displayFleetCameras').inputValue(), 'off');
    assert.equal(await reopened.locator('#displayFleetDetails').isChecked(), false);
    await openSettingsPage('settingsMonitor');
    assert.equal(await reopened.locator('#displayMonitorChart').isChecked(), false);
    await openSettingsPage('settingsHistory');
    assert.equal(await reopened.locator('#displayHistoryTotals').isChecked(), false);
    await openSettingsPage('settingsAppearance');
    await reopened.locator('#displayTheme').selectOption('dark');
    await reopened.locator('#settingsCancel').click();
    assert.equal(await reopened.locator('html').getAttribute('data-theme'), 'light');
    await reopened.locator('#settingsButton').click();
    await openSettingsPage('settingsPrinters');
    const printerName = reopened.locator('#printerFields input[data-key="name"]').first();
    await printerName.fill('Simulated Creator');
    await openSettingsPage('settingsFiles');
    await reopened.locator('#displayRows').selectOption('1');
    await save.click();
    await settings.waitFor({state:'hidden'});
    assert.equal(savedPrinters.length, 1);
    assert.equal(savedPrinters[0][0].name, 'Simulated Creator');
    await reopened.waitForFunction(() => document.querySelectorAll('#files > .file-card').length === 4);
    const direct = await browser.newPage();
    direct.on('pageerror', error => errors.push(error.message));
    await direct.route('**/status', async route => {
      const response = await route.fetch();
      await route.fulfill({response, json:{...await response.json(), managedCamera:false}});
    });
    await direct.goto(urls.panel);
    const directCamera = direct.locator('#camera');
    const directSettings = direct.getByRole('dialog', {name:'Camera', exact:true});
    const openDirectSettings = direct.locator('#jobPanel').getByRole('button', {name:'Camera', exact:true});
    await openDirectSettings.click();
    await directSettings.locator('#cameraUrl').fill(urls.direct);
    await directSettings.getByRole('button', {name:'Connect', exact:true}).click();
    await direct.waitForFunction(() => !document.querySelector('#camera').hidden);
    await directSettings.getByRole('button', {name:'Close', exact:true}).click();
    const directSessions = (await stats()).direct_sessions;
    const directViewer = direct.locator('#jobsCameraDialog');
    await directCamera.getByRole('button', {name:'Enlarge camera'}).click();
    await directViewer.locator('#cameraImage').waitFor({state:'visible'});
    await directViewer.getByRole('button', {name:'Close'}).click();
    await directCamera.locator('#cameraImage').waitFor({state:'visible'});
    assert.equal((await stats()).direct_sessions, directSessions);
    await openDirectSettings.click();
    await directSettings.getByRole('button', {name:'Disconnect', exact:true}).click();
    assert.ok(await directCamera.getByRole('button', {name:'Enlarge camera'}).isHidden());
    await direct.close();
    assert.deepEqual(errors, []);
    console.log('PASS: camera, fleet, catalog pagination, history names, saved display settings, and printer editing.');
  } finally {
    if (browser) await browser.close();
    fixture.stdin.end();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
