package ca.voiceclaw.canvaswidget;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.widget.EditText;
import android.widget.TextView;

/** The app's only screen: where the boards page lives and its widget token (both shown under Settings > Phone widget),
 *  with Test, Save and a button that asks the launcher to add the bar. Extras with the same names prefill it. */
public class SetupActivity extends Activity {
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.setup);
        SharedPreferences p = Config.prefs(this);
        Intent in = getIntent();
        for (String k : new String[]{"base", "token", "title", "cf_id", "cf_secret"}) {
            String v = in.getStringExtra(k);
            field(k).setText(v != null ? v : p.getString(k, ""));
        }
        TextView out = findViewById(R.id.result);
        findViewById(R.id.test).setOnClickListener(v -> {
            save(p); out.setText("Checking…");
            new Thread(() -> { String line = CanvasWidget.fetchLine(this, Config.load(this)); runOnUiThread(() -> out.setText(line)); }).start();
        });
        findViewById(R.id.save).setOnClickListener(v -> { save(p); CanvasWidget.refresh(this); out.setText("Saved."); });
        findViewById(R.id.pin).setOnClickListener(v -> { save(p); CanvasWidget.refresh(this); CanvasWidget.requestPin(this); });
        if (in.getBooleanExtra("save", false)) { save(p); CanvasWidget.refresh(this); finish(); }
    }

    private EditText field(String k) {
        switch (k) {
            case "base": return findViewById(R.id.base);
            case "token": return findViewById(R.id.token);
            case "title": return findViewById(R.id.title);
            case "cf_id": return findViewById(R.id.cfid);
            default: return findViewById(R.id.cfsecret);
        }
    }

    private void save(SharedPreferences p) {
        SharedPreferences.Editor e = p.edit();
        for (String k : new String[]{"base", "token", "title", "cf_id", "cf_secret"}) e.putString(k, field(k).getText().toString().trim());
        e.apply();
    }
}
