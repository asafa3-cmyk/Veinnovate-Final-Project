#ifndef VEINNET_BENCH_WARMUP_H
#define VEINNET_BENCH_WARMUP_H
// Experimental policy 1: count-bounded active warm-up, not proven settling.
static const uint8_t BENCH_WARMUP_POLICY = 1;
static const uint32_t BENCH_WARMUP_PAIRS = 500;
static const uint32_t BENCH_WARMUP_DEADLINE_MS = 8000;
struct BenchWarmup {
  bool enabled=false, active=false;
  uint32_t emitted=0, startedMs=0;
  uint64_t measurementBoundaryUs=0;
  void begin(uint32_t now) {
    enabled=active=true; emitted=0; startedMs=now; measurementBoundaryUs=0;
  }
  void legacy() { enabled=active=false; emitted=0; measurementBoundaryUs=0; }
  bool expired(uint32_t now) const {
    return active && uint32_t(now-startedMs) >= BENCH_WARMUP_DEADLINE_MS;
  }
};
#endif
