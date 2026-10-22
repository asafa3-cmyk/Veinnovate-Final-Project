#ifndef VEINNET_BENCH_FINAL_WITNESS_H
#define VEINNET_BENCH_FINAL_WITNESS_H
#include "protocol.h"

struct BenchFinalWitness {
  bool armed=false, ready=false, pending=false;
  uint64_t nonce=0;
  FinalIntegrityWitness value{};
  WarmupFinalIntegrityWitness warmupValue{};
  bool warmup=false;
  void arm(uint64_t token, bool withWarmup=false) {
    nonce=token; armed=token!=0; ready=pending=false; warmup=withWarmup;
  }
  void freeze(uint64_t uid, uint32_t session, uint32_t samples, uint8_t config,
              uint32_t hw, uint32_t sw, uint8_t uncertainty, uint8_t outcome,
              uint64_t timestamp) {
    if (!armed || ready) return;
    value={VEINNET_PROTOCOL_VERSION,4,1,uid,nonce,session,samples,config,
           hw,sw,uncertainty,outcome,timestamp,0};
    armed=false; ready=pending=true;
  }
  void freezeWarmup(uint32_t pairs, uint64_t transition) {
    warmupValue={value.protocol_version,4,2,value.hardware_uid,
      value.acquisition_nonce,value.sensor_session_id,value.emitted_samples,
      value.sensor_config_id,value.fifo_overflow_count,value.software_overflow_count,
      value.acquisition_uncertainty,value.outcome,value.boundary_timestamp_us,
      pairs,transition,0};
  }
  const uint8_t* bytes() const {
    return warmup ? (const uint8_t*)&warmupValue : (const uint8_t*)&value;
  }
  uint16_t size() const { return warmup ? WARMUP_FINAL_WITNESS_SIZE : FINAL_WITNESS_SIZE; }
};
#endif
