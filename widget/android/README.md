# Clewbert widget (Android)

An "Ask" bar for your phone's home screen, the way the Google bar works, but for your boards:

- tap the words: your boards page opens with the search box ready (type a question, or find a board, note or picture);
- tap the mic: the page opens with the voice already listening;
- tap the bulb: the list of agent tasks;
- the small line underneath is live: questions waiting for you, tasks running, tasks finished since you last heard. It refreshes every 15 minutes.

Prefer a plain icon? You do not need this app: open your boards page in Chrome and use the browser menu, **Add to Home screen**.

## Install

1. On the phone, download the latest `.apk` from the [releases page](https://github.com/ianpilon/clewbert/releases) and open it (Android asks once to allow installs from your browser).
2. On your boards page, open **Settings** (the gear). Under **Phone widget** it shows the **Address** and the **Token**.
3. Open the **Clewbert widget** app, enter both, tap **Test** (it should print "All quiet" or what the agents are doing), then **Save**.
4. Tap **Add the bar to my home screen** and confirm. You can also long-press the home screen, choose Widgets, and look for Clewbert widget.

The phone must be able to reach the address: the same Wi-Fi as the computer running Clewbert, a tunnel, or a server on the internet. If the page sits behind Cloudflare Access, the setup screen also takes a Cloudflare service token (Zero Trust > Access > Service auth), allowed on that Access app.

The token lives in your data folder as `widget-token`. Delete the file and restart Clewbert to make a new one.

## Build it yourself

Needs the Android SDK (platform 36, build-tools 36) and Java 17 or newer. `local.properties` must point at the SDK: `sdk.dir=/path/to/Android/sdk`.

```
./gradlew assembleDebug        # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease      # signed with ~/.config/canvas-widget/signing.properties when present
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Handy from a computer with `adb`: prefill and save the setup in one go, then ask the launcher to add the bar:

```
adb shell am start -n ca.voiceclaw.canvaswidget/.SetupActivity --es base https://boards.example.com --es token YOURTOKEN --es title "Ask Clewbert" --ez save true
adb shell am start -n ca.voiceclaw.canvaswidget/.PinActivity
```
