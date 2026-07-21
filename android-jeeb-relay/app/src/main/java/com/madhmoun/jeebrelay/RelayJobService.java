package com.madhmoun.jeebrelay;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class RelayJobService extends JobService {
    private static final int JOB_ID = 731204;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    static void schedule(Context context) {
        JobScheduler scheduler = context.getSystemService(JobScheduler.class);
        if (scheduler == null) {
            return;
        }
        JobInfo job = new JobInfo.Builder(
                JOB_ID,
                new ComponentName(context, RelayJobService.class)
        )
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setMinimumLatency(15000)
                .setOverrideDeadline(120000)
                .setPersisted(true)
                .setBackoffCriteria(30000, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .build();
        scheduler.schedule(job);
    }

    static void flushNow(Context context) {
        Executors.newSingleThreadExecutor().execute(() -> {
            boolean retry = RelayHttpClient.flush(context.getApplicationContext());
            if (retry || PendingQueue.size(context) > 0) {
                schedule(context);
            }
        });
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        executor.execute(() -> {
            boolean retry = RelayHttpClient.flush(getApplicationContext());
            jobFinished(params, retry || PendingQueue.size(this) > 0);
        });
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return PendingQueue.size(this) > 0;
    }
}
