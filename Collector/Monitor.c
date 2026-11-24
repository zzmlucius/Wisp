#define _GNU_SOURCE
#include "internal.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/magic.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

const IndicatorInfo MonitorIndicators[IND_COUNT] = {
#define IND(id, name, unit, scope, source) [IND_##id] = { #name, unit, scope, #source },
#include "Indicator.def"
#undef IND
};

static const enum Source bindings[IND_COUNT] = {
#define IND(id, name, unit, scope, source) [IND_##id] = source,
#include "Indicator.def"
#undef IND
};

const char *MonitorStatusName(MonitorStatus status)
{
    static const char *const names[] = {"ok", "baseline", "unavailable", "read_error",
        "reset", "no_events", "unverified", "incomplete"};
    return (unsigned)status < sizeof(names) / sizeof(*names) ? names[status] : "unknown";
}

static int int_compare(const void *a, const void *b)
{
    int left = *(const int *)a, right = *(const int *)b;
    return (left > right) - (left < right);
}

static int double_compare(const void *a, const void *b)
{
    double left = *(const double *)a, right = *(const double *)b;
    return (left > right) - (left < right);
}

static bool number(const char **text, uint64_t *value)
{
    const char *at = *text;
    while (isspace((unsigned char)*at))
        at++;
    if (!isdigit((unsigned char)*at))
        return false;
    char *end;
    errno = 0;
    unsigned long long parsed = strtoull(at, &end, 10);
    if (errno)
        return false;
    *text = end;
    *value = parsed;
    return true;
}

int parse_cpu_list(const char *text, CpuList *list)
{
    CpuList parsed = {0};
    size_t capacity = 0;
    const size_t limit = 1U << 20;
    do {
        uint64_t first, last;
        if (!number(&text, &first))
            goto invalid;
        last = first;
        if (*text == '-') {
            text++;
            if (!number(&text, &last))
                goto invalid;
        }
        if (last < first || last >= limit || last - first + 1 > limit - parsed.count)
            goto invalid;
        size_t needed = parsed.count + last - first + 1;
        if (capacity < needed) {
            capacity = needed > 2 * capacity ? needed : 2 * capacity;
            int *ids = realloc(parsed.ids, capacity * sizeof(*ids));
            if (!ids)
                goto fail;
            parsed.ids = ids;
        }
        for (uint64_t id = first; id <= last; id++)
            parsed.ids[parsed.count++] = (int)id;
        if (*text != ',')
            break;
        text++;
    } while (true);
    while (isspace((unsigned char)*text))
        text++;
    if (*text)
        goto invalid;
    qsort(parsed.ids, parsed.count, sizeof(*parsed.ids), int_compare);
    for (size_t i = 1; i < parsed.count; i++)
        if (parsed.ids[i] == parsed.ids[i - 1])
            goto invalid;
    *list = parsed;
    return 0;
invalid:
    errno = EINVAL;
fail:
    free(parsed.ids);
    return -1;
}

static bool contains(const CpuList *list, int id)
{
    return list->count && bsearch(&id, list->ids, list->count, sizeof(id), int_compare) != NULL;
}

static bool same_cpus(const CpuList *a, const CpuList *b)
{
    return a->count == b->count &&
           (!a->count || !memcmp(a->ids, b->ids, a->count * sizeof(*a->ids)));
}

static void metric(Metric *m, MonitorStatus status, double value, MonitorWindow window)
{
    *m = (Metric){ .status = status, .value = status == MON_OK ? value : NAN, .window = window };
}

static void source_status(Metric *m, enum Source source, MonitorStatus status,
                          MonitorWindow window)
{
    for (size_t i = 0; i < IND_COUNT; i++)
        if (bindings[i] == source)
            metric(&m[i], status, 0, window);
}

static int stamp(MonitorContext *ctx, uint64_t *ns)
{
    if (monitor_now(ns) == 0)
        return 0;
    ctx->clock_error = errno;
    *ns = 0;
    return -1;
}

static MonitorStatus file_error(void)
{
    return errno == ENOENT || errno == ENOTDIR || errno == EACCES || errno == EPERM
        ? MON_UNAVAILABLE : MON_READ_ERROR;
}

static FILE *system_file(MonitorContext *ctx, const char *name)
{
    char *path;
    if (asprintf(&path, "%s%s", ctx->root, name) < 0)
        return NULL;
    FILE *file = fopen(path, "re");
    int error = errno;
    free(path);
    errno = error;
    return file;
}

static MonitorStatus online_cpus(MonitorContext *ctx, const char *name, CpuList *list)
{
    FILE *file = system_file(ctx, name);
    if (!file)
        return file_error();
    char *line = NULL;
    size_t capacity = 0;
    MonitorStatus status = MON_READ_ERROR;
    if (getline(&line, &capacity, file) >= 0 && !parse_cpu_list(line, list))
        status = MON_OK;
    free(line);
    fclose(file);
    return status;
}

static MonitorStatus selected_cpus(MonitorContext *ctx, CpuList *selected, size_t *count)
{
    MonitorStatus status = online_cpus(ctx, "/sys/devices/system/cpu/online", selected);
    if (status != MON_OK)
        return status;
    *count = selected->count;
    if (ctx->selected.count) {
        size_t kept = 0;
        for (size_t i = 0; i < selected->count; i++)
            if (contains(&ctx->selected, selected->ids[i]))
                selected->ids[kept++] = selected->ids[i];
        selected->count = kept;
    }
    return MON_OK;
}

static uint64_t midpoint(MonitorWindow window)
{
    return window.start_ns + (window.end_ns - window.start_ns) / 2;
}

static void rate(MonitorContext *ctx, Metric *m, enum Source source, Counter now,
                 MonitorStatus status, double scale)
{
    Counter *previous = &ctx->counters[source];
    if (status != MON_OK) {
        previous->valid = false;
        metric(m, status, 0, now.window);
        return;
    }
    uint64_t end = midpoint(now.window), start = midpoint(previous->window);
    if (!previous->valid)
        metric(m, MON_BASELINE, 0, now.window);
    else if (now.value < previous->value || end <= start ||
             now.device != previous->device || now.inode != previous->inode)
        metric(m, MON_RESET, 0, now.window);
    else
        metric(m, MON_OK, (now.value - previous->value) * scale / (end - start),
               (MonitorWindow){previous->window.start_ns, now.window.end_ns});
    now.valid = true;
    *previous = now;
}

static void cpu_aggregates(Metric *m, const CpuCounter *now, const CpuCounter *previous)
{
    MonitorStatus status = MON_UNAVAILABLE;
    double mean = 0, p90 = 0, sd = 0;
    MonitorWindow window = now->window;
    if (!now->valid) {
        status = now->count ? MON_READ_ERROR : MON_UNAVAILABLE;
    } else if (!previous->valid) {
        status = MON_BASELINE;
    } else if (now->count != previous->count) {
        status = MON_RESET;
    } else {
        double *usage = malloc(now->count * sizeof(*usage));
        if (!usage)
            status = MON_READ_ERROR;
        else {
            double io_mean = 0, m2 = 0;
            status = MON_OK;
            for (size_t i = 0; i < now->count; i++) {
                const Cpu *a = &now->cpus[i], *b = &previous->cpus[i];
                uint64_t delta[8], total = 0;
                if (a->id != b->id) {
                    status = MON_RESET;
                    break;
                }
                for (size_t j = 0; j < 8; j++) {
                    if (a->ticks[j] < b->ticks[j] ||
                        UINT64_MAX - total < a->ticks[j] - b->ticks[j]) {
                        status = MON_RESET;
                        break;
                    }
                    delta[j] = a->ticks[j] - b->ticks[j];
                    total += delta[j];
                }
                if (status != MON_OK || !total) {
                    status = MON_RESET;
                    break;
                }
                usage[i] = 100.0 * (total - delta[3] - delta[4]) / total;
                mean += usage[i];
                double io = 100.0 * delta[4] / total;
                double diff = io - io_mean;
                io_mean += diff / (i + 1);
                m2 += diff * (io - io_mean);
            }
            if (status == MON_OK) {
                qsort(usage, now->count, sizeof(*usage), double_compare);
                mean /= now->count;
                p90 = usage[(9 * now->count + 9) / 10 - 1];
                sd = sqrt(m2 / now->count);
                window.start_ns = previous->window.start_ns;
            }
            free(usage);
        }
    }
    metric(&m[IND_CPU_MEAN], status, mean, window);
    metric(&m[IND_CPU_P90], status, p90, window);
    metric(&m[IND_IOWAIT_STDDEV], status, sd, window);
}

static int cpu_compare(const void *key, const void *value)
{
    return int_compare(key, &((const Cpu *)value)->id);
}

static void read_stat(MonitorContext *ctx, Metric *m)
{
    free(ctx->current_cpus.ids);
    ctx->current_cpus = (CpuList){0};
    size_t online = 0;
    MonitorStatus cpu_status = selected_cpus(ctx, &ctx->current_cpus, &online);
    ctx->selection_status = cpu_status;
    CpuCounter cpu = { .count = ctx->current_cpus.count };
    Counter ctxt = {0};
    MonitorStatus ctxt_status = MON_READ_ERROR;
    uint64_t runnable = 0;
    bool have_runnable = false;
    if (cpu.count)
        cpu.cpus = calloc(cpu.count, sizeof(*cpu.cpus));
    if (cpu.cpus)
        for (size_t i = 0; i < cpu.count; i++)
            cpu.cpus[i].id = ctx->current_cpus.ids[i];

    stamp(ctx, &ctxt.window.start_ns);
    FILE *file = system_file(ctx, "/proc/stat");
    bool opened = file != NULL;
    if (opened) {
        char *line = NULL;
        size_t cap = 0;
        while (getline(&line, &cap, file) >= 0) {
            const char *p = line;
            if (!strncmp(line, "ctxt ", 5)) {
                p += 5;
                if (number(&p, &ctxt.value))
                    ctxt_status = MON_OK;
            } else if (!strncmp(line, "procs_running ", 14)) {
                p += 14;
                have_runnable = number(&p, &runnable);
            } else if (cpu.cpus && !strncmp(line, "cpu", 3) &&
                       isdigit((unsigned char)line[3])) {
                p += 3;
                uint64_t id;
                if (!number(&p, &id) || id > INT32_MAX || !isspace((unsigned char)*p))
                    continue;
                int key = (int)id;
                Cpu *entry = bsearch(&key, cpu.cpus, cpu.count, sizeof(*cpu.cpus), cpu_compare);
                if (!entry)
                    continue;
                entry->present = true;
                for (size_t j = 0; j < 8; j++)
                    if (!number(&p, &entry->ticks[j])) {
                        entry->present = false;
                        break;
                    }
            }
        }
        if (ferror(file)) {
            ctxt_status = MON_READ_ERROR;
            cpu_status = MON_READ_ERROR;
            have_runnable = false;
        }
        free(line);
        fclose(file);
    } else {
        ctxt_status = file_error();
        cpu_status = ctxt_status;
    }
    stamp(ctx, &ctxt.window.end_ns);
    cpu.window = ctxt.window;
    cpu.valid = cpu.cpus && cpu_status == MON_OK;
    for (size_t i = 0; cpu.valid && i < cpu.count; i++)
        cpu.valid = cpu.cpus[i].present;
    if (cpu_status == MON_OK)
        cpu_aggregates(m, &cpu, &ctx->cpu);
    else {
        metric(&m[IND_CPU_MEAN], cpu_status, 0, cpu.window);
        metric(&m[IND_CPU_P90], cpu_status, 0, cpu.window);
        metric(&m[IND_IOWAIT_STDDEV], cpu_status, 0, cpu.window);
    }
    free(ctx->cpu.cpus);
    ctx->cpu = cpu;
    MonitorStatus runnable_status = !opened ? ctxt_status
        : !have_runnable ? MON_READ_ERROR : !online ? cpu_status : MON_OK;
    metric(&m[IND_RUNNABLE], runnable_status, online ? (double)runnable / online : 0, ctxt.window);
    rate(ctx, &m[IND_CTXT], CPU_STAT, ctxt, ctxt_status, 1e9);
}

static bool field(FILE *file, const char *prefix, uint64_t *result)
{
    char *line = NULL;
    size_t cap = 0, length = strlen(prefix);
    bool ok = false;
    while (getline(&line, &cap, file) >= 0) {
        if (strncmp(line, prefix, length))
            continue;
        const char *p = line + length;
        if (!strcmp(prefix, "some ")) {
            p = strstr(line, " total=");
            if (!p)
                break;
            p += 7;
        }
        ok = number(&p, result);
        break;
    }
    free(line);
    return ok;
}

static MonitorStatus scalar(MonitorContext *ctx, const char *path, const char *cg,
                            const char *prefix, Counter *out)
{
    if (!path || (cg && !*cg))
        return MON_UNAVAILABLE;
    stamp(ctx, &out->window.start_ns);
    FILE *file = NULL;
    if (cg) {
        char *directory;
        if (asprintf(&directory, "%s%s", ctx->root, cg) < 0)
            return MON_READ_ERROR;
        int fd = open(directory, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        int error = errno;
        free(directory);
        errno = error;
        if (fd < 0)
            return file_error();
        struct stat st;
        struct statfs fs;
        if (fstat(fd, &st) || fstatfs(fd, &fs)) {
            close(fd);
            return MON_READ_ERROR;
        }
        if (!*ctx->root && fs.f_type != CGROUP2_SUPER_MAGIC) {
            close(fd);
            return MON_UNAVAILABLE;
        }
        out->device = st.st_dev;
        out->inode = st.st_ino;
        int child = openat(fd, path, O_RDONLY | O_CLOEXEC);
        error = errno;
        close(fd);
        errno = error;
        if (child < 0)
            return file_error();
        file = fdopen(child, "r");
        if (!file) {
            close(child);
            return MON_READ_ERROR;
        }
    } else {
        file = system_file(ctx, path);
        if (!file)
            return file_error();
    }
    bool ok = field(file, prefix, &out->value);
    fclose(file);
    stamp(ctx, &out->window.end_ns);
    return ok ? MON_OK : MON_READ_ERROR;
}

static void scalar_rate(MonitorContext *ctx, Metric *m, enum Source source, IndicatorId id,
                        const char *path, const char *cg, const char *prefix, double scale)
{
    Counter value = {0};
    MonitorStatus status = scalar(ctx, path, cg, prefix, &value);
    if (!value.window.end_ns)
        value.window = (MonitorWindow){0};
    rate(ctx, &m[id], source, value, status, scale);
}

static void read_cpu_psi(MonitorContext *ctx, Metric *m)
{
    scalar_rate(ctx, m, CPU_PSI, IND_CPU_PSI, "/proc/pressure/cpu", NULL, "some ", 1e5);
}

static void read_io_psi(MonitorContext *ctx, Metric *m)
{
    scalar_rate(ctx, m, IO_PSI, IND_IO_PSI, "/proc/pressure/io", NULL, "some ", 1e5);
}

static void read_fg_usage(MonitorContext *ctx, Metric *m)
{
    scalar_rate(ctx, m, FG_USAGE, IND_FG_USAGE, "cpu.stat", ctx->fg ? ctx->fg : "", "usage_usec ", 1e3);
}

static void read_bg_usage(MonitorContext *ctx, Metric *m)
{
    scalar_rate(ctx, m, BG_USAGE, IND_BG_USAGE, "cpu.stat", ctx->bg ? ctx->bg : "", "usage_usec ", 1e3);
}

static void read_fg_psi(MonitorContext *ctx, Metric *m)
{
    scalar_rate(ctx, m, FG_PSI, IND_FG_PSI, "cpu.pressure", ctx->fg ? ctx->fg : "", "some ", 1e5);
}

static void read_bg_psi(MonitorContext *ctx, Metric *m)
{
    scalar_rate(ctx, m, BG_PSI, IND_BG_PSI, "cpu.pressure", ctx->bg ? ctx->bg : "", "some ", 1e5);
}

static void read_mem(MonitorContext *ctx, Metric *m)
{
    Counter memory = {0};
    MonitorStatus status = scalar(ctx, "/proc/meminfo", NULL, "MemAvailable:", &memory);
    metric(&m[IND_MEM_AVAILABLE], status, memory.value,
           memory.window.end_ns ? memory.window : (MonitorWindow){0});
}

static void read_disk(MonitorContext *ctx, Metric *m)
{
    if (!ctx->device)
        return;
    char *path;
    if (asprintf(&path, "/sys/block/%s/inflight", ctx->device) < 0) {
        source_status(m, DISK, MON_READ_ERROR, (MonitorWindow){0});
        return;
    }
    MonitorWindow window;
    stamp(ctx, &window.start_ns);
    FILE *file = system_file(ctx, path);
    free(path);
    if (!file) {
        source_status(m, DISK, file_error(), (MonitorWindow){0});
        return;
    }
    uint64_t reads = 0, writes = 0;
    MonitorStatus status = fscanf(file, "%" SCNu64 " %" SCNu64, &reads, &writes) == 2
        ? MON_OK : MON_READ_ERROR;
    fclose(file);
    stamp(ctx, &window.end_ns);
    metric(&m[IND_DISK_READ], status, reads, window);
    metric(&m[IND_DISK_WRITE], status, writes, window);
}

static bool subtract(unsigned long long a, unsigned long long b, uint64_t *sum)
{
    if (a < b || UINT64_MAX - *sum < a - b)
        return false;
    *sum += a - b;
    return true;
}

static void sched_deltas(MonitorContext *ctx, Metric *m, const CpuList *selected,
                         MonitorStatus selection, MonitorWindow window, uint64_t dt)
{
    uint64_t wakeups = 0, migrations = 0, histogram[WAKE_BUCKETS] = {0}, drops = 0;
    bool valid = true, have_cpus = selection == MON_OK && selected->count;
    bool same = selection == MON_OK && same_cpus(selected, &ctx->sched_cpus);
    size_t n = 0;
    double mean = 0, m2 = 0;
    for (size_t i = 0; i < ctx->possible.count && valid; i++) {
        const struct sched_counts *a = &ctx->sched_now[i], *b = &ctx->sched_baseline[i];
        valid = subtract(a->wakeups, b->wakeups, &wakeups) &&
                subtract(a->migrations, b->migrations, &migrations) &&
                subtract(a->dropped, b->dropped, &drops);
        for (size_t j = 0; j < WAKE_BUCKETS && valid; j++)
            valid = subtract(a->histogram[j], b->histogram[j], &histogram[j]);
        if (have_cpus && contains(selected, ctx->possible.ids[i])) {
            uint64_t switches = 0;
            valid = valid && subtract(a->switches, b->switches, &switches);
            n++;
            double diff = switches - mean;
            mean += diff / n;
            m2 += diff * (switches - mean);
        }
    }
    if (!valid) {
        source_status(m, SCHED, MON_RESET, window);
        return;
    }
    metric(&m[IND_WAKEUPS], MON_OK, wakeups * 1e9 / dt, window);
    metric(&m[IND_MIGRATIONS], MON_OK, migrations * 1e9 / dt, window);
    MonitorStatus switch_status = selection != MON_OK ? selection : !have_cpus ? MON_UNAVAILABLE :
        !same ? MON_RESET : n != selected->count ? MON_INCOMPLETE : MON_OK;
    metric(&m[IND_SWITCH_VARIANCE], switch_status, n ? m2 / n : 0, window);

    uint64_t count = 0;
    for (size_t i = 0; i < WAKE_BUCKETS; i++)
        count += histogram[i];
    if (drops || !count) {
        metric(&m[IND_WAKE_P95], drops ? MON_INCOMPLETE : MON_NO_EVENTS, 0, window);
        return;
    }
    uint64_t target = count - count / 20;
    size_t bucket = 0;
    for (; histogram[bucket] < target; bucket++)
        target -= histogram[bucket];
    metric(&m[IND_WAKE_P95], MON_OK, ldexp(1.0, (int)bucket + 1) / 1e3, window);
}

static MonitorStatus copy_selection(MonitorContext *ctx, CpuList *selected)
{
    size_t count = ctx->current_cpus.count;
    if (ctx->selection_status != MON_OK || !count)
        return ctx->selection_status;
    selected->ids = malloc(count * sizeof(*selected->ids));
    if (!selected->ids)
        return MON_READ_ERROR;
    memcpy(selected->ids, ctx->current_cpus.ids, count * sizeof(*selected->ids));
    selected->count = count;
    return MON_OK;
}

static void read_sched(MonitorContext *ctx, Metric *m)
{
    if (!ctx->sched)
        return;
    CpuList selected = {0};
    MonitorStatus selection = copy_selection(ctx, &selected);
    MonitorWindow now;
    stamp(ctx, &now.start_ns);
    int error = probe_read(ctx->sched, ctx->sched_now);
    stamp(ctx, &now.end_ns);
    if (error) {
        free(selected.ids);
        ctx->sched_valid = false;
        source_status(m, SCHED, MON_READ_ERROR, now);
        return;
    }
    if (!ctx->sched_valid)
        source_status(m, SCHED, MON_BASELINE, now);
    else if (midpoint(now) <= midpoint(ctx->sched_window))
        source_status(m, SCHED, MON_RESET, now);
    else
        sched_deltas(ctx, m, &selected, selection,
                     (MonitorWindow){ctx->sched_window.start_ns, now.end_ns},
                     midpoint(now) - midpoint(ctx->sched_window));
    struct sched_counts *old = ctx->sched_baseline;
    ctx->sched_baseline = ctx->sched_now;
    ctx->sched_now = old;
    free(ctx->sched_cpus.ids);
    ctx->sched_cpus = selected;
    ctx->sched_window = now;
    ctx->sched_valid = true;
}

static void read_runqueue(MonitorContext *ctx, Metric *m)
{
    if (!ctx->rq) {
        if (!strcmp(ctx->rq_status, "unverified"))
            source_status(m, RUNQUEUE, MON_UNVERIFIED, (MonitorWindow){0});
        return;
    }
    CpuList selected = {0};
    MonitorStatus status = copy_selection(ctx, &selected);
    MonitorWindow now;
    stamp(ctx, &now.start_ns);
    int error = probe_read(ctx->rq, ctx->rq_now);
    stamp(ctx, &now.end_ns);
    MonitorWindow window = now;
    unsigned int maximum = 0;
    if (error || status != MON_OK) {
        ctx->rq_valid = false;
        if (error)
            status = MON_READ_ERROR;
    } else {
        if (!ctx->rq_valid)
            status = MON_BASELINE;
        else if (!selected.count)
            status = MON_UNAVAILABLE;
        else if (!same_cpus(&selected, &ctx->rq_cpus))
            status = MON_RESET;
        else {
            size_t n = 0;
            for (size_t i = 0; i < ctx->possible.count; i++) {
                if (!contains(&selected, ctx->possible.ids[i]))
                    continue;
                const struct rq_observation *obs = &ctx->rq_now[ctx->possible.ids[i]];
                if (obs->timestamp_ns <= ctx->rq_window.end_ns || obs->timestamp_ns > now.end_ns)
                    continue;
                n++;
                if (obs->running > maximum)
                    maximum = obs->running;
            }
            if (n != selected.count)
                status = MON_INCOMPLETE;
            window.start_ns = ctx->rq_window.end_ns;
        }
        ctx->rq_valid = true;
    }
    ctx->rq_window = now;
    free(ctx->rq_cpus.ids);
    ctx->rq_cpus = selected;
    metric(&m[IND_RUNQUEUE_MAX], status, maximum, window);
}

const ReadSource MonitorSources[SOURCE_COUNT] = {
    [CPU_STAT] = read_stat, [CPU_PSI] = read_cpu_psi, [IO_PSI] = read_io_psi,
    [FG_USAGE] = read_fg_usage, [BG_USAGE] = read_bg_usage, [FG_PSI] = read_fg_psi,
    [BG_PSI] = read_bg_psi, [SCHED] = read_sched, [DISK] = read_disk,
    [MEM] = read_mem, [RUNQUEUE] = read_runqueue
};

MonitorContext *MonitorCreate(const MonitorConfig *cfg)
{
    if (!cfg || (cfg->fg_cgroup && *cfg->fg_cgroup != '/') ||
        (cfg->bg_cgroup && *cfg->bg_cgroup != '/') ||
        (cfg->block_device && (!*cfg->block_device || strchr(cfg->block_device, '/') ||
                              !strcmp(cfg->block_device, ".") || !strcmp(cfg->block_device, "..")))) {
        errno = EINVAL;
        return NULL;
    }
    for (size_t i = 0; i < IND_COUNT; i++) {
        if (!MonitorIndicators[i].name || !MonitorSources[bindings[i]]) {
            errno = EINVAL;
            return NULL;
        }
        for (size_t j = 0; j < i; j++)
            if (!strcmp(MonitorIndicators[i].name, MonitorIndicators[j].name)) {
                errno = EINVAL;
                return NULL;
            }
    }
    MonitorContext *ctx = calloc(1, sizeof(*ctx));
    if (!ctx)
        return NULL;
    if (!(ctx->root = strdup(cfg->linux_root ? cfg->linux_root : "")) ||
        (cfg->fg_cgroup && !(ctx->fg = strdup(cfg->fg_cgroup))) ||
        (cfg->bg_cgroup && !(ctx->bg = strdup(cfg->bg_cgroup))) ||
        (cfg->block_device && !(ctx->device = strdup(cfg->block_device))) ||
        (cfg->cpus && parse_cpu_list(cfg->cpus, &ctx->selected))) {
        int error = errno;
        MonitorDestroy(ctx);
        errno = error;
        return NULL;
    }

    ctx->stop_flag = cfg->stop_flag;
    strcpy(ctx->sched_status, "not_configured");
    strcpy(ctx->rq_status, cfg->bpf_object && !cfg->runqueue_verified ? "unverified" : "not_configured");
    if (!cfg->bpf_object)
        return ctx;
    if (access(cfg->bpf_object, R_OK) < 0) {
        snprintf(ctx->sched_status, sizeof(ctx->sched_status), "object_unavailable: %s", strerror(errno));
        if (cfg->runqueue_verified)
            strcpy(ctx->rq_status, "object_unavailable");
        return ctx;
    }
    if (online_cpus(ctx, "/sys/devices/system/cpu/possible", &ctx->possible) != MON_OK) {
        strcpy(ctx->sched_status, "possible_cpus_unavailable");
        if (cfg->runqueue_verified)
            strcpy(ctx->rq_status, "possible_cpus_unavailable");
        return ctx;
    }
    size_t count = ctx->possible.count;
    ctx->sched_baseline = calloc(count, sizeof(*ctx->sched_baseline));
    ctx->sched_now = calloc(count, sizeof(*ctx->sched_now));
    if (ctx->sched_baseline && ctx->sched_now)
        ctx->sched = probe_create(cfg->bpf_object, false, count, ctx->sched_status);
    else
        strcpy(ctx->sched_status, "allocation_failed");
    if (cfg->runqueue_verified) {
        ctx->rq_now = calloc((size_t)ctx->possible.ids[count - 1] + 1, sizeof(*ctx->rq_now));
        if (ctx->rq_now)
            ctx->rq = probe_create(cfg->bpf_object, true,
                                  (size_t)ctx->possible.ids[count - 1] + 1, ctx->rq_status);
        else
            strcpy(ctx->rq_status, "allocation_failed");
    }
    return ctx;
}

const char *MonitorProbeStatus(const MonitorContext *ctx, bool runqueue)
{
    return !ctx ? "no_context" : runqueue ? ctx->rq_status : ctx->sched_status;
}

void MonitorDestroy(MonitorContext *ctx)
{
    if (!ctx)
        return;
    probe_destroy(ctx->rq);
    probe_destroy(ctx->sched);
    free(ctx->root);
    free(ctx->fg);
    free(ctx->bg);
    free(ctx->device);
    free(ctx->selected.ids);
    free(ctx->current_cpus.ids);
    free(ctx->possible.ids);
    free(ctx->sched_cpus.ids);
    free(ctx->rq_cpus.ids);
    free(ctx->cpu.cpus);
    free(ctx->sched_baseline);
    free(ctx->sched_now);
    free(ctx->rq_now);
    free(ctx);
}
