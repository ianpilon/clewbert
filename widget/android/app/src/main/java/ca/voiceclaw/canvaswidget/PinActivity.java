package ca.voiceclaw.canvaswidget;

import android.app.Activity;
import android.os.Bundle;

/** An invisible screen: launchers only honour "add this widget" from an app in the foreground. It asks, then closes. */
public class PinActivity extends Activity {
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        CanvasWidget.requestPin(this);
        finish();
    }
}
