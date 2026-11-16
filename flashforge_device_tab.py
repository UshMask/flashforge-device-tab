# /// script
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "FlashForge Device Tab"
# description = "Read-only FlashForge monitoring inside OrcaSlicer's Device tab."
# author = "Max"
# version = "1.4.0"
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
HISTORY_LIMIT = 500
HISTORY_CHECKPOINT = 30
MAX_JOB_GAP = 180
DISCOVERY_INTERVAL = 30
IDLE_POLL_INTERVAL = 15

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
dialog.settings input, dialog.settings select { width: 100%; }
dialog.settings input[type="checkbox"] { width: auto; margin: 0; }
dialog.settings label.check { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); margin-top: 10px; }
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
      <button id="settingsButton" type="button" aria-haspopup="dialog">Settings</button>
      <button id="nativePage" type="button">Printer Web Interface</button>
    </div>
  </header>
  <p class="device-message" id="viewMessage" role="status" hidden></p>
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
  <section class="panel catalog" aria-labelledby="files-title">
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
    <h2 id="settingsTitle">Printer connection</h2>
    <label for="settingMode">Device tab shows</label>
    <select id="settingMode">
      <option value="profile">This printer (active profile)</option>
      <option value="dashboard">All printers at once</option>
    </select>
    <label class="check"><input type="checkbox" id="settingReadCode"> Read CheckCode from the printer profile</label>
    <label class="check"><input type="checkbox" id="settingTab"> Show the Printers tab</label>
    <label for="settingHost">Adventurer IPv4 address</label>
    <input id="settingHost" type="text" maxlength="15" autocomplete="off" spellcheck="false" placeholder="192.168.1.51">
    <label for="settingCode">CheckCode</label>
    <input id="settingCode" type="password" maxlength="32" autocomplete="off" placeholder="from printer network settings">
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
el('settingsButton').onclick = async () => {
  el('settingsStatus').textContent = '';
  settingsDialog.showModal();
  try {
    const data = await getJson('/settings');
    el('settingMode').value = data.mode === 'device' ? 'device' : 'panel';
    el('settingReadCode').checked = data.read_check_code !== false;
    el('settingTab').checked = data.tab_enabled !== false;
    el('settingHost').value = data.host || '';
    el('settingCode').value = data.check_code || '';
    el('settingHost').focus();
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
      body: JSON.stringify({mode: el('settingMode').value,
                            host: el('settingHost').value.trim(),
                            check_code: el('settingCode').value.trim(),
                            read_check_code: el('settingReadCode').checked,
                            tab_enabled: el('settingTab').checked}),
      signal: AbortSignal.timeout(12000),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Could not save the settings.');
    settingsDialog.close();
    window.location.replace('/');
  } catch (error) {
    status.textContent = error.message || 'Could not save the settings.';
  } finally {
    save.disabled = false;
  }
};
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

DASHBOARD_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}}</title>
<style>
:root {
  color-scheme: light;
  --bg: #efefef; --panel: #fff; --header: #f7f7f7; --text: #242b31; --muted: #5d6873;
  --line: #d9dee3; --blue: #166ac7; --track: #e4e9ef; --ok: #23764b; --warn: #92502a;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #242829; --panel: #303637; --header: #373f41; --text: #f0f3f6; --muted: #b7c1c8;
    --line: #4b5558; --blue: #77baff; --track: #50585c; --ok: #88d8a8; --warn: #f0b488;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px "Segoe UI", sans-serif; font-variant-numeric: tabular-nums; }
main { max-width: 1280px; margin: auto; padding: 20px 24px 28px; }
h1 { font-size: 17px; font-weight: 600; margin: 0; }
h2 { font-size: 14px; font-weight: 600; margin: 0; }
.device { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; padding: 0 0 10px; margin-bottom: 10px; border-bottom: 2px solid var(--panel); }
.identity, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.muted { color: var(--muted); }
.note { margin: 0 0 14px; font-size: 12px; color: var(--muted); max-width: 900px; }
.connection { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
.connection::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.connection.connected { color: var(--ok); }
.connection.disconnected { color: var(--warn); }
button { font: inherit; color: inherit; background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 6px 10px; cursor: pointer; }
button:hover:enabled { border-color: var(--blue); }
input, select { font: inherit; color: inherit; background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 6px 9px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 10px; min-height: 190px; }
.card.foreign { opacity: .82; }
.card header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.card .name { font-size: 15px; font-weight: 600; overflow-wrap: anywhere; }
.card .host { font-size: 12px; color: var(--muted); }
.card .state { font-size: 12px; color: var(--muted); white-space: nowrap; }
.card .state[data-state="active"] { color: var(--blue); }
.card .state[data-state="complete"] { color: var(--ok); }
.card .file { min-height: 18px; font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.progress-line { display: flex; align-items: center; gap: 8px; }
progress { display: block; appearance: none; width: 100%; height: 8px; border: 0; border-radius: 5px; background: var(--track); overflow: hidden; }
progress::-webkit-progress-bar { background: var(--track); border-radius: 5px; }
progress::-webkit-progress-value { background: var(--blue); border-radius: 5px; }
.percent { min-width: 4ch; text-align: right; font-size: 12px; }
.readouts { display: flex; gap: 16px; font-size: 12px; color: var(--muted); }
.readouts strong { color: var(--text); font-size: 14px; }
.card .footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: auto; }
.card a { color: var(--blue); text-decoration: none; font-size: 12px; font-weight: 600; }
.card a:hover { text-decoration: underline; }
.empty { background: var(--panel); border: 1px dashed var(--line); border-radius: 8px; padding: 28px; text-align: center; color: var(--muted); }
dialog { width: min(460px, calc(100vw - 32px)); padding: 0; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text); }
dialog::backdrop { background: rgba(10, 14, 16, .55); }
dialog form { display: flex; flex-direction: column; gap: 8px; padding: 18px 20px 16px; }
dialog label { font-size: 12px; color: var(--muted); }
dialog label.check { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); }
dialog input[type="checkbox"] { width: auto; margin: 0; }
dialog ul { margin: 4px 0 0; padding-left: 18px; font-size: 13px; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
.dialog-actions button[type="submit"] { background: var(--blue); border-color: var(--blue); color: #fff; }
</style>
</head>
<body>
<main>
  <header class="device">
    <div class="identity">
      <h1>{{title}}</h1>
      <span class="connection" id="connection" role="status">Connecting…</span>
      <span class="muted" id="count"></span>
    </div>
    <div class="actions">
      <span class="muted" id="modeLabel"></span>
      <button id="refresh" type="button">Refresh</button>
      <button id="settingsButton" type="button" aria-haspopup="dialog">Settings</button>
    </div>
  </header>
  <p class="note">
    Live data is read over the local network and only FlashForge printers are supported.
    Other printers are listed with their own web interface; printers without one cannot be
    monitored by this plugin.
  </p>
  <div class="grid" id="grid"></div>
  <p class="empty" id="empty" hidden>
    No printer profiles yet. Add printers in OrcaSlicer, then reopen this tab.
  </p>
</main>
<dialog id="settingsDialog" aria-labelledby="settingsTitle">
  <form id="settingsForm">
    <h2 id="settingsTitle">Device tab</h2>
    <label for="settingMode">What the Device tab shows for FlashForge printers</label>
    <select id="settingMode">
      <option value="panel">Printer panel (recommended)</option>
      <option value="device">All printers dashboard</option>
    </select>
    <label class="check"><input type="checkbox" id="settingReadCode"> Read CheckCode from printer profiles</label>
    <label class="check"><input type="checkbox" id="settingTab"> Show this Printers tab</label>
    <p class="muted">The dashboard is always available in this tab. Choosing “All printers
      dashboard” additionally replaces the Device tab page for FlashForge profiles.</p>
    <p class="muted" id="settingsStatus" role="status"></p>
    <ul id="printerList"></ul>
    <div class="dialog-actions">
      <button type="button" id="settingsCancel">Cancel</button>
      <button type="submit" id="settingsSave">Save</button>
    </div>
  </form>
</dialog>
<script>
const el = id => document.getElementById(id);
const bridge = window.orca && typeof window.orca.postMessage === 'function' ? window.orca : null;

function duration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const rest = seconds % 60;
  const pad = value => String(value).padStart(2, '0');
  return pad(hours) + ':' + pad(minutes) + ':' + pad(rest);
}

function stateOf(printer) {
  if (printer.complete) return 'complete';
  if (printer.printing) return 'active';
  return '';
}

function header(printer, label) {
  const head = document.createElement('header');
  const left = document.createElement('div');
  const name = document.createElement('div');
  name.className = 'name';
  name.textContent = printer.name;
  const host = document.createElement('div');
  host.className = 'host';
  host.textContent = printer.host || '';
  left.append(name, host);
  const state = document.createElement('span');
  state.className = 'state';
  state.dataset.state = label ? '' : stateOf(printer);
  state.textContent = label || printer.status || (printer.error ? 'No answer' : 'Waiting…');
  head.append(left, state);
  return head;
}

function foreignCard(printer, text, linkText, href) {
  const article = document.createElement('article');
  article.className = 'card foreign';
  article.append(header(printer, text));
  const footer = document.createElement('div');
  footer.className = 'footer';
  const spacer = document.createElement('span');
  const link = document.createElement('a');
  link.href = href;
  link.textContent = linkText;
  if (/^https?:/i.test(href)) {
    link.target = '_blank';
    link.rel = 'noreferrer';
  }
  if (href) {
    footer.append(spacer, link);
  } else {
    const note = document.createElement('span');
    note.className = 'muted';
    note.textContent = 'not monitored';
    footer.append(spacer, note);
  }
  article.append(footer);
  return article;
}

function meteorCard(printer) {
  const article = document.createElement('article');
  article.className = 'card';
  article.append(header(printer));

  const file = document.createElement('div');
  file.className = 'file';
  file.textContent = printer.file || (printer.error ? printer.error : '');

  const line = document.createElement('div');
  line.className = 'progress-line';
  const bar = document.createElement('progress');
  bar.max = 100;
  bar.value = printer.percent ?? 0;
  const percent = document.createElement('span');
  percent.className = 'percent';
  percent.textContent = printer.percent == null ? '' : printer.percent + '%';
  line.append(bar, percent);

  const readouts = document.createElement('div');
  readouts.className = 'readouts';
  const nozzle = document.createElement('span');
  nozzle.textContent = 'Nozzle ';
  const nozzleValue = document.createElement('strong');
  nozzleValue.textContent = Number.isFinite(printer.nozzle) ? Math.round(printer.nozzle) + ' °C' : '—';
  nozzle.append(nozzleValue);
  const bed = document.createElement('span');
  bed.textContent = 'Bed ';
  const bedValue = document.createElement('strong');
  bedValue.textContent = Number.isFinite(printer.bed) ? Math.round(printer.bed) + ' °C' : '—';
  bed.append(bedValue);
  readouts.append(nozzle, bed);

  const footer = document.createElement('div');
  footer.className = 'footer';
  const remaining = document.createElement('span');
  remaining.className = 'muted';
  remaining.textContent = printer.remaining == null ? '' : duration(printer.remaining) + ' left';
  const link = document.createElement('a');
  link.href = '/' + printer.host + '/';
  link.textContent = 'Open panel';
  footer.append(remaining, link);

  article.append(file, line, readouts, footer);
  return article;
}

function card(printer) {
  if (printer.kind === 'web') {
    return foreignCard(printer, 'Web interface', 'Open', printer.url || ('http://' + printer.host));
  }
  if (printer.kind === 'local') {
    return foreignCard(printer, 'No web interface', '', '');
  }
  return meteorCard(printer);
}

function render(data) {
  const printers = data.printers || [];
  el('connection').className = 'connection connected';
  el('connection').textContent = 'Connected';
  el('modeLabel').textContent = data.mode === 'device' ? 'Device tab: dashboard' : 'Device tab: panel';
  el('count').textContent = printers.length
    ? printers.length + (printers.length === 1 ? ' printer' : ' printers') : '';
  el('empty').hidden = printers.length > 0;
  el('grid').replaceChildren(...printers.map(card));
}

async function load() {
  if (bridge) {
    bridge.postMessage({action: 'dashboard'});
    return;
  }
  try {
    const response = await fetch('data', {cache: 'no-store', signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw Error('No response');
    render(await response.json());
  } catch {
    el('connection').className = 'connection disconnected';
    el('connection').textContent = 'No response';
  }
}

async function loadSettings() {
  if (bridge) {
    bridge.postMessage({action: 'settings'});
    return;
  }
  try {
    const response = await fetch('settings', {cache: 'no-store', signal: AbortSignal.timeout(12000)});
    applySettings(await response.json());
  } catch {
    el('settingsStatus').textContent = 'Could not read the settings.';
  }
}

function applySettings(data) {
  if (!data) return;
  el('settingMode').value = data.mode === 'device' ? 'device' : 'panel';
  el('settingReadCode').checked = data.read_check_code !== false;
  el('settingTab').checked = data.tab_enabled !== false;
  const items = (data.printers || []).map(printer => {
    const item = document.createElement('li');
    const kind = printer.kind === 'flashforge' ? 'monitored'
      : printer.kind === 'web' ? 'web interface' : 'no web interface';
    item.textContent = printer.name + ' · ' + (printer.host || 'local') + ' — ' + kind
      + (printer.active ? ' (active profile)' : '');
    return item;
  });
  el('printerList').replaceChildren(...items);
}

if (bridge) {
  bridge.onMessage(payload => {
    if (!payload) return;
    if (payload.error) {
      el('settingsStatus').textContent = payload.error;
      return;
    }
    if (payload.printers) {
      render(payload);
      if (el('settingsDialog').open) applySettings(payload);
    }
    if (payload.mode) el('settingMode').value = payload.mode === 'device' ? 'device' : 'panel';
  });
}

el('refresh').onclick = load;

el('settingsButton').onclick = () => {
  el('settingsStatus').textContent = '';
  el('settingsDialog').showModal();
  loadSettings();
};

el('settingsCancel').onclick = () => el('settingsDialog').close();

el('settingsForm').onsubmit = async event => {
  event.preventDefault();
  const status = el('settingsStatus');
  const save = el('settingsSave');
  const mode = el('settingMode').value;
  const options = {read_check_code: el('settingReadCode').checked,
                   tab_enabled: el('settingTab').checked};
  save.disabled = true;
  status.textContent = 'Saving…';
  try {
    if (bridge) {
      bridge.postMessage({action: 'options', mode, ...options});
      status.textContent = 'Saved.';
      el('settingsDialog').close();
      setTimeout(load, 300);
    } else {
      const response = await fetch('settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-FlashForge-Tab-Action': 'settings'},
        body: JSON.stringify({mode, ...options}),
        signal: AbortSignal.timeout(12000),
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.error || 'Could not save the settings.');
      el('settingsDialog').close();
      window.location.reload();
    }
  } catch (error) {
    status.textContent = error.message || 'Could not save the settings.';
  } finally {
    save.disabled = false;
  }
};

load();
setInterval(load, 5000);
</script>
</body>
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
DASHBOARD_TITLE = "FlashForge printers"


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


def dashboard_url():
    return f"http://{WEB_HOST}:{WEB_PORT}/dashboard/"


def dashboard_identity(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.netloc == f"{WEB_HOST}:{WEB_PORT}" and \
        parsed.path.strip("/") == "dashboard"


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
    apply_settings = None
    discover = None
    debug_payload = None

    def configure(self, get_config, storage):
        self.get_config = get_config
        self.storage = storage
        self.monitors = {}
        self.errors = {}
        self.printers = []
        self.state_lock = threading.RLock()
        self.printer_lock = threading.Lock()
        self.uploading = threading.Event()
        self.uploaded_at = 0.0
        self.stopping = threading.Event()
        self.poller = None
        self.next_discovery = 0.0
        self.heal_device_tab = None
        self.heal_attempts = 0

    # ---- configuration -------------------------------------------------

    def config(self):
        try:
            value = self.get_config()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def mode(self):
        value = self.config().get("mode")
        if value in ("device", "dashboard"):
            return "device"
        return "panel"

    def legacy_settings(self):
        config = self.config()
        return config.get("flashforge_host") or "", config.get("check_code") or ""

    def active_printer(self):
        with self.state_lock:
            flash = [entry for entry in self.printers if entry.get("kind", "flashforge") == "flashforge"]
            for entry in flash:
                if entry.get("active"):
                    return entry
            return flash[0] if flash else None

    def settings_for(self):
        entry = self.active_printer()
        if entry:
            return entry["host"], entry.get("code", "")
        return self.legacy_settings()

    def settings_payload(self):
        entry = self.active_printer() or {}
        with self.state_lock:
            printers = [{"host": item["host"], "name": item.get("name", ""),
                         "kind": item.get("kind", "flashforge"),
                         "active": bool(item.get("active"))} for item in self.printers]
        config = self.config()
        return {"host": entry.get("host", ""), "check_code": entry.get("code", ""),
                "endpoint": entry.get("host", ""), "mode": self.mode(), "printers": printers,
                "read_check_code": config.get("read_check_code", True),
                "tab_enabled": config.get("tab_enabled", True)}

    def refresh_printers(self):
        entries = []
        if self.discover is not None:
            try:
                entries = self.discover() or []
            except Exception:
                entries = []
        if not entries:
            # No FlashForge preset yet: fall back to the address from the old config.
            host, code = self.legacy_settings()
            if host:
                entries = [{"kind": "flashforge", "host": host, "code": code,
                            "name": "Printer", "active": True}]
        with self.state_lock:
            self.printers = entries
            known = {entry["host"] for entry in entries}
            for host in [name for name in self.monitors if name not in known]:
                self.monitors.pop(host).history.save(int(time.time()))
                self.errors.pop(host, None)
            for entry in entries:
                if entry.get("kind", "flashforge") != "flashforge":
                    continue
                monitor = self.monitors.get(entry["host"])
                if monitor is None:
                    try:
                        adapter = FlashForge(entry["host"], entry.get("code", ""))
                    except ValueError as error:
                        self.errors[entry["host"]] = str(error)
                        continue
                    monitor = PrinterMonitor(
                        adapter, self.storage / f"flashforge-{adapter.host}-history.json")
                    monitor.endpoint = adapter.host
                    monitor.next_due = 0.0
                    self.monitors[entry["host"]] = monitor
                    self.errors.pop(entry["host"], None)
                else:
                    monitor.adapter.check_code = entry.get("code", "")
            for host in [name for name in self.monitors
                         if name not in {entry["host"] for entry in entries}]:
                self.monitors.pop(host).history.save(int(time.time()))

    def dashboard_payload(self):
        printers = []
        for entry in list(self.printers):
            monitor = self.monitors.get(entry["host"])
            data, error = None, self.errors.get(entry["host"], "")
            if monitor is not None:
                with monitor.state_lock:
                    data = monitor.latest
                    error = monitor.error or error
            item = {"host": entry["host"], "name": entry.get("name") or entry["host"],
                    "kind": entry.get("kind", "flashforge"), "url": entry.get("url", ""),
                    "activeProfile": bool(entry.get("active")), "error": error,
                    "status": None, "percent": None, "remaining": None, "file": "",
                    "nozzle": None, "bed": None, "printing": False, "complete": False}
            if data:
                item.update(status=data.get("status"), percent=data.get("percent"),
                            remaining=data.get("remaining"), file=data.get("file"),
                            nozzle=data.get("nozzle"), bed=data.get("bed"),
                            printing=bool(data.get("active")), complete=bool(data.get("complete")))
            printers.append(item)
        return {"mode": self.mode(), "printers": printers}

    # ---- upload coordination -------------------------------------------

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

    # ---- polling --------------------------------------------------------

    def poll_printer(self):
        try:
            while not self.stopping.is_set():
                now = int(time.time())
                mono = time.monotonic()
                try:
                    if mono >= self.next_discovery:
                        self.refresh_printers()
                        self.next_discovery = mono + DISCOVERY_INTERVAL
                        if self.heal_device_tab is not None and self.heal_attempts < 6:
                            self.heal_attempts += 1
                            try:
                                if self.heal_device_tab():
                                    self.heal_device_tab = None
                            except Exception:
                                pass
                    active = (self.active_printer() or {}).get("host")
                    for host, monitor in list(self.monitors.items()):
                        if mono < getattr(monitor, "next_due", 0.0):
                            continue
                        monitor.next_due = mono + (POLL_INTERVAL if host == active else IDLE_POLL_INTERVAL)
                        with self.printer_io():
                            monitor.sample(now)
                except UploadInProgress:
                    pass
                except Exception:
                    # One bad response must not end the poll loop or the server.
                    with self.state_lock:
                        for host in list(self.monitors):
                            self.errors[host] = "Could not update printer data."
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
    """Answers on the ports older plugin releases used and redirects to the panel."""

    def do_GET(self):
        service = FlashForgeService.instance
        server = service._server if service is not None else None
        parts = [part for part in self.path.partition("?")[0].split("/") if part]
        host = ""
        for part in parts:
            try:
                host = local_address(part)
                break
            except ValueError:
                continue
        if server is not None:
            if server.mode() == "device":
                target = dashboard_url()
            else:
                active = server.active_printer() or {}
                target = monitor_url(host or active.get("host", ""))
        else:
            target = monitor_url(host) if host else dashboard_url()
        self.send_response(308)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


class MonitorHandler(BaseHTTPRequestHandler):
    def request_route(self):
        path = self.path.partition("?")[0]
        first, _, rest = path.lstrip("/").partition("/")
        return first, rest

    # ---- POST -----------------------------------------------------------

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
            self.save_settings(endpoint)
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

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 4096:
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_settings(self, endpoint):
        payload = self.read_json_body()
        if payload is None:
            self.reply_json({"error": "Invalid request."}, 400)
            return
        apply = self.server.apply_settings
        if apply is None:
            self.reply_json({"error": "Settings are unavailable."}, 503)
            return
        try:
            previous = self.server.mode()
            result = apply(str(payload.get("host") or ""), str(payload.get("check_code") or ""),
                           str(payload.get("mode") or ""), payload.get("read_check_code"),
                           payload.get("tab_enabled"))
        except ValueError as error:
            self.reply_json({"error": str(error)}, 400)
            return
        except (OSError, RuntimeError) as error:
            self.reply_json({"error": str(error)}, 409)
            return
        self.reply_json({"mode": self.server.mode(), "endpoint": result,
                         "moved": self.server.mode() != previous})

    # ---- GET ------------------------------------------------------------

    def do_GET(self):
        if self.headers.get("Host") != f"{WEB_HOST}:{WEB_PORT}":
            self.send_error(403)
            return
        endpoint, route = self.request_route()
        if endpoint != "dashboard" and self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            self.send_error(403)
            return
        if endpoint == "dashboard":
            if route and self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                self.send_error(403)
                return
            if not route and not self.path.partition("?")[0].endswith("/"):
                self.redirect(dashboard_url())
                return
            self.serve_dashboard(route)
            return
        if not endpoint:
            target = self.server.active_printer()
            if self.server.mode() == "device" or not target:
                self.redirect(dashboard_url())
                return
            self.redirect(monitor_url(target["host"]))
            return
        if not route:
            if not self.path.partition("?")[0].endswith("/"):
                self.redirect(monitor_url(endpoint))
                return
            page = PAGE.replace("{{printer}}", MONITOR_TITLE)
            self.reply(page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route == "settings":
            self.reply_json(self.server.settings_payload())
            return
        try:
            if self.server.check_view is not None:
                endpoint = self.server.check_view(endpoint)
        except (OSError, RuntimeError, ValueError) as error:
            self.reply_json({"error": str(error)}, 409)
            return
        self.serve_printer(endpoint, route)

    def serve_dashboard(self, route):
        if not route:
            # Shown inside the plugin's own Printers tab, which is a different
            # origin, so the page has to be frameable; it carries no data itself.
            page = DASHBOARD_PAGE.replace("{{title}}", DASHBOARD_TITLE)
            self.reply(page.encode("utf-8"), "text/html; charset=utf-8", frameable=True)
            return
        if route == "data":
            self.reply_json(self.server.dashboard_payload())
            return
        if route == "debug":
            self.reply_json(self.server.debug_payload or {})
            return
        if route == "settings":
            self.reply_json(self.server.settings_payload())
            return
        self.send_error(404)

    def serve_printer(self, endpoint, route):
        with self.server.state_lock:
            monitor = self.server.monitors.get(endpoint)
            error = self.server.errors.get(endpoint, "")
        if monitor is None:
            message = error or "Waiting for the printer…"
            if route == "telemetry":
                self.reply_json({"temperatures": [], "history": [], "historyError": "", "cameraUrl": ""})
            else:
                self.reply_json({"error": message}, 503)
            return
        try:
            if route == "telemetry":
                with monitor.state_lock:
                    data = {"temperatures": list(monitor.temperatures),
                            "history": monitor.history_snapshot,
                            "historyError": monitor.history_error,
                            "cameraUrl": monitor.camera_url}
                self.reply_json(data)
                return
            if self.server.upload_active():
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

    def redirect(self, location):
        self.send_response(308)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def reply_json(self, data, status=200):
        self.reply(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def reply(self, body, content_type, status=200, frameable=False):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if not frameable:
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
        return {"mode": "panel", "flashforge_host": "", "check_code": "",
                "read_check_code": True, "tab_enabled": True, "codes": {}}

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
                  for name in ("mode", "flashforge_host", "check_code")}
        result["read_check_code"] = config.get("read_check_code", True) is not False
        result["tab_enabled"] = config.get("tab_enabled", True) is not False
        codes = config.get("codes")
        result["codes"] = {str(key): str(value) for key, value in codes.items()
                           if isinstance(value, str)} if isinstance(codes, dict) else {}
        return result

    def discover_printers(self):
        """Printer profiles from OrcaSlicer, classified by how they can be reached."""
        printers = []
        seen = set()

        def add(kind, name, host, code="", url="", active=False):
            key = (kind, host, name)
            if key in seen:
                return
            seen.add(key)
            printers.append({"kind": kind, "host": host, "name": name, "code": code,
                             "url": url, "active": active})

        def is_user(preset):
            try:
                return bool(preset.is_user())
            except Exception:
                return False

        def classify(name, host_type, print_host, code, url="", active=False, user=False):
            if not print_host:
                # System presets are templates, not printers the user owns.
                if user:
                    add("local", name, "", active=active)
                return
            try:
                host = printer_endpoint(print_host)
            except ValueError:
                return
            if host_type == "flashforge":
                add("flashforge", name, host, code if read_codes else stored_codes.get(host, ""),
                    "", active)
            else:
                add("web", name, host, "", url or f"http://{host}", active)

        settings = self.config()
        read_codes = settings.get("read_check_code", True)
        stored_codes = settings.get("codes") or {}

        try:
            bundle = orca.host.preset_bundle()
        except Exception:
            return printers

        collection = None
        selected = ""
        try:
            collection = bundle.printers
            selected = collection.selected_preset_name() or ""
        except Exception:
            collection = None

        if collection is not None:
            # Named lookup and index lookup can differ between OrcaSlicer releases,
            # so try both and let the dedupe keep the list clean.
            try:
                names = list(collection.preset_names() or [])
            except Exception:
                names = []
            for name in names:
                try:
                    preset = collection.find_preset(name)
                except Exception:
                    preset = None
                if preset is None:
                    continue
                try:
                    classify(name, preset.config_value("host_type") or "",
                             preset.config_value("print_host") or "",
                             preset.config_value("printhost_apikey") or "",
                             preset.config_value(DEVICE_UI_KEY) or "",
                             name == selected, is_user(preset))
                except Exception:
                    continue
            try:
                size = collection.size()
            except Exception:
                size = 0
            for index in range(size):
                try:
                    preset = collection.preset(index)
                    name = preset.name
                    classify(name, preset.config_value("host_type") or "",
                             preset.config_value("print_host") or "",
                             preset.config_value("printhost_apikey") or "",
                             preset.config_value(DEVICE_UI_KEY) or "",
                             name == selected, is_user(preset))
                except Exception:
                    continue

        # The merged configuration always describes the active profile, even when
        # the preset collection is not readable from the plugin thread.
        try:
            host_type = bundle.full_config_value("host_type") or ""
            print_host = bundle.full_config_value("print_host") or ""
            code = bundle.full_config_value("printhost_apikey") or ""
            if print_host:
                try:
                    known = printer_endpoint(print_host) in {entry["host"] for entry in printers}
                except ValueError:
                    known = True
                if not known:
                    classify("Active printer", host_type, print_host, code, "", True, True)
        except Exception:
            pass
        return printers

    def discovery_debug(self):
        info = {}
        try:
            bundle = orca.host.preset_bundle()
            collection = bundle.printers
            info["selected"] = collection.selected_preset_name()
            info["size"] = collection.size()
            info["names"] = list(collection.preset_names() or [])[:40]
            rows = []
            for index in range(min(collection.size(), 40)):
                try:
                    preset = collection.preset(index)
                    rows.append({"name": preset.name,
                                 "host_type": preset.config_value("host_type") or "",
                                 "print_host": preset.config_value("print_host") or ""})
                except Exception as error:
                    rows.append({"error": str(error)})
            info["presets"] = rows
            info["full"] = {key: bundle.full_config_value(key)
                            for key in ("host_type", "print_host", "printhost_apikey")}
        except Exception as error:
            info["error"] = str(error)
        return info

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
            server.discover = self.discover_printers
            server.restore_page = self.restore_page
            server.check_view = self.check_view
            server.apply_settings = self.apply_settings
            server.debug_payload = self.discovery_debug
            server.heal_device_tab = self.heal_device_tab
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
            self.heal_device_tab()
        except Exception:
            pass

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
        elif event in (orca.LifecycleEvent.DeviceSelected, orca.LifecycleEvent.PrintStateChanged):
            server.next_discovery = 0.0

    def check_view(self, endpoint):
        if self._server is not None and self._server.mode() == "device":
            return endpoint
        preset = orca.host.preset_bundle().current_printer_preset()
        if preset.config_value("host_type") != "flashforge":
            raise ValueError("Open FlashForge Device Tab for a FlashForge printer profile from Speed Dial.")
        current = printer_endpoint(preset.config_value("print_host") or "")
        if endpoint and endpoint != current:
            raise ValueError("The printer address changed. Reopen FlashForge Device Tab for the current printer.")
        return current

    def apply_settings(self, host, check_code, mode, read_check_code=None, tab_enabled=None):
        server = self._server
        host = local_address(host) if host else ""
        if len(check_code) > 32 or any(not 33 <= ord(c) <= 126 for c in check_code):
            raise ValueError("The CheckCode must be printable ASCII, up to 32 characters.")
        if mode in ("profile",):
            mode = "panel"
        elif mode in ("dashboard",):
            mode = "device"
        if mode and mode not in ("panel", "device"):
            raise ValueError("Unknown display mode.")
        current_host, current_code = server.active_printer() and \
            (server.active_printer()["host"], server.active_printer().get("code", "")) or server.legacy_settings()
        config = dict(server.config())
        read_codes = config.get("read_check_code", True) if read_check_code is None else bool(read_check_code)
        if host and (host != current_host or (check_code and check_code != current_code)):
            if read_codes is False and host == current_host:
                # Codes are not read from profiles: keep them in the plugin config.
                codes = dict(config.get("codes") or {})
                codes[host] = check_code
                config["codes"] = codes
            else:
                transaction = orca.host.preset.printer.begin_update()
                preset = orca.host.preset_bundle().printers.edited_preset()
                if preset.name != transaction.target_name:
                    raise ValueError("The active printer profile changed. Try again.")
                if preset.config_value("host_type") != "flashforge":
                    raise ValueError("Select a printer profile that uses the FlashForge host.")
                transaction.set("print_host", host)
                if read_codes:
                    transaction.set("printhost_apikey", check_code)
                transaction.commit()
            current_host, current_code = host, check_code
        wanted_mode = mode or server.mode()
        config["mode"] = wanted_mode
        config["read_check_code"] = read_codes
        if tab_enabled is not None:
            config["tab_enabled"] = bool(tab_enabled)
        if host:
            config["flashforge_host"] = host
            config["check_code"] = check_code
        self.save_config(json.dumps(config, ensure_ascii=False))
        server.next_discovery = 0.0
        self._point_device_tab(current_host, wanted_mode)
        return current_host

    def _point_device_tab(self, host, mode):
        """Keep the Device tab on the page that matches the chosen display mode."""
        try:
            transaction = orca.host.preset.printer.begin_update()
            preset = orca.host.preset_bundle().printers.edited_preset()
            if preset.name != transaction.target_name:
                return
            if preset.config_value("host_type") != "flashforge":
                return
            current = preset.config_value(DEVICE_UI_KEY) or ""
            target = dashboard_url() if mode == "device" else monitor_url(host)
            if current == target:
                return
            if current and not (monitor_identity(current) or dashboard_identity(current)):
                try:
                    pages = self.saved_pages()
                except (OSError, ValueError):
                    pages = {}
                pages[preset.name] = current
                pending = self._page_store.with_suffix(".tmp")
                pending.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
                os.replace(pending, self._page_store)
            transaction.set(DEVICE_UI_KEY, target)
            transaction.commit()
        except (OSError, RuntimeError, ValueError):
            pass

    def set_mode(self, mode):
        if mode in ("profile",):
            mode = "panel"
        elif mode in ("dashboard",):
            mode = "device"
        if mode not in ("panel", "device"):
            raise ValueError("Unknown display mode.")
        config = dict(self._server.config())
        config["mode"] = mode
        self.save_config(json.dumps(config, ensure_ascii=False))
        host, _ = self._server.settings_for()
        self._point_device_tab(host, mode)
        return mode

    def heal_device_tab(self):
        """Point a stale plugin URL in the active profile at the current server."""
        try:
            transaction = orca.host.preset.printer.begin_update()
            preset = orca.host.preset_bundle().printers.edited_preset()
            if preset.name != transaction.target_name:
                return False
            if preset.config_value("host_type") != "flashforge":
                return True
            current = preset.config_value(DEVICE_UI_KEY) or ""
            prefixes = tuple(f"http://{WEB_HOST}:{port}" for port in (18765, 18769, WEB_PORT))
            if not current.startswith(prefixes):
                return True
            host = printer_endpoint(preset.config_value("print_host") or "")
            target = dashboard_url() if self._server.mode() == "device" else monitor_url(host)
            if current == target:
                return True
            if not current.startswith(f"http://{WEB_HOST}:{WEB_PORT}"):
                try:
                    pages = self.saved_pages()
                except (OSError, ValueError):
                    pages = {}
                pages.setdefault(preset.name, "")
                pending = self._page_store.with_suffix(".tmp")
                pending.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
                os.replace(pending, self._page_store)
            transaction.set(DEVICE_UI_KEY, target)
            transaction.commit()
            return True
        except (OSError, RuntimeError, ValueError):
            return False

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
            endpoint = printer_endpoint(print_host)
            if not self._server.printers:
                # First click after install: profiles have not been scanned yet.
                self._server.refresh_printers()
            if restore_view is not None and restore_view != endpoint:
                raise ValueError("This monitor page is not open for the current OrcaSlicer profile.")
            if restore_view is not None:
                fallback = presets.selected_preset().config_value(DEVICE_UI_KEY) or ""
                if monitor_identity(fallback) or dashboard_identity(fallback):
                    fallback = ""
                try:
                    pages = self.saved_pages()
                except (OSError, ValueError):
                    pages = {}
                target = pages.get(preset.name, fallback)
                if monitor_identity(target) or dashboard_identity(target):
                    target = fallback
                message = "Opened the printer web interface."
            else:
                host, check_code = preset.config_value("print_host"), preset.config_value("printhost_apikey")
                if not host:
                    raise ValueError("Enter the printer address in the printer profile first.")
                managed = endpoint == self._server.settings_for()[0]
                if not managed:
                    raise ValueError("The printer profile is not managed by this plugin.")
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
                target = dashboard_url() if self._server.mode() == "device" else monitor_url(endpoint)
                message = "Opened FlashForge Device Tab in the Device tab."

            if self._stopping.is_set():
                raise ValueError("FlashForge Device Tab is shutting down.")
            transaction.set(DEVICE_UI_KEY, target)
            transaction.commit()
            return message
        finally:
            self._switch_lock.release()

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


class PrintersPageService(orca.pages.PagesPluginCapabilityBase):
    """Dashboard as its own OrcaSlicer tab."""

    def get_name(self):
        return "Printers"

    def get_type(self):
        return orca.PluginType.Pages

    def get_ui(self):
        service = FlashForgeService.instance
        if service is not None and service._server is not None:
            if not service._server.config().get("tab_enabled", True):
                return (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<style>html,body{margin:0;height:100%;background:#242829;color:#cdd6db;"
                    "font:14px 'Segoe UI',sans-serif}div{padding:28px 32px;max-width:560px}"
                    "h2{font-size:16px;margin:0 0 8px}p{margin:0;color:#93a1a9}</style></head>"
                    "<body><div><h2>Printers tab is turned off</h2>"
                    "<p>Enable it in the Device tab → FlashForge Device Tab → Settings.</p>"
                    "</div></body></html>"
                )
        # The dashboard is served by the plugin's loopback server, so the tab and
        # the Device tab share one implementation and one data path.
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>html,body{margin:0;height:100%;background:#242829}"
            "iframe{border:0;width:100%;height:100%;display:block}</style></head>"
            f"<body><iframe src='{dashboard_url()}' title='FlashForge printers'></iframe></body></html>"
        )

    def on_message(self, message):
        service = FlashForgeService.instance
        if service is None or service._server is None:
            return
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            return
        try:
            action = payload.get("action")
            if action == "dashboard":
                self.post_message(service._server.dashboard_payload())
            elif action == "settings":
                self.post_message(service._server.settings_payload())
            elif action in ("mode", "options"):
                service.apply_settings("", "", str(payload.get("mode") or ""),
                                       payload.get("read_check_code"),
                                       payload.get("tab_enabled"))
                self.post_message({"mode": service._server.mode()})
        except ValueError as error:
            self.post_message({"error": str(error)})


@orca.plugin
class FlashForgeDeviceTab(orca.base):
    def register_capabilities(self):
        # The loopback page is declared here. Reaching the printer itself needs a
        # per-host grant, which OrcaSlicer asks for the first time the plugin
        # connects to a new address.
        orca.request_permissions(network_socket=[f"('{WEB_HOST}', {WEB_PORT})"], threading=True)
        orca.register_capability(FlashForgeService)
        orca.register_capability(PrintersPageService)
