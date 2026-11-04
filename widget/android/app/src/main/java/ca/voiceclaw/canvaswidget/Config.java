package ca.voiceclaw.canvaswidget;

import android.content.Context;
import android.content.SharedPreferences;

/** What the widget needs to know, kept in the app's own private preferences: the page's address, the widget token
 *  from Settings, the words on the bar, and (only behind Cloudflare Access) a service token. */
public final class Config {
    public final String base, token, title, cfId, cfSecret;

    private Config(SharedPreferences p) {
        base = p.getString("base", "").trim().replaceAll("/+$", "");
        token = p.getString("token", "").trim();
        String t = p.getString("title", "").trim();
        title = t.isEmpty() ? "Ask Clewbert" : t;
        cfId = p.getString("cf_id", "").trim();
        cfSecret = p.getString("cf_secret", "").trim();
    }

    public static Config load(Context c) { return new Config(prefs(c)); }

    public static SharedPreferences prefs(Context c) { return c.getSharedPreferences("widget", Context.MODE_PRIVATE); }

    public boolean ready() { return !base.isEmpty() && !token.isEmpty(); }
}
