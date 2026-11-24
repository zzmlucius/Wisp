#include "vmlinux.h"
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "probe_data.h"

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, unsigned int);
    __type(value, struct sched_counts);
} counts SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, int);
    __type(value, __u64);
} pending SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, unsigned int);
    __type(value, struct rq_observation);
} rq_latest SEC(".maps");

static __always_inline struct sched_counts *local_counts(void)
{
    unsigned int zero = 0;
    return bpf_map_lookup_elem(&counts, &zero);
}

static __always_inline int wake(int tid)
{
    struct sched_counts *c = local_counts();
    if (!c || tid <= 0)
        return 0;
    __sync_fetch_and_add(&c->wakeups, 1);
    __u64 now = bpf_ktime_get_ns();
    long error = bpf_map_update_elem(&pending, &tid, &now, BPF_NOEXIST);
    /* A duplicate event keeps the first enqueue timestamp. */
    if (error && error != -17)
        __sync_fetch_and_add(&c->dropped, 1);
    return 0;
}

SEC("tp/sched/sched_wakeup")
int record_wakeup(struct trace_event_raw_sched_wakeup_template *event)
{
    return wake(event->pid);
}

SEC("tp/sched/sched_wakeup_new")
int record_new(struct trace_event_raw_sched_wakeup_template *event)
{
    return wake(event->pid);
}

static __always_inline unsigned int latency_bucket(__u64 ns)
{
    unsigned int bucket = 0;
#define STEP(shift) if (ns >> shift) { ns >>= shift; bucket += shift; }
    STEP(32)
    STEP(16)
    STEP(8)
    STEP(4)
    STEP(2)
    STEP(1)
#undef STEP
    return bucket & (WAKE_BUCKETS - 1);
}

SEC("tp/sched/sched_switch")
int record_switch(struct trace_event_raw_sched_switch *event)
{
    struct sched_counts *c = local_counts();
    if (!c)
        return 0;
    __sync_fetch_and_add(&c->switches, 1);
    int next = event->next_pid;
    __u64 *start = bpf_map_lookup_elem(&pending, &next);
    if (start) {
        __u64 when = *start, now = bpf_ktime_get_ns();
        bpf_map_delete_elem(&pending, &next);
        if (now >= when)
            __sync_fetch_and_add(&c->histogram[latency_bucket(now - when)], 1);
    }
    return 0;
}

SEC("tp/sched/sched_process_exit")
int clear_exit(void *event)
{
    int tid = (int)bpf_get_current_pid_tgid();
    bpf_map_delete_elem(&pending, &tid);
    return 0;
}

SEC("tp/sched/sched_migrate_task")
int record_migration(void *event)
{
    struct sched_counts *c = local_counts();
    if (c)
        __sync_fetch_and_add(&c->migrations, 1);
    return 0;
}

SEC("kprobe/update_rq_clock")
int BPF_KPROBE(record_rq, struct rq *rq)
{
    unsigned int cpu = 0;
    struct rq_observation value = {0};
    if (bpf_core_field_exists(rq->cpu) && BPF_CORE_READ_INTO(&cpu, rq, cpu))
        return 0;
    if (BPF_CORE_READ_INTO(&value.running, rq, nr_running))
        return 0;
    value.timestamp_ns = bpf_ktime_get_ns();
    bpf_map_update_elem(&rq_latest, &cpu, &value, BPF_ANY);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
