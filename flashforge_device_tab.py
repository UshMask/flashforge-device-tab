# /// script
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "FlashForge Device Tab"
# description = "Follow local FlashForge prints in OrcaSlicer's Device or FlashForge tab."
# author = "Max"
# version = "1.14.5"
# ///
import base64
import errno
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import select
import secrets
import socket
import tempfile
import threading
import time
import zipfile
from collections import deque
from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from string import Template
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import orca

PORT = 8899
API_PORT = 8898
CAMERA_PORT = 8080
DISCOVERY_PORT = 48899
DISCOVERY_MODELS = {0x23: "Adventurer 5M", 0x24: "Adventurer 5M Pro", 0x26: "AD5X",
                    0x28: "Creator 5", 0x29: "Creator 5 Pro"}
IFS_MATERIALS = {
    "ad5x": ("PLA", "PLA-CF", "PETG", "PETG-CF", "ABS", "SILK", "TPU", "PET-CF",
             "PAHT-CF", "PA-CF", "PC", "PA", "PC-ABS", "PPS-CF"),
    "creator": ("PLA", "PLA-CF", "PETG", "PETG-CF", "ABS", "ASA", "SILK", "PET-CF",
                "PAHT-CF", "S-PAHT", "S-Multi", "PA-CF", "HIPS", "PVA", "TPU-90A",
                "TPU-95A", "TPU-64D", "PC", "PA", "PC-ABS", "PPS-CF"),
}
AD5X_COLORS = ("FFFFFF", "FEF043", "DCF478", "0ACC38", "067749", "0C6283", "0DE2A0", "75D9F3",
               "45A8F9", "2750E0", "46328E", "A03CF7", "F330F9", "D4B0DC", "F95D73", "F72224",
               "7C4B00", "F98D33", "FDEBD5", "D3C4A3", "AF7836", "898989", "BCBCBC", "161616")
CREATOR_COLORS = ("FFFFFF", "FFF245", "DEF578", "21CC3D", "167A4B", "156682", "24E4A0", "7BD9F0",
                  "4CAAF8", "2E54DD", "48358C", "A341F7", "F435F6", "D5B4DE", "FA6173", "F82D29",
                  "805003", "F9903B", "FCEBD7", "D5C5A1", "B17C38", "8C8C89", "BEBEBE", "1B1B1B")
IFS_COLORS = {"ad5x": AD5X_COLORS, "creator": CREATOR_COLORS}
MAX_PRINT_SOURCE = 512 * 1024 * 1024
MAX_FILE_UPLOAD = 128 * 1024 * 1024
MAX_SLICE_INFO = 1024 * 1024
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

LANGUAGES = {
    "ca": "Català", "cs": "Čeština", "de": "Deutsch", "en": "English",
    "es": "Español", "eu": "Euskara", "fr": "Français", "hu": "Magyar",
    "it": "Italiano", "ja": "日本語", "ko": "한국어", "lt": "Lietuvių",
    "nl": "Nederlands", "pl": "Polski", "pt-BR": "Português (Brasil)", "ro": "Română",
    "ru": "Русский", "sv": "Svenska", "th": "ไทย", "tr": "Türkçe", "uk": "Українська",
    "vi": "Tiếng Việt", "zh-CN": "简体中文", "zh-TW": "繁體中文",
}


def normalize_language(code, fallback="en"):
    if not isinstance(code, str):
        return fallback
    parts = code.strip().lower().replace("_", "-").split(".", 1)[0].split("-")
    if parts[0] == "zh":
        return "zh-TW" if {"hant", "tw", "hk", "mo"}.intersection(parts[1:]) else "zh-CN"
    if parts[0] == "pt":
        return "pt-BR"
    return parts[0] if parts[0] in LANGUAGES else fallback


def orca_language(fallback="en"):
    # The direct API gives the actual UI language. OrcaSlicer 2.5.x's older
    # app_info compatibility API reports only a subset of languages.
    for read in (lambda: orca.host.app_language(), lambda: orca.host.app_info().get("language")):
        try:
            code = normalize_language(read(), "")
        except (AttributeError, RuntimeError):
            continue
        if code:
            return code
    return fallback


def translate(language, message):
    return TRANSLATIONS.get(language, {}).get(message, message)


UI_TRANSLATION_SCRIPT = r'''
const uiLocale = document.documentElement.lang === 'en' ? 'en-US' : document.documentElement.lang;
const {messages, states} = JSON.parse(document.getElementById('plugin-translations').textContent);
function t(message, fields = {}) {
  const label = Object.hasOwn(messages, message) ? messages[message] : message;
  return String(label ?? '').replace(/\{([a-z]+)\}/gi, (marker, key) => Object.hasOwn(fields, key) ? fields[key] : marker);
}
'''


def render_page(page, language, **slots):
    data = json.dumps({"messages": TRANSLATIONS.get(language, {}), "states": STATES}, ensure_ascii=False)
    values = {"language": language, "translations": data.replace("<", r"\u003c"),
              "translationScript": UI_TRANSLATION_SCRIPT, **slots}
    return re.sub(r"\{\{(t:)?([^{}]+)\}\}",
                  lambda match: escape(translate(language, match[2])) if match[1] else values[match[2]], page)


CAMERA_EXPAND_ICON = '''<svg class="camera-expand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 4H4v4m12-4h4v4M4 16v4h4m12-4v4h-4"/></svg>'''

JOB_CARD_CONTENT = Template('''
        <div class="jobs-body">
          <div class="jobs-printer-heading">$heading</div>
          <div class="jobs-file-row">$filename<strong class="jobs-percent" hidden></strong></div>
          <progress max="100" aria-label="{{t:Print progress}}" hidden></progress>
          $metrics
          <div class="jobs-last" hidden>{{t:Last completed}} <strong></strong></div>
          <p class="jobs-completion-hint" role="status" hidden>{{t:Remove the print and close the completion dialog on the printer, if shown.}}</p>
          <div class="jobs-actions">$actions</div>
          $extra
        </div>
        <div class="jobs-media">
          <figure class="jobs-model">
            <div class="jobs-media-frame">
              <div class="jobs-fallback"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true">
                <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Zm0 9 8-4.5M12 12 4 7.5m8 4.5v9"/>
              </svg><span>{{t:No preview}}</span></div>
              <img data-preview$preview_attrs alt="" hidden>
            </div>
          </figure>
          <div class="jobs-actions jobs-model-actions"$print_attrs>$print_buttons</div>
        </div>
''')

FLEET_JOB_CONTENT = JOB_CARD_CONTENT.substitute(
    heading='''<div class="jobs-printer-identity"><h3 tabindex="-1"></h3><span class="jobs-state"></span></div>
            <a data-monitor data-icon="arrow">{{t:Monitor}}</a>''',
    filename='<span class="jobs-file"></span>',
    preview_attrs="", print_attrs="",
    metrics='<div class="row jobs-meta"><span data-layer></span><strong data-eta></strong></div>',
    actions='''<button type="button" data-retry data-icon="refresh" hidden>{{t:Reconnect camera}}</button>
            <button type="button" data-clear-bed data-icon="check" hidden>{{t:Bed is clear}}</button>
            <a data-files data-icon="arrow" hidden>{{t:Browse files}}</a>
            <a data-repeat data-icon="refresh" hidden>{{t:Print again}}</a>''',
    extra='''<details><summary>{{t:Job details}}</summary><dl></dl><ol class="jobs-events"></ol></details>
          <p class="jobs-meta" data-job-result role="status"></p>''',
    print_buttons='''<button type="button" data-print-action data-icon="pause" hidden></button>
            <button type="button" class="danger" data-print-cancel data-icon="stop" hidden>{{t:Cancel}}</button>''',
)

MONITOR_JOB_CONTENT = JOB_CARD_CONTENT.substitute(
    heading='''<div class="jobs-printer-identity">
              <h2 id="job-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M7 3h7l4 4v14H7z M14 3v5h4 M10 12h5 M10 16h5"/></svg>{{t:Print job}}</h2>
              <span id="status" class="connection" role="status">{{t:Connecting…}}</span>
              <button class="jobs-link" id="diagnose" type="button" data-icon="find">{{t:Check connection}}</button>
            </div>
            <div class="monitor-job-tools">
              <button class="jobs-link" id="cameraOpen" type="button" data-icon="camera" aria-haspopup="dialog">{{t:Camera}}</button>
              <button class="jobs-link" id="monitorHistory" type="button" data-icon="history" aria-label="{{t:Print History}}" title="{{t:Print History}}" hidden></button>
            </div>''',
    filename='''<button class="jobs-file" id="openJob" type="button" hidden></button>
            <span class="jobs-file" id="jobFile">—</span>''',
    preview_attrs=' id="jobImage"', print_attrs=' id="jobActions"',
    metrics='''<div class="row jobs-meta" id="jobMetrics">
            <span id="jobMeta"><span id="layers"></span><span id="elapsed"></span><span id="filament"></span></span>
            <span id="time"><strong id="remaining"></strong> <span id="finishTime" hidden></span></span>
          </div>''',
    actions='<button class="jobs-link" id="reprintJob" type="button" data-icon="refresh" hidden disabled>{{t:Print again}}</button>',
    extra='''<p class="jobs-meta" id="timeHint" hidden></p>
          <p class="jobs-meta" id="actionStatus" role="status" hidden></p>''',
    print_buttons='''<button id="pausePrint" type="button" data-icon="pause" hidden>{{t:Pause}}</button>
            <button id="resumePrint" type="button" data-icon="play" hidden>{{t:Resume}}</button>
            <button id="cancelPrint" type="button" class="danger" data-icon="stop" hidden>{{t:Cancel}}</button>''',
)


PAGE = r'''<!doctype html>
<html lang="{{language}}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{printer}}</title>
<style>
:root {
  color-scheme: light;
  --bg: #f5f5ef;
  --panel: #fffefa;
  --header: var(--panel);
  --text: #232c25;
  --muted: #5b665c;
  --line: #d7dcd1;
  --blue: #39745c;
  --track: #e4e9ef;
  --ok: #23764b;
  --warn: #92502a;
  --danger: #923b37;
  --bed-series: #a34a20;
  --file-card-min: 125px;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #1d2521;
  --panel: #29322d;
  --text: #f1f4ec;
  --muted: #bcc8bc;
  --line: #465449;
  --blue: #98d6b1;
  --track: #50585c;
  --ok: #88d8a8;
  --warn: #f0b488;
  --danger: #ffb4ae;
  --bed-series: #ffc08d;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px "Segoe UI", sans-serif; font-variant-numeric: tabular-nums; }
main { max-width: 1280px; margin: auto; padding: 20px 24px 28px; }
h1, h2, p, dl, dd { margin: 0; }
h1 { font-size: 17px; font-weight: 600; }
h2 { font-size: 14px; font-weight: 600; }
.muted { color: var(--muted); }
.connection, .row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.row { justify-content: space-between; }
.connection { gap: 6px; color: var(--muted); font-size: 12px; white-space: nowrap; }
.connection::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.connection.connected { color: var(--ok); }
.connection.attention { color: var(--warn); }
.connection.disconnected { color: var(--warn); max-width: 100%; min-width: 0; flex-wrap: nowrap;
  white-space: normal; overflow-wrap: anywhere; }
.connection.disconnected::before { flex: 0 0 7px; }
.dashboard { display: grid; grid-template-columns: minmax(0, 3fr) minmax(380px, 2fr); gap: 16px; align-items: start; }
.monitor-main, .printer-sidebar { display: grid; gap: 16px; min-width: 0; align-content: start; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.panel > .panel-title:first-child { border-radius: 7px 7px 0 0; }
.panel-title { padding: 9px 13px; background: var(--header); }
.caption { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
progress { display: block; appearance: none; width: 100%; height: 8px; border: 0; border-radius: 5px; background: var(--track); overflow: hidden; }
progress::-webkit-progress-bar { background: var(--track); border-radius: 5px; }
progress::-webkit-progress-value { background: var(--blue); border-radius: 5px; }
progress::-moz-progress-bar { background: var(--blue); border-radius: 5px; }
.readouts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 12px; }
.readout { padding: 11px 8px; border-bottom: 1px solid var(--line); }
.readout:nth-child(odd) { border-right: 1px solid var(--line); }
.readout:nth-child(even) { padding-left: 13px; }
.readout:nth-last-child(-n+2) { border-bottom: 0; }
.readout dt { color: var(--muted); font-size: 12px; }
.readout dd { margin-top: 4px; font-size: 14px; overflow-wrap: anywhere; }
.readout strong { color: var(--blue); font-size: 19px; font-weight: 600; }
.stale .readouts { opacity: .65; }
.monitor-tools { margin-top: 12px; }
.tool-body { padding: 12px 13px; }
.chart { display: block; width: 100%; height: 168px; }
#chartPlots {display:grid;gap:8px}
.chart-legend { display: flex; flex-wrap: wrap; gap: 5px 12px; margin: 4px 0 1px; font-size: 12px; }
.chart-legend span::before { content: ""; display: inline-block; width: 15px; border-top: 2px solid var(--series); margin-right: 5px; vertical-align: 3px; }
.chart-legend .target::before { border-top-style: dashed; }
.nozzle-series { --series: var(--blue); }
.bed-series { --series: var(--bed-series); }
summary.panel-title { cursor: pointer; font-weight: 600; }
details .tool-body { border-top: 1px solid var(--line); }
.camera-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 8px 0; }
.camera-form input { flex: 1 1 100%; width: auto; min-width: 0; }
.camera-form:has(#cameraStop:not([hidden])) #cameraStart {display:none}
#cameraLight[aria-pressed=true] {border-color:var(--blue);color:var(--text);background:var(--track);font-weight:600}
#cameraLight[aria-pressed=true]::before {background:var(--blue)}
.history-summary, .history-table { margin-top: 8px; }
.history-table { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 13px; }
.history-table th, .history-table td { padding: 7px 9px; text-align: left; border-bottom: 1px solid var(--line); }
.history-table th:nth-child(2) { width: 23ch; }
.history-table th:nth-child(3) { width: 13ch; }
.history-table th:nth-child(4) { width: 14ch; }
.history-table .history-filename {
  display: block; max-width: 100%; padding: 0; border: 0; background: transparent;
  color: var(--blue); font-weight: 600; text-align: start; line-height: 1.35; overflow-wrap: anywhere;
}
.history-filename:hover { text-decoration: underline; }
.history-filename:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
.history-printer-name {display:block;margin-top:3px;font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.table-wrap { overflow: auto; max-height: 350px; }
@media (max-width: 620px) {
  .history-table, .history-table tbody { display: block; }
  .history-table thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  .history-table tbody tr {
    display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, .9fr) minmax(0, 1fr);
    border-bottom: 1px solid var(--line); padding: 8px 9px; gap: 4px 8px;
  }
  .history-table td { padding: 0; border: 0; color: var(--muted); overflow-wrap: anywhere; }
  .history-table td:first-child { grid-column: 1 / -1; }
}
.file-controls { justify-content: flex-start; padding: 10px 12px; }
input, button { font: inherit; color: inherit; background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 6px 9px; }
input { min-width: 0; width: min(100%, 245px); }
input::placeholder { color: var(--muted); }
button { cursor: pointer; }
[data-icon]::before {content:"";display:inline-block;width:1.1em;height:1.1em;margin-inline-end:.4em;vertical-align:-.19em;
  background:currentColor;mask:var(--action-icon) center/contain no-repeat}
[data-icon="monitor"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M3%204h18v13H3z%20M8%2021h8%20M12%2017v4%20M6%2011h3l2-3%202%206%202-3h3%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="fleet"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M3%204h7v7H3z%20M14%204h7v7h-7z%20M3%2015h7v6H3z%20M14%2015h7v6h-7z%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="history"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M3%2010a9%209%200%201%201%201%208%20M3%204v6h6%20M12%207v5l4%202%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="bell"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M6%209a6%206%200%200%201%2012%200c0%207%203%207%203%209H3c0-2%203-2%203-9%20M10%2021h4%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="settings"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M4%206h16%20M4%2012h16%20M4%2018h16%20M8%204v4%20M16%2010v4%20M10%2016v4%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="find"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M10%203a7%207%200%201%200%200%2014%207%207%200%200%200%200-14z%20M15%2015l6%206%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="plus"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M12%205v14%20M5%2012h14%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="remove"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M4%206h16%20M9%206V3h6v3%20M6%206l1%2015h10l1-15%20M10%2010v7%20M14%2010v7%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="edit"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M4%2016.5V20h3.5L19%208.5%2015.5%205z%20M13.5%207l3.5%203.5%20M15.5%205%2017%203.5a1.5%201.5%200%200%201%202.1%200l1.4%201.4a1.5%201.5%200%200%201%200%202.1L19%208.5%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="check"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m5%2012%204%204L19%206%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="refresh"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M20%2010a8%208%200%200%200-14-4L3%209%20M3%204v5h5%20M4%2014a8%208%200%200%200%2014%204l3-3%20M16%2015h5v5%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="print"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M6%208V3h12v5%20M6%2017H3V9h18v8h-3%20M6%2014h12v7H6z%20M17%2011h1%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="play"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m9%205%2011%207-11%207z%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="pause"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M9%205v14%20M15%205v14%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="stop"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M7%207h10v10H7z%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="camera"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M3%207h4l2-3h6l2%203h4v13H3z%20M15.5%2013a3.5%203.5%200%201%201-7%200%203.5%203.5%200%201%201%207%200%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="light"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M9%2018h6%20M10%2021h4%20M8%2014a6%206%200%201%201%208%200l-1%201v1H9v-1z%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="unplug"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m8%205%2011%2011%20M9%209l-3%203a4%204%200%200%200%206%206l3-3%20M8%2017l-4%204%20M14%208l4-4%20M17%2011l4-4%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="link"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M10%207l2-2a5%205%200%200%201%207%207l-2%202%20M14%2017l-2%202a5%205%200%200%201-7-7l2-2%20M9%2015l6-6%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="arrow"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M5%2012h14%20M13%206l6%206-6%206%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="copy"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M8%208h12v13H8z%20M16%205V3H4v13h2%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="close"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m6%206%2012%2012%20M18%206%206%2018%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="warning"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M12%204%203%2020h18z%20M12%2010v4%20M12%2017h.01%22%2F%3E%3C%2Fsvg%3E")}
[data-icon="browser"]{--action-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.7%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M12%205H4v15h15v-8%20M14%204h6v6%20M11%2013l9-9%22%2F%3E%3C%2Fsvg%3E")}
button:hover:enabled, button:focus-visible, input:focus-visible { border-color: var(--blue); }
button:disabled { opacity: .55; cursor: default; }
.files { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, var(--file-card-min)), 1fr)); gap: 8px; padding: 0 12px 12px; }
:root[data-card-size="small"] { --file-card-min: 104px; }
:root[data-card-size="large"] { --file-card-min: 168px; }
.file-card { min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 4px; }
.preview { position: relative; display: grid; place-items: center; aspect-ratio: 1.65; background: var(--header); font-size: 11px; }
.preview img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: inherit; }
.file-name { display: block; padding: 7px 8px; font-size: 12px; }
.file-name-label {
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  block-size: 2.7em; overflow: hidden;
  line-height: 1.35; white-space: normal; overflow-wrap: anywhere;
}
.pager { border-top: 1px solid var(--line); padding: 8px 12px; justify-content:center; gap:8px; }
.pager form {display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}
.pager input {width:5ch;text-align:center;padding:6px 3px}
.pager button {min-width:34px}
@media (max-width: 740px) {
  main { padding: 14px 12px; }
}
@media (max-width: 820px) {
  .dashboard { grid-template-columns: minmax(0, 1fr); reading-flow: grid-rows; }
  .monitor-main, .printer-sidebar { display: contents; }

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
button.danger { border-color: #a4443f; color: var(--danger); }
button.danger.armed { background: #a4443f; color: #fff; }
.setup ol { margin: 6px 0 10px; padding-left: 20px; line-height: 1.55; }
.setup code { background: var(--header); border-radius: 4px; padding: 1px 5px; font-size: 12px; }
dialog.settings .caption { margin-top: 2px; }
.settings-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.settings-actions button[type="submit"] { background: var(--blue); border-color: var(--blue); color: #fff; }
/* Device panel */
main {padding-top:6px}
/* The active tab shares its surface and baseline with the page. */
.tabs {display:flex;align-items:end;justify-content:space-between;gap:8px 16px;margin-bottom:14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.view-tabs,.view-actions {display:flex;align-items:center;gap:3px}
.tabs button {white-space:nowrap;line-height:1.2;color:var(--muted)}
.view-tabs button {position:relative;margin-bottom:-1px;border:1px solid transparent;border-bottom-color:var(--line);border-radius:9px 9px 0 0;padding:9px 16px;background:transparent}
.view-tabs button:hover {background:var(--panel)}
.view-tabs button[aria-current="page"] {background:var(--panel);border-color:var(--line);border-bottom-color:var(--panel);color:var(--text);font-weight:600}
.view-actions {margin-left:auto;padding-bottom:4px;max-width:100%;flex-wrap:wrap;justify-content:flex-end}
.view-actions button {background:transparent;border-color:transparent;padding:5px 8px}
#devicePicker {min-width:0;max-width:min(170px,100%);padding:4px 6px;border:1px solid var(--line);
  border-radius:5px;background:var(--panel);color:var(--text);font:inherit;font-size:12px}
#nativePage {justify-self:start;color:var(--blue)}
.panel{border-radius:12px}
.panel-title{padding:13px 16px}
.readings .panel-title {gap:4px 12px}
.readings .readouts {grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:12px;padding:4px 16px 12px}
.readings .readout {padding:0;border:0}
.connection-footer {display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
  padding:8px 16px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.connection-meta {display:flex;gap:4px 12px;align-items:center;flex-wrap:wrap}
.connection-footer button {margin-left:auto}
.totals .readouts {grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:0}
.totals .readout {padding:0;border:0}
#manualPanel {container-type:inline-size;overflow:hidden}
#manualPanel summary {display:flex;align-items:center;gap:8px;list-style:none}
#manualPanel summary::-webkit-details-marker {display:none}
#manualPanel summary::before {content:'';width:6px;height:6px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;transform:rotate(-45deg)}
#manualPanel[open] summary::before {transform:rotate(45deg) translateY(-2px)}
#manualPanel .manual-body {display:grid;gap:8px;padding:10px 12px 12px}
#manualPanel button,#manualPanel select {font:inherit;font-size:13px;line-height:1.2;border:1px solid var(--line);border-radius:5px;background:var(--panel);color:var(--text);padding:4px 8px}
#manualPanel button:disabled {opacity:.5;cursor:default}
#manualPanel button:enabled:hover {background:var(--bg);border-color:var(--muted)}
#manualPanel :focus-visible,.step-switch input:focus-visible + span {outline:2px solid var(--blue);outline-offset:2px}
.manual-group {display:grid;gap:8px;padding-block:9px 0;border-top:1px solid var(--line)}
.manual-body > .manual-group:first-of-type {border-top:0;padding-top:0}
.panel-inset {display:grid;gap:8px;padding:10px;border:1px solid var(--line);border-radius:7px;
  background:color-mix(in srgb,var(--panel) 72%,var(--bg))}
.manual-heading,.inset-heading {margin:0;font-size:13px;font-weight:650}
.manual-surface {display:grid;grid-template-columns:minmax(0,1fr) clamp(88px,24%,112px);gap:10px 14px}
.manual-xy {min-width:0;display:grid;justify-items:center;align-content:start;gap:6px}
.axis-toolbar {display:flex;align-items:center;gap:5px;min-width:0}
.axis-toolbar .step-switch {margin-left:auto}
.axis-heading {display:flex;align-items:baseline;flex-wrap:wrap;gap:2px 6px;width:100%}
.axis-heading small {color:var(--muted);font-variant-numeric:tabular-nums;white-space:pre}
.axis-heading strong {font-size:13px;font-weight:600}
.step-switch {display:flex;font-size:12px}
.step-switch label {position:relative;cursor:pointer}
.step-switch input {position:absolute;opacity:0;width:1px;height:1px}
.step-switch span {display:block;border:1px solid var(--line);padding:5px 8px;white-space:nowrap}
.step-switch label:first-child span {border-radius:5px 0 0 5px}
.step-switch label:last-child span {border-radius:0 5px 5px 0;border-left:0}
.step-switch input:checked + span {background:var(--blue);border-color:var(--blue);color:var(--panel);font-weight:600}
.jog-pad {position:relative;isolation:isolate;width:min(100%,196px);aspect-ratio:1;border-radius:50%}
#manualPanel .jog-pad button[data-jog] {position:absolute;inset:0;width:100%;height:100%;padding:0;border:0;border-radius:50%;background:#30453a;color:#f1f5ed;transform:rotate(var(--angle));clip-path:polygon(1px 0,calc(100% - 1px) 0,50% calc(50% - 1px));mask-image:radial-gradient(circle,transparent 24px,#000 25px)}
.jog-pad::after {content:"";position:absolute;inset:50% auto auto 50%;width:50px;height:50px;transform:translate(-50%,-50%);clip-path:circle(50%);z-index:1}
.jog-nozzle {position:absolute;inset:50% auto auto 50%;transform:translate(-50%,-50%);width:29px;height:29px;color:var(--muted);pointer-events:none}
.jog-pad [data-jog="y+"] {--angle:0deg}
.jog-pad [data-jog="x+"] {--angle:90deg}
.jog-pad [data-jog="y-"] {--angle:180deg}
.jog-pad [data-jog="x-"] {--angle:270deg}
.jog-pad [data-jog] span {position:absolute;top:16.5%;left:50%;transform:translate(-50%,-50%) rotate(calc(0deg - var(--angle)));line-height:1;font-size:17px;font-weight:600}
#manualPanel .jog-pad button[data-jog]:is(:hover,:focus-visible):enabled {background:#52735f}
#manualPanel .axis-toolbar button[data-manual="home"] {background:#eea274;color:#262e28}
#manualPanel .axis-toolbar button[data-manual="home"]:hover:enabled {background:#ffb688}
.mc-icon {display:inline-block;width:1.45em;height:1.45em;flex:none;background:currentColor;mask:var(--image) center/contain no-repeat;-webkit-mask:var(--image) center/contain no-repeat}
#manualPanel .jog-pad button[data-jog]:disabled {opacity:1;background:#48564e;color:#c6d0c9}
#manualPanel .axis-toolbar button[data-manual="home"]:disabled {opacity:1;background:#505c54;color:#c6d0c9;border-color:#9faea3}
#manualPanel button[data-travel-limited="true"]:enabled {filter:grayscale(1);opacity:.55}
.axis-toolbar button {display:flex;align-items:center;justify-content:center;gap:4px;min-height:28px;white-space:nowrap}
.axis-toolbar button svg {display:block;flex:none;width:15px;height:15px;pointer-events:none}
[data-released="false"] [data-lock-open],[data-released="true"] [data-lock-closed] {display:none}
.z-row {min-width:0;display:grid;grid-template-rows:auto 1fr;border-left:1px solid var(--line);padding-left:14px;gap:6px}
.z-row strong {align-self:center}
.motion-buttons {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.z-row .motion-buttons {grid-template-columns:1fr;grid-template-rows:1fr 46px 1fr;gap:5px}
#manualPanel .z-row button,.manual-services button {display:flex;align-items:center;justify-content:center;gap:6px;min-height:32px}
#manualPanel .z-row button {min-height:36px;padding-inline:4px}
.z-plate {place-self:center;width:min(100%,68px);height:42px;color:var(--muted)}
.z-row .mc-up {width:13px;height:15px}
.z-row [data-z-step^="-"] .mc-up {transform:rotate(180deg)}
.timer-row select,.timer-row button {min-height:32px}
#feedDistance {color:var(--muted)}
.nozzle-controls,.filament-actions {display:grid;grid-template-columns:minmax(0,1.45fr) repeat(2,minmax(0,1fr));align-items:center;gap:5px}
.nozzle-controls label {display:flex;align-items:center;flex-wrap:wrap;gap:3px 5px}
.filament-actions .motion-buttons {grid-column:2/-1;gap:5px}
.nozzle-controls button,.filament-actions button {min-height:32px}
.calibration-row {display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.85fr);align-items:center;gap:8px}
.calibration-row h3 {overflow-wrap:anywhere}
.manual-services {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
#manualPanel .manual-services button .mc-icon {width:1em;height:1em}
#manualPanel .manual-services button {min-width:0;font-size:12px;padding:4px}
#calibrationStatus {color:var(--muted);font-size:12px}
#calibrationProgress {margin-top:6px;background:linear-gradient(var(--blue),var(--blue)) left/30% 100% no-repeat var(--track);
  animation:calibration-progress 1.5s ease-in-out infinite alternate}
#calibrationProgress::-webkit-progress-bar {background:transparent}
#calibrationProgress::-moz-progress-bar {background:transparent}
@keyframes calibration-progress {to {background-position:right}}
@media (prefers-reduced-motion:reduce) {#calibrationProgress {animation:none}}
.timer-row {display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:stretch}
.timer-row label {display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);align-items:center;gap:6px;font-size:13px;font-weight:600;overflow-wrap:anywhere}
#manualPanel .timer-row select {min-width:0;width:100%}
.manual-summary {margin-left:auto;min-width:0;display:flex;align-items:center;justify-content:flex-end;color:var(--muted);font-size:12px;font-weight:400;text-align:right;line-height:1.25}
.manual-summary span {min-width:0;overflow-wrap:anywhere}
.manual-summary span:empty {display:none}
.manual-summary span:not(:empty) + span:not(:empty) {margin-left:7px;padding-left:7px;border-left:1px solid var(--muted)}

.manual-notices {display:grid;gap:4px}
.manual-notices:not(:has(p:not(:empty),button:not([hidden]))) {display:none}
.manual-notices button {display:inline-flex;align-items:center;justify-self:start;gap:6px}
.nozzle-controls .heat-short {display:none}
@container (max-width:360px) {
  #manualPanel .manual-body {padding-inline:8px}
  .manual-surface {gap:10px}
  .z-row {padding-left:8px}
  #manualPanel .axis-toolbar button {font-size:12px;padding-inline:5px}
  .axis-toolbar button svg {width:12px;height:12px}
  .axis-toolbar .step-switch span {padding-inline:5px}
  #manualPanel .nozzle-controls button,#manualPanel .nozzle-controls select {font-size:12px;padding-inline:5px}
  :is(:lang(ru),:lang(en)) .nozzle-controls .heat-long {display:none}
  :is(:lang(ru),:lang(en)) .nozzle-controls .heat-short {display:inline}
  .timer-row label {font-size:12px}
  :is(:lang(ru),:lang(en)) .timer-row label {grid-template-columns:max-content minmax(0,1fr)}
  #manualPanel .timer-row select {font-size:12px;padding-inline:4px}
}
@container (max-width:300px) {
  .axis-toolbar {gap:3px}
  #manualPanel .axis-toolbar button {font-size:11px;padding-inline:3px;gap:3px}
  .axis-toolbar button svg {width:11px;height:11px}
  .axis-toolbar .step-switch {font-size:11px}
  .axis-toolbar .step-switch span {padding-inline:3px}
}
.mc-level {--image:url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAE8AAAA/CAQAAAALH13MAAAFD0lEQVR42u2ZP2hkRRzHP795b/9EjdEQE5RDU8gJFlqIXKNcpY2CjVgI1nZ2WltaWMohihzHFep1J7HQgIocKEQQvUo4FBs9m+QM4bJJ9s3P4v3e7Mzu22Qvvk0W8Q3LsvNmZ77z/f2d38D/z3/2kQbneoAV9skQfmN39jb5Lsouyh4vAVkTE7sG4eVAC+ghTc3cNLxeU7w1A09qZpFZgScJGAEKMP5kVoQrBiWzjxuCfeq6R8LZzMDTRMguYXMm2NMIojPArjmZ5A3MoYdqpJ6+7ulUgmSjpqFjuJLTh1cAShHA6IhPnAHh+oQ9Db9kFoRbgvGHmsmpsqdB1D5yOXLyjqXeZfjEF5YfQQNAPUn2dIQXDd8+YlRDzwkJV8KikrA5SEJ9EvC8sXgMQd85vAwF3uISiyjtscmBhq3Ai2zwPNBu2m3Xb+dNFOUaZ4C2pfBwAWUbZZeXQx/AebZRNjlvo6f2lGn6O6i1n1gFOgblfZSdEXjPsmujt3gugd24cB1wP0+F309wlVX2bElJjCYnA86xRtf67+ONxn3tkFG0gce5HvhTfuQhe/uBCfc2rwB3A09yMxp5jUWgc3wnPYl4u8BZfo6W/Z6HgQ4XUW4HePA0f0aj1ukAHZxl01Piz9EFzvBdtPQvnAU+MXh7vAY8kzC3BkAXN314jruAB/k2AXiOCyj7KPu8wCqb0dsrYMxlBk+mBy8zgCt8FUH4Iwh8i/f4IXpzGcznucDe1OBl1uaAZdYjGPXt0+gUJwHc1OG1mAOW+PJQcFcSVyJRmyq8Fi0cc8ACa2PBXapNqWS6uudwZKbkXWCJL2rBfXhkXWEqAFMd6gDLfD0C7qMJMsZGmDxMY8TC/DLfTCDW+m0e05JlpI0b1wEW+TyAuzjh7LGayJ0KL22H7VDoAvNcjvxcNpEHHbQJAQ72NPjjUbtzZsXzfMw6C0D3yMUG8Fq0xsGTEWB1YizPCr72zFAuU860zz102aRNYcdKjYpE49YRO5MoSlEzfnafiqdHWWIvUtJUXQesiblmsb7MOPAUFo09HuhbScOFI6WiQa/Lf+WRRpdzePoUCB1+53oM8+qRsfNk20aczGcszJhUixJbKcBW6JolpVPIrcwgQJ8MuMk2rXCkFtM8pTCtSt1q5RAUb2U0HalO+WDBmlSjS7fSRkxnHV0W7X2rHJNHdbgCuMGrbLBgUOOShNoiqXOo1KMCX44qj99ZFG0qwP2wBQkQnX3vsMo6jwSlEyQfqqU7doEee/+qGBSXhCSyfx2pyBRhnhYHbIV1beP5UAHMI0CHfogH/THLu8CvUBhno2MkiQZFjWMfOBtoJ1UEQXMcRcTBAQfJ3v1YcII3IRRj/bwPBTQXYkjd1YMLoF3EraB5CCnx5Ykjx9vONBRy0kuWOPRVxhObhJj7liBgDYGOBE7JXpseraSCoJVwvYGBHTaBWw3US3UiVzUYsQvcohfJTtAKnlql9DHe5le69CMxFMHSsqH7Mg2ORE3UYoEqGyqJE42s6jASeQTBsccyK6lq5QagMF8zz+szEzX8wHL3AW8KHNeZZNoFw5Hyb7ler2KvVNRWsBp3yqGsfO4tVS435fyMv/kraESVihdRKpQm+nEc0ST7zS0OVBY9sOkqFXPBP3hLxfrWPB6PMM+NWcgBjlSdSe41XI3LaEqUcvh9h0ywv2wojvraa5T0/laPWFGi23M/NjZNYJfjLlea5W7MrP8Agc1mo7gc9oEAAAAASUVORK5CYII=)}
.mc-shaper {--image:url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAE0AAAA2CAQAAAAo5dw5AAAFp0lEQVR42u3ZX4xUdxUH8M+9l53dZYNVUlts2rU0BOvGAlqgIKVgIJI0wfpUExOiqVGjNRqjPojRFyXxwRetmuJbqYk2vGh4EF9KXGslsBKogGlIIaWmgE23QCqysDM/H+bs3dmdGXYWhgUTf/dh9zf33jPf3/n7PWcyN76y+Jt0deVdk9RlYMzrksZuwsq7pqubCNLtpr/ihiX0uEPF2O2mqWV2es1557zkC/puB8PWPXS7NOUasQy57NbAy2Qq4JlpwJLkDSvDSeYcXiZT6MM3WgBLkv0WdcWDrwNabj4+4nQJ5k2/9Fvj5X7HrTNmBc+VQIbdCz7lXcm45KyVXa0yHWosN4BHnAtgrxjEAn34kiSpSX58K4xZGMBPS51tRZ9MD9gjGZMc9cDcm7MPdzoawP7gvQSsDNtKyJ+fe2gLsE0tAHw5dDkB7R5HJJclz89teU9yY9gik3Day5inVpb5N+0Pias9eON1YTaRNM8V91mFy9jnVYxPKe9/CQMv9fDcQoOPW4I+7HOliUAedAJXsRq10tjXVRtmA+0K1suROefQNL0k/MOxkLnOYGgwu17SlM/iyWTAI7H7sxMtGdvBKFMfNRRBMqmz7GZBy7DO0ti96HKTOSsY8e+Q+zCqihJWmm330Dm0Kh7zHvCWv7Yh4yOOx26FPlfD/HMQBqsD4iGnWmS9qsKowyXNfKCUn6Sb52sZHvKhSBfDLjTdzaQwaX0tNRSarAW4qQfpqtY2GkRvOPt0nWUh7bAz8elKjDXBqvPgLvahCevj/yOOtemlqjjqqA+4qsfHvM87CtW2dkjIbbbBImOO2eP09XjlIq9G7fx5S7pUKPToww5JVXLGqrL8N+u4zoQ/7dQUlvycu2cXOD34TMlkt7UAVodW58BbXYonn2r7NXXA329B4c/Y2PJA1/DIX8Wrb1s+TQN5eWX6sdjfg/E+cw0Ww1eaYFUlyVudg+tBjwPx+u/Mn1J+pl51gS9IrkiGLWzbmK9o6C+SU15v2B33wc501o/HvB2vfbshsrMWpbvAd+LZCza0lfqTEshpW8AXG5qfFzrLaP34YbxSsx6VKZpq1vEmo/H8N9vE5oNOlr71KFHQthqLT7/V6VQkMxyesM9d6GvwrqyFXy70cpj0N22kfrXUz3dRkStU8KTkXzZ31sv2Yk1pzu0oVBQltNbr2TjKCR9ukdhzu+P+YfcGMaj7acUy96FQdFYNtpbufACVMruntno+GICWNPHdDKtsjPt7/VOmKkNVj6pXvKGQ1PIZJmT1fmBT7PY7jtTQD7QjOgecjDK1lvjqyeNsdKereMcfUQl5VCW5Qq0TAtWLR50Pc/4AWWnOdkW67p17wtsOuz/kTNybb6/kkmSvomF8MxlWmazRCyqebpFNEp5wR0kg6TU+g0FrcsmhiNbl1jQU9RxrbQiwL6pG5W1MvNMIwf2GJbumUb8KFjkeXfnvLUBvnK1VCEycuh+Pl+VqV+g/j4T8s9DnWeuuHYkZNpS5+OkG/lWfpE2mxs9SGrK1MSfKVi8G/CneO2sNBqL7X2c0jrq7E5K4xsUSwOYI5MJ8bHIxhiwjHU7OcnkwkO3lgGYnBHnfFR3+RPc/4/pFCe28teWnD5U5e0KfnQArYjw45LXQT/K1uF8vYZclI+7pLLHmjpQgxuyw3GJfj0Q7LnmpTaFuxcMmmduPGpx7t6c837D/Xmc6yzHkQssR6Lik6hMzQJpKjwqFfrzfwbaD1YWzIT6fbKj8U6/PdfRjRtYAbwLc5qYD1yQXbYn474hf1BlDs+bGPNnx7yyTabMOrwePN4RY/XrXE8g7HUbXhTFop/+UQi55tlNS16bDyrCkYfab/NqSMgVfs+nLYlo20fzD3YYsVnXSMaNdGhwOWuEuo/7m9SmaTmZ04luzrvndxe38Y6G2Jef/639w/Rf9bF4XxxmWAQAAAABJRU5ErkJggg==)}
.mc-clear {--image:url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEwAAAA/CAQAAADgKObPAAAHhklEQVR42u2ab2gcxxnGf97xusNUy5JDxNQIjEAcCEMwGILBuB8MxsUQTIyhdTGlpWBqQorBOJiCIQSCwU4b+Vs+KA1qwHEx/ZNQg1oTWlwHNW2CG0gErYITcHGd2jJS3dNJt7c7/bDvjWbv9nSKq5MS6OwH3e3t7Dz7vO/z/pkV/H98GYbqfcmmNVnI8A0OUOU+b/IG6Rfl2XdxA+uOm1Q3HlYIbGMGS40GlgY1LO8yKL9t6LiIpUkTSxOLpYHlPAAahVqNP/XDjE9yVyBNUOWimHOOPUBESLgRwACuYKljuUoMwK8F2hUiwGwUtOMCY5YdwCCwi7qc+w5g0BsBbBvTwtdpTwwvCbAPGXacbYDb17BcJxaPi4AhPhVo54AQs94C2MsslgYN9nvhIwKOOgnsAwxKPG2d4P1OzDjuFKpQGDSK1wXaVSeBdQscz1CnieUeo174CAmJgR3cEWjHgXD9GBvmb8LXmY5cEKKBk04Co2JOtR6sjUtQvUnUJVFFTAr0l+VcXz1NYYDDknwsx1bICfv5t0hgv5ee+gBMARqocFMy4uUei7ws4eQtIgetT7WEAc4LWzV297g+f4Q6lhOiWfrDlwF2UxNgL65i1mEaUqNt718hlOe8VpKeWeVSE8LZi/1TogaOuTr1+0CIRhECmhd4myu8zTlMYVaVW1gsd3myf7VXhQ8F1iQGiNDidxEfSAC5zVCbJ/3QSaVv43mBVWcvYDBoNAYwTIkCiyZWQMzvsdSpcaQ/sKrcFmA/ks5Io9FEhES8J7x8yohE+mVoR2jQxDLFYD+A/VSWniJ2+a8ILDfliBhZoxx3l7HMSSG0xuMgcyL9A55ONZqYkJj3hM07VIFYDB1KrtjDLJYas16J1GMEq3T90wzwABjjt+5sQkog5koLMlFABigCMhTv8CqQUuFUaXZ95HGWOndpMs22thCiidHEvOsYGwUqGCcOTQRU+QDLLJZTa8dYlaMkJCSMcafAYwpkpGRkci51vykgICAAIv7OK4Ah5TgjawXsOYZ5wNe4xmtdr0lXOJMQABNcQ3OfKs+uDbCDHGKJLfyLn5CULq9K4OQsZsLlEoYFLjDPVhb49mok0AtYyGke4yGPc4k/lP5evEPm/U0FYsoiS8A1xoEFBjmFLhHY56o8TlJjlhrTHZ6hvHARuf2eGUaBGC2RrthVDjGD5R5NTjw6YwoY5iSGEMMYHxeMptzMrM2MqTc/IyuY/x9cAAwZ32X7/xImXnct7WCBbNUR+W9IXsiZjSXylxnnLSz3XC/QaU7Ve+friJinyWFv2rKJtBf5r0tKmpaU1L1ly7NAnRqHSvzLfepmygTDWSABXuUaYFxU8k3nf1+SbNAycVZ653d4BcgwnGFQeoGAoACuawmuvCLnLjtdWvZdOpSip0JIhanCVkq8Yu+9lZviIuekTmnnV3VjLKXKD+TzGB9hCo6dSR7MZ6coUj4D/knKLZYKgigbn/Fj6SBOsE/cRrWxr1YqciyWKYZQxJL3QvdsLb4MERERVXazk11UidBut6J7mX7V1cIxyvlkz5h2wNX2R4DIK2PaXd+gMUTyew7TdMSvzu5hn+u2TroiPeyUQLsiW7XCZdFc5GqFUG6hHbT8bN6lGznbHZaS6+CcrHGbJ4BKm5+psjBx1rW0e1HEwkKn84cOKBxknElu8L2ebz5CNBVgmGlZZ0K49gWjOkneKXsOlufRGOGrDFZ+GEDzCyy3qDMpobgbX/njRAx6O7iWb7qg7NqYoC16wQtSY77PBCmh1FspaemrmFyhA3yVeeaZ5zGi0iKIQjJLSdD8nDfl3LMMMU/ozUt9YBEpx3hKvl3gE4yDVS7izD1QgwEMYc9Qkc9SJAwwz5jceQ9H3f1FAso9xVdYIGSCrQC8wUskbCGhSYrFsslVpK3DYuW3JSo8TkKNSX5ZylHreiWfICFkhgH2ALCd69xmCxlg5QrRSuTtA84x2vbKRbnY5WvR38PXxKWNhvKO3MNa/UAMbHM7QhelKSyECoUGIqeTZ7o4ru6A1qu460z8fuCJgEPSFk6zU+TnJfGUEHhCNnr/xM9WqN+WD78q6+7ogQsDytVouTwSFoHfMA6kjDDqp6XNXqUwIJ/f5yEQkjg+AhQhgfSJgafi3JHTkiIgBxR498i8fiAkEUs95C+cIGNRpJP3Xg4YwIJrPwa5T1JoLfKnW+1IZU6vkd/1afn2H2FMkS4DS4C/8jEjLDDMVX7FA1KUGEvJ8xcruJZJMmFhuWfy54QtDmROgwRISOT8fg6yiOYTbpU3gho4I5rMX8U03O70ykeTBvXC0VzFzHxOQ/7mqlTLzt/+sv4SR4EHrkoKHGdlBsvDb+KYC7wwHIr8O2clQCaeGZKgMfyRb3EHI2yWrKc5z9yqeFrL4xJDhXdOCrWp4AH52MHXiUnJnGepNlUts9XavfCbOuWYCzpqhQYJmfQGi6LOj/iz20nLlk2pvhj/J9FuvM09A+Tyvo7PzaMtHBRSedrme8rfC9m8wlK54IvhMfic4JRbMnAOETiTpd13jDb1fELldUc8EmPKS3+q0G2l3Wf8F2b0M2+JmP7CAAAAAElFTkSuQmCC)}
.mc-up {--image:url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACoAAAA4CAQAAAAh1KJAAAADoklEQVR42q2YPYhVRxTHfzP3vnv3+bK7KGElIDEpFAMWCmIlivgRUFktJGmCiF1A7AwpgoUiKHYqYqFFClFRcDVNMBAtBUEkRWRTJYF8YKEuaNb33L0nxZudOzPvfj13Z9nizpz5zzlz/mfOOU9RN5T3JTQYulZCDLRqCglRAxmFIqaFJmMJR2JV0EulaZsuE3zLMqaR4I4XoWWL6whv+WJpDO+bexlBEHrsWirIcwZSEF6xebGgCjjuQArCn6wrYEfj0QIOB5CC8JiVjouV+Ws0RoC9/FcAKkzRtkRTaHQVaH5mG9jCv4WQgnAFDaSArgZV9tQO8BnTpZCCcBaA1OzR5aD95VFgNY8rIQXhGyAiqbtTjaYDLOdBLaQgHAESkmo3KdpAylQjSKHHASCtfhESIuCqt/E3rvDSfn3PSW/1DTvMzZYa34JgU8ZWtjHjeB0ueBIv2ei8Y4VkP1bgjEnm7Pd9ElJ+9mSm+djsHhgpcCiAPAXAQd7amXtMAB/xyJN7yPIi2BHgc2ezIFw0wTjJrAM6TgR8yjNP9gZJeAktYBN/eWLX0MAKYL8TrHcZAz4ENvCHJ3/JyxFEwHqeeiI/mOjuBKBTdIAOo8BOx4GCcIYkf2hS4Gtv+SfGjP7LgH28sSt3DJP75PvSu7DnrM11jYAxzhsKCY+YcPIT7HFAb5Oa4/oaHWberMzwFdrNen2/nUAQfmFtwIndvLagt2jZjNAn/FEyhL/Za5GUuxm+4ynbB7i7g1cW9Cba2dbX6jS/Mgm0GEHlIauMoS0+KQiI7bxwqKMGcu04awwtzcsa2xCdJaXL7wFoFlRQYeHTI2GGGTQxvYXJ2AprekREvCuomKqetp4piObyI2MDqRAgY37gkZkvgFfOwZq58NhcU9W0pnNAFyrBoCLUDSpPPwMpT6/BRCL19akGL1cqIjPjwmV+kRlXGJeDRoHW+TFibZTcEl1ZA2CorgrMV8GMqtY09zF+NDs6uizwNZVqTfPzVYFjtcOBwFlx474k9HY2fHfSrNHJ3SXDtTzFbZAUMKXW/PKGQex/QSyVayoBA/3LEKurlF2MrujyFjYNysyHt9jMUVLCAF8/GUZTqjctpuGVUr7K+4K6tFGNcoCVqTM/q46dwnAQXUvz4X8hqBXJAqOVl/dLlGgC2qULCLNNr6JJ7KeM2SZjlKjeXXEN4BzwDz+yioyMD3hCtyBxD1Em5GMlbRQRMc95UfY2Lb5hrxz/A+2CzoR0K+ciAAAAAElFTkSuQmCC)}
#slots{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;padding:8px 13px 15px}
.slot{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:8px 2px;text-align:center;font-size:12px;overflow-wrap:anywhere}
.slot.active{border-color:var(--blue)}
button.slot {position:relative;min-width:0;line-height:inherit;color:var(--text)}
button.slot[data-icon]::before {position:absolute;top:7px;right:6px;margin:0;color:var(--blue)}
button.slot:hover {border-color:var(--blue);background:var(--panel)}
#slotColors {display:grid;grid-template-columns:repeat(8,minmax(25px,1fr));gap:5px;margin:8px 0}
#slotColors button {min-height:32px;background:var(--swatch);border:1px solid var(--line)}
#slotColors button[aria-pressed=true] {outline:2px solid var(--text);outline-offset:1px}
#discoveredPrinters {display:grid;gap:4px;margin-block:8px}
#discoveredPrinters:empty {display:none}
#printSource {margin-block:8px}
#printSource input {display:block;width:100%;margin-block:4px}
#printSelection,#sourceStatus {overflow-wrap:anywhere}
#printSlots label {display:grid;gap:4px;margin-block:8px}
#printSlots select {width:100%;min-width:0}
.filament-colors {display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;font-size:12px;color:var(--muted)}
.filament-mark {display:inline-block;width:12px;height:12px;margin-inline-end:5px;border:1px solid var(--line);border-radius:50%;vertical-align:-2px}
.filament-warning {color:var(--warn)}
.spool{margin:5px auto;width:34px;height:34px;border-radius:50%;background:radial-gradient(circle,var(--panel) 0 14%,transparent 16%),repeating-radial-gradient(circle,var(--spool) 0 2px,color-mix(in srgb,var(--spool) 70%,#555) 3px)}
.printer-fields{display:grid;grid-template-columns:1fr 1fr;gap:6px;border:1px solid var(--line);border-radius:7px;margin:6px 0;padding:9px}
.printer-fields label{min-width:0}
.printer-fields input{min-width:0}
.settings input[type="checkbox"]{width:auto}
#infoTitle {overflow-wrap:anywhere}
#infoBody{white-space:pre-line;overflow-wrap:anywhere;margin:12px 0}
.printer-check-status {grid-column:1/-1;margin:0;overflow-wrap:anywhere}
.printer-check-status:empty,#settingsSaveStatus:empty {display:none}
.printer-check-status[data-result=success] {color:var(--ok)}
.printer-check-status[data-result=error] {color:var(--warn)}
#settingsDialog {width:min(680px,calc(100% - 24px));max-height:90vh;overflow:hidden}
dialog.settings #settingsForm {display:flex;max-height:calc(90vh - 2px);padding:0;gap:0}
#settingsTitle {flex:none;margin:0;padding:16px 20px 12px;font-size:17px}
#settingsLayout {display:grid;grid-template-columns:minmax(160px,192px) minmax(0,1fr);grid-template-rows:minmax(0,1fr);
  min-height:min(245px,50vh);overflow:hidden;border-top:1px solid var(--line)}
#settingsSections {display:flex;flex-direction:column;gap:3px;padding:10px 10px 10px 16px;
  border-inline-end:1px solid var(--line);overflow:auto}
#settingsSections button {display:flex;align-items:center;gap:7px;text-align:start;min-width:0;
  background:transparent;color:var(--muted);border-color:transparent;overflow-wrap:anywhere}
#settingsSections button::before {flex:none;margin:0}
#settingsSections button:hover {background:var(--bg);color:var(--text)}
#settingsSections button:focus-visible {outline:2px solid var(--blue);outline-offset:-2px}
#settingsSections button[aria-pressed="true"] {border-color:var(--blue);color:var(--text);background:var(--bg)}
#settingsContent {min-width:0;min-height:0;overflow:auto;padding:10px 20px 14px;scrollbar-gutter:stable}
.settings-fields {display:grid;gap:2px;padding-block:3px}
.settings-fields label {display:flex;align-items:center;justify-content:space-between;gap:8px 14px;flex-wrap:wrap;
  margin:0;padding:9px 0;font-size:13px;color:var(--text);border-bottom:1px solid var(--line)}
.settings-fields label:last-child {border-bottom:0}
.settings-fields select {min-width:min(100%,150px);max-width:100%;font:inherit;color:inherit;background:var(--bg);
  border:1px solid var(--line);border-radius:4px;padding:6px 8px}
.settings-fields input[type="checkbox"] {margin:2px 0;accent-color:var(--blue)}
#settingsFooter {flex:none;background:var(--panel);border-top:1px solid var(--line);padding:10px 20px}
#settingsFooter .settings-actions {margin:0}
#settingsSave {color:var(--panel)}
#settingsSaveStatus {margin:0 0 8px;overflow-wrap:anywhere}
@media(max-width:520px) {
  #settingsLayout {grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);min-height:0}
  #settingsSections {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;
    padding:8px 12px;border-inline-end:0;border-bottom:1px solid var(--line);max-height:38vh}
  #settingsSections button {padding-inline:5px;gap:5px;font-size:12px}
  .settings-fields label {gap:6px 8px}
  .settings-fields select {min-width:min(100%,116px)}
  #settingsTitle,#settingsContent,#settingsFooter {padding-inline:12px}
}
@media(max-width:640px){.tabs{gap:4px}.view-tabs button{padding-inline:10px}
.tabs button{font-size:12px}
}
@media(max-width:420px){
  .view-tabs {flex-wrap:wrap;max-width:100%;gap:2px}
  .view-tabs button {padding-inline:7px}
  .view-tabs [data-icon]::before {margin-inline-end:.25em}
}

.section-icon {width:17px;height:17px;vertical-align:-3px;margin-right:8px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;color:var(--blue)}

#manualPanel .section-icon {margin:0}
.panel > .panel-title:first-child {border-radius:11px 11px 0 0}
details.panel:not([open]) > summary.panel-title {border-radius:11px}
.panel-title {padding:11px 16px}
.panel > .tool-body {padding-inline:16px}
.dashboard, .monitor-main, .printer-sidebar {gap:12px}
.foldable-panel > summary {display:flex;align-items:center;gap:8px;list-style:none}
.foldable-panel > summary::-webkit-details-marker {display:none}
.foldable-panel > summary::before {content:'';flex:0 0 6px;height:6px;border:solid var(--muted);
  border-width:0 1.5px 1.5px 0;transform:rotate(-45deg)}
.foldable-panel[open] > summary::before {transform:rotate(45deg) translateY(-2px)}
.foldable-panel > summary:focus-visible {outline:2px solid var(--blue);outline-offset:-3px}
.foldable-title {display:flex;align-items:center;gap:6px;white-space:nowrap}
.foldable-meta {min-width:0;margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:12px;font-weight:400;color:var(--muted)}
.foldable-panel[open] .foldable-meta:not(.chart-trend),.foldable-meta-compact,.chart-trend-compact {display:none}
.chart-controls {display:flex;justify-content:flex-end;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.chart-controls label {display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.chart-controls select {font:inherit}
.catalog .file-controls {flex-wrap:wrap;gap:8px}
.catalog #refresh {margin-left:auto}
.catalog #printSelected:enabled {background:var(--blue);color:var(--panel);font-weight:600}
.catalog .file-card {display:block;width:100%;padding:0;text-align:left}
.catalog button.file-card:hover {border-color:var(--blue)}
.catalog button.file-card:focus-visible {outline:2px solid var(--blue);outline-offset:2px}
.catalog button.file-card[aria-pressed="true"] {border-color:var(--blue);box-shadow:inset 0 0 0 1px var(--blue)}
.catalog button.file-card[aria-pressed="true"] .file-name {color:var(--blue);font-weight:600}
.catalog .file-note {margin:0 16px 10px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
@media(max-width:460px) {
  .foldable-panel:not([open]) .foldable-meta-wide {display:none}
  .foldable-panel:not([open]) .foldable-meta-compact {display:block}
  .chart-trend-wide {display:none}
  .chart-trend-compact {display:inline}
}
.catalog .file-controls {padding:0 16px 10px}
.catalog .files {padding-inline:16px}
.readings .readouts {gap:10px 16px}


@media (max-width:820px) {
  .dashboard #jobPanel {order:-3}
  .dashboard #manualPanel {order:-1}

  .dashboard .readings {order:-2}
}

#jobPanel {grid-column:1 / -1}
:root[data-tall-job-card="on"] #jobPanel {min-block-size:210px}
:root[data-tall-job-card="on"] #jobPanel .jobs-body {align-content:center;gap:8px}
#placementNotice {margin:0 0 12px;color:var(--warn)}
#setupDialog {width:min(520px,calc(100vw - 24px))}
#setupDialog form {max-height:calc(90dvh - 2px);overflow:auto}
#setupDialog select,#displayPlacement {max-width:100%;min-width:0}
#setupDialog .settings-actions {flex-wrap:wrap}
#setupCandidates button {margin-top:4px}
#setupDialog .printer-fields {border:0;padding:0}
#setupDialog .printer-fields .caption {grid-column:1 / -1}
#setupStatus:empty,#setupCandidates:empty {display:none}
#cameraSettings .tool-body {padding:14px 16px;overflow-wrap:anywhere}
#cameraSettings .camera-form {padding:0;flex-direction:row}
#cameraSettings .caption:empty {display:none}
#cameraSettings .row {margin-top:10px;justify-content:flex-end}

#status:not(.disconnected) + #diagnose,#status[hidden] + #diagnose {display:none}
#job {display:contents}
#openJob {display:block;width:fit-content;max-width:100%;padding:0;border:0;background:none;
  color:var(--text);text-align:left;font-size:inherit}
#openJob:hover {color:var(--blue);text-decoration:underline}
#job[data-active="true"] .jobs-model {cursor:pointer}
#jobs {display:grid;gap:12px;padding-top:8px}
.fleet-toolbar,.fleet-tools,.fleet-filters {display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.fleet-toolbar {justify-content:space-between;gap:10px 16px}
.fleet-filters button {display:flex;align-items:center;gap:6px;background:transparent;color:var(--muted);white-space:nowrap}
.fleet-filters button[aria-pressed="true"] {background:var(--panel);border-color:var(--blue);color:var(--text)}
.fleet-filters [data-count] {font-size:11px;min-width:1.5em;text-align:center;color:var(--muted)}
.fleet-tools {flex:1 1 290px;justify-content:flex-end}
#fleetSearch {flex:1 1 135px;width:auto;max-width:235px;font:inherit}
#fleetCameras[aria-pressed="true"] {border-color:var(--blue);color:var(--blue)}
#fleetCameras[aria-pressed="false"] {background:transparent;color:var(--muted)}
#jobsList {display:grid;gap:8px;margin:0;padding:0;list-style:none;grid-template-columns:minmax(0,1fr)}
.jobs-card {display:grid;grid-template-columns:minmax(138px,16%) minmax(0,1fr);grid-template-areas:"media job";
  background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.jobs-card:has(.jobs-camera:not([hidden])) {
  grid-template-columns:minmax(138px,16%) minmax(0,1fr) clamp(140px,20vw,220px);
  grid-template-areas:"media job camera"}
.jobs-printer-heading {display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:3px}
.jobs-printer-identity {display:flex;align-items:baseline;flex-wrap:wrap;min-width:0;gap:4px 10px}
.jobs-printer-heading :is(h2,h3) {margin:0;font-size:15px;line-height:1.25;overflow-wrap:anywhere}
#job .jobs-printer-heading {flex-wrap:wrap}
.monitor-job-tools {display:flex;align-items:center;gap:6px;margin-inline-start:auto}
#job .monitor-job-tools .jobs-link {padding:2px 4px}
#monitorHistory::before {margin-inline-end:0}
.jobs-printer-heading a {flex:none}
.jobs-state {display:flex;align-items:baseline;gap:6px;margin:0;font-size:12px;color:var(--muted)}
.jobs-state::before {content:"";display:inline-block;flex:0 0 6px;height:6px;align-self:center;border-radius:50%;background:currentColor}
.jobs-card[data-group="active"] .jobs-state,.jobs-card[data-ready="true"] .jobs-state {color:var(--ok)}
.jobs-card[data-group="attention"] .jobs-state {color:var(--warn)}
.jobs-printer-heading :is(a,.jobs-link),.jobs-actions :is(a,.jobs-link) {color:var(--blue);font-size:12px;font-weight:600;text-decoration:none}
.jobs-card a:hover {text-decoration:underline}
.jobs-body {grid-area:job;min-width:0;display:grid;align-content:start;gap:5px;padding:8px 10px}
.jobs-file-row {display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.jobs-file {font-weight:600;overflow-wrap:anywhere;min-width:0}
.jobs-file-row strong {flex:none;color:var(--blue)}
:is(.jobs-card,#job)[data-active="false"] .jobs-file {color:var(--muted);font-weight:400}
.jobs-card progress {height:6px}
.jobs-meta,.jobs-events,.jobs-last {font-size:12px;color:var(--muted)}
.jobs-meta:has(>span:empty):has(>strong:empty) {display:none}
.jobs-last {display:flex;flex-wrap:wrap;gap:2px 6px}
.jobs-last strong {font-weight:600;color:var(--text);overflow-wrap:anywhere;min-width:0}
.jobs-completion-hint {color:var(--warn);font-size:12px}
.jobs-actions {display:flex;align-items:center;flex-wrap:wrap;gap:6px 10px}
.jobs-actions:not(:has(:is(button,a):not([hidden]))) {display:none}
.jobs-actions :is(a,.jobs-link) {padding-block:4px}
.jobs-card .jobs-link {border:0;background:none;padding-inline:0}
.jobs-card .jobs-link:enabled:hover {text-decoration:underline}
#cameraOpen {padding-block:0;flex:none}
#jobMeta,#jobMetrics #time {display:inline-flex;flex-wrap:wrap;gap:4px 10px}
#jobMeta span:empty {display:none}
#jobMetrics:not(:has(>span:not([hidden]))) {display:none}
.jobs-body details {font-size:12px;color:var(--muted)}
:root[data-fleet-details="off"] .jobs-body details {display:none}
.jobs-body summary {cursor:pointer;width:fit-content}
.jobs-body details[open] {border-top:1px solid var(--line);padding-top:6px}
.jobs-card dl {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:8px 0}
.jobs-card dd {color:var(--text)}
.jobs-events {list-style:none;margin:0;padding:0}
.jobs-events li + li {margin-top:3px}
[data-job-result]:empty {display:none}
.jobs-media {grid-area:media;display:grid;grid-template-rows:minmax(86px,1fr) auto;min-width:0}
.jobs-media figure,.jobs-camera {min-width:0;margin:0}
.jobs-model {display:grid}
.jobs-model-actions {justify-content:center;gap:4px;padding:0 4px 4px}
.jobs-model-actions button {padding:4px 6px;font-size:12px}
.jobs-camera {grid-area:camera;position:relative;
  align-self:stretch;width:100%;min-height:0;aspect-ratio:16/10}
.jobs-camera figcaption {position:absolute;bottom:6px;left:6px;padding:2px 5px;border-radius:4px;
  background:#151a18d9;color:#fff;font-size:11px;pointer-events:none}
.jobs-camera:not(:has([data-stream]:not([hidden]))) figcaption {visibility:hidden}
.jobs-camera .jobs-media-frame {position:absolute;inset:0;border-radius:0}
.jobs-media-frame,#jobsCameraFrame {position:relative;display:grid;place-items:center;overflow:hidden;
  background:var(--bg);border-radius:6px}
.jobs-media-frame img,#jobsCameraFrame img {position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.jobs-model .jobs-media-frame {background:transparent;border-radius:0}
.jobs-camera .muted,.jobs-fallback {padding:3px 4px;text-align:center;font-size:11px}
.jobs-fallback {display:grid;justify-items:center;gap:3px;color:var(--muted)}
.jobs-fallback svg {width:22px;height:22px}
.jobs-model:has([data-preview]:not([hidden])) .jobs-fallback {visibility:hidden}
.jobs-camera-tools {position:absolute;top:4px;right:4px;display:flex;gap:4px}
.jobs-camera-tools button {display:grid;place-items:center;width:28px;height:28px;padding:0;background:var(--panel)}
.jobs-camera-tools svg,.camera-expand-icon {width:14px;height:14px}
.jobs-camera-tools button[data-icon]::before {margin:0}
#jobsNotice {padding:16px}
#jobsNotice button {margin-top:8px}
#jobsCameraDialog {width:min(680px,calc(100% - 24px));padding:12px}
#jobsCameraFrame {aspect-ratio:16/10;margin-top:10px}
@media(pointer:coarse){.jobs-camera-tools button{width:44px;height:44px}}
@media(max-width:650px){
  .fleet-tools {justify-content:flex-start}
  #fleetSearch {max-width:none;min-width:118px}
}
@media(max-width:540px){
  .jobs-card {grid-template-columns:minmax(0,1fr);grid-template-areas:"job" "media"}
  .jobs-card:has(.jobs-camera:not([hidden])) {
    grid-template-columns:minmax(142px,41%) minmax(0,1fr);grid-template-areas:"job job" "media camera"}
  .jobs-card:not(:has(.jobs-camera:not([hidden]))) .jobs-media {width:min(100%,160px)}
  .fleet-tools {flex-basis:100%}
}
@media(max-width:345px){#fleetSearch {flex-basis:100%}}
main:has(.dashboard.stale) #job > * {opacity:.65}
#otherTools {display:flex;align-items:baseline;gap:6px 14px;flex-wrap:wrap;padding:0 16px 12px;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
#otherTools span {white-space:nowrap}
.detail-groups {display:grid;gap:8px}
.detail-groups .readouts {padding:0;gap:8px 14px}
.detail-groups .readout {padding:0;border:0}
#printerSensors {margin:0}
#printerSensors:empty {display:none}
#history .totals {margin-bottom:12px}
#history .totals dd {font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
#historyStatus {margin-bottom:6px;white-space:pre-line;overflow-wrap:anywhere}
.history-controls {display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}
.history-controls label {display:inline-flex;align-items:center;gap:6px;cursor:pointer}
.history-controls input[type="checkbox"] {width:auto;margin:0;accent-color:var(--blue)}
#historyPrinter {max-width:100%;min-width:0;font:inherit;color:inherit;background:var(--panel);
  border:1px solid var(--line);border-radius:4px;padding:6px 8px}
.preview {background:var(--bg);color:var(--muted)}
:root[data-density="compact"] .tabs {margin-bottom:9px}
:root[data-density="compact"] .dashboard,
:root[data-density="compact"] .monitor-main,
:root[data-density="compact"] .printer-sidebar,
:root[data-density="compact"] #jobsList {gap:6px}
:root[data-density="compact"] .panel-title {padding-block:7px}
:root[data-density="compact"] .jobs-body,
:root[data-density="compact"] .file-name {padding-block:6px}
:root[data-density="compact"] .chart {height:126px}
</style>
</head>
<body>
<main>
  <nav class="tabs" aria-label="{{t:Device navigation}}">
    <div class="view-tabs"><button id="monitorTab" data-icon="monitor">{{t:Monitor}}</button><button id="fleetToggle" data-icon="fleet">{{t:All printers}}</button><button id="historyTab" data-icon="history">{{t:History}}</button>
    </div><div class="view-actions"><select id="devicePicker" aria-label="{{t:Open printer}}" hidden></select><button id="notificationsButton" data-icon="bell">{{t:Notifications}}</button><button id="settingsButton" data-icon="settings" aria-haspopup="dialog">{{t:Settings}}</button>
  </div></nav>
  <p class="caption" id="placementNotice" role="status" hidden></p>

  <section class="panel" id="setup" hidden>
    <h2 class="panel-title">{{t:Set up your first printer}}</h2>
    <div class="tool-body setup">
      <button id="openSetup" type="button" data-icon="plus">{{t:Add printer}}</button>
      <p class="caption" id="setupOutcome" role="status" hidden></p>
    </div>
  </section>
  <section id="jobs" aria-label="{{t:All printers}}" hidden>
    <div class="fleet-toolbar">
      <div class="fleet-filters" role="group" aria-label="{{t:Filter by printer status}}">
        <button type="button" data-fleet-filter="all" aria-controls="jobsList" aria-pressed="true">{{t:All}} <span data-count></span></button>
        <button type="button" data-fleet-filter="active" aria-controls="jobsList" aria-pressed="false">{{t:Printing}} <span data-count></span></button>
        <button type="button" data-fleet-filter="idle" aria-controls="jobsList" aria-pressed="false">{{t:Idle}} <span data-count></span></button>
        <button type="button" data-fleet-filter="attention" aria-controls="jobsList" aria-pressed="false">{{t:Attention}} <span data-count></span></button>
      </div>
      <div class="fleet-tools">
        <input type="search" id="fleetSearch" aria-label="{{t:Search printers and jobs}}" placeholder="{{t:Search…}}">
        <button type="button" id="fleetCameras" data-icon="camera" aria-label="{{t:Live cameras}}" aria-pressed="true">{{t:Cameras}}</button>
        <button type="button" id="fleetRefresh" data-icon="refresh">{{t:Refresh}}</button>
      </div>
    </div>
    <ul id="jobsList" aria-label="{{t:Printer jobs}}" hidden></ul>
    <div id="jobsNotice" class="panel" hidden>
      <p class="muted" role="status"></p>
      <button type="button" id="fleetClear" data-icon="close">{{t:Clear filters}}</button>
    </div>
    <template id="jobTile">
      <li class="jobs-card">
''' + FLEET_JOB_CONTENT + r'''        <figure class="jobs-camera" hidden>
          <div class="jobs-media-frame">
            <img data-stream alt="" hidden><span class="muted" role="status" hidden></span>
            <div class="jobs-camera-tools">
              <button type="button" data-camera aria-label="{{t:Enlarge camera}}" title="{{t:Enlarge camera}}" hidden>
''' + CAMERA_EXPAND_ICON + r'''
              </button>
              <button type="button" data-snapshot aria-label="{{t:Save snapshot}}" title="{{t:Save snapshot}}" hidden>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M3 7h4l2-3h6l2 3h4v13H3z"/><circle cx="12" cy="13" r="3.5"/></svg>
              </button>
            </div>
          </div>
          <figcaption>{{t:Live camera}}</figcaption>
        </figure>
      </li>
    </template>
  </section>
  <dialog id="jobsCameraDialog" aria-labelledby="jobsCameraName">
    <div class="row"><h2 id="jobsCameraName"></h2><form method="dialog"><button type="submit" data-icon="close">{{t:Close}}</button></form></div>
    <div id="jobsCameraFrame"></div>
  </dialog>
  <dialog class="settings" id="cameraSettings" aria-labelledby="cameraSettingsTitle">
    <div class="tool-body">
      <h2 id="cameraSettingsTitle">{{t:Camera}}</h2>
      <form class="camera-form" id="cameraForm">
        <label for="cameraUrl">{{t:MJPEG stream URL (HTTP or HTTPS)}}</label>
        <input id="cameraUrl" type="url" maxlength="2048" placeholder="http://…" autocomplete="off">
        <button id="cameraStart" type="submit" data-icon="play">{{t:Connect}}</button>
        <button id="cameraStop" type="button" data-icon="unplug" hidden>{{t:Disconnect}}</button>
        <button id="cameraLight" type="button" data-icon="light" aria-label="{{t:Chamber light}}" aria-pressed="false" hidden>{{t:Light off}}</button>
      </form>
      <p class="caption" id="cameraStatus" role="status"></p>
      <p class="caption" id="lightStatus" role="status"></p>
      <div class="row"><button id="cameraSettingsClose" type="button">{{t:Close}}</button></div>
    </div>
  </dialog>
  <div class="dashboard" id="monitor">
    <section class="jobs-card" id="jobPanel" aria-labelledby="job-title">
      <div id="job" data-active="false">
''' + MONITOR_JOB_CONTENT + r'''      </div>
      <figure class="jobs-camera" id="camera" hidden>
        <div class="jobs-media-frame">
          <img id="cameraImage" data-stream alt="{{t:Printer camera feed}}" referrerpolicy="no-referrer" hidden>
          <div class="jobs-camera-tools">
            <button id="cameraExpand" type="button" aria-label="{{t:Enlarge camera}}" title="{{t:Enlarge camera}}">''' + CAMERA_EXPAND_ICON + r'''</button>
            <button id="cameraSnapshot" type="button" data-icon="camera" aria-label="{{t:Save snapshot}}" title="{{t:Save snapshot}}"></button>
          </div>
        </div>
        <figcaption>{{t:Live camera}}</figcaption>
      </figure>
    </section>
    <div class="monitor-main">
      <details class="panel readings foldable-panel" id="readingsPanel" aria-labelledby="readouts-title" open>
        <summary class="panel-title">
          <span class="foldable-title" id="readouts-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 13h4l3-8 4 14 3-7h4"/></svg>{{t:Printer Readings}}</span>
          <span class="foldable-meta foldable-meta-wide" id="readingsSummary">{{t:Waiting for readings}}</span>
          <span class="foldable-meta foldable-meta-compact" id="readingsSummaryCompact">{{t:Waiting for readings}}</span>
        </summary>
        <dl class="readouts">
          <div class="readout" id="singleNozzle"><dt id="nozzleLabel">{{t:Nozzle}}</dt><dd><strong id="nozzle">—</strong> <span class="muted" id="nozzleTarget"></span></dd></div>
          <div class="readout"><dt>{{t:Bed}}</dt><dd><strong id="bed">—</strong> <span class="muted" id="bedTarget"></span></dd></div>

          <div class="readout" id="chamberReading" hidden><dt>{{t:Chamber}}</dt><dd id="chamber"></dd></div>
          <div class="readout" id="fanReading" hidden><dt>{{t:Part Cooling Fan}}</dt><dd><strong id="fan"></strong></dd></div>
          <div class="readout" id="speedReading" hidden><dt>{{t:Print Speed}}</dt><dd><strong id="speed"></strong></dd></div>
        </dl>
        <div id="otherTools" hidden></div>
        <div class="connection-footer">
          <div class="connection-meta"><p id="freshness"></p></div>
        </div>
      </details>
    <details class="panel foldable-panel" id="temperaturesPanel" aria-labelledby="chart-title" open>
      <summary class="panel-title">
        <span class="foldable-title" id="chart-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 4v16h16 M7 15l4-5 4 2 5-7"/></svg>{{t:Temperatures}}</span>
        <span class="foldable-meta chart-trend" title="{{t:Temperature change over the last 5 minutes}}">
          <span class="chart-trend-wide" id="chartTrend">{{t:Collecting readings…}}</span><span class="chart-trend-compact" id="chartTrendCompact">{{t:Collecting readings…}}</span>
        </span>
      </summary>
      <div class="tool-body">
        <div class="chart-controls">
          <select id="chartTool" aria-label="{{t:Chart nozzle}}" hidden></select>
          <label>{{t:Range}} <select id="chartRange"><option value="5">{{t:5 min}}</option><option value="15">{{t:15 min}}</option><option value="30" selected>{{t:30 min}}</option></select></label>
        </div>
        <div id="chartPlots">
          <canvas class="chart" id="chart" role="img" aria-label="{{t:Chart of actual and target temperatures for the nozzle and bed}}"></canvas>
          <canvas class="chart" id="chartBed" role="img" aria-label="{{t:Bed}}" hidden></canvas>
        </div>
        <p class="caption" id="chartSample"></p><div class="chart-legend muted">
          <span class="nozzle-series" id="chartNozzle" data-chart-line="nozzle">{{t:Nozzle}}</span><span class="nozzle-series target" id="chartNozzleTarget" data-chart-line="nozzleTarget">{{t:Nozzle Target}}</span>
          <span class="bed-series" data-chart-line="bed">{{t:Bed}}</span><span class="bed-series target" data-chart-line="bedTarget">{{t:Bed Target}}</span>
        </div>
      </div>
    </details>
    <details class="panel catalog foldable-panel" id="files-section" aria-labelledby="files-title" open>
      <summary class="panel-title">
        <span class="foldable-title" id="files-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 7h7l2 2h9v10H3z M3 7V5h7l2 2h7v2"/></svg>{{t:Printer Files}}</span>
        <span class="foldable-meta" id="filesSummary">{{t:Loading…}}</span>
      </summary>
      <div class="row file-controls">
        <input id="search" type="search" placeholder="{{t:Search files}}" aria-label="{{t:Search files}}">
        <span class="muted" id="fileCount" role="status"></span>
        <button id="refresh" type="button" data-icon="refresh">{{t:Refresh}}</button>
        <button id="uploadFile" type="button" hidden>{{t:Upload file…}}</button>
        <button id="printSelected" type="button" data-icon="print" disabled hidden>{{t:Print…}}</button>
      </div>
      <p id="fileSelection" class="file-note" hidden></p>
      <p id="filePrintStatus" class="file-note" role="status" hidden></p>
      <div class="files" id="files" role="group" aria-label="{{t:Stored files}}"></div>
      <div class="row pager" id="pager" hidden>
        <button id="previous" type="button" aria-label="{{t:Previous page}}" title="{{t:Previous page}}">‹</button>
        <form id="pageJump">
          <input id="pageNumber" type="text" inputmode="numeric" autocomplete="off" aria-label="{{t:Page number}}" aria-describedby="pageTotal" title="{{t:Enter a page number}}">
          <span aria-hidden="true">/</span><span id="pageTotal" class="muted"></span>
        </form>
        <button id="next" type="button" aria-label="{{t:Next page}}" title="{{t:Next page}}">›</button>
      </div>
    </details>

    </div>
    <div class="printer-sidebar" id="printerTools">
      <details class="panel" id="manualPanel" open hidden>
        <summary class="panel-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7h6m4 0h6 M4 17h10m4 0h2 M10 4h4v6h-4z M14 14h4v6h-4z"/></svg>{{t:Manual control}}<span class="manual-summary" role="status" aria-live="polite" aria-atomic="true"><span id="manualActivity"></span><span id="manualHomeNotice"></span></span></summary>
        <div class="tool-body manual-body" id="manualBody">
          <div class="manual-notices">
            <button data-group="clear" data-manual="clear" hidden><span class="mc-icon mc-clear" aria-hidden="true"></span>{{t:Clear error}}</button>
          </div>
          <section class="manual-group" data-group="motion" aria-label="{{t:Axes}}" hidden>
            <div class="axis-toolbar">
              <button data-manual="home" aria-label="{{t:Home all axes}}" title="{{t:Home all axes}}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="m2.5 11 9.5-8 9.5 8M5 9.5V21h5v-6h4v6h5V9.5"/></svg>{{t:Home}}</button>
              <button type="button" data-manual="release" data-group="release" data-released="false" aria-label="{{t:Release motors}}" title="{{t:Release motors}}" hidden>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true" focusable="false">
                  <rect x="5" y="11" width="14" height="10" rx="2"/>
                  <path data-lock-closed d="M8 11V8a4 4 0 0 1 8 0v3"/>
                  <path data-lock-open d="M8 11V8a4 4 0 0 1 7.5-2"/>
                </svg><span>{{t:Release motors}}</span>
              </button>
              <div class="step-switch" id="jogStep" role="radiogroup" aria-label="{{t:Axis movement step}}">
                <label><input name="xyStep" type="radio" value="1" checked><span>{{t:1 mm}}</span></label>
                <label><input name="xyStep" type="radio" value="10"><span>{{t:10 mm}}</span></label>
              </div>
            </div>
            <div class="manual-surface">
              <div class="manual-xy">
                <div class="axis-heading"><strong>XY</strong><small id="xyPosition"></small></div>
                <div class="jog-pad" tabindex="0" role="group" aria-label="{{t:XY movement. Arrow keys move 1 mm, Shift moves 10 mm.}}">
                  <button data-jog="y+" aria-label="{{t:Move Y positive}}"><span>Y+</span></button>
                  <button data-jog="x-" aria-label="{{t:Move X negative}}"><span>X−</span></button>
                  <button data-jog="x+" aria-label="{{t:Move X positive}}"><span>X+</span></button>
                  <button data-jog="y-" aria-label="{{t:Move Y negative}}"><span>Y−</span></button>
                  <svg class="jog-nozzle" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M12 3h8v6h-8zM9 9h14v9H9zM11 18l3 6h4l3-6m-5 6v4M6 11v5m20-5v5"/></svg>
                </div>
              </div>
              <div class="z-row" role="group" aria-labelledby="zPart">
                <div class="axis-heading"><strong id="zPart">Z</strong><small id="zPosition"></small></div>
                <div class="motion-buttons">
                  <button data-z-step="1" aria-label="{{t:Move up}}"><span class="mc-icon mc-up" aria-hidden="true"></span>{{t:Up}}</button>
                  <svg class="z-plate" viewBox="0 0 64 40" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M14 11h36l10 14H4zM4 25v4h56v-4M19 16h26m-31 5h36M24 11l-4 14m20-14 4 14"/></svg>
                  <button data-z-step="-1" aria-label="{{t:Move down}}"><span class="mc-icon mc-up" aria-hidden="true"></span>{{t:Down}}</button>
                </div>
              </div>
            </div>
          </section>
          <section class="manual-group" data-group="filament" aria-labelledby="filamentTitle" hidden>
            <h3 class="manual-heading" id="filamentTitle">{{t:Filament}}</h3>
            <div class="nozzle-controls" data-group="nozzleHeat" hidden>
              <label>{{t:Nozzle}} <select id="heatTarget"><option value="180">180 °C</option><option value="200" selected>200 °C</option>
                <option value="220">220 °C</option><option value="240">240 °C</option><option value="260">260 °C</option><option value="280">280 °C</option></select></label>
              <button data-heat aria-label="{{t:Set temperature}}"><span class="heat-long">{{t:Set temperature}}</span><span class="heat-short">{{t:Heat}}</span></button>
              <button data-manual="heat0" aria-label="{{t:Heat off}}"><span class="heat-long">{{t:Heat off}}</span><span class="heat-short">{{t:Off}}</span></button>
            </div>
            <div class="filament-actions" role="group" aria-label="{{t:Filament movement}}" aria-describedby="feedDistance">
              <small id="feedDistance">{{t:10 mm per move}}</small>
              <div class="motion-buttons"><button data-manual="e+10">{{t:Feed}}</button><button data-manual="e-10">{{t:Retract}}</button></div>
            </div>
          </section>
          <section class="manual-group" data-group="calibration" aria-labelledby="calibrationTitle" hidden>
            <div class="calibration-row">
              <h3 class="manual-heading" id="calibrationTitle">{{t:Calibration}}</h3>
              <div class="manual-services">
                <button data-manual="level"><span class="mc-icon mc-level" aria-hidden="true"></span>{{t:Level bed}}</button>
                <button data-manual="shaper"><span class="mc-icon mc-shaper" aria-hidden="true"></span>{{t:Input shaper}}</button>
              </div>
            </div>
            <div id="calibrationStatus" hidden><span id="calibrationMessage" role="status"></span>
              <progress id="calibrationProgress" aria-label="{{t:Calibration in progress}}" hidden></progress></div>
          </section>
          <div class="manual-group" data-group="timer" hidden>
            <div class="timer-row"><label>{{t:Auto shutdown}} <select id="manualTimer"><option value="">{{t:Choose delay…}}</option><option value="0">{{t:Off}}</option>
              <option value="30">{{t:30 min}}</option><option value="60">{{t:60 min}}</option><option value="120">{{t:120 min}}</option></select></label>
              <button data-timer>{{t:Apply}}</button></div>
          </div>
        </div>
      </details>
      <section id="materials" class="panel" hidden><h2 class="panel-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 6c0-4 14-4 14 0s-14 4-14 0z M5 6v12c0 4 14 4 14 0V6 M5 12c0 4 14 4 14 0"/></svg><span id="materialsTitle">{{t:Materials · IFS}}</span></h2><div id="slots"></div></section>
      <details class="panel" id="printerDetails">
        <summary class="panel-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M21 12a9 9 0 1 1-18 0 9 9 0 1 1 18 0 M12 11v6 M12 7v1"/></svg>{{t:Printer details}}</summary>
        <div class="tool-body detail-groups">
          <section class="panel-inset" aria-labelledby="printerDeviceTitle">
            <h3 class="inset-heading" id="printerDeviceTitle">{{t:Device}}</h3>
            <dl class="readouts">
              <div class="readout"><dt>{{t:Model}}</dt><dd id="printerModel"></dd></div>
              <div class="readout"><dt>{{t:Firmware}}</dt><dd id="printerFirmware"></dd></div>
              <div class="readout"><dt>{{t:Build volume}}</dt><dd id="printerVolume"></dd></div>
              <div class="readout"><dt>{{t:Light}}</dt><dd id="printerLight"></dd></div>
            </dl>
            <p class="caption" id="printerSensors"></p>
          </section>
          <section class="panel-inset" aria-labelledby="printerConnectionTitle">
            <h3 class="inset-heading" id="printerConnectionTitle">{{t:Connection}}</h3>
            <dl class="readouts">
              <div class="readout"><dt>{{t:Serial}}</dt><dd id="printerSerial"></dd></div>
              <div class="readout"><dt>MAC</dt><dd id="printerMac"></dd></div>
            </dl>
            <a id="nativePage" target="_blank" rel="noopener noreferrer" data-icon="browser" hidden>{{t:Open Printer Interface in Browser}}</a>
          </section>
        </div>
      </details>
    </div>
  </div>
  <details class="panel monitor-tools" id="history">
    <summary class="panel-title"><svg class="section-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 10a9 9 0 1 1 1 8 M3 4v6h6 M12 7v5l4 2"/></svg>{{t:Print History}}</summary>
    <div class="tool-body">
      <section class="totals panel-inset" id="historyTotals" aria-labelledby="totals-title">
        <h2 class="inset-heading" id="totals-title">{{t:Totals}}</h2>
        <dl class="readouts">
          <div class="readout"><dt>{{t:Jobs}}</dt><dd id="statJobs"></dd></div>
          <div class="readout"><dt>{{t:Print time}}</dt><dd id="statHours"></dd></div>
          <div class="readout"><dt>{{t:Print success rate}}</dt><dd id="statSuccess"></dd></div>
        </dl>
      </section>
      <p class="muted" id="historyStatus" role="status">{{t:Print history is recorded while OrcaSlicer is open.}}</p>
      <div class="history-controls">
        <select id="historyPrinter" aria-label="{{t:Printer}}"><option value="">{{t:All printers}}</option></select>
        <input type="search" id="historySearch" placeholder="{{t:Find a file}}" aria-label="{{t:Find a history entry}}">
        <label><input type="checkbox" id="failedJobs"> {{t:Errors and cancellations}}</label>
      </div>
      <p class="history-summary" id="historySummary"></p>
      <div class="table-wrap" id="historyTable" hidden>
        <table class="history-table" role="table">
          <thead role="rowgroup"><tr role="row"><th scope="col">{{t:File}}</th><th scope="col">{{t:Started}}</th><th scope="col">{{t:Print Time}}</th><th scope="col">{{t:Result}}</th></tr></thead>
          <tbody id="historyRows" role="rowgroup"></tbody>
        </table>
      </div>
    </div>
  </details>
</main>
<dialog class="settings" id="setupDialog" aria-labelledby="setupTitle">
  <form id="setupForm" novalidate>
    <h2 id="setupTitle">{{t:Set up your first printer}}</h2>
    <section id="setupPlacementStep">
      <label for="setupPlacement">{{t:Tab placement}}</label>
      <select id="setupPlacement" aria-describedby="setupPlacementHint">
        <option value="device">{{t:Replace Printer tab}}</option><option value="flashforge">FlashForge</option>
      </select>
      <p id="setupPlacementHint" class="caption" hidden>{{t:Save this setting, then restart OrcaSlicer to switch tabs.}}</p>
    </section>
    <section id="setupPrinterStep" hidden>
      <div class="row">
        <button type="button" id="setupFind" data-icon="find">{{t:Find printers}}</button>
        <button type="button" id="setupManual">{{t:Enter IP manually}}</button>
      </div>
      <div class="row" id="setupCandidates"></div>
      <div id="setupPrinterFields" hidden></div>
    </section>
    <p id="setupStatus" class="caption" role="status"></p>
    <div class="settings-actions">
      <button type="button" id="setupCancel">{{t:Cancel}}</button>
      <button type="button" id="setupBack" hidden>{{t:Back}}</button>
      <button type="button" id="setupNext" data-icon="arrow">{{t:Next}}</button>
      <button type="submit" id="setupSave" data-icon="check" hidden>{{t:Save}}</button>
    </div>
  </form>
</dialog>
<dialog class="settings" id="settingsDialog" aria-labelledby="settingsTitle">
  <form id="settingsForm" novalidate>
    <h2 id="settingsTitle">{{t:Settings}}</h2>
    <div id="settingsLayout">
      <div id="settingsSections" role="group" aria-label="{{t:Settings}}">
        <button id="settingsAppearanceTab" type="button" data-icon="settings" aria-controls="settingsAppearance" aria-pressed="false">{{t:Appearance}}</button>
        <button id="settingsMonitorTab" type="button" data-icon="monitor" aria-controls="settingsMonitor" aria-pressed="false">{{t:Monitor}}</button>
        <button id="settingsFleetTab" type="button" data-icon="fleet" aria-controls="settingsFleet" aria-pressed="false">{{t:All printers}}</button>
        <button id="settingsFilesTab" type="button" data-icon="print" aria-controls="settingsFiles" aria-pressed="false">{{t:Printer Files}}</button>
        <button id="settingsHistoryTab" type="button" data-icon="history" aria-controls="settingsHistory" aria-pressed="false">{{t:Print History}}</button>
        <button id="settingsPrintersTab" type="button" data-icon="link" aria-controls="settingsPrinters" aria-pressed="true">{{t:Connection}}</button>
      </div>
      <div id="settingsContent">
        <section class="settings-fields" id="settingsAppearance" aria-labelledby="settingsAppearanceTab" hidden>
          <label for="displayTheme">{{t:Theme}}<select id="displayTheme">
            <option value="system">{{t:System theme}}</option>
            <option value="light">{{t:Light theme}}</option>
            <option value="dark">{{t:Dark theme}}</option>
          </select></label>
          <label for="displayDensity">{{t:Layout}}<select id="displayDensity">
            <option value="standard">{{t:Standard}}</option>
            <option value="compact">{{t:Compact}}</option>
          </select></label>
        </section>
        <section class="settings-fields" id="settingsMonitor" aria-labelledby="settingsMonitorTab" hidden>
          <label for="displayPlacement">{{t:Tab placement}}<select id="displayPlacement" aria-describedby="displayPlacementHint" disabled>
            <option value="device">{{t:Replace Printer tab}}</option><option value="flashforge">FlashForge</option>
          </select></label>
          <p id="displayPlacementHint" class="caption" role="status" hidden></p>
          <label for="displayTallJobCard">{{t:Taller print job card}}<input type="checkbox" id="displayTallJobCard"></label>
          <label for="displayMonitorChart">{{t:Temperatures}}<input type="checkbox" id="displayMonitorChart"></label>
          <label for="displayChartLayout">{{t:Chart layout}}<select id="displayChartLayout">
            <option value="shared">{{t:Shared scale}}</option>
            <option value="separate">{{t:Separate scales}}</option>
          </select></label>
          <label for="displayChartRange">{{t:Range}}<select id="displayChartRange">
            <option value="5">{{t:5 min}}</option><option value="15">{{t:15 min}}</option><option value="30">{{t:30 min}}</option>
          </select></label>
          <label>{{t:Nozzle}}<input type="checkbox" data-chart-choice="nozzle"></label>
          <label>{{t:Nozzle Target}}<input type="checkbox" data-chart-choice="nozzleTarget"></label>
          <label>{{t:Bed}}<input type="checkbox" data-chart-choice="bed"></label>
          <label>{{t:Bed Target}}<input type="checkbox" data-chart-choice="bedTarget"></label>
          <label for="displayPrinterDetails">{{t:Printer details}}<input type="checkbox" id="displayPrinterDetails"></label>
        </section>
        <section class="settings-fields" id="settingsFleet" aria-labelledby="settingsFleetTab" hidden>
          <label for="displayFleetCameras">{{t:Live cameras}}<select id="displayFleetCameras">
            <option value="all">{{t:While this tab is open}}</option>
            <option value="printing">{{t:Only during an active print}}</option>
            <option value="off">{{t:Off}}</option>
          </select></label>
          <label for="displayFleetDetails">{{t:Job details}}<input type="checkbox" id="displayFleetDetails"></label>
          <label for="displayFleetWaitForRemoval">{{t:Wait for part removal}}<input type="checkbox" id="displayFleetWaitForRemoval"></label>
        </section>
        <section class="settings-fields" id="settingsFiles" aria-labelledby="settingsFilesTab" hidden>
          <label for="displayCardSize">{{t:Card size}}<select id="displayCardSize">
            <option value="small">{{t:Small}}</option>
            <option value="medium">{{t:Medium}}</option>
            <option value="large">{{t:Large}}</option>
          </select></label>
          <label for="displayRows">{{t:Rows per page}}<select id="displayRows">
            <option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option>
          </select></label>
          <label for="displayPreviews">{{t:Show file previews}}<input type="checkbox" id="displayPreviews"></label>
        </section>
        <section class="settings-fields" id="settingsHistory" aria-labelledby="settingsHistoryTab" hidden>
          <label for="displayHistoryTotals">{{t:Totals}}<input type="checkbox" id="displayHistoryTotals"></label>
        </section>
        <section id="settingsPrinters" aria-labelledby="settingsPrintersTab">
          <div id="printerFields"></div>
          <div class="row"><button type="button" id="findPrinters" data-icon="find">{{t:Find printers}}</button><button type="button" id="addPrinter" data-icon="plus">{{t:Add printer}}</button></div>
          <div id="discoveredPrinters"></div>
          <p class="caption" id="settingsStatus" role="status"></p>
          <details id="bulk"><summary>{{t:Edit as text}}</summary><label for="printersText">{{t:One printer per line: address | CheckCode | name | serial number}}</label>
            <textarea id="printersText" rows="5" spellcheck="false" placeholder="192.168.1.50 | 1a2b3c4d | Adventurer 5M"></textarea>
            <p class="caption">{{t:Use a JSON array of four strings when a value contains a vertical bar.}}</p>
          </details>
          <p class="caption">{{t:Creator 5 and 5 Pro require both CheckCode and the printer serial number. Adventurer 5M reads its serial number automatically.}}</p>
        </section>
      </div>
    </div>
    <div id="settingsFooter"><p class="caption" id="settingsSaveStatus" role="status"></p>
    <div class="settings-actions">
      <button type="button" id="settingsCancel">{{t:Cancel}}</button>
      <button type="submit" id="settingsSave" data-icon="check">{{t:Save}}</button>
    </div>
    </div>
  </form>
</dialog>
<dialog class="settings" id="uploadFileDialog" aria-labelledby="uploadFileTitle">
  <form id="uploadFileForm" class="tool-body">
    <h2 id="uploadFileTitle">{{t:Upload file}}</h2>
    <p id="uploadFilePrinter" class="muted"></p>
    <label>{{t:Prepared print file}} <input id="uploadFileInput" type="file" accept=".gcode,.3mf" required></label>
    <label>{{t:File name on printer}} <input id="uploadFileName" type="text" maxlength="120" required></label>
    <progress id="uploadFileProgress" max="100" aria-label="{{t:Upload progress}}" hidden></progress>
    <p id="uploadFileMessage" role="status"></p>
    <div class="row"><button id="closeUploadFile" type="button">{{t:Close}}</button><button id="confirmUploadFile" type="submit">{{t:Upload}}</button></div>
  </form>
</dialog>
<dialog class="settings" id="printDialog" aria-labelledby="printTitle"><form id="printForm" class="tool-body">
<h2 id="printTitle">{{t:Print stored file}}</h2><p id="printSelection"></p>
<div id="printSource" hidden><label for="sourceFile">{{t:Original sliced .3mf}}</label>
<input id="sourceFile" type="file" accept=".3mf" aria-describedby="sourceHelp">
<p class="caption" id="sourceHelp">{{t:Choose the original .3mf with one sliced plate and the same file name.}}</p>
<button id="useSavedSource" type="button" data-icon="link" hidden>{{t:Use linked materials}}</button>
<p class="caption" id="sourceStatus" role="status"></p></div>
<label id="levelOption"><input id="printLevel" type="checkbox"> {{t:Level bed before printing}}</label>
<div id="printSlots"></div><p id="printError" role="status"></p>
<div class="row"><button type="button" id="dismissPrint">{{t:Cancel}}</button><button type="submit" id="confirmPrint" data-icon="print">{{t:Print}}</button></div>
</form></dialog>
<dialog class="settings" id="slotDialog" aria-labelledby="slotTitle"><form id="slotForm" class="tool-body">
<h2 id="slotTitle">{{t:Edit slot}}</h2>
<label for="slotMaterial">{{t:Material}}</label><select id="slotMaterial" required></select>
<div id="slotColors" role="group" aria-label="{{t:Color}}"></div>
<p id="slotStatus" role="status"></p>
<div class="row"><button type="button" id="dismissSlot">{{t:Cancel}}</button><button type="submit" id="saveSlot" data-icon="check">{{t:Save}}</button></div>
</form></dialog>
<dialog class="settings" id="infoDialog"><div class="tool-body"><h2 id="infoTitle"></h2><div id="infoBody"></div>
  <div class="settings-actions"><button id="copyReport" data-icon="copy" hidden>{{t:Copy report}}</button><button id="closeInfo">{{t:Close}}</button></div></div></dialog>
<dialog class="settings" id="notifyDialog"><form id="notifyForm"><h2>{{t:Host notifications}}</h2>
<label><input type="checkbox" name="finished"> {{t:Print finished}}</label><label><input type="checkbox" name="cancelled"> {{t:Print cancelled}}</label>
<label><input type="checkbox" name="error"> {{t:Printer error}}</label><label><input type="checkbox" name="offline"> {{t:Offline for at least one minute}}</label>
<p id="notifyStatus" role="status"></p><div class="settings-actions"><button type="button" id="closeNotify">{{t:Cancel}}</button><button type="submit" data-icon="check">{{t:Save}}</button></div></form></dialog>
<script id="plugin-translations" type="application/json">{{translations}}</script>
<script>
{{translationScript}}
const el = id => document.getElementById(id);
const stateLabel = value => t(Object.hasOwn(states, value) ? states[value] : value);
const jobActionLabel = action => t({pause:'Pause', resume:'Resume', cancel:'Cancel'}[action] || action);
const formatLocales = [uiLocale, uiLocale === 'eu' ? 'es-ES' : 'en-GB'];
const weightFormat = new Intl.NumberFormat(formatLocales, {style:'unit', unit:'gram', maximumFractionDigits:2});
const fileCountLabel = count => uiLocale === 'en-US' ? count + (count === 1 ? ' file' : ' files')
  : t("Files: {count}", {count});
const displayKey = 'ff-display-settings';
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
const chartLineKeys = ['nozzle','nozzleTarget','bed','bedTarget'];
const defaults = {
  theme:'system', density:'standard', cardSize:'medium', rows:3, previews:true,
  tallJobCard:true, monitorChart:true, chartLayout:'shared', chartRange:30, chartLines:chartLineKeys,
  printerDetails:true, fleetDetails:true, fleetWaitForRemoval:true, historyTotals:true
};
function readDisplaySettings() {
  let saved;
  try {saved = JSON.parse(localStorage.getItem(displayKey));} catch {}
  if (!saved) return {...defaults};
  const chartLines = Array.isArray(saved.chartLines) ? chartLineKeys.filter(key => saved.chartLines.includes(key)) : [];
  return {
    theme:['system','light','dark'].includes(saved.theme) ? saved.theme : defaults.theme,
    density:['standard','compact'].includes(saved.density) ? saved.density : defaults.density,
    cardSize:['small','medium','large'].includes(saved.cardSize) ? saved.cardSize : defaults.cardSize,
    rows:Number.isInteger(saved.rows) && saved.rows >= 1 && saved.rows <= 6 ? saved.rows : defaults.rows,
    previews:typeof saved.previews === 'boolean' ? saved.previews : defaults.previews,
    tallJobCard:saved.tallJobCard !== false,
    monitorChart:saved.monitorChart !== false,
    chartLayout:saved.chartLayout === 'separate' ? 'separate' : 'shared',
    chartRange:[5,15,30].includes(saved.chartRange) ? saved.chartRange : defaults.chartRange,
    chartLines:chartLines.length ? chartLines : chartLineKeys,
    printerDetails:saved.printerDetails !== false,
    fleetDetails:saved.fleetDetails !== false,
    fleetWaitForRemoval:saved.fleetWaitForRemoval !== false,
    historyTotals:saved.historyTotals !== false
  };
}
let display = readDisplaySettings();
function applyDisplayStyles() {
  const root = document.documentElement;
  root.dataset.theme = display.theme === 'system' ? (systemTheme.matches ? 'dark' : 'light') : display.theme;
  root.dataset.density = display.density;
  root.dataset.cardSize = display.cardSize;
  root.dataset.tallJobCard = display.tallJobCard ? 'on' : 'off';
  root.dataset.fleetDetails = display.fleetDetails ? 'on' : 'off';
  el('temperaturesPanel').hidden = !display.monitorChart;
  el('printerDetails').hidden = !display.printerDetails;
  el('historyTotals').hidden = !display.historyTotals;
}
applyDisplayStyles();
let pageSize = display.rows * 4;
let view = location.hash === '#history' ? 'history' : ['#allPrinters', '#printJobs'].includes(location.hash) ? 'fleet'
  : !['#monitor', '#files-section'].includes(location.hash) && localStorage.getItem('ff-fleet') === '1' ? 'fleet' : 'monitor';
let openFleetFiles = location.hash === '#files-section';
let lastObservation = null;
let connectionFailure = '';
let historyData = null;
let historyLoading = false;
let historyFilter = view === 'history' ? new URL(location.href).searchParams.get('historyPrinter') || '' : '';
let historyRowsKey = '';
let chartKey = 'nozzle';
const finishClock = new Intl.DateTimeFormat(formatLocales, {hour:'2-digit', minute:'2-digit'});
function finishAt(seconds, stamp) {
  return Number.isFinite(seconds) && Number.isFinite(stamp) ? finishClock.format((stamp + seconds) * 1000) : '';
}
let infoRevision = 0;
function showInfo(title, body, copy = false) {
  el('infoTitle').textContent = title;
  el('infoBody').textContent = body;
  el('copyReport').hidden = !copy;
  el('copyReport').textContent = t("Copy report");
  if (!el('infoDialog').open) el('infoDialog').showModal();
  return ++infoRevision;
}
el('closeInfo').onclick = () => el('infoDialog').close();
el('copyReport').onclick = async () => {
  try { await navigator.clipboard.writeText(el('infoBody').textContent); el('copyReport').textContent = t("Copied"); }
  catch { const range = document.createRange(); range.selectNodeContents(el('infoBody')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); }
};
async function postLocal(route, url = endpoint('/' + route), payload, timeout = 15000) {
  const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-FlashForge-Tab-Action':route},
    body: payload === undefined ? undefined : JSON.stringify(payload), signal:AbortSignal.timeout(timeout)});
  const result = await response.json();
  if (!response.ok) throw Object.assign(Error(result.error || t("Request failed.")), {code:result.code});
  return result;
}
async function diagnose(host) {
  const revision = showInfo(t("Connection check"), t("Reading printer status…"));
  const current = () => el('infoDialog').open && revision === infoRevision;
  try {
    const url = host ? new URL('/' + encodeURIComponent(host) + '/diagnostics', location.origin) : endpoint('/diagnostics');
    const data = await postLocal('diagnostics', url);
    if (!current()) return;
    showInfo(t("Connection check"), ['FlashForge Device Tab 1.14.5', t("Transport: {transport}", {transport:data.transport}), t("Status: received"),
      t("Authenticated readings: {availability}", {availability:data.authenticated ? t("available") : t("unavailable")}),
      t("Camera URL: {status}", {status:data.cameraReported ? t("reported; stream not tested") : t("not reported")})].join('\n'), true);
  } catch (error) { if (current()) showInfo(t("Connection check"), error.message, true); }
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
let lastSlots = [], slotSignature = '', editedSlot = null, editedColor = '', lightState = null, lightPending = false;
let creatorMaterials = false;
function showSlots(slots, editor) {
  el('materials').hidden = !slots?.length;
  const signature = JSON.stringify([slots || [], editor || null, creatorMaterials]);
  if (signature === slotSignature) return;
  slotSignature = signature;
  el('materialsTitle').textContent = creatorMaterials ? t("Materials") : t("Materials · IFS");
  const focusedSlot = document.activeElement?.dataset?.slotId;
  el('slots').replaceChildren(...(slots || []).map(slot => {
    const editable = editor && Object.hasOwn(slot, 'present');
    const box = document.createElement(editable ? 'button' : 'div'), reel = document.createElement('span'), text = document.createElement('span');
    box.className = 'slot' + (slot.active ? ' active' : '');
    const slotId = creatorMaterials ? String(slot.id) : 'A' + slot.id;
    box.textContent = creatorMaterials ? t("Slot {number}", {number:slot.id}) : slotId;
    reel.className = 'spool';
    reel.style.display = 'block';
    reel.style.setProperty('--spool', slot.present === false ? '#818681' : slot.color || '#889088');
    text.textContent = slot.present === false ? t("Empty") : slot.name || t("Unknown");
    if (slot.active && slot.present !== false) text.append(document.createElement('br'), t("Selected"));
    if (editable) {
      box.type = 'button';
      box.dataset.slotId = slot.id;
      box.dataset.icon = 'edit';
      box.setAttribute('aria-label', t("Edit slot {slot} · {material}", {slot:slotId, material:text.textContent}));
      box.setAttribute('aria-haspopup', 'dialog');
      box.onclick = () => openSlot(slot, editor, slotId);
    }
    box.append(reel,text);return box;
  }));
  if (focusedSlot) el('slots').querySelector('button[data-slot-id="' + focusedSlot + '"]')?.focus();
}
function openSlot(slot, editor, slotId) {
  editedSlot = slot.id;
  editedColor = (slot.color || '').slice(1).toUpperCase();
  const material = el('slotMaterial');
  material.replaceChildren(new Option(t("Choose material"), ''), ...editor.materials.map(value => new Option(value)));
  material.value = editor.materials.includes(slot.name) ? slot.name : '';
  el('slotTitle').textContent = t("Edit slot {slot}", {slot:slotId});
  el('slotStatus').textContent = '';
  const names = [t("White"),t("Yellow"),t("Light green"),t("Green"),t("Dark green"),t("Teal"),t("Cyan"),t("Light blue"),t("Blue"),t("Dark blue"),t("Purple"),t("Violet"),t("Magenta"),t("Pink"),t("Coral"),t("Red"),t("Brown"),t("Orange"),t("Cream"),t("Tan"),t("Dark brown"),t("Gray"),t("Light gray"),t("Black")];
  el('slotColors').replaceChildren(...editor.colors.map((color, index) => {
    const swatch = document.createElement('button');
    swatch.type = 'button';
    swatch.title = names[index] + ' #' + color;
    swatch.setAttribute('aria-label', swatch.title);
    swatch.setAttribute('aria-pressed', String(color === editedColor));
    swatch.style.setProperty('--swatch', '#' + color);
    swatch.onclick = () => {
      editedColor = color;
      for (const button of el('slotColors').children) button.setAttribute('aria-pressed', String(button === swatch));
    };
    return swatch;
  }));
  if (!editor.colors.includes(editedColor)) editedColor = '';
  el('slotDialog').showModal();
}
el('dismissSlot').onclick = () => el('slotDialog').close();
el('slotForm').onsubmit = async event => {
  event.preventDefault();
  const save = el('saveSlot');
  if (save.disabled) return;
  if (!editedColor) { el('slotStatus').textContent = t("Choose a color."); return; }
  save.disabled = el('dismissSlot').disabled = true;
  try {
    await postLocal('action', undefined, {action:'slot/' + editedSlot + '/' + el('slotMaterial').value + '/' + editedColor});
    el('slotDialog').close();
    await updateStatus();
    if (!el('refresh').disabled) refreshFiles();
  } catch (error) { el('slotStatus').textContent = error.message; }
  finally { save.disabled = el('dismissSlot').disabled = false; }
};
el('slotDialog').oncancel = event => { if (el('saveSlot').disabled) event.preventDefault(); };
function updateLight(light) {
  lightState = typeof light === 'boolean' ? light : null;
  const button = el('cameraLight');
  button.hidden = lightState === null && !lightPending;
  button.disabled = lightState === null || lightPending;
  button.setAttribute('aria-pressed', String(lightState === true));
  button.textContent = lightPending ? t("Setting light…") : lightState ? t("Light on") : t("Light off");
}
el('cameraLight').onclick = async () => {
  if (lightState === null || lightPending) return;
  const target = !lightState;
  lightPending = true;
  updateLight(lightState);
  el('lightStatus').textContent = '';
  try {
    const result = await postLocal('action', undefined, {action:'light/' + (target ? 'on' : 'off')});
    updateLight(result.state === 'On');
  } catch (error) { el('lightStatus').textContent = error.message; }
  finally { lightPending = false; updateLight(lightState); }
};
function updateAge() {
  const age = lastObservation == null ? null : Math.max(0, Math.floor(Date.now()/1000-lastObservation));
  el('freshness').textContent = age == null ? t("Waiting for readings") : t("Last reading: {seconds} s ago", {seconds:age});
  if (age > 30 && !connectionFailure) {
    el('status').textContent = t("Readings delayed");
    setReadingsSummary(t("Readings delayed"));
    updateTemperatureTrend();
    el('status').className = 'connection disconnected';
    el('monitor').classList.add('stale');
    showSlots(lastSlots);
    updateLight(null);
    updateActions({}, true);
  }
}
setInterval(updateAge,1000);

const selectedPrinter = new URL(location.href).searchParams.get('printer');
const separateTab = location.pathname === '/flashforge/';
const setupTab = location.pathname === '/setup/';
const nativePage = el('nativePage');
function endpoint(path) {
  const url = new URL(path.replace(/^\//, ''), location.href);
  if (selectedPrinter && !['/native','/settings','/notifications','/history'].includes(path)) url.searchParams.set('printer',selectedPrinter);
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
let observedJob = '';
let lastCompletedFile = '';
let jobReady = false;
let previewKey = '';
let catalogLoaded = false;
let catalogRevision;
let fileUploadBusy = false;
const imageRequests = new Map();

async function getJson(path) {
  const response = await fetch(endpoint(path), {cache: 'no-store', signal: AbortSignal.timeout(12000)});
  const data = await response.json();
  if (!response.ok) throw Object.assign(Error(data.error || t("No response")), {status: response.status});
  return data;
}

if (!separateTab && !setupTab && !selectedPrinter) {
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

function setReadingsSummary(text, compact = text) {
  el('readingsSummary').textContent = text;
  el('readingsSummaryCompact').textContent = compact;
}

function updateReadingsSummary(data, tools, heated) {
  const parts = [], compact = [];
  function addTemperature(label, shortLabel, actual, target) {
    if (!Number.isFinite(actual) && !Number.isFinite(target)) return;
    const value = Number.isFinite(target) ? plain(actual) + ' / ' + celsius(target) : celsius(actual);
    parts.push(label + ' ' + value);
    compact.push(shortLabel + ' ' + (Number.isFinite(actual) ? plain(actual) + '°' : '—'));
  }
  if (tools.length < 2) {
    addTemperature(t("Nozzle"), t("N"), data.nozzle, data.nozzleTarget);
  } else if (heated.length === 1) {
    const tool = heated[0], label = 'T' + (tools.indexOf(tool) + 1);
    addTemperature(label, label, tool.actual, tool.target);
  } else {
    const status = heated.length ? t("Heating nozzles: {count}", {count:heated.length}) : t("Nozzles: {count}", {count:tools.length});
    parts.push(status);
    compact.push(status);
  }
  addTemperature(t("Bed"), t("B"), data.bed, data.bedTarget);
  setReadingsSummary(parts.join(' · ') || t("No readings"), compact.join(' · ') || t("No readings"));
}

const chart = el('chart');
const chartBed = el('chartBed');
const chartRange = el('chartRange');
const chartLegend = [...document.querySelectorAll('[data-chart-line]')];
const chartChoices = [...document.querySelectorAll('[data-chart-choice]')];
chartRange.value = String(display.chartRange);
const chartDuration = () => Number(chartRange.value) * 60000;
let temperatures = [];
let chartEnd = 0;
const shortTime = new Intl.DateTimeFormat(formatLocales, {hour: '2-digit', minute: '2-digit'});
const historyTime = new Intl.DateTimeFormat(formatLocales, {dateStyle: 'short', timeStyle: 'short'});

function setTemperatureTrend(text, compact = text) {
  el('chartTrend').textContent = text;
  el('chartTrendCompact').textContent = compact;
}

function updateTemperatureTrend(now = Date.now()) {
  const latest = temperatures.at(-1);
  if (connectionFailure || (latest && now - latest.t > 30000)) {
    setTemperatureTrend(t("Trend unavailable"));
    return;
  }
  const cutoff = latest?.t - 5 * 60000;
  const wide = [], compact = [];
  const nozzle = chartKey === 'nozzle' ? t("Nozzle") : chartKey.toUpperCase();
  for (const [field, label, shortLabel] of [[chartKey, nozzle, chartKey === 'nozzle' ? t("N") : nozzle], ['bed', t("Bed"), t("B")]]) {
    if (!Number.isFinite(latest?.[field])) continue;
    const earlier = temperatures.reduce((closest, sample) => Number.isFinite(sample[field])
      && (!closest || Math.abs(sample.t - cutoff) < Math.abs(closest.t - cutoff)) ? sample : closest, null);
    if (!earlier || Math.abs(earlier.t - cutoff) > 18000) continue;
    const delta = Math.round(latest[field] - earlier[field]);
    const change = (delta > 0 ? '+' : delta < 0 ? '−' : '') + Math.abs(delta) + '°';
    wide.push(label + ' ' + change);
    compact.push(shortLabel + ' ' + change);
  }
  if (!wide.length) setTemperatureTrend(t("Collecting readings…"));
  else setTemperatureTrend(t("5 min · {readings}", {readings:wide.join(' · ')}),
    uiLocale === 'en-US' ? '5m · ' + compact.join(' · ') : t("5 min · {readings}", {readings:compact.join(' · ')}));
}

function drawChart() {
  updateLegend();
  const series = visibleTemperatureSeries();
  const separate = display.chartLayout === 'separate';
  const nozzle = separate ? series.filter(line => line.nozzle) : series;
  const bed = separate ? series.filter(line => !line.nozzle) : [];
  chart.hidden = !nozzle.length;
  chartBed.hidden = !bed.length;
  if (!el('temperaturesPanel').open) return;
  const end = chartEnd = Date.now();
  const start = end - chartDuration();
  const samples = temperatures.filter(sample => sample.t >= start && sample.t <= end);
  if (nozzle.length) drawTemperaturePlot(chart, nozzle, samples, start, end, separate ? el('chartNozzle').textContent : '');
  if (bed.length) drawTemperaturePlot(chartBed, bed, samples, start, end, t("Bed"));
}

function visibleTemperatureSeries() {
  return chartLegend.filter(element => display.chartLines.includes(element.dataset.chartLine)).map(element => {
    const key = element.dataset.chartLine;
    const target = key.endsWith('Target');
    const nozzle = key.startsWith('nozzle');
    return {nozzle, target, field:nozzle ? chartKey + (target ? 'Target' : '') : key,
      color:nozzle ? '--blue' : '--bed-series', label:element.textContent};
  });
}

function temperatureAxis(samples, series, separate) {
  let low = Infinity, high = -Infinity;
  for (const sample of samples) for (const {field} of series) {
    if (!Number.isFinite(sample[field])) continue;
    low = Math.min(low, sample[field]);
    high = Math.max(high, sample[field]);
  }
  if (!separate || !Number.isFinite(high)) return {min:0, max:Math.max(100, Math.ceil(high / 50) * 50), steps:4};
  const step = Math.max(5, Math.ceil((high - low) / 20) * 5);
  const paddedMin = Math.floor((low - step / 2) / step) * step;
  const min = low >= 0 ? Math.max(0, paddedMin) : paddedMin;
  const max = Math.ceil((high + step / 2) / step) * step;
  return {min, max, steps:(max - min) / step};
}

function drawTemperaturePlot(canvas, series, samples, start, end, title) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  canvas.setAttribute('aria-label', series.map(line => line.label).join(' · '));
  const ctx = canvas.getContext('2d');
  ctx.scale(scale, scale);
  const colors = getComputedStyle(canvas);
  const left = 41, right = width - 16, top = title ? 26 : 11, bottom = height - 26;
  const {min,max,steps} = temperatureAxis(samples, series, Boolean(title));
  const x = t => left + (t - start) / (end - start) * (right - left);
  const y = value => bottom - (Math.max(min, value) - min) / (max - min) * (bottom - top);
  ctx.font = '11px "Segoe UI", sans-serif';
  ctx.fillStyle = colors.getPropertyValue('--muted').trim();
  ctx.strokeStyle = colors.getPropertyValue('--line').trim();
  if (title) ctx.fillText(title, left, 12);
  for (let tick = 0; tick <= steps; tick++) {
    const value = min + (max - min) * tick / steps;
    ctx.beginPath();
    ctx.moveTo(left, y(value));
    ctx.lineTo(right, y(value));
    ctx.stroke();
    ctx.fillText(Math.round(value) + '°', 5, y(value) + 4);
  }
  ctx.fillText(shortTime.format(start), left, height - 5);
  const current = shortTime.format(end);
  ctx.fillText(current, right - ctx.measureText(current).width, height - 5);
  if (!samples.some(sample => series.some(({field}) => Number.isFinite(sample[field])))) {
    const label = t("Waiting for temperature readings");
    ctx.fillText(label, Math.max(left + 5, (width - ctx.measureText(label).width) / 2), height / 2);
  }
  for (const {field,color,target} of series) {
    ctx.strokeStyle = ctx.fillStyle = colors.getPropertyValue(color).trim();
    ctx.lineWidth = 2;
    ctx.setLineDash(target ? [5, 4] : []);
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
    if (last && !target) {
      ctx.beginPath();
      ctx.arc(...last, 2.5, 0, 2 * Math.PI);
      ctx.fill();
    }
  }
}

function redrawTemperatureChart() {
  el('chartSample').textContent = '';
  drawChart();
}
chartRange.onchange = () => {
  display = {...display, chartRange:Number(chartRange.value)};
  try {localStorage.setItem(displayKey, JSON.stringify(display));} catch {}
  redrawTemperatureChart();
};
el('chartTool').onchange = () => {
  chartKey = el('chartTool').value;
  updateLegend();
  updateTemperatureTrend();
  redrawTemperatureChart();
};
function updateLegend() {
  const label = chartKey === 'nozzle' ? el('nozzleLabel').textContent : chartKey.toUpperCase();
  el('chartNozzle').textContent = label;
  el('chartNozzleTarget').textContent = chartKey === 'nozzle' ? t("Nozzle Target") : label + ' · ' + t("Target");
  for (const element of chartLegend) element.hidden = !display.chartLines.includes(element.dataset.chartLine);
  const picker = el('chartTool');
  picker.hidden = picker.options.length < 2 || !display.chartLines.some(key => key.startsWith('nozzle'));
}
function sampleTemperaturePlot(event) {
  const canvas = event.currentTarget;
  const range = chartDuration();
  const t = chartEnd - range + range * (event.offsetX - 41) / Math.max(1,canvas.clientWidth - 57);
  const s = temperatures.reduce((a,b) => !a || Math.abs(b.t-t)<Math.abs(a.t-t) ? b:a,null);
  const readings = s && Math.abs(s.t-t)<18000 ? visibleTemperatureSeries()
    .filter(line => Number.isFinite(s[line.field])
      && (display.chartLayout === 'shared' || line.nozzle === (canvas === chart)))
    .map(line => line.label + ' ' + plain(s[line.field]) + ' °C') : [];
  el('chartSample').textContent = readings.length ? [shortTime.format(s.t), ...readings].join(' · ') : '';
}
for (const canvas of [chart, chartBed]) {
  canvas.onpointermove = sampleTemperaturePlot;
  canvas.onpointerleave = () => {el('chartSample').textContent = '';};
}
async function updateTools() {
  if (setupTab) return;
  updateHistory();
  try {
    const data = await getJson('/telemetry');
    if (data.language && data.language !== document.documentElement.lang && !document.querySelector('dialog[open]')) {
      location.reload();
      return;
    }
    temperatures = data.temperatures;
    updateTemperatureTrend();
    if (Array.isArray(data.printers)) {
      fleetPrinters = data.printers;
      applyView();
    }
    drawChart();
    if (data.cameraUrl && !el('cameraUrl').value && !el('cameraUrl').matches(':focus')) {
      el('cameraUrl').value = data.cameraUrl;
    }
  } catch {
    setTemperatureTrend(t("Trend unavailable"));
    drawChart();
  }
}

async function updateHistory() {
  const panel = el('history');
  if (historyLoading || panel.hidden || !panel.open) return;
  historyLoading = true;
  try {
    historyData = await getJson('/history');
    renderHistory();
  } catch {
    el('historyStatus').textContent = t("Print history is temporarily unavailable.");
  } finally {
    historyLoading = false;
  }
}
el('historySearch').oninput = renderHistory;
el('failedJobs').onchange = renderHistory;
el('historyPrinter').onchange = event => {
  historyFilter = event.target.value;
  replaceViewAddress();
  renderHistory();
};
function appendFilename(label, name) {
  const parts = name.split(/(?<=_)(?=[^_])/);
  for (const [i, part] of parts.entries()) {
    if (i) label.append(document.createElement('wbr'));
    label.append(part);
  }
}
function renderHistory() {
  if (!historyData) return;
  const picker = el('historyPrinter');
  const printers = historyData.printers;
  const options = JSON.stringify(printers.map(({host,name}) => [host,name]));
  if (picker.dataset.options !== options) {
    picker.replaceChildren(new Option(t("All printers"), ''), ...printers.map(({host,name}) =>
      new Option(name === host ? host : name + ' · ' + host, host)));
    picker.dataset.options = options;
  }
  const selected = printers.find(printer => printer.host === historyFilter);
  if (!selected && historyFilter) {
    historyFilter = '';
    replaceViewAddress();
  }
  picker.value = historyFilter;
  const scope = selected ? [selected] : printers;
  const stats = selected ? selected.stats : historyData.stats;
  el('statJobs').textContent = String(stats.jobs);
  el('statHours').textContent = stats.seconds ? duration(stats.seconds) : t("0 min");
  el('statSuccess').textContent = stats.successRate == null ? '—' : stats.successRate + ' %';
  const errors = scope.filter(printer => printer.error)
    .map(printer => printer.name + ' · ' + t(printer.error));
  el('historyStatus').textContent = errors.length ? errors.join('\n')
    : t(stats.jobs ? "Print history is recorded while OrcaSlicer is open."
      : "No print jobs recorded yet. Print history is recorded while OrcaSlicer is open.");

  const query = el('historySearch').value.trim().toLowerCase();
  const failures = el('failedJobs').checked;
  const jobs = scope.flatMap(({host,name,jobs}) => jobs
    .filter(job => job.file.toLowerCase().includes(query) && (!failures || ['error','cancelled'].includes(job.result)))
    .map(job => ({job,host,name}))).sort((a,b) => b.job.started - a.job.started);
  const completed = jobs.filter(({job}) => job.result === 'complete').length;
  el('historySummary').textContent = jobs.length
    ? t("Completed: {done} of {total}", {done:completed, total:jobs.length})
    : stats.jobs ? t("No matching jobs.") : '';
  el('historyTable').hidden = !jobs.length;
  const key = JSON.stringify(jobs.map(({job,host,name}) =>
    [host,name,job.file,job.started,job.ended,job.elapsed,job.result]));
  if (historyRowsKey === key) return;
  historyRowsKey = key;
  const results = {active:t("In Progress"), complete:t("Completed"), cancelled:t("Canceled"),
                   error:t("Error"), unknown:t("Unknown Result")};
  const rows = document.createDocumentFragment();
  for (const {job,host,name:printerName} of jobs) {
    const row = document.createElement('tr');
    row.setAttribute('role', 'row');
    const name = job.file || t("Unknown filename");
    const started = historyTime.format(job.started * 1000);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-filename';
    appendFilename(button, name);
    button.onclick = () => showInfo(name, [
      t("Printer") + ' · ' + printerName + (printerName === host ? '' : ' (' + host + ')'),
      t("Started: {time}", {time:started}),
      t("Ended: {time}", {time:Number.isSafeInteger(job.ended) && job.ended >= 0 && job.ended <= 253402300799 ? historyTime.format(job.ended * 1000) : t("Not recorded")}),
      t("Print time: {time}", {time:job.elapsed == null ? '—' : duration(job.elapsed)}),
      t("Result: {result}", {result:results[job.result]}),
      job.result === 'unknown' ? t("The end of this job was not observed.") : ''
    ].filter(Boolean).join('\n'));
    const label = document.createElement('span');
    label.className = 'history-printer-name';
    label.textContent = printerName;
    label.title = host;
    row.insertCell().append(button, label);
    for (const value of [started, job.elapsed == null ? '—' : duration(job.elapsed), results[job.result]])
      row.insertCell().textContent = value;
    rows.append(row);
  }
  el('historyRows').replaceChildren(rows);
}

const cameraImage = el('cameraImage');
const cameraUrl = el('cameraUrl');
const cameraStatus = el('cameraStatus');
const cameraSettings = el('cameraSettings');
el('cameraOpen').onclick = () => cameraSettings.showModal();
el('cameraSettingsClose').onclick = () => cameraSettings.close();
const cameraStorageKey = 'flashforge-device-tab-camera-' + (selectedPrinter ? '/' + selectedPrinter + '/' : location.pathname);
let cameraTimeout;
let managedCamera = false;
let cameraSession = null;
let cameraFrame = null;
let directCameraSource = '';
let frameUrl = '';
let resumeCamera = false;
const resumeKey = cameraStorageKey + '-resume';
try { resumeCamera = localStorage.getItem(resumeKey) === '1'; } catch {}
try { cameraUrl.value = localStorage.getItem(cameraStorageKey) || ''; } catch {}

function rememberCamera(value) {
  resumeCamera = value;
  try { localStorage.setItem(resumeKey, value ? '1' : '0'); } catch {}
}
function showCameraConnection(connected) {
  const start = el('cameraStart'), stop = el('cameraStop');
  const restoreFocus = document.activeElement === (connected ? start : stop);
  stop.hidden = !connected;
  start.disabled = connected;
  cameraUrl.readOnly = connected;
  if (restoreFocus) (connected ? stop : start).focus();
}

function showMonitorCameraFrame(visible) {
  el('camera').hidden = !visible;
  cameraImage.hidden = !visible;
}

function stopCamera(message = '', suspend = false) {
  if (!suspend) {
    if (managedCamera) rememberCamera(false);
    directCameraSource = '';
  }
  cameraSession?.abort();
  cameraSession = null;
  cameraFrame = null;
  clearTimeout(cameraTimeout);
  closeCameraViewer(cameraImage);
  cameraImage.removeAttribute('src');
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = '';
  showMonitorCameraFrame(false);
  showCameraConnection(false);
  cameraStatus.textContent = message;
  for (const tile of jobTiles.values()) {
    if (tile.shared) stopJobCamera(tile);
  }
}

// Parse JPEG segment lengths so an embedded thumbnail cannot terminate the outer image.
function* jpegFrames(chunk, state) {
  for (const byte of chunk) {
    if (state.length) {
      if (state.length === state.bytes.length) throw Error(t("Camera frame exceeds the size limit."));
      state.bytes[state.length++] = byte;
      if (state.sizeBytes) {
        state.size = state.size * 256 + byte;
        if (--state.sizeBytes === 0) {
          if (state.size < 2) throw Error(t("Invalid JPEG segment."));
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

async function readCameraFrames(endpointUrl, session, onFrame) {
  const response = await fetch(endpointUrl, {method:'POST', signal:session.signal,
    headers:{'X-FlashForge-Tab-Action':'camera-stream'}});
  if (!response.ok) throw Error((await response.json()).error || t("Camera unavailable."));
  const reader = response.body.getReader();
  const state = {bytes:new Uint8Array(2000000), length:0, marker:false, sizeBytes:0, skip:0};
  try {
    while (!session.signal.aborted) {
      const {done, value} = await reader.read();
      if (done) throw Error(t("Camera stream ended. Reconnect to try again."));
      for (const blob of jpegFrames(value, state)) {
        const url = URL.createObjectURL(blob);
        const decoded = new Image();
        decoded.src = url;
        try { await decoded.decode(); } catch (error) { URL.revokeObjectURL(url); throw error; }
        if (session.signal.aborted) { URL.revokeObjectURL(url); return; }
        onFrame(blob, url);
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

async function startManagedCamera() {
  if (cameraSession) return;
  const session = new AbortController();
  cameraSession = session;
  rememberCamera(true);
  showCameraConnection(true);
  cameraStatus.textContent = t("Opening Creator 5 Pro camera…");
  const frameTimeout = () => stopCamera(t("No camera frame received. Connect to try again."));
  cameraTimeout = setTimeout(frameTimeout, 20000);
  try {
    await readCameraFrames(endpoint('/camera-stream'), session, (blob, url) => {
      cameraImage.src = url;
      if (frameUrl) URL.revokeObjectURL(frameUrl);
      frameUrl = url;
      cameraFrame = blob;
      updateSharedJobCamera();
      showMonitorCameraFrame(true);
      cameraStatus.textContent = '';
      clearTimeout(cameraTimeout);
      cameraTimeout = setTimeout(frameTimeout, 10000);
    });
  } catch (error) {
    if (cameraSession === session) stopCamera(error.message || t("Could not open the camera."));
  }
}
function syncCameraView() {
  const shared = !el('jobs').hidden && [...jobTiles.values()].some(tile => tile.shared);
  const monitorVisible = !el('monitor').hidden;
  if (!monitorVisible) {
    closeCameraViewer(cameraImage);
    if (cameraSettings.open) cameraSettings.close();
  }
  const visible = !document.hidden && (shared || monitorVisible);
  if (!visible) {
    if (cameraSession || cameraImage.hasAttribute('src')) stopCamera('', true);
  } else if (managedCamera) {
    if (resumeCamera) startManagedCamera();
  } else if (directCameraSource && !cameraImage.hasAttribute('src')) {
    startDirectCamera();
  }
}
function startDirectCamera() {
  cameraImage.src = directCameraSource;
  showCameraConnection(true);
  cameraStatus.textContent = t("Connecting…");
  cameraTimeout = setTimeout(() => stopCamera(t("No frame received. Check the URL and confirm the stream uses MJPEG.")), 12000);
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
    stopCamera(t("Enter an HTTP or HTTPS MJPEG URL without a username or password."));
    return;
  }
  try { localStorage.setItem(cameraStorageKey, url.href); } catch {}
  directCameraSource = url.href;
  startDirectCamera();
};
cameraImage.onload = () => {
  if (managedCamera || !cameraImage.hasAttribute('src')) return;
  showMonitorCameraFrame(true);
  clearTimeout(cameraTimeout);
  cameraStatus.textContent = '';
};
cameraImage.onerror = () => {
  if (cameraImage.hasAttribute('src')) stopCamera(t("Could not open the MJPEG stream. Check the camera URL."));
};
el('cameraStop').onclick = () => stopCamera();
for (const id of ['printerDetails','manualPanel','readingsPanel','temperaturesPanel','files-section']) {
  const node = el(id), key='ff-layout:'+(selectedPrinter || location.pathname)+':'+id;
  try {
    const saved = localStorage.getItem(key);
    if (saved !== null) node.open = saved === 'open';
    if (id === 'manualPanel' && location.hash === '#manualPanel') node.open = true;
  } catch {}
  node.ontoggle=()=>{ try {localStorage.setItem(key,node.open?'open':'closed');} catch {}
    if (id==='temperaturesPanel' && node.open) drawChart();
  };
}
el('history').ontoggle = updateHistory;
new ResizeObserver(drawChart).observe(el('chartPlots'));
systemTheme.addEventListener('change', () => {
  if (display.theme !== 'system') return;
  applyDisplayStyles();
  drawChart();
});

function previewUrl(id) {
  if (!imageRequests.has(id)) {
    const promise = fetch(endpoint('/preview/' + id), {signal: AbortSignal.timeout(12000)})
      .then(response => {
        if (response.status === 404) {
          // A newly sent job may be listed before its thumbnail is available.
          setTimeout(() => {
            if (imageRequests.get(id) === promise) imageRequests.delete(id);
          }, 5000);
          return null;
        }
        if (!response.ok) throw Error(t("Preview unavailable"));
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
  const filename = lastCompletedFile || jobFile;
  const matching = files.filter(file => file.name === filename);
  const key = filename && matching.length === 1 ? matching[0].id : '';
  const image = el('jobImage');
  if (key !== previewKey) {
    previewKey = key;
    image.hidden = true;
  }
  if (!image.hidden) return;
  if (!key) return;
  const request = previewUrl(key);
  const url = await request;
  if (!url || previewKey !== key || imageRequests.get(key) !== request) return;
  image.src = url;
  image.hidden = false;
}

function renderJobProgress(card, value) {
  const progress = card.querySelector('progress');
  const percent = card.querySelector('.jobs-percent');
  progress.hidden = percent.hidden = value == null;
  progress.value = value ?? 0;
  percent.textContent = value == null ? '' : value + '%';
}

function renderLastCompleted(card, filename) {
  const row = card.querySelector('.jobs-last');
  row.hidden = !filename;
  row.querySelector('strong').textContent = filename || '';
}

function jobLayerLabel(layer, total) {
  if (layer == null) return '';
  return total ? t("Layer {layer} of {total}", {layer,total}) : t("Layer {layer}", {layer});
}

function jobTimeLeft(seconds) {
  return seconds == null ? '' : t("{time} left", {time:duration(seconds)});
}

function updateJobReprint() {
  const button = el('reprintJob');
  button.hidden = !jobReady || !lastCompletedFile;
  button.disabled = button.hidden || !catalogLoaded || el('refresh').disabled;
}

function isReadyForReprint(state, active) {
  return !active && ['READY', 'IDLE'].includes(state);
}

function showCompletionHint(card, complete, ready) {
  card.querySelector('.jobs-completion-hint').hidden = !complete || ready;
}

function updateJob(data) {
  const card = el('job');
  const previousJob = jobFile;
  lastCompletedFile = data.active ? '' : data.lastCompleted?.file || '';
  jobReady = isReadyForReprint(data.state, data.active);
  showCompletionHint(card, data.complete, jobReady);
  jobFile = data.active || (data.complete && !lastCompletedFile) ? data.file || '' : '';
  if (catalogLoaded && (data.fileCatalogRevision !== catalogRevision
      || (data.active && jobFile && previousJob !== jobFile && !files.some(file => file.name === jobFile)))) refreshFiles();
  const hasJob = Boolean(jobFile || data.active || data.complete);
  const showProgress = hasJob && !lastCompletedFile;
  renderLastCompleted(card, lastCompletedFile);
  el('jobFile').textContent = showProgress
    ? jobFile || (data.complete ? t("Print complete") : t("Printing")) : t("No active print job");
  el('jobFile').title = jobFile;
  card.dataset.active = String(Boolean(data.active));
  el('openJob').textContent = el('jobFile').textContent;
  el('openJob').title = jobFile;
  updateJobLink();
  updateJobReprint();
  renderJobProgress(card, showProgress ? data.percent : null);
  el('time').hidden = !showProgress || data.remaining == null;
  const finish = data.state === 'PAUSED' ? '' : finishAt(data.remaining, data.observedAt);
  el('finishTime').hidden = !finish;
  el('finishTime').textContent = finish ? t("Finish ≈ {time}", {time:finish}) : '';
  el('remaining').textContent = jobTimeLeft(data.remaining);
  el('timeHint').hidden = !data.active || !data.timeHint;
  el('timeHint').textContent = t(data.timeHint);
  el('layers').textContent = jobLayerLabel(data.layer, data.layerTotal);
  el('elapsed').textContent = data.elapsed == null ? '' : t("Elapsed: {time}", {time:duration(data.elapsed)});
  el('filament').textContent = data.filament == null ? '' : t("Estimated material: {weight}", {weight:weightFormat.format(data.filament)});
  el('jobMeta').hidden = !showProgress || (!el('layers').textContent && data.elapsed == null && data.filament == null);
  updateJobPreview();
}

async function updateStatus() {
  if (setupTab || polling || document.visibilityState !== 'visible') return;
  polling = true;
  const connection = el('status');
  try {
    const data = await getJson('/status');
    connectionFailure = '';
    lastObservation = data.observedAt;
    if (submittedFile && data.active && data.file === submittedFile) {
      el('filePrintStatus').textContent = t("Print started · {file}", {file:submittedFile});
      submittedFile = null;
      if (submittedSource) refreshFiles();
    } else if (submittedFile && Date.now() >= submittedUntil) {
      el('filePrintStatus').textContent = t("Start unconfirmed · {file}", {file:submittedFile});
      submittedFile = null;
    }
    lastSlots = data.materials || [];
    creatorMaterials = data.materialFamily === 'creator';
    showSlots(lastSlots, data.slotEditor);
    updateLight(data.light);
    connection.className = ['Error', 'Needs Attention', 'Paused', 'Pausing'].includes(data.status)
      ? 'connection attention' : 'connection connected';
    connection.textContent = stateLabel(data.status) || t("Connected");
    connection.title = '';
    el('monitor').classList.remove('stale');
    el('nozzle').textContent = plain(data.nozzle);
    el('nozzleTarget').textContent = data.nozzleTarget == null
      ? (Number.isFinite(data.nozzle) ? '°C' : '') : '/ ' + celsius(data.nozzleTarget);
    el('bed').textContent = plain(data.bed);
    el('bedTarget').textContent = data.bedTarget == null
      ? (Number.isFinite(data.bed) ? '°C' : '') : '/ ' + celsius(data.bedTarget);
    const tools = data.tools || [];
    el('nozzleLabel').textContent = tools.length ? 'T1' : t("Nozzle");
    const picker = el('chartTool');
    if (picker.options.length !== tools.length) {
      picker.replaceChildren(...tools.map((_,i) => new Option('T'+(i+1),'t'+(i+1))));
      chartKey = tools.length ? picker.value : 'nozzle';
    }
    updateLegend();
    const temperatures = (actual, target) => [actual, target]
      .map(value => value == null ? '—' : celsius(value)).join(' / ');
    const heated = tools.filter(tool => tool.target > 0);
    const multi = tools.length > 1;
    updateReadingsSummary(data, tools, heated);
    el('singleNozzle').hidden = multi && heated.length !== 1;
    if (multi && heated.length === 1) {
      const tool = heated[0];
      el('nozzleLabel').textContent = t("Nozzle T{tool}", {tool:tools.indexOf(tool) + 1});
      el('nozzle').textContent = plain(tool.actual);
      el('nozzleTarget').textContent = '/ ' + celsius(tool.target);
    }
    const others = el('otherTools');
    others.hidden = !multi;
    if (multi) {
      const secondary = tools.flatMap((tool, i) => {
        if (heated.length === 1 && tool === heated[0]) return [];
        const item = document.createElement(tool.target > 0 ? 'strong' : 'span');
        const value = tool.target === 0 ? (celsius(tool.actual) || '—') : temperatures(tool.actual, tool.target);
        item.textContent = 'T' + (i + 1) + ' ' + value;
        return [item];
      });
      others.replaceChildren(heated.length === 1 ? t("Other nozzles") : t("Nozzles"), ...secondary);
    }
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
    connection.textContent = error.status === 409 ? t("Profile changed") : t("Disconnected");
    setReadingsSummary(error.status === 409 ? t("Profile changed") : t("Readings unavailable"));
    connection.title = error.message;
    el('monitor').classList.add('stale');
    showSlots(lastSlots);
    updateLight(null);
    updateActions({}, error.status !== 409);
    jobReady = false;
    updateJobReprint();
    showCompletionHint(el('job'), false, false);
    el('time').hidden = true;
    el('timeHint').hidden = true;
    if (error.status === 409) {
      updateJob({});
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
  set('printerLight', sensors.led == null ? '' : sensors.led ? t("On") : t("Off"));
  const ends = sensors.endstops || {};
  const parts = Object.entries(ends).map(([axis, value]) => axis.toUpperCase() + ' ' + value);
  const door = {open:"Open", close:"Closed"}[sensors.door];
  if (door) parts.push(t("Door / lid: {state}", {state:t(door)}));
  if (sensors.errorCode) parts.push(t("Error: {code}", {code:sensors.errorCode}));
  set('printerSensors', Object.keys(ends).length ? t("Endstops: {state}", {state:parts.join('  ')}) : parts.join('  '));
}

let manual = null, manualPending = false, manualHomeRequired = false;
let calibration = null, calibrationRequest = '';
const calibrationLabels = {level:t("Bed leveling"), shaper:t("Input shaper")};
let manualFeedback = '', manualDetail = '', manualMotion = '', feedbackTimer;
let fastMotionPollUntil = 0;
const manualBody = el('manualBody');
function manualLabel(key) {
  if (/^[xyze][+-]/.test(key)) return key[0].toUpperCase() + ' ' + key.slice(1).replace('-', '−') + ' mm';
  if (key.startsWith('heat')) return key === 'heat0' ? t("Heat off") : t("Nozzle {temperature} °C", {temperature:key.slice(4)});
  if (key.startsWith('timer')) return key === 'timer0' ? t("Timer off") : t("Timer {minutes} min", {minutes:key.slice(5)});
  return {home:t("Home"), release:t("Release motors"), level:t("Level bed"), shaper:t("Input shaper"), clear:t("Clear error")}[key] || key;
}
function manualKey(button) {
  if (button.dataset.zStep) {
    if (!['bed','nozzle'].includes(manual?.zPart)) return '';
    const delta = Number(el('jogStep').querySelector(':checked').value) * Number(button.dataset.zStep) * (manual.zPart === 'bed' ? -1 : 1);
    return 'z' + (delta > 0 ? '+' : '') + delta;
  }
  if (button.dataset.jog) return button.dataset.jog + el('jogStep').querySelector(':checked').value;
  if (button.hasAttribute('data-heat')) return 'heat' + el('heatTarget').value;
  if (button.hasAttribute('data-timer')) return 'timer' + el('manualTimer').value;
  return button.dataset.manual;
}
function renderManual() {
  el('manualPanel').hidden = !manual;
  el('zPart').textContent = manual?.zPart === 'bed' ? t("Bed Z") : t("Nozzle Z");
  const coordinate = axis => Number.isFinite(manual?.position?.[axis]) ? manual.position[axis].toFixed(2) : '—';
  el('xyPosition').textContent = 'X ' + coordinate('X') + '  Y ' + coordinate('Y');
  el('zPosition').textContent = coordinate('Z');
  const distance = el('feedDistance');
  if (distance) distance.textContent = manual?.filamentNeedsHeat ? t("Heat nozzle to 180 °C") : t("10 mm per move");
  const timer = el('manualTimer');
  if (!timer.dataset.edited) timer.value = manual?.timerValue ?? '';
  for (const node of manualBody.querySelectorAll('[data-group]'))
    node.hidden = !manual?.groups.includes(node.dataset.group) || (node.dataset.group === 'clear' && !manual.errorCode);
  for (const button of manualBody.querySelectorAll('button')) {
    const key = manualKey(button);
    if (button.dataset.jog || button.dataset.zStep) {
      const limited = Boolean(manual?.travelLimited?.includes(key));
      button.dataset.travelLimited = String(limited);
      button.title = limited ? t("This step exceeds the axis travel limit") : '';
    }
    if (key === 'release') {
      const released = Boolean(manual?.released);
      button.dataset.released = String(released);
      button.title = released ? t("Motors released. Press Home before moving the axes.") : t("Release motors");
      button.setAttribute('aria-label', button.title);
    }
    button.disabled = manualPending || !manual?.allowed.includes(key)
      || (calibration?.phase === 'waiting' && Object.hasOwn(calibrationLabels, key));
  }
  const [status, ...hints] = (manual?.description || '').split(' · ');
  const checking = manual?.checkingPosition;
  const activity = el('manualActivity');
  activity.textContent = manualDetail || manualFeedback;
  activity.title = activity.textContent;
  if (status === 'Locked') hints[0] = calibration?.phase === 'running' ? '' : t("Manual controls require an idle printer");
  const operation = calibrationLabels[calibrationRequest || calibration?.key] || t("Calibration");
  const phase = calibrationRequest ? 'sending' : calibration?.phase;
  const feedback = {
    sending:t("Sending request…"), waiting:t("Waiting for the printer…"), running:t("In progress"),
    checking:t("Checking printer status…"), ready:t("Finished. Printer ready."),
    failed:t("Printer reported an error."), unconfirmed:t("Start unconfirmed. Check the printer."),
    unavailable:t("Printer status unavailable."),
  }[phase];
  const message = feedback ? operation + ' · ' + feedback : '';
  el('calibrationStatus').hidden = !message;
  el('calibrationProgress').hidden = phase !== 'running';
  const calibrationMessage = el('calibrationMessage');
  if (calibrationMessage.textContent !== message) calibrationMessage.textContent = message;
  const homeNotice = el('manualHomeNotice');
  let homeMessage = '';
  if (manual?.releaseHoming) homeMessage = t("Waiting for Home to finish…");
  else if (status === 'Unavailable') homeMessage = t("Waiting for printer…");
  else if (manualMotion) homeMessage = manualMotion;
  else if (checking) homeMessage = t("Checking position…");
  else if ((manual?.released || manualHomeRequired) && manual?.allowed.includes('home'))
    homeMessage = t("Press Home to move.");
  else if (hints.filter(Boolean).length) homeMessage = hints.filter(Boolean).map(part => t(part)).join(' · ');
  else if (manual?.allowed.some(key => /^[xyz][+-]/.test(key))) homeMessage = t("Ready to move");
  if (homeNotice.textContent !== homeMessage) homeNotice.textContent = homeMessage;
}
async function sendManual(key) {
  if (manualPending || !manual?.allowed.includes(key)) return;
  const isCalibration = Object.hasOwn(calibrationLabels, key);
  const isAxisJog = /^[xyz][+-]/.test(key);
  const isHttpJog = isAxisJog && manual.tcpMotion === false;
  if (isCalibration && calibration?.phase === 'waiting') return;
  manualDetail = '';
  if (key === 'home') manualHomeRequired = false;
  if (isCalibration) calibrationRequest = key;
  clearTimeout(feedbackTimer);
  manualFeedback = isCalibration ? '' : t("Sending…");
  manualPending = true;
  renderManual();
  try {
    const timeout = key === 'home' ? 45000 : 15000;
    const data = await postLocal('action', endpoint('/action'), {action:'manual/' + key}, timeout);
    if (!isCalibration) {
      manualFeedback = t("{command} · Accepted", {command:manualLabel(key)});
      feedbackTimer = setTimeout(() => { manualFeedback = ''; manualDetail = ''; renderManual(); }, 3000);
      renderManual();
    }
    if (isAxisJog) manualHomeRequired = false;
    if (isAxisJog || key === 'home' || key === 'release') fastMotionPollUntil = Date.now() + 5000;
    if (key.startsWith('timer')) delete el('manualTimer').dataset.edited;
    if (isCalibration) calibration = {key, phase:'waiting'};
    else if (isHttpJog || (key !== 'home' && !/^[xyze][+-]/.test(key))) manualDetail = t(data.state);
  } catch (error) {
    const homeRequired = error.code === 'home_required';
    const travelLimit = error.code === 'travel_limit';
    const axesBusy = error.code === 'axes_busy';
    const noReply = error.code === 'no_response' || ['TimeoutError','AbortError','TypeError'].includes(error.name);
    manualFeedback = homeRequired ? t("Home required") : travelLimit ? t("Travel limit")
      : axesBusy ? t("Axes busy") : noReply ? t("No response") : t("Command failed");
    if (homeRequired) manualHomeRequired = true;
    if (!homeRequired) manualDetail = isCalibration ? calibrationLabels[key] + ' · ' + error.message : error.message;
  } finally {
    calibrationRequest = '';
    renderManual();
    await updateStatus();
    manualPending = false;
    renderManual();
  }
}
manualBody.onclick = event => {
  const button = event.target.closest('button');
  if (button) sendManual(manualKey(button));
};
el('jogStep').onchange = renderManual;
el('manualTimer').onchange = () => { el('manualTimer').dataset.edited = '1'; renderManual(); };
manualBody.onkeydown = event => {
  if (!event.target.matches('.jog-pad') || event.altKey || event.ctrlKey || event.metaKey) return;
  const axis = {ArrowLeft:'x-',ArrowRight:'x+',ArrowDown:'y-',ArrowUp:'y+'}[event.key];
  if (!axis) return;
  event.preventDefault();
  if (!event.repeat) sendManual(axis + (event.shiftKey ? '10' : '1'));
};
function updateActions(data, preserveManual = false) {
  manualMotion = data.state === 'HOMING' ? t("Homing…") : data.state === 'MOVING' ? t("Moving…") : '';
  calibration = preserveManual && ['waiting','running','checking','unavailable'].includes(calibration?.phase)
    ? {...calibration, phase:'unavailable'} : data.calibration || null;
  manual = preserveManual && manual
    ? {...manual, position: null, allowed: [], filamentNeedsHeat: false, description: 'Unavailable · Waiting for current printer readings'}
    : data.manual || null;
  renderManual();
  const state = data.state || '';
  observedJob = data.active && data.jobControls ? data.jobToken || '' : '';
  const paused = observedJob && state === 'PAUSED';
  const running = observedJob && !paused && state !== 'PAUSING';
  el('pausePrint').hidden = !running;
  el('resumePrint').hidden = !paused;
  el('cancelPrint').hidden = !(running || paused);
  const cancel = el('cancelPrint');
  if (cancel.classList.contains('armed') && cancel.dataset.job !== observedJob) {
    cancel.classList.remove('armed');
    cancel.textContent = t("Cancel");
  }
}

async function runAction(action, job = observedJob) {
  const status = el('actionStatus');
  const buttons = el('jobActions').querySelectorAll('button');
  if (!job || job !== observedJob || Array.from(buttons).some(button => button.disabled)) return;
  for (const button of buttons) button.disabled = true;
  status.hidden = false;
  status.textContent = t("Checking the result of {command}…", {command:jobActionLabel(action)});
  try {
    const response = await fetch(endpoint('/action'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-FlashForge-Tab-Action': 'action'},
      body: JSON.stringify({action, job}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || t("The printer did not accept the command."));
    status.textContent = t("Printer confirmed {command} · {state}", {command:jobActionLabel(action), state:stateLabel(data.state)});
    await updateStatus();
  } catch (error) {
    status.textContent = error.message || t("Command result unavailable.");
  } finally {
    for (const button of buttons) button.disabled = false;
  }
}

function saveCameraBlob(blob) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'printer-snapshot.jpg';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}

async function downloadSnapshot() {
  try {
    let blob = cameraFrame;
    if (!blob) {
      if (managedCamera) return;
      const response = await fetch(endpoint('/snapshot'));
      if (!response.ok) throw new Error(t("No snapshot"));
      blob = await response.blob();
    }
    saveCameraBlob(blob);
  } catch {
    el('cameraStatus').textContent = t("Could not read a snapshot from the printer.");
  }
}

let printOptions = null, selectedFile = null, submittedFile = null, submittedUntil = 0, submittedSource = false;
let sourceRevision = 0, sourceToken = '';
el('dismissPrint').onclick = () => el('printDialog').close();
el('printDialog').onclose = () => {
  sourceRevision++;
  el('sourceFile').value = '';
};

function renderPrintSlots(requirements) {
  const rows = el('printSlots');
  rows.replaceChildren();
  const slots = lastSlots.length ? lastSlots : printOptions.slots;
  for (const item of requirements) {
    const label = document.createElement('label'), select = document.createElement('select');
    const title = document.createElement('span');
    const number = item.toolId + 1;
    title.textContent = (printOptions.family === 'creator' ? 'T' + number : t("Filament {number}", {number}))
      + (item.materialName ? ' · ' + item.materialName : '');
    select.dataset.tool = item.toolId;
    select.required = Boolean(item.materialName);
    select.add(new Option(item.materialName ? t("Choose a loaded slot") : t("Unused"), ''));
    for (const slot of slots) {
      if (slot.present === false || !slot.name || !slot.color ||
          (item.materialName && item.materialName.toLowerCase() !== slot.name.toLowerCase())) continue;
      select.add(new Option(t("Slot {number}", {number:slot.id}) + ' · ' + slot.name + ' ' + slot.color, slot.id));
    }
    if (item.materialName && select.options.length === 1) select.options[0].textContent = t("No loaded slot matches");
    label.append(title, select);
    rows.append(label);
    if (item.materialColor) {
      const colors = document.createElement('div');
      colors.className = 'filament-colors';
      colors.id = 'filament-colors-' + item.toolId;
      colors.setAttribute('role','status');
      select.setAttribute('aria-describedby',colors.id);
      const swatch = (name, color) => {
        const text = document.createElement('span'), marker = document.createElement('span');
        marker.className = 'filament-mark';
        marker.style.backgroundColor = color;
        marker.setAttribute('aria-hidden','true');
        text.append(marker, name + ' ' + color.toUpperCase());
        return text;
      };
      const updateColors = () => {
        colors.replaceChildren(swatch(t("File"), item.materialColor));
        const slot = slots.find(entry => String(entry.id) === select.value);
        if (!slot?.color) return;
        colors.append(swatch(t("Slot"),slot.color));
        if (item.materialColor.toUpperCase() !== slot.color.toUpperCase()) {
          const warning = document.createElement('span');
          warning.className = 'filament-warning';
          warning.dataset.icon = 'warning';
          warning.textContent = t("Color differs");
          colors.append(warning);
        }
      };
      select.onchange = updateColors;
      updateColors();
      rows.append(colors);
    }
  }
}

async function readPrintSource(file) {
  const revision = ++sourceRevision;
  const current = () => sourceRevision === revision && el('printDialog').open;
  const status = el('sourceStatus');
  const saved = el('useSavedSource');
  const input = el('sourceFile');
  el('sourceHelp').hidden = false;
  sourceToken = '';
  renderPrintSlots([]);
  el('confirmPrint').disabled = saved.disabled = input.disabled = true;
  status.textContent = file ? t("Reading materials…") : t("Checking the saved preview…");
  try {
    const url = endpoint('/print-source');
    url.searchParams.set('printer', selectedFile.printer);
    url.searchParams.set('file', selectedFile.id);
    if (file) url.searchParams.set('name', file.name);
    const response = await fetch(url, {method:'POST', body:file,
      headers:{'X-FlashForge-Tab-Action':'print-source'}, signal:AbortSignal.timeout(90000)});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || t("Could not read the source."));
    if (!current()) return;
    sourceToken = data.token;
    renderPrintSlots(data.requirements);
    el('sourceHelp').hidden = true;
    el('confirmPrint').disabled = false;
    status.textContent = file ? file.name : t("Linked materials loaded. Check that the stored file has not been replaced.");
  } catch (error) { if (current()) status.textContent = error.message; }
  finally { if (current()) saved.disabled = input.disabled = false; }
}
el('sourceFile').onchange = event => {
  const file = event.target.files[0];
  if (file) readPrintSource(file);
};
el('useSavedSource').onclick = () => readPrintSource();

function openPrint(file) {
  selectedFile = {...file, printer:printOptions.printer};
  sourceRevision++;
  sourceToken = '';
  const creator = printOptions.family === 'creator';
  el('printSource').hidden = !creator;
  el('sourceHelp').hidden = false;
  el('sourceFile').value = '';
  el('sourceFile').disabled = false;
  el('useSavedSource').hidden = !file.savedSource;
  el('useSavedSource').disabled = false;
  el('sourceStatus').textContent = '';
  el('confirmPrint').disabled = creator;
  el('printSelection').textContent = file.name + ' · ' + printOptions.printer;
  el('printError').textContent = '';
  el('levelOption').hidden = !printOptions.http;
  el('printLevel').checked = false;
  renderPrintSlots(printOptions.family === 'ad5x' && (lastSlots.length || printOptions.slots.length)
    ? Array.from({length:4}, (_,toolId) => ({toolId})) : []);
  el('printDialog').showModal();
}
el('printForm').onsubmit = async event => {
  event.preventDefault();
  if (el('confirmPrint').disabled) return;
  const selections = [...el('printSlots').querySelectorAll('select')];
  const mapping = ['0','0','0','0'];
  for (const select of selections) mapping[Number(select.dataset.tool)] = select.value || '0';
  const slots = selections.length ? mapping.join('') : '';
  if (slots && /^0+$/.test(slots)) { el('printError').textContent = t("Select the slots used by this file."); return; }
  el('confirmPrint').disabled = true;
  try {
    const file = selectedFile;
    const url = endpoint('/action');
    url.searchParams.set('printer', file.printer);
    await postLocal('action', url, {action:'print/' + file.id + '/' + Number(el('printLevel').checked) + '/' + slots + (sourceToken ? '/' + sourceToken : '')});
    submittedFile = file.name;
    submittedUntil = Date.now() + 60000;
    submittedSource = Boolean(sourceToken);
    el('filePrintStatus').textContent = t("Print requested · {file}", {file:file.name});
    el('filePrintStatus').hidden = false;
    el('printDialog').close();
    await updateStatus();
  } catch (error) { el('printError').textContent = error.message; }
  finally { el('confirmPrint').disabled = false; }
};
let selectedCatalogFile = null;
function selectCatalogFile(file) {
  selectedCatalogFile = file;
  const print = el('printSelected');
  print.hidden = !printOptions?.family;
  const unsupported = file && printOptions?.family === 'creator' && !file.name.toLowerCase().endsWith('.3mf');
  print.disabled = !file || !printOptions?.family || el('refresh').disabled || Boolean(unsupported);
  const selection = el('fileSelection');
  selection.hidden = !printOptions?.family;
  selection.textContent = unsupported ? t("Creator repeat printing requires a .3mf.")
    : file ? t("Selected · {file}", {file:file.name}) : t("Select a file to print.");
  for (const card of el('files').querySelectorAll('button[data-file-id]'))
    card.setAttribute('aria-pressed', String(card.dataset.fileId === file?.id));
}
el('printSelected').onclick = () => {
  if (!el('printSelected').disabled && selectedCatalogFile) openPrint(selectedCatalogFile);
};
function fileCard(file) {
  const selectable = Boolean(printOptions?.family);
  const card = document.createElement(selectable ? 'button' : 'article');
  if (selectable) {
    card.type = 'button';
    card.dataset.fileId = file.id;
    card.setAttribute('aria-pressed', 'false');
    card.onclick = () => selectCatalogFile(file);
  }
  card.className = 'file-card';
  const preview = document.createElement('span');
  preview.className = 'preview muted';
  preview.hidden = !display.previews;
  preview.innerHTML = '<svg viewBox="0 0 48 48" width="38" height="38" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="m24 5 16 9v19l-16 10-16-10V14z M8 14l16 10 16-10 M24 24v19"/></svg>';
  const name = document.createElement('span');
  name.className = 'file-name';
  const label = document.createElement('span');
  label.className = 'file-name-label';
  appendFilename(label, file.name);
  name.title = file.name;
  name.append(label);
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
      image.onload = () => { if (version === renderVersion) preview.replaceChildren(image); };
      image.src = url;
    }
  }
}

const fileGrid = el('files');
function catalogPageSize() {
  if (!fileGrid.clientWidth) return pageSize;
  const columns = getComputedStyle(fileGrid).gridTemplateColumns.split(' ').length;
  return columns * display.rows;
}

function renderFiles(nextPageSize = catalogPageSize()) {
  const version = ++renderVersion;
  const query = el('search').value.trim().toLocaleLowerCase();
  const matching = files.filter(file => file.name.toLocaleLowerCase().includes(query));
  if (nextPageSize !== pageSize) {
    const first = page * pageSize;
    const selected = matching.findIndex(file => file.id === selectedCatalogFile?.id);
    const anchor = selected >= first && selected < first + pageSize ? selected : first;
    page = Math.floor(anchor / nextPageSize);
    pageSize = nextPageSize;
  }
  const pageCount = Math.ceil(matching.length / pageSize);
  page = Math.max(0, Math.min(page, pageCount - 1));
  el('fileCount').textContent = matching.length ? fileCountLabel(matching.length) : query ? t("No matching files") : t("No files");
  el('pager').hidden = pageCount < 2;
  el('previous').disabled = page === 0;
  el('next').disabled = page + 1 >= pageCount;
  el('pageNumber').value = page + 1;
  el('pageTotal').textContent = pageCount;
  el('pageTotal').setAttribute('aria-label', t("Total pages: {count}", {count:pageCount}));
  const entries = matching.slice(page * pageSize, (page + 1) * pageSize).map(file => ({file, ...fileCard(file)}));
  fileGrid.replaceChildren(...entries.map(entry => entry.card));
  selectCatalogFile(entries.find(entry => entry.file.id === selectedCatalogFile?.id)?.file || null);
  if (display.previews) loadPreviews(entries, version);
}

new ResizeObserver(() => {
  const size = catalogPageSize();
  if (size !== pageSize) renderFiles(size);
}).observe(fileGrid);

async function refreshFiles() {
  if (el('refresh').disabled || fileUploadBusy) return;
  el('refresh').disabled = true;
  updateJobReprint();
  selectCatalogFile(null);
  el('fileCount').textContent = t("Loading…");
  el('filesSummary').textContent = t("Loading…");
  try {
    const catalog = await getJson('/files');
    files = catalog.files;
    printOptions = catalog.printOptions;
    catalogRevision = catalog.revision;
    el('uploadFile').hidden = !printOptions?.upload;
    openReprintFromUrl();
    el('filesSummary').textContent = files.length ? fileCountLabel(files.length) : t("No files");
    catalogLoaded = true;
    for (const promise of imageRequests.values()) promise.then(url => { if (url) URL.revokeObjectURL(url); });
    imageRequests.clear();
    previewKey = '';
    el('jobImage').hidden = true;
    page = 0;
    updateJobPreview();
    renderFiles();
  } catch {
    el('fileCount').textContent = t("Could not load the file list");
    el('filesSummary').textContent = t("Could not load files");
  } finally {
    el('refresh').disabled = false;
    updateJobReprint();
    selectCatalogFile(selectedCatalogFile);
  }
}

const uploadFileDialog = el('uploadFileDialog');
const uploadFileInput = el('uploadFileInput');
const uploadFileName = el('uploadFileName');
const uploadFileProgress = el('uploadFileProgress');
const uploadFileMessage = el('uploadFileMessage');

el('uploadFile').onclick = () => {
  uploadFileInput.value = '';
  uploadFileName.value = '';
  uploadFileMessage.textContent = t("The file will be saved on the printer without starting a print.");
  el('uploadFilePrinter').textContent = currentPrinterName();
  uploadFileProgress.hidden = true;
  uploadFileDialog.showModal();
};
el('closeUploadFile').onclick = () => uploadFileDialog.close();
uploadFileDialog.oncancel = event => { if (fileUploadBusy) event.preventDefault(); };
uploadFileInput.onchange = () => {
  const file = uploadFileInput.files[0];
  const suffix = file?.name.match(/(?:\.gcode\.3mf|\.3mf|\.gcode)$/i)?.[0];
  if (!suffix) { uploadFileName.value = ''; return; }
  const stem = file.name.slice(0,-suffix.length).normalize('NFKD').replace(/[^A-Za-z0-9 _.-]/g,'')
    .replace(/^[^A-Za-z0-9]+/,'').trim().slice(0,120 - suffix.length);
  uploadFileName.value = (stem || 'Print') + suffix;
};
el('uploadFileForm').onsubmit = async event => {
  event.preventDefault();
  if (fileUploadBusy) return;
  const file = uploadFileInput.files[0];
  const name = uploadFileName.value.trim();
  if (!file || !name || !printOptions?.upload) return;
  if (!/^[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}$/.test(name) || !/\.(?:gcode|3mf)$/i.test(name)) {
    uploadFileMessage.textContent = t("Use A–Z, digits, spaces, dots, underscores or hyphens and a .gcode or .3mf extension.");
    return;
  }
  if (!file.size || file.size > 128 * 1024 * 1024) {
    uploadFileMessage.textContent = t("Choose a nonempty print file no larger than 128 MB.");
    return;
  }
  const url = endpoint('/upload-file');
  url.searchParams.set('printer', printOptions.printer);
  url.searchParams.set('name', name);
  fileUploadBusy = true;
  const controls = [uploadFileInput, uploadFileName, el('closeUploadFile'), el('confirmUploadFile')];
  controls.forEach(control => { control.disabled = true; });
  uploadFileProgress.value = 0;
  uploadFileProgress.hidden = false;
  uploadFileMessage.textContent = t("Sending file…");
  try {
    await new Promise((resolve,reject) => {
      const request = new XMLHttpRequest();
      request.open('POST', url.href);
      request.responseType = 'json';
      request.timeout = 720000;
      request.setRequestHeader('X-FlashForge-Tab-Action','upload-file');
      request.upload.onprogress = progress => {
        if (progress.lengthComputable) uploadFileProgress.value = progress.loaded / progress.total * 100;
      };
      request.upload.onload = () => {
        uploadFileProgress.removeAttribute('value');
        uploadFileMessage.textContent = t("Saving file on the printer and checking the result…");
      };
      request.onload = () => request.status === 200 ? resolve()
        : reject(Error(request.response?.error || t("Could not confirm the upload. Refresh Printer Files.")));
      request.onerror = request.ontimeout = () => reject(Error(t("Could not confirm the upload. Refresh Printer Files before retrying.")));
      request.send(file);
    });
    uploadFileDialog.close();
    el('search').value = '';
    el('filePrintStatus').textContent = t("Uploaded · {file}", {file:name});
    el('filePrintStatus').hidden = false;
  } catch (error) {
    uploadFileMessage.textContent = error.message;
  } finally {
    fileUploadBusy = false;
    controls.forEach(control => { control.disabled = false; });
    uploadFileProgress.hidden = true;
    await refreshFiles();
  }
};
el('refresh').onclick = refreshFiles;
el('search').oninput = () => { page = 0; renderFiles(); };
el('pageJump').onsubmit = event => {
  event.preventDefault();
  const input = el('pageNumber');
  const value = input.value.trim();
  if (/^[0-9]+$/.test(value)) {
    page = Math.min(Number(el('pageTotal').textContent), Math.max(1, Number(value))) - 1;
    renderFiles();
  } else {
    input.value = page + 1;
  }
  input.select();
};
el('pageNumber').onblur = event => { event.target.value = page + 1; };
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
      try { parts = JSON.parse(line); } catch { throw Error(t("Invalid JSON printer row.")); }
      if (!Array.isArray(parts) || parts.length !== 4 || !parts.every(p => typeof p === 'string'))
        throw Error(t("A JSON printer row needs four strings: address, CheckCode, name and serial."));
    } else {
      parts = line.split('|').map(part => part.trim());
      if (parts.length > 4) throw Error(t("Use a JSON array for values containing a vertical bar."));
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
const fleetSearch = el('fleetSearch');
const fleetFilters = [...document.querySelectorAll('[data-fleet-filter]')];
let fleetFilter = 'all';
const fleetCameraButton = el('fleetCameras');
const confirmedBeds = new Map();
let fleetCameras = localStorage.getItem('ff-fleet-cameras') !== 'off';
let fleetCameraScope = localStorage.getItem('ff-fleet-camera-scope') === 'printing' ? 'printing' : 'all';
fleetCameraButton.setAttribute('aria-pressed', String(fleetCameras));

function waitingForPartRemoval(item) {
  return display.fleetWaitForRemoval && !item.printing
    && Boolean(item.bedClearToken && confirmedBeds.get(item.host) !== item.bedClearToken);
}

function fleetGroup(item) {
  if (waitingForPartRemoval(item) || item.error || ['PAUSED', 'PAUSING', 'ERROR', 'ATTENTION'].includes(item.state)) return 'attention';
  if (item.printing) return 'active';
  if (item.observedAt && !['BUSY', 'CALIBRATING', 'CANCELING', 'CANCELLING'].includes(item.state)) return 'idle';
  return 'other';
}

function matchesFleetSearch(item, query) {
  return !query || [item.name, item.host, item.printing ? item.file : item.lastCompleted?.file]
    .some(value => value?.toLowerCase().includes(query));
}

function clearFleetFilters() {
  fleetFilter = 'all';
  fleetSearch.value = '';
}

function updateJobLink() {
  const linked = view !== 'fleet' && el('job').dataset.active === 'true';
  el('openJob').hidden = !linked;
  el('jobFile').hidden = linked;
}

const jobTiles = new Map();
const jobsCameraDialog = el('jobsCameraDialog');
let enlargedCamera = null;

function placeCameraImage(stream, parent, before = null) {
  // A state-preserving move keeps direct MJPEG requests open in Chromium.
  if (parent.moveBefore) parent.moveBefore(stream, before);
  else parent.insertBefore(stream, before);
}

function openCameraViewer(stream, name) {
  if (enlargedCamera || stream.hidden) return;
  enlargedCamera = {stream, parent:stream.parentNode, before:stream.nextSibling};
  el('jobsCameraName').textContent = name;
  placeCameraImage(stream, el('jobsCameraFrame'));
  jobsCameraDialog.showModal();
}

function closeCameraViewer(stream) {
  if (enlargedCamera?.stream === stream && jobsCameraDialog.open) jobsCameraDialog.close();
}

jobsCameraDialog.onclose = () => {
  if (!enlargedCamera) return;
  const {stream, parent, before} = enlargedCamera;
  enlargedCamera = null;
  if (parent.isConnected) placeCameraImage(stream, parent, before);
  else stream.remove();
};
el('cameraExpand').onclick = () => openCameraViewer(cameraImage, currentPrinterName());

function jobEndpoint(path, host) {
  const url = endpoint(path);
  url.searchParams.set('printer', host);
  return url;
}

function jobPage(item, {reprint, hash = 'monitor'} = {}) {
  const url = printerPage(item.host);
  if (separateTab && item.host === fleetPrinters[0]?.host) url.searchParams.delete('printer');
  url.searchParams.delete('reprint');
  if (reprint) url.searchParams.set('reprint', reprint);
  url.hash = hash;
  return url;
}

function showFleetFiles() {
  const section = el('files-section');
  section.open = true;
  section.scrollIntoView({block:'start'});
  section.querySelector('summary').focus({preventScroll:true});
}

function openReprintFromUrl() {
  const url = new URL(location.href);
  const name = url.searchParams.get('reprint');
  if (!name) return;
  url.searchParams.delete('reprint');
  window.history.replaceState(null, '', url);
  openReprint(name);
}

function openReprint(name) {
  const matches = files.filter(file => file.name === name);
  if (matches.length !== 1 || !printOptions?.family) {
    showInfo(t("Print again"), t("The file is unavailable or cannot be identified on this printer."));
  } else if (printOptions.family === 'creator' && !name.toLowerCase().endsWith('.3mf')) {
    showInfo(t("Print again"), t("Creator repeat printing requires a .3mf."));
  } else {
    openPrint(matches[0]);
  }
}

el('reprintJob').onclick = () => {
  if (lastCompletedFile && !el('reprintJob').disabled) openReprint(lastCompletedFile);
};

function jobCameraSource(item) {
  if (item.managedCamera) return 'managed';
  try {
    const configKey = item.current ? cameraStorageKey : 'flashforge-device-tab-camera-/' + item.host + '/';
    const configured = localStorage.getItem(configKey) ||
      (separateTab && item.host === fleetPrinters[0]?.host && localStorage.getItem('flashforge-device-tab-camera-/flashforge/'));
    const url = new URL(configured || item.cameraUrl);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) return url.href;
  } catch {}
  return '';
}

function stopJobCamera(tile) {
  closeCameraViewer(tile.stream);
  tile.camera?.abort();
  tile.camera = null;
  tile.shared = false;
  tile.blob = null;
  clearTimeout(tile.cameraTimeout);
  tile.stream.removeAttribute('src');
  tile.stream.hidden = true;
  tile.cameraPanel.hidden = true;
  tile.mediaStatus.textContent = '';
  tile.mediaStatus.hidden = true;
  tile.node.querySelector('[data-camera]').hidden = true;
  tile.node.querySelector('[data-snapshot]').hidden = true;
  if (tile.frame) URL.revokeObjectURL(tile.frame);
  tile.frame = '';
}

function stopJobCameras() {
  for (const tile of jobTiles.values()) stopJobCamera(tile);
}
window.addEventListener('pagehide', stopJobCameras);
window.addEventListener('pageshow', () => { if (!el('jobs').hidden) renderJobs(); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopJobCameras();
  else if (!el('jobs').hidden) renderJobs();
});

function showJobCameraFrame(tile) {
  tile.cameraPanel.hidden = false;
  tile.stream.hidden = false;
  tile.node.querySelector('[data-camera]').hidden = false;
  tile.node.querySelector('[data-snapshot]').hidden = !tile.blob;
  tile.mediaStatus.textContent = '';
  tile.mediaStatus.hidden = true;
  tile.retry.hidden = true;
}

function updateSharedJobCamera() {
  for (const tile of jobTiles.values()) {
    if (!tile.shared || !cameraFrame) continue;
    tile.stream.src = frameUrl;
    tile.blob = cameraFrame;
    showJobCameraFrame(tile);
  }
}

function startJobCamera(tile, item) {
  const enabled = fleetCameras && (fleetCameraScope === 'all' || item.printing);
  const source = enabled && !item.error ? jobCameraSource(item) : '';
  if (tile.source !== source) {
    stopJobCamera(tile);
    tile.source = source;
    tile.failed = false;
  }
  if (document.hidden || !source || tile.camera || tile.shared || tile.failed) return;
  tile.cameraPanel.hidden = false;
  tile.mediaStatus.textContent = t("Connecting camera…");
  tile.mediaStatus.hidden = false;
  if (item.current && source === 'managed' && cameraSession) {
    tile.shared = true;
    updateSharedJobCamera();
    return;
  }
  const camera = new AbortController();
  tile.camera = camera;
  const failed = () => {
    if (tile.camera !== camera) return;
    stopJobCamera(tile);
    tile.failed = true;
    tile.retry.hidden = false;
  };
  const showFrame = () => {
    if (tile.camera !== camera) return;
    showJobCameraFrame(tile);
    clearTimeout(tile.cameraTimeout);
  };
  tile.cameraTimeout = setTimeout(failed, 20000);
  tile.stream.onload = source === 'managed' ? null : showFrame;
  tile.stream.onerror = source === 'managed' ? null : failed;
  if (source === 'managed') {
    readCameraFrames(jobEndpoint('/camera-stream', item.host), camera, (blob, url) => {
      tile.blob = blob;
      tile.stream.src = url;
      if (tile.frame) URL.revokeObjectURL(tile.frame);
      tile.frame = url;
      showFrame();
      tile.cameraTimeout = setTimeout(failed, 10000);
    }).catch(failed);
  } else tile.stream.src = source;
}

function clearJobTilePreview(tile) {
  tile.previewRequest?.abort();
  tile.previewRequest = null;
  tile.previewRetryAt = 0;
  tile.previewFailures = 0;
  const image = tile.node.querySelector('[data-preview]');
  image.hidden = true;
  image.removeAttribute('src');
  if (tile.previewUrl) URL.revokeObjectURL(tile.previewUrl);
  tile.previewUrl = '';
}

async function loadJobTilePreview(tile, item) {
  const filename = tile.active ? item.file : item.lastCompleted?.file || '';
  if (tile.previewFile !== filename) {
    clearJobTilePreview(tile);
    tile.previewFile = filename;
  }
  const image = tile.node.querySelector('[data-preview]');
  if (!filename || item.error || !image.hidden || tile.previewRequest || Date.now() < tile.previewRetryAt) return;
  const request = new AbortController();
  tile.previewRequest = request;
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(12000)]);
  let url = '';
  try {
    const response = await fetch(jobEndpoint('/files', item.host), {signal});
    if (!response.ok) throw Error('File catalog unavailable');
    const matches = (await response.json()).files.filter(file => file.name === filename);
    signal.throwIfAborted();
    if (matches.length === 0) throw Error('File not yet in catalog');
    if (matches.length > 1) {
      tile.previewRetryAt = Infinity;
      return;
    }
    const preview = await fetch(jobEndpoint('/preview/' + matches[0].id, item.host), {signal});
    signal.throwIfAborted();
    if (!preview.ok) throw Error('Preview unavailable');
    const blob = await preview.blob();
    signal.throwIfAborted();
    url = URL.createObjectURL(blob);
    image.src = url;
    await image.decode();
    signal.throwIfAborted();
    image.alt = t("Preview of {file}", {file:filename});
    image.hidden = false;
    tile.previewUrl = url;
    url = '';
    tile.previewFailures = 0;
  } catch {
    if (!request.signal.aborted) {
      image.removeAttribute('src');
      const delay = Math.min(30000, 1500 * 2 ** Math.min(tile.previewFailures, 5));
      tile.previewFailures++;
      tile.previewRetryAt = Date.now() + delay;
    }
  } finally {
    if (url) URL.revokeObjectURL(url);
    if (tile.previewRequest === request) tile.previewRequest = null;
  }
}

async function runJobTileAction(tile, action) {
  if (tile.pending || !tile.active || !tile.item.jobControls || tile.item.error || !tile.job) return;
  const buttons = tile.node.querySelectorAll('[data-print-action],[data-print-cancel]');
  const result = tile.node.querySelector('[data-job-result]');
  tile.pending = true;
  buttons.forEach(button => { button.disabled = true; });
  result.textContent = t("Checking printer…");
  const job = tile.job;
  try {
    const data = await postLocal('action', jobEndpoint('/action', tile.item.host), {action, job});
    if (tile.active && tile.job === job)
      result.textContent = t("Printer confirmed · {state}", {state:stateLabel(data.state)});
    await updateStatus();
  } catch (error) {
    if (tile.active && tile.job === job)
      result.textContent = error.message || t("Command result unavailable.");
  } finally {
    tile.pending = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function confirmBedClear(tile) {
  const item = tile.item;
  if (tile.confirmingBed || !waitingForPartRemoval(item)) return;
  const button = tile.node.querySelector('[data-clear-bed]');
  const status = tile.node.querySelector('[data-job-result]');
  tile.confirmingBed = true;
  button.disabled = true;
  status.textContent = t("Saving…");
  try {
    await postLocal('confirm-bed-clear', jobEndpoint('/confirm-bed-clear', item.host), {token:item.bedClearToken});
    confirmedBeds.set(item.host, item.bedClearToken);
    status.textContent = '';
    if (view === 'fleet') renderJobs();
  } catch (error) {
    status.textContent = error.message || t("Could not save the settings.");
  } finally {
    tile.confirmingBed = false;
    button.disabled = false;
  }
}

function createJobTile(item) {
  const node = el('jobTile').content.firstElementChild.cloneNode(true);
  const cameraPanel = node.querySelector('.jobs-camera');
  const stream = node.querySelector('[data-stream]');
  stream.alt = t("Live camera · {printer}", {printer:item.name || item.host});
  const mediaStatus = cameraPanel.querySelector('[role="status"]');
  const retry = node.querySelector('[data-retry]');
  const tile = {node, stream, cameraPanel, mediaStatus, retry, item};
  const monitor = node.querySelector('[data-monitor]');
  const filesLink = node.querySelector('[data-files]');
  node.querySelector('[data-clear-bed]').onclick = () => confirmBedClear(tile);
  for (const [link, showFiles] of [[monitor, false], [filesLink, true]]) {
    link.onclick = event => {
      if (!tile.item.current || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      switchTab('monitor');
      if (showFiles) showFleetFiles();
      else el('monitorTab').focus();
    };
  }
  node.querySelector('[data-print-action]').onclick = () => {
    runJobTileAction(tile, tile.item.state === 'PAUSED' ? 'resume' : 'pause');
  };
  const cancel = node.querySelector('[data-print-cancel]');
  cancel.onclick = () => {
    if (tile.cancelUntil > Date.now()) {
      tile.cancelUntil = 0;
      cancel.textContent = t("Cancel");
      runJobTileAction(tile, 'cancel');
    } else {
      const until = tile.cancelUntil = Date.now() + 5000;
      cancel.textContent = t("Cancel print?");
      setTimeout(() => {
        if (tile.cancelUntil === until) cancel.textContent = t("Cancel");
      }, 5000);
    }
  };
  node.querySelector('[data-camera]').onclick = () => openCameraViewer(stream, tile.item.name || tile.item.host);
  node.querySelector('[data-snapshot]').onclick = () => {
    if (tile.blob) saveCameraBlob(tile.blob);
  };
  retry.onclick = () => {
    tile.failed = false;
    retry.hidden = true;
    startJobCamera(tile, tile.item);
  };
  return tile;
}

function updateTileJob(tile, item) {
  const active = Boolean(item.printing);
  const file = active ? item.file : '';
  const job = active ? item.jobToken || '' : '';
  if (tile.active === active && tile.file === file && tile.job === job) return;
  const {node} = tile;
  tile.active = active;
  tile.file = file;
  tile.job = job;
  tile.cancelUntil = 0;
  tile.controlState = null;
  node.querySelector('details').open = false;
  node.querySelector('[data-job-result]').textContent = '';
  node.dataset.active = String(active);
  for (const selector of ['.row.jobs-meta', 'details'])
    node.querySelector(selector).hidden = !active;
}

function updateJobTile(tile, item) {
  updateTileJob(tile, item);
  tile.item = item;
  const {node} = tile;
  const set = (selector, value) => {
    const element = node.querySelector(selector);
    if (element.textContent !== value) element.textContent = value;
  };
  set('h3', item.name || item.host);
  node.dataset.group = fleetGroup(item);
  const waiting = waitingForPartRemoval(item);
  const ready = !item.error && isReadyForReprint(item.state, item.printing);
  showCompletionHint(node, item.complete && !item.error && !item.printing, ready);
  node.dataset.ready = String(ready && !waiting);
  node.querySelector('[data-clear-bed]').hidden = !waiting;
  node.querySelector('[data-monitor]').href = jobPage(item);
  startJobCamera(tile, item);
  if (!tile.source) tile.retry.hidden = true;
  loadJobTilePreview(tile, item);
  renderJobProgress(node, tile.active ? item.percent : null);
  renderLastCompleted(node, !tile.active && (!item.error || waiting) ? item.lastCompleted?.file : '');
  if (!tile.active) {
    const warning = item.error || ['ERROR','ATTENTION'].includes(item.state);
    const waitingLabel = waiting ? t("Waiting for part removal") : '';
    set('.jobs-state', item.error ? t("Disconnected") : (!warning && waitingLabel) || stateLabel(item.status)
      || (item.observedAt ? t("Connected") : t("Connecting…")));
    set('.jobs-file', waiting ? (warning ? waitingLabel : '')
      : item.error ? t("Job status unavailable") : item.observedAt ? t("No active print job") : t("Waiting for job status"));
    const completed = item.lastCompleted?.file;
    node.querySelector('[data-print-action]').hidden = true;
    node.querySelector('[data-print-cancel]').hidden = true;
    const filesLink = node.querySelector('[data-files]');
    filesLink.hidden = !ready;
    if (ready) filesLink.href = jobPage(item, {hash:'files-section'});
    const repeat = node.querySelector('[data-repeat]');
    repeat.hidden = !ready || waiting || !completed;
    if (!repeat.hidden) repeat.href = jobPage(item, {reprint:completed});
    return;
  }
  for (const selector of ['[data-files]', '[data-repeat]']) node.querySelector(selector).hidden = true;
  set('.jobs-state', item.error ? t("Disconnected") : stateLabel(item.status) || t("Printing"));
  set('.jobs-file', item.file || t("Active print"));
  const canControl = Boolean(item.jobControls && !item.error && tile.job && item.state !== 'PAUSING');
  node.querySelector('[data-print-action]').hidden = !canControl;
  node.querySelector('[data-print-action]').dataset.icon = item.state === 'PAUSED' ? 'play' : 'pause';
  node.querySelector('[data-print-cancel]').hidden = !canControl;
  set('[data-print-action]', item.state === 'PAUSED' ? t("Resume") : t("Pause"));
  if (!canControl || tile.controlState !== item.state) {
    tile.cancelUntil = 0;
    set('[data-print-cancel]', t("Cancel"));
  }
  tile.controlState = item.state;
  set('[data-layer]', jobLayerLabel(item.layer, item.layerTotal));
  const timed = !item.error && !['PAUSED', 'PAUSING'].includes(item.state);
  const finish = timed ? finishAt(item.remaining, item.observedAt) : '';
  set('[data-eta]', timed ? jobTimeLeft(item.remaining) : '');
  const temp = (value, target) => value == null ? '' : plain(value) + (target == null ? '' : ' / ' + plain(target)) + ' °C';
  const tools = item.tools?.length > 1 ? item.tools.map((tool,i) => ['T' + (i + 1),temp(tool.actual,tool.target)]) : [];
  const metrics = [...(tools.length ? tools : [[t("Nozzle"),temp(item.nozzle,item.nozzleTarget)]]),
    [t("Bed"),temp(item.bed,item.bedTarget)], [t("Print speed"),item.speed == null ? '' : item.speed + '%'],
    [t("Part fan"),item.fan == null ? '' : item.fan + '%'], [t("Elapsed"),item.elapsed == null ? '' : duration(item.elapsed)],
    [t("Finish"),finish], [t("Estimated material"),item.filament == null ? '' : weightFormat.format(item.filament)]].filter(([,value]) => value);
  const events = item.events || [];
  const details = node.querySelector('details');
  details.hidden = !metrics.length && !events.length;
  const signature = JSON.stringify([metrics,events]);
  if (tile.details !== signature) {
    tile.details = signature;
    node.querySelector('dl').replaceChildren(...metrics.map(([label,value]) => {
      const group = document.createElement('div'), dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = label; dd.textContent = value; group.append(dt,dd); return group;
    }));
    node.querySelector('ol').replaceChildren(...events.slice().reverse().map(([stamp,label]) => {
      const entry = document.createElement('li');
      entry.textContent = shortTime.format(stamp * 1000) + ' · ' + t(label);
      return entry;
    }));
  }
}

function renderJobs() {
  const list = el('jobsList');
  const query = fleetSearch.value.trim().toLowerCase();
  const matches = fleetPrinters.filter(item => matchesFleetSearch(item, query));
  const counts = {all:matches.length, active:0, idle:0, attention:0, other:0};
  for (const item of matches) counts[fleetGroup(item)]++;
  for (const button of fleetFilters) {
    const filter = button.dataset.fleetFilter;
    button.setAttribute('aria-pressed', String(fleetFilter === filter));
    const count = button.querySelector('[data-count]');
    if (count.textContent !== String(counts[filter])) count.textContent = counts[filter];
  }
  const visible = fleetFilter === 'all' ? matches : matches.filter(item => fleetGroup(item) === fleetFilter);
  const byHost = new Map(visible.map(item => [item.host,item]));
  for (const [host,tile] of jobTiles) {
    const item = byHost.get(host);
    if (item) continue;
    stopJobCamera(tile);
    clearJobTilePreview(tile);
    tile.node.remove();
    jobTiles.delete(host);
  }
  let previous = null;
  for (const item of visible) {
    let tile = jobTiles.get(item.host);
    if (!tile) {
      tile = createJobTile(item);
      jobTiles.set(item.host,tile);
    }
    if (tile.node.parentElement !== list || tile.node.previousElementSibling !== previous) {
      list.insertBefore(tile.node, previous ? previous.nextElementSibling : list.firstElementChild);
    }
    updateJobTile(tile,item);
    previous = tile.node;
  }
  list.hidden = !visible.length;
  const notice = el('jobsNotice');
  notice.hidden = visible.length > 0;
  if (!notice.hidden) {
    const message = query ? t("No printers match your search and filters.")
      : ({all:t("No printers configured."), active:t("No printers are printing."), idle:t("No idle printers."),
          attention:t("No printers need attention.")})[fleetFilter];
    const status = notice.querySelector('[role="status"]');
    if (status.textContent !== message) status.textContent = message;
  }
}

function currentPrinterName() {
  const printer = fleetPrinters.find(item => item.current);
  return printer?.name || printer?.host || t("Printer");
}

function applyView() {
  const hasPrinters = fleetPrinters.length > 0;
  const showFleet = hasPrinters && view === 'fleet';
  const showMonitor = hasPrinters && view === 'monitor';
  const showHistory = view === 'history';
  updateJobLink();
  const current = fleetPrinters.find(item => item.current);
  el('status').hidden = !showMonitor;
  el('monitorHistory').hidden = !showMonitor || !current;
  el('jobs').hidden = !showFleet;
  el('setup').hidden = !setupTab && (hasPrinters || showHistory);
  el('history').hidden = !showHistory;
  if (showHistory) el('history').open = true;
  for (const id of ['monitor','printerTools','files-section']) el(id).hidden = !showMonitor;
  if (showFleet) renderJobs();
  else stopJobCameras();
  syncCameraView();
  if (showMonitor && openFleetFiles) {
    openFleetFiles = false;
    showFleetFiles();
  }
  for (const [id,key] of [['monitorTab','monitor'],['fleetToggle','fleet'],['historyTab','history']])
    el(id).setAttribute('aria-current',view === key ? 'page' : 'false');
  const picker=el('devicePicker');
  const options = JSON.stringify(fleetPrinters.map(p=>[p.name,p.host,p.current]));
  if (picker.dataset.options !== options) {
    picker.replaceChildren(...fleetPrinters.map(p=>new Option(p.name||p.host,p.host,false,p.current)));
    picker.dataset.options = options;
  }
  picker.hidden = !showMonitor || fleetPrinters.length < 2;
}
function replaceViewAddress() {
  const url = new URL(location.href);
  url.hash = view === 'fleet' ? 'allPrinters' : view;
  if (view === 'history' && historyFilter) url.searchParams.set('historyPrinter', historyFilter);
  else url.searchParams.delete('historyPrinter');
  if (url.href !== location.href) window.history.replaceState(null, '', url);
}
function switchTab(next) {
  view = next;
  replaceViewAddress();
  try {localStorage.setItem('ff-fleet',next === 'fleet' ? '1':'0');} catch {}
  applyView();drawChart();
  if (next === 'history') {
    renderHistory();
    updateHistory();
  }
}
el('devicePicker').onchange = event => {location.href = printerPage(event.target.value);};
el('monitorTab').onclick = () => switchTab('monitor');
el('historyTab').onclick = () => {
  historyFilter = '';
  switchTab('history');
};
el('monitorHistory').onclick = () => {
  historyFilter = fleetPrinters.find(printer => printer.current)?.host || '';
  el('historySearch').value = '';
  el('failedJobs').checked = false;
  switchTab('history');
  el('historyPrinter').focus();
};
el('openJob').onclick = () => {
  const current = fleetPrinters.find(item => item.current);
  if (current && (!matchesFleetSearch(current, fleetSearch.value.trim().toLowerCase())
    || (fleetFilter !== 'all' && fleetGroup(current) !== fleetFilter))) clearFleetFilters();
  switchTab('fleet');
  const heading = jobTiles.get(current?.host)?.node.querySelector('h3');
  (heading ?? fleetToggle).focus();
};
el('job').querySelector('.jobs-model').onclick = () => {
  if (!el('openJob').hidden) el('openJob').click();
};
fleetToggle.onclick = () => switchTab('fleet');
function refreshFleetView() {
  renderJobs();
  syncCameraView();
}
fleetSearch.oninput = refreshFleetView;
for (const button of fleetFilters) {
  button.onclick = () => {
    fleetFilter = button.dataset.fleetFilter;
    refreshFleetView();
  };
}
el('fleetClear').onclick = () => {
  clearFleetFilters();
  refreshFleetView();
  fleetSearch.focus();
};
function setFleetCameraMode(mode) {
  const enabled = mode !== 'off';
  const scope = enabled ? mode : fleetCameraScope;
  if (fleetCameras === enabled && fleetCameraScope === scope) return;
  fleetCameras = enabled;
  fleetCameraScope = scope;
  fleetCameraButton.setAttribute('aria-pressed', String(fleetCameras));
  try {
    localStorage.setItem('ff-fleet-cameras', fleetCameras ? 'on' : 'off');
    localStorage.setItem('ff-fleet-camera-scope', fleetCameraScope);
  } catch {}
  if (!el('jobs').hidden) refreshFleetView();
}
fleetCameraButton.onclick = () => setFleetCameraMode(fleetCameras ? 'off' : fleetCameraScope);
el('fleetRefresh').onclick = async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {await updateTools();}
  finally {button.disabled = false;}
};
el('pausePrint').onclick = () => runAction('pause');
el('resumePrint').onclick = () => runAction('resume');
el('cancelPrint').onclick = () => {
  const button = el('cancelPrint');
  if (!observedJob) return;
  if (!button.classList.contains('armed')) {
    button.classList.add('armed');
    button.dataset.job = observedJob;
    button.textContent = t("Cancel print?");
    const armedJob = observedJob;
    setTimeout(() => {
      if (button.classList.contains('armed') && button.dataset.job === armedJob) {
        button.classList.remove('armed');
        button.textContent = t("Cancel");
      }
    }, 5000);
    return;
  }
  button.classList.remove('armed');
  button.textContent = t("Cancel");
  runAction('cancel', button.dataset.job);
};
el('cameraSnapshot').onclick = downloadSnapshot;

function printerRow(entry = {}, wizard = false) {
  const row = document.createElement('fieldset');
  row.className = 'printer-fields';
  const fields = {name:t("Name"),host:t("IPv4 address"),serial:t("Serial number"),code:'CheckCode'};
  if (wizard) fields.name = t("Name (optional)");
  for (const key of wizard ? ['host','code','serial','name'] : Object.keys(fields)) {
    const label = document.createElement('label'), input = document.createElement('input');
    label.textContent = fields[key]; input.dataset.key = key; input.value = entry[key] || '';
    input.type = key === 'code' ? 'password' : 'text';input.autocomplete = 'off';
    if (key === 'host') input.required = true;
    label.append(input);row.append(label);
  }
  if (wizard) {
    const hint = document.createElement('p');
    hint.className = 'caption';
    hint.textContent = t("Find the IP address and CheckCode in the printer’s network settings, and the serial number in its device information or on its label.")
      + ' ' + t("For Creator 5 and 5 Pro, enable LAN-only mode and enter both CheckCode and serial number.");
    row.append(hint);
  }
  const check=document.createElement('button'), remove=document.createElement('button'), status=document.createElement('p');
  check.type=remove.type='button';check.textContent=t("Check connection");remove.textContent=t("Remove");
  check.dataset.icon='find';remove.dataset.icon='remove';
  status.className='caption printer-check-status';
  status.setAttribute('role','status');
  let revision = 0;
  row.oninput = () => { revision++; status.textContent = ''; };
  check.onclick = async () => {
    const attempt = ++revision;
    const current = () => row.isConnected && row.closest('dialog')?.open && revision === attempt;
    const values = printerValues(row);
    check.disabled = true;
    status.dataset.result = '';
    status.textContent = t("Checking connection…");
    try {
      const data = await postLocal('test-printer', undefined, values);
      if (current()) {
        status.dataset.result = 'success';
        status.textContent = t("Connected · {transport}", {transport:data.transport});
      }
    } catch (error) {
      if (current()) {
        status.dataset.result = 'error';
        status.textContent = error.message;
      }
    }
    finally { check.disabled = false; }
  };
  remove.onclick=()=>row.remove();
  row.append(check);
  if (!wizard) row.append(remove);
  row.append(status);
  return row;
}
function printerValues(row) {
  return Object.fromEntries(Array.from(row.querySelectorAll('input'), input=>[input.dataset.key,input.value.trim()]));
}
function fieldPrinters() {
  return Array.from(el('printerFields').children, printerValues);
}
function setRows(entries) {el('printerFields').replaceChildren(...entries.map(entry => printerRow(entry)));}
el('addPrinter').onclick=()=>el('printerFields').append(printerRow());
async function printerCandidates(list, status, current, choose, exclude = () => []) {
  list.replaceChildren();
  status.textContent = t("Searching…");
  try {
    const {printers} = await postLocal('discover');
    if (!current()) return;
    const configured = new Set(exclude());
    const found = printers.filter(printer => !configured.has(printer.host));
    status.textContent = found.length ? t("Select a printer to add.") : t("No new printers found.");
    list.replaceChildren(...found.map(printer => {
      const add = document.createElement('button');
      add.type = 'button';
      add.dataset.icon = 'plus';
      add.textContent = printer.name + ' · ' + printer.host;
      add.onclick = () => { choose(printer); add.remove(); status.textContent = ''; };
      return add;
    }));
  } catch (error) { if (current()) status.textContent = error.message; }
}
let settingsRevision = 0;
el('findPrinters').onclick = async () => {
  const revision = settingsRevision;
  const button = el('findPrinters');
  const current = () => settingsDialog.open && revision === settingsRevision && !el('bulk').open;
  button.disabled = true;
  try {
    await printerCandidates(el('discoveredPrinters'), el('settingsStatus'), current, printer => {
      if (fieldPrinters().some(entry => entry.host === printer.host)) return;
      const row = printerRow(printer);
      const empty = [...el('printerFields').children].find(fieldset => Object.values(printerValues(fieldset)).every(value => !value));
      if (empty) empty.replaceWith(row);
      else el('printerFields').append(row);
      row.querySelector('[data-key="code"]').focus();
    }, () => fieldPrinters().map(printer => printer.host));
  } finally {
    if (revision === settingsRevision) button.disabled = false;
  }
};

let placementState = {active:'device',saved:'device'};
function readPlacement(data) {
  if (!['device','flashforge'].includes(data.placement)) return;
  placementState = {active:data.placement,saved:data.savedPlacement || data.placement};
  const notice = el('placementNotice');
  notice.hidden = placementState.saved === placementState.active;
  notice.textContent = notice.hidden ? '' : t("Saved. Restart OrcaSlicer to apply the change.");
}
function placementHint(select, hint) {
  hint.hidden = select.value === placementState.active;
  hint.textContent = hint.hidden ? '' : select.value === placementState.saved
    ? t("Saved. Restart OrcaSlicer to apply the change.")
    : t("Save this setting, then restart OrcaSlicer to switch tabs.");
}
el('displayPlacement').onchange = () => placementHint(el('displayPlacement'), el('displayPlacementHint'));
el('setupPlacement').onchange = () => placementHint(el('setupPlacement'), el('setupPlacementHint'));

const setupDialog = el('setupDialog');
const setupFields = el('setupPrinterFields');
const setupStatus = el('setupStatus');
const setupSeenKey = 'ff-first-printer-setup-seen';
let setupRevision = 0;
let savingSetup = false;
function setupStep(printer) {
  el('setupPlacementStep').hidden = printer;
  el('setupPrinterStep').hidden = !printer;
  el('setupNext').hidden = printer;
  el('setupBack').hidden = !printer;
  el('setupSave').hidden = !printer || setupFields.hidden;
  setupStatus.textContent = '';
  if (printer) (setupFields.hidden ? el('setupFind') : setupFields.querySelector('[data-key="host"]')).focus();
  else el('setupPlacement').focus();
}
function openSetup(data) {
  if (setupDialog.open || settingsDialog.open || data.printers.length) return;
  setupRevision++;
  readPlacement(data);
  el('setupPlacement').value = placementState.saved;
  placementHint(el('setupPlacement'), el('setupPlacementHint'));
  setupFields.replaceChildren(printerRow({host:data.setupHost || ''}, true));
  setupFields.hidden = !data.setupHost;
  el('setupCandidates').replaceChildren();
  el('setupFind').disabled = false;
  setupDialog.showModal();
  setupStep(false);
  try { localStorage.setItem(setupSeenKey, '1'); } catch {}
}
async function startSetup(manual = false) {
  try {
    const data = await getJson('/settings');
    readPlacement(data);
    if (data.printers.length) {
      el('openSetup').hidden = true;
      if (setupTab && placementState.active === 'device' && placementState.saved === 'device') {
        el('setupOutcome').hidden = false;
        el('setupOutcome').textContent = t("Select a printer profile that uses the FlashForge host.");
      }
      return;
    }
    if (manual || new URL(location.href).searchParams.has('wizard') || !localStorage.getItem(setupSeenKey)) openSetup(data);
  } catch (error) {
    if (manual) {
      el('setupOutcome').hidden = false;
      el('setupOutcome').textContent = error.message || t("Could not read the settings.");
    }
  }
}
el('openSetup').onclick = () => startSetup(true);
el('setupCancel').onclick = () => setupDialog.close();
setupDialog.addEventListener('cancel', event => { if (savingSetup) event.preventDefault(); });
el('setupNext').onclick = () => setupStep(true);
el('setupBack').onclick = () => setupStep(false);
el('setupManual').onclick = () => {
  setupFields.hidden = false;
  el('setupSave').hidden = false;
  setupFields.querySelector('[data-key="host"]').focus();
};
el('setupFind').onclick = async () => {
  const revision = setupRevision;
  const button = el('setupFind');
  const current = () => setupDialog.open && revision === setupRevision && !savingSetup;
  button.disabled = true;
  try {
    await printerCandidates(el('setupCandidates'), setupStatus, current, printer => {
      setupFields.replaceChildren(printerRow(printer, true));
      setupFields.hidden = false;
      el('setupSave').hidden = false;
      setupFields.querySelector('[data-key="code"]').focus();
    });
  } finally {
    if (revision === setupRevision) button.disabled = false;
  }
};
el('setupForm').onsubmit = async event => {
  event.preventDefault();
  if (!setupDialog.open || savingSetup || el('setupSave').hidden) return;
  const host = setupFields.querySelector('[data-key="host"]');
  if (!host.value.trim()) { host.focus(); host.reportValidity(); return; }
  savingSetup = true;
  const form = el('setupForm');
  form.inert = true;
  setupStatus.textContent = t("Saving…");
  try {
    const data = await postLocal('settings', endpoint('/settings'), {
      printers:[printerValues(setupFields)], placement:el('setupPlacement').value, first:true
    });
    readPlacement(data);
    setupDialog.close();
    if (setupTab && !new URL(location.href).searchParams.has('wizard') && data.setupHost === data.endpoint) {
      location.replace('/' + encodeURIComponent(data.endpoint) + '/');
    } else if (setupTab) {
      await startSetup();
    } else {
      await updateTools();
      await updateStatus();
    }
  } catch (error) {
    setupStatus.textContent = error.message || t("Could not save the settings.");
  } finally {
    form.inert = false;
    savingSetup = false;
  }
};

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
  el('findPrinters').hidden=open;el('discoveredPrinters').hidden=open;
  for(const field of el('printerFields').querySelectorAll('input')) field.disabled=open;
};
const settingsTabs = [...el('settingsSections').querySelectorAll('button')];
let printerSettingsSnapshot = null;
let savingSettings = false;
function updateSettingsSave() {
  el('settingsSave').disabled = savingSettings || (printerSettingsSnapshot === null && !el('settingsPrinters').hidden);
}
function showSettingsSection(section) {
  for (const button of settingsTabs) {
    const id = button.getAttribute('aria-controls');
    button.setAttribute('aria-pressed', String(id === section));
    el(id).hidden = id !== section;
  }
  el('settingsContent').scrollTop = 0;
  updateSettingsSave();
}
for (const button of settingsTabs) {
  button.onclick = () => showSettingsSection(button.getAttribute('aria-controls'));
}
el('settingsButton').onclick = async () => {
  const revision = ++settingsRevision;
  printerSettingsSnapshot = null;
  el('settingsStatus').textContent = '';
  el('settingsSaveStatus').textContent = '';
  el('findPrinters').disabled = true;
  el('addPrinter').disabled = true;
  el('displayPlacement').disabled = true;
  el('displayPlacement').value = placementState.saved;
  placementHint(el('displayPlacement'), el('displayPlacementHint'));
  el('discoveredPrinters').replaceChildren();
  el('printerFields').replaceChildren();
  el('printersText').value = '';
  el('displayTheme').value = display.theme;
  el('displayDensity').value = display.density;
  el('displayCardSize').value = display.cardSize;
  el('displayRows').value = display.rows;
  el('displayPreviews').checked = display.previews;
  el('displayTallJobCard').checked = display.tallJobCard;
  el('displayMonitorChart').checked = display.monitorChart;
  el('displayChartLayout').value = display.chartLayout;
  el('displayChartRange').value = String(display.chartRange);
  for (const choice of chartChoices) choice.checked = display.chartLines.includes(choice.dataset.chartChoice);
  el('displayPrinterDetails').checked = display.printerDetails;
  el('displayFleetCameras').value = fleetCameras ? fleetCameraScope : 'off';
  el('displayFleetDetails').checked = display.fleetDetails;
  el('displayFleetWaitForRemoval').checked = display.fleetWaitForRemoval;
  el('displayHistoryTotals').checked = display.historyTotals;
  showSettingsSection(fleetPrinters.length ? 'settingsAppearance' : 'settingsPrinters');
  settingsDialog.showModal();
  try {
    const data = await getJson('/settings');
    if (!settingsDialog.open || revision !== settingsRevision) return;
    readPlacement(data);
    el('displayPlacement').value = placementState.saved;
    el('displayPlacement').disabled = !data.savedPlacement;
    placementHint(el('displayPlacement'), el('displayPlacementHint'));
    printerSettingsSnapshot = formatPrinters(data.printers);
    el('printersText').value = printerSettingsSnapshot;
    setRows(data.printers.length ? data.printers : [{}]);
    if (el('bulk').open) for (const field of el('printerFields').querySelectorAll('input')) field.disabled = true;
    el('findPrinters').disabled = false;
    el('addPrinter').disabled = false;
    updateSettingsSave();
  } catch (error) {
    if (settingsDialog.open && revision === settingsRevision)
      el('settingsStatus').textContent = error.message || t("Could not read the settings.");
  }
};
el('settingsCancel').onclick = () => settingsDialog.close();
settingsDialog.addEventListener('cancel', event => {
  if (savingSettings) event.preventDefault();
});
el('settingsForm').onsubmit = async event => {
  event.preventDefault();
  if (el('settingsSave').disabled) return;
  const status = el('settingsSaveStatus');
  savingSettings = true;
  updateSettingsSave();
  el('settingsCancel').disabled = true;
  el('settingsContent').inert = true;
  status.textContent = t("Saving…");
  try {
    const chartLines = chartChoices.filter(choice => choice.checked).map(choice => choice.dataset.chartChoice);
    if (!chartLines.length) {
      showSettingsSection('settingsMonitor');
      throw Error(t("Select at least one temperature line."));
    }
    let printers, data;
    if (printerSettingsSnapshot !== null) {
      const payload = {};
      try {
        printers = el('bulk').open ? parsePrinters(el('printersText').value)
          : fieldPrinters().filter(printer => Object.values(printer).some(Boolean));
        if (formatPrinters(printers) !== printerSettingsSnapshot) payload.printers = printers;
        const placement = el('displayPlacement');
        if (!placement.disabled && placement.value !== placementState.saved) payload.placement = placement.value;
        if (Object.keys(payload).length) data = await postLocal('settings', endpoint('/settings'), payload);
        if (data) readPlacement(data);
        if (payload.printers) printerSettingsSnapshot = formatPrinters(printers);
      } catch (error) {
        showSettingsSection(payload.placement && !payload.printers ? 'settingsMonitor' : 'settingsPrinters');
        throw error;
      }
    }
    const next = {
      theme:el('displayTheme').value, density:el('displayDensity').value,
      cardSize:el('displayCardSize').value, rows:Number(el('displayRows').value),
      previews:el('displayPreviews').checked,
      tallJobCard:el('displayTallJobCard').checked,
      monitorChart:el('displayMonitorChart').checked,
      chartLayout:el('displayChartLayout').value,
      chartRange:Number(el('displayChartRange').value),
      chartLines,
      printerDetails:el('displayPrinterDetails').checked,
      fleetDetails:el('displayFleetDetails').checked,
      fleetWaitForRemoval:el('displayFleetWaitForRemoval').checked,
      historyTotals:el('displayHistoryTotals').checked
    };
    if (JSON.stringify(next) !== JSON.stringify(display)) {
      localStorage.setItem(displayKey, JSON.stringify(next));
      display = next;
      applyDisplayStyles();
      renderFiles();
      chartRange.value = String(display.chartRange);
      redrawTemperatureChart();
      if (view === 'fleet') renderJobs();
    }
    setFleetCameraMode(el('displayFleetCameras').value);
    settingsDialog.close();
    if (!data) return;
    if (setupTab) {
      if (data.setupHost && !new URL(location.href).searchParams.has('wizard')) {
        location.replace('/' + encodeURIComponent(data.setupHost) + '/');
      } else await startSetup(false);
    } else if (!separateTab && data.moved && !printers.some(p => location.pathname === '/' + p.host + '/')) {
      location.assign('/' + encodeURIComponent(data.endpoint) + '/');
    } else if (selectedPrinter && !printers.some(p => p.host === selectedPrinter)) {
      location.assign(location.pathname);
    } else await updateStatus();
  } catch (error) {
    status.textContent = error.message || t("Could not save the settings.");
  } finally {
    savingSettings = false;
    el('settingsCancel').disabled = false;
    el('settingsContent').inert = false;
    updateSettingsSave();
  }
};
applyView();
startSetup();
let nextPanelPoll = 0;
setInterval(() => {
  const now = performance.now();
  if (Date.now() < fastMotionPollUntil || manual?.checkingPosition || manual?.releaseHoming || now >= nextPanelPoll) {
    nextPanelPoll = now + 5000;
    updateStatus();
  }
}, 1000);
</script>
</body>
</html>'''

CONFIG_PAGE = r'''<!doctype html>
<html lang="{{language}}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {color-scheme:light dark;--surface:light-dark(#f5f5ef,#242c27);--control:light-dark(#fffefa,#29322d);
  --line:light-dark(#d7dcd1,#465449);--muted:light-dark(#5b665c,#bcc8bc);--accent:light-dark(#39745c,#98d6b1)}
body {font:14px "Segoe UI",sans-serif;color:var(--orca-fg,CanvasText);margin:12px}
main {max-width:520px;display:grid;gap:10px}
.config-group {display:grid;gap:6px;padding:10px;border:1px solid var(--line);border-radius:7px;background:var(--surface)}
h2 {font-size:14px;margin:0 0 3px}
input,select,button {box-sizing:border-box;font:inherit;color:inherit;background:var(--control);
  border:1px solid var(--line);border-radius:5px;padding:6px 8px}
input,select {width:100%;min-width:0}
#connectionConfig {grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
#connectionConfig h2,#connectionConfig p {grid-column:1/-1}
#connectionConfig label {display:grid;align-content:start;gap:4px}
#connectionConfig label:last-of-type {grid-column:1/-1}
p {margin:0;color:var(--muted);line-height:1.4}
#placementHelp {border-inline-start:3px solid var(--accent);padding-inline-start:8px}
.config-actions {display:flex;align-items:center;gap:8px}
.config-icon {width:1.1em;height:1.1em;margin-inline-end:.35em;vertical-align:-.2em;fill:none;stroke:currentColor;
  stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
h2 .config-icon {color:var(--accent)}
button {cursor:pointer}
button:disabled {opacity:.55;cursor:default}
:focus-visible {outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:320px) {#connectionConfig {grid-template-columns:minmax(0,1fr)}}
</style>
<main>
  <section class="config-group" aria-labelledby="placementTitle">
    <h2 id="placementTitle"><svg class="config-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 5h18v14H3z M3 10h18 M9 5v5 M15 5v5"/></svg>{{t:Tab placement}}</h2>
    <select id="tabPlacement" aria-labelledby="placementTitle" aria-describedby="placementHelp"><option value="device">{{t:Replace Printer tab}}</option><option value="flashforge">FlashForge</option></select>
    <p id="placementHelp" role="status" hidden></p>
  </section>
  <section class="config-group">
    <label for="interfaceLanguage">{{t:Interface language}}</label>
    <select id="interfaceLanguage" aria-describedby="languageHelp">
      <option value="">{{t:Follow OrcaSlicer}}</option>
      {{languageOptions}}
    </select>
    <p id="languageHelp" hidden>{{t:The new language appears when you reopen the monitor.}}</p>
  </section>
  <section class="config-group" id="connectionConfig" aria-labelledby="connectionTitle">
    <h2 id="connectionTitle"><svg class="config-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M10 7l2-2a5 5 0 0 1 7 7l-2 2 M14 17l-2 2a5 5 0 0 1-7-7l2-2 M9 15l6-6"/></svg>{{t:Printer connection}}</h2>
    <label>{{t:Printer IPv4 address}}
      <input type="text" id="flashforgeHost" placeholder="192.168.1.51" maxlength="15" autocomplete="off"></label>
    <label>CheckCode <input type="password" id="code" maxlength="32" autocomplete="off"></label>
    <label>{{t:Serial number (required for Creator 5 / 5 Pro)}}
      <input type="text" id="serial" maxlength="64" autocomplete="off"></label>
    <p>{{t:Allow OrcaSlicer to access your printer when prompted, then reload the plugin.}}</p>
  </section>
  <div class="config-actions">
    <button id="save" type="button" disabled><svg class="config-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m5 12 4 4L19 6"/></svg>{{t:Save}}</button>
    <span id="saved" role="status"></span>
  </div>
</main>
<script id="plugin-translations" type="application/json">{{translations}}</script>
<script>
{{translationScript}}
const byId = id => document.getElementById(id);
const fields = {flashforge_host: byId('flashforgeHost'), check_code: byId('code'), serial_number: byId('serial')};
const placement = byId('tabPlacement');
const language = byId('interfaceLanguage');
const save = byId('save');
const saved = byId('saved');
const activePlacement = '{{placement}}';
let printers = [];
let storedPlacement = activePlacement;
function updatePlacementNotice() {
  const notice = byId('placementHelp');
  notice.hidden = placement.value === activePlacement;
  notice.textContent = notice.hidden ? '' : placement.value === storedPlacement
    ? t("Saved. Restart OrcaSlicer to apply the change.")
    : t("Save this setting, then restart OrcaSlicer to switch tabs.");
}
placement.onchange = updatePlacementNotice;
window.orca.onConfig(config => {
  printers = Array.isArray(config.printers) ? config.printers : [];
  placement.value = ['device','flashforge'].includes(config.placement) ? config.placement : activePlacement;
  language.value = [...language.options].some(option => option.value === config.language) ? config.language : '';
  const first = printers[0];
  fields.flashforge_host.value = first?.host || config.flashforge_host || '';
  fields.check_code.value = first?.code ?? config.check_code ?? '';
  fields.serial_number.value = first?.serial ?? config.serial_number ?? '';
  storedPlacement = placement.value;
  updatePlacementNotice();
  saved.textContent = '';
  save.disabled = false;
});
for (const field of [placement, language, ...Object.values(fields)]) field.oninput = () => { saved.textContent = ''; };
language.onchange = () => { byId('languageHelp').hidden = false; };
save.onclick = () => {
  const config = Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value.trim()]));
  config.placement = placement.value;
  config.language = language.value;
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
    "CALIBRATING": "Calibrating",
    "PRINTING": "Printing", "BUILDING": "Printing", "BUILDING_FROM_SD": "Printing",
    "PAUSED": "Paused", "ERROR": "Error",
    "COMPLETED": "Completed", "BUILDING_COMPLETED": "Completed",
    "HEATING": "Heating", "PAUSING": "Pausing",
    "CANCELING": "Canceling", "CANCELLING": "Canceling",
    "CANCELLED": "Canceled", "CANCELED": "Canceled",
    "FINISHED": "Completed", "STOPPED": "Canceled", "ATTENTION": "Needs Attention",
}

ERROR_TEMPLATES = (
    "The printer HTTP API returned status {code}.",
    "The printer rejected the HTTP request (code {code}).",
    "{axis} move exceeds the axis travel limits ({minimum} to {maximum} mm).",
    "Printer rejected {command}.",
    "Select slots for these file filaments: {numbers}",
    "Filament {number} requires {material}.",
    "The current job changed before {command} could be confirmed.",
    "Printer did not confirm {command}. Last reported state: {state}.",
    "Printer must be idle. {reason}",
)

ERROR_PATTERNS = tuple(
    (re.compile(re.sub(r"\\\{([a-z]+)\\\}", r"(?P<\1>.+?)", re.escape(template))), template)
    for template in ERROR_TEMPLATES
)


def translate_error(language, message):
    catalog = TRANSLATIONS.get(language, {})
    if not catalog or not isinstance(message, str):
        return message
    if message in catalog:
        return catalog[message]
    for pattern, template in ERROR_PATTERNS:
        match = pattern.fullmatch(message)
        if match:
            fields = match.groupdict()
            for key in ("command", "state", "reason"):
                if key in fields:
                    value = fields[key]
                    label = STATES.get(value, value.capitalize() if value in ("pause", "resume", "cancel", "unavailable") else value)
                    fields[key] = catalog.get(label, value)
            return catalog.get(template, template).format(**fields)
    return message


ACTIVE_JOB = {"PRINTING", "BUILDING", "BUILDING_FROM_SD", "PAUSED", "HEATING", "PAUSING"}
COMPLETED_JOB = {"COMPLETED", "BUILDING_COMPLETED", "FINISHED"}
CANCELLED_JOB = {"CANCELLED", "CANCELED", "STOPPED"}
ENDED_JOB = {**dict.fromkeys(CANCELLED_JOB, "cancelled"), "ERROR": "error"}
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


def last_completed_job(jobs):
    completed = next((job for job in jobs if job["result"] == "complete" and job["file"]), None)
    if completed:
        return {"file": completed["file"], "ended": completed.get("ended", 0)}
    return None


def bed_clear_token(jobs, active):
    if active or not jobs or jobs[0]["result"] != "complete":
        return ""
    job = jobs[0]
    ended = job.get("ended")
    if type(ended) is not int or ended < job["started"]:
        return ""
    identity = json.dumps([job["started"], ended, job["file"]]).encode("ascii")
    return hashlib.sha256(identity).hexdigest()


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

    return checked_printer_response(result)


def checked_printer_response(result):
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


def upload_flashforge_file(host, serial, check_code, filename, stream, length, check_available):
    boundary = secrets.token_hex(24)
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="gcodeFile"; filename="{filename}"\r\n'
              'Content-Type: application/octet-stream\r\n\r\n').encode("ascii")
    suffix = f'\r\n--{boundary}--\r\n'.encode("ascii")

    def chunks():
        yield prefix
        while chunk := stream.read(1024 * 1024):
            check_available()
            yield chunk
        check_available()
        yield suffix

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(prefix) + length + len(suffix)),
        "serialNumber": serial, "checkCode": check_code, "fileSize": str(length),
        "printNow": "false", "levelingBeforePrint": "false", "flowCalibration": "false",
        "firstLayerInspection": "false", "timeLapseVideo": "false", "useMatlStation": "false",
        "gcodeToolCnt": "0",
    }
    connection = http.client.HTTPConnection(host, API_PORT, timeout=30)
    try:
        check_available()
        connection.request("POST", "/uploadGcode", chunks(), headers)
        response = connection.getresponse()
        if response.status != 200:
            raise PrinterAPIError(f"The printer HTTP API returned status {response.status}.")
        body = response.read(65537)
        if len(body) > 65536:
            raise PrinterAPIError("The printer HTTP response is too large.")
        try:
            result = json.loads(body)
        except (ValueError, RecursionError) as error:
            raise PrinterAPIError("The printer returned invalid HTTP data.") from error
        checked_printer_response(result)
    finally:
        connection.close()


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


def chamber_light(detail):
    value = detail.get("lightStatus")
    return value == "open" if value in ("open", "close") else None


def printer_pid(value):
    if isinstance(value, str) and re.fullmatch(r"(?:0x)?[0-9a-f]{1,4}", value.strip().lower()):
        return int(value, 16)
    return value if type(value) is int else None


def accessory_options(detail, model, state, slots):
    pid = printer_pid(detail.get("pid"))
    name = re.sub(r"[\s_-]+", "", str(model).casefold()).removeprefix("flashforge")
    family = manual_family(DISCOVERY_MODELS.get(pid, "")) if pid is not None else manual_family(model)
    light_model = pid in (0x24, 0x28, 0x29) if pid is not None else (
        family == "creator" or name in {"adventurer5mpro", "ad5mpro"})
    light = chamber_light(detail) if light_model else None
    station = detail.get("matlStationInfo")
    ready = state in {"READY", "IDLE"} and str(detail.get("status", "")).upper() in {"READY", "IDLE"}
    editor = None
    if family in IFS_COLORS and ready and slots and isinstance(station, dict) and station.get("stateAction", 0) == 0:
        editor = {"materials": IFS_MATERIALS[family], "colors": IFS_COLORS[family]}
    return light, editor


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
    if name in {"adventureriii", "adventurer3", "adventurer3lite", "adventurer3pro", "adventurer3pro2"}:
        return "3"
    if name in {"adventurer4", "adventurer4pro", "adventurer4lite"}:
        return "4"
    return None


def manual_controls(detail, state, tcp_model="", nozzle=None, *, tcp_idle_after_cancel=False):
    tcp = manual_family(tcp_model)
    pid = detail.get("pid")
    model = (detail.get("model") or tcp_model) if pid is None else DISCOVERY_MODELS.get(printer_pid(pid))
    family = manual_family(model)
    legacy = family in {"3", "4"}
    if not family or (tcp and tcp != family) or (legacy and tcp != family):
        return None
    if not legacy and not detail:
        return None
    error = detail.get("errorCode")
    idle = tcp_idle_after_cancel or (
        state in {"READY", "IDLE"} and (legacy or str(detail.get("status", "")).strip().upper() in {"READY", "IDLE"})
    )
    tcp_motion = tcp == family and (legacy or family == "5m")
    ad5x_manual = family == "ad5x"
    if ad5x_manual:
        firmware = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)", str(detail.get("firmwareVersion", "")))
        if firmware:
            ad5x_manual = tuple(map(int, firmware.groups())) >= (1, 1, 6)
    motion = tcp_motion or ad5x_manual
    filament = tcp_motion or (ad5x_manual and detail.get("extrudeCtrl") == 1)
    hot = (nozzle if nozzle is not None else detail_number(detail, "rightTemp", 0, 500)) or 0
    groups = []
    if motion:
        groups.append("motion")
    if family == "5m":
        groups.append("release")
    if filament:
        groups.append("filament")
    if tcp_motion and family == "5m":
        groups.append("nozzleHeat")
    if not legacy:
        groups.append("calibration")
    if ad5x_manual:
        groups.append("clear")
    if not legacy and detail.get("autoShutdown") in ("open", "close"):
        groups.append("timer")
    allowed = []
    if idle and "nozzleHeat" in groups:
        allowed.append("heat0")
        if not error:
            allowed.extend(f"heat{temp}" for temp in range(180, 281, 20))
    if idle:
        if isinstance(error, str) and error.strip():
            if ad5x_manual:
                allowed.append("clear")
        else:
            if motion:
                allowed.append("home")
            if tcp_motion and "release" in groups:
                allowed.append("release")
            if not legacy:
                allowed.extend(("level", "shaper"))
            if tcp_motion or (ad5x_manual and detail.get("moveCtrl") == 1):
                allowed.extend(f"{axis}{delta:+d}" for axis in "xyz" for delta in (-10, -1, 1, 10))
            if filament and hot >= 180:
                allowed.extend(("e+10", "e-10"))
        if "timer" in groups:
            allowed.extend(("timer0", "timer30", "timer60", "timer120"))
    if tcp_idle_after_cancel:
        # The TCP session confirms motion and heating independently of the HTTP job status.
        allowed = [key for key in allowed if key == "home" or key.startswith(("heat", "x", "y", "z", "e"))]
    timer = detail.get("autoShutdown")
    notes = ["Ready" if idle else "Locked · printer must be idle"]
    if not idle and state in CANCELLED_JOB | {"CANCELING", "CANCELLING"}:
        notes.append(STATES[state])
    if idle and error:
        notes.append(str(error))
    return {"allowed": allowed, "groups": groups, "tcpMotion": tcp_motion,
            "filamentNeedsHeat": bool(idle and filament and hot < 180 and not error),
            "zPart": "nozzle" if legacy else "bed",
            "timerValue": "0" if timer == "close" else str(detail.get("autoShutdownTime", "")) if timer == "open" else "",
            "description": " · ".join(notes), "errorCode": error}


class ManualHomeRequired(ValueError):
    pass


class ManualTravelLimit(ValueError):
    pass


class ManualAxesBusy(ValueError):
    pass


MANUAL_TRAVEL = {"3": (150, 150, 150), "4": (220, 200, 250),
                 "5m": (220, 220, 220), "ad5x": (220, 220, 220)}


def manual_axis_position(reply, axis):
    match = re.search(r"\b" + axis + r":[ \t]*(-?\d+(?:\.\d+)?)(?=\s|$)", reply)
    return float(match[1]) if match else None


def bounded_5m_position(reply):
    position = tuple(manual_axis_position(reply, axis) for axis in "XYZ")
    if all(value is not None for value in position):
        x, y, z = position
        if abs(x) <= 110.2 and abs(y) <= 110.2 and 0 <= z <= 220.2:
            return position
    return None


def at_5m_park(position):
    return position and all(abs(value - 110) <= 0.2 for value in position[:2])


def at_5m_final_park(position):
    return (all(isinstance(value, (int, float)) for value in position)
            and at_5m_park(position) and abs(position[2] - 220) <= 0.05)


def stationary_check_seconds(position):
    # Only use the short window at the 5M's final XYZ parking position.
    return 2 if at_5m_final_park(position) else 10


def checked_manual_target(key, position, volume, family):
    axis = key[0].upper()
    index = "XYZ".index(axis)
    nominal = MANUAL_TRAVEL[family][index]
    reported = volume[index] if volume and len(volume) == 3 else None
    if reported is None and family == "ad5x":
        reported = nominal
    if type(reported) is not int or reported <= 0:
        raise ValueError("Printer did not report the axis travel limits.")
    minimum, maximum = 0, min(reported, nominal)
    if axis in "XY" and family != "ad5x":
        # The stock Adventurer 3, 4 and 5M printing envelopes have a centered XY origin.
        maximum /= 2
        minimum = -maximum
    target = position + int(key[1:])
    # A 5M can report X/Y a few hundredths past its nominal home coordinate.
    if family == "5m" and axis in "XY" and minimum < position < maximum:
        if maximum < target <= maximum + 0.1:
            return maximum
        if minimum - 0.1 <= target < minimum:
            return minimum
    if not minimum <= target <= maximum:
        raise ManualTravelLimit(f"{axis} move exceeds the axis travel limits ({minimum:g} to {maximum:g} mm).")
    return target


def ad5x_axis_position(host, axis):
    deadline = time.monotonic() + 0.75
    try:
        with socket.create_connection((host, PORT), timeout=0.75) as sock:
            return manual_axis_position(command(sock, "M114", deadline), axis)
    except (OSError, ValueError):
        # Some firmware or network configurations only expose the HTTP controls.
        return None


def tcp_manual_move(host, key, volume, model, release_marker=None, settled_homing=None, track_home=None, record_target=None,
                    *, require_idle_axes=False):
    """Keep the idle check and the single move in the same printer control session."""
    with socket.create_connection((host, PORT), timeout=2) as sock:
        def send(code, deadline=None):
            reply = command(sock, code, deadline) if deadline is not None else command(sock, code)
            if code.startswith(("G1 X", "G1 Y", "G1 Z")) and re.search(r"\bmust home axis first\b", reply, re.I):
                raise ManualHomeRequired("The printer requires homing before manual movement.")
            rejected = re.search(r"\b(?:error|failed|denied|unknown)\b", reply, re.I)
            if code == "M601 S1" and (rejected or "control success" not in reply.casefold()):
                raise ValueError("Printer denied manual control.")
            if rejected:
                raise ValueError("Printer rejected " + code.split()[0] + ".")
            return reply

        send("M601 S1")
        try:
            status = send("M119")
            mode_match = re.search(r"^MoveMode:[ \t]*(\S+)", status, re.M | re.I)
            mode = mode_match[1].upper() if mode_match else ""
            if not re.search(r"^MachineStatus:[ \t]*(?:READY|IDLE)[ \t]*\r?$", status, re.M | re.I):
                raise ManualAxesBusy("Printer must be idle.")
            if require_idle_axes and mode not in {"READY", "IDLE"}:
                raise ManualAxesBusy("Printer must be idle. Wait for movement or Home to finish.")
            verified_park = None
            if mode == "HOMING" and settled_homing and manual_family(model) == "5m":
                if bounded_5m_position(send("M114")) == settled_homing:
                    mode = "READY"
                    verified_park = settled_homing
            if mode in {"MOVING", "HOMING"}:
                raise ManualAxesBusy("Printer must be idle. Wait for movement or Home to finish.")
            if key.startswith("heat"):
                if manual_family(model) != "5m" or not re.fullmatch(r"heat(?:0|180|200|220|240|260|280)", key):
                    raise ValueError("Unsupported nozzle target.")
                target = int(key[4:])
                send(f"M104 S{target}")
                for attempt in range(4):
                    if attempt:
                        time.sleep(0.5)
                    reading = re.search(r"\bT0:\s*[\d.]+\s*/\s*(\d+(?:\.\d+)?)", send("M105"))
                    if reading and float(reading[1]) == target:
                        return "Printer confirmed the nozzle target."
                raise ValueError("Printer has not confirmed the nozzle target. Check Printer Readings.")
            if key == "home":
                try:
                    # A 5M can take several seconds to acknowledge homing.
                    send("G28", time.monotonic() + 30 if manual_family(model) == "5m" else None)
                except OSError:
                    if track_home:
                        track_home(acknowledged=False)
                    raise
                else:
                    if track_home:
                        track_home(acknowledged=True)
            elif key == "release":
                if manual_family(model) != "5m" or release_marker is None:
                    raise ValueError("Motor release is unavailable for this printer.")
                if mode not in {"READY", "IDLE"}:
                    raise ValueError("Printer did not confirm that the axes are idle.")
                # Keep the interlock even if the printer receives M18 but its reply is lost.
                release_marker.touch(exist_ok=True)
                send("M18")
            elif key.startswith("e"):
                if release_marker and release_marker.is_file() and mode not in {"READY", "IDLE"}:
                    raise ManualAxesBusy("Wait for the axes to stop before moving filament.")
                temp = re.search(r"\bT0:\s*(\d+(?:\.\d+)?)\s*/", send("M105"))
                if not temp or float(temp[1]) < 180:
                    raise ValueError("Heat the nozzle to at least 180 °C.")
                if manual_family(model) == "5m":
                    send("G92 E0")
                send(f"G1 E{int(key[1:])} F120")
            else:
                axis = key[0].upper()
                coordinates = verified_park
                if coordinates is None:
                    reply = send("M114")
                    position = manual_axis_position(reply, axis)
                    if manual_family(model) == "5m":
                        coordinates = bounded_5m_position(reply)
                else:
                    position = coordinates["XYZ".index(axis)]
                if position is None:
                    raise ValueError("Printer did not report the axis position.")
                target = checked_manual_target(key, position, volume, manual_family(model))
                send("G90")
                send(f"G1 {axis}{target:.3f} F300")
                if coordinates and record_target:
                    expected = list(coordinates)
                    expected["XYZ".index(axis)] = round(target, 3)
                    record_target(tuple(expected))
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
    state = {"PAUSE": "PAUSED", "CANCEL": "CANCELING", "CALIBRATE_DOING": "CALIBRATING"}.get(state, state)
    if not state:
        raise PrinterAPIError("The printer returned invalid status data.")
    complete = state in COMPLETED_JOB
    active = state in ACTIVE_JOB
    has_job = active or complete or state in ENDED_JOB
    model = DISCOVERY_MODELS.get(printer_pid(detail.get("pid"))) or field("model")
    family = manual_family(model)
    creator = family == "creator"
    creator_pro = creator and model.casefold().endswith("pro")
    volume = re.fullmatch(r"(\d{1,4})[xX](\d{1,4})[xX](\d{1,4})", field("measure"))

    def tool_values(key):
        values = detail.get(key)
        if not isinstance(values, list):
            return []
        return [detail_number({index: value}, index, 0, 500)
                for index, value in enumerate(values[:4])]

    actuals = tool_values("nozzleTemps")
    targets = tool_values("nozzleTargetTemps")
    single = family in {"ad5x", "5m", "3", "4"}
    count = 4 if creator else max(len(actuals), len(targets))
    tools = [] if single else [
        {"actual": actuals[i] if i < len(actuals) else None,
         "target": targets[i] if i < len(targets) else None} for i in range(count)]
    def nozzle_value(key, values):
        if tools:
            return values[0] if values else None
        scalar = detail_number(detail, key, 0, 500)
        return scalar if scalar is not None else (values[0] if values else None)

    readings = {
        "nozzle": nozzle_value("rightTemp", actuals),
        "nozzleTarget": nozzle_value("rightTargetTemp", targets),
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
    slots = material_slots(detail)
    light, editor = accessory_options(detail, model, state, slots)
    return {
        "status": STATES.get(state, state), "state": state, **readings,
        "temperatures": readings, "tools": tools, "jobControls": True,
        "materials": slots, "materialFamily": family, "light": light, "slotEditor": editor, "transport": "HTTP",
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
        "cameraUrl": camera, "managedCamera": creator, "manual": manual_controls(detail, state),
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
        state = state_match[1].upper()
        move = re.search(r"^MoveMode:[ \t]*(\S+)", status, re.M | re.I)
        move_mode = move[1].upper() if move else ""
        serial = None
        machine = ""
        try:
            machine = command(sock, "M115", deadline)
        except (OSError, ValueError):
            pass
        if machine:
            serial = re.search(r"^(?:SN|Serial Number):[ \t]*([\w-]+)", machine, re.M | re.I)
        homing_park = None
        position = {}
        model = re.search(r"^(?:echo: )?Machine Type:[ \t]*([^\r\n]+)", machine, re.M | re.I)
        family = manual_family(model[1]) if model else None
        if family in MANUAL_TRAVEL and state in {"READY", "IDLE"}:
            try:
                reply = command(sock, "M114", deadline)
                position = {axis: manual_axis_position(reply, axis) for axis in "XYZ"}
                if (serial and check_code and family == "5m"
                        and move_mode == "HOMING"):
                    homing_park = bounded_5m_position(reply)
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
    detail, time_hint, http_state = {}, "", ""
    tcp_idle_after_cancel = False
    if serial:
        detail, time_hint = read_job_detail(serial[1], check_code, host)
        http_state = str(detail.get("status", "")).strip().upper()
        if http_state not in {"READY", "IDLE"} or detail.get("errorCode"):
            homing_park = None
        if http_state == "CALIBRATE_DOING":
            state = "CALIBRATING"
        elif family == "5m" and state in {"READY", "IDLE"} and http_state in CANCELLED_JOB:
            state = http_state
            tcp_idle_after_cancel = move_mode in {"READY", "IDLE"} and not detail.get("errorCode")
    elif state in ACTIVE_JOB:
        if not check_code:
            time_hint = "Enter your CheckCode in the plugin settings"
        else:
            time_hint = "Printer serial number unavailable"
    complete = state in COMPLETED_JOB or (state == "BUSY" and http_state in COMPLETED_JOB)
    active = state in ACTIVE_JOB
    percent = 100 if complete else min(100, round(current * 100 / total)) if active and total else None
    if active and percent is None:
        # Older firmware exposes progress through M27 without a layer count.
        sd = re.search(r"^SD printing byte[ \t]+(\d{1,12})/(\d{1,12})\s*$", progress, re.M | re.I)
        if sd:
            done, size = map(int, sd.groups())
            if 0 <= done <= size and size:
                percent = round(done * 100 / size)
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
    sensors = {"endstops": endstops,
               "led": bool(int(led[1])) if led else None,
               "moveMode": move_mode, "stationaryCandidate": homing_park,
               "homingPark": homing_park if at_5m_park(homing_park) else None}
    controls = manual_controls(detail, state, info.get("model", ""), readings["nozzle"],
                               tcp_idle_after_cancel=tcp_idle_after_cancel)
    if controls and position:
        limited = []
        for key in controls["allowed"]:
            if re.fullmatch(r"[xyz][+-](?:1|10)", key):
                current = position.get(key[0].upper())
                if current is not None:
                    try:
                        checked_manual_target(key, current, info.get("volume"), family)
                    except ManualTravelLimit:
                        limited.append(key)
                    except ValueError:
                        pass  # Unknown travel dimensions give no visual prediction.
        controls["position"] = position
        controls["travelLimited"] = limited
    slots = material_slots(detail)
    light, editor = accessory_options(detail, info.get("model", ""), state, slots)
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
        "materials": slots, "light": light, "slotEditor": editor, "transport": "TCP + HTTP" if detail else "TCP",
        "manual": controls,
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


def printer_credentials(entry):
    if not isinstance(entry, dict):
        raise ValueError("Invalid printer entry.")
    host = local_address(str(entry.get("host") or ""))
    code = str(entry.get("code") or "")
    if len(code) > 32 or any(not 33 <= ord(c) <= 126 for c in code):
        raise ValueError("The CheckCode must be printable ASCII, up to 32 characters.")
    return host, code, printer_serial(entry.get("serial"))


def discover_printers(timeout=1.5):
    devices = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(b"\x00", ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while len(devices) < 32 and (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                packet, sender = sock.recvfrom(1024)
            except socket.timeout:
                break
            if len(packet) not in (276, 280) or packet[0x86:0x88] != b"\x2b\x71":
                continue
            model = DISCOVERY_MODELS.get(int.from_bytes(packet[0x88:0x8a], "big"))
            if not model:
                continue
            try:
                host = local_address(sender[0])
            except ValueError:
                continue
            serial = packet[0x92:0x112].split(b"\0", 1)[0].decode("ascii", "ignore")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", serial):
                serial = ""
            devices.setdefault(host, {"host": host, "name": model, "serial": serial})
    return sorted(devices.values(), key=lambda item: ipaddress.IPv4Address(item["host"]))


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
            catalog = printer_api(self.host, self.serial, self.check_code, "/gcodeList", limit=1024 * 1024)
            names = catalog.get("gcodeList")
            if not isinstance(names, list) and isinstance(catalog.get("gcodeListDetail"), list):
                names = [row.get("gcodeFileName") for row in catalog["gcodeListDetail"] if isinstance(row, dict)]
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


def print_source_name(name):
    return re.sub(r"(?:\.gcode)?\.3mf$", "", name, flags=re.I).casefold()


def source_requirements(stream):
    try:
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            code = [item for item in members if re.fullmatch(r"Metadata/plate_[1-9][0-9]*\.gcode", item.filename)]
            configs = [item for item in members if item.filename == "Metadata/slice_info.config"]
            if len(code) != 1 or not code[0].file_size or len(configs) != 1:
                raise ValueError("Choose a sliced .3mf with one plate.")
            if configs[0].file_size > MAX_SLICE_INFO:
                raise ValueError("The slice information is too large.")
            data = archive.read(configs[0]).decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
            raise ValueError("Invalid slice information.")
        plates = [plate for plate in ElementTree.fromstring(data).findall("plate")
                  if plate.findall("filament")]
    except (zipfile.BadZipFile, ElementTree.ParseError, UnicodeError, RuntimeError, NotImplementedError) as error:
        raise ValueError("Could not read slice information from this .3mf.") from error
    if len(plates) != 1:
        raise ValueError("Choose a sliced .3mf with one plate and filament information.")
    requirements = {}
    for filament in plates[0].findall("filament"):
        number = filament.get("id", "")
        material = filament.get("type", "")
        color = filament.get("color", "")
        if (not re.fullmatch(r"[1-4]", number) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 +._-]{0,47}", material)
                or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color) or int(number) - 1 in requirements):
            raise ValueError("The .3mf has invalid or unsupported filament assignments.")
        tool = int(number) - 1
        requirements[tool] = {"toolId": tool, "materialName": material, "materialColor": color.upper()}
    return list(requirements.values())


def preview_digest(adapter, path):
    try:
        image = adapter.preview(path)
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(image).hexdigest() if image else ""


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
                raw_events = job.get("events")
                job["events"] = [event for event in raw_events[-32:]
                                 if isinstance(event, list) and len(event) == 2
                                 and type(event[0]) is int and 0 <= event[0] <= 253402300799
                                 and isinstance(event[1], str) and len(event[1]) <= 80] if isinstance(raw_events, list) else []
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

    def record_event(self, job, now, label):
        job["events"] = [*job.get("events", []), [now, label]][-32:]
        self.dirty = True

    def connection_lost(self, now):
        if not self.jobs or self.jobs[0]["result"] != "active":
            return False
        job = self.jobs[0]
        events = job.get("events", [])
        if events and events[-1][1] == "Connection lost":
            return False
        self.record_event(job, now, "Connection lost")
        self.save(now)
        return True

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
                    self.record_event(job, now, {
                        "complete": "Print completed", "cancelled": "Print cancelled", "error": "Printer reported an error",
                    }[job["result"]])
                if job["result"] == "complete" and elapsed is not None:
                    job["elapsed"] = elapsed
                self.dirty = write_now = True
                job = None

        if status["active"] and not job:
            job = {"file": name, "started": now - (elapsed or 0), "seen": now,
                   "elapsed": elapsed, "layer": layer, "result": "active"}
            self.record_event(job, job["started"], "Printing started")
            if state == "PAUSED":
                self.record_event(job, now, "Print paused")
            job["lastState"] = state
            self.jobs.insert(0, job)
            del self.jobs[HISTORY_LIMIT:]
            self.dirty = write_now = True
        elif status["active"]:
            events = job.get("events", [])
            if not events:
                self.record_event(job, job["started"], "Printing started")
            elif events[-1][1] == "Connection lost":
                self.record_event(job, now, "Connection restored")
                write_now = True
            previous = job.get("lastState")
            if state == "PAUSED" and previous != "PAUSED":
                self.record_event(job, now, "Print paused")
                write_now = True
            elif previous == "PAUSED" and state in {"PRINTING", "BUILDING", "BUILDING_FROM_SD"}:
                self.record_event(job, now, "Print resumed")
                write_now = True
            job["lastState"] = state
            job["file"] = name or job["file"]
            job["seen"] = now
            job["elapsed"] = elapsed if elapsed is not None else job["elapsed"]
            job["layer"] = layer if layer is not None else job["layer"]
            self.dirty = True
        if write_now or now - self.saved_at >= HISTORY_CHECKPOINT:
            self.save(now)


def same_observed_job(previous, current):
    if not previous or not previous.get("active") or not current.get("active"):
        return False
    if (not current.get("file") or previous.get("file") != current["file"]
            or previous.get("info", {}).get("serial") != current.get("info", {}).get("serial")):
        return False
    for key in ("layer", "elapsed", "percent"):
        before, after = previous.get(key), current.get(key)
        if before is not None and after is not None and after < before:
            return False
    return True


class PrinterMonitor:
    def __init__(self, adapter, history_path):
        self.adapter = adapter
        self.history = PrintHistory(history_path)
        self.temperatures = deque(maxlen=361)
        self.files = {}
        self.file_order_path = history_path.with_suffix(".files.json")
        try:
            order = json.loads(self.file_order_path.read_text(encoding="utf-8"))
            self.file_order = [key for key in order if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{32}", key)] if isinstance(order, list) else []
        except (OSError, ValueError, RecursionError):
            self.file_order = []
        self.latest = None
        self.camera_url = ""
        self.camera_lock = threading.Lock()
        self.next_camera_probe = 0
        self.error = "Connecting to the printer…"
        self.permission_denied = False
        self.poll_worker = None
        self.state_lock = threading.Lock()
        self.manual_release_marker = None
        self.manual_storage = history_path.parent
        self.manual_serial = ""
        self.homing_legacy = ""
        self.expected_manual_position = None
        self.manual_target_deadline = 0
        self.manual_home_until = 0
        self.manual_home_seen = False
        self.manual_home_acknowledged = None
        self.homing_park_observation = None
        self.calibration = None
        self.calibration_deadline = 0
        self.history_snapshot = [job.copy() for job in self.history.jobs]
        self.history_error = self.history.error

    def update_file_catalog(self, files):
        with self.state_lock:
            known = set(self.file_order)
            ordered = {key: value for key, value in files.items() if key not in known}
            ordered.update((key, files[key]) for key in self.file_order if key in files)
            self.files = ordered
            order = list(ordered)
            if order != self.file_order:
                self.file_order = order
                temp = self.file_order_path.with_suffix(".tmp")
                try:
                    temp.write_text(json.dumps(order), encoding="utf-8")
                    os.replace(temp, self.file_order_path)
                except OSError:
                    pass
            return ordered

    def bind_manual_printer(self, status):
        serial = status.get("info", {}).get("serial") or ""
        if serial == self.manual_serial:
            return
        self.manual_serial = serial
        self.manual_home_until = 0
        self.expected_manual_position = None
        self.homing_park_observation = None
        digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()
        self.manual_release_marker = self.manual_storage / f"serial-{digest}.manual-home-required" if serial else None

    def legacy_home_fingerprint(self):
        # Old empty markers identify an address, so any one could belong to a moved printer.
        markers = []
        try:
            for path in sorted(self.manual_storage.iterdir()):
                if not (path.name.startswith("flashforge-") and path.name.endswith("-history.manual-home-required")):
                    continue
                try:
                    markers.append((path.name, path.stat().st_mtime_ns))
                except FileNotFoundError:
                    continue
        except OSError:
            return "unreadable"
        return hashlib.sha256(repr(markers).encode("utf-8")).hexdigest() if markers else ""

    def manual_home_required(self):
        if self.manual_release_marker is None:
            return True
        try:
            self.manual_release_marker.stat()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            return True
        legacy = self.legacy_home_fingerprint()
        if not legacy:
            return False
        try:
            receipt = self.manual_release_marker.with_suffix(".legacy-homed").read_text(encoding="ascii")
        except (OSError, UnicodeError):
            return True
        return legacy == "unreadable" or receipt != legacy

    def manual_controls_view(self, controls, status=None):
        sensors = (status or {}).get("sensors") or {}
        if (controls and controls.get("tcpMotion") and "release" in controls["groups"]
                and sensors.get("moveMode", "").upper() == "HOMING" and not self.settled_homing_position(status)):
            can_home = self.settled_homing_position(status, for_home=True) is not None
            allowed = [key for key in controls["allowed"] if key.startswith("timer") or (key == "home" and can_home)
                       or key in {"level", "shaper"}]
            hint = "Press Home to restore axis control" if can_home else "Checking axis position…"
            controls = {**controls, "allowed": allowed, "description": "Motion · " + hint, "checkingPosition": not can_home}
        if not controls or "release" not in controls["groups"] or not self.manual_home_required():
            return controls
        homing = self.manual_home_until > time.monotonic()
        sensors = (status or {}).get("sensors") or {}
        mode = sensors.get("moveMode", "").upper()
        filament_idle = not homing and (mode in {"READY", "IDLE"} or
                        (mode == "HOMING" and self.settled_homing_position(status) is not None))
        allowed = [key for key in controls["allowed"]
                   if key.startswith(("heat", "timer")) or (key == "home" and not homing)
                   or (key in {"e+10", "e-10"} and filament_idle)]
        return {**controls, "allowed": allowed, "released": True, "releaseHoming": homing,
                "releaseHomeAcknowledged": self.manual_home_acknowledged, "filamentNeedsHeat": bool(filament_idle and controls["filamentNeedsHeat"])}

    def track_manual_home(self, acknowledged):
        with self.state_lock:
            self.manual_home_seen = False
            self.manual_home_acknowledged = acknowledged
            self.manual_home_until = time.monotonic() + 60
            self.next_due = 0

    def record_manual_target(self, position):
        with self.state_lock:
            self.expected_manual_position = position
            self.manual_target_deadline = time.monotonic() + 5
            self.next_due = 0

    def matches_manual_target(self, position):
        # Firmware rounds reported positions to motor steps.
        return (position is not None and self.expected_manual_position is not None
                and all(abs(a - b) <= 0.025 for a, b in zip(position, self.expected_manual_position)))

    def settled_homing_position(self, status, for_home=False):
        if self.homing_park_observation is None:
            return None
        position, first, last = self.homing_park_observation
        sensors = status.get("sensors") or {}
        current = sensors.get("stationaryCandidate") or sensors.get("homingPark")
        expected = self.matches_manual_target(position)
        if (position == current and (for_home or expected or at_5m_park(position))
                and (expected or last - first >= stationary_check_seconds(position))
                and time.monotonic() - last <= IDLE_POLL_INTERVAL + POLL_INTERVAL):
            return position
        return None

    def update_manual_home(self, status):
        self.bind_manual_printer(status)
        now = time.monotonic()
        sensors = status.get("sensors") or {}
        parked = sensors.get("stationaryCandidate") or sensors.get("homingPark")
        if self.matches_manual_target(parked):
            self.manual_target_deadline = 0
        elif now >= self.manual_target_deadline or status["active"] or status["state"] not in {"READY", "IDLE"}:
            self.expected_manual_position = None
        observation = self.homing_park_observation
        if parked is None:
            self.homing_park_observation = None
        elif observation and observation[0] == parked and now - observation[2] <= IDLE_POLL_INTERVAL + POLL_INTERVAL:
            self.homing_park_observation = (parked, observation[1], now)
        else:
            self.homing_park_observation = (parked, now, now)
        if not self.manual_home_until:
            return
        state = status["state"]
        motion = sensors.get("moveMode", "").upper()
        manual = status.get("manual") or {}
        error = manual.get("errorCode")
        idle = state in {"READY", "IDLE"}
        settled = self.settled_homing_position(status)
        parked_after_home = at_5m_park(settled) and (self.manual_home_acknowledged or self.manual_home_seen)
        position = manual.get("position") or {}
        xyz = tuple(position.get(axis) for axis in "XYZ")
        # G28 can finish before the first status poll observes any motion.
        idle_after_home = motion in {"READY", "IDLE"} and (
            self.manual_home_seen or (self.manual_home_acknowledged and at_5m_final_park(xyz)))
        if (now >= self.manual_home_until or status["active"] or error
                or state in ENDED_JOB or state in {"ATTENTION", "CALIBRATING"}):
            self.manual_home_until = 0
        elif idle and (parked_after_home or idle_after_home):
            self.manual_home_until = 0
            try:
                if self.manual_release_marker is not None:
                    if self.homing_legacy and self.homing_legacy != "unreadable":
                        self.manual_release_marker.with_suffix(".legacy-homed").write_text(self.homing_legacy, encoding="ascii")
                    self.manual_release_marker.unlink(missing_ok=True)
            except OSError:
                # Keep the interlock if its saved state could not be cleared.
                pass
        elif (state in {"HOMING", "MOVING"} or motion == "MOVING" or (motion == "HOMING" and not idle)):
            self.manual_home_seen = True

    def update_calibration(self, status, now):
        current = self.calibration or {}
        phase = current.get("phase")
        key = current.get("key")
        state = status["state"]
        manual = status.get("manual") or {}
        if state == "CALIBRATING":
            self.calibration = {"phase": "running", "key": key if phase in {"waiting", "running", "checking"} else None}
        elif phase in {"waiting", "running", "checking"}:
            if state in {"ERROR", "ATTENTION"} or manual.get("errorCode"):
                self.calibration = {"phase": "failed", "key": key}
            elif phase == "waiting":
                if now >= self.calibration_deadline:
                    self.calibration = {"phase": "unconfirmed", "key": key}
                else:
                    return
            elif "level" in manual.get("allowed", ()):
                self.calibration = {"phase": "ready", "key": key}
            else:
                self.calibration = {"phase": "checking", "key": key}
            if self.calibration["phase"] in {"failed", "ready", "unconfirmed"}:
                self.calibration_deadline = now + 30
        elif phase and now >= self.calibration_deadline:
            self.calibration = None

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
                if status.get("active") and status.get("file") and status.get("info", {}).get("serial"):
                    previous_token = self.latest.get("jobToken") if same_observed_job(self.latest, status) else None
                    status["jobToken"] = previous_token or secrets.token_hex(16)
                self.update_manual_home(status)
                observation = self.homing_park_observation
                if (self.manual_target_deadline > time.monotonic() or
                        (observation and observation[2] - observation[1] < stationary_check_seconds(observation[0])
                         and not self.matches_manual_target(observation[0]))):
                    self.next_due = min(getattr(self, "next_due", 0), time.monotonic() + 1)
                self.update_calibration(status, now)
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
            return
        changed = self.history.connection_lost(now)
        with self.state_lock:
            if changed:
                self.history_snapshot = [job.copy() for job in self.history.jobs]
                self.history_error = self.history.error
            if time.monotonic() >= self.manual_home_until or self.permission_denied:
                self.manual_home_until = 0
            self.homing_park_observation = None


class UploadInProgress(Exception):
    pass


WEB_HOST = "127.0.0.1"
WEB_PORT = 18771
LEGACY_WEB_PORTS = (18769, 18765)
MONITOR_BIND_ERROR = "The local FlashForge port ({address}) is in use or reserved. Close all OrcaSlicer windows and reopen OrcaSlicer."
DEVICE_UI_KEY = "print_host_webui"
MONITOR_TITLE = "FlashForge"
# OrcaSlicer reads registered capabilities before capability config is available.
PLACEMENT_FILE = Path(__file__).with_name("monitor-placement.txt")
SETUP_PAGE = "setup"


DEFAULT_PLACEMENT = "device"


def normalize_placement(value, fallback=DEFAULT_PLACEMENT):
    return value if value in ("device", "flashforge") else fallback


def startup_placement():
    try:
        storage = Path(orca.host.plugin.storage())
    except (AttributeError, OSError, RuntimeError):
        storage = PLACEMENT_FILE.parent
    for path in (storage / PLACEMENT_FILE.name, PLACEMENT_FILE):
        try:
            choice = normalize_placement(path.read_text(encoding="ascii").strip(), None)
        except (OSError, UnicodeError):
            continue
        if choice is not None:
            return choice
    return DEFAULT_PLACEMENT


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
    if parts in ([""], [SETUP_PAGE]):
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


def is_flashforge_profile(preset):
    return (preset.config_value("host_type") == "flashforge"
            or (preset.config_value("printer_model") or "").casefold().startswith("flashforge "))


class LoopbackHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        # Winsock's SO_REUSEADDR can let two servers listen on the same port.
        if not self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # The numeric loopback address does not need a hostname lookup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


class LoopbackServer(LoopbackHTTPServer):
    poller = None
    native_page = None
    check_view = None
    setup_profile = None

    placement = DEFAULT_PLACEMENT
    host_language = "en"
    webview_language = ""

    def language_for(self, user_agent=""):
        # OrcaSlicer's embedded WebView also sends the UI language, including
        # locales omitted by app_info(). Its bare "zh" cannot identify the script.
        if "BBL-Slicer/" in user_agent:
            match = re.search(r"\bBBL-Language/([a-zA-Z_-]+)(?=\s|$)", user_agent)
            if match:
                ambiguous_chinese = match[1].lower() == "zh" and self.host_language.startswith("zh-")
                self.webview_language = "" if ambiguous_chinese else normalize_language(match[1], "")
        return normalize_language(self.get_config().get("language"), self.webview_language or self.host_language)

    def configure(self, get_config, storage):
        self.get_config = get_config
        self.storage = storage
        self.monitors = {}
        self.error = ""
        self.state_lock = threading.Lock()
        self.cleared_beds_path = storage / "cleared-beds.json"
        try:
            cleared = json.loads(self.cleared_beds_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cleared = {}
        self.cleared_beds = ({host: token for host, token in cleared.items()
                              if isinstance(token, str) and re.fullmatch(r"[0-9a-f]{64}", token)}
                             if isinstance(cleared, dict) else {})
        self.history_names_path = storage / "history-printers.json"
        try:
            names = json.loads(self.history_names_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            names = {}
        self.history_names = ({host: name[:60] for host, name in names.items() if isinstance(name, str) and name}
                              if isinstance(names, dict) else {})
        self.printer_locks = {}
        self.upload_gate = threading.Condition()
        self.active_printer_io = 0
        self.uploads = {}
        self.discovery_lock = threading.Lock()
        self.source_lock = threading.Lock()
        self.source_transfer = threading.Lock()
        self.upload_generation = 0
        self.file_catalog_revision = 0
        self.file_transfer_locks = {}
        self.source_path = storage / "print-sources.json"
        try:
            saved_sources = json.loads(self.source_path.read_text(encoding="utf-8"))
            self.sources = {key: value for key, value in saved_sources.items() if isinstance(value, dict)}
        except (OSError, ValueError, AttributeError):
            self.sources = {}
        self.apply_settings = None
        self.notify = None
        self.stopping = threading.Event()
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
                "notifications": self.notifications, "placement": self.placement,
                "savedPlacement": normalize_placement(self.get_config().get("placement"), self.placement),
                "setupHost": self.setup_profile() if not printers and self.setup_profile else ""}

    def remember_history_names(self, entries):
        names = {**self.history_names, **{entry["host"]: entry["name"] for entry in entries}}
        if names == self.history_names:
            return
        pending = self.history_names_path.with_suffix(".tmp")
        try:
            pending.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
            os.replace(pending, self.history_names_path)
        except OSError:
            return
        self.history_names = names

    def history_payload(self):
        configured = {entry["host"]: entry["name"] for entry in self.printers()}
        with self.state_lock:
            monitors = self.monitors.copy()
        paths = {}
        for path in self.storage.glob("flashforge-*-history.json"):
            try:
                host = local_address(path.name.removeprefix("flashforge-").removesuffix("-history.json"))
            except ValueError:
                continue
            if path.is_file() and not path.is_symlink():
                paths[host] = path
        now = int(time.time())
        printers = []
        for host in dict.fromkeys((*configured, *sorted(paths))):
            name = configured.get(host) or self.history_names.get(host) or host
            monitor = monitors.get(host)
            if monitor is not None and host in configured:
                with monitor.state_lock:
                    jobs, error = monitor.history_snapshot, monitor.history_error
            elif host in paths:
                saved = PrintHistory(paths[host])
                jobs = saved.jobs
                error = "Print history is temporarily unavailable." if saved.error else ""
                if host not in configured:
                    # A retired printer is no longer sampled, so an unfinished
                    # record is not a live print. Leave the saved row intact.
                    jobs = [{**job, "result": "unknown"} if job["result"] == "active" else job for job in jobs]
            else:
                jobs, error = [], ""
            printers.append({"host": host, "name": name, "jobs": jobs, "error": error,
                             "stats": history_stats(jobs, now)})
        jobs = [job for printer in printers for job in printer["jobs"]]
        jobs.sort(key=lambda job: job["started"], reverse=True)
        return {"printers": printers, "stats": history_stats(jobs, now)}

    def confirm_bed_clear(self, host, token):
        if host not in {entry["host"] for entry in self.printers()}:
            return False
        with self.state_lock:
            monitor = self.monitors.get(host)
            if monitor is None:
                return False
            with monitor.state_lock:
                expected = bed_clear_token(monitor.history_snapshot, (monitor.latest or {}).get("active"))
                if not expected or expected != token:
                    return False
                if self.cleared_beds.get(host) != token:
                    updated = {**self.cleared_beds, host: token}
                    pending = self.cleared_beds_path.with_suffix(".tmp")
                    pending.write_text(json.dumps(updated), encoding="utf-8")
                    os.replace(pending, self.cleared_beds_path)
                    self.cleared_beds = updated
                return True

    def fleet_payload(self, current=None):
        with self.state_lock:
            cleared = self.cleared_beds
        items = []
        for entry in self.printers():
            monitor = self.monitors.get(entry["host"])
            data, error, camera, history = None, "", "", ()
            if monitor is not None:
                with monitor.state_lock:
                    data = monitor.latest
                    error = monitor.error
                    camera = monitor.camera_url
                    history = monitor.history_snapshot
            job = history[0] if history and history[0]["result"] == "active" else {}
            item = {"host": entry["host"], "name": entry["name"],
                    "current": entry["host"] == current, "error": error,
                    "status": None, "percent": None, "remaining": None,
                    "file": "", "nozzle": None, "bed": None,
                    "printing": False, "complete": False, "cameraUrl": camera,
                    "events": job.get("events", []),
                    "lastCompleted": last_completed_job(history)}
            if data:
                item.update(state=data.get("state"), observedAt=data.get("observedAt"), status=data.get("status"), percent=data.get("percent"),
                            remaining=data.get("remaining"), file=data.get("file"),
                            nozzle=data.get("nozzle"), bed=data.get("bed"),
                            printing=bool(data.get("active")), complete=bool(data.get("complete")))
                item.update({key: data.get(key) for key in (
                    "layer", "layerTotal", "elapsed", "filament", "nozzleTarget", "bedTarget",
                    "tools", "speed", "fan", "managedCamera", "jobControls", "jobToken",
                )})
            elif job and error:
                item.update(printing=True, file=job["file"], layer=job.get("layer"))
            token = bed_clear_token(history, item["printing"])
            if token and cleared.get(entry["host"]) != token:
                item["bedClearToken"] = token
            items.append(item)
        return items

    def upload_active(self):
        with self.upload_gate:
            now = time.monotonic()
            return any(deadlines[-1] > now for deadlines in self.uploads.values())

    def save_sources(self):
        try:
            path = self.source_path.with_suffix(".tmp")
            path.write_text(json.dumps(self.sources, ensure_ascii=False), encoding="utf-8")
            os.replace(path, self.source_path)
        except OSError:
            pass

    def confirm_print_source(self, monitor):
        source = getattr(monitor, "pending_print_source", None)
        if not source:
            return
        status = monitor.latest or {}
        if source["generation"] != self.upload_generation or time.monotonic() >= source["expires"]:
            monitor.pending_print_source = None
        elif (status.get("active") and status.get("file") == source["name"]
              and status.get("info", {}).get("serial") == source["serial"]):
            monitor.pending_print_source = None
            if source["preview"]:
                with self.source_lock:
                    if source["generation"] == self.upload_generation:
                        key = source["serial"] + ":" + source["file_id"]
                        self.sources.pop(key, None)
                        self.sources[key] = {field: source[field] for field in ("name", "preview", "requirements")}
                        if len(self.sources) > 100:
                            self.sources.pop(next(iter(self.sources)))
                        self.save_sources()

    @contextmanager
    def printer_io(self, host):
        with self.upload_gate:
            if self.upload_active() or self.stopping.is_set():
                raise UploadInProgress
            lock = self.printer_locks.setdefault(host, threading.Lock())
        with lock:
            with self.upload_gate:
                if self.upload_active() or self.stopping.is_set():
                    raise UploadInProgress
                self.active_printer_io += 1
            try:
                yield
            finally:
                with self.upload_gate:
                    self.active_printer_io -= 1
                    self.upload_gate.notify_all()

    def begin_upload(self, name=""):
        # The Orca upload worker calls this before opening its printer connection.
        # Drain the in-flight request, but never hold the caller for long: the
        # port has a single client, so a bounded wait is better than a GUI stall.
        with self.upload_gate:
            now = time.monotonic()
            self.uploads = {key: deadlines for key, deadlines in self.uploads.items()
                            if key == name or deadlines[-1] > now}
            self.uploads.setdefault(name, deque()).append(now + UPLOAD_TIMEOUT)
            with self.source_lock:
                self.upload_generation += 1
                # Orca reports the local temporary filename, not the destination.
                if self.sources:
                    self.sources.clear()
                    self.save_sources()
            self.upload_gate.wait_for(lambda: not self.active_printer_io, timeout=UPLOAD_DRAIN)

    def finish_upload(self, name=""):
        with self.upload_gate:
            deadlines = self.uploads.get(name)
            if deadlines:
                deadlines.popleft()
                if not deadlines:
                    del self.uploads[name]
            self.file_catalog_revision += 1

    def poll_device(self, entry, monitor):
        try:
            with self.printer_io(entry["host"]):
                monitor.adapter.configure(entry["code"], entry["serial"])
                monitor.sample(int(time.time()))
                self.confirm_print_source(monitor)
            if self.notify is not None and not self.stopping.is_set():
                monitor.notifications = self.notifications
                try:
                    self.notify(monitor)
                except Exception:
                    pass
        except UploadInProgress:
            monitor.next_due = 0
        except Exception:
            with monitor.state_lock:
                monitor.latest = None
                monitor.error = "Could not update printer data."
        finally:
            if self.stopping.is_set():
                monitor.history.save(int(time.time()))

    def poll_printer(self):
        try:
            while not self.stopping.is_set():
                now = int(time.time())
                mono = time.monotonic()
                try:
                    entries = self.printers()
                    self.remember_history_names(entries)
                    hosts = {entry["host"] for entry in entries}
                    with self.state_lock:
                        for host in [name for name in self.monitors if name not in hosts]:
                            monitor = self.monitors[host]
                            if monitor.poll_worker and monitor.poll_worker.is_alive():
                                continue
                            with self.upload_gate:
                                lock = self.printer_locks.setdefault(host, threading.Lock())
                            if lock.acquire(blocking=False):
                                try:
                                    self.monitors.pop(host).history.save(now)
                                finally:
                                    lock.release()
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
                        if (monitor is None or mono < getattr(monitor, "next_due", 0.0)
                                or (monitor.poll_worker and monitor.poll_worker.is_alive())
                                or self.upload_active()):
                            continue
                        priority = self.viewed_host if self.viewed_host in hosts else entries[0]["host"]
                        interval = POLL_INTERVAL if entry["host"] == priority or len(entries) <= 3 else IDLE_POLL_INTERVAL
                        if monitor.manual_home_until:
                            interval = 1
                        monitor.next_due = mono + interval
                        monitor.poll_worker = threading.Thread(
                            target=self.poll_device, args=(entry, monitor), daemon=True,
                            name=f"FlashForge poll {entry['host']}")
                        monitor.poll_worker.start()
                except Exception:
                    # A settings read failure must not discard unrelated snapshots.
                    pass
                self.stopping.wait(0.5)
        finally:
            for monitor in list(self.monitors.values()):
                if not monitor.poll_worker or not monitor.poll_worker.is_alive():
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

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


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
        if endpoint == SETUP_PAGE:
            raise ValueError("This printer is not configured yet.")
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
                or action not in ("settings", "action", "diagnostics", "notifications", "camera-stream",
                                  "discover", "test-printer", "print-source", "upload-file", "confirm-bed-clear")):
            self.reply_json({"error": "Open the monitor in OrcaSlicer."}, 403)
            return
        endpoint, route = self.request_route()
        if route in {"camera-stream", "action", "print-source", "upload-file", "confirm-bed-clear"} or (route == "diagnostics" and "?" in self.path):
            try:
                endpoint = self.checked_printer(endpoint)
            except (OSError, RuntimeError, ValueError) as error:
                self.reply_json({"error": str(error)}, 409)
                return
        if route == action == "camera-stream":
            self.stream_camera(endpoint)
            return
        if route == action == "discover":
            if not self.server.discovery_lock.acquire(blocking=False):
                self.reply_json({"error": "A printer search is already running."}, 409)
                return
            try:
                printers = discover_printers()
            except OSError:
                self.reply_json({"error": "Network discovery unavailable. Enter the printer address manually."}, 503)
            else:
                self.reply_json({"printers": printers})
            finally:
                self.server.discovery_lock.release()
            return
        if route == action == "test-printer":
            self.test_printer()
            return
        if route == action == "upload-file":
            self.upload_file(endpoint)
            return
        if route == action == "print-source":
            self.prepare_print_source(endpoint)
            return
        if route == action == "confirm-bed-clear":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 256:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                token = payload.get("token") if isinstance(payload, dict) else None
                if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
                    raise ValueError
                confirmed = self.server.confirm_bed_clear(endpoint, token)
            except ValueError:
                self.reply_json({"error": "Invalid request."}, 400)
            except OSError:
                self.reply_json({"error": "Could not save the settings."}, 503)
            else:
                if confirmed:
                    self.reply_json({"confirmed": True})
                else:
                    self.reply_json({"error": "The print job changed. Refresh its status and try again."}, 409)
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
                with self.server.printer_io(endpoint):
                    status = FlashForge(endpoint, entry["code"], entry["serial"]).status(True)
                    monitor = self.server.monitors.get(endpoint)
                    if monitor is not None:
                        with monitor.state_lock:
                            if monitor.permission_denied:
                                monitor.permission_denied = False
                                monitor.next_due = 0.0
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

    def upload_file(self, endpoint):
        with self.server.upload_gate:
            lock = self.server.file_transfer_locks.setdefault(endpoint, threading.Lock())
        if not lock.acquire(blocking=False):
            self.reply_json({"error": "A file transfer is already in progress."}, 409)
            return
        try:
            try:
                name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}", name) or not name.lower().endswith((".gcode", ".3mf")):
                    raise ValueError("Use a .gcode or sliced .3mf name with A–Z, digits, spaces, dots, underscores or hyphens.")
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_FILE_UPLOAD:
                    raise ValueError("Choose a print file no larger than 128 MB.")
                self.connection.settimeout(15)
                with tempfile.TemporaryFile(dir=self.server.storage) as stream:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise OSError("The local file transfer was interrupted.")
                        stream.write(chunk)
                        remaining -= len(chunk)
                    stream.seek(0)
                    if name.lower().endswith(".3mf"):
                        with zipfile.ZipFile(stream) as archive:
                            plates = [info for info in archive.infolist()
                                      if re.fullmatch(r"Metadata/plate_[1-9][0-9]*\.gcode", info.filename)]
                            if len(plates) != 1 or not plates[0].file_size:
                                raise ValueError("Choose a sliced .3mf with one plate.")
                        stream.seek(0)
                    elif b"\0" in stream.read(8192):
                        raise ValueError("Choose a text .gcode file or a sliced .3mf.")
                    stream.seek(0)
                    state = self.save_uploaded_file(endpoint, name, stream, length)
            except UploadInProgress:
                self.reply_json({"error": "A file transfer is already in progress. Refresh before retrying."}, 409)
            except (ValueError, zipfile.BadZipFile) as error:
                self.reply_json({"error": str(error)}, 400)
            except (OSError, http.client.HTTPException):
                self.reply_json({"error": "Could not confirm the upload. Refresh Printer Files before retrying."}, 503)
            else:
                self.reply_json({"file": name, "state": state})
        finally:
            lock.release()

    def save_uploaded_file(self, endpoint, name, stream, length):
        def check_available():
            if self.server.upload_active() or self.server.stopping.is_set():
                raise UploadInProgress

        with self.server.printer_io(endpoint):
            monitor = self.server.monitors.get(endpoint)
            entry = next((item for item in self.server.printers() if item["host"] == endpoint), None)
            if not monitor or not entry or not entry["code"]:
                raise ValueError("Connect an Adventurer 5M with its CheckCode first.")
            adapter = monitor.adapter
            adapter.configure(entry["code"], entry["serial"])
            status = adapter.status(True)
            serial = status["info"].get("serial")
            if (manual_family(status["info"].get("model")) != "5m"
                    or "pro" in status["info"].get("model", "").casefold()):
                raise ValueError("File upload is available on Adventurer 5M.")
            if not serial or not status.get("jobControls") or (entry["serial"] and entry["serial"] != serial):
                raise ValueError("Check the printer serial number and CheckCode.")
            if status["state"] not in {"READY", "IDLE"} or status["active"]:
                raise ValueError("The printer must be idle to upload a file.")
            detail = printer_api(endpoint, serial, entry["code"], "/detail").get("detail", {})
            if (not isinstance(detail, dict) or str(detail.get("status", "")).upper() not in {"READY", "IDLE"}
                    or detail.get("errorCode")):
                raise ValueError("The printer must be idle and free of errors to upload a file.")
            previous = monitor.update_file_catalog(adapter.files())
            if any(name.casefold() == file[0].casefold() for file in previous.values()):
                raise ValueError("A file with this name already exists. Choose a different name.")
            upload_flashforge_file(endpoint, serial, entry["code"], name, stream, length, check_available)
            for attempt in range(5):
                if attempt:
                    time.sleep(0.75)
                check_available()
                files = monitor.update_file_catalog(adapter.files())
                status = adapter.status(True)
                if status["active"]:
                    raise ValueError("The printer reported an active print. Check the printer before retrying.")
                if any(file[0] == name for file in files.values()) and status["state"] in {"READY", "IDLE"}:
                    self.server.file_catalog_revision += 1
                    monitor.next_due = 0
                    return status["state"]
            raise ValueError("The printer accepted the transfer, but the file and idle state could not both be verified.")

    def test_printer(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 1024:
                raise ValueError("Invalid printer entry.")
            host, code, serial = printer_credentials(json.loads(self.rfile.read(length)))
            with self.server.printer_io(host):
                status = FlashForge(host, code, serial).status(True)
            if code and status.get("transport") == "TCP":
                raise ValueError("Basic readings received. CheckCode could not be verified.")
            self.reply_json({"model": status["info"].get("model", ""), "transport": status["transport"]})
        except (ValueError, UnicodeError, RecursionError) as error:
            self.reply_json({"error": str(error)}, 400)
        except (OSError, UploadInProgress):
            self.reply_json({"error": "Could not check this printer. Check the address and network permissions."}, 503)

    def prepare_print_source(self, endpoint):
        if not self.server.source_transfer.acquire(blocking=False):
            self.reply_json({"error": "Another print source is being read."}, 409)
            return
        try:
            query = parse_qs(urlsplit(self.path).query)
            file_id, = query.get("file", [""])
            name, = query.get("name", [""])
            monitor = self.server.monitors.get(endpoint)
            if not monitor or not re.fullmatch(r"[0-9a-f]{32}", file_id):
                raise ValueError("Select a stored file first.")
            with monitor.state_lock:
                status = monitor.latest or {}
                stored = monitor.files.get(file_id)
            serial = status.get("info", {}).get("serial")
            if manual_family(status.get("info", {}).get("model")) != "creator" or not serial or not stored:
                raise ValueError("Refresh the Creator printer's file list first.")
            if not stored[0].lower().endswith(".3mf"):
                raise ValueError("Creator repeat printing requires a .3mf.")
            generation = self.server.upload_generation
            if name:
                if (len(name) > 255 or any(c in name for c in "/\\\0\r\n") or not name.lower().endswith(".3mf")
                        or print_source_name(name) != print_source_name(stored[0])):
                    raise ValueError("Choose the sliced .3mf that matches the stored file name.")
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_PRINT_SOURCE:
                    raise ValueError("Choose a .3mf no larger than 512 MB.")
                self.connection.settimeout(10)
                with tempfile.TemporaryFile(dir=self.server.storage) as stream:
                    while length:
                        chunk = self.rfile.read(min(length, 1024 * 1024))
                        if not chunk:
                            raise ValueError("The .3mf transfer was interrupted.")
                        stream.write(chunk)
                        length -= len(chunk)
                    requirements = source_requirements(stream)
            else:
                with self.server.source_lock:
                    saved = self.server.sources.get(serial + ":" + file_id, {})
                requirements = saved.get("requirements")
                if saved.get("name") != stored[0] or not isinstance(requirements, list) or not requirements:
                    raise ValueError("Choose the original sliced .3mf.")
            with self.server.printer_io(endpoint):
                if generation != self.server.upload_generation or monitor.adapter.files().get(file_id) != stored:
                    raise ValueError("The file list changed. Refresh Printer Files.")
                preview = preview_digest(monitor.adapter, stored[1])
                if not name and (not preview or preview != saved.get("preview")):
                    raise ValueError("The saved preview changed. Choose the original sliced .3mf.")
                source = {"token": secrets.token_hex(16), "file_id": file_id, "name": stored[0], "serial": serial,
                          "requirements": requirements, "preview": preview, "generation": generation,
                          "expires": time.monotonic() + 600}
                monitor.print_source = source
            self.reply_json({"token": source["token"], "requirements": requirements})
        except (ValueError, UnicodeError, RecursionError) as error:
            self.reply_json({"error": str(error)}, 400)
        except UploadInProgress:
            self.reply_json({"error": "Wait until OrcaSlicer finishes sending the print job."}, 409)
        except OSError:
            self.reply_json({"error": "Could not read this print source."}, 503)
        finally:
            self.server.source_transfer.release()
            self.close_connection = True

    def stream_camera(self, endpoint):
        monitor = self.server.monitors.get(endpoint)
        entry = next((p for p in self.server.printers() if p["host"] == endpoint), None)
        if not monitor or not entry or not entry["code"] or not entry["serial"]:
            self.reply_json({"error": "Save the printer serial and CheckCode first."}, 409)
            return
        with monitor.state_lock:
            status = monitor.latest or {}
        if not status.get("managedCamera"):
            self.reply_json({"error": "Camera unavailable."}, 409)
            return
        if not monitor.camera_lock.acquire(timeout=6):
            self.reply_json({"error": "Camera is in use. Close the other view and reconnect."}, 409)
            return

        def disconnected(timeout=0):
            return (self.server.stopping.is_set() or self.server.monitors.get(endpoint) is not monitor
                    or bool(select.select([self.connection], [], [], timeout)[0]))

        started = False
        try:
            parts = urlsplit(status.get("cameraUrl") or "/?action=stream")
            path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
            # Creator firmware serves MJPEG directly, without opening it via /control.
            for attempt in range(4):
                if disconnected(0.35 if attempt else 0):
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
        except (ValueError, OSError, http.client.HTTPException):
            if not started and not disconnected():
                self.reply_json({"error": "No camera frame received. Connect to try again."}, 503)
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
            if command in ("pause", "resume", "cancel"):
                job = payload.get("job")
                if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f]{32}", job):
                    raise ValueError("The print job changed. Refresh its status and try again.")
                state = act(endpoint, command, expected_job=job)
            else:
                state = act(endpoint, str(command or ""))
        except (TimeoutError, ConnectionError) as error:
            self.reply_json({"error": str(error) or "Printer did not respond.", "code": "no_response"}, 503)
            return
        except ManualHomeRequired as error:
            self.reply_json({"error": str(error), "code": "home_required"}, 409)
            return
        except ManualTravelLimit as error:
            self.reply_json({"error": str(error), "code": "travel_limit"}, 409)
            return
        except ManualAxesBusy as error:
            self.reply_json({"error": str(error), "code": "axes_busy"}, 409)
            return
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
            if "placement" in payload or "first" in payload:
                if ("placement" in payload and payload["placement"] not in ("device", "flashforge")
                        or "first" in payload and type(payload["first"]) is not bool
                        or "printers" in payload and not isinstance(payload["printers"], list)
                        or payload.get("first") and not payload.get("printers")):
                    raise ValueError("Invalid request.")
                endpoint = apply(payload.get("printers"), placement=payload.get("placement"),
                                 first=payload.get("first", False))
            else:
                if not isinstance(payload.get("printers"), list):
                    raise ValueError("Invalid printer list.")
                endpoint = apply(payload["printers"])
        except ValueError as error:
            self.reply_json({"error": str(error)}, 400)
            return
        except (OSError, RuntimeError) as error:
            self.reply_json({"error": str(error)}, 409)
            return
        self.reply_json({"endpoint": endpoint, "moved": endpoint != previous,
                         "placement": self.server.placement,
                         "savedPlacement": normalize_placement(self.server.get_config().get("placement"),
                                                               self.server.placement),
                         "setupHost": self.server.setup_profile() if self.server.setup_profile else ""})

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
            page = render_page(PAGE, self.server.language_for(self.headers.get("User-Agent", "")), printer=MONITOR_TITLE)
            self.reply(page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            self.send_error(403)
            return
        if route == "settings":
            self.reply_json(self.server.settings_payload())
            return
        if route == "history":
            self.reply_json(self.server.history_payload())
            return
        if route == "placement":
            self.reply_json({"placement": self.server.placement})
            return
        if route == "native":
            try:
                url = self.server.native_page(endpoint) if self.server.native_page and endpoint not in ("flashforge", SETUP_PAGE) else ""
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
                    language = self.server.language_for(self.headers.get("User-Agent", ""))
                    self.reply_json({"temperatures": [], "history": [], "historyError": "",
                                     "cameraUrl": "", "printers": self.server.fleet_payload(endpoint),
                                     "language": language})
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
                data["language"] = self.server.language_for(self.headers.get("User-Agent", ""))
                self.reply_json(data)
                return
            if uploading:
                raise UploadInProgress
            with monitor.state_lock:
                data = monitor.latest
                error = monitor.error
                calibration = monitor.calibration
                if route == "status" and data is not None:
                    data = {**data, "manual": monitor.manual_controls_view(data.get("manual"), data),
                            "lastCompleted": last_completed_job(monitor.history_snapshot)}
            if data is None:
                self.reply_json({"error": error}, 503)
                return
            if route == "status":
                self.reply_json({**data, "calibration": calibration, "fileCatalogRevision": self.server.file_catalog_revision})
                return
            if route == "files":
                with self.server.printer_io(endpoint):
                    files = monitor.update_file_catalog(monitor.adapter.files())
                    revision = self.server.file_catalog_revision
                family = manual_family(data["info"].get("model"))
                serial = data["info"].get("serial") or ""
                with self.server.source_lock:
                    entries = [{"id": key, "name": name,
                                "savedSource": family == "creator" and serial + ":" + key in self.server.sources}
                               for key, (name, _) in files.items()]
                model = data["info"].get("model", "")
                upload = family == "5m" and "pro" not in model.casefold() and bool(data.get("jobControls"))
                self.reply_json({"files": entries, "revision": revision,
                                 "printOptions": {"upload": upload, "family": family, "http": family not in {"3", "4"},
                                                  "printer": endpoint, "slots": data.get("materials") or []}})
                return
            if route == "snapshot":
                if data.get("managedCamera"):
                    self.reply_json({"error": "Save a frame from the active camera view."}, 409)
                    return
                camera = monitor.camera_url
                if not camera:
                    self.reply_json({"error": "This printer has no camera."}, 404)
                    return
                with self.server.printer_io(endpoint):
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
                    with self.server.printer_io(endpoint):
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
        key = "error" if status >= 400 else "timeHint"
        if data.get(key):
            language = self.server.language_for(self.headers.get("User-Agent", ""))
            data = {**data, key: translate_error(language, data[key])}
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
    _setup_window = None
    instance = None
    active_placement = DEFAULT_PLACEMENT
    host_language = "en"

    def get_name(self):
        return 'FlashForge Device Tab'

    def get_default_config(self):
        return {"printers": [], "flashforge_host": "", "check_code": "", "serial_number": "",
                "placement": self.active_placement, "language": ""}

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        language = self.interface_language()
        options = "".join(f'<option value="{code}" lang="{code}">{escape(name)}</option>' for code, name in LANGUAGES.items())
        return render_page(CONFIG_PAGE, language, placement=self.active_placement, languageOptions=options)

    def refresh_host_language(self):
        # OrcaSlicer may load the plugin on a worker, while its older app_info
        # API accepts only the UI thread. Cache the last successful UI read.
        language = orca_language("")
        if language:
            self.host_language = language
            server = self._server
            if server is not None:
                server.host_language = language
        return self.host_language

    def interface_language(self):
        host_language = self.refresh_host_language()
        if self._server is not None:
            return self._server.language_for()
        return self.config()["language"] or host_language

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
        result["placement"] = normalize_placement(config.get("placement"), self.active_placement)
        result["language"] = normalize_language(config.get("language"), "")
        return result

    def on_load(self):
        # Bind during the lifecycle callback so OrcaSlicer reports startup
        # errors in plugin diagnostics instead of losing them in a worker.
        storage = Path(orca.host.plugin.storage())
        self.refresh_host_language()
        self._page_store = storage / "device-pages.json"
        self._switch_lock = threading.Lock()
        self._placement_lock = threading.Lock()
        self._next_placement = None
        self._stopping = threading.Event()
        self._view_refresh = threading.Event()
        try:
            server = LoopbackServer((WEB_HOST, WEB_PORT), MonitorHandler)
        except OSError as error:
            # Windows also reports an exclusively bound or reserved port as WSAEACCES.
            if error.errno != errno.EADDRINUSE and getattr(error, "winerror", None) not in (10013, 10048):
                raise
            message = translate(self.host_language, MONITOR_BIND_ERROR)
            raise RuntimeError(message.format(address=f"{WEB_HOST}:{WEB_PORT}")) from error
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                                  name='FlashForge Device Tab HTTP', daemon=True)
        try:
            server.configure(self.config, storage)
            server.host_language = self.host_language
            server.placement = self.active_placement
            server.native_page = self.native_page
            server.act = self.act
            server.notify = self.notify
            server.check_view = self.check_view
            server.setup_profile = self.setup_profile
            server.apply_settings = self.apply_settings
            worker.start()
        except Exception:
            server.server_close()
            raise
        self._server = server
        for port in LEGACY_WEB_PORTS:
            try:
                alias = LoopbackHTTPServer((WEB_HOST, port), AliasRedirectHandler)
            except OSError:
                continue
            threading.Thread(target=alias.serve_forever, kwargs={'poll_interval': 0.5},
                             name=f'FlashForge Device Tab alias {port}', daemon=True).start()
            self._aliases = getattr(self, "_aliases", [])
            self._aliases.append(alias)
        FlashForgeService.instance = self
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
        if not is_flashforge_profile(preset):
            return
        print_host = preset.config_value("print_host") or ""
        current_url = preset.config_value(DEVICE_UI_KEY) or ""
        if self.active_placement == "device":
            target = self.device_monitor_url(print_host)
            if not target or current_url == target:
                return
        elif monitor_identity(current_url) is None:
            return
        expected = (preset.name, print_host, current_url)
        self.switch_view(expected_profile=expected)

    def device_monitor_url(self, print_host):
        printers = self._server.printers()
        if not printers:
            return monitor_url(SETUP_PAGE)
        endpoint = printer_endpoint(print_host) if print_host else printers[0]["host"]
        return monitor_url(endpoint) if any(item["host"] == endpoint for item in printers) else None

    def setup_profile(self):
        try:
            preset = orca.host.preset_bundle().current_printer_preset()
            if is_flashforge_profile(preset):
                print_host = preset.config_value("print_host") or ""
                if print_host:
                    return printer_endpoint(print_host)
                if self.active_placement == "device":
                    return monitor_identity(self.device_monitor_url("") or "") or ""
        except (AttributeError, OSError, RuntimeError, ValueError):
            pass
        return ""

    def write_next_placement(self, placement):
        if placement == self._next_placement:
            return
        # Registration can run before the host exposes plugin storage. Keep a
        # copy next to the script as well as in the persistent plugin directory.
        for path in dict.fromkeys((self._page_store.with_name(PLACEMENT_FILE.name), PLACEMENT_FILE)):
            staged = path.with_suffix(".tmp")
            staged.write_text(placement, encoding="ascii")
            os.replace(staged, path)
        self._next_placement = placement

    def sync_next_placement(self):
        # The host's settings iframe only writes capability config. Mirror the
        # saved preference so it is available before the next registration.
        try:
            with self._placement_lock:
                self.write_next_placement(self.config()["placement"])
        except (OSError, RuntimeError):
            # A later check or shutdown can retry a transient host/write failure.
            pass

    def on_unload(self):
        if self._server is None:
            return
        self._stopping.set()
        self._view_refresh.set()
        self.sync_next_placement()
        # Do not join the page worker here: it may be waiting for the UI thread
        # that is unloading us. It checks _stopping before committing a change.
        for alias in getattr(self, "_aliases", []):
            alias.shutdown()
            alias.server_close()
        self._aliases = []
        self._server.shutdown()
        self._server.server_close()
        self._server = None

    def on_lifecycle_event(self, event, context):
        server = self._server
        if server is None:
            return
        if event == orca.LifecycleEvent.UploadStarted:
            server.begin_upload(getattr(context, "name", ""))
        elif event == orca.LifecycleEvent.UploadFinished:
            server.finish_upload(getattr(context, "name", ""))
        elif event in (orca.LifecycleEvent.PresetSelected, orca.LifecycleEvent.ProjectOpened,
                       orca.LifecycleEvent.NewProject):
            self.refresh_host_language()
            self._view_refresh.set()

    def check_view(self, endpoint):
        preset = orca.host.preset_bundle().current_printer_preset()
        return self.check_profile_view(preset, endpoint)

    def check_profile_view(self, preset, endpoint):
        if self.active_placement != "device":
            raise ValueError("Open the FlashForge tab to see the monitor.")
        if not is_flashforge_profile(preset):
            raise ValueError("Open FlashForge Device Tab for a FlashForge printer profile from Speed Dial.")
        if monitor_identity(preset.config_value(DEVICE_UI_KEY) or "") != endpoint:
            raise ValueError("Open FlashForge Device Tab for the current printer profile from Speed Dial.")
        print_host = preset.config_value("print_host") or ""
        current = printer_endpoint(print_host) if print_host else endpoint
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
            if not is_flashforge_profile(preset):
                raise ValueError("Select a printer profile that uses the FlashForge host.")
            current_url = preset.config_value(DEVICE_UI_KEY) or ""
            print_host = preset.config_value("print_host") or ""
            if expected_profile is not None and expected_profile != (preset.name, print_host, current_url):
                raise ValueError("The active printer profile changed. Try again.")
            current_view = monitor_identity(current_url)
            show_monitor = self.active_placement == "device"
            target = self.device_monitor_url(print_host) if show_monitor else None
            if show_monitor and target is None:
                raise ValueError("The printer address in the plugin settings does not match the active profile.")
            if (show_monitor and current_url == target
                    or not show_monitor and current_view is None):
                return ("FlashForge Device Tab is in the Printer tab." if show_monitor
                        else "Open the FlashForge tab to see the monitor.")
            transaction = orca.host.preset.printer.begin_update()
            edited = presets.edited_preset()
            if (edited.name != transaction.target_name or edited.name != preset.name
                    or not is_flashforge_profile(edited)
                    or edited.config_value("print_host") != print_host
                    or (edited.config_value(DEVICE_UI_KEY) or "") != current_url):
                raise ValueError("The active printer profile changed. Try again.")
            if not show_monitor:
                fallback = presets.selected_preset().config_value(DEVICE_UI_KEY) or ""
                target = self.original_page(preset.name, fallback)
                message = "Open the FlashForge tab to see the monitor."
            else:
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
                message = "Opened FlashForge Device Tab in the Printer tab."

            if self._stopping.is_set():
                raise ValueError("FlashForge Device Tab is shutting down.")
            transaction.set(DEVICE_UI_KEY, target)
            transaction.commit()
            return message
        finally:
            self._switch_lock.release()

    def apply_settings(self, printers=None, *, placement=None, first=False):
        if printers is not None and not isinstance(printers, list):
            raise ValueError("Invalid printer list.")
        clean = []
        for entry in printers or []:
            host, code, serial = printer_credentials(entry)
            if any(item["host"] == host for item in clean):
                continue
            clean.append({"host": host, "code": code,
                          "name": str(entry.get("name") or host)[:60]})
            if serial:
                clean[-1]["serial"] = serial
        if printers is not None and not clean:
            raise ValueError("Add at least one printer address.")
        with self._placement_lock:
            previous_host = self._server.settings_for()[0]
            config = self.config()
            retry_first = first and bool(previous_host)
            if retry_first:
                # A previous attempt can save config before a later write fails.
                # Retry the same setup only while its previous result still matches.
                if (config.get("printers") != clean or config.get("placement") != placement):
                    raise RuntimeError("A printer is already configured.")
            if placement is not None:
                config["placement"] = placement
            host = previous_host
            if clean:
                host = clean[0]["host"]
                config.update(printers=clean, flashforge_host=host, check_code=clean[0]["code"],
                              serial_number=clean[0].get("serial", ""))
            if not retry_first:
                self.save_config(json.dumps(config, ensure_ascii=False))
            if placement is not None:
                self.write_next_placement(placement)
        if self.active_placement == "device" and (host != previous_host or first):
            self._point_device_tab(host)
        self._view_refresh.set()
        return host

    def _point_device_tab(self, host):
        """Keep the Device tab pointed at the first configured printer."""
        try:
            preset = orca.host.preset_bundle().current_printer_preset()
            if preset.config_value("host_type") != "flashforge":
                return
            print_host = preset.config_value("print_host") or ""
            current = printer_endpoint(print_host) if print_host else ""
            if current == host:
                return
            transaction = orca.host.preset.printer.begin_update()
            edited = orca.host.preset_bundle().printers.edited_preset()
            if (edited.name != transaction.target_name or edited.name != preset.name
                    or edited.config_value("host_type") != "flashforge"
                    or (edited.config_value("print_host") or "") != print_host):
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
        language = self._server.language_for() if self._server is not None else self.host_language
        name = getattr(monitor, "name", "") or translate(language, "Printer")
        message = translate(language, message)
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

    def set_accessory(self, monitor, entry, light, slot):
        current = monitor.adapter.status(include_details=True)
        serial = current["info"].get("serial")
        host, code = entry["host"], entry["code"]
        if not serial or not code or (entry["serial"] and entry["serial"] != serial):
            raise ValueError("Check the printer serial number and CheckCode.")
        if light:
            if current.get("light") is None:
                raise ValueError("Light control is unavailable on this printer.")
            enabled = light[1] == "on"
            if current["light"] is enabled:
                return "On" if enabled else "Off"
            command, args = "lightControl_cmd", {"status": "open" if enabled else "close"}
        else:
            slot_id, material, color = int(slot[1]), slot[2], slot[3]
            editor = current.get("slotEditor")
            if not editor or current["active"]:
                raise ValueError("Slots can be edited while the printer and material station are idle.")
            if material not in editor["materials"] or color not in editor["colors"]:
                raise ValueError("Choose a material and color supported by this printer.")
            if not any(item["id"] == slot_id and "present" in item for item in current["materials"]):
                raise ValueError("The printer did not report this slot.")
            creator = editor["colors"] == CREATOR_COLORS
            command = "msConfig_cmd"
            args = {"slot": slot_id, "mt": material, "rgb": "#" + color if creator else color}
        if self._server.upload_active() or self._server.stopping.is_set():
            raise UploadInProgress
        printer_api(host, serial, code, "/control", {"payload": {"cmd": command, "args": args}})
        monitor.next_due = 0
        for attempt in range(3):
            if attempt and self._server.stopping.wait(0.25):
                break
            if self._server.upload_active() or self._server.stopping.is_set():
                break
            detail = read_http_detail(host, serial, code)
            slots = material_slots(detail)
            if light and chamber_light(detail) is enabled:
                with monitor.state_lock:
                    if monitor.latest:
                        monitor.latest = {**monitor.latest, "light": enabled,
                                          "sensors": {**monitor.latest.get("sensors", {}), "led": enabled}}
                return "On" if enabled else "Off"
            if slot and any(item["id"] == slot_id and item.get("name") == material
                            and item.get("color", "").upper() == "#" + color for item in slots):
                with monitor.state_lock:
                    if monitor.latest:
                        monitor.latest = {**monitor.latest, "materials": slots}
                return "Slot updated."
        raise ValueError("The printer did not confirm the change.")

    def start_stored_file(self, monitor, entry, file_id, level, slots, source_token=None):
        current = monitor.adapter.status(include_details=True)
        serial = current["info"].get("serial")
        family = manual_family(current["info"].get("model"))
        if not family or (entry["serial"] and entry["serial"] != serial):
            raise ValueError("Check the selected printer.")
        if current["state"] not in {"READY", "IDLE"} or current["active"]:
            raise ValueError("The printer must be idle.")
        if time.monotonic() < getattr(monitor, "print_request_until", 0):
            raise ValueError("A print request was already sent. Check the printer status.")
        item = monitor.adapter.files().get(file_id)
        if not item or item != monitor.files.get(file_id):
            raise ValueError("The file list changed. Refresh Printer Files.")
        name, path = item
        if not name or name in {".", ".."} or any(ord(c) < 32 or c in "/\\" for c in name):
            raise ValueError("Invalid printer file name.")
        if self._server.upload_active() or self._server.stopping.is_set():
            raise UploadInProgress
        if family in {"3", "4"}:
            if slots or level != "0" or not isinstance(path, bytes):
                raise ValueError("Invalid TCP print options.")
            with socket.create_connection((entry["host"], PORT), timeout=3) as sock:
                if "control success" not in command(sock, "M601 S1").lower():
                    raise ValueError("Printer denied control.")
                try:
                    if not re.search(r"^MachineStatus:[ \t]*(READY|IDLE)[ \t]*\r?$", command(sock, "M119"), re.M | re.I):
                        raise ValueError("The printer must be idle.")
                    raw_name = path.rsplit(b"/", 1)[-1]
                    monitor.print_request_until = time.monotonic() + 60
                    reply, _ = command_reply(sock, b"M23 0:/user/" + raw_name)
                    if re.search(rb"error|failed|not exist|not found|denied", reply, re.I):
                        raise ValueError("Printer rejected the selected file.")
                finally:
                    command(sock, "M602")
        else:
            if not serial or not entry["code"]:
                raise ValueError("Configure the printer serial number and CheckCode.")
            fields = {"fileName": name, "levelingBeforePrint": level == "1"}
            source = None
            if family in {"ad5x", "creator"}:
                available = {s["id"]: s for s in current.get("materials", [])}
                catalog = printer_api(entry["host"], serial, entry["code"], "/gcodeList", limit=1024 * 1024)
                details = next((row for row in catalog.get("gcodeListDetail", [])
                                if isinstance(row, dict) and row.get("gcodeFileName") == name), {})
                requirements = {r["toolId"]: r for r in details.get("gcodeToolDatas", [])
                                if isinstance(r, dict) and type(r.get("toolId")) is int}
                if family == "creator":
                    source = getattr(monitor, "print_source", None)
                    if (not name.lower().endswith(".3mf") or not source or not source_token
                            or source_token != source["token"] or source["file_id"] != file_id or source["serial"] != serial
                            or source["generation"] != self._server.upload_generation
                            or time.monotonic() >= source["expires"]):
                        raise ValueError("Choose the original sliced .3mf for this Creator file.")
                    if source["preview"] and preview_digest(monitor.adapter, path) != source["preview"]:
                        raise ValueError("The file preview changed. Choose the original sliced .3mf again.")
                    requirements = {r["toolId"]: r for r in source["requirements"]}
                selected = {i for i, digit in enumerate(slots) if digit != "0"}
                if requirements and (available or family == "creator") and selected != requirements.keys():
                    numbers = ", ".join(str(i + 1) for i in sorted(requirements))
                    raise ValueError("Select slots for these file filaments: " + numbers)
                if not selected and (len(requirements) > 1 or details.get("gcodeToolCnt", 0) > 1):
                    raise ValueError("This file requires multiple filaments.")
                mappings = []
                for tool, digit in enumerate(slots):
                    if digit == "0":
                        continue
                    slot = available.get(int(digit))
                    if not slot or slot.get("present") is False or not slot.get("name") or not slot.get("color"):
                        raise ValueError("Refresh the file list and check the loaded materials.")
                    requirement = requirements.get(tool, {})
                    if requirement.get("materialName") and requirement["materialName"].casefold() != slot["name"].casefold():
                        raise ValueError(f"Filament {tool + 1} requires {requirement['materialName']}.")
                    mappings.append({"toolId": tool, "slotId": int(digit), "materialName": slot["name"],
                                     "slotMaterialColor": slot["color"], "toolMaterialColor": requirement.get("materialColor") or slot["color"]})
                if (available or family == "creator") and not mappings:
                    raise ValueError("Select the material slots for this file.")
                fields.update(materialMappings=mappings, flowCalibration=False, timeLapseVideo=False)
                if family == "ad5x":
                    fields.update(useMatlStation=bool(mappings), gcodeToolCnt=len(mappings) or 1)
            if self._server.upload_active() or self._server.stopping.is_set():
                raise UploadInProgress
            monitor.print_request_until = time.monotonic() + 60
            printer_api(entry["host"], serial, entry["code"], "/printGcode", fields)
            if source:
                source["expires"] = time.monotonic() + 90
                monitor.pending_print_source = source
                monitor.print_source = None
        monitor.next_due = 0
        return "Print requested."

    def act(self, host, action, expected_file=None, *, expected_job=None):
        target = self.ACTIONS.get(action)
        manual = action.startswith("manual/")
        stored = re.fullmatch(r"print/([0-9a-f]{32})/([01])/([0-4]{4}|)(?:/([0-9a-f]{32}))?", action)
        light = re.fullmatch(r"light/(on|off)", action)
        slot = re.fullmatch(r"slot/([1-4])/([A-Za-z0-9-]{1,16})/([0-9A-F]{6})", action)
        if target is None and not (manual or stored or light or slot):
            raise ValueError("Unknown printer command.")
        monitor = self._server.monitors.get(host)
        if monitor is None:
            raise ValueError("The printer is not connected yet.")
        try:
            with self._server.printer_io(host):
                entry = next((item for item in self._server.printers() if item["host"] == host), None)
                if entry is None:
                    raise ValueError("This printer is not in the plugin settings.")
                if self._server.monitors.get(host) is not monitor:
                    raise ValueError("The printer is not connected yet.")
                monitor.adapter.configure(entry["code"], entry["serial"])
                if light or slot:
                    return self.set_accessory(monitor, entry, light, slot)
                if stored:
                    return self.start_stored_file(monitor, entry, *stored.groups())
                if manual:
                    current = monitor.adapter.status(include_details=True)
                    key = action[7:]
                    with monitor.state_lock:
                        monitor.bind_manual_printer(current)
                        controls = monitor.manual_controls_view(current.get("manual"), current)
                        settled_homing = monitor.settled_homing_position(current, for_home=key == "home")
                    jog = re.fullmatch(r"[xyze][+-](?:1|10)", key)
                    if not controls or key not in controls["allowed"]:
                        if controls and controls.get("released") and jog and key[0] in "xyz":
                            raise ManualHomeRequired("Press Home before moving the axes.")
                        if controls and controls["description"].startswith("Motion · ") and (key == "home" or jog):
                            raise ManualAxesBusy("Printer must be idle. " + controls["description"].split(" · ", 1)[1])
                        raise ValueError("Command unavailable for the current printer state.")
                    if controls["tcpMotion"] and (key in {"home", "release"} or key.startswith("heat") or jog):
                        if entry["serial"] and entry["serial"] != current["info"].get("serial"):
                            raise ValueError("Check the printer serial number.")
                        if key == "release" and monitor.manual_release_marker is None:
                            raise ValueError("Check the printer serial number.")
                        if self._server.upload_active() or self._server.stopping.is_set():
                            raise UploadInProgress
                        info = current["info"]
                        may_move = key in {"home", "release"} or bool(jog and key[0] in "xyz" and settled_homing)
                        track_home = monitor.track_manual_home if key == "home" and controls.get("released") else None
                        if track_home:
                            monitor.homing_legacy = monitor.legacy_home_fingerprint()
                        try:
                            result = tcp_manual_move(host, key, info.get("volume"), info.get("model", ""),
                                                     monitor.manual_release_marker, settled_homing, track_home,
                                                     monitor.record_manual_target,
                                                     require_idle_axes=current["state"] in CANCELLED_JOB)
                        except (ManualAxesBusy, ManualTravelLimit):
                            may_move = False
                            raise
                        finally:
                            if may_move:
                                with monitor.state_lock:
                                    monitor.homing_park_observation = None
                                    if key in {"home", "release"}:
                                        monitor.expected_manual_position = None
                        if key == "release":
                            return "Motor release sent."
                        if track_home:
                            return "Home sent. Waiting for the printer to confirm completion."
                        return result
                    serial = current["info"].get("serial")
                    if not serial or not entry["code"] or (entry["serial"] and entry["serial"] != serial):
                        raise ValueError("Check the printer serial number and CheckCode.")
                    payload = manual_payload(key, controls)
                    position_unavailable = False
                    if payload["cmd"] == "moveCtrl_cmd":
                        position = ad5x_axis_position(host, key[0].upper())
                        position_unavailable = position is None
                        if not position_unavailable:
                            checked_manual_target(key, position, current["info"].get("volume"), "ad5x")
                    if key in ("level", "shaper"):
                        with monitor.state_lock:
                            if monitor.calibration and monitor.calibration["phase"] == "waiting":
                                raise ValueError("Waiting for the printer to confirm calibration.")
                    if self._server.upload_active() or self._server.stopping.is_set():
                        raise UploadInProgress
                    printer_api(host, serial, entry["code"], "/control", {"payload": payload})
                    if key in ("level", "shaper"):
                        with monitor.state_lock:
                            monitor.calibration = {"phase": "waiting", "key": key}
                            monitor.calibration_deadline = int(time.time()) + 25
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
                    if position_unavailable:
                        return "Printer accepted the command. Axis limits could not be checked."
                    return "Printer accepted the command."
                with monitor.state_lock:
                    previous = monitor.latest
                    if expected_job is not None and (previous or {}).get("jobToken") != expected_job:
                        raise ValueError("The print job changed. Refresh its status and try again.")
                data = monitor.adapter.status(include_details=True)
                if not same_observed_job(previous, data):
                    with monitor.state_lock:
                        if previous is not None:
                            monitor.latest = {**previous, "jobToken": None}
                        monitor.next_due = 0
                    raise ValueError("The print job changed. Refresh its status and try again.")
                state = data.get("state")
                wrong_state = (action == "resume" and state != "PAUSED"
                               or action == "pause" and state in {"PAUSED", "PAUSING"})
                if (not data.get("jobControls") or wrong_state
                        or (expected_file is not None and data.get("file") != expected_file)):
                    monitor.next_due = 0
                    raise ValueError("The print job changed. Refresh its status and try again.")
                if self._server.upload_active() or self._server.stopping.is_set():
                    raise UploadInProgress
                serial = ((data or {}).get("info") or {}).get("serial") or ""
                if not serial:
                    raise ValueError("The printer did not report its serial number.")
                pinned_serial = monitor.adapter.http_only or bool(entry["serial"])
                if pinned_serial and serial != entry["serial"]:
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
        language = self.interface_language()
        try:
            preset = orca.host.preset_bundle().current_printer_preset()
            if self._server is not None and not self._server.printers() and (
                    self.active_placement == "flashforge" or not is_flashforge_profile(preset)):
                if self._setup_window is None or not self._setup_window.is_open():
                    url = monitor_url(SETUP_PAGE) + "?wizard=1"
                    html = f'<script>location.replace({json.dumps(url)})</script>'
                    self._setup_window = orca.host.ui.create_window(
                        html, translate(language, "Set up your first printer"), width=560, height=640)
                message = "Set up your first printer"
            else:
                message = self.switch_view()
        except (OSError, RuntimeError, ValueError) as error:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, translate_error(language, str(error)))
        return orca.ExecutionResult.success(translate(language, message))


class FlashForgePage(orca.pages.PagesPluginCapabilityBase):
    def get_name(self):
        return "FlashForge"

    def get_type(self):
        return orca.PluginType.Pages

    def get_icon(self):
        # Orca requires a file path even for single-file plugins.
        service = FlashForgeService.instance
        if service is not None:
            service.refresh_host_language()
        icon = Path(orca.host.plugin.storage()) / "flashforge-tab.svg"
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
               '<g fill="none" stroke="#fff" stroke-width="1" stroke-linejoin="round">'
               '<rect x="2.5" y="2.5" width="15" height="15" rx="1"/>'
               '<path d="M3 6.5h14 M7.5 6.5v2l2.5 3 2.5-3v-2 M7 13.5h6 M5 15.5h10"/>'
               '</g></svg>')
        if not icon.is_file() or icon.read_bytes() != svg.encode("ascii"):
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_bytes(svg.encode("ascii"))
        return str(icon)

    def get_ui(self):
        url = monitor_url() + "flashforge/"
        service = FlashForgeService.instance
        language = orca_language() if service is None else service.interface_language()
        return f'''<!doctype html><html lang="{language}"><meta charset="utf-8">
<script>location.replace({json.dumps(url)})</script>
<a href="{url}">{escape(translate(language, "Open FlashForge"))}</a></html>'''


# Embedded so installing the single .py file also installs its translations.
# Each message is stored once. Tuple columns follow the corresponding locale order.
TRANSLATION_LOCALES = (
    "ca", "cs", "de", "es", "eu", "fr", "hu", "it", "ja", "ko", "lt", "nl",
    "pl", "pt-BR", "ro", "ru", "sv", "th", "tr", "uk", "vi", "zh-CN", "zh-TW",
)
FULL_TRANSLATION_LOCALES = (
    "de", "es", "fr", "it", "ja", "ko", "pl", "pt-BR", "ru", "uk", "zh-CN", "zh-TW",
)

CORE_MESSAGES = {
    "Back": (
        "Enrere", "Zpět", "Zurück", "Atrás", "Atzera", "Retour", "Vissza", "Indietro", "戻る", "뒤로", "Atgal", "Terug",
        "Wstecz", "Voltar", "Înapoi", "Назад", "Tillbaka", "ย้อนกลับ", "Geri", "Назад", "Quay lại", "上一步", "上一步",
    ),
    "Next": (
        "Següent", "Další", "Weiter", "Siguiente", "Hurrengoa", "Suivant", "Tovább", "Avanti", "次へ", "다음", "Toliau",
        "Volgende", "Dalej", "Avançar", "Înainte", "Далее", "Nästa", "ถัดไป", "İleri", "Далі", "Tiếp theo", "下一步",
        "下一步",
    ),
    "Enter IP manually": (
        "Introduïu l’IP manualment", "Zadat IP ručně", "IP manuell eingeben", "Introducir IP manualmente",
        "Sartu IPa eskuz", "Saisir l’IP manuellement", "IP-cím megadása kézzel", "Inserisci IP manualmente", "IPを手動入力",
        "IP 직접 입력", "Įvesti IP rankiniu būdu", "IP handmatig invoeren", "Wpisz adres IP ręcznie",
        "Inserir IP manualmente", "Introdu IP-ul manual", "Ввести IP вручную", "Ange IP manuellt", "ป้อน IP ด้วยตนเอง",
        "IP adresini elle gir", "Ввести IP вручну", "Nhập IP thủ công", "手动输入 IP", "手動輸入 IP",
    ),
    "Name (optional)": (
        "Nom (opcional)", "Název (nepovinný)", "Name (optional)", "Nombre (opcional)", "Izena (aukerakoa)",
        "Nom (facultatif)", "Név (opcionális)", "Nome (facoltativo)", "名前（任意）", "이름(선택 사항)", "Pavadinimas (nebūtinas)",
        "Naam (optioneel)", "Nazwa (opcjonalna)", "Nome (opcional)", "Nume (opțional)", "Имя (необязательно)",
        "Namn (valfritt)", "ชื่อ (ไม่บังคับ)", "Ad (isteğe bağlı)", "Назва (необов’язково)", "Tên (không bắt buộc)",
        "名称（可选）", "名稱（選填）",
    ),
    "Find the IP address and CheckCode in the printer’s network settings, and the serial number in its device information or on its label.": (
        "Trobareu l’IP i el CheckCode als ajustos de xarxa, i el número de sèrie a la informació o l’etiqueta de la impressora.",
        "IP adresu a CheckCode najdete v nastavení sítě tiskárny, sériové číslo v informacích o zařízení nebo na štítku.",
        "IP-Adresse und CheckCode stehen in den Netzwerkeinstellungen, die Seriennummer in den Geräteinformationen oder auf dem Typenschild.",
        "Busca la IP y el CheckCode en los ajustes de red de la impresora, y el número de serie en la información del dispositivo o en su etiqueta.",
        "IP helbidea eta CheckCodea sare-ezarpenetan daude, eta serie-zenbakia gailuaren informazioan edo etiketaren gainean.",
        "L’adresse IP et le CheckCode figurent dans les paramètres réseau, et le numéro de série dans les informations de l’imprimante ou sur son étiquette.",
        "Az IP-cím és a CheckCode a hálózati beállításokban, a sorozatszám az eszközadatoknál vagy a címkén található.",
        "Trovi IP e CheckCode nelle impostazioni di rete, e il numero di serie nelle informazioni della stampante o sull’etichetta.",
        "IPアドレスとCheckCodeはプリンターのネットワーク設定で、シリアル番号は機器情報または本体ラベルで確認できます。",
        "IP 주소와 CheckCode는 프린터의 네트워크 설정에서, 일련번호는 기기 정보 또는 라벨에서 확인하세요.",
        "IP adresą ir CheckCode rasite tinklo nustatymuose, o serijos numerį – įrenginio informacijoje arba etiketėje.",
        "Het IP-adres en de CheckCode staan bij de netwerkinstellingen, het serienummer bij de apparaatgegevens of op het label.",
        "Adres IP i CheckCode znajdziesz w ustawieniach sieci, a numer seryjny w informacjach o urządzeniu lub na etykiecie.",
        "O IP e o CheckCode estão nas configurações de rede. Veja o número de série nas informações do dispositivo ou na etiqueta.",
        "Găsești IP-ul și CheckCode-ul în setările de rețea, iar numărul de serie în informațiile imprimantei sau pe etichetă.",
        "IP-адрес и CheckCode указаны в настройках сети принтера, а серийный номер — в сведениях об устройстве или на наклейке.",
        "IP-adressen och CheckCode finns i nätverksinställningarna. Serienumret finns i enhetsinformationen eller på etiketten.",
        "ดูที่อยู่ IP และ CheckCode ในการตั้งค่าเครือข่ายของเครื่องพิมพ์ และดูหมายเลขซีเรียลในข้อมูลอุปกรณ์หรือบนฉลาก",
        "IP adresi ve CheckCode ağ ayarlarında, seri numarası ise cihaz bilgilerinde veya etikette bulunur.",
        "IP-адреса й CheckCode є в налаштуваннях мережі принтера, а серійний номер — у відомостях про пристрій або на наліпці.",
        "Xem địa chỉ IP và CheckCode trong cài đặt mạng. Xem số sê-ri trong thông tin thiết bị hoặc trên nhãn máy in.",
        "在打印机的网络设置中查看 IP 地址和 CheckCode，在设备信息或机身标签上查看序列号。", "在印表機的網路設定中查看 IP 位址和 CheckCode，在裝置資訊或機身標籤上查看序號。",
    ),
    "For Creator 5 and 5 Pro, enable LAN-only mode and enter both CheckCode and serial number.": (
        "Per a Creator 5 i 5 Pro, activeu el mode només LAN i introduïu el CheckCode i el número de sèrie.",
        "U Creator 5 a 5 Pro zapněte režim pouze LAN a zadejte CheckCode i sériové číslo.",
        "Aktivieren Sie beim Creator 5 und 5 Pro den LAN-only-Modus und geben Sie CheckCode und Seriennummer ein.",
        "Para Creator 5 y 5 Pro, activa el modo solo LAN e introduce el CheckCode y el número de serie.",
        "Creator 5 eta 5 Pro modeloetan, gaitu LAN soilik modua eta sartu CheckCodea eta serie-zenbakia.",
        "Sur Creator 5 et 5 Pro, activez le mode LAN uniquement et saisissez le CheckCode et le numéro de série.",
        "Creator 5 és 5 Pro esetén kapcsolja be a csak LAN módot, és adja meg a CheckCode-ot és a sorozatszámot.",
        "Per Creator 5 e 5 Pro abilita la modalità solo LAN e inserisci CheckCode e numero di serie.",
        "Creator 5と5 ProではLAN専用モードを有効にし、CheckCodeとシリアル番号を入力してください。",
        "Creator 5와 5 Pro는 LAN 전용 모드를 켜고 CheckCode와 일련번호를 모두 입력하세요.",
        "Creator 5 ir 5 Pro įjunkite tik LAN režimą ir įveskite CheckCode bei serijos numerį.",
        "Schakel voor Creator 5 en 5 Pro de LAN-only-modus in en voer de CheckCode en het serienummer in.",
        "W Creator 5 i 5 Pro włącz tryb tylko LAN i wpisz CheckCode oraz numer seryjny.",
        "Na Creator 5 e 5 Pro, ative o modo somente LAN e insira o CheckCode e o número de série.",
        "Pentru Creator 5 și 5 Pro, activează modul doar LAN și introdu atât CheckCode-ul, cât și numărul de serie.",
        "Для Creator 5 и 5 Pro включите режим «Только LAN» и заполните CheckCode и серийный номер.",
        "För Creator 5 och 5 Pro aktiverar du läget Endast LAN och anger både CheckCode och serienummer.",
        "สำหรับ Creator 5 และ 5 Pro ให้เปิดโหมด LAN เท่านั้นและป้อนทั้ง CheckCode และหมายเลขซีเรียล",
        "Creator 5 ve 5 Pro için yalnızca LAN modunu açın ve CheckCode ile seri numarasını girin.",
        "Для Creator 5 і 5 Pro увімкніть режим «Лише LAN» і введіть CheckCode та серійний номер.",
        "Với Creator 5 và 5 Pro, bật chế độ chỉ LAN rồi nhập cả CheckCode và số sê-ri.",
        "对于 Creator 5 和 5 Pro，请启用仅局域网模式并填写 CheckCode 和序列号。", "使用 Creator 5 和 5 Pro 時，請啟用僅區域網路模式並輸入 CheckCode 和序號。",
    ),
    MONITOR_BIND_ERROR: (
        "El port local de FlashForge ({address}) està ocupat o reservat. Tanqueu totes les finestres de l’OrcaSlicer i torneu-lo a obrir.",
        "Místní port FlashForge ({address}) se už používá nebo je rezervovaný. Zavřete všechna okna OrcaSliceru a znovu jej spusťte.",
        "Der lokale FlashForge-Port ({address}) ist belegt oder reserviert. Schließen Sie alle OrcaSlicer-Fenster und starten Sie OrcaSlicer neu.",
        "El puerto local de FlashForge ({address}) está ocupado o reservado. Cierra todas las ventanas de OrcaSlicer y vuelve a abrirlo.",
        "FlashForge-ren ataka lokala ({address}) erabilita edo erreserbatuta dago. Itxi OrcaSlicer-en leiho guztiak eta ireki berriro OrcaSlicer.",
        "Le port local de FlashForge ({address}) est déjà utilisé ou réservé. Fermez toutes les fenêtres d’OrcaSlicer, puis relancez OrcaSlicer.",
        "A FlashForge helyi portja ({address}) használatban van vagy le van foglalva. Zárja be az OrcaSlicer összes ablakát, majd indítsa újra az alkalmazást.",
        "La porta locale di FlashForge ({address}) è in uso o riservata. Chiudi tutte le finestre di OrcaSlicer e riaprilo.",
        "FlashForge のローカルポート ({address}) は使用中か予約済みです。OrcaSlicer のすべてのウィンドウを閉じてから、OrcaSlicer を起動し直してください。",
        "FlashForge의 로컬 포트({address})가 사용 중이거나 예약되어 있습니다. OrcaSlicer 창을 모두 닫고 OrcaSlicer를 다시 여세요.",
        "„FlashForge“ vietinis prievadas ({address}) yra naudojamas arba rezervuotas. Uždarykite visus „OrcaSlicer“ langus ir vėl paleiskite „OrcaSlicer“.",
        "De lokale poort van FlashForge ({address}) is in gebruik of gereserveerd. Sluit alle vensters van OrcaSlicer en open OrcaSlicer opnieuw.",
        "Lokalny port FlashForge ({address}) jest zajęty lub zarezerwowany. Zamknij wszystkie okna OrcaSlicer i uruchom program ponownie.",
        "A porta local do FlashForge ({address}) está em uso ou reservada. Feche todas as janelas do OrcaSlicer e abra o programa novamente.",
        "Portul local FlashForge ({address}) este ocupat sau rezervat. Închide toate ferestrele OrcaSlicer și redeschide aplicația.",
        "Локальный порт FlashForge ({address}) занят или зарезервирован. Закройте все окна OrcaSlicer и запустите его снова.",
        "Den lokala porten för FlashForge ({address}) används eller är reserverad. Stäng alla OrcaSlicer-fönster och starta OrcaSlicer igen.",
        "พอร์ตภายในเครื่องของ FlashForge ({address}) ถูกใช้งานหรือจองไว้. ปิดหน้าต่าง OrcaSlicer ทั้งหมดแล้วเปิด OrcaSlicer อีกครั้ง",
        "FlashForge’un yerel bağlantı noktası ({address}) kullanımda veya ayrılmış. Tüm OrcaSlicer pencerelerini kapatıp OrcaSlicer’ı yeniden açın.",
        "Локальний порт FlashForge ({address}) зайнятий або зарезервований. Закрийте всі вікна OrcaSlicer і запустіть його знову.",
        "Cổng cục bộ của FlashForge ({address}) đang được dùng hoặc đã được dành riêng. Hãy đóng tất cả cửa sổ OrcaSlicer rồi mở lại ứng dụng.",
        "FlashForge 的本地端口（{address}）已被占用或保留。请关闭所有 OrcaSlicer 窗口，然后重新打开 OrcaSlicer。",
        "FlashForge 的本機連接埠（{address}）已被使用或保留。請關閉所有 OrcaSlicer 視窗，再重新開啟 OrcaSlicer。",
    ),
    "Remove the print and close the completion dialog on the printer, if shown.": (
        "Retira la peça i tanca el diàleg de finalització a la impressora, si apareix.",
        "Sejměte výtisk a zavřete dialog o dokončení na tiskárně, pokud se zobrazuje.",
        "Entferne das Druckteil und schließe den Abschlussdialog am Drucker, falls er angezeigt wird.",
        "Retira la pieza y cierra el aviso de finalización en la impresora si aparece.",
        "Kendu inprimatutako pieza eta itxi amaierako elkarrizketa inprimagailuan, agertzen bada.",
        "Retirez la pièce et fermez la boîte de dialogue de fin sur l’imprimante si elle s’affiche.",
        "Vedd le a nyomatot, és zárd be a nyomtatón a befejezési ablakot, ha megjelenik.",
        "Rimuovi il pezzo e chiudi l’avviso di fine stampa sulla stampante, se visualizzato.",
        "造形物を取り外し、プリンターに完了ダイアログが表示されていれば閉じてください。", "출력물을 제거하고 프린터에 완료 창이 표시되면 닫으세요.",
        "Nuimkite spaudinį ir užverkite baigimo langą spausdintuve, jei jis rodomas.",
        "Verwijder de afdruk en sluit het voltooiingsvenster op de printer als het wordt weergegeven.",
        "Zdejmij wydruk i zamknij okno zakończenia na drukarce, jeśli jest widoczne.",
        "Retire a peça e feche o aviso de conclusão na impressora, se estiver visível.",
        "Îndepărtează piesa și închide fereastra de finalizare de pe imprimantă, dacă apare.",
        "Снимите деталь со стола и закройте окно «Печать завершена» на экране принтера, если оно открыто.",
        "Ta bort utskriften och stäng dialogrutan på skrivaren om den visas.",
        "นำชิ้นงานออกและปิดหน้าต่างแจ้งการพิมพ์เสร็จบนเครื่องพิมพ์หากปรากฏ",
        "Baskıyı çıkarın ve görüntüleniyorsa yazıcıdaki tamamlanma penceresini kapatın.",
        "Зніміть деталь зі столу та закрийте вікно завершення друку на екрані принтера, якщо воно відкрите.",
        "Lấy bản in ra và đóng hộp thoại hoàn tất trên máy in nếu hộp thoại đang hiển thị.", "取下打印件。如果打印机显示完成提示，请关闭提示。",
        "取下列印件。如果印表機顯示完成提示，請關閉提示。",
    ),
    "While this tab is open": (
        "Mentre aquesta pestanya estigui oberta", "Když je tato karta otevřená", "Solange dieser Tab geöffnet ist",
        "Mientras esta pestaña esté abierta", "Fitxa hau irekita dagoen bitartean", "Tant que cet onglet est ouvert",
        "Amíg ez a lap nyitva van", "Finché questa scheda è aperta", "このタブを開いている間", "이 탭이 열려 있는 동안",
        "Kol šis skirtukas atvertas", "Zolang dit tabblad open is", "Gdy ta karta jest otwarta",
        "Enquanto esta aba estiver aberta", "Cât timp această filă este deschisă", "Пока открыта эта вкладка",
        "Medan den här fliken är öppen", "ขณะที่เปิดแท็บนี้", "Bu sekme açıkken", "Поки відкрита ця вкладка",
        "Khi thẻ này đang mở", "此选项卡打开时", "此分頁開啟時",
    ),
    "Only during an active print": (
        "Només durant una impressió activa", "Pouze během aktivního tisku", "Nur bei einem aktiven Druckauftrag",
        "Solo con una impresión activa", "Inprimaketa aktiboan soilik", "Uniquement pendant une impression active",
        "Csak aktív nyomtatás közben", "Solo con una stampa attiva", "印刷ジョブが進行中のときのみ", "인쇄 작업이 진행 중일 때만",
        "Tik vykstant spausdinimui", "Alleen bij een actieve afdruktaak", "Tylko podczas aktywnego drukowania",
        "Somente durante uma impressão ativa", "Doar în timpul unei imprimări active", "Только при активном задании",
        "Endast under en aktiv utskrift", "เฉพาะเมื่อมีงานพิมพ์ที่กำลังดำเนินการ",
        "Yalnızca etkin bir baskı işi sırasında", "Лише під час активного завдання", "Chỉ khi có tác vụ in đang chạy",
        "仅在有活动打印任务时", "僅在有進行中的列印工作時",
    ),
    "Waiting for part removal": (
        "Pendent de retirar la peça", "Čeká na sejmutí výtisku", "Wartet auf Entnahme des Druckteils",
        "Esperando retirar la pieza", "Pieza kentzeko zain", "En attente du retrait de la pièce",
        "Az alkatrész eltávolítására vár", "In attesa della rimozione del pezzo", "造形物の取り外し待ち", "출력물 제거 대기 중",
        "Laukiama, kol bus nuimta detalė", "Wacht op verwijderen van het onderdeel", "Czeka na zdjęcie wydruku",
        "Aguardando a retirada da peça", "Așteaptă îndepărtarea piesei", "Ждёт снятия детали",
        "Väntar på att delen tas bort", "รอนำชิ้นงานออก", "Parçanın çıkarılması bekleniyor", "Чекає на зняття деталі",
        "Đang chờ lấy chi tiết ra", "等待取下打印件", "等待取下列印件",
    ),
    "Wait for part removal": (
        "Esperar que es retiri la peça", "Čekat na sejmutí výtisku", "Auf Entnahme des Druckteils warten",
        "Esperar a que se retire la pieza", "Pieza kendu arte itxaron", "Attendre le retrait de la pièce",
        "Várakozás az alkatrész eltávolítására", "Attendi la rimozione del pezzo", "造形物の取り外しを待つ", "출력물 제거 대기",
        "Laukti, kol bus nuimta detalė", "Wachten op verwijdering van het onderdeel", "Czekaj na zdjęcie wydruku",
        "Aguardar a retirada da peça", "Așteaptă îndepărtarea piesei", "Ждать снятия детали",
        "Vänta tills delen tas bort", "รอให้นำชิ้นงานออก", "Parçanın çıkarılmasını bekle", "Чекати на зняття деталі",
        "Chờ lấy chi tiết ra", "等待取下打印件", "等待取下列印件",
    ),
    "Bed is clear": (
        "El llit és lliure", "Podložka je volná", "Druckbett ist frei", "La cama está libre", "Ohea libre dago",
        "Plateau libre", "Az asztal szabad", "Piatto libero", "ベッドを空にした", "베드 비움 확인", "Stalas laisvas",
        "Printbed is vrij", "Stół jest wolny", "Mesa livre", "Patul este liber", "Стол свободен", "Byggplattan är fri",
        "แท่นพิมพ์ว่าง", "Tabla boş", "Стіл вільний", "Bàn in đã trống", "热床已清空", "熱床已清空",
    ),
    "Chart layout": (
        "Disposició del gràfic", "Rozložení grafu", "Diagrammlayout", "Diseño del gráfico", "Grafikoaren diseinua",
        "Disposition du graphique", "Diagram elrendezése", "Disposizione del grafico", "グラフの配置", "그래프 레이아웃",
        "Diagramos išdėstymas", "Grafiekindeling", "Układ wykresu", "Layout do gráfico", "Aspectul graficului",
        "Вид графика", "Diagramlayout", "เค้าโครงกราฟ", "Grafik düzeni", "Вигляд графіка", "Bố cục biểu đồ", "图表布局",
        "圖表配置",
    ),
    "Shared scale": (
        "Escala compartida", "Společná stupnice", "Gemeinsame Skala", "Escala común", "Eskala partekatua",
        "Échelle commune", "Közös skála", "Scala comune", "共通スケール", "공통 눈금", "Bendra skalė", "Gedeelde schaal",
        "Wspólna skala", "Escala comum", "Scară comună", "Общая шкала", "Gemensam skala", "สเกลร่วม", "Ortak ölçek",
        "Спільна шкала", "Thang đo chung", "共用刻度", "共用刻度",
    ),
    "Separate scales": (
        "Escales separades", "Samostatné stupnice", "Getrennte Skalen", "Escalas independientes", "Eskala bereiziak",
        "Échelles séparées", "Külön skálák", "Scale separate", "個別スケール", "개별 눈금", "Atskiros skalės",
        "Afzonderlijke schalen", "Osobne skale", "Escalas separadas", "Scări separate", "Отдельные шкалы",
        "Separata skalor", "สเกลแยก", "Ayrı ölçekler", "Окремі шкали", "Thang đo riêng", "分别设定刻度", "個別刻度",
    ),
    "Select at least one temperature line.": (
        "Seleccioneu almenys una línia de temperatura.", "Vyberte alespoň jednu teplotní křivku.",
        "Wählen Sie mindestens eine Temperaturkurve aus.", "Selecciona al menos una línea de temperatura.",
        "Hautatu gutxienez tenperatura-lerro bat.", "Sélectionnez au moins une courbe de température.",
        "Válasszon legalább egy hőmérsékletgörbét.", "Seleziona almeno una curva di temperatura.",
        "温度の線を少なくとも1つ選択してください。", "온도 곡선을 하나 이상 선택하세요.", "Pasirinkite bent vieną temperatūros kreivę.",
        "Selecteer minstens één temperatuurlijn.", "Wybierz co najmniej jedną linię temperatury.",
        "Selecione pelo menos uma curva de temperatura.", "Selectați cel puțin o curbă de temperatură.",
        "Выберите хотя бы одну линию температуры.", "Välj minst en temperaturkurva.",
        "เลือกเส้นอุณหภูมิอย่างน้อยหนึ่งเส้น", "En az bir sıcaklık çizgisi seçin.",
        "Виберіть хоча б одну лінію температури.", "Chọn ít nhất một đường nhiệt độ.", "请至少选择一条温度曲线。", "請至少選擇一條溫度曲線。",
    ),
    "Active print": (
        "Impressió activa", "Aktivní tisk", "Aktiver Druck", "Impresión activa", "Inprimatze aktiboa",
        "Impression active", "Aktív nyomtatás", "Stampa attiva", "アクティブプリント", "액티브 프린트", "Aktyvus spausdinimas",
        "Actieve afdruk", "Aktywny wydruk", "Impressão ativa", "Imprimare activă", "Текущая печать", "Aktiv utskrift",
        "พิมพ์ที่ใช้งานอยู่", "Aktif yazdırma", "Активний друк", "In hoạt động", "主动打印", "主動列印",
    ),
    "Add at least one printer address.": (
        "Afegiu almenys una adreça d'impressora.", "Přidejte alespoň jednu adresu tiskárny.",
        "Fügen Sie mindestens eine Druckeradresse hinzu.", "Agregue al menos una dirección de impresora.",
        "Gehitu gutxienez inprimagailu-helbide bat.", "Ajoutez au moins une adresse d'imprimante.",
        "Adjon hozzá legalább egy nyomtatócímet.", "Aggiungere almeno un indirizzo stampante.",
        "少なくとも 1 つのプリンター アドレスを追加します。", "하나 이상의 프린터 주소를 추가하세요.", "Pridėkite bent vieną spausdintuvo adresą.",
        "Voeg ten minste één printeradres toe.", "Dodaj co najmniej jeden adres drukarki.",
        "Adicione pelo menos um endereço de impressora.", "Adăugați cel puțin o adresă de imprimantă.",
        "Добавьте хотя бы один адрес принтера.", "Lägg till minst en skrivaradress.",
        "เพิ่มที่อยู่เครื่องพิมพ์อย่างน้อยหนึ่งรายการ", "En az bir yazıcı adresi ekleyin.",
        "Додайте принаймні одну адресу принтера.", "Thêm ít nhất một địa chỉ máy in.", "添加至少一台打印机地址。", "新增至少一台印表機位址。",
    ),
    "Add printer": (
        "Afegeix una impressora", "Přidat tiskárnu", "Drucker hinzufügen", "Agregar impresora", "Gehitu inprimagailua",
        "Ajouter une imprimante", "Nyomtató hozzáadása", "Aggiungi stampante", "プリンターの追加", "프린터 추가",
        "Pridėti spausdintuvą", "Printer toevoegen", "Dodaj drukarkę", "Adicionar impressora", "Adăugați imprimantă",
        "Добавить принтер", "Lägg till skrivare", "เพิ่มเครื่องพิมพ์", "Yazıcı ekle", "Додати принтер", "Thêm máy in",
        "添加打印机", "新增印表機",
    ),
    "All": (
        "Tots", "Vše", "Alle", "Todos", "Guztiak", "Tous", "Mind", "Tutti", "全て", "전체", "Viskas", "Alles", "Wszystko",
        "Todos", "Toate", "Все", "Alla", "ทั้งหมด", "Hepsi", "Всі", "Tất cả", "全部", "全部",
    ),
    "All printers": (
        "Totes les impressores", "Všechny tiskárny", "Alle Drucker", "Todas las impresoras", "Inprimagailu guztiak",
        "Toutes les imprimantes", "Minden nyomtató", "Tutte le stampanti", "すべてのプリンター", "모든 프린터", "Visi spausdintuvai",
        "Alle printers", "Wszystkie drukarki", "Todas as impressoras", "Toate imprimantele", "Все принтеры",
        "Alla skrivare", "เครื่องพิมพ์ทั้งหมด", "Tüm yazıcılar", "Усі принтери", "Tất cả máy in", "所有打印机", "所有印表機",
    ),
    "Allow OrcaSlicer to access your printer when prompted, then reload the plugin.": (
        "Permeteu que OrcaSlicer accedeixi a la vostra impressora quan se us demani i, a continuació, torneu a carregar el connector.",
        "Po zobrazení výzvy povolte OrcaSlicer přístup k tiskárně a poté znovu načtěte plugin.",
        "Erlauben Sie OrcaSlicer den Zugriff auf Ihren Drucker, wenn Sie dazu aufgefordert werden, und laden Sie dann das Plugin neu.",
        "Permita que OrcaSlicer acceda a su impresora cuando se le solicite, luego vuelva a cargar el complemento.",
        "Baimendu OrcaSlicer inprimagailuan sartzea eskatzen dionean, eta, gero, kargatu plugina.",
        "Autorisez OrcaSlicer à accéder à votre imprimante lorsque vous y êtes invité, puis rechargez le plugin.",
        "Engedélyezze a OrcaSlicer számára a nyomtató elérését, amikor a rendszer kéri, majd töltse be újra a beépülő modult.",
        "Consenti a OrcaSlicer di accedere alla stampante quando richiesto, quindi ricarica il plug-in.",
        "プロンプトが表示されたら、OrcaSlicer がプリンターにアクセスできるようにし、プラグインをリロードします。",
        "메시지가 표시되면 OrcaSlicer가 프린터에 액세스하도록 허용한 다음 플러그인을 다시 로드하십시오.",
        "Leiskite OrcaSlicer pasiekti spausdintuvą, kai būsite paraginti, tada iš naujo įkelkite papildinį.",
        "Geef OrcaSlicer toegang tot uw printer wanneer daarom wordt gevraagd en laad de plug-in opnieuw.",
        "Zezwól OrcaSlicer na dostęp do drukarki, gdy zostaniesz o to poproszony, a następnie załaduj ponownie wtyczkę.",
        "Permita que o OrcaSlicer acesse sua impressora quando solicitado e recarregue o plug-in.",
        "Permiteți OrcaSlicer să vă acceseze imprimanta atunci când vi se solicită, apoi reîncărcați pluginul.",
        "Когда OrcaSlicer запросит доступ к принтеру, разрешите его и перезагрузите плагин.",
        "Tillåt OrcaSlicer att komma åt din skrivare när du uppmanas, ladda sedan om plugin-programmet.",
        "อนุญาตให้ OrcaSlicer เข้าถึงเครื่องพิมพ์ของคุณเมื่อได้รับแจ้ง จากนั้นโหลดปลั๊กอินอีกครั้ง",
        "İstendiğinde OrcaSlicer'nin yazıcınıza erişmesine izin verin, ardından eklentiyi yeniden yükleyin.",
        "Надайте OrcaSlicer доступ до вашого принтера, коли буде запропоновано, а потім перезавантажте плагін.",
        "Cho phép OrcaSlicer truy cập vào máy in của bạn khi được nhắc, sau đó tải lại plugin.",
        "出现提示时允许OrcaSlicer 访问您的打印机，然后重新加载插件。", "出現提示時允許OrcaSlicer 存取您的印表機，然後重新載入插件。",
    ),
    "Allow access to the printer, then reload the plugin.": (
        "Permet l'accés a la impressora i, a continuació, torna a carregar el connector.",
        "Povolte přístup k tiskárně a poté znovu načtěte plugin.",
        "Erlauben Sie den Zugriff auf den Drucker und laden Sie dann das Plugin neu.",
        "Permita el acceso a la impresora y luego vuelva a cargar el complemento.",
        "Baimendu inprimagailurako sarbidea eta, ondoren, kargatu plugina.",
        "Autorisez l'accès à l'imprimante, puis rechargez le plugin.",
        "Engedélyezze a hozzáférést a nyomtatóhoz, majd töltse be újra a bővítményt.",
        "Consentire l'accesso alla stampante, quindi ricaricare il plug-in.", "プリンターへのアクセスを許可してからプラグインを再読み込みしてください。",
        "프린터 접근을 허용한 후 플러그인을 다시 로드하세요.", "Leiskite pasiekti spausdintuvą, tada iš naujo įkelkite papildinį.",
        "Geef toegang tot de printer en laad de plug-in opnieuw.",
        "Zezwól na dostęp do drukarki, a następnie załaduj ponownie wtyczkę.",
        "Permita o acesso à impressora e recarregue o plug-in.",
        "Permiteți accesul la imprimantă, apoi reîncărcați pluginul.",
        "Разрешите доступ к принтеру, затем перезагрузите плагин.",
        "Tillåt åtkomst till skrivaren och ladda sedan om plugin-programmet.",
        "อนุญาตให้เข้าถึงเครื่องพิมพ์ จากนั้นโหลดปลั๊กอินใหม่",
        "Yazıcıya erişime izin verin, ardından eklentiyi yeniden yükleyin.",
        "Надайте доступ до принтера, а потім перезавантажте плагін.",
        "Cho phép truy cập vào máy in, sau đó tải lại plugin.", "允许访问打印机，然后重新加载插件。", "允許存取印表機，然後重新載入插件。",
    ),
    "Appearance": (
        "Aparença", "Vzhled", "Aussehen", "Apariencia", "Itxura", "Apparence", "Megjelenés", "Aspetto", "外観", "외관",
        "Išvaizda", "Uiterlijk", "Wygląd", "Aparência", "Aspect", "Внешний вид", "Utseende", "ลักษณะที่ปรากฏ",
        "Görünüm", "Зовнішній вигляд", "Giao diện", "外观", "外觀",
    ),
    "Apply": (
        "Aplica", "Použít", "Anwenden", "Aplicar", "Aplikatu", "Appliquer", "Alkalmaz", "Applica", "適用", "적용",
        "Taikyti", "Toepassen", "Zastosuj", "Aplicar", "Aplică", "Применить", "Verkställ", "นำไปใช้", "Uygula",
        "Застосувати", "Áp dụng", "应用", "套用",
    ),
    "Attention": (
        "Atenció", "Pozor", "Achtung", "Atención", "Arreta", "Attention", "Figyelem", "Attenzione", "注意", "주의",
        "Dėmesio", "Let op", "Uwaga", "Atenção", "Atenție", "Внимание", "Observera", "ความสนใจ", "Dikkat", "Увага",
        "Chú ý", "注意", "注意",
    ),
    "Auto shutdown": (
        "Apagat automàtic", "Automatické vypnutí", "Automatische Abschaltung", "Apagado automático",
        "Itzaltze automatikoa", "Arrêt automatique", "Automatikus kikapcsolás", "Spegnimento automatico", "自動シャットダウン",
        "자동 종료", "Automatinis išjungimas", "Automatische uitschakeling", "Automatyczne wyłączanie",
        "Desligamento automático", "Oprire automată", "Автовыключение", "Automatisk avstängning", "ปิดเครื่องอัตโนมัติ",
        "Otomatik kapanma", "Автоматичне вимкнення", "Tự động tắt máy", "自动关机", "自動關機",
    ),
    "Axes": (
        "Eixos", "Osy", "Achsen", "Ejes", "Ardatzak", "Axes", "Tengelyek", "Assi", "軸", "축", "Ašys", "Assen", "Osie",
        "Eixos", "Axe", "Оси", "Axlar", "แกน", "Eksenler", "Осі", "Trục", "轴", "軸",
    ),
    "Axes busy": (
        "Eixos ocupats", "Osy jsou zaneprázdněné", "Achsen beschäftigt", "Ejes ocupados", "Ardatzak lanpetuta",
        "Axes occupés", "A tengelyek foglaltak", "Assi occupati", "軸がビジーです", "축 사용 중", "Ašys užimtos", "Assen bezet",
        "Osie zajęte", "Eixos ocupados", "Axele ocupate", "Оси заняты", "Axlar upptagna", "แกนไม่ว่าง",
        "Eksenler meşgul", "Сокири зайняті", "Trục bận", "轴忙", "軸忙",
    ),
    "Axis movement step": (
        "Pas de moviment de l'eix", "Krok pohybu osy", "Achsenbewegungsschritt", "Paso de movimiento del eje",
        "Ardatzaren mugimenduaren urratsa", "Incrément de déplacement des axes", "Tengelymozgás lépés",
        "Passo di movimento dell'asse", "軸移動ステップ", "축 이동 단계", "Ašies judėjimo žingsnis", "Asbewegingsstap",
        "Krok ruchu osi", "Etapa de movimento do eixo", "Pas de mișcare a axei", "Шаг перемещения осей",
        "Axelrörelsesteg", "ขั้นตอนการเคลื่อนแกน", "Eksen hareket adımı", "Крок руху осі", "Bước chuyển động của trục",
        "轴移动步距", "軸移動步距",
    ),
    "B": (
        "Llit", "Podl.", "Bett", "Cama", "Ohea", "Plat.", "Aszt.", "Piatto", "ベッド", "베드", "Pagr.", "Printbed", "Stół",
        "Mesa", "Pat", "Стол", "Platta", "ฐานพิมพ์", "Yatak", "Стіл", "Bàn in", "热床", "列印板",
    ),
    "Basic readings received. CheckCode could not be verified.": (
        "Lectures bàsiques rebudes. No s'ha pogut verificar CheckCode.",
        "Přijaté základní hodnoty. CheckCode se nepodařilo ověřit.",
        "Grundlegende Messwerte erhalten. CheckCode konnte nicht verifiziert werden.",
        "Lecturas básicas recibidas. No se pudo verificar CheckCode.",
        "Jasotako oinarrizko irakurketak. Ezin izan da egiaztatu CheckCode.",
        "Lectures de base reçues. CheckCode n’a pas pu être vérifié.",
        "Alapvető értékek érkeztek. A CheckCode nem ellenőrizhető.",
        "Letture di base ricevute. Impossibile verificare CheckCode.", "基本的な測定値を受信しました。 CheckCodeは確認できませんでした。",
        "기본 판독값이 수신되었습니다. CheckCode를 확인할 수 없습니다.", "Gauti pagrindiniai rodmenys. Nepavyko patikrinti CheckCode.",
        "Basismetingen ontvangen. CheckCode kon niet worden geverifieerd.",
        "Otrzymano podstawowe odczyty. Nie można zweryfikować CheckCode.",
        "Leituras básicas recebidas. CheckCode não pôde ser verificado.",
        "Citiri de bază primite. CheckCode nu a putut fi verificat.",
        "Основные показания получены. Проверить CheckCode не удалось.",
        "Grundläggande avläsningar mottagna. CheckCode kunde inte verifieras.",
        "การอ่านค่าพื้นฐานที่ได้รับ ไม่สามารถตรวจสอบ CheckCode ได้", "Alınan temel okumalar. CheckCode doğrulanamadı.",
        "Отримано основні показання. CheckCode не вдалося перевірити.",
        "Đã nhận được các bài đọc cơ bản. CheckCode không thể được xác minh.", "收到基本读数。 CheckCode 无法验证。",
        "收到基本讀數。 CheckCode 無法驗證。",
    ),
    "Bed": (
        "Llit", "Podložka", "Druckbett", "Cama", "Ohea", "Plateau", "Asztal", "Piatto", "ベッド", "베드", "Pagrindas",
        "Printbed", "Stół", "Mesa", "Pat de imprimare", "Стол", "Byggplatta", "ฐานพิมพ์", "Yatak", "Стіл", "Bàn in",
        "热床", "列印板",
    ),
    "Bed Target": (
        "Temperatura objectiu del llit", "Cílová teplota podložky", "Druckbett-Solltemperatur",
        "Temperatura objetivo de la cama", "Ohearen helburuko tenperatura", "Consigne du plateau",
        "Asztal célhőmérséklete", "Temperatura obiettivo del piatto", "ベッド目標温度", "베드 목표 온도",
        "Tikslinė pagrindo temperatūra", "Doeltemperatuur printbed", "Temperatura docelowa stołu",
        "Temperatura alvo da mesa", "Temperatura țintă a patului", "Целевая температура стола",
        "Måltemperatur för byggplattan", "อุณหภูมิเป้าหมายของฐานพิมพ์", "Yatak hedef sıcaklığı",
        "Цільова температура столу", "Nhiệt độ đặt của bàn in", "热床目标温度", "列印板目標溫度",
    ),
    "Bed Z": (
        "Eix Z del llit", "Osa Z podložky", "Z-Achse des Druckbetts", "Eje Z de la cama", "Ohearen Z ardatza",
        "Axe Z du plateau", "Asztal Z tengelye", "Asse Z del piatto", "ベッドのZ軸", "베드 Z축", "Pagrindo Z ašis",
        "Z-as van het printbed", "Oś Z stołu", "Eixo Z da mesa", "Axa Z a patului", "Ось Z стола",
        "Byggplattans Z-axel", "แกน Z ของฐานพิมพ์", "Baskı yatağının Z ekseni", "Вісь Z столу", "Trục Z của bàn in",
        "热床 Z 轴", "列印板 Z 軸",
    ),
    "Bed leveling": (
        "Anivellament del llit", "Vyrovnání podložky", "Druckbettnivellierung", "Nivelación de Cama",
        "Ohearen nibelatzea", "Mise à niveau du plateau", "Asztalszintezés", "Livellamento del piatto", "ベッドレベリング",
        "베드 레벨링", "Spausdinimo pagrindo išlyginimas", "Bednivellering", "Poziomowanie stołu", "Nivelamento da mesa",
        "Nivelarea patului", "Выравнивание стола", "Justering av Byggplattan", "ปรับระดับฐานพิมพ์", "Yatak Seviyeleme",
        "Вирівнювання столу", "Cân bằng bàn in", "热床调平", "熱床調平",
    ),
    "Browse files": (
        "Explorar fitxers", "Procházet soubory", "Dateien durchsuchen", "Explorar archivos", "Arakatu fitxategiak",
        "Parcourir les fichiers", "Fájlok böngészése", "Sfoglia i file", "ファイルを参照する", "파일 찾아보기", "Naršyti failus",
        "Blader door bestanden", "Przeglądaj pliki", "Procurar arquivos", "Răsfoiți fișiere", "Открыть файлы",
        "Bläddra bland filer", "เรียกดูไฟล์", "Dosyalara göz atın", "Перегляд файлів", "Duyệt tập tin", "浏览文件", "瀏覽文件",
    ),
    "Build volume": (
        "Volum d’impressió", "Tiskový objem", "Bauvolumen", "Volumen de impresión", "Inprimatze-bolumena",
        "Volume d’impression", "Nyomtatási térfogat", "Volume di stampa", "造形サイズ", "출력 가능 부피", "Spausdinimo tūris",
        "Bouwvolume", "Objętość robocza", "Volume de impressão", "Volum de imprimare", "Область печати", "Byggvolym",
        "ปริมาตรการพิมพ์", "Baskı hacmi", "Область друку", "Thể tích in", "成型空间", "成型空間",
    ),
    "Busy": (
        "Ocupat", "Zaneprázdněn", "Beschäftigt", "Ocupado", "Lanpetuta", "Occupé", "Elfoglalt", "Occupato", "忙しい", "바쁨",
        "Užimtas", "Bezet", "Zajęty", "Ocupado", "Ocupat", "Занят", "Upptagen", "ไม่ว่าง", "Meşgul", "Зайнятий",
        "Bận rộn", "忙", "忙",
    ),
    "Calibrating": (
        "Calibrant", "Kalibrace", "Kalibrieren", "Calibración", "Kalibratzen", "Calibrage", "Kalibrálás",
        "Calibrazione", "校正する", "교정 중", "Kalibravimas", "Kalibreren", "Kalibracja", "Calibrando", "Calibrare",
        "Калибровка", "Kalibrering", "การปรับเทียบ", "Kalibrasyon", "Калібрування", "Hiệu chỉnh", "校准", "校準",
    ),
    "Calibration": (
        "Calibració", "Kalibrace", "Kalibrierung", "Calibración", "Kalibrazioa", "Étalonnage", "Kalibrálás",
        "Calibrazione", "校正", "교정", "Kalibravimas", "Kalibratie", "Kalibracja", "Calibração", "Calibrare", "Калибровка",
        "Kalibrering", "การสอบเทียบ", "Kalibrasyon", "Калібрування", "Hiệu chuẩn", "校准", "校準",
    ),
    "Calibration in progress": (
        "Calibració en curs", "Probíhá kalibrace", "Kalibrierung läuft", "Calibración en curso", "Kalibrazioa abian da",
        "Étalonnage en cours", "Kalibrálás folyamatban", "Calibrazione in corso", "校正中です", "교정 진행 중",
        "Kalibravimas vyksta", "Kalibratie wordt uitgevoerd", "Kalibracja w toku", "Calibração em andamento",
        "Calibrare în curs", "Выполняется калибровка", "Kalibrering pågår", "อยู่ระหว่างการปรับเทียบ",
        "Kalibrasyon devam ediyor", "Виконується калібрування", "Đang hiệu chỉnh", "校准正在进行中", "校準正在進行中",
    ),
    "Camera": (
        "Càmera", "Fotoaparát", "Kamera", "Cámara", "Kamera", "Caméra", "Kamera", "Fotocamera", "カメラ", "카메라",
        "Fotoaparatas", "Camera", "Kamera", "Câmera", "Cameră", "Камера", "Kamera", "กล้อง", "Kamera", "Камера",
        "Máy ảnh", "相机", "相機",
    ),
    "Camera unavailable.": (
        "Càmera no disponible.", "Kamera není k dispozici.", "Kamera nicht verfügbar.", "Cámara no disponible.",
        "Kamera ez dago erabilgarri.", "Caméra indisponible.", "A kamera nem elérhető.", "Fotocamera non disponibile.",
        "カメラが使用できません。", "카메라를 사용할 수 없습니다.", "Kamera nepasiekiama.", "Camera niet beschikbaar.", "Kamera niedostępna.",
        "Câmera indisponível.", "Camera nu este disponibilă.", "Камера недоступна.", "Kameran är inte tillgänglig.",
        "กล้องใช้งานไม่ได้", "Kamera kullanılamıyor.", "Камера недоступна.", "Máy ảnh không có sẵn.", "相机不可用。",
        "相機不可用。",
    ),
    "Cameras": (
        "Càmeres", "Kamery", "Kameras", "Cámaras", "Kamerak", "Caméras", "Kamerák", "Telecamere", "カメラ", "카메라",
        "Fotoaparatai", "Camera's", "Kamery", "Câmeras", "Camere", "Камеры", "Kameror", "กล้อง", "Kameralar", "Камери",
        "Máy ảnh", "相机", "相機",
    ),
    "Cancel": (
        "Cancel·lar", "Zrušit", "Abbrechen", "Cancelar", "Utzi", "Annuler", "Mégse", "Annulla", "取消し", "취소", "Atšaukti",
        "Annuleren", "Anuluj", "Cancelar", "Anulați", "Отмена", "Avbryt", "ยกเลิก", "İptal", "Скасувати", "Hủy", "取消",
        "取消",
    ),
    "Cancel print?": (
        "Cancel·la la impressió?", "Zrušit tisk?", "Druck abbrechen?", "¿Cancelar impresión?",
        "Inprimatzea bertan behera utzi?", "Annuler l'impression ?", "Megszakítja a nyomtatást?",
        "Annullare la stampa?", "印刷を中止しますか？", "인쇄를 취소하시겠습니까?", "Atšaukti spausdinimą?", "Afdrukken annuleren?",
        "Anulować drukowanie?", "Cancelar impressão?", "Anulați imprimarea?", "Отменить печать?", "Avbryta utskriften?",
        "ยกเลิกการพิมพ์หรือไม่?", "Yazdırma iptal edilsin mi?", "Скасувати друк?", "Hủy in?", "取消打印？", "取消列印？",
    ),
    "Canceled": (
        "Cancel·lat", "Zrušeno", "Abgesagt", "Cancelado", "Ezeztatuta", "Annulé", "Törölve", "Annullato", "中止", "취소됨",
        "Atšaukta", "Geannuleerd", "Anulowano", "Cancelado", "Anulat", "Отменено", "Avbruten", "ยกเลิกแล้ว",
        "İptal edildi", "Скасовано", "Đã hủy", "取消", "取消",
    ),
    "Canceling": (
        "Cancel·lant", "Ruší se", "Wird abgebrochen", "Cancelando", "Ezeztatzen", "Annulation en cours",
        "Megszakítás folyamatban", "Annullamento in corso", "キャンセル中", "취소 중", "Atšaukiama", "Wordt geannuleerd",
        "Anulowanie w toku", "Cancelando", "Se anulează", "Отменяется", "Avbryter", "กำลังยกเลิก", "İptal ediliyor",
        "Скасування триває", "Đang hủy", "正在取消", "正在取消",
    ),
    "Card size": (
        "Mida de la targeta", "Velikost karty", "Kartengröße", "Tamaño de tarjeta", "Txartelaren tamaina",
        "Taille de la carte", "Kártyaméret", "Formato della carta", "カードサイズ", "카드 사이즈", "Kortelės dydis",
        "Kaartformaat", "Rozmiar karty", "Tamanho do cartão", "Dimensiunea cardului", "Размер карточек", "Kortstorlek",
        "ขนาดการ์ด", "Kart boyutu", "Розмір картки", "Kích thước thẻ", "卡片尺寸", "卡片尺寸",
    ),
    "Chamber": (
        "Cambra d’impressió", "Tisková komora", "Druckkammer", "Cámara de impresión", "Inprimatze-ganbera",
        "Chambre d’impression", "Nyomtatókamra", "Camera di stampa", "造形チャンバー", "출력 챔버", "Spausdinimo kamera",
        "Printkamer", "Komora druku", "Câmara de impressão", "Cameră de imprimare", "Камера печати", "Utskriftskammare",
        "ห้องพิมพ์", "Baskı haznesi", "Камера друку", "Buồng in", "打印腔", "列印腔",
    ),
    "Chamber light": (
        "Llum de la cambra d’impressió", "Osvětlení tiskové komory", "Druckkammerbeleuchtung",
        "Luz de la cámara de impresión", "Inprimatze-ganberako argia", "Éclairage de la chambre d’impression",
        "Nyomtatókamra világítása", "Luce della camera di stampa", "チャンバー照明", "출력 챔버 조명",
        "Spausdinimo kameros apšvietimas", "Verlichting van de printkamer", "Oświetlenie komory druku",
        "Luz da câmara de impressão", "Iluminarea camerei de imprimare", "Подсветка камеры печати",
        "Belysning i utskriftskammaren", "ไฟในห้องพิมพ์", "Baskı haznesi ışığı", "Підсвічування камери друку",
        "Đèn buồng in", "打印腔照明", "列印腔照明",
    ),
    "Check connection": (
        "Comproveu la connexió", "Zkontrolujte připojení", "Verbindung prüfen", "Comprobar conexión",
        "Egiaztatu konexioa", "Vérifier la connexion", "Ellenőrizze a csatlakozást", "Controllare la connessione",
        "接続を確認してください", "연결 확인", "Patikrinkite jungtį", "Controleer de aansluiting", "Sprawdź połączenie",
        "Verifique a conexão", "Verificați conexiunea", "Проверить подключение", "Kontrollera anslutningen",
        "ตรวจสอบการเชื่อมต่อ", "Bağlantıyı kontrol edin", "Перевірте з'єднання", "Kiểm tra kết nối", "检查连接", "檢查連接",
    ),
    "Check the CheckCode in the plugin settings.": (
        "Comproveu el CheckCode a la configuració del connector.", "Zkontrolujte CheckCode v nastavení pluginu.",
        "Überprüfen Sie CheckCode in den Plugin-Einstellungen.",
        "Verifique CheckCode en la configuración del complemento.", "Egiaztatu CheckCode pluginaren ezarpenetan.",
        "Vérifiez le CheckCode dans les paramètres du plugin.", "Ellenőrizze a CheckCode-t a bővítmény beállításaiban.",
        "Controlla CheckCode nelle impostazioni del plugin.", "プラグイン設定のCheckCodeを確認してください。",
        "플러그인 설정에서 CheckCode를 확인하세요.", "Patikrinkite CheckCode papildinio nustatymuose.",
        "Controleer de CheckCode in de plug-ininstellingen.", "Sprawdź opcję CheckCode w ustawieniach wtyczki.",
        "Verifique CheckCode nas configurações do plugin.", "Verificați CheckCode în setările pluginului.",
        "Проверьте CheckCode в настройках плагина.", "Kontrollera CheckCode i plugin-inställningarna.",
        "ตรวจสอบ CheckCode ในการตั้งค่าปลั๊กอิน", "Eklenti ayarlarında CheckCode'yi kontrol edin.",
        "Перевірте CheckCode у налаштуваннях плагіна.", "Kiểm tra CheckCode trong cài đặt plugin.",
        "检查插件设置中的CheckCode。", "檢查插件設定中的CheckCode。",
    ),
    "Check the printer serial number and CheckCode.": (
        "Comproveu el número de sèrie de la impressora i CheckCode.",
        "Zkontrolujte sériové číslo tiskárny a CheckCode.",
        "Überprüfen Sie die Seriennummer des Druckers und CheckCode.",
        "Verifique el número de serie de la impresora y CheckCode.",
        "Egiaztatu inprimagailuaren serie-zenbakia eta CheckCode.",
        "Vérifiez le numéro de série de l'imprimante et CheckCode.",
        "Ellenőrizze a nyomtató sorozatszámát és a CheckCode-t.",
        "Controllare il numero di serie della stampante e CheckCode.", "プリンターのシリアル番号とCheckCodeを確認してください。",
        "프린터 일련번호와 CheckCode를 확인하세요.", "Patikrinkite spausdintuvo serijos numerį ir CheckCode.",
        "Controleer het serienummer van de printer en CheckCode.", "Sprawdź numer seryjny drukarki i CheckCode.",
        "Verifique o número de série da impressora e CheckCode.",
        "Verificați numărul de serie al imprimantei și CheckCode.", "Проверьте серийный номер принтера и CheckCode.",
        "Kontrollera skrivarens serienummer och CheckCode.", "ตรวจสอบหมายเลขซีเรียลของเครื่องพิมพ์และ CheckCode",
        "Yazıcının seri numarasını ve CheckCode'yi kontrol edin.", "Перевірте серійний номер принтера та CheckCode.",
        "Kiểm tra số sê-ri máy in và CheckCode.", "检查打印机序列号和CheckCode。", "檢查印表機序號和CheckCode。",
    ),
    "Check the printer serial number in the plugin settings.": (
        "Comproveu el número de sèrie de la impressora a la configuració del connector.",
        "Zkontrolujte sériové číslo tiskárny v nastavení pluginu.",
        "Überprüfen Sie die Seriennummer des Druckers in den Plugin-Einstellungen.",
        "Verifique el número de serie de la impresora en la configuración del complemento.",
        "Egiaztatu inprimagailuaren serie-zenbakia pluginaren ezarpenetan.",
        "Vérifiez le numéro de série de l'imprimante dans les paramètres du plugin.",
        "Ellenőrizze a nyomtató sorozatszámát a bővítmény beállításainál.",
        "Controllare il numero di serie della stampante nelle impostazioni del plugin.",
        "プラグイン設定でプリンターのシリアル番号を確認してください。", "플러그인 설정에서 프린터 일련번호를 확인하세요.",
        "Patikrinkite spausdintuvo serijos numerį papildinio nustatymuose.",
        "Controleer het serienummer van de printer in de plug-in-instellingen.",
        "Sprawdź numer seryjny drukarki w ustawieniach wtyczki.",
        "Verifique o número de série da impressora nas configurações do plugin.",
        "Verificați numărul de serie al imprimantei în setările pluginului.",
        "Проверьте серийный номер принтера в настройках плагина.",
        "Kontrollera skrivarens serienummer i plugin-inställningarna.",
        "ตรวจสอบหมายเลขซีเรียลของเครื่องพิมพ์ในการตั้งค่าปลั๊กอิน",
        "Eklenti ayarlarında yazıcının seri numarasını kontrol edin.",
        "Перевірте серійний номер принтера в налаштуваннях плагіна.", "Kiểm tra số sê-ri máy in trong cài đặt plugin.",
        "检查插件设置中的打印机序列号。", "檢查插件設定中的印表機序號。",
    ),
    "Check the printer serial number.": (
        "Comproveu el número de sèrie de la impressora.", "Zkontrolujte sériové číslo tiskárny.",
        "Überprüfen Sie die Seriennummer des Druckers.", "Verifique el número de serie de la impresora.",
        "Egiaztatu inprimagailuaren serie-zenbakia.", "Vérifiez le numéro de série de l'imprimante.",
        "Ellenőrizze a nyomtató sorozatszámát.", "Controllare il numero di serie della stampante.",
        "プリンターのシリアル番号を確認してください。", "프린터 일련번호를 확인하세요.", "Patikrinkite spausdintuvo serijos numerį.",
        "Controleer het serienummer van de printer.", "Sprawdź numer seryjny drukarki.",
        "Verifique o número de série da impressora.", "Verificați numărul de serie al imprimantei.",
        "Проверьте серийный номер принтера.", "Kontrollera skrivarens serienummer.",
        "ตรวจสอบหมายเลขซีเรียลของเครื่องพิมพ์", "Yazıcının seri numarasını kontrol edin.",
        "Перевірте серійний номер принтера.", "Kiểm tra số sê-ri máy in.", "检查打印机序列号。", "檢查印表機序號。",
    ),
    "Check the selected printer.": (
        "Comproveu la impressora seleccionada.", "Zkontrolujte vybranou tiskárnu.",
        "Überprüfen Sie den ausgewählten Drucker.", "Verifique la impresora seleccionada.",
        "Egiaztatu hautatutako inprimagailua.", "Vérifiez l'imprimante sélectionnée.",
        "Ellenőrizze a kiválasztott nyomtatót.", "Controllare la stampante selezionata.", "選択したプリンターを確認します。",
        "선택한 프린터를 확인하세요.", "Patikrinkite pasirinktą spausdintuvą.", "Controleer de geselecteerde printer.",
        "Sprawdź wybraną drukarkę.", "Verifique a impressora selecionada.", "Verificați imprimanta selectată.",
        "Проверьте выбранный принтер.", "Kontrollera den valda skrivaren.", "ตรวจสอบเครื่องพิมพ์ที่เลือก",
        "Seçilen yazıcıyı kontrol edin.", "Перевірте вибраний принтер.", "Kiểm tra máy in đã chọn.", "检查选择的打印机。",
        "檢查所選的印表機。",
    ),
    "Check the serial number and CheckCode in the plugin settings.": (
        "Comproveu el número de sèrie i CheckCode a la configuració del connector.",
        "Zkontrolujte sériové číslo a CheckCode v nastavení pluginu.",
        "Überprüfen Sie die Seriennummer und CheckCode in den Plugin-Einstellungen.",
        "Verifique el número de serie y CheckCode en la configuración del complemento.",
        "Egiaztatu serie-zenbakia eta CheckCode pluginaren ezarpenetan.",
        "Vérifiez le numéro de série et CheckCode dans les paramètres du plugin.",
        "Ellenőrizze a sorozatszámot és a CheckCode-t a bővítmény beállításainál.",
        "Controlla il numero di serie e CheckCode nelle impostazioni del plugin.", "プラグイン設定でシリアル番号とCheckCodeを確認してください。",
        "플러그인 설정에서 일련번호와 CheckCode를 확인하세요.", "Patikrinkite serijos numerį ir CheckCode papildinio nustatymuose.",
        "Controleer het serienummer en CheckCode in de plug-ininstellingen.",
        "Sprawdź numer seryjny i CheckCode w ustawieniach wtyczki.",
        "Verifique o número de série e CheckCode nas configurações do plugin.",
        "Verificați numărul de serie și CheckCode în setările pluginului.",
        "Проверьте серийный номер и CheckCode в настройках плагина.",
        "Kontrollera serienumret och CheckCode i plugin-inställningarna.",
        "ตรวจสอบหมายเลขซีเรียลและ CheckCode ในการตั้งค่าปลั๊กอิน",
        "Eklenti ayarlarında seri numarasını ve CheckCode'yi kontrol edin.",
        "Перевірте серійний номер і CheckCode у налаштуваннях плагіна.",
        "Kiểm tra số sê-ri và CheckCode trong cài đặt plugin.", "检查插件设置中的序列号和CheckCode。", "檢查插件設定中的序號和CheckCode。",
    ),
    "Checking the saved preview…": (
        "Comprovant la previsualització desada...", "Kontrola uloženého náhledu…",
        "Überprüfen der gespeicherten Vorschau…", "Comprobando la vista previa guardada…",
        "Gordetako aurrebista egiaztatzen…", "Vérification de l'aperçu enregistré…", "A mentett előnézet ellenőrzése…",
        "Controllo dell'anteprima salvata…", "保存したプレビューを確認中…", "저장된 미리보기를 확인하는 중…", "Tikrinama išsaugota peržiūra...",
        "Het opgeslagen voorbeeld controleren…", "Sprawdzanie zapisanego podglądu…",
        "Verificando a visualização salva…", "Se verifică previzualizarea salvată...", "Проверка сохранённого эскиза…",
        "Kontrollerar den sparade förhandsgranskningen...", "กำลังตรวจสอบตัวอย่างที่บันทึกไว้...",
        "Kaydedilen önizleme kontrol ediliyor…", "Перевірка збереженого попереднього перегляду…",
        "Đang kiểm tra bản xem trước đã lưu…", "检查保存的预览...", "檢查已儲存的預覽...",
    ),
    "Choose Find printers or enter the address. The CheckCode is in the printer's network settings.": (
        "Trieu Cerca impressores o introduïu l'adreça. La CheckCode es troba a la configuració de xarxa de la impressora.",
        "Zvolte Najít tiskárny nebo zadejte adresu. CheckCode je v nastavení sítě tiskárny.",
        "Wählen Sie Drucker suchen oder geben Sie die Adresse ein. Der CheckCode befindet sich in den Netzwerkeinstellungen des Druckers.",
        "Elija Buscar impresoras o ingrese la dirección. El CheckCode está en la configuración de red de la impresora.",
        "Aukeratu Bilatu inprimagailuak edo idatzi helbidea. CheckCode inprimagailuaren sareko ezarpenetan dago.",
        "Choisissez Rechercher des imprimantes ou saisissez l'adresse. Le CheckCode se trouve dans les paramètres réseau de l'imprimante.",
        "Válassza a Nyomtatók keresése lehetőséget, vagy írja be a címet. A CheckCode a nyomtató hálózati beállításai között van.",
        "Scegli Trova stampanti o inserisci l'indirizzo. CheckCode si trova nelle impostazioni di rete della stampante.",
        "[プリンターの検索] を選択するか、アドレスを入力します。 CheckCode はプリンターのネットワーク設定にあります。",
        "프린터 찾기를 선택하거나 주소를 입력하세요. CheckCode는 프린터의 네트워크 설정에 있습니다.",
        "Pasirinkite Rasti spausdintuvus arba įveskite adresą. CheckCode yra spausdintuvo tinklo nustatymuose.",
        "Kies Printers zoeken of voer het adres in. De CheckCode bevindt zich in de netwerkinstellingen van de printer.",
        "Wybierz opcję Znajdź drukarki lub wprowadź adres. CheckCode znajduje się w ustawieniach sieciowych drukarki.",
        "Escolha Encontrar impressoras ou digite o endereço. O CheckCode está nas configurações de rede da impressora.",
        "Alegeți Găsiți imprimante sau introduceți adresa. CheckCode se află în setările de rețea ale imprimantei.",
        "Выберите «Найти принтеры» или введите адрес. CheckCode находится в сетевых настройках принтера.",
        "Välj Hitta skrivare eller ange adressen. CheckCode finns i skrivarens nätverksinställningar.",
        "เลือก ค้นหาเครื่องพิมพ์ หรือป้อนที่อยู่ CheckCode อยู่ในการตั้งค่าเครือข่ายของเครื่องพิมพ์",
        "Yazıcı bul'u seçin veya adresi girin. CheckCode yazıcının ağ ayarlarındadır.",
        "Виберіть Знайти принтери або введіть адресу. CheckCode знаходиться в налаштуваннях мережі принтера.",
        "Chọn Tìm máy in hoặc nhập địa chỉ. CheckCode nằm trong cài đặt mạng của máy in.",
        "选择查找打印机或输入地址。 CheckCode 位于打印机的网络设置中。", "選擇尋找印表機或輸入位址。 CheckCode 位於印表機的網路設定中。",
    ),
    "Choose a color.": (
        "Trieu un color.", "Vyberte barvu.", "Wählen Sie eine Farbe.", "Elige un color.", "Aukeratu kolore bat.",
        "Choisissez une couleur.", "Válasszon ki egy színt.", "Scegli un colore.", "カラーをお選びください。", "색상을 선택하세요.",
        "Pasirinkite spalvą.", "Kies een kleur.", "Wybierz kolor.", "Escolha uma cor.", "Alegeți o culoare.",
        "Выберите цвет.", "Välj en färg.", "เลือกสี", "Bir renk seçin.", "Виберіть колір.", "Chọn một màu.", "选择一种颜色。",
        "選擇一種顏色。",
    ),
    "Choose a loaded slot": (
        "Trieu una ranura carregada", "Vyberte nabitý slot", "Wählen Sie einen geladenen Steckplatz",
        "Elige una ranura cargada", "Aukeratu kargatutako zirrikitua", "Choisissez un emplacement chargé",
        "Válasszon egy betöltött nyílást", "Scegli uno slot caricato", "ロードされているスロットを選択してください", "로드된 슬롯 선택",
        "Pasirinkite įkrautą lizdą", "Kies een geladen slot", "Wybierz załadowane gniazdo", "Escolha um slot carregado",
        "Alegeți un slot încărcat", "Выберите слот с материалом", "Välj en laddad plats", "เลือกช่องที่โหลด",
        "Yüklü bir yuva seçin", "Виберіть завантажений слот", "Chọn một vị trí đã tải", "选择已加载的插槽", "選擇已載入的插槽",
    ),
    "Choose a material and color supported by this printer.": (
        "Trieu un material i un color compatibles amb aquesta impressora.",
        "Vyberte materiál a barvu podporovanou touto tiskárnou.",
        "Wählen Sie ein Material und eine Farbe, die von diesem Drucker unterstützt werden.",
        "Elija un material y color admitidos por esta impresora.",
        "Aukeratu inprimagailu honek onartzen duen materiala eta kolorea.",
        "Choisissez un matériau et une couleur pris en charge par cette imprimante.",
        "Válassza ki a nyomtató által támogatott anyagot és színt.",
        "Scegli un materiale e un colore supportati da questa stampante.", "このプリンターがサポートしている素材と色を選択してください。",
        "이 프린터가 지원하는 재질과 색상을 선택하세요.", "Pasirinkite medžiagą ir spalvą, kurią palaiko šis spausdintuvas.",
        "Kies een materiaal en kleur die door deze printer worden ondersteund.",
        "Wybierz materiał i kolor obsługiwany przez tę drukarkę.",
        "Escolha um material e uma cor suportados por esta impressora.",
        "Alegeți un material și o culoare acceptate de această imprimantă.",
        "Выберите материал и цвет, поддерживаемые этим принтером.",
        "Välj ett material och en färg som stöds av denna skrivare.", "เลือกวัสดุและสีที่เครื่องพิมพ์นี้รองรับ",
        "Bu yazıcının desteklediği bir malzeme ve renk seçin.",
        "Виберіть матеріал і колір, які підтримуються цим принтером.",
        "Chọn chất liệu và màu sắc được máy in này hỗ trợ.", "选择本打印机支持的材料和颜色。", "選擇本印表機支援的材質和顏色。",
    ),
    "Choose delay…": (
        "Tria retard...", "Vyberte zpoždění…", "Verzögerung wählen…", "Elige retraso…", "Aukeratu atzerapena…",
        "Choisissez le délai…", "Válasszon késleltetést…", "Scegli ritardo...", "遅延を選択してください…", "지연을 선택하세요…",
        "Pasirinkite delsą…", "Kies vertraging…", "Wybierz opóźnienie…", "Escolha o atraso…", "Alegeți întârziere...",
        "Выберите задержку…", "Välj fördröjning...", "เลือกดีเลย์...", "Gecikmeyi seçin…", "Виберіть затримку...",
        "Chọn độ trễ…", "选择延迟...", "選擇延遲...",
    ),
    "Choose material": (
        "Trieu material", "Vyberte materiál", "Material auswählen", "Elige material", "Aukeratu materiala",
        "Choisissez le matériau", "Válasszon anyagot", "Scegli il materiale", "素材を選ぶ", "소재를 선택하세요",
        "Pasirinkite medžiagą", "Kies materiaal", "Wybierz materiał", "Escolha o material", "Alegeți materialul",
        "Выберите материал", "Välj material", "เลือกวัสดุ", "Malzemeyi seçin", "Виберіть матеріал", "Chọn vật liệu",
        "选择材料", "選擇材料",
    ),
    "Clear error": (
        "Esborra l'error", "Odstraňte chybu", "Fehler löschen", "Borrar error", "Garbitu errorea", "Effacer l'erreur",
        "Hiba törlése", "Cancella errore", "エラー解除", "클리어 에러", "Išvalyti klaidą", "Fout wissen", "Usuń błąd",
        "Limpar erro", "Ștergeți eroarea", "Сбросить ошибку", "Rensa fel", "ข้อผิดพลาดที่ชัดเจน", "Hatayı temizle",
        "Очистити помилку", "Xóa lỗi", "清除错误", "清除錯誤",
    ),
    "Clear filters": (
        "Esborra els filtres", "Vymazat filtry", "Filter löschen", "Borrar filtros", "Garbitu iragazkiak",
        "Effacer les filtres", "Tiszta szűrők", "Cancella filtri", "フィルターをクリアする", "필터 지우기", "Išvalykite filtrus",
        "Filters wissen", "Wyczyść filtry", "Limpar filtros", "Ștergeți filtrele", "Очистить фильтры", "Rensa filter",
        "ล้างตัวกรอง", "Filtreleri temizle", "Очистити фільтри", "Xóa bộ lọc", "清除过滤器", "清除過濾器",
    ),
    "Close": (
        "Tancar", "Zavřít", "Schließen", "Cerrar", "Itxi", "Fermer", "Bezárás", "Chiudi", "閉じる", "닫기", "Uždaryti",
        "Sluiten", "Zamknij", "Fechar", "Închide", "Закрыть", "Stäng", "ปิด", "Kapat", "Закрити", "Đóng", "关闭", "關閉",
    ),
    "Closed": (
        "Tancat", "Zavřeno", "Geschlossen", "Cerrado", "Itxita", "Fermé", "Zárva", "Chiuso", "閉", "닫힘", "Uždaryta",
        "Gesloten", "Zamknięte", "Fechado", "Închis", "Закрыто", "Stängd", "ปิด", "Kapalı", "Зачинено", "Đóng", "已关闭",
        "已關閉",
    ),
    "Collecting readings…": (
        "Recollint mesures…", "Sběr naměřených hodnot…", "Messwerte werden erfasst…", "Recopilando mediciones…",
        "Neurketak biltzen…", "Collecte des mesures…", "Mérési adatok gyűjtése…", "Raccolta delle misurazioni…",
        "測定値を取得中…", "측정값을 수집하는 중…", "Renkami rodmenys…", "Meetwaarden worden verzameld…", "Zbieranie odczytów…",
        "Coletando medições…", "Se colectează măsurători…", "Сбор показаний…", "Samlar in mätvärden…",
        "กำลังรวบรวมค่าที่วัดได้…", "Ölçümler toplanıyor…", "Збір показань…", "Đang thu thập số đo…", "正在采集读数…",
        "正在收集讀數…",
    ),
    "Color": (
        "Color", "Barva", "Farbe", "Color", "Kolorea", "Couleur", "Szín", "Colore", "カラー", "컬러", "Spalva", "Kleur",
        "Kolor", "Cor", "Culoare", "Цвет", "Färg", "สี", "Renk", "Колір", "Màu sắc", "颜色", "顏色",
    ),
    "Color differs": (
        "El color és diferent", "Barva se liší", "Farbe weicht ab", "El color difiere", "Kolorea desberdina da",
        "La couleur diffère", "A szín eltérő", "Il colore è diverso", "色が異なります", "색상이 다릅니다", "Spalva skiriasi",
        "Kleur verschilt", "Kolor jest inny", "A cor é diferente", "Culoarea diferă", "Цвет отличается",
        "Färgen skiljer sig åt", "สีแตกต่าง", "Renk farklıdır", "Колір відрізняється", "Màu sắc khác nhau", "颜色不同",
        "顏色不同",
    ),
    "Command accepted. The printer has not confirmed the change.": (
        "S'ha acceptat l'ordre. La impressora no ha confirmat el canvi.", "Příkaz přijat. Tiskárna nepotvrdila změnu.",
        "Befehl angenommen. Der Drucker hat die Änderung nicht bestätigt.",
        "Comando aceptado. La imprenta no ha confirmado el cambio.",
        "Komandoa onartu da. Inprimagailuak ez du aldaketa baieztatu.",
        "Commande acceptée. L'imprimeur n'a pas confirmé le changement.",
        "Parancs elfogadva. A nyomtató nem erősítette meg a változtatást.",
        "Comando accettato. La stampante non ha confermato la modifica.", "コマンドを受け付けました。プリンターは変更を確認していません。",
        "명령이 승인되었습니다. 프린터에서 변경 사항을 확인하지 않았습니다.", "Komanda priimta. Spausdintuvas nepatvirtino pakeitimo.",
        "Commando geaccepteerd. De printer heeft de wijziging niet bevestigd.",
        "Polecenie przyjęte. Drukarka nie potwierdziła zmiany.",
        "Comando aceito. A impressora não confirmou a alteração.",
        "Comanda acceptată. Imprimanta nu a confirmat modificarea.",
        "Команда принята. Принтер не подтвердил изменение.",
        "Kommandot accepterat. Skrivaren har inte bekräftat ändringen.",
        "ยอมรับคำสั่งแล้ว เครื่องพิมพ์ไม่ได้ยืนยันการเปลี่ยนแปลง", "Komut kabul edildi. Yazıcı değişikliği onaylamadı.",
        "Команду прийнято. Принтер не підтвердив зміни.", "Lệnh được chấp nhận. Máy in chưa xác nhận thay đổi.",
        "命令已接受。打印机尚未确认更改。", "指令已接受。印表機尚未確認更改。",
    ),
    "Command failed": (
        "L'ordre ha fallat", "Příkaz selhal", "Befehl fehlgeschlagen", "El comando falló", "Komandoak huts egin du",
        "Échec de la commande", "A parancs nem sikerült", "Comando fallito", "コマンドが失敗しました", "명령이 실패했습니다.",
        "Komanda nepavyko", "Opdracht mislukt", "Polecenie nie powiodło się", "Comando falhou", "Comanda a eșuat",
        "Команда не выполнена", "Kommando misslyckades", "คำสั่งล้มเหลว", "Komut başarısız oldu", "Помилка команди",
        "Lệnh không thành công", "命令失败", "指令失敗",
    ),
    "Command result unavailable.": (
        "Resultat de l'ordre no disponible.", "Výsledek příkazu není k dispozici.", "Befehlsergebnis nicht verfügbar.",
        "El resultado del comando no está disponible.", "Komandoaren emaitza ez dago erabilgarri.",
        "Résultat de la commande indisponible.", "A parancs eredménye nem érhető el.",
        "Risultato del comando non disponibile.", "コマンド結果がありません。", "명령 결과를 확인할 수 없습니다.",
        "Komandos rezultatas nepasiekiamas.", "Commandoresultaat niet beschikbaar.", "Wynik polecenia niedostępny.",
        "Resultado do comando indisponível.", "Rezultatul comenzii nu este disponibil.",
        "Результат команды недоступен.", "Kommandoresultat är inte tillgängligt.", "ผลคำสั่งไม่พร้อมใช้งาน",
        "Komut sonucu mevcut değil.", "Результат команди недоступний.", "Kết quả lệnh không có sẵn.", "命令结果不可用。",
        "命令結果不可用。",
    ),
    "Command unavailable for the current printer state.": (
        "L'ordre no està disponible per a l'estat actual de la impressora.",
        "Příkaz není pro aktuální stav tiskárny dostupný.", "Befehl für den aktuellen Druckerstatus nicht verfügbar.",
        "Comando no disponible para el estado actual de la impresora.",
        "Komandoa ez dago erabilgarri inprimagailuaren uneko egoerarako.",
        "Commande indisponible pour l'état actuel de l'imprimante.",
        "A parancs nem érhető el a nyomtató aktuális állapotához.",
        "Comando non disponibile per lo stato corrente della stampante.", "現在のプリンターの状態ではコマンドを使用できません。",
        "현재 프린터 상태에서는 명령을 사용할 수 없습니다.", "Komanda nepasiekiama dabartinei spausdintuvo būsenai.",
        "Opdracht niet beschikbaar voor de huidige printerstatus.",
        "Polecenie niedostępne dla bieżącego stanu drukarki.",
        "Comando indisponível para o estado atual da impressora.",
        "Comandă indisponibilă pentru starea curentă a imprimantei.",
        "Команда недоступна для текущего состояния принтера.",
        "Kommandot är inte tillgängligt för det aktuella skrivarläget.",
        "คำสั่งไม่พร้อมใช้งานสำหรับสถานะเครื่องพิมพ์ปัจจุบัน", "Mevcut yazıcı durumu için komut mevcut değil.",
        "Команда недоступна для поточного стану принтера.", "Lệnh không khả dụng cho trạng thái máy in hiện tại.",
        "命令对于当前打印机状态不可用。", "指令對於目前印表機狀態不可用。",
    ),
    "Compact": (
        "Compacte", "Kompaktní", "Kompakt", "Compacto", "Trinkoa", "Compacte", "Kompakt", "Compatto", "コンパクト", "콤팩트",
        "Kompaktiškas", "Compact", "Kompaktowy", "Compacto", "Compact", "Компактная", "Kompakt", "กะทัดรัด", "Kompakt",
        "Компактний", "Nhỏ gọn", "紧凑型", "緊湊型",
    ),
    "Configure the printer serial number and CheckCode.": (
        "Configureu el número de sèrie de la impressora i CheckCode.",
        "Nakonfigurujte sériové číslo tiskárny a CheckCode.",
        "Konfigurieren Sie die Seriennummer des Druckers und CheckCode.",
        "Configure el número de serie de la impresora y CheckCode.",
        "Konfiguratu inprimagailuaren serie-zenbakia eta CheckCode.",
        "Configurez le numéro de série de l'imprimante et CheckCode.",
        "Konfigurálja a nyomtató sorozatszámát és a CheckCode-t.",
        "Configurare il numero di serie della stampante e CheckCode.", "プリンターのシリアル番号とCheckCodeを設定します。",
        "프린터 일련번호와 CheckCode를 구성합니다.", "Konfigūruokite spausdintuvo serijos numerį ir CheckCode.",
        "Configureer het serienummer van de printer en CheckCode.", "Skonfiguruj numer seryjny drukarki i CheckCode.",
        "Configure o número de série da impressora e CheckCode.",
        "Configurați numărul de serie al imprimantei și CheckCode.", "Укажите серийный номер принтера и CheckCode.",
        "Konfigurera skrivarens serienummer och CheckCode.", "กำหนดค่าหมายเลขซีเรียลของเครื่องพิมพ์และ CheckCode",
        "Yazıcı seri numarasını ve CheckCode'yi yapılandırın.", "Налаштуйте серійний номер принтера та CheckCode.",
        "Định cấu hình số sê-ri máy in và CheckCode.", "配置打印机序列号和CheckCode。", "設定印表機序號和CheckCode。",
    ),
    "Connect": (
        "Connectar", "Připojte", "Verbinden", "Conectar", "Konektatu", "Connecter", "Csatlakozás", "Connetti", "つながる",
        "연결하다", "Prisijunkite", "Verbinden", "Połącz", "Conectar", "Conectare", "Подключиться", "Anslut", "เชื่อมต่อ",
        "Bağlan", "Підключення", "Kết nối", "连接", "連接",
    ),
    "Connect an Adventurer 5M with its CheckCode first.": (
        "Connecteu primer un Adventurer 5M amb el seu CheckCode.", "Nejprve připojte Adventurer 5M k jeho CheckCode.",
        "Verbinden Sie zuerst einen Adventurer 5M mit seinem CheckCode.",
        "Conecte primero un Adventurer 5M con su CheckCode.", "Konektatu lehenik Adventurer 5M bere CheckCode batekin.",
        "Connectez d'abord un Adventurer 5M avec son CheckCode.",
        "Először csatlakoztasson egy Adventurer 5M-t a CheckCode-hez.",
        "Collegare prima un Adventurer 5M al suo CheckCode.", "まず、Adventurer 5M をその CheckCode に接続します。",
        "먼저 Adventurer 5M를 CheckCode와 연결하세요.", "Pirmiausia prijunkite Adventurer 5M prie jo CheckCode.",
        "Verbind eerst een Adventurer 5M met zijn CheckCode.", "Najpierw połącz Adventurer 5M z jego CheckCode.",
        "Conecte primeiro um Adventurer 5M ao seu CheckCode.", "Conectați mai întâi un Adventurer 5M cu CheckCode.",
        "Сначала подключите Adventurer 5M и укажите его CheckCode.",
        "Anslut en Adventurer 5M med dess CheckCode först.", "เชื่อมต่อ Adventurer 5M กับ CheckCode ก่อน",
        "Önce bir Adventurer 5M'yi CheckCode'sine bağlayın.", "Спочатку підключіть Adventurer 5M до CheckCode.",
        "Trước tiên hãy kết nối Adventurer 5M với CheckCode của nó.", "首先将Adventurer 5M与其CheckCode连接。",
        "先將Adventurer 5M與其CheckCode連接。",
    ),
    "Connect to FlashForge printers on your local network.": (
        "Connecteu-vos a impressores FlashForge de la vostra xarxa local.",
        "Připojte se k tiskárnám FlashForge ve vaší místní síti.",
        "Stellen Sie eine Verbindung zu FlashForge-Druckern in Ihrem lokalen Netzwerk her.",
        "Conéctese a impresoras FlashForge en su red local.",
        "Konektatu zure sare lokaleko FlashForge inprimagailuetara.",
        "Connectez-vous aux imprimantes FlashForge sur votre réseau local.",
        "Csatlakozás FlashForge nyomtatókhoz a helyi hálózaton.",
        "Connettersi alle stampanti FlashForge sulla rete locale.", "ローカルネットワーク上のFlashForgeプリンターに接続します。",
        "로컬 네트워크의 FlashForge 프린터에 연결하세요.", "Prisijunkite prie FlashForge spausdintuvų vietiniame tinkle.",
        "Maak verbinding met FlashForge-printers op uw lokale netwerk.",
        "Połącz się z drukarkami FlashForge w sieci lokalnej.",
        "Conecte-se a impressoras FlashForge em sua rede local.",
        "Conectați-vă la imprimantele FlashForge din rețeaua locală.",
        "Подключитесь к принтерам FlashForge в локальной сети.",
        "Anslut till FlashForge-skrivare i ditt lokala nätverk.",
        "เชื่อมต่อกับเครื่องพิมพ์ FlashForge บนเครือข่ายท้องถิ่นของคุณ",
        "Yerel ağınızdaki FlashForge yazıcılara bağlanın.", "Підключіться до принтерів FlashForge у локальній мережі.",
        "Kết nối với máy in FlashForge trên mạng cục bộ của bạn.", "连接到本地网络上的FlashForge 打印机。",
        "連接到本地網路上的FlashForge 印表機。",
    ),
    "Connected": (
        "Connectat", "Připojeno", "Verbunden", "Conectado", "Konektatuta", "Connecté", "Csatlakoztatva", "Connesso",
        "つながった", "연결됨", "Prisijungta", "Verbonden", "Połączono", "Conectado", "Conectat", "Подключён", "Ansluten",
        "เชื่อมต่อแล้ว", "Bağlı", "Підключено", "Đã kết nối", "已连接", "已連接",
    ),
    "Connected · {transport}": (
        "Connectat · {transport}", "Připojeno · {transport}", "Verbunden · {transport}", "Conectado · {transport}",
        "Konektatuta · {transport}", "Connecté · {transport}", "Csatlakoztatva · {transport}", "Connesso · {transport}",
        "接続済み・{transport}", "연결됨 · {transport}", "Prijungtas · {transport}", "Verbonden · {transport}",
        "Połączono · {transport}", "Conectado · {transport}", "Conectat · {transport}", "Подключено · {transport}",
        "Ansluten · {transport}", "เชื่อมต่อแล้ว · {transport}", "Bağlı · {transport}", "Підключено · {transport}",
        "Đã kết nối · {transport}", "已连接·{transport}", "已連接·{transport}",
    ),
    "Connecting camera…": (
        "S'està connectant la càmera...", "Připojování kamery…", "Kamera anschließen…", "Conectando la cámara…",
        "Kamera konektatzen…", "Connexion de la caméra…", "Kamera csatlakoztatása…", "Collegamento della fotocamera…",
        "カメラを接続中…", "카메라 연결 중…", "Prijungiama kamera…", "Camera aansluiten…", "Podłączanie kamery…",
        "Conectando câmera…", "Se conectează camera...", "Подключение камеры…", "Ansluter kamera...",
        "กำลังเชื่อมต่อกล้อง...", "Kamera bağlanıyor…", "Підключення камери…", "Đang kết nối camera…", "连接相机...",
        "連接相機...",
    ),
    "Connecting to the printer…": (
        "S'està connectant a la impressora...", "Připojování k tiskárně…", "Anschließen an den Drucker…",
        "Conexión a la impresora…", "Inprimagailura konektatzen…", "Connexion à l'imprimante…",
        "Csatlakozás a nyomtatóhoz…", "Connessione alla stampante…", "プリンターに接続中…", "프린터에 연결하는 중…",
        "Jungiamasi prie spausdintuvo...", "Verbinding maken met de printer…", "Podłączanie do drukarki…",
        "Conectando à impressora…", "Se conectează la imprimantă...", "Подключение к принтеру…",
        "Ansluter till skrivaren...", "กำลังเชื่อมต่อกับเครื่องพิมพ์...", "Yazıcıya bağlanılıyor…",
        "Підключення до принтера…", "Đang kết nối với máy in…", "连接到打印机...", "連接到印表機...",
    ),
    "Connecting…": (
        "S'està connectant...", "Připojování…", "Verbinden…", "Conectando…", "Konektatzen…", "Connexion…",
        "Csatlakozás…", "Connessione…", "接続中…", "연결 중…", "Jungiamasi…", "Verbinden…", "Łączenie…", "Conectando…",
        "Se conectează...", "Подключение…", "Ansluter...", "กำลังเชื่อมต่อ...", "Bağlanıyor…", "Підключення…",
        "Đang kết nối…", "正在连接...", "正在連線...",
    ),
    "Connection": (
        "Connexió", "Připojení", "Verbindung", "Conexión", "Konexioa", "Connexion", "Csatlakozás", "Connessione", "接続",
        "연결", "Ryšys", "Verbinding", "Połączenie", "Conexão", "Conexiune", "Подключение", "Anslutning", "การเชื่อมต่อ",
        "Bağlantı", "З'єднання", "Kết nối", "连接", "連接",
    ),
    "Connection check": (
        "Comprovació de connexió", "Kontrola připojení", "Verbindungsprüfung", "Verificación de conexión",
        "Konexioaren egiaztapena", "Vérification de la connexion", "Csatlakozás ellenőrzése",
        "Verifica della connessione", "接続確認", "연결 확인", "Ryšio patikrinimas", "Verbindingscontrole",
        "Kontrola połączenia", "Verificação de conexão", "Verificarea conexiunii", "Проверка подключения",
        "Anslutningskontroll", "ตรวจสอบการเชื่อมต่อ", "Bağlantı kontrolü", "Перевірка підключення", "Kiểm tra kết nối",
        "连接检查", "連線檢查",
    ),
    "Connection lost": (
        "S'ha perdut la connexió", "Spojení ztraceno", "Verbindung unterbrochen", "Conexión perdida",
        "Konexioa galdu da", "Connexion perdue", "A kapcsolat megszakadt", "Connessione persa", "接続が切れました",
        "연결이 끊겼습니다.", "Ryšys nutrūko", "Verbinding verbroken", "Utracono połączenie", "Conexão perdida",
        "Conexiune pierdută", "Соединение потеряно", "Anslutning förlorad", "การเชื่อมต่อขาดหาย", "Bağlantı kesildi",
        "З'єднання втрачено", "Mất kết nối", "连接丢失", "連線遺失",
    ),
    "Connection restored": (
        "S'ha restaurat la connexió", "Spojení obnoveno", "Verbindung wiederhergestellt", "Conexión restaurada",
        "Konexioa berrezarri da", "Connexion rétablie", "A kapcsolat helyreállt", "Connessione ripristinata",
        "接続が回復しました", "연결이 복원되었습니다.", "Ryšys atkurtas", "Verbinding hersteld", "Połączenie przywrócone",
        "Conexão restaurada", "Conexiune restabilită", "Соединение восстановлено", "Anslutningen återställd",
        "คืนการเชื่อมต่อแล้ว", "Bağlantı yeniden sağlandı", "З'єднання відновлено", "Đã khôi phục kết nối", "连接已恢复",
        "連線已恢復",
    ),
    "Copied": (
        "Copiat", "Zkopírováno", "Kopiert", "Copiado", "Kopiatua", "Copié", "Másolva", "Copiato", "コピーしました", "복사됨",
        "Nukopijuota", "Gekopieerd", "Skopiowano", "Copiado", "Copiat", "Скопировано", "Kopierat", "คัดลอกแล้ว",
        "Kopyalandı", "Скопійовано", "Đã sao chép", "已复制", "已複製",
    ),
    "Could not check this printer. Check the address and network permissions.": (
        "No s'ha pogut comprovar aquesta impressora. Comproveu l'adreça i els permisos de xarxa.",
        "Tuto tiskárnu nelze zkontrolovat. Zkontrolujte adresu a síťová oprávnění.",
        "Dieser Drucker konnte nicht überprüft werden. Überprüfen Sie die Adresse und Netzwerkberechtigungen.",
        "No se pudo verificar esta impresora. Verifique la dirección y los permisos de red.",
        "Ezin izan da inprimagailu hau egiaztatu. Egiaztatu helbidea eta sareko baimenak.",
        "Impossible de vérifier cette imprimante. Vérifiez l'adresse et les autorisations réseau.",
        "Nem sikerült ellenőrizni ezt a nyomtatót. Ellenőrizze a címet és a hálózati engedélyeket.",
        "Impossibile controllare questa stampante. Controlla l'indirizzo e le autorizzazioni di rete.",
        "このプリンターを確認できませんでした。アドレスとネットワーク権限を確認してください。", "이 프린터를 확인할 수 없습니다. 주소와 네트워크 권한을 확인하세요.",
        "Nepavyko patikrinti šio spausdintuvo. Patikrinkite adresą ir tinklo leidimus.",
        "Kan deze printer niet controleren. Controleer het adres en de netwerkrechten.",
        "Nie można sprawdzić tej drukarki. Sprawdź adres i uprawnienia sieciowe.",
        "Não foi possível verificar esta impressora. Verifique o endereço e as permissões de rede.",
        "Nu s-a putut verifica această imprimantă. Verificați adresa și permisiunile de rețea.",
        "Не удалось проверить этот принтер. Проверьте адрес и сетевые разрешения.",
        "Kunde inte kontrollera den här skrivaren. Kontrollera adressen och nätverksbehörigheterna.",
        "ไม่สามารถตรวจสอบเครื่องพิมพ์นี้ได้ ตรวจสอบที่อยู่และการอนุญาตเครือข่าย",
        "Bu yazıcı kontrol edilemedi. Adresi ve ağ izinlerini kontrol edin.",
        "Не вдалося перевірити цей принтер. Перевірте адресу та дозволи мережі.",
        "Không thể kiểm tra máy in này. Kiểm tra địa chỉ và quyền mạng.", "无法检查该打印机。检查地址和网络权限。", "無法檢查該印表機。檢查位址和網路權限。",
    ),
    "Could not load files": (
        "No s'han pogut carregar els fitxers", "Nelze načíst soubory", "Dateien konnten nicht geladen werden",
        "No se pudieron cargar archivos", "Ezin izan dira fitxategiak kargatu", "Impossible de charger les fichiers",
        "Nem sikerült betölteni a fájlokat", "Impossibile caricare i file", "ファイルを読み込めませんでした", "파일을 로드할 수 없습니다",
        "Nepavyko įkelti failų", "Kon bestanden niet laden", "Nie można załadować plików",
        "Não foi possível carregar os arquivos", "Nu s-au putut încărca fișierele", "Не удалось загрузить файлы.",
        "Det gick inte att ladda filer", "ไม่สามารถโหลดไฟล์ได้", "Dosyalar yüklenemedi", "Не вдалося завантажити файли",
        "Không thể tải tập tin", "无法加载文件", "無法載入文件",
    ),
    "Could not load the file list": (
        "No s'ha pogut carregar la llista de fitxers", "Nelze načíst seznam souborů",
        "Die Dateiliste konnte nicht geladen werden", "No se pudo cargar la lista de archivos",
        "Ezin izan da fitxategien zerrenda kargatu", "Impossible de charger la liste des fichiers",
        "Nem sikerült betölteni a fájllistát", "Impossibile caricare l'elenco dei file", "ファイルリストを読み込めませんでした",
        "파일 목록을 로드할 수 없습니다", "Nepavyko įkelti failų sąrašo", "Kan de bestandslijst niet laden",
        "Nie można załadować listy plików", "Não foi possível carregar a lista de arquivos",
        "Nu s-a putut încărca lista de fișiere", "Не удалось загрузить список файлов.",
        "Det gick inte att ladda fillistan", "ไม่สามารถโหลดรายการไฟล์ได้", "Dosya listesi yüklenemedi",
        "Не вдалося завантажити список файлів", "Không thể tải danh sách tập tin", "无法加载文件列表", "無法載入文件列表",
    ),
    "Could not open the camera.": (
        "No s'ha pogut obrir la càmera.", "Fotoaparát nelze otevřít.", "Die Kamera konnte nicht geöffnet werden.",
        "No se pudo abrir la cámara.", "Ezin izan da kamera ireki.", "Impossible d'ouvrir la caméra.",
        "Nem sikerült kinyitni a kamerát.", "Impossibile aprire la fotocamera.", "カメラを開けませんでした。", "카메라를 열 수 없습니다.",
        "Nepavyko atidaryti fotoaparato.", "Kan de camera niet openen.", "Nie można otworzyć aparatu.",
        "Não foi possível abrir a câmera.", "Nu s-a putut deschide camera.", "Не удалось открыть камеру.",
        "Kunde inte öppna kameran.", "ไม่สามารถเปิดกล้องได้", "Kamera açılamadı.", "Не вдалося відкрити камеру.",
        "Không thể mở camera.", "无法打开相机。", "無法開啟相機。",
    ),
    "Could not read printer data.": (
        "No s'han pogut llegir les dades de la impressora.", "Nelze přečíst data tiskárny.",
        "Druckerdaten konnten nicht gelesen werden.", "No se pudieron leer los datos de la impresora.",
        "Ezin izan dira inprimagailuaren datuak irakurri.", "Impossible de lire les données de l'imprimante.",
        "Nem sikerült beolvasni a nyomtató adatait.", "Impossibile leggere i dati della stampante.",
        "プリンターデータを読み取れませんでした。", "프린터 데이터를 읽을 수 없습니다.", "Nepavyko nuskaityti spausdintuvo duomenų.",
        "Kon printergegevens niet lezen.", "Nie można odczytać danych drukarki.",
        "Não foi possível ler os dados da impressora.", "Nu s-au putut citi datele imprimantei.",
        "Не удалось прочитать данные принтера.", "Kunde inte läsa skrivardata.", "ไม่สามารถอ่านข้อมูลเครื่องพิมพ์ได้",
        "Yazıcı verileri okunamadı.", "Не вдалося прочитати дані принтера.", "Không thể đọc dữ liệu máy in.",
        "无法读取打印机数据。", "無法讀取印表機資料。",
    ),
    "Could not read the settings.": (
        "No s'ha pogut llegir la configuració.", "Nelze přečíst nastavení.",
        "Die Einstellungen konnten nicht gelesen werden.", "No se pudo leer la configuración.",
        "Ezin izan dira ezarpenak irakurri.", "Impossible de lire les paramètres.",
        "Nem sikerült beolvasni a beállításokat.", "Impossibile leggere le impostazioni.", "設定を読み込めませんでした。",
        "설정을 읽을 수 없습니다.", "Nepavyko nuskaityti nustatymų.", "Kon de instellingen niet lezen.",
        "Nie można odczytać ustawień.", "Não foi possível ler as configurações.", "Nu s-au putut citi setările.",
        "Не удалось прочитать настройки.", "Kunde inte läsa inställningarna.", "ไม่สามารถอ่านการตั้งค่าได้",
        "Ayarlar okunamadı.", "Не вдалося прочитати налаштування.", "Không thể đọc cài đặt.", "无法读取设置。", "無法讀取設定。",
    ),
    "Could not save the settings.": (
        "No s'ha pogut desar la configuració.", "Nelze uložit nastavení.",
        "Die Einstellungen konnten nicht gespeichert werden.", "No se pudo guardar la configuración.",
        "Ezin izan dira ezarpenak gorde.", "Impossible d'enregistrer les paramètres.",
        "Nem sikerült menteni a beállításokat.", "Impossibile salvare le impostazioni.", "設定を保存できませんでした。",
        "설정을 저장할 수 없습니다.", "Nepavyko išsaugoti nustatymų.", "Kon de instellingen niet opslaan.",
        "Nie można zapisać ustawień.", "Não foi possível salvar as configurações.", "Nu s-au putut salva setările.",
        "Не удалось сохранить настройки.", "Kunde inte spara inställningarna.", "ไม่สามารถบันทึกการตั้งค่าได้",
        "Ayarlar kaydedilemedi.", "Не вдалося зберегти налаштування.", "Không thể lưu cài đặt.", "无法保存设置。", "無法儲存設定。",
    ),
    "Creator 5 and 5 Pro require both CheckCode and the printer serial number. Adventurer 5M reads its serial number automatically.": (
        "Creator 5 and 5 Pro requereix tant CheckCode com el número de sèrie de la impressora. Adventurer 5M llegeix el seu número de sèrie automàticament.",
        "Creator 5 and 5 Pro vyžadují CheckCode i sériové číslo tiskárny. Adventurer 5M načte sériové číslo automaticky.",
        "Für Creator 5 and 5 Pro sind sowohl CheckCode als auch die Seriennummer des Druckers erforderlich. Adventurer 5M liest seine Seriennummer automatisch.",
        "Creator 5 and 5 Pro requiere tanto CheckCode como el número de serie de la impresora. Adventurer 5M lee su número de serie automáticamente.",
        "Creator 5 and 5 Pro CheckCode eta inprimagailuaren serie zenbakia behar dira. Adventurer 5M-k bere serie-zenbakia automatikoki irakurtzen du.",
        "Creator 5 and 5 Pro nécessite à la fois CheckCode et le numéro de série de l'imprimante. Adventurer 5M lit automatiquement son numéro de série.",
        "A Creator 5 and 5 Pro esetén a CheckCode és a nyomtató sorozatszáma is szükséges. A Adventurer 5M automatikusan beolvassa a sorozatszámát.",
        "Creator 5 and 5 Pro richiedono sia CheckCode che il numero di serie della stampante. Adventurer 5M legge automaticamente il proprio numero di serie.",
        "Creator 5 and 5 Pro には、CheckCode とプリンターのシリアル番号の両方が必要です。 Adventurer 5M はシリアル番号を自動的に読み取ります。",
        "Creator 5 and 5 Pro에는 CheckCode와 프린터 일련 번호가 모두 필요합니다. Adventurer 5M는 일련번호를 자동으로 읽습니다.",
        "Creator 5 and 5 Pro reikalingas ir CheckCode, ir spausdintuvo serijos numeris. Adventurer 5M automatiškai nuskaito savo serijos numerį.",
        "Voor Creator 5 and 5 Pro zijn zowel CheckCode als het serienummer van de printer vereist. Adventurer 5M leest het serienummer automatisch.",
        "Creator 5 and 5 Pro wymaga zarówno CheckCode, jak i numeru seryjnego drukarki. Adventurer 5M automatycznie odczytuje swój numer seryjny.",
        "Creator 5 and 5 Pro requer CheckCode e o número de série da impressora. Adventurer 5M lê seu número de série automaticamente.",
        "Creator 5 and 5 Pro necesită atât CheckCode, cât și numărul de serie al imprimantei. Adventurer 5M citește automat numărul de serie.",
        "Для Creator 5 и 5 Pro нужны CheckCode и серийный номер. У Adventurer 5M серийный номер определяется автоматически.",
        "Creator 5 and 5 Pro kräver både CheckCode och skrivarens serienummer. Adventurer 5M läser dess serienummer automatiskt.",
        "Creator 5 and 5 Pro ต้องใช้ทั้ง CheckCode และหมายเลขซีเรียลของเครื่องพิมพ์ Adventurer 5M อ่านหมายเลขซีเรียลโดยอัตโนมัติ",
        "Creator 5 and 5 Pro, hem CheckCode hem de yazıcı seri numarasını gerektirir. Adventurer 5M seri numarasını otomatik olarak okur.",
        "Creator 5 and 5 Pro вимагає як CheckCode, так і серійний номер принтера. Adventurer 5M автоматично зчитує серійний номер.",
        "Creator 5 and 5 Pro yêu cầu cả CheckCode và số sê-ri máy in. Adventurer 5M tự động đọc số sê-ri của nó.",
        "Creator 5 and 5 Pro 需要CheckCode 和打印机序列号。 Adventurer 5M 自动读取其序列号。",
        "Creator 5 and 5 Pro 需要CheckCode 和印表機序號。 Adventurer 5M 自動讀取其序號。",
    ),
    "Dark theme": (
        "Tema fosc", "Tmavý motiv", "Dunkles Thema", "Tema oscuro", "Gai iluna", "Thème sombre", "Sötét téma",
        "Tema scuro", "ダークテーマ", "어두운 테마", "Tamsi tema", "Donker thema", "Ciemny motyw", "Tema escuro",
        "Tema întunecată", "Темная тема", "Mörkt tema", "ธีมสีเข้ม", "Karanlık tema", "Темна тема", "Chủ đề tối",
        "深色主题", "深色主題",
    ),
    "Device": (
        "Dispositiu", "Zařízení", "Gerät", "Dispositivo", "Gailua", "Appareil", "Nyomtató", "Dispositivo", "デバイス", "장치",
        "Įrenginys", "Apparaat", "Urządzenie", "Dispositivo", "Dispozitiv", "Принтер", "Enhet", "อุปกรณ์", "Cihaz",
        "Пристрій", "Thiết bị", "设备", "裝置",
    ),
    "Disconnect": (
        "Desconnectar", "Odpojit", "Trennen", "Desconectar", "Deskonektatu", "Déconnecter", "Leválasztás",
        "Disconnetti", "切断", "연결 끊기", "Atjunkite", "Verbinding verbreken", "Rozłącz", "Desconectar", "Deconectați",
        "Отключиться", "Koppla bort", "ตัดการเชื่อมต่อ", "Bağlantıyı Kes", "Відключити", "Ngắt kết nối", "断开连接", "斷開連接",
    ),
    "Disconnected": (
        "Desconnectat", "Odpojeno", "Nicht verbunden", "Desconectado", "Deskonektatuta", "Déconnecté", "Megszakadt",
        "Disconnesso", "切断されました", "연결 끊김", "Atjungtas", "Verbinding verbroken", "Rozłączono", "Desconectado",
        "Deconectat", "Нет связи", "Frånkopplad", "ตัดการเชื่อมต่อ", "Bağlantı Kesildi", "Відключено",
        "Đã ngắt kết nối", "断开连接", "斷開連接",
    ),
    "Down": (
        "Avall", "Dolů", "Unten", "Abajo", "Behera", "Bas", "Le", "Giù", "下へ", "다운", "Žemyn", "Omlaag", "W dół",
        "Para baixo", "Jos", "Вниз", "Ner", "ลง", "Aşağı", "Вниз", "Xuống", "下", "下",
    ),
    "Edit slot": (
        "Edita l'espai", "Upravit slot", "Steckplatz bearbeiten", "Editar espacio", "Editatu zirrikitua",
        "Modifier l'emplacement", "Szerkesztési hely", "Modifica slot", "スロットの編集", "슬롯 편집", "Redaguoti lizdą",
        "Slot bewerken", "Edytuj miejsce", "Editar slot", "Editați slotul", "Редактировать слот", "Redigera plats",
        "แก้ไขช่อง", "Slotu düzenle", "Слот редагування", "Chỉnh sửa vị trí", "编辑槽位", "編輯槽位",
    ),
    "Edit slot {slot}": (
        "Edita la ranura {slot}", "Upravit slot {slot}", "Steckplatz bearbeiten {slot}", "Editar ranura {slot}",
        "Editatu zirrikitua {slot}", "Modifier l'emplacement {slot}", "{slot} bővítőhely szerkesztése",
        "Modifica slot {slot}", "スロット編集 {slot}", "슬롯 {slot} 편집", "Redaguoti lizdą {slot}", "Slot {slot} bewerken",
        "Edytuj szczelinę {slot}", "Editar slot {slot}", "Editați slotul {slot}", "Редактировать слот {slot}",
        "Redigera kortplats {slot}", "แก้ไขช่อง {slot}", "Slotu düzenle {slot}", "Слот редагування {slot}",
        "Chỉnh sửa vị trí {slot}", "编辑槽 {slot}", "編輯槽 {slot}",
    ),
    "Elapsed": (
        "Transcorregut", "Uplynulo", "Verstrichen", "Transcurrido", "Igaro", "Écoulé", "Eltelt", "Trascorso", "経過",
        "경과", "Praėjo", "Verstreken", "Upłynęło", "Decorrido", "S-a scurs", "Прошло", "Förfluten", "ผ่านไปแล้ว",
        "Geçen", "Минуло", "Đã qua", "已过去", "已過去",
    ),
    "Empty": (
        "Buit", "Prázdné", "Leer", "Vacío", "Hutsik", "Vide", "Üres", "Vuoto", "空", "비어 있음", "Tuščia", "Leeg", "Puste",
        "Vazio", "Gol", "Пусто", "Tom", "ว่างเปล่า", "Boş", "Порожньо", "Trống", "空", "空",
    ),
    "Enable LAN-only mode on the printer.": (
        "Habiliteu el mode només LAN a la impressora.", "Povolte na tiskárně režim pouze LAN.",
        "Aktivieren Sie den Nur-LAN-Modus auf dem Drucker.", "Habilite el modo solo LAN en la impresora.",
        "Gaitu LAN soilik modua inprimagailuan.", "Activez le mode LAN uniquement sur l'imprimante.",
        "Engedélyezze a csak LAN módot a nyomtatón.", "Abilita la modalità solo LAN sulla stampante.",
        "プリンターでLAN専用モードを有効にします。", "프린터에서 LAN 전용 모드를 활성화합니다.", "Įjunkite spausdintuvo tik LAN režimą.",
        "Schakel de modus Alleen LAN in op de printer.", "Włącz tryb tylko LAN w drukarce.",
        "Ative o modo somente LAN na impressora.", "Activați modul numai pentru LAN pe imprimantă.",
        "Включите на принтере режим работы только по локальной сети.", "Aktivera endast LAN-läget på skrivaren.",
        "เปิดใช้งานโหมดเฉพาะ LAN บนเครื่องพิมพ์", "Yazıcıda yalnızca LAN modunu etkinleştirin.",
        "Увімкніть на принтері режим лише LAN.", "Bật chế độ chỉ LAN trên máy in.", "在打印机上启用仅LAN 模式。",
        "在印表機上啟用僅LAN 模式。",
    ),
    "Enlarge camera": (
        "Ampliar la càmera", "Zvětšení kamery", "Kamera vergrößern", "Ampliar cámara", "Kamera handitu",
        "Agrandir la caméra", "Kamera nagyítása", "Ingrandisci la fotocamera", "カメラを拡大する", "카메라 확대",
        "Padidinti fotoaparatą", "Camera vergroten", "Powiększ kamerę", "Ampliar câmera", "Măriți camera",
        "Увеличить изображение камеры", "Förstora kameran", "ขยายกล้อง", "Kamerayı büyüt", "Збільшити камеру",
        "Phóng to máy ảnh", "放大相机", "放大相機",
    ),
    "Enter a local IPv4 address for the printer.": (
        "Introduïu una adreça IPv4 local per a la impressora.", "Zadejte místní adresu IPv4 pro tiskárnu.",
        "Geben Sie eine lokale IPv4-Adresse für den Drucker ein.",
        "Introduzca una dirección IPv4 local para la impresora.", "Sartu inprimagailurako tokiko IPv4 helbide bat.",
        "Saisissez une adresse IPv4 locale pour l'imprimante.", "Adja meg a nyomtató helyi IPv4-címét.",
        "Immettere un indirizzo IPv4 locale per la stampante.", "プリンターのローカル IPv4 アドレスを入力します。",
        "프린터의 로컬 IPv4 주소를 입력하세요.", "Įveskite vietinį spausdintuvo IPv4 adresą.",
        "Voer een lokaal IPv4-adres voor de printer in.", "Wprowadź lokalny adres IPv4 drukarki.",
        "Insira um endereço IPv4 local para a impressora.", "Introduceți o adresă IPv4 locală pentru imprimantă.",
        "Введите локальный адрес IPv4 для принтера.", "Ange en lokal IPv4-adress för skrivaren.",
        "ป้อนที่อยู่ IPv4 ในเครื่องสำหรับเครื่องพิมพ์", "Yazıcı için yerel bir IPv4 adresi girin.",
        "Введіть локальну адресу IPv4 для принтера.", "Nhập địa chỉ IPv4 cục bộ cho máy in.", "输入打印机的本地IPv4 地址。",
        "輸入印表機的本機IPv4 位址。",
    ),
    "Enter a page number": (
        "Introduïu un número de pàgina", "Zadejte číslo stránky", "Geben Sie eine Seitenzahl ein",
        "Introduzca un número de página", "Sartu orrialde-zenbaki bat", "Entrez un numéro de page",
        "Írjon be egy oldalszámot", "Immettere un numero di pagina", "ページ番号を入力してください", "페이지 번호를 입력하세요",
        "Įveskite puslapio numerį", "Voer een paginanummer in", "Wprowadź numer strony", "Insira um número de página",
        "Introduceți un număr de pagină", "Введите номер страницы.", "Ange ett sidnummer", "ป้อนหมายเลขหน้า",
        "Bir sayfa numarası girin", "Введіть номер сторінки", "Nhập số trang", "输入页码", "輸入頁碼",
    ),
    "Enter your CheckCode in the plugin settings": (
        "Introduïu el vostre CheckCode a la configuració del connector", "Zadejte svůj CheckCode do nastavení pluginu",
        "Geben Sie Ihr CheckCode in den Plugin-Einstellungen ein",
        "Ingrese su CheckCode en la configuración del complemento", "Sartu zure CheckCode pluginaren ezarpenetan",
        "Entrez votre CheckCode dans les paramètres du plugin", "Adja meg CheckCode-jét a bővítmény beállításaiban",
        "Inserisci il tuo CheckCode nelle impostazioni del plugin", "プラグイン設定にCheckCodeを入力してください",
        "플러그인 설정에서 CheckCode를 입력하세요", "Įskiepio nustatymuose įveskite savo CheckCode",
        "Voer uw CheckCode in de plugin-instellingen in", "Wprowadź swój CheckCode w ustawieniach wtyczki",
        "Insira seu CheckCode nas configurações do plugin", "Introduceți CheckCode în setările pluginului",
        "Введите свой CheckCode в настройках плагина.", "Ange din CheckCode i plugin-inställningarna",
        "ป้อน CheckCode ของคุณในการตั้งค่าปลั๊กอิน", "CheckCode'nizi eklenti ayarlarına girin",
        "Введіть свій CheckCode у налаштуваннях плагіна", "Nhập CheckCode của bạn vào cài đặt plugin",
        "在插件设置中输入您的CheckCode", "在插件設定中輸入您的CheckCode",
    ),
    "Enter your printer address in the plugin settings.": (
        "Introduïu l'adreça de la impressora a la configuració del connector.",
        "V nastavení pluginu zadejte adresu vaší tiskárny.",
        "Geben Sie Ihre Druckeradresse in den Plugin-Einstellungen ein.",
        "Ingrese la dirección de su impresora en la configuración del complemento.",
        "Sartu zure inprimagailuaren helbidea pluginaren ezarpenetan.",
        "Entrez l'adresse de votre imprimante dans les paramètres du plugin.",
        "Adja meg a nyomtató címét a bővítmény beállításaiban.",
        "Inserisci l'indirizzo della tua stampante nelle impostazioni del plugin.", "プラグイン設定にプリンターのアドレスを入力します。",
        "플러그인 설정에서 프린터 주소를 입력하세요.", "Įskiepio nustatymuose įveskite savo spausdintuvo adresą.",
        "Voer uw printeradres in de plug-ininstellingen in.", "Wprowadź adres swojej drukarki w ustawieniach wtyczki.",
        "Insira o endereço da sua impressora nas configurações do plugin.",
        "Introduceți adresa imprimantei în setările pluginului.", "Введите адрес вашего принтера в настройках плагина.",
        "Ange din skrivaradress i plugin-inställningarna.", "ป้อนที่อยู่เครื่องพิมพ์ของคุณในการตั้งค่าปลั๊กอิน",
        "Yazıcınızın adresini eklenti ayarlarında girin.", "Введіть адресу свого принтера в налаштуваннях плагіна.",
        "Nhập địa chỉ máy in của bạn trong cài đặt plugin.", "在插件设置中输入您的打印机地址。", "在插件設定中輸入您的印表機位址。",
    ),
    "Error": (
        "Error", "Chyba", "Fehler", "Error", "Errorea", "Erreur", "Hiba", "Errore", "エラー", "오류", "Klaida", "Fout",
        "Błąd", "Erro", "Eroare", "Ошибка", "Fel", "ข้อผิดพลาด", "Hata", "Помилка", "Lỗi", "错误", "錯誤",
    ),
    "Error: {code}": (
        "Error: {code}", "Chyba: {code}", "Fehler: {code}", "Error: {code}", "Errorea: {code}", "Erreur : {code}",
        "Hiba: {code}", "Errore: {code}", "エラー: {code}", "오류: {code}", "Klaida: {code}", "Fout: {code}", "Błąd: {code}",
        "Erro: {code}", "Eroare: {code}", "Ошибка: {code}", "Fel: {code}", "ข้อผิดพลาด: {code}", "Hata: {code}",
        "Помилка: {code}", "Lỗi: {code}", "错误：{code}", "錯誤：{code}",
    ),
    "Errors and cancellations": (
        "Errors i cancel·lacions", "Chyby a zrušení", "Fehler und Stornierungen", "Errores y cancelaciones",
        "Akatsak eta baliogabetzeak", "Erreurs et annulations", "Hibák és törlések", "Errori e cancellazioni",
        "エラーとキャンセル", "오류 및 취소", "Klaidos ir atšaukimai", "Fouten en annuleringen", "Błędy i anulowania",
        "Erros e cancelamentos", "Erori și anulări", "Ошибки и отмены", "Fel och avbokningar", "ข้อผิดพลาดและการยกเลิก",
        "Hatalar ve iptaller", "Помилки та скасування", "Lỗi và hủy", "错误和取消", "錯誤與取消",
    ),
    "Feed": (
        "Carrega", "Zavést filament", "Filament fördern", "Empujar filamento", "Sartu filamentua",
        "Avancer le filament", "Filament betolása", "Avanza filamento", "送り出す", "필라멘트 밀기", "Stumti giją",
        "Filament doorvoeren", "Wsuń filament", "Avançar filamento", "Împinge filamentul", "Подать", "Mata fram",
        "ป้อนเส้น", "Filamenti ilerlet", "Подати", "Đẩy sợi in", "进料", "進料",
    ),
    "Filament": (
        "Filament", "Filament", "Filament", "Filamento", "Filamentua", "Filament", "Filament", "Filamento", "フィラメント",
        "필라멘트", "Gija", "Filament", "Filament", "Filamento", "Filament", "Филамент", "Filament", "เส้นพลาสติก",
        "Filament", "Філамент", "Sợi in", "耗材", "線材",
    ),
    "Filament movement": (
        "Moviment del filament", "Pohyb vlákna", "Filamentbewegung", "Movimiento del filamento",
        "Filamentuen mugimendua", "Mouvement des filaments", "Izzószál mozgása", "Movimento del filamento", "フィラメントの動き",
        "필라멘트의 움직임", "Kaitinamojo siūlelio judėjimas", "Filamentbeweging", "Ruch włókien", "Movimento do filamento",
        "Mișcarea filamentului", "Движение филамента", "Filamentrörelse", "การเคลื่อนไหวของเส้นใย", "Filament hareketi",
        "Рух нитки", "Chuyển động của dây tóc", "灯丝运动", "燈絲運動",
    ),
    "Filament {number} requires {material}.": (
        "El filament {number} requereix {material}.", "Vlákno {number} vyžaduje {material}.",
        "Filament {number} erfordert {material}.", "El filamento {number} requiere {material}.",
        "Filament {number} {material} behar du.", "Le filament {number} nécessite {material}.",
        "A {number} izzószálhoz a {material} szükséges.", "Il filamento {number} richiede {material}.",
        "フィラメント {number} には {material} が必要です。", "필라멘트 {number}에는 {material}가 필요합니다.",
        "Filamentui {number} reikalingas {material}.", "Filament {number} vereist {material}.",
        "Filament {number} wymaga {material}.", "O filamento {number} requer {material}.",
        "Filamentul {number} necesită {material}.", "Для филамента {number} нужен материал {material}.",
        "Filament {number} kräver {material}.", "เส้นใย {number} ต้องใช้ {material}",
        "Filament {number}, {material} gerektirir.", "Нитка {number} вимагає {material}.",
        "Sợi in {number} cần vật liệu {material}.", "灯丝{number} 需要{material}。", "燈絲{number} 需要{material}。",
    ),
    "File": (
        "Fitxer", "Soubor", "Datei", "Archivo", "Fitxategia", "Fichier", "Fájl", "File", "ファイル", "파일", "Failas",
        "Bestand", "Plik", "Arquivo", "Fișier", "Файл", "Fil", "ไฟล์", "Dosya", "Файл", "Tệp", "文件", "文件",
    ),
    "Filter by printer status": (
        "Filtra per estat de la impressora", "Filtrování podle stavu tiskárny", "Nach Druckerstatus filtern",
        "Filtrar por estado de la impresora", "Iragazi inprimagailuaren egoeraren arabera",
        "Filtrer par état de l'imprimante", "Szűrés a nyomtató állapota szerint", "Filtra per stato della stampante",
        "プリンターのステータスで絞り込む", "프린터 상태별 필터링", "Filtruoti pagal spausdintuvo būseną", "Filter op printerstatus",
        "Filtruj według stanu drukarki", "Filtrar por status da impressora", "Filtrați după starea imprimantei",
        "Фильтр по состоянию принтера", "Filtrera efter skrivarstatus", "กรองตามสถานะเครื่องพิมพ์",
        "Yazıcı durumuna göre filtrele", "Фільтрувати за станом принтера", "Lọc theo trạng thái máy in", "按打印机状态过滤",
        "按印表機狀態過濾",
    ),
    "Find a file": (
        "Cerca un fitxer", "Najít soubor", "Eine Datei suchen", "Buscar un archivo", "Bilatu fitxategi bat",
        "Rechercher un fichier", "Fájl keresése", "Trova un file", "ファイルを探す", "파일 찾기", "Raskite failą",
        "Zoek een bestand", "Znajdź plik", "Localizar um arquivo", "Găsiți un fișier", "Найти файл", "Hitta en fil",
        "ค้นหาไฟล์", "Bir dosya bul", "Знайдіть файл", "Tìm tập tin", "查找文件", "查找文件",
    ),
    "Find a history entry": (
        "Cerca una entrada de l'historial", "Najít záznam historie", "Suchen Sie nach einem Verlaufseintrag",
        "Buscar una entrada del historial", "Bilatu historiako sarrera bat", "Rechercher une entrée de l'historique",
        "Előzménybejegyzés keresése", "Trova una voce della cronologia", "履歴エントリを検索する", "기록 항목 찾기",
        "Raskite istorijos įrašą", "Zoek een geschiedenisitem", "Znajdź wpis w historii",
        "Encontre uma entrada no histórico", "Găsiți o intrare în istoric", "Найти запись в истории",
        "Hitta en historikpost", "ค้นหารายการประวัติ", "Bir geçmiş girişi bulun", "Знайдіть запис в історії",
        "Tìm mục lịch sử", "查找历史条目", "查找歷史條目",
    ),
    "Find printers": (
        "Cerca impressores", "Najít tiskárny", "Drucker finden", "Buscar impresoras", "Bilatu inprimagailuak",
        "Rechercher des imprimantes", "Nyomtatók keresése", "Trova stampanti", "プリンターを探す", "프린터 찾기",
        "Raskite spausdintuvus", "Zoek printers", "Znajdź drukarki", "Encontrar impressoras", "Găsiți imprimante",
        "Найти принтеры", "Hitta skrivare", "ค้นหาเครื่องพิมพ์", "Yazıcıları bulun", "Знайти принтери", "Tìm máy in",
        "查找打印机", "尋找印表機",
    ),
    "Finish": (
        "Acabat", "Dokončit", "Fertig", "Finalizar", "Amaitu", "Terminer", "Befejezés", "Fine", "仕上げ", "마무리", "Baigti",
        "Voltooien", "Zakończ", "Concluir", "Terminare", "Окончание", "Avsluta", "เสร็จสิ้น", "Bitir", "Завершити",
        "Kết thúc", "完成", "完成",
    ),
    "Finished. Printer ready.": (
        "Acabat. Impressora a punt.", "Hotovo. Tiskárna připravena.", "Fertig. Drucker bereit.",
        "Terminado. Impresora lista.", "Amaitu. Inprimagailua prest.", "Terminé. Imprimante prête.",
        "Kész. Nyomtató készen áll.", "Finito. Stampante pronta.", "終了しました。プリンターの準備ができました。", "끝났습니다. 프린터가 준비되었습니다.",
        "Baigta. Spausdintuvas paruoštas.", "Klaar. Printer gereed.", "Zakończono. Drukarka gotowa.",
        "Concluído. Impressora pronta.", "Terminat. Imprimanta gata.", "Завершено. Принтер готов.",
        "Färdig. Skrivaren klar.", "เสร็จสิ้นแล้ว เครื่องพิมพ์พร้อม", "Bitti. Yazıcı hazır.",
        "Готово. Принтер готовий.", "Đã hoàn thành. Máy in đã sẵn sàng.", "完成。打印机准备就绪。", "完成。印表機準備就緒。",
    ),
    "Follow OrcaSlicer": (
        "Idioma de l’OrcaSlicer", "Podle OrcaSliceru", "Sprache von OrcaSlicer", "Idioma de OrcaSlicer",
        "OrcaSlicer-en hizkuntza", "Langue d’OrcaSlicer", "Az OrcaSlicer nyelve", "Lingua di OrcaSlicer",
        "OrcaSlicerの言語に従う", "OrcaSlicer 언어 사용", "OrcaSlicer kalba", "Taal van OrcaSlicer", "Język OrcaSlicer",
        "Idioma do OrcaSlicer", "Limba OrcaSlicer", "Язык OrcaSlicer", "OrcaSlicers språk", "ใช้ภาษาของ OrcaSlicer",
        "OrcaSlicer dilini kullan", "Мова OrcaSlicer", "Dùng ngôn ngữ OrcaSlicer", "跟随 OrcaSlicer", "跟隨 OrcaSlicer",
    ),
    "Heat nozzle to 180 °C": (
        "Escalfar el broquet a 180 °C", "Zahřejte trysku na 180 °C", "Düse auf 180 °C erwärmen",
        "Calentar la boquilla a 180 °C", "Berotu pita 180 °C-ra", "Chauffer la buse à 180 °C",
        "Melegítse fel a fúvókát 180 °C-ra", "Riscaldare l'ugello a 180 °C", "ノズルを180℃に加熱する", "노즐을 180°C로 가열",
        "Antgalį pašildykite iki 180 °C", "Verwarm het mondstuk tot 180 °C", "Podgrzej dyszę do 180°C",
        "Aquecer o bico a 180 °C", "Încălziți duza la 180 °C", "Нагрейте сопло до 180 °C.",
        "Värm munstycket till 180 °C", "หัวฉีดความร้อนถึง 180 °C", "Memeyi 180 °C'ye ısıtın",
        "Нагрійте сопло до 180 °C", "Làm nóng vòi phun đến 180 °C", "将喷嘴加热至180°C", "將噴嘴加熱至180°C",
    ),
    "Heat off": (
        "Escalfament apagat", "Vyhřívání vypnuto", "Heizung aus", "Calor apagado", "Beroa itzali", "Arrêt du chauffage",
        "Fűtés kikapcsolása", "Riscaldamento spento", "ヒートオフ", "난방 끄기", "Šildymas išjungtas", "Verwarming uit",
        "Wyłącz ogrzewanie", "Aquecimento desligado", "Încălzire oprită", "Выключить нагрев", "Värme av", "ปิดความร้อน",
        "Isıtma kapalı", "Вимкнути тепло", "Giảm nhiệt", "加热关闭", "加熱關閉",
    ),
    "Heat the nozzle to at least 180 °C.": (
        "Escalfeu el broquet com a mínim a 180 °C.", "Zahřejte trysku alespoň na 180 °C.",
        "Erhitzen Sie die Düse auf mindestens 180 °C.", "Calentar la boquilla al menos a 180 °C.",
        "Berotu pita gutxienez 180 °C-ra.", "Chauffer la buse à au moins 180 °C.",
        "Melegítse fel a fúvókát legalább 180 °C-ra.", "Riscaldare l'ugello ad almeno 180 °C.", "ノズルを180℃以上に加熱します。",
        "노즐을 최소 180°C로 가열하세요.", "Įkaitinkite antgalį bent iki 180 °C.", "Verwarm het mondstuk tot minimaal 180 °C.",
        "Rozgrzej dyszę do temperatury co najmniej 180 °C.", "Aqueça o bico a pelo menos 180 °C.",
        "Încălziți duza la cel puțin 180 °C.", "Нагрейте сопло минимум до 180 °C.",
        "Värm munstycket till minst 180 °C.", "ตั้งอุณหภูมิหัวฉีดให้ร้อนอย่างน้อย 180 °C",
        "Nozulu en az 180 °C'ye ısıtın.", "Нагрійте сопло щонайменше до 180 °C.",
        "Làm nóng vòi phun đến nhiệt độ ít nhất là 180 °C.", "将喷嘴加热到至少180 °C。", "將噴嘴加熱至至少180 °C。",
    ),
    "Heating": (
        "Escalfament", "Zahřívání", "Aufheizen", "Calentando", "Berotzen", "Chauffe", "Felfűtés", "Riscaldamento",
        "加熱中", "가열 중", "Kaitinimas", "Opwarmen", "Nagrzewanie", "Aquecendo", "Încălzire", "Нагрев", "Uppvärmning",
        "กำลังทำความร้อน", "Isınıyor", "Нагрівання", "Đang gia nhiệt", "正在加热", "正在加熱",
    ),
    "History": (
        "Historial", "Historie", "Verlauf", "Historial", "Historia", "Historique", "Előzmények", "Cronologia", "履歴",
        "기록", "Istorija", "Geschiedenis", "Historia", "Histórico", "Istoric", "История", "Historik", "ประวัติ",
        "Geçmiş", "Історія", "Lịch sử", "历史记录", "歷史記錄",
    ),
    "Home": (
        "Origen", "Nastavit výchozí polohu", "Referenzfahrt", "Llevar al origen", "Hasierako posiziora",
        "Prise d’origine", "Alaphelyzet", "Azzera assi", "原点復帰", "원점 복귀", "Nustatyti pradinę padėtį", "Naar nulpunt",
        "Bazuj osie", "Ir à origem", "Mergi la origine", "В ноль", "Kör till nolläget", "กลับจุดเริ่มต้น",
        "Başlangıç konumuna git", "У нуль", "Về gốc tọa độ", "回零", "回原點",
    ),
    "Home all axes": (
        "Porta tots els eixos a l’origen", "Nastavit výchozí polohu všech os", "Alle Achsen referenzieren",
        "Llevar todos los ejes al origen", "Eraman ardatz guztiak hasierako posiziora",
        "Faire la prise d’origine de tous les axes", "Minden tengely alaphelyzetbe állítása",
        "Porta tutti gli assi all’origine", "全軸を原点復帰", "모든 축 원점 복귀", "Nustatyti visų ašių pradinę padėtį",
        "Alle assen naar het nulpunt", "Bazuj wszystkie osie", "Levar todos os eixos à origem",
        "Du toate axele la origine", "Вернуть все оси в ноль", "Kör alla axlar till nolläget",
        "กลับสู่จุดเริ่มต้นทุกแกน", "Tüm eksenleri başlangıç konumuna getir", "Обнулити всі осі",
        "Đưa mọi trục về gốc tọa độ", "所有轴回零", "所有軸回原點",
    ),
    "Home required": (
        "Cal tornar a l’origen", "Je nutné nastavit výchozí polohu", "Referenzfahrt erforderlich",
        "Es necesario llevar los ejes al origen", "Hasierako posiziora itzuli behar da", "Prise d’origine requise",
        "Alaphelyzetbe állítás szükséges", "Azzeramento degli assi richiesto", "原点復帰が必要", "원점 복귀 필요",
        "Reikia nustatyti pradinę padėtį", "Nulpunt instellen vereist", "Wymagane bazowanie",
        "É necessário voltar à origem", "Revenire la origine necesară", "Нужно вернуть оси в ноль",
        "Nollställning krävs", "ต้องกลับจุดเริ่มต้นก่อน", "Başlangıç konumuna dönülmeli", "Потрібне обнулення осей",
        "Cần đưa trục về gốc", "请先回零", "請先回原點",
    ),
    "Home sent. Waiting for the printer to confirm completion.": (
        "S’ha enviat l’ordre de tornar a l’origen. Esperant que la impressora confirmi que ha acabat.",
        "Příkaz k návratu do výchozí polohy byl odeslán. Čeká se na potvrzení dokončení tiskárnou.",
        "Referenzfahrt angefordert. Warten auf die Bestätigung des Druckers.",
        "Se envió la orden de volver al origen. Esperando la confirmación de la impresora.",
        "Hasierako posiziora itzultzeko agindua bidali da. Inprimagailuaren baieztapenaren zain.",
        "Prise d’origine demandée. En attente de la confirmation de l’imprimante.",
        "Az alaphelyzetbe állítás parancsa elküldve. Várakozás a nyomtató visszaigazolására.",
        "Comando di azzeramento degli assi inviato. In attesa della conferma della stampante.",
        "原点復帰コマンドを送信しました。プリンターが完了を報告するまでお待ちください。", "원점 복귀 명령을 보냈습니다. 프린터의 완료 확인을 기다리는 중입니다.",
        "Išsiųsta pradinės padėties nustatymo komanda. Laukiama, kol spausdintuvas patvirtins pabaigą.",
        "Opdracht om naar het nulpunt te gaan verzonden. Wachten op bevestiging van de printer.",
        "Wysłano polecenie bazowania osi. Oczekiwanie na potwierdzenie zakończenia przez drukarkę.",
        "Comando de retorno à origem enviado. Aguardando a confirmação da impressora.",
        "Comanda de revenire la origine a fost trimisă. Se așteaptă confirmarea imprimantei.",
        "Команда возврата в ноль отправлена. Ожидание подтверждения от принтера.",
        "Kommandot att köra till nolläget har skickats. Väntar på bekräftelse från skrivaren.",
        "ส่งคำสั่งให้แกนกลับจุดเริ่มต้นแล้ว กำลังรอเครื่องพิมพ์ยืนยันว่าเสร็จสิ้น",
        "Başlangıç konumuna dönme komutu gönderildi. Yazıcının tamamlandığını onaylaması bekleniyor.",
        "Команду повернення осей у нуль надіслано. Очікування підтвердження від принтера.",
        "Đã gửi lệnh đưa các trục về gốc. Đang chờ máy in xác nhận đã hoàn tất.", "已发送回零命令，正在等待打印机确认完成。",
        "已傳送回原點指令，正在等待印表機確認完成。",
    ),
    "Homing…": (
        "Inici...", "Návrat domů…", "Referenzfahrt…", "Regresando…", "Hasiera…", "Retour à destination…", "Homing…",
        "Homing...", "ホーミング…", "호밍…", "Patalpos...", "Op weg…", "Powrót do bazy…", "Retorno…", "Homing...",
        "Возврат осей в ноль…", "Hemsökning...", "กลับบ้าน…", "Hedefe dönüş…", "Наведення…", "Về nhà…", "归位…", "歸位…",
    ),
    "IPv4 address": (
        "Adreça IPv4", "Adresa IPv4", "IPv4-Adresse", "Dirección IPv4", "IPv4 helbidea", "Adresse IPv4", "IPv4 cím",
        "Indirizzo IPv4", "IPv4アドレス", "IPv4 주소", "IPv4 adresas", "IPv4-adres", "Adres IPv4", "Endereço IPv4",
        "adresa IPv4", "Адрес IPv4", "IPv4-adress", "ที่อยู่ IPv4", "IPv4 adresi", "Адреса IPv4", "Địa chỉ IPv4",
        "IPv4地址", "IPv4位址",
    ),
    "Idle": (
        "Inactiu", "Nečinný", "Leerlauf", "Inactivo", "Inaktibo", "Inactif", "Üresjárat", "Inattivo", "アイドル", "유휴 상태",
        "Neveikia", "Inactief", "Bezczynność", "Inativo", "Inactiv", "Без задания", "Inaktiv", "ไม่ได้ใช้งาน", "Boşta",
        "Бездіяльність", "Nhàn rỗi", "空闲", "空閒",
    ),
    "In Progress": (
        "En curs", "Probíhá", "In Bearbeitung", "En progreso", "Eraikitzen", "En cours", "Folyamatban", "In corso",
        "進行中", "진행 중", "Vykdoma", "In uitvoering", "W toku", "Em andamento", "În curs", "В процессе", "Pågår",
        "อยู่ระหว่างดำเนินการ", "Devam Ediyor", "У процесі", "Đang tiến hành", "进行中", "進行中",
    ),
    "In progress": (
        "En curs", "Probíhá", "In Bearbeitung", "En curso", "Abian", "En cours", "Folyamatban", "In corso", "進行中",
        "진행중", "Vykdoma", "Bezig", "W toku", "Em andamento", "În curs", "В процессе", "Pågår", "อยู่ระหว่างดำเนินการ",
        "Devam ediyor", "У процесі", "Đang tiến hành", "进行中", "進行中",
    ),
    "Input shaper": (
        "Compensació de vibracions", "Kompenzace vibrací", "Input Shaper", "Compensación de vibraciones",
        "Sarrera-moldaketa", "Compensation des vibrations", "Rezgéskompenzáció", "Compensazione risonanza", "入力シェーピング",
        "입력 셰이핑", "Įvesties formavimas", "Input shaping", "Kompensacja drgań", "Modelagem de entrada",
        "Compensarea vibrațiilor", "Шейпер", "Vibrationskompensering", "ชดเชยการสั่น", "Titreşim telafisi",
        "Компенсація вібрацій", "Bù rung", "输入整形", "輸入整形",
    ),
    "Interface language": (
        "Idioma de la interfície", "Jazyk rozhraní", "Schnittstellensprache", "Idioma de la interfaz",
        "Interfaze hizkuntza", "Langue de l'interface", "Interfész nyelve", "Lingua dell'interfaccia", "インターフェース言語",
        "인터페이스 언어", "Sąsajos kalba", "Interfacetaal", "Język interfejsu", "Idioma da interface", "Limba interfeței",
        "Язык интерфейса", "Gränssnittsspråk", "ภาษาอินเทอร์เฟซ", "Arayüz dili", "Мова інтерфейсу",
        "Ngôn ngữ giao diện", "界面语言", "介面語言",
    ),
    "Invalid request.": (
        "Sol·licitud no vàlida.", "Neplatný požadavek.", "Ungültige Anfrage.", "Solicitud no válida.",
        "Eskaera baliogabea.", "Demande invalide.", "Érvénytelen kérés.", "Richiesta non valida.", "リクエストが無効です。",
        "요청이 잘못되었습니다.", "Neteisinga užklausa.", "Ongeldig verzoek.", "Nieprawidłowe żądanie.", "Solicitação inválida.",
        "Solicitare nevalidă.", "Неверный запрос.", "Ogiltig begäran.", "คำขอไม่ถูกต้อง", "Geçersiz istek.",
        "Недійсний запит.", "Yêu cầu không hợp lệ.", "无效请求。", "無效請求。",
    ),
    "Job details": (
        "Detalls de la impressió", "Podrobnosti tiskové úlohy", "Druckauftragsdetails",
        "Detalles del trabajo de impresión", "Inprimatze-lanaren xehetasunak", "Détails de la tâche d’impression",
        "Nyomtatási feladat részletei", "Dettagli del lavoro di stampa", "造形ジョブの詳細", "출력 작업 상세 정보",
        "Spausdinimo užduoties informacija", "Details van printtaak", "Szczegóły zadania drukowania",
        "Detalhes da tarefa de impressão", "Detaliile sarcinii de imprimare", "Подробности задания",
        "Information om utskriftsjobbet", "รายละเอียดงานพิมพ์", "Yazdırma işi ayrıntıları", "Подробиці завдання друку",
        "Chi tiết tác vụ in", "打印任务详情", "列印工作詳細資料",
    ),
    "Job status unavailable": (
        "Estat de la impressió no disponible", "Stav tiskové úlohy není k dispozici",
        "Druckauftragsstatus nicht verfügbar", "Estado del trabajo de impresión no disponible",
        "Inprimatze-lanaren egoera ezin da ikusi", "État de la tâche d’impression indisponible",
        "A nyomtatási feladat állapota nem érhető el", "Stato del lavoro di stampa non disponibile", "造形ジョブの状況を取得できません",
        "출력 작업 상태를 확인할 수 없음", "Spausdinimo užduoties būsena nepasiekiama", "Status van printtaak niet beschikbaar",
        "Stan zadania drukowania niedostępny", "Status da tarefa de impressão indisponível",
        "Starea sarcinii de imprimare nu este disponibilă", "Состояние задания недоступно",
        "Utskriftsjobbets status är inte tillgänglig", "ไม่สามารถดูสถานะงานพิมพ์ได้", "Yazdırma işi durumu alınamadı",
        "Стан завдання друку недоступний", "Không có thông tin về trạng thái tác vụ in", "无法获取打印任务状态", "無法取得列印工作狀態",
    ),
    "Jobs": (
        "Treballs d’impressió", "Tiskové úlohy", "Druckaufträge", "Trabajos de impresión", "Inprimatze-lanak",
        "Tâches d’impression", "Nyomtatási feladatok", "Lavori di stampa", "造形ジョブ", "출력 작업", "Spausdinimo užduotys",
        "Printtaken", "Zadania drukowania", "Tarefas de impressão", "Sarcini de imprimare", "Задания", "Utskriftsjobb",
        "งานพิมพ์", "Yazdırma işleri", "Завдання друку", "Tác vụ in", "打印任务", "列印工作",
    ),
    "Large": (
        "Gran", "Velký", "Groß", "Grande", "Handia", "Grand", "Nagy", "Grande", "大", "대", "Didelis", "Groot", "Duży",
        "Grande", "Mare", "Крупный", "Stor", "ใหญ่", "Büyük", "Великий", "Lớn", "大号", "大號",
    ),
    "Last completed": (
        "Darrer completat", "Naposledy dokončeno", "Zuletzt abgeschlossen", "Último completado", "Azken osatua",
        "Dernière réalisation", "Utoljára befejezve", "Ultimo completato", "最後に完了しました", "마지막 완료",
        "Paskutinį kartą baigta", "Laatst voltooid", "Ostatnio ukończono", "Última conclusão", "Ultima finalizare",
        "Последнее завершённое задание", "Senast klar", "เสร็จสิ้นครั้งสุดท้าย", "Son tamamlanan", "Востаннє завершено",
        "Hoàn thành lần cuối", "最后完成", "最後完成",
    ),
    "Layout": (
        "Disseny", "Rozložení", "Layout", "Diseño", "Diseinua", "Disposition", "Elrendezés", "Disposizione", "レイアウト",
        "레이아웃", "Išdėstymas", "Indeling", "Układ", "Layout", "Aspect", "Расположение", "Layout", "เค้าโครง", "Düzen",
        "Макет", "Bố cục", "布局", "佈局",
    ),
    "Level bed": (
        "Anivella el llit", "Vyrovnat podložku", "Druckbett nivellieren", "Nivelar cama", "Nibelatu ohea",
        "Niveler le plateau", "Asztal szintezése", "Livella piatto", "ベッドを水平調整", "베드 레벨링", "Išlyginti pagrindą",
        "Printbed nivelleren", "Wypoziomuj stół", "Nivelar mesa", "Nivelează patul", "Выровнять стол",
        "Nivellera byggplattan", "ปรับระดับฐานพิมพ์", "Yatağı seviyelendir", "Вирівняти стіл", "Cân bằng bàn in",
        "热床调平", "熱床調平",
    ),
    "Level bed before printing": (
        "Niveleu el llit abans d'imprimir", "Před tiskem vyrovnejte podložku", "Bett vor dem Drucken nivellieren",
        "Nivele la cama antes de imprimir", "Mailatu ohea inprimatu aurretik", "Nivelez le lit avant l'impression",
        "Nyomtatás előtt vízszintes fekhely", "Livellare il piano prima della stampa", "印刷前のレベルベッド", "인쇄 전 수평 침대",
        "Prieš spausdindami išlyginkite", "Niveau bed vóór het afdrukken", "Wypoziomuj łóżko przed drukowaniem",
        "Nivelar a base antes de imprimir", "Nivelați patul înainte de imprimare", "Выравнивать стол перед печатью",
        "Jämna ut bädden före utskrift", "เตียงระดับก่อนการพิมพ์", "Yazdırmadan önce seviye yatağı",
        "Рівне ліжко перед друком", "Cân bằng giường trước khi in", "打印前调平床台", "列印前調平床台",
    ),
    "Light": (
        "Llum", "Světlo", "Licht", "Luz", "Argia", "Lumière", "Fény", "Luce", "ライト", "라이트", "Šviesa", "Licht",
        "Światło", "Luz", "Lumină", "Подсветка", "Ljus", "แสง", "Işık", "Світло", "Ánh sáng", "光", "光",
    ),
    "Light control is unavailable on this printer.": (
        "El control de llum no està disponible en aquesta impressora.",
        "U této tiskárny není k dispozici ovládání osvětlení.",
        "Die Lichtsteuerung ist bei diesem Drucker nicht verfügbar.",
        "El control de iluminación no está disponible en esta impresora.",
        "Argiaren kontrola ez dago erabilgarri inprimagailu honetan.",
        "Le contrôle de la lumière n'est pas disponible sur cette imprimante.",
        "A fényvezérlés nem érhető el ezen a nyomtatón.",
        "Il controllo della luce non è disponibile su questa stampante.", "このプリンターでは調光はできません。",
        "이 프린터에서는 조명 제어를 사용할 수 없습니다.", "Šiame spausdintuve šviesos valdymo funkcija negalima.",
        "Lichtregeling is niet beschikbaar op deze printer.",
        "Sterowanie oświetleniem jest niedostępne w tej drukarce.",
        "O controle de luz não está disponível nesta impressora.",
        "Controlul luminii nu este disponibil pe această imprimantă.",
        "На этом принтере управление подсветкой недоступно.", "Ljuskontroll är inte tillgänglig på den här skrivaren.",
        "การควบคุมแสงไม่พร้อมใช้งานบนเครื่องพิมพ์นี้", "Bu yazıcıda ışık kontrolü mevcut değildir.",
        "На цьому принтері недоступне керування світлом.", "Điều khiển ánh sáng không khả dụng trên máy in này.",
        "本打印机无法进行灯光控制。", "本印表機無法進行燈光控制。",
    ),
    "Light off": (
        "Llum apagada", "Světlo nesvítí", "Licht aus", "Luz apagada", "Argia itzalita", "Lumière éteinte",
        "Világítás ki", "Luce spenta", "消灯", "소등", "Šviesa išjungta", "Licht uit", "Światło wyłączone", "Luz apagada",
        "Lumina stinsă", "Подсветка выключена", "Lampan släckt", "ปิดไฟ", "Işık kapalı", "Світло вимкнено", "Tắt đèn",
        "灯灭", "燈滅",
    ),
    "Light on": (
        "Llum encès", "Světlo svítí", "Licht an", "Luz encendida", "Argia piztuta", "Lumière allumée", "Világít",
        "Luce accesa", "ライト点灯", "불 켜짐", "Šviesa įjungta", "Licht aan", "Światło włączone", "Luz acesa",
        "Lumină aprinsă", "Подсветка включена", "Lampan tänd", "เปิดไฟ", "Işık açık", "Горить світло", "Bật đèn", "灯亮",
        "燈亮",
    ),
    "Light theme": (
        "Tema lleuger", "Světlé téma", "Lichtthema", "Tema ligero", "Gai argia", "Thème lumière", "Világos téma",
        "Tema chiaro", "ライトテーマ", "라이트 테마", "Šviesi tema", "Licht thema", "Lekki motyw", "Tema claro", "Tema ușoară",
        "Светлая тема", "Lätt tema", "ธีมสว่าง", "Işık teması", "Світла тема", "Chủ đề sáng", "浅色主题", "淺色主題",
    ),
    "Live camera": (
        "Càmera en directe", "Živá kamera", "Live-Kamera", "Cámara en vivo", "Zuzeneko kamera", "Caméra en direct",
        "Élő kamera", "Telecamera live", "ライブカメラ", "라이브 카메라", "Tiesioginė kamera", "Live-camera", "Kamera na żywo",
        "Câmera ao vivo", "Cameră live", "Трансляция с камеры", "Livekamera", "กล้องถ่ายทอดสด", "Canlı kamera",
        "Жива камера", "Máy ảnh trực tiếp", "实时摄像头", "即時攝影機",
    ),
    "Live cameras": (
        "Càmeres en directe", "Živé kamery", "Live-Kameras", "Cámaras en vivo", "Zuzeneko kamerak", "Caméras en direct",
        "Élő kamerák", "Telecamere live", "ライブカメラ", "라이브 카메라", "Tiesioginės kameros", "Live-camera's", "Kamery na żywo",
        "Câmeras ao vivo", "Camere live", "Трансляции с камер", "Livekameror", "กล้องถ่ายทอดสด", "Canlı kameralar",
        "Камери в реальному часі", "Camera trực tiếp", "现场摄像机", "現場攝影機",
    ),
    "Loading…": (
        "S'està carregant...", "Načítání…", "Wird geladen…", "Cargando…", "Kargatzen…", "Chargement…", "Betöltés…",
        "Caricamento in corso…", "読み込み中…", "로딩 중…", "Įkeliama…", "Laden…", "Ładowanie…", "Carregando…", "Se încarcă...",
        "Загрузка…", "Laddar...", "กำลังโหลด...", "Yükleniyor…", "Завантаження…", "Đang tải…", "加载中...", "載入中...",
    ),
    "Locked": (
        "Bloquejat", "Zamčeno", "Gesperrt", "Bloqueado", "Blokeatua", "Verrouillé", "Lezárva", "Bloccato", "ロック済み",
        "잠김", "Užrakinta", "Vergrendeld", "Zablokowane", "Bloqueado", "Blocat", "Заблокировано", "Låst", "ล็อคอยู่",
        "Kilitli", "Заблоковано", "Đã khóa", "锁定", "鎖定",
    ),
    "Manual control": (
        "Control manual", "Ruční ovládání", "Manuelle Steuerung", "Control manual", "Eskuzko kontrola",
        "Commande manuelle", "Kézi vezérlés", "Controllo manuale", "手動制御", "수동 제어", "Rankinis valdymas",
        "Handmatige bediening", "Sterowanie ręczne", "Controle manual", "Control manual", "Ручное управление",
        "Manuell styrning", "การควบคุมด้วยตนเอง", "Manuel kontrol", "Ручне керування", "Điều khiển bằng tay", "手动控制",
        "手動控制",
    ),
    "Manual controls require an idle printer": (
        "Els controls manuals requereixen una impressora inactiva", "Ruční ovládání vyžaduje nečinnou tiskárnu",
        "Manuelle Steuerungen erfordern einen inaktiven Drucker",
        "Los controles manuales requieren una impresora inactiva",
        "Eskuzko kontrolek inaktibo inprimagailu bat behar dute",
        "Les commandes manuelles nécessitent une imprimante inactive",
        "A kézi vezérléshez üresjárati nyomtatóra van szükség", "I controlli manuali richiedono una stampante inattiva",
        "手動制御にはアイドル状態のプリンターが必要です", "수동 제어에는 유휴 프린터가 필요합니다.",
        "Rankiniam valdymui reikalingas neveikiantis spausdintuvas",
        "Voor handmatige bediening is een inactieve printer vereist", "Sterowanie ręczne wymaga bezczynnej drukarki",
        "Os controles manuais exigem uma impressora ociosa", "Comenzile manuale necesită o imprimantă inactivă",
        "Ручное управление доступно, когда принтер свободен", "Manuella kontroller kräver en inaktiv skrivare",
        "การควบคุมแบบแมนนวลจำเป็นต้องใช้เครื่องพิมพ์ที่ไม่ได้ใช้งาน", "Manuel kontroller boşta bir yazıcı gerektirir",
        "Для ручного керування потрібен неактивний принтер", "Điều khiển thủ công yêu cầu máy in không hoạt động",
        "手动控制需要一台空闲打印机", "手動控制需要一台空閒印表機",
    ),
    "Material": (
        "Material", "Materiál", "Material", "Materiales", "Materiala", "Matériel", "Anyag", "Materiale", "素材", "소재",
        "Medžiaga", "Materiaal", "Materiał", "Materiais", "Material", "Материал", "Material", "วัสดุ", "Malzeme",
        "Матеріал", "Chất liệu", "材质", "材質",
    ),
    "Materials · IFS": (
        "Materials · IFS", "Materiály · IFS", "Materialien · IFS", "Materiales · IFS", "Materialak · IFS",
        "Matériaux · IFS", "Anyagok · IFS", "Materiali · IFS", "素材・IFS", "재료 · IFS", "Medžiagos · IFS",
        "Materialen · IFS", "Materiały · IFS", "Materiais · IFS", "Materiale · IFS", "Материалы · IFS",
        "Material · IFS", "วัสดุ · IFS", "Malzemeler · IFS", "Матеріали · IFS", "Vật liệu · IFS", "材料·IFS", "材料·IFS",
    ),
    "Materials": (
        "Materials", "Materiály", "Materialien", "Materiales", "Materialak", "Matériaux", "Anyagok", "Materiali", "素材",
        "재료", "Medžiagos", "Materialen", "Materiały", "Materiais", "Materiale", "Материалы", "Material", "วัสดุ",
        "Malzemeler", "Матеріали", "Vật liệu", "材料", "材料",
    ),
    "Medium": (
        "Mitjà", "Střední", "Mittel", "Medio", "Ertaina", "Moyen", "Közepes", "Medio", "中", "중형", "Vidutinis",
        "Gemiddeld", "Średni", "Médio", "Medie", "Средний", "Medium", "ปานกลาง", "Orta", "Середній", "Trung bình", "中",
        "中",
    ),
    "Model": (
        "Model", "Model", "Modell", "Modelo", "Eredua", "Modèle", "Modell", "Modello", "モデル", "모델", "Modelis", "Model",
        "Model", "Modelo", "Model", "Модель", "Modell", "รุ่น", "Modeli", "Модель", "Mô hình", "型号", "型號",
    ),
    "Monitor": (
        "Monitor", "Monitor", "Monitor", "Monitor", "Monitorea", "Moniteur", "Monitor", "Monitor", "モニター", "모니터",
        "Stebėjimas", "Monitor", "Monitor", "Monitor", "Monitor", "Монитор", "Övervakning", "สถานะเครื่องพิมพ์",
        "İzleme", "Монітор", "Giám sát", "监控", "監控",
    ),
    "Monitoring is paused during file transfer.": (
        "La supervisió s'atura durant la transferència de fitxers.",
        "Monitorování je během přenosu souborů pozastaveno.",
        "Die Überwachung wird während der Dateiübertragung angehalten.",
        "La supervisión se detiene durante la transferencia de archivos.",
        "Monitorizazioa eten egiten da fitxategien transferentzian.",
        "La surveillance est suspendue pendant le transfert de fichiers.", "Fájlátvitel közben a figyelés szünetel.",
        "Il monitoraggio viene sospeso durante il trasferimento dei file.", "ファイル転送中は監視が一時停止されます。",
        "파일 전송 중에는 모니터링이 일시 정지됩니다.", "Stebėjimas pristabdytas failų perdavimo metu.",
        "De bewaking wordt gepauzeerd tijdens de bestandsoverdracht.",
        "Monitorowanie zostało wstrzymane podczas przesyłania pliku.",
        "O monitoramento é pausado durante a transferência de arquivos.",
        "Monitorizarea este întreruptă în timpul transferului de fișiere.",
        "Во время передачи файлов мониторинг приостанавливается.", "Övervakning pausas under filöverföring.",
        "การตรวจสอบจะถูกหยุดชั่วคราวระหว่างการถ่ายโอนไฟล์", "Dosya aktarımı sırasında izleme duraklatılır.",
        "Моніторинг призупинено під час передачі файлу.", "Quá trình giám sát bị tạm dừng trong quá trình truyền tệp.",
        "文件传输期间监视暂停。", "文件傳輸期間監視暫停。",
    ),
    "Motor release is unavailable for this printer.": (
        "L'alliberament del motor no està disponible per a aquesta impressora.",
        "U této tiskárny není k dispozici uvolnění motoru.",
        "Die Motorfreigabe ist für diesen Drucker nicht verfügbar.",
        "La liberación del motor no está disponible para esta impresora.",
        "Motor-oharra ez dago erabilgarri inprimagailu honetarako.",
        "Le déverrouillage du moteur n'est pas disponible pour cette imprimante.",
        "A motoros kioldó nem érhető el ehhez a nyomtatóhoz.",
        "Il rilascio del motore non è disponibile per questa stampante.", "このプリンターにはモーターレリーズがありません。",
        "이 프린터에서는 모터 해제를 사용할 수 없습니다.", "Šiam spausdintuvui variklio atleidimo funkcija negalima.",
        "Motorontgrendeling is niet beschikbaar voor deze printer.",
        "Zwolnienie silnika jest niedostępne dla tej drukarki.",
        "A liberação do motor não está disponível para esta impressora.",
        "Eliberarea motorului nu este disponibilă pentru această imprimantă.",
        "На этом принтере нельзя разблокировать двигатели.",
        "Motorfrigöring är inte tillgänglig för den här skrivaren.", "ไม่สามารถปลดมอเตอร์ได้สำหรับเครื่องพิมพ์นี้",
        "Motor serbest bırakma bu yazıcıda mevcut değildir.", "Розблокування двигуна недоступне для цього принтера.",
        "Máy in này không có khả năng nhả động cơ.", "本打印机不具备电机释放功能。", "本印表機不具備馬達釋放功能。",
    ),
    "Motor release sent.": (
        "Alliberament del motor enviat.", "Uvolnění motoru odesláno.", "Motorfreigabe gesendet.",
        "Liberación del motor enviada.", "Motor-oharra bidali da.", "Libération du moteur envoyée.",
        "Motorkioldó elküldve.", "Sblocco motore inviato.", "モーターリリースを送信しました。", "모터 해제가 전송되었습니다.",
        "Variklio išjungimas išsiųstas.", "Motorvrijgave verzonden.", "Wysłano zezwolenie na silnik.",
        "Liberação do motor enviada.", "Eliberarea motorului trimisă.", "Команда разблокировки двигателей отправлена.",
        "Motorfrigöring skickad.", "ส่งการคลายมอเตอร์แล้ว", "Motor serbest bırakma gönderildi.",
        "Розблокування двигуна надіслано.", "Đã gửi bản phát hành động cơ.", "电机释放送出。", "馬達釋放送出。",
    ),
    "Motors released. Press Home before moving the axes.": (
        "Motors desbloquejats. Prem Origen abans de moure els eixos.",
        "Motory jsou uvolněné. Před pohybem os nastavte výchozí polohu.",
        "Motoren freigegeben. Vor dem Bewegen der Achsen eine Referenzfahrt ausführen.",
        "Motores liberados. Lleva los ejes al origen antes de moverlos.",
        "Motorrak askatuta. Eraman ardatzak hasierako posiziora mugitu aurretik.",
        "Moteurs déverrouillés. Faites la prise d’origine avant de déplacer les axes.",
        "Motorok kioldva. Mozgatás előtt állítsd alaphelyzetbe a tengelyeket.",
        "Motori sbloccati. Azzera gli assi prima di spostarli.", "モーターを解放しました。軸を移動する前に原点復帰してください。",
        "모터가 해제되었습니다. 축을 움직이기 전에 원점으로 복귀하세요.",
        "Varikliai atlaisvinti. Prieš judindami ašis nustatykite pradinę padėtį.",
        "Motoren vrijgegeven. Ga naar het nulpunt voordat je de assen beweegt.",
        "Silniki zwolnione. Przed ruchem wykonaj bazowanie osi.",
        "Motores liberados. Volte à origem antes de mover os eixos.",
        "Motoare eliberate. Du axele la origine înainte de a le deplasa.",
        "Двигатели разблокированы. Перед перемещением осей нажмите «В ноль».",
        "Motorerna är frikopplade. Nollställ axlarna innan du förflyttar dem.",
        "ปลดล็อกมอเตอร์แล้ว ให้กลับจุดเริ่มต้นก่อนขยับแกน",
        "Motorlar serbest bırakıldı. Eksenleri hareket ettirmeden önce başlangıç konumuna dönün.",
        "Двигуни розблоковано. Перед переміщенням осей натисніть «У нуль».",
        "Đã nhả động cơ. Đưa trục về gốc trước khi di chuyển.", "电机已释放。移动各轴前，请先回零。", "馬達已釋放。移動各軸前，請先回原點。",
    ),
    "Move X negative": (
        "Mou X negatiu", "Posuňte X záporně", "X negativ verschieben", "Mover X negativo", "Mugitu X negatiboa",
        "Déplacer X négatif", "Mozgatás X negatív", "Sposta X negativo", "移動 X マイナス", "이동 X 음수", "Perkelti X neigiamą",
        "Verplaats X negatief", "Przesuń X ujemnie", "Mover X negativo", "Mutare X negativ",
        "Переместить по оси X в сторону минуса", "Flytta X negativ", "ย้าย X ลบ", "X negatifini taşı",
        "Перемістіть X у мінус", "Di chuyển X âm", "移动X负", "移動X負",
    ),
    "Move X positive": (
        "Mou X positiu", "Posuňte X kladně", "X positiv verschieben", "Mover X positivo", "Mugitu X positiboa",
        "Déplacer X positif", "Mozgassa X pozitív irányba", "Muovi X positivo", "移動Xプラス", "무브 X 플러스",
        "Perkelkite X teigiamai", "Verplaats X positief", "Przesuń X w górę", "Mover X positivo", "Mutați X pozitiv",
        "Переместить по оси X в сторону плюса", "Flytta X positivt", "ย้าย X เป็นบวก",
        "X'i pozitif yönde hareket ettirin", "Переміщення X позитивне", "Di chuyển X dương", "X 正向移动", "X 正向移動",
    ),
    "Move Y negative": (
        "Mou Y negatiu", "Posuňte Y záporně", "Y negativ verschieben", "Mover Y negativo", "Mugitu Y negatiboa",
        "Déplacer Y négatif", "Mozgatás Y negatívra", "Sposta Y negativo", "Yをマイナスに移動", "Y 음수 이동",
        "Perkelti Y neigiamą", "Verplaats Y negatief", "Przesuń Y ujemnie", "Mover Y negativo", "Mutare Y negativ",
        "Переместить по оси Y в сторону минуса", "Flytta Y negativ", "ย้าย Y ลบ", "Y'yi negatif olarak hareket ettirin",
        "Перемістіть Y мінус", "Di chuyển Y âm", "Y负向移动", "Y負向移動",
    ),
    "Move Y positive": (
        "Mou Y positiu", "Posuňte Y kladně", "Y positiv verschieben", "Mover Y positivo", "Mugitu Y positiboa",
        "Déplacer Y positif", "Mozgassa Y pozitív irányba", "Sposta Y positivo", "Yを正に移動", "Y를 긍정적으로 이동",
        "Perkelkite Y teigiamai", "Verplaats Y positief", "Przesuń Y w górę", "Mover Y positivo", "Mutați Y pozitiv",
        "Переместить по оси Y в сторону плюса", "Flytta Y positivt", "ย้าย Y เป็นบวก", "Y'yi pozitif hareket ettirin",
        "Переміщення Y позитивне", "Di chuyển Y dương", "Y 轴正向移动", "Y 軸正向移動",
    ),
    "Move down": (
        "Mou cap avall", "Posuňte se dolů", "Nach unten bewegen", "Bajar", "Mugitu behera", "Descendre", "Lépés lefelé",
        "Spostati verso il basso", "下に移動", "아래로 이동", "Judėti žemyn", "Ga naar beneden", "Przesuń w dół",
        "Mover para baixo", "Deplasați în jos", "Переместить вниз", "Flytta nedåt", "เลื่อนลง",
        "Aşağıya doğru hareket ettirin", "Рух вниз", "Di chuyển xuống", "下移", "下移",
    ),
    "Move sent to the printer.": (
        "Moviment enviat a la impressora.", "Přesun odeslán do tiskárny.", "Verschieben an den Drucker gesendet.",
        "Mover enviado a la impresora.", "Mugitu bidalitako inprimagailura.", "Déplacement envoyé à l'imprimante.",
        "Áthelyezés elküldve a nyomtatóra.", "Spostamento inviato alla stampante.", "プリンターに移動を送信します。",
        "프린터로 이동이 전송되었습니다.", "Perkelti, išsiųsta į spausdintuvą.", "Verplaatsing verzonden naar de printer.",
        "Przenieś wysłany do drukarki.", "Movimentação enviada para a impressora.", "Mutare trimisă la imprimantă.",
        "Команда перемещения отправлена.", "Flytta skickas till skrivaren.", "ย้ายส่งไปยังเครื่องพิมพ์",
        "Yazıcıya gönderilen taşıma.", "Переміщення надіслано на принтер.", "Di chuyển được gửi tới máy in.",
        "移动发送至打印机。", "移動傳送至印表機。",
    ),
    "Move up": (
        "Mou cap amunt", "Přejít nahoru", "Nach oben bewegen", "Subir", "Mugitu gora", "Monter", "Lépés felfelé",
        "Sposta in alto", "上に移動", "위로 이동", "Perkelti aukštyn", "Ga omhoog", "Przejdź w górę", "Subir",
        "Deplasați-vă în sus", "Переместить вверх", "Flytta uppåt", "เลื่อนขึ้น", "Yukarı git", "Рух вгору", "Tiến lên",
        "向上移动", "向上移動",
    ),
    "Moving…": (
        "S'està movent...", "Stěhování…", "Umzug…", "Moviéndose…", "Mugitzen…", "Déménagement…", "Költözés…",
        "Spostamento…", "引越し…", "이사…", "Perkeliama…", "Verhuizen…", "Przenoszenie…", "Mudança…", "Se mută...",
        "Перемещение…", "Flyttar...", "การเคลื่อนไหว...", "Hareket ediyor…", "Переміщення…", "Đang di chuyển…", "移动…",
        "移動…",
    ),
    "N": (
        "Broq.", "Tr.", "Düse", "Boq.", "Pita", "Buse", "Fúv.", "Ugello", "ノズル", "노즐", "Purkšt.", "Mondst.", "Dysza",
        "Bico", "Duză", "Соп.", "Munst.", "หัวฉีด", "Nozul", "Сопло", "Đầu phun", "喷嘴", "噴嘴",
    ),
    "Needs Attention": (
        "Necessita atenció", "Vyžaduje pozornost", "Benötigt Aufmerksamkeit", "Necesita Atención", "Arreta behar du",
        "a besoin d'attention", "Figyelmet igényel", "Richiede attenzione", "注意が必要です", "주의가 필요함", "Reikia dėmesio",
        "Heeft aandacht nodig", "Wymaga uwagi", "Precisa de Atenção", "Are nevoie de atenție", "Требуется внимание",
        "Behöver uppmärksamhet", "ต้องการความสนใจ", "Dikkat Edilmesi Gerekiyor", "Потребує уваги", "Cần Chú Ý",
        "需要注意的事项", "需要注意的事項",
    ),
    "Network discovery unavailable. Enter the printer address manually.": (
        "Detecció de xarxa no disponible. Introduïu l'adreça de la impressora manualment.",
        "Zjišťování sítě není k dispozici. Zadejte adresu tiskárny ručně.",
        "Netzwerkerkennung nicht verfügbar. Geben Sie die Druckeradresse manuell ein.",
        "Descubrimiento de red no disponible. Introduzca la dirección de la impresora manualmente.",
        "Sarearen aurkikuntza ez dago erabilgarri. Sartu eskuz inprimagailuaren helbidea.",
        "Découverte du réseau indisponible. Saisissez manuellement l'adresse de l'imprimante.",
        "A hálózatfelderítés nem érhető el. Írja be kézzel a nyomtató címét.",
        "Individuazione della rete non disponibile. Immettere manualmente l'indirizzo della stampante.",
        "ネットワーク探索が利用できません。プリンターのアドレスを手動で入力します。", "네트워크 검색을 사용할 수 없습니다. 프린터 주소를 수동으로 입력하세요.",
        "Tinklo aptikimas nepasiekiamas. Įveskite spausdintuvo adresą rankiniu būdu.",
        "Netwerkdetectie niet beschikbaar. Voer het printeradres handmatig in.",
        "Wykrywanie sieci niedostępne. Wprowadź adres drukarki ręcznie.",
        "Descoberta de rede indisponível. Insira o endereço da impressora manualmente.",
        "Descoperirea rețelei indisponibilă. Introduceți manual adresa imprimantei.",
        "Поиск в локальной сети недоступен. Введите адрес принтера вручную.",
        "Nätverksupptäckt är inte tillgängligt. Ange skrivarens adress manuellt.",
        "การค้นหาเครือข่ายไม่พร้อมใช้งาน ป้อนที่อยู่เครื่องพิมพ์ด้วยตนเอง",
        "Ağ keşfi kullanılamıyor. Yazıcı adresini manuel olarak girin.",
        "Виявлення мережі недоступне. Введіть адресу принтера вручну.",
        "Khám phá mạng không khả dụng. Nhập địa chỉ máy in theo cách thủ công.", "网络发现不可用。手动输入打印机地址。",
        "網路發現不可用。手動輸入印表機地址。",
    ),
    "Next page": (
        "Pàgina següent", "Další stránka", "Nächste Seite", "Página siguiente", "Hurrengo orria", "Page suivante",
        "Következő oldal", "Pagina successiva", "次のページ", "다음 페이지", "Kitas puslapis", "Volgende pagina",
        "Następna strona", "Próxima página", "Pagina următoare", "Следующая страница", "Nästa sida", "หน้าถัดไป",
        "Sonraki sayfa", "Наступна сторінка", "Trang tiếp theo", "下一页", "下一頁",
    ),
    "No active print job": (
        "No hi ha cap tasca d'impressió activa", "Žádná aktivní tisková úloha", "Kein aktiver Druckauftrag",
        "Ningún trabajo de impresión activo", "Ez dago inprimatze-lanik aktiborik", "Aucun travail d'impression actif",
        "Nincs aktív nyomtatási feladat", "Nessun lavoro di stampa attivo", "アクティブな印刷ジョブがありません", "활성 인쇄 작업이 없습니다.",
        "Nėra aktyvios spausdinimo užduoties", "Geen actieve afdruktaak", "Brak aktywnego zadania drukowania",
        "Nenhum trabalho de impressão ativo", "Nicio lucrare de imprimare activă", "Нет активного задания",
        "Inget aktivt utskriftsjobb", "ไม่มีงานพิมพ์ที่ใช้งานอยู่", "Etkin yazdırma işi yok",
        "Немає активного завдання друку", "Không có lệnh in nào đang hoạt động", "没有活动的打印作业", "沒有活動的列印作業",
    ),
    "No files": (
        "No hi ha fitxers", "Žádné soubory", "Keine Dateien", "No hay archivos", "Ez dago fitxategirik",
        "Aucun fichier", "Nincs fájl", "Nessun file", "ファイルなし", "파일 없음", "Failų nėra", "Geen bestanden", "Brak plików",
        "Nenhum arquivo", "Niciun fișier", "Файлов нет", "Inga filer", "ไม่มีไฟล์", "Dosya yok", "Файлів немає",
        "Không có tệp", "没有文件", "沒有檔案",
    ),
    "No idle printers.": (
        "No hi ha impressores inactives.", "Žádné nečinné tiskárny.", "Keine inaktiven Drucker.",
        "No hay impresoras inactivas.", "Ez dago inaktibo inprimagailurik.", "Aucune imprimante inactive.",
        "Nincsenek üresjárati nyomtatók.", "Nessuna stampante inattiva.", "アイドル状態のプリンターはありません。", "유휴 프린터가 없습니다.",
        "Nėra neveikiančių spausdintuvų.", "Geen inactieve printers.", "Brak bezczynnych drukarek.",
        "Nenhuma impressora ociosa.", "Fără imprimante inactive.", "Нет свободных принтеров.",
        "Inga inaktiva skrivare.", "ไม่มีเครื่องพิมพ์ที่ไม่ได้ใช้งาน", "Boşta kalan yazıcı yok.",
        "Немає неактивних принтерів.", "Không có máy in nhàn rỗi.", "没有空闲打印机。", "沒有空閒印表機。",
    ),
    "No loaded slot matches": (
        "No hi ha coincidències d'espai carregat", "Žádný načtený slot neodpovídá",
        "Keine geladenen Slot-Übereinstimmungen", "No hay coincidencias de tragamonedas cargadas",
        "Ez dago kargatutako zirrikitu bat-etortzerik", "Aucune correspondance de slot chargée",
        "Nincsenek betöltött helyek", "Nessuna corrispondenza di slot caricata", "ロードされたスロットが一致しません",
        "로드된 슬롯이 일치하지 않습니다.", "Nėra įkeltų lizdų atitikčių", "Geen geladen slotovereenkomsten",
        "Brak załadowanych dopasowań do slotów", "Nenhum slot carregado corresponde",
        "Nicio potrivire a sloturilor încărcate", "Нет подходящего слота с материалом", "Inga laddade slotmatcher",
        "ไม่มีช่องที่โหลดตรงกัน", "Yüklü slot eşleşmesi yok", "Немає завантажених слотів",
        "Không có kết quả phù hợp nào được tải", "没有加载的插槽匹配", "沒有載入的插槽匹配",
    ),
    "No matching files": (
        "No s’han trobat fitxers", "Nenalezeny žádné soubory", "Keine passenden Dateien",
        "No hay archivos coincidentes", "Ez dago bat datorren fitxategirik", "Aucun fichier correspondant",
        "Nincs egyező fájl", "Nessun file corrispondente", "一致するファイルがありません", "일치하는 파일 없음", "Atitinkančių failų nėra",
        "Geen overeenkomende bestanden", "Brak pasujących plików", "Nenhum arquivo encontrado", "Niciun fișier găsit",
        "Подходящих файлов нет", "Inga matchande filer", "ไม่พบไฟล์ที่ตรงกัน", "Eşleşen dosya yok",
        "Відповідних файлів немає", "Không có tệp phù hợp", "没有匹配的文件", "沒有符合的檔案",
    ),
    "No new printers found.": (
        "No s'han trobat impressores noves.", "Nebyly nalezeny žádné nové tiskárny.", "Keine neuen Drucker gefunden.",
        "No se encontraron nuevas impresoras.", "Ez da inprimagailu berririk aurkitu.",
        "Aucune nouvelle imprimante trouvée.", "Nem található új nyomtató.", "Nessuna nuova stampante trovata.",
        "新しいプリンターは見つかりませんでした。", "새 프린터를 찾을 수 없습니다.", "Naujų spausdintuvų nerasta.", "Geen nieuwe printers gevonden.",
        "Nie znaleziono nowych drukarek.", "Nenhuma impressora nova encontrada.", "Nu s-au găsit imprimante noi.",
        "Новых принтеров не обнаружено.", "Inga nya skrivare hittades.", "ไม่พบเครื่องพิมพ์ใหม่",
        "Yeni yazıcı bulunamadı.", "Не знайдено нових принтерів.", "Không tìm thấy máy in mới.", "未找到新打印机。", "未找到新印表機。",
    ),
    "No preview": (
        "Sense previsualització", "Žádný náhled", "Keine Vorschau", "Sin vista previa", "Aurrebistarik ez",
        "Aucun aperçu", "Nincs előnézet", "Nessuna anteprima", "プレビューなし", "미리보기 없음", "Nėra peržiūros", "Geen voorbeeld",
        "Brak podglądu", "Sem visualização", "Nicio previzualizare", "Нет эскиза", "Ingen förhandsgranskning",
        "ไม่มีการแสดงตัวอย่าง", "Önizleme yok", "Без попереднього перегляду", "Không có bản xem trước", "无预览", "無預覽",
    ),
    "No printers are printing.": (
        "No s'està imprimint cap impressora.", "Netisknou žádné tiskárny.", "Es drucken keine Drucker.",
        "No hay impresoras imprimiendo.", "Ez da inprimagailurik inprimatzen ari.", "Aucune imprimante n'imprime.",
        "Egyetlen nyomtató sem nyomtat.", "Nessuna stampante sta stampando.", "印刷を行っているプリンターがありません。",
        "인쇄 중인 프린터가 없습니다.", "Jokie spausdintuvai nespausdina.", "Er zijn geen printers bezig met afdrukken.",
        "Żadna drukarka nie drukuje.", "Nenhuma impressora está imprimindo.", "Nicio imprimantă nu se imprimă.",
        "Ни один принтер не печатает.", "Inga skrivare skriver ut.", "ไม่มีเครื่องพิมพ์กำลังพิมพ์อยู่",
        "Hiçbir yazıcı yazdırmıyor.", "Жоден принтер не друкує.", "Không có máy in nào đang in.", "没有打印机正在打印。",
        "沒有印表機正在列印。",
    ),
    "No printers configured.": (
        "No s'ha configurat cap impressora.", "Nejsou nakonfigurovány žádné tiskárny.", "Keine Drucker konfiguriert.",
        "No hay impresoras configuradas.", "Ez dago inprimagailurik konfiguratuta.", "Aucune imprimante configurée.",
        "Nincsenek konfigurálva nyomtatók.", "Nessuna stampante configurata.", "プリンターが設定されていません。", "구성된 프린터가 없습니다.",
        "Nėra sukonfigūruotų spausdintuvų.", "Geen printers geconfigureerd.", "Nie skonfigurowano żadnych drukarek.",
        "Nenhuma impressora configurada.", "Nicio imprimantă configurată.", "Принтеры не настроены.",
        "Inga skrivare konfigurerade.", "ไม่มีการกำหนดค่าเครื่องพิมพ์", "Yapılandırılmış yazıcı yok.",
        "Немає налаштованих принтерів.", "Không có máy in nào được cấu hình.", "未配置打印机。", "未配置印表機。",
    ),
    "No readings": (
        "Sense lectures", "Žádné údaje", "Keine Messwerte", "Sin lecturas", "Ez dago irakurketarik", "Aucune lecture",
        "Nincs leolvasás", "Nessuna lettura", "読み取り値なし", "판독값 없음", "Jokių rodmenų", "Geen metingen", "Brak odczytów",
        "Sem leituras", "Fără citiri", "Нет показаний", "Inga avläsningar", "ไม่มีการอ่าน", "Okuma yok",
        "Немає показань", "Không có bài đọc", "无读数", "無讀數",
    ),
    "No response": (
        "Sense resposta", "Žádná odezva", "Keine Antwort", "Sin respuesta", "Ez dago erantzunik", "Aucune réponse",
        "Nincs válasz", "Nessuna risposta", "応答なし", "무응답", "Nėra atsakymo", "Geen reactie", "Brak odpowiedzi",
        "Sem resposta", "Niciun răspuns", "Нет ответа", "Inget svar", "ไม่มีการตอบสนอง", "Yanıt yok", "Немає відповіді",
        "Không có phản hồi", "无反应", "無反應",
    ),
    "Notifications": (
        "Notificacions", "Oznámení", "Benachrichtigungen", "Notificaciones", "Jakinarazpenak", "Notifications",
        "Értesítések", "Notifiche", "お知らせ", "알림", "Pranešimai", "Meldingen", "Powiadomienia", "Notificações",
        "Notificări", "Уведомления", "Aviseringar", "การแจ้งเตือน", "Bildirimler", "Сповіщення", "Thông báo", "通知",
        "通知",
    ),
    "Nozzle": (
        "Broquet", "Tryska", "Düse", "Boquilla", "Pita", "Buse", "Fúvóka", "Ugello", "ノズル", "노즐", "Purkštukas",
        "Mondstuk", "Dysza", "Bico", "Duză", "Сопло", "Nozzel", "หัวฉีด", "Nozul", "Сопло", "Đầu phun", "喷嘴", "噴嘴",
    ),
    "Nozzle Target": (
        "Temperatura objectiu del broquet", "Cílová teplota trysky", "Düsen-Solltemperatur",
        "Temperatura objetivo de la boquilla", "Pitaren helburuko tenperatura", "Consigne de la buse",
        "Fúvóka célhőmérséklete", "Temperatura obiettivo dell’ugello", "ノズル目標温度", "노즐 목표 온도",
        "Tikslinė purkštuko temperatūra", "Doeltemperatuur mondstuk", "Temperatura docelowa dyszy",
        "Temperatura alvo do bico", "Temperatura țintă a duzei", "Целевая температура сопла",
        "Måltemperatur för munstycket", "อุณหภูมิเป้าหมายของหัวฉีด", "Nozul hedef sıcaklığı",
        "Цільова температура сопла", "Nhiệt độ đặt của đầu phun", "喷嘴目标温度", "噴嘴目標溫度",
    ),
    "Nozzle T{tool}": (
        "Broquet T{tool}", "Tryska T{tool}", "Düse T{tool}", "Boquilla T{tool}", "Tobera T{tool}", "Buse T{tool}",
        "T{tool} fúvóka", "Ugello T{tool}", "ノズル T{tool}", "노즐 T{tool}", "Antgalis T{tool}", "Mondstuk T{tool}",
        "Dysza T{tool}", "Bocal T{tool}", "Duză T{tool}", "Сопло T{tool}", "Munstycke T{tool}", "หัวฉีด T{tool}",
        "Nozul T{tool}", "Насадка T{tool}", "Vòi phun T{tool}", "喷嘴T{tool}", "噴嘴T{tool}",
    ),
    "Nozzle {temperature} °C": (
        "Broquet {temperature} °C", "Tryska {temperature} °C", "Düse {temperature} °C", "Boquilla {temperature} °C",
        "Tobera {temperature} °C", "Buse {temperature} °C", "Fúvóka {temperature} °C", "Ugello {temperature} °C",
        "ノズル {temperature} ℃", "노즐 {temperature} °C", "Antgalis {temperature} °C", "Mondstuk {temperature} °C",
        "Dysza {temperature} °C", "Bico {temperature} °C", "Duză {temperature} °C", "Сопло {temperature} °C",
        "Munstycke {temperature} °C", "หัวฉีด {temperature} °C", "Nozul {temperature} °C", "Сопло {temperature} °C",
        "Vòi phun {temperature} °C", "喷嘴 {temperature} °C", "噴嘴 {temperature} °C",
    ),
    "Off": (
        "Apagat", "Vypnuto", "Aus", "Apagado", "Desaktibatuta", "Désactivé", "Ki", "Spento", "オフ", "끄기", "Išjungta",
        "Uit", "Wyłączone", "Desligado", "Oprit", "Выкл.", "Av", "ปิด", "Kapalı", "Вимк", "Tắt", "关", "關",
    ),
    "On": (
        "Activat", "Zapnuto", "Ein", "encendido", "Aktibatuta", "Activé", "Be", "Acceso", "オン", "온", "Įjungta", "Aan",
        "Włączone", "Ligado", "Pornit", "Вкл.", "På", "เปิด", "Açık", "Увімкнено", "Bật", "开", "開",
    ),
    "Open": (
        "Obert", "Otevřeno", "Offen", "Abierto", "Irekita", "Ouvert", "Nyitva", "Aperto", "開", "열림", "Atidaryta",
        "Open", "Otwarte", "Aberto", "Deschis", "Открыто", "Öppen", "เปิด", "Açık", "Відчинено", "Mở", "已打开", "已開啟",
    ),
    "Open FlashForge": (
        "Obre FlashForge", "Otevřete FlashForge", "Öffnen Sie FlashForge", "Abrir FlashForge", "Ireki FlashForge",
        "Ouvrir FlashForge", "Nyissa meg a FlashForge-t", "Apri FlashForge", "FlashForgeを開く", "FlashForge 열기",
        "Atidarykite FlashForge", "FlashForge openen", "Otwórz FlashForge", "Abra FlashForge", "Deschideți FlashForge",
        "Открыть FlashForge", "Öppna FlashForge", "เปิด FlashForge", "FlashForge'yi açın", "Відкрийте FlashForge",
        "Mở FlashForge", "打开FlashForge", "開啟FlashForge",
    ),
    "Open Printer Interface in Browser": (
        "Obriu la interfície de la impressora al navegador", "Otevřete rozhraní tiskárny v prohlížeči",
        "Öffnen Sie die Druckerschnittstelle im Browser", "Abrir la interfaz de la impresora en el navegador",
        "Ireki inprimagailuaren interfazea arakatzailean", "Ouvrir l'interface de l'imprimante dans le navigateur",
        "Nyissa meg a Nyomtatófelületet a böngészőben", "Apre l'interfaccia della stampante nel browser",
        "ブラウザでプリンターインターフェースを開く", "브라우저에서 프린터 인터페이스 열기", "Atidarykite spausdintuvo sąsają naršyklėje",
        "Open de printerinterface in de browser", "Otwórz interfejs drukarki w przeglądarce",
        "Abrir interface da impressora no navegador", "Deschideți Interfața imprimantei în Browser",
        "Открыть интерфейс принтера в браузере", "Öppna skrivargränssnittet i webbläsaren",
        "เปิดอินเทอร์เฟซเครื่องพิมพ์ในเบราว์เซอร์", "Tarayıcıda Yazıcı Arayüzünü Açın",
        "Відкрийте інтерфейс принтера у браузері", "Mở giao diện máy in trong trình duyệt", "在浏览器中打开打印机界面",
        "在瀏覽器中開啟印表機介面",
    ),
    "Open printer": (
        "Obre la impressora", "Otevřete tiskárnu", "Drucker öffnen", "Abrir impresora", "Ireki inprimagailua",
        "Ouvrir l'imprimante", "Nyissa meg a nyomtatót", "Aprire la stampante", "プリンターを開く", "프린터 열기",
        "Atidarykite spausdintuvą", "Printer openen", "Otwórz drukarkę", "Abrir impressora", "Deschideți imprimanta",
        "Открыть принтер", "Öppna skrivaren", "เปิดเครื่องพิมพ์", "Yazıcıyı aç", "Відкрийте принтер", "Mở máy in",
        "打开打印机", "開啟印表機",
    ),
    "Orange": (
        "Taronja", "Oranžová", "Orange", "Naranja", "Laranja", "Orange", "Narancs", "Arancione", "オレンジ", "오렌지",
        "Oranžinė", "Oranje", "Pomarańczowy", "Laranja", "Portocaliu", "Оранжевый", "Orange", "ส้ม", "Turuncu",
        "Помаранчевий", "Cam", "橙色", "橘色",
    ),
    "OrcaSlicer is sending a print job. Monitoring will resume when the transfer is complete.": (
        "OrcaSlicer està enviant una tasca d'impressió. La supervisió es reprendrà quan finalitzi la transferència.",
        "OrcaSlicer odesílá tiskovou úlohu. Po dokončení přenosu bude sledování pokračovat.",
        "OrcaSlicer sendet einen Druckauftrag. Die Überwachung wird wieder aufgenommen, sobald die Übertragung abgeschlossen ist.",
        "OrcaSlicer está enviando un trabajo de impresión. La supervisión se reanudará cuando se complete la transferencia.",
        "OrcaSlicer inprimatze-lan bat bidaltzen ari da. Kontrola transferentzia amaitzean berriro hasiko da.",
        "OrcaSlicer envoie un travail d'impression. La surveillance reprendra une fois le transfert terminé.",
        "A OrcaSlicer nyomtatási feladatot küld. A figyelés az átvitel befejezésekor folytatódik.",
        "OrcaSlicer sta inviando un lavoro di stampa. Il monitoraggio riprenderà al termine del trasferimento.",
        "OrcaSlicer が印刷ジョブを送信しています。転送が完了すると監視が再開されます。", "OrcaSlicer가 인쇄 작업을 보내는 중입니다. 전송이 완료되면 모니터링이 재개됩니다.",
        "OrcaSlicer siunčia spausdinimo užduotį. Stebėjimas bus atnaujintas, kai perkėlimas bus baigtas.",
        "OrcaSlicer verzendt een afdruktaak. De bewaking wordt hervat zodra de overdracht is voltooid.",
        "OrcaSlicer wysyła zadanie drukowania. Monitorowanie zostanie wznowione po zakończeniu przesyłania.",
        "OrcaSlicer está enviando um trabalho de impressão. O monitoramento será retomado quando a transferência for concluída.",
        "OrcaSlicer trimite o lucrare de imprimare. Monitorizarea va relua când transferul este finalizat.",
        "OrcaSlicer отправляет задание на печать. Мониторинг возобновится после завершения передачи.",
        "OrcaSlicer skickar ett utskriftsjobb. Övervakningen kommer att återupptas när överföringen är klar.",
        "OrcaSlicer กำลังส่งงานพิมพ์ การตรวจสอบจะกลับมาทำงานอีกครั้งเมื่อการถ่ายโอนเสร็จสมบูรณ์",
        "OrcaSlicer bir yazdırma işi gönderiyor. Aktarım tamamlandığında izleme devam edecektir.",
        "OrcaSlicer надсилає завдання друку. Моніторинг відновиться після завершення передачі.",
        "OrcaSlicer đang gửi lệnh in. Quá trình giám sát sẽ tiếp tục khi quá trình truyền hoàn tất.",
        "OrcaSlicer 正在发送打印作业。传输完成后，监控将恢复。", "OrcaSlicer 正在傳送列印作業。傳輸完成後，監控將恢復。",
    ),
    "OrcaSlicer is sending a print job. Try again when it finishes.": (
        "OrcaSlicer està enviant una tasca d'impressió. Torna-ho a provar quan acabi.",
        "OrcaSlicer odesílá tiskovou úlohu. Zkuste to znovu, až to skončí.",
        "OrcaSlicer sendet einen Druckauftrag. Versuchen Sie es erneut, wenn der Vorgang abgeschlossen ist.",
        "OrcaSlicer está enviando un trabajo de impresión. Inténtalo de nuevo cuando termine.",
        "OrcaSlicer inprimatze-lan bat bidaltzen ari da. Saiatu berriro amaitzen denean.",
        "OrcaSlicer envoie un travail d'impression. Réessayez une fois terminé.",
        "A OrcaSlicer nyomtatási feladatot küld. Próbálja újra, ha befejezte.",
        "OrcaSlicer sta inviando un lavoro di stampa. Riprovare al termine.",
        "OrcaSlicer が印刷ジョブを送信しています。終了したらもう一度試してください。", "OrcaSlicer가 인쇄 작업을 보내는 중입니다. 완료되면 다시 시도하세요.",
        "OrcaSlicer siunčia spausdinimo užduotį. Bandykite dar kartą, kai baigsis.",
        "OrcaSlicer verzendt een afdruktaak. Probeer het opnieuw als het klaar is.",
        "OrcaSlicer wysyła zadanie drukowania. Spróbuj ponownie po zakończeniu.",
        "OrcaSlicer está enviando um trabalho de impressão. Tente novamente quando terminar.",
        "OrcaSlicer trimite o lucrare de imprimare. Încercați din nou când se termină.",
        "OrcaSlicer отправляет задание печати. Повторите попытку после передачи.",
        "OrcaSlicer skickar ett utskriftsjobb. Försök igen när det är klart.",
        "OrcaSlicer กำลังส่งงานพิมพ์ ลองอีกครั้งเมื่อเสร็จสิ้น",
        "OrcaSlicer bir yazdırma işi gönderiyor. Bittiğinde tekrar deneyin.",
        "OrcaSlicer надсилає завдання друку. Спробуйте знову, коли він закінчиться.",
        "OrcaSlicer đang gửi lệnh in. Hãy thử lại khi nó kết thúc.", "OrcaSlicer 正在发送打印作业。完成后重试。",
        "OrcaSlicer 正在傳送​​列印作業。完成後重試。",
    ),
    "Original sliced .3mf": (
        "Fitxer .3mf original amb la laminació", "Původní naslicovaný soubor .3mf", "Originale geslicte .3mf-Datei",
        "Archivo .3mf laminado original", "Jatorrizko .3mf geruzatua", "Fichier .3mf d’origine déjà tranché",
        "Eredeti szeletelt .3mf fájl", "File .3mf originale già affettato", "スライス済みの元の .3mf ファイル",
        "슬라이스를 완료한 원본 .3mf 파일", "Originalus supjaustytas .3mf failas", "Oorspronkelijk geslicet .3mf-bestand",
        "Oryginalny plik .3mf po cięciu", "Arquivo .3mf fatiado original", "Fișier .3mf original cu felierea efectuată",
        "Исходный .3mf с нарезкой", "Ursprunglig .3mf-fil med utskriftsbanor", "ไฟล์ .3mf ต้นฉบับที่สไลซ์แล้ว",
        "Dilimlenmiş özgün .3mf dosyası", "Вихідний файл .3mf з нарізкою", "Tệp .3mf gốc đã cắt lớp", "已切片的原始 .3mf 文件",
        "已切片的原始 .3mf 檔案",
    ),
    "Page number": (
        "Número de pàgina", "Číslo stránky", "Seitenzahl", "Número de página", "Orri-zenbakia", "Numéro de page",
        "Oldalszám", "Numero di pagina", "ページ数", "페이지 번호", "Puslapio numeris", "Paginanummer", "Numer strony",
        "Número da página", "Numărul paginii", "Номер страницы", "Sidnummer", "หมายเลขหน้า", "Sayfa numarası",
        "Номер сторінки", "Số trang", "页码", "頁碼",
    ),
    "Part Cooling Fan": (
        "Ventilador de refrigeració de la peça", "Ventilátor chlazení výtisku", "Bauteillüfter", "Ventilador de capa",
        "Pieza hozteko haizagailua", "Ventilateur de refroidissement de la pièce", "Tárgyhűtő ventilátor",
        "Ventola di raffreddamento del pezzo", "造形物冷却ファン", "출력물 냉각 팬", "Detalės aušinimo ventiliatorius",
        "Ventilator voor printkoeling", "Wentylator chłodzenia wydruku", "Ventoinha de resfriamento da peça",
        "Ventilatorul de răcire a piesei", "Вентилятор обдува модели", "Kylfläkt för utskriften",
        "พัดลมระบายความร้อนชิ้นงาน", "Parça soğutma fanı", "Вентилятор обдування моделі", "Quạt làm mát mẫu in",
        "模型冷却风扇", "模型冷卻風扇",
    ),
    "Part fan": (
        "Ventilador de peça", "Ventilátor výtisku", "Bauteillüfter", "Ventilador de capa", "Pieza-haizagailua",
        "Ventilateur de pièce", "Tárgyhűtő ventilátor", "Ventola del pezzo", "造形物冷却ファン", "출력물 냉각 팬",
        "Detalės aušinimas", "Printkoeling", "Chłodzenie wydruku", "Ventoinha da peça", "Ventilatorul piesei",
        "Обдув модели", "Detaljkylfläkt", "พัดลมชิ้นงาน", "Parça soğutma fanı", "Обдування моделі",
        "Quạt làm mát mẫu in", "模型风扇", "模型風扇",
    ),
    "Pause": (
        "Pausa", "Pauza", "Pause", "Pausa", "Pausa", "Pause", "Szünet", "Pausa", "一時停止", "일시정지", "Pauzė", "Pauze",
        "Pauza", "Pausa", "Pauză", "Пауза", "Paus", "หยุดชั่วคราว", "Duraklat", "Пауза", "Tạm dừng", "暂停", "暫停",
    ),
    "Paused": (
        "En pausa", "Pozastaveno", "Angehalten", "En pausa", "Pausatuta", "En pause", "Szünet", "In pausa", "一時停止中",
        "일시중지됨", "Pristabdyta", "Gepauzeerd", "Wstrzymano", "Pausado", "Întrerupt", "На паузе", "Pausad",
        "หยุดชั่วคราว", "Duraklatıldı", "Призупинено", "Đã tạm dừng", "暂停", "暫停",
    ),
    "Pausing": (
        "Pausa", "Pozastavení", "Pause", "Pausa", "Pausa", "Pause", "Szünet", "Pausa", "一時停止中", "일시정지", "Pristabdoma",
        "Pauzeren", "Wstrzymywanie", "Pausa", "Pauză", "Приостановка", "Pausar", "หยุดชั่วคราว", "Duraklatma", "Пауза",
        "Đang tạm dừng", "暂停", "暫停",
    ),
    "Prepared print file": (
        "Fitxer d'impressió preparat", "Připravený tiskový soubor", "Vorbereitete Druckdatei",
        "Archivo de impresión preparado", "Prestatutako inprimatzeko fitxategia", "Fichier d'impression préparé",
        "Előkészített nyomtatási fájl", "File di stampa preparato", "準備された印刷ファイル", "준비된 인쇄 파일",
        "Paruoštas spausdinimo failas", "Voorbereid drukbestand", "Przygotowany plik do druku",
        "Arquivo de impressão preparado", "Fișier de imprimare pregătit", "Подготовленный файл для печати",
        "Förberedd utskriftsfil", "ไฟล์พิมพ์ที่เตรียมไว้", "Hazırlanan yazdırma dosyası",
        "Підготовлений файл для друку", "File in đã chuẩn bị sẵn", "准备打印文件", "準備列印文件",
    ),
    "Press Home before moving the axes.": (
        "Prem Origen abans de moure els eixos.", "Před pohybem nastavte výchozí polohu.",
        "Vor der Bewegung eine Referenzfahrt ausführen.", "Lleva los ejes al origen antes de moverlos.",
        "Eraman ardatzak hasierako posiziora mugitu aurretik.", "Faites la prise d’origine avant tout déplacement.",
        "Mozgatás előtt állítsd alaphelyzetbe a tengelyeket.", "Azzera gli assi prima di spostarli.",
        "移動する前に原点復帰してください。", "이동하기 전에 원점 복귀를 실행하세요.", "Prieš pradėdami judėti nustatykite pradinę padėtį.",
        "Ga naar het nulpunt voordat je de assen beweegt.", "Przed ruchem wykonaj bazowanie osi.",
        "Volte à origem antes de mover os eixos.", "Du axele la origine înainte de deplasare.",
        "Перед перемещением осей нажмите «В ноль».", "Nollställ axlarna innan du förflyttar dem.",
        "กลับจุดเริ่มต้นก่อนขยับแกน", "Eksenleri hareket ettirmeden önce başlangıç konumuna dönün.",
        "Натисніть «У нуль» перед рухом осей.", "Đưa trục về gốc trước khi di chuyển.", "请先回零，再移动各轴。", "請先回原點，再移動各軸。",
    ),
    "Press Home to move.": (
        "Prem Origen abans de moure els eixos.", "Před pohybem nastavte výchozí polohu.",
        "Vor der Bewegung eine Referenzfahrt ausführen.", "Lleva los ejes al origen antes de moverlos.",
        "Eraman ardatzak hasierako posiziora mugitu aurretik.", "Faites la prise d’origine avant tout déplacement.",
        "Mozgatás előtt állítsd alaphelyzetbe a tengelyeket.", "Azzera gli assi prima di spostarli.",
        "移動する前に原点復帰してください。", "이동하기 전에 원점 복귀를 실행하세요.", "Prieš pradėdami judėti nustatykite pradinę padėtį.",
        "Ga naar het nulpunt voordat je de assen beweegt.", "Przed ruchem wykonaj bazowanie osi.",
        "Volte à origem antes de mover os eixos.", "Du axele la origine înainte de deplasare.",
        "Нажмите «В ноль» перед перемещением осей.", "Nollställ axlarna innan du förflyttar dem.",
        "กลับจุดเริ่มต้นก่อนขยับแกน", "Eksenleri hareket ettirmeden önce başlangıç konumuna dönün.",
        "Натисніть «У нуль» перед рухом осей.", "Đưa trục về gốc trước khi di chuyển.", "请先回零，再移动各轴。", "請先回原點，再移動各軸。",
    ),
    "Press Home to restore axis control": (
        "Prem Origen abans de moure els eixos.", "Před pohybem nastavte výchozí polohu.",
        "Vor der Bewegung eine Referenzfahrt ausführen.", "Lleva los ejes al origen antes de moverlos.",
        "Eraman ardatzak hasierako posiziora mugitu aurretik.", "Faites la prise d’origine avant tout déplacement.",
        "Mozgatás előtt állítsd alaphelyzetbe a tengelyeket.", "Azzera gli assi prima di spostarli.",
        "移動する前に原点復帰してください。", "이동하기 전에 원점 복귀를 실행하세요.", "Prieš pradėdami judėti nustatykite pradinę padėtį.",
        "Ga naar het nulpunt voordat je de assen beweegt.", "Przed ruchem wykonaj bazowanie osi.",
        "Volte à origem antes de mover os eixos.", "Du axele la origine înainte de deplasare.",
        "Нажмите «В ноль», чтобы снова управлять осями", "Nollställ axlarna innan du förflyttar dem.",
        "กลับจุดเริ่มต้นก่อนขยับแกน", "Eksenleri hareket ettirmeden önce başlangıç konumuna dönün.",
        "Натисніть «У нуль» перед рухом осей.", "Đưa trục về gốc trước khi di chuyển.", "请先回零，再移动各轴。", "請先回原點，再移動各軸。",
    ),
    "Press Settings in the panel header.": (
        "Premeu Configuració a la capçalera del panell.", "Stiskněte Nastavení v záhlaví panelu.",
        "Drücken Sie in der Kopfzeile des Bedienfelds auf „Einstellungen“.",
        "Presione Configuración en el encabezado del panel.", "Sakatu Ezarpenak panelaren goiburuan.",
        "Appuyez sur Paramètres dans l'en-tête du panneau.", "Nyomja meg a Beállítások gombot a panel fejlécében.",
        "Premere Impostazioni nell'intestazione del pannello.", "パネルヘッダーの「設定」を押します。", "패널 헤더에서 설정을 누르세요.",
        "Paspauskite Nustatymai skydelio antraštėje.", "Druk op Instellingen in de paneelkop.",
        "Naciśnij Ustawienia w nagłówku panelu.", "Pressione Configurações no cabeçalho do painel.",
        "Apăsați Setări în antetul panoului.", "Нажмите «Настройки» в заголовке панели.",
        "Tryck på Inställningar i panelhuvudet.", "กด การตั้งค่า ในส่วนหัวของแผง", "Panel başlığında Ayarlar'a basın.",
        "Натисніть Налаштування в заголовку панелі.", "Nhấn Cài đặt trong tiêu đề bảng điều khiển.", "按面板标题中的“设置”。",
        "按面板標題中的「設定」。",
    ),
    "Preview unavailable": (
        "Vista prèvia no disponible", "Náhled není k dispozici", "Vorschau nicht verfügbar",
        "Vista previa no disponible", "Aurrebista ez dago erabilgarri", "Aperçu indisponible",
        "Az előnézet nem érhető el", "Anteprima non disponibile", "プレビューはできません", "미리보기 불가", "Peržiūra negalima",
        "Voorbeeld niet beschikbaar", "Podgląd niedostępny", "Visualização indisponível",
        "Previzualizare indisponibilă", "Эскиз недоступен", "Förhandsgranskning inte tillgänglig",
        "ดูตัวอย่างไม่พร้อมใช้งาน", "Önizleme kullanılamıyor", "Попередній перегляд недоступний",
        "Không có bản xem trước", "预览不可用", "預覽不可用",
    ),
    "Previous page": (
        "Pàgina anterior", "Předchozí stránka", "Vorherige Seite", "Página anterior", "Aurreko orria",
        "Page précédente", "Előző oldal", "Pagina precedente", "前ページへ", "이전 페이지", "Ankstesnis puslapis",
        "Vorige pagina", "Poprzednia strona", "Página anterior", "Pagina anterioară", "Предыдущая страница",
        "Föregående sida", "หน้าก่อนหน้า", "Önceki sayfa", "Попередня сторінка", "Trang trước", "上一页", "上一頁",
    ),
    "Print": (
        "Imprimeix", "Tisk", "Drucken", "Imprimir", "Inprimatu", "Imprimer", "Nyomtatás", "Stampa", "印刷", "인쇄",
        "Spausdinti", "Afdrukken", "Drukuj", "Imprimir", "Imprimare", "Печать", "Skriv ut", "พิมพ์", "Yazdır", "Друк",
        "In", "打印", "列印",
    ),
    "Print History": (
        "Historial d'impressió", "Historie tisku", "Druckhistorie", "Historial de impresión", "Inprimatu historia",
        "Historique d’impression", "Nyomtatási előzmények", "Stampa cronologia", "造形履歴", "인쇄 이력",
        "Spausdinimo istorija", "Geschiedenis afdrukken", "Historia drukowania", "Imprimir histórico",
        "Istoricul tipăririi", "История печати", "Skriv ut historik", "ประวัติการพิมพ์", "Yazdırma Geçmişi",
        "Історія друку", "Lịch sử in", "打印历史", "列印歷史",
    ),
    "Print Speed": (
        "Velocitat d'impressió", "Rychlost tisku", "Druckgeschwindigkeit", "Velocidad de impresión",
        "Inprimatze-abiadura", "Vitesse d'impression", "Nyomtatási sebesség", "Velocità di stampa", "印刷速度", "인쇄 속도",
        "Spausdinimo greitis", "Afdruksnelheid", "Szybkość drukowania", "Velocidade de impressão",
        "Viteza de imprimare", "Скорость печати", "Utskriftshastighet", "ความเร็วในการพิมพ์", "Baskı Hızı",
        "Швидкість друку", "Tốc độ in", "打印速度", "列印速度",
    ),
    "Print Time": (
        "Temps d'impressió", "Doba tisku", "Druckzeit", "Hora de impresión", "Inprimatze-ordua", "Temps d'impression",
        "Nyomtatási idő", "Orario di stampa", "印刷時間", "인쇄 시간", "Spausdinimo laikas", "Afdruktijd", "Czas drukowania",
        "Tempo de impressão", "Timp de imprimare", "Время печати", "Utskriftstid", "เวลาในการพิมพ์", "Yazdırma Süresi",
        "Час друку", "Thời gian in", "打印时间", "列印時間",
    ),
    "Print again": (
        "Imprimeix de nou", "Tiskněte znovu", "Erneut drucken", "Imprimir de nuevo", "Inprimatu berriro",
        "Imprimer à nouveau", "Nyomtasson újra", "Stampa di nuovo", "再印刷", "다시 인쇄", "Spausdinti dar kartą",
        "Druk opnieuw af", "Wydrukuj ponownie", "Imprimir novamente", "Imprimați din nou", "Напечатать снова",
        "Skriv ut igen", "พิมพ์อีกครั้ง", "Tekrar yazdır", "Друкуйте ще раз", "In lại", "再次打印", "再次列印",
    ),
    "Print cancelled": (
        "Impressió cancel·lada", "Tisk zrušen", "Druck abgebrochen", "Impresión cancelada",
        "Inprimaketa bertan behera utzi da", "Impression annulée", "Nyomtatás megszakítva", "Stampa annullata",
        "印刷キャンセル", "인쇄가 취소되었습니다.", "Spausdinimas atšauktas", "Afdrukken geannuleerd", "Drukowanie anulowane",
        "Impressão cancelada", "Imprimare anulată", "Печать отменена", "Utskriften avbröts", "ยกเลิกการพิมพ์แล้ว",
        "Yazdırma iptal edildi", "Друк скасовано", "Đã hủy in", "打印取消", "列印取消",
    ),
    "Print complete": (
        "Impressió completada", "Tisk dokončen", "Druck abgeschlossen", "Impresión completa", "Inprimatu osatuta",
        "Impression terminée", "Nyomtatás kész", "Stampa completata", "印刷完了", "인쇄 완료", "Spausdinimas baigtas",
        "Afdrukken voltooid", "Drukowanie zakończone", "Impressão concluída", "Imprimare finalizată",
        "Печать завершена", "Utskrift klar", "พิมพ์เสร็จสมบูรณ์", "Yazdırma tamamlandı", "Друк завершено", "In xong",
        "打印完成", "列印完成",
    ),
    "Print completed": (
        "Impressió completada", "Tisk dokončen", "Druck abgeschlossen", "Impresión completada", "Inprimatu egin da",
        "Impression terminée", "A nyomtatás befejeződött", "Stampa completata", "印刷完了", "인쇄 완료", "Spausdinimas baigtas",
        "Afdrukken voltooid", "Drukowanie zakończone", "Impressão concluída", "Imprimare finalizată",
        "Печать завершена", "Utskrift klar", "พิมพ์เสร็จแล้ว", "Yazdırma tamamlandı", "Друк завершено", "In xong",
        "打印完成", "列印完成",
    ),
    "Print finished": (
        "Impressió acabada", "Tisk dokončen", "Druck beendet", "Impresión terminada", "Inprimatu amaitu da",
        "Impression terminée", "A nyomtatás kész", "Stampa terminata", "印刷完了", "인쇄가 끝난 상태", "Spausdinimas baigtas",
        "Afdrukken voltooid", "Drukowanie zakończone", "Impressão concluída", "Imprimare terminată", "Печать завершена",
        "Utskriften är klar", "พิมพ์เสร็จแล้ว", "Yazdırma tamamlandı", "Друк завершено", "In xong", "打印完成", "列印完成",
    ),
    "Print job": (
        "Treball d'impressió", "Tisková úloha", "Druckauftrag", "Trabajo de impresión", "Inprimatzeko lana",
        "Travail d'impression", "Nyomtatási feladat", "Lavoro di stampa", "印刷ジョブ", "인쇄 작업", "Spausdinimo užduotis",
        "Afdruktaak", "Zadanie drukowania", "Imprimir trabalho", "Lucrare de imprimare", "Задание печати",
        "Utskriftsjobb", "งานพิมพ์", "Yazdırma işi", "Завдання друку", "Lệnh in", "打印作业", "列印作業",
    ),
    "Print paused": (
        "Impressió en pausa", "Tisk pozastaven", "Druck angehalten", "Impresión en pausa", "Inprimaketa eten da",
        "Impression en pause", "Nyomtatás szünetel", "Stampa in pausa", "印刷一時停止中", "인쇄가 일시 중지되었습니다.",
        "Spausdinimas pristabdytas", "Afdrukken onderbroken", "Drukowanie wstrzymane", "Impressão pausada",
        "Imprimare întreruptă", "Печать приостановлена", "Utskrift pausad", "หยุดการพิมพ์ชั่วคราว",
        "Yazdırma duraklatıldı", "Друк призупинено", "Tạm dừng in", "打印暂停", "列印暫停",
    ),
    "Print progress": (
        "Progrés de la impressió", "Průběh tisku", "Druckfortschritt", "Progreso de impresión", "Inprimatu aurrerapena",
        "Progression de l'impression", "Nyomtatás folyamata", "Stampa avanzamento", "印刷進捗状況", "인쇄 진행",
        "Spausdinimo eiga", "Voortgang afdruk", "Postęp drukowania", "Imprimir progresso", "Progresul imprimării",
        "Ход печати", "Utskriftsförlopp", "ความคืบหน้าในการพิมพ์", "Yazdırma ilerlemesi", "Хід друку", "Tiến độ in",
        "打印进度", "列印進度",
    ),
    "Print requested · {file}": (
        "Impressió sol·licitada · {file}", "Vyžádaný tisk · {file}", "Druck erbeten · {file}",
        "Impresión solicitada · {file}", "Inprimatu eskatua · {file}", "Impression demandée · {file}",
        "Nyomtatás kérve · {file}", "Stampa richiesta · {file}", "印刷要求・{file}", "인쇄 요청됨 · {file}",
        "Reikalingas spausdinimas · {file}", "Afdrukken aangevraagd · {file}", "Zażądano wydruku · {file}",
        "Impressão solicitada · {file}", "Tipărire solicitată · {file}", "Команда печати отправлена · {file}",
        "Utskrift begärd · {file}", "ขอพิมพ์ · {file}", "Yazdırma talep edildi · {file}", "Потрібний друк · {file}",
        "Yêu cầu in · {file}", "要求打印·{file}", "要求列印·{file}",
    ),
    "Print requested.": (
        "Impressió sol·licitada.", "Vyžádaný tisk.", "Druck angefordert.", "Impresión solicitada.",
        "Inprimatu egin da.", "Impression demandée.", "Nyomtatás kért.", "Stampa richiesta.", "印刷が要求されました。",
        "인쇄를 요청했습니다.", "Paprašyta spausdinti.", "Afdrukken gevraagd.", "Zażądano wydruku.", "Impressão solicitada.",
        "Tipărire solicitată.", "Команда печати отправлена.", "Utskrift begärd.", "ขอพิมพ์", "Yazdırma talep edildi.",
        "Потрібний друк.", "Yêu cầu in.", "请求打印。", "請求列印。",
    ),
    "Print resumed": (
        "S'ha reprès la impressió", "Tisk obnoven", "Der Druck wird fortgesetzt", "Impresión reanudada",
        "Inprimatu berriro", "Reprise de l'impression", "A nyomtatás folytatódik", "Stampa ripresa", "印刷再開", "인쇄 재개",
        "Spausdinimas atnaujintas", "Afdrukken hervat", "Drukowanie wznowione", "Impressão retomada",
        "Imprimarea a fost reluată", "Печать возобновлена", "Utskriften återupptogs", "พิมพ์ต่อ",
        "Yazdırma devam ettirildi", "Друк відновлено", "Đã tiếp tục in", "打印恢复", "列印恢復",
    ),
    "Print speed": (
        "Velocitat d'impressió", "Rychlost tisku", "Druckgeschwindigkeit", "Velocidad de impresión",
        "Inprimatzeko abiadura", "Vitesse d'impression", "Nyomtatási sebesség", "Velocità di stampa", "印刷速度", "인쇄 속도",
        "Spausdinimo greitis", "Afdruksnelheid", "Szybkość drukowania", "Velocidade de impressão",
        "Viteza de imprimare", "Скорость печати", "Utskriftshastighet", "ความเร็วในการพิมพ์", "Baskı hızı",
        "Швидкість друку", "Tốc độ in", "打印速度", "列印速度",
    ),
    "Print started · {file}": (
        "Impressió iniciada · {file}", "Tisk zahájen · {file}", "Druck gestartet · {file}",
        "Impresión iniciada · {file}", "Inprimaketa hasi da · {file}", "Impression lancée · {file}",
        "Nyomtatás megkezdődött · {file}", "Stampa avviata · {file}", "印刷開始・{file}", "인쇄 시작됨 · {file}",
        "Spausdinimas pradėtas · {file}", "Afdrukken gestart · {file}", "Rozpoczęto drukowanie · {file}",
        "Impressão iniciada · {file}", "Imprimare a început · {file}", "Печать началась · {file}",
        "Utskrift påbörjad · {file}", "เริ่มการพิมพ์ · {file}", "Yazdırma başladı · {file}", "Друк розпочато · {file}",
        "Đã bắt đầu in · {file}", "打印开始·{file}", "列印開始·{file}",
    ),
    "Print stored file": (
        "Imprimeix el fitxer emmagatzemat", "Tisk uloženého souboru", "Gespeicherte Datei drucken",
        "Imprimir archivo almacenado", "Inprimatu gordetako fitxategia", "Imprimer le fichier stocké",
        "Tárolt fájl nyomtatása", "Stampa il file memorizzato", "保存したファイルを印刷する", "저장 파일 인쇄",
        "Spausdinti išsaugotą failą", "Opgeslagen bestand afdrukken", "Wydrukuj zapisany plik",
        "Imprimir arquivo armazenado", "Imprimați fișierul stocat", "Печать файла из памяти принтера",
        "Skriv ut lagrad fil", "พิมพ์ไฟล์ที่เก็บไว้", "Saklanan dosyayı yazdır", "Друк збереженого файлу",
        "In tập tin đã lưu trữ", "打印存储文件", "列印儲存文件",
    ),
    "Printer": (
        "Impressora", "Tiskárna", "Drucker", "Impresora", "Inprimagailua", "Imprimante", "Nyomtató", "Stampante",
        "プリンター", "프린터", "Spausdintuvas", "Printer", "Drukarka", "Impressora", "Imprimantă", "Принтер", "Skrivare",
        "เครื่องพิมพ์", "Yazıcı", "Принтер", "Máy in", "打印机", "印表機",
    ),
    "Printer Files": (
        "Fitxers d'impressora", "Soubory tiskárny", "Druckerdateien", "Archivos de impresora",
        "Inprimagailu-fitxategiak", "Fichiers d'imprimante", "Nyomtatófájlok", "File della stampante", "プリンターファイル",
        "프린터 파일", "Spausdintuvo failai", "Printerbestanden", "Pliki drukarkowe", "Arquivos de impressora",
        "Fișiere de imprimantă", "Файлы принтера", "Skrivarfiler", "ไฟล์เครื่องพิมพ์", "Yazıcı Dosyaları",
        "Файли принтера", "Tệp máy in", "打印机文件", "印表機文件",
    ),
    "Printer IPv4 address": (
        "Adreça IPv4 de la impressora", "Adresa tiskárny IPv4", "Drucker-IPv4-Adresse",
        "Dirección IPv4 de la impresora", "Inprimagailuaren IPv4 helbidea", "Adresse IPv4 de l'imprimante",
        "A nyomtató IPv4 címe", "Indirizzo IPv4 della stampante", "プリンター IPv4 アドレス", "프린터 IPv4 주소",
        "Spausdintuvo IPv4 adresas", "Printer IPv4-adres", "Adres drukarki IPv4", "Endereço da impressora IPv4",
        "Adresa imprimantei IPv4", "Адрес IPv4 принтера", "Skrivarens IPv4-adress", "ที่อยู่เครื่องพิมพ์ IPv4",
        "Yazıcı IPv4 adresi", "Адреса принтера IPv4", "Địa chỉ máy in IPv4", "打印机IPv4地址", "印表機IPv4位址",
    ),
    "Printer Readings": (
        "Lectures de la impressora", "Údaje tiskárny", "Druckermesswerte", "Lecturas de la impresora",
        "Inprimagailuaren irakurketak", "Lectures de l'imprimante", "Nyomtató leolvasása", "Letture stampante",
        "プリンターの測定値", "프린터 판독값", "Spausdintuvo rodmenys", "Printerwaarden", "Odczyty drukarki", "Leituras da impressora",
        "Citirile imprimantă", "Показания принтера", "Skrivaravläsningar", "การอ่านเครื่องพิมพ์", "Yazıcı Okumaları",
        "Показання принтера", "Bài đọc trên máy in", "打印机读数", "印表機讀數",
    ),
    "Printer accepted the command.": (
        "La impressora ha acceptat l'ordre.", "Tiskárna přijala příkaz.", "Drucker hat den Befehl akzeptiert.",
        "La impresora aceptó el comando.", "Inprimagailuak komandoa onartu du.", "L'imprimante a accepté la commande.",
        "A nyomtató elfogadta a parancsot.", "La stampante ha accettato il comando.", "プリンターがコマンドを受け付けました。",
        "프린터가 명령을 수락했습니다.", "Spausdintuvas priėmė komandą.", "Printer accepteerde de opdracht.",
        "Drukarka zaakceptowała polecenie.", "A impressora aceitou o comando.", "Imprimanta a acceptat comanda.",
        "Принтер принял команду.", "Skrivaren accepterade kommandot.", "เครื่องพิมพ์ยอมรับคำสั่ง",
        "Yazıcı komutu kabul etti.", "Принтер прийняв команду.", "Máy in chấp nhận lệnh.", "打印机接受命令。", "印表機接受指令。",
    ),
    "Printer accepted the command. Axis limits could not be checked.": (
        "La impressora ha acceptat l'ordre. No s'han pogut comprovar els límits dels eixos.",
        "Tiskárna přijala příkaz. Limity os nebylo možné zkontrolovat.",
        "Drucker akzeptierte den Befehl. Achsengrenzen konnten nicht überprüft werden.",
        "La impresora aceptó el comando. No se pudieron comprobar los límites del eje.",
        "Inprimagailuak komandoa onartu du. Ardatzen mugak ezin izan dira egiaztatu.",
        "L'imprimante a accepté la commande. Les limites de l'axe n'ont pas pu être vérifiées.",
        "A nyomtató elfogadta a parancsot. A tengelyhatárokat nem lehetett ellenőrizni.",
        "La stampante ha accettato il comando. Non è stato possibile controllare i limiti dell'asse.",
        "プリンターがコマンドを受け付けました。軸の制限をチェックできませんでした。", "프린터가 명령을 수락했습니다. 축 제한을 확인할 수 없습니다.",
        "Spausdintuvas priėmė komandą. Nepavyko patikrinti ašies ribų.",
        "Printer accepteerde de opdracht. Aslimieten konden niet worden gecontroleerd.",
        "Drukarka zaakceptowała polecenie. Nie można sprawdzić granic osi.",
        "A impressora aceitou o comando. Os limites do eixo não puderam ser verificados.",
        "Imprimanta a acceptat comanda. Limitele axelor nu au putut fi verificate.",
        "Принтер принял команду. Проверить пределы хода осей не удалось.",
        "Skrivaren accepterade kommandot. Axelgränser kunde inte kontrolleras.",
        "เครื่องพิมพ์ยอมรับคำสั่ง ไม่สามารถตรวจสอบขีดจำกัดของแกนได้",
        "Yazıcı komutu kabul etti. Eksen sınırları kontrol edilemedi.",
        "Принтер прийняв команду. Не вдалося перевірити межі осі.",
        "Máy in chấp nhận lệnh. Không thể kiểm tra giới hạn trục.", "打印机接受命令。无法检查轴限制。", "印表機接受指令。無法檢查軸限制。",
    ),
    "Printer connection": (
        "Connexió de la impressora", "Připojení tiskárny", "Druckeranschluss", "Conexión de impresora",
        "Inprimagailuaren konexioa", "Connexion de l'imprimante", "Nyomtatócsatlakozás", "Collegamento stampante",
        "プリンター接続", "프린터 접속", "Spausdintuvo jungtis", "Printeraansluiting", "Podłączenie drukarki",
        "Conexão da impressora", "Conexiune la imprimantă", "Подключение принтера", "Skrivaranslutning",
        "การเชื่อมต่อเครื่องพิมพ์", "Yazıcı bağlantısı", "Підключення принтера", "Kết nối máy in", "打印机连接", "印表機連接",
    ),
    "Printer control is unavailable.": (
        "El control de la impressora no està disponible.", "Ovládání tiskárny je nedostupné.",
        "Die Druckersteuerung ist nicht verfügbar.", "El control de la impresora no está disponible.",
        "Inprimagailuaren kontrola ez dago erabilgarri.", "Le contrôle de l'imprimante n'est pas disponible.",
        "A nyomtatóvezérlés nem érhető el.", "Il controllo della stampante non è disponibile.", "プリンター制御ができません。",
        "프린터 제어가 불가능합니다.", "Spausdintuvo valdymas nepasiekiamas.", "Printerbesturing is niet beschikbaar.",
        "Sterowanie drukarką jest niedostępne.", "O controle da impressora não está disponível.",
        "Controlul imprimantei este indisponibil.", "Управление принтером недоступно.",
        "Skrivarkontroll är inte tillgänglig.", "การควบคุมเครื่องพิมพ์ไม่พร้อมใช้งาน", "Yazıcı kontrolü mevcut değil.",
        "Керування принтером недоступне.", "Điều khiển máy in không khả dụng.", "打印机控制不可用。", "印表機控制不可用。",
    ),
    "Printer denied control.": (
        "Control denegat de la impressora.", "Tiskárna odepřela kontrolu.", "Drucker verweigert die Steuerung.",
        "Control denegado de la impresora.", "Inprimagailuak kontrola ukatu du.", "L'imprimante a refusé le contrôle.",
        "A nyomtató vezérlése megtagadva.", "Controllo negato stampante.", "プリンターが制御を拒否しました。", "프린터 제어가 거부되었습니다.",
        "Spausdintuvo nevaldymas.", "Printer heeft controle geweigerd.", "Odmowa kontroli drukarki.",
        "Controle negado da impressora.", "Controlul refuzat imprimantei.", "Принтер отказал в управлении.",
        "Skrivaren nekad kontroll.", "เครื่องพิมพ์ปฏิเสธการควบคุม", "Yazıcı kontrolü reddetti.", "Принтер не керує.",
        "Máy in bị từ chối kiểm soát.", "打印机拒绝控制。", "印表機拒絕控制。",
    ),
    "Printer denied manual control.": (
        "A la impressora s'ha denegat el control manual.", "Tiskárna odepřela ruční ovládání.",
        "Drucker verweigert manuelle Steuerung.", "Impresora denegada control manual.",
        "Inprimagailuak eskuzko kontrola ukatu du.", "L'imprimante a refusé le contrôle manuel.",
        "A nyomtató megtagadta a kézi vezérlést.", "La stampante ha negato il controllo manuale.", "プリンターが手動制御を拒否しました。",
        "프린터가 수동 제어를 거부했습니다.", "Spausdintuvui neleidžiama valdyti rankiniu būdu.",
        "Printer heeft handmatige bediening geweigerd.", "Drukarka odmówiła ręcznego sterowania.",
        "Impressora negou controle manual.", "Imprimanta a refuzat controlul manual.",
        "Принтер отклонил ручное управление.", "Skrivaren nekad manuell kontroll.",
        "เครื่องพิมพ์ปฏิเสธการควบคุมด้วยตนเอง", "Yazıcı manuel kontrolü reddetti.",
        "Принтеру заборонено ручне керування.", "Máy in bị từ chối điều khiển thủ công.", "打印机拒绝手动控制。", "印表機拒絕手動控制。",
    ),
    "Printer did not confirm that the axes are idle.": (
        "La impressora no ha confirmat que els eixos estiguin inactius.", "Tiskárna nepotvrdila, že jsou osy nečinné.",
        "Der Drucker hat nicht bestätigt, dass die Achsen im Leerlauf sind.",
        "La impresora no confirmó que los ejes estén inactivos.",
        "Inprimagailuak ez du baieztatu ardatzak inaktibo daudela.",
        "L'imprimante n'a pas confirmé que les axes sont inactifs.",
        "A nyomtató nem erősítette meg, hogy a tengelyek tétlenek.",
        "La stampante non ha confermato che gli assi sono inattivi.", "プリンタは軸がアイドルであることを確認しませんでした。",
        "프린터가 축이 유휴 상태인지 확인하지 못했습니다.", "Spausdintuvas nepatvirtino, kad ašys neveikia.",
        "De printer heeft niet bevestigd dat de assen inactief zijn.",
        "Drukarka nie potwierdziła, że osie są bezczynne.", "A impressora não confirmou que os eixos estão ociosos.",
        "Imprimanta nu a confirmat că axele sunt inactive.", "Принтер не подтвердил остановку осей.",
        "Skrivaren bekräftade inte att axlarna är lediga.", "เครื่องพิมพ์ไม่ได้ยืนยันว่าแกนไม่ได้ใช้งาน",
        "Yazıcı eksenlerin boşta olduğunu onaylamadı.", "Принтер не підтвердив, що осі неактивні.",
        "Máy in không xác nhận rằng các trục đang ở trạng thái rảnh.", "打印机未确认轴是否空闲。", "印表機未確認軸是否閒置。",
    ),
    "Printer error": (
        "Error de la impressora", "Chyba tiskárny", "Druckerfehler", "Error de impresora", "Inprimagailuaren errorea",
        "Erreur d'imprimante", "Nyomtatóhiba", "Errore stampante", "プリンターエラー", "프린터 오류", "Spausdintuvo klaida",
        "Printerfout", "Błąd drukarki", "Erro de impressora", "Eroare imprimantă", "Ошибка принтера", "Skrivarfel",
        "เครื่องพิมพ์เกิดข้อผิดพลาด", "Yazıcı hatası", "Помилка принтера", "Lỗi máy in", "打印机错误", "印表機錯誤",
    ),
    "Printer jobs": (
        "Treballs d'impressora", "Tiskové úlohy", "Druckeraufträge", "Trabajos de impresora", "Inprimagailu-lanak",
        "Travaux d'impression", "Nyomtatói feladatok", "Lavori della stampante", "プリンタージョブ", "프린터 작업",
        "Spausdintuvo užduotys", "Printertaken", "Zadania drukarki", "Trabalhos de impressora", "Lucrări de imprimantă",
        "Задания принтера", "Skrivarjobb", "งานเครื่องพิมพ์", "Yazıcı işleri", "Завдання принтера", "Công việc in ấn",
        "打印机作业", "印表機作業",
    ),
    "Printer must be idle.": (
        "La impressora ha d'estar inactiva.", "Tiskárna musí být nečinná.", "Der Drucker muss im Leerlauf sein.",
        "La impresora debe estar inactiva.", "Inprimagailuak inaktibo egon behar du.",
        "L'imprimante doit être inactive.", "A nyomtatónak tétlennek kell lennie.",
        "La stampante deve essere inattiva.", "プリンターはアイドル状態でなければなりません。", "프린터가 유휴 상태여야 합니다.",
        "Spausdintuvas turi neveikti.", "Printer moet inactief zijn.", "Drukarka musi być bezczynna.",
        "A impressora deve estar ociosa.", "Imprimanta trebuie să fie inactivă.", "Принтер должен быть свободен.",
        "Skrivaren måste vara inaktiv.", "เครื่องพิมพ์จะต้องไม่ได้ใช้งาน", "Yazıcı boşta olmalıdır.",
        "Принтер має бути неактивним.", "Máy in phải ở chế độ chờ.", "打印机必须处于空闲状态。", "印表機必須處於閒置狀態。",
    ),
    "Printer must be idle. Wait for movement or Home to finish.": (
        "La impressora ha d’estar inactiva. Espera que acabi el moviment o la tornada a l’origen.",
        "Tiskárna musí být v klidu. Počkejte, až skončí pohyb nebo návrat do výchozí polohy.",
        "Der Drucker muss im Leerlauf sein. Warten Sie, bis die Bewegung oder die Referenzfahrt abgeschlossen ist.",
        "La impresora debe estar inactiva. Espera a que termine el movimiento o el regreso al origen.",
        "Inprimagailuak geldirik egon behar du. Itxaron mugimendua edo hasierako posiziora itzultzea amaitu arte.",
        "L’imprimante doit être au repos. Attendez la fin du déplacement ou de la prise d’origine.",
        "A nyomtatónak tétlennek kell lennie. Várd meg a mozgás vagy az alaphelyzetbe állítás végét.",
        "La stampante deve essere inattiva. Attendi il termine del movimento o dell’azzeramento.",
        "プリンターは待機状態である必要があります。移動または原点復帰が終わるまでお待ちください。", "프린터가 대기 상태여야 합니다. 이동 또는 원점 복귀가 끝날 때까지 기다리세요.",
        "Spausdintuvas turi būti laisvas. Palaukite, kol baigsis judėjimas arba pradinės padėties nustatymas.",
        "De printer moet inactief zijn. Wacht tot de assen stilstaan of het nulpunt hebben bereikt.",
        "Drukarka musi być bezczynna. Poczekaj na zakończenie ruchu lub bazowania.",
        "A impressora deve estar ociosa. Aguarde o fim do movimento ou do retorno à origem.",
        "Imprimanta trebuie să fie inactivă. Așteaptă finalizarea deplasării sau a revenirii la origine.",
        "Дождитесь, пока принтер закончит перемещение или обнуление осей.",
        "Skrivaren måste vara inaktiv. Vänta tills förflyttningen eller körningen till nolläget är klar.",
        "เครื่องพิมพ์ต้องอยู่ในสถานะว่าง รอให้การเคลื่อนที่หรือการกลับจุดเริ่มต้นเสร็จสิ้น",
        "Yazıcı boşta olmalıdır. Hareketin veya başlangıç konumuna dönüşün tamamlanmasını bekleyin.",
        "Принтер має бути вільним. Дочекайтеся завершення переміщення або повернення в нуль.",
        "Máy in phải ở chế độ chờ. Đợi các trục di chuyển xong hoặc trở về gốc.", "打印机必须处于空闲状态。请等待移动或回零完成。",
        "印表機必須處於閒置狀態。請等待移動或回原點完成。",
    ),
    "Printer must be idle. {reason}": (
        "La impressora ha d'estar inactiva. {reason}", "Tiskárna musí být nečinná. {reason}",
        "Der Drucker muss im Leerlauf sein. {reason}", "La impresora debe estar inactiva. {reason}",
        "Inprimagailuak inaktibo egon behar du. {reason}", "L'imprimante doit être inactive. {reason}",
        "A nyomtatónak tétlennek kell lennie. {reason}", "La stampante deve essere inattiva. {reason}",
        "プリンターはアイドル状態である必要があります。 {reason}", "프린터가 유휴 상태여야 합니다. {reason}", "Spausdintuvas turi neveikti. {reason}",
        "Printer moet inactief zijn. {reason}", "Drukarka musi być bezczynna. {reason}",
        "A impressora deve estar ociosa. {reason}", "Imprimanta trebuie să fie inactivă. {reason}",
        "Принтер должен быть свободен. {reason}", "Skrivaren måste vara inaktiv. {reason}",
        "เครื่องพิมพ์จะต้องไม่ได้ใช้งาน {reason}", "Yazıcı boşta olmalıdır. {reason}",
        "Принтер має бути неактивним. {reason}", "Máy in phải ở chế độ chờ. {reason}", "打印机必须处于空闲状态。 {reason}",
        "印表機必須處於閒置狀態。 {reason}",
    ),
    "Printer rejected the selected file.": (
        "La impressora ha rebutjat el fitxer seleccionat.", "Tiskárna odmítla vybraný soubor.",
        "Der Drucker hat die ausgewählte Datei abgelehnt.", "La impresora rechazó el archivo seleccionado.",
        "Inprimagailuak aukeratutako fitxategia baztertu du.", "L'imprimante a rejeté le fichier sélectionné.",
        "A nyomtató elutasította a kiválasztott fájlt.", "La stampante ha rifiutato il file selezionato.",
        "選択したファイルをプリンタが拒否しました。", "프린터가 선택한 파일을 거부했습니다.", "Spausdintuvas atmetė pasirinktą failą.",
        "De printer heeft het geselecteerde bestand afgewezen.", "Drukarka odrzuciła wybrany plik.",
        "A impressora rejeitou o arquivo selecionado.", "Imprimanta a respins fișierul selectat.",
        "Принтер отклонил выбранный файл.", "Skrivaren avvisade den valda filen.", "เครื่องพิมพ์ปฏิเสธไฟล์ที่เลือก",
        "Yazıcı seçilen dosyayı reddetti.", "Принтер відхилив вибраний файл.", "Máy in từ chối tệp đã chọn.",
        "打印机拒绝选择的文件。", "印表機拒絕選擇的文件。",
    ),
    "Printer rejected {command}.": (
        "La impressora ha rebutjat {command}.", "Tiskárna odmítnuta {command}.", "Drucker abgelehnt {command}.",
        "Impresora rechazada {command}.", "Inprimagailuak baztertu du {command}.", "L'imprimante a rejeté {command}.",
        "A nyomtató elutasította a {command}-t.", "Stampante rifiutata {command}.", "プリンターが{command}を拒否しました。",
        "프린터가 {command}를 거부했습니다.", "Spausdintuvas atmestas {command}.", "Printer heeft {command} afgewezen.",
        "Drukarka odrzuciła {command}.", "A impressora rejeitou {command}.", "Imprimanta a respins {command}.",
        "Принтер отклонил команду «{command}».", "Skrivaren avvisades {command}.", "เครื่องพิมพ์ถูกปฏิเสธ {command}",
        "Yazıcı {command}'yi reddetti.", "Принтер відхилив {command}.", "Máy in bị từ chối {command}.",
        "打印机拒绝{command}。", "印表機拒絕{command}。",
    ),
    "Printer reported an error": (
        "La impressora ha informat d'un error", "Tiskárna ohlásila chybu", "Der Drucker hat einen Fehler gemeldet",
        "La impresora informó un error", "Inprimagailuak errore bat eman du", "L'imprimante a signalé une erreur",
        "A nyomtató hibát jelentett", "La stampante ha segnalato un errore", "プリンターがエラーを報告しました", "프린터에서 오류를 보고했습니다.",
        "Spausdintuvas pranešė apie klaidą", "Printer heeft een fout gerapporteerd", "Drukarka zgłosiła błąd",
        "A impressora relatou um erro", "Imprimanta a raportat o eroare", "Принтер сообщил об ошибке",
        "Skrivaren rapporterade ett fel", "เครื่องพิมพ์รายงานข้อผิดพลาด", "Yazıcı bir hata bildirdi",
        "Принтер повідомив про помилку", "Máy in báo lỗi", "打印机报告错误", "印表機報告錯誤",
    ),
    "Printer reported an error.": (
        "La impressora ha informat d'un error.", "Tiskárna ohlásila chybu.", "Der Drucker hat einen Fehler gemeldet.",
        "La impresora informó un error.", "Inprimagailuak errore bat eman du.", "L'imprimante a signalé une erreur.",
        "A nyomtató hibát jelentett.", "La stampante ha segnalato un errore.", "プリンターがエラーを報告しました。",
        "프린터에서 오류가 보고되었습니다.", "Spausdintuvas pranešė apie klaidą.", "Printer heeft een fout gerapporteerd.",
        "Drukarka zgłosiła błąd.", "A impressora relatou um erro.", "Imprimanta a raportat o eroare.",
        "Принтер сообщил об ошибке.", "Skrivaren rapporterade ett fel.", "เครื่องพิมพ์รายงานข้อผิดพลาด",
        "Yazıcı bir hata bildirdi.", "Принтер повідомив про помилку.", "Máy in báo lỗi.", "打印机报告错误。", "印表機報告錯誤。",
    ),
    "Printer status unavailable.": (
        "Estat de la impressora no disponible.", "Stav tiskárny není k dispozici.", "Druckerstatus nicht verfügbar.",
        "Estado de la impresora no disponible.", "Inprimagailuaren egoera ez dago erabilgarri.",
        "État de l'imprimante indisponible.", "A nyomtató állapota nem érhető el.",
        "Stato della stampante non disponibile.", "プリンターのステータスが利用できません。", "프린터 상태를 확인할 수 없습니다.",
        "Spausdintuvo būsena nepasiekiama.", "Printerstatus niet beschikbaar.", "Stan drukarki niedostępny.",
        "Status da impressora indisponível.", "Starea imprimantei indisponibilă.", "Состояние принтера недоступно.",
        "Skrivarstatus inte tillgänglig.", "สถานะเครื่องพิมพ์ไม่พร้อมใช้งาน", "Yazıcı durumu mevcut değil.",
        "Статус принтера недоступний.", "Trạng thái máy in không khả dụng.", "打印机状态不可用。", "印表機狀態不可用。",
    ),
    "Printers": (
        "Impressores", "Tiskárny", "Drucker", "Impresoras", "Inprimagailuak", "Imprimantes", "Nyomtatók", "Stampanti",
        "プリンター", "프린터", "Spausdintuvai", "Printers", "Drukarki", "Impressoras", "Imprimante", "Принтеры", "Skrivare",
        "เครื่องพิมพ์", "Yazıcılar", "Принтери", "Máy in", "打印机", "印表機",
    ),
    "Printing": (
        "Impressió", "Tisk", "Drucken", "Impresión", "Inprimaketa", "Impression", "Nyomtatás", "Stampa", "印刷", "인쇄",
        "Spausdinimas", "Afdrukken", "Drukowanie", "Impressão", "Imprimare", "Печать", "Utskrift", "การพิมพ์",
        "Yazdırma", "Друк", "In ấn", "打印", "列印",
    ),
    "Printing started": (
        "S'ha iniciat la impressió", "Tisk zahájen", "Der Druckvorgang wurde gestartet", "Impresión iniciada",
        "Inprimaketa hasi da", "L'impression a démarré", "A nyomtatás megkezdődött", "La stampa è iniziata", "印刷開始",
        "인쇄가 시작되었습니다", "Spausdinimas pradėtas", "Het afdrukken is gestart", "Rozpoczęto drukowanie",
        "Impressão iniciada", "Imprimarea a început", "Печать началась.", "Utskrift påbörjad", "เริ่มต้นการพิมพ์",
        "Yazdırma başladı", "Розпочато друк", "Bắt đầu in", "打印开始", "列印開始",
    ),
    "Print…": (
        "Imprimeix...", "Tisk…", "Drucken…", "Imprimir…", "Inprimatu…", "Imprimer…", "Nyomtatás…", "Stampa…", "印刷…",
        "인쇄…", "Spausdinti…", "Afdrukken…", "Drukuj…", "Imprimir…", "Imprimați...", "Печать…", "Skriv ut...",
        "พิมพ์...", "Yazdır…", "Друк...", "In…", "打印...", "列印...",
    ),
    "Range": (
        "Interval", "Rozsah", "Reichweite", "Rango", "Tartea", "Portée", "Tartomány", "Intervallo", "範囲", "범위",
        "Diapazonas", "Bereik", "Zasięg", "Alcance", "Interval", "Диапазон", "Räckvidd", "พิสัย", "Aralık", "Діапазон",
        "Phạm vi", "范围", "範圍",
    ),
    "Readings delayed": (
        "Lectures retardades", "Zpoždění čtení", "Messwerte verzögert", "Lecturas retrasadas", "Irakurketak atzeratuta",
        "Lectures retardées", "Leolvasás késik", "Letture ritardate", "測定値の遅れ", "판독 지연", "Skaitymai vėluoja",
        "Metingen vertraagd", "Odczyty opóźnione", "Leituras atrasadas", "Citirile întârziate",
        "Показания поступают с задержкой", "Avläsningar försenade", "การอ่านล่าช้า", "Okumalar gecikti",
        "Зчитування затримано", "Bài đọc bị trì hoãn", "读数延迟", "讀數延遲",
    ),
    "Readings unavailable": (
        "Lectures no disponibles", "Údaje nejsou k dispozici", "Messwerte nicht verfügbar", "Lecturas no disponibles",
        "Irakurgaiak ez daude erabilgarri", "Lectures indisponibles", "Az adatok nem érhetők el",
        "Letture non disponibili", "測定値がありません", "열람 불가", "Rodmenys nepasiekiami", "Metingen niet beschikbaar",
        "Odczyty niedostępne", "Leituras indisponíveis", "Citirile indisponibile", "Нет показаний",
        "Avläsningar inte tillgängliga", "ไม่สามารถอ่านค่าได้", "Okumalar mevcut değil", "Показання недоступні",
        "Không có bài đọc", "读数不可用", "讀數不可用",
    ),
    "Ready": (
        "A punt", "Připraveno", "Bereit", "Listo", "Prest", "Prête", "Kész", "Pronto", "待機中", "준비", "Paruošta", "Klaar",
        "Gotowe", "Pronto", "Gata", "Готов", "Klar", "พร้อม", "Hazır", "Готовий", "Sẵn sàng", "就绪", "就緒",
    ),
    "Ready to move": (
        "A punt per moure’s", "Připraveno k pohybu", "Bereit zur Bewegung", "Listo para moverse", "Mugitzeko prest",
        "Prêt au déplacement", "Mozgásra kész", "Pronto al movimento", "移動可能", "이동 준비 완료", "Pasiruošta judėti",
        "Klaar om te bewegen", "Gotowy do ruchu", "Pronto para mover", "Pregătit pentru deplasare",
        "Можно перемещать оси", "Redo att förflytta", "พร้อมเคลื่อนที่", "Harekete hazır", "Осі готові до руху",
        "Có thể di chuyển", "可以移动", "可以移動",
    ),
    "Reconnect camera": (
        "Torneu a connectar la càmera", "Znovu připojte kameru", "Kamera erneut anschließen",
        "Vuelva a conectar la cámara", "Berriro konektatu kamera", "Reconnecter la caméra",
        "Csatlakoztassa újra a kamerát", "Ricollegare la fotocamera", "カメラを再接続します", "카메라 다시 연결",
        "Iš naujo prijunkite kamerą", "Sluit de camera opnieuw aan", "Podłącz ponownie kamerę", "Reconecte a câmera",
        "Reconectați camera", "Подключить камеру снова", "Återanslut kameran", "เชื่อมต่อกล้องอีกครั้ง",
        "Kamerayı yeniden bağlayın", "Повторно підключіть камеру", "Kết nối lại camera", "重新连接相机", "重新連接相機",
    ),
    "Refresh": (
        "Actualitzar", "Obnovit", "Aktualisieren", "Actualizar", "Freskatu", "Actualiser", "Frissítés", "Aggiorna",
        "リフレッシュ", "리프레시", "Atnaujinti", "Vernieuwen", "Odśwież", "Atualizar", "Reîmprospătare", "Обновить", "Uppdatera",
        "รีเฟรช", "Yenile", "Оновити", "Làm mới", "刷新", "刷新",
    ),
    "Refresh the file list and check the loaded materials.": (
        "Actualitzeu la llista de fitxers i comproveu els materials carregats.",
        "Obnovte seznam souborů a zkontrolujte vložené materiály.",
        "Aktualisieren Sie die Dateiliste und überprüfen Sie die geladenen Materialien.",
        "Actualice la lista de archivos y verifique los materiales cargados.",
        "Freskatu fitxategien zerrenda eta egiaztatu kargatutako materialak.",
        "Actualisez la liste des fichiers et vérifiez les matériaux chargés.",
        "Frissítse a fájllistát és ellenőrizze a betöltött anyagokat.",
        "Aggiorna l'elenco dei file e controlla i materiali caricati.", "ファイルリストを更新し、読み込んだ素材を確認します。",
        "파일 목록을 새로 고치고 로드된 소재를 확인하세요.", "Atnaujinkite failų sąrašą ir patikrinkite įkeltas medžiagas.",
        "Ververs de bestandslijst en controleer de geladen materialen.",
        "Odśwież listę plików i sprawdź załadowane materiały.",
        "Atualize a lista de arquivos e verifique os materiais carregados.",
        "Reîmprospătați lista de fișiere și verificați materialele încărcate.",
        "Обновите список файлов и проверьте загруженные материалы.",
        "Uppdatera fillistan och kontrollera det laddade materialet.", "รีเฟรชรายการไฟล์และตรวจสอบวัสดุที่โหลด",
        "Dosya listesini yenileyin ve yüklenen malzemeleri kontrol edin.",
        "Оновіть список файлів і перевірте завантажені матеріали.",
        "Làm mới danh sách tập tin và kiểm tra các tài liệu đã tải.", "刷新文件列表，查看加载的素材。", "刷新文件列表，查看已載入的素材。",
    ),
    "Release motors": (
        "Desbloqueja els motors", "Uvolnit motory", "Motoren entriegeln", "Desbloquear motores", "Desblokeatu motorrak",
        "Déverrouiller les moteurs", "Motorok kioldása", "Sblocca i motori", "モーターを解放", "모터 잠금 해제",
        "Atlaisvinti variklius", "Motoren ontgrendelen", "Odblokuj silniki", "Destravar motores",
        "Deblochează motoarele", "Отпустить моторы", "Frikoppla motorerna", "ปลดล็อกมอเตอร์", "Motorların kilidini aç",
        "Розблокувати двигуни", "Mở khóa động cơ", "解除电机锁定", "解除馬達鎖定",
    ),
    "Remove": (
        "Eliminar", "Odstraňte", "Entfernen", "Eliminar", "Kendu", "Supprimer", "Távolítsa el", "Rimuovi", "削除", "삭제",
        "Pašalinti", "Verwijderen", "Usuń", "Remover", "Îndepărtați", "Удалить", "Ta bort", "ลบออก", "Kaldır",
        "Видалити", "Xóa", "删除", "刪除",
    ),
    "Replace Printer tab": (
        "Substitueix el contingut de la pestanya Impressora", "Nahradit obsah karty Tiskárna",
        "Inhalt des Drucker-Tabs ersetzen", "Sustituir el contenido de la pestaña Impresora",
        "Ordeztu Inprimagailua fitxaren edukia", "Remplacer le contenu de l’onglet Imprimante",
        "A Nyomtató lap tartalmának cseréje", "Sostituisci il contenuto della scheda Stampante", "プリンタータブの内容を置き換える",
        "프린터 탭의 콘텐츠 바꾸기", "Pakeisti spausdintuvo skirtuko turinį", "Inhoud van het tabblad Printer vervangen",
        "Zastąp zawartość karty Drukarka", "Substituir o conteúdo da aba Impressora",
        "Înlocuiește conținutul filei Imprimantă", "Заменить содержимое вкладки «Принтер»",
        "Ersätt innehållet på fliken Skrivare", "แทนที่เนื้อหาของแท็บเครื่องพิมพ์",
        "Yazıcı sekmesinin içeriğini değiştir", "Замінити вміст вкладки «Принтер»", "Thay nội dung thẻ Máy in",
        "替换“打印机”选项卡的内容", "取代「印表機」分頁的內容",
    ),
    "Request failed.": (
        "La sol·licitud ha fallat.", "Požadavek se nezdařil.", "Anfrage fehlgeschlagen.", "La solicitud falló.",
        "Eskaerak huts egin du.", "La demande a échoué.", "A kérés sikertelen.", "Richiesta fallita.", "リクエストが失敗しました。",
        "요청이 실패했습니다.", "Užklausa nepavyko.", "Verzoek mislukt.", "Żądanie nie powiodło się.", "Falha na solicitação.",
        "Solicitarea a eșuat.", "Не удалось выполнить запрос.", "Begäran misslyckades.", "คำขอล้มเหลว",
        "İstek başarısız oldu.", "Помилка запиту.", "Yêu cầu không thành công.", "请求失败。", "請求失敗。",
    ),
    "Resume": (
        "Reprendre", "Pokračovat", "Fortsetzen", "Reanudar", "Berrekin", "Reprendre", "Folytatás", "Continua", "再開",
        "계속", "Tęsti", "Hervatten", "Wznów", "Continuar", "Reluați", "Продолжить", "Återuppta", "พิมพ์ต่อ", "Sürdür",
        "Продовжити", "Tiếp tục", "继续", "繼續",
    ),
    "Retract": (
        "Retira", "Vysunout filament", "Filament zurückziehen", "Retraer filamento", "Atzeratu filamentua",
        "Rétracter le filament", "Filament visszahúzása", "Ritrai filamento", "引き戻す", "필라멘트 당기기", "Įtraukti giją",
        "Filament terugtrekken", "Cofnij filament", "Retrair filamento", "Retrage filamentul", "Втянуть",
        "Dra tillbaka", "ดึงเส้นกลับ", "Filamenti geri çek", "Втягнути", "Rút sợi in", "退料", "退料",
    ),
    "Save": (
        "Desa", "Uložit", "Speichern", "Guardar", "Gorde", "Enregistrer", "Mentés", "Salva", "保存", "저장", "Išsaugoti",
        "Opslaan", "Zapisz", "Salvar", "Salvare", "Сохранить", "Spara", "บันทึก", "Kaydet", "Зберегти", "Lưu", "保存",
        "保存",
    ),
    "Save snapshot": (
        "Desa la instantània", "Uložit snímek", "Schnappschuss speichern", "Guardar instantánea", "Gorde argazkia",
        "Enregistrer un instantané", "Pillanatkép mentése", "Salva istantanea", "スナップショットの保存", "스냅샷 저장",
        "Išsaugoti momentinę nuotrauką", "Momentopname opslaan", "Zapisz migawkę", "Salvar instantâneo",
        "Salvați instantaneul", "Сохранить снимок", "Spara ögonblicksbild", "บันทึกสแนปชอต", "Anlık görüntüyü kaydet",
        "Зберегти знімок", "Lưu ảnh chụp nhanh", "保存快照", "儲存快照",
    ),
    "Save the printer serial and CheckCode first.": (
        "Deseu primer la sèrie de la impressora i CheckCode.", "Nejprve uložte sériové číslo tiskárny a CheckCode.",
        "Speichern Sie zuerst die Seriennummer des Druckers und CheckCode.",
        "Guarde primero el número de serie de la impresora y CheckCode.",
        "Gorde lehenik inprimagailuaren seriea eta CheckCode.",
        "Enregistrez d'abord le numéro de série de l'imprimante et CheckCode.",
        "Először mentse el a nyomtató sorozatát és a CheckCode-t.",
        "Salvare prima il seriale della stampante e CheckCode.", "まずプリンターのシリアルとCheckCodeを保存します。",
        "먼저 프린터 시리얼과 CheckCode를 저장하세요.", "Pirmiausia išsaugokite serijinį spausdintuvą ir CheckCode.",
        "Sla eerst het printerserienummer en de CheckCode op.", "Najpierw zapisz numer seryjny drukarki i CheckCode.",
        "Salve primeiro o serial da impressora e CheckCode.", "Salvați mai întâi seria imprimantei și CheckCode.",
        "Сначала сохраните серийный номер принтера и CheckCode.", "Spara skrivarens serienummer och CheckCode först.",
        "บันทึกอนุกรมของเครื่องพิมพ์และ CheckCode ก่อน", "Önce yazıcı seri numarasını ve CheckCode'yi kaydedin.",
        "Спочатку збережіть серійний номер принтера та CheckCode.", "Lưu serial máy in và CheckCode trước.",
        "先保存打印机序列号和CheckCode。", "先保存印表機序號和CheckCode。",
    ),
    "Saving file on the printer and checking the result…": (
        "S'està desant el fitxer a la impressora i comprovant el resultat...",
        "Ukládání souboru na tiskárnu a kontrola výsledku…", "Datei auf dem Drucker speichern und Ergebnis prüfen…",
        "Guardar el archivo en la impresora y comprobar el resultado…",
        "Fitxategia gordetzen inprimagailuan eta emaitza egiaztatzen...",
        "Sauvegarde du fichier sur l'imprimante et vérification du résultat…",
        "Fájl mentése a nyomtatóra és az eredmény ellenőrzése…",
        "Salvataggio del file sulla stampante e verifica del risultato...", "プリンターにファイルを保存して結果を確認すると…",
        "파일을 프린터에 저장하고 결과를 확인하는 중…", "Failas išsaugomas spausdintuve ir tikrinamas rezultatas...",
        "Bestand opslaan op de printer en het resultaat controleren…",
        "Zapisywanie pliku na drukarce i sprawdzanie wyniku…",
        "Salvando arquivo na impressora e verificando o resultado…",
        "Se salvează fișierul pe imprimantă și se verifică rezultatul...",
        "Сохранение файла на принтере и проверка результата…", "Sparar fil på skrivaren och kontrollerar resultatet...",
        "บันทึกไฟล์บนเครื่องพิมพ์และตรวจสอบผลลัพธ์...", "Dosya yazıcıya kaydediliyor ve sonuç kontrol ediliyor…",
        "Збереження файлу на принтері та перевірка результату…", "Lưu file vào máy in và kiểm tra kết quả…",
        "将文件保存到打印机并检查结果...", "將文件儲存至印表機並檢查結果...",
    ),
    "Saving…": (
        "S'està desant...", "Ukládání…", "Speichern…", "Guardando…", "Gordetzen…", "Sauvegarde…", "Mentés…",
        "Salvataggio…", "保存中…", "저장 중…", "Išsaugoma…", "Opslaan…", "Zapisywanie…", "Salvando…", "Se salvează…",
        "Сохранение…", "Sparar...", "กำลังบันทึก...", "Kaydediliyor…", "Збереження…", "Đang lưu…", "保存...", "儲存...",
    ),
    "Search files": (
        "Cerca fitxers", "Hledat soubory", "Dateien durchsuchen", "Buscar archivos", "Bilatu fitxategiak",
        "Rechercher des fichiers", "Fájlok keresése", "Cerca file", "ファイルの検索", "파일 검색", "Ieškokite failų",
        "Bestanden zoeken", "Wyszukaj pliki", "Pesquisar arquivos", "Căutați fișiere", "Поиск файлов",
        "Sök efter filer", "ค้นหาไฟล์", "Dosyaları arayın", "Пошук файлів", "Tìm kiếm tập tin", "搜索文件", "搜尋文件",
    ),
    "Search printers and jobs": (
        "Cerca impressores i feines", "Vyhledávání tiskáren a úloh", "Drucker und Jobs suchen",
        "Buscar impresoras y trabajos", "Bilatu inprimagailuak eta lanak", "Rechercher des imprimantes et des tâches",
        "Nyomtatók és munkák keresése", "Cerca stampanti e lavori", "プリンターとジョブを検索する", "프린터 및 작업 검색",
        "Ieškokite spausdintuvų ir darbų", "Zoek printers en taken", "Wyszukaj drukarki i zadania",
        "Pesquisar impressoras e trabalhos", "Căutați imprimante și locuri de muncă", "Поиск принтеров и заданий",
        "Sök efter skrivare och jobb", "ค้นหาเครื่องพิมพ์และงาน", "Yazıcıları ve işleri arayın",
        "Пошук принтерів і завдань", "Tìm kiếm máy in và việc làm", "搜索打印机和作业", "搜尋印表機和作業",
    ),
    "Select a file to print.": (
        "Selecciona un fitxer per imprimir.", "Vyberte soubor k tisku.", "Datei zum Drucken auswählen.",
        "Selecciona un archivo para imprimir.", "Hautatu inprimatzeko fitxategia.",
        "Sélectionnez un fichier à imprimer.", "Válassz ki egy nyomtatandó fájlt.", "Seleziona un file da stampare.",
        "造形するファイルを選択してください。", "출력할 파일을 선택하세요.", "Pasirinkite spausdinamą failą.", "Kies een bestand om te printen.",
        "Wybierz plik do druku.", "Selecione um arquivo para imprimir.", "Selectează un fișier de imprimat.",
        "Выберите файл для печати.", "Välj en fil att skriva ut.", "เลือกไฟล์ที่จะพิมพ์", "Yazdırılacak dosyayı seçin.",
        "Виберіть файл для друку.", "Chọn tệp để in.", "请选择要打印的文件。", "請選擇要列印的檔案。",
    ),
    "Select slots for these file filaments: {numbers}": (
        "Seleccioneu ranures per a aquests filaments de fitxer: {numbers}",
        "Vyberte sloty pro tyto filamenty: {numbers}",
        "Wählen Sie Steckplätze für diese Feilenfilamente aus: {numbers}",
        "Seleccione ranuras para estos filamentos de archivos: {numbers}",
        "Hautatu zirrikituak fitxategi-harizpi hauetarako: {numbers}",
        "Sélectionnez les emplacements pour ces filaments de fichiers : {numbers}",
        "Válasszon nyílásokat a következő fájlszálak számára: {numbers}",
        "Seleziona gli slot per questi filamenti di file: {numbers}", "これらのファイル フィラメントのスロットを選択します: {numbers}",
        "다음 파일 필라멘트에 대한 슬롯 선택: {numbers}", "Pasirinkite šių failų gijų lizdus: {numbers}",
        "Selecteer slots voor deze vijlfilamenten: {numbers}", "Wybierz gniazda dla tych plików: {numbers}",
        "Selecione slots para estes filamentos de arquivo: {numbers}",
        "Selectați sloturi pentru aceste filamente de fișiere: {numbers}",
        "Выберите слоты для материалов файла: {numbers}", "Välj platser för dessa filament: {numbers}",
        "เลือกช่องสำหรับฟิลาเมนต์ของไฟล์เหล่านี้: {numbers}", "Bu dosya filamentleri için yuvaları seçin: {numbers}",
        "Виберіть слоти для цих ниток файлу: {numbers}", "Chọn vị trí cho các sợi tệp này: {numbers}",
        "为这些文件丝选择插槽：{numbers}", "為這些文件絲選擇插槽：{numbers}",
    ),
    "Select the material slots for this file.": (
        "Seleccioneu les ranures de material per a aquest fitxer.", "Vyberte sloty materiálu pro tento soubor.",
        "Wählen Sie die Materialplätze für diese Datei aus.", "Seleccione las ranuras de material para este archivo.",
        "Hautatu fitxategi honetarako material zirrikituak.",
        "Sélectionnez les emplacements de matériau pour ce fichier.", "Válassza ki az anyagnyílásokat ehhez a fájlhoz.",
        "Seleziona gli slot materiale per questo file.", "このファイルの素材スロットを選択します。", "이 파일에 대한 자료 슬롯을 선택합니다.",
        "Pasirinkite šio failo medžiagos plyšius.", "Selecteer de materiaalslots voor dit bestand.",
        "Wybierz gniazda materiałów dla tego pliku.", "Selecione os slots de material para este arquivo.",
        "Selectați sloturile de material pentru acest fișier.", "Выберите слоты с материалами для этого файла.",
        "Välj materialplatser för denna fil.", "เลือกช่องวัสดุสำหรับไฟล์นี้", "Bu dosya için malzeme yuvalarını seçin.",
        "Виберіть слоти матеріалу для цього файлу.", "Chọn khe vật liệu cho tệp này.", "选择该文件的素材槽。", "選擇該文件的素材槽。",
    ),
    "Select the slots used by this file.": (
        "Seleccioneu les ranures utilitzades per aquest fitxer.", "Vyberte sloty používané tímto souborem.",
        "Wählen Sie die von dieser Datei verwendeten Slots aus.", "Seleccione las ranuras utilizadas por este archivo.",
        "Hautatu fitxategi honek erabiltzen dituen zirrikituak.",
        "Sélectionnez les emplacements utilisés par ce fichier.", "Válassza ki a fájl által használt nyílásokat.",
        "Seleziona gli slot utilizzati da questo file.", "このファイルが使用するスロットを選択します。", "이 파일이 사용하는 슬롯을 선택하세요.",
        "Pasirinkite šio failo naudojamus lizdus.", "Selecteer de slots die door dit bestand worden gebruikt.",
        "Wybierz gniazda używane przez ten plik.", "Selecione os slots usados ​​por este arquivo.",
        "Selectați sloturile utilizate de acest fișier.", "Выберите слоты, используемые этим файлом.",
        "Välj de platser som används av den här filen.", "เลือกช่องที่ใช้สำหรับไฟล์นี้",
        "Bu dosya tarafından kullanılan yuvaları seçin.", "Виберіть слоти, які використовуються цим файлом.",
        "Chọn các vị trí được sử dụng bởi tệp này.", "选择该文件使用的槽位。", "選擇該檔案所使用的插槽。",
    ),
    "Sending file…": (
        "S'està enviant el fitxer...", "Odesílání souboru…", "Datei wird gesendet…", "Enviando archivo…",
        "Fitxategia bidaltzen…", "Envoi du fichier…", "Fájl küldése…", "Invio file…", "ファイルを送信中…", "파일을 보내는 중…",
        "Siunčiamas failas…", "Bestand verzenden…", "Wysyłanie pliku…", "Enviando arquivo…", "Se trimite fișierul...",
        "Отправка файла…", "Skickar fil...", "กำลังส่งไฟล์...", "Dosya gönderiliyor…", "Надсилання файлу…",
        "Đang gửi tệp…", "正在发送文件...", "正在傳送文件...",
    ),
    "Sending…": (
        "S'està enviant...", "Odesílání…", "Wird gesendet…", "Enviando…", "Bidaltzen…", "Envoi…", "Küldés…", "Invio…",
        "送信中…", "보내는 중…", "Siunčiama…", "Verzenden…", "Wysyłanie…", "Enviando…", "Se trimite...", "Отправка…",
        "Skickar...", "กำลังส่ง...", "Gönderiliyor…", "Надсилання…", "Đang gửi…", "正在发送...", "正在傳送...",
    ),
    "Separate FlashForge tab (default)": (
        "Pestanya FlashForge separada (per defecte)", "Samostatná karta FlashForge (výchozí)",
        "Separate FlashForge-Registerkarte (Standard)", "Pestaña FlashForge separada (predeterminada)",
        "Bereizi FlashForge fitxa (lehenetsia)", "Onglet FlashForge séparé (par défaut)",
        "Külön FlashForge lap (alapértelmezett)", "Scheda FlashForge separata (predefinita)", "個別のFlashForgeタブ（デフォルト）",
        "별도의 FlashForge 탭(기본값)", "Atskiras FlashForge skirtukas (numatytasis)", "Apart tabblad FlashForge (standaard)",
        "Oddzielna zakładka FlashForge (domyślna)", "Guia FlashForge separada (padrão)",
        "Filă separată FlashForge (implicit)", "Отдельная вкладка FlashForge (по умолчанию)",
        "Separat FlashForge-flik (standard)", "แยกแท็บ FlashForge (ค่าเริ่มต้น)",
        "Ayrı FlashForge sekmesi (varsayılan)", "Окрема вкладка FlashForge (за замовчуванням)",
        "Tab FlashForge riêng biệt (mặc định)", "单独的FlashForge选项卡（默认）", "單獨的FlashForge選項卡（預設）",
    ),
    "Serial": (
        "Serial", "Sériové", "Seriell", "Serie", "Seriea", "Série", "Sorozat", "Seriale", "シリアル", "연재", "Serijos",
        "Serieel", "Numer seryjny", "Série", "Serial", "Серийный номер", "Seriell", "อนุกรม", "Seri", "Серійний",
        "nối tiếp", "串口", "串口",
    ),
    "Serial number": (
        "Número de sèrie", "Sériové číslo", "Seriennummer", "Número de serie", "Serie-zenbakia", "Numéro de série",
        "Sorozatszám", "Numero di serie", "シリアルナンバー", "일련번호", "Serijos numeris", "Serienummer", "Numer seryjny",
        "Número de série", "Număr de serie", "Серийный номер", "Serienummer", "หมายเลขซีเรียล", "Seri numarası",
        "Серійний номер", "Số sê-ri", "序列号", "序號",
    ),
    "Serial number (required for Creator 5 / 5 Pro)": (
        "Número de sèrie (necessari per a Creator 5 / 5 Pro)", "Sériové číslo (vyžadováno pro Creator 5 / 5 Pro)",
        "Seriennummer (erforderlich für Creator 5 / 5 Pro)", "Número de serie (obligatorio para Creator 5 / 5 Pro)",
        "Serie-zenbakia (beharrezkoa Creator 5 / 5 Pro)", "Numéro de série (obligatoire pour Creator 5 / 5 Pro)",
        "Sorozatszám (szükséges a Creator 5 / 5 Pro-hez)", "Numero di serie (richiesto per Creator 5 / 5 Pro)",
        "シリアル番号（Creator 5 / 5 Proに必要）", "일련번호(Creator 5 / 5 Pro에 필요)",
        "Serijos numeris (reikalingas Creator 5 / 5 Pro)", "Serienummer (vereist voor Creator 5 / 5 Pro)",
        "Numer seryjny (wymagany dla Creator 5 / 5 Pro)", "Número de série (obrigatório para Creator 5 / 5 Pro)",
        "Număr de serie (necesar pentru Creator 5 / 5 Pro)", "Серийный номер (требуется для Creator 5 / 5 Pro)",
        "Serienummer (krävs för Creator 5 / 5 Pro)", "หมายเลขซีเรียล (จำเป็นสำหรับ Creator 5 / 5 Pro)",
        "Seri numarası (Creator 5 / 5 Pro için gereklidir)", "Серійний номер (необхідний для Creator 5 / 5 Pro)",
        "Số sê-ri (bắt buộc đối với Creator 5 / 5 Pro)", "序列号（Creator 5 / 5 Pro 必需）", "序號（Creator 5 / 5 Pro 必需）",
    ),
    "Set temperature": (
        "Fixa la temperatura", "Nastavit teplotu", "Solltemperatur einstellen", "Fijar temperatura",
        "Ezarri tenperatura", "Régler la température", "Hőmérséklet beállítása", "Imposta temperatura", "目標温度を設定",
        "목표 온도 설정", "Nustatyti temperatūrą", "Temperatuur instellen", "Ustaw temperaturę", "Definir temperatura",
        "Setează temperatura", "Установить температуру", "Ställ in temperaturen", "ตั้งอุณหภูมิเป้าหมาย",
        "Sıcaklığı ayarla", "Задати температуру", "Đặt nhiệt độ", "设置目标温度", "設定目標溫度",
    ),
    "Heat": (
        "Escalfa", "Ohřát", "Heizen", "Calentar", "Berotu", "Chauffer", "Fűtés", "Scalda", "加熱", "가열", "Šildyti",
        "Verwarmen", "Grzej", "Aquecer", "Încălzește", "Нагреть", "Värm", "ให้ความร้อน", "Isıt", "Нагріти", "Gia nhiệt",
        "加热", "加熱",
    ),
    "Set up your first printer": (
        "Configura la teva primera impressora", "Nastavte svou první tiskárnu", "Richten Sie Ihren ersten Drucker ein",
        "Configure su primera impresora", "Konfiguratu zure lehen inprimagailua",
        "Configurer votre première imprimante", "Állítsa be első nyomtatóját", "Configura la tua prima stampante",
        "初めてのプリンターをセットアップする", "첫 번째 프린터 설정", "Nustatykite pirmąjį spausdintuvą", "Uw eerste printer instellen",
        "Skonfiguruj swoją pierwszą drukarkę", "Configure sua primeira impressora", "Configurați prima imprimantă",
        "Настройте первый принтер", "Konfigurera din första skrivare", "ตั้งค่าเครื่องพิมพ์เครื่องแรกของคุณ",
        "İlk yazıcınızı kurun", "Налаштуйте свій перший принтер", "Thiết lập máy in đầu tiên của bạn", "设置您的第一台打印机",
        "設定您的第一台印表機",
    ),
    "Settings": (
        "Configuració", "Nastavení", "Einstellungen", "Ajustes", "Ezarpenak", "Réglages", "Beállítások", "Impostazioni",
        "設定", "설정", "Parametrai", "Instellingen", "Ustawienia", "Configurações", "Setări", "Настройки", "Inställningar",
        "การตั้งค่า", "Ayarlar", "Налаштування", "Cài đặt", "设置", "設定",
    ),
    "Settings are unavailable.": (
        "La configuració no està disponible.", "Nastavení nejsou dostupná.", "Einstellungen sind nicht verfügbar.",
        "Las configuraciones no están disponibles.", "Ezarpenak ez daude erabilgarri.",
        "Les paramètres ne sont pas disponibles.", "A beállítások nem érhetők el.",
        "Le impostazioni non sono disponibili.", "設定ができません。", "설정이 불가능합니다.", "Nustatymai nepasiekiami.",
        "Instellingen zijn niet beschikbaar.", "Ustawienia są niedostępne.", "As configurações não estão disponíveis.",
        "Setările nu sunt disponibile.", "Настройки недоступны.", "Inställningar är inte tillgängliga.",
        "การตั้งค่าไม่พร้อมใช้งาน", "Ayarlar kullanılamıyor.", "Налаштування недоступні.", "Cài đặt không khả dụng.",
        "设置不可用。", "設定不可用。",
    ),
    "Show file previews": (
        "Mostra les previsualitzacions de fitxers", "Zobrazit náhledy souborů", "Dateivorschau anzeigen",
        "Mostrar vistas previas de archivos", "Erakutsi fitxategien aurrebista", "Afficher les aperçus des fichiers",
        "Fájl-előnézetek megjelenítése", "Mostra le anteprime dei file", "ファイルのプレビューを表示する", "파일 미리보기 표시",
        "Rodyti failų peržiūras", "Bestandsvoorbeelden weergeven", "Wyświetl podgląd plików",
        "Mostrar visualizações de arquivos", "Afișează previzualizările fișierelor", "Показывать эскизы файлов",
        "Visa filförhandsvisningar", "แสดงตัวอย่างไฟล์", "Dosya önizlemelerini göster",
        "Показати попередній перегляд файлів", "Hiển thị bản xem trước tệp", "显示文件预览", "顯示文件預覽",
    ),
    "Slot": (
        "Ranura", "Slot", "Steckplatz", "Ranura", "Slot", "Emplacement", "Slot", "Fessura", "スロット", "슬롯", "Lizdas",
        "Sleuf", "Gniazdo", "Slot", "Slot", "Слот", "Fack", "สล็อต", "Yuvası", "Слот", "Khe", "槽位", "槽位",
    ),
    "Slot {number}": (
        "Ranura {number}", "Slot {number}", "Steckplatz {number}", "Ranura {number}", "Zirrikitu {number}",
        "Emplacement {number}", "{number} nyílás", "Scanalatura {number}", "スロット {number}", "슬롯 {number}",
        "Lizdas {number}", "Slot {number}", "Gniazdo {number}", "Slot {number}", "Slot {number}", "Слот {number}",
        "Slot {number}", "สล็อต {number}", "Yuva {number}", "Слот {number}", "Khe {number}", "插槽{number}", "插槽{number}",
    ),
    "Slots can be edited while the printer and material station are idle.": (
        "Les ranures es poden editar mentre la impressora i l'estació de material estan inactius.",
        "Sloty lze upravovat, když jsou tiskárna a stanice materiálu nečinné.",
        "Slots können bearbeitet werden, während der Drucker und die Materialstation im Leerlauf sind.",
        "Las ranuras se pueden editar mientras la impresora y la estación de material están inactivas.",
        "Zirrikituak edita daitezke inprimagailua eta material geltokia inaktibo dauden bitartean.",
        "Les emplacements peuvent être modifiés lorsque l'imprimante et la station de matériau sont inactives.",
        "A bővítőhelyek szerkeszthetők, miközben a nyomtató és az anyagállomás tétlen.",
        "Gli slot possono essere modificati mentre la stampante e la stazione del materiale sono inattive.",
        "スロットは、プリンタとマテリアル ステーションがアイドル状態のときに編集できます。", "프린터와 재료 스테이션이 유휴 상태인 동안 슬롯을 편집할 수 있습니다.",
        "Lizdas galima redaguoti, kol spausdintuvas ir medžiagų stotis neveikia.",
        "Slots kunnen worden bewerkt terwijl de printer en het materiaalstation inactief zijn.",
        "Szczeliny można edytować, gdy drukarka i stacja materiałowa są bezczynne.",
        "Os slots podem ser editados enquanto a impressora e a estação de material estão ociosas.",
        "Sloturile pot fi editate în timp ce imprimanta și stația de material sunt inactive.",
        "Слоты можно редактировать, пока принтер и станция подачи материала простаивают.",
        "Fack kan redigeras när skrivaren och materialstationen är inaktiva.",
        "ช่องสามารถแก้ไขได้ในขณะที่เครื่องพิมพ์และสถานีวัสดุไม่ได้ใช้งาน",
        "Yuvalar, yazıcı ve malzeme istasyonu boştayken düzenlenebilir.",
        "Слоти можна редагувати, коли принтер і станція матеріалів неактивні.",
        "Các khe có thể được chỉnh sửa trong khi máy in và trạm nguyên liệu không hoạt động.", "可以在打印机和材料站空闲时编辑槽位。",
        "可以在印表機和材料站空閒時編輯插槽。",
    ),
    "Standard": (
        "Estàndard", "Standardní", "Standard", "Estándar", "Estandarra", "Norme", "Normál", "Standard", "スタンダード",
        "스탠다드", "Standartinis", "Standaard", "Standardowe", "Padrão", "Standard", "Стандартная", "Standard", "มาตรฐาน",
        "Standart", "Стандарт", "Tiêu chuẩn", "标准", "標準",
    ),
    "Start unconfirmed · {file}": (
        "Inici sense confirmar · {file}", "Start nepotvrzený · {file}", "Start unbestätigt · {file}",
        "Inicio sin confirmar · {file}", "Hasi baieztatu gabe · {file}", "Démarrage non confirmé · {file}",
        "Indítás nincs megerősítve · {file}", "Inizio non confermato · {file}", "起動未確認・{file}", "시작 미확인 · {file}",
        "Pradėti nepatvirtinta · {file}", "Start onbevestigd · {file}", "Uruchomienie niepotwierdzone · {file}",
        "Início não confirmado · {file}", "Începe neconfirmat · {file}", "Начало не подтверждено · {file}",
        "Starta obekräftad · {file}", "เริ่มไม่ได้รับการยืนยัน · {file}", "Onaylanmamış başlangıç · {file}",
        "Початок непідтверджено · {file}", "Bắt đầu chưa được xác nhận · {file}", "未确认开始·{file}", "未確認開始·{file}",
    ),
    "Start unconfirmed. Check the printer.": (
        "Inici sense confirmar. Comproveu la impressora.", "Start nepotvrzen. Zkontrolujte tiskárnu.",
        "Start unbestätigt. Überprüfen Sie den Drucker.", "Inicio sin confirmar. Verifique la impresora.",
        "Hasi baieztatu gabe. Egiaztatu inprimagailua.", "Démarrage non confirmé. Vérifiez l'imprimante.",
        "Indítás nincs megerősítve. Ellenőrizze a nyomtatót.", "Inizio non confermato. Controllare la stampante.",
        "起動未確認。プリンターを確認してください。", "시작이 확인되지 않았습니다. 프린터를 확인하세요.", "Pradėti nepatvirtinta. Patikrinkite spausdintuvą.",
        "Start onbevestigd. Controleer de printer.", "Uruchomienie niepotwierdzone. Sprawdź drukarkę.",
        "Início não confirmado. Verifique a impressora.", "Începe neconfirmat. Verificați imprimanta.",
        "Начало не подтверждено. Проверьте принтер.", "Start obekräftad. Kontrollera skrivaren.",
        "เริ่มไม่ได้รับการยืนยัน ตรวจสอบเครื่องพิมพ์", "Onaylanmamış başlat. Yazıcıyı kontrol edin.",
        "Початок непідтверджено. Перевірте принтер.", "Bắt đầu chưa được xác nhận. Kiểm tra máy in.", "未确认开始。检查打印机。",
        "未確認開始。檢查印表機。",
    ),
    "Stored files": (
        "Fitxers emmagatzemats", "Uložené soubory", "Gespeicherte Dateien", "Archivos almacenados",
        "Gordetako fitxategiak", "Fichiers stockés", "Tárolt fájlok", "File memorizzati", "保存ファイル", "저장 파일",
        "Išsaugoti failai", "Opgeslagen bestanden", "Zapisane pliki", "Arquivos armazenados", "Fișiere stocate",
        "Сохраненные файлы", "Lagrade filer", "ไฟล์ที่จัดเก็บ", "Saklanan dosyalar", "Збережені файли",
        "Tệp được lưu trữ", "存储的文件", "儲存的文件",
    ),
    "System theme": (
        "Tema del sistema", "Systémové téma", "Systemthema", "Tema del sistema", "Sistemaren gaia", "Thème système",
        "Rendszertéma", "Tema del sistema", "システムテーマ", "시스템 테마", "Sistemos tema", "Systeemthema", "Motyw systemowy",
        "Tema do sistema", "Tema de sistem", "Системная тема", "Systemtema", "ธีมของระบบ", "Sistem teması",
        "Тема системи", "Chủ đề hệ thống", "系统主题", "系統主題",
    ),
    "Tab placement": (
        "Col·locació de pestanyes", "Umístění tabulátoru", "Tab-Platzierung", "Colocación de pestañas",
        "Fitxen kokapena", "Placement des onglets", "Lap elhelyezése", "Posizionamento delle schede", "タブの配置", "탭 배치",
        "Skirtuko vieta", "Tabbladplaatsing", "Umieszczenie tabulatorów", "Posicionamento das guias",
        "Plasarea filelor", "Вкладка монитора", "Flikplacering", "การจัดวางแท็บ", "Sekme yerleşimi",
        "Розміщення вкладок", "Vị trí tab", "选项卡放置", "選項卡放置",
    ),
    "Target": (
        "Temperatura objectiu", "Cílová teplota", "Solltemperatur", "Temperatura objetivo", "Helburuko tenperatura",
        "Température cible", "Célhőmérséklet", "Temperatura obiettivo", "目標温度", "목표 온도", "Tikslinė temperatūra",
        "Doeltemperatuur", "Temperatura docelowa", "Temperatura alvo", "Temperatură țintă", "Заданная температура",
        "Måltemperatur", "อุณหภูมิเป้าหมาย", "Hedef sıcaklık", "Цільова температура", "Nhiệt độ mục tiêu", "目标温度",
        "目標溫度",
    ),
    "Temperatures": (
        "Temperatures", "Teploty", "Temperaturen", "Temperaturas", "Tenperaturak", "Températures", "Hőmérséklet",
        "Temperature", "気温", "기온", "Temperatūra", "Temperaturen", "Temperatury", "Temperaturas", "Temperaturi",
        "Температура", "Temperaturer", "อุณหภูมิ", "Sıcaklıklar", "Температури", "Nhiệt độ", "温度", "溫度",
    ),
    "The CheckCode must be printable ASCII, up to 32 characters.": (
        "El CheckCode ha de ser ASCII imprimible, amb un màxim de 32 caràcters.",
        "CheckCode musí být tisknutelné ASCII, až 32 znaků.",
        "Das CheckCode muss druckbares ASCII mit bis zu 32 Zeichen sein.",
        "El CheckCode debe ser ASCII imprimible, de hasta 32 caracteres.",
        "CheckCode ASCII inprimagarria izan behar du, 32 karaktere gehienez.",
        "Le CheckCode doit être en ASCII imprimable, comportant jusqu'à 32 caractères.",
        "A CheckCode-nek nyomtatható ASCII-nek kell lennie, legfeljebb 32 karakterből.",
        "CheckCode deve essere ASCII stampabile, fino a 32 caratteri.", "CheckCode は、最大 32 文字の印刷可能な ASCII でなければなりません。",
        "CheckCode는 인쇄 가능한 ASCII여야 하며 최대 32자까지 가능합니다.", "CheckCode turi būti spausdinamas ASCII, iki 32 simbolių.",
        "De CheckCode moet afdrukbaar ASCII zijn, maximaal 32 tekens.",
        "Wartość CheckCode musi być zapisywana w formacie ASCII i mieć maksymalnie 32 znaki.",
        "O CheckCode deve ser ASCII imprimível, com até 32 caracteres.",
        "CheckCode trebuie să fie imprimabil ASCII, până la 32 de caractere.",
        "CheckCode должен содержать до 32 печатных символов ASCII.",
        "CheckCode måste vara utskrivbar ASCII, upp till 32 tecken.",
        "CheckCode ต้องเป็น ASCII ที่สามารถพิมพ์ได้ สูงสุด 32 ตัวอักษร",
        "CheckCode, 32 karaktere kadar yazdırılabilir ASCII olmalıdır.",
        "CheckCode має бути ASCII для друку, до 32 символів.",
        "CheckCode phải là mã ASCII có thể in được, tối đa 32 ký tự.", "CheckCode 必须是可打印的ASCII，最多32 个字符。",
        "CheckCode 必須是可列印的ASCII，最多32 個字元。",
    ),
    "The current job changed before {command} could be confirmed.": (
        "La feina actual ha canviat abans que es pogués confirmar {command}.",
        "Aktuální úloha se změnila před potvrzením {command}.",
        "Der aktuelle Job wurde geändert, bevor {command} bestätigt werden konnte.",
        "El trabajo actual cambió antes de que se pudiera confirmar {command}.",
        "Uneko lana aldatu da {command} berretsi aurretik.",
        "Le travail en cours a été modifié avant que {command} puisse être confirmé.",
        "Az aktuális feladat megváltozott, mielőtt a {command} megerősítést nyert volna.",
        "Il lavoro corrente è stato modificato prima che {command} potesse essere confermato.",
        "{command}を確認する前に現在のジョブが変更されました。", "{command}를 확인하기 전에 현재 작업이 변경되었습니다.",
        "Dabartinis darbas pasikeitė prieš patvirtinant {command}.",
        "De huidige taak is gewijzigd voordat {command} kon worden bevestigd.",
        "Bieżące zadanie zostało zmienione przed potwierdzeniem {command}.",
        "O trabalho atual foi alterado antes que {command} pudesse ser confirmado.",
        "Lucrarea curentă sa schimbat înainte ca {command} să poată fi confirmat.",
        "Текущее задание сменилось до подтверждения команды «{command}».",
        "Det aktuella jobbet ändrades innan {command} kunde bekräftas.",
        "งานปัจจุบันมีการเปลี่ยนแปลงก่อนที่ {command} จะได้รับการยืนยัน",
        "{command} onaylanamadan mevcut iş değiştirildi.",
        "Поточне завдання змінилося до того, як можна було підтвердити {command}.",
        "Công việc hiện tại đã thay đổi trước khi {command} được xác nhận.", "在确认{command} 之前，当前作业已更改。",
        "在確認{command} 之前，目前作業已變更。",
    ),
    "The file is unavailable or cannot be identified on this printer.": (
        "El fitxer no està disponible o no es pot identificar en aquesta impressora.",
        "Soubor je nedostupný nebo jej nelze na této tiskárně identifikovat.",
        "Die Datei ist nicht verfügbar oder kann auf diesem Drucker nicht identifiziert werden.",
        "El archivo no está disponible o no se puede identificar en esta impresora.",
        "Fitxategia ez dago erabilgarri edo ezin da identifikatu inprimagailu honetan.",
        "Le fichier n'est pas disponible ou ne peut pas être identifié sur cette imprimante.",
        "A fájl nem érhető el, vagy nem azonosítható ezen a nyomtatón.",
        "Il file non è disponibile o non può essere identificato su questa stampante.",
        "このプリンターではファイルが利用できないか、認識できません。", "이 프린터에서 파일을 사용할 수 없거나 식별할 수 없습니다.",
        "Failas nepasiekiamas arba jo negalima identifikuoti šiame spausdintuve.",
        "Het bestand is niet beschikbaar of kan niet worden geïdentificeerd op deze printer.",
        "Plik jest niedostępny lub nie można go zidentyfikować na tej drukarce.",
        "O arquivo não está disponível ou não pode ser identificado nesta impressora.",
        "Fișierul nu este disponibil sau nu poate fi identificat pe această imprimantă.",
        "Файл недоступен или его не удалось найти на принтере.",
        "Filen är inte tillgänglig eller kan inte identifieras på den här skrivaren.",
        "ไฟล์ไม่พร้อมใช้งานหรือไม่สามารถระบุได้ในเครื่องพิมพ์นี้",
        "Dosya bu yazıcıda kullanılamıyor veya tanımlanamıyor.",
        "Файл недоступний або не може бути ідентифікований на цьому принтері.",
        "Tệp không khả dụng hoặc không thể xác định được trên máy in này.", "文件不可用或无法在本打印机上识别。", "檔案無法使用或無法在本印表機上辨識。",
    ),
    "The file list changed. Refresh Printer Files.": (
        "La llista de fitxers ha canviat. Actualitza els fitxers de la impressora.",
        "Seznam souborů se změnil. Obnovte soubory tiskárny.",
        "Die Dateiliste hat sich geändert. Druckerdateien aktualisieren.",
        "La lista de archivos cambió. Actualizar archivos de impresora.",
        "Fitxategien zerrenda aldatu da. Freskatu inprimagailuaren fitxategiak.",
        "La liste des fichiers a changé. Actualiser les fichiers de l'imprimante.",
        "A fájllista megváltozott. Nyomtatófájlok frissítése.",
        "L'elenco dei file è cambiato. Aggiorna i file della stampante.", "ファイルリストを変更しました。プリンターファイルを更新します。",
        "파일 목록이 변경되었습니다. 프린터 파일을 새로 고칩니다.", "Pakeistas failų sąrašas. Atnaujinkite spausdintuvo failus.",
        "De bestandslijst is gewijzigd. Vernieuw printerbestanden.",
        "Lista plików uległa zmianie. Odśwież pliki drukarki.",
        "A lista de arquivos foi alterada. Atualize os arquivos da impressora.",
        "Lista de fișiere sa schimbat. Reîmprospătați fișierele imprimantei.",
        "Список файлов изменился. Обновите список файлов принтера.", "Fillistan har ändrats. Uppdatera skrivarfiler.",
        "รายการไฟล์มีการเปลี่ยนแปลง รีเฟรชไฟล์เครื่องพิมพ์", "Dosya listesi değişti. Yazıcı Dosyalarını Yenileyin.",
        "Список файлів змінено. Оновити файли принтера.", "Danh sách tập tin đã thay đổi. Làm mới tập tin máy in.",
        "文件列表已更改。刷新打印机文件。", "文件清單已變更。刷新印表機檔案。",
    ),
    "The file will be saved on the printer without starting a print.": (
        "El fitxer es desarà a la impressora sense iniciar una impressió.",
        "Soubor bude uložen na tiskárně bez zahájení tisku.",
        "Die Datei wird auf dem Drucker gespeichert, ohne dass ein Druckvorgang gestartet wird.",
        "El archivo se guardará en la impresora sin iniciar la impresión.",
        "Fitxategia inprimagailuan gordeko da inprimatzerik hasi gabe.",
        "Le fichier sera enregistré sur l'imprimante sans démarrer d'impression.",
        "A fájl a nyomtatás megkezdése nélkül kerül mentésre a nyomtatóra.",
        "Il file verrà salvato sulla stampante senza avviare la stampa.", "ファイルは印刷を開始せずにプリンターに保存されます。",
        "인쇄를 시작하지 않고도 파일이 프린터에 저장됩니다.", "Failas bus išsaugotas spausdintuve nepradėjus spausdinimo.",
        "Het bestand wordt op de printer opgeslagen zonder een afdruk te starten.",
        "Plik zostanie zapisany na drukarce bez rozpoczynania drukowania.",
        "O arquivo será salvo na impressora sem iniciar a impressão.",
        "Fișierul va fi salvat pe imprimantă fără a începe o imprimare.",
        "Файл будет сохранен на принтере без запуска печати.",
        "Filen kommer att sparas på skrivaren utan att starta en utskrift.",
        "ไฟล์จะถูกบันทึกลงในเครื่องพิมพ์โดยไม่ต้องเริ่มพิมพ์",
        "Dosya, yazdırma işlemi başlatılmadan yazıcıya kaydedilecektir.",
        "Файл буде збережено на принтері без запуску друку.", "Tệp sẽ được lưu trên máy in mà không bắt đầu in.",
        "文件将保存在打印机上，而不开始打印。", "文件將保存在印表機上，且不會開始列印。",
    ),
    "The print job changed. Refresh its status and try again.": (
        "El treball d'impressió ha canviat. Actualitzeu-ne l'estat i torneu-ho a provar.",
        "Tisková úloha se změnila. Obnovte jeho stav a zkuste to znovu.",
        "Der Druckauftrag wurde geändert. Aktualisieren Sie den Status und versuchen Sie es erneut.",
        "El trabajo de impresión cambió. Actualiza su estado e inténtalo de nuevo.",
        "Inprimatzeko lana aldatu da. Freskatu bere egoera eta saiatu berriro.",
        "Le travail d'impression a changé. Actualisez son statut et réessayez.",
        "A nyomtatási feladat megváltozott. Frissítse az állapotát, és próbálja újra.",
        "Il lavoro di stampa è cambiato. Aggiorna il suo stato e riprova.", "印刷ジョブが変更されました。ステータスを更新して再試行してください。",
        "인쇄 작업이 변경되었습니다. 상태를 새로 고치고 다시 시도하세요.",
        "Spausdinimo užduotis pasikeitė. Atnaujinkite jo būseną ir bandykite dar kartą.",
        "De afdruktaak is gewijzigd. Ververs de status en probeer het opnieuw.",
        "Zadanie drukowania zostało zmienione. Odśwież jego status i spróbuj ponownie.",
        "O trabalho de impressão foi alterado. Atualize seu status e tente novamente.",
        "Lucrarea de imprimare sa schimbat. Actualizează-i starea și încearcă din nou.",
        "Задание печати сменилось. Обновите его состояние и повторите попытку.",
        "Utskriftsjobbet har ändrats. Uppdatera dess status och försök igen.",
        "งานพิมพ์มีการเปลี่ยนแปลง รีเฟรชสถานะแล้วลองอีกครั้ง",
        "Yazdırma işi değişti. Durumunu yenileyin ve tekrar deneyin.",
        "Завдання друку змінено. Оновіть його статус і повторіть спробу.",
        "Công việc in đã thay đổi. Hãy làm mới trạng thái của nó và thử lại.", "打印作业已更改。刷新其状态并重试。", "列印作業已變更。刷新其狀態並重試。",
    ),
    "The printer API is unavailable": (
        "L’API de la impressora no està disponible", "Rozhraní API tiskárny není dostupné",
        "Die Drucker-API ist nicht verfügbar", "La API de la impresora no está disponible",
        "Inprimagailuaren APIa ez dago erabilgarri", "L’API de l’imprimante est indisponible",
        "A nyomtató API-ja nem érhető el", "L’API della stampante non è disponibile", "プリンターの API を利用できません",
        "프린터 API를 사용할 수 없습니다", "Spausdintuvo API nepasiekiama", "De printer-API is niet beschikbaar",
        "Interfejs API drukarki jest niedostępny", "A API da impressora está indisponível",
        "API-ul imprimantei nu este disponibil", "API принтера недоступен", "Skrivarens API är inte tillgängligt",
        "ไม่สามารถใช้ API ของเครื่องพิมพ์ได้", "Yazıcı API’si kullanılamıyor", "API принтера недоступний",
        "Không thể truy cập API của máy in", "打印机 API 不可用", "印表機 API 無法使用",
    ),
    "The printer accepted the transfer, but the file and idle state could not both be verified.": (
        "La impressora va acceptar la transferència, però no s'han pogut verificar tant el fitxer com l'estat inactiu.",
        "Tiskárna přijala přenos, ale nebylo možné ověřit soubor i klidový stav.",
        "Der Drucker hat die Übertragung akzeptiert, aber sowohl die Datei als auch der Ruhezustand konnten nicht überprüft werden.",
        "La impresora aceptó la transferencia, pero no se pudieron verificar el archivo y el estado inactivo.",
        "Inprimagailuak transferentzia onartu du, baina ezin izan dira biak egiaztatu fitxategia eta inaktibo egoera.",
        "L'imprimante a accepté le transfert, mais le fichier et l'état d'inactivité n'ont pas pu être vérifiés.",
        "A nyomtató elfogadta az átvitelt, de a fájl és a készenléti állapot nem ellenőrizhető.",
        "La stampante ha accettato il trasferimento, ma non è stato possibile verificare sia il file che lo stato inattivo.",
        "プリンタは転送を受け入れましたが、ファイルとアイドル状態の両方を確認できませんでした。", "프린터가 전송을 승인했지만 파일과 유휴 상태를 모두 확인할 수 없습니다.",
        "Spausdintuvas priėmė perdavimą, tačiau nepavyko patikrinti failo ir neveikimo būsenos.",
        "De printer heeft de overdracht geaccepteerd, maar het bestand en de inactieve status kunnen niet allebei worden geverifieerd.",
        "Drukarka zaakceptowała transfer, ale nie można było zweryfikować pliku i stanu bezczynności.",
        "A impressora aceitou a transferência, mas o arquivo e o estado inativo não puderam ser verificados.",
        "Imprimanta a acceptat transferul, dar fișierul și starea inactivă nu au putut fi verificate.",
        "Принтер принял передачу. Не удалось подтвердить одновременно наличие файла и готовность принтера.",
        "Skrivaren accepterade överföringen, men både fil och viloläge kunde inte verifieras.",
        "เครื่องพิมพ์ยอมรับการถ่ายโอน แต่ทั้งไฟล์และสถานะไม่ได้ใช้งานไม่สามารถตรวจสอบได้",
        "Yazıcı aktarımı kabul etti ancak dosya ve boşta durumu doğrulanamadı.",
        "Принтер прийняв передачу, але не вдалося перевірити файл і стан очікування.",
        "Máy in đã chấp nhận quá trình truyền nhưng không thể xác minh cả tập tin và trạng thái không hoạt động.",
        "打印机接受传输，但无法同时验证文件和空闲状态。", "印表機接受傳輸，但無法同時驗證文件和閒置狀態。",
    ),
    "The printer did not confirm the change.": (
        "La impressora no ha confirmat el canvi.", "Tiskárna nepotvrdila změnu.",
        "Der Drucker hat die Änderung nicht bestätigt.", "La impresora no confirmó el cambio.",
        "Inprimagailuak ez du aldaketa baieztatu.", "L'imprimante n'a pas confirmé la modification.",
        "A nyomtató nem erősítette meg a változtatást.", "La stampante non ha confermato la modifica.",
        "プリンターは変更を確認しませんでした。", "프린터가 변경 사항을 확인하지 못했습니다.", "Spausdintuvas nepatvirtino pakeitimo.",
        "De printer heeft de wijziging niet bevestigd.", "Drukarka nie potwierdziła zmiany.",
        "A impressora não confirmou a alteração.", "Imprimanta nu a confirmat modificarea.",
        "Принтер не подтвердил изменение.", "Skrivaren bekräftade inte ändringen.",
        "เครื่องพิมพ์ไม่ยืนยันการเปลี่ยนแปลง", "Yazıcı değişikliği onaylamadı.", "Принтер не підтвердив зміни.",
        "Máy in không xác nhận thay đổi.", "打印机未确认更改。", "印表機未確認變更。",
    ),
    "The printer did not respond": (
        "La impressora no va respondre", "Tiskárna neodpovídá", "Der Drucker hat nicht reagiert",
        "La impresora no respondió", "Inprimagailuak ez du erantzun", "L'imprimante n'a pas répondu",
        "A nyomtató nem válaszolt", "La stampante non ha risposto", "プリンターが応答しませんでした", "프린터가 응답하지 않습니다",
        "Spausdintuvas neatsakė", "De printer reageerde niet", "Drukarka nie odpowiedziała",
        "A impressora não respondeu", "Imprimanta nu a răspuns", "Принтер не ответил.", "Skrivaren svarade inte",
        "เครื่องพิมพ์ไม่ตอบสนอง", "Yazıcı yanıt vermedi", "Принтер не відповідає", "Máy in không phản hồi", "打印机没有响应",
        "印表機沒有回應",
    ),
    "The printer is not connected yet.": (
        "La impressora encara no està connectada.", "Tiskárna ještě není připojena.",
        "Der Drucker ist noch nicht angeschlossen.", "La impresora aún no está conectada.",
        "Inprimagailua ez dago oraindik konektatuta.", "L'imprimante n'est pas encore connectée.",
        "A nyomtató még nincs csatlakoztatva.", "La stampante non è ancora collegata.", "プリンターがまだ接続されていません。",
        "프린터가 아직 연결되지 않았습니다.", "Spausdintuvas dar neprijungtas.", "De printer is nog niet aangesloten.",
        "Drukarka nie jest jeszcze podłączona.", "A impressora ainda não está conectada.",
        "Imprimanta nu este încă conectată.", "Принтер еще не подключен.", "Skrivaren är inte ansluten ännu.",
        "ยังไม่ได้เชื่อมต่อเครื่องพิมพ์", "Yazıcı henüz bağlanmadı.", "Принтер ще не підключено.",
        "Máy in chưa được kết nối.", "打印机尚未连接。", "印表機尚未連線。",
    ),
    "The printer must be idle and free of errors to upload a file.": (
        "La impressora ha d'estar inactiva i sense errors per carregar un fitxer.",
        "Aby bylo možné nahrát soubor, tiskárna musí být nečinná a bez chyb.",
        "Der Drucker muss inaktiv und fehlerfrei sein, um eine Datei hochladen zu können.",
        "La impresora debe estar inactiva y sin errores para cargar un archivo.",
        "Inprimagailuak inaktibo egon behar du eta akatsik gabe egon behar du fitxategi bat kargatzeko.",
        "L'imprimante doit être inactive et exempte d'erreurs pour télécharger un fichier.",
        "A nyomtatónak tétlennek és hibamentesnek kell lennie a fájl feltöltéséhez.",
        "La stampante deve essere inattiva e priva di errori per caricare un file.",
        "ファイルをアップロードするには、プリンタがアイドル状態であり、エラーが発生していない必要があります。", "파일을 업로드하려면 프린터가 유휴 상태이고 오류가 없어야 합니다.",
        "Kad būtų galima įkelti failą, spausdintuvas turi būti neaktyvus ir be klaidų.",
        "De printer moet inactief zijn en geen fouten bevatten om een ​​bestand te kunnen uploaden.",
        "Aby można było przesłać plik, drukarka musi być bezczynna i wolna od błędów.",
        "A impressora deve estar ociosa e livre de erros para carregar um arquivo.",
        "Imprimanta trebuie să fie inactivă și să nu aibă erori pentru a încărca un fișier.",
        "Для загрузки файла принтер должен быть свободен, без активных ошибок.",
        "Skrivaren måste vara inaktiv och fri från fel för att ladda upp en fil.",
        "เครื่องพิมพ์จะต้องไม่มีการใช้งานและไม่มีข้อผิดพลาดในการอัปโหลดไฟล์",
        "Dosya yüklemek için yazıcının boşta ve hatasız olması gerekir.",
        "Для завантаження файлу принтер має бути неактивним і без помилок.",
        "Máy in phải ở trạng thái rảnh và không có lỗi để tải tệp lên.", "打印机必须空闲且无错误才能上传文件。", "印表機必須閒置且無錯誤才能上傳文件。",
    ),
    "The printer must be idle to upload a file.": (
        "La impressora ha d'estar inactiva per carregar un fitxer.",
        "Aby bylo možné nahrát soubor, tiskárna musí být nečinná.",
        "Der Drucker muss im Leerlauf sein, um eine Datei hochladen zu können.",
        "La impresora debe estar inactiva para cargar un archivo.",
        "Inprimagailuak inaktibo egon behar du fitxategi bat kargatzeko.",
        "L'imprimante doit être inactive pour télécharger un fichier.",
        "A nyomtatónak készenlétben kell lennie a fájl feltöltéséhez.",
        "La stampante deve essere inattiva per caricare un file.", "ファイルをアップロードするには、プリンタがアイドル状態である必要があります。",
        "파일을 업로드하려면 프린터가 유휴 상태여야 합니다.", "Spausdintuvas turi būti neaktyvus, kad galėtų įkelti failą.",
        "De printer moet inactief zijn om een ​​bestand te kunnen uploaden.",
        "Aby można było przesłać plik, drukarka musi być bezczynna.",
        "A impressora deve estar ociosa para carregar um arquivo.",
        "Imprimanta trebuie să fie inactivă pentru a încărca un fișier.",
        "Для загрузки файла принтер должен быть свободен.", "Skrivaren måste vara inaktiv för att ladda upp en fil.",
        "เครื่องพิมพ์จะต้องไม่ได้ใช้งานจึงจะอัพโหลดไฟล์ได้",
        "Bir dosyayı karşıya yüklemek için yazıcının boşta olması gerekir.",
        "Для завантаження файлу принтер має бути неактивним.", "Máy in phải ở chế độ chờ để tải tệp lên.",
        "打印机必须处于空闲状态才能上传文件。", "印表機必須處於閒置狀態才能上傳檔案。",
    ),
    "The printer must be idle.": (
        "La impressora ha d'estar inactiva.", "Tiskárna musí být nečinná.", "Der Drucker muss im Leerlauf sein.",
        "La impresora debe estar inactiva.", "Inprimagailuak inaktibo egon behar du.",
        "L'imprimante doit être inactive.", "A nyomtatónak tétlennek kell lennie.",
        "La stampante deve essere inattiva.", "プリンターはアイドル状態である必要があります。", "프린터가 유휴 상태여야 합니다.",
        "Spausdintuvas turi neveikti.", "De printer moet inactief zijn.", "Drukarka musi być bezczynna.",
        "A impressora deve estar ociosa.", "Imprimanta trebuie să fie inactivă.", "Принтер должен быть свободен.",
        "Skrivaren måste vara inaktiv.", "เครื่องพิมพ์จะต้องไม่ได้ใช้งาน", "Yazıcı boşta olmalıdır.",
        "Принтер має бути неактивним.", "Máy in phải ở chế độ chờ.", "打印机必须处于空闲状态。", "印表機必須處於閒置狀態。",
    ),
    "The printer reported an active print. Check the printer before retrying.": (
        "La impressora ha informat d'una impressió activa. Comproveu la impressora abans de tornar-ho a provar.",
        "Tiskárna oznámila aktivní tisk. Před dalším pokusem zkontrolujte tiskárnu.",
        "Der Drucker hat einen aktiven Druck gemeldet. Überprüfen Sie den Drucker, bevor Sie es erneut versuchen.",
        "La impresora informó una impresión activa. Verifique la impresora antes de volver a intentarlo.",
        "Inprimagailuak inprimaketa aktibo baten berri eman du. Egiaztatu inprimagailua berriro saiatu aurretik.",
        "L'imprimante a signalé une impression active. Vérifiez l'imprimante avant de réessayer.",
        "A nyomtató aktív nyomtatást jelentett. Mielőtt újra próbálkozna, ellenőrizze a nyomtatót.",
        "La stampante ha segnalato una stampa attiva. Controllare la stampante prima di riprovare.",
        "プリンターはアクティブな印刷を報告しました。再試行する前にプリンターを確認してください。", "프린터에서 활성 인쇄를 보고했습니다. 다시 시도하기 전에 프린터를 확인하십시오.",
        "Spausdintuvas pranešė apie aktyvų spausdinimą. Prieš bandydami dar kartą patikrinkite spausdintuvą.",
        "De printer meldde een actieve afdruk. Controleer de printer voordat u het opnieuw probeert.",
        "Drukarka zgłosiła aktywny wydruk. Sprawdź drukarkę przed ponowną próbą.",
        "A impressora relatou uma impressão ativa. Verifique a impressora antes de tentar novamente.",
        "Imprimanta a raportat o imprimare activă. Verificați imprimanta înainte de a încerca din nou.",
        "Принтер сообщил об активной печати. Прежде чем повторить попытку, проверьте принтер.",
        "Skrivaren rapporterade en aktiv utskrift. Kontrollera skrivaren innan du försöker igen.",
        "เครื่องพิมพ์รายงานว่ามีงานพิมพ์อยู่ ตรวจสอบเครื่องพิมพ์ก่อนลองอีกครั้ง",
        "Yazıcı etkin bir yazdırma bildirdi. Yeniden denemeden önce yazıcıyı kontrol edin.",
        "Принтер повідомив про активний друк. Перевірте принтер перед повторною спробою.",
        "Máy in báo cáo bản in đang hoạt động. Kiểm tra máy in trước khi thử lại.", "打印机报告正在进行打印。重试之前请检查打印机。",
        "印表機報告正在進行列印。重試前請檢查印表機。",
    ),
    "The printer requires homing before manual movement.": (
        "Torna els eixos a l’origen abans de moure’ls manualment.", "Před ručním pohybem vraťte osy do výchozí polohy.",
        "Führen Sie vor manuellen Bewegungen eine Referenzfahrt durch.",
        "Lleva los ejes al origen antes de moverlos manualmente.",
        "Eraman ardatzak hasierako posiziora eskuz mugitu aurretik.",
        "Faites la prise d’origine avant de déplacer les axes manuellement.",
        "Kézi mozgatás előtt állítsd alaphelyzetbe a tengelyeket.", "Azzera gli assi prima di spostarli manualmente.",
        "手動で軸を移動する前に原点復帰が必要です。", "축을 수동으로 이동하기 전에 원점 복귀를 실행하세요.",
        "Prieš judindami ašis rankiniu būdu nustatykite pradinę padėtį.",
        "Ga naar het nulpunt voordat je de assen handmatig beweegt.", "Przed ręcznym ruchem wykonaj bazowanie osi.",
        "Volte os eixos à origem antes de movê-los manualmente.", "Du axele la origine înainte de a le deplasa manual.",
        "Перед ручным перемещением верните оси в ноль.", "Kör axlarna till nolläget innan du förflyttar dem manuellt.",
        "ให้แกนกลับจุดเริ่มต้นก่อนขยับด้วยตนเอง", "Eksenleri elle hareket ettirmeden önce başlangıç konumuna döndürün.",
        "Перед ручним переміщенням поверніть осі в нуль.", "Đưa các trục về gốc trước khi di chuyển thủ công.",
        "手动移动各轴前，请先回零。", "手動移動各軸前，請先回原點。",
    ),
    "Theme": (
        "Tema", "Téma", "Thema", "Tema", "Gaia", "Thème", "Téma", "Tema", "テーマ", "테마", "Tema", "Thema", "Motyw", "Tema",
        "Tema", "Тема", "Tema", "ธีม", "Tema", "Тема", "Chủ đề", "主题", "主題",
    ),
    "This file requires multiple filaments.": (
        "Aquest fitxer requereix diversos filaments.", "Tento soubor vyžaduje více vláken.",
        "Diese Datei erfordert mehrere Filamente.", "Este archivo requiere múltiples filamentos.",
        "Fitxategi honek hainbat harizpi behar ditu.", "Ce fichier nécessite plusieurs filaments.",
        "Ez a fájl több szálat igényel.", "Questo file richiede più filamenti.", "このファイルには複数のフィラメントが必要です。",
        "이 파일에는 여러 개의 필라멘트가 필요합니다.", "Šiam failui reikia kelių gijų.",
        "Voor dit bestand zijn meerdere filamenten nodig.", "Ten plik wymaga wielu włókien.",
        "Este arquivo requer múltiplos filamentos.", "Acest fișier necesită mai multe filamente.",
        "Для этого файла требуется несколько материалов.", "Denna fil kräver flera filament.",
        "ไฟล์นี้ต้องใช้หลายเส้นใย", "Bu dosya birden fazla filament gerektirir.", "Цей файл вимагає кількох ниток.",
        "Tập tin này yêu cầu nhiều sợi.", "该文件需要多个灯丝。", "該文件需要多個燈絲。",
    ),
    "This printer has no camera.": (
        "Aquesta impressora no té càmera.", "Tato tiskárna nemá fotoaparát.",
        "Dieser Drucker verfügt über keine Kamera.", "Esta impresora no tiene cámara.",
        "Inprimagailu honek ez du kamerarik.", "Cette imprimante n'a pas d'appareil photo.",
        "Ebben a nyomtatóban nincs kamera.", "Questa stampante non è dotata di fotocamera.", "このプリンタにはカメラがありません。",
        "이 프린터에는 카메라가 없습니다.", "Šiame spausdintuve nėra fotoaparato.", "Deze printer heeft geen camera.",
        "Ta drukarka nie jest wyposażona w aparat.", "Esta impressora não possui câmera.",
        "Această imprimantă nu are cameră.", "В этом принтере нет камеры.", "Den här skrivaren har ingen kamera.",
        "เครื่องพิมพ์นี้ไม่มีกล้อง", "Bu yazıcının kamerası yoktur.", "Цей принтер не має камери.",
        "Máy in này không có camera.", "本打印机没有摄像头。", "本印表機沒有攝影機。",
    ),
    "This printer is not configured yet.": (
        "Aquesta impressora encara no està configurada.", "Tato tiskárna ještě není nakonfigurována.",
        "Dieser Drucker ist noch nicht konfiguriert.", "Esta impresora aún no está configurada.",
        "Inprimagailu hau ez dago oraindik konfiguratuta.", "Cette imprimante n'est pas encore configurée.",
        "Ez a nyomtató még nincs konfigurálva.", "Questa stampante non è ancora configurata.", "このプリンターはまだ設定されていません。",
        "이 프린터는 아직 구성되지 않았습니다.", "Šis spausdintuvas dar nesukonfigūruotas.", "Deze printer is nog niet geconfigureerd.",
        "Ta drukarka nie jest jeszcze skonfigurowana.", "Esta impressora ainda não está configurada.",
        "Această imprimantă nu este încă configurată.", "Этот принтер еще не настроен.",
        "Den här skrivaren är inte konfigurerad ännu.", "เครื่องพิมพ์นี้ยังไม่ได้กำหนดค่า",
        "Bu yazıcı henüz yapılandırılmadı.", "Цей принтер ще не налаштовано.", "Máy in này chưa được cấu hình.",
        "该打印机尚未配置。", "該印表機尚未配置。",
    ),
    "This step exceeds the axis travel limit": (
        "Aquest pas supera el límit de recorregut de l'eix", "Tento krok překračuje limit pohybu osy",
        "Dieser Schritt überschreitet die Achsenwegbegrenzung", "Este paso excede el límite de recorrido del eje.",
        "Urrats honek ardatzaren ibilbide-muga gainditzen du", "Ce pas dépasse la limite de déplacement de l'axe",
        "Ez a lépés meghaladja a tengely mozgási határértékét", "Questo passo supera il limite di corsa dell'asse",
        "このステップは軸の移動制限を超えています", "이 단계는 축 이동 제한을 초과합니다.", "Šis žingsnis viršija ašies eigos ribą",
        "Deze stap overschrijdt de asbewegingslimiet", "Ten krok przekracza limit ruchu osi",
        "Esta etapa excede o limite de deslocamento do eixo", "Acest pas depășește limita de deplasare a axei",
        "Этот шаг выходит за пределы хода оси", "Detta steg överskrider axelns rörelsegräns",
        "ขั้นตอนนี้เกินขีดจำกัดการเคลื่อนที่ของแกน", "Bu adım eksen hareket sınırını aşıyor",
        "Цей крок перевищує обмеження ходу осі", "Bước này vượt quá giới hạn hành trình trục", "该步超出轴行程极限", "該步超出軸行程極限",
    ),
    "Timer off": (
        "Temporitzador desactivat", "Časovač vypnut", "Timer aus", "Temporizador desactivado",
        "Tenporizadorea itzalita", "Minuterie désactivée", "Időzítő kikapcsolva", "Timer disattivato", "タイマーを無効化",
        "타이머 끄기", "Laikmatis išjungtas", "Timer uit", "Minutnik wyłączony", "Temporizador desligado",
        "Temporizator oprit", "Таймер выключен", "Timer av", "ปิดตัวตั้งเวลา", "Zamanlayıcı kapalı", "Таймер вимкнено",
        "Tắt hẹn giờ", "定时关机已关闭", "已關閉定時關機",
    ),
    "Timer {minutes} min": (
        "Temporitzador {minutes} min", "Časovač {minutes} min", "Timer {minutes} min", "Temporizador {minutes} mín.",
        "Tenporizadore {minutes} min", "Minuterie {minutes} min.", "Időzítő {minutes} min",
        "Temporizzatore {minutes} min", "タイマー {minutes} 分", "타이머 {minutes} 분", "Laikmatis {minutes} min",
        "Timer {minutes} min", "Zegar {minutes} min", "Temporizador {minutes} min", "Temporizator {minutes} min",
        "Таймер {minutes} мин.", "Timer {minutes} min", "ตัวจับเวลา {minutes} นาที", "Zamanlayıcı {minutes} dk.",
        "Таймер {minutes} хв", "Hẹn giờ {minutes} phút", "定时器{minutes}分钟", "定時器{minutes}分鐘",
    ),
    "Travel limit": (
        "Límit de recorregut de l’eix", "Limit pojezdu osy", "Verfahrgrenze der Achse", "Límite de recorrido del eje",
        "Ardatzaren ibilbide-muga", "Limite de course de l’axe", "Tengely mozgáshatára", "Limite di corsa dell’asse",
        "軸の移動限界", "축 이동 한계", "Ašies eigos riba", "Bewegingslimiet van de as", "Granica zakresu ruchu osi",
        "Limite de curso do eixo", "Limita cursei axei", "Предел хода оси", "Axelns rörelsegräns",
        "ขีดจำกัดการเคลื่อนที่ของแกน", "Eksen hareket sınırı", "Межа ходу осі", "Giới hạn hành trình của trục", "轴行程限制",
        "軸行程限制",
    ),
    "Unavailable": (
        "No disponible", "Nedostupné", "Nicht verfügbar", "No disponible", "Ez dago erabilgarri", "Indisponible",
        "Nem elérhető", "Non disponibile", "なし", "이용 불가", "Nepasiekiama", "Niet beschikbaar", "Niedostępne",
        "Indisponível", "Indisponibil", "Недоступно", "Ej tillgänglig", "ไม่สามารถใช้งานได้", "Kullanılamaz",
        "Недоступний", "Không có sẵn", "不可用", "不可用",
    ),
    "Unknown": (
        "Desconegut", "Neznámý", "Unbekannt", "Desconocido", "Ezezaguna", "Inconnu", "Ismeretlen", "Sconosciuto", "不明",
        "알 수 없음", "Nežinoma", "Onbekend", "Nieznane", "Desconhecido", "Necunoscut", "Неизвестно", "Okänd", "ไม่ทราบ",
        "Bilinmiyor", "Невідомо", "Không rõ", "未知", "未知",
    ),
    "Unsupported nozzle target.": (
        "Objectiu de broquet no compatible.", "Nepodporovaný cíl trysky.", "Nicht unterstütztes Düsenziel.",
        "Objetivo de boquilla no compatible.", "Onartu gabeko toberaren helburua.",
        "Cible de buse non prise en charge.", "Nem támogatott fúvókacél.", "Target ugello non supportato.",
        "サポートされていないノズル ターゲットです。", "지원되지 않는 노즐 대상입니다.", "Nepalaikomas purkštuko taikinys.",
        "Niet-ondersteund spuitdopdoel.", "Nieobsługiwany cel dyszy.", "Alvo do bico não suportado.",
        "Ținta duzei neacceptată.", "Эта температура сопла не поддерживается.", "Munstycksmål som inte stöds.",
        "เป้าหมายหัวฉีดที่ไม่รองรับ", "Desteklenmeyen püskürtme ucu hedefi.", "Непідтримувана ціль сопла.",
        "Mục tiêu vòi phun không được hỗ trợ.", "喷嘴目标不受支持。", "噴嘴目標不受支援。",
    ),
    "Up": (
        "Amunt", "Nahoru", "Oben", "Arriba", "Gora", "Plus", "Fel", "Su", "上", "업", "Aukštyn", "Omhoog", "W górę",
        "Acima", "Sus", "Вверх", "Upp", "ขึ้น", "Yukarı", "Вгору", "Lên", "向上", "向上",
    ),
    "Upload": (
        "Carrega", "Nahrát", "Hochladen", "Subir", "Kargatu", "Télécharger", "Feltöltés", "Carica", "アップロード", "업로드",
        "Įkelti", "Uploaden", "Prześlij", "Carregar", "Încărcare", "Загрузить", "Ladda upp", "อัพโหลด", "Yükle",
        "Завантажити", "Tải lên", "上传", "上傳",
    ),
    "Upload file": (
        "Carrega el fitxer", "Nahrát soubor", "Datei hochladen", "Subir archivo", "Kargatu fitxategia",
        "Télécharger le fichier", "Fájl feltöltése", "Carica file", "ファイルをアップロードする", "파일 업로드", "Įkelti failą",
        "Bestand uploaden", "Prześlij plik", "Carregar arquivo", "Încărcați fișierul", "Загрузить файл",
        "Ladda upp fil", "อัพโหลดไฟล์", "Dosya yükle", "Завантажити файл", "Tải tập tin lên", "上传文件", "上傳文件",
    ),
    "Upload file…": (
        "Carrega el fitxer...", "Nahrát soubor…", "Datei hochladen…", "Subir archivo…", "Kargatu fitxategia…",
        "Télécharger le fichier…", "Fájl feltöltése…", "Carica file...", "ファイルをアップロード…", "파일 업로드…", "Įkelti failą…",
        "Bestand uploaden…", "Prześlij plik…", "Carregar arquivo…", "Încărcați fișier...", "Загрузить файл…",
        "Ladda upp fil...", "อัพโหลดไฟล์...", "Dosya yükle…", "Завантажити файл…", "Tải tập tin lên…", "上传文件...",
        "上傳檔案...",
    ),
    "Use linked materials": (
        "Utilitzeu materials enllaçats", "Používejte propojené materiály", "Verknüpfte Materialien verwenden",
        "Usar materiales vinculados", "Lotutako materialak erabili", "Utiliser des matériaux liés",
        "Használjon linkelt anyagokat", "Utilizza i materiali collegati", "リンクされた素材を利用する", "링크자료 활용",
        "Naudokite susietas medžiagas", "Gebruik gekoppelde materialen", "Korzystaj z połączonych materiałów",
        "Use materiais vinculados", "Utilizați materiale legate", "Использовать сохранённое сопоставление материалов",
        "Använd länkat material", "ใช้สื่อที่เชื่อมโยงกัน", "Bağlantılı materyalleri kullanın",
        "Використовуйте матеріали за посиланнями", "Sử dụng tài liệu được liên kết", "使用链接材料", "使用連結材料",
    ),
    "Wait for the axes to stop before moving filament.": (
        "Espereu que els eixos s'aturin abans de moure el filament.",
        "Před posunutím vlákna počkejte, až se osy zastaví.",
        "Warten Sie, bis die Achsen angehalten haben, bevor Sie das Filament bewegen.",
        "Espere a que los ejes se detengan antes de mover el filamento.",
        "Itxaron ardatzak gelditu arte harizpia mugitu aurretik.",
        "Attendez l'arrêt des axes avant de déplacer le filament.",
        "Az izzószál mozgatása előtt várja meg, amíg a tengelyek megállnak.",
        "Attendere che gli assi si fermino prima di spostare il filamento.", "フィラメントを移動する前に、軸が停止するのを待ってください。",
        "필라멘트를 이동하기 전에 축이 멈출 때까지 기다리십시오.", "Palaukite, kol ašys sustos, prieš perkeldami kaitinimo siūlą.",
        "Wacht tot de assen stoppen voordat u het filament verplaatst.",
        "Przed poruszeniem żarnika poczekaj, aż osie się zatrzymają.",
        "Espere que os eixos parem antes de mover o filamento.",
        "Așteptați ca axele să se oprească înainte de a muta filamentul.",
        "Перед подачей или втягиванием филамента дождитесь остановки осей.",
        "Vänta tills axlarna stannar innan du flyttar glödtråden.", "รอให้แกนหยุดก่อนที่จะย้ายเส้นใย",
        "Filamenti hareket ettirmeden önce eksenlerin durmasını bekleyin.",
        "Зачекайте, поки осі зупиняться, перш ніж рухати нитку.", "Đợi trục dừng lại trước khi di chuyển dây tóc.",
        "等待轴停止后再移动灯丝。", "等待軸停止後再移動燈絲。",
    ),
    "Wait until OrcaSlicer finishes sending the print job.": (
        "Espereu fins que OrcaSlicer acabi d'enviar el treball d'impressió.",
        "Počkejte, dokud OrcaSlicer nedokončí odesílání tiskové úlohy.",
        "Warten Sie, bis OrcaSlicer das Senden des Druckauftrags abgeschlossen hat.",
        "Espere hasta que OrcaSlicer termine de enviar el trabajo de impresión.",
        "Itxaron OrcaSlicer inprimatze-lana bidaltzen amaitu arte.",
        "Attendez que OrcaSlicer ait fini d'envoyer le travail d'impression.",
        "Várjon, amíg a OrcaSlicer befejezi a nyomtatási feladat elküldését.",
        "Attendere fino a quando OrcaSlicer termina l'invio del lavoro di stampa.", "OrcaSlicer が印刷ジョブの送信を完了するまで待ちます。",
        "OrcaSlicer가 인쇄 작업 전송을 마칠 때까지 기다리십시오.", "Palaukite, kol OrcaSlicer baigs siųsti spausdinimo užduotį.",
        "Wacht totdat OrcaSlicer klaar is met het verzenden van de afdruktaak.",
        "Poczekaj, aż OrcaSlicer zakończy wysyłanie zadania drukowania.",
        "Aguarde até que OrcaSlicer termine de enviar o trabalho de impressão.",
        "Așteptați până când OrcaSlicer termină trimiterea lucrării de imprimare.",
        "Подождите, пока OrcaSlicer завершит отправку задания на печать.",
        "Vänta tills OrcaSlicer har skickat utskriftsjobbet klart.", "รอจนกระทั่ง OrcaSlicer ส่งงานพิมพ์เสร็จสิ้น",
        "OrcaSlicer yazdırma işini göndermeyi bitirene kadar bekleyin.",
        "Зачекайте, поки OrcaSlicer завершить надсилання завдання друку.",
        "Đợi cho đến khi OrcaSlicer gửi xong lệnh in.", "等待OrcaSlicer 完成发送打印作业。", "等待OrcaSlicer 完成傳送列印作業。",
    ),
    "Waiting for Home to finish…": (
        "Esperant que els eixos arribin a l’origen…", "Čeká se na nastavení výchozí polohy…", "Referenzfahrt läuft…",
        "Esperando que los ejes lleguen al origen…", "Ardatzak hasierako posiziora iritsi zain…",
        "Prise d’origine en cours…", "Várakozás az alaphelyzetbe állításra…", "Azzeramento degli assi in corso…",
        "原点復帰の完了を待っています…", "원점 복귀가 완료되기를 기다리는 중…", "Laukiama, kol bus nustatyta pradinė padėtis…",
        "Wachten tot de assen het nulpunt bereiken…", "Trwa bazowanie osi…", "Aguardando os eixos chegarem à origem…",
        "Se așteaptă revenirea axelor la origine…", "Ожидание возврата осей в ноль…",
        "Väntar på att axlarna når nolläget…", "กำลังรอให้แกนกลับถึงจุดเริ่มต้น…",
        "Eksenlerin başlangıç konumuna dönmesi bekleniyor…", "Очікування завершення обнулення осей…",
        "Đang chờ các trục về gốc tọa độ…", "正在等待回零完成…", "正在等待回原點完成…",
    ),
    "XY movement. Arrow keys move 1 mm, Shift moves 10 mm.": (
        "moviment XY. Les tecles de fletxa es mouen 1 mm, Majúscules es mouen 10 mm.",
        "XY pohyb. Klávesy se šipkami se posunou o 1 mm, Shift o 10 mm.",
        "XY-Bewegung. Die Pfeiltasten bewegen sich um 1 mm, die Umschalttaste bewegt sich um 10 mm.",
        "movimiento XY. Las teclas de flecha se mueven 1 mm, Shift se mueve 10 mm.",
        "XY mugimendua. Gezi-teklak 1 mm mugitzen ditu, Maius 10 mm.",
        "Mouvement XY. Les touches fléchées se déplacent de 1 mm, Maj se déplace de 10 mm.",
        "XY mozgás. A nyílbillentyűk 1 mm-rel, a Shift 10 mm-rel mozognak.",
        "Movimento XY. I tasti freccia si spostano di 1 mm, Maiusc si sposta di 10 mm.",
        "XY移動。矢印キーは 1 mm 移動し、Shift キーは 10 mm 移動します。", "XY 이동. 화살표 키는 1mm 이동하고 Shift 키는 10mm 이동합니다.",
        "XY judesys. Rodyklių klavišai juda 1 mm, Shift – 10 mm.",
        "XY-beweging. Pijltjestoetsen bewegen 1 mm, Shift beweegt 10 mm.",
        "Ruch XY. Klawisze strzałek przesuwają się o 1 mm, Shift przesuwa się o 10 mm.",
        "Movimento XY. As teclas de seta movem 1 mm, Shift move 10 mm.",
        "Mișcare XY. Tastele săgeți se mișcă cu 1 mm, Shift se mișcă cu 10 mm.",
        "Перемещение по X и Y. Стрелки перемещают на 1 мм, Shift со стрелкой — на 10 мм.",
        "XY-rörelse. Piltangenterna flyttas 1 mm, Shift flyttas 10 mm.",
        "การเคลื่อนไหว XY ปุ่มลูกศรเลื่อน 1 มม. Shift เลื่อน 10 มม.",
        "XY hareketi. Ok tuşları 1 mm hareket eder, Shift 10 mm hareket eder.",
        "Рух XY. Клавіші зі стрілками переміщуються на 1 мм, Shift – на 10 мм.",
        "chuyển động XY. Phím mũi tên di chuyển 1 mm, Shift di chuyển 10 mm.", "XY运动。箭头键移动 1 毫米，Shift 移动 10 毫米。",
        "XY運動。箭頭鍵移動 1 毫米，Shift 移動 10 毫米。",
    ),
    "{axis} move exceeds the axis travel limits ({minimum} to {maximum} mm).": (
        "El moviment {axis} supera els límits de recorregut de l'eix ({minimum} a {maximum} mm).",
        "Pohyb {axis} překračuje limity pohybu osy ({minimum} až {maximum} mm).",
        "Die {axis}-Bewegung überschreitet die Achsenweggrenzen ({minimum} bis {maximum} mm).",
        "El movimiento de {axis} excede los límites de recorrido del eje ({minimum} a {maximum} mm).",
        "{axis} mugimenduak ardatzaren bidaia-mugak gainditzen ditu ({minimum}-tik {maximum} mm-ra).",
        "Le déplacement du {axis} dépasse les limites de déplacement de l'axe (de {minimum} à {maximum} mm).",
        "A {axis} mozgás meghaladja a tengely mozgási határait ({minimum}-től {maximum} mm-ig).",
        "Lo spostamento di {axis} supera i limiti di corsa dell'asse (da {minimum} a {maximum} mm).",
        "{axis}移動が軸移動限界({minimum}～{maximum}mm)を超えています。", "{axis} 이동이 축 이동 제한({minimum} ~ {maximum} mm)을 초과합니다.",
        "{axis} judėjimas viršija ašies eigos ribas (nuo {minimum} iki {maximum} mm).",
        "{axis}-beweging overschrijdt de asbewegingslimieten ({minimum} tot {maximum} mm).",
        "Ruch {axis} przekracza ograniczenia ruchu osi ({minimum} do {maximum} mm).",
        "O movimento {axis} excede os limites de deslocamento do eixo ({minimum} a {maximum} mm).",
        "Mișcarea {axis} depășește limitele de deplasare a axei ({minimum} până la {maximum} mm).",
        "Перемещение по оси {axis} выходит за допустимые пределы (от {minimum} до {maximum} мм).",
        "{axis}-rörelsen överskrider axelns rörelsegränser ({minimum} till {maximum} mm).",
        "การเคลื่อนของ {axis} เกินขีดจำกัดการเคลื่อนที่ของแกน ({minimum} ถึง {maximum} mm)",
        "{axis} hareketi eksen hareket sınırlarını aşıyor ({minimum} ila {maximum} mm).",
        "Переміщення {axis} перевищує межі ходу осі ({minimum} до {maximum} мм).",
        "{axis} di chuyển vượt quá giới hạn hành trình trục ({minimum} đến {maximum} mm).",
        "{axis} 移动超出轴行程限制（{minimum} 至{maximum} 毫米）。", "{axis} 移動超出軸行程限制（{minimum} 至{maximum} mm）。",
    ),
}

# Other locales use the English key for these less common messages.
EXTENDED_MESSAGES = {
    "Taller print job card": (
        "Höhere Druckauftragskarte", "Tarjeta de trabajo de impresión más alta",
        "Carte de tâche d’impression plus haute", "Scheda del lavoro di stampa più alta", "印刷ジョブカードを高くする",
        "인쇄 작업 카드 높이 늘리기", "Wyższa karta zadania druku", "Cartão de impressão mais alto", "Высокая карточка задания",
        "Вища картка завдання друку", "加高打印任务卡片", "加高列印工作卡片",
    ),
    " · Total print time: {time}": (
        " · Gesamtdruckzeit: {time}", " · Tiempo total de impresión: {time}", " · Temps d'impression total : {time}",
        " · Tempo di stampa totale: {time}", " ・総印刷時間：{time}", " · 총 인쇄 시간: {time}",
        " · Całkowity czas drukowania: {time}", " · Tempo total de impressão: {time}", " · Общее время печати: {time}",
        " · Загальний час друку: {time}", " ·总打印时间：{time}", " ·總列印時間：{time}",
    ),
    "0 min": (
        "0 Min", "0 minutos", "0 min", "0 min", "0分", "0분", "0 min", "0 minutos", "0 мин.", "0 хв", "0分钟", "0分鐘",
    ),
    "1 mm": (
        "1 mm", "1mm", "1 mm", "1mm", "1mm", "1mm", "1 mm", "1 mm", "1 мм", "1 мм", "1毫米", "1毫米",
    ),
    "10 mm": (
        "10 mm", "10mm", "10 millimètres", "10mm", "10mm", "10mm", "10 mm", "10mm", "10 мм", "10 мм", "10毫米", "10毫米",
    ),
    "10 mm per move": (
        "10 mm pro Zug", "10 mm por movimiento", "10 mm par mouvement", "10 mm per mossa", "1移動あたり10mm", "이동당 10mm",
        "10 mm na ruch", "10 mm por movimento", "10 мм за ход", "10 мм на хід", "每移动10毫米", "每移動10毫米",
    ),
    "120 min": (
        "120 Min", "120 minutos", "120 minutes", "120 min", "120分", "120분", "120 min", "120 minutos", "120 мин.",
        "120 хв", "120分钟", "120分鐘",
    ),
    "15 min": (
        "15 Min", "15 minutos", "15 minutes", "15 min", "15分", "15분", "15 min", "15 minutos", "15 мин.", "15 хв",
        "15分钟", "15分鐘",
    ),
    "30 min": (
        "30 Min", "30 minutos", "30 minutes", "30 min", "30分", "30분", "30 min", "30 minutos", "30 мин.", "30 хв",
        "30分钟", "30分鐘",
    ),
    "5 min": (
        "5 Min", "5 minutos", "5 minutes", "5 min", "5分", "5분", "5 min", "5 minutos", "5 мин.", "5 хв", "5分钟", "5分鐘",
    ),
    "5 min · {readings}": (
        "5 Min. · {readings}", "5 min · {readings}", "5 minutes · {readings}", "5 minuti · {readings}", "5分・{readings}",
        "5분 · {readings}", "5 min · {readings}", "5 min · {readings}", "5 мин · {readings}", "5 хв · {readings}",
        "5分钟·{readings}", "5分鐘·{readings}",
    ),
    "60 min": (
        "60 Min", "60 minutos", "60 minutes", "60 min", "60分", "60분", "60 min", "60 minutos", "60 мин.", "60 хв",
        "60分钟", "60分鐘",
    ),
    "A JSON printer row needs four strings: address, CheckCode, name and serial.": (
        "Eine JSON-Druckerzeile benötigt vier Zeichenfolgen: Adresse, CheckCode, Name und Seriennummer.",
        "Una fila de impresora JSON necesita cuatro cadenas: dirección, CheckCode, nombre y serie.",
        "Une ligne d'imprimante JSON nécessite quatre chaînes : adresse, CheckCode, nom et numéro de série.",
        "Una riga della stampante JSON necessita di quattro stringhe: indirizzo, CheckCode, nome e seriale.",
        "JSON プリンタ行には、アドレス、CheckCode、名前、シリアルの 4 つの文字列が必要です。", "JSON 프린터 행에는 주소, CheckCode, 이름, 일련번호 등 4개의 문자열이 필요합니다.",
        "Rząd drukarek JSON wymaga czterech ciągów znaków: adresu, CheckCode, nazwy i numeru seryjnego.",
        "Uma linha de impressora JSON precisa de quatro strings: endereço, CheckCode, nome e serial.",
        "В записи принтера в формате JSON должно быть четыре строки: адрес, CheckCode, имя и серийный номер.",
        "Для рядка принтера JSON потрібні чотири рядки: адреса, CheckCode, назва та серійний номер.",
        "JSON 打印机行需要四个字符串：地址、CheckCode、名称和序列号。", "JSON 印表機行需要四個字串：位址、CheckCode、名稱和序號。",
    ),
    "Another print source is being read.": (
        "Eine andere Druckquelle wird gelesen.", "Se está leyendo otra fuente impresa.",
        "Une autre source d'impression est en cours de lecture.",
        "È in corso la lettura di un'altra origine di stampa.", "別の印刷ソースを読み取っています。", "다른 인쇄 소스를 읽는 중입니다.",
        "Trwa odczytywanie innego źródła wydruku.", "Outra fonte de impressão está sendo lida.",
        "Уже считывается другой файл для печати.", "Зчитується інше джерело друку.", "正在读取另一个打印源。", "正在讀取另一個列印來源。",
    ),
    "Authenticated readings: {availability}": (
        "Authentifizierte Messwerte: {availability}", "Lecturas autenticadas: {availability}",
        "Lectures authentifiées : {availability}", "Letture autenticate: {availability}", "認証済み読み取り値: {availability}",
        "인증 수치: {availability}", "Uwierzytelnione odczyty: {availability}", "Leituras autenticadas: {availability}",
        "Данные с авторизацией: {availability}", "Автентифіковані показання: {availability}", "认证读数：{availability}",
        "認證讀數：{availability}",
    ),
    "Black": (
        "Schwarz", "Negro", "Noir", "Nero", "ブラック", "블랙", "Czarny", "Preto", "Черный", "Чорний", "黑色", "黑色",
    ),
    "Blue": (
        "Blau", "Azul", "Bleu", "Blu", "ブルー", "블루", "Niebieski", "Azul", "Синий", "Синій", "蓝色", "藍色",
    ),
    "Brown": (
        "Braun", "Marrón", "Marron", "Marrone", "ブラウン", "브라운", "Brązowy", "Marrom", "Коричневый", "Коричневий", "棕色",
        "棕色",
    ),
    "Camera URL: {status}": (
        "Kamera-URL: {status}", "URL de la cámara: {status}", "URL de la caméra : {status}",
        "URL della telecamera: {status}", "カメラURL：{status}", "카메라 URL: {status}", "Adres URL kamery: {status}",
        "URL da câmera: {status}", "Адрес камеры: {status}", "URL-адреса камери: {status}", "相机网址：{status}",
        "相機網址：{status}",
    ),
    "Camera frame exceeds the size limit.": (
        "Der Kamerarahmen überschreitet die Größenbeschränkung.", "El marco de la cámara excede el límite de tamaño.",
        "Le cadre de la caméra dépasse la limite de taille.",
        "La cornice della fotocamera supera il limite di dimensione.", "カメラのフレームがサイズ制限を超えています。",
        "카메라 프레임이 크기 제한을 초과했습니다.", "Ramka kamery przekracza limit rozmiaru.",
        "O quadro da câmera excede o limite de tamanho.", "Кадр с камеры слишком большой.",
        "Рамка камери перевищує обмеження розміру.", "相机框超出尺寸限制。", "相機框超出尺寸限制。",
    ),
    "Camera is in use. Close the other view and reconnect.": (
        "Kamera wird verwendet. Schließen Sie die andere Ansicht und stellen Sie die Verbindung wieder her.",
        "La cámara está en uso. Cierra la otra vista y vuelve a conectarte.",
        "La caméra est en cours d'utilisation. Fermez l’autre vue et reconnectez-vous.",
        "La fotocamera è in uso. Chiudi l'altra vista e riconnettiti.", "カメラが使用中です。他のビューを閉じて、再接続します。",
        "카메라가 사용중입니다. 다른 보기를 닫고 다시 연결하십시오.", "Kamera jest w użyciu. Zamknij drugi widok i połącz się ponownie.",
        "A câmera está em uso. Feche a outra visualização e reconecte.",
        "Камера уже открыта. Закройте другой просмотр и подключитесь снова.",
        "Камера використовується. Закрийте інше подання та знову підключіться.", "相机正在使用中。关闭另一个视图并重新连接。",
        "相機正在使用中。關閉另一個視圖並重新連線。",
    ),
    "Camera is warming up": (
        "Kamera wärmt auf", "La cámara se está calentando", "La caméra chauffe", "La fotocamera si sta riscaldando",
        "カメラがウォーミングアップ中です", "카메라 준비 중", "Kamera nagrzewa się", "A câmera está aquecendo", "Камера запускается",
        "Камера розігрівається", "相机正在预热", "相機正在預熱",
    ),
    "Camera stream ended. Reconnect to try again.": (
        "Kamerastream beendet. Stellen Sie die Verbindung wieder her, um es erneut zu versuchen.",
        "La transmisión de la cámara finalizó. Vuelve a conectarte para intentarlo de nuevo.",
        "Le flux de la caméra est terminé. Reconnectez-vous pour réessayer.",
        "Il flusso della telecamera è terminato. Riconnettiti per riprovare.", "カメラストリームが終了しました。再接続してもう一度試してください。",
        "카메라 스트리밍이 종료되었습니다. 다시 시도하려면 다시 연결하세요.",
        "Strumień z kamery zakończył się. Połącz się ponownie, aby spróbować jeszcze raz.",
        "A transmissão da câmera terminou. Reconecte para tentar novamente.",
        "Трансляция с камеры прервалась. Подключитесь снова.",
        "Потік камери завершено. Повторно підключіться, щоб повторити спробу.", "摄像机流结束。重新连接再试一次。", "攝影機串流結束。重新連線再試一次。",
    ),
    "Chart nozzle": (
        "Diagrammdüse", "Boquilla para gráficos", "Buse graphique", "Ugello carta", "チャートノズル", "차트 노즐",
        "Dysza wykresowa", "Bico gráfico", "Сопло на графике", "Насадка для діаграм", "图表喷嘴", "圖表噴嘴",
    ),
    "Chart of actual and target temperatures for the nozzle and bed": (
        "Diagramm der Ist- und Zieltemperaturen für Düse und Bett",
        "Gráfico de temperaturas reales y objetivo para la boquilla y el lecho",
        "Graphique des températures réelles et cibles pour la buse et le lit",
        "Grafico delle temperature effettive e target per l'ugello e il letto", "ノズルとベッドの実際温度と目標温度のグラフ",
        "노즐과 베드의 실제온도와 목표온도 차트", "Wykres rzeczywistych i docelowych temperatur dyszy i złoża",
        "Gráfico de temperaturas reais e alvo para o bico e leito",
        "График фактической и заданной температуры сопла и стола",
        "Діаграма фактичної та цільової температур для сопла та шару", "喷嘴和床的实际温度和目标温度图表", "噴嘴和床的實際溫度和目標溫度圖表",
    ),
    "Checking axis position…": (
        "Position prüfen…", "Comprobando la posición…", "Vérification de la position…", "Controllo della posizione...",
        "位置確認中…", "위치 확인 중…", "Sprawdzam pozycję…", "Verificando posição…", "Проверка положения осей…",
        "Перевірка позиції…", "检查位置…", "檢查位置…",
    ),
    "Checking connection…": (
        "Verbindung prüfen…", "Comprobando la conexión…", "Vérification de la connexion…",
        "Controllo della connessione…", "接続を確認中…", "연결 확인 중…", "Sprawdzam połączenie…", "Verificando conexão…",
        "Проверка подключения…", "Перевірка з’єднання…", "检查连接...", "檢查連線...",
    ),
    "Checking position…": (
        "Position prüfen…", "Comprobando la posición…", "Vérification de la position…", "Controllo della posizione...",
        "位置確認中…", "위치 확인 중…", "Sprawdzam pozycję…", "Verificando posição…", "Проверка положения…", "Перевірка позиції…",
        "检查位置…", "檢查位置…",
    ),
    "Checking printer status…": (
        "Druckerstatus prüfen…", "Comprobando el estado de la impresora…", "Vérification de l'état de l'imprimante…",
        "Controllo dello stato della stampante…", "プリンターの状態を確認中…", "프린터 상태를 확인하는 중…", "Sprawdzanie stanu drukarki…",
        "Verificando o status da impressora…", "Проверка состояния принтера…", "Перевірка стану принтера…",
        "检查打印机状态...", "檢查印表機狀態...",
    ),
    "Checking printer…": (
        "Drucker prüfen…", "Comprobando la impresora…", "Vérification de l'imprimante…", "Controllo della stampante…",
        "プリンターを確認中…", "프린터를 확인하는 중…", "Sprawdzam drukarkę…", "Verificando a impressora…", "Проверка принтера…",
        "Перевірка принтера…", "检查打印机...", "檢查印表機...",
    ),
    "Checking the result of {command}…": (
        "Überprüfen des Ergebnisses von {command}…", "Comprobando el resultado de {command}…",
        "Vérification du résultat de {command}…", "Controllo del risultato di {command}…", "{command}の結果を確認中…",
        "{command} 결과를 확인하는 중…", "Sprawdzanie wyniku {command}…", "Verificando o resultado de {command}…",
        "Проверка результата команды «{command}»…", "Перевірка результату {command}…", "检查{command}的结果...",
        "檢查{command}的結果...",
    ),
    "Choose a .3mf no larger than 512 MB.": (
        "Wählen Sie ein .3mf, das nicht größer als 512 MB ist.", "Elija un .3mf que no supere los 512 MB.",
        "Choisissez un .3mf ne dépassant pas 512 Mo.", "Scegli un .3mf non più grande di 512 MB.",
        "512 MB 以下の .3mf を選択してください。", "512MB 이하의 .3mf를 선택하세요.",
        "Wybierz plik .3mf o rozmiarze nie większym niż 512 MB.", "Escolha um .3mf com tamanho não superior a 512 MB.",
        "Выберите .3mf размером не более 512 МБ.", "Виберіть .3mf розміром не більше 512 МБ.", "选择不大于512 MB 的.3mf。",
        "選擇不大於512 MB 的.3mf。",
    ),
    "Choose a nonempty print file no larger than 128 MB.": (
        "Wählen Sie eine nicht leere Druckdatei, die nicht größer als 128 MB ist.",
        "Elija un archivo de impresión que no esté vacío y no supere los 128 MB.",
        "Choisissez un fichier d'impression non vide ne dépassant pas 128 Mo.",
        "Scegli un file di stampa non vuoto non più grande di 128 MB.", "128 MB 以下の空でない印刷ファイルを選択してください。",
        "128MB 이하의 비어 있지 않은 인쇄 파일을 선택하세요.", "Wybierz niepusty plik wydruku o rozmiarze nie większym niż 128 MB.",
        "Escolha um arquivo de impressão não vazio com tamanho máximo de 128 MB.",
        "Выберите непустой файл для печати размером не более 128 МБ.",
        "Виберіть непорожній файл для друку розміром не більше 128 МБ.", "选择不大于128 MB 的非空打印文件。",
        "選擇不大於128 MB 的非空白列印檔案。",
    ),
    "Choose a print file no larger than 128 MB.": (
        "Wählen Sie eine Druckdatei, die nicht größer als 128 MB ist.",
        "Elija un archivo de impresión que no supere los 128 MB.",
        "Choisissez un fichier d'impression ne dépassant pas 128 Mo.",
        "Scegli un file di stampa non più grande di 128 MB.", "印刷ファイルは 128 MB 以下のものを選択してください。",
        "128MB 이하의 인쇄 파일을 선택하세요.", "Wybierz plik wydruku o rozmiarze nie większym niż 128 MB.",
        "Escolha um arquivo de impressão com tamanho máximo de 128 MB.",
        "Выберите файл для печати размером не более 128 МБ.", "Виберіть файл для друку розміром не більше 128 МБ.",
        "选择不大于128 MB 的打印文件。", "選擇不大於128 MB 的列印檔案。",
    ),
    "Choose a sliced .3mf with one plate and filament information.": (
        "Wählen Sie eine .3mf-Datei mit einer geslicten Druckplatte und Filamentinformationen.",
        "Selecciona un archivo .3mf con una sola placa laminada y datos del filamento.",
        "Choisissez un fichier .3mf contenant une seule plaque tranchée et les données de filament.",
        "Scegli un file .3mf con un solo piatto affettato e i dati dei filamenti.",
        "スライス済みプレート1枚とフィラメント情報を含む .3mf ファイルを選択してください。", "슬라이스를 완료한 플레이트 하나와 필라멘트 정보가 들어 있는 .3mf 파일을 선택하세요.",
        "Wybierz plik .3mf zawierający jedną pociętą płytę roboczą i dane filamentów.",
        "Escolha um arquivo .3mf com uma única placa fatiada e informações dos filamentos.",
        "Выберите .3mf с одним нарезанным столом и данными о материалах.",
        "Виберіть файл .3mf з однією нарізаною пластиною та даними про філаменти.", "请选择包含一张已切片打印盘和耗材信息的 .3mf 文件。",
        "請選擇包含一張已切片列印板與線材資訊的 .3mf 檔案。",
    ),
    "Choose a sliced .3mf with one plate.": (
        "Wählen Sie eine .3mf-Datei mit genau einer geslicten Druckplatte.",
        "Selecciona un archivo .3mf con una sola placa de impresión ya laminada.",
        "Choisissez un fichier .3mf qui contient une seule plaque déjà tranchée.",
        "Scegli un file .3mf con un solo piatto già affettato.", "スライス済みプレートを1枚だけ含む .3mf ファイルを選択してください。",
        "슬라이스를 완료한 플레이트가 하나만 들어 있는 .3mf 파일을 선택하세요.", "Wybierz plik .3mf zawierający jedną pociętą płytę roboczą.",
        "Escolha um arquivo .3mf com apenas uma placa de impressão fatiada.",
        "Выберите .3mf с одним нарезанным столом.", "Виберіть файл .3mf з однією нарізаною пластиною.",
        "请选择仅包含一张已切片打印盘的 .3mf 文件。", "請選擇只包含一張已切片列印板的 .3mf 檔案。",
    ),
    "Choose a text .gcode file or a sliced .3mf.": (
        "Wählen Sie eine Text-.gcode-Datei oder eine segmentierte .3mf-Datei.",
        "Elija un archivo .gcode de texto o un archivo .3mf cortado.",
        "Choisissez un fichier texte .gcode ou un fichier .3mf tranché.",
        "Scegli un file di testo .gcode o uno .3mf suddiviso in sezioni.",
        "テキスト .gcode ファイルまたはスライスされた .3mf ファイルを選択します。", "텍스트 .gcode 파일이나 슬라이스 .3mf를 선택하세요.",
        "Wybierz plik tekstowy .gcode lub pokrojony plik .3mf.",
        "Escolha um arquivo de texto .gcode ou um .3mf fatiado.",
        "Выберите текстовый файл .gcode или .3mf с подготовленной нарезкой.",
        "Виберіть текстовий файл .gcode або нарізаний .3mf.", "选择文本.gcode 文件或切片.3mf 文件。", "選擇文字.gcode 檔案或切片.3mf 檔案。",
    ),
    "Choose the original .3mf with one sliced plate and the same file name.": (
        "Wählen Sie die originale .3mf-Datei mit genau einer geslicten Druckplatte und demselben Dateinamen.",
        "Selecciona el archivo .3mf original con una sola placa laminada y el mismo nombre de archivo.",
        "Choisissez le fichier .3mf d’origine qui contient une seule plaque tranchée et porte le même nom.",
        "Scegli il file .3mf originale con un solo piatto già affettato e lo stesso nome.",
        "同じファイル名で、スライス済みプレートを1枚だけ含む元の .3mf ファイルを選択してください。", "파일 이름이 같고 슬라이스를 완료한 플레이트가 하나만 들어 있는 원본 .3mf 파일을 선택하세요.",
        "Wybierz oryginalny plik .3mf z jedną pociętą płytą roboczą i tą samą nazwą.",
        "Escolha o arquivo .3mf original com uma única placa fatiada e o mesmo nome.",
        "Выберите исходный .3mf с одним нарезанным столом и тем же именем файла.",
        "Виберіть вихідний файл .3mf з однією нарізаною пластиною та такою самою назвою.",
        "请选择同名的原始 .3mf 文件，该文件应仅包含一张已切片的打印盘。", "請選擇同名的原始 .3mf 檔案，且檔案須只包含一張已切片的列印板。",
    ),
    "Choose the original sliced .3mf for this Creator file.": (
        "Wählen Sie das ursprünglich geschnittene .3mf für diese Creator-Datei.",
        "Elija el .3mf cortado original para este archivo Creator.",
        "Choisissez le .3mf tranché original pour ce fichier Creator.",
        "Scegli lo .3mf affettato originale per questo file Creator.", "この Creator ファイルには、スライスされたオリジナルの .3mf を選択します。",
        "이 Creator 파일에 대해 슬라이스된 원본 .3mf를 선택하십시오.", "Wybierz oryginalny pokrojony plik .3mf dla tego pliku kreatora.",
        "Escolha o .3mf fatiado original para este arquivo Creator.",
        "Выберите исходный .3mf с нарезкой для этого файла Creator.",
        "Виберіть оригінальний нарізаний .3mf для цього файлу Creator.", "为此Creator 文件选择原始切片.3mf。",
        "為此Creator 檔案選擇原始切片.3mf。",
    ),
    "Choose the original sliced .3mf.": (
        "Wählen Sie das original geschnittene .3mf.", "Elija el .3mf en rodajas original.",
        "Choisissez le .3mf tranché original.", "Scegli l'originale .3mf a fette.", "オリジナルスライス.3mfをお選びください。",
        "오리지널 슬라이스 .3mf를 선택하세요.", "Wybierz oryginalny pokrojony plik .3mf.", "Escolha o .3mf fatiado original.",
        "Выберите исходный .3mf с нарезкой.", "Виберіть оригінальний нарізаний .3mf.", "选择原始切片.3mf。", "選擇原始切片.3mf。",
    ),
    "Choose the sliced .3mf that matches the stored file name.": (
        "Wählen Sie das geschnittene .3mf aus, das dem gespeicherten Dateinamen entspricht.",
        "Elija el .3mf cortado que coincida con el nombre del archivo almacenado.",
        "Choisissez le .3mf découpé qui correspond au nom du fichier stocké.",
        "Scegli lo .3mf affettato che corrisponde al nome del file memorizzato.", "保存されているファイル名と一致するスライスされた.3mfを選択します。",
        "저장된 파일 이름과 일치하는 슬라이스 .3mf를 선택하십시오.", "Wybierz pokrojony plik .3mf odpowiadający nazwie zapisanego pliku.",
        "Escolha o .3mf fatiado que corresponda ao nome do arquivo armazenado.",
        "Выберите .3mf с нарезкой и тем же именем, что у файла на принтере.",
        "Виберіть нарізаний .3mf, який відповідає імені збереженого файлу.", "选择与存储的文件名匹配的切片.3mf。",
        "選擇與儲存的檔案名稱相符的切片.3mf。",
    ),
    "Completed": (
        "Abgeschlossen", "Completado", "Terminé", "Completato", "完成", "완료", "Ukończono", "Concluído", "Завершено",
        "Виконано", "已完成", "已完成",
    ),
    "Completed: {done} of {total}": (
        "Abgeschlossen: {done} von {total}", "Completado: {done} de {total}", "Terminé : {done} de {total}",
        "Completato: {done} di {total}", "完成：{done}の{total}", "완성: {total}의 {done}", "Ukończono: {done} z {total}",
        "Concluído: {done} de {total}", "Завершено: {done} из {total}", "Виконано: {done} з {total}",
        "已完成：{total} 的 {done}", "已完成：{total} 的 {done}",
    ),
    "Copy report": (
        "Bericht kopieren", "Copiar informe", "Copier le rapport", "Copia rapporto", "コピーレポート", "보고서 복사",
        "Skopiuj raport", "Copiar relatório", "Копировать отчёт", "Копіювати звіт", "复制报告", "複製報告",
    ),
    "Coral": (
        "Koralle", "Corales", "Corail", "Corallo", "サンゴ", "산호초", "Koral", "Coral", "Коралл", "Корал", "珊瑚", "珊瑚",
    ),
    "Could not confirm the upload. Refresh Printer Files before retrying.": (
        "Der Upload konnte nicht bestätigt werden. Aktualisieren Sie die Druckerdateien, bevor Sie es erneut versuchen.",
        "No se pudo confirmar la carga. Actualice los archivos de la impresora antes de volver a intentarlo.",
        "Impossible de confirmer le téléchargement. Actualisez les fichiers de l'imprimante avant de réessayer.",
        "Impossibile confermare il caricamento. Aggiorna i file della stampante prima di riprovare.",
        "アップロードが確認できませんでした。再試行する前にプリンタ ファイルを更新してください。", "업로드를 확인할 수 없습니다. 다시 시도하기 전에 프린터 파일을 새로 고치십시오.",
        "Nie można potwierdzić przesyłania. Odśwież pliki drukarki przed ponowną próbą.",
        "Não foi possível confirmar o upload. Atualize os arquivos da impressora antes de tentar novamente.",
        "Не удалось подтвердить загрузку. Обновите список файлов принтера перед повторной попыткой.",
        "Не вдалося підтвердити завантаження. Оновіть файли принтера перед повторною спробою.", "无法确认上传。重试之前刷新打印机文件。",
        "無法確認上傳。重試之前刷新印表機文件。",
    ),
    "Could not confirm the upload. Refresh Printer Files.": (
        "Der Upload konnte nicht bestätigt werden. Druckerdateien aktualisieren.",
        "No se pudo confirmar la carga. Actualizar archivos de impresora.",
        "Impossible de confirmer le téléchargement. Actualiser les fichiers de l'imprimante.",
        "Impossibile confermare il caricamento. Aggiorna i file della stampante.", "アップロードが確認できませんでした。プリンターファイルを更新します。",
        "업로드를 확인할 수 없습니다. 프린터 파일을 새로 고칩니다.", "Nie można potwierdzić przesyłania. Odśwież pliki drukarki.",
        "Não foi possível confirmar o upload. Atualize os arquivos da impressora.",
        "Не удалось подтвердить загрузку. Обновите список файлов принтера.",
        "Не вдалося підтвердити завантаження. Оновити файли принтера.", "无法确认上传。刷新打印机文件。", "無法確認上傳。刷新印表機檔案。",
    ),
    "Could not open the MJPEG stream. Check the camera URL.": (
        "Der MJPEG-Stream konnte nicht geöffnet werden. Überprüfen Sie die Kamera-URL.",
        "No se pudo abrir la transmisión MJPEG. Verifique la URL de la cámara.",
        "Impossible d'ouvrir le flux MJPEG. Vérifiez l'URL de la caméra.",
        "Impossibile aprire il flusso MJPEG. Controlla l'URL della fotocamera.",
        "MJPEG ストリームを開けませんでした。カメラのURLを確認してください。", "MJPEG 스트림을 열 수 없습니다. 카메라 URL을 확인하세요.",
        "Nie można otworzyć strumienia MJPEG. Sprawdź adres URL kamery.",
        "Não foi possível abrir o fluxo MJPEG. Verifique o URL da câmera.",
        "Не удалось открыть поток MJPEG. Проверьте URL-адрес камеры.",
        "Не вдалося відкрити потік MJPEG. Перевірте URL камери.", "无法打开MJPEG 流。检查相机 URL。", "無法開啟MJPEG 流。檢查相機 URL。",
    ),
    "Could not read a snapshot from the printer.": (
        "Ein Schnappschuss konnte nicht vom Drucker gelesen werden.",
        "No se pudo leer una instantánea de la impresora.",
        "Impossible de lire un instantané à partir de l'imprimante.",
        "Impossibile leggere un'istantanea dalla stampante.", "プリンターからスナップショットを読み取れませんでした。", "프린터에서 스냅샷을 읽을 수 없습니다.",
        "Nie można odczytać migawki z drukarki.", "Não foi possível ler um instantâneo da impressora.",
        "Не удалось получить снимок с принтера.", "Не вдалося прочитати знімок із принтера.", "无法从打印机读取快照。",
        "無法從印表機讀取快照。",
    ),
    "Could not read slice information from this .3mf.": (
        "Slice-Informationen von diesem .3mf konnten nicht gelesen werden.",
        "No se pudo leer la información del segmento de este .3mf.",
        "Impossible de lire les informations de tranche de ce .3mf.",
        "Impossibile leggere le informazioni sulla sezione da questo .3mf.", "この.3mfからスライス情報を読み取れませんでした。",
        "이 .3mf에서 슬라이스 정보를 읽을 수 없습니다.", "Nie można odczytać informacji o plasterku z tego .3mf.",
        "Não foi possível ler as informações de fatia deste .3mf.",
        "Не удалось прочитать данные нарезки из этого .3mf.", "Не вдалося прочитати інформацію зрізу з цього .3mf.",
        "无法从此.3mf 读取切片信息。", "無法從此.3mf 讀取切片資訊。",
    ),
    "Could not read the camera.": (
        "Die Kamera konnte nicht gelesen werden.", "No se pudo leer la cámara.", "Impossible de lire la caméra.",
        "Impossibile leggere la fotocamera.", "カメラを読み取れませんでした。", "카메라를 읽을 수 없습니다.",
        "Nie można odczytać danych z kamery.", "Não foi possível ler a câmera.", "Не удалось прочитать данные камеры.",
        "Не вдалося прочитати камеру.", "无法读取相机。", "無法讀取相機。",
    ),
    "Could not read the saved printer web interface URLs.": (
        "Die gespeicherten Webschnittstellen-URLs des Druckers konnten nicht gelesen werden.",
        "No se pudieron leer las URL de la interfaz web de la impresora guardadas.",
        "Impossible de lire les URL de l'interface Web de l'imprimante enregistrées.",
        "Impossibile leggere gli URL dell'interfaccia Web della stampante salvati.",
        "保存されたプリンター Web インターフェイス URL を読み取れませんでした。", "저장된 프린터 웹 인터페이스 URL을 읽을 수 없습니다.",
        "Nie można odczytać zapisanych adresów URL interfejsu sieciowego drukarki.",
        "Não foi possível ler os URLs salvos da interface da Web da impressora.",
        "Не удалось прочитать сохраненные URL-адреса веб-интерфейса принтера.",
        "Не вдалося прочитати збережені URL-адреси веб-інтерфейсу принтера.", "无法读取已保存的打印机Web 界面URL。",
        "無法讀取已儲存的印表機Web 介面URL。",
    ),
    "Could not read the source.": (
        "Die Quelle konnte nicht gelesen werden.", "No se pudo leer la fuente.", "Impossible de lire la source.",
        "Impossibile leggere la fonte.", "ソースが読み込めませんでした。", "소스를 읽을 수 없습니다.", "Nie można odczytać źródła.",
        "Não foi possível ler a fonte.", "Не удалось прочитать исходный файл.", "Не вдалося прочитати джерело.",
        "无法读取源代码。", "無法讀取原始碼。",
    ),
    "Could not read this print source.": (
        "Diese Druckquelle konnte nicht gelesen werden.", "No se pudo leer esta fuente impresa.",
        "Impossible de lire cette source d'impression.", "Impossibile leggere questa origine di stampa.",
        "この印刷ソースを読み取れませんでした。", "이 인쇄 소스를 읽을 수 없습니다.", "Nie można odczytać tego źródła wydruku.",
        "Não foi possível ler esta fonte de impressão.", "Не удалось прочитать исходный файл печати.",
        "Не вдалося прочитати це джерело друку.", "无法读取该打印源。", "無法讀取該列印來源。",
    ),
    "Could not save notification settings.": (
        "Benachrichtigungseinstellungen konnten nicht gespeichert werden.",
        "No se pudo guardar la configuración de notificaciones.",
        "Impossible d'enregistrer les paramètres de notification.", "Impossibile salvare le impostazioni di notifica.",
        "通知設定を保存できませんでした。", "알림 설정을 저장할 수 없습니다.", "Nie można zapisać ustawień powiadomień.",
        "Não foi possível salvar as configurações de notificação.", "Не удалось сохранить настройки уведомлений.",
        "Не вдалося зберегти налаштування сповіщень.", "无法保存通知设置。", "無法儲存通知設定。",
    ),
    "Could not save print history. New entries are still available for this session.": (
        "Der Druckverlauf konnte nicht gespeichert werden. Für diese Sitzung sind noch neue Einträge verfügbar.",
        "No se pudo guardar el historial de impresión. Todavía hay nuevas entradas disponibles para esta sesión.",
        "Impossible de sauvegarder l'historique des impressions. De nouvelles entrées sont encore disponibles pour cette session.",
        "Impossibile salvare la cronologia di stampa. Sono ancora disponibili nuove voci per questa sessione.",
        "印刷履歴を保存できませんでした。このセッションでは引き続き新しいエントリを利用できます。", "인쇄 기록을 저장할 수 없습니다. 이 세션에는 아직 새 항목을 사용할 수 있습니다.",
        "Nie można zapisać historii drukowania. W tej sesji nadal dostępne są nowe wpisy.",
        "Não foi possível salvar o histórico de impressão. Novas entradas ainda estão disponíveis para esta sessão.",
        "Не удалось сохранить историю печати. Новые записи доступны до конца сеанса.",
        "Не вдалося зберегти історію друку. Нові записи все ще доступні для цього сеансу.", "无法保存打印历史记录。本次会议仍有新条目可供使用。",
        "無法儲存列印歷史記錄。本次會議仍有新條目可供使用。",
    ),
    "Cream": (
        "Creme", "Crema", "Crème", "Panna", "クリーム", "크림", "Krem", "Creme", "Кремовый", "Крем", "霜", "霜",
    ),
    "Creator repeat printing requires a .3mf.": (
        "Für den wiederholten Druck des Creators ist ein .3mf erforderlich.",
        "La impresión repetida de Creator requiere un .3mf.", "L'impression répétée du créateur nécessite un .3mf.",
        "La stampa ripetuta del creatore richiede un .3mf.", "クリエイターリピート印刷には.3mfが必要です。", "Creator 반복 인쇄에는 .3mf가 필요합니다.",
        "Ponowne drukowanie przez twórcę wymaga .3mf.", "A impressão repetida do Creator requer um .3mf.",
        "Для повторной печати на Creator нужен файл .3mf.", "Повторний друк Creator вимагає .3mf.",
        "Creator重复打印需要.3mf。", "Creator重複列印需要.3mf。",
    ),
    "Cyan": (
        "Cyan", "Cian", "Cyan", "Ciano", "シアン", "시안", "Cyjan", "Ciano", "Циан", "Блакитний", "青色", "青色",
    ),
    "Dark blue": (
        "Dunkelblau", "Azul oscuro", "Bleu foncé", "Blu scuro", "ダークブルー", "다크 블루", "Ciemnoniebieski", "Azul escuro",
        "Темно-синий", "Темно-синій", "深蓝色", "深藍色",
    ),
    "Dark brown": (
        "Dunkelbraun", "Marrón oscuro", "Brun foncé", "Marrone scuro", "ダークブラウン", "암갈색", "Ciemny brąz",
        "Castanho escuro", "Темно-коричневый", "Темно-коричневий", "深棕色", "深棕色",
    ),
    "Dark green": (
        "Dunkelgrün", "Verde oscuro", "Vert foncé", "Verde scuro", "ダークグリーン", "다크그린", "Ciemnozielony", "Verde escuro",
        "Темно-зеленый", "Темно-зелений", "深绿色", "深綠色",
    ),
    "Device navigation": (
        "Gerätenavigation", "Navegación del dispositivo", "Navigation sur l'appareil", "Navigazione del dispositivo",
        "デバイスナビゲーション", "기기 탐색", "Nawigacja urządzenia", "Navegação do dispositivo", "Навигация по вкладкам принтера",
        "Навігація пристрою", "设备导航", "裝置導航",
    ),
    "Door / lid: {state}": (
        "Tür/Deckel: {state}", "Puerta / tapa: {state}", "Porte / couvercle : {state}", "Porta/coperchio: {state}",
        "扉・蓋：{state}", "문/뚜껑:{state}", "Drzwi/pokrywa: {state}", "Porta/tampa: {state}", "Дверь/крышка: {state}",
        "Дверцята/кришка: {state}", "门/盖：{state}", "門/蓋子：{state}",
    ),
    "Edit as text": (
        "Als Text bearbeiten", "Editar como texto", "Modifier sous forme de texte", "Modifica come testo", "テキストとして編集",
        "텍스트로 편집", "Edytuj jako tekst", "Editar como texto", "Редактировать как текст", "Редагувати як текст", "编辑为文本",
        "編輯為文字",
    ),
    "Edit slot {slot} · {material}": (
        "Steckplatz bearbeiten {slot} · {material}", "Editar ranura {slot} · {material}",
        "Modifier l'emplacement {slot} · {material}", "Modifica slot {slot} · {material}", "スロット編集 {slot}・{material}",
        "슬롯 편집 {slot} · {material}", "Edytuj szczelinę {slot} · {material}", "Editar slot {slot} · {material}",
        "Редактировать слот {slot} · {material}", "Слот редагування {slot} · {material}", "编辑槽 {slot} · {material}",
        "編輯槽 {slot} · {material}",
    ),
    "Elapsed: {time}": (
        "Verstrichen: {time}", "Transcurrido: {time}", "Écoulé : {time}", "Trascorso: {time}", "経過時間: {time}",
        "경과: {time}", "Upłynęło: {time}", "Decorrido: {time}", "Прошло: {time}", "Минуло: {time}", "已过：{time}",
        "已過：{time}",
    ),
    "Empty camera stream": (
        "Leerer Kamerastream", "Transmisión de cámara vacía", "Flux de caméra vide", "Flusso della telecamera vuoto",
        "空のカメラストリーム", "빈 카메라 스트림", "Pusty strumień kamery", "Fluxo de câmera vazio", "Камера не передаёт кадры",
        "Порожній потік камери", "空相机流", "空相機流",
    ),
    "Ended: {time}": (
        "Beendet: {time}", "Finalizó: {time}", "Terminé : {time}", "Terminato: {time}", "終了：{time}", "종료됨: {time}",
        "Zakończono: {time}", "Finalizado: {time}", "Окончание: {time}", "Завершено: {time}", "结束：{time}", "結束：{time}",
    ),
    "Endstops: {state}": (
        "Endschalter: {state}", "Finales de carrera: {state}", "Fins de course : {state}", "Finecorsa: {state}",
        "リミットスイッチ：{state}", "리미트 스위치: {state}", "Krańcówki: {state}", "Sensores de fim de curso: {state}",
        "Концевики: {state}", "Кінцеві вимикачі: {state}", "限位开关：{state}", "限位開關：{state}",
    ),
    "Enter a serial number using up to 64 letters, digits, hyphens or underscores.": (
        "Geben Sie eine Seriennummer mit bis zu 64 Buchstaben, Ziffern, Bindestrichen oder Unterstrichen ein.",
        "Ingrese un número de serie usando hasta 64 letras, dígitos, guiones o guiones bajos.",
        "Saisissez un numéro de série en utilisant jusqu'à 64 lettres, chiffres, traits d'union ou traits de soulignement.",
        "Immettere un numero di serie utilizzando fino a 64 lettere, cifre, trattini o trattini bassi.",
        "シリアル番号を64文字以内の文字、数字、ハイフン、アンダースコアで入力します。", "최대 64자의 문자, 숫자, 하이픈, 밑줄을 사용하여 일련번호를 입력하세요.",
        "Wprowadź numer seryjny, składający się maksymalnie z 64 liter, cyfr, łączników i podkreśleń.",
        "Insira um número de série usando até 64 letras, dígitos, hífens ou sublinhados.",
        "Введите серийный номер, используя до 64 букв, цифр, дефисов или подчеркиваний.",
        "Введіть серійний номер, використовуючи до 64 літер, цифр, дефісів або підкреслень.",
        "输入最多64 个字母、数字、连字符或下划线的序列号。", "輸入最多64 個字母、數字、連字號或底線的序號。",
    ),
    "Enter an HTTP or HTTPS MJPEG URL without a username or password.": (
        "Geben Sie eine HTTP- oder HTTPS MJPEG-URL ohne Benutzernamen oder Passwort ein.",
        "Ingrese una URL HTTP o HTTPS MJPEG sin nombre de usuario ni contraseña.",
        "Saisissez une URL HTTP ou HTTPS MJPEG sans nom d'utilisateur ni mot de passe.",
        "Inserisci un URL HTTP o HTTPS MJPEG senza nome utente o password.",
        "ユーザー名やパスワードを入力せずに、HTTP または HTTPS MJPEG URL を入力します。", "사용자 이름이나 비밀번호 없이 HTTP 또는 HTTPS MJPEG URL을 입력하세요.",
        "Wprowadź adres URL HTTP lub HTTPS MJPEG bez nazwy użytkownika i hasła.",
        "Insira um URL HTTP ou HTTPS MJPEG sem nome de usuário ou senha.",
        "Введите HTTP- или HTTPS-адрес потока MJPEG без имени пользователя и пароля.",
        "Введіть URL-адресу HTTP або HTTPS MJPEG без імені користувача чи пароля.", "输入HTTP 或HTTPS MJPEG URL，无需用户名或密码。",
        "輸入HTTP 或HTTPS MJPEG URL，無需使用者名稱或密碼。",
    ),
    "Estimated material": (
        "Geschätztes Material", "Material estimado", "Matériel estimé", "Materiale stimato", "推定材料", "추정자료",
        "Szacowany materiał", "Material estimado", "Расход материала", "Орієнтовний матеріал", "预计材料", "預計材料",
    ),
    "Estimated material: {weight}": (
        "Geschätztes Material: {weight}", "Material estimado: {weight}", "Matériel estimé : {weight}",
        "Materiale stimato: {weight}", "推定素材：{weight}", "추정자료 : {weight}", "Szacowany materiał: {weight}",
        "Material estimado: {weight}", "Расход материала: {weight}", "Орієнтовний матеріал: {weight}", "预计材料：{weight}",
        "預計材料：{weight}",
    ),
    "Filament {number}": (
        "Filament {number}", "Filamento {number}", "Filament {number}", "Filamento {number}", "フィラメント {number}",
        "필라멘트 {number}", "Włókno {number}", "Filamento {number}", "Филамент {number}", "Нитка {number}", "长丝{number}",
        "長絲{number}",
    ),
    "File name on printer": (
        "Dateiname auf dem Drucker", "Nombre de archivo en la impresora", "Nom du fichier sur l'imprimante",
        "Nome del file sulla stampante", "プリンター上のファイル名", "프린터의 파일 이름", "Nazwa pliku na drukarce",
        "Nome do arquivo na impressora", "Имя файла на принтере", "Назва файлу на принтері", "打印机上的文件名", "印表機上的檔案名稱",
    ),
    "File upload is available on Adventurer 5M.": (
        "Das Hochladen von Dateien ist auf Adventurer 5M verfügbar.",
        "La carga de archivos está disponible en Adventurer 5M.",
        "Le téléchargement de fichiers est disponible sur Adventurer 5M.",
        "Il caricamento file è disponibile su Adventurer 5M.", "Adventurer 5Mにてファイルアップロードが可能です。",
        "파일 업로드는 Adventurer 5M에서 가능합니다.", "Przesyłanie plików jest możliwe w witrynie Adventurer 5M.",
        "O upload de arquivos está disponível no Adventurer 5M.", "Загрузка файлов доступна на Adventurer 5M.",
        "Завантаження файлів доступне на Adventurer 5M.", "Adventurer 5M支持文件上传。", "Adventurer 5M支援文件上傳。",
    ),
    "Files: {count}": (
        "Dateien: {count}", "Archivos: {count}", "Fichiers : {count}", "File: {count}", "ファイル：{count}", "파일: {count}",
        "Pliki: {count}", "Arquivos: {count}", "Файлы: {count}", "Файли: {count}", "文件：{count}", "文件：{count}",
    ),
    "Finish ≈ {time}": (
        "Ende ≈ {time}", "Acabado ≈ {time}", "Finition ≈ {time}", "Finitura ≈ {time}", "仕上げ ≈ {time}", "마무리 ≒ {time}",
        "Zakończenie ≈ {time}", "Concluir ≈ {time}", "Окончание ≈ {time}", "Оздоблення ≈ {time}", "表面处理 ≈ {time}",
        "表面處理 ≈ {time}",
    ),
    "Firmware": (
        "Firmware", "Firmware", "Micrologiciel", "Firmware", "ファームウェア", "펌웨어", "Oprogramowanie sprzętowe", "Firmware",
        "Прошивка", "Прошивка", "固件", "韌體",
    ),
    "FlashForge Device Tab is shutting down.": (
        "FlashForge Device Tab wird heruntergefahren.", "FlashForge Device Tab se está cerrando.",
        "FlashForge Device Tab s'arrête.", "FlashForge Device Tab si sta spegnendo.",
        "FlashForge Device Tabはシャットダウン中です。", "FlashForge Device Tab가 종료됩니다.", "FlashForge Device Tab zamyka się.",
        "FlashForge Device Tab está desligando.", "FlashForge Device Tab отключается.",
        "FlashForge Device Tab вимикається.", "FlashForge Device Tab 正在关闭。", "FlashForge Device Tab 正在關閉。",
    ),
    "Gray": (
        "Grau", "Gris", "Gris", "Grigio", "グレー", "그레이", "Szary", "Cinza", "Серый", "Сірий", "灰色", "灰色",
    ),
    "Green": (
        "Grün", "Verde", "Vert", "Verde", "グリーン", "그린", "Zielony", "Verde", "Зеленый", "Зелений", "绿色", "綠色",
    ),
    "Heating nozzles: {count}": (
        "Aufheizende Düsen: {count}", "Boquillas en calentamiento: {count}", "Buses en chauffe : {count}",
        "Ugelli in riscaldamento: {count}", "加熱中のノズル：{count}", "가열 중인 노즐: {count}", "Nagrzewające się dysze: {count}",
        "Bicos em aquecimento: {count}", "Сопел на нагреве: {count}", "Сопел, що нагріваються: {count}",
        "正在加热的喷嘴：{count}", "正在加熱的噴嘴：{count}",
    ),
    "Host notifications": (
        "Host-Benachrichtigungen", "Notificaciones del anfitrión", "Notifications de l'hôte", "Notifiche dell'host",
        "ホスト通知", "호스트 알림", "Powiadomienia hosta", "Notificações do anfitrião", "Уведомления OrcaSlicer",
        "Сповіщення хоста", "主机通知", "主機通知",
    ),
    "Incomplete file list": (
        "Unvollständige Dateiliste", "Lista de archivos incompleta", "Liste de fichiers incomplète",
        "Elenco file incompleto", "不完全なファイルリスト", "불완전한 파일 목록", "Niekompletna lista plików",
        "Lista de arquivos incompleta", "Неполный список файлов", "Неповний список файлів", "文件列表不完整", "文件清單不完整",
    ),
    "Incomplete response from the printer": (
        "Unvollständige Antwort vom Drucker", "Respuesta incompleta de la impresora",
        "Réponse incomplète de l'imprimante", "Risposta incompleta dalla stampante", "プリンターからの応答が不完全", "프린터의 불완전한 응답",
        "Niekompletna odpowiedź z drukarki", "Resposta incompleta da impressora", "Неполный ответ принтера.",
        "Неповна відповідь від принтера", "打印机响应不完整", "印表機回應不完整",
    ),
    "Invalid JPEG segment.": (
        "Ungültiges JPEG-Segment.", "Segmento JPEG no válido.", "Segment JPEG invalide.", "Segmento JPEG non valido.",
        "JPEGセグメントが無効です。", "JPEG 세그먼트가 잘못되었습니다.", "Nieprawidłowy segment JPEG.", "Segmento JPEG inválido.",
        "Неверный сегмент JPEG.", "Недійсний сегмент JPEG.", "JPEG 段无效。", "JPEG 段無效。",
    ),
    "Invalid JSON printer row.": (
        "Ungültige JSON-Druckerzeile.", "Fila de impresora JSON no válida.", "Ligne d'imprimante JSON non valide.",
        "Riga stampante JSON non valida.", "JSON プリンタ行が無効です。", "JSON 프린터 행이 잘못되었습니다.",
        "Nieprawidłowy rząd drukarki JSON.", "Linha de impressora JSON inválida.",
        "Некорректная запись принтера в формате JSON.", "Недійсний рядок принтера JSON.", "JSON 打印机行无效。",
        "JSON 印表機行無效。",
    ),
    "Invalid TCP print options.": (
        "Ungültige TCP-Druckoptionen.", "Opciones de impresión TCP no válidas.",
        "Options d'impression TCP non valides.", "Opzioni di stampa TCP non valide.", "TCP 印刷オプションが無効です。",
        "TCP 인쇄 옵션이 잘못되었습니다.", "Nieprawidłowe opcje drukowania TCP.", "Opções de impressão TCP inválidas.",
        "Неверные параметры печати TCP.", "Недійсні параметри друку TCP.", "TCP 打印选项无效。", "TCP 列印選項無效。",
    ),
    "Invalid entry in the file list": (
        "Ungültiger Eintrag in der Dateiliste", "Entrada no válida en la lista de archivos",
        "Entrée invalide dans la liste des fichiers", "Voce non valida nell'elenco dei file", "ファイルリストのエントリが無効です",
        "파일 목록에 잘못된 항목이 있습니다.", "Nieprawidłowy wpis na liście plików", "Entrada inválida na lista de arquivos",
        "Неверная запись в списке файлов.", "Неправильний запис у списку файлів", "文件列表中的条目无效", "文件清單中的項目無效",
    ),
    "Invalid notification settings.": (
        "Ungültige Benachrichtigungseinstellungen.", "Configuración de notificación no válida.",
        "Paramètres de notification invalides.", "Impostazioni di notifica non valide.", "通知設定が無効です。",
        "알림 설정이 잘못되었습니다.", "Nieprawidłowe ustawienia powiadomień.", "Configurações de notificação inválidas.",
        "Неверные настройки уведомлений.", "Недійсні налаштування сповіщень.", "通知设置无效。", "通知設定無效。",
    ),
    "Invalid print history format": (
        "Ungültiges Druckverlaufsformat", "Formato de historial de impresión no válido",
        "Format d'historique d'impression invalide", "Formato della cronologia di stampa non valido", "印刷履歴の形式が無効です",
        "잘못된 인쇄 기록 형식", "Nieprawidłowy format historii drukowania", "Formato de histórico de impressão inválido",
        "Неверный формат истории печати.", "Недійсний формат історії друку", "打印历史记录格式无效", "列印歷史記錄格式無效",
    ),
    "Invalid print job.": (
        "Ungültiger Druckauftrag.", "Trabajo de impresión no válido.", "Travail d'impression invalide.",
        "Lavoro di stampa non valido.", "無効な印刷ジョブです。", "잘못된 인쇄 작업입니다.", "Nieprawidłowe zadanie drukowania.",
        "Trabalho de impressão inválido.", "Недопустимое задание печати.", "Недійсне завдання друку.", "打印作业无效。",
        "列印作業無效。",
    ),
    "Invalid printer entry.": (
        "Ungültiger Druckereintrag.", "Entrada de impresora no válida.", "Entrée d'imprimante invalide.",
        "Voce stampante non valida.", "プリンタのエントリが無効です。", "잘못된 프린터 항목입니다.", "Nieprawidłowy wpis dotyczący drukarki.",
        "Entrada de impressora inválida.", "Некорректные данные принтера.", "Недійсний введений принтер.", "打印机输入无效。",
        "印表機輸入無效。",
    ),
    "Invalid printer file name.": (
        "Ungültiger Druckerdateiname.", "Nombre de archivo de impresora no válido.",
        "Nom de fichier d'imprimante invalide.", "Nome file stampante non valido.", "プリンタファイル名が不正です。",
        "프린터 파일 이름이 잘못되었습니다.", "Nieprawidłowa nazwa pliku drukarkowego.", "Nome de arquivo de impressora inválido.",
        "Недопустимое имя файла на принтере.", "Недійсне ім'я файлу принтера.", "打印机文件名无效。", "印表機檔案名稱無效。",
    ),
    "Invalid printer list.": (
        "Ungültige Druckerliste.", "Lista de impresoras no válidas.", "Liste d'imprimantes invalide.",
        "Elenco stampanti non valido.", "プリンターリストが無効です。", "잘못된 프린터 목록입니다.", "Nieprawidłowa lista drukarek.",
        "Lista de impressoras inválidas.", "Неверный список принтеров.", "Недійсний список принтерів.", "打印机列表无效。",
        "印表機清單無效。",
    ),
    "Invalid slice information.": (
        "Ungültige Slice-Informationen.", "Información de sector no válida.", "Informations de tranche invalides.",
        "Informazioni sulla sezione non valide.", "スライス情報が不正です。", "잘못된 슬라이스 정보입니다.",
        "Nieprawidłowe informacje o plasterku.", "Informações de fatia inválidas.", "Некорректные данные нарезки.",
        "Недійсна інформація зрізу.", "切片信息无效。", "切片資訊無效。",
    ),
    "Last reading: {seconds} s ago": (
        "Letzter Messwert vor {seconds} s", "Última lectura: hace {seconds} s", "Dernière mesure il y a {seconds} s",
        "Ultima lettura: {seconds} s fa", "最終更新：{seconds} 秒前", "마지막 측정: {seconds}초 전",
        "Ostatni odczyt: {seconds} s temu", "Última leitura: há {seconds} s", "Последнее показание: {seconds} с назад",
        "Останнє показання: {seconds} с тому", "最后更新：{seconds} 秒前", "最後更新：{seconds} 秒前",
    ),
    "Layer {layer}": (
        "Schicht {layer}", "Capa {layer}", "Couche {layer}", "Livello {layer}", "レイヤー{layer}", "레이어 {layer}",
        "Warstwa {layer}", "Camada {layer}", "Слой {layer}", "Шар {layer}", "层{layer}", "層{layer}",
    ),
    "Layer {layer} of {total}": (
        "Schicht {layer} von {total}", "Capa {layer} de {total}", "Couche {layer} sur {total}",
        "Strato {layer} di {total}", "レイヤー {layer} / {total}", "레이어 {layer} / {total}", "Warstwa {layer} z {total}",
        "Camada {layer} de {total}", "Слой {layer} из {total}", "Шар {layer} із {total}", "第 {layer} 层 / 共 {total} 层",
        "第 {layer} 層 / 共 {total} 層",
    ),
    "Light blue": (
        "Hellblau", "Azul claro", "Bleu clair", "Azzurro", "ライトブルー", "라이트 블루", "Jasnoniebieski", "Azul claro",
        "Голубой", "Світло-блакитний", "浅蓝色", "淺藍色",
    ),
    "Light gray": (
        "Hellgrau", "Gris claro", "Gris clair", "Grigio chiaro", "ライトグレー", "라이트 그레이", "Jasnoszary", "Cinza claro",
        "Светло-серый", "Світло-сірий", "浅灰色", "淺灰色",
    ),
    "Light green": (
        "Hellgrün", "Verde claro", "Vert clair", "Verde chiaro", "ライトグリーン", "라이트 그린", "Jasnozielony", "Verde claro",
        "Светло-зеленый", "Світло-зелений", "浅绿色", "淺綠色",
    ),
    "Linked materials loaded. Check that the stored file has not been replaced.": (
        "Verlinkte Materialien geladen. Stellen Sie sicher, dass die gespeicherte Datei nicht ersetzt wurde.",
        "Materiales vinculados cargados. Compruebe que el archivo almacenado no haya sido reemplazado.",
        "Matériaux liés chargés. Vérifiez que le fichier stocké n'a pas été remplacé.",
        "Materiali collegati caricati. Verificare che il file memorizzato non sia stato sostituito.",
        "リンク素材を読み込みました。保存されているファイルが置き換えられていないことを確認してください。", "링크된 자료를 불러왔습니다. 저장된 파일이 교체되지 않았는지 확인하세요.",
        "Załadowano połączone materiały. Sprawdź, czy zapisany plik nie został zastąpiony.",
        "Materiais vinculados carregados. Verifique se o arquivo armazenado não foi substituído.",
        "Сопоставление материалов загружено. Проверьте, что файл на принтере не заменён.",
        "Завантажено пов’язані матеріали. Переконайтеся, що збережений файл не було замінено.",
        "链接材料已加载。检查存储的文件是否未被替换。", "連結資料已載入。檢查儲存的檔案是否未被取代。",
    ),
    "Live camera · {printer}": (
        "Live-Kamera · {printer}", "Cámara en vivo · {printer}", "Caméra en direct · {printer}",
        "Telecamera live · {printer}", "ライブカメラ・{printer}", "라이브 카메라 · {printer}", "Kamera na żywo · {printer}",
        "Câmera ao vivo · {printer}", "Камера в прямом эфире · {printer}", "Камера в реальному часі · {printer}",
        "现场摄像头·{printer}", "現場攝影機·{printer}",
    ),
    "MJPEG stream URL (HTTP or HTTPS)": (
        "MJPEG-Stream-URL (HTTP oder HTTPS)", "URL de transmisión MJPEG (HTTP o HTTPS)",
        "URL du flux MJPEG (HTTP ou HTTPS)", "URL dello streaming MJPEG (HTTP o HTTPS)",
        "MJPEG ストリーム URL (HTTP または HTTPS)", "MJPEG 스트림 URL(HTTP 또는 HTTPS)",
        "Adres URL strumienia MJPEG (HTTP lub HTTPS)", "URL do fluxo MJPEG (HTTP ou HTTPS)",
        "URL-адрес потока MJPEG (HTTP или HTTPS)", "URL-адреса потоку MJPEG (HTTP або HTTPS)",
        "MJPEG 流 URL（HTTP 或 HTTPS）", "MJPEG 串流 URL（HTTP 或 HTTPS）",
    ),
    "Magenta": (
        "Magenta", "Magenta", "Magenta", "Magenta", "マゼンタ", "마젠타", "Magenta", "Magenta", "Пурпурный", "Маджента", "洋红色",
        "洋紅色",
    ),
    "Name": (
        "Name", "Nombre", "Nom", "Nome", "名前", "이름", "Imię", "Nome", "Имя", "Ім'я", "姓名", "姓名",
    ),
    "No camera frame received. Connect to try again.": (
        "Kein Kamerabild empfangen. Stellen Sie eine Verbindung her, um es erneut zu versuchen.",
        "No se recibió ningún cuadro de cámara. Conéctate para intentarlo de nuevo.",
        "Aucune image de caméra reçue. Connectez-vous pour réessayer.",
        "Nessun fotogramma ricevuto. Connettiti per riprovare.", "カメラフレームを受信しませんでした。接続して再試行してください。",
        "카메라 프레임이 수신되지 않았습니다. 다시 시도하려면 연결하세요.", "Nie odebrano ramki z kamery. Połącz się, aby spróbować ponownie.",
        "Nenhum quadro de câmera recebido. Conecte-se para tentar novamente.",
        "Не получен кадр с камеры. Подключитесь, чтобы повторить попытку.",
        "Кадр камери не отримано. Підключіться, щоб повторити спробу.", "未接收到相机帧。连接再试一次。", "未接收到相機訊框。連接再試一次。",
    ),
    "No frame received. Check the URL and confirm the stream uses MJPEG.": (
        "Kein Frame empfangen. Überprüfen Sie die URL und bestätigen Sie, dass der Stream MJPEG verwendet.",
        "No se recibió ninguna trama. Verifique la URL y confirme que la transmisión usa MJPEG.",
        "Aucune trame reçue. Vérifiez l'URL et confirmez que le flux utilise MJPEG.",
        "Nessun frame ricevuto. Controlla l'URL e conferma che lo stream utilizza MJPEG.",
        "フレームを受信できませんでした。 URL をチェックして、ストリームが MJPEG を使用していることを確認します。",
        "프레임이 수신되지 않았습니다. URL을 확인하고 스트림이 MJPEG를 사용하는지 확인하세요.",
        "Nie odebrano ramki. Sprawdź adres URL i potwierdź, że strumień używa MJPEG.",
        "Nenhum quadro recebido. Verifique o URL e confirme se o stream usa MJPEG.",
        "Кадр не получен. Проверьте URL-адрес и убедитесь, что поток использует MJPEG.",
        "Кадр не отримано. Перевірте URL-адресу та підтвердьте, що потік використовує MJPEG.",
        "未收到帧。检查 URL 并确认流使用 MJPEG。", "未收到訊框。檢查 URL 並確認串流使用 MJPEG。",
    ),
    "No matching jobs.": (
        "Keine passenden Druckaufträge.", "No hay trabajos de impresión que coincidan.",
        "Aucune tâche d’impression correspondante.", "Nessun lavoro di stampa corrispondente.", "一致する造形ジョブがありません。",
        "일치하는 출력 작업이 없습니다.", "Brak pasujących zadań drukowania.", "Nenhuma tarefa de impressão correspondente.",
        "Нет подходящих заданий.", "Відповідних завдань друку немає.", "没有符合条件的打印任务。", "沒有符合條件的列印工作。",
    ),
    "No print jobs recorded yet. Print history is recorded while OrcaSlicer is open.": (
        "Noch keine Druckaufträge erfasst. Der Druckverlauf wird aufgezeichnet, während OrcaSlicer geöffnet ist.",
        "Aún no se han registrado trabajos de impresión. El historial de impresión se registra mientras OrcaSlicer está abierto.",
        "Aucun travail d'impression enregistré pour le moment. L'historique d'impression est enregistré lorsque OrcaSlicer est ouvert.",
        "Nessun lavoro di stampa ancora registrato. La cronologia di stampa viene registrata mentre OrcaSlicer è aperto.",
        "印刷ジョブはまだ記録されていません。印刷履歴はOrcaSlicerを開いている間に記録されます。", "아직 기록된 인쇄 작업이 없습니다. OrcaSlicer가 열려 있는 동안 인쇄 기록이 기록됩니다.",
        "Nie zarejestrowano jeszcze żadnych zadań drukowania. Historia drukowania jest zapisywana, gdy OrcaSlicer jest otwarty.",
        "Nenhum trabalho de impressão registrado ainda. O histórico de impressão é registrado enquanto o OrcaSlicer está aberto.",
        "Заданий пока нет. История печати ведётся, пока открыт OrcaSlicer.",
        "Ще не зареєстровано завдань друку. Історія друку записується, коли OrcaSlicer відкрито.",
        "尚未记录打印作业。 OrcaSlicer 打开时会记录打印历史记录。", "尚未記錄列印作業。 OrcaSlicer 開啟時會記錄列印歷史記錄。",
    ),
    "No printers match your search and filters.": (
        "Keine Drucker entsprechen Ihrer Suche und Ihren Filtern.",
        "Ninguna impresora coincide con su búsqueda y filtros.",
        "Aucune imprimante ne correspond à votre recherche et à vos filtres.",
        "Nessuna stampante corrisponde alla ricerca e ai filtri.", "検索とフィルターに一致するプリンターはありません。",
        "검색 및 필터와 일치하는 프린터가 없습니다.", "Żadna drukarka nie spełnia kryteriów wyszukiwania i filtrów.",
        "Nenhuma impressora corresponde à sua pesquisa e filtros.",
        "Нет принтеров, соответствующих вашему запросу и фильтрам.",
        "Жоден принтер не відповідає вашому пошуку та фільтрам.", "没有打印机符合您的搜索和过滤器。", "沒有印表機符合您的搜尋和篩選器。",
    ),
    "No printers need attention.": (
        "Kein Drucker benötigt Aufmerksamkeit.", "Ninguna impresora necesita atención.",
        "Aucune imprimante n'a besoin d'attention.", "Nessuna stampante necessita di attenzione.",
        "プリンターは注意する必要がありません。", "주의가 필요한 프린터는 없습니다.", "Żadna drukarka nie wymaga uwagi.",
        "Nenhuma impressora precisa de atenção.", "Ни один принтер не требует внимания.",
        "Жоден принтер не потребує уваги.", "没有打印机需要注意。", "沒有印表機需要注意。",
    ),
    "No snapshot": (
        "Kein Schnappschuss", "Sin instantánea", "Aucun instantané", "Nessuna istantanea", "スナップショットなし", "스냅샷 없음",
        "Brak migawki", "Sem instantâneo", "Нет снимка", "Немає знімка", "无快照", "無快照",
    ),
    "Not recorded": (
        "Nicht erfasst", "No registrado", "Non enregistré", "Non registrato", "未収録", "녹음되지 않음", "Nie nagrano",
        "Não registrado", "Не записано", "Не записано", "未记录", "未記錄",
    ),
    "Nozzle Z": (
        "Z-Achse der Düse", "Eje Z de la boquilla", "Axe Z de la buse", "Asse Z dell’ugello", "ノズルのZ軸", "노즐 Z축",
        "Oś Z dyszy", "Eixo Z do bico", "Ось Z сопла", "Вісь Z сопла", "喷嘴 Z 轴", "噴嘴 Z 軸",
    ),
    "Nozzles": (
        "Düsen", "Boquillas", "Buses", "Ugelli", "ノズル", "노즐", "Dysze", "Bicos", "Сопла", "Сопла", "喷嘴", "噴嘴",
    ),
    "Nozzles: {count}": (
        "Düsen: {count}", "Boquillas: {count}", "Buses : {count}", "Ugelli: {count}", "ノズル: {count}", "노즐: {count}",
        "Dysze: {count}", "Bicos: {count}", "Сопел: {count}", "Насадки: {count}", "喷嘴：{count}", "噴嘴：{count}",
    ),
    "Offline for at least one minute": (
        "Mindestens eine Minute offline", "Sin conexión durante al menos un minuto",
        "Hors ligne pendant au moins une minute", "Offline per almeno un minuto", "1分以上オフライン", "1분 이상 오프라인",
        "Offline przez co najmniej jedną minutę", "Off-line por pelo menos um minuto",
        "Принтер не в сети больше минуты", "Не в мережі принаймні одну хвилину", "离线至少一分钟", "離線至少一分鐘",
    ),
    "One printer per line: address | CheckCode | name | serial number": (
        "Ein Drucker pro Zeile: Adresse | CheckCode | Name | Seriennummer",
        "Una impresora por línea: dirección | CheckCode | nombre | número de serie",
        "Une imprimante par ligne : adresse | CheckCode | nom | numéro de série",
        "Una stampante per riga: indirizzo | CheckCode | nome | numero di serie",
        "1 行に 1 台：アドレス | CheckCode | 名前 | シリアル番号", "한 줄에 하나의 프린터: 주소 | CheckCode | 이름 | 일련번호",
        "Jedna drukarka w wierszu: adres | CheckCode | imię | numer seryjny",
        "Uma impressora por linha: endereço | CheckCode | nome | número de série",
        "Один принтер на строку: адрес | CheckCode | имя | серийный номер",
        "Один принтер на рядок: адреса | CheckCode | назва | серійний номер", "每行一台打印机：地址| CheckCode |名称 |序列号",
        "每行一台印表機：位址| CheckCode |名稱 |序號",
    ),
    "Open FlashForge Device Tab for a FlashForge printer profile from Speed Dial.": (
        "Öffnen Sie FlashForge Device Tab für ein FlashForge-Druckerprofil über Speed Dial.",
        "Abra FlashForge Device Tab para obtener un perfil de impresora FlashForge desde Marcación rápida.",
        "Ouvrez FlashForge Device Tab pour un profil d'imprimante FlashForge à partir de Speed Dial.",
        "Aprire FlashForge Device Tab per un profilo stampante FlashForge da Selezione rapida.",
        "スピード ダイヤルから FlashForge プリンター プロファイルの FlashForge Device Tab を開きます。",
        "단축 다이얼에서 FlashForge 프린터 프로필에 대한 FlashForge Device Tab를 엽니다.",
        "Otwórz FlashForge Device Tab, aby wyświetlić profil drukarki FlashForge z szybkiego wybierania.",
        "Abra FlashForge Device Tab para um perfil de impressora FlashForge no Speed Dial.",
        "Выберите профиль принтера FlashForge и запустите FlashForge Device Tab из меню быстрого доступа.",
        "Відкрийте FlashForge Device Tab для профілю принтера FlashForge із швидкого набору.",
        "从快速拨号中打开FlashForge 打印机配置文件的FlashForge Device Tab。", "從快速撥號開啟FlashForge 印表機設定檔的FlashForge Device Tab。",
    ),
    "Open FlashForge Device Tab for the current printer profile from Speed Dial.": (
        "Öffnen Sie FlashForge Device Tab für das aktuelle Druckerprofil über Speed ​​Dial.",
        "Abra FlashForge Device Tab para el perfil de impresora actual desde Marcación rápida.",
        "Ouvrez FlashForge Device Tab pour le profil d'imprimante actuel à partir de la numérotation rapide.",
        "Aprire FlashForge Device Tab per il profilo stampante corrente da Selezione rapida.",
        "スピード ダイヤルから現在のプリンター プロファイルの FlashForge Device Tab を開きます。",
        "단축 다이얼에서 현재 프린터 프로필에 대한 FlashForge Device Tab를 엽니다.",
        "Otwórz FlashForge Device Tab dla bieżącego profilu drukarki z szybkiego wybierania.",
        "Abra FlashForge Device Tab para o perfil de impressora atual na Discagem Rápida.",
        "Запустите FlashForge Device Tab для текущего профиля из меню быстрого доступа.",
        "Відкрийте FlashForge Device Tab для поточного профілю принтера з швидкого набору.",
        "从快速拨号中打开当前打印机配置文件的FlashForge Device Tab。", "從快速撥號開啟目前印表機設定檔的FlashForge Device Tab。",
    ),
    "Open the FlashForge tab to see the monitor.": (
        "Öffnen Sie die Registerkarte FlashForge, um den Monitor anzuzeigen.",
        "Abra la pestaña FlashForge para ver el monitor.", "Ouvrez l'onglet FlashForge pour voir le moniteur.",
        "Aprire la scheda FlashForge per vedere il monitor.", "FlashForge タブを開いてモニターを表示します。",
        "FlashForge 탭을 열어 모니터를 확인하세요.", "Otwórz zakładkę FlashForge, aby zobaczyć monitor.",
        "Abra a guia FlashForge para ver o monitor.", "Монитор доступен на вкладке FlashForge.",
        "Відкрийте вкладку FlashForge, щоб побачити монітор.", "打开FlashForge 选项卡查看监视器。", "開啟FlashForge 標籤查看監視器。",
    ),
    "Open the monitor in OrcaSlicer.": (
        "Öffnen Sie den Monitor in OrcaSlicer.", "Abra el monitor en OrcaSlicer.",
        "Ouvrez le moniteur dans OrcaSlicer.", "Aprire il monitor in OrcaSlicer.", "OrcaSlicerでモニターを開きます。",
        "OrcaSlicer에서 모니터를 엽니다.", "Otwórz monitor w OrcaSlicer.", "Abra o monitor em OrcaSlicer.",
        "Откройте монитор в OrcaSlicer.", "Відкрийте монітор у OrcaSlicer.", "打开OrcaSlicer中的监视器。", "開啟OrcaSlicer中的監視器。",
    ),
    "Opened FlashForge Device Tab in the Printer tab.": (
        "FlashForge Device Tab auf der Registerkarte „Drucker“ geöffnet.",
        "Se abrió FlashForge Device Tab en la pestaña Impresora.",
        "Ouverture de FlashForge Device Tab dans l'onglet Imprimante.",
        "Aperto FlashForge Device Tab nella scheda Stampante.", "プリンタータブでFlashForge Device Tabを開きました。",
        "프린터 탭에서 FlashForge Device Tab를 열었습니다.", "Otwarto FlashForge Device Tab na karcie Drukarka.",
        "FlashForge Device Tab aberto na guia Impressora.", "FlashForge Device Tab открыт на вкладке «Принтер».",
        "Відкрито FlashForge Device Tab на вкладці «Принтер».", "在打印机选项卡中打开FlashForge Device Tab。",
        "在印表機標籤中開啟FlashForge Device Tab。",
    ),
    "Opening Creator 5 Pro camera…": (
        "Öffnen der Creator 5 Pro-Kamera…", "Abriendo la cámara Creator 5 Pro…",
        "Ouverture de la caméra Creator 5 Pro…", "Apertura della telecamera Creator 5 Pro…", "Creator 5 Pro カメラを開く…",
        "Creator 5 Pro 카메라 열기…", "Otwieranie kamery Creator 5 Pro…", "Abrindo a câmera Creator 5 Pro…",
        "Подключение камеры Creator 5 Pro…", "Відкриття камери Creator 5 Pro…", "打开Creator 5 Pro相机...",
        "開啟Creator 5 Pro相機...",
    ),
    "Other nozzles": (
        "Andere Düsen", "Otras boquillas", "Autres buses", "Altri ugelli", "その他のノズル", "기타 노즐", "Inne dysze",
        "Outros bicos", "Другие сопла", "Інші сопла", "其他喷嘴", "其他噴嘴",
    ),
    "Paused, disconnected or reporting a problem": (
        "Angehalten, getrennt oder ein Problem gemeldet", "Pausado, desconectado o reportando un problema",
        "En pause, déconnecté ou signalement d'un problème", "In pausa, disconnesso o segnalazione di un problema",
        "一時停止、切断、または問題の報告", "일시 중지, 연결 끊김 또는 문제 보고", "Wstrzymano, rozłączono lub zgłaszano problem",
        "Pausado, desconectado ou relatando um problema", "На паузе, без связи или с ошибкой",
        "Призупинено, відключено або повідомлено про проблему", "暂停、断开连接或报告问题", "暫停、斷開連線或回報問題",
    ),
    "Pink": (
        "Rosa", "Rosa", "Rose", "Rosa", "ピンク", "핑크", "Różowy", "Rosa", "Розовый", "Рожевий", "粉色", "粉紅色",
    ),
    "Preview of {file}": (
        "Vorschau von {file}", "Vista previa de {file}", "Aperçu de {file}", "Anteprima di {file}", "{file}のプレビュー",
        "{file} 미리보기", "Podgląd {file}", "Visualização de {file}", "Эскиз файла {file}", "Попередній перегляд {file}",
        "{file}预览", "{file}預覽",
    ),
    "Print history is recorded while OrcaSlicer is open.": (
        "Der Druckverlauf wird aufgezeichnet, während OrcaSlicer geöffnet ist.",
        "El historial de impresión se registra mientras OrcaSlicer está abierto.",
        "L'historique d'impression est enregistré lorsque OrcaSlicer est ouvert.",
        "La cronologia di stampa viene registrata mentre OrcaSlicer è aperto.", "OrcaSlicerを開いている間、印刷履歴が記録されます。",
        "OrcaSlicer가 열려 있는 동안 인쇄 이력이 기록됩니다.", "Historia drukowania jest zapisywana, gdy OrcaSlicer jest otwarty.",
        "O histórico de impressão é registrado enquanto o OrcaSlicer está aberto.",
        "История печати ведётся, пока открыт OrcaSlicer.", "Історія друку записується, коли OrcaSlicer відкрито.",
        "OrcaSlicer 打开时会记录打印历史记录。", "OrcaSlicer 開啟時會記錄列印歷史記錄。",
    ),
    "Print history is temporarily unavailable.": (
        "Der Druckverlauf ist vorübergehend nicht verfügbar.",
        "El historial de impresión no está disponible temporalmente.",
        "L'historique d'impression est temporairement indisponible.",
        "La cronologia di stampa è temporaneamente non disponibile.", "印刷履歴が一時的に利用できなくなります。",
        "인쇄 기록을 일시적으로 사용할 수 없습니다.", "Historia drukowania jest tymczasowo niedostępna.",
        "O histórico de impressão está temporariamente indisponível.", "История печати временно недоступна.",
        "Історія друку тимчасово недоступна.", "打印历史记录暂时不可用。", "列印歷史記錄暫時無法使用。",
    ),
    "Print success rate": (
        "Erfolgsquote", "Tasa de éxito", "Taux de réussite", "Tasso di riuscita", "造形成功率", "출력 성공률",
        "Odsetek udanych wydruków", "Taxa de sucesso", "Доля успешных заданий", "Частка успішних завдань", "打印成功率",
        "列印成功率",
    ),
    "Print time": (
        "Druckzeit", "Hora de impresión", "Temps d'impression", "Orario di stampa", "印刷時間", "인쇄 시간", "Czas drukowania",
        "Hora de impressão", "Время печати", "Час друку", "打印时间", "列印時間",
    ),
    "Print time: {time}": (
        "Druckzeit: {time}", "Hora de impresión: {time}", "Temps d'impression : {time}", "Orario di stampa: {time}",
        "印刷時間: {time}", "인쇄 시간: {time}", "Czas wydruku: {time}", "Tempo de impressão: {time}", "Время печати: {time}",
        "Час друку: {time}", "打印时间：{time}", "列印時間：{time}",
    ),
    "Printer camera feed": (
        "Bild der Druckerkamera", "Vídeo de la cámara de la impresora", "Vidéo de la caméra de l’imprimante",
        "Video della fotocamera della stampante", "プリンターのカメラ映像", "프린터 카메라 영상", "Obraz z kamery drukarki",
        "Vídeo da câmera da impressora", "Изображение с камеры принтера", "Зображення з камери принтера", "打印机摄像头画面",
        "印表機攝影機畫面",
    ),
    "Printer confirmed the nozzle target.": (
        "Der Drucker bestätigte das Düsenziel.", "La impresora confirmó el objetivo de la boquilla.",
        "L'imprimante a confirmé la cible de la buse.", "La stampante ha confermato il target dell'ugello.",
        "プリンターはノズルターゲットを確認しました。", "프린터가 노즐 대상을 확인했습니다.", "Drukarka potwierdziła cel dyszy.",
        "A impressora confirmou o alvo do bico.", "Принтер подтвердил заданную температуру сопла.",
        "Принтер підтвердив цільове сопло.", "打印机已确认喷嘴目标。", "印表機已確認噴嘴目標。",
    ),
    "Printer confirmed the shutdown timer setting.": (
        "Der Drucker hat die Einstellung des Abschalttimers bestätigt.",
        "La impresora confirmó la configuración del temporizador de apagado.",
        "L'imprimante a confirmé le réglage de la minuterie d'arrêt.",
        "La stampante ha confermato l'impostazione del timer di spegnimento.", "プリンターはシャットダウンタイマー設定を確認しました。",
        "프린터가 종료 타이머 설정을 확인했습니다.", "Drukarka potwierdziła ustawienie licznika czasu wyłączania.",
        "A impressora confirmou a configuração do temporizador de desligamento.",
        "Принтер подтвердил настройку таймера автовыключения.", "Принтер підтвердив налаштування таймера вимкнення.",
        "打印机确认关机定时器设置。", "印表機確認關機定​​時器設定。",
    ),
    "Printer confirmed {command} · {state}": (
        "Drucker bestätigt {command} · {state}", "Impresora confirmada {command} · {state}",
        "Imprimante confirmée {command} · {state}", "Stampante confermata {command} · {state}",
        "プリンター確認済み {command}・{state}", "프린터 확인 {command} · {state}", "Drukarka potwierdzona {command} · {state}",
        "Impressora confirmada {command} · {state}", "Принтер подтвердил команду «{command}» · {state}",
        "Принтер підтверджено {command} · {state}", "打印机确认{command}·{state}", "印表機確認{command}·{state}",
    ),
    "Printer confirmed · {state}": (
        "Drucker bestätigt · {state}", "Impresora confirmada · {state}", "Imprimante confirmée · {state}",
        "Stampante confermata · {state}", "プリンター確認済み・{state}", "프린터 확인됨 · {state}", "Drukarka potwierdzona · {state}",
        "Impressora confirmada · {state}", "Принтер подтвердил команду · {state}", "Принтер підтверджено · {state}",
        "打印机确认·{state}", "印表機確認·{state}",
    ),
    "Printer details": (
        "Druckerdetails", "Detalles de la impresora", "Détails de l'imprimante", "Dettagli della stampante", "プリンター詳細",
        "프린터 세부사항", "Szczegóły drukarki", "Detalhes da impressora", "Подробности о принтере", "Деталі принтера",
        "打印机详细信息", "印表機詳細信息",
    ),
    "Printer did not confirm {command}. Last reported state: {state}.": (
        "Drucker hat {command} nicht bestätigt. Letzter gemeldeter Status: {state}.",
        "La impresora no confirmó {command}. Último estado reportado: {state}.",
        "L'imprimante n'a pas confirmé {command}. Dernier état signalé : {state}.",
        "La stampante non ha confermato {command}. Ultimo stato segnalato: {state}.",
        "プリンターが{command}を確認できませんでした。最後に報告された状態: {state}。", "프린터가 {command}를 확인하지 못했습니다. 마지막으로 보고된 상태: {state}.",
        "Drukarka nie potwierdziła {command}. Ostatni zgłoszony stan: {state}.",
        "A impressora não confirmou {command}. Último estado relatado: {state}.",
        "Принтер не подтвердил команду «{command}». Последнее состояние: {state}.",
        "Принтер не підтвердив {command}. Останній зареєстрований стан: {state}.", "打印机未确认{command}。最后报告状态：{state}。",
        "印表機未確認{command}。最後報告狀態：{state}。",
    ),
    "Printer did not report the axis position.": (
        "Der Drucker hat die Achsenposition nicht gemeldet.", "La impresora no informó la posición del eje.",
        "L'imprimante n'a pas signalé la position de l'axe.", "La stampante non ha riportato la posizione dell'asse.",
        "プリンタが軸位置を報告しませんでした。", "프린터가 축 위치를 보고하지 않았습니다.", "Drukarka nie zgłosiła położenia osi.",
        "A impressora não informou a posição do eixo.", "Принтер не сообщил о положении оси.",
        "Принтер не повідомив про положення осі.", "打印机未报告轴位置。", "印表機未報告軸位置。",
    ),
    "Printer did not report the axis travel limits.": (
        "Der Drucker hat die Achsenweggrenzen nicht gemeldet.",
        "La impresora no informó los límites de recorrido del eje.",
        "L'imprimante n'a pas signalé les limites de déplacement de l'axe.",
        "La stampante non ha riportato i limiti di corsa dell'asse.", "プリンタは軸の移動制限を報告しませんでした。",
        "프린터가 축 이동 제한을 보고하지 않았습니다.", "Drukarka nie zgłosiła limitów ruchu osi.",
        "A impressora não relatou os limites de deslocamento do eixo.",
        "Принтер не сообщил о пределах перемещения оси.", "Принтер не повідомив про обмеження ходу осі.",
        "打印机未报告轴行程限制。", "印表機未報告軸行程限制。",
    ),
    "Printer has not confirmed the nozzle target. Check Printer Readings.": (
        "Der Drucker hat das Düsenziel nicht bestätigt. Überprüfen Sie die Druckerwerte.",
        "La impresora no ha confirmado el objetivo de la boquilla. Verifique las lecturas de la impresora.",
        "L'imprimante n'a pas confirmé la cible de la buse. Vérifiez les lectures de l'imprimante.",
        "La stampante non ha confermato il target dell'ugello. Controllare le letture della stampante.",
        "プリンターがノズルターゲットを確認していません。プリンターの測定値を確認します。", "프린터가 노즐 대상을 확인하지 않았습니다. 프린터 판독값을 확인하세요.",
        "Drukarka nie potwierdziła docelowej dyszy. Sprawdź odczyty drukarki.",
        "A impressora não confirmou o alvo do bico. Verifique as leituras da impressora.",
        "Принтер не подтвердил заданную температуру сопла. Проверьте показания принтера.",
        "Принтер не підтвердив цільове сопло. Перевірте показання принтера.", "打印机未确认喷嘴目标。检查打印机读数。",
        "印表機未確認噴嘴目標。檢查印表機讀數。",
    ),
    "Printer reports the error cleared.": (
        "Der Drucker meldet, dass der Fehler behoben wurde.", "La impresora informa que el error se borró.",
        "L'imprimante signale que l'erreur a été effacée.", "La stampante segnala che l'errore è stato risolto.",
        "プリンターはエラーが解消されたことを報告します。", "프린터에서 오류가 해결되었다고 보고합니다.", "Drukarka zgłasza, że ​​błąd został usunięty.",
        "A impressora relata que o erro foi resolvido.", "Принтер сообщает, что ошибка устранена.",
        "Принтер повідомляє про усунення помилки.", "打印机报告错误已清除。", "印表機報告錯誤已清除。",
    ),
    "Printer serial number unavailable": (
        "Druckerseriennummer nicht verfügbar", "Número de serie de la impresora no disponible",
        "Numéro de série de l'imprimante indisponible", "Numero di serie della stampante non disponibile",
        "プリンターのシリアル番号が利用できません", "프린터 일련번호를 알 수 없습니다.", "Numer seryjny drukarki jest niedostępny",
        "Número de série da impressora indisponível", "Серийный номер принтера недоступен",
        "Серійний номер принтера недоступний", "打印机序列号不可用", "印表機序號不可用",
    ),
    "Profile changed": (
        "Profil geändert", "Perfil cambiado", "Profil modifié", "Profilo cambiato", "プロフィール変更しました", "프로필이 변경되었습니다",
        "Profil został zmieniony", "Perfil alterado", "Профиль изменён", "Профіль змінено", "个人资料已更改", "個人資料已更改",
    ),
    "Purple": (
        "Lila", "Púrpura", "Violet", "Viola", "パープル", "퍼플", "Fioletowy", "Roxo", "Фиолетовый", "Фіолетовий", "紫色", "紫色",
    ),
    "Reading materials…": (
        "Materialdaten werden gelesen…", "Leyendo los datos de los materiales…", "Lecture des données de matériaux…",
        "Lettura dei dati dei materiali…", "材料データを読み込み中…", "재료 정보 읽는 중…", "Odczytywanie danych materiałów…",
        "Lendo dados dos materiais…", "Чтение данных о материалах…", "Зчитування даних про матеріали…", "正在读取耗材信息…",
        "正在讀取線材資料…",
    ),
    "Reading printer status…": (
        "Druckerstatus wird gelesen…", "Leyendo el estado de la impresora…", "Lecture de l'état de l'imprimante…",
        "Lettura dello stato della stampante…", "プリンターステータスを読み取り中…", "프린터 상태를 읽는 중…", "Odczytywanie stanu drukarki…",
        "Lendo o status da impressora…", "Чтение состояния принтера…", "Читання стану принтера…", "读取打印机状态...",
        "讀取印表機狀態...",
    ),
    "Red": (
        "Rot", "Rojo", "Rouge", "Rosso", "レッド", "레드", "Czerwony", "Vermelho", "Красный", "Червоний", "红色", "紅色",
    ),
    "Refresh the Creator printer's file list first.": (
        "Aktualisieren Sie zuerst die Dateiliste des Creator-Druckers.",
        "Primero actualice la lista de archivos de la impresora Creator.",
        "Actualisez d'abord la liste des fichiers de l'imprimante Creator.",
        "Aggiornare prima l'elenco dei file della stampante Creator.", "まずCreatorプリンターのファイルリストを更新してください。",
        "먼저 Creator 프린터의 파일 목록을 새로 고치세요.", "Najpierw odśwież listę plików drukarki w kreatorze.",
        "Atualize primeiro a lista de arquivos da impressora Creator.",
        "Сначала обновите список файлов принтера Creator.", "Спочатку оновіть список файлів принтера Creator.",
        "首先刷新Creator打印机的文件列表。", "先刷新Creator印表機的文件清單。",
    ),
    "Restored the printer's original web page.": (
        "Die ursprüngliche Webseite des Druckers wurde wiederhergestellt.",
        "Restauró la página web original de la impresora.", "Restauration de la page Web originale de l'imprimante.",
        "Ripristinata la pagina web originale della stampante.", "プリンターの元のWebページを復元しました。", "프린터의 원래 웹 페이지를 복원했습니다.",
        "Przywrócono oryginalną stronę internetową drukarki.", "Restaurou a página da Web original da impressora.",
        "Восстановлена исходная веб-страница принтера.", "Відновлено оригінальну веб-сторінку принтера.",
        "恢复了打印机的原始网页。", "恢復了印表機的原始網頁。",
    ),
    "Result": (
        "Ergebnis", "Resultado", "Résultat", "Risultato", "結果", "결과", "Wynik", "Resultado", "Результат", "Результат",
        "结果", "結果",
    ),
    "Result: {result}": (
        "Ergebnis: {result}", "Resultado: {result}", "Résultat : {result}", "Risultato: {result}", "結果：{result}",
        "결과: {result}", "Wynik: {result}", "Resultado: {result}", "Результат: {result}", "Результат: {result}",
        "结果：{result}", "結果：{result}",
    ),
    "Rows per page": (
        "Zeilen pro Seite", "Filas por página", "Lignes par page", "Righe per pagina", "ページあたりの行数", "페이지당 행 수",
        "Wierszy na stronę", "Linhas por página", "Строк на странице", "рядків на сторінці", "每页行数", "每頁行數",
    ),
    "Save a frame from the active camera view.": (
        "Speichern Sie ein Bild aus der aktiven Kameraansicht.", "Guarde un fotograma de la vista de cámara activa.",
        "Enregistrez une image à partir de la vue de caméra active.",
        "Salva un fotogramma dalla vista della telecamera attiva.", "アクティブなカメラビューからフレームを保存します。",
        "활성 카메라 보기에서 프레임을 저장합니다.", "Zapisz klatkę z aktywnego widoku kamery.",
        "Salve um quadro da visualização da câmera ativa.", "Сохраните кадр из открытой трансляции камеры.",
        "Збережіть кадр з активної камери.", "保存活动摄像机视图中的帧。", "儲存活動攝影機檢視中的畫面。",
    ),
    "Save and wait a few seconds: temperatures and progress appear here.": (
        "Speichern und einige Sekunden warten: Hier werden Temperaturen und Fortschritt angezeigt.",
        "Guarda y espera unos segundos: aquí aparecen las temperaturas y el progreso.",
        "Enregistrez et attendez quelques secondes : les températures et la progression apparaissent ici.",
        "Salva e attendi qualche secondo: qui compaiono le temperature e l'avanzamento.",
        "保存して数秒待ちます。ここに温度と進行状況が表示されます。", "저장하고 몇 초 기다리면 온도와 진행 상황이 여기에 표시됩니다.",
        "Zapisz i poczekaj kilka sekund: tutaj pojawią się temperatury i postęp.",
        "Salve e aguarde alguns segundos: as temperaturas e o progresso aparecem aqui.",
        "Сохраните настройки. Через несколько секунд здесь появятся температуры и ход печати.",
        "Збережіть і зачекайте кілька секунд: тут з’являться температури та прогрес.", "保存并等待几秒钟：此处显示温度和进度。",
        "儲存並等待幾秒鐘：此處顯示溫度和進度。",
    ),
    "Save this printer before checking the connection.": (
        "Speichern Sie diesen Drucker, bevor Sie die Verbindung überprüfen.",
        "Guarde esta impresora antes de comprobar la conexión.",
        "Enregistrez cette imprimante avant de vérifier la connexion.",
        "Salvare questa stampante prima di verificare la connessione.", "接続を確認する前に、このプリンターを保存してください。",
        "연결을 확인하기 전에 이 프린터를 저장하십시오.", "Zapisz tę drukarkę przed sprawdzeniem połączenia.",
        "Salve esta impressora antes de verificar a conexão.",
        "Сохраните настройки принтера перед проверкой подключения.",
        "Збережіть цей принтер перед перевіркою підключення.", "在检查连接之前保存本打印机。", "在檢查連線之前儲存本印表機。",
    ),
    "Save this setting, then restart OrcaSlicer to switch tabs.": (
        "Speichern Sie diese Einstellung und starten Sie dann OrcaSlicer neu, um die Registerkarten zu wechseln.",
        "Guarde esta configuración, luego reinicie OrcaSlicer para cambiar de pestaña.",
        "Enregistrez ce paramètre, puis redémarrez OrcaSlicer pour changer d'onglet.",
        "Salva questa impostazione, quindi riavvia OrcaSlicer per cambiare scheda.",
        "この設定を保存し、OrcaSlicerを再起動してタブを切り替えます。", "이 설정을 저장한 후 OrcaSlicer를 다시 시작하여 탭을 전환하세요.",
        "Zapisz to ustawienie, a następnie uruchom ponownie OrcaSlicer, aby przełączyć karty.",
        "Salve esta configuração e reinicie o OrcaSlicer para alternar entre guias.",
        "Сохраните этот параметр, затем перезапустите OrcaSlicer, чтобы переключить вкладки.",
        "Збережіть це налаштування, а потім перезапустіть OrcaSlicer, щоб переключити вкладки.",
        "保存此设置，然后重新启动OrcaSlicer即可切换标签。", "儲存此設置，然後重新啟動OrcaSlicer即可切換標籤。",
    ),
    "Saved. Restart OrcaSlicer to apply the change.": (
        "Gespeichert. Starten Sie OrcaSlicer neu, um die Änderung zu übernehmen.",
        "Guardado. Reinicie OrcaSlicer para aplicar el cambio.",
        "Enregistré. Redémarrez OrcaSlicer pour appliquer la modification.",
        "Salvato. Riavviare OrcaSlicer per applicare la modifica.", "保存されました。 OrcaSlicer を再起動して、変更を適用します。",
        "저장되었습니다. 변경 사항을 적용하려면 OrcaSlicer를 다시 시작하세요.", "Zapisano. Uruchom ponownie OrcaSlicer, aby zastosować zmianę.",
        "Salvo. Reinicie OrcaSlicer para aplicar a alteração.",
        "Сохранено. Перезапустите OrcaSlicer, чтобы применить изменения.",
        "Збережено. Перезапустіть OrcaSlicer, щоб застосувати зміни.", "已保存。重新启动 OrcaSlicer 以应用更改。",
        "已儲存。重新啟動 OrcaSlicer 以套用變更。",
    ),
    "Searching…": (
        "Suche…", "Buscando…", "Recherche…", "Ricerca…", "検索中…", "검색 중…", "Wyszukiwanie…", "Procurando…", "Поиск…",
        "Пошук…", "搜索...", "搜尋...",
    ),
    "Search…": (
        "Suchen…", "Buscar…", "Rechercher…", "Cerca…", "検索…", "검색…", "Szukaj…", "Pesquisar…", "Поиск…", "Пошук…",
        "搜索...", "搜尋...",
    ),
    "Select a printer profile that uses the FlashForge host.": (
        "Wählen Sie ein Druckerprofil aus, das den FlashForge-Host verwendet.",
        "Seleccione un perfil de impresora que utilice el host FlashForge.",
        "Sélectionnez un profil d'imprimante qui utilise l'hôte FlashForge.",
        "Selezionare un profilo stampante che utilizza l'host FlashForge.", "FlashForgeホストを使用するプリンタープロファイルを選択します。",
        "FlashForge 호스트를 사용하는 프린터 프로필을 선택합니다.", "Wybierz profil drukarki korzystający z hosta FlashForge.",
        "Selecione um perfil de impressora que use o host FlashForge.",
        "Выберите профиль принтера, использующий хост FlashForge.",
        "Виберіть профіль принтера, який використовує хост FlashForge.", "选择使用FlashForge 主机的打印机配置文件。",
        "選擇使用FlashForge 主機的印表機設定檔。",
    ),
    "Select a printer to add.": (
        "Wählen Sie einen Drucker zum Hinzufügen aus.", "Seleccione una impresora para agregar.",
        "Sélectionnez une imprimante à ajouter.", "Selezionare una stampante da aggiungere.", "追加するプリンターを選択します。",
        "추가할 프린터를 선택하세요.", "Wybierz drukarkę do dodania.", "Selecione uma impressora para adicionar.",
        "Выберите принтер для добавления.", "Виберіть принтер для додавання.", "选择要添加的打印机。", "選擇要新增的印表機。",
    ),
    "Select a stored file first.": (
        "Wählen Sie zunächst eine gespeicherte Datei aus.", "Seleccione primero un archivo almacenado.",
        "Sélectionnez d'abord un fichier enregistré.", "Selezionare prima un file memorizzato.",
        "最初に保存されているファイルを選択します。", "먼저 저장된 파일을 선택하세요.", "Najpierw wybierz zapisany plik.",
        "Selecione primeiro um arquivo armazenado.", "Сначала выберите сохраненный файл.",
        "Спочатку виберіть збережений файл.", "首先选择一个存储的文件。", "先選擇一個儲存的檔案。",
    ),
    "Selected": (
        "Ausgewählt", "Seleccionado", "Sélectionné", "Selezionato", "選択済み", "선정됨", "Wybrano", "Selecionado", "Выбрано",
        "Вибране", "已选择", "已選擇",
    ),
    "Selected · {file}": (
        "Ausgewählt · {file}", "Seleccionado · {file}", "Sélectionné · {file}", "Selezionato · {file}", "選択済み・{file}",
        "선정 · {file}", "Wybrano · {file}", "Selecionado · {file}", "Выбрано · {file}", "Вибране · {file}", "已选·{file}",
        "已選·{file}",
    ),
    "Sending request…": (
        "Anfrage wird gesendet…", "Enviando solicitud…", "Envoi de la demande…", "Invio richiesta…", "リクエストを送信中…",
        "요청을 보내는 중…", "Wysyłam żądanie…", "Enviando solicitação…", "Отправка запроса…", "Надсилання запиту…", "发送请求...",
        "發送請求...",
    ),
    "Setting light…": (
        "Beleuchtung wird umgeschaltet…", "Cambiando la iluminación…", "Commutation de l’éclairage…",
        "Commutazione della luce…", "照明を切り替え中…", "조명 전환 중…", "Przełączanie oświetlenia…", "Alternando a iluminação…",
        "Переключение подсветки…", "Перемикання підсвічування…", "正在切换照明…", "正在切換照明…",
    ),
    "Slot updated.": (
        "Steckplatz aktualisiert.", "Ranura actualizada.", "Emplacement mis à jour.", "Slot aggiornato.",
        "スロットを更新しました。", "슬롯이 업데이트되었습니다.", "Zaktualizowano gniazdo.", "Slot atualizado.", "Обновлен слот.",
        "Слот оновлено.", "插槽已更新。", "插槽已更新。",
    ),
    "Small": (
        "Klein", "Pequeño", "Petit", "Piccolo", "小", "소형", "Mały", "Pequeno", "Мелкий", "Маленький", "小", "小",
    ),
    "Started": (
        "Gestartet", "Iniciado", "Démarré", "Avviato", "始めました", "시작됨", "Rozpoczęto", "Iniciado", "Начало", "Почато",
        "开始", "開始",
    ),
    "Started: {time}": (
        "Gestartet: {time}", "Iniciado: {time}", "Démarré : {time}", "Iniziato: {time}", "始めました：{time}", "시작됨: {time}",
        "Rozpoczęto: {time}", "Iniciado: {time}", "Начало: {time}", "Розпочато: {time}", "开始：{time}", "開始：{time}",
    ),
    "Status: received": (
        "Status: empfangen", "Estado: recibido", "Statut : reçu", "Stato: ricevuto", "ステータス: 受信済み", "상태: 접수됨",
        "Status: otrzymano", "Status: recebido", "Состояние: данные получены", "Статус: отримано", "状态：已收到", "狀態：已收到",
    ),
    "Tan": (
        "Hellbraun", "Beige oscuro", "Beige foncé", "Beige scuro", "タン（淡褐色）", "황갈색", "Jasnobrązowy", "Bege-escuro",
        "Песочный", "Піщаний", "浅棕色", "淺棕色",
    ),
    "Teal": (
        "Blaugrün", "Verde azulado", "Sarcelle", "Verde acqua", "ティール", "틸", "Turkusowy", "Azul-petróleo", "Бирюзовый",
        "Бірюзовий", "青色", "青色",
    ),
    "Temperature change over the last 5 minutes": (
        "Temperaturänderung in den letzten 5 Minuten", "Cambio de temperatura en los últimos 5 minutos",
        "Changement de température au cours des 5 dernières minutes",
        "Variazione della temperatura negli ultimi 5 minuti", "過去5分間の気温変化", "지난 5분간의 온도 변화",
        "Zmiana temperatury w ciągu ostatnich 5 minut", "Mudança de temperatura nos últimos 5 minutos",
        "Изменение температуры за последние 5 минут", "Зміна температури за останні 5 хвилин", "最近5分钟温度变化", "最近5分鐘溫度變化",
    ),
    "The .3mf has invalid or unsupported filament assignments.": (
        "Der .3mf verfügt über ungültige oder nicht unterstützte Filamentzuweisungen.",
        "El .3mf tiene asignaciones de filamentos no válidas o no admitidas.",
        "Le .3mf a des affectations de filaments invalides ou non prises en charge.",
        ".3mf ha assegnazioni di filamenti non valide o non supportate.", ".3mf には無効またはサポートされていないフィラメント割り当てがあります。",
        ".3mf에 유효하지 않거나 지원되지 않는 필라멘트 할당이 있습니다.", ".3mf ma nieprawidłowe lub nieobsługiwane przypisania żarników.",
        "O .3mf possui atribuições de filamento inválidas ou não suportadas.",
        "В файле .3mf недопустимые или неподдерживаемые назначения материалов.",
        ".3mf має недійсні або непідтримувані призначення ниток.", ".3mf 的灯丝分配无效或不受支持。", ".3mf 的燈絲分配無效或不受支援。",
    ),
    "The .3mf transfer was interrupted.": (
        "Die .3mf-Übertragung wurde unterbrochen.", "La transferencia .3mf fue interrumpida.",
        "Le transfert .3mf a été interrompu.", "Il trasferimento .3mf è stato interrotto.", ".3mf転送が中断されました。",
        ".3mf 전송이 중단되었습니다.", "Przesyłanie .3mf zostało przerwane.", "A transferência .3mf foi interrompida.",
        "Передача .3mf была прервана.", "Передачу .3mf було перервано.", ".3mf 传输被中断。", ".3mf 傳輸被中斷。",
    ),
    "The active printer profile changed. Try again.": (
        "Das aktive Druckerprofil hat sich geändert. Versuchen Sie es erneut.",
        "El perfil de la impresora activa cambió. Intentar otra vez.",
        "Le profil d'imprimante actif a changé. Essayer à nouveau.",
        "Il profilo della stampante attiva è cambiato. Riprova.", "アクティブなプリンタープロファイルが変更されました。もう一度やり直してください。",
        "활성 프린터 프로필이 변경되었습니다. 다시 시도해 보세요.", "Zmienił się aktywny profil drukarki. Spróbuj ponownie.",
        "O perfil da impressora ativa foi alterado. Tente novamente.",
        "Активный профиль принтера изменен. Попробуйте еще раз.", "Активний профіль принтера змінено. Спробуйте знову.",
        "活动打印机配置文件已更改。再试一次。", "活動印表機設定檔已變更。再試一次。",
    ),
    "The end of this job was not observed.": (
        "Das Ende dieses Auftrags wurde nicht beobachtet.", "No se observó el final de este trabajo.",
        "La fin de ce travail n'a pas été observée.", "La fine di questo lavoro non è stata rispettata.",
        "このジョブの終了は観察されませんでした。", "이 작업의 종료가 관찰되지 않았습니다.", "Nie zaobserwowano końca tego zadania.",
        "O término deste trabalho não foi observado.", "Плагин не зафиксировал завершение этого задания.",
        "Кінець цієї роботи не спостерігався.", "未观察到该作业的结束。", "未觀察到該作業的結束。",
    ),
    "The file preview changed. Choose the original sliced .3mf again.": (
        "Die Dateivorschau hat sich geändert. Wählen Sie erneut das in Scheiben geschnittene Original-.3mf.",
        "La vista previa del archivo cambió. Elija nuevamente el .3mf en rodajas original.",
        "L'aperçu du fichier a changé. Choisissez à nouveau le .3mf tranché d'origine.",
        "L'anteprima del file è cambiata. Scegli di nuovo lo .3mf affettato originale.",
        "ファイルのプレビューが変更されました。元のスライスされた .3mf を再度選択します。", "파일 미리보기가 변경되었습니다. 원래 슬라이스 .3mf를 다시 선택하십시오.",
        "Podgląd pliku uległ zmianie. Wybierz ponownie oryginalny pokrojony .3mf.",
        "A visualização do arquivo foi alterada. Escolha o .3mf fatiado original novamente.",
        "Эскиз файла изменился. Снова выберите исходный .3mf с нарезкой.",
        "Попередній перегляд файлу змінено. Знову виберіть оригінальний нарізаний .3mf.", "文件预览发生变化。再次选择原来切片的.3mf。",
        "文件預覽發生變化。再次選擇原來切片的.3mf。",
    ),
    "The local file transfer was interrupted.": (
        "Die lokale Dateiübertragung wurde unterbrochen.", "La transferencia del archivo local fue interrumpida.",
        "Le transfert de fichiers locaux a été interrompu.", "Il trasferimento del file locale è stato interrotto.",
        "ローカルファイル転送が中断されました。", "로컬 파일 전송이 중단되었습니다.", "Lokalny transfer plików został przerwany.",
        "A transferência de arquivos locais foi interrompida.", "Локальная передача файлов была прервана.",
        "Локальну передачу файлу було перервано.", "本地文件传输中断。", "本地文件傳輸中斷。",
    ),
    "The monitor is in the Printer tab.": (
        "Der Monitor befindet sich auf der Registerkarte „Drucker“.", "El monitor está en la pestaña Impresora.",
        "Le moniteur se trouve dans l'onglet Imprimante.", "Il monitor si trova nella scheda Stampante.",
        "モニターは「プリンター」タブにあります。", "모니터는 프린터 탭에 있습니다.", "Monitor znajduje się na karcie Drukarka.",
        "O monitor está na guia Impressora.", "Монитор находится на вкладке «Принтер».",
        "Монітор знаходиться на вкладці «Принтер».", "监视器位于打印机选项卡中。", "監視器位於印表機標籤中。",
    ),
    "The new language appears when you reopen the monitor.": (
        "Die neue Sprache erscheint, wenn Sie den Monitor erneut öffnen.",
        "El nuevo idioma aparece cuando vuelve a abrir el monitor.",
        "La nouvelle langue apparaît lorsque vous rouvrez le moniteur.",
        "La nuova lingua viene visualizzata quando si riapre il monitor.", "モニターを再度開くと、新しい言語が表示されます。",
        "모니터를 다시 열면 새로운 언어가 나타납니다.", "Nowy język pojawi się po ponownym otwarciu monitora.",
        "O novo idioma aparece quando você reabre o monitor.", "Сохраните настройки и снова откройте монитор.",
        "Нова мова з’являється, коли ви повторно відкриваєте монітор.", "重新打开显示器时会出现新语言。", "重新開啟顯示器時會出現新語言。",
    ),
    "The printer HTTP API returned status {code}.": (
        "Die Drucker-HTTP-API hat den Status {code} zurückgegeben.",
        "La API HTTP de la impresora devolvió el estado {code}.",
        "L'API HTTP de l'imprimante a renvoyé le statut {code}.",
        "L'API HTTP della stampante ha restituito lo stato {code}.", "プリンター HTTP API がステータス {code} を返しました。",
        "프린터 HTTP API가 {code} 상태를 반환했습니다.", "Interfejs API HTTP drukarki zwrócił status {code}.",
        "A API HTTP da impressora retornou o status {code}.", "API HTTP принтера вернул статус {code}.",
        "API принтера HTTP повернув статус {code}.", "打印机HTTP API 返回状态{code}。", "印表機HTTP API 返回狀態{code}。",
    ),
    "The printer HTTP connection failed.": (
        "Die Verbindung zum Drucker HTTP ist fehlgeschlagen.", "Falló la conexión de la impresora HTTP.",
        "La connexion de l'imprimante HTTP a échoué.", "La connessione alla stampante HTTP non è riuscita.",
        "プリンターHTTP接続に失敗しました。", "프린터 HTTP 연결에 실패했습니다.", "Połączenie drukarki HTTP nie powiodło się.",
        "A conexão da impressora HTTP falhou.", "Не удалось подключиться к принтеру по HTTP.",
        "Збій підключення принтера HTTP.", "打印机HTTP 连接失败。", "印表機HTTP 連線失敗。",
    ),
    "The printer HTTP response is too large.": (
        "Die HTTP-Antwort des Druckers ist zu groß.", "La respuesta HTTP de la impresora es demasiado grande.",
        "La réponse HTTP de l'imprimante est trop grande.", "La risposta HTTP della stampante è troppo grande.",
        "プリンタのHTTPレスポンスが大きすぎます。", "프린터 HTTP 응답이 너무 큽니다.", "Odpowiedź drukarki HTTP jest zbyt duża.",
        "A resposta HTTP da impressora é muito grande.", "Ответ принтера HTTP слишком велик.",
        "Відповідь принтера HTTP надто велика.", "打印机HTTP响应太大。", "印表機HTTP反應太大。",
    ),
    "The printer address changed. Reopen FlashForge Device Tab for the current printer.": (
        "Die Druckeradresse hat sich geändert. Öffnen Sie FlashForge Device Tab erneut für den aktuellen Drucker.",
        "La dirección de la impresora cambió. Vuelva a abrir FlashForge Device Tab para la impresora actual.",
        "L'adresse de l'imprimante a changé. Rouvrez FlashForge Device Tab pour l’imprimante actuelle.",
        "L'indirizzo della stampante è cambiato. Riapri FlashForge Device Tab per la stampante corrente.",
        "プリンターアドレスが変更されました。現在のプリンターの FlashForge Device Tab を再度開きます。",
        "프린터 주소가 변경되었습니다. 현재 프린터에 대해 FlashForge Device Tab를 다시 엽니다.",
        "Adres drukarki uległ zmianie. Otwórz ponownie FlashForge Device Tab dla bieżącej drukarki.",
        "O endereço da impressora foi alterado. Abra novamente o FlashForge Device Tab para a impressora atual.",
        "Адрес принтера изменился. Снова откройте FlashForge Device Tab для текущего принтера.",
        "Адреса принтера змінена. Повторно відкрийте FlashForge Device Tab для поточного принтера.",
        "打印机地址改变。为当前打印机重新打开 FlashForge Device Tab。", "印表機位址改變。為目前印表機重新開啟 FlashForge Device Tab。",
    ),
    "The printer address in the plugin settings does not match the active profile.": (
        "Die Druckeradresse in den Plugin-Einstellungen stimmt nicht mit dem aktiven Profil überein.",
        "La dirección de la impresora en la configuración del complemento no coincide con el perfil activo.",
        "L'adresse de l'imprimante dans les paramètres du plugin ne correspond pas au profil actif.",
        "L'indirizzo della stampante nelle impostazioni del plug-in non corrisponde al profilo attivo.",
        "プラグイン設定のプリンターアドレスがアクティブなプロファイルと一致しません。", "플러그인 설정의 프린터 주소가 활성 프로필과 일치하지 않습니다.",
        "Adres drukarki w ustawieniach wtyczki nie odpowiada aktywnemu profilowi.",
        "O endereço da impressora nas configurações do plugin não corresponde ao perfil ativo.",
        "Адрес принтера в настройках плагина не соответствует активному профилю.",
        "Адреса принтера в налаштуваннях плагіна не відповідає активному профілю.", "插件设置中的打印机地址与活动配置文件不匹配。",
        "插件設定中的印表機位址與活動設定檔不符。",
    ),
    "The printer did not accept the command.": (
        "Der Drucker hat den Befehl nicht akzeptiert.", "La impresora no aceptó el comando.",
        "L'imprimante n'a pas accepté la commande.", "La stampante non ha accettato il comando.",
        "プリンターがコマンドを受け付けませんでした。", "프린터가 명령을 받아들이지 않았습니다.", "Drukarka nie przyjęła polecenia.",
        "A impressora não aceitou o comando.", "Принтер не принял команду.", "Принтер не прийняв команду.", "打印机不接受命令。",
        "印表機不接受指令。",
    ),
    "The printer did not finish sending data": (
        "Der Drucker hat das Senden der Daten nicht abgeschlossen", "La impresora no terminó de enviar datos",
        "L'imprimante n'a pas fini d'envoyer les données", "La stampante non ha terminato l'invio dei dati",
        "プリンターがデータの送信を完了しませんでした", "프린터에서 데이터 전송이 완료되지 않았습니다.", "Drukarka nie zakończyła wysyłania danych",
        "A impressora não concluiu o envio de dados", "Принтер не завершил отправку данных.",
        "Принтер не завершив надсилання даних", "打印机未完成数据发送", "印表機未完成資料傳送",
    ),
    "The printer did not report its serial number.": (
        "Der Drucker hat seine Seriennummer nicht gemeldet.", "La impresora no informó su número de serie.",
        "L'imprimante n'a pas indiqué son numéro de série.",
        "La stampante non ha riportato il proprio numero di serie.", "プリンターがシリアル番号を報告しませんでした。",
        "프린터가 일련 번호를 보고하지 않았습니다.", "Drukarka nie zgłosiła swojego numeru seryjnego.",
        "A impressora não informou seu número de série.", "Принтер не сообщил свой серийный номер.",
        "Принтер не повідомив свій серійний номер.", "打印机未报告其序列号。", "印表機未報告其序號。",
    ),
    "The printer did not report this slot.": (
        "Der Drucker hat diesen Steckplatz nicht gemeldet.", "La impresora no informó esta ranura.",
        "L'imprimante n'a pas signalé cet emplacement.", "La stampante non ha segnalato questo slot.",
        "プリンタはこのスロットを報告しませんでした。", "프린터가 이 슬롯을 보고하지 않았습니다.", "Drukarka nie zgłosiła tego gniazda.",
        "A impressora não relatou esse slot.", "Принтер не сообщил об этом слоте.",
        "Принтер не повідомив про цей слот.", "打印机未报告此插槽。", "印表機未回報此插槽。",
    ),
    "The printer did not respond or returned incomplete data": (
        "Der Drucker hat nicht reagiert oder unvollständige Daten zurückgegeben",
        "La impresora no respondió o devolvió datos incompletos",
        "L'imprimante n'a pas répondu ou a renvoyé des données incomplètes",
        "La stampante non ha risposto o ha restituito dati incompleti", "プリンターが応答しないか、不完全なデータを返しました",
        "프린터가 응답하지 않거나 불완전한 데이터를 반환했습니다.", "Drukarka nie odpowiedziała lub zwróciła niekompletne dane",
        "A impressora não respondeu ou retornou dados incompletos", "Принтер не ответил или вернул неполные данные.",
        "Принтер не відповідає або повертає неповні дані", "打印机没有响应或返回的数据不完整", "印表機沒有回應或回傳的資料不完整",
    ),
    "The printer has not reported the remaining time yet": (
        "Der Drucker hat die verbleibende Zeit noch nicht gemeldet",
        "La impresora aún no ha informado del tiempo restante", "L'imprimante n'a pas encore signalé le temps restant",
        "La stampante non ha ancora segnalato il tempo rimanente", "プリンターはまだ残り時間を報告していません",
        "프린터가 아직 남은 시간을 보고하지 않았습니다.", "Drukarka nie zgłosiła jeszcze pozostałego czasu",
        "A impressora ainda não informou o tempo restante", "Принтер еще не сообщил об оставшемся времени.",
        "Принтер ще не повідомив час, що залишився", "打印机尚未报告剩余时间", "印表機尚未報告剩餘時間",
    ),
    "The printer rejected the HTTP request (code {code}).": (
        "Der Drucker hat die HTTP-Anfrage abgelehnt (Code {code}).",
        "La impresora rechazó la solicitud HTTP (código {code}).",
        "L'imprimante a rejeté la demande HTTP (code {code}).",
        "La stampante ha rifiutato la richiesta HTTP (codice {code}).", "プリンターは HTTP 要求 (コード {code}) を拒否しました。",
        "프린터가 HTTP 요청(코드 {code})을 거부했습니다.", "Drukarka odrzuciła żądanie HTTP (kod {code}).",
        "A impressora rejeitou a solicitação HTTP (código {code}).", "Принтер отклонил запрос HTTP (код {code}).",
        "Принтер відхилив запит HTTP (код {code}).", "打印机拒绝HTTP 请求（代码{code}）。", "印表機拒絕HTTP 請求（代碼{code}）。",
    ),
    "The printer response is too large": (
        "Die Druckerantwort ist zu groß", "La respuesta de la impresora es demasiado grande",
        "La réponse de l'imprimante est trop importante", "La risposta della stampante è troppo ampia",
        "プリンターの応答が大きすぎます", "프린터 응답이 너무 큼", "Odpowiedź drukarki jest zbyt duża",
        "A resposta da impressora é muito grande", "Ответ принтера слишком большой",
        "Відповідь принтера занадто велика", "打印机响应太大", "印表機回應太大",
    ),
    "The printer returned an invalid file list.": (
        "Der Drucker hat eine Liste ungültiger Dateien zurückgegeben.",
        "La impresora devolvió una lista de archivos no válida.",
        "L'imprimante a renvoyé une liste de fichiers non valide.",
        "La stampante ha restituito un elenco di file non valido.", "プリンターが無効なファイルリストを返しました。",
        "프린터가 잘못된 파일 목록을 반환했습니다.", "Drukarka zwróciła nieprawidłową listę plików.",
        "A impressora retornou uma lista de arquivos inválida.", "Принтер вернул неверный список файлов.",
        "Принтер повернув недійсний список файлів.", "打印机返回无效的文件列表。", "印表機傳回無效的文件清單。",
    ),
    "The printer returned invalid HTTP data.": (
        "Der Drucker hat ungültige HTTP-Daten zurückgegeben.", "La impresora devolvió datos HTTP no válidos.",
        "L'imprimante a renvoyé des données HTTP non valides.", "La stampante ha restituito dati HTTP non validi.",
        "プリンターが無効な HTTP データを返しました。", "프린터가 잘못된 HTTP 데이터를 반환했습니다.", "Drukarka zwróciła nieprawidłowe dane HTTP.",
        "A impressora retornou dados HTTP inválidos.", "Принтер вернул неверные данные HTTP.",
        "Принтер повернув недійсні дані HTTP.", "打印机返回无效的HTTP 数据。", "印表機傳回無效的HTTP 資料。",
    ),
    "The printer returned invalid status data.": (
        "Der Drucker hat ungültige Statusdaten zurückgegeben.", "La impresora devolvió datos de estado no válidos.",
        "L'imprimante a renvoyé des données d'état non valides.",
        "La stampante ha restituito dati di stato non validi.", "プリンタが無効なステータス データを返しました。", "프린터가 잘못된 상태 데이터를 반환했습니다.",
        "Drukarka zwróciła nieprawidłowe dane o stanie.", "A impressora retornou dados de status inválidos.",
        "Принтер вернул неверные данные о состоянии.", "Принтер повернув недійсні дані про стан.", "打印机返回无效的状态数据。",
        "印表機傳回無效的狀態資料。",
    ),
    "The printer stopped sending data": (
        "Der Drucker hat aufgehört, Daten zu senden", "La impresora dejó de enviar datos",
        "L'imprimante a cessé d'envoyer des données", "La stampante ha smesso di inviare dati", "プリンターがデータを送信しなくなった",
        "프린터가 데이터 전송을 중지했습니다.", "Drukarka przestała wysyłać dane", "A impressora parou de enviar dados",
        "Принтер перестал отправлять данные.", "Принтер припинив надсилання даних", "打印机停止发送数据", "印表機停止傳送數據",
    ),
    "The saved preview changed. Choose the original sliced .3mf.": (
        "Die gespeicherte Vorschau hat sich geändert. Wählen Sie das original geschnittene .3mf.",
        "La vista previa guardada cambió. Elija el .3mf en rodajas original.",
        "L'aperçu enregistré a changé. Choisissez le .3mf tranché original.",
        "L'anteprima salvata è cambiata. Scegli l'originale affettato .3mf.",
        "保存されたプレビューが変更されました。オリジナルのスライスされた.3mfを選択してください。", "저장된 미리보기가 변경되었습니다. 원래 슬라이스 .3mf를 선택합니다.",
        "Zapisany podgląd został zmieniony. Wybierz oryginalny pokrojony .3mf.",
        "A visualização salva foi alterada. Escolha o .3mf fatiado original.",
        "Сохранённый эскиз изменился. Выберите исходный .3mf с нарезкой.",
        "Збережений попередній перегляд змінено. Вибирайте оригінальну нарізку .3mf.", "保存的预览发生变化。选择原始切片.3mf。",
        "儲存的預覽發生變化。選擇原始切片.3mf。",
    ),
    "The saved print history is damaged. New entries are available for this session.": (
        "Der gespeicherte Druckverlauf ist beschädigt. Für diese Sitzung sind neue Einträge verfügbar.",
        "El historial de impresión guardado está dañado. Hay nuevas entradas disponibles para esta sesión.",
        "L'historique d'impression enregistré est endommagé. De nouvelles entrées sont disponibles pour cette session.",
        "La cronologia di stampa salvata è danneggiata. Sono disponibili nuove voci per questa sessione.",
        "保存されている印刷履歴が破損しています。このセッションでは新しいエントリが利用可能です。", "저장된 인쇄 기록이 손상되었습니다. 이 세션에 새 항목을 사용할 수 있습니다.",
        "Zapisana historia drukowania jest uszkodzona. W tej sesji dostępne są nowe wpisy.",
        "O histórico de impressão salvo está danificado. Novas entradas estão disponíveis para esta sessão.",
        "Сохранённая история печати повреждена. Новые записи доступны до конца сеанса.",
        "Збережену історію друку пошкоджено. Для цього сеансу доступні нові записи.", "保存的打印历史记录已损坏。本次会议有新条目可供使用。",
        "儲存的列印記錄已損壞。本次會議有新條目可供使用。",
    ),
    "The slice information is too large.": (
        "Die Slice-Informationen sind zu groß.", "La información del sector es demasiado grande.",
        "Les informations de tranche sont trop volumineuses.", "Le informazioni sulla sezione sono troppo grandi.",
        "スライス情報が大きすぎます。", "슬라이스 정보가 너무 큽니다.", "Informacje o plasterku są za duże.",
        "As informações da fatia são muito grandes.", "Данные нарезки слишком велики.",
        "Інформація про фрагмент завелика.", "切片信息过大。", "切片資訊過大。",
    ),
    "This printer is not in the plugin settings.": (
        "Dieser Drucker ist nicht in den Plugin-Einstellungen enthalten.",
        "Esta impresora no está en la configuración del complemento.",
        "Cette imprimante n'est pas dans les paramètres du plugin.",
        "Questa stampante non è presente nelle impostazioni del plugin.", "このプリンターはプラグイン設定にありません。",
        "이 프린터는 플러그인 설정에 없습니다.", "Ta drukarka nie jest widoczna w ustawieniach wtyczki.",
        "Esta impressora não está nas configurações do plugin.", "Этого принтера нет в настройках плагина.",
        "Цей принтер відсутній у налаштуваннях плагіна.", "该打印机不在插件设置中。", "此印表機不在插件設定中。",
    ),
    "Total pages: {count}": (
        "Gesamtseiten: {count}", "Total de páginas: {count}", "Nombre total de pages : {count}",
        "Pagine totali: {count}", "総ページ数：{count}", "총 페이지 수: {count}", "Całkowita liczba stron: {count}",
        "Total de páginas: {count}", "Всего страниц: {count}", "Всього сторінок: {count}", "总页数：{count}", "總頁數：{count}",
    ),
    "Totals": (
        "Gesamtsummen", "Totales", "Totaux", "Totali", "合計", "합계", "Sumy", "Totais", "Итоги", "Підсумки", "总计", "總計",
    ),
    "Transport: {transport}": (
        "Protokoll: {transport}", "Protocolo: {transport}", "Protocole : {transport}", "Protocollo: {transport}",
        "プロトコル：{transport}", "프로토콜: {transport}", "Protokół: {transport}", "Protocolo: {transport}",
        "Протокол: {transport}", "Протокол: {transport}", "协议：{transport}", "通訊協定：{transport}",
    ),
    "Trend unavailable": (
        "Trend nicht verfügbar", "Tendencia no disponible", "Tendance indisponible", "Tendenza non disponibile",
        "トレンドが利用できません", "트렌드 확인 불가", "Trend niedostępny", "Tendência indisponível",
        "Нет данных об изменении температуры", "Тренд недоступний", "趋势不可用", "趨勢不可用",
    ),
    "Unexpected response from the printer": (
        "Unerwartete Antwort vom Drucker", "Respuesta inesperada de la impresora", "Réponse inattendue de l'imprimante",
        "Risposta imprevista dalla stampante", "プリンターからの予期せぬ応答", "프린터에서 예상치 못한 응답이 발생했습니다.",
        "Nieoczekiwana reakcja drukarki", "Resposta inesperada da impressora", "Неожиданный ответ принтера.",
        "Неочікувана відповідь від принтера", "打印机出现意外响应", "印表機出現意外回應",
    ),
    "Unknown Result": (
        "Unbekanntes Ergebnis", "Resultado desconocido", "Résultat inconnu", "Risultato sconosciuto", "結果不明",
        "알 수 없는 결과", "Nieznany wynik", "Resultado Desconhecido", "Результат неизвестен", "Невідомий результат", "未知结果",
        "未知結果",
    ),
    "Unknown filename": (
        "Unbekannter Dateiname", "Nombre de archivo desconocido", "Nom de fichier inconnu", "Nome file sconosciuto",
        "不明なファイル名", "파일명을 알 수 없음", "Nieznana nazwa pliku", "Nome de arquivo desconhecido", "Неизвестное имя файла",
        "Невідоме ім'я файлу", "未知的文件名", "未知的檔案名",
    ),
    "Unknown printer command.": (
        "Unbekannter Druckerbefehl.", "Comando de impresora desconocido.", "Commande d'imprimante inconnue.",
        "Comando stampante sconosciuto.", "不明なプリンターコマンドです。", "알 수 없는 프린터 명령입니다.", "Nieznane polecenie drukarki.",
        "Comando de impressora desconhecido.", "Неизвестная команда принтера.", "Невідома команда принтера.",
        "未知的打印机命令。", "未知的印表機指令。",
    ),
    "Unrecognized file list format": (
        "Unbekanntes Dateilistenformat", "Formato de lista de archivos no reconocido",
        "Format de liste de fichiers non reconnu", "Formato elenco file non riconosciuto", "認識できないファイルリスト形式",
        "인식할 수 없는 파일 목록 형식", "Nierozpoznany format listy plików", "Formato de lista de arquivos não reconhecido",
        "Нераспознанный формат списка файлов.", "Нерозпізнаний формат списку файлів", "无法识别的文件列表格式", "無法辨識的文件清單格式",
    ),
    "Unused": (
        "Unbenutzt", "Sin usar", "Inutilisé", "Inutilizzato", "未使用", "미사용", "Nieużywane", "Não utilizado",
        "Не используется", "Не використовується", "未使用", "未使用",
    ),
    "Upload progress": (
        "Upload-Fortschritt", "Progreso de carga", "Progression du téléchargement", "Caricamento progressi",
        "アップロードの進行状況", "업로드 진행", "Postęp przesyłania", "Progresso do upload", "Ход загрузки", "Хід завантаження",
        "上传进度", "上傳進度",
    ),
    "Uploaded · {file}": (
        "Hochgeladen · {file}", "Subido · {file}", "Téléchargé · {file}", "Caricato · {file}", "アップロードしました・{file}",
        "업로드됨 · {file}", "Przesłano · {file}", "Carregado · {file}", "Загружено · {file}", "Завантажено · {file}",
        "已上传·{file}", "已上傳·{file}",
    ),
    "Use A–Z, digits, spaces, dots, underscores or hyphens and a .gcode or .3mf extension.": (
        "Verwenden Sie A–Z, Ziffern, Leerzeichen, Punkte, Unterstriche oder Bindestriche und eine .gcode- oder .3mf-Erweiterung.",
        "Utilice A–Z, dígitos, espacios, puntos, guiones bajos o guiones y una extensión .gcode o .3mf.",
        "Utilisez A–Z, des chiffres, des espaces, des points, des traits de soulignement ou des traits d'union et une extension .gcode ou .3mf.",
        "Utilizzare A–Z, cifre, spazi, punti, caratteri di sottolineatura o trattini e un'estensione .gcode o .3mf.",
        "A ～ Z、数字、スペース、ドット、アンダースコア、またはハイフン、および .gcode または .3mf 拡張子を使用します。",
        "A–Z, 숫자, 공백, 점, 밑줄, 하이픈 및 .gcode 또는 .3mf 확장자를 사용하세요.",
        "Użyj A–Z, cyfr, spacji, kropek, podkreśleń lub łączników oraz rozszerzenia .gcode lub .3mf.",
        "Use A–Z, dígitos, espaços, pontos, sublinhados ou hifens e uma extensão .gcode ou .3mf.",
        "Используйте буквы A–Z, цифры, пробелы, точки, подчеркивания или дефисы, а также расширение .gcode или .3mf.",
        "Використовуйте A–Z, цифри, пробіли, крапки, підкреслення або дефіси та розширення .gcode або .3mf.",
        "使用A–Z、数字、空格、点、下划线或连字符以及.gcode 或.3mf 扩展名。", "使用A–Z、數字、空格、點、底線或連字號以及.gcode 或.3mf 副檔名。",
    ),
    "Use a .gcode or sliced .3mf name with A–Z, digits, spaces, dots, underscores or hyphens.": (
        "Verwenden Sie einen .gcode- oder segmentierten .3mf-Namen mit A–Z, Ziffern, Leerzeichen, Punkten, Unterstrichen oder Bindestrichen.",
        "Utilice un nombre .gcode o .3mf dividido con A–Z, dígitos, espacios, puntos, guiones bajos o guiones.",
        "Utilisez un nom .gcode ou .3mf découpé avec A à Z, des chiffres, des espaces, des points, des traits de soulignement ou des traits d'union.",
        "Utilizza un nome .gcode o .3mf suddiviso con A–Z, cifre, spazi, punti, trattini bassi o trattini.",
        "A ～ Z、数字、スペース、ドット、アンダースコア、またはハイフンを使用した .gcode またはスライスされた .3mf 名を使用します。",
        "A–Z, 숫자, 공백, 점, 밑줄 또는 하이픈이 포함된 .gcode 또는 분할된 .3mf 이름을 사용하십시오.",
        "Użyj nazwy .gcode lub pokrojonej nazwy .3mf zawierającej A–Z, cyfry, spacje, kropki, podkreślenia lub łączniki.",
        "Use um nome .gcode ou .3mf fatiado com A–Z, dígitos, espaços, pontos, sublinhados ou hifens.",
        "В имени файла .gcode или .3mf с нарезкой допустимы буквы A–Z, цифры, пробелы, точки, подчёркивания и дефисы.",
        "Використовуйте назву .gcode або фрагментовану назву .3mf з A–Z, цифрами, пробілами, крапками, підкресленням або дефісом.",
        "使用包含A-Z、数字、空格、点、下划线或连字符的.gcode 或切片.3mf 名称。", "使用包含A-Z、數字、空格、點、底線或連字符的.gcode 或切片.3mf 名稱。",
    ),
    "Use a JSON array for values containing a vertical bar.": (
        "Verwenden Sie ein JSON-Array für Werte, die einen vertikalen Balken enthalten.",
        "Utilice una matriz JSON para valores que contengan una barra vertical.",
        "Utilisez un tableau JSON pour les valeurs contenant une barre verticale.",
        "Utilizza un array JSON per valori contenenti una barra verticale.", "縦棒を含む値には JSON 配列を使用します。",
        "수직 막대가 포함된 값에는 JSON 배열을 사용하세요.", "Użyj tablicy JSON dla wartości zawierających pionową kreskę.",
        "Use uma matriz JSON para valores contendo uma barra vertical.",
        "Используйте массив JSON для значений, содержащих вертикальную черту.",
        "Використовуйте масив JSON для значень, що містять вертикальну смугу.", "对包含竖线的值使用JSON 数组。",
        "對包含垂直線的值使用JSON 陣列。",
    ),
    "Use a JSON array of four strings when a value contains a vertical bar.": (
        "Verwenden Sie ein JSON-Array aus vier Zeichenfolgen, wenn ein Wert einen vertikalen Balken enthält.",
        "Utilice una matriz JSON de cuatro cadenas cuando un valor contenga una barra vertical.",
        "Utilisez un tableau JSON de quatre chaînes lorsqu'une valeur contient une barre verticale.",
        "Utilizza un array JSON di quattro stringhe quando un valore contiene una barra verticale.",
        "値に縦棒が含まれる場合は、4 つの文字列の JSON 配列を使用します。", "값에 수직 막대가 포함된 경우 4개의 문자열로 구성된 JSON 배열을 사용합니다.",
        "Użyj tablicy JSON składającej się z czterech ciągów, jeśli wartość zawiera pionową kreskę.",
        "Use uma matriz JSON de quatro strings quando um valor contiver uma barra vertical.",
        "Используйте массив JSON из четырех строк, если значение содержит вертикальную черту.",
        "Використовуйте масив JSON із чотирьох рядків, якщо значення містить вертикальну риску.",
        "当值包含竖线时，使用由四个字符串组成的JSON 数组。", "當值包含垂直線時，使用由四個字串組成的JSON 陣列。",
    ),
    "Violet": (
        "Violett", "Violeta", "Violette", "Viola", "バイオレット", "바이올렛", "Fioletowy", "Violeta", "Фиолетовый", "Фіалка",
        "紫罗兰色", "紫羅蘭色",
    ),
    "Wait for the printer to reconnect with the new settings.": (
        "Warten Sie, bis der Drucker die Verbindung mit den neuen Einstellungen wiederhergestellt hat.",
        "Espere a que la impresora se vuelva a conectar con la nueva configuración.",
        "Attendez que l'imprimante se reconnecte avec les nouveaux paramètres.",
        "Attendere che la stampante si riconnetta con le nuove impostazioni.", "プリンターが新しい設定で再接続されるまで待ちます。",
        "프린터가 새로운 설정으로 다시 연결될 때까지 기다립니다.", "Poczekaj, aż drukarka ponownie połączy się z nowymi ustawieniami.",
        "Aguarde a impressora se reconectar com as novas configurações.",
        "Подождите, пока принтер повторно подключится с новыми настройками.",
        "Зачекайте, поки принтер відновить підключення з новими параметрами.", "等待打印机使用新设置重新连接。", "等待印表機使用新設定重新連線。",
    ),
    "Waiting for job status": (
        "Warten auf Auftragsstatus", "Esperando el estado del trabajo", "En attente du statut du travail",
        "In attesa dello stato del lavoro", "ジョブ待ち状態", "작업 상태 대기 중", "Oczekiwanie na status zadania",
        "Aguardando status do trabalho", "Ожидание статуса задания", "Очікування статусу завдання", "等待作业状态", "等待作業狀態",
    ),
    "Waiting for printer…": (
        "Warten auf Drucker…", "Esperando la impresora…", "En attente de l'imprimante…", "In attesa della stampante…",
        "プリンターを待っています…", "프린터를 기다리는 중…", "Oczekiwanie na drukarkę…", "Aguardando impressora…", "Ожидание принтера…",
        "Очікування принтера…", "等待打印机...", "等待印表機...",
    ),
    "Waiting for readings": (
        "Warten auf Messwerte", "Esperando las mediciones", "En attente des mesures", "In attesa delle misurazioni",
        "測定値を待機中", "측정값 대기 중", "Oczekiwanie na odczyty", "Aguardando medições", "Ожидание показаний",
        "Очікування показань", "正在等待读数", "正在等候讀數",
    ),
    "Waiting for temperature readings": (
        "Warten auf Temperaturmesswerte", "Esperando lecturas de temperatura", "En attente des relevés de température",
        "In attesa delle letture della temperatura", "検温待ち", "온도 판독 대기 중", "Oczekiwanie na odczyty temperatury",
        "Aguardando leituras de temperatura", "Ожидание показаний температуры", "Очікування показань температури",
        "等待温度读数", "等待溫度讀數",
    ),
    "Waiting for the printer to confirm calibration.": (
        "Warten auf die Bestätigung der Kalibrierung durch den Drucker.",
        "Esperando a que la impresora confirme la calibración.", "Attendre que l'imprimante confirme l'étalonnage.",
        "In attesa che la stampante confermi la calibrazione.", "プリンターがキャリブレーションを確認するのを待っています。",
        "프린터가 교정을 확인하기를 기다리는 중입니다.", "Oczekiwanie, aż drukarka potwierdzi kalibrację.",
        "Aguardando a impressora confirmar a calibração.", "Ожидание подтверждения калибровки принтером.",
        "Очікування підтвердження калібрування від принтера.", "等待打印机确认校准。", "等待印表機確認校準。",
    ),
    "Waiting for the printer…": (
        "Warten auf den Drucker…", "Esperando la impresora…", "En attente de l'imprimante…",
        "In attesa della stampante…", "プリンターを待っています…", "프린터를 기다리는 중…", "Oczekiwanie na drukarkę…",
        "Aguardando a impressora…", "Ожидание принтера…", "Очікування принтера…", "等待打印机...", "等待印表機...",
    ),
    "White": (
        "Weiß", "Blanco", "Blanc", "Bianco", "ホワイト", "화이트", "Biały", "Branco", "Белый", "Білий", "白色", "白色",
    ),
    "Yellow": (
        "Gelb", "Amarillo", "Jaune", "Giallo", "イエロー", "옐로우", "Żółty", "Amarelo", "Желтый", "Жовтий", "黄色", "黃色",
    ),
    "available": (
        "verfügbar", "disponible", "disponible", "disponibili", "あり", "있음", "dostępne", "disponível", "доступно",
        "доступно", "可用", "可用",
    ),
    "not reported": (
        "nicht gemeldet", "no reportado", "non signalé", "non riportato", "未報告", "보고되지 않음", "nie zgłoszono",
        "não relatado", "не получен", "не повідомляється", "未报道", "未報道",
    ),
    "reported; stream not tested": (
        "gemeldet; Stream nicht getestet", "reportado; flujo no probado", "signalés ; flux non testé",
        "segnalati; flusso non testato", "報告。ストリームはテストされていません", "보고됨; 테스트되지 않은 스트림",
        "zgłoszono; strumień nie testowany", "relatado; fluxo não testado", "получен, трансляция не проверялась",
        "повідомлено; потік не перевірено", "报告；流未测试", "報告；流未測試",
    ),
    "unavailable": (
        "nicht verfügbar", "no disponible", "indisponible", "non disponibile", "利用不可", "이용 불가", "niedostępne",
        "indisponível", "недоступно", "недоступний", "不可用", "不可用",
    ),
    "{command} · Accepted": (
        "{command} · Akzeptiert", "{command} · Aceptado", "{command} · Accepté", "{command} · Accettato", "{command}・可",
        "{command} · 수락됨", "{command} · Zaakceptowano", "{command} · Aceito", "{command} · Принято",
        "{command} · Прийнято", "{command}·已接受", "{command}·已接受",
    ),
    "{time} left": (
        "Noch {time}", "Quedan {time}", "Reste {time}", "Tempo rimasto: {time}", "残り {time}", "남은 시간: {time}",
        "Pozostało {time}", "Tempo restante: {time}", "Осталось {time}", "Залишилося {time}", "剩余 {time}", "剩餘 {time}",
    ),
}

TRANSLATIONS = {locale: {} for locale in TRANSLATION_LOCALES}
for messages, locales in ((CORE_MESSAGES, TRANSLATION_LOCALES),
                          (EXTENDED_MESSAGES, FULL_TRANSLATION_LOCALES)):
    for message, values in messages.items():
        for locale, value in zip(locales, values):
            TRANSLATIONS[locale][message] = value


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

