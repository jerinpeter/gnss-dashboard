const $ = (id) => document.getElementById(id);

const STATUS_CLASS = { good: "badge-good", warning: "badge-warning", serious: "badge-serious", critical: "badge-critical", neutral: "badge-neutral" };

function setBadge(el, text, status) {
  el.textContent = text;
  el.className = "badge badge-lg " + (STATUS_CLASS[status] || STATUS_CLASS.neutral);
}

function fmtDeg(v, dp = 6) { return typeof v === "number" ? v.toFixed(dp) + "°" : "–"; }
function fmtM(v, dp = 2) { return typeof v === "number" ? v.toFixed(dp) + " m" : "–"; }
function fmtDuration(s) {
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + "h " : "") + (h || m ? String(m).padStart(2, "0") + "m " : "") + String(sec).padStart(2, "0") + "s";
}

function fixStatusClass(fix) {
  if (!fix.gnssFixOk) return "critical";
  if (fix.carrSoln === 2) return "good";
  if (fix.carrSoln === 1) return "warning";
  if (fix.fixType === 5) return "good";
  if (fix.diffSoln) return "warning";
  if (fix.fixType === 3) return "serious";
  if (fix.fixType === 2) return "serious";
  return "critical";
}

const UTC_STANDARDS = { 0: "Unknown", 1: "CRL", 2: "NIST", 3: "USNO", 4: "BIPM", 5: "EU", 6: "SU", 7: "NTSC" };

function render(state) {
  const connPill = $("connPill");
  connPill.className = "pill " + (state.connected ? "pill-good" : "pill-critical");
  connPill.innerHTML = '<span class="dot"></span>' + (state.connected ? "LIVE" : "OFFLINE");
  $("uptime").textContent = fmtDuration(state.uptime);

  if (state.receiver && state.receiver.hwVersion) {
    const exts = (state.receiver.extensions || []).filter(e => /MOD=|FWVER=|PROTVER=/.test(e)).join(" · ");
    $("receiverLine").textContent = `HW ${state.receiver.hwVersion} · ${exts || state.receiver.swVersion}`;
    $("fwInfo").textContent = (state.receiver.extensions || []).join(" · ") || state.receiver.swVersion;
  } else if (!state.connected) {
    $("receiverLine").textContent = state.port_error || "waiting for device…";
  }

  // Fix
  const fix = state.fix || {};
  if (fix.fixTypeName) {
    let label = fix.fixTypeName;
    if (fix.carrSoln) label += " · " + fix.carrSolnName;
    setBadge($("fixBadge"), label, fixStatusClass(fix));
  }
  $("lat").textContent = fmtDeg(fix.lat);
  $("lon").textContent = fmtDeg(fix.lon);
  $("height").textContent = fmtM(fix.hMSL_m);
  $("hAcc").textContent = fmtM(fix.hAcc_m);
  $("vAcc").textContent = fmtM(fix.vAcc_m);
  $("numSV").textContent = fix.numSV ?? "–";
  $("speed").textContent = typeof fix.gSpeed_kmh === "number" ? fix.gSpeed_kmh.toFixed(2) + " km/h" : "–";
  $("carrSoln").textContent = fix.carrSolnName || "–";

  // Time
  const t = state.time || {};
  if (t.utc) $("utcClock").textContent = t.utc.replace("T", "  ").replace("Z", " UTC");
  $("utcValid").textContent = t.validUTC ? "Yes" : "No";
  $("tAcc").textContent = typeof t.tAcc_ns === "number" ? "±" + t.tAcc_ns + " ns" : "–";
  $("utcStd").textContent = UTC_STANDARDS[t.utcStandard] ?? "–";
  const st = state.status || {};
  $("ttff").textContent = typeof st.ttff_s === "number" ? st.ttff_s + " s" : "–";
  $("msss").textContent = typeof st.msss_s === "number" ? fmtDuration(st.msss_s) : "–";
  const clk = state.clock || {};
  $("clkBias").textContent = typeof clk.biasNs === "number" ? (clk.biasNs / 1000).toFixed(2) + " µs" : "–";
  $("clkDrift").textContent = typeof clk.driftNsPerS === "number" ? clk.driftNsPerS + " ns/s" : "–";
  $("clkFAcc").textContent = typeof clk.freqAccPsPerS === "number" ? "±" + clk.freqAccPsPerS + " ps/s" : "–";

  // Survey-in / base mode
  const svin = state.svin || {}, tmode3 = state.tmode3 || {};
  if (tmode3.rcvrModeName) {
    let label = tmode3.rcvrModeName;
    let status = "neutral";
    if (tmode3.rcvrMode === 2) status = "good";
    else if (tmode3.rcvrMode === 1) {
      label += svin.valid ? " · COMPLETE" : svin.active ? " · IN PROGRESS" : "";
      status = svin.valid ? "good" : svin.active ? "warning" : "neutral";
    }
    setBadge($("tmodeBadge"), label, status);
  }
  if (tmode3.svinMinDur_s) {
    const durPct = Math.min(100, (svin.dur_s || 0) / tmode3.svinMinDur_s * 100);
    $("durBar").style.width = durPct + "%";
    $("durBar").style.background = durPct >= 100 ? "var(--good)" : "var(--gps)";
    $("durText").textContent = `${svin.dur_s ?? 0}s / ${tmode3.svinMinDur_s}s`;
  }
  if (tmode3.svinAccLimit_m && typeof svin.meanAcc_m === "number") {
    const accPct = Math.min(100, tmode3.svinAccLimit_m / Math.max(svin.meanAcc_m, 0.001) * 100);
    $("accBar").style.width = accPct + "%";
    $("accBar").style.background = svin.meanAcc_m <= tmode3.svinAccLimit_m ? "var(--good)" : "var(--gps)";
    $("accText").textContent = `${svin.meanAcc_m.toFixed(3)} m / ${tmode3.svinAccLimit_m.toFixed(2)} m target`;
  }
  $("obs").textContent = svin.obs ?? "–";
  $("svinValid").textContent = svin.valid ? "Yes" : svin.active ? "Not yet" : "–";
  $("svinHistoryCount").textContent = state.survey_history_count ?? 0;

  // DOP
  renderDOP(state.dop || {});

  // Satellites
  const sats = state.satellites || [];
  $("satCount").textContent = sats.length ? `(${sats.filter(s => s.used).length} used / ${sats.length} tracked)` : "";
  drawSkyplot(sats);
  renderSignalBars(sats);

  // Health
  const h = state.health || {};
  $("antStatus").textContent = h.antennaStatus || "–";
  $("antPower").textContent = h.antennaPower || "–";
  $("jamState").textContent = h.jammingState || "–";
  if (typeof h.jamPercent === "number") {
    $("jamBar").style.width = h.jamPercent + "%";
    $("jamBar").style.background = h.jamPercent > 60 ? "var(--critical)" : h.jamPercent > 30 ? "var(--warning)" : "var(--good)";
    $("jamText").textContent = `${h.jamInd} / 255`;
  }
  if (typeof h.agcPercent === "number") {
    $("agcBar").style.width = h.agcPercent + "%";
    $("agcBar").style.background = h.agcPercent > 90 ? "var(--warning)" : "var(--gps)";
    $("agcText").textContent = h.agcPercent + "%";
  }
  $("noise").textContent = typeof h.noisePerMS === "number" ? h.noisePerMS : "–";
  const SPOOF_STATE = { 0: "Unknown", 1: "None detected", 2: "Indicated", 3: "Multiple indications" };
  $("spoofState").textContent = SPOOF_STATE[st.spoofDetState] ?? "–";
  $("rtcCalib").textContent = h.rtcCalib === undefined ? "–" : (h.rtcCalib ? "Yes" : "No");
  $("safeBoot").textContent = h.safeBoot === undefined ? "–" : (h.safeBoot ? "Yes ⚠️" : "No");
  $("xtalAbsent").textContent = h.xtalAbsent === undefined ? "–" : (h.xtalAbsent ? "Absent ⚠️" : "Present");

  renderRfBands(state.rf_bands || []);
  drawSpectrum(state.spectrum || []);
  renderPiHealth(state.sysinfo || {});
  renderChipHealth(state.chip || {});
  renderNavConfig(state.nav_config || {}, state.gnss_caps || {});
  renderRTCM(state.rtcm || {});
  renderPorts(state.ports || []);
  renderMap(fix, svin);
  renderConsole(state.log || []);
  renderRawlog(state.rawlog || {});

  $("ubxRate").textContent = (state.msg_rates?.UBX ?? 0).toFixed(1);
  $("nmeaRate").textContent = (state.msg_rates?.NMEA ?? 0).toFixed(1);
  $("rtcmRate").textContent = (state.msg_rates?.RTCM3 ?? 0).toFixed(1);
}

function fmtBytes(n) {
  if (typeof n !== "number") return "–";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function renderRawlog(rl) {
  if (!rl || rl.started == null) {
    setBadge($("rawlogBadge"), "Not started", "neutral");
    return;
  }
  if (rl.active) {
    setBadge($("rawlogBadge"), "Recording", "good");
  } else {
    setBadge($("rawlogBadge"), rl.epochs > 0 ? "Complete — ready for OPUS" : "Stopped", rl.epochs > 0 ? "good" : "neutral");
  }
  const total = rl.ends - rl.started;
  const elapsed = total - (rl.remaining_s ?? 0);
  const pct = total > 0 ? Math.min(100, elapsed / total * 100) : 0;
  $("rawlogTimeBar").style.width = pct + "%";
  $("rawlogTimeBar").style.background = rl.active ? "var(--gps)" : "var(--good)";
  $("rawlogTimeText").textContent = rl.active
    ? `${fmtDuration(elapsed)} elapsed · ${fmtDuration(rl.remaining_s)} remaining`
    : `${fmtDuration(elapsed)} total`;
  $("rawlogEpochs").textContent = rl.epochs;
  $("rawlogBytes").textContent = fmtBytes(rl.bytes);
  $("rawlogPath").textContent = rl.path || "–";
}

function renderChipHealth(chip) {
  if (chip.cpuLoad === undefined) return;
  $("chipCpuBar").style.width = chip.cpuLoad + "%";
  $("chipCpuBar").style.background = chip.cpuLoad > 85 ? "var(--critical)" : chip.cpuLoad > 60 ? "var(--warning)" : "var(--gps)";
  $("chipCpuText").textContent = `${chip.cpuLoad}% (peak ${chip.cpuLoadMax}%)`;

  $("chipMemBar").style.width = chip.memUsage + "%";
  $("chipMemBar").style.background = chip.memUsage > 85 ? "var(--critical)" : "var(--gps)";
  $("chipMemText").textContent = `${chip.memUsage}% (peak ${chip.memUsageMax}%)`;

  $("chipIoBar").style.width = chip.ioUsage + "%";
  $("chipIoBar").style.background = chip.ioUsage > 85 ? "var(--critical)" : "var(--gps)";
  $("chipIoText").textContent = `${chip.ioUsage}% (peak ${chip.ioUsageMax}%)`;

  $("chipTemp").textContent = typeof chip.tempC === "number" ? chip.tempC + "°C" : "–";
  $("chipBoot").textContent = chip.bootType || "–";
  $("chipRuntime").textContent = typeof chip.runTime_s === "number" ? fmtDuration(chip.runTime_s) : "–";
  const logsEl = $("chipLogs");
  logsEl.textContent = `${chip.noticeCount ?? 0} / ${chip.warnCount ?? 0} / ${chip.errorCount ?? 0}`;
  logsEl.style.color = chip.errorCount > 0 ? "var(--critical)" : chip.warnCount > 0 ? "var(--warning)" : "var(--good)";
}

function renderNavConfig(nc, caps) {
  if (nc.dynModel) {
    setBadge($("dynModelBadge"), `Dynamic model: ${nc.dynModel}`, nc.dynModelIsStationary ? "good" : "warning");
  }
  $("fixModeCfg").textContent = nc.fixMode || "–";
  $("minElevCfg").textContent = typeof nc.minElev_deg === "number" ? nc.minElev_deg + "°" : "–";
  $("dopMaskCfg").textContent = (nc.pDopMask ?? "–") + " / " + (nc.tDopMask ?? "–");
  $("accMaskCfg").textContent = `${nc.pAccMask_m ?? "–"} m / ${nc.tAccMask_m ?? "–"} m`;
  $("dgnssTimeoutCfg").textContent = typeof nc.dgnssTimeout_s === "number" ? nc.dgnssTimeout_s + " s" : "–";
  $("simulCfg").textContent = caps.simultaneous ?? "–";
  $("gnssEnabled").textContent = (caps.enabled || []).join(", ") || "–";
}

function renderRfBands(bands) {
  const el = $("rfBands");
  if (!bands.length) {
    el.innerHTML = '<span class="muted" style="font-size:12.5px">Waiting for MON-RF…</span>';
    return;
  }
  el.innerHTML = bands.map(b => {
    const jamColor = b.jamPercent > 60 ? "var(--critical)" : b.jamPercent > 30 ? "var(--warning)" : "var(--good)";
    const agcColor = b.agcPercent > 90 ? "var(--warning)" : "var(--gps)";
    return `<div class="rf-band">
      <div class="rf-band-title"><span>${b.band}</span><span class="mono-chip">block ${b.blockId}</span></div>
      <div class="progress-row">
        <div class="progress-label"><span>Jamming</span><span class="tab">${b.jamInd}/255</span></div>
        <div class="progress-track"><div class="progress-fill" style="width:${b.jamPercent}%;background:${jamColor}"></div></div>
      </div>
      <div class="progress-row">
        <div class="progress-label"><span>AGC</span><span class="tab">${b.agcPercent}%</span></div>
        <div class="progress-track"><div class="progress-fill" style="width:${b.agcPercent}%;background:${agcColor}"></div></div>
      </div>
      <div class="kv-grid">
        <div class="kv"><span class="k">Noise/ms</span><span class="v tab">${b.noisePerMS}</span></div>
        <div class="kv"><span class="k">Self-test</span><span class="v">${b.postStatus === 0 ? "Pass" : "Code " + b.postStatus}</span></div>
        <div class="kv"><span class="k">I/Q offset</span><span class="v tab">${b.ofsI} / ${b.ofsQ}</span></div>
        <div class="kv"><span class="k">I/Q gain</span><span class="v tab">${b.magI} / ${b.magQ}</span></div>
      </div>
    </div>`;
  }).join("");
}

const SPECTRUM_COLORS = ["#3987e5", "#1baf7a"];
function drawSpectrum(blocks) {
  const canvas = $("spectrumCanvas");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 860, cssH = 220;
  if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
    canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  if (!blocks.length) return;

  const padL = 34, padR = 10, padT = 10, padB = 20, gap = 16;
  const totalW = cssW - padL - padR - gap * (blocks.length - 1);
  const segW = totalW / blocks.length;
  const h = cssH - padT - padB;
  let allBins = blocks.flatMap(b => b.bins);
  const yMax = Math.max(160, ...allBins), yMin = Math.min(30, ...allBins);

  ctx.strokeStyle = getCss("--gridline");
  ctx.fillStyle = getCss("--muted");
  ctx.font = "10px system-ui";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const y = padT + h * i / 4;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(cssW - padR, y); ctx.stroke();
    ctx.fillText(Math.round(yMax - (yMax - yMin) * i / 4), padL - 6, y + 3);
  }

  blocks.forEach((b, bi) => {
    const bins = b.bins;
    const color = SPECTRUM_COLORS[bi % SPECTRUM_COLORS.length];
    const segX0 = padL + bi * (segW + gap);
    ctx.beginPath();
    bins.forEach((v, i) => {
      const x = segX0 + segW * i / (bins.length - 1);
      const y = padT + h * (1 - (v - yMin) / (yMax - yMin));
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.lineTo(segX0 + segW, padT + h);
    ctx.lineTo(segX0, padT + h);
    ctx.closePath();
    ctx.fillStyle = color + "22";
    ctx.fill();

    const centerMHz = (b.centerHz / 1e6).toFixed(1);
    const startMHz = ((b.centerHz - b.spanHz / 2) / 1e6).toFixed(0);
    const endMHz = ((b.centerHz + b.spanHz / 2) / 1e6).toFixed(0);
    ctx.fillStyle = getCss("--muted");
    ctx.font = "10px system-ui";
    ctx.textAlign = "left";
    ctx.fillText(startMHz, segX0, cssH - 5);
    ctx.textAlign = "right";
    ctx.fillText(endMHz, segX0 + segW, cssH - 5);
    ctx.textAlign = "center";
    ctx.fillStyle = color;
    ctx.fillText(centerMHz + " MHz", segX0 + segW / 2, padT + 10);
  });

  const legend = $("spectrumLegend");
  legend.innerHTML = blocks.map((b, bi) =>
    `<span class="legend-item"><span class="legend-swatch" style="background:${SPECTRUM_COLORS[bi % SPECTRUM_COLORS.length]}"></span>Block ${bi} · PGA gain ${b.pga}</span>`
  ).join("");
}

function renderPiHealth(sys) {
  if (!sys || sys.cpuPercent === undefined) return;
  const t = sys.throttled || {};
  if (t.under_voltage_now || t.throttled_now) {
    setBadge($("piThrottleBadge"), "Under-voltage / throttled NOW", "critical");
  } else if (t.under_voltage_occurred || t.throttled_occurred) {
    setBadge($("piThrottleBadge"), "OK now — occurred earlier", "warning");
  } else {
    setBadge($("piThrottleBadge"), "Power OK, no throttling", "good");
  }
  $("piCpuBar").style.width = sys.cpuPercent + "%";
  $("piCpuBar").style.background = sys.cpuPercent > 85 ? "var(--critical)" : sys.cpuPercent > 60 ? "var(--warning)" : "var(--gps)";
  $("piCpuText").textContent = sys.cpuPercent.toFixed(0) + "% (" + (sys.cpuPerCore || []).map(c => c.toFixed(0)).join("/") + "%)";

  $("piMemBar").style.width = sys.memUsedPercent + "%";
  $("piMemBar").style.background = sys.memUsedPercent > 90 ? "var(--critical)" : "var(--gps)";
  $("piMemText").textContent = `${fmtBytes(sys.memUsedBytes)} / ${fmtBytes(sys.memTotalBytes)}`;

  $("piDiskBar").style.width = sys.diskUsedPercent + "%";
  $("piDiskBar").style.background = sys.diskUsedPercent > 90 ? "var(--critical)" : "var(--good)";
  $("piDiskText").textContent = `${fmtBytes(sys.diskTotalBytes - sys.diskFreeBytes)} used · ${fmtBytes(sys.diskFreeBytes)} free`;

  if (typeof sys.cpuTempC === "number") {
    const pct = Math.min(100, sys.cpuTempC / 85 * 100);
    $("piTempBar").style.width = pct + "%";
    $("piTempBar").style.background = sys.cpuTempC > 75 ? "var(--critical)" : sys.cpuTempC > 60 ? "var(--warning)" : "var(--good)";
    $("piTempText").textContent = sys.cpuTempC.toFixed(1) + "°C";
  }
  $("piLoad").textContent = (sys.loadAvg || []).map(l => l.toFixed(2)).join(" / ");
  if (sys.bootTime) {
    $("piUptime").textContent = fmtDuration(Date.now() / 1000 - sys.bootTime);
  }
}

function renderDOP(dop) {
  const order = [["gDOP", "GDOP"], ["pDOP", "PDOP"], ["hDOP", "HDOP"], ["vDOP", "VDOP"], ["tDOP", "TDOP"]];
  const grid = $("dopGrid");
  grid.innerHTML = order.map(([key, label]) => {
    const v = dop[key];
    const na = typeof v !== "number" || v >= 99;
    return `<div class="dop-tile"><div class="dop-value ${na ? "dop-na" : ""}">${na ? "N/A" : v.toFixed(2)}</div><div class="dop-label">${label}</div></div>`;
  }).join("");
}

function renderRTCM(rtcm) {
  const rows = Object.entries(rtcm).sort((a, b) => a[0].localeCompare(b[0]));
  const tbody = document.querySelector("#rtcmTable tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">No RTCM3 traffic observed on this port</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(([id, m]) => {
    const barWidth = Math.min(100, m.rate * 40);
    return `<tr>
      <td><span class="mono-chip">${id}</span></td>
      <td>${m.name}</td>
      <td><span class="rate-bar" style="width:${barWidth}px"></span>${m.rate.toFixed(2)}/s</td>
      <td>${m.count}</td>
      <td class="${m.age > 5 ? "overrun-bad" : ""}">${m.age != null ? m.age + "s" : "–"}</td>
    </tr>`;
  }).join("");
}

function renderPorts(ports) {
  const tbody = document.querySelector("#portsTable tbody");
  if (!ports.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">Waiting for MON-COMMS…</td></tr>';
    return;
  }
  tbody.innerHTML = ports.map(p => `<tr>
    <td><span class="mono-chip">${p.label}</span></td>
    <td>${p.txUsage}% <span class="muted">(peak ${p.txPeakUsage}%)</span></td>
    <td>${p.rxUsage}% <span class="muted">(peak ${p.rxPeakUsage}%)</span></td>
    <td class="${p.overrunErrs > 0 ? "overrun-bad" : "overrun-ok"}">${p.overrunErrs}</td>
  </tr>`).join("");
}

// ---------------- Map ----------------
let leafletMap = null, mapMarker = null, mapAccCircle = null, mapCentered = false;

function initMap() {
  try {
    if (typeof L === "undefined") throw new Error("Leaflet failed to load (offline?)");
    leafletMap = L.map("mapView", { attributionControl: false, zoomControl: false }).setView([20, 0], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(leafletMap);
  } catch (e) {
    console.warn("Map unavailable:", e.message);
    $("mapView").innerHTML = '<div class="muted" style="padding:16px;font-size:12.5px">Map tiles unavailable (no internet). Coordinates are still shown below.</div>';
  }
}

function renderMap(fix, svin) {
  if (!leafletMap || typeof fix.lat !== "number") {
    if (typeof fix.lat === "number") {
      $("mapCoords").textContent = `${fix.lat.toFixed(6)}, ${fix.lon.toFixed(6)}`;
      const acc = typeof svin.meanAcc_m === "number" ? svin.meanAcc_m : fix.hAcc_m;
      $("mapAcc").textContent = typeof acc === "number" ? "±" + acc.toFixed(2) + " m" : "–";
    }
    return;
  }
  const latlng = [fix.lat, fix.lon];
  const acc = typeof svin.meanAcc_m === "number" ? svin.meanAcc_m : fix.hAcc_m;
  if (!mapMarker) {
    mapMarker = L.circleMarker(latlng, { radius: 6, color: "#ffffff", weight: 2, fillColor: "#3987e5", fillOpacity: 1 }).addTo(leafletMap);
    mapAccCircle = L.circle(latlng, { radius: acc || 1, color: "#3987e5", weight: 1, fillColor: "#3987e5", fillOpacity: 0.12 }).addTo(leafletMap);
  } else {
    mapMarker.setLatLng(latlng);
    mapAccCircle.setLatLng(latlng);
    if (acc) mapAccCircle.setRadius(acc);
  }
  if (!mapCentered) {
    leafletMap.setView(latlng, 18);
    mapCentered = true;
    setTimeout(() => leafletMap.invalidateSize(), 200);
  }
  $("mapCoords").textContent = `${fix.lat.toFixed(6)}, ${fix.lon.toFixed(6)}`;
  $("mapAcc").textContent = typeof acc === "number" ? "±" + acc.toFixed(2) + " m" : "–";
}

// ---------------- Console ----------------
let lastLogSeq = 0;
function renderConsole(entries) {
  const el = $("console");
  const newOnes = entries.filter(e => e.seq > lastLogSeq);
  if (!newOnes.length) return;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  newOnes.forEach(e => {
    lastLogSeq = Math.max(lastLogSeq, e.seq);
    const row = document.createElement("div");
    row.className = "console-row";
    const time = new Date(e.t * 1000).toLocaleTimeString([], { hour12: false }) + "." + String(Math.round((e.t % 1) * 100)).padStart(2, "0");
    row.innerHTML = `<span class="console-t">${time}</span><span class="console-proto console-proto-${e.proto}">${e.proto}</span><span class="console-ident">${e.ident}</span>`;
    el.appendChild(row);
  });
  while (el.childElementCount > 200) el.removeChild(el.firstChild);
  if (nearBottom) el.scrollTop = el.scrollHeight;
}

// ---------------- Sky plot ----------------
const skyCanvas = $("skyplot");
const skyCtx = skyCanvas.getContext("2d");
let lastSats = [];
let selectedSatKey = null;
const satKey = (s) => `${s.gnssId}_${s.svId}`;

function toggleSelect(key) {
  selectedSatKey = selectedSatKey === key ? null : key;
  drawSkyplot(lastSats);
  renderSignalBars(lastSats);
}

function drawSkyplot(sats) {
  lastSats = sats;
  const w = skyCanvas.width, h = skyCanvas.height;
  const cx = w / 2, cy = h / 2, maxR = Math.min(w, h) / 2 - 24;
  skyCtx.clearRect(0, 0, w, h);

  skyCtx.strokeStyle = getCss("--gridline");
  skyCtx.fillStyle = getCss("--muted");
  skyCtx.font = "11px system-ui";
  skyCtx.textAlign = "center";
  [0, 30, 60, 90].forEach(el => {
    const r = (90 - el) / 90 * maxR;
    skyCtx.beginPath();
    skyCtx.arc(cx, cy, Math.max(r, 0.5), 0, Math.PI * 2);
    skyCtx.stroke();
    if (el < 90) skyCtx.fillText(el + "°", cx + 4, cy - r - 3);
  });
  skyCtx.strokeStyle = getCss("--baseline");
  skyCtx.beginPath(); skyCtx.moveTo(cx - maxR, cy); skyCtx.lineTo(cx + maxR, cy); skyCtx.stroke();
  skyCtx.beginPath(); skyCtx.moveTo(cx, cy - maxR); skyCtx.lineTo(cx, cy + maxR); skyCtx.stroke();

  skyCtx.fillStyle = getCss("--text-secondary");
  skyCtx.font = "13px system-ui";
  skyCtx.fillText("N", cx, cy - maxR - 8);
  skyCtx.fillText("S", cx, cy + maxR + 16);
  skyCtx.fillText("E", cx + maxR + 10, cy + 4);
  skyCtx.fillText("W", cx - maxR - 10, cy + 4);

  sats.forEach(s => {
    const r = (90 - s.elev) / 90 * maxR;
    const azRad = s.azim * Math.PI / 180;
    const x = cx + r * Math.sin(azRad);
    const y = cy - r * Math.cos(azRad);
    const isSelected = selectedSatKey === satKey(s);
    const radius = (3 + Math.min(s.cno, 50) / 50 * 6) * (isSelected ? 1.4 : 1);
    s._x = x; s._y = y; s._r = radius;
    const dimmed = selectedSatKey && !isSelected;
    skyCtx.globalAlpha = dimmed ? 0.18 : (s.used ? 1 : 0.45);
    skyCtx.beginPath();
    skyCtx.arc(x, y, radius, 0, Math.PI * 2);
    skyCtx.fillStyle = s.color;
    skyCtx.fill();
    if (s.used || isSelected) {
      skyCtx.globalAlpha = dimmed ? 0.18 : 1;
      skyCtx.lineWidth = isSelected ? 2.5 : 1.5;
      skyCtx.strokeStyle = isSelected ? "#ffffff" : "rgba(255,255,255,0.6)";
      skyCtx.stroke();
    }
    skyCtx.globalAlpha = 1;
  });

  renderSkyLegend(sats);
}

function renderSkyLegend(sats) {
  const present = [...new Set(sats.map(s => s.gnssName))];
  const legend = $("skyLegend");
  if (!present.length) { legend.innerHTML = ""; return; }
  legend.innerHTML = present.map(name => {
    const color = sats.find(s => s.gnssName === name).color;
    return `<span class="legend-item"><span class="legend-swatch" style="background:${color}"></span>${name}</span>`;
  }).join("");
}

function getCss(varName) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

const skyTooltip = document.createElement("div");
skyTooltip.style.cssText = "position:fixed;pointer-events:none;background:#212120;border:1px solid rgba(255,255,255,0.15);padding:6px 10px;border-radius:6px;font-size:12px;display:none;z-index:100;box-shadow:0 4px 16px rgba(0,0,0,0.4)";
document.body.appendChild(skyTooltip);

skyCanvas.addEventListener("mousemove", (e) => {
  const rect = skyCanvas.getBoundingClientRect();
  const scaleX = skyCanvas.width / rect.width, scaleY = skyCanvas.height / rect.height;
  const mx = (e.clientX - rect.left) * scaleX, my = (e.clientY - rect.top) * scaleY;
  const hit = lastSats.find(s => s._x != null && Math.hypot(s._x - mx, s._y - my) <= s._r + 2);
  if (hit) {
    skyTooltip.style.display = "block";
    skyTooltip.style.left = e.clientX + 14 + "px";
    skyTooltip.style.top = e.clientY + 10 + "px";
    skyTooltip.innerHTML = `<b>${hit.gnssName} ${hit.prn}</b><br>${hit.cno} dB-Hz · el ${hit.elev}° az ${hit.azim}°<br>${hit.used ? "used in fix" : "tracked only"}`;
  } else {
    skyTooltip.style.display = "none";
  }
});
skyCanvas.addEventListener("mouseleave", () => skyTooltip.style.display = "none");
skyCanvas.addEventListener("click", (e) => {
  const rect = skyCanvas.getBoundingClientRect();
  const scaleX = skyCanvas.width / rect.width, scaleY = skyCanvas.height / rect.height;
  const mx = (e.clientX - rect.left) * scaleX, my = (e.clientY - rect.top) * scaleY;
  const hit = lastSats.find(s => s._x != null && Math.hypot(s._x - mx, s._y - my) <= s._r + 2);
  if (hit) toggleSelect(satKey(hit));
});

// ---------------- Signal bars ----------------
function renderSignalBars(sats) {
  const container = $("signalBars");
  const sorted = [...sats].sort((a, b) => a.gnssName.localeCompare(b.gnssName) || b.cno - a.cno);
  container.innerHTML = sorted.map(s => {
    const pct = Math.max(2, Math.min(100, s.cno / 55 * 100));
    const key = satKey(s);
    const isSelected = selectedSatKey === key;
    const dimmed = selectedSatKey && !isSelected;
    return `<div class="signal-bar-col ${isSelected ? "selected" : ""} ${dimmed ? "dimmed" : ""}" data-key="${key}"
        title="${s.gnssName} ${s.prn} · ${s.cno} dB-Hz · el ${s.elev}° az ${s.azim}° · ${s.used ? "used" : "tracked"}">
      <div class="bar ${s.used ? "bar-used" : ""}" style="height:${pct}%;background:${s.color}"></div>
      <div class="bar-label">${s.prn}</div>
    </div>`;
  }).join("");
  container.querySelectorAll(".signal-bar-col").forEach(col => {
    col.addEventListener("click", () => toggleSelect(col.dataset.key));
  });
}

// ---------------- WebSocket ----------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  ws.onclose = () => {
    $("connPill").className = "pill pill-critical";
    $("connPill").innerHTML = '<span class="dot"></span>RECONNECTING';
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();
}
initMap();
connect();
