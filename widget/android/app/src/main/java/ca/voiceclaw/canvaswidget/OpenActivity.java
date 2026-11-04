package ca.voiceclaw.canvaswidget;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.net.Uri;
import android.os.Bundle;
import java.net.HttpURLConnection;
import java.net.URL;

/** A tap on the bar lands here for an instant (no window of its own). It leaves the tap's wish on the server, then brings the
 *  installed boards app to the front the way its icon does, so a warm app resumes without the splash screen that opening an
 *  address causes. Without an installed app, the address opens as before. */
public class OpenActivity extends Activity {
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        Config cfg = Config.load(this);
        String hint = getIntent().getStringExtra("hint"), url = getIntent().getStringExtra("url");
        if (cfg.ready() && hint != null) new Thread(() -> leaveHint(cfg, hint)).start();
        Intent launch = cfg.ready() ? appLaunch(cfg.base) : null;
        if (launch == null) launch = new Intent(Intent.ACTION_VIEW, Uri.parse(url != null ? url : cfg.base));
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try { startActivity(launch); }
        catch (Exception e) { try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(cfg.base)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); } catch (Exception ignored) {} }
        finish();
        overridePendingTransition(0, 0);
    }

    /** The launcher intent of the web app Chrome installed for the boards address (its package starts with org.chromium.webapk),
     *  or null when none is installed. */
    Intent appLaunch(String base) {
        try {
            PackageManager pm = getPackageManager();
            Intent view = new Intent(Intent.ACTION_VIEW, Uri.parse(base + "/"));
            for (ResolveInfo r : pm.queryIntentActivities(view, PackageManager.MATCH_DEFAULT_ONLY | PackageManager.MATCH_ALL)) {
                String pkg = r.activityInfo == null ? null : r.activityInfo.packageName;
                if (pkg != null && pkg.startsWith("org.chromium.webapk.")) {
                    Intent launch = pm.getLaunchIntentForPackage(pkg);
                    if (launch != null) return launch;
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    static void leaveHint(Config cfg, String hint) {
        try {
            HttpURLConnection h = (HttpURLConnection) new URL(cfg.base + "/api/widget/hint").openConnection();
            h.setConnectTimeout(4000); h.setReadTimeout(4000); h.setRequestMethod("POST"); h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json"); h.setRequestProperty("X-Widget-Token", cfg.token);
            if (!cfg.cfId.isEmpty()) { h.setRequestProperty("CF-Access-Client-Id", cfg.cfId); h.setRequestProperty("CF-Access-Client-Secret", cfg.cfSecret); }
            h.getOutputStream().write(("{\"hint\":\"" + hint + "\"}").getBytes("UTF-8"));
            h.getResponseCode();
        } catch (Exception ignored) {}
    }
}
