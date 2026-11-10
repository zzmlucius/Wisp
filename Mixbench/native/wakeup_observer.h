/* Diagnostic sidecar only: retain schbench's timestamps, histogram and wake paths. */
#ifndef WISP_WAKEUP_OBSERVER_H
#define WISP_WAKEUP_OBSERVER_H
#include <stdint.h>
#include <inttypes.h>

enum { WISP_POST_UNKNOWN, WISP_POST_REQUEST, WISP_POST_SHUTDOWN };

struct wisp_wakeup_event {
	uint64_t resume_us;
	uint32_t latency_us;
	uint8_t stopping;
	uint8_t last_post;
	uint16_t reserved;
};

struct wisp_trace_buffer {
	struct wisp_wakeup_event *events;
	size_t used;
	uint64_t dropped;
	unsigned int last_post;
} __attribute__((aligned(64)));

static struct wisp_trace_buffer *wisp_buffers;
static struct wisp_wakeup_event *wisp_events;
static const char *wisp_trace_dir;
static size_t wisp_capacity, wisp_worker_count;

static void wisp_trace_init(void)
{
	const char *capacity = getenv("WISP_WAKEUP_CAPACITY");
	char *end = NULL;
	size_t bytes, i;
	wisp_trace_dir = getenv("WISP_WAKEUP_TRACE_DIR");
	if (!wisp_trace_dir || !*wisp_trace_dir)
		return;
	wisp_capacity = capacity ? strtoul(capacity, &end, 10) : 4096;
	if ((capacity && (!*capacity || *end)) || !wisp_capacity || wisp_capacity > 65536 ||
	    message_threads <= 0 || worker_threads <= 0)
		goto unavailable;
	wisp_worker_count = (size_t)message_threads * worker_threads;
	if (wisp_worker_count > (64 * 1024 * 1024) / sizeof(*wisp_events) / wisp_capacity)
		goto unavailable;
	bytes = wisp_worker_count * wisp_capacity * sizeof(*wisp_events);
	if (posix_memalign((void **)&wisp_buffers, 64, wisp_worker_count * sizeof(*wisp_buffers)))
		goto unavailable;
	memset(wisp_buffers, 0, wisp_worker_count * sizeof(*wisp_buffers));
	wisp_events = malloc(bytes);
	if (!wisp_events)
		goto unavailable;
	/* Touch each page before workers start; no reallocations or page faults on first record. */
	for (i = 0; i < bytes; i += 4096)
		((volatile unsigned char *)wisp_events)[i] = 0;
	((volatile unsigned char *)wisp_events)[bytes - 1] = 0;
	for (i = 0; i < wisp_worker_count; i++)
		wisp_buffers[i].events = wisp_events + i * wisp_capacity;
	return;
unavailable:
	fprintf(stderr, "wakeup observation unavailable: invalid capacity or allocation failure\n");
	free(wisp_buffers);
	wisp_buffers = NULL;
	free(wisp_events);
	wisp_events = NULL;
}

static inline void wisp_note_post(struct thread_data *worker, unsigned int kind)
{
	if (worker->wisp_trace)
		__atomic_store_n(&worker->wisp_trace->last_post, kind, __ATOMIC_RELAXED);
}

static inline void wisp_record_wakeup(struct thread_data *worker,
		unsigned int latency_us, const struct timeval *resume, unsigned long stopping_flag)
{
	struct wisp_trace_buffer *buffer = worker->wisp_trace;
	struct wisp_wakeup_event *event;
	if (!buffer)
		return;
	if (buffer->used == wisp_capacity) {
		buffer->dropped++;
		return;
	}
	event = &buffer->events[buffer->used++];
	event->resume_us = (uint64_t)resume->tv_sec * 1000000 + resume->tv_usec;
	event->latency_us = latency_us;
	event->stopping = !!stopping_flag;
	event->last_post = __atomic_load_n(&buffer->last_post, __ATOMIC_RELAXED);
}

static void wisp_trace_dump(unsigned long native_samples)
{
	char *csv_path = NULL, *meta_path = NULL;
	FILE *csv = NULL, *meta = NULL;
	size_t i, j;
	uint64_t recorded = 0, dropped = 0;
	int write_ok = 1;
	struct timespec dump_begin, dump_end;
	double dump_elapsed_s = 0;
	if (!wisp_trace_dir || !*wisp_trace_dir)
		return;
	if (asprintf(&csv_path, "%s/front-wakeup.csv", wisp_trace_dir) < 0 ||
	    asprintf(&meta_path, "%s/front-wakeup.meta.json", wisp_trace_dir) < 0)
		goto done;
	if (!wisp_buffers)
		goto metadata;
	clock_gettime(CLOCK_MONOTONIC, &dump_begin);
	csv = fopen(csv_path, "w");
	if (!csv) {
		write_ok = 0;
		goto metadata;
	}
	setvbuf(csv, NULL, _IOFBF, 65536);
	fputs("worker_id,sequence,resume_realtime_us,origin_realtime_us,latency_us,stopping,last_post_kind\n", csv);
	for (i = 0; i < wisp_worker_count; i++) {
		struct wisp_trace_buffer *buffer = &wisp_buffers[i];
		recorded += buffer->used;
		dropped += buffer->dropped;
		for (j = 0; j < buffer->used; j++) {
			struct wisp_wakeup_event *event = &buffer->events[j];
			const char *post = event->last_post == WISP_POST_SHUTDOWN ? "shutdown" :
				event->last_post == WISP_POST_REQUEST ? "request" : "unknown";
			fprintf(csv, "%zu,%zu,%" PRIu64 ",%" PRIu64 ",%u,%u,%s\n",
				i, j + 1, event->resume_us, event->resume_us - event->latency_us,
				event->latency_us, event->stopping, post);
		}
	}
	if (ferror(csv))
		write_ok = 0;
	if (fclose(csv))
		write_ok = 0;
	csv = NULL;
	clock_gettime(CLOCK_MONOTONIC, &dump_end);
	dump_elapsed_s = dump_end.tv_sec - dump_begin.tv_sec +
		(dump_end.tv_nsec - dump_begin.tv_nsec) / 1000000000.0;
metadata:
	meta = fopen(meta_path, "w");
	if (!meta)
		goto done;
	fprintf(meta, "{\"schema_version\":1,\"capacity_per_worker\":%zu,\"worker_count\":%zu,"
		"\"record_bytes\":%zu,\"allocated_event_bytes\":%zu,\"recorded_samples\":%" PRIu64 ","
		"\"dropped_samples\":%" PRIu64 ",\"native_wakeup_samples\":%lu,"
		"\"capture_complete\":%s,\"csv_dump_elapsed_s\":%.9f,\"workers\":[",
		wisp_capacity, wisp_worker_count, sizeof(*wisp_events),
		wisp_buffers ? wisp_capacity * wisp_worker_count * sizeof(*wisp_events) : 0,
		recorded, dropped, native_samples,
		wisp_buffers && write_ok && !dropped && recorded == native_samples ? "true" : "false", dump_elapsed_s);
	if (wisp_buffers)
		for (i = 0; i < wisp_worker_count; i++)
			fprintf(meta, "%s{\"worker_id\":%zu,\"recorded\":%zu,\"dropped\":%" PRIu64 "}",
				i ? "," : "", i, wisp_buffers[i].used, wisp_buffers[i].dropped);
	fputs("]}\n", meta);
done:
	if (csv)
		fclose(csv);
	if (meta)
		fclose(meta);
	free(csv_path);
	free(meta_path);
	free(wisp_events);
	free(wisp_buffers);
}
#endif
