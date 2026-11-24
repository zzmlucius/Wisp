#ifndef WISP_MONITOR_INTERNAL_H
#define WISP_MONITOR_INTERNAL_H
#include "monitor.h"
#include "probe_data.h"
#include <stddef.h>
#include <sys/types.h>

enum Source { CPU_STAT, CPU_PSI, IO_PSI, FG_USAGE, BG_USAGE, FG_PSI, BG_PSI,
              SCHED, DISK, MEM, RUNQUEUE, SOURCE_COUNT };

typedef struct {
    double value;
    MonitorStatus status;
    MonitorWindow window;
} Metric;

typedef struct {
    uint64_t value;
    MonitorWindow window;
    dev_t device;
    ino_t inode;
    bool valid;
} Counter;

typedef struct {
    int id;
    uint64_t ticks[8];
    bool present;
} Cpu;

typedef struct {
    Cpu *cpus;
    size_t count;
    MonitorWindow window;
    bool valid;
} CpuCounter;

typedef struct {
    int *ids;
    size_t count;
} CpuList;

struct Probe;
struct MonitorContext {
    CpuList selected, current_cpus;
    MonitorStatus selection_status;
    char *root, *fg, *bg, *device;
    Counter counters[SOURCE_COUNT];
    CpuCounter cpu;
    struct Probe *sched, *rq;
    struct sched_counts *sched_baseline, *sched_now;
    struct rq_observation *rq_now;
    CpuList possible, sched_cpus, rq_cpus;
    MonitorWindow sched_window, rq_window;
    bool sched_valid, rq_valid;
    char sched_status[160], rq_status[160];
    uint64_t last_ns;
    int clock_error;
    const volatile sig_atomic_t *stop_flag;
};

typedef void (*ReadSource)(MonitorContext *, Metric[IND_COUNT]);
extern const ReadSource MonitorSources[SOURCE_COUNT];
int monitor_now(uint64_t *ns);
int parse_cpu_list(const char *text, CpuList *list);
struct Probe *probe_create(const char *path, bool rq, size_t cpus, char status[160]);
int probe_read(struct Probe *probe, void *values);
void probe_destroy(struct Probe *probe);
#endif
