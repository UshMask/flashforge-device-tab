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
    const context = await browser.newContext();
    const stats = async () => (await fetch(urls.stats)).json();
    const waitForClose = async () => {
      for (let i = 0; i < 60; i++) {
        if ((await stats()).events.at(-1) === 'close') return;
        await new Promise(resolve => setTimeout(resolve,100));
      }
      throw Error('Camera did not close');
    };
    const page = await context.newPage();
    await page.goto(urls.panel);
    await page.locator('#camera summary').click();
    await page.waitForFunction(() => document.querySelector('#cameraUrl').disabled);
    await page.locator('#cameraStart').click();
    await page.locator('#cameraSnapshot').waitFor({state:'visible'});
    assert.ok(await page.locator('#cameraImage').evaluate(img => img.naturalWidth > 0));
    await page.evaluate(() => {
      document.querySelector('#cameraSnapshot').addEventListener('click', () => {
        window.frameAtClick = document.querySelector('#cameraImage').src;
        window.frameBytes = fetch(window.frameAtClick).then(r => r.arrayBuffer())
          .then(buffer => Array.from(new Uint8Array(buffer)));
      }, {capture:true, once:true});
    });
    const download = page.waitForEvent('download');
    await page.locator('#cameraSnapshot').click();
    const bytes = await readFile(await (await download).path());
    assert.deepEqual(bytes, Buffer.from(await page.evaluate(() => window.frameBytes)));
    await page.waitForFunction(() => document.querySelector('#cameraImage').src !== window.frameAtClick);
    const first = await stats();
    assert.ok(first.hashes.includes(createHash('sha256').update(bytes).digest('hex')));
    assert.deepEqual(first.events, ['open','video']);
    await page.close();
    await waitForClose();
    const reopened = await context.newPage();
    await reopened.goto(urls.panel);
    await reopened.locator('#cameraSnapshot').waitFor({state:'visible'});
    await reopened.locator('#cameraStop').click();
    await waitForClose();
    await reopened.reload();
    await reopened.waitForFunction(() => document.querySelector('#cameraUrl').disabled);
    assert.ok(await reopened.locator('#cameraStart').isEnabled());
    assert.deepEqual((await stats()).events, ['open','video','close','open','video','close']);
    console.log('PASS: displayed JPEG, matching snapshot, uninterrupted frames, tab close, automatic reconnect, disconnect.');
  } finally {
    if (browser) await browser.close();
    fixture.stdin.end();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
