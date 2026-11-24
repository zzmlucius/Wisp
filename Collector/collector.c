#define _GNU_SOURCE
#include "monitor.h"
#include "probe_data.h"
#include <ctype.h>
#include <errno.h>
#include <getopt.h>
#include <grp.h>
#include <poll.h>
#include <sys/prctl.h>
#include <unistd.h>
#include <inttypes.h>
#include <locale.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static volatile sig_atomic_t stop;

typedef struct {
    FILE *csv, *status;
    size_t rows, limit;
    bool parent_pipe;
} Output;

static void handle_signal(int signum)
{
    (void)signum;
    stop = 1;
}

static MonitorDecision write_sample(const double vector[IND_COUNT], const SampleMeta *meta,
                                    void *userdata)
{
    Output *out = userdata;
    struct pollfd parent = {.fd = STDIN_FILENO, .events = POLLIN};
    char input;
    if (stop || (out->parent_pipe && poll(&parent, 1, 0) > 0 &&
                 ((parent.revents & (POLLHUP | POLLERR | POLLNVAL)) ||
                  ((parent.revents & POLLIN) && read(STDIN_FILENO, &input, 1) == 0))))
        return MON_STOP;
    fprintf(out->csv, "%" PRIu64 ",%.9f", meta->timestamp_monotonic_ns, meta->dt_s);
    fprintf(out->status, "{\"timestamp_monotonic_ns\":%" PRIu64 ",\"indicators\":[",
            meta->timestamp_monotonic_ns);
    for (size_t i = 0; i < IND_COUNT; i++) {
        fputc(',', out->csv);
        if (meta->status[i] == MON_OK)
            fprintf(out->csv, "%.17g", vector[i]);
        fprintf(out->status, "%s[\"%s\",%" PRIu64 ",%" PRIu64 "]", i ? "," : "",
                MonitorStatusName(meta->status[i]), meta->window[i].start_ns, meta->window[i].end_ns);
    }
    fputc('\n', out->csv);
    fputs("]}\n", out->status);
    if (fflush(out->csv) || fflush(out->status))
        return MON_SINK_ERROR;
    if (++out->rows == 1) {
        int written = out->parent_pipe ? printf("ready %ld\n", (long)getpid()) : puts("ready");
        if (written < 0 || fflush(stdout))
            return MON_SINK_ERROR;
    }
    return out->rows == out->limit ? MON_STOP : MON_CONTINUE;
}

static FILE *create_output(const char *dir, const char *prefix, const char *suffix)
{
    char *path;
    if (asprintf(&path, "%s/%s%s", dir, prefix, suffix) < 0)
        return NULL;
    FILE *file = fopen(path, "wxe");
    int error = errno;
    free(path);
    errno = error;
    return file;
}

static void json_string(FILE *file, const char *s)
{
    if (!s) {
        fputs("null", file);
        return;
    }
    fputc('"', file);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (*p < 32)
            fprintf(file, "\\u%04x", *p);
        else {
            if (*p == '"' || *p == '\\')
                fputc('\\', file);
            fputc(*p, file);
        }
    }
    fputc('"', file);
}

static int write_schema(const char *dir, const MonitorConfig *cfg, MonitorContext *ctx,
                        uint64_t interval_ns, const char *prefix)
{
    FILE *file = create_output(dir, prefix, "-meta.json");
    if (!file)
        return -1;
    fprintf(file, "{\"schema_version\":%d,\"clock\":\"CLOCK_MONOTONIC\","
                  "\"interval_ns\":%" PRIu64 ",\"cpus\":", MONITOR_SCHEMA_VERSION, interval_ns);
    json_string(file, cfg->cpus);
    fputs(",\"block_device\":", file);
    json_string(file, cfg->block_device);
    fputs(",\"cgroups\":{\"front\":", file);
    json_string(file, cfg->fg_cgroup);
    fputs(",\"back\":", file);
    json_string(file, cfg->bg_cgroup);
    fputs("},\"probes\":{\"scheduler\":", file);
    json_string(file, MonitorProbeStatus(ctx, false));
    fputs(",\"runqueue\":", file);
    json_string(file, MonitorProbeStatus(ctx, true));
    fprintf(file, "},\"wakeup_histogram\":{\"buckets\":%d,"
                  "\"bucket_upper_us\":\"2^(index+1)/1000\"},"
                  "\"sample_file\":\"%s.csv\",\"sample_status_file\":\"%s-status.jsonl\","
                  "\"sample_status_order\":[\"status\",\"start_ns\",\"end_ns\"],"
                  "\"columns\":[", WAKE_BUCKETS, prefix, prefix);
    for (size_t i = 0; i < IND_COUNT; i++) {
        const IndicatorInfo *info = &MonitorIndicators[i];
        fprintf(file, "%s{\"id\":%zu,\"name\":\"%s\",\"unit\":\"%s\","
                      "\"scope\":\"%s\",\"source\":\"%s\"}",
                i ? "," : "", i, info->name, info->unit, info->scope, info->source);
    }
    fputs("]}\n", file);
    int error = ferror(file);
    return fclose(file) || error ? -1 : 0;
}

static void usage(FILE *out)
{
    fputs("wisp-collector --output DIR [--cpus LIST] [--interval SECONDS] [--samples N]\n"
          "    [--fg-cgroup PATH] [--bg-cgroup PATH] [--device NAME]\n"
          "    [--bpf-object PATH] [--runqueue-verified] [--prefix NAME]\n", out);
}

static bool sudo_id(const char *key, unsigned long *result)
{
    const char *value = getenv(key);
    if (!value || !isdigit((unsigned char)*value))
        return false;
    char *end;
    errno = 0;
    *result = strtoul(value, &end, 10);
    return !errno && !*end && *result > 0 && *result < UINT32_MAX;
}

static int drop_attachment_privileges(void)
{
    unsigned long uid, gid;
    if (!sudo_id("SUDO_UID", &uid) || !sudo_id("SUDO_GID", &gid)) {
        errno = EINVAL;
        return -1;
    }
    if (prctl(PR_SET_KEEPCAPS, 0L, 0L, 0L, 0L) < 0 || setgroups(0, NULL) ||
        setresgid(gid, gid, gid) || setresuid(uid, uid, uid) ||
        prctl(PR_SET_NO_NEW_PRIVS, 1L, 0L, 0L, 0L) < 0)
        return -1;
    return 0;
}

int main(int argc, char **argv)
{
    static const struct option options[] = {
        {"output", required_argument, NULL, 'o'}, {"cpus", required_argument, NULL, 'c'},
        {"interval", required_argument, NULL, 'i'}, {"samples", required_argument, NULL, 'n'},
        {"fg-cgroup", required_argument, NULL, 'f'}, {"bg-cgroup", required_argument, NULL, 'b'},
        {"device", required_argument, NULL, 'd'}, {"bpf-object", required_argument, NULL, 'p'},
        {"runqueue-verified", no_argument, NULL, 'r'}, {"prefix", required_argument, NULL, 'x'},
        {"sudo-attach", no_argument, NULL, 'a'}, {"help", no_argument, NULL, 'h'}, {0}
    };
    setlocale(LC_NUMERIC, "C");
    MonitorConfig cfg = {.stop_flag = &stop};
    const char *dir = NULL, *prefix = "workload";
    uint64_t period = 1000000000;
    Output output = {0};
    int option;
    while ((option = getopt_long(argc, argv, "", options, NULL)) != -1) {
        switch (option) {
        case 'o': dir = optarg; break;
        case 'c': cfg.cpus = optarg; break;
        case 'f': cfg.fg_cgroup = optarg; break;
        case 'b': cfg.bg_cgroup = optarg; break;
        case 'd': cfg.block_device = optarg; break;
        case 'p': cfg.bpf_object = optarg; break;
        case 'r': cfg.runqueue_verified = true; break;
        case 'x': prefix = optarg; break;
        case 'a': output.parent_pipe = true; break;
        case 'h': usage(stdout); return 0;
        case 'i': {
            char *end;
            double seconds = strtod(optarg, &end);
            if (end == optarg || *end || !isfinite(seconds) || seconds < 1e-9 || seconds > 2147483647)
                goto invalid;
            period = (uint64_t)llround(seconds * 1e9);
            break;
        }
        case 'n': {
            char *end;
            errno = 0;
            unsigned long long count = strtoull(optarg, &end, 10);
            if (optarg[0] < '0' || optarg[0] > '9' || *end || errno || !count || count > SIZE_MAX)
                goto invalid;
            output.limit = count;
            break;
        }
        default: goto invalid;
        }
    }
    if (!*prefix)
        goto invalid;
    for (const unsigned char *c = (const unsigned char *)prefix; *c; c++)
        if (!isalnum(*c) && *c != '-' && *c != '_')
            goto invalid;
    if (!dir || optind != argc || (cfg.runqueue_verified && !cfg.bpf_object) ||
        (output.parent_pipe && (geteuid() != 0 || !cfg.bpf_object || cfg.runqueue_verified)))
        goto invalid;

    struct sigaction action = {.sa_handler = handle_signal};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL)) {
        perror("collector signal");
        return 1;
    }
    MonitorContext *ctx = MonitorCreate(&cfg);
    if (!ctx) {
        perror("MonitorCreate");
        return 1;
    }
    int result = 1;
    if (output.parent_pipe) {
        if (strcmp(MonitorProbeStatus(ctx, false), "attached")) {
            fprintf(stderr, "eBPF attachment failed: %s\n", MonitorProbeStatus(ctx, false));
            goto done;
        }
        if (drop_attachment_privileges()) {
            perror("drop collector privileges");
            goto done;
        }
    }
    output.csv = create_output(dir, prefix, ".csv");
    output.status = output.csv ? create_output(dir, prefix, "-status.jsonl") : NULL;
    if (!output.status || write_schema(dir, &cfg, ctx, period, prefix)) {
        perror("collector output");
        goto done;
    }
    fputs("timestamp_monotonic_ns,dt_s", output.csv);
    for (size_t i = 0; i < IND_COUNT; i++)
        fprintf(output.csv, ",%s", MonitorIndicators[i].name);
    fputc('\n', output.csv);
    if (MonitorLoopNs(period, ctx, write_sample, &output) < 0) {
        perror("MonitorLoop");
        goto done;
    }
    result = 0;
done:
    if (output.csv && fclose(output.csv))
        result = 1;
    if (output.status && fclose(output.status))
        result = 1;
    MonitorDestroy(ctx);
    if (output.parent_pipe && !result)
        fputs("scheduler probe descriptors closed\n", stderr);
    return result;
invalid:
    usage(stderr);
    return 2;
}
