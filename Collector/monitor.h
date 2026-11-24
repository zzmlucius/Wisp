#ifndef WISP_MONITOR_H
#define WISP_MONITOR_H

#include <stdbool.h>
#include <signal.h>
#include <stdint.h>

#define MONITOR_SCHEMA_VERSION 1

typedef enum {
#define IND(id, name, unit, scope, source) IND_##id,
#include "Indicator.def"
#undef IND
    IND_COUNT
} IndicatorId;

typedef enum {
    MON_OK, MON_BASELINE, MON_UNAVAILABLE, MON_READ_ERROR, MON_RESET,
    MON_NO_EVENTS, MON_UNVERIFIED, MON_INCOMPLETE
} MonitorStatus;

typedef struct {
    const char *name, *unit, *scope, *source;
} IndicatorInfo;

typedef struct {
    uint64_t start_ns, end_ns;
} MonitorWindow;

typedef struct {
    uint64_t timestamp_monotonic_ns;
    double dt_s;
    MonitorStatus status[IND_COUNT];
    MonitorWindow window[IND_COUNT];
} SampleMeta;

typedef struct {
    const char *cpus;        /* Linux CPU list. NULL selects all online CPUs. */
    const char *fg_cgroup;   /* Optional cgroup v2 directories. */
    const char *bg_cgroup;
    const char *block_device;   /* Optional basename under /sys/block. */
    const char *bpf_object;     /* Optional compiled probe object. */
    bool runqueue_verified;    /* Set only after validating this kernel and scheduler. */
    const char *linux_root;    /* NULL for live Linux, alternate root for fixtures. */
    const volatile sig_atomic_t *stop_flag; /* Optional caller-owned flag. */
} MonitorConfig;

typedef struct MonitorContext MonitorContext;
typedef enum { MON_CONTINUE, MON_STOP, MON_SINK_ERROR } MonitorDecision;
typedef MonitorDecision (*MonitorSink)(const double vector[IND_COUNT],
                                       const SampleMeta *meta, void *userdata);

extern const IndicatorInfo MonitorIndicators[IND_COUNT];
const char *MonitorStatusName(MonitorStatus status);
MonitorContext *MonitorCreate(const MonitorConfig *config);
void MonitorDestroy(MonitorContext *ctx);
const char *MonitorProbeStatus(const MonitorContext *ctx, bool runqueue);
int snap(MonitorContext *ctx, double Vector[IND_COUNT], SampleMeta *meta);
/* Synchronous. Caller owns ctx; call MonitorDestroy on exit to release probes. */
int MonitorLoop(int interval, MonitorContext *ctx, MonitorSink sink, void *userdata);
int MonitorLoopNs(uint64_t interval_ns, MonitorContext *ctx, MonitorSink sink, void *userdata);
#endif
