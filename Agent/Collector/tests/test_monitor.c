#define _GNU_SOURCE
#include "monitor.h"

#include <assert.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

static void fixture(const char *root, const char *name, const char *value)
{
    char *path;
    assert(asprintf(&path, "%s/%s", root, name) >= 0);
    if (value) {
        FILE *file = fopen(path, "w");
        assert(file && fputs(value, file) >= 0 && !fclose(file));
    } else
        assert(!unlink(path));
    free(path);
}

static void directory(const char *root, const char *name)
{
    char *path;
    assert(asprintf(&path, "%s/%s", root, name) >= 0);
    assert(!mkdir(path, 0700));
    free(path);
}

static void close_to(double actual, double expected)
{
    assert(isfinite(actual) && fabs(actual - expected) < 0.0001);
}

static MonitorDecision stop_after_one(const double *vector, const SampleMeta *meta, void *data)
{
    (void)vector;
    (void)meta;
    ++*(int *)data;
    return MON_STOP;
}

int main(void)
{
    char root[] = "/tmp/wisp-monitor-test.XXXXXX";
    assert(mkdtemp(root));
    const char *dirs[] = {"proc", "proc/pressure", "sys", "sys/devices", "sys/devices/system",
        "sys/devices/system/cpu", "sys/block", "sys/block/mock", "front", "back"};
    for (size_t i = 0; i < sizeof(dirs)/sizeof(*dirs); i++)
        directory(root, dirs[i]);

    const char *online = "sys/devices/system/cpu/online";
    const char *stat = "proc/stat", *cpu = "proc/pressure/cpu", *io = "proc/pressure/io";
    fixture(root, online, "0-1,3\n");
    fixture(root, stat, "cpu0 100 0 0 100 0 0 0 0 900 0\n"
        "cpu3 100 0 0 100 0 0 0 0 900 0\nctxt 100\nprocs_running 6\n");
    fixture(root, cpu, "some avg10=0.00 total=1000\nfull avg10=0.00 total=9000\n");
    fixture(root, io, "some avg10=0.00 total=0\n");
    fixture(root, "proc/meminfo", "MemTotal: 10000 kB\nMemAvailable: 6000 kB\n");
    fixture(root, "sys/block/mock/inflight", "0 4\n");
    fixture(root, "front/cpu.stat", "usage_usec 500\n");
    fixture(root, "front/cpu.pressure", "some avg10=0.00 total=3\n");
    fixture(root, "back/cpu.stat", "usage_usec 300\n");
    fixture(root, "back/cpu.pressure", "some avg10=0.00 total=0\n");
    MonitorConfig config = {.cpus = "0,3", .fg_cgroup = "/front", .bg_cgroup = "/back",
                            .block_device = "mock", .linux_root = root};
    MonitorContext *ctx = MonitorCreate(&config);
    assert(ctx && IND_COUNT == 19 && IND_RUNQUEUE_MAX == 18 && IND_FG_USAGE == 6);
    double values[IND_COUNT];
    SampleMeta first, second;
    assert(!snap(ctx, values, &first));
    assert(first.dt_s == 0 && first.status[IND_CPU_MEAN] == MON_BASELINE);
    assert(first.status[IND_WAKEUPS] == MON_UNAVAILABLE && isnan(values[IND_WAKEUPS]));
    assert(first.status[IND_CPU_PSI] == MON_BASELINE && isnan(values[IND_CPU_PSI]));
    close_to(values[IND_DISK_READ], 0);
    close_to(values[IND_DISK_WRITE], 4);
    close_to(values[IND_RUNNABLE], 2);
    close_to(values[IND_MEM_AVAILABLE], 6000);

    fixture(root, stat, "cpu0 120 0 0 180 0 0 0 0 90000 0\n"
        "cpu3 170 0 0 120 10 0 0 0 90000 0\nctxt 120\nprocs_running 3\n");
    fixture(root, cpu, "some avg10=0.00 total=1001\n");
    fixture(root, "front/cpu.stat", "usage_usec 505\n");
    assert(!snap(ctx, values, &second));
    assert(second.dt_s > 0);
    close_to(values[IND_CPU_MEAN], 45);
    close_to(values[IND_CPU_P90], 70);
    close_to(values[IND_IOWAIT_STDDEV], 5);
    close_to(values[IND_RUNNABLE], 1);
    close_to(values[IND_IO_PSI], 0);
    close_to(values[IND_BG_USAGE], 0);
    assert(second.status[IND_CPU_PSI] == MON_OK && values[IND_CPU_PSI] > 0);
    assert(second.status[IND_FG_USAGE] == MON_OK && values[IND_FG_USAGE] > 0);
    double psi_elapsed_ns = 1e5 / values[IND_CPU_PSI];
    assert(psi_elapsed_ns <= (double)(second.window[IND_CPU_PSI].end_ns -
                                      first.window[IND_CPU_PSI].start_ns));
    assert(second.window[IND_CPU_PSI].start_ns == first.window[IND_CPU_PSI].start_ns);
    assert(second.window[IND_CPU_PSI].end_ns > first.window[IND_CPU_PSI].end_ns);

    fixture(root, online, "0-1\n");
    fixture(root, cpu, "some total=12\n");
    fixture(root, io, "some bad_format\n");
    fixture(root, "front/cpu.stat", NULL);
    assert(!snap(ctx, values, &second));
    assert(second.status[IND_CPU_MEAN] == MON_RESET && isnan(values[IND_CPU_MEAN]));
    assert(second.status[IND_CPU_PSI] == MON_RESET && isnan(values[IND_CPU_PSI]));
    assert(second.status[IND_IO_PSI] == MON_READ_ERROR);
    assert(second.status[IND_FG_USAGE] == MON_UNAVAILABLE);
    fixture(root, "front/cpu.stat", "usage_usec 99999\n");
    fixture(root, io, "some total=0\n");
    assert(!snap(ctx, values, &second));
    assert(second.status[IND_FG_USAGE] == MON_BASELINE);
    assert(second.status[IND_IO_PSI] == MON_BASELINE);

    int calls = 0;
    assert(MonitorLoop(-1, ctx, stop_after_one, &calls) < 0 && errno == EINVAL);
    assert(!MonitorLoop(0, ctx, stop_after_one, &calls) && calls == 1);
    MonitorDestroy(ctx);
    const char *files[] = {online, stat, cpu, io, "proc/meminfo", "sys/block/mock/inflight",
        "front/cpu.stat", "front/cpu.pressure", "back/cpu.stat", "back/cpu.pressure"};
    for (size_t i = 0; i < sizeof(files)/sizeof(*files); i++)
        fixture(root, files[i], NULL);
    for (size_t i = sizeof(dirs)/sizeof(*dirs); i; i--) {
        char *path;
        assert(asprintf(&path, "%s/%s", root, dirs[i-1]) >= 0);
        assert(!rmdir(path));
        free(path);
    }
    assert(!rmdir(root));
}
