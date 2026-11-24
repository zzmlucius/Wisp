#define _POSIX_C_SOURCE 200809L
#include "internal.h"
#include <errno.h>
#include <time.h>

int monitor_now(uint64_t *ns)
{
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time) < 0)
        return -1;
    *ns = (uint64_t)time.tv_sec * 1000000000 + time.tv_nsec;
    return 0;
}

int MonitorLoopNs(uint64_t period, MonitorContext *ctx, MonitorSink sink, void *userdata)
{
    uint64_t deadline;
    if (!period || period > (uint64_t)INT32_MAX * 1000000000 || !ctx || !sink) {
        errno = EINVAL;
        return -1;
    }
    if (monitor_now(&deadline) < 0)
        return -1;
    for (;;) {
        if (ctx->stop_flag && *ctx->stop_flag)
            return 0;
        double vector[IND_COUNT];
        SampleMeta meta;
        if (snap(ctx, vector, &meta) < 0)
            return -1;
        MonitorDecision decision = sink(vector, &meta, userdata);
        if (decision == MON_STOP)
            return 0;
        if (decision != MON_CONTINUE) {
            errno = ECANCELED;
            return -1;
        }
        uint64_t now;
        if (monitor_now(&now) < 0)
            return -1;
        deadline += ((now - deadline) / period + 1) * period;
        struct timespec target = { .tv_sec = deadline / 1000000000,
                                   .tv_nsec = deadline % 1000000000 };
        int error;
        do {
            error = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, NULL);
            if (ctx->stop_flag && *ctx->stop_flag)
                return 0;
        } while (error == EINTR);
        if (error) {
            errno = error;
            return -1;
        }
    }
}

int MonitorLoop(int interval, MonitorContext *ctx, MonitorSink sink, void *userdata)
{
    if (interval < 0) {
        errno = EINVAL;
        return -1;
    }
    return MonitorLoopNs((uint64_t)(interval ? interval : 1) * 1000000000,
                         ctx, sink, userdata);
}
