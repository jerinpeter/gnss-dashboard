"""
RTK base station monitor for a u-blox ZED-F9P (or similar) receiver on USB.

Passive design: everything shown comes from POLL requests (which never change
receiver config) plus whatever the receiver already streams unsolicited
(NMEA, RTCM3). We never SET/CFG anything, so the base station's actual job -
streaming RTCM corrections to a rover on its other ports - is never touched.
"""
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import psutil
import serial
from flask import Flask, jsonify, send_from_directory
from flask_sock import Sock
from pyubx2 import UBXReader, UBXMessage, POLL, SET

SERIAL_PORT = "/dev/ttyACM0"
BAUD = 38400
BROADCAST_HZ = 4
FAST_POLL_INTERVAL = 1.0   # NAV-* messages
SLOW_POLL_INTERVAL = 5.0   # MON-HW / MON-COMMS
VER_RETRY_INTERVAL = 10.0  # MON-VER, until we get it once
RAWLOG_DIR = "raw_logs"
RAWLOG_INTERVAL = 30.0     # seconds between RXM-RAWX polls (static RINEX-typical interval)
GPS_LEAP_SECONDS = 18      # current GPS-UTC offset; confirmed from this receiver's RXM-RAWX.leapS
RAWLOG_MARKER = os.path.join(RAWLOG_DIR, ".rawlog_session.json")

GNSS_NAMES = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS"}
GNSS_PRN_PREFIX = {0: "G", 1: "S", 2: "E", 3: "C", 4: "I", 5: "J", 6: "R"}  # RINEX-style satellite ID prefix
# fixed categorical order per dataviz palette (blue,orange,aqua,yellow,magenta,green)
# dark-column hues from the categorical palette (fixed order = CVD-safety mechanism)
GNSS_COLORS = {
    "GPS": "#3987e5", "GLONASS": "#d95926", "Galileo": "#199e70",
    "BeiDou": "#c98500", "SBAS": "#d55181", "QZSS": "#008300", "IMES": "#9085e9",
}
FIX_TYPE_NAMES = {0: "No Fix", 1: "Dead Reckoning", 2: "2D", 3: "3D", 4: "3D+DR", 5: "Time Only"}
CARR_SOLN_NAMES = {0: "None", 1: "RTK Float", 2: "RTK Fixed"}
ANTENNA_STATUS = {0: "INIT", 1: "UNKNOWN", 2: "OK", 3: "SHORT", 4: "OPEN"}
ANTENNA_POWER = {0: "OFF", 1: "ON", 2: "UNKNOWN"}
JAMMING_STATE = {0: "UNKNOWN", 1: "OK", 2: "WARNING", 3: "CRITICAL"}
PORT_TYPE_NAMES = {0: "I2C", 1: "UART1", 2: "UART2", 3: "USB", 4: "SPI"}
PROTOCOL_NAMES = {0: "UBX", 1: "NMEA", 2: "RTCM2", 5: "RTCM3", 6: "SPARTN"}
RTCM_MSG_NAMES = {
    "1005": "Station ARP (no antenna height)",
    "1006": "Station ARP + antenna height",
    "1074": "GPS MSM4", "1077": "GPS MSM7",
    "1084": "GLONASS MSM4", "1087": "GLONASS MSM7",
    "1094": "Galileo MSM4", "1097": "Galileo MSM7",
    "1124": "BeiDou MSM4", "1127": "BeiDou MSM7",
    "1230": "GLONASS code-phase biases",
    "4072": "u-blox proprietary",
}


class RollingRate:
    """Tracks event timestamps in a rolling window to compute a rate/sec."""

    def __init__(self, window=10.0):
        self.window = window
        self.times = deque()
        self.count = 0
        self.last_seen = None

    def hit(self):
        now = time.time()
        self.times.append(now)
        self.count += 1
        self.last_seen = now
        self._trim(now)

    def _trim(self, now):
        while self.times and now - self.times[0] > self.window:
            self.times.popleft()

    def rate(self):
        self._trim(time.time())
        return len(self.times) / self.window


class ReceiverState:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.port_error = None
        self.receiver = {}
        self.fix = {}
        self.time = {}
        self.dop = {}
        self.status = {}
        self.svin = {}
        self.tmode3 = {}
        self.satellites = []
        self.health = {}
        self.ports = []
        self.rf_bands = []
        self.spectrum = []
        self.sysinfo = {}
        self.chip = {}
        self.clock = {}
        self.gnss_caps = {}
        self.nav_config = {}
        self.survey_history_count, self.last_svin_key = self._load_existing_survey_history()
        self.rtcm_stats = {}   # ident -> RollingRate
        self.msg_class_rates = {"UBX": RollingRate(), "NMEA": RollingRate(), "RTCM3": RollingRate()}
        self.byte_rate = RollingRate()
        self.msg_log = deque(maxlen=300)
        self.msg_seq = 0
        self.start_time = time.time()
        self.rawlog_active = False
        self.rawlog_file = None
        self.rawlog_path = None
        self.rawlog_started = None
        self.rawlog_ends = None
        self.rawlog_epochs = 0
        self.rawlog_bytes = 0
        self.last_rawx_arrival = 0.0
        self.last_cfg_msg_rate_usb = None

    @staticmethod
    def _load_existing_survey_history():
        path = os.path.join(RAWLOG_DIR, "survey_history.jsonl")
        try:
            with open(path) as f:
                lines = f.readlines()
            if not lines:
                return 0, None
            last = json.loads(lines[-1])
            return len(lines), (last.get("dur_s"), last.get("obs"))
        except (OSError, json.JSONDecodeError):
            return 0, None

    def log_message(self, proto, ident):
        self.msg_seq += 1
        self.msg_log.append({"seq": self.msg_seq, "t": round(time.time(), 2), "proto": proto, "ident": ident})

    def _write_marker(self):
        # best-effort persistence so a power-cycle/restart can resume the same
        # session instead of losing it or fragmenting it into a new file
        try:
            with open(RAWLOG_MARKER, "w") as f:
                json.dump({
                    "path": self.rawlog_path,
                    "started": self.rawlog_started,
                    "ends": self.rawlog_ends,
                    "epochs": self.rawlog_epochs,
                    "bytes": self.rawlog_bytes,
                }, f)
        except OSError:
            pass

    def start_rawlog(self, hours):
        os.makedirs(RAWLOG_DIR, exist_ok=True)
        fname = f"basestation_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.ubx"
        path = os.path.join(RAWLOG_DIR, fname)
        with self.lock:
            if self.rawlog_file:
                self.rawlog_file.close()
            self.rawlog_file = open(path, "ab")
            self.rawlog_path = path
            self.rawlog_active = True
            self.rawlog_started = time.time()
            self.rawlog_ends = time.time() + hours * 3600
            self.rawlog_epochs = 0
            self.rawlog_bytes = 0
            self._write_marker()
        return path

    def resume_or_start_rawlog(self, default_hours):
        """Called once at process startup. If a prior session marker points at
        a still-valid (not yet expired) log file, reopen and keep appending to
        it - so a power-cycle or crash-restart continues the same observation
        session instead of fragmenting it into a fresh file each time."""
        os.makedirs(RAWLOG_DIR, exist_ok=True)
        try:
            with open(RAWLOG_MARKER) as f:
                marker = json.load(f)
            path = marker["path"]
            if marker["ends"] > time.time() and os.path.exists(path):
                with self.lock:
                    self.rawlog_file = open(path, "ab")
                    self.rawlog_path = path
                    self.rawlog_active = True
                    self.rawlog_started = marker["started"]
                    self.rawlog_ends = marker["ends"]
                    self.rawlog_epochs = marker.get("epochs", 0)
                    self.rawlog_bytes = os.path.getsize(path)
                print(f"Resumed raw log session -> {path} "
                      f"({self.rawlog_epochs} epochs already captured, "
                      f"ends in {(self.rawlog_ends - time.time()) / 3600:.1f}h)")
                return path
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return self.start_rawlog(default_hours)

    def extend_rawlog(self, hours_from_now):
        with self.lock:
            if not self.rawlog_active:
                return False
            self.rawlog_ends = time.time() + hours_from_now * 3600
            self._write_marker()
            return True

    def stop_rawlog(self):
        with self.lock:
            self.rawlog_active = False
            if self.rawlog_file:
                self.rawlog_file.close()
                self.rawlog_file = None
        try:
            os.remove(RAWLOG_MARKER)
        except OSError:
            pass

    def write_rawlog_frame(self, raw_bytes):
        with self.lock:
            if not self.rawlog_active or self.rawlog_file is None:
                return
            self.rawlog_file.write(raw_bytes)
            self.rawlog_file.flush()
            self.rawlog_epochs += 1
            self.rawlog_bytes += len(raw_bytes)
            ended = time.time() >= self.rawlog_ends
            if ended:
                self.rawlog_active = False
                self.rawlog_file.close()
                self.rawlog_file = None
            self._write_marker()

    def record_survey_result(self):
        """Append a completed survey-in result to a history file for later
        comparison against the OPUS-derived position. Called from handle_ubx
        while state.lock is already held - must not re-acquire it."""
        if not self.fix.get("lat"):
            return
        record = {
            "t": datetime.utcnow().isoformat() + "Z",
            "lat": self.fix.get("lat"), "lon": self.fix.get("lon"),
            "height_m": self.fix.get("hMSL_m"),
            "meanAcc_m": self.svin.get("meanAcc_m"),
            "dur_s": self.svin.get("dur_s"), "obs": self.svin.get("obs"),
        }
        try:
            os.makedirs(RAWLOG_DIR, exist_ok=True)
            with open(os.path.join(RAWLOG_DIR, "survey_history.jsonl"), "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass
        self.survey_history_count += 1

    def snapshot(self):
        with self.lock:
            rtcm_out = {}
            for ident, rr in self.rtcm_stats.items():
                rtcm_out[ident] = {
                    "name": RTCM_MSG_NAMES.get(ident, "Unknown"),
                    "count": rr.count,
                    "rate": round(rr.rate(), 2),
                    "age": round(time.time() - rr.last_seen, 1) if rr.last_seen else None,
                }
            msg_rates = {k: round(v.rate(), 1) for k, v in self.msg_class_rates.items()}
            log_tail = list(self.msg_log)[-25:]
            rawlog = {
                "active": self.rawlog_active,
                "path": self.rawlog_path,
                "epochs": self.rawlog_epochs,
                "bytes": self.rawlog_bytes,
                "started": self.rawlog_started,
                "ends": self.rawlog_ends,
                "remaining_s": max(0, round(self.rawlog_ends - time.time())) if self.rawlog_ends else None,
            }
            return {
                "connected": self.connected,
                "port_error": self.port_error,
                "uptime": round(time.time() - self.start_time, 1),
                "receiver": self.receiver,
                "fix": self.fix,
                "time": self.time,
                "dop": self.dop,
                "status": self.status,
                "svin": self.svin,
                "tmode3": self.tmode3,
                "satellites": self.satellites,
                "health": self.health,
                "rf_bands": self.rf_bands,
                "spectrum": self.spectrum,
                "sysinfo": self.sysinfo,
                "chip": self.chip,
                "clock": self.clock,
                "gnss_caps": self.gnss_caps,
                "nav_config": self.nav_config,
                "survey_history_count": self.survey_history_count,
                "ports": self.ports,
                "rtcm": rtcm_out,
                "msg_rates": msg_rates,
                "log": log_tail,
                "rawlog": rawlog,
            }


state = ReceiverState()
_write_lock = threading.Lock()
_ser_holder = {"ser": None}


def safe_write(ubx_msg):
    ser = _ser_holder["ser"]
    if ser is None:
        return
    try:
        with _write_lock:
            ser.write(ubx_msg.serialize())
            ser.flush()
    except Exception as e:
        print(f"safe_write error: {e}")


def handle_ubx(parsed, ident):
    with state.lock:
        if ident == "NAV-PVT":
            state.fix = {
                "fixType": parsed.fixType,
                "fixTypeName": FIX_TYPE_NAMES.get(parsed.fixType, "?"),
                "gnssFixOk": bool(parsed.gnssFixOk),
                "diffSoln": bool(parsed.diffSoln),
                "carrSoln": getattr(parsed, "carrSoln", 0),
                "carrSolnName": CARR_SOLN_NAMES.get(getattr(parsed, "carrSoln", 0), "?"),
                "lat": parsed.lat, "lon": parsed.lon,
                "height_m": parsed.height / 1000.0,
                "hMSL_m": parsed.hMSL / 1000.0,
                "hAcc_m": parsed.hAcc / 1000.0,
                "vAcc_m": parsed.vAcc / 1000.0,
                "numSV": parsed.numSV,
                "gSpeed_kmh": parsed.gSpeed / 1000.0 * 3.6,
                "headMot": parsed.headMot,
                "pDOP": parsed.pDOP,
            }
        elif ident == "NAV-SAT":
            sats = []
            n = parsed.numSvs
            for i in range(1, n + 1):
                suf = f"_{i:02d}"
                gid = getattr(parsed, f"gnssId{suf}", 0)
                gname = GNSS_NAMES.get(gid, f"GNSS{gid}")
                svid = getattr(parsed, f"svId{suf}", 0)
                sats.append({
                    "gnssId": gid, "gnssName": gname,
                    "color": GNSS_COLORS.get(gname, "#898781"),
                    "svId": svid,
                    "prn": f"{GNSS_PRN_PREFIX.get(gid, '?')}{svid:02d}",
                    "cno": getattr(parsed, f"cno{suf}", 0),
                    "elev": getattr(parsed, f"elev{suf}", 0),
                    "azim": getattr(parsed, f"azim{suf}", 0),
                    "used": bool(getattr(parsed, f"svUsed{suf}", 0)),
                    "quality": getattr(parsed, f"qualityInd{suf}", 0),
                    "health": getattr(parsed, f"health{suf}", 0),
                    "diffCorr": bool(getattr(parsed, f"diffCorr{suf}", 0)),
                })
            state.satellites = sats
        elif ident == "NAV-DOP":
            state.dop = {
                "gDOP": parsed.gDOP, "pDOP": parsed.pDOP, "tDOP": parsed.tDOP,
                "vDOP": parsed.vDOP, "hDOP": parsed.hDOP, "nDOP": parsed.nDOP, "eDOP": parsed.eDOP,
            }
        elif ident == "NAV-STATUS":
            state.status = {
                "ttff_s": round(parsed.ttff / 1000.0, 1),
                "msss_s": round(parsed.msss / 1000.0, 1),
                "spoofDetState": getattr(parsed, "spoofDetState", 0),
                "carrSolnValid": bool(getattr(parsed, "carrSolnValid", 0)),
            }
        elif ident == "NAV-CLOCK":
            state.clock = {
                "biasNs": parsed.clkB,
                "driftNsPerS": parsed.clkD,
                "timeAccNs": parsed.tAcc,
                "freqAccPsPerS": parsed.fAcc,
            }
        elif ident == "MON-GNSS":
            supported, enabled = [], []
            for name in ("GPS", "Glonass", "Galileo", "Beidou"):
                if getattr(parsed, f"{name}Sup", 0):
                    supported.append(name)
                if getattr(parsed, f"{name}Ena", 0):
                    enabled.append(name)
            state.gnss_caps = {"supported": supported, "enabled": enabled, "simultaneous": parsed.simultaneous}
        elif ident == "CFG-NAV5":
            DYN_MODEL = {0: "Portable", 2: "Stationary", 3: "Pedestrian", 4: "Automotive", 5: "Sea",
                         6: "Airborne <1g", 7: "Airborne <2g", 8: "Airborne <4g", 9: "Wrist", 10: "Bike",
                         11: "Mower", 12: "E-scooter"}
            FIX_MODE = {1: "2D only", 2: "3D only", 3: "Auto 2D/3D"}
            state.nav_config = {
                "dynModel": DYN_MODEL.get(parsed.dynModel, f"Model {parsed.dynModel}"),
                "dynModelIsStationary": parsed.dynModel == 2,
                "fixMode": FIX_MODE.get(parsed.fixMode, "?"),
                "minElev_deg": parsed.minElev,
                "pDopMask": parsed.pDop,
                "tDopMask": parsed.tDop,
                "pAccMask_m": parsed.pAcc,
                "tAccMask_m": parsed.tAcc,
                "dgnssTimeout_s": parsed.dgnssTimeOut,
            }
        elif ident == "NAV-SVIN":
            state.svin = {
                "active": bool(parsed.active),
                "valid": bool(parsed.valid),
                "dur_s": parsed.dur,
                "obs": parsed.obs,
                "meanAcc_m": parsed.meanAcc / 10000.0,
            }
            # A survey-in freezes dur/obs/meanAcc once valid (it doesn't keep
            # refining), so dedup on (dur_s, obs) rather than a valid-transition
            # edge - a bare process restart must not re-log the same frozen
            # result as if it were a new independent survey.
            key = (parsed.dur, parsed.obs)
            if state.svin["valid"] and key != state.last_svin_key:
                state.last_svin_key = key
                state.record_survey_result()
        elif ident == "NAV-TIMEUTC":
            state.time = {
                "utc": f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}T"
                       f"{parsed.hour:02d}:{parsed.min:02d}:{parsed.sec:02d}Z",
                "validUTC": bool(parsed.validUTC),
                "tAcc_ns": parsed.tAcc,
                "utcStandard": parsed.utcStandard,
            }
        elif ident == "CFG-TMODE3":
            state.tmode3 = {
                "rcvrMode": parsed.rcvrMode,
                "rcvrModeName": {0: "Disabled", 1: "Survey-In", 2: "Fixed"}.get(parsed.rcvrMode, "?"),
                "svinMinDur_s": parsed.svinMinDur,
                "svinAccLimit_m": parsed.svinAccLimit / 10000.0,
            }
        elif ident == "MON-VER":
            sw = parsed.swVersion.rstrip(b"\x00").decode(errors="replace") if isinstance(parsed.swVersion, bytes) else str(parsed.swVersion)
            hw = parsed.hwVersion.rstrip(b"\x00").decode(errors="replace") if isinstance(parsed.hwVersion, bytes) else str(parsed.hwVersion)
            exts = []
            i = 1
            while hasattr(parsed, f"extension_{i:02d}"):
                v = getattr(parsed, f"extension_{i:02d}")
                if isinstance(v, bytes):
                    v = v.rstrip(b"\x00").decode(errors="replace")
                if v:
                    exts.append(v)
                i += 1
            state.receiver = {"swVersion": sw, "hwVersion": hw, "extensions": exts}
        elif ident == "MON-HW":
            state.health = dict(state.health)
            state.health.update({
                "noisePerMS": parsed.noisePerMS,
                "agcCnt": parsed.agcCnt,
                "agcPercent": round(parsed.agcCnt / 8191.0 * 100, 1),
                "antennaStatus": ANTENNA_STATUS.get(parsed.aStatus, "?"),
                "antennaPower": ANTENNA_POWER.get(parsed.aPower, "?"),
                "jamInd": parsed.jamInd,
                "jamPercent": round(parsed.jamInd / 255.0 * 100, 1),
                "jammingState": JAMMING_STATE.get(getattr(parsed, "jammingState", 0), "?"),
                "safeBoot": bool(getattr(parsed, "safeBoot", 0)),
                "rtcCalib": bool(getattr(parsed, "rtcCalib", 0)),
                "xtalAbsent": bool(getattr(parsed, "xtalAbsent", 0)),
            })
        elif ident == "MON-RF":
            # MON-RF has no center-frequency field; block order is fixed by the
            # RF front-end hardware chain (confirmed against MON-SPAN's center
            # frequencies: block 0 = upper L-band, block 1 = lower L-band).
            n = parsed.nBlocks
            bands = []
            for i in range(1, n + 1):
                suf = f"_{i:02d}"
                band_name = "L1 / E1 / B1 (upper)" if i == 1 else "L2 / L5 / E5 (lower)"
                jam_ind = getattr(parsed, f"jamInd{suf}", 0)
                agc = getattr(parsed, f"agcCnt{suf}", 0)
                bands.append({
                    "blockId": getattr(parsed, f"blockId{suf}", i - 1),
                    "band": band_name,
                    "antennaStatus": ANTENNA_STATUS.get(getattr(parsed, f"antStatus{suf}", 0), "?"),
                    "antennaPower": ANTENNA_POWER.get(getattr(parsed, f"antPower{suf}", 0), "?"),
                    "jammingState": JAMMING_STATE.get(getattr(parsed, f"jammingState{suf}", 0), "?"),
                    "jamInd": jam_ind,
                    "jamPercent": round(jam_ind / 255.0 * 100, 1),
                    "noisePerMS": getattr(parsed, f"noisePerMS{suf}", 0),
                    "agcCnt": agc,
                    "agcPercent": round(agc / 8191.0 * 100, 1),
                    "postStatus": getattr(parsed, f"postStatus{suf}", 0),
                    "ofsI": getattr(parsed, f"ofsI{suf}", 0), "magI": getattr(parsed, f"magI{suf}", 0),
                    "ofsQ": getattr(parsed, f"ofsQ{suf}", 0), "magQ": getattr(parsed, f"magQ{suf}", 0),
                })
            state.rf_bands = bands
        elif ident == "MON-SPAN":
            n = parsed.numRfBlocks
            spectrum = []
            for i in range(1, n + 1):
                suf = f"_{i:02d}"
                bins = getattr(parsed, f"spectrum{suf}", [])
                spectrum.append({
                    "blockId": i - 1,
                    "centerHz": getattr(parsed, f"center{suf}", 0),
                    "spanHz": getattr(parsed, f"span{suf}", 0),
                    "resHz": getattr(parsed, f"res{suf}", 1),
                    "pga": getattr(parsed, f"pga{suf}", 0),
                    "bins": list(bins),
                })
            state.spectrum = spectrum
        elif ident == "MON-SYS":
            state.chip = {
                "bootType": {0: "Unknown", 1: "Cold start", 2: "Watchdog", 3: "Hardware reset",
                             4: "Hardware backup", 5: "Software backup", 6: "Software reset",
                             7: "VIO fail", 8: "VIO fail (backup)", 9: "V-Core HW fail",
                             10: "V-Core watchdog"}.get(parsed.bootType, f"Type {parsed.bootType}"),
                "cpuLoad": parsed.cpuLoad,
                "cpuLoadMax": parsed.cpuLoadMax,
                "memUsage": parsed.memUsage,
                "memUsageMax": parsed.memUsageMax,
                "ioUsage": parsed.ioUsage,
                "ioUsageMax": parsed.ioUsageMax,
                "runTime_s": parsed.runTime,
                "noticeCount": parsed.noticeCount,
                "warnCount": parsed.warnCount,
                "errorCount": parsed.errorCount,
                "tempC": parsed.tempValue,
            }
        elif ident == "MON-COMMS":
            n = parsed.nPorts
            protids = []
            i = 1
            while hasattr(parsed, f"protIds_{i:02d}"):
                protids.append(getattr(parsed, f"protIds_{i:02d}"))
                i += 1
            ports = []
            for i in range(1, n + 1):
                suf = f"_{i:02d}"
                pid = getattr(parsed, f"portId{suf}", 0)
                ptype = PORT_TYPE_NAMES.get(pid >> 8, f"0x{pid:04x}")
                sub = pid & 0xFF
                label = f"{ptype}" + (f".{sub}" if sub else "")
                msgs = {}
                for j, protid in enumerate(protids, start=1):
                    cnt = getattr(parsed, f"msgs{suf}_{j:02d}", 0)
                    if cnt:
                        msgs[PROTOCOL_NAMES.get(protid, f"Proto{protid}")] = cnt
                ports.append({
                    "label": label,
                    "txUsage": getattr(parsed, f"txUsage{suf}", 0),
                    "txPeakUsage": getattr(parsed, f"txPeakUsage{suf}", 0),
                    "rxUsage": getattr(parsed, f"rxUsage{suf}", 0),
                    "rxPeakUsage": getattr(parsed, f"rxPeakUsage{suf}", 0),
                    "txBytes": getattr(parsed, f"txBytes{suf}", 0),
                    "rxBytes": getattr(parsed, f"rxBytes{suf}", 0),
                    "overrunErrs": getattr(parsed, f"overrunErrs{suf}", 0),
                    "skipped": getattr(parsed, f"skipped{suf}", 0),
                    "msgs": msgs,
                })
            state.ports = ports


def serial_reader_loop():
    while True:
        try:
            ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
            _ser_holder["ser"] = ser
            with state.lock:
                state.connected = True
                state.port_error = None
            ubr = UBXReader(ser, protfilter=7)
            for raw, parsed in ubr:
                if parsed is None or raw is None:
                    continue
                with state.lock:
                    state.byte_rate.hit()
                ident = getattr(parsed, "identity", type(parsed).__name__)
                if ident.isdigit():
                    with state.lock:
                        if ident not in state.rtcm_stats:
                            state.rtcm_stats[ident] = RollingRate()
                        state.rtcm_stats[ident].hit()
                        state.msg_class_rates["RTCM3"].hit()
                        state.log_message("RTCM3", ident)
                elif ident.startswith(("GN", "GP", "GL", "GA", "GB", "GQ", "GI")):
                    # NMEA arrives at ~30/s (split GSV sentences); too noisy for the
                    # console log, so it only counts toward the rate stat.
                    with state.lock:
                        state.msg_class_rates["NMEA"].hit()
                else:
                    with state.lock:
                        state.msg_class_rates["UBX"].hit()
                        state.log_message("UBX", ident)
                    handle_ubx(parsed, ident)
                    if ident == "RXM-RAWX":
                        state.last_rawx_arrival = time.time()
                        state.write_rawlog_frame(raw)
                    elif ident == "CFG-MSG" and getattr(parsed, "msgClass", None) == "RXM" \
                            and getattr(parsed, "msgID", None) == "RXM-RAWX":
                        state.last_cfg_msg_rate_usb = getattr(parsed, "rateUSB", None)
        except (serial.SerialException, OSError) as e:
            with state.lock:
                state.connected = False
                state.port_error = str(e)
            _ser_holder["ser"] = None
            time.sleep(2)
        except StopIteration:
            time.sleep(1)


def _set_rawx_rate(rate):
    safe_write(UBXMessage("CFG", "CFG-MSG", SET, msgClass=0x02, msgID=0x15,
                           rateDDC=0, rateUART1=0, rateUART2=0,
                           rateUSB=rate, rateSPI=0))


def enable_rawx_autoreport():
    """Enable RXM-RAWX auto-output on the USB port only, at a divisor of
    RAWLOG_INTERVAL against the receiver's own 1Hz measurement clock. This
    replaces polling: a poll's send/receive round trip drifts under loop and
    serial jitter (measured ~31s in practice), which OPUS's format checker
    rejects (it requires exactly 1/2/3/5/10/15/30s and each epoch to land
    exactly on a matching second-of-minute, e.g. :00/:30 for a 30s rate).

    The rate divider's phase (which epoch counts as "0") is fixed at whatever
    moment the rate is (re)enabled, and the receiver's internal timebase is
    GPS time, not UTC - GPS time has no leap seconds and currently runs
    GPS_LEAP_SECONDS ahead of UTC (confirmed from this receiver's own
    RXM-RAWX.leapS field). RINEX files are labeled with the "GPS" time
    system, so the requirement is alignment in GPS time. Computing the
    boundary in raw UTC (as an earlier version of this function did) adds a
    non-multiple-of-30 offset before the alignment happens, which reliably
    shifted every epoch by exactly leapS seconds - confirmed empirically
    (kept landing on :18/:48 instead of :00/:30). So: disable, wait for a
    true GPS-time boundary, then re-enable exactly on it.

    All other ports are explicitly re-set to their current (0) rate so this
    cannot affect UART1/UART2, which is where RTCM reaches the rover.

    Self-verifying: after (re)enabling, this waits for the first resulting
    epoch and checks its actual wall-clock grid alignment, retrying the
    disable/wait/enable cycle if it's off, rather than trusting one attempt
    blindly - a misaligned run was found this way once (an OPUS rejection
    on data from an earlier restart the same day).

    Importantly, this only touches the rate at all if it's not already
    correctly enabled and aligned: the receiver keeps running regardless of
    this *process* restarting (only an actual power cycle resets it), so
    blindly re-toggling on every software restart only adds risk of
    introducing a fresh misalignment where a working one already existed -
    which is exactly what happened once. So: poll the current rate first,
    and if it's already correct, verify alignment without touching config
    at all; only fall through to the disable/realign/enable dance if the
    rate isn't set or the existing stream turns out to be misaligned."""
    def check_current_alignment(timeout_s):
        before = state.last_rawx_arrival
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if state.last_rawx_arrival != before:
                # arrival is a raw UTC epoch timestamp, but the alignment
                # target is in GPS time (UTC + GPS_LEAP_SECONDS) - checking
                # without this conversion gives a constant bogus offset
                # (30 - 18 = 12, exactly the phantom "12.03s" this was
                # chasing before the bug was found)
                arrival_gps = state.last_rawx_arrival + GPS_LEAP_SECONDS
                phase = arrival_gps % int(RAWLOG_INTERVAL)
                return min(phase, RAWLOG_INTERVAL - phase)
            time.sleep(0.2)
        return None  # nothing arrived to check

    # Check whether it's already enabled and aligned before touching anything -
    # the receiver keeps running regardless of this process restarting, so
    # blindly re-toggling on every software restart only risks introducing a
    # fresh misalignment where a working one already existed.
    misalign = check_current_alignment(int(RAWLOG_INTERVAL) + 2)
    if misalign is not None and misalign < 2.0:
        print(f"Already enabled and aligned (phase offset {misalign:.2f}s) - leaving it alone")
        return
    print(f"Not currently aligned ({'phase offset ' + str(round(misalign, 1)) + 's' if misalign is not None else 'nothing arrived to check'}) - (re)aligning")

    for attempt in range(1, 6):
        _set_rawx_rate(0)
        time.sleep(1.5)
        gps_now = time.time() + GPS_LEAP_SECONDS
        next_boundary_gps = (int(gps_now) // int(RAWLOG_INTERVAL) + 1) * int(RAWLOG_INTERVAL)
        wait = next_boundary_gps - gps_now
        print(f"Aligning RXM-RAWX output to GPS-time boundary {next_boundary_gps} "
              f"(waiting {wait:.3f}s, attempt {attempt})")
        time.sleep(max(0, wait))
        before = state.last_rawx_arrival
        _set_rawx_rate(int(RAWLOG_INTERVAL))
        print(f"RXM-RAWX enable sent at gps_time={time.time() + GPS_LEAP_SECONDS:.3f} "
              f"(target was {next_boundary_gps})")

        misalign = check_current_alignment(int(RAWLOG_INTERVAL) + 5)
        if misalign is None:
            print("No RXM-RAWX arrived to verify alignment against - proceeding anyway")
            return
        print(f"Grid phase offset {misalign:.2f}s")
        if misalign < 2.0:
            print("Alignment verified clean")
            return
        print(f"Alignment off by ~{misalign:.1f}s, retrying (waiting 3s before next attempt)")
        time.sleep(3)
    print("WARNING: could not verify clean RXM-RAWX alignment after 5 attempts, proceeding anyway")


def poller_loop():
    last_ver = 0
    last_slow = 0
    last_tmode = 0
    last_gnss_caps = 0
    last_nav_config = 0
    while True:
        now = time.time()
        if state.connected:
            for cls, ident in (("NAV", "NAV-PVT"), ("NAV", "NAV-SAT"), ("NAV", "NAV-DOP"),
                               ("NAV", "NAV-STATUS"), ("NAV", "NAV-SVIN"), ("NAV", "NAV-TIMEUTC"),
                               ("NAV", "NAV-CLOCK")):
                safe_write(UBXMessage(cls, ident, POLL))
                time.sleep(0.05)
            if now - last_slow > SLOW_POLL_INTERVAL:
                safe_write(UBXMessage("MON", "MON-HW", POLL))
                time.sleep(0.05)
                safe_write(UBXMessage("MON", "MON-COMMS", POLL))
                time.sleep(0.05)
                safe_write(UBXMessage("MON", "MON-RF", POLL))
                time.sleep(0.05)
                safe_write(UBXMessage("MON", "MON-SPAN", POLL))
                time.sleep(0.05)
                safe_write(UBXMessage("MON", "MON-SYS", POLL))
                last_slow = now
            if not state.receiver and now - last_ver > VER_RETRY_INTERVAL:
                safe_write(UBXMessage("MON", "MON-VER", POLL))
                last_ver = now
            if not state.tmode3 and now - last_tmode > VER_RETRY_INTERVAL:
                safe_write(UBXMessage("CFG", "CFG-TMODE3", POLL))
                last_tmode = now
            if not state.gnss_caps and now - last_gnss_caps > VER_RETRY_INTERVAL:
                safe_write(UBXMessage("MON", "MON-GNSS", POLL))
                last_gnss_caps = now
            if not state.nav_config and now - last_nav_config > VER_RETRY_INTERVAL:
                safe_write(UBXMessage("CFG", "CFG-NAV5", POLL))
                last_nav_config = now
        time.sleep(FAST_POLL_INTERVAL)


def read_vcgencmd_temp():
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=2).stdout
        return float(re.search(r"[\d.]+", out).group())
    except Exception:
        return None


def read_vcgencmd_throttled():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        bits = int(re.search(r"0x([0-9a-fA-F]+)", out).group(1), 16)
        return {
            "under_voltage_now": bool(bits & 0x1),
            "throttled_now": bool(bits & 0x4),
            "under_voltage_occurred": bool(bits & 0x10000),
            "throttled_occurred": bool(bits & 0x40000),
        }
    except Exception:
        return None


def system_stats_loop():
    psutil.cpu_percent(percpu=True)  # prime the internal sample window
    while True:
        try:
            vm = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            with state.lock:
                state.sysinfo = {
                    "cpuPercent": psutil.cpu_percent(),
                    "cpuPerCore": psutil.cpu_percent(percpu=True),
                    "memUsedPercent": vm.percent,
                    "memUsedBytes": vm.used,
                    "memTotalBytes": vm.total,
                    "diskUsedPercent": disk.percent,
                    "diskFreeBytes": disk.free,
                    "diskTotalBytes": disk.total,
                    "loadAvg": os.getloadavg(),
                    "cpuTempC": read_vcgencmd_temp(),
                    "throttled": read_vcgencmd_throttled(),
                    "bootTime": psutil.boot_time(),
                }
        except Exception:
            pass
        time.sleep(2)


# hook CFG-TMODE3 responses (handled in handle_ubx via ident switch already registered above)

app = Flask(__name__, static_folder="static", static_url_path="")
sock = Sock(app)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/rawlog/start/<float:hours>", methods=["POST"])
def api_rawlog_start(hours):
    path = state.start_rawlog(hours)
    return jsonify({"started": True, "path": path, "hours": hours})


@app.route("/api/rawlog/extend/<float:hours>", methods=["POST"])
def api_rawlog_extend(hours):
    ok = state.extend_rawlog(hours)
    return jsonify({"extended": ok, "new_end_in_hours": hours})


@app.route("/api/rawlog/stop", methods=["POST"])
def api_rawlog_stop():
    state.stop_rawlog()
    return jsonify({"stopped": True})


@sock.route("/ws")
def ws_handler(ws):
    period = 1.0 / BROADCAST_HZ
    while True:
        try:
            ws.send(json.dumps(state.snapshot()))
        except Exception:
            break
        time.sleep(period)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    threading.Thread(target=serial_reader_loop, daemon=True).start()
    time.sleep(2)  # let the serial connection establish
    # Do the UTC-boundary alignment while the channel is quiet - poller_loop's
    # continuous NAV-*/MON-* polling (once/sec) was found to delay the
    # receiver's processing of this CFG-MSG by as much as ~18s, throwing off
    # the alignment. Only start the poller after this completes.
    enable_rawx_autoreport()
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=system_stats_loop, daemon=True).start()
    path = state.resume_or_start_rawlog(default_hours=96)
    app.run(host="0.0.0.0", port=5000, threaded=True)
