#!/usr/bin/env python3
"""One explicitly authorized C16/Z23 phase; never touches the historical ledger."""
from __future__ import annotations
import argparse, asyncio, base64, hashlib, json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'receiver'))
from c16_follow_on import FollowOnStream, FollowOnError, PHASES, UID, REQUIRED_CAPACITY, replay_stream
from preflight_stability import ContactBaseline
from stage9_capture import _subscribe_and_commit_profile, _start_capture_once
from stage9_transport_diagnostics import validate_external_batch_root, _write, _fsync_directory
from ble_identity import decode_identity_value
from ble_uuids import CHAR_CONTROL_UUID, CHAR_IDENTITY_UUID
from protocol_decoder import ControlCommand, encode_control_packet
from capture_quality_metrics import cardiac_quality_metrics
PLAN=ROOT/'config/stage9b_c16_z23_follow_on_002.review.json'

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def canonical(d):return (json.dumps(d,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()

def persist_phase(directory, stream, *, terminal_error=None):
    result=stream.result()
    if terminal_error:result.update(outcome='ABSTAIN',error=terminal_error)
    result['notification_archive']=stream.rows
    result['quality_metrics']=cardiac_quality_metrics(result['measurement_red'],result['measurement_ir'],100) if result['outcome']=='INTEGRITY_RETAINED' and stream.phase=='ACQ-01' else None
    result['ended_utc']=datetime.now(timezone.utc).isoformat()
    # Reservation remains immutable even if persistence is interrupted.
    staging=directory/'result.tmp'
    _write(staging,canonical(result));os.replace(staging,directory/'result.json')
    files=[directory/'result.json',directory/'reservation.json',*sorted((directory/'notifications').glob('*.jsonl'))]
    _write(directory/'manifest.sha256',''.join(f'{sha(p)}  {p.relative_to(directory)}\n' for p in files).encode())
    _fsync_directory(directory)
    verify_phase(directory)
    return result

def verify_phase(directory, contact_baseline=None):
    lines=(directory/'manifest.sha256').read_text().splitlines()
    files=[directory/'result.json',directory/'reservation.json',*sorted((directory/'notifications').glob('*.jsonl'))]
    if lines!=[f'{sha(p)}  {p.relative_to(directory)}' for p in files]:
        raise FollowOnError('phase manifest mismatch')
    r=json.loads((directory/'result.json').read_text())
    journals=list((directory/'notifications').glob('*.jsonl'))
    if journals:
        from notification_archive import replay_journal
        if len(journals)!=1:raise FollowOnError('ambiguous notification journal')
        replay_journal(journals[0])
        rows=[json.loads(line)['row'] for line in journals[0].read_text().splitlines()]
        if rows!=r['notification_archive']:raise FollowOnError('journal/export mismatch')
    baseline_payload=r.get('contact_baseline')
    baseline=contact_baseline or (ContactBaseline(**baseline_payload) if baseline_payload else None)
    replay=replay_stream(r['notification_archive'],phase=r['phase'],capacity=r['capacity'],start_ns=r['start_ns'],config_id=r.get('config_id',16),contact_baseline=baseline).result()
    for key,value in replay.items():
        if key not in ('outcome','error') and key in r and value!=r[key]:raise FollowOnError('phase replay mismatch: '+key)
    if r['outcome']=='INTEGRITY_RETAINED' and replay['outcome']!='INTEGRITY_RETAINED':raise FollowOnError('replay rejects retained phase')
    return r

def reserve_phase(root, phase, authorization, plan=PLAN):
    validate_external_batch_root(root)
    plan_experiment_id=json.loads(plan.read_text()).get('experiment_id')
    if not plan_experiment_id or root.name!=plan_experiment_id:
        raise FollowOnError('new experiment directory matching plan experiment_id required')
    a=json.loads(authorization.read_text())
    phases=a.get('authorized_phases')
    if (a.get('authorized') is not True or a.get('plan_sha256')!=sha(plan)
        or not (a.get('phase')==phase or phases==list(PHASES))):
        raise FollowOnError('explicit matching protocol authorization required')
    # Human checkpoint evidence is required for each separate phase, not inferred.
    for field in ('operator_checkpoint_utc','firmware_evidence_sha256','hardware_state_confirmed','rest_and_placement_confirmed'):
        if not a.get(field):raise FollowOnError('missing checkpoint: '+field)
    evidence_path=authorization.parent/a.get('firmware_evidence_path','MISSING')
    if not evidence_path.is_file() or sha(evidence_path)!=a['firmware_evidence_sha256']:
        raise FollowOnError('installed firmware evidence hash mismatch')
    evidence=json.loads(evidence_path.read_text())
    if (evidence.get('hardware_uid')!=UID or evidence.get('short_code')!='VN-001'
        or evidence.get('installed_verified') is not True or not evidence.get('verified_at_utc')
        or not evidence.get('artifact_sha256') or evidence.get('source_provenance_complete') is not True):
        raise FollowOnError('verified installed image and complete source provenance required')
    capacity=evidence.get('capture_capacity_pairs')
    FollowOnStream(phase,capacity=capacity)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for previous in PHASES[:PHASES.index(phase)]:
        r=verify_phase(root/previous)
        reservation=json.loads((root/previous/'reservation.json').read_text())
        if r['outcome']!='INTEGRITY_RETAINED' or reservation['plan_sha256']!=sha(plan):raise FollowOnError('previous phase failed or plan changed')
    directory=root/phase
    directory.mkdir(mode=0o700) # O_EXCL-equivalent atomic reservation; no overwrite/retry
    _write(directory/'reservation.json',canonical(a));_fsync_directory(directory);_fsync_directory(root)
    return directory,capacity

async def collect(client, disconnected, stream, journal=None, before_start=None):
    gate=None;terminal=None;wake=asyncio.Event()
    def callback(_sender,raw):
        ns=time.monotonic_ns();utc=datetime.now(timezone.utc).isoformat()
        try:
            if journal:journal.append(bytes(raw),host_monotonic_ns=ns,host_utc=utc)
            stream.ingest(bytes(raw),host_monotonic_ns=ns,host_utc=utc)
        except Exception as exc:stream.fail('PERSISTENCE_FAILED: '+str(exc))
        if stream.done:wake.set()
    try:
        identity=decode_identity_value(await asyncio.wait_for(client.read_gatt_char(CHAR_IDENTITY_UUID),8))
        if identity.hardware_uid!=UID or identity.short_code!='VN-001':raise FollowOnError('GATT identity mismatch')
        gate=await _subscribe_and_commit_profile(client=client,disconnected=disconnected,on_data=callback,expected_uid=UID,expected_config_id=stream.config_id)
        if before_start is not None:
            await before_start()
        await _start_capture_once(client=client,gate=gate,disconnected=disconnected,announce=lambda:stream.start(time.monotonic_ns()))
        while not stream.done:
            if disconnected.is_set():raise FollowOnError('DISCONNECTED')
            stream.tick(time.monotonic_ns())
            if stream.done:break
            try:await asyncio.wait_for(wake.wait(),.02)
            except asyncio.TimeoutError:pass
        if stream.error:terminal=stream.error
    except Exception as exc:
        terminal=str(exc);stream.fail(terminal)
    finally:
        if gate:gate.close()
        try:await asyncio.wait_for(client.write_gatt_char(CHAR_CONTROL_UUID,encode_control_packet(command=int(ControlCommand.STOP))),2)
        except Exception:terminal='STOP_UNCONFIRMED';stream.fail(terminal)
    if disconnected.is_set():terminal='DISCONNECTED';stream.fail(terminal)
    return terminal

async def run(root,phase,authorization,plan=PLAN):
    directory,capacity=reserve_phase(root,phase,authorization,plan)
    stream=FollowOnStream(phase,capacity=capacity);client=None;terminal=None
    from notification_archive import NotificationJournal
    journal=NotificationJournal(directory/'notifications')
    from bleak_adapter import BleakClientAdapter,scan_v2_adapters
    from live_ble_discovery import LiveBleDiscovery
    disconnected=asyncio.Event()
    try:
        discovery=LiveBleDiscovery(freshness_s=15)
        await scan_v2_adapters(discovery,timeout_s=10)
        candidates=discovery.fresh_candidates(UID)
        if len(candidates)!=1:raise FollowOnError('exactly one fresh UID candidate required')
        client=BleakClientAdapter(candidates[0].connection_handle,disconnected.set)
        await asyncio.wait_for(client.connect(),10)
        terminal=await collect(client,disconnected,stream,journal)
        for previous in PHASES[:PHASES.index(phase)]:
            if verify_phase(root/previous)['session_id']==stream.session:
                raise FollowOnError('session reused across phases')
    except BaseException as exc:terminal=str(exc) or type(exc).__name__;stream.fail(terminal)
    finally:
        if client:
            try:await asyncio.wait_for(client.disconnect(),3)
            except Exception:terminal='DISCONNECT_UNCONFIRMED';stream.fail(terminal)
        persist_phase(directory,stream,terminal_error=terminal)
    return 2 if terminal or stream.error else 0

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-root',type=Path,required=True)
    parser.add_argument('--phase',choices=PHASES,required=True)
    parser.add_argument('--authorization',type=Path,required=True)
    parser.add_argument('--plan',type=Path,default=PLAN)
    args=parser.parse_args()
    raise SystemExit(asyncio.run(run(args.batch_root,args.phase,args.authorization,args.plan)))
