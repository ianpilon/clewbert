package ca.voiceclaw.canvaswidget;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Every 15 minutes (the system's floor) the bar asks the server again. */
public class RefreshJob extends JobService {
    @Override public boolean onStartJob(JobParameters p) { CanvasWidget.refresh(this); return false; }
    @Override public boolean onStopJob(JobParameters p) { return false; }
}
