#include "internal.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef WITH_BPF
#include <bpf/bpf.h>
#include <bpf/libbpf.h>

struct Probe {
    struct bpf_object *object;
    struct bpf_link *links[8];
    size_t links_count, count;
    int fd;
    bool rq;
};

void probe_destroy(struct Probe *p)
{
    if (!p)
        return;
    for (size_t i = p->links_count; i; i--)
        bpf_link__destroy(p->links[i - 1]);
    bpf_object__close(p->object);
    free(p);
}

struct Probe *probe_create(const char *path, bool rq, size_t count, char status[160])
{
    struct Probe *p = calloc(1, sizeof(*p));
    int error = ENOMEM;
    const char *phase = "allocate";
    if (!p)
        goto fail;
    p->rq = rq;
    p->count = count;
    phase = "open";
    p->object = bpf_object__open_file(path, NULL);
    error = -libbpf_get_error(p->object);
    if (error) {
        p->object = NULL;
        goto fail;
    }

    struct bpf_program *program;
    struct bpf_map *map;
    phase = "configure";
    bpf_object__for_each_program(program, p->object) {
        bool for_rq = !strcmp(bpf_program__name(program), "record_rq");
        if ((error = -bpf_program__set_autoload(program, for_rq == rq)))
            goto fail;
    }
    bpf_object__for_each_map(map, p->object) {
        bool for_rq = !strcmp(bpf_map__name(map), "rq_latest");
        if ((error = -bpf_map__set_autocreate(map, for_rq == rq)))
            goto fail;
        if (for_rq && rq && (error = -bpf_map__set_max_entries(map, count)))
            goto fail;
    }
    if (!rq && libbpf_num_possible_cpus() != (int)count) {
        error = EINVAL;
        goto fail;
    }
    phase = "load";
    if ((error = -bpf_object__load(p->object)))
        goto fail;
    phase = "map_fd";
    p->fd = bpf_object__find_map_fd_by_name(p->object, rq ? "rq_latest" : "counts");
    if (p->fd < 0) {
        error = -p->fd;
        goto fail;
    }
    bpf_object__for_each_program(program, p->object) {
        if (!bpf_program__autoload(program))
            continue;
        phase = bpf_program__name(program);
        if (p->links_count == sizeof(p->links) / sizeof(*p->links)) {
            error = E2BIG;
            goto fail;
        }
        struct bpf_link *link = bpf_program__attach(program);
        error = -libbpf_get_error(link);
        if (error)
            goto fail;
        p->links[p->links_count++] = link;
        if (!rq && geteuid() == 0) {
            struct bpf_prog_info info = {0};
            unsigned int size = sizeof(info);
            if (!bpf_obj_get_info_by_fd(bpf_program__fd(program), &info, &size))
                fprintf(stderr, "scheduler probe program id: %u\n", info.id);
        }
    }
    if (!p->links_count) {
        phase = "attach";
        error = ENOENT;
        goto fail;
    }
    strcpy(status, "attached");
    return p;
fail:
    snprintf(status, 160, "%s_failed: %s", phase, strerror(error));
    probe_destroy(p);
    return NULL;
}

int probe_read(struct Probe *p, void *values)
{
    unsigned int key = 0;
    if (!p->rq)
        return bpf_map_lookup_elem(p->fd, &key, values);
    struct rq_observation *observations = values;
    for (; key < p->count; key++) {
        struct rq_observation second;
        if (bpf_map_lookup_elem(p->fd, &key, &observations[key]) ||
            bpf_map_lookup_elem(p->fd, &key, &second))
            return -1;
        if (observations[key].timestamp_ns != second.timestamp_ns ||
            observations[key].running != second.running)
            observations[key].timestamp_ns = 0;
    }
    return 0;
}
#else
struct Probe *probe_create(const char *path, bool rq, size_t count, char status[160])
{
    (void)path;
    (void)rq;
    (void)count;
    strcpy(status, "collector_built_without_libbpf");
    return NULL;
}

int probe_read(struct Probe *p, void *values)
{
    (void)p;
    (void)values;
    errno = ENOSYS;
    return -1;
}

void probe_destroy(struct Probe *p)
{
    (void)p;
}
#endif
