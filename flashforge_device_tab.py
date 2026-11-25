# /// script
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "FlashForge Device Tab"
# description = "Follow local FlashForge prints in OrcaSlicer's Device or FlashForge tab."
# author = "Max"
# version = "1.12.1"
# ///
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import select
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import parse_qs, urlsplit

import orca

PORT = 8899
API_PORT = 8898
CAMERA_PORT = 8080
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
NOTIFICATIONS = {"finished": True, "cancelled": True, "error": True, "offline": False}

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
.row { justify-content: space-between; }
.device-state { font-size: 13px; color: var(--muted); }
.device-state[data-state="active"] { color: var(--blue); }
.device-state[data-state="complete"] { color: var(--ok); }
.connection { gap: 6px; color: var(--muted); font-size: 12px; white-space: nowrap; }
.connection::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.connection.connected { color: var(--ok); }
.connection.disconnected { color: var(--warn); max-width: 100%; min-width: 0; flex-wrap: nowrap;
  white-space: normal; overflow-wrap: anywhere; }
.connection.disconnected::before { flex: 0 0 7px; }
.dashboard { display: grid; grid-template-columns: minmax(0, 3fr) minmax(380px, 2fr); gap: 16px; align-items: start; }
.monitor-main, .printer-sidebar { display: grid; gap: 16px; min-width: 0; align-content: start; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.panel > .panel-title:first-child { border-radius: 7px 7px 0 0; }
.identity::before { content: ""; width: 24px; height: 24px; background: center / contain no-repeat url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAF7klEQVR4nK2XbYydRRXHf2dmnpd77/buu+lWtEJSsYsEP/mSiLEpVBpQGuFqjBE/GLUiRCWmxKSJRkOESBRsgn7R2KgQWdrFmLQFQ2hLjFUS24Dd1i3WUNB2F2F3u3f33vu8zPHD031pd7t2dznJ8+GZzMz5zX/OnJwjzJkAumbbtm4XB5u95zpBI1SF1ZiIKiYxRk8krey5qcHB0Qu+AFQu/Ajg22u1+8TIt8TIVYhZld8Fpop6fxb1u8affOqHMxBCrWYZGMjba3fstnHpLp8kkOceQeVtglD1oAjWGhOG+GbrqfH+/s8yNCQC0FH79DckLj+izUaCEogxYkRIswyvuirnRoTAOXyhgCKkplQKfaO1c3xg4AGp1mpdBn8SY7vJc8QYk+eeqWaDrmqVKAhYKYIASZbx5sQE5TjGWYuqKiKq3tedSzc64GaxrlezzBfOc5xzPHT3dj7z8U3E5WhVAK1GwtMvvMAPfrWbRpLgrBX1Xk0QVLOMW52F6zFGEdSIMNlq8ZOvfZV7Nm/h6ZeP8trICIFz6DKvQkRIs5y+nm7u2XILlbYy2x/8EUG5jAqKiOK53ik+FBARQzNNuaq3l89vvonfHPkTX/je93EuQFeogREhbSX87Ds7uGvTTTy07re8NjJCFIYzAoUG5nb33lMplShhOPbKK9BsEkchztrZL7COwF3ms+6iuVEYQpJwdPgUoUJbuUQRh7MyqbuUWoFEPe9b/24++pEP016pkHu/IgWsMUxMTfH+a64mlcVVXABgjaGuKXfeuInP3bhpwQKdL9klNpPRLh1LgDoZ1izMKwsA8jynakJ+8ex+HtszSGd1DXmeY61lfLLOl2//JNs/sZVxn2CtnV3TYUJ2H3yOnz65h441bXNrzk/yxVu3ct+ntpHn+f8HKAaF0bExhk+doq2jkzTPCJyjPjZGfXqayFjUF0EGkAGRsUy3WvxjeJi2zk7SLCOwjvr4OGf/+yYWWVS5RQEUCJzDlEqU44gsdxhjyKpVXjxxkrrPiG1ApkVsRMbRRDny9+PE1SqlMCR0RUBOl0qESySzyyZ7VcV7j1fFq5LlOZU4Zt+fj/Clhx/GqWARRCE2ju2PPsLe5w9TiWPSPJ9d571fMocsqsDlLPee7mqVvc8fRFF+ueN+AjF85dEf8/iBZ+np6CBb5J6XsmUBAOSqvKO7i8GDh2krl4mCgMef+SO9XV3Ldr48AAHNPa16gwZKxQT8evAPoFApl6mPnQeUoBRjnLn8W10pgOZKqb2N/i0foqgTFGMMAhclqn+9OMT0W+cRe2W1xBUBiBHSRgufxfRd+56F2WZ2nuH0X46TNhOitvLbBCDgk5yu9WtZ+971/PPIy4gsTqCqvOuGDYyWY8b+/QY2ClYPIGJoTTeJKiU2fOwDZK1kSYAgjpgcHePc8BmiOFo5gIggIuRJytpr11PprHLs94cRs3SRrF4JKzHr+q9m4vVRuLDPFQOICCikWYaxFryy/oYNbLz5gyRTzSsDKMecOnyUo2dGcNaQZhmoLgrimBdSRoRWkpAJrOvpIZ+sM9GmHHpiP4ee2A9LnORiiuIN2jAgn6zT19uDF6GZFNenc/PEiUgKqKonCgLOjIyy729/5e7NW2gmCa+eO1fkcr/MkswUVXVfTzffvu12Dgy9xOnX/0Mchah6pHjHqVPPkGhRpalA6Bw7dj1GcK/w9a23YWc2XJb7uTzkgQMvHeObu3YhRor2SxFUxeCPS1ut1uvwJxHTgfcixkiSpuTec03fOkpxtOKaUIBmK+H02bOICHEQ4OfK8oax2UYBaK/dcb8tlR70jbnGRIBGkuC9R5Z9/hkVimwZh2HR+81rTPJG44GJgT07i9asv187hoZ+Z+LoTt9qccGripgVup4PMa81M8aYKCJvNvdNdHZtY2zMX1TGddRqO7HmXhHpLSJeWf7tL4YgM83pW6r+5xMjb3yXQ4dy5u0+2y531WrvzI25RVSvU9W4GF1hiy5FKSyGlmBOaJo+M75376vzfOr/AJq6riJACkPeAAAAAElFTkSuQmCC); }
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
.monitor-tools { margin-top: 12px; }
.tool-body { padding: 12px 13px; }
.chart { display: block; width: 100%; height: 168px; }
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
}
@media (max-width: 820px) {
  .dashboard { grid-template-columns: minmax(0, 1fr); reading-flow: grid-rows; }
  .monitor-main, .printer-sidebar { display: contents; }
  .dashboard .hero { order: -2; }
  .dashboard .readings { order: -1; }
  .dashboard .catalog { order: 1; }
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
button.danger { border-color: #a4443f; color: #ffb4ae; }
button.danger.armed { background: #a4443f; color: #fff; }
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
/* Device panel */
:root { --bg:#f5f5ef; --panel:#fffefa; --header:var(--panel); --text:#232c25; --muted:#5b665c; --line:#d7dcd1; --blue:#39745c; }
@media(prefers-color-scheme:dark){:root { --bg:#1d2521; --panel:#29322d; --text:#f1f4ec; --muted:#bcc8bc; --line:#465449; --blue:#98d6b1; }}
main {padding-top:0}
.mast,.tabs{display:flex;align-items:center;gap:14px}
.mast{background:#202824;color:#edf4ec;padding:12px 18px;margin:0 -12px}
.mast strong{font-size:20px}
.mast span{color:#c2cec3}
.mast a,.hero button{background:#33453b;color:#f5f6ef;border-color:#65776a}
.hero button.danger{background:#33453b;color:#ffc1a8}
.mast a{margin-left:auto;padding:5px 9px;border:1px solid #65776a;border-radius:6px;text-decoration:none}
.tabs{overflow:auto;border-bottom:1px solid var(--line);gap:18px}
.tabs button{white-space:nowrap;border:0;border-bottom:3px solid transparent;border-radius:0;background:none;padding:13px 2px;color:var(--muted)}
.tabs button[aria-current="page"]{border-color:var(--blue);color:var(--text)}
#notificationsButton{margin-left:auto}
.device{margin:22px 0 14px;border:0}
.device h1{font-size:clamp(24px,3vw,40px);letter-spacing:-.04em}
.panel{border-radius:12px}
.panel-title{padding:13px 16px}
.hero,.hero .panel-title{background:#24332b;color:#f5f6ed}
.hero .filename{font-size:clamp(20px,2.5vw,34px);letter-spacing:-.03em}
.hero .caption,.hero .job-meta{color:#c7d5c6}
.hero .eta,.hero .percent{color:#ffac7d}
.hero .percent{order:-1;min-width:0}
.hero .progress-line{display:grid;gap:6px}
.hero .percent{text-align:left;font-size:clamp(30px,4vw,46px);line-height:1.05}
.hero .eta{font-size:21px}
.hero .job-meta{margin-top:13px}
.mast a,.hero button{color:#edf6ec;background:#304538}
.hero progress::-webkit-progress-value{background:#fa9862}
.hero .progress-line progress{height:6px}
.hero .job{min-height:145px}
.hero .job:has(#progressLine[hidden]):has(#time[hidden]){min-height:88px}
#jobActions:has(button:not([hidden])){display:flex;padding:10px 16px;border-top:1px solid #526154}
#jobActions:not(:has(button:not([hidden]))){display:none}
#cancelPrint{margin-left:auto}
.readings .panel-title {gap:4px 12px}
.readings .readouts {grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:12px;padding:4px 16px 12px}
.readings .readout {padding:0;border:0}
.connection-footer {display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
  padding:8px 16px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.connection-meta {display:flex;gap:4px 12px;align-items:center;flex-wrap:wrap}
.connection-footer button {margin-left:auto}
.totals .readouts {grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:0}
.totals .readout {padding:0;border:0}
#manualPanel {overflow:hidden; container-type:inline-size}
#manualPanel summary {display:flex;align-items:center;gap:8px;list-style:none;font-size:16px}
#manualPanel summary::-webkit-details-marker {display:none}
#manualPanel summary::after {content:'';width:6px;height:6px;border:solid var(--muted);border-width:0 1px 1px 0;
  transform:translateY(-2px) rotate(45deg);margin:0 4px}
#manualPanel[open] summary::after {transform:translateY(2px) rotate(225deg)}
#manualBadge {margin-left:auto;font-size:12px;font-weight:600;color:var(--muted)}
#manualBadge::before {content:'';display:inline-block;width:7px;height:7px;border-radius:50%;background:currentColor;margin-right:6px}
#manualBadge[data-ready="true"] {color:var(--blue)}
#manualPanel .manual-body {display:grid;gap:12px;padding:12px 16px 16px}
.manual-body p {margin:0}
.manual-surface {display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:16px;
  background:#22312c;color:#f0f6f1;border:1px solid #394c43;border-radius:8px;padding:12px}
.manual-xy,.motion-side {display:grid;align-content:start;gap:12px;min-width:0}
.manual-xy {justify-items:center}
.manual-bar,.manual-row {display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.manual-bar {width:100%;justify-content:space-between}
.manual-bar strong,.motion-side strong {font-size:13px;font-weight:600}
.step-switch {display:inline-flex;background:#17241f;border-radius:6px;padding:3px;gap:2px}
.step-switch label {position:relative;cursor:pointer}
.step-switch input {position:absolute;opacity:0;width:1px;height:1px}
.step-switch span {display:block;padding:5px 9px;border-radius:4px;font-size:12px;color:#d7e3db}
.step-switch input:checked + span {background:#c2e9ce;color:#183025;font-weight:700}
.step-switch input:focus-visible + span {outline:2px solid white;outline-offset:1px}
.jog-pad {position:relative;width:min(100%,148px);aspect-ratio:1;border-radius:50%;background:#16231e;
  border:2px solid #15211c;box-shadow:0 0 0 1px #53675c}
.jog-pad:focus-visible {outline:2px solid #c2e9ce;outline-offset:4px}
.jog-pad button[data-jog] {position:absolute;inset:0;width:100%;height:100%;padding:0;border:0;border-radius:50%;
  transform:rotate(var(--angle));clip-path:polygon(0 0,100% 0,50% 49%);color:#eff8ef;
  background:radial-gradient(circle,transparent 51%,#4c6257 52%,transparent 54%),#35493e}
.jog-pad [data-jog="y+"] {--angle:0deg}
.jog-pad [data-jog="x+"] {--angle:90deg}
.jog-pad [data-jog="y-"] {--angle:180deg}
.jog-pad [data-jog="x-"] {--angle:270deg}
.jog-pad [data-jog] span {position:absolute;top:10%;left:50%;transform:translateX(-50%) rotate(calc(0deg - var(--angle)));font-weight:700}
.jog-pad button[data-jog]:is(:hover,:focus-visible):enabled {background:#52745f}
.jog-pad button[data-jog]:active:enabled {background:#658a71}
.jog-pad button[data-manual="home"] {position:absolute;inset:32%;border:2px solid #17251e;border-radius:50%;padding:0;
  background:#c2e9ce;color:#173024;display:grid;align-content:center;justify-items:center;font-size:11px;font-weight:700}
.home-symbol {font-size:20px;line-height:1}
.motion-side {border-left:1px solid #475a4f;padding-left:12px}
.motion-group {display:grid;gap:4px;min-width:0}
.motion-group small {font-size:11px;color:#d0ded3}
.motion-buttons {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}
.manual-surface .filament-pad button {border:1px solid #5b7163;background:#304439;color:#f0f6f1;min-height:32px}
.manual-surface .z-row button {color:#eef4ef;background:#304439;border-color:#5b7163;min-height:32px}
.motion-buttons button {padding:4px}
.manual-surface :is(.filament-pad,.z-row) button:hover:enabled {background:#476350}
.manual-surface button:disabled {opacity:.5}
.manual-row button,.manual-row select {min-height:34px}
.manual-services {display:flex;flex-wrap:wrap;gap:8px}
.manual-services button {flex:1 1 auto;min-height:36px}
.timer-row {border-top:1px solid var(--line);padding-top:8px}
.timer-row label {display:flex;align-items:center;justify-content:space-between;flex:1;gap:8px;flex-wrap:wrap}
#manualResult:empty,#manualState:empty {display:none}
@container (max-width:360px) {
  #manualPanel .manual-body {padding-inline:12px}
  .manual-surface {grid-template-columns:minmax(0,1fr);gap:12px}
  .motion-side {grid-template-columns:repeat(2,minmax(0,1fr));border:0;border-top:1px solid #475a4f;
    padding:8px 0 0}
  .motion-side:has(.filament-pad[hidden]) {grid-template-columns:minmax(0,1fr)}
}
#slots{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;padding:8px 13px 15px}
.slot{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:8px 2px;text-align:center;font-size:12px;overflow-wrap:anywhere}
.slot.active{border-color:var(--blue)}
.spool{margin:5px auto;width:34px;height:34px;border-radius:50%;background:radial-gradient(circle,var(--panel) 0 14%,transparent 16%),repeating-radial-gradient(circle,var(--spool) 0 2px,color-mix(in srgb,var(--spool) 70%,#555) 3px)}
.fleet-row{grid-template-columns:minmax(0,1fr) minmax(85px,.45fr) auto}
.fleet-row progress{margin-top:5px}
.printer-fields{display:grid;grid-template-columns:1fr 1fr;gap:6px;border:1px solid var(--line);border-radius:7px;margin:6px 0;padding:9px}
.printer-fields label{min-width:0}
.printer-fields input{min-width:0}
.settings input[type="checkbox"]{width:auto}
#infoBody{white-space:pre-line;overflow-wrap:anywhere;margin:12px 0}
#settingsDialog{max-height:90vh;overflow:auto}
.history-table button{padding:2px 4px;text-align:left}
@media(max-width:640px){.tabs{gap:10px}
.identity{min-width:0}
.tabs button{font-size:12px}
.mast{margin:0}
.fleet-row{grid-template-columns:minmax(0,1fr) 80px auto}
.device{margin:14px 0}
.hero .job{min-height:100px}}
</style>
</head>
<body>
<main>
  <div class="mast"><strong>FlashForge</strong><span>Device Tab</span><a id="nativePage" target="_blank" rel="noopener noreferrer" hidden>Open Printer Interface in Browser</a></div>
  <nav class="tabs" aria-label="Device navigation">
    <button id="monitorTab">Monitor</button><button id="fleetToggle">All printers</button><button id="historyTab">History</button>
    <button id="notificationsButton">Notifications</button><button id="settingsButton" aria-haspopup="dialog">Settings</button>
  </nav>
  <header class="device"><div class="identity"><h1 id="deviceName">{{printer}}</h1><span id="status" class="device-state"></span></div>
    <select id="devicePicker" aria-label="Open printer" hidden></select></header>
  <section class="panel" id="setup" hidden>
    <h2 class="panel-title">Set up your first printer</h2>
    <div class="tool-body setup">
      <ol>
        <li>Press <b>Settings</b> in the panel header.</li>
        <li>Add a printer or use <b>Edit as text</b> for a list: <code>192.168.1.50 | 1a2b3c4d | Adventurer 5M</code> —
            address | CheckCode | name. The CheckCode is in the printer's network settings.</li>
        <li>Save and wait a few seconds: temperatures and progress appear here.</li>
      </ol>
      <p class="caption">Only FlashForge printers on the local network are supported. The panel never starts a
        print or deletes files; it can only pause, resume or cancel the running job.</p>
    </div>
  </section>
  <section class="panel" id="fleet" hidden>
    <div class="row panel-title"><h2>Printers</h2><label><input type="checkbox" id="attention"> Needs attention</label><button id="fleetRefresh" type="button">Refresh</button></div>
    <div class="tool-body"><div class="fleet-list" id="fleetList"></div></div>
  </section>
  <div class="dashboard" id="monitor">
    <div class="monitor-main">
    <section class="panel hero" aria-labelledby="job-title">
      <h2 class="panel-title" id="job-title">Print Job</h2>
      <div class="job" id="job" data-preview="none">
        <div>
          <p class="filename" id="jobFile">—</p>
          <div class="progress-line" id="progressLine" hidden>
            <progress id="bar" max="100" aria-label="Print progress"></progress>
            <strong class="percent" id="percent"></strong>
          </div>
          <div class="time" id="time" hidden>
            <strong class="eta" id="remaining"></strong>
            <span class="caption">Time remaining <b id="finishTime"></b></span>
          </div>
          <div class="job-meta" id="jobMeta" hidden>
            <span id="layers"></span><span id="elapsed"></span><span id="filament"></span>
          </div>
          <p class="caption" id="timeHint" hidden></p>
        </div>
        <img class="job-image" id="jobImage" alt="Current print job preview" hidden>
      </div>
      <div class="device-actions" id="jobActions" hidden>
      <button id="pausePrint" type="button" hidden>Pause</button>
      <button id="resumePrint" type="button" hidden>Resume</button>
      <button id="cancelPrint" type="button" class="danger" hidden>Cancel</button>
      </div>
      <p id="actionStatus" role="status" class="tool-body" hidden></p>
    </section>
    <section class="panel" aria-labelledby="chart-title">
      <div class="row panel-title"><h2 id="chart-title">Temperatures · Last 30 Minutes</h2><select id="chartTool" aria-label="Chart nozzle" hidden></select></div>
      <div class="tool-body">
        <canvas class="chart" id="chart" role="img" aria-label="Chart of actual and target temperatures for the nozzle and bed"></canvas>
        <p class="caption" id="chartSample"></p><div class="chart-legend muted">
          <span class="nozzle-series" id="chartNozzle">Nozzle</span><span class="nozzle-series target" id="chartNozzleTarget">Nozzle Target</span>
          <span class="bed-series">Bed</span><span class="bed-series target">Bed Target</span>
        </div>
      </div>
    </section>
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
    </div>
    <div class="printer-sidebar" id="printerTools">
      <section class="panel readings" aria-labelledby="readouts-title">
        <div class="row panel-title"><h2 id="readouts-title">Printer Readings</h2><span class="connection" id="connection" role="status">Connecting…</span></div>
        <dl class="readouts">
          <div class="readout"><dt id="nozzleLabel">Nozzle</dt><dd><strong id="nozzle">—</strong> <span class="muted" id="nozzleTarget"></span></dd></div>
          <div class="readout"><dt>Bed</dt><dd><strong id="bed">—</strong> <span class="muted" id="bedTarget"></span></dd></div>
          <div class="readout" id="extraNozzlesReading" hidden><dt>Other tools</dt><dd id="extraNozzles"></dd></div>
          <div class="readout" id="chamberReading" hidden><dt>Chamber</dt><dd id="chamber"></dd></div>
          <div class="readout" id="fanReading" hidden><dt>Part Cooling Fan</dt><dd><strong id="fan"></strong></dd></div>
          <div class="readout" id="speedReading" hidden><dt>Print Speed</dt><dd><strong id="speed"></strong></dd></div>
        </dl>
        <div class="connection-footer">
          <div class="connection-meta"><p id="freshness"></p><p id="transport"></p></div>
          <button id="diagnose" type="button">Check connection</button>
        </div>
      </section>
      <details class="panel" id="manualPanel" hidden>
        <summary class="panel-title">Manual control <span id="manualBadge"></span></summary>
        <div class="tool-body manual-body" id="manualBody">
          <p class="caption" id="manualState"></p>
          <div class="manual-surface" data-group="motion">
            <div class="manual-xy">
              <div class="manual-bar"><strong>XY axes</strong>
                <div class="step-switch" id="jogStep" role="radiogroup" aria-label="XY step">
                  <label><input name="xyStep" type="radio" value="1" checked><span>1 mm</span></label>
                  <label><input name="xyStep" type="radio" value="10"><span>10 mm</span></label>
                </div>
              </div>
              <div class="jog-pad" tabindex="0" role="group" aria-label="XY movement. Arrow keys move 1 mm, Shift moves 10 mm.">
                <button data-jog="y+" aria-label="Move Y positive"><span>Y+</span></button>
                <button data-jog="x-" aria-label="Move X negative"><span>X−</span></button>
                <button data-jog="x+" aria-label="Move X positive"><span>X+</span></button>
                <button data-jog="y-" aria-label="Move Y negative"><span>Y−</span></button>
                <button data-manual="home"><span class="home-symbol" aria-hidden="true">⌂</span>Home</button>
              </div>
            </div>
            <div class="motion-side">
              <div class="motion-group z-row" role="group" aria-label="Z axis, millimeters">
                <strong>Z axis <small>mm</small></strong>
                <div class="motion-buttons">
                  <button data-manual="z-10">−10</button><button data-manual="z-1">−1</button>
                  <button data-manual="z+10">+10</button><button data-manual="z+1">+1</button>
                </div>
              </div>
              <div class="motion-group filament-pad" data-group="filament" role="group" aria-label="Filament, 10 millimeters">
                <strong>Filament <small>10 mm</small></strong>
                <div class="motion-buttons"><button data-manual="e+10">Feed</button><button data-manual="e-10">Retract</button></div>
              </div>
            </div>
          </div>
          <p class="caption" data-group="motion">Focus the XY pad for arrow keys: 1 mm · Shift 10 mm</p>
          <div class="manual-services" data-group="calibration"><button data-manual="level">Level bed</button>
            <button data-manual="shaper">Input shaper</button><button data-group="clear" data-manual="clear">Clear error</button></div>
          <div class="manual-row timer-row" data-group="timer"><label>Auto shutdown <select id="manualTimer"><option value="">Choose delay…</option><option value="0">Off</option>
            <option value="30">30 min</option><option value="60">60 min</option><option value="120">120 min</option></select></label>
            <button data-timer>Apply</button></div>
          <p id="manualResult" role="status" class="caption"></p>
        </div>
      </details>
      <section id="materials" class="panel" hidden><h2 class="panel-title">Materials · IFS</h2><div id="slots"></div></section>
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
          <button id="cameraSnapshot" type="button" hidden>Save snapshot</button>
          <img class="camera-image" id="cameraImage" alt="Printer camera feed" referrerpolicy="no-referrer" hidden>
        </div>
      </details>
      <section class="panel totals" aria-labelledby="totals-title">
        <h2 class="panel-title" id="totals-title">Totals</h2>
        <div class="tool-body">
          <dl class="readouts">
            <div class="readout"><dt>Jobs</dt><dd id="statJobs"></dd></div>
            <div class="readout"><dt>Print time</dt><dd id="statHours"></dd></div>
            <div class="readout"><dt>Finished cleanly</dt><dd id="statSuccess"></dd></div>
          </dl>
        </div>
      </section>
      <details class="panel" id="printerDetails">
        <summary class="panel-title">Printer details</summary>
        <div class="tool-body">
          <dl class="readouts">
            <div class="readout"><dt>Model</dt><dd id="printerModel"></dd></div>
            <div class="readout"><dt>Firmware</dt><dd id="printerFirmware"></dd></div>
            <div class="readout"><dt>Serial</dt><dd id="printerSerial"></dd></div>
            <div class="readout"><dt>MAC</dt><dd id="printerMac"></dd></div>
            <div class="readout"><dt>Build volume</dt><dd id="printerVolume"></dd></div>
            <div class="readout"><dt>Light</dt><dd id="printerLight"></dd></div>
          </dl>
          <p class="caption" id="printerSensors"></p>
        </div>
      </details>
    </div>
  </div>
  <details class="panel monitor-tools" id="history">
    <summary class="panel-title">Print History</summary>
    <div class="tool-body">
      <p class="muted" id="historyStatus" role="status">Print history is recorded while OrcaSlicer is open.</p>
      <div class="file-controls"><input type="search" id="historySearch" placeholder="Find a file" aria-label="Find a history entry"> <label><input type="checkbox" id="failedJobs"> Errors and cancellations</label></div>
      <p class="history-summary" id="historySummary"></p>
      <div class="table-wrap" id="historyTable" hidden>
        <table class="history-table">
          <thead><tr><th scope="col">File</th><th scope="col">Started</th><th scope="col">Print Time</th><th scope="col">Result</th></tr></thead>
          <tbody id="historyRows"></tbody>
        </table>
      </div>
    </div>
  </details>
</main>
<dialog class="settings" id="settingsDialog" aria-labelledby="settingsTitle">
  <form id="settingsForm">
    <h2 id="settingsTitle">Printers</h2>
    <div id="printerFields"></div><button type="button" id="addPrinter">Add printer</button>
    <details id="bulk"><summary>Edit as text</summary><label for="printersText">One printer per line: address | CheckCode | name | serial number</label>
    <textarea id="printersText" rows="5" spellcheck="false" placeholder="192.168.1.50 | 1a2b3c4d | Adventurer 5M"></textarea>
    <p class="caption">Use a JSON array of four strings when a value contains a vertical bar.</p></details>
    <p class="caption">Creator 5 and 5 Pro require both CheckCode and the printer serial number.
      Adventurer 5M reads its serial number automatically.</p>
    <p class="caption" id="settingsStatus" role="status"></p>
    <div class="settings-actions">
      <button type="button" id="settingsCancel">Cancel</button>
      <button type="submit" id="settingsSave">Save</button>
    </div>
  </form>
</dialog>
<dialog class="settings" id="infoDialog"><div class="tool-body"><h2 id="infoTitle"></h2><div id="infoBody"></div>
  <div class="settings-actions"><button id="copyReport" hidden>Copy report</button><button id="closeInfo">Close</button></div></div></dialog>
<dialog class="settings" id="notifyDialog"><form id="notifyForm"><h2>Host notifications</h2>
<label><input type="checkbox" name="finished"> Print finished</label><label><input type="checkbox" name="cancelled"> Print cancelled</label>
<label><input type="checkbox" name="error"> Printer error</label><label><input type="checkbox" name="offline"> Offline for at least one minute</label>
<p id="notifyStatus" role="status"></p><div class="settings-actions"><button type="button" id="closeNotify">Cancel</button><button type="submit">Save</button></div></form></dialog>
<script>
const el = id => document.getElementById(id);
const pageSize = 12;
let view = localStorage.getItem('ff-fleet') === '1' ? 'fleet' : 'monitor';
let lastObservation = null;
let connectionFailure = '';
let historyJobs = [];
let chartKey = 'nozzle';
const finishClock = new Intl.DateTimeFormat(undefined, {hour:'2-digit', minute:'2-digit'});
function finishAt(seconds, stamp) {
  return Number.isFinite(seconds) && Number.isFinite(stamp) ? finishClock.format((stamp + seconds) * 1000) : '';
}
let infoRevision = 0;
function showInfo(title, body, copy = false) {
  el('infoTitle').textContent = title;
  el('infoBody').textContent = body;
  el('copyReport').hidden = !copy;
  el('copyReport').textContent = 'Copy report';
  if (!el('infoDialog').open) el('infoDialog').showModal();
  return ++infoRevision;
}
el('closeInfo').onclick = () => el('infoDialog').close();
el('copyReport').onclick = async () => {
  try { await navigator.clipboard.writeText(el('infoBody').textContent); el('copyReport').textContent = 'Copied'; }
  catch { const range = document.createRange(); range.selectNodeContents(el('infoBody')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); }
};
async function postLocal(route, url = endpoint('/' + route), payload) {
  const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-FlashForge-Tab-Action':route},
    body: payload === undefined ? undefined : JSON.stringify(payload), signal:AbortSignal.timeout(15000)});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed.');
  return result;
}
async function diagnose(host) {
  const revision = showInfo('Connection check', 'Reading printer status…');
  const current = () => el('infoDialog').open && revision === infoRevision;
  try {
    const url = host ? new URL('/' + encodeURIComponent(host) + '/diagnostics', location.origin) : endpoint('/diagnostics');
    const data = await postLocal('diagnostics', url);
    if (!current()) return;
    showInfo('Connection check', ['FlashForge Device Tab 1.12.1', 'Transport: ' + data.transport, 'Status: received',
      'Authenticated readings: ' + (data.authenticated ? 'available' : 'unavailable'),
      'Camera URL: ' + (data.cameraReported ? 'reported; stream not tested' : 'not reported')].join('\n'), true);
  } catch (error) { if (current()) showInfo('Connection check', error.message, true); }
}
el('diagnose').onclick = () => diagnose();
el('notificationsButton').onclick = async () => {
  const form = el('notifyForm'), save = form.querySelector('[type=submit]');
  save.disabled = true;
  el('notifyStatus').textContent = '';
  el('notifyDialog').showModal();
  try {const {notifications} = await getJson('/settings'); for (const [key,v] of Object.entries(notifications)) form.elements[key].checked = v; save.disabled = false;}
  catch(error) {el('notifyStatus').textContent = error.message;}
};
el('closeNotify').onclick = () => el('notifyDialog').close();
el('notifyForm').onsubmit = async event => {
  event.preventDefault();
  const checks = event.target.querySelectorAll('input[name]');
  try { await postLocal('notifications', undefined, Object.fromEntries(Array.from(checks, c => [c.name,c.checked]))); el('notifyDialog').close(); }
  catch(error) {el('notifyStatus').textContent = error.message;}
};
function showSlots(slots) {
  el('materials').hidden = !slots?.length;
  el('slots').replaceChildren(...(slots || []).map(slot => {
    const box = document.createElement('div'), reel = document.createElement('div'), text = document.createElement('div');
    box.className = 'slot' + (slot.active ? ' active' : '');
    box.textContent = 'A' + slot.id;
    reel.className = 'spool';
    reel.style.setProperty('--spool', slot.present === false ? '#818681' : slot.color || '#889088');
    text.textContent = slot.present === false ? 'Empty' : slot.name || 'Unknown';
    if (slot.active && slot.present !== false) text.append(document.createElement('br'), 'Selected');
    box.append(reel,text);return box;
  }));
}
function updateAge() {
  const age = lastObservation == null ? null : Math.max(0, Math.floor(Date.now()/1000-lastObservation));
  el('freshness').textContent = age == null ? 'Waiting for the first reading' : 'Last reading: ' + age + ' s ago';
  if (age > 30 && !connectionFailure) {
    el('connection').textContent = 'Delayed';
    el('connection').className = 'connection disconnected';
    el('monitor').classList.add('stale');
    updateActions({}, true);
  }
}
setInterval(updateAge,1000);

const selectedPrinter = new URL(location.href).searchParams.get('printer');
const separateTab = location.pathname === '/flashforge/';
const nativePage = el('nativePage');
function endpoint(path) {
  const url = new URL(path.replace(/^\//, ''), location.href);
  if (selectedPrinter && !['/native','/settings','/notifications'].includes(path)) url.searchParams.set('printer',selectedPrinter);
  return url;
}
function printerPage(host) {
  const url = new URL(location.href);
  if (url.pathname === '/' + host + '/') url.searchParams.delete('printer');
  else url.searchParams.set('printer',host);
  return url;
}
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

if (!separateTab && !selectedPrinter) {
  getJson('/native').then(({url}) => {
    if (url) {
      nativePage.href = url;
      nativePage.hidden = false;
    }
  }).catch(() => {});
}

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
    [chartKey, colors.getPropertyValue('--blue').trim(), []],
    [chartKey + 'Target', colors.getPropertyValue('--blue').trim(), [5, 4]],
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

el('chartTool').onchange = () => { chartKey = el('chartTool').value; updateLegend(); drawChart(); };
function updateLegend() {
  const label = chartKey === 'nozzle' ? el('nozzleLabel').textContent : chartKey.toUpperCase();
  el('chartNozzle').textContent = label;
  el('chartNozzleTarget').textContent = label + ' Target';
}
chart.onpointermove = event => {
  const t = Date.now() - 1800000 + 1800000 * (event.offsetX - 41) / Math.max(1,chart.clientWidth-57);
  const s = temperatures.reduce((a,b) => !a || Math.abs(b.t-t)<Math.abs(a.t-t) ? b:a,null);
  el('chartSample').textContent = s && Math.abs(s.t-t)<18000 ? shortTime.format(s.t)+' · '+el('chartNozzle').textContent+' '+plain(s[chartKey])+' / '+plain(s[chartKey+'Target'])+' °C · Bed '+plain(s.bed)+' / '+plain(s.bedTarget)+' °C' : '';
};
chart.onpointerleave = () => {el('chartSample').textContent = '';};
async function updateTools() {
  try {
    const data = await getJson('/telemetry');
    temperatures = data.temperatures;
    if (Array.isArray(data.printers)) {
      fleetPrinters = data.printers;
      applyView();
    }
    if (data.stats) {
      el('statJobs').textContent = String(data.stats.jobs || 0);
      el('statHours').textContent = data.stats.seconds ? duration(data.stats.seconds) : '0 min';
      el('statSuccess').textContent = data.stats.successRate == null
        ? '—' : data.stats.successRate + ' %';
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
    historyJobs = jobs;
    el('historySummary').dataset.summary = el('historySummary').textContent;
    renderHistory();
  } catch {
    drawChart();
    el('historyStatus').textContent = 'Print history is temporarily unavailable.';
  }
}
el('historySearch').oninput = renderHistory;
el('failedJobs').onchange = renderHistory;
function renderHistory() {
    const jobs = historyJobs.filter(job => (job.file || '').toLowerCase().includes(el('historySearch').value.toLowerCase())
      && (!el('failedJobs').checked || ['error','cancelled'].includes(job.result)));
    const results = {active: 'In Progress', complete: 'Completed', cancelled: 'Canceled',
                     error: 'Error', unknown: 'Unknown Result'};
    el('historyRows').replaceChildren(...jobs.map(job => {
      const row = document.createElement('tr');
      for (const value of [job.file || 'Unknown filename', historyTime.format(job.started * 1000),
                           job.elapsed == null ? '—' : duration(job.elapsed), results[job.result]]) {
        const cell = row.insertCell();
        cell.textContent = value;
      }
      const button = document.createElement('button');
      button.textContent = row.cells[0].textContent;
      button.onclick = () => showInfo(button.textContent, [
        'Started: ' + historyTime.format(job.started * 1000),
        'Ended: ' + (Number.isSafeInteger(job.ended) && job.ended >= 0 && job.ended <= 253402300799 ? historyTime.format(job.ended * 1000) : 'Not recorded'),
        'Print time: ' + (job.elapsed == null ? '—' : duration(job.elapsed)), 'Result: ' + results[job.result],
        job.result === 'unknown' ? 'The end of this job was not observed.' : ''
      ].filter(Boolean).join('\n'));
      row.cells[0].replaceChildren(button);
      return row;
    }));
    el('historyTable').hidden = !jobs.length;
    if (!jobs.length && historyJobs.length) el('historySummary').textContent = 'No matching jobs.';
    else el('historySummary').textContent = el('historySummary').dataset.summary || '';

}

const cameraImage = el('cameraImage');
const cameraUrl = el('cameraUrl');
const cameraStatus = el('cameraStatus');
const cameraStorageKey = 'flashforge-device-tab-camera-' + (selectedPrinter ? '/' + selectedPrinter + '/' : location.pathname);
let cameraTimeout;
let managedCamera = false;
let cameraSession = null;
let cameraFrame = null;
let frameUrl = '';
let resumeCamera = false;
const resumeKey = cameraStorageKey + '-resume';
try { resumeCamera = localStorage.getItem(resumeKey) === '1'; } catch {}
try { cameraUrl.value = localStorage.getItem(cameraStorageKey) || ''; } catch {}

function rememberCamera(value) {
  resumeCamera = value;
  try { localStorage.setItem(resumeKey, value ? '1' : '0'); } catch {}
}
function stopCamera(message = '', suspend = false) {
  if (!suspend && managedCamera) rememberCamera(false);
  cameraSession?.abort();
  cameraSession = null;
  cameraFrame = null;
  clearTimeout(cameraTimeout);
  cameraImage.removeAttribute('src');
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = '';
  cameraImage.hidden = true;
  el('cameraSnapshot').hidden = true;
  el('cameraStop').hidden = true;
  el('cameraStart').disabled = false;
  cameraStatus.textContent = message;
}

// Parse JPEG segment lengths so an embedded thumbnail cannot terminate the outer image.
function* jpegFrames(chunk, state) {
  for (const byte of chunk) {
    if (state.length) {
      if (state.length === state.bytes.length) throw Error('Camera frame exceeds the size limit.');
      state.bytes[state.length++] = byte;
      if (state.sizeBytes) {
        state.size = state.size * 256 + byte;
        if (--state.sizeBytes === 0) {
          if (state.size < 2) throw Error('Invalid JPEG segment.');
          state.skip = state.size - 2;
        }
      } else if (state.skip) {
        state.skip--;
      } else if (state.marker && byte === 217) {
        yield new Blob([state.bytes.subarray(0,state.length)], {type:'image/jpeg'});
        state.length = 0;
      } else if (state.marker && byte > 1 && byte < 255 && !(byte >= 208 && byte <= 216)) {
        state.sizeBytes = 2;
        state.size = 0;
      } else {
        state.marker = byte === 255;
        continue;
      }
      state.marker = false;
    } else {
      if (state.marker && byte === 216) {
        state.bytes.set([255,216]);
        state.length = 2;
      }
      state.marker = byte === 255;
    }
  }
}

async function startManagedCamera() {
  if (cameraSession) return;
  const session = new AbortController();
  cameraSession = session;
  rememberCamera(true);
  el('cameraStart').disabled = true;
  el('cameraStop').hidden = false;
  cameraStatus.textContent = 'Opening Creator 5 Pro camera…';
  const frameTimeout = () => stopCamera('No camera frame received. Connect to try again.');
  cameraTimeout = setTimeout(frameTimeout, 20000);
  try {
    const response = await fetch(endpoint('/camera-stream'), {method:'POST', signal:session.signal,
      headers:{'X-FlashForge-Tab-Action':'camera-stream'}});
    if (!response.ok) throw Error((await response.json()).error);
    const reader = response.body.getReader();
    // The proxy forwards original MJPEG chunks. JPEG markers may straddle two reads.
    const state = {bytes:new Uint8Array(2000000), length:0, marker:false, sizeBytes:0, skip:0};
    while (cameraSession === session) {
      const {done, value} = await reader.read();
      if (done) throw Error('Camera stream ended. Connect to try again.');
      for (const blob of jpegFrames(value, state)) {
        const url = URL.createObjectURL(blob);
        const decoded = new Image();
        decoded.src = url;
        try { await decoded.decode(); } catch (error) { URL.revokeObjectURL(url); throw error; }
        if (cameraSession !== session) { URL.revokeObjectURL(url); return; }
        cameraImage.src = url;
        if (frameUrl) URL.revokeObjectURL(frameUrl);
        frameUrl = url;
        cameraFrame = blob;
        cameraImage.hidden = false;
        el('cameraSnapshot').hidden = false;
        cameraStatus.textContent = '';
        clearTimeout(cameraTimeout);
        cameraTimeout = setTimeout(frameTimeout, 10000);
      }
    }
  } catch (error) {
    if (cameraSession === session) stopCamera(error.message || 'Could not open the camera.');
  }
}
function syncCameraView() {
  if (!managedCamera) return;
  if (document.hidden || el('printerTools').hidden || !el('camera').open) {
    if (cameraSession) stopCamera('', true);
  } else if (resumeCamera) startManagedCamera();
}
document.addEventListener('visibilitychange', syncCameraView);
window.addEventListener('pagehide', () => stopCamera('', true));
window.addEventListener('pageshow', syncCameraView);
el('cameraForm').onsubmit = event => {
  event.preventDefault();
  if (managedCamera) { startManagedCamera(); return; }
  let url;
  try { url = new URL(cameraUrl.value.trim()); } catch {}
  if (!url || !['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    stopCamera('Enter an HTTP or HTTPS MJPEG URL without a username or password.');
    return;
  }
  try { localStorage.setItem(cameraStorageKey, url.href); } catch {}
  cameraImage.src = url.href;
  cameraImage.hidden = false;
  el('cameraSnapshot').hidden = false;
  el('cameraStart').disabled = true;
  el('cameraStop').hidden = false;
  cameraStatus.textContent = 'Connecting…';
  cameraTimeout = setTimeout(() => stopCamera('No frame received. Check the URL and confirm the stream uses MJPEG.'), 12000);
};
cameraImage.onload = () => {
  if (managedCamera) return;
  clearTimeout(cameraTimeout);
  cameraStatus.textContent = '';
};
cameraImage.onerror = () => {
  if (cameraImage.hasAttribute('src')) stopCamera('Could not open the MJPEG stream. Check the camera URL.');
};
el('cameraStop').onclick = () => stopCamera();
for (const id of ['camera','printerDetails']) {
  const node = el(id), key='ff-layout:'+(selectedPrinter || location.pathname)+':'+id;
  try {node.open=localStorage.getItem(key)==='open';} catch {}
  node.ontoggle=()=>{ try {localStorage.setItem(key,node.open?'open':'closed');} catch {}
    if (id==='camera') { if (!node.open) stopCamera('', true); else syncCameraView(); } };
}
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
  const finish = data.state === 'PAUSED' ? '' : finishAt(data.remaining, data.observedAt);
  el('finishTime').textContent = finish ? ' · Finish ≈ ' + finish : '';
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
    connectionFailure = '';
    lastObservation = data.observedAt;
    el('transport').textContent = data.transport || '';
    showSlots(data.materials);
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
    const tools = data.tools || [];
    el('nozzleLabel').textContent = tools.length ? 'T1' : 'Nozzle';
    const picker = el('chartTool');
    if (picker.options.length !== tools.length) {
      picker.replaceChildren(...tools.map((_,i) => new Option('T'+(i+1),'t'+(i+1))));
      chartKey = tools.length ? picker.value : 'nozzle';
    }
    picker.hidden = tools.length < 2;
    updateLegend();
    const temperatures = (actual, target) => [actual, target]
      .map(value => value == null ? '—' : celsius(value)).join(' / ');
    el('extraNozzlesReading').hidden = tools.length < 2;
    el('extraNozzles').textContent = tools.slice(1).map((tool, index) =>
      'T' + (index + 2) + ' ' + temperatures(tool.actual, tool.target)).join(' · ');
    el('chamberReading').hidden = data.chamber == null;
    el('chamber').textContent = data.chamber == null ? ''
      : temperatures(data.chamber.actual, data.chamber.target);
    for (const key of ['fan', 'speed']) {
      el(key + 'Reading').hidden = data[key] == null;
      el(key).textContent = data[key] == null ? '' : data[key] + ' %';
    }
    updateJob(data);
    managedCamera = Boolean(data.managedCamera);
    cameraUrl.hidden = managedCamera;
    cameraUrl.disabled = managedCamera;
    document.querySelector('label[for="cameraUrl"]').hidden = managedCamera;
    syncCameraView();
    updatePrinter(data);
    updateActions(data);
    updateAge();
    if (!catalogLoaded) refreshFiles();
  } catch (error) {
    connection.className = 'connection disconnected';
    connectionFailure = error.message;
    connection.textContent = error.message === 'Failed to fetch' ? 'Disconnected' : error.message;
    el('monitor').classList.add('stale');
    el('status').hidden = false;
    el('status').textContent = error.status === 409 ? 'Profile changed' : 'Monitoring paused';
    updateActions({}, error.status !== 409);
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

function updatePrinter(data) {
  const info = data.info || {};
  const sensors = data.sensors || {};
  const set = (id, value) => { const node = el(id); if (node) node.textContent = value || ''; };
  set('printerModel', info.model);
  set('printerFirmware', info.firmware);
  set('printerSerial', info.serial);
  set('printerMac', info.mac);
  set('printerVolume', info.volume ? info.volume.join(' x ') + ' mm' : '');
  set('printerLight', sensors.led == null ? '' : sensors.led ? 'On' : 'Off');
  const ends = sensors.endstops || {};
  const parts = Object.entries(ends).map(([axis, value]) => axis.toUpperCase() + ' ' + value);
  if (sensors.door) parts.push('Door / lid: ' + sensors.door);
  if (sensors.errorCode) parts.push('Error: ' + sensors.errorCode);
  set('printerSensors', (Object.keys(ends).length ? 'Endstops: ' : '') + parts.join('  '));
}

let manual = null, manualPending = false;
const manualBody = el('manualBody');
function manualKey(button) {
  if (button.dataset.jog) return button.dataset.jog + el('jogStep').querySelector(':checked').value;
  if (button.hasAttribute('data-timer')) return 'timer' + el('manualTimer').value;
  return button.dataset.manual;
}
function renderManual() {
  el('manualPanel').hidden = !manual;
  for (const node of manualBody.querySelectorAll('[data-group]'))
    node.hidden = !manual?.groups.includes(node.dataset.group);
  for (const button of manualBody.querySelectorAll('button'))
    button.disabled = manualPending || !manual?.allowed.includes(manualKey(button));
  const [status, ...details] = (manual?.description || '').split(' · ');
  const badge = el('manualBadge');
  badge.textContent = manualPending ? 'Sending…' : status;
  badge.dataset.ready = String(status === 'Ready' && !manualPending);
  el('manualState').textContent = details.join(' · ');
}
async function sendManual(key) {
  if (manualPending || !manual?.allowed.includes(key)) return;
  manualPending = true;
  renderManual();
  try {
    const data = await postLocal('action', endpoint('/action'), {action:'manual/' + key});
    el('manualResult').textContent = data.state;
  } catch (error) {
    el('manualResult').textContent = error.message;
  } finally {
    await updateStatus();
    manualPending = false;
    renderManual();
  }
}
manualBody.onclick = event => {
  const button = event.target.closest('button');
  if (button) sendManual(manualKey(button));
};
el('jogStep').onchange = el('manualTimer').onchange = renderManual;
manualBody.onkeydown = event => {
  if (!event.target.matches('.jog-pad') || event.altKey || event.ctrlKey || event.metaKey) return;
  const axis = {ArrowLeft:'x-',ArrowRight:'x+',ArrowDown:'y-',ArrowUp:'y+'}[event.key];
  if (!axis) return;
  event.preventDefault();
  if (!event.repeat) sendManual(axis + (event.shiftKey ? '10' : '1'));
};
function updateActions(data, preserveManual = false) {
  manual = preserveManual && manual
    ? {...manual, allowed: [], description: 'Unavailable · Waiting for current printer readings'}
    : data.manual || null;
  renderManual();
  const state = data.state || '';
  const paused = data.jobControls && state === 'PAUSED';
  const running = data.jobControls && data.active && !paused;
  el('pausePrint').hidden = !running;
  el('resumePrint').hidden = !paused;
  el('cancelPrint').hidden = !(running || paused);
  const cancel = el('cancelPrint');
  if (cancel.classList.contains('armed') && (!data.jobControls || !data.active || cancel.dataset.job !== data.file)) {
    cancel.classList.remove('armed');
    cancel.textContent = 'Cancel';
  }
}

async function runAction(action) {
  const status = el('actionStatus');
  const buttons = el('jobActions').querySelectorAll('button');
  if (Array.from(buttons).some(button => button.disabled)) return;
  for (const button of buttons) button.disabled = true;
  status.hidden = false;
  status.textContent = 'Checking the result of ' + action + '…';
  try {
    const response = await fetch(endpoint('/action'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-FlashForge-Tab-Action': 'action'},
      body: JSON.stringify({action}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'The printer did not accept the command.');
    status.textContent = 'Printer confirmed ' + action + ' · ' + data.state;
    await updateStatus();
  } catch (error) {
    status.textContent = error.message || 'Command result unavailable.';
  } finally {
    for (const button of buttons) button.disabled = false;
  }
}

async function downloadSnapshot() {
  try {
    let blob = cameraFrame;
    if (!blob) {
      if (managedCamera) return;
      const response = await fetch(endpoint('/snapshot'));
      if (!response.ok) throw new Error('No snapshot');
      blob = await response.blob();
    }
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'printer-snapshot.jpg';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  } catch {
    el('cameraStatus').textContent = 'Could not read a snapshot from the printer.';
  }
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
    let parts;
    if (line.trimStart().startsWith('[')) {
      try { parts = JSON.parse(line); } catch { throw Error('Invalid JSON printer row.'); }
      if (!Array.isArray(parts) || parts.length !== 4 || !parts.every(p => typeof p === 'string'))
        throw Error('A JSON printer row needs four strings: address, CheckCode, name and serial.');
    } else {
      parts = line.split('|').map(part => part.trim());
      if (parts.length > 4) throw Error('Use a JSON array for values containing a vertical bar.');
    }
    if (!parts[0]) continue;
    printers.push({host: parts[0], code: parts[1] || '', name: parts[2] || '', serial: parts[3] || ''});
  }
  return printers;
}

function formatPrinters(printers) {
  return (printers || []).map(item => {
    const parts = [item.host, item.code || '', item.name || '', item.serial || ''];
    return parts.some(p => /[|\r\n]/.test(p)) ? JSON.stringify(parts) : parts.join(' | ').replace(/(?: \| )+$/, '');
  }).join('\n');
}

const fleetToggle = el('fleetToggle');
let fleetPrinters = [];

function renderFleet() {
  const visible = fleetPrinters.filter(item => !el('attention').checked || item.error || ['PAUSED','PAUSING','ERROR'].includes(item.state));
  const rows = visible.map(item => {
    const row = document.createElement('div');
    row.className = 'fleet-row' + (item.current ? ' current' : '');
    const left = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = item.name || item.host;
    const meta = document.createElement('div');
    meta.className = 'meta';
    const state = item.status || (item.error ? 'No answer' : 'Waiting…');

    const bits = [state];
    if (item.file) bits.push(item.file);
    const finish = item.state === 'PAUSED' ? '' : finishAt(item.remaining,item.observedAt);
    if (finish) bits.push('Finish ≈ ' + finish);
    meta.textContent = bits.join(' · ');
    left.append(name, meta);
    const right = document.createElement('div');
    right.className = 'value';
    if (item.current) {
      const open = document.createElement('button'); open.textContent = 'Open'; open.onclick = () => switchTab('monitor'); right.append(open);
    } else {
      const link = document.createElement('a');
      link.href = printerPage(item.host);
      link.onclick = () => {try {localStorage.setItem('ff-fleet','0');} catch {}};
      link.textContent = 'Open';
      right.append(link);
    }
    const progress = document.createElement('div');
    if (item.percent != null) {
      progress.textContent = item.percent + '%';
      const bar = document.createElement('progress');bar.max = 100;bar.value = item.percent;progress.append(bar);
    }
    row.append(left, progress, right);
    return row;
  });
  el('fleetList').replaceChildren(...rows);
  if (!rows.length) el('fleetList').textContent = 'No printers need attention.';
}

function applyView() {
  const hasPrinters = fleetPrinters.length > 0;
  const showFleet = hasPrinters && view === 'fleet';
  const showMonitor = hasPrinters && view === 'monitor';
  const current = fleetPrinters.find(item => item.current);
  const name = current?.name || 'FlashForge';
  el('deviceName').textContent = showFleet ? 'All printers' : view === 'history' ? 'Print history · ' + name : name;
  el('status').hidden = !showMonitor;
  el('setup').hidden = hasPrinters;
  el('fleet').hidden = !showFleet;
  el('history').hidden = !hasPrinters || view !== 'history';
  if (!el('history').hidden) el('history').open = true;
  for (const id of ['monitor','printerTools','files-section','jobActions']) el(id).hidden = !showMonitor;
  syncCameraView();
  for (const [id,key] of [['monitorTab','monitor'],['fleetToggle','fleet'],['historyTab','history']])
    el(id).setAttribute('aria-current',view === key ? 'page' : 'false');
  const picker=el('devicePicker');
  const options = JSON.stringify(fleetPrinters.map(p=>[p.name,p.host,p.current]));
  if (picker.dataset.options !== options) {
    picker.replaceChildren(...fleetPrinters.map(p=>new Option(p.name||p.host,p.host,false,p.current)));
    picker.dataset.options = options;
  }
  picker.hidden = !showMonitor || fleetPrinters.length < 2;
  if (showFleet) renderFleet();
}
function switchTab(next) {
  view = next;
  try {localStorage.setItem('ff-fleet',next === 'fleet' ? '1':'0');} catch {}
  applyView();drawChart();
}
el('devicePicker').onchange = event => {location.href = printerPage(event.target.value);};
el('monitorTab').onclick = () => switchTab('monitor');
el('historyTab').onclick = () => switchTab('history');
fleetToggle.onclick = () => switchTab('fleet');
el('attention').onchange = renderFleet;
el('fleetRefresh').onclick = () => updateStatus();
el('pausePrint').onclick = () => runAction('pause');
el('resumePrint').onclick = () => runAction('resume');
el('cancelPrint').onclick = () => {
  const button = el('cancelPrint');
  if (!button.classList.contains('armed')) {
    button.classList.add('armed');
    button.dataset.job = jobFile;
    button.textContent = 'Cancel print?';
    setTimeout(() => {
      if (button.classList.contains('armed')) {
        button.classList.remove('armed');
        button.textContent = 'Cancel';
      }
    }, 5000);
    return;
  }
  button.classList.remove('armed');
  button.textContent = 'Cancel';
  runAction('cancel');
};
el('cameraSnapshot').onclick = downloadSnapshot;

function printerRow(entry = {}) {
  const row = document.createElement('fieldset');
  row.className = 'printer-fields';
  for (const [key,title] of [['name','Name'],['host','IPv4 address'],['serial','Serial number'],['code','CheckCode']]) {
    const label = document.createElement('label'), input = document.createElement('input');
    label.textContent = title; input.dataset.key = key; input.value = entry[key] || '';
    input.type = key === 'code' ? 'password' : 'text';input.autocomplete = 'off';
    if (key === 'host') input.required = true;
    label.append(input);row.append(label);
  }
  const check=document.createElement('button'), remove=document.createElement('button');
  check.type=remove.type='button';check.textContent='Check saved connection';remove.textContent='Remove';
  check.disabled=!entry.host;
  row.oninput=()=>{check.disabled=true;};
  check.onclick=()=>diagnose(entry.host);remove.onclick=()=>row.remove();
  row.append(check,remove);return row;
}
function fieldPrinters() {
  return Array.from(el('printerFields').children,row => Object.fromEntries(
    Array.from(row.querySelectorAll('input'), input=>[input.dataset.key,input.value.trim()])));
}
function setRows(entries) {el('printerFields').replaceChildren(...entries.map(printerRow));}
el('addPrinter').onclick=()=>el('printerFields').append(printerRow());
let bulkActive=false;
el('bulk').ontoggle=()=>{
  const open=el('bulk').open;
  if (open === bulkActive) return;
  if(open) el('printersText').value=formatPrinters(fieldPrinters());
  else {
    try { setRows(parsePrinters(el('printersText').value)); }
    catch (error) {
      el('settingsStatus').textContent = error.message;
      el('bulk').open = true;
      return;
    }
  }
  bulkActive=open;
  el('settingsStatus').textContent = '';
  el('printerFields').hidden=open;el('addPrinter').hidden=open;
  for(const field of el('printerFields').querySelectorAll('input')) field.disabled=open;
};
el('settingsButton').onclick = async () => {
  el('settingsStatus').textContent = '';
  el('settingsSave').disabled=true;
  el('printerFields').replaceChildren();
  el('printersText').value='';
  settingsDialog.showModal();
  try {
    const data = await getJson('/settings');
    el('printersText').value = formatPrinters(data.printers);
    setRows(data.printers.length ? data.printers : [{}]);
    if (el('bulk').open) for (const field of el('printerFields').querySelectorAll('input')) field.disabled=true;
    el('settingsSave').disabled=false;
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
    const printers = el('bulk').open ? parsePrinters(el('printersText').value) : fieldPrinters();
    const response = await fetch(endpoint('/settings'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-FlashForge-Tab-Action': 'settings'},
      body: JSON.stringify({printers}),
      signal: AbortSignal.timeout(12000),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Could not save the settings.');
    settingsDialog.close();
    if (!separateTab && data.moved && !printers.some(p => location.pathname === '/' + p.host + '/')) {
      location.assign('/' + encodeURIComponent(data.endpoint) + '/');
    } else if (selectedPrinter && !printers.some(p => p.host === selectedPrinter)) {
      location.assign(location.pathname);
    } else await updateStatus();
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
input, select, button { font: inherit; padding: 6px 8px; }
p { max-width: 520px; }
</style>
<div>
  <label for="tabPlacement">Show monitor in</label>
  <select id="tabPlacement" aria-describedby="placementHelp"><option value="device">Printer tab</option><option value="flashforge">FlashForge tab</option></select>
  <p id="placementHelp">Restart OrcaSlicer to switch tabs.</p>
  <label for="flashforgeHost">Printer IPv4 address</label>
  <input type="text" id="flashforgeHost" placeholder="192.168.1.51" maxlength="15" autocomplete="off">
  <label for="code">CheckCode</label>
  <input type="password" id="code" maxlength="32" autocomplete="off">
  <label for="serial">Serial number (required for Creator 5 / 5 Pro)</label>
  <input type="text" id="serial" maxlength="64" autocomplete="off">
  <p>Allow OrcaSlicer to access your printer when prompted, then reload the plugin.</p>
  <button id="save" type="button" disabled>Save</button>
  <span id="saved" role="status"></span>
</div>
<script>
const byId = id => document.getElementById(id);
const fields = {flashforge_host: byId('flashforgeHost'), check_code: byId('code'), serial_number: byId('serial')};
const placement = byId('tabPlacement');
const save = byId('save');
const saved = byId('saved');
const activePlacement = '{{placement}}';
let printers = [];
window.orca.onConfig(config => {
  printers = Array.isArray(config.printers) ? config.printers : [];
  placement.value = config.placement === 'flashforge' ? 'flashforge' : 'device';
  const first = printers[0];
  fields.flashforge_host.value = first?.host || config.flashforge_host || '';
  fields.check_code.value = first?.code ?? config.check_code ?? '';
  fields.serial_number.value = first?.serial ?? config.serial_number ?? '';
  saved.textContent = placement.value !== activePlacement ? 'Saved. Restart OrcaSlicer to apply.' : '';
  save.disabled = false;
});
for (const field of [placement, ...Object.values(fields)]) field.oninput = () => { saved.textContent = ''; };
save.onclick = () => {
  const config = Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value.trim()]));
  config.placement = placement.value;
  if (printers.length) {
    config.printers = [{...printers[0], host: config.flashforge_host, code: config.check_code,
      serial: config.serial_number}, ...printers.slice(1)];
  }
  window.orca.saveConfig(config);
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
TEMP_ACK = re.compile(rb"(?:^|\n)ok[ \t]+T0:[^\r\n]+\r?\n")
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
        if request == b"M105":
            inline = TEMP_ACK.search(data)
            if inline:
                return bytes(data[:inline.end()]), bytearray(data[inline.end():])
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


def history_stats(jobs, now):
    finished = [job for job in jobs
                if job.get("result") in ("complete", "cancelled", "error")]
    seconds = 0
    for job in jobs:
        elapsed = job.get("elapsed")
        if not isinstance(elapsed, int) or elapsed < 0:
            continue
        seconds += elapsed if job.get("result") != "active" else max(0, now - job.get("started", now))
    complete = sum(1 for job in finished if job.get("result") == "complete")
    return {
        "jobs": len(jobs),
        "finished": len(finished),
        "complete": complete,
        "successRate": round(complete * 100 / len(finished)) if finished else None,
        "seconds": seconds,
        "last": jobs[0].get("file", "") if jobs else "",
    }


class PrinterAPIError(ValueError):
    pass


def printer_api(host, serial, check_code, path, fields=None, timeout=3, limit=65536):
    body = json.dumps({"serialNumber": serial, "checkCode": check_code, **(fields or {})})
    connection = http.client.HTTPConnection(host, API_PORT, timeout=timeout)
    try:
        connection.request("POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise PrinterAPIError(f"The printer HTTP API returned status {response.status}.")
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise PrinterAPIError("The printer HTTP response is too large.")
        result = json.loads(raw)
    except (ValueError, RecursionError) as error:
        if isinstance(error, PrinterAPIError):
            raise
        raise PrinterAPIError("The printer returned invalid HTTP data.") from error
    except http.client.HTTPException as error:
        raise OSError("The printer HTTP connection failed.") from error
    finally:
        connection.close()

    if not isinstance(result, dict) or type(result.get("code")) is not int:
        raise PrinterAPIError("The printer returned invalid HTTP data.")
    code = result["code"]
    message = result.get("message")
    if code == -2:
        raise PrinterAPIError("Enable LAN-only mode on the printer.")
    if code == 1:
        if message == "SN is different":
            raise PrinterAPIError("Check the printer serial number in the plugin settings.")
        if message == "Access code is different":
            raise PrinterAPIError("Check the CheckCode in the plugin settings.")
        raise PrinterAPIError("Check the serial number and CheckCode in the plugin settings.")
    if code:
        raise PrinterAPIError(f"The printer rejected the HTTP request (code {code}).")
    return result


def control_job(host, serial, check_code, action):
    """Pause, resume or cancel the current job through the printer's HTTP API."""
    printer_api(host, serial, check_code, "/control",
                {"payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}})


def read_snapshot(camera_url, limit=2_000_000):
    """One JPEG frame from the printer's MJPEG stream."""
    parts = urlsplit(camera_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=3)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            return None
        buffer = b""
        while len(buffer) < limit:
            block = response.read(8192)
            if not block:
                break
            buffer += block
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                buffer = buffer[-2:]
                continue
            end = buffer.find(b"\xff\xd9", start + 2)
            if end > 0:
                return buffer[start:end + 2]
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    return None


def read_job_detail(serial, check_code, host):
    try:
        return read_http_detail(host, serial, check_code), ""
    except PrinterAPIError as error:
        return {}, str(error)
    except OSError:
        return {}, "The printer API is unavailable"


def read_http_detail(host, serial, check_code):
    detail = printer_api(host, serial, check_code, "/detail", timeout=1.5).get("detail")
    if not isinstance(detail, dict):
        raise PrinterAPIError("The printer returned invalid status data.")
    return detail


def detail_number(detail, key, lower, upper):
    if isinstance(detail.get(key), bool):
        return None
    try:
        value = float(detail[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return value if lower <= value <= upper else None


def material_slots(detail):
    if detail.get("hasMatlStation") in (0, "0"):
        return []
    station = detail.get("matlStationInfo")
    if not isinstance(station, dict) or station.get("slotCnt") != 4:
        return []
    entries = station.get("slotInfos")
    if not isinstance(entries, list):
        return []
    slots = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slotId")
        if type(slot) is not int or not 1 <= slot <= 4:
            continue
        color = entry.get("materialColor")
        material = entry.get("materialName")
        present = entry.get("hasFilament")
        slots[slot] = {"id": slot, "present": present if type(present) is bool else None,
                       "name": material[:48] if isinstance(material, str) else "",
                       "color": color if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "",
                       "active": type(station.get("currentSlot")) is int and station["currentSlot"] == slot}
    return [slots.get(i, {"id": i}) for i in range(1, 5)] if slots else []


def manual_family(model):
    name = re.sub(r"[\s_-]+", "", str(model).casefold()).removeprefix("flashforge")
    if name in {"ad5x", "adventurer5x"}:
        return "ad5x"
    if name in {"adventurer5m", "adventurer5mpro", "ad5m", "ad5mpro"}:
        return "5m"
    if name in {"creator5", "creator5pro"}:
        return "creator"
    if name in {"adventureriii", "adventurer3", "adventurer3lite", "adventurer3pro",
                "adventurer3pro2", "adventurer4", "adventurer4pro", "adventurer4lite"}:
        return "legacy"
    return None


def manual_controls(detail, state, tcp_model="", nozzle=None):
    tcp = manual_family(tcp_model)
    pid = detail.get("pid")
    if pid is None:
        family = manual_family(detail.get("model") or tcp_model)
    else:
        if isinstance(pid, str) and re.fullmatch(r"(?:0x)?[0-9a-f]{1,4}", pid.lower()):
            pid = int(pid, 16)
        family = {35:"5m", 36:"5m", 38:"ad5x", 40:"creator", 41:"creator"}.get(pid) if type(pid) is int else None
    if not family or (tcp and tcp != family) or (family == "legacy" and tcp != family):
        return None
    legacy = family == "legacy"
    if not legacy and not detail:
        return None
    idle = state in {"READY", "IDLE"} and (legacy or str(detail.get("status", "")).upper() in {"READY", "IDLE"})
    error = detail.get("errorCode")
    tcp_motion = tcp == family and family in {"legacy", "5m"}
    motion = tcp_motion or family == "ad5x"
    filament = tcp_motion or (family == "ad5x" and detail.get("extrudeCtrl") == 1)
    hot = (nozzle if nozzle is not None else detail_number(detail, "rightTemp", 0, 500)) or 0
    groups = []
    if motion:
        groups.append("motion")
    if filament:
        groups.append("filament")
    if not legacy:
        groups.append("calibration")
    if family == "ad5x":
        groups.append("clear")
    if not legacy and detail.get("autoShutdown") in ("open", "close"):
        groups.append("timer")
    allowed = []
    if idle:
        if isinstance(error, str) and error.strip():
            if family == "ad5x":
                allowed.append("clear")
        else:
            if motion:
                allowed.append("home")
            if not legacy:
                allowed.extend(("level", "shaper"))
            if tcp_motion or (family == "ad5x" and detail.get("moveCtrl") == 1):
                allowed.extend(f"{axis}{delta:+d}" for axis in "xyz" for delta in (-10, -1, 1, 10))
            if filament and hot >= 180:
                allowed.extend(("e+10", "e-10"))
        if "timer" in groups:
            allowed.extend(("timer0", "timer30", "timer60", "timer120"))
    timer = detail.get("autoShutdown")
    notes = ["Ready" if idle else "Locked · printer must be idle"]
    if idle and error:
        notes.append(str(error))
    if idle and filament and "e+10" not in allowed and not error:
        notes.append("Filament feed requires a nozzle temperature of at least 180 °C")
    if timer == "close":
        notes.append("Auto shutdown off")
    elif timer == "open":
        notes.append(f"Auto shutdown on · {detail.get('autoShutdownTime', '—')} min")
    return {"allowed": allowed, "groups": groups, "tcpMotion": tcp_motion, "description": " · ".join(notes), "errorCode": error}


def tcp_manual_move(host, key, volume):
    """Keep the idle check and the single move in the same printer control session."""
    with socket.create_connection((host, PORT), timeout=2) as sock:
        def send(code):
            reply = command(sock, code)
            if re.search(r"\b(?:error|failed|denied|unknown)\b", reply, re.I):
                raise ValueError("Printer rejected " + code.split()[0] + ".")
            return reply

        if "control success" not in send("M601 S1").casefold():
            raise ValueError("Printer did not grant control.")
        try:
            if not re.search(r"^MachineStatus:\s*(?:READY|IDLE)\s*$", send("M119"), re.M | re.I):
                raise ValueError("Printer must be idle.")
            if key == "home":
                send("G28")
            elif key.startswith("e"):
                temp = re.search(r"\bT0:\s*(\d+(?:\.\d+)?)\s*/", send("M105"))
                if not temp or float(temp[1]) < 180:
                    raise ValueError("Heat the nozzle to at least 180 °C.")
                send(f"G1 E{int(key[1:])} F120")
            else:
                axis = key[0].upper()
                pos = re.search(r"\b" + axis + r":\s*(-?\d+(?:\.\d+)?)(?=\s|$)", send("M114"))
                if not pos:
                    raise ValueError("Printer did not report the axis position.")
                target = float(pos[1]) + int(key[1:])
                if volume and not 0 <= target <= volume["XYZ".index(axis)]:
                    raise ValueError("Move exceeds the reported build volume.")
                send("G90")
                send(f"G1 {axis}{target:.3f} F300")
        finally:
            send("M602")
    return "Move sent to the printer."


def manual_payload(key, controls):
    if not controls or key not in controls["allowed"]:
        raise ValueError("Command unavailable for the current printer state.")
    if key == "clear":
        return {"cmd": "errorCodeCtrl_cmd", "args": {"action": "clearErrorCode", "errorCode": controls["errorCode"]}}
    if key == "home":
        return {"cmd": "homingCtrl_cmd"}
    if key in ("level", "shaper"):
        return {"cmd": "calibration_cmd", "args": {"levelingDetection": "open" if key == "level" else "close",
                                                   "vibrationCompensation": "open" if key == "shaper" else "close"}}
    if key.startswith("timer"):
        minutes = int(key[5:])
        return {"cmd": "delayClose_cmd", "args": {"automaticShutdown": "open" if minutes else "close",
                                                  "shutdownAfterTime": minutes}}
    return {"cmd": "extrudeCtrl_cmd" if key[0] == "e" else "moveCtrl_cmd",
            "args": {"axis": key[0], "delta": int(key[1:])}}


def read_http_status(host, serial, check_code):
    detail = read_http_detail(host, serial, check_code)

    def field(key):
        value = detail.get(key)
        return value.strip() if isinstance(value, str) else ""

    state = field("status").upper()
    state = {"PAUSE": "PAUSED", "CANCEL": "CANCELING", "CALIBRATE_DOING": "BUSY"}.get(state, state)
    if not state:
        raise PrinterAPIError("The printer returned invalid status data.")
    complete = state in {"COMPLETED", "BUILDING_COMPLETED", "FINISHED"}
    active = state in ACTIVE_JOB
    has_job = active or complete or state in ENDED_JOB
    model = field("model")
    pid = detail.get("pid")
    creator_pro = model.casefold() == "creator 5 pro" or pid in (41, "29", "0x29")
    creator = creator_pro or model.casefold() == "creator 5" or pid in (40, "28", "0x28")
    volume = re.fullmatch(r"(\d{1,4})[xX](\d{1,4})[xX](\d{1,4})", field("measure"))

    def tool_values(key):
        values = detail.get(key)
        if not isinstance(values, list):
            return []
        return [detail_number({index: value}, index, 0, 500)
                for index, value in enumerate(values[:4])]

    actuals = tool_values("nozzleTemps")
    targets = tool_values("nozzleTargetTemps")
    tools = [{"actual": actual, "target": target}
             for actual, target in zip((actuals + [None] * 4)[:4],
                                       (targets + [None] * 4)[:4])] if creator or actuals or targets else []
    readings = {
        "nozzle": tools[0]["actual"] if tools else detail_number(detail, "rightTemp", 0, 500),
        "nozzleTarget": tools[0]["target"] if tools else detail_number(detail, "rightTargetTemp", 0, 500),
        "bed": detail_number(detail, "platTemp", 0, 200),
        "bedTarget": detail_number(detail, "platTargetTemp", 0, 200),
    }
    chamber = {"actual": detail_number(detail, "chamberTemp", 0, 150),
               "target": detail_number(detail, "chamberTargetTemp", 0, 150)}
    camera = field("cameraStreamUrl")
    try:
        parts = urlsplit(camera)
        if parts.scheme != "http" or parts.hostname != host or parts.port != 8080 or parts.username is not None:
            camera = ""
    except ValueError:
        camera = ""
    led = field("lightStatus")
    door = field("doorStatus")
    remaining = detail_number(detail, "estimatedTime", 1e-9, 365 * 24 * 3600) if active else None
    elapsed = detail_number(detail, "printDuration", 1 if complete else 0, 365 * 24 * 3600) if has_job else None
    progress = detail_number(detail, "printProgress", 0, 1) if active else None
    total = detail_number(detail, "targetPrintLayer", 1, 1_000_000) if has_job else None
    layer = detail_number(detail, "printLayer", 0, total) if total is not None else None
    return {
        "status": STATES.get(state, state), "state": state, **readings,
        "temperatures": readings, "tools": tools, "jobControls": True,
        "materials": material_slots(detail), "transport": "HTTP",
        "chamber": chamber if creator_pro and any(value is not None for value in chamber.values()) else None,
        "layer": int(layer) if layer is not None else None,
        "layerTotal": int(total) if total is not None else None,
        "percent": 100 if complete else round(progress * 100) if progress is not None else None,
        "file": field("printFileName").rsplit("/", 1)[-1] if has_job else "",
        "active": active, "complete": complete,
        "remaining": math.ceil(remaining) if remaining is not None else None,
        "timeHint": "The printer has not reported the remaining time yet" if active and remaining is None else "",
        "elapsed": int(elapsed) if elapsed is not None else None,
        "filament": None,
        "fan": detail_number(detail, "coolingFanSpeed", 0, 100),
        "speed": detail_number(detail, "printSpeedAdjust", 1, 1000),
        "cameraUrl": camera, "managedCamera": creator_pro, "manual": manual_controls(detail, state),
        "info": {"model": model, "name": field("name"), "firmware": field("firmwareVersion"),
                 "serial": serial, "mac": field("macAddr"), "volume": list(map(int, volume.groups())) if volume else None,
                 "tools": detail_number(detail, "nozzleCnt", 1, 16)},
        "sensors": {"led": led == "open" if led in {"open", "close"} else None,
                    "door": door if creator_pro and door in {"open", "close"} else "",
                    "errorCode": field("errorCode"), "endstops": {}, "moveMode": ""},
    }


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
        machine = ""
        try:
            machine = command(sock, "M115", deadline)
        except (OSError, ValueError):
            pass
        if machine:
            serial = re.search(r"^(?:SN|Serial Number):[ \t]*([\w-]+)", machine, re.M | re.I)

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
    if active and percent is None:
        # Older firmware exposes progress through M27 without a layer count.
        sd = re.search(r"^SD printing byte[ \t]+(\d{1,12})/(\d{1,12})\s*$", progress, re.M | re.I)
        if sd:
            done, size = map(int, sd.groups())
            if 0 <= done <= size and size:
                percent = round(done * 100 / size)
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
    info = {}
    if machine:
        fields = {}
        for line in machine.splitlines():
            if ":" in line:
                key, _, value = line.removeprefix("echo: ").partition(":")
                fields[key.strip().lower()] = value.strip()
        build = re.search(r"^X:[ \t]*(\d+)\s*Y:\s*(\d+)\s*Z:\s*(\d+)", machine, re.M)
        info = {
            "model": fields.get("machine type", ""),
            "name": fields.get("machine name", ""),
            "firmware": fields.get("firmware", ""),
            "serial": serial[1] if serial else "",
            "mac": fields.get("mac address", ""),
            "tools": detail_number({"tools": fields.get("tool count")}, "tools", 0, 16),
            "volume": [int(build[1]), int(build[2]), int(build[3])] if build else None,
        }
    endstops = {}
    endstop_match = re.search(r"^Endstop:[ \t]*(.+)$", status, re.M)
    if endstop_match:
        for axis, key in (("x", "X-max"), ("y", "Y-max"), ("z", "Z-min")):
            match = re.search(key + r":\s*(-?\d+)", endstop_match[1], re.I)
            if match:
                endstops[axis] = int(match[1])
    led = re.search(r"^LED:[ \t]*(\d+)", status, re.M)
    move = re.search(r"^MoveMode:[ \t]*(\S+)", status, re.M)
    sensors = {"endstops": endstops,
               "led": bool(int(led[1])) if led else None,
               "moveMode": move[1] if move else ""}
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
        "jobControls": bool(serial and check_code and not time_hint),
        "materials": material_slots(detail), "transport": "TCP + HTTP" if detail else "TCP",
        "manual": manual_controls(detail, state, info.get("model", ""), readings["nozzle"]),
        "remaining": math.ceil(seconds) if seconds is not None else None,
        "timeHint": time_hint or ("The printer has not reported the remaining time yet" if active and seconds is None else ""),
        "elapsed": int(elapsed) if elapsed is not None else None,
        "filament": filament,
        "fan": detail_number(detail, "coolingFanSpeed", 0, 100),
        "speed": detail_number(detail, "printSpeedAdjust", 1, 1000),
        "cameraUrl": camera if isinstance(camera, str) and camera.startswith(("http://", "https://")) else "",
        "info": info,
        "sensors": sensors,
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


def printer_serial(value):
    serial = str(value or "").strip()
    if serial and not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", serial):
        raise ValueError("Enter a serial number using up to 64 letters, digits, hyphens or underscores.")
    return serial


class FlashForge:
    def __init__(self, address, check_code, serial=""):
        self.host = local_address(address)
        self.http_only = False
        self.check_code = check_code
        self.serial = serial

    def configure(self, check_code, serial):
        if (check_code, serial) != (self.check_code, self.serial):
            self.http_only = False
        self.check_code = check_code
        self.serial = serial

    def status(self, include_details):
        if not self.http_only:
            try:
                return read_status(self.host, self.check_code, include_details)
            except PermissionError:
                raise
            except (OSError, ValueError):
                if not self.serial or not self.check_code:
                    raise PrinterAPIError(
                        "TCP connection failed. For Creator 5 / 5 Pro, set the serial number and CheckCode."
                    ) from None
        try:
            status = read_http_status(self.host, self.serial, self.check_code)
        except PermissionError:
            raise
        except OSError:
            self.http_only = False
            raise
        self.http_only = True
        return status

    def files(self):
        if self.http_only:
            names = printer_api(self.host, self.serial, self.check_code, "/gcodeList",
                                limit=1024 * 1024).get("gcodeList")
            if not isinstance(names, list):
                raise PrinterAPIError("The printer returned an invalid file list.")
            files = {}
            for name in names:
                if isinstance(name, str) and name and not any(c in name for c in "/\\\0\r\n"):
                    try:
                        key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
                    except UnicodeEncodeError:
                        continue
                    files[key] = (name, name)
            return files
        return {
            key: (path.rsplit(b"/", 1)[-1].decode("utf-8", "replace"), path)
            for key, path in read_files(self.host).items()
        }

    def preview(self, path):
        if self.http_only:
            if not isinstance(path, str):
                return None
            result = printer_api(self.host, self.serial, self.check_code, "/gcodeThumb",
                                 {"fileName": path}, limit=4 * ((MAX_PREVIEW + 2) // 3) + 4096)
            encoded = result.get("imageData")
            if not isinstance(encoded, str) or not encoded:
                return None
            try:
                image = base64.b64decode(encoded, validate=True)
            except ValueError:
                return None
            if len(image) <= MAX_PREVIEW and preview_type(image):
                return image
            return None
        if not isinstance(path, bytes):
            return None
        return read_preview(self.host, path)


def preview_type(image):
    if image and image.startswith(PNG_HEADER) and image.endswith(PNG_END):
        return "image/png"
    if image and image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9"):
        return "image/jpeg"
    return None


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
            if (gap and (status["active"] or status["complete"]) and name and name == job["file"]
                    and elapsed is not None and job["elapsed"] is not None):
                # A continuing job advances by the time we were away. A new
                # run of the same file has a different inferred start time.
                gained = elapsed - job["elapsed"]
                gap = abs(gained - (now - job["seen"])) > 10 or elapsed <= now - job["seen"]
            if changed or reset or gap:
                job["result"] = "unknown"
            elif status["complete"]:
                job["result"] = "complete"
            elif state in ENDED_JOB:
                job["result"] = ENDED_JOB[state]
            elif state in {"READY", "IDLE"}:
                job["result"] = "unknown"
            if job["result"] != "active":
                if job["result"] != "unknown":
                    job["ended"] = now
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
        self.camera_lock = threading.Lock()
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
        except PrinterAPIError as error:
            with self.state_lock:
                self.latest = None
                self.error = str(error)
        except (OSError, ValueError):
            with self.state_lock:
                self.latest = None
                self.error = "The printer did not respond"
        else:
            self.history.observe(status, now)
            with self.state_lock:
                self.error = ""
                status["observedAt"] = now
                self.latest = status
                sample = {"t": now * 1000, **status["temperatures"]}
                for index, tool in enumerate(status.get("tools", []), 1):
                    sample[f"t{index}"] = tool["actual"]
                    sample[f"t{index}Target"] = tool["target"]
                self.temperatures.append(sample)
                self.history_snapshot = [job.copy() for job in self.history.jobs]
                self.history_error = self.history.error
                if probe_camera or status["cameraUrl"]:
                    self.camera_url = status["cameraUrl"]
                    self.next_camera_probe = time.monotonic() + 60


class UploadInProgress(Exception):
    pass


WEB_HOST = "127.0.0.1"
WEB_PORT = 18771
LEGACY_WEB_PORTS = (18769, 18765)
DEVICE_UI_KEY = "print_host_webui"
MONITOR_TITLE = "FlashForge"
# OrcaSlicer reads registered capabilities before capability config is available.
PLACEMENT_FILE = Path(__file__).with_name("monitor-placement.txt")


def startup_placement():
    try:
        return "flashforge" if PLACEMENT_FILE.read_text(encoding="ascii").strip() == "flashforge" else "device"
    except (OSError, UnicodeError):
        return "device"


def monitor_url(endpoint=""):
    path = f"{WEB_PORT}/{endpoint}/" if endpoint else f"{WEB_PORT}/"
    return f"http://{WEB_HOST}:{path}"


def monitor_identity(url):
    try:
        url = urlsplit(url)
    except ValueError:
        return None
    if url.scheme != "http" or url.netloc not in {f"{WEB_HOST}:{port}" for port in (WEB_PORT, *LEGACY_WEB_PORTS)}:
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
    native_page = None
    check_view = None

    placement = "device"

    def configure(self, get_config, storage):
        self.get_config = get_config
        self.storage = storage
        self.monitors = {}
        self.error = ""
        self.state_lock = threading.Lock()
        self.printer_lock = threading.Lock()
        self.apply_settings = None
        self.notify = None
        self.uploading = threading.Event()
        self.uploaded_at = 0.0
        self.stopping = threading.Event()
        self.poller = None
        self.viewed_host = ""
        try:
            saved = json.loads((storage / "notifications.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.notifications = {k: saved[k] if isinstance(saved, dict) and type(saved.get(k)) is bool else v
                              for k, v in NOTIFICATIONS.items()}

    def printers(self):
        """Configured printers and their plugin credentials."""
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
                           "name": str(entry.get("name") or host)[:60],
                           "serial": str(entry.get("serial") or "").strip()})
        if not result:
            # Older releases kept a single address in the plugin config.
            try:
                host = local_address(config.get("flashforge_host") or "")
            except ValueError:
                host = ""
            if host:
                result.append({"host": host, "code": config.get("check_code") or "",
                               "name": "Printer", "serial": config.get("serial_number") or ""})
        return result

    def settings_for(self):
        printers = self.printers()
        return (printers[0]["host"], printers[0]["code"]) if printers else ("", "")

    def settings_payload(self):
        printers = self.printers()
        return {"printers": printers, "endpoint": printers[0]["host"] if printers else "",
                "notifications": self.notifications, "placement": self.placement}

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
                item.update(state=data.get("state"), observedAt=data.get("observedAt"), status=data.get("status"), percent=data.get("percent"),
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
                                    adapter = FlashForge(entry["host"], entry["code"], entry["serial"])
                                except ValueError:
                                    continue
                                monitor = PrinterMonitor(
                                    adapter, self.storage / f"flashforge-{entry['host']}-history.json")
                                monitor.endpoint = entry["host"]
                                monitor.next_due = 0.0
                                self.monitors[entry["host"]] = monitor
                            monitor.name = entry["name"]
                    for entry in entries:
                        monitor = self.monitors.get(entry["host"])
                        if monitor is None or mono < getattr(monitor, "next_due", 0.0):
                            continue
                        priority = self.viewed_host if self.viewed_host in hosts else entries[0]["host"]
                        interval = POLL_INTERVAL if entry["host"] == priority or len(entries) <= 3 else IDLE_POLL_INTERVAL
                        monitor.next_due = mono + interval
                        with self.printer_io():
                            monitor.adapter.configure(entry["code"], entry["serial"])
                            monitor.sample(int(time.time()))
                        if self.notify is not None:
                            try:
                                monitor.notifications = self.notifications
                                self.notify(monitor)
                            except Exception:
                                pass
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

    def selected_printer(self, endpoint):
        requested = parse_qs(urlsplit(self.path).query).get("printer", [])
        if not requested:
            return endpoint
        if len(requested) != 1 or requested[0] not in {p["host"] for p in self.server.printers()}:
            raise ValueError("This printer is not in the plugin settings.")
        return requested[0]

    def checked_printer(self, endpoint):
        if endpoint == "flashforge":
            if self.server.placement != "flashforge":
                raise ValueError("The monitor is in the Printer tab.")
            endpoint = self.server.settings_for()[0]
        elif self.server.check_view is not None:
            endpoint = self.server.check_view(endpoint)
        return self.selected_printer(endpoint)

    def do_POST(self):
        authority = f"{WEB_HOST}:{WEB_PORT}"
        action = self.headers.get("X-FlashForge-Tab-Action")
        if (self.headers.get("Host") != authority
                or self.headers.get("Origin") != f"http://{authority}"
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
                or action not in ("settings", "action", "diagnostics", "notifications", "camera-stream")):
            self.reply_json({"error": "Open the monitor in OrcaSlicer."}, 403)
            return
        endpoint, route = self.request_route()
        if route in {"camera-stream", "action"} or (route == "diagnostics" and "?" in self.path):
            try:
                endpoint = self.checked_printer(endpoint)
            except (OSError, RuntimeError, ValueError) as error:
                self.reply_json({"error": str(error)}, 409)
                return
        if route == action == "camera-stream":
            self.stream_camera(endpoint)
            return
        if route == action == "notifications":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 512:
                    raise ValueError
                prefs = json.loads(self.rfile.read(length))
                if not isinstance(prefs, dict) or prefs.keys() != NOTIFICATIONS.keys() or any(type(v) is not bool for v in prefs.values()):
                    raise ValueError
                with self.server.state_lock:
                    path = self.server.storage / "notifications.json"
                    temp = path.with_suffix(".tmp")
                    temp.write_text(json.dumps(prefs), encoding="utf-8")
                    os.replace(temp, path)
                    self.server.notifications = prefs
            except (ValueError, TypeError):
                self.reply_json({"error": "Invalid notification settings."}, 400)
            except OSError:
                self.reply_json({"error": "Could not save notification settings."}, 503)
            else:
                self.reply_json(prefs)
            return
        if route == action == "diagnostics":
            entry = next((p for p in self.server.printers() if p["host"] == endpoint), None)
            if not entry:
                self.reply_json({"error": "Save this printer before checking the connection."}, 409)
                return
            try:
                with self.server.printer_io():
                    status = FlashForge(endpoint, entry["code"], entry["serial"]).status(True)
                self.reply_json({"transport": status["transport"], "authenticated": status["jobControls"],
                                 "cameraReported": bool(status["cameraUrl"]), "telemetry": True})
            except UploadInProgress:
                self.reply_json({"error": "Monitoring is paused during file transfer."}, 503)
            except (OSError, ValueError) as error:
                self.reply_json({"error": str(error) if isinstance(error, PrinterAPIError) else "Printer did not respond."}, 503)
            return
        if route == "settings" and action == "settings":
            self.save_settings()
            return
        if route == "action" and action == "action":
            self.run_action(endpoint)
            return
        self.send_error(404)

    def stream_camera(self, endpoint):
        monitor = self.server.monitors.get(endpoint)
        entry = next((p for p in self.server.printers() if p["host"] == endpoint), None)
        if not monitor or not entry or not entry["code"] or not entry["serial"]:
            self.reply_json({"error": "Save the printer serial and CheckCode first."}, 409)
            return
        with monitor.state_lock:
            status = monitor.latest or {}
        if not status.get("managedCamera"):
            self.reply_json({"error": "The Creator 5 Pro camera is unavailable."}, 409)
            return
        if not monitor.camera_lock.acquire(timeout=6):
            self.reply_json({"error": "Camera is in use. Close the other view and reconnect."}, 409)
            return

        def control(action):
            printer_api(endpoint, entry["serial"], entry["code"], "/control",
                        {"payload": {"cmd": "streamCtrl", "args": {"action": action}}})

        def disconnected(timeout=0):
            return (self.server.stopping.is_set() or self.server.monitors.get(endpoint) is not monitor
                    or bool(select.select([self.connection], [], [], timeout)[0]))

        started = attempted = False
        try:
            if disconnected():
                return
            parts = urlsplit(status.get("cameraUrl") or "/?action=stream")
            path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
            # Some firmware acknowledges open before the video listener is ready.
            for attempt in range(4):
                if disconnected():
                    return
                with self.server.printer_io():
                    attempted = True
                    control("open")
                if disconnected(2):
                    return
                upstream = http.client.HTTPConnection(endpoint, CAMERA_PORT, timeout=3)
                try:
                    upstream.request("GET", path)
                    with upstream.getresponse() as response:
                        if response.status != 200:
                            raise OSError("Camera is warming up")
                        chunk = response.read1(32768)
                        if not chunk:
                            raise OSError("Empty camera stream")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        started = True
                        self.connection.settimeout(4)
                        while chunk and not disconnected():
                            self.wfile.write(chunk)
                            chunk = response.read1(32768)
                        return
                except (OSError, http.client.HTTPException):
                    if started or attempt == 3:
                        raise
                finally:
                    upstream.close()
        except (ValueError, OSError, http.client.HTTPException, UploadInProgress):
            if not started and not disconnected():
                self.reply_json({"error": "Could not start the Creator 5 Pro camera. Check LAN mode and reconnect."}, 503)
        finally:
            try:
                if attempted:
                    control("close")
            except (OSError, ValueError):
                pass
            finally:
                monitor.camera_lock.release()

    def run_action(self, endpoint):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 1024:
            self.reply_json({"error": "Invalid request."}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            self.reply_json({"error": "Invalid request."}, 400)
            return
        command = payload.get("action") if isinstance(payload, dict) else None
        act = getattr(self.server, "act", None)
        if act is None:
            self.reply_json({"error": "Printer control is unavailable."}, 503)
            return
        try:
            state = act(endpoint, str(command or ""))
        except ValueError as error:
            self.reply_json({"error": str(error)}, 400)
            return
        except (OSError, RuntimeError) as error:
            self.reply_json({"error": str(error)}, 503)
            return
        self.reply_json({"state": state})

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
        if route == "placement":
            self.reply_json({"placement": self.server.placement})
            return
        if route == "native":
            try:
                url = self.server.native_page(endpoint) if self.server.native_page and endpoint != "flashforge" else ""
            except (OSError, RuntimeError, ValueError) as error:
                self.reply_json({"error": str(error)}, 409)
            else:
                self.reply_json({"url": url})
            return

        try:
            endpoint = self.checked_printer(endpoint)
            if printers and endpoint not in {entry["host"] for entry in printers}:
                raise ValueError("This printer is not in the plugin settings.")
        except (OSError, RuntimeError, ValueError) as error:
            self.reply_json({"error": str(error)}, 409)
            return

        try:
            monitor = self.server.monitors.get(endpoint)
            if route == "status" and monitor is not None:
                self.server.viewed_host = endpoint
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
                data["stats"] = history_stats(monitor.history_snapshot, int(time.time()))
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
            if route == "snapshot":
                if data.get("managedCamera"):
                    self.reply_json({"error": "Save a frame from the active camera view."}, 409)
                    return
                camera = monitor.camera_url
                if not camera:
                    self.reply_json({"error": "This printer has no camera."}, 404)
                    return
                with self.server.printer_io():
                    image = read_snapshot(camera)
                if not image:
                    self.reply_json({"error": "Could not read the camera."}, 503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(image)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Disposition", 'inline; filename="snapshot.jpg"')
                self.end_headers()
                self.wfile.write(image)
                return
            if route.startswith("preview/"):
                key = route.removeprefix("preview/")
                with monitor.state_lock:
                    entry = monitor.files.get(key)
                if entry is not None:
                    with self.server.printer_io():
                        image = monitor.adapter.preview(entry[1])
                    media_type = preview_type(image)
                    self.reply(image if media_type else b"", media_type or "image/png", 200 if media_type else 404)
                    return
        except UploadInProgress:
            self.reply_json({"error": "OrcaSlicer is sending a print job. Monitoring will resume when the transfer is complete."}, 503)
            return
        except PrinterAPIError as error:
            self.reply_json({"error": str(error)}, 503)
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
    active_placement = "device"

    def get_name(self):
        return 'FlashForge Device Tab'

    def get_default_config(self):
        return {"printers": [], "flashforge_host": "", "check_code": "", "serial_number": "", "placement": "device"}

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return CONFIG_PAGE.replace("{{placement}}", self.active_placement)

    def config(self):
        try:
            config = json.loads(self.get_config())
        except (TypeError, ValueError):
            return self.get_default_config()
        if not isinstance(config, dict):
            return self.get_default_config()
        result = {name: config[name].strip() if isinstance(config.get(name), str) else ""
                  for name in ("flashforge_host", "check_code", "serial_number")}
        result["printers"] = config.get("printers") if isinstance(config.get("printers"), list) else []
        result["placement"] = "flashforge" if config.get("placement") == "flashforge" else "device"
        return result

    def on_load(self):
        # Bind during the lifecycle callback so OrcaSlicer reports startup
        # errors in plugin diagnostics instead of losing them in a worker.
        storage = Path(orca.host.plugin.storage())
        self._page_store = storage / "device-pages.json"
        self._switch_lock = threading.Lock()
        self._placement_lock = threading.Lock()
        self._next_placement = self.active_placement
        self._stopping = threading.Event()
        self._view_refresh = threading.Event()
        server = LoopbackServer((WEB_HOST, WEB_PORT), MonitorHandler)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                                  name='FlashForge Device Tab HTTP', daemon=True)
        try:
            server.configure(self.config, storage)
            server.placement = self.active_placement
            server.native_page = self.native_page
            server.act = self.act
            server.notify = self.notify
            server.check_view = self.check_view
            server.apply_settings = self.apply_settings
            worker.start()
        except Exception:
            server.server_close()
            raise
        for port in LEGACY_WEB_PORTS:
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
        # Preset transactions block while Orca dispatches to the UI. Never run
        # them inside on_load or a lifecycle callback, which may own that UI.
        self._view_worker = threading.Thread(target=self._restore_views,
                                            name='FlashForge Device page', daemon=True)
        self._view_worker.start()
        self._view_refresh.set()

    def _restore_views(self):
        while True:
            refresh = self._view_refresh.wait(0.5)
            if self._stopping.is_set():
                return
            self.sync_next_placement()
            if not refresh:
                continue
            self._view_refresh.clear()
            for _ in range(10):
                try:
                    self.restore_selected_view()
                    break
                except (OSError, RuntimeError, ValueError):
                    # Startup and rapid preset changes can briefly invalidate
                    # the target. Retry with a fresh snapshot, off the UI thread.
                    if self._stopping.wait(0.1):
                        return

    def restore_selected_view(self):
        server = self._server
        if server is None or self._stopping.is_set():
            return
        preset = orca.host.preset_bundle().current_printer_preset()
        if preset.config_value("host_type") != "flashforge":
            return
        endpoint = printer_endpoint(preset.config_value("print_host") or "")
        current_url = preset.config_value(DEVICE_UI_KEY) or ""
        show_monitor = self.active_placement == "device"
        if show_monitor and (current_url == monitor_url(endpoint)
                             or not any(item["host"] == endpoint for item in server.printers())):
            return
        if not show_monitor and monitor_identity(current_url) is None:
            return
        expected = (preset.name, endpoint, current_url)
        self.switch_view(expected_profile=expected)

    def adopt_profile_address(self):
        """First run after an upgrade: take the address from the active profile."""
        config = self.config()
        if config.get("flashforge_host") or config.get("printers"):
            return
        preset = orca.host.preset_bundle().current_printer_preset()
        if preset.config_value("host_type") != "flashforge":
            return
        print_host = preset.config_value("print_host") or ""
        if not print_host:
            return
        host = printer_endpoint(print_host)
        config["flashforge_host"] = host
        self.save_config(json.dumps(config, ensure_ascii=False))

    def sync_next_placement(self):
        # The host's settings iframe only writes capability config. Mirror the
        # saved preference so it is available before the next registration.
        try:
            with self._placement_lock:
                placement = self.config()["placement"]
                if placement != self._next_placement:
                    staged = PLACEMENT_FILE.with_suffix(".tmp")
                    staged.write_text(placement, encoding="ascii")
                    os.replace(staged, PLACEMENT_FILE)
                    self._next_placement = placement
        except (OSError, RuntimeError):
            # A later check or shutdown can retry a transient host/write failure.
            pass

    def on_unload(self):
        self._stopping.set()
        self._view_refresh.set()
        self.sync_next_placement()
        # Do not join the page worker here: it may be waiting for the UI thread
        # that is unloading us. It checks _stopping before committing a change.
        for alias in getattr(self, "_aliases", []):
            alias.shutdown()
            alias.server_close()
        self._aliases = []
        if self._server is not None:
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
        elif event in (orca.LifecycleEvent.PresetSelected, orca.LifecycleEvent.ProjectOpened,
                       orca.LifecycleEvent.NewProject):
            self._view_refresh.set()

    def check_view(self, endpoint):
        preset = orca.host.preset_bundle().current_printer_preset()
        return self.check_profile_view(preset, endpoint)

    def check_profile_view(self, preset, endpoint):
        if self.active_placement != "device":
            raise ValueError("Open the FlashForge tab to see the monitor.")
        if preset.config_value("host_type") != "flashforge":
            raise ValueError("Open FlashForge Device Tab for a FlashForge printer profile from Speed Dial.")
        if monitor_identity(preset.config_value(DEVICE_UI_KEY) or "") != endpoint:
            raise ValueError("Open FlashForge Device Tab for the current printer profile from Speed Dial.")
        current = printer_endpoint(preset.config_value("print_host") or "")
        if endpoint and endpoint != current:
            raise ValueError("The printer address changed. Reopen FlashForge Device Tab for the current printer.")
        return current

    def original_page(self, name, fallback=""):
        try:
            target = self.saved_pages().get(name, fallback)
        except (OSError, ValueError):
            target = fallback
        if monitor_identity(target) is not None:
            target = fallback
        return target if monitor_identity(target) is None else ""

    def native_page(self, endpoint):
        bundle = orca.host.preset_bundle()
        preset = bundle.current_printer_preset()
        current = self.check_profile_view(preset, endpoint)
        fallback = bundle.printers.selected_preset().config_value(DEVICE_UI_KEY) or ""
        target = self.original_page(preset.name, fallback) or preset.config_value("print_host") or current
        target = target if "://" in target else "http://" + target
        try:
            url = urlsplit(target)
            if url.scheme in ("http", "https") and url.hostname and url.username is None and "\\" not in target:
                return target
        except ValueError:
            pass
        return f"http://{current}"

    def saved_pages(self):
        try:
            pages = json.loads(self._page_store.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(pages, dict) or any(not isinstance(url, str) for url in pages.values()):
            raise ValueError("Could not read the saved printer web interface URLs.")
        return pages

    def switch_view(self, *, expected_profile=None):
        if not self._switch_lock.acquire(blocking=False):
            raise ValueError("A page switch is already in progress.")
        try:
            if self._stopping.is_set():
                raise ValueError("FlashForge Device Tab is shutting down.")
            presets = orca.host.preset_bundle().printers
            preset = presets.edited_preset()
            if preset.config_value("host_type") != "flashforge":
                raise ValueError("Select a printer profile that uses the FlashForge host.")
            current_url = preset.config_value(DEVICE_UI_KEY) or ""
            print_host = preset.config_value("print_host") or ""
            endpoint = printer_endpoint(print_host)
            if expected_profile is not None and expected_profile != (preset.name, endpoint, current_url):
                raise ValueError("The active printer profile changed. Try again.")
            current_view = monitor_identity(current_url)
            show_monitor = self.active_placement == "device"
            if (show_monitor and current_url == monitor_url(endpoint)
                    or not show_monitor and current_view is None):
                return ("FlashForge Device Tab is in the Printer tab." if show_monitor
                        else "Open the FlashForge tab to see the monitor.")
            transaction = orca.host.preset.printer.begin_update()
            edited = presets.edited_preset()
            if (edited.name != transaction.target_name or edited.name != preset.name
                    or edited.config_value("host_type") != "flashforge"
                    or edited.config_value("print_host") != print_host
                    or (edited.config_value(DEVICE_UI_KEY) or "") != current_url):
                raise ValueError("The active printer profile changed. Try again.")
            if not show_monitor:
                fallback = presets.selected_preset().config_value(DEVICE_UI_KEY) or ""
                target = self.original_page(preset.name, fallback)
                message = "Open the FlashForge tab to see the monitor."
            else:
                entry = next((item for item in self._server.printers() if item["host"] == endpoint), None)
                if entry is None:
                    raise ValueError("The printer address in the plugin settings does not match the active profile.")
                FlashForge(endpoint, entry.get("code", ""))
                try:
                    pages = self.saved_pages()
                except ValueError:
                    archived = self._page_store.with_suffix(f".invalid-{time.time_ns()}.json")
                    self._page_store.rename(archived)
                    pages = {}
                if current_view is None:
                    pages[preset.name] = current_url
                pending = self._page_store.with_suffix(".tmp")
                pending.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
                os.replace(pending, self._page_store)
                target = monitor_url(endpoint)
                message = "Opened FlashForge Device Tab in the Printer tab."

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
            serial = printer_serial(entry.get("serial"))
            if serial:
                clean[-1]["serial"] = serial
        if not clean:
            raise ValueError("Add at least one printer address.")
        host, check_code = clean[0]["host"], clean[0]["code"]
        previous_host = self._server.settings_for()[0]
        self.save_config(json.dumps({"printers": clean, "flashforge_host": host,
                                     "check_code": check_code,
                                     "serial_number": clean[0].get("serial", ""),
                                     "placement": self.config()["placement"]}, ensure_ascii=False))
        if self.active_placement == "device" and host != previous_host:
            self._point_device_tab(host)
        self._view_refresh.set()
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

    def notify(self, monitor):
        """Show a host notification when a job ends."""
        with monitor.state_lock:
            data = monitor.latest
        preferences = getattr(monitor, "notifications", NOTIFICATIONS)
        if not data:
            now = time.monotonic()
            if getattr(monitor, "offline_since", None) is None:
                monitor.offline_since = now
            if not preferences["offline"] or now - monitor.offline_since < 60 or getattr(monitor, "offline_notified", False):
                return
            monitor.offline_notified = True
            state = "OFFLINE"
        else:
            monitor.offline_since = None
            monitor.offline_notified = False
            state = data.get("state")
        previous = getattr(monitor, "last_state", None)
        if state != "OFFLINE":
            monitor.last_state = state
        if previous == state:
            return
        if state in ("COMPLETED", "BUILDING_COMPLETED", "FINISHED") and preferences["finished"]:
            message = "Print finished"
        elif state in ("CANCELLED", "CANCELED", "STOPPED") and preferences["cancelled"]:
            message = "Print cancelled"
        elif state == "ERROR" and preferences["error"]:
            message = "Printer error"
        elif state == "OFFLINE":
            message = "Printer offline for at least one minute"
        else:
            return
        name = getattr(monitor, "name", "") or "Printer"
        file = (data or {}).get("file") or ""
        try:
            ui = orca.host.ui
            levels = ui.NotificationLevel
            level = getattr(levels, "WarningNotificationLevel" if state in {"ERROR", "OFFLINE"}
                            else "RegularNotificationLevel")
            ui.push_notification(level, f"{name}: {message}" + (f" — {file}" if file else ""))
        except (AttributeError, RuntimeError, TypeError):
            pass

    ACTIONS = {"pause": "pause", "resume": "continue", "cancel": "cancel"}
    CONFIRMED_STATES = {
        "pause": {"PAUSED"},
        "resume": {"PRINTING", "BUILDING", "BUILDING_FROM_SD", "HEATING"},
        "cancel": {"CANCELING", "CANCELLING", "CANCELLED", "CANCELED", "STOPPED"},
    }

    def act(self, host, action):
        target = self.ACTIONS.get(action)
        manual = action.startswith("manual/")
        if target is None and not manual:
            raise ValueError("Unknown printer command.")
        printers = self._server.printers()
        entry = next((item for item in printers if item["host"] == host), None)
        if entry is None:
            raise ValueError("This printer is not in the plugin settings.")
        monitor = self._server.monitors.get(host)
        if monitor is None:
            raise ValueError("The printer is not connected yet.")
        try:
            with self._server.printer_io():
                if manual:
                    current = monitor.adapter.status(include_details=True)
                    key = action[7:]
                    controls = current.get("manual")
                    if not controls or key not in controls["allowed"]:
                        raise ValueError("Command unavailable for the current printer state.")
                    if controls["tcpMotion"] and (key == "home" or re.fullmatch(r"[xyze][+-](?:1|10)", key)):
                        if entry["serial"] and entry["serial"] != current["info"].get("serial"):
                            raise ValueError("Check the printer serial number.")
                        if self._server.upload_active() or self._server.stopping.is_set():
                            raise UploadInProgress
                        return tcp_manual_move(host, key, current["info"].get("volume"))
                    serial = current["info"].get("serial")
                    if not serial or not entry["code"] or (entry["serial"] and entry["serial"] != serial):
                        raise ValueError("Check the printer serial number and CheckCode.")
                    payload = manual_payload(key, controls)
                    if self._server.upload_active() or self._server.stopping.is_set():
                        raise UploadInProgress
                    printer_api(host, serial, entry["code"], "/control", {"payload": payload})
                    if key == "clear" or key.startswith("timer"):
                        detail = read_http_detail(host, serial, entry["code"])
                        if key == "clear" and detail.get("errorCode") == "":
                            return "Printer reports the error cleared."
                        if key.startswith("timer"):
                            minutes = int(key[5:])
                            if (detail.get("autoShutdown") == ("open" if minutes else "close")
                                    and (not minutes or detail.get("autoShutdownTime") == minutes)):
                                return "Printer confirmed the shutdown timer setting."
                        return "Command accepted. The printer has not confirmed the change."
                    return "Printer accepted the command."
                with monitor.state_lock:
                    data = monitor.latest
                serial = ((data or {}).get("info") or {}).get("serial") or ""
                if not serial:
                    raise ValueError("The printer did not report its serial number.")
                if monitor.adapter.http_only and serial != entry["serial"]:
                    raise ValueError("Wait for the printer to reconnect with the new settings.")
                control_job(host, serial, entry["code"], target)
                deadline = time.monotonic() + 4
                for attempt in range(4):
                    if attempt and (time.monotonic() >= deadline or self._server.stopping.wait(0.6)):
                        break
                    if self._server.upload_active() or self._server.stopping.is_set():
                        break
                    monitor.sample(int(time.time()))
                    with monitor.state_lock:
                        observed = monitor.latest or {}
                    state = observed.get("state", "")
                    if data.get("file") and observed.get("file") and data["file"] != observed["file"]:
                        raise RuntimeError(f"The current job changed before {action} could be confirmed.")
                    if state in self.CONFIRMED_STATES[action]:
                        return state
                with monitor.state_lock:
                    last = (monitor.latest or {}).get("state") or "unavailable"
                raise RuntimeError(f"Printer did not confirm {action}. Last reported state: {last}.")
        except UploadInProgress as error:
            raise RuntimeError("OrcaSlicer is sending a print job. Try again when it finishes.") from error

    def execute(self):
        try:
            message = self.switch_view()
        except (OSError, RuntimeError, ValueError) as error:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, str(error))
        return orca.ExecutionResult.success(message)


class FlashForgePage(orca.pages.PagesPluginCapabilityBase):
    def get_name(self):
        return "FlashForge"

    def get_type(self):
        return orca.PluginType.Pages

    def get_ui(self):
        url = monitor_url() + "flashforge/"
        return f'''<!doctype html><meta charset="utf-8">
<script>location.replace({json.dumps(url)})</script>
<a href="{url}">Open FlashForge</a>'''


@orca.plugin
class FlashForgeDeviceTab(orca.base):
    def register_capabilities(self):
        # The loopback page is declared here. Reaching the printer itself needs a
        # per-host grant, which OrcaSlicer asks for the first time the plugin
        # connects to a new address.
        orca.request_permissions(network_socket=[f"('{WEB_HOST}', {WEB_PORT})"], threading=True)
        FlashForgeService.active_placement = startup_placement()
        orca.register_capability(FlashForgeService)
        if FlashForgeService.active_placement == "flashforge":
            orca.register_capability(FlashForgePage)

