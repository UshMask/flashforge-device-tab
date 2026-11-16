# /// script
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "FlashForge Device Tab"
# description = "Read-only FlashForge monitoring inside OrcaSlicer's Device tab."
# author = "Max"
# version = "1.6.2"
# ///
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import urlsplit

import orca

PORT = 8899
API_PORT = 8898
MAX_PREVIEW = 2 * 1024 * 1024
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"

# A stalled printer should fail one read, not one whole catalog request: the
# idle timeout applies per read while the total cap still bounds a trickling
# transfer.
READ_IDLE = 4.0
READ_TOTAL = 30.0

POLL_INTERVAL = 5
IDLE_POLL_INTERVAL = 15
HISTORY_LIMIT = 500
HISTORY_CHECKPOINT = 30
MAX_JOB_GAP = 180

# The Orca upload worker must not be held hostage by a monitor request that is
# already talking to the printer, so the drain wait is short and best-effort.
UPLOAD_DRAIN = 1.5
# If UploadFinished never arrives (cancelled or failed transfer), monitoring
# resumes on its own instead of answering 503 until the plugin is reloaded.
UPLOAD_TIMEOUT = 900

PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{printer}}</title>
<style>
:root {
  color-scheme: light;
  --bg: #efefef;
  --panel: #fff;
  --header: #f7f7f7;
  --text: #242b31;
  --muted: #5d6873;
  --line: #d9dee3;
  --blue: #166ac7;
  --track: #e4e9ef;
  --ok: #23764b;
  --warn: #92502a;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #242829;
    --panel: #303637;
    --header: #373f41;
    --text: #f0f3f6;
    --muted: #b7c1c8;
    --line: #4b5558;
    --blue: #77baff;
    --track: #50585c;
    --ok: #88d8a8;
    --warn: #f0b488;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px "Segoe UI", sans-serif; font-variant-numeric: tabular-nums; }
main { max-width: 1280px; margin: auto; padding: 20px 24px 28px; }
h1, h2, p, dl, dd { margin: 0; }
h1 { font-size: 17px; font-weight: 600; }
h2 { font-size: 14px; font-weight: 600; }
.muted { color: var(--muted); }
.device { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; padding: 0 0 10px; margin-bottom: 12px; border-bottom: 2px solid var(--panel); }
.identity, .connection, .device-actions, .row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.device-message { margin: -4px 0 12px; font-size: 12px; color: var(--warn); }
.row { justify-content: space-between; }
.device-state { font-size: 13px; color: var(--muted); }
.device-state[data-state="active"] { color: var(--blue); }
.device-state[data-state="complete"] { color: var(--ok); }
.connection { gap: 6px; color: var(--muted); font-size: 12px; white-space: nowrap; }
.connection::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.connection.connected { color: var(--ok); }
.connection.disconnected { color: var(--warn); }
.dashboard { display: grid; grid-template-columns: minmax(0, 1.75fr) minmax(280px, 1fr); gap: 12px; align-items: start; }
.dashboard > * { min-width: 0; }
.panel { background: var(--panel); }
.panel-title { padding: 9px 13px; background: var(--header); }
.job { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: center; padding: 16px; min-height: 158px; }
.job[data-preview="none"] { grid-template-columns: minmax(0, 1fr); }
.filename { font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.job-image { width: clamp(72px, 10vw, 130px); aspect-ratio: 1; object-fit: contain; border-radius: 5px; background: var(--header); }
.time { margin-top: 15px; }
.eta { display: block; color: var(--blue); font-size: 27px; font-weight: 600; line-height: 1.15; }
.caption { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
.progress-line { display: flex; align-items: center; gap: 10px; margin-top: 13px; }
progress { display: block; appearance: none; width: 100%; height: 8px; border: 0; border-radius: 5px; background: var(--track); overflow: hidden; }
progress::-webkit-progress-bar { background: var(--track); border-radius: 5px; }
progress::-webkit-progress-value { background: var(--blue); border-radius: 5px; }
progress::-moz-progress-bar { background: var(--blue); border-radius: 5px; }
.percent { min-width: 4ch; text-align: right; }
.job-meta { display: flex; flex-wrap: wrap; gap: 4px 15px; margin-top: 8px; font-size: 12px; color: var(--muted); }
.readouts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 12px; }
.readout { padding: 11px 8px; border-bottom: 1px solid var(--line); }
.readout:nth-child(odd) { border-right: 1px solid var(--line); }
.readout:nth-child(even) { padding-left: 13px; }
.readout:nth-last-child(-n+2) { border-bottom: 0; }
.readout dt { color: var(--muted); font-size: 12px; }
.readout dd { margin-top: 4px; font-size: 14px; overflow-wrap: anywhere; }
.readout strong { color: var(--blue); font-size: 19px; font-weight: 600; }
.stale .job, .stale .readouts { opacity: .65; }
.catalog { margin-top: 16px; }
.monitor-tools { margin-top: 12px; }
.tool-body { padding: 12px 13px; }
.chart { display: block; width: 100%; height: 207px; }
.chart-legend { display: flex; flex-wrap: wrap; gap: 5px 12px; margin: 4px 0 1px; font-size: 12px; }
.chart-legend span::before { content: ""; display: inline-block; width: 15px; border-top: 2px solid var(--series); margin-right: 5px; vertical-align: 3px; }
.chart-legend .target::before { border-top-style: dashed; }
.nozzle-series { --series: var(--blue); }
.bed-series { --series: #a34a20; }
@media (prefers-color-scheme: dark) { .bed-series { --series: #ffc08d; } }
summary.panel-title { cursor: pointer; font-weight: 600; }
details .tool-body { border-top: 1px solid var(--line); }
.camera-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 8px 0; }
.camera-form input { flex: 1 1 220px; width: auto; }
.camera-image { display: block; width: 100%; max-height: 360px; object-fit: contain; background: var(--header); margin-top: 8px; }
.history-summary, .history-table { margin-top: 8px; }
.history-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.history-table th, .history-table td { padding: 7px 9px; text-align: left; border-bottom: 1px solid var(--line); }
.history-table th:not(:first-child), .history-table td:not(:first-child) { white-space: nowrap; }
.history-table td:first-child { overflow-wrap: anywhere; }
.table-wrap { overflow: auto; max-height: 350px; }
.file-controls { justify-content: flex-start; padding: 10px 12px; }
input, button { font: inherit; color: inherit; background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 6px 9px; }
input { min-width: 0; width: min(100%, 245px); }
input::placeholder { color: var(--muted); }
button { cursor: pointer; }
button:hover:enabled, button:focus-visible, input:focus-visible { border-color: var(--blue); }
button:disabled { opacity: .55; cursor: default; }
.files { display: grid; grid-template-columns: repeat(auto-fill, minmax(125px, 1fr)); gap: 8px; padding: 0 12px 12px; }
.file-card { min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 4px; }
.preview { position: relative; display: grid; place-items: center; aspect-ratio: 1.65; background: var(--header); font-size: 11px; }
.preview img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: inherit; }
.file-name { padding: 7px 8px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 12px; }
.pager { border-top: 1px solid var(--line); padding: 8px 12px; }
@media (max-width: 740px) {
  main { padding: 14px 12px; }
  .dashboard { grid-template-columns: minmax(0, 1fr); }
}
dialog.settings { width: min(420px, calc(100vw - 32px)); padding: 0; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text); }
dialog.settings::backdrop { background: rgba(10, 14, 16, .55); }
dialog.settings form { display: flex; flex-direction: column; gap: 6px; padding: 18px 20px 16px; }
dialog.settings h2 { margin-bottom: 6px; }
dialog.settings label { margin-top: 8px; font-size: 12px; color: var(--muted); }
dialog.settings input, dialog.settings textarea { width: 100%; }
dialog.settings textarea { font: 13px "Segoe UI", sans-serif; resize: vertical; min-height: 96px; }
.fleet-toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); cursor: pointer; white-space: nowrap; }
.fleet-toggle input { margin: 0; }
.setup ol { margin: 6px 0 10px; padding-left: 20px; line-height: 1.55; }
.setup code { background: var(--header); border-radius: 4px; padding: 1px 5px; font-size: 12px; }
.fleet-list { display: flex; flex-direction: column; gap: 8px; }
.fleet-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; }
.fleet-row.current { border-color: var(--blue); }
.fleet-row .name { font-weight: 600; overflow-wrap: anywhere; }
.fleet-row .meta { font-size: 12px; color: var(--muted); }
.fleet-row .value { font-size: 13px; white-space: nowrap; }
.fleet-row a { color: var(--blue); font-size: 12px; font-weight: 600; text-decoration: none; }
dialog.settings .caption { margin-top: 2px; }
.settings-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.settings-actions button[type="submit"] { background: var(--blue); border-color: var(--blue); color: #fff; }
</style>
</head>
<body>
<main>
  <header class="device">
    <div class="identity"><h1>{{printer}}</h1><span class="device-state" id="status"></span></div>
    <div class="device-actions">
      <span class="connection" id="connection" role="status">Connecting…</span>
      <label class="fleet-toggle"><input type="checkbox" id="fleetToggle"> All printers</label>
      <button id="settingsButton" type="button" aria-haspopup="dialog">Settings</button>
      <button id="nativePage" type="button">Printer Web Interface</button>
    </div>
  </header>
  <p class="device-message" id="viewMessage" role="status" hidden></p>
  <section class="panel" id="setup" hidden>
    <h2 class="panel-title">Set up your first printer</h2>
    <div class="tool-body setup">
      <ol>
        <li>This page is shown by the plugin inside OrcaSlicer: Device tab → <b>Speed Dial</b> → <b>FlashForge Device Tab</b>.</li>
        <li>Press <b>Settings</b> in the panel header.</li>
        <li>Add one line per printer: <code>192.168.3.37 | 03dd42ee | Adventurer 5M</code> —
            address | CheckCode | name. The CheckCode is in the printer's network settings.</li>
        <li>Save and wait a few seconds: temperatures and progress appear here.</li>
      </ol>
      <p class="caption">Only FlashForge printers on the local network are supported. The plugin is read-only:
        it never starts, pauses or cancels a job.</p>
    </div>
  </section>
  <section class="panel" id="fleet" hidden>
    <div class="row panel-title"><h2>Printers</h2><button id="fleetRefresh" type="button">Refresh</button></div>
    <div class="tool-body"><div class="fleet-list" id="fleetList"></div></div>
  </section>
  <div class="dashboard" id="monitor">
    <section class="panel" aria-labelledby="job-title">
      <h2 class="panel-title" id="job-title">Print Job</h2>
      <div class="job" id="job" data-preview="none">
        <div>
          <p class="filename" id="jobFile">—</p>
          <div class="time" id="time" hidden>
            <strong class="eta" id="remaining"></strong>
            <span class="caption">Time Remaining</span>
          </div>
          <div class="progress-line" id="progressLine" hidden>
            <progress id="bar" max="100" aria-label="Print progress"></progress>
            <strong class="percent" id="percent"></strong>
          </div>
          <div class="job-meta" id="jobMeta" hidden>
            <span id="layers"></span><span id="elapsed"></span><span id="filament"></span>
          </div>
          <p class="caption" id="timeHint" hidden></p>
        </div>
        <img class="job-image" id="jobImage" alt="Current print job preview" hidden>
      </div>
    </section>
    <section class="panel" aria-labelledby="readouts-title">
      <h2 class="panel-title" id="readouts-title">Printer Readings</h2>
      <dl class="readouts">
        <div class="readout"><dt>Nozzle</dt><dd><strong id="nozzle">—</strong> <span class="muted" id="nozzleTarget"></span></dd></div>
        <div class="readout"><dt>Bed</dt><dd><strong id="bed">—</strong> <span class="muted" id="bedTarget"></span></dd></div>
        <div class="readout" id="fanReading" hidden><dt>Part Cooling Fan</dt><dd><strong id="fan"></strong></dd></div>
        <div class="readout" id="speedReading" hidden><dt>Print Speed</dt><dd><strong id="speed"></strong></dd></div>
      </dl>
    </section>
  </div>
  <div class="dashboard monitor-tools">
    <section class="panel" aria-labelledby="chart-title">
      <h2 class="panel-title" id="chart-title">Temperatures · Last 30 Minutes</h2>
      <div class="tool-body">
        <canvas class="chart" id="chart" role="img" aria-label="Chart of actual and target temperatures for the nozzle and bed"></canvas>
        <div class="chart-legend muted">
          <span class="nozzle-series">Nozzle</span><span class="nozzle-series target">Nozzle Target</span>
          <span class="bed-series">Bed</span><span class="bed-series target">Bed Target</span>
        </div>
      </div>
    </section>
    <details class="panel" id="camera">
      <summary class="panel-title">Camera</summary>
      <div class="tool-body">
        <label for="cameraUrl">MJPEG stream URL (HTTP or HTTPS)</label>
        <form class="camera-form" id="cameraForm">
          <input id="cameraUrl" type="url" maxlength="2048" placeholder="http://…" autocomplete="off">
          <button id="cameraStart" type="submit">Connect</button>
          <button id="cameraStop" type="button" hidden>Disconnect</button>
        </form>
        <p class="caption" id="cameraStatus" role="status"></p>
        <img class="camera-image" id="cameraImage" alt="Printer camera feed" referrerpolicy="no-referrer" hidden>
      </div>
    </details>
  </div>
  <details class="panel monitor-tools" id="history">
    <summary class="panel-title">Print History</summary>
    <div class="tool-body">
      <p class="muted" id="historyStatus" role="status">Print history is recorded while OrcaSlicer is open.</p>
      <p class="history-summary" id="historySummary"></p>
      <div class="table-wrap" id="historyTable" hidden>
        <table class="history-table">
          <thead><tr><th scope="col">File</th><th scope="col">Started</th><th scope="col">Print Time</th><th scope="col">Result</th></tr></thead>
          <tbody id="historyRows"></tbody>
        </table>
      </div>
    </div>
  </details>
  <section class="panel catalog" id="files-section" aria-labelledby="files-title">
    <div class="row panel-title"><h2 id="files-title">Printer Files</h2><button id="refresh">Refresh</button></div>
    <div class="row file-controls">
      <input id="search" type="search" placeholder="Search files" aria-label="Search files">
      <span class="muted" id="fileCount" role="status"></span>
    </div>
    <div class="files" id="files"></div>
    <div class="row pager" id="pager" hidden>
      <button id="previous">Previous</button>
      <span class="muted" id="pageNumber"></span>
      <button id="next">Next</button>
    </div>
  </section>
</main>
<dialog class="settings" id="settingsDialog" aria-labelledby="settingsTitle">
  <form id="settingsForm">
    <h2 id="settingsTitle">Printers</h2>
    <label for="printersText">One printer per line: address | CheckCode | name</label>
    <textarea id="printersText" rows="5" spellcheck="false" placeholder="192.168.3.37 | 03dd42ee | Adventurer 5M"></textarea>
    <p class="caption">CheckCode is optional: without it the panel cannot show the remaining time,
      filament estimate or camera address.</p>
    <p class="caption" id="settingsStatus" role="status"></p>
    <div class="settings-actions">
      <button type="button" id="settingsCancel">Cancel</button>
      <button type="submit" id="settingsSave">Save</button>
    </div>
  </form>
</dialog>
<script>
const el = id => document.getElementById(id);
const pageSize = 12;
const endpoint = path => new URL(path.replace(/^\//, ''), window.location.href);
let files = [];
let page = 0;
let renderVersion = 0;
let polling = false;
let jobFile = '';
let previewKey = '';
let catalogLoaded = false;
const imageRequests = new Map();

async function getJson(path) {
  const response = await fetch(endpoint(path), {cache: 'no-store', signal: AbortSignal.timeout(12000)});
  const data = await response.json();
  if (!response.ok) throw Object.assign(Error(data.error || 'No response'), {status: response.status});
  return data;
}

el('nativePage').onclick = async () => {
  const button = el('nativePage');
  const message = el('viewMessage');
  button.disabled = true;
  message.hidden = true;
  try {
    const response = await fetch(endpoint('/native'), {method: 'POST',
      headers: {'X-FlashForge-Tab-Action': 'restore'}, signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw Error((await response.json()).error);
    message.textContent = 'Opening the printer web interface…';
    message.hidden = false;
    setTimeout(() => { button.disabled = false; message.hidden = true; }, 5000);
  } catch (error) {
    message.textContent = error.message || 'Could not open the printer web interface.';
    message.hidden = false;
    button.disabled = false;
  }
};

function duration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const rest = seconds % 60;
  const pad = value => String(value).padStart(2, '0');
  return pad(hours) + ':' + pad(minutes) + ':' + pad(rest);
}

const celsius = value => Number.isFinite(value) ? Math.round(value) + ' °C' : '';
const plain = value => Number.isFinite(value) ? String(Math.round(value)) : '—';

const chart = el('chart');
let temperatures = [];
const shortTime = new Intl.DateTimeFormat('en-US', {hour: '2-digit', minute: '2-digit'});
const historyTime = new Intl.DateTimeFormat('en-US', {dateStyle: 'short', timeStyle: 'short'});

function drawChart() {
  const width = chart.clientWidth;
  const height = chart.clientHeight;
  if (!width || !height) return;
  const scale = window.devicePixelRatio || 1;
  chart.width = Math.round(width * scale);
  chart.height = Math.round(height * scale);
  const ctx = chart.getContext('2d');
  ctx.scale(scale, scale);
  const colors = getComputedStyle(chart);
  const series = [
    ['nozzle', colors.getPropertyValue('--blue').trim(), []],
    ['nozzleTarget', colors.getPropertyValue('--blue').trim(), [5, 4]],
    ['bed', colors.colorScheme === 'dark' ? '#ffc08d' : '#a34a20', []],
    ['bedTarget', colors.colorScheme === 'dark' ? '#ffc08d' : '#a34a20', [5, 4]]
  ];
  const end = Date.now();
  const start = end - 30 * 60 * 1000;
  const left = 41, right = width - 16, top = 11, bottom = height - 26;
  const samples = temperatures.filter(sample => sample.t >= start);
  const max = Math.max(100, Math.ceil(Math.max(0, ...samples.flatMap(sample =>
    series.map(([field]) => sample[field] ?? 0))) / 50) * 50);
  const x = t => left + (t - start) / (end - start) * (right - left);
  const y = value => bottom - Math.max(0, value) / max * (bottom - top);
  ctx.font = '11px "Segoe UI", sans-serif';
  ctx.fillStyle = colors.getPropertyValue('--muted').trim();
  ctx.strokeStyle = colors.getPropertyValue('--line').trim();
  for (let tick = 0; tick <= 4; tick++) {
    const value = max * tick / 4;
    ctx.beginPath();
    ctx.moveTo(left, y(value));
    ctx.lineTo(right, y(value));
    ctx.stroke();
    ctx.fillText(Math.round(value) + '°', 5, y(value) + 4);
  }
  ctx.fillText(shortTime.format(start), left, height - 5);
  const current = shortTime.format(end);
  ctx.fillText(current, right - ctx.measureText(current).width, height - 5);
  if (!samples.some(sample => sample.nozzle != null || sample.bed != null)) {
    const label = 'Waiting for temperature readings';
    ctx.fillText(label, Math.max(left + 5, (width - ctx.measureText(label).width) / 2), height / 2);
  }
  for (const [field, color, dash] of series) {
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash(dash);
    ctx.beginPath();
    let previous = 0;
    let last = null;
    for (const sample of samples) {
      const value = sample[field];
      if (!Number.isFinite(value)) {
        previous = 0;
        continue;
      }
      const px = x(sample.t), py = y(value);
      if (previous && sample.t - previous < 18000) ctx.lineTo(px, py);
      else ctx.moveTo(px, py);
      previous = sample.t;
      last = [px, py];
    }
    ctx.stroke();
    if (last && !dash.length) {
      ctx.beginPath();
      ctx.arc(...last, 2.5, 0, 2 * Math.PI);
      ctx.fill();
    }
  }
}

async function updateTools() {
  try {
    const data = await getJson('/telemetry');
    temperatures = data.temperatures;
    if (Array.isArray(data.printers)) {
      fleetPrinters = data.printers;
      applyView();
    }
    drawChart();
    if (data.cameraUrl && !el('cameraUrl').value && !el('cameraUrl').matches(':focus')) {
      el('cameraUrl').value = data.cameraUrl;
    }
    if (!el('history').open) return;
    const jobs = data.history;
    let message = 'Print history is recorded while OrcaSlicer is open.';
    if (data.historyError) message = data.historyError;
    else if (!jobs.length) message = 'No print jobs recorded yet. Print history is recorded while OrcaSlicer is open.';
    el('historyStatus').textContent = message;
    const completed = jobs.filter(job => job.result === 'complete');
    const total = completed.reduce((seconds, job) => seconds + (job.elapsed ?? 0), 0);
    el('historySummary').textContent = jobs.length
      ? 'Completed: ' + completed.length + ' of ' + jobs.length
        + (total ? ' · Total print time: ' + duration(total) : '')
      : '';
    const results = {active: 'In Progress', complete: 'Completed', cancelled: 'Canceled',
                     error: 'Error', unknown: 'Unknown Result'};
    el('historyRows').replaceChildren(...jobs.map(job => {
      const row = document.createElement('tr');
      for (const value of [job.file || 'Unknown filename', historyTime.format(job.started * 1000),
                           job.elapsed == null ? '—' : duration(job.elapsed), results[job.result]]) {
        const cell = row.insertCell();
        cell.textContent = value;
      }
      return row;
    }));
    el('historyTable').hidden = !jobs.length;
  } catch {
    drawChart();
    el('historyStatus').textContent = 'Print history is temporarily unavailable.';
  }
}

const cameraImage = el('cameraImage');
const cameraUrl = el('cameraUrl');
const cameraStatus = el('cameraStatus');
const cameraStorageKey = 'flashforge-device-tab-camera-' + window.location.pathname;
let cameraTimeout;
try { cameraUrl.value = localStorage.getItem(cameraStorageKey) || ''; } catch {}

function stopCamera(message = '') {
  clearTimeout(cameraTimeout);
  cameraImage.removeAttribute('src');
  cameraImage.hidden = true;
  el('cameraStop').hidden = true;
  el('cameraStart').disabled = false;
  cameraStatus.textContent = message;
}

el('cameraForm').onsubmit = event => {
  event.preventDefault();
  let url;
  try { url = new URL(cameraUrl.value.trim()); } catch {}
  if (!url || !['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    stopCamera('Enter an HTTP or HTTPS MJPEG URL without a username or password.');
    return;
  }
  try { localStorage.setItem(cameraStorageKey, url.href); } catch {}
  cameraImage.src = url.href;
  cameraImage.hidden = false;
  el('cameraStart').disabled = true;
  el('cameraStop').hidden = false;
  cameraStatus.textContent = 'Connecting…';
  cameraTimeout = setTimeout(() => stopCamera('No frame received. Check the URL and confirm the stream uses MJPEG.'), 12000);
};
cameraImage.onload = () => {
  clearTimeout(cameraTimeout);
  cameraStatus.textContent = '';
};
cameraImage.onerror = () => {
  if (cameraImage.hasAttribute('src')) stopCamera('Could not open the MJPEG stream. Check the camera URL.');
};
el('cameraStop').onclick = () => stopCamera();
el('camera').ontoggle = () => { if (!el('camera').open) stopCamera(); };
el('history').ontoggle = () => { if (el('history').open) updateTools(); };
new ResizeObserver(drawChart).observe(chart);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', drawChart);

function previewUrl(id) {
  if (!imageRequests.has(id)) {
    const promise = fetch(endpoint('/preview/' + id), {signal: AbortSignal.timeout(12000)})
      .then(response => {
        if (response.status === 404) return null;
        if (!response.ok) throw Error('Preview unavailable');
        return response.blob().then(blob => URL.createObjectURL(blob));
      })
      .catch(() => {
        if (imageRequests.get(id) === promise) imageRequests.delete(id);
        return null;
      });
    imageRequests.set(id, promise);
  }
  return imageRequests.get(id);
}

async function updateJobPreview() {
  const matching = files.filter(file => file.name === jobFile);
  const key = jobFile && matching.length === 1 ? matching[0].id : '';
  const image = el('jobImage');
  if (key !== previewKey) {
    previewKey = key;
    image.hidden = true;
    el('job').dataset.preview = 'none';
  }
  if (!image.hidden) return;
  if (!key) return;
  const request = previewUrl(key);
  const url = await request;
  if (!url || previewKey !== key || imageRequests.get(key) !== request) return;
  image.src = url;
  image.hidden = false;
  el('job').dataset.preview = 'available';
}

function updateJob(data) {
  const previousJob = jobFile;
  jobFile = data.file || '';
  if (catalogLoaded && data.active && jobFile && previousJob !== jobFile
      && !files.some(file => file.name === jobFile)) refreshFiles();
  el('jobFile').textContent = jobFile || (data.complete ? 'Print complete' : data.active ? 'Printing' : 'No active print job');
  el('progressLine').hidden = data.percent == null;
  el('percent').textContent = data.percent == null ? '' : data.percent + '%';
  el('bar').value = data.percent ?? 0;
  el('time').hidden = data.remaining == null;
  el('remaining').textContent = data.remaining == null ? '' : duration(data.remaining);
  el('timeHint').hidden = !data.active || !data.timeHint;
  el('timeHint').textContent = data.timeHint || '';
  el('layers').textContent = data.layer == null || !data.layerTotal
    ? '' : 'Layer ' + data.layer + ' of ' + data.layerTotal;
  el('elapsed').textContent = data.elapsed == null ? '' : 'Elapsed: ' + duration(data.elapsed);
  el('filament').textContent = data.filament == null ? '' : 'Estimated material: ' + data.filament.toLocaleString('en-US', {maximumFractionDigits: 2}) + ' g';
  el('jobMeta').hidden = !el('layers').textContent && data.elapsed == null && data.filament == null;
  updateJobPreview();
}

async function updateStatus() {
  if (polling || document.visibilityState !== 'visible') return;
  polling = true;
  const connection = el('connection');
  try {
    const data = await getJson('/status');
    connection.className = 'connection connected';
    connection.textContent = 'Connected';
    el('monitor').classList.remove('stale');
    el('status').textContent = data.status;
    el('status').dataset.state = data.complete ? 'complete' : data.active ? 'active' : '';
    el('nozzle').textContent = plain(data.nozzle);
    el('nozzleTarget').textContent = data.nozzleTarget == null
      ? (Number.isFinite(data.nozzle) ? '°C' : '') : '/ ' + celsius(data.nozzleTarget);
    el('bed').textContent = plain(data.bed);
    el('bedTarget').textContent = data.bedTarget == null
      ? (Number.isFinite(data.bed) ? '°C' : '') : '/ ' + celsius(data.bedTarget);
    for (const key of ['fan', 'speed']) {
      el(key + 'Reading').hidden = data[key] == null;
      el(key).textContent = data[key] == null ? '' : data[key] + ' %';
    }
    updateJob(data);
    if (!catalogLoaded) refreshFiles();
  } catch (error) {
    connection.className = 'connection disconnected';
    connection.textContent = error.message === 'Failed to fetch' ? 'Disconnected' : error.message;
    el('monitor').classList.add('stale');
    el('status').textContent = '';
    el('time').hidden = true;
    el('timeHint').hidden = true;
    if (error.status === 409) {
      files = [];
      jobFile = '';
      catalogLoaded = false;
      for (const promise of imageRequests.values()) promise.then(url => { if (url) URL.revokeObjectURL(url); });
      imageRequests.clear();
      updateJobPreview();
      renderFiles();
      stopCamera();
    }
  } finally {
    polling = false;
  }
  updateTools();
}

function fileCard(file) {
  const card = document.createElement('article');
  card.className = 'file-card';
  const preview = document.createElement('div');
  preview.className = 'preview muted';
  const placeholder = document.createElement('span');
  placeholder.textContent = 'Preview unavailable';
  preview.append(placeholder);
  const name = document.createElement('div');
  name.className = 'file-name';
  name.textContent = file.name;
  name.title = file.name;
  card.append(preview, name);
  return {card, preview};
}

async function loadPreviews(entries, version) {
  for (const {file, preview} of entries) {
    if (version !== renderVersion) return;
    const url = await previewUrl(file.id);
    if (url && version === renderVersion) {
      const image = new Image();
      image.alt = '';
      image.src = url;
      preview.replaceChildren(image);
    }
  }
}

function renderFiles() {
  const version = ++renderVersion;
  const query = el('search').value.trim().toLocaleLowerCase();
  const matching = files.filter(file => file.name.toLocaleLowerCase().includes(query));
  const pageCount = Math.ceil(matching.length / pageSize);
  page = Math.max(0, Math.min(page, pageCount - 1));
  el('fileCount').textContent = matching.length ? String(matching.length) : query ? 'No matching files' : 'No files';
  el('pager').hidden = pageCount < 2;
  el('previous').disabled = page === 0;
  el('next').disabled = page + 1 >= pageCount;
  el('pageNumber').textContent = (page + 1) + ' / ' + pageCount;
  const entries = matching.slice(page * pageSize, (page + 1) * pageSize).map(file => ({file, ...fileCard(file)}));
  el('files').replaceChildren(...entries.map(entry => entry.card));
  loadPreviews(entries, version);
}

async function refreshFiles() {
  if (el('refresh').disabled) return;
  el('refresh').disabled = true;
  el('fileCount').textContent = 'Loading…';
  try {
    files = (await getJson('/files')).files;
    catalogLoaded = true;
    for (const promise of imageRequests.values()) promise.then(url => { if (url) URL.revokeObjectURL(url); });
    imageRequests.clear();
    previewKey = '';
    el('jobImage').hidden = true;
    el('job').dataset.preview = 'none';
    page = 0;
    updateJobPreview();
    renderFiles();
  } catch {
    el('fileCount').textContent = 'Could not load the file list';
  } finally {
    el('refresh').disabled = false;
  }
}

el('refresh').onclick = refreshFiles;
el('search').oninput = () => { page = 0; renderFiles(); };
el('previous').onclick = () => { page--; renderFiles(); };
el('next').onclick = () => { page++; renderFiles(); };
document.addEventListener('visibilitychange', updateStatus);
updateStatus();
refreshFiles();
const settingsDialog = el('settingsDialog');
function parsePrinters(value) {
  const printers = [];
  for (const line of value.split(/\r?\n/)) {
    const parts = line.split('|').map(part => part.trim());
    if (!parts[0]) continue;
    printers.push({host: parts[0], code: parts[1] || '', name: parts[2] || ''});
  }
  return printers;
}

function formatPrinters(printers) {
  return (printers || []).map(item => [item.host, item.code || '', item.name || '']
    .join(' | ').replace(/ \| $/, '')).join('\n');
}

const fleetToggle = el('fleetToggle');
let fleetPrinters = [];

function renderFleet() {
  const rows = fleetPrinters.map(item => {
    const row = document.createElement('div');
    row.className = 'fleet-row' + (item.current ? ' current' : '');
    const left = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = item.name || item.host;
    const meta = document.createElement('div');
    meta.className = 'meta';
    const state = item.status || (item.error ? 'No answer' : 'Waiting…');

    const bits = [item.host, state];
    if (item.file) bits.push(item.file);
    if (item.percent != null) bits.push(item.percent + '%');
    if (item.remaining != null) bits.push(duration(item.remaining) + ' left');
    meta.textContent = bits.join(' · ');
    left.append(name, meta);
    const right = document.createElement('div');
    right.className = 'value';
    if (item.current) {
      right.textContent = 'open';
    } else {
      const link = document.createElement('a');
      link.href = '/' + item.host + '/';
      link.textContent = 'Open';
      right.append(link);
    }
    row.append(left, right);
    return row;
  });
  el('fleetList').replaceChildren(...rows);
}

function applyView() {
  const hasPrinters = fleetPrinters.length > 0;
  const showFleet = hasPrinters && fleetToggle.checked;
  el('setup').hidden = hasPrinters;
  el('fleet').hidden = !showFleet;
  el('monitor').hidden = !hasPrinters || showFleet;
  for (const id of ['chart', 'history', 'camera', 'files-section']) {
    const node = el(id);
    if (node) node.hidden = !hasPrinters || showFleet;
  }
  if (showFleet) renderFleet();
}

fleetToggle.checked = localStorage.getItem('ff-fleet') === '1';
fleetToggle.onchange = () => {
  try { localStorage.setItem('ff-fleet', fleetToggle.checked ? '1' : '0'); } catch {}
  applyView();
};
el('fleetRefresh').onclick = () => updateStatus();

el('settingsButton').onclick = async () => {
  el('settingsStatus').textContent = '';
  settingsDialog.showModal();
  try {
    const data = await getJson('/settings');
    el('printersText').value = formatPrinters(data.printers);
    el('printersText').focus();
  } catch (error) {
    el('settingsStatus').textContent = error.message || 'Could not read the settings.';
  }
};
el('settingsCancel').onclick = () => settingsDialog.close();
el('settingsForm').onsubmit = async event => {
  event.preventDefault();
  const status = el('settingsStatus');
  const save = el('settingsSave');
  save.disabled = true;
  status.textContent = 'Saving…';
  try {
    const response = await fetch(endpoint('/settings'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-FlashForge-Tab-Action': 'settings'},
      body: JSON.stringify({printers: parsePrinters(el('printersText').value)}),
      signal: AbortSignal.timeout(12000),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Could not save the settings.');
    settingsDialog.close();
    await updateStatus();
  } catch (error) {
    status.textContent = error.message || 'Could not save the settings.';
  } finally {
    save.disabled = false;
  }
};
applyView();
setInterval(updateStatus, 5000);
</script>
</body>
</html>'''

CONFIG_PAGE = r'''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<style>
body { font: 14px "Segoe UI", sans-serif; color: var(--orca-fg, #eee); margin: 12px; }
label { display: block; margin: 8px 0 5px; }
input, button { font: inherit; padding: 6px 8px; }
p { max-width: 520px; }
</style>
<div>
  <label for="flashforgeHost">Adventurer IPv4 address</label>
  <input type="text" id="flashforgeHost" placeholder="192.168.1.51" maxlength="15" autocomplete="off">
  <label for="code">CheckCode</label>
  <input type="password" id="code" maxlength="32" autocomplete="off">
  <p>From the Device tab, open Speed Dial and choose FlashForge Device Tab.</p>
  <p>Allow OrcaSlicer to access your printer when prompted, then reload the plugin.</p>
  <button id="save" type="button">Save</button>
  <span id="saved" role="status"></span>
</div>
<script>
const byId = id => document.getElementById(id);
const fields = {flashforge_host: byId('flashforgeHost'), check_code: byId('code')};
const saved = byId('saved');
let pending = false;
window.orca.onConfig(config => {
  for (const [key, field] of Object.entries(fields)) field.value = config[key] || '';
  if (pending) saved.textContent = 'Saved';
  pending = false;
});
for (const field of Object.values(fields)) field.oninput = () => { saved.textContent = ''; };
byId('save').onclick = () => {
  pending = true;
  window.orca.saveConfig(Object.fromEntries(Object.entries(fields).map(([key, field]) =>
    [key, field.value.trim()])));
};
</script>
</html>'''

STATES = {
    "READY": "Ready", "IDLE": "Ready", "BUSY": "Busy",
    "PRINTING": "Printing", "BUILDING": "Printing", "BUILDING_FROM_SD": "Printing",
    "PAUSED": "Paused", "ERROR": "Error",
    "COMPLETED": "Completed", "BUILDING_COMPLETED": "Completed",
    "HEATING": "Heating", "PAUSING": "Pausing",
    "CANCELING": "Canceling", "CANCELLING": "Canceling",
    "CANCELLED": "Canceled", "CANCELED": "Canceled",
    "FINISHED": "Completed", "STOPPED": "Canceled", "ATTENTION": "Needs Attention",
}
ACTIVE_JOB = {"PRINTING", "BUILDING", "BUILDING_FROM_SD", "PAUSED", "HEATING", "PAUSING"}
ENDED_JOB = {"CANCELLED": "cancelled", "CANCELED": "cancelled", "STOPPED": "cancelled", "ERROR": "error"}
ACK = re.compile(rb"(?:^|\n)ok\r?\n")
LIST_HEADER = b"D\xaa\xaaD"
LIST_ENTRY = b"::\xa3\xa3"
IMAGE_FRAME = b"**\xa2\xa2"


def command_reply(sock, request, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + 2
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("The printer did not finish sending data")
    sock.settimeout(remaining)
    sock.sendall(b"~" + request + b"\r\n")
    data = bytearray()
    while len(data) < 16384:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The printer did not finish sending data")
        sock.settimeout(remaining)
        block = sock.recv(4096)
        if not block:
            break
        data.extend(block)
        ack = ACK.search(data)
        if ack:
            return bytes(data[:ack.start()]), bytearray(data[ack.end():])
    raise ValueError("Incomplete response from the printer")


def command(sock, name, deadline=None):
    reply, _ = command_reply(sock, name.encode("ascii"), deadline)
    return reply.decode("utf-8", "replace")


def payloads(sock, data, limit, idle=READ_IDLE, total=READ_TOTAL):
    """Yield the reply as it grows.

    Each read gets its own idle timeout, so a slow but steady transfer still
    completes; the total cap only stops an endless trickle.
    """
    started = time.monotonic()
    while True:
        if len(data) > limit:
            raise ValueError("The printer response is too large")
        yield data
        remaining_total = total - (time.monotonic() - started)
        if remaining_total <= 0:
            raise TimeoutError("The printer did not finish sending data")
        sock.settimeout(min(idle, remaining_total))
        try:
            block = sock.recv(8192)
        except TimeoutError:
            raise TimeoutError("The printer stopped sending data") from None
        if not block:
            return
        data.extend(block)


def decode_file_list(data):
    if len(data) < 8:
        return None
    if not data.startswith(LIST_HEADER):
        raise ValueError("Unrecognized file list format")
    count = int.from_bytes(data[4:8], "big")
    files = {}
    offset = 8
    for _ in range(count):
        if offset + 8 > len(data):
            return None
        if data[offset:offset + 4] != LIST_ENTRY:
            raise ValueError("Invalid entry in the file list")
        size = int.from_bytes(data[offset + 4:offset + 8], "big")
        offset += 8
        if offset + size > len(data):
            return None
        path = bytes(data[offset:offset + size])
        offset += size
        if path.startswith(b"/") and not any(c in path for c in (b"\0", b"\r", b"\n")):
            files[hashlib.sha256(path).hexdigest()[:32]] = path
    return files


def read_files(host):
    with socket.create_connection((host, PORT), timeout=2) as sock:
        _, data = command_reply(sock, b"M661")
        for payload in payloads(sock, data, 1024 * 1024):
            files = decode_file_list(payload)
            if files is not None:
                return files
    raise ValueError("Incomplete file list")


def read_preview(host, path):
    with socket.create_connection((host, PORT), timeout=2) as sock:
        _, data = command_reply(sock, b"M662 " + path)
        try:
            for payload in payloads(sock, data, MAX_PREVIEW + 8):
                if payload.startswith(IMAGE_FRAME) and len(payload) >= 8:
                    size = int.from_bytes(payload[4:8], "big")
                    if not 0 < size <= MAX_PREVIEW:
                        return None
                    if len(payload) >= 8 + size:
                        image = bytes(payload[8:8 + size])
                        return image if image.startswith(PNG_HEADER) and image.endswith(PNG_END) else None
                elif payload.startswith(PNG_HEADER) and payload.endswith(PNG_END):
                    return bytes(payload)
        except TimeoutError:
            pass
    return None


def read_job_detail(serial, check_code, host):
    body = json.dumps({"serialNumber": serial, "checkCode": check_code})
    connection = http.client.HTTPConnection(host, API_PORT, timeout=1.5)
    try:
        connection.request("POST", "/detail", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return {}, "The printer API is unavailable"
        result = json.loads(response.read(65536))
        if result.get("code") != 0:
            return {}, "Check the CheckCode in the plugin settings"
        detail = result.get("detail")
        if isinstance(detail, dict):
            return detail, ""
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return {}, "The printer API is unavailable"


def detail_number(detail, key, lower, upper):
    try:
        value = float(detail[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return value if lower <= value <= upper else None


def read_status(host, check_code, include_details=False):
    with socket.create_connection((host, PORT), timeout=2) as sock:
        deadline = time.monotonic() + 2.5
        temp = command(sock, "M105", deadline)
        progress = command(sock, "M27", deadline)
        status = command(sock, "M119", deadline)
        state_match = re.search(r"^MachineStatus:[ \t]*(\S+)", status, re.M)
        if not state_match or not re.search(r"\bT0:", temp):
            raise ValueError("Unexpected response from the printer")
        state = state_match[1]
        serial = None
        if check_code and (include_details or state in ACTIVE_JOB):
            try:
                serial = re.search(r"^SN:[ \t]*([\w-]+)", command(sock, "M115", deadline), re.M)
            except (OSError, ValueError):
                pass

    def temperature(key):
        match = re.search(r"\b" + key + r":\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", temp)
        if not match:
            return None, None
        return float(match[1]), float(match[2])

    filename = re.search(r"^CurrentFile:[ \t]*([^\r\n]*)", status, re.M)
    layer = re.search(r"^Layer:\s*(\d+)\s*/\s*(\d+)", progress, re.M | re.I)
    current, total = (int(layer[1]), int(layer[2])) if layer else (0, 0)
    complete = state in {"COMPLETED", "BUILDING_COMPLETED"}
    active = state in ACTIVE_JOB
    percent = 100 if complete else min(100, round(current * 100 / total)) if active and total else None
    detail, time_hint = {}, ""
    if serial:
        detail, time_hint = read_job_detail(serial[1], check_code, host)
    elif active:
        if not check_code:
            time_hint = "Enter your CheckCode in the plugin settings"
        else:
            time_hint = "Printer serial number unavailable"

    seconds = detail_number(detail, "estimatedTime", 1e-9, 365 * 24 * 3600) if active else None
    elapsed = detail_number(detail, "printDuration", 1, 365 * 24 * 3600) if active or complete else None
    filament = detail_number(detail, "estimatedRightWeight", 1e-9, math.inf) if active else None
    if filament is not None and not math.isfinite(filament):
        filament = None
    readings = dict(zip(("nozzle", "nozzleTarget", "bed", "bedTarget"), (*temperature("T0"), *temperature("B"))))
    raw_name = filename[1].strip() if filename else ""
    detail_name = detail.get("printFileName")
    if not raw_name and isinstance(detail_name, str):
        raw_name = detail_name.strip()
    camera = detail.get("cameraStreamUrl")
    return {
        "status": STATES.get(state, state),
        "state": state,
        "nozzle": readings["nozzle"],
        "nozzleTarget": readings["nozzleTarget"],
        "bed": readings["bed"],
        "bedTarget": readings["bedTarget"],
        "temperatures": readings,
        "layer": current if layer and total and (active or complete) else None,
        "layerTotal": total if layer and total and (active or complete) else None,
        "percent": percent,
        "file": raw_name.rsplit("/", 1)[-1] if active or complete or state in ENDED_JOB else "",
        "active": active, "complete": complete,
        "remaining": math.ceil(seconds) if seconds is not None else None,
        "timeHint": time_hint or ("The printer has not reported the remaining time yet" if active and seconds is None else ""),
        "elapsed": int(elapsed) if elapsed is not None else None,
        "filament": filament,
        "fan": detail_number(detail, "coolingFanSpeed", 0, 100),
        "speed": detail_number(detail, "printSpeedAdjust", 1, 1000),
        "cameraUrl": camera if isinstance(camera, str) and camera.startswith(("http://", "https://")) else "",
    }


def local_address(host):
    try:
        address = ipaddress.IPv4Address((host or "").strip())
    except ipaddress.AddressValueError as error:
        raise ValueError("Enter a local IPv4 address for the printer.") from error
    # A LAN printer only: loopback stays available for local proxies and tests.
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
    if not any(address in ipaddress.IPv4Network(cidr) for cidr in networks):
        raise ValueError("Enter a local IPv4 address for the printer.")
    return str(address)


class FlashForge:
    def __init__(self, address, check_code):
        self.host = local_address(address)
        self.check_code = check_code

    def status(self, include_details):
        return read_status(self.host, self.check_code, include_details)

    def files(self):
        return {
            key: (path.rsplit(b"/", 1)[-1].decode("utf-8", "replace"), path)
            for key, path in read_files(self.host).items()
        }

    def preview(self, path):
        return read_preview(self.host, path)


class PrintHistory:
    def __init__(self, path):
        self.path = path
        self.jobs = []
        self.error = ""
        self.writable = True
        self.dirty = False
        self.saved_at = 0
        try:
            jobs = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(jobs, list):
                raise ValueError("Invalid print history format")
            for job in jobs[:HISTORY_LIMIT]:
                if (not isinstance(job, dict) or type(job.get("started")) is not int
                        or not 0 <= job["started"] <= 253402300799
                        or job.get("result") not in {"active", "complete", "cancelled", "error", "unknown"}
                        or not isinstance(job.get("file", ""), str)
                        or any(job.get(field) is not None
                               and (type(job[field]) is not int or job[field] < 0)
                               for field in ("elapsed", "layer", "seen"))):
                    raise ValueError("Invalid print history format")
                # Old or incomplete jobs can still be displayed. Without a last
                # observation, their result cannot be inferred from the next poll.
                if job["result"] == "active" and job.get("seen") is None:
                    job["result"] = "unknown"
                for field, default in (("file", ""), ("elapsed", None), ("layer", None)):
                    job.setdefault(field, default)
                self.jobs.append(job)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.jobs = []
            self.writable = False
            self.error = "The saved print history is damaged. New entries are available for this session."

    def save(self, now):
        if not self.dirty or not self.writable:
            return
        self.saved_at = now
        pending = self.path.with_suffix(".tmp")
        try:
            pending.write_text(json.dumps(self.jobs, ensure_ascii=False), encoding="utf-8")
            os.replace(pending, self.path)
        except OSError:
            self.error = "Could not save print history. New entries are still available for this session."
        else:
            self.error = ""
            self.dirty = False

    def observe(self, status, now):
        job = self.jobs[0] if self.jobs and self.jobs[0]["result"] == "active" else None
        name = status["file"]
        elapsed, layer = status["elapsed"], status["layer"]
        state = status["state"]
        write_now = False
        if job:
            changed = bool(job["file"] and name and job["file"] != name)
            reset = status["active"] and (
                (elapsed is not None and job["elapsed"] is not None and elapsed + 10 < job["elapsed"])
                or (layer is not None and job["layer"] is not None and layer + 1 < job["layer"])
            )
            gap = now - job["seen"] > MAX_JOB_GAP
            if changed or reset or gap:
                job["result"] = "unknown"
            elif status["complete"]:
                job["result"] = "complete"
            elif state in ENDED_JOB:
                job["result"] = ENDED_JOB[state]
            elif state in {"READY", "IDLE"}:
                job["result"] = "unknown"
            if job["result"] != "active":
                if job["result"] == "complete" and elapsed is not None:
                    job["elapsed"] = elapsed
                self.dirty = write_now = True
                job = None

        if status["active"] and not job:
            job = {"file": name, "started": now - (elapsed or 0), "seen": now,
                   "elapsed": elapsed, "layer": layer, "result": "active"}
            self.jobs.insert(0, job)
            del self.jobs[HISTORY_LIMIT:]
            self.dirty = write_now = True
        elif status["active"]:
            job["file"] = name or job["file"]
            job["seen"] = now
            job["elapsed"] = elapsed if elapsed is not None else job["elapsed"]
            job["layer"] = layer if layer is not None else job["layer"]
            self.dirty = True
        if write_now or now - self.saved_at >= HISTORY_CHECKPOINT:
            self.save(now)


class PrinterMonitor:
    def __init__(self, adapter, history_path):
        self.adapter = adapter
        self.history = PrintHistory(history_path)
        self.temperatures = deque(maxlen=361)
        self.files = {}
        self.latest = None
        self.camera_url = ""
        self.next_camera_probe = 0
        self.error = "Connecting to the printer…"
        self.permission_denied = False
        self.state_lock = threading.Lock()
        self.history_snapshot = [job.copy() for job in self.history.jobs]
        self.history_error = self.history.error

    def sample(self, now):
        if self.permission_denied:
            return
        probe_camera = time.monotonic() >= self.next_camera_probe
        try:
            status = self.adapter.status(include_details=probe_camera)
        except PermissionError:
            with self.state_lock:
                self.latest = None
                self.permission_denied = True
                self.error = "Allow access to the printer, then reload the plugin."
        except (OSError, ValueError):
            with self.state_lock:
                self.latest = None
                self.error = "The printer did not respond"
        else:
            self.history.observe(status, now)
            with self.state_lock:
                self.latest = status
                self.temperatures.append({"t": now * 1000, **status["temperatures"]})
                self.history_snapshot = [job.copy() for job in self.history.jobs]
                self.history_error = self.history.error
                if probe_camera or status["cameraUrl"]:
                    self.camera_url = status["cameraUrl"]
                    self.next_camera_probe = time.monotonic() + 60


class UploadInProgress(Exception):
    pass


WEB_HOST = "127.0.0.1"
WEB_PORT = 18771
DEVICE_UI_KEY = "print_host_webui"
MONITOR_TITLE = "FlashForge"


def monitor_url(endpoint=""):
    path = f"{WEB_PORT}/{endpoint}/" if endpoint else f"{WEB_PORT}/"
    return f"http://{WEB_HOST}:{path}"


def monitor_identity(url):
    try:
        url = urlsplit(url)
    except ValueError:
        return None
    if url.scheme != "http" or url.netloc != f"{WEB_HOST}:{WEB_PORT}":
        return None
    parts = url.path.strip("/").split("/")
    if not parts[0]:
        return ""
    try:
        return local_address(parts[0]) if len(parts) == 1 else None
    except ValueError:
        return None


def printer_endpoint(print_host):
    try:
        url = urlsplit(print_host if "://" in print_host else "http://" + print_host)
    except ValueError as error:
        raise ValueError("Enter a local IPv4 address for the printer.") from error
    return local_address(url.hostname)


class LoopbackServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    restore_page = None
    check_view = None

    def configure(self, get_config, storage):
        self.get_config = get_config
        self.storage = storage
        self.monitors = {}
        self.error = ""
        self.state_lock = threading.Lock()
        self.printer_lock = threading.Lock()
        self.apply_settings = None
        self.uploading = threading.Event()
        self.uploaded_at = 0.0
        self.stopping = threading.Event()
        self.poller = None

    def printers(self):
        """Configured printers: address, CheckCode and a display name."""
        config = self.get_config()
        result = []
        for entry in config.get("printers") or []:
            if not isinstance(entry, dict):
                continue
            try:
                host = local_address(str(entry.get("host") or ""))
            except ValueError:
                continue
            if any(item["host"] == host for item in result):
                continue
            result.append({"host": host, "code": str(entry.get("code") or ""),
                           "name": str(entry.get("name") or host)[:60]})
        if not result:
            # Older releases kept a single address in the plugin config.
            try:
                host = local_address(config.get("flashforge_host") or "")
            except ValueError:
                host = ""
            if host:
                result.append({"host": host, "code": config.get("check_code") or "",
                               "name": "Printer"})
        return result

    def settings_for(self):
        printers = self.printers()
        return (printers[0]["host"], printers[0]["code"]) if printers else ("", "")

    def settings_payload(self):
        printers = self.printers()
        return {"printers": printers, "endpoint": printers[0]["host"] if printers else ""}

    def fleet_payload(self, current=None):
        items = []
        for entry in self.printers():
            monitor = self.monitors.get(entry["host"])
            data, error = None, ""
            if monitor is not None:
                with monitor.state_lock:
                    data = monitor.latest
                    error = monitor.error
            item = {"host": entry["host"], "name": entry["name"],
                    "current": entry["host"] == current, "error": error,
                    "status": None, "percent": None, "remaining": None,
                    "file": "", "nozzle": None, "bed": None,
                    "printing": False, "complete": False}
            if data:
                item.update(status=data.get("status"), percent=data.get("percent"),
                            remaining=data.get("remaining"), file=data.get("file"),
                            nozzle=data.get("nozzle"), bed=data.get("bed"),
                            printing=bool(data.get("active")), complete=bool(data.get("complete")))
            items.append(item)
        return items

    def upload_active(self):
        # A cancelled or crashed Orca upload may never deliver UploadFinished;
        # without this the monitor would answer 503 until the plugin reloads.
        if self.uploading.is_set() and time.monotonic() - self.uploaded_at > UPLOAD_TIMEOUT:
            self.uploading.clear()
        return self.uploading.is_set()

    @contextmanager
    def printer_io(self):
        if self.upload_active() or self.stopping.is_set():
            raise UploadInProgress
        with self.printer_lock:
            if self.upload_active() or self.stopping.is_set():
                raise UploadInProgress
            yield

    def begin_upload(self):
        # The Orca upload worker calls this before opening its printer connection.
        # Drain the in-flight request, but never hold the caller for long: the
        # port has a single client, so a bounded wait is better than a GUI stall.
        self.uploading.set()
        self.uploaded_at = time.monotonic()
        acquired = self.printer_lock.acquire(timeout=UPLOAD_DRAIN)
        if acquired:
            self.printer_lock.release()

    def poll_printer(self):
        try:
            while not self.stopping.is_set():
                now = int(time.time())
                mono = time.monotonic()
                try:
                    entries = self.printers()
                    hosts = {entry["host"] for entry in entries}
                    with self.state_lock:
                        for host in [name for name in self.monitors if name not in hosts]:
                            self.monitors.pop(host).history.save(now)
                        for entry in entries:
                            monitor = self.monitors.get(entry["host"])
                            if monitor is None:
                                try:
                                    adapter = FlashForge(entry["host"], entry["code"])
                                except ValueError:
                                    continue
                                monitor = PrinterMonitor(
                                    adapter, self.storage / f"flashforge-{entry['host']}-history.json")
                                monitor.endpoint = entry["host"]
                                monitor.next_due = 0.0
                                self.monitors[entry["host"]] = monitor
                            monitor.adapter.check_code = entry["code"]
                            monitor.name = entry["name"]
                    for position, entry in enumerate(entries):
                        monitor = self.monitors.get(entry["host"])
                        if monitor is None or mono < getattr(monitor, "next_due", 0.0):
                            continue
                        # The first printer is the one usually on screen.
                        interval = POLL_INTERVAL if position == 0 or len(entries) <= 3 else IDLE_POLL_INTERVAL
                        monitor.next_due = mono + interval
                        with self.printer_io():
                            monitor.sample(now)
                except UploadInProgress:
                    pass
                except Exception:
                    for monitor in list(self.monitors.values()):
                        with monitor.state_lock:
                            monitor.latest = None
                            monitor.error = "Could not update printer data."
                self.stopping.wait(0.5)
        finally:
            for monitor in list(self.monitors.values()):
                monitor.history.save(int(time.time()))

    def serve_forever(self, poll_interval=0.5):
        worker = threading.Thread(target=self.poll_printer, name="FlashForge Device Tab poller", daemon=True)
        worker.start()
        self.poller = worker
        super().serve_forever(poll_interval)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self):
        if hasattr(self, "stopping"):
            self.stopping.set()
        super().server_close()
        if self.poller is not None:
            self.poller.join(timeout=3)

    def server_bind(self):
        # HTTPServer.server_bind() resolves the host name. A numeric loopback
        # listener only needs the socket permission for its configured address.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


class AliasServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class AliasRedirectHandler(BaseHTTPRequestHandler):
    """Answers on the ports older releases used and forwards to the panel."""

    def do_GET(self):
        service = FlashForgeService.instance
        server = service._server if service is not None else None
        host = ""
        for part in [part for part in self.path.partition("?")[0].split("/") if part]:
            try:
                host = local_address(part)
                break
            except ValueError:
                continue
        if host:
            target = monitor_url(host)
        elif server is not None:
            target = monitor_url(server.settings_for()[0] or "")
        else:
            target = monitor_url()
        self.send_response(308)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


class MonitorHandler(BaseHTTPRequestHandler):
    def request_route(self):
        path = self.path.partition("?")[0]
        endpoint, _, route = path.lstrip("/").partition("/")
        return endpoint, route

    def do_POST(self):
        authority = f"{WEB_HOST}:{WEB_PORT}"
        action = self.headers.get("X-FlashForge-Tab-Action")
        if (self.headers.get("Host") != authority
                or self.headers.get("Origin") != f"http://{authority}"
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
                or action not in ("restore", "settings")):
            self.reply_json({"error": "Open the monitor from the Device tab."}, 403)
            return
        endpoint, route = self.request_route()
        if route == "settings" and action == "settings":
            self.save_settings()
            return
        if route != "native" or action != "restore" or self.server.restore_page is None:
            self.reply_json({"error": "The printer web interface is unavailable."}, 404)
            return
        try:
            self.server.restore_page(endpoint)
        except (OSError, RuntimeError, ValueError) as error:
            self.reply_json({"error": str(error)}, 409)
        else:
            self.reply_json({})

    def save_settings(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 4096:
            self.reply_json({"error": "Invalid request."}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            self.reply_json({"error": "Invalid request."}, 400)
            return
        if not isinstance(payload, dict):
            self.reply_json({"error": "Invalid request."}, 400)
            return
        apply = self.server.apply_settings
        if apply is None:
            self.reply_json({"error": "Settings are unavailable."}, 503)
            return
        try:
            previous = self.server.settings_for()[0]
            endpoint = apply(payload.get("printers"))
        except ValueError as error:
            self.reply_json({"error": str(error)}, 400)
            return
        except (OSError, RuntimeError) as error:
            self.reply_json({"error": str(error)}, 409)
            return
        self.reply_json({"endpoint": endpoint, "moved": endpoint != previous})

    def do_GET(self):
        if self.headers.get("Host") != f"{WEB_HOST}:{WEB_PORT}":
            self.send_error(403)
            return
        endpoint, route = self.request_route()
        printers = self.server.printers()
        configured = printers[0]["host"] if printers else ""
        if not endpoint:
            if configured:
                self.send_response(308)
                self.send_header("Location", monitor_url(configured))
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.reply_json({"error": "Enter your printer address in the plugin settings."}, 503)
            return
        if not route:
            if not self.path.partition("?")[0].endswith("/"):
                self.send_response(308)
                self.send_header("Location", monitor_url(endpoint))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            page = PAGE.replace("{{printer}}", MONITOR_TITLE)
            self.reply(page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            self.send_error(403)
            return
        if route == "settings":
            self.reply_json(self.server.settings_payload())
            return

        try:
            if self.server.check_view is not None:
                endpoint = self.server.check_view(endpoint)
            if printers and endpoint not in {entry["host"] for entry in printers}:
                raise ValueError("This printer is not in the plugin settings.")
        except (OSError, RuntimeError, ValueError) as error:
            self.reply_json({"error": str(error)}, 409)
            return

        try:
            monitor = self.server.monitors.get(endpoint)
            uploading = self.server.upload_active()
            if monitor is None:
                if route == "telemetry":
                    self.reply_json({"temperatures": [], "history": [], "historyError": "",
                                     "cameraUrl": "", "printers": self.server.fleet_payload(endpoint)})
                else:
                    self.reply_json({"error": "This printer is not configured yet."}, 503)
                return
            if route == "telemetry":
                with monitor.state_lock:
                    data = {"temperatures": list(monitor.temperatures),
                            "history": monitor.history_snapshot,
                            "historyError": monitor.history_error,
                            "cameraUrl": monitor.camera_url}
                data["printers"] = self.server.fleet_payload(endpoint)
                self.reply_json(data)
                return
            if uploading:
                raise UploadInProgress
            with monitor.state_lock:
                data = monitor.latest
                error = monitor.error
            if data is None:
                self.reply_json({"error": error}, 503)
                return
            if route == "status":
                self.reply_json(data)
                return
            if route == "files":
                with self.server.printer_io():
                    files = monitor.adapter.files()
                with monitor.state_lock:
                    monitor.files = files
                self.reply_json({"files": [{"id": key, "name": name} for key, (name, _) in files.items()]})
                return
            if route.startswith("preview/"):
                key = route.removeprefix("preview/")
                with monitor.state_lock:
                    entry = monitor.files.get(key)
                if entry is not None:
                    with self.server.printer_io():
                        image = monitor.adapter.preview(entry[1])
                    self.reply(image or b"", "image/png", 200 if image else 404)
                    return
        except UploadInProgress:
            self.reply_json({"error": "OrcaSlicer is sending a print job. Monitoring will resume when the transfer is complete."}, 503)
            return
        except (OSError, ValueError, RuntimeError):
            self.reply_json({"error": "The printer did not respond or returned incomplete data"}, 503)
            return
        except Exception:
            self.reply_json({"error": "Could not read printer data."}, 500)
            return
        self.send_error(404)

    def snapshot(self):
        with self.server.state_lock:
            return None, self.server.error, self.server.upload_active()

    def reply_json(self, data, status=200):
        self.reply(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def reply(self, body, content_type, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            pass

    def log_message(self, format, *args):
        pass


class FlashForgeService(orca.script.ScriptPluginCapabilityBase):
    _server = None
    instance = None

    def get_name(self):
        return 'FlashForge Device Tab'

    def get_default_config(self):
        return {"printers": [], "flashforge_host": "", "check_code": ""}

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return CONFIG_PAGE

    def config(self):
        try:
            config = json.loads(self.get_config())
        except (TypeError, ValueError):
            return self.get_default_config()
        if not isinstance(config, dict):
            return self.get_default_config()
        result = {name: config[name].strip() if isinstance(config.get(name), str) else ""
                  for name in ("flashforge_host", "check_code")}
        result["printers"] = config.get("printers") if isinstance(config.get("printers"), list) else []
        return result

    def on_load(self):
        # Bind during the lifecycle callback so OrcaSlicer reports startup
        # errors in plugin diagnostics instead of losing them in a worker.
        storage = Path(orca.host.plugin.storage())
        self._page_store = storage / "device-pages.json"
        self._switch_lock = threading.Lock()
        self._stopping = threading.Event()
        server = LoopbackServer((WEB_HOST, WEB_PORT), MonitorHandler)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                                  name='FlashForge Device Tab HTTP', daemon=True)
        try:
            server.configure(self.config, storage)
            server.restore_page = self.restore_page
            server.check_view = self.check_view
            server.apply_settings = self.apply_settings
            worker.start()
        except Exception:
            server.server_close()
            raise
        for port in (18769, 18765):
            try:
                alias = AliasServer((WEB_HOST, port), AliasRedirectHandler)
            except OSError:
                continue
            threading.Thread(target=alias.serve_forever, kwargs={'poll_interval': 0.5},
                             name=f'FlashForge Device Tab alias {port}', daemon=True).start()
            self._aliases = getattr(self, "_aliases", [])
            self._aliases.append(alias)
        self._server = server
        FlashForgeService.instance = self
        try:
            self.adopt_profile_address()
        except Exception:
            pass

    def adopt_profile_address(self):
        """First run after an upgrade: take the address from the active profile."""
        config = self.config()
        if config.get("flashforge_host"):
            return
        preset = orca.host.preset_bundle().current_printer_preset()
        if preset.config_value("host_type") != "flashforge":
            return
        print_host = preset.config_value("print_host") or ""
        if not print_host:
            return
        host = printer_endpoint(print_host)
        self.save_config(json.dumps({"flashforge_host": host,
                                     "check_code": config.get("check_code", "")},
                                    ensure_ascii=False))

    def on_unload(self):
        for alias in getattr(self, "_aliases", []):
            alias.shutdown()
            alias.server_close()
        self._aliases = []
        if self._server is not None:
            self._stopping.set()
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def on_lifecycle_event(self, event, context):
        server = self._server
        if server is None:
            return
        if event == orca.LifecycleEvent.UploadStarted:
            server.begin_upload()
        elif event == orca.LifecycleEvent.UploadFinished:
            server.uploading.clear()

    def check_view(self, endpoint):
        preset = orca.host.preset_bundle().current_printer_preset()
        if preset.config_value("host_type") != "flashforge":
            raise ValueError("Open FlashForge Device Tab for a FlashForge printer profile from Speed Dial.")
        if monitor_identity(preset.config_value(DEVICE_UI_KEY) or "") != endpoint:
            raise ValueError("Open FlashForge Device Tab for the current printer profile from Speed Dial.")
        current = printer_endpoint(preset.config_value("print_host") or "")
        if endpoint and endpoint != current:
            raise ValueError("The printer address changed. Reopen FlashForge Device Tab for the current printer.")
        return current

    def saved_pages(self):
        try:
            pages = json.loads(self._page_store.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(pages, dict) or any(not isinstance(url, str) for url in pages.values()):
            raise ValueError("Could not read the saved printer web interface URLs.")
        return pages

    def switch_view(self, restore_view=None):
        if not self._switch_lock.acquire(blocking=False):
            raise ValueError("A page switch is already in progress.")
        try:
            if self._stopping.is_set():
                raise ValueError("FlashForge Device Tab is shutting down.")
            transaction = orca.host.preset.printer.begin_update()
            presets = orca.host.preset_bundle().printers
            preset = presets.edited_preset()
            if preset.name != transaction.target_name:
                raise ValueError("The active printer profile changed. Try again.")
            if preset.config_value("host_type") != "flashforge":
                raise ValueError("Select a printer profile that uses the FlashForge host.")
            current_url = preset.config_value(DEVICE_UI_KEY) or ""
            print_host = preset.config_value("print_host") or ""
            current_view = monitor_identity(current_url)
            if restore_view is not None and restore_view != current_view:
                raise ValueError("This monitor page is not open for the current OrcaSlicer profile.")
            if current_view:
                # A damaged cache or an older URL can always fall back to Orca's profile.
                fallback = presets.selected_preset().config_value(DEVICE_UI_KEY) or ""
                if monitor_identity(fallback):
                    fallback = ""
                try:
                    pages = self.saved_pages()
                except (OSError, ValueError):
                    pages = {}
                target = pages.get(preset.name, fallback)
                if monitor_identity(target):
                    target = fallback
                message = "Opened the printer web interface."
            else:
                endpoint = printer_endpoint(print_host)
                host, check_code = self._server.settings_for()
                if not host or endpoint != printer_endpoint(host):
                    raise ValueError("The printer address in the plugin settings does not match the active profile.")
                FlashForge(host, check_code)
                try:
                    pages = self.saved_pages()
                except ValueError:
                    archived = self._page_store.with_suffix(f".invalid-{time.time_ns()}.json")
                    self._page_store.rename(archived)
                    pages = {}
                pages[preset.name] = current_url
                pending = self._page_store.with_suffix(".tmp")
                pending.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
                os.replace(pending, self._page_store)
                target = monitor_url(endpoint)
                message = "Opened FlashForge Device Tab in the Device tab."

            if self._stopping.is_set():
                raise ValueError("FlashForge Device Tab is shutting down.")
            transaction.set(DEVICE_UI_KEY, target)
            transaction.commit()
            return message
        finally:
            self._switch_lock.release()

    def apply_settings(self, printers):
        if not isinstance(printers, list):
            raise ValueError("Invalid printer list.")
        clean = []
        for entry in printers:
            if not isinstance(entry, dict):
                raise ValueError("Invalid printer entry.")
            host = local_address(str(entry.get("host") or ""))
            code = str(entry.get("code") or "")
            if len(code) > 32 or any(not 33 <= ord(c) <= 126 for c in code):
                raise ValueError("The CheckCode must be printable ASCII, up to 32 characters.")
            if any(item["host"] == host for item in clean):
                continue
            clean.append({"host": host, "code": code,
                          "name": str(entry.get("name") or host)[:60]})
        if not clean:
            raise ValueError("Add at least one printer address.")
        host, check_code = clean[0]["host"], clean[0]["code"]
        self.save_config(json.dumps({"printers": clean, "flashforge_host": host,
                                     "check_code": check_code}, ensure_ascii=False))
        self._point_device_tab(host)
        return host

    def _point_device_tab(self, host):
        """Keep the Device tab pointed at the first configured printer."""
        try:
            preset = orca.host.preset_bundle().current_printer_preset()
            if preset.config_value("host_type") != "flashforge":
                return
            current = printer_endpoint(preset.config_value("print_host") or "")
            if current == host:
                return
            transaction = orca.host.preset.printer.begin_update()
            edited = orca.host.preset_bundle().printers.edited_preset()
            if edited.name != transaction.target_name:
                return
            if monitor_identity(edited.config_value(DEVICE_UI_KEY) or "") == current:
                transaction.set(DEVICE_UI_KEY, monitor_url(host))
            transaction.set("print_host", host)
            transaction.commit()
        except (OSError, RuntimeError, ValueError):
            pass

    def restore_page(self, view):
        # Request threads are not joined during unload. They can wait for the
        # GUI commit and report its outcome without blocking the server.
        self.switch_view(restore_view=view)

    def execute(self):
        try:
            message = self.switch_view()
        except (OSError, RuntimeError, ValueError) as error:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, str(error))
        return orca.ExecutionResult.success(message)


@orca.plugin
class FlashForgeDeviceTab(orca.base):
    def register_capabilities(self):
        # The loopback page is declared here. Reaching the printer itself needs a
        # per-host grant, which OrcaSlicer asks for the first time the plugin
        # connects to a new address.
        orca.request_permissions(network_socket=[f"('{WEB_HOST}', {WEB_PORT})"], threading=True)
        orca.register_capability(FlashForgeService)

