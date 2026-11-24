#ifndef WISP_PROBE_DATA_H
#define WISP_PROBE_DATA_H
#define WAKE_BUCKETS 64
struct sched_counts {
    unsigned long long wakeups, migrations, switches, dropped;
    unsigned long long histogram[WAKE_BUCKETS];
};
struct rq_observation {
    unsigned long long timestamp_ns;
    unsigned int running;
};
#endif
