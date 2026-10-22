#!/usr/bin/env python3
"""FAST1 / BENCH driver: T-0 preflight -> BLE gate -> per-slot capture with announcements and live gates. Stops on any failure.

Backend model (verified in code): the experiment backend is per-slot. Each slot = ONE nurse-forced capture (POST .../capture):
5 s pre-flight + 30 s window, result at GET .../readings (VitalsResult), plan/budget at GET .../measurement-control.
Toward the Jetson it only does the same two care-API calls the panel makes (capture, cuff); bench_check.sh additionally uses fast1_mode.sh.

Cuff readings arrive by appending lines to <run>/cuff_in.txt:  "M03 121/78/70"  "B0 125/80/72 + 123/79/71" (AVG2)  "GO BPUP1"  "STOP".
--dry-run: preflight only (no sensor).
"""
import argparse, hashlib, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
MANIFEST_SHA = "35a4539ca8c340123d23d8759225e7a74e1287e189f7596f5a8883149a31aa9a"
MODEL_SHA = "1f017653c5c4bba4ee1a96e97e18d4c97f672ce32f98c0e60f3bd31a5eb06e03"
VERSION = "prototype-continuous-1.1.0"
JETSON, JROOT, API = "veinnovate@192.168.55.1", "/home/veinnovate/veinnovate-stage13", "localhost:4173"
SERVICE = "veinnovate-stage13-edge.service"
CARE = "/v1/identity/care/sessions/"
TIERS = ("PROTOTYPE_HIGH_CONFIDENCE", "PROTOTYPE_LOW_CONFIDENCE", "PROTOTYPE_ARTIFACT")
PREFLIGHT_S = 5   # backend pre-flight before the 30-s window (measurement_policy: capture deadline >= 35 s)


class Stop(Exception):
    """Any failed gate/stop condition: nothing else starts."""


def utc(t=None): return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).isoformat(timespec="milliseconds")
def mmss(s): s = max(0, int(round(s))); return f"{s // 60:02d}:{s % 60:02d}"
def ts_of(v):
    """ISO string or epoch -> epoch seconds (None if unparseable)."""
    try: return float(v) if not isinstance(v, str) else datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except Exception: return None


class Edge:
    """View of the Jetson through ssh; `run` is injectable for tests."""
    def __init__(self, run=None): self.run = run or self._ssh

    @staticmethod
    def _ssh(cmd, stdin=None):
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", JETSON, cmd], input=stdin, capture_output=True, text=True, timeout=40)
        if p.returncode: raise Stop(f"ssh failed ({p.returncode}): {p.stderr.strip()[:200]}")
        return p.stdout

    def get(self, path): return json.loads(self.run(f"curl -sf {API}{path}"))

    def post(self, path, body):
        tok = os.environ.get("VEINNET_API_TOKEN")
        h = f" -H 'Authorization: Bearer {tok}'" if tok else ""
        return json.loads(self.run(f"curl -sf -X POST -H 'Content-Type: application/json'{h} --data-binary @- {API}{path}", json.dumps(body)))

    # typed accessors (the only place that knows the URL layout)
    def runtime(self): return self.get("/v1/runtime/status")["sessions"][0]
    def status(self, sid): return self.get(f"{CARE}{sid}/status")
    def control(self, sid): return self.get(f"{CARE}{sid}/measurement-control")
    def readings(self, sid): return self.get(f"{CARE}{sid}/readings")["readings"]
    def capture(self, sid, staff): return self.post(f"{CARE}{sid}/capture", {"staff": staff})
    def cuff(self, sid, body): return self.post(f"{CARE}{sid}/cuff", body)

    def calibration(self, uid="A90AFDAF672D66F1"):
        """Active (non-invalidated) calibration entries of the experiment adapter, read from the service's calibration store (read-only)."""
        out = self.run("python3 -c \"import json;d=json.load(open('/home/veinnovate/.local/state/veinnovate-stage13/calibration.json'))['active'];"
                       f"print(json.dumps([dict(sbp=r['baseline_cuff_sbp'],dbp=r['baseline_cuff_dbp'],t=r['captured_at_utc']) for r in d.values() if r['device_id']=='{uid}' and not r.get('invalidated')]))\"")
        return json.loads(out)


# ------------------------------------------------------------------ T-0 preflight
def preflight(edge, say, project=PROJECT, here=HERE):
    ok = lambda name, detail="": say(f"  PASS {name} {detail}".rstrip())
    def bad(name, detail): raise Stop(f"PREFLIGHT FAIL: {name} - {detail}")
    man = project / "output/prototype_continuous_20261020/SHA256SUMS.txt"
    if hashlib.sha256(man.read_bytes()).hexdigest() != MANIFEST_SHA: bad("manifest sha", "SHA256SUMS.txt != 35a4539c...")
    for f in (man, here / "SHA256SUMS_FAST1.txt"):
        if not f.exists(): bad("hash list", f"{f.name} missing")
        r = subprocess.run(["shasum", "-a", "256", "-c", str(f)], cwd=project, capture_output=True, text=True)
        if r.returncode: bad(f"Mac {f.name}", "; ".join(l for l in r.stdout.splitlines() if "FAILED" in l)[:300])
        ok(f"Mac {f.name}", f"{len(r.stdout.splitlines())} files OK")
    pins = {k: v for k, v in json.loads((here / "jetson_pins.json").read_text()).items() if not k.startswith("_")}
    got = dict(l.split()[::-1] for l in edge.run(f"cd {JROOT} && sha256sum {' '.join(pins)}").splitlines())
    for k, v in pins.items():
        if got.get(k) != v: bad("Jetson file hash", k)
    want = next(l.split()[0] for l in man.read_text().splitlines() if l.endswith("/patch/prototype_continuous.py"))
    if got["Hardware_integration_ready/receiver/prototype_continuous.py"] != want: bad("Jetson prototype_continuous.py", "!= manifest")
    ok("Jetson files", f"{len(pins)} pinned hashes == pins; prototype_continuous.py == manifest")
    kv = dict(l.split("=", 1) for l in edge.run(f"systemctl --user show {SERVICE} -p ActiveState -p NRestarts -p MainPID").splitlines())
    if kv["ActiveState"] != "active": bad("service", kv["ActiveState"])
    env = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in edge.run(f"tr '\\0' '\\n' </proc/{kv['MainPID']}/environ | grep ^VEINNET_").splitlines() if "=" in l}
    if env.get("VEINNET_PROTOTYPE_CONTINUOUS") != "1": bad("flag", "VEINNET_PROTOTYPE_CONTINUOUS=1 not in process env")
    h = edge.get("/v1/health")
    if h.get("model_sha256") != MODEL_SHA or not h.get("model_adapter_loaded"): bad("health model", str(h.get("model_sha256")))
    ok("Jetson service", f"active, NRestarts={kv['NRestarts']}, flag=1, health model 1f017653")
    return dict(restarts=int(kv["NRestarts"]), env=env)


# ------------------------------------------------------------------ gates on a VitalsResult (GET .../readings item)
def gate_reading(r, need_bp=False):
    """Shape (proved on a real replayed record: fixture_reading_real.json): provenance.sqi_version, provenance.model_sha256,
    hr.{bpm,reason}, spo2.{pct,status,algorithm_version}, bp.sbp_mmhg/dbp_mmhg, result_state, acquisition_end_utc, emitted_at_utc."""
    v = (r.get("provenance") or {}).get("sqi_version")
    if not (isinstance(v, str) and v.startswith(VERSION)): raise Stop(f"GATE: sqi_version {v!r} does not start with {VERSION}")
    m = (r.get("provenance") or {}).get("model_sha256")
    if m != MODEL_SHA: raise Stop(f"GATE: model {m}")
    hr, sp = r.get("hr") or {}, r.get("spo2") or {}
    if hr.get("bpm") is None or hr.get("reason") not in TIERS: raise Stop(f"GATE: hr.bpm/hr.reason missing or not a prototype tier: {hr}")
    if sp.get("pct") is None: raise Stop(f"GATE: spo2.pct missing: {sp}")
    if "UNCALIBRATED_MAXIM_LINEAR" not in str(sp.get("algorithm_version")): raise Stop(f"GATE: spo2 label missing: {sp.get('algorithm_version')}")
    bp = r.get("bp") or {}
    if need_bp and bp.get("sbp_mmhg") is None: raise Stop("GATE 2: calibrated window has no BP")
    end, emit = ts_of(r.get("acquisition_end_utc")), ts_of(r.get("emitted_at_utc"))
    return dict(version=v, hr=hr["bpm"], tier=hr["reason"], spo2=sp["pct"], sbp=bp.get("sbp_mmhg"), dbp=bp.get("dbp_mmhg"), state=r.get("result_state"),
                warnings=";".join((r.get("sqi") or {}).get("reason_codes") or []), cal=(r.get("calibration") or {}).get("status"),
                acq_start=r.get("acquisition_start_utc"), acq_end=r.get("acquisition_end_utc"), emitted=r.get("emitted_at_utc"),
                latency_s=round(emit - end, 3) if emit is not None and end is not None else None)


CUFF = re.compile(r"^(\w+)\s+(\d+)/(\d+)/(\d+)(?:\s*\+\s*(\d+)/(\d+)/(\d+))?\s*$")


class Run:
    def __init__(self, plan, edge, run_dir, say, clock=time.time, sleep=time.sleep, session=None, staff=None, placeholder_cuff=None, mirror=None):
        self.plan, self.edge, self.dir, self.say, self.clock, self.sleep = plan, edge, Path(run_dir), say, clock, sleep
        self.session, self.staff, self.placeholder, self.mirror = session, staff, placeholder_cuff, mirror
        self.go, self.cuffed, self.cuff_pos = set(), set(), 0
        self.seen, self.bad_integrity, self.last_ok_conn = set(), 0, clock()
        self.results, self.cal_done, self.need_bp_next, self.cal_expect = {}, False, False, None
        for f in ("cuff_events.jsonl", "events.jsonl", "live_results.jsonl", "slot_map.jsonl", "cuff_in.txt"): (self.dir / f).touch()

    def log(self, f, d):
        with open(self.dir / f, "a") as fh: fh.write(json.dumps(d) + "\n")

    def event(self, kind, **kw): self.log("events.jsonl", dict(t=utc(self.clock()), kind=kind, **kw))

    # -- operator input
    def read_cuff_in(self):
        lines = (self.dir / "cuff_in.txt").read_text().splitlines()
        new, self.cuff_pos = lines[self.cuff_pos:], len(lines)
        for ln in new:
            ln = ln.strip()
            if not ln: continue
            if ln.upper() == "STOP": raise Stop("operator STOP")
            if ln.upper().startswith("GO "): self.go.add(ln.split()[1]); self.say(f"GO received: {ln.split()[1]}"); continue
            self.save_cuff(ln)

    def save_cuff(self, ln):
        m = CUFF.match(ln)
        if not m: self.say(f"!! unreadable cuff line {ln!r} (format: SLOT s/d/h [+ s/d/h])"); return
        vals = [int(x) for x in m.groups()[1:] if x]; rd = [vals[i:i + 3] for i in range(0, len(vals), 3)]; slot = m.group(1)
        if slot not in {s["slot"] for s in self.plan["slots"]}: self.say(f"!! unknown slot {slot}"); return
        self.log("cuff_events.jsonl", dict(t=utc(self.clock()), kind="SAVE", slot=slot, values=rd[0], readings=rd, replaced=None))
        self.cuffed.add(slot); self.say(f"cuff saved {slot}: {' + '.join('/'.join(map(str, r)) for r in rd)}")
        if slot in self.plan["baseline_slots"] and not self.cal_done: self.calibrate(slot, rd)

    def calibrate(self, slot, rd):
        res = self.results.get(slot)
        if not res: raise Stop(f"GATE: no result recorded for {slot}; cannot calibrate")
        bid = (self.edge.status(self.session).get("baseline_reading_id")) or res["id"]
        body = dict(staff=self.staff, sbp=rd[0][0], dbp=rd[0][1], measured_at_utc=self.clock(), baseline_reading_id=bid)
        if len(rd) > 1: body |= dict(repeat_sbp=rd[1][0], repeat_dbp=rd[1][1])
        self.say(f"calibrating on {slot} ({len(rd)} cuff reading(s))")
        self.log("cuff_events.jsonl", dict(t=utc(self.clock()), kind="CALIBRATION_INTENT", slot=slot))
        reply = self.edge.cuff(self.session, body)
        self.log("cuff_events.jsonl", dict(t=utc(self.clock()), kind="CALIBRATION_ACK", slot=slot, reply=reply))
        end = self.clock() + 15
        while self.edge.status(self.session).get("calibration_status") not in ("VALID", "WARNING"):
            if self.clock() > end: raise Stop(f"{slot} calibration not active after POST (reply {reply}); the only allowed repeat is start_fast1.sh --b0r")
            self.sleep(1)
        self.cal_expect = (sum(r[0] for r in rd) / len(rd), sum(r[1] for r in rd) / len(rd))   # the service stores the mean of the cuff readings
        self.cal_done = self.need_bp_next = True; self.say(f"calibration ACTIVE on {slot}")

    # -- edge polling
    def poll(self):
        now = self.clock()
        try: conn = self.edge.runtime().get("connection_status")
        except Stop: raise
        except Exception as e: self.event("EXCEPTION", err=str(e)[:200]); return
        if conn == "CONNECTED": self.last_ok_conn = now
        elif now - self.last_ok_conn > 120: self.event("BLE_DROP", seconds=int(now - self.last_ok_conn)); raise Stop("STOP: BLE drop > 2 min")

    def wait_until(self, t, label=None, every=60):
        last = -1e9
        while self.clock() < t:
            self.read_cuff_in(); self.poll()
            if label and self.clock() - last >= every: self.say(f"{label}: {mmss(t - self.clock())} to go"); last = self.clock()
            self.sleep(min(2, max(0.0, t - self.clock())))

    def wait_cond(self, cond, timeout, msg):
        end = self.clock() + timeout
        while not cond():
            if self.clock() > end: raise Stop(msg)
            self.read_cuff_in(); self.poll(); self.sleep(1)

    def new_reading(self, deadline):
        """Wait for the result of the capture just requested (the only unseen reading)."""
        while True:
            for r in self.edge.readings(self.session):
                if r["reading_id"] not in self.seen:
                    self.seen.add(r["reading_id"]); return r
            if self.clock() > deadline: return None
            self.read_cuff_in(); self.poll(); self.sleep(1)

    def do_slot(self, i, s, ts, nxt, nx_delay, nx_manual):
        W, lead, name = self.plan["window_s"], self.plan["cuff_lead_s"], s["slot"]
        self.wait_until(ts - lead, f"{name} cuff in")
        self.say(f">>> CUFF NOW - {name} ({s['cuff']}): start the cuff so it overlaps the window (sensor starts in {mmss(lead)})")
        self.wait_until(ts)
        ctl, st = self.edge.control(self.session), self.edge.status(self.session)
        want_mode = self.plan.get("backend_mode", "EXPERIMENT_FAST1")
        if ctl.get("mode") != want_mode or ctl.get("state") != "ACTIVE" or ctl.get("attempts_reserved") != i:
            raise Stop(f"GATE: backend budget/contract {ctl} does not match slot {name} (#{i}, mode {want_mode})")
        if st.get("connection_status") != "CONNECTED" or not st.get("capture_available") or st.get("schedule_enabled") is not False:
            raise Stop(f"GATE: session not connected/idle/unscheduled: {st}")
        self.log("cuff_events.jsonl", dict(t=utc(self.clock()), kind="CAPTURE_INTENT", slot=name))
        ack = self.edge.capture(self.session, self.staff)
        if ack.get("requested") is not True: raise Stop(f"capture acknowledgement not confirmed ({ack}); reconcile, never retry blindly")
        self.log("cuff_events.jsonl", dict(t=utc(self.clock()), kind="CAPTURE_ACK", slot=name))
        self.log("slot_map.jsonl", dict(slot=name, start_utc=utc(ts + PREFLIGHT_S), end_utc=utc(ts + PREFLIGHT_S + W)))
        self.wait_until(ts + PREFLIGHT_S)
        self.say(f">>> WINDOW START - {name}: " + ("MOVE the sensor hand deliberately" if s["kind"] == "MOTION" else "hold the sensor hand STILL"))
        self.wait_until(ts + PREFLIGHT_S + W)
        when = f"in {mmss(ts + nx_delay - self.clock())}" if nx_delay is not None else "after owner GO" if nx_manual else "- last slot" if nxt == "END" else ""
        self.say(f">>> WINDOW END - {name}. Enter cuff: echo '{name} s/d/h' >> {self.dir}/cuff_in.txt   (next slot {nxt} {when})")
        r = self.new_reading(ts + PREFLIGHT_S + W + 30)
        if r is None:
            self.event("GAP", slot=name); self.bad_integrity += 1
            if name in self.plan["baseline_slots"] or self.bad_integrity >= 3: raise Stop(f"STOP: no result for {name} ({self.bad_integrity} consecutive failure(s))")
            self.say(f"!! no result for {name} (GAP logged)"); return
        g = gate_reading(r, need_bp=self.need_bp_next)
        self.bad_integrity = 0
        if self.need_bp_next:
            act = self.edge.calibration()
            if len(act) != 1 or abs(act[0]["sbp"] - self.cal_expect[0]) > 0.5 or abs(act[0]["dbp"] - self.cal_expect[1]) > 0.5:
                raise Stop(f"GATE 2: active calibration baseline cuff {act} != this run's baseline cuff entry {self.cal_expect} (stale/bench calibration?)")
            self.need_bp_next = False
            self.say(f"GATE 2 PASS: first calibrated window has BP + 1.1.0 + HR/SpO2; active calibration baseline cuff {act[0]['sbp']:.0f}/{act[0]['dbp']:.0f} == this run's B0 entry")
        self.results[name] = dict(id=r["reading_id"], **g)
        self.log("live_results.jsonl", dict(slot=name, id=r["reading_id"], seen_utc=utc(self.clock()), **g))
        self.say(f"  result {name}: BP {g['sbp'] and round(g['sbp'])}/{g['dbp'] and round(g['dbp'])} HR {g['hr']} {g['tier'].replace('PROTOTYPE_', '')} SpO2 {g['spo2']} "
                 f"[{g['warnings'] or '-'}] latency {g['latency_s']} s")
        if name in self.plan["baseline_slots"] and not self.cal_done:
            if self.placeholder and name not in self.cuffed: self.save_cuff(f"{name} {self.placeholder}")
            self.wait_cond(lambda: self.cal_done, 300, f"{name}: no cuff/calibration within 300 s of window end")
        if self.mirror:
            try: self.mirror()
            except Exception as e: self.event("MIRROR_FAIL", err=str(e)[:200])

    def execute(self):
        slots = self.plan["slots"]
        self.seen = {r["reading_id"] for r in self.edge.readings(self.session)}   # only results of THIS run count
        if self.plan.get("require_no_calibration_at_start"):
            st, act = self.edge.status(self.session), self.edge.calibration()
            if st.get("calibration_status") in ("VALID", "WARNING") or act:
                raise Stop(f"GATE 0: an active calibration exists before B0 (status {st.get('calibration_status')}, entries {act}) - bench/stale calibration must be cleared (fast1_mode.sh fast1)")
            self.say(f"  PASS no active calibration (status {st.get('calibration_status')}, 0 active entries for the adapter) - B0 will calibrate fresh")
        t0 = self.clock(); starts = {}
        self.say(f"RUN START {self.plan['run_id']}: rest {self.plan['rest_before_b0_s']} s, hand still, cuff on the OTHER arm")
        for i, s in enumerate(slots):
            at, name = s["at"], s["slot"]
            nx = slots[i + 1] if i + 1 < len(slots) else None
            if "rest_s" in at: ts = t0 + at["rest_s"]
            elif "after" in at: ts = max(starts[at["after"]] + at["delay_s"], self.clock() + self.plan["cuff_lead_s"])
            else:
                self.say(f"WAITING for owner: {name} - do the BP-raising method, hold the sensor hand still, then: echo 'GO {name}' >> {self.dir}/cuff_in.txt")
                while name not in self.go: self.read_cuff_in(); self.poll(); self.sleep(1)
                ts = self.clock() + self.plan["cuff_lead_s"]
            starts[name] = ts
            nxt = nx["slot"] if nx else "END"
            self.do_slot(i, s, ts, nxt, nx["at"]["delay_s"] if nx and nx["at"].get("after") == name else None, bool(nx and nx["at"].get("manual")))
        end = self.clock() + 180
        while len(self.cuffed) < len(slots) and self.clock() < end and not self.placeholder:   # the last slot's cuff is still arriving
            self.read_cuff_in(); self.poll(); self.sleep(1)
        miss = [x["slot"] for x in slots if x["slot"] not in self.cuffed]
        if self.mirror:
            try: self.mirror()
            except Exception as e: self.event("MIRROR_FAIL", err=str(e)[:200])
        self.say(f"RUN COMPLETE. cuff missing for: {', '.join(miss) or 'none'}. Next: analyze_fast1.py (see MORNING_GO.md)")


def ble_gate(edge, say, clock=time.time, sleep=time.sleep, timeout=180):
    t = clock()
    while edge.runtime().get("connection_status") != "CONNECTED":
        if clock() - t > timeout: raise Stop(f"BLE: not CONNECTED within {timeout} s (adapter ON? in range?)")
        sleep(3)
    sid = edge.runtime()["session_id"]; say("  PASS BLE connected"); return sid


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True); ap.add_argument("--plan", default=str(HERE / "fast1_plan.json"))
    ap.add_argument("--api-config", default=str(Path.home() / "VeinnovateRuns/panel15_config.json")); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--b0r", action="store_true", help="the one allowed repeat: B0 slot is run as B0R (needs fast1_mode.sh b0r first)")
    ap.add_argument("--placeholder-cuff", help="bench only: 's/d/h' auto-entered for B0 (NOT data)")
    ap.add_argument("--mirror-remote"); ap.add_argument("--skip-preflight", action="store_true")
    a = ap.parse_args(argv)
    say = lambda m: print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)
    plan = json.loads(Path(a.plan).read_text())
    if a.b0r:
        for s in plan["slots"]:
            if s["slot"] == "B0": s["slot"] = "B0R"
            if s["at"].get("after") == "B0": s["at"]["after"] = "B0R"
        plan["baseline_slots"] = ["B0R"]; plan["backend_mode"] = "EXPERIMENT_FAST1_B0R"
    run_dir = Path(a.run_dir).expanduser(); run_dir.mkdir(parents=True, exist_ok=True)
    edge = Edge()
    try:
        if not a.skip_preflight: say("T-0 PREFLIGHT"); preflight(edge, say)
        if a.dry_run:
            say("DRY RUN: preflight passed; BLE step / gates / timeline NOT executed (no sensor)."); return 0
        say("BLE connect gate (adapter must be ON and in place)")
        sid = ble_gate(edge, say)
        cfg = json.loads(Path(a.api_config).read_text())
        mirror = None
        if a.mirror_remote:
            import fast1_mirror
            mirror = lambda: fast1_mirror.once(JETSON, a.mirror_remote, run_dir)
        Run(plan, edge, run_dir, say, session=sid, staff=cfg["staff"], placeholder_cuff=a.placeholder_cuff, mirror=mirror).execute()
    except Stop as e:
        say(f"STOP: {e}"); return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
