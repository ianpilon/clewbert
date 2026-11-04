package ca.voiceclaw.canvaswidget;

import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.widget.RemoteViews;
import org.json.JSONObject;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/** The "Ask" bar: tap the words to search your boards, the mic to talk to them, the bulb for the task list.
 *  The small line underneath is live: questions waiting, tasks running, tasks finished since you last heard. */
public class CanvasWidget extends AppWidgetProvider {
    public static final String REFRESH = "ca.voiceclaw.canvaswidget.REFRESH";
    public static final String PIN = "ca.voiceclaw.canvaswidget.PIN";

    @Override public void onUpdate(Context c, AppWidgetManager m, int[] ids) { paint(c, null); refresh(c); }

    @Override public void onEnabled(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        js.schedule(new JobInfo.Builder(1, new ComponentName(c, RefreshJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPeriodic(15 * 60 * 1000L).setPersisted(true).build());
    }

    @Override public void onDisabled(Context c) { c.getSystemService(JobScheduler.class).cancel(1); }

    @Override public void onReceive(Context c, Intent i) {
        super.onReceive(c, i);
        if (REFRESH.equals(i.getAction())) { paint(c, c.getString(R.string.loading)); refresh(c); }
        if (PIN.equals(i.getAction())) requestPin(c);
    }

    static void requestPin(Context c) {
        AppWidgetManager.getInstance(c).requestPinAppWidget(new ComponentName(c, CanvasWidget.class), null, null);
    }

    /** Draws the bar, with the status line given (null keeps the last one the system remembers). */
    static void paint(Context c, String status) {
        Config cfg = Config.load(c);
        RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget);
        v.setTextViewText(R.id.title, cfg.title);
        if (!cfg.ready()) {
            v.setTextViewText(R.id.status, c.getString(R.string.setup_needed));
            PendingIntent setup = PendingIntent.getActivity(c, 9, new Intent(c, SetupActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            v.setOnClickPendingIntent(R.id.ask, setup); v.setOnClickPendingIntent(R.id.mic, setup); v.setOnClickPendingIntent(R.id.bulb, setup);
        } else {
            if (status != null) v.setTextViewText(R.id.status, status);
            v.setOnClickPendingIntent(R.id.ask, open(c, cfg.base + "/?q=", 1));
            v.setOnClickPendingIntent(R.id.mic, open(c, cfg.base + "/?voice=1", 2));
            v.setOnClickPendingIntent(R.id.bulb, open(c, cfg.base + "/#agents", 3));
        }
        AppWidgetManager.getInstance(c).updateAppWidget(new ComponentName(c, CanvasWidget.class), v);
    }

    static PendingIntent open(Context c, String url, int code) {
        Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(c, code, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /** Asks the server for the counts, off the main thread. */
    static void refresh(Context c) {
        Config cfg = Config.load(c);
        if (!cfg.ready()) { paint(c, null); return; }
        new Thread(() -> paint(c, fetchLine(c, cfg))).start();
    }

    /** One line for the bar, or a plain reason it could not be read. */
    static String fetchLine(Context c, Config cfg) {
        try {
            HttpURLConnection h = (HttpURLConnection) new URL(cfg.base + "/api/widget").openConnection();
            h.setConnectTimeout(8000); h.setReadTimeout(8000);
            h.setRequestProperty("X-Widget-Token", cfg.token);
            if (!cfg.cfId.isEmpty()) { h.setRequestProperty("CF-Access-Client-Id", cfg.cfId); h.setRequestProperty("CF-Access-Client-Secret", cfg.cfSecret); }
            if (h.getResponseCode() == 401) return c.getString(R.string.bad_token);
            try (InputStream in = h.getInputStream()) {
                JSONObject j = new JSONObject(new String(in.readAllBytes(), "UTF-8"));
                int w = j.optInt("waiting"), r = j.optInt("running"), d = j.optInt("done");
                StringBuilder s = new StringBuilder();
                if (w > 0) s.append(w).append(w == 1 ? " question waiting" : " questions waiting");
                if (r > 0) s.append(s.length() > 0 ? " · " : "").append(r).append(r == 1 ? " task running" : " tasks running");
                if (d > 0) s.append(s.length() > 0 ? " · " : "").append(d).append(" finished");
                return s.length() > 0 ? s + "  —  " + j.optString("line") : j.optString("line", "All quiet.");
            }
        } catch (Exception e) {
            return c.getString(R.string.unreachable);
        }
    }
}
