#!/usr/bin/env python3
"""Finite, hardware-free timing and energy sensitivity report for Stage 16."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'receiver'))
from measurement_policy import CaptureSchedule, MeasurementPolicy, State, simulate_energy


def report():
    policy=MeasurementPolicy.load(ROOT/'config/measurement_policy_v1.json')
    schedule=CaptureSchedule(policy,now=0)
    schedule.transition(State.IDLE,now=0)
    attempts=[]
    now=0
    for due in (600,1800,3600):
        now=due
        if due==600: schedule.request_nurse()
        source,slot=schedule.take(now=now)
        schedule.transition(source,now=now)
        schedule.transition(State.TRANSMIT,now=now+5)
        schedule.finish(now=now+35,calibration_due=False)
        attempts.append({'source':source.value,'due_s':slot,'end_s':now+35,'response_s':35,
                         'evidence_class':'SIMULATED_NOT_TARGET_MEASURED'})
    assert [a['source'] for a in attempts]==['NURSE_FORCED_CAPTURE','SCHEDULED_CAPTURE','SCHEDULED_CAPTURE']
    assert schedule.next_due==5400
    sensitivities=[]
    for idle in (9.42,10.03):
        for active in (20,30,40):
            sensitivities.append({'idle_reference_ma':idle,'active_assumption_ma':active,
                'active_assumption_note':'Sensitivity scenario, not physical data or a verified component total',
                **simulate_energy(idle_current_ma=idle,active_current_ma=active,active_duration_s=35,
                                  interval_s=policy.scheduled_interval_s)})
    return {'schema_version':'1.0.0','policy_id':policy.policy_id,'interval_s':policy.scheduled_interval_s,
            'response_target_s':policy.capture_deadline_s,'preflight_budget_s':5,'fixed_window_s':30,
            'downstream_placeholder_s':5,'alternative':2,'clinical_interval_claim':False,
            'timing_attempts':attempts,'energy_sensitivity':sensitivities,
            'limitations':['Idle/advertising attribution is owner-designated',
                'Active draw, conversion losses and loaded usable capacity are not measured',
                'Real downstream latency is not measured; complete windows are never truncated to meet 40 s',
                'Nurse overrides add energy and never replace a scheduled obligation']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    value=report()
    text=json.dumps(value,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text)
    else: print(text,end='')
