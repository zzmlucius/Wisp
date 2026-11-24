/* One pending sample per worker: no event array, posting markers or trace CSV. */
#ifndef WISP_WAKEUP_OBSERVER_H
#define WISP_WAKEUP_OBSERVER_H
static inline void wisp_record_last_wakeup(struct thread_data *td,
		unsigned int latency_us, unsigned long stopping_flag)
{
	if (td->wisp_last_valid)
		add_lat(&td->wakeup_stats, td->wisp_last_us);
	td->wisp_last_us = latency_us;
	td->wisp_last_stopping = !!stopping_flag;
	td->wisp_last_valid = 1;
}

/* Called once, after joining this worker. */
static void wisp_finish_last_wakeup(struct thread_data *td)
{
	if (td->wisp_last_valid && !td->wisp_last_stopping)
		add_lat(&td->wakeup_stats, td->wisp_last_us);
}

static void wisp_dump_last_wakeups(struct thread_data *threads)
{
	const char *directory = getenv("WISP_WAKEUP_TAIL_DIR");
	char *path = NULL;
	FILE *file;
	unsigned long retained = 0, excluded = 0;
	int msg, i, index = 0, id = 0;
	if (!directory || !*directory)
		return;
	if (asprintf(&path, "%s/front-wakeup.meta.json", directory) < 0)
		return;
	file = fopen(path, "w");
	free(path);
	if (!file)
		return;
	for (msg = 0; msg < message_threads; msg++) {
		index++;
		for (i = 0; i < worker_threads; i++, index++) {
			struct thread_data *td = &threads[index];
			retained += td->wakeup_stats.nr_samples;
			excluded += td->wisp_last_valid && td->wisp_last_stopping;
		}
	}
	fprintf(file, "{\"schema_version\":2,\"revision\":\"last-worker-stopping-v1\","
		"\"worker_count\":%d,\"slot_bytes_per_worker\":8,"
		"\"native_wakeup_samples\":%lu,\"retained_samples\":%lu,"
		"\"excluded_samples\":%lu,\"workers\":[",
		message_threads * worker_threads, retained + excluded, retained, excluded);
	index = 0;
	for (msg = 0; msg < message_threads; msg++) {
		index++;
		for (i = 0; i < worker_threads; i++, index++, id++) {
			struct thread_data *td = &threads[index];
			fprintf(file, "%s{\"worker_id\":%d,\"has_sample\":%s,"
				"\"last_latency_us\":%u,\"stopping\":%u,"
				"\"retained_samples\":%lu,\"excluded_samples\":%u}",
				id ? "," : "", id, td->wisp_last_valid ? "true" : "false",
				td->wisp_last_us, td->wisp_last_stopping,
				td->wakeup_stats.nr_samples,
				td->wisp_last_valid && td->wisp_last_stopping);
		}
	}
	fputs("]}\n", file);
	fclose(file);
}
#endif
