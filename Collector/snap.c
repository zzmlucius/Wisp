#include "internal.h"
#include <errno.h>
#include <math.h>

int snap(MonitorContext *ctx, double Vector[IND_COUNT], SampleMeta *meta)
{
    if (!ctx || !Vector || !meta) {
        errno = EINVAL;
        return -1;
    }
    uint64_t now;
    if (monitor_now(&now) < 0)
        return -1;
    *meta = (SampleMeta){ .timestamp_monotonic_ns = now,
        .dt_s = ctx->last_ns ? (now - ctx->last_ns) / 1e9 : 0 };
    Metric values[IND_COUNT];
    for (size_t i = 0; i < IND_COUNT; i++)
        values[i] = (Metric){ .value = NAN, .status = MON_UNAVAILABLE };
    ctx->clock_error = 0;
    for (size_t source = 0; source < SOURCE_COUNT; source++)
        MonitorSources[source](ctx, values);
    if (ctx->clock_error) {
        errno = ctx->clock_error;
        return -1;
    }
    for (size_t i = 0; i < IND_COUNT; i++) {
        Vector[i] = values[i].value;
        meta->status[i] = values[i].status;
        meta->window[i] = values[i].window;
    }
    ctx->last_ns = now;
    return 0;
}
