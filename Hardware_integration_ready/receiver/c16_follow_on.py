"""Exact review-draft C16/Z23 stream state machine. No BLE or implicit retries."""
from __future__ import annotations
from dataclasses import asdict
from preflight_stability import ContactBaseline, evaluate_preflight, DEFAULT_MIN_SECONDS, DEFAULT_TIMEOUT_SECONDS
from protocol_decoder import decode_data_packet, MAX_SAMPLES_PER_PACKET, Flags
from notification_archive import notification_row, replay_notifications

UID='A90AFDAF672D66F1'
PHASES=('CAL-NC','CAL-Z23','ACQ-01')
RATE=100
PREFLIGHT_PAIRS=round(DEFAULT_MIN_SECONDS*RATE)
MEASUREMENT_PAIRS=3000
# PASS is committed on a notification boundary, never in the middle of an
# already acquired notification. At most 17 additional preflight samples;
# another 17 may accompany the last measurement sample. Retain both tails.
MAX_PREFLIGHT_PAIRS=PREFLIGHT_PAIRS+MAX_SAMPLES_PER_PACKET-1
REQUIRED_CAPACITY=MAX_PREFLIGHT_PAIRS+MEASUREMENT_PAIRS+MAX_SAMPLES_PER_PACKET-1

class FollowOnError(RuntimeError): pass

class FollowOnStream:
    def __init__(self, phase: str, *, capacity: int, config_id: int = 16, contact_baseline: ContactBaseline | None = None):
        if phase not in PHASES: raise FollowOnError('unsupported phase')
        if type(capacity) is not int or capacity < REQUIRED_CAPACITY:
            raise FollowOnError(f'firmware must support at least {REQUIRED_CAPACITY} pairs')
        if type(config_id) is not int or not 0 <= config_id <= 255:
            raise FollowOnError('config_id must be uint8')
        if contact_baseline is not None and contact_baseline.config_id != config_id:
            raise FollowOnError('contact baseline config_id mismatch')
        self.phase=phase;self.capacity=capacity;self.config_id=config_id;self.contact_baseline=contact_baseline;self.rows=[];self.red=[];self.ir=[]
        self.start_ns=None;self.session=None;self.last_device_us=None;self.last_host_ns=None
        self.preflight=None;self.window_start=None;self.done=False;self.error=None
        self.received_pairs=0;self.window_host_first=None;self.window_host_last=None

    def start(self, monotonic_ns):
        if self.start_ns is not None: raise FollowOnError('restart forbidden')
        self.start_ns=monotonic_ns

    def fail(self, reason):
        self.error=reason;self.done=True

    def tick(self, now_ns):
        if self.start_ns is None or self.done: return
        elapsed=(now_ns-self.start_ns)/1e9
        if self.phase=='ACQ-01' and self.preflight is None and elapsed>=DEFAULT_TIMEOUT_SECONDS:
            self.fail('PREFLIGHT_TIMEOUT')
        elif elapsed>= (45 if self.phase=='ACQ-01' else 5):
            self.fail('CAPTURE_TIMEOUT')

    def ingest(self, raw, *, host_monotonic_ns, host_utc):
        self.rows.append(notification_row(raw,index=len(self.rows),host_monotonic_ns=host_monotonic_ns,host_utc=host_utc))
        try:
            if self.start_ns is None: raise FollowOnError('DATA_BEFORE_START')
            self.tick(host_monotonic_ns)
            if self.error: return
            p=decode_data_packet(raw)
            if f'{p.hardware_uid:016X}'!=UID or p.sensor_config_id!=self.config_id or p.effective_sample_rate_hz!=RATE:
                raise FollowOnError('IDENTITY_CONFIG_RATE_MISMATCH')
            if self.session is not None and p.sensor_session_id!=self.session:
                raise FollowOnError('SESSION_CHANGED')
            if self.session is not None and p.flags & int(Flags.SESSION_RESET):
                raise FollowOnError('SESSION_RESET')
            if p.protocol_version != 3 or p.acquisition_uncertainty or p.software_overflow_count:
                raise FollowOnError('ACQUISITION_CONTINUITY_UNCERTAIN')
            if p.flags & int(Flags.FIFO_OVERFLOW_SINCE_LAST|Flags.CLIPPED_SAMPLE_PRESENT) or p.fifo_overflow_count:
                raise FollowOnError('OVERFLOW_OR_CLIPPING')
            if any(v>=262143 for v in p.red+p.ir): raise FollowOnError('CLIPPING')
            if p.first_sample_index!=self.received_pairs: raise FollowOnError('SAMPLE_ORDER_OR_LOSS')
            if self.last_device_us is not None and p.device_monotonic_timestamp_us<=self.last_device_us:
                raise FollowOnError('DEVICE_TIMING')
            if self.last_host_ns is not None and host_monotonic_ns<=self.last_host_ns:
                raise FollowOnError('HOST_TIMING')
            from datetime import datetime, timezone
            utc=datetime.fromisoformat(host_utc.replace('Z','+00:00'))
            if utc.utcoffset()!=timezone.utc.utcoffset(utc): raise FollowOnError('HOST_UTC')
            self.session=p.sensor_session_id;self.last_device_us=p.device_monotonic_timestamp_us
            self.last_host_ns=host_monotonic_ns
            self.received_pairs+=p.sample_count
            if self.received_pairs>self.capacity: raise FollowOnError('FIRMWARE_CAPACITY_EXCEEDED')
            if self.done: return # valid trailing STOP-flight notifications retained, not analyzed
            self.red.extend(p.red);self.ir.extend(p.ir)
            if self.phase!='ACQ-01':
                if len(self.red)>=200:
                    self.preflight=evaluate_preflight(self.red[:200],self.ir[:200],config_id=self.config_id,contact_baseline=self.contact_baseline)
                    expected='NO_CONTACT_DETECTED' if self.phase=='CAL-NC' else None
                    if (self.preflight.reason!=expected or (self.phase=='CAL-Z23' and not self.preflight.passed)):
                        raise FollowOnError('CALIBRATION_VERIFICATION_FAILED')
                    self.done=True
                return
            if self.preflight is None and len(self.red)>=PREFLIGHT_PAIRS:
                self.preflight=evaluate_preflight(self.red[:PREFLIGHT_PAIRS],self.ir[:PREFLIGHT_PAIRS],config_id=self.config_id,contact_baseline=self.contact_baseline)
                if not self.preflight.passed: raise FollowOnError('PREFLIGHT_'+str(self.preflight.reason))
                self.window_start=len(self.red)
                if self.window_start>MAX_PREFLIGHT_PAIRS: raise FollowOnError('PREFLIGHT_BUDGET')
            elif self.window_start is not None:
                if self.window_host_first is None:self.window_host_first=host_monotonic_ns
                self.window_host_last=host_monotonic_ns
                if len(self.red)-self.window_start>=MEASUREMENT_PAIRS:self.done=True
        except Exception as exc:
            self.fail(str(exc))

    def result(self):
        start=self.window_start
        red=self.red[start:start+MEASUREMENT_PAIRS] if start is not None else []
        ir=self.ir[start:start+MEASUREMENT_PAIRS] if start is not None else []
        return {'phase':self.phase,'config_id':self.config_id,'outcome':'ABSTAIN' if self.error or not self.done else 'INTEGRITY_RETAINED',
                'error':self.error,'capacity':self.capacity,'required_capacity':REQUIRED_CAPACITY,
                'contact_baseline':asdict(self.contact_baseline) if self.contact_baseline else None,
                'start_ns':self.start_ns,'session_id':self.session,'preflight':asdict(self.preflight) if self.preflight else None,
                'preflight_evaluated_pairs':PREFLIGHT_PAIRS if self.phase=='ACQ-01' and self.preflight else None,
                'measurement_first_index':start,'measurement_last_index':start+2999 if start is not None and len(red)==3000 else None,
                'measurement_red':red,'measurement_ir':ir,'notification_count':len(self.rows),
                'received_pairs':self.received_pairs,'window_first_host_ns':self.window_host_first,'window_last_host_ns':self.window_host_last}

def replay_stream(rows, *, phase, capacity, start_ns, config_id=16, contact_baseline=None):
    replay_notifications(rows)
    stream=FollowOnStream(phase,capacity=capacity,config_id=config_id,contact_baseline=contact_baseline)
    if start_ns is not None:stream.start(start_ns)
    import base64
    for row in rows:
        stream.ingest(base64.b64decode(row['raw_base64']),host_monotonic_ns=row['host_monotonic_ns'],host_utc=row['host_utc'])
    return stream
