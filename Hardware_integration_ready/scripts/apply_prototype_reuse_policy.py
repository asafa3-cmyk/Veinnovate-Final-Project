"""Explicit owner-authorized maintenance; run with edge stopped and backed up."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'receiver'))
from adapter_inventory import AdapterRecord, PROTOTYPE_REUSE_POLICY, DEVELOPMENT_CODES
from identity_service import IdentityService

def apply(db_dir, policy_path, *, calibration_path):
    policy=json.loads(Path(policy_path).read_text())
    if policy['policy_version'] != PROTOTYPE_REUSE_POLICY:
        raise ValueError('Unsupported prototype policy')
    records=[AdapterRecord(**r) for r in policy['registered_identities']]
    if {r.short_code for r in records} != DEVELOPMENT_CODES:
        raise ValueError('Policy must identify the three registered development units')
    state=Path(db_dir)
    protected=[state/'patient_assignments.sqlite3',state/'measurement_sessions.sqlite3',Path(calibration_path)]
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected if p.exists()}
    service=IdentityService.from_db_dir(state)
    # Preflight all bindings before changing any policy or consumed flag.
    for r in records:
        if service.assignment_store.get_active_by_uid(r.hardware_uid) or service.session_store.get_active_by_uid(r.hardware_uid):
            raise ValueError('Active binding blocks prototype-policy maintenance')
        existing=service.inventory.get_by_code(r.short_code)
        if existing and existing.hardware_uid != r.hardware_uid:
            raise ValueError('Registered inventory identity conflict')
    results=[]
    for r in records:
        service.inventory.restore_development_record(r,source_ref=policy['source_inventory']+'#sha256='+policy['source_sha256'])
        row=service.authorize_prototype_reuse(r.short_code,r.hardware_uid,authorization_ref=policy['authorization_ref'],recover=True)
        results.append({'short_code':row.short_code,'consumed':row.consumed})
    after={str(p):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in protected if p.exists()}
    if before != after:
        raise RuntimeError('Protected assignment/session/calibration files changed')
    return {'policy_version':PROTOTYPE_REUSE_POLICY,'adapters':results,'ended_history_and_calibration_unchanged':True}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-dir',type=Path,required=True)
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--calibration',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(apply(args.db_dir,args.policy,calibration_path=args.calibration)))
